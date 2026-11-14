import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def run_scenario(base, r, client_cls=None, check_fn=None, violates_fn=None, simulate_fn=None, clear_fn=None):
    from agents import drift
    if client_cls is None:
        client_cls = Client
    if check_fn is None:
        check_fn = drift.check
    if violates_fn is None:
        violates_fn = violates
    if simulate_fn is None:
        simulate_fn = simulate
    if clear_fn is None:
        clear_fn = clear_simulation
    for cust in ("hotel-aurora", "school-hervanta"):
        with tempfile.TemporaryDirectory() as td:
            st = os.path.join(td, "drift.json")
            m = client_cls(base, cust).j("GET", "/v1/mandate")
            excl = {a.lower() for a in (m.get("exclude_allergens") or [])}
            res = check_fn(cust, KEYS[cust], base, state_path=st)
        flags = res["flags"]
        r.say(f"{cust}: {len(flags)} flags")
        r.check(f"{cust} standing order returned", len(res["standing_order"]["lines"]) > 0)
        r.check(f"{cust} flags have a why and a route", all(f.get("why") and f.get("route") in ("auto", "approval") for f in flags))
        subs = [f["fix"] for f in flags if (f.get("fix") or {}).get("action") == "substitute"]
        r.check(f"{cust} substitutes respect mandate allergens", all(not violates_fn(s["product_id"], excl) for s in subs))
        r.check(f"{cust} allergen flags never auto", all(f.get("route") == "approval" for f in flags if f.get("kind") == "mandate"))
    with tempfile.TemporaryDirectory() as td:
        st = os.path.join(td, "drift.json")
        check_fn("hotel-aurora", KEYS["hotel-aurora"], base, state_path=st)
        c = client_cls(base, "hotel-aurora")
        pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
        try:
            simulate_fn(c, "stockout", product_id=pid, available_amount=0)
            flags = check_fn("hotel-aurora", KEYS["hotel-aurora"], base, state_path=st)["flags"]
        finally:
            clear_fn(c)
    r.check("injected stock-out is flagged for that product", any(f.get("product_id") == pid for f in flags))


def go():
    b = base or parse_base()
    run = r or Run("s7_drift", b)
    run_scenario(b, run)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s7_drift", base)
    r.guard(go)
