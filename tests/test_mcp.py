import asyncio
import json

import pytest

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
EXPECTED = {"search_products", "get_product", "parse_request", "get_offers", "request_quote", "place_order",
            "get_order", "list_orders", "list_approvals", "decide_approval", "get_audit_trail", "get_mandate"}


def rpc(c, method, params=None, headers=None, id=1):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}, headers={**H, **({"mcp-session-id": rpc.sid} if getattr(rpc, "sid", None) and method != "initialize" else {}), **(headers or {})})
    assert r.status_code == 200, r.text
    rpc.sid = r.headers.get("mcp-session-id", getattr(rpc, "sid", None))
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(l[6:] for l in body.splitlines() if l.startswith("data: "))
    return json.loads(body)


def init(c):
    rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={**H, "mcp-session-id": rpc.sid})


def test_list_tools_object():
    from aimo.mcp_server import mcp
    assert {t.name for t in asyncio.run(mcp.list_tools())} == EXPECTED


def test_tools_list_over_http(client):
    init(client)
    out = rpc(client, "tools/list", id=2)
    assert {t["name"] for t in out["result"]["tools"]} == EXPECTED


def test_call_needs_key(client):
    init(client)
    out = rpc(client, "tools/call", {"name": "get_mandate", "arguments": {}}, id=3)
    assert out["result"]["isError"]
    assert "unauthorized" in json.dumps(out["result"]["content"])


def test_call_with_key(client, agent_key):
    init(client)
    out = rpc(client, "tools/call", {"name": "get_mandate", "arguments": {}}, {"X-Agent-Key": agent_key}, id=4)
    assert not out["result"].get("isError")
    assert "hotel-aurora" in json.dumps(out["result"])
