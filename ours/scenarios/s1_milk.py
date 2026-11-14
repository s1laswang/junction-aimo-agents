import inspect, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def _mcp_tools(base):
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def tools():
        async with create_mcp_http_client(headers={"X-Agent-Key": KEYS["hotel-aurora"]}) as http:
            async with streamable_http_client(base + "/mcp", http_client=http) as st:
                async with ClientSession(st[0], st[1]) as sess:
                    await sess.initialize()
                    return {t.name for t in (await sess.list_tools()).tools}

    return asyncio.run(tools())


def _call_mcp_tools(fn, base):
    try:
        has_args = bool(inspect.signature(fn).parameters)
    except (ValueError, TypeError):
        has_args = True
    return set(fn(base) if has_args else fn())


def run_scenario(c, r, base, day=None, verify_fn=verify_jws, mcp_tools_fn=None, buyer_run_fn=None):
    base = (base or "").rstrip("/")
    day = day or next_monday()
    r.say("hotel-aurora needs 1000 L lactose-free milk by Monday")
    text = f"I need 1000 litres of lactose-free milk by {day}. Find an option that meets our requirements based on price and availability."
    need = c.parse(text, "hotel-aurora")
    item = need["items"][0]
    r.check("parsed 1000 L lactose-free", item["quantity"] == 1000 and item["constraints"].get("lactose_free") is True, str(item))
    offers = c.offers(need)
    it = offers["items"][0]
    offs = it["offers"]
    r.check("offers returned", len(offs) > 0)
    r.check("offers cover 1000 L", it["shortfall"] == 0, f"shortfall {it['shortfall']}")
    r.check("offers carry price and availability", all(o["unit_price_net"] is not None and o["available_amount"] is not None for o in offs))
    q = c.quote(best_picks_covering(it), day)
    hdr, _ = decode_jws(q["jws"])
    r.check("quote signed (EdDSA)", hdr.get("alg") == "EdDSA", str(hdr))
    try:
        verify_fn(base, q["jws"])
        ok = True
    except Exception:
        ok = False
    r.check("jws verifies against jwks.json", ok)
    o = c.order(q)
    r.check("order has Decision", o["decision"]["outcome"] in ("auto", "needs_approval"), str(o["decision"]))
    r.check("decision has reasons", len(o["decision"]["reasons"]) > 0)
    ev = c.audit(o["order_id"])
    events = ev["events"] if isinstance(ev, dict) and "events" in ev else ev
    r.check("audit trail has steps", len(events) >= 3, str(sorted({e["action"] for e in events})))
    r.check("every audit event has why", all(e.get("why") for e in events))
    try:
        names = _mcp_tools(base) if mcp_tools_fn is None else _call_mcp_tools(mcp_tools_fn, base)
        r.check("MCP lists all tools", {"get_offers", "request_quote", "place_order", "get_audit_trail"} <= names, str(names))
        if buyer_run_fn is None:
            from agents import buyer
            buyer_run_fn = buyer.run
        out = buyer_run_fn(text, "hotel-aurora", KEYS["hotel-aurora"], base + "/mcp")
        o2 = out.get("order") or {}
        r.check("MCP buyer agent gets an order with Decision", bool(o2.get("decision", {}).get("outcome")), str(out.get("log", ""))[-150:])
    except Exception as e:
        r.check("MCP leg", False, f"{type(e).__name__}: {e}"[:150])


def go():
    b = base or parse_base()
    run = r or Run("s1_milk", b)
    run_scenario(Client(b, "hotel-aurora"), run, b)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s1_milk", base)
    r.guard(go)
