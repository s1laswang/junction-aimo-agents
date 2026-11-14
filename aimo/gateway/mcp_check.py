"""Tiny MCP client check: list tools and call search_products + get_offers.

    uv run python -m aimo.gateway.mcp_check                       # http://localhost:8000/mcp
    uv run python -m aimo.gateway.mcp_check http://host:8011/mcp
    uv run python -m aimo.gateway.mcp_check stdio                 # spawns python -m aimo.gateway.mcp_server
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


def _text(res) -> str:
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc:
        return json.dumps(sc, ensure_ascii=False)[:600]
    return " ".join(getattr(c, "text", "") for c in (res.content or []))[:600]


async def main(target: str) -> None:
    server = (StdioServerParameters(command=sys.executable, args=["-m", "aimo.gateway.mcp_server"])
              if target == "stdio" else target)
    async with Client(server) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools.tools]
        print(f"{len(names)} tools:", ", ".join(names))
        res = await client.call_tool("search_products", {"query": "lactose free milk 1 l", "limit": 3})
        print("search_products ->", _text(res))
        from aimo.backend.clock import next_delivery_day
        res = await client.call_tool("get_offers", {"product_ids": ["463038", "464306"],
                                                    "requested_date": next_delivery_day().isoformat()})
        print("get_offers ->", _text(res))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"))
