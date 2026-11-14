import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def run_scenario(base, r, client_cls=Client, propose_fn=None, violates_fn=None):
    if propose_fn is None:
        from agents import seller
        propose_fn = seller.propose
    if violates_fn is None:
        violates_fn = violates
    seen_props = {}
    for cust in ("hotel-aurora", "hospital-tays"):
        c = client_cls(base, cust)
        m = c.j("GET", "/v1/mandate")
        excl = set(m.get("exclude_allergens", []))
        props = propose_fn(cust, KEYS[cust], base)
        seen_props[cust] = props
        r.say(f"{cust}: {len(props)} proposals")
        r.check(f"{cust} proposals have a why", all(p.get("why") for p in props))
        r.check(f"{cust} proposals respect excluded allergens", all(not violates_fn(p["offer"]["product_id"], excl) for p in props))
        cats = m.get("allowed_categories")
        r.check(f"{cust} proposals respect allowed categories", not cats or all(p["offer"]["product"].get("category") in cats for p in props))
    r.check("hotel got at least one proposal", len(seen_props["hotel-aurora"]) > 0)


def go():
    b = base or parse_base()
    run = r or Run("s6_proactive_seller", b)
    run_scenario(b, run, client_cls=Client)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s6_proactive_seller", base)
    r.guard(go)
