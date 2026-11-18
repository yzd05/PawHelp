"""Run: uv run python -m mcp_servers.aftersales_server. No real aftersales backend."""

import json
import os
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from mcp_servers.scenario import delay, read_scenario

mcp = FastMCP(
    "PawHelp aftersales demo",
    host="127.0.0.1",
    port=int(os.environ.get("MCP_AFTERSALES_PORT", "8102")),
    json_response=True,
    stateless_http=True,
)
OrderId = Annotated[str, Field(min_length=1, max_length=64)]


async def scenario_for(order_id: str, ctx: Context) -> dict[str, Any]:
    value = read_scenario(ctx)
    if value.get("order_id") != order_id:
        raise ValueError("订单与可信场景不匹配")
    await delay()
    return value


@mcp.tool()
async def query_warranty(order_id: OrderId, ctx: Context) -> str:
    """查询演示订单保修状态；不是店铺政策承诺。"""
    value = await scenario_for(order_id, ctx)
    signed = value.get("delivered_at")
    until = datetime.fromisoformat(signed) + timedelta(days=365) if signed else None
    in_warranty = bool(until and until > datetime.now(UTC))
    return json.dumps(
        {
            "order_id": order_id,
            "is_demo": True,
            "status": "在保" if in_warranty else "尚未起保或已过期",
            "warranty_until": until.isoformat() if until else None,
            "message": "演示服务按签收日起一年模拟保修，不代表真实商品保修政策。",
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def query_return_progress(order_id: OrderId, ctx: Context) -> str:
    """查询固定种子的演示退货进度，不执行退款，不代表退款申请表的真实处理状态。"""
    await scenario_for(order_id, ctx)
    status = Random("return-demo:" + order_id).choice(("暂无演示退货记录", "演示审核中", "演示待寄回"))
    return json.dumps(
        {
            "order_id": order_id,
            "is_demo": True,
            "status": status,
            "message": "独立模拟进度；不代表真实退款成功或本地退款申请已处理。",
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
