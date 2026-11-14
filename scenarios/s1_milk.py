"""s1 milk (after Momin's s1_milk): 1,000 L lactose-free milk at Hotel Aurora by Monday, live offers,
signed quote verified against JWKS by a third party, policy decision with reasons, audit trail, MCP surface."""
from __future__ import annotations

import asyncio

from scenarios._common import Client, Run, main, next_monday, verify_signed


def run(r: Run) -> None:
    c = Client(r.base, "hotel-aurora")
    day = next_monday()
    plan = c.plan("lactose-free milk", 1000, "L", day, claims=["FREE_FROM_LACTOSE"], preferred=["463038"])
    r.check("plan covers 1000 L", plan["shortfall"] == 0, f"shortfall {plan['shortfall']}")
    r.check("every plan line keeps FREE_FROM_LACTOSE", all(
        "FREE_FROM_LACTOSE" in c.j("GET", f"/v1/products/{l['product_id']}")["claims"] for l in plan["lines"]))
    r.check("good/better/best candidates with reasons", plan["candidates"] and all(x["reasons"] for x in plan["candidates"]))
    usual = next((x for x in plan["candidates"] if x["product"]["id"] == "463038"), None)
    if usual and usual["tier"] == "rejected":
        r.say(f"usual 463038 rejected: {usual['reasons'][0][:100]}")
    offers = c.j("POST", "/v1/offers", json={"product_ids": [l["product_id"] for l in plan["lines"]],
                                             "requested_date": str(day), "customer_id": "hotel-aurora"})
    r.check("offers carry live price + provenance", all(o["unit_price"] is not None and o.get("provenance") for o in offers))
    q = c.quote_ok(plan["lines"], day, plan["need"])
    v = verify_signed(r.base, q)
    r.check("quote signed and verifies against /.well-known/jwks.json", v["ok"], v["reasons"])
    r.check("quote has a price lock and availability snapshot", q["expires_at"] and q["availability_snapshot"])
    o = c.order(q).json()
    r.check("order has a policy decision", o["decision"]["outcome"] in ("auto_approve", "needs_approval", "reject"),
            o["decision"]["outcome"])
    r.check("decision has rule hits", len(o["decision"]["hits"]) > 0)
    r.say(f"order {o['order_id']}: {o['status']} ({o['decision']['outcome']}, {o['decision'].get('approver_role')})")
    ev = c.audit(o["order_id"])
    r.check("audit trail has >= 3 steps with summaries", len(ev) >= 3 and all(e["summary"] for e in ev),
            sorted({e["action"] for e in ev}))
    report = c.call("GET", f"/v1/audit/report/{o['order_id']}").text
    r.check("readable audit report", "Chain verified" in report or len(report) > 200, report[:120])

    async def tools() -> set[str]:
        from mcp import Client as McpClient
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"})
        async with http:
            async with McpClient(streamable_http_client(r.base + "/mcp", http_client=http)) as mc:
                return {t.name for t in (await mc.list_tools()).tools}

    try:
        names = asyncio.run(tools())
    except Exception as e:  # noqa: BLE001
        names = set()
        r.say(f"MCP client error: {type(e).__name__}: {e}"[:200])
    want = {"search_products", "get_offers", "plan_need", "create_quote", "place_order", "get_audit_trail",
            "plan_recipe", "check_standing_order_drift", "propose_offer"}
    r.check("MCP lists core + ported tools", want <= names, sorted(want - names))


if __name__ == "__main__":
    main("s1_milk", run)
