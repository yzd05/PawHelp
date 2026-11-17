"""Live tool snapshots: non-blocking local registration + cached MCP discovery with backoff."""

from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import hashlib
import hmac
import importlib.util
import json
import logging
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.types import CallToolResult, TextContent

from app.repositories.orders import OrderNotFound
from app.tools.business import ToolRuntime, build_business_tools
from app.tools.executor import TransientToolError
from app.tools.registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
_SERVERS = ("logistics", "aftersales")
_active_runtime: ContextVar[ToolRuntime | None] = ContextVar("pawhelp_mcp_runtime", default=None)


def _transport_failure(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(_transport_failure(item) for item in error.exceptions)
    return isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError))


def _bind_mcp_runtime(raw_tool: BaseTool, runtime: ToolRuntime) -> BaseTool:
    """Bind the turn's trusted ToolRuntime around a cached MCP tool without cross-turn state leakage."""

    async def invoke_bound(**kwargs: Any) -> Any:
        token = _active_runtime.set(runtime)
        try:
            return await raw_tool.ainvoke(kwargs)
        finally:
            _active_runtime.reset(token)

    return StructuredTool(
        name=raw_tool.name,
        description=raw_tool.description,
        args_schema=raw_tool.args_schema,
        coroutine=invoke_bound,
        response_format="content",
    )


