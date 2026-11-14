import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def _has_candidates(it):
    return bool(it.get("offers")) and bool(it.get("alternatives") or len(it["offers"]) > 1)


def run_scenario(c, r, day=None):
    day = day or next_delivery_day()
    r.say("last-minute event on the next delivery day: 400 portions of pasta carbonara, item short -> recipe-logic substitution")
    need = c.parse(f"Last-minute event on the next delivery day, we need pasta carbonara for 400 portions by {day}", "hotel-aurora")
    r.check("recipe expanded to ingredient lines", len(need["items"]) >= 3, f"{len(need['items'])} lines")
    offers = c.offers(need)
    had_alts = any(_has_candidates(it) for it in offers["items"])
    simulated = False
    try:
        if not [o for it in offers["items"] for o in it["offers"] if is_substitute(o)]:
            target = next((it for it in offers["items"] if _has_candidates(it)), None)
            if target:
                simulated = True
                simulate(c, "stockout", product_id=target["offers"][0]["product_id"], available_amount=1)
                offers = c.offers(need)
        short = [it for it in offers["items"] if it.get("shortfall", 0) > 0 or any(o.get("notes") for o in it["offers"])]
        covered = [it for it in offers["items"] if it["offers"]]
        r.check("offers or substitutes found for lines", len(covered) >= len(need["items"]) // 2, f"{len(covered)}/{len(need['items'])}")
        subs = [o for it in offers["items"] for o in it["offers"] if is_substitute(o)]
        if had_alts or subs:
            r.check("at least one substitute offer", len(subs) >= 1, f"{len(subs)} substitutes")
        r.check("substitute offers say what they replace", all(o["substitute_for"] != o["product_id"] for o in subs), f"{len(subs)} substitutes, {len(short)} short lines")
        picks = [pick_of(o, o["units_needed"]) for it in offers["items"] for o in it["offers"] if o["units_needed"] > 0]
        q = c.quote(picks, day)
        o = c.order(q)
        if o["status"] == "pending_approval":
            o = c.decide(o["order_id"], True, "scenario-human", "event ok")
        r.check("order accepted", o["status"] in ("placed", "approved", "confirmed"), o["status"])
        picked = {p["product_id"] for p in picks if p.get("substitute_for")}
        r.check("every picked substitute is in Order.substitutions", picked <= {x["substitute_id"] for x in o["substitutions"]}, str(picked))
        for s in o["substitutions"]:
            r.check("substitution recorded with reason", s.get("original_id") and s.get("substitute_id") and s.get("reason"), str(s))
        excl = set(c.j("GET", "/v1/mandate").get("exclude_allergens", []))
        bad = [s for s in o["substitutions"] if violates(s["substitute_id"], excl)]
        r.check("substitutes respect mandate allergens", not bad)
        ev = c.audit(o["order_id"])
        events = ev["events"] if isinstance(ev, dict) and "events" in ev else ev
        r.check("audit explains substitutions", all(e.get("why") for e in events))
    finally:
        if simulated:
            clear_simulation(c)


def go():
    b = base or parse_base()
    run = r or Run("s3_event_substitution", b)
    run_scenario(Client(b, "hotel-aurora"), run)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s3_event_substitution", base)
    r.guard(go)
