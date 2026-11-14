import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = os.environ.get("AIMO_URL", "http://127.0.0.1:8080").rstrip("/")
r = Run("s5_allergen_safe", base)
_ORIG_VIOLATES = violates


def _item_violates(pid, db, excl, excl_codes=None):
    if violates is not _ORIG_VIOLATES:
        return violates(pid, excl)
    banned = excl_codes if excl_codes is not None else excluded_codes(excl)
    item = db.get(pid) if isinstance(db, dict) and pid in db else (db.get(str(pid)) if isinstance(db, dict) else None)
    if isinstance(item, dict) and "synkkaData" in item:
        synkka = item.get("synkkaData") or {}
        codes = {
            v["id"]
            for cl in (synkka.get("classifications") or [])
            if isinstance(cl, dict) and cl.get("name") == "allergen"
            for v in (cl.get("values") or [])
            if isinstance(v, dict) and v.get("id") and v.get("unit") in ("CONTAINS", "MAY_CONTAIN")
        }
        return bool(codes & banned)
    if isinstance(item, dict) and "allergens" in item:
        cleaned = [
            str(a).split(":", 1)[1] if str(a).lower().startswith(("may:", "may_contain:")) else str(a)
            for a in (item.get("allergens") or [])
        ]
        return bool(excluded_codes(cleaned) & banned)
    try:
        return violates(pid, excl)
    except FileNotFoundError:
        return False


def run_scenario(c, r, day=None, db_fn=product_db):
    day = day or next_monday()
    m = c.j("GET", "/v1/mandate")
    excl = set(m["exclude_allergens"])
    excl_codes = excluded_codes(excl)
    r.say(f"hospital-tays excludes {sorted(excl)}")
    r.check("mandate excludes allergens", len(excl) > 0)
    need = c.parse(f"We need 100 litres of lactose-free milk and 50 kg of bread by {day}", "hospital-tays")
    offers = c.offers(need)
    seen = [o["product_id"] for it in offers["items"] for o in it["offers"]]
    r.check("offers returned", len(seen) > 0)
    db = db_fn()
    leaks = [p for p in seen if _item_violates(p, db, excl, excl_codes)]
    r.check("no offer contains an excluded allergen", not leaks, str(leaks[:5]))
    candidates = [p for p, v in db.items() if isinstance(v, dict) and not v.get("deleted") and _item_violates(p, db, excl, excl_codes)]
    bad, q = None, None
    for pid in candidates[:15]:
        try:
            resp = c.call("POST", "/v1/quotes", json={"offers": [{"product_id": pid, "quantity": 1}], "delivery_date": str(day)})
        except httpx.HTTPStatusError:
            continue
        if resp.status_code == 200:
            bad, q = pid, resp.json()
            break
    r.check("found a violating product in data", bad is not None)
    if q is None:
        return
    o = c.order(q)
    r.check("violating order blocked", o["decision"]["outcome"] == "blocked" and o["status"] == "blocked", str(o["decision"]))
    r.check("block reason is ALLERGEN_CONFLICT with text", any(x.get("code") == "ALLERGEN_CONFLICT" and x.get("text") for x in o["decision"]["reasons"]), str(o["decision"]["reasons"]))
    ev = c.audit(o["order_id"])
    events = ev["events"] if isinstance(ev, dict) and "events" in ev else ev
    r.check("audit has order.blocked with why", any(e.get("action") == "order.blocked" and bool(str(e.get("why") or "").strip()) for e in events), str(events))


def go():
    run_scenario(Client(base, "hospital-tays"), r)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s5_allergen_safe", base)
    r.guard(go)
