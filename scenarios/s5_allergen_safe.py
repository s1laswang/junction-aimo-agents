from _common import *

base = parse_base()
r = Run("s5_allergen_safe", base)


def go():
    c = Client(base, "hospital-tays")
    day = next_monday()
    m = c.j("GET", "/v1/mandate")
    excl = set(m["exclude_allergens"])
    r.say(f"hospital-tays excludes {sorted(excl)}")
    r.check("mandate excludes allergens", len(excl) > 0)
    need = c.parse(f"We need 100 litres of lactose-free milk and 50 kg of bread by {day}", "hospital-tays")
    offers = c.offers(need)
    seen = [o["product_id"] for it in offers["items"] for o in it["offers"]]
    r.check("offers returned", len(seen) > 0)
    leaks = [p for p in seen if violates(p, excl)]
    r.check("no offer contains an excluded allergen", not leaks, str(leaks[:5]))
    bad = next((p for p, v in product_db().items() if not v["deleted"] and violates(p, excl)), None)
    r.check("found a violating product in data", bad is not None)
    q = c.quote([{"product_id": bad, "quantity": 1}], day)
    o = c.order(q)
    r.check("violating order blocked", o["decision"]["outcome"] == "blocked" and o["status"] == "blocked", str(o["decision"]))
    r.check("block reason is ALLERGEN_CONFLICT with text", any(x.get("code") == "ALLERGEN_CONFLICT" and x.get("text") for x in o["decision"]["reasons"]), str(o["decision"]["reasons"]))


r.guard(go)
