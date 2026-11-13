from _common import *

base = parse_base()
r = Run("s6_proactive_seller", base)


def go():
    from agents import seller
    for cust in ("hotel-aurora", "hospital-tays"):
        c = Client(base, cust)
        m = c.j("GET", "/v1/mandate")
        excl = set(m.get("exclude_allergens", []))
        props = seller.propose(cust, KEYS[cust], base)
        r.say(f"{cust}: {len(props)} proposals")
        r.check(f"{cust} proposals have a why", all(p.get("why") for p in props))
        r.check(f"{cust} proposals respect excluded allergens", all(not violates(p["offer"]["product_id"], excl) for p in props))
        cats = m.get("allowed_categories")
        r.check(f"{cust} proposals respect allowed categories", not cats or all(p["offer"]["product"].get("category") in cats for p in props))
    r.check("hotel got at least one proposal", len(seller.propose("hotel-aurora", KEYS["hotel-aurora"], base)) > 0)


r.guard(go)
