from _common import *

base = parse_base()
r = Run("s1_milk", base)


def go():
    r.say("hotel-aurora needs 1000 L lactose-free milk by Monday")
    c = Client(base, "hotel-aurora")
    day = next_monday()
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
        verify_jws(base, q["jws"])
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
        import asyncio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def run():
            async with streamablehttp_client(base + "/mcp", headers={"X-Agent-Key": KEYS["hotel-aurora"]}) as (rd, wr, _):
                async with ClientSession(rd, wr) as s:
                    await s.initialize()
                    tools = {t.name for t in (await s.list_tools()).tools}
                    return tools, await s.call_tool("get_offers", need)

        tools, res = asyncio.run(run())
        r.check("MCP lists all tools", {"get_offers", "request_quote", "place_order", "get_audit_trail"} <= tools, str(tools))
        r.check("MCP get_offers returns offers", not res.isError)
    except Exception as e:
        r.check("MCP get_offers", False, f"{type(e).__name__}: {e}"[:150])


r.guard(go)
