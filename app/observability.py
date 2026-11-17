"""One graph-level and background tracing seam; actual usage is independent of trace export."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, AbstractContextManager, ExitStack, asynccontextmanager, suppress
from contextvars import ContextVar, Token
import inspect
import ipaddress
import logging
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables import Runnable as LCRunnable

from app.database.models import RequestObservation

logger = logging.getLogger(__name__)
_active: ContextVar[dict[str, Any] | None] = ContextVar("pawhelp_observation", default=None)

_CLOUD_LANGFUSE_SUFFIXES = (".langfuse.com", ".langfuse.dev")
_CLOUD_LANGFUSE_HOSTS = {"langfuse.com", "langfuse.dev"}
_INTERNAL_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".lan",
    ".home.arpa",
    ".svc",
    ".cluster.local",
    ".docker",
    ".corp",
    ".intranet",
)


def is_allowed_self_hosted_langfuse_url(raw_url: str) -> bool:
    """Allow loopback, private IPs, and container/internal hostnames; reject public cloud domains."""

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in _CLOUD_LANGFUSE_HOSTS or host.endswith(_CLOUD_LANGFUSE_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Allow single-label Docker service names (e.g. langfuse-web) and private internal domains.
        return "." not in host or host.endswith(_INTERNAL_HOST_SUFFIXES)
    return address.is_loopback or address.is_private


def _reset_context_token(var: ContextVar[Any], token: Token[Any]) -> None:
    """Safely reset a ContextVar even if an async generator is finalized in another context."""

    try:
        var.reset(token)
    except ValueError:
        logger.debug("observation ContextVar reset skipped across async context boundary")


async def invoke_with_callbacks(runnable: Any, payload: Any, callbacks: list[Any]) -> Any:
    """Attach callbacks when supported by the runnable without breaking minimal test doubles."""

    if not callbacks:
        return await runnable.ainvoke(payload)
    if isinstance(runnable, LCRunnable):
        return await runnable.ainvoke(payload, config={"callbacks": callbacks})
    try:
        parameters = inspect.signature(runnable.ainvoke).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_config = "config" in parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    if accepts_config:
        return await runnable.ainvoke(payload, config={"callbacks": callbacks})
    return await runnable.ainvoke(payload)


class UsageCollector(AsyncCallbackHandler):
    """Collect provider token usage per LLM run with source attribution."""

    def __init__(self, model: str, *, source: str = "chat") -> None:
        self.model = model
        self.source = source
        self.rows: list[dict[str, Any]] = []
        self.seen: set[str] = set()

    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        run_key = str(run_id)
        if run_key in self.seen:
            return
        self.seen.add(run_key)
        usage = None
        for group in response.generations:
            for generation in group:
                candidate = getattr(getattr(generation, "message", None), "usage_metadata", None)
                usage = candidate or usage
        output = response.llm_output or {}
        usage = usage or output.get("token_usage") or output.get("usage")
        self.rows.append(
            {
                "run_id": run_key,
                "model": output.get("model_name", self.model),
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")) if usage else None,
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")) if usage else None,
                "status": "success",
                "source": self.source,
            }
        )

    async def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        run_key = str(run_id)
        if run_key not in self.seen:
            self.seen.add(run_key)
            self.rows.append(
                {
                    "run_id": run_key,
                    "model": self.model,
                    "input_tokens": None,
                    "output_tokens": None,
                    "status": "failed",
                    "source": self.source,
                }
            )


class _RequestObservationScope(
    AbstractAsyncContextManager[dict[str, Any]],
    AbstractContextManager[dict[str, Any]],
):
    """Async-first observation scope that also supports synchronous `with` callers."""

    def __init__(self, async_cm_factory: Any) -> None:
        self._factory = async_cm_factory
        self._async_cm: AbstractAsyncContextManager[dict[str, Any]] | None = None
        self._sync_stack: ExitStack | None = None
        self._sync_token: Token[dict[str, Any] | None] | None = None

    async def __aenter__(self) -> dict[str, Any]:
        self._async_cm = self._factory()
        return await self._async_cm.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        if self._async_cm is None:
            return False
        return await self._async_cm.__aexit__(exc_type, exc, tb)

    def __enter__(self) -> dict[str, Any]:
        return self._factory.sync_enter(self)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self._factory.sync_exit(self, exc_type, exc, tb)


class Observability:
    """Manage Langfuse tracing and local MySQL usage ledger persistence."""

    def __init__(self, settings: Any, session_factory: Any) -> None:
        self.settings = settings
        self.sessions = session_factory
        self.client = None
        self.enabled = bool(settings.observability_enabled)
        if self.enabled and settings.langfuse_enabled:
            if not is_allowed_self_hosted_langfuse_url(settings.langfuse_base_url):
                raise ValueError("LANGFUSE_BASE_URL must explicitly point to your local/private self-hosted server")
            if (
                not settings.langfuse_public_key.get_secret_value()
                or not settings.langfuse_secret_key.get_secret_value()
            ):
                raise ValueError("self-hosted Langfuse keys are required when enabled")
            from langfuse import Langfuse

            self.client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
            )

    def _build_record(
        self,
        *,
        intent: str = "未识别",
        source: str = "chat",
        model_name: str | None = None,
    ) -> dict[str, Any]:
        collector = UsageCollector(model_name or self.settings.openai_model, source=source)
        callbacks: list[Any] = [collector] if self.enabled else []
        if self.client:
            from langfuse.langchain import CallbackHandler

            callbacks.append(CallbackHandler(public_key=self.settings.langfuse_public_key.get_secret_value()))
        return {
            "id": str(uuid4()),
            "intent": intent,
            "source": source,
            "collector": collector,
            "callbacks": callbacks,
            "status": "success",
            "started": perf_counter(),
            "span": None,
            "trace_id": None,
        }

    def _enter_span_stack(self, stack: ExitStack, record: dict[str, Any], turn: Any) -> None:
        if not self.client:
            return
        span = stack.enter_context(
            self.client.start_as_current_observation(
                name="customer-service",
                as_type="agent",
                input={"question": turn.raw_question},
            )
        )
        record["span"] = span
        record["trace_id"] = span.trace_id if span else None
        from langfuse import propagate_attributes

        stack.enter_context(
            propagate_attributes(
                session_id=str(turn.conversation_id),
                user_id=str(turn.user_id),
                trace_name="customer-service",
            )
        )

    @asynccontextmanager
    async def _async_request(self, turn: Any):
        record = self._build_record(intent="未识别", source="chat")
        token = _active.set(record)
        try:
            with ExitStack() as stack:
                self._enter_span_stack(stack, record, turn)
                yield record
        finally:
            _reset_context_token(_active, token)

    def request(self, turn: Any) -> _RequestObservationScope:
        """Observe one conversational turn safely across async streaming generators."""

        def factory() -> AbstractAsyncContextManager[dict[str, Any]]:
            return self._async_request(turn)

        def sync_enter(scope: _RequestObservationScope) -> dict[str, Any]:
            record = self._build_record(intent="未识别", source="chat")
            scope._sync_token = _active.set(record)
            scope._sync_stack = ExitStack()
            try:
                self._enter_span_stack(scope._sync_stack, record, turn)
            except BaseException:
                scope._sync_stack.close()
                _reset_context_token(_active, scope._sync_token)
                raise
            return record

        def sync_exit(scope: _RequestObservationScope, exc_type: Any, exc: Any, tb: Any) -> bool:
            try:
                if scope._sync_stack is not None:
                    scope._sync_stack.__exit__(exc_type, exc, tb)
            finally:
                if scope._sync_token is not None:
                    _reset_context_token(_active, scope._sync_token)
            return False

        factory.sync_enter = sync_enter  # type: ignore[attr-defined]
        factory.sync_exit = sync_exit  # type: ignore[attr-defined]
        return _RequestObservationScope(factory)

    @asynccontextmanager
    async def background(
        self,
        *,
        source: str,
        intent: str,
        conversation_id: int | None = None,
        model_name: str | None = None,
    ):
        """Track token consumption of background tasks (summaries, titles, etc.) by source."""

        record = self._build_record(intent=intent[:32], source=source, model_name=model_name)
        try:
            yield record
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception:
            record["status"] = "failed"
            raise
        finally:
            if self.enabled and record["collector"].rows:
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(
                        self._persist_observation(
                            record,
                            conversation_id=conversation_id,
                            user_message_id=None,
                        )
                    )

    async def save(self, record: dict[str, Any], turn: Any) -> None:
        if not self.enabled:
            return
        await self._persist_observation(
            record,
            conversation_id=getattr(turn, "conversation_id", None),
            user_message_id=getattr(turn, "user_message_id", None),
        )

    async def _persist_observation(
        self,
        record: dict[str, Any],
        *,
        conversation_id: int | None,
        user_message_id: int | None,
    ) -> None:
        if not self.enabled or self.sessions is None:
            return
        try:
            session_cm = self.sessions()
            if session_cm is None:
                return
            async with session_cm as session, session.begin():
                session.add(
                    RequestObservation(
                        id=record["id"],
                        conversation_id=conversation_id,
                        user_message_id=user_message_id,
                        intent=record["intent"],
                        trace_id=record.get("trace_id"),
                        model_usage=record["collector"].rows,
                        duration_ms=int((perf_counter() - record["started"]) * 1000),
                        status=record["status"],
                    )
                )
        except Exception:
            logger.exception("observation persistence failed request=%s", record["id"])

    async def close(self) -> None:
        if self.client:
            await asyncio.to_thread(self.client.flush)


def record_intent(intent: str) -> None:
    record = _active.get()
    if record is not None:
        record["intent"] = intent
        if record.get("span"):
            record["span"].update(metadata={"intent": intent})


def finish_trace(record: dict[str, Any], state: dict[str, Any]) -> None:
    record_intent(state.get("intent", record["intent"]))
    if record.get("span"):
        from langfuse import propagate_attributes

        # Runs on the root request observation, so intent becomes trace metadata too.
        with propagate_attributes(metadata={"intent": record["intent"]}):
            record["span"].update(output={"answer": state.get("answer"), "intent": record["intent"]})
