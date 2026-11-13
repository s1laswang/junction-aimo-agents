import tempfile
from _common import *

base = parse_base()
r = Run("s4_stockout", base)


def go():
    from agents import drift
    c = Client(base, "hotel-aurora")
    day = next_monday()
    r.say("order placed, supplier stock-out mid-week: drift watcher re-plans and asks a human")
    need = c.parse(f"I need 200 litres of lactose-free milk by {day}", "hotel-aurora")
    q = c.quote(best_picks_covering(c.offers(need)["items"][0]), day)
    o = c.order(q)
    if o["status"] == "pending_approval":
        o = c.decide(o["order_id"], True, note="initial ok")
    r.check("order placed before fault", o["status"] in ("placed", "approved", "confirmed"), o["status"])
    pid = o["lines"][0]["product_id"]
    simulate(c, "stockout", product_id=pid, available_amount=0)
    try:
        res = drift.watch_orders("hotel-aurora", KEYS["hotel-aurora"], base, state_path=os.path.join(tempfile.mkdtemp(), "d.json"))
    finally:
        clear_simulation(c)
    mine = [x for x in res if x["order_id"] == o["order_id"]]
    r.check("watcher flagged the affected order", bool(mine), str(len(res)))
    r.check("every flag has a why", all(x["why"] for x in mine))
    nid = next((x["new_order_id"] for x in mine if x["new_order_id"]), None)
    r.check("a replacement order was created", bool(nid))
    new = c.j("GET", f"/v1/orders/{nid}") if nid else {}
    r.check("replacement waits for a human", new.get("status") == "pending_approval", str(new.get("status")))
    reasons = new.get("decision", {}).get("reasons", [])
    r.check("reason AGENT_REQUESTED", any(x["code"] == "AGENT_REQUESTED" for x in reasons), str(reasons))
    r.check("note names the original order", any(o["order_id"] in x.get("text", "") for x in reasons))
    events = c.audit(nid) if nid else []
    events = events["events"] if isinstance(events, dict) and "events" in events else events
    r.check("audit has events with a why", bool(events) and all(e.get("why") for e in events))


r.guard(go)
