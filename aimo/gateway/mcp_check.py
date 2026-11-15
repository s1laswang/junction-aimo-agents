"""Tiny MCP client check: list tools and call search_products + get_offers.

    uv run python -m aimo.gateway.mcp_check                       # http://localhost:8000/mcp
    uv run python -m aimo.gateway.mcp_check http://host:8011/mcp
    uv run python -m aimo.gateway.mcp_check stdio                 # spawns python -m aimo.gateway.mcp_server
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlparse

from mcp import Client
from mcp.client.stdio import StdioServerParameters

_MAX_TARGET_CHARS = 2048


def _has_bad_target_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if ch.isspace() or code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _validate_target(target: Any) -> str:
    if isinstance(target, bool) or not isinstance(target, str):
        raise ValueError("target must be 'stdio' or an http(s) URL")
    if not target or len(target) > _MAX_TARGET_CHARS:
        raise ValueError("target is empty or exceeds maximum length")
    if _has_bad_target_char(target):
        raise ValueError("target must not contain whitespace or control characters")
    if target == "stdio":
        return "stdio"
    if not target.startswith(("http://", "https://")):
        raise ValueError("target must be 'stdio' or start with http:// or https://")
    parsed = urlparse(target)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not host:
        raise ValueError("target URL must include scheme and host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target URL must not contain credentials")
    if ".." in host or host.startswith("."):
        raise ValueError("target URL host is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("target URL has an invalid port") from exc
    if port is not None and (port < 1 or port > 65535):
        raise ValueError("target URL port out of range")
    return target


def _text(res: Any) -> str:
    if res is None:
        return ""
    sc = getattr(res, "structured_content", None)
    if sc is None:
        sc = getattr(res, "structuredContent", None)
    raw_content = getattr(res, "content", None)
    if sc is not None and (sc or not raw_content):
        try:
            return json.dumps(sc, ensure_ascii=False, default=str)[:600]
        except (TypeError, ValueError, OverflowError):
            return str(sc)[:600]
    if not isinstance(raw_content, (list, tuple)):
        return ""
    parts: list[str] = []
    for c in raw_content:
        if c is None:
            continue
        txt = getattr(c, "text", None)
        if txt is not None:
            parts.append(txt if isinstance(txt, str) else str(txt))
    return " ".join(parts)[:600]


async def main(target: str = "http://localhost:8000/mcp") -> None:
    clean_target = _validate_target(target)
    server = (StdioServerParameters(command=sys.executable, args=["-m", "aimo.gateway.mcp_server"])
              if clean_target == "stdio" else clean_target)
    async with Client(server) as client:
        tools = await client.list_tools()
        tool_items = getattr(tools, "tools", None) or []
        names = [str(getattr(t, "name", "")) for t in tool_items if getattr(t, "name", None)]
        print(f"{len(names)} tools:", ", ".join(names))
        res = await client.call_tool("search_products", {"query": "lactose free milk 1 l", "limit": 3})
        print("search_products ->", _text(res))
        from aimo.backend.clock import next_delivery_day
        res = await client.call_tool("get_offers", {"product_ids": ["463038", "464306"],
                                                    "requested_date": next_delivery_day().isoformat()})
        print("get_offers ->", _text(res))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print("usage: python -m aimo.gateway.mcp_check [URL|stdio]", file=sys.stderr)
        raise SystemExit(2)
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
