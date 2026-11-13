from _common import *

base = parse_base()
r = Run("s4_stockout", base)


def go():
    c = Client(base, "hotel-aurora")
    day = next_monday()
    r.say("order placed Monday, supplier stock-out Wednesday: system reacts and asks a human")
    need = c.parse(f"I need 200 litres of lactose-free milk by {day}", "hotel-aurora")
    offers = c.offers(need)
    q = c.quote(best_picks_covering(offers["items"][0]), day)
    o = c.order(q)
    if o["status"] == "pending_approval":
        o = c.decide(o["order_id"], True, "scenario-human", "initial ok")
    r.check("order placed before fault", o["status"] in ("placed", "approved", "confirmed"), o["status"])
    pid = o["lines"][0]["product_id"]
    sim = simulate(c, "stockout", product_id=pid, available_amount=0)
    r.check("fault injected", sim is not None)
    after = c.j("GET", f"/v1/orders/{o['order_id']}")
    pend = any(a["order_id"] == o["order_id"] for a in c.approvals())
    clear_simulation(c)
    changed = after["status"] != o["status"] or after["substitutions"] or after["lines"] != o["lines"]
    r.check("order reacted (split, substitute or new date)", bool(changed) or pend, after["status"])
    r.check("human asked before changing the order", pend or after["decision"]["outcome"] == "needs_approval", after["decision"]["outcome"])
    events = c.audit(o["order_id"])
    events = events["events"] if isinstance(events, dict) and "events" in events else events
    r.check("audit records the stock-out with a why", any("stock" in (e.get("why", "") + e.get("action", "")).lower() for e in events))
    for s in after["substitutions"]:
        r.check("substitute is a different product with reason", s["substitute_id"] != s["original_id"] and s["reason"], str(s))


r.guard(go)
