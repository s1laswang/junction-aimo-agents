import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def run_scenario(c, r, base, day=None, watch_fn=None, violates_fn=None):
    day = day or next_monday()
    if watch_fn is None:
        from agents import drift
        watch_fn = drift.watch_orders
    if violates_fn is None:
        violates_fn = violates
    m = c.j("GET", "/v1/mandate")
    r.say("order placed, supplier stock-out mid-week: system reacts server-side and asks a human")
    need = c.parse(f"I need 200 litres of lactose-free milk by {day}", "hotel-aurora")
    q = c.quote(best_picks_covering(c.offers(need)["items"][0]), day)
    o = c.order(q)
    if o["status"] == "pending_approval":
        o = c.decide(o["order_id"], True, note="initial ok")
    r.check("order placed before fault", o["status"] in ("placed", "approved", "confirmed"), o["status"])
    pid = o["lines"][0]["product_id"]
    simulate(c, "stockout", product_id=pid, available_amount=0)
    try:
        after = c.j("GET", f"/v1/orders/{o['order_id']}")
        r.check("same order moved to pending_approval", after["status"] == "pending_approval", after["status"])
        reasons = after["decision"]["reasons"]
        r.check("decision has STOCKOUT reason with text", any(x["code"] == "STOCKOUT" and x.get("text") for x in reasons), str(reasons))
        subs = after["substitutions"]
        split = len(after["lines"]) > len(o["lines"]) or after["lines"] != o["lines"]
        r.check("substitution or split present", bool(subs) or split, f"{len(subs)} subs, {len(after['lines'])} lines")
        excl = m.get("exclude_allergens", [])
        r.check("substitutes and lines allergen-safe", not any(violates_fn(x["substitute_id"], excl) for x in subs) and not any(violates_fn(l["product_id"], excl) for l in after["lines"]))
        r.check("substitutions carry a reason", all(x.get("reason") for x in subs))
        ev = c.audit(o["order_id"])
        ev = ev["events"] if isinstance(ev, dict) and "events" in ev else ev
        stock = [e for e in ev if "stock" in e["action"].lower()]
        r.check("audit has a stockout event with a why", bool(stock) and all(e.get("why") for e in stock), str(sorted({e["action"] for e in ev})))
        before_n = len(c.j("GET", "/v1/orders", params={"customer": "hotel-aurora"}))
        with tempfile.TemporaryDirectory() as td:
            res = watch_fn("hotel-aurora", KEYS["hotel-aurora"], base, state_path=os.path.join(td, "d.json"))
        after_n = len(c.j("GET", "/v1/orders", params={"customer": "hotel-aurora"}))
        mine = [x for x in res if x["order_id"] == o["order_id"]]
        r.check("watcher reports stockout_handled", bool(mine) and all(x.get("kind") == "stockout_handled" and x["new_order_id"] is None for x in mine), str(mine)[:120])
        r.check("watcher places no duplicate order", after_n == before_n and not any(x.get("new_order_id") for x in res), f"{before_n}->{after_n}")
    finally:
        clear_simulation(c)
    done = c.decide(o["order_id"], True, note="approve reaction")
    r.check("human approval -> placed", done["status"] in ("placed", "approved", "confirmed"), done["status"])


def go():
    b = base or parse_base()
    run = r or Run("s4_stockout", b)
    run_scenario(Client(b, "hotel-aurora"), run, b)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s4_stockout", base)
    r.guard(go)
