"""Verify trusted demo snapshots; headers are NOT real customer authentication."""

from __future__ import annotations

import asyncio
import base64
from functools import lru_cache
import hashlib
import hmac
import json
import os
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DemoSettings(BaseSettings):
    mcp_demo_secret: SecretStr = SecretStr("local-demo-only-change-me")
    mock_delay_seconds: float = 0.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_demo_settings() -> DemoSettings:
    """Load and cache MCP demonstration settings once per process."""

    return DemoSettings()


def read_scenario(ctx: Any) -> dict[str, Any]:
    headers = ctx.request_context.request.headers
    payload = headers.get("x-pawhelp-scenario", "")
    signature = headers.get("x-pawhelp-signature", "")
    secret = get_demo_settings().mcp_demo_secret.get_secret_value().encode("utf-8")
    expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not payload or len(payload) > 16000 or not hmac.compare_digest(expected, signature):
        raise ValueError("未提供可信演示场景")
    value = json.loads(base64.urlsafe_b64decode(payload))
    if not isinstance(value, dict) or not value.get("is_demo"):
        raise ValueError("仅支持演示订单")
    return value


async def delay() -> None:
    raw = os.environ.get("MOCK_DELAY_SECONDS")
    configured = float(raw) if raw is not None else get_demo_settings().mock_delay_seconds
    await asyncio.sleep(max(0.0, min(120.0, configured)))
