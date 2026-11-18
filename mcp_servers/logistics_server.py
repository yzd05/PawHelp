"""Run: uv run python -m mcp_servers.logistics_server."""

import json
import os
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from mcp_servers.scenario import delay, read_scenario

mcp = FastMCP(
    "PawHelp logistics demo",
    host="127.0.0.1",
    port=int(os.environ.get("MCP_LOGISTICS_PORT", "8101")),
    json_response=True,
    stateless_http=True,
)
_LOCAL = ZoneInfo("Asia/Shanghai")


def local_text(utc_value: str | None) -> str | None:
    """Same wording as the order cards, so the model copies times instead of converting UTC."""
    if not utc_value:
        return utc_value
    formatted = datetime.fromisoformat(utc_value).astimezone(_LOCAL).strftime("%Y/%m/%d %H:%M:%S")
    return formatted + "（香港/北京时间）"


@mcp.tool()
async def query_logistics(
    tracking_no: Annotated[str, Field(min_length=1, max_length=64)],
    ctx: Context,
) -> str:
    """用查询订单返回的运单号查询演示物流轨迹；仅当前用户的演示运单。时间已是香港/北京时间，回答时原样引用。"""
    scenario = read_scenario(ctx)
    if scenario.get("tracking_no") != tracking_no:
        raise ValueError("运单与可信场景不匹配")
    await delay()
    # All critical states/dates come from one persisted fixture. No second random truth.
    keys = (
        "order_id",
        "tracking_no",
        "carrier",
        "status",
        "status_label",
        "location",
        "trace",
        "updated_at",
        "estimated_delivery_at",
        "is_demo",
    )
    result = {k: scenario[k] for k in keys if k in scenario}
    for key in ("updated_at", "estimated_delivery_at"):
        if key in result:
            result[key] = local_text(result[key])
    if "trace" in result:
        result["trace"] = [
            {**event, "occurred_at": local_text(event.get("occurred_at"))}
            for event in result["trace"]
        ]
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
