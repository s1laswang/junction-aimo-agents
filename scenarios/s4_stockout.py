"""s4 stock-out (after Momin's s4_stockout, using our X-Aimo-Faults instead of his /v1/admin/simulate):
an order is placed; the supplier then runs out. The next quote is refused per line with a reason, the
planner re-plans around the stock-out, and the standing-order watcher flags it in plain language."""
from __future__ import annotations

from scenarios._common import Client, Run, main, next_monday


def run(r: Run) -> None:
    day = next_monday()
    c = Client(r.base, "hotel-aurora")
    plan = c.plan("lactose-free milk", 200, "L", day, claims=["FREE_FROM_LACTOSE"], preferred=["463038"])
    q = c.quote_ok(plan["lines"], day, plan["need"])
    o = c.order(q).json()
    if o["status"] == "pending_approval":
        o = c.approve(o, q["quote_hash"], True, "initial ok")
    r.check("order placed before the fault", o["status"] == "placed", o["status"])
    pid = q["lines"][0]["product_id"]
    cf = Client(r.base, "hotel-aurora", faults=f"stockout:{pid}")
    res = cf.quote(q["lines"], day)
    body = res.json()
    r.check("re-quote refused 409 lines_unavailable", res.status_code == 409 and body.get("error") == "lines_unavailable",
            res.status_code)
    refused = (body.get("detail") or {}).get("refused", [])
    r.check("refusal names the product and the reason", any(x["product_id"] == pid and x["reason"] for x in refused), refused)
    p2 = cf.plan("lactose-free milk", 200, "L", day, claims=["FREE_FROM_LACTOSE"], preferred=["463038"])
    r.check("re-plan avoids the stocked-out product", all(l["product_id"] != pid for l in p2["lines"]) and p2["lines"],
            [l["product_id"] for l in p2["lines"]])
    r.check("re-plan still covers 200 L", p2["shortfall"] == 0, p2["shortfall"])
    q2 = cf.quote_ok(p2["lines"], day, p2["need"])
    o2 = cf.order(q2).json()
    r.check("replacement order goes through policy", o2["status"] in ("placed", "pending_approval"), o2["status"])
    r.say(f"replacement {o2['order_id']}: {o2['status']} ({', '.join(l['product_id'] for l in q2['lines'])})")
    egg = "463982"
    dw = Client(r.base, "hotel-aurora", faults=f"stockout:{egg}").j("GET", "/v1/drift/hotel-aurora")
    f = [x for x in dw["flags"] if x["product_id"] == egg and x["kind"] == "availability"]
    r.check("standing-order watcher flags the stock-out in plain language", f and "Eggs" in f[0]["message"], dw["flags"])
    if f:
        r.say(f[0]["message"])
    r.check("audit has the refused quote", any(e["action"] == "quote.refused" for e in c.j("GET", "/v1/audit")))


if __name__ == "__main__":
    main("s4_stockout", run)
