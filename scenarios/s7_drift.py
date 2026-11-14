import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *


def run_scenario(base, r):
    from agents import drift
    for cust in ("hotel-aurora", "school-hervanta"):
        st = os.path.join(tempfile.mkdtemp(), "drift.json")
        m = Client(base, cust).j("GET", "/v1/mandate")
        excl = {a.lower() for a in m.get("exclude_allergens", [])}
        res = drift.check(cust, KEYS[cust], base, state_path=st)
        flags = res["flags"]
        r.say(f"{cust}: {len(flags)} flags")
        r.check(f"{cust} standing order returned", len(res["standing_order"]["lines"]) > 0)
        r.check(f"{cust} flags have a why and a route", all(f.get("why") and f.get("route") in ("auto", "approval") for f in flags))
        subs = [f["fix"] for f in flags if f["fix"].get("action") == "substitute"]
        r.check(f"{cust} substitutes respect mandate allergens", all(not violates(s["product_id"], excl) for s in subs))
        r.check(f"{cust} allergen flags never auto", all(f["route"] == "approval" for f in flags if f["kind"] == "mandate"))
    st = os.path.join(tempfile.mkdtemp(), "drift.json")
    drift.check("hotel-aurora", KEYS["hotel-aurora"], base, state_path=st)
    c = Client(base, "hotel-aurora")
    pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    try:
        simulate(c, "stockout", product_id=pid, available_amount=0)
        flags = drift.check("hotel-aurora", KEYS["hotel-aurora"], base, state_path=st)["flags"]
    finally:
        clear_simulation(c)
    r.check("injected stock-out is flagged for that product", any(f["product_id"] == pid for f in flags))


if __name__ == "__main__":
    base = parse_base()
    r = Run("s7_drift", base)
    r.guard(lambda: run_scenario(base, r))
