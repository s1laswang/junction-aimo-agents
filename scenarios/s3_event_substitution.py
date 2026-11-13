from _common import *

base = parse_base()
r = Run("s3_event_substitution", base)


def go():
    c = Client(base, "hotel-aurora")
    day = date.today() + timedelta(days=2)
    r.say("last-minute event: 400 portions of cream sauce pasta, 2 days out, item short -> recipe-logic substitution")
    need = c.parse(f"Last-minute event, we need pasta carbonara for 400 portions by {day}", "hotel-aurora")
    r.check("recipe expanded to ingredient lines", len(need["items"]) >= 3, f"{len(need['items'])} lines")
    offers = c.offers(need)
    short = [it for it in offers["items"] if it["shortfall"] > 0 or any(o.get("notes") for o in it["offers"])]
    covered = [it for it in offers["items"] if it["offers"]]
    r.check("offers or substitutes found for lines", len(covered) >= len(need["items"]) // 2, f"{len(covered)}/{len(need['items'])}")
    subs = [o for it in offers["items"] for o in it["offers"] if is_substitute(o)]
    r.check("substitute offers say what they replace", all(o["substitute_for"] != o["product_id"] for o in subs), f"{len(subs)} substitutes, {len(short)} short lines")
    q = c.quote(picks_from(offers), day)
    o = c.order(q)
    if o["status"] == "pending_approval":
        o = c.decide(o["order_id"], True, "scenario-human", "event ok")
    r.check("order accepted", o["status"] in ("placed", "approved", "confirmed"), o["status"])
    picked = {p["product_id"] for p in picks_from(offers) if p.get("substitute_for")}
    r.check("every picked substitute is in Order.substitutions", picked <= {x["substitute_id"] for x in o["substitutions"]}, str(picked))
    for s in o["substitutions"]:
        r.check("substitution recorded with reason", s.get("original_id") and s.get("substitute_id") and s.get("reason"), str(s))
    excl = set(c.j("GET", "/v1/mandate").get("exclude_allergens", []))
    bad = [s for s in o["substitutions"] if (allergens_of(s["substitute_id"]) or set()) & excl]
    r.check("substitutes respect mandate allergens", not bad)
    ev = c.audit(o["order_id"])
    events = ev["events"] if isinstance(ev, dict) and "events" in ev else ev
    r.check("audit explains substitutions", all(e.get("why") for e in events))


r.guard(go)
