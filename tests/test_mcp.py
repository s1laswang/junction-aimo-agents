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


def test_decide_approval_roles(client, agent_key):
    init(client)
    args = {"name": "decide_approval", "arguments": {"order_id": "nope", "approve": True, "by": "x"}}
    out = rpc(client, "tools/call", args, {"X-Agent-Key": agent_key}, id=5)
    assert out["result"]["isError"] and "forbidden" in json.dumps(out["result"]["content"])
    out = rpc(client, "tools/call", args, {"X-Admin-Key": "demo-admin"}, id=6)
    assert out["result"]["isError"] and "NOT_FOUND" in json.dumps(out["result"]["content"])
    out = rpc(client, "tools/call", args, id=7)
    assert "unauthorized" in json.dumps(out["result"]["content"])


def test_parse_audited(client, agent_key):
    r = client.post("/v1/needs/parse", json={"text": "I need 100 litres of milk by Friday", "customer": "hotel-aurora"},
                    headers={"X-Agent-Key": agent_key})
    assert r.status_code == 200
    ev = client.get("/v1/audit?customer=hotel-aurora", headers={"X-Admin-Key": "demo-admin"}).json()["events"]
    e = [x for x in ev if x["action"] == "parse.request"][-1]
    assert e["actor"] == "agent:buyer-aurora" and "milk" in e["why"]


def test_offers_audited(client, agent_key):
    need = client.post("/v1/needs/parse", json={"text": "I need 100 litres of lactose-free milk by Friday", "customer": "hotel-aurora"}).json()
    r = client.post("/v1/offers", json=need, headers={"X-Agent-Key": agent_key})
    assert r.status_code == 200, r.text
    ev = client.get("/v1/audit?customer=hotel-aurora", headers={"X-Admin-Key": "demo-admin"}).json()["events"]
    e = [x for x in ev if x["action"] == "offers.compared"][-1]
    assert "Compared" in e["why"] and "shortfall" in e["why"]


def _stdio_call(env, name, args):
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "aimo.mcp_server"], env={**os.environ, **env})
        async with stdio_client(params) as st:
            async with ClientSession(st[0], st[1]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                return names, await s.call_tool(name, args)
    return asyncio.run(go())


def test_stdio(tmp_db):
    names, res = _stdio_call({"AIMO_AGENT_KEY": "demo-hotel-aurora", "AIMO_DB": tmp_db}, "get_mandate", {})
    assert names == EXPECTED
    assert not res.is_error and "hotel-aurora" in json.dumps([c.model_dump() for c in res.content])
    names, res = _stdio_call({"AIMO_AGENT_KEY": "", "AIMO_DB": tmp_db}, "get_mandate", {})
    assert res.is_error


def test_uvicorn_mcp_with_web(tmp_db):
    import os, socket, subprocess, sys, time
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "aimo.app:app", "--port", str(port), "--log-level", "warning"],
                            env={**os.environ, "AIMO_DB": tmp_db})
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        assert httpx.get(f"http://127.0.0.1:{port}/").status_code == 200

        async def go():
            async with create_mcp_http_client(headers={"X-Agent-Key": "demo-hotel-aurora"}) as http:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as st:
                    async with ClientSession(st[0], st[1]) as sess:
                        await sess.initialize()
                        return {t.name for t in (await sess.list_tools()).tools}
        assert asyncio.run(go()) == EXPECTED
    finally:
        proc.terminate()
        proc.wait(10)


def test_parse_summary(client):
    r = client.post("/v1/needs/parse", json={"text": "I need 100 litres of milk by Friday", "customer": "hotel-aurora"}).json()
    assert "milk" in r["summary"] and r["items"]
    init(client)
    out = rpc(client, "tools/call", {"name": "parse_request", "arguments": {"text": "100 litres of milk by Friday", "customer": "hotel-aurora"}}, id=9)
    assert "summary" in json.dumps(out["result"]["content"])