class ToolCatalog:
    """Provide turn-scoped tool registries with non-blocking policy/plugin mtime caching and MCP TTL/backoff."""

    def __init__(
        self,
        settings: Any,
        *,
        builder: Any = build_business_tools,
        discovery_ttl_seconds: float = 30.0,
        failure_backoff_seconds: float = 10.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self.settings = settings
        self.builder = builder
        self.discovery_ttl_seconds = max(0.0, discovery_ttl_seconds)
        self.failure_backoff_seconds = max(0.0, failure_backoff_seconds)
        self.max_backoff_seconds = max(self.failure_backoff_seconds, max_backoff_seconds)
        self.plugins: dict[Path, tuple[tuple[int, int], Any]] = {}
        self._policy_cache: tuple[tuple[int, int] | None, dict[str, str]] = (None, {})
        self._mcp_cache: dict[str, tuple[float, list[BaseTool]]] = {}
        self._mcp_backoff: dict[str, tuple[float, int]] = {}
        self._client: MultiServerMCPClient | None = None

    def invalidate(self) -> None:
        """Clear cached MCP discovery state and backoff counters."""

        self._mcp_cache.clear()
        self._mcp_backoff.clear()

    def _load_plugins_sync(self) -> list[Any]:
        plugin_dir = Path(self.settings.tool_plugins_path)
        if not plugin_dir.is_absolute():
            plugin_dir = ROOT / plugin_dir
        if not plugin_dir.is_dir():
            return []
        modules: list[Any] = []
        for path in sorted(plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self.plugins.get(path)
                if cached is None or cached[0] != stamp:
                    definition = importlib.util.spec_from_file_location("pawhelp_plugin_" + path.stem, path)
                    if definition is None or definition.loader is None:
                        raise ValueError(f"cannot load plugin spec for {path.name}")
                    module = importlib.util.module_from_spec(definition)
                    definition.loader.exec_module(module)
                    self.plugins[path] = (stamp, module)
                modules.append(self.plugins[path][1])
            except Exception:
                logger.exception("local tool plugin rejected path=%s", path.name)
        return modules

    def _load_policy_sync(self) -> dict[str, str]:
        policy_path = Path(self.settings.tool_policy_path)
        if not policy_path.is_absolute():
            policy_path = ROOT / policy_path
        try:
            stat = policy_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._policy_cache[0] == stamp:
                return self._policy_cache[1]
            parsed = json.loads(policy_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("policy must be object")
            policy = {str(k): str(v) for k, v in parsed.items()}
            self._policy_cache = (stamp, policy)
            return policy
        except (ValueError, OSError):
            logger.exception("tool policy unavailable; MCP tools denied")
            self._policy_cache = (None, {})
            return {}

    def _load_local_artifacts_sync(self) -> tuple[list[Any], dict[str, str]]:
        modules = self._load_plugins_sync()
        policy = self._load_policy_sync() if self.settings.mcp_enabled else {}
        return modules, policy

    def _mcp_client(self) -> MultiServerMCPClient:
        if self._client is not None:
            return self._client

        async def inject_scenario(request: Any, handler: Any) -> Any:
            # Private server headers are built from owned DB snapshots, never model arguments.
            runtime = _active_runtime.get()
            if runtime is None or runtime.order_data is None:
                raise TransientToolError("MCP scenario runtime is unavailable")
            args = request.args
            orders = runtime.order_data
            try:
                if request.server_name == "logistics":
                    scenario = await orders.get_logistics(
                        runtime.user_id,
                        tracking_no=args.get("tracking_no"),
                        order_id=args.get("order_id"),
                    )
                else:
                    scenario = await orders.get_order(runtime.user_id, str(args.get("order_id", "")))
                if not scenario.get("is_demo", False):
                    raise ValueError("MCP demo servers cannot process real purchases")
            except OrderNotFound:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text='{"code":"order_not_found","error":"没有找到您的这笔订单或运单"}',
                        )
                    ]
                )
            payload = base64.urlsafe_b64encode(
                json.dumps(scenario, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            signature = hmac.new(
                self.settings.mcp_demo_secret.get_secret_value().encode("utf-8"),
                payload.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            headers = {"x-pawhelp-scenario": payload, "x-pawhelp-signature": signature}
            try:
                return await handler(request.override(headers=headers))
            except Exception as exc:
                if _transport_failure(exc):
                    raise TransientToolError() from exc
                raise

        self._client = MultiServerMCPClient(
            {
                "logistics": {"url": self.settings.mcp_logistics_url, "transport": "streamable_http"},
                "aftersales": {"url": self.settings.mcp_aftersales_url, "transport": "streamable_http"},
            },
            tool_interceptors=[inject_scenario],
        )
        return self._client

    async def _discover_server(self, server: str) -> list[BaseTool]:
        now = monotonic()
        cached = self._mcp_cache.get(server)
        if cached is not None and now < cached[0]:
            return cached[1]
        backoff = self._mcp_backoff.get(server)
        if backoff is not None and now < backoff[0]:
            return []
        client = self._mcp_client()
        try:
            async with asyncio.timeout(self.settings.mcp_discovery_timeout_seconds):
                tools = list(await client.get_tools(server_name=server))
        except Exception:
            previous_failures = backoff[1] if backoff is not None else 0
            failures = previous_failures + 1
            delay = (
                min(self.max_backoff_seconds, self.failure_backoff_seconds * (2 ** (failures - 1)))
                if self.failure_backoff_seconds > 0
                else 0.0
            )
            self._mcp_backoff[server] = (monotonic() + delay, failures)
            self._mcp_cache.pop(server, None)
            logger.warning(
                "MCP discovery unavailable server=%s failures=%s backoff=%.1fs; other tools remain usable",
                server,
                failures,
                delay,
                exc_info=True,
            )
            return []

        self._mcp_backoff.pop(server, None)
        if self.discovery_ttl_seconds > 0:
            self._mcp_cache[server] = (monotonic() + self.discovery_ttl_seconds, tools)
        else:
            self._mcp_cache.pop(server, None)
        return tools

    async def snapshot(self, runtime: ToolRuntime) -> ToolRegistry:
        registry = ToolRegistry(runtime=runtime)
        for tool in self.builder(runtime):
            # The public compatibility builder is not active in the conversation workflow.
            if tool.name not in {"query_logistics", "query_faq"}:
                registry.register(tool)

        plugin_modules, policy = await asyncio.to_thread(self._load_local_artifacts_sync)
        for module in plugin_modules:
            try:
                module.register(runtime, registry)
            except Exception:
                logger.exception("local tool plugin registration rejected module=%s", getattr(module, "__name__", "unknown"))

        if not self.settings.mcp_enabled:
            return registry

        discovered_lists = await asyncio.gather(*(self._discover_server(server) for server in _SERVERS))
        candidates = [
            ToolSpec(
                _bind_mcp_runtime(raw_tool, runtime),
                "mcp",
                server,
                policy.get(f"mcp:{server}:{raw_tool.name}", "deny"),
            )
            for server, tools in zip(_SERVERS, discovered_lists)
            for raw_tool in tools
        ]
        # An unknown denied name cannot shadow another server's locally approved tool.
        for spec in sorted(candidates, key=lambda candidate: candidate.permission == "deny"):
            try:
                registry.register(spec)
            except Exception:
                logger.warning("MCP tool rejected server=%s name=%s", spec.server, spec.tool.name, exc_info=True)
        logger.debug(
            "tool_catalog conversation=%s tools=%s",
            runtime.conversation_id,
            [
                (name, registry.spec(name).source, registry.spec(name).server, registry.spec(name).permission)
                for name in sorted(registry.names)
            ],
        )
        return registry
