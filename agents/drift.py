import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

import httpx

PRICE_PCT = 5.0
AUTO_PRICE_PCT = 10.0
CREEP_PCT = 20.0

SEEDS = {
    "school-hervanta": {
        "weekday": 1,
        "lines": [
            {"product_id": "460396", "name": "Valio skimmed milk 10 l novobox", "unit": "CS", "weeks": [8, 8, 8, 9, 9, 10, 11, 12]},
            {"product_id": "461578", "name": "Vaasan Rye bread 45x85g", "unit": "CS", "weeks": [6, 6, 6, 6, 6, 6, 6, 6]},
            {"product_id": "460573", "name": "PP POTATO PRE-WASHED BELLAROSA", "unit": "KG", "weeks": [120, 120, 120, 120, 120, 120, 120, 120]},
        ],
    },
    "hospital-tays": {
        "weekday": 0,
        "lines": [
            {"product_id": "462775", "name": "Arla Milkdrink 1L lactose free fat free UHT", "unit": "ST", "weeks": [60, 60, 60, 62, 64, 66, 70, 74]},
            {"product_id": "461765", "name": "Moilas Gluten-free Dark Bread 600g sliced frozen", "unit": "KI", "weeks": [10, 10, 10, 10, 10, 10, 10, 10]},
            {"product_id": "460200", "name": "Elovena 1kg wholegrain oat flakes", "unit": "ST", "weeks": [20, 20, 20, 20, 20, 20, 20, 20]},
        ],
    },
    "hotel-aurora": {
        "weekday": 3,
        "lines": [
            {"product_id": "463982", "name": "Kieku Barn Eggs L180 12300g", "unit": "KI", "weeks": [4, 4, 4, 4, 4, 4, 4, 4]},
            {"product_id": "460468", "name": "Paulig Presidentti Tumma Paahto 18x300g", "unit": "KI", "weeks": [3, 3, 3, 3, 3, 3, 3, 3]},
            {"product_id": "460884", "name": "Reuter & Stolt Croissant butter 18 % 80 x 90g", "unit": "KI", "weeks": [5, 5, 5, 5, 6, 6, 7, 8]},
        ],
    },
}


def next_delivery(weekday, today=None):
    today = today or date.today()
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def _safe(alt, excluded):
    return not ({a.lower() for a in alt.get("allergens", [])} & excluded)


def best_alt(after, qty, excluded):
    ok = [
        a for a in after.get("alternatives", [])
        if _safe(a, excluded) and not a.get("sales_block") and (a.get("available_amount") or 0) >= qty
    ]
    return min(ok, key=lambda a: a["price"]) if ok else None


def _sub_fix(alt, qty):
    return {"action": "substitute", "product_id": alt["product_id"], "name": alt.get("name"), "price": alt["price"], "quantity": qty}


def drift_rules(line, before, after, mandate, delivery):
    pid, name, qty = line["product_id"], line["name"], line["weeks"][-1]
    excluded = {a.lower() for a in mandate.get("exclude_allergens", [])}
    alt = best_alt(after, qty, excluded)
    subst = mandate.get("substitutions", "never")
    sub_route = "auto" if alt and subst == "within_prefs" else "approval"
    flags = []

    def flag(kind, b, a, why, fix, route):
        flags.append({"product_id": pid, "kind": kind, "before": b, "after": a, "why": why, "fix": fix, "route": route})

    if not after.get("found"):
        flag("unavailable", "offered", "not offered", f"{name} is not offered for delivery on {delivery}.",
             _sub_fix(alt, qty) if alt else {"action": "none"}, sub_route if alt else "approval")
        return flags

    bad = sorted({a.lower() for a in after.get("allergens", [])} & excluded)
    if bad:
        flag("mandate", "allowed", ", ".join(bad),
             f"{name} now lists excluded allergen {', '.join(bad)}; this customer's mandate forbids it.",
             _sub_fix(alt, qty) if alt else {"action": "remove_line"}, "approval")

    if after.get("sales_block"):
        flag("sales_block", "open", "blocked", f"{name} has a sales block for {delivery} and cannot be ordered.",
             _sub_fix(alt, qty) if alt else {"action": "none"}, sub_route if alt else "approval")
    else:
        avail = after.get("available_amount")
        if avail is not None and avail < qty:
            flag("short", qty, avail, f"Only {avail} of {qty} {line['unit']} of {name} available for {delivery}.",
                 {"action": "reduce_qty", "quantity": avail}, "auto")
        late = after.get("available_date")
        if late and late > delivery:
            flag("late", delivery, late, f"{name} is only available from {late}, after the {delivery} delivery.",
                 {"action": "move_date", "date": late}, "approval")

    old, new = before.get("price"), after.get("price")
    if old and new is not None:
        pct = round((new - old) / old * 100, 1)
        if abs(pct) >= PRICE_PCT:
            cap = mandate.get("per_order_eur")
            over = cap is not None and new * qty > cap
            route = "auto" if pct < 0 or (pct <= AUTO_PRICE_PCT and not over) else "approval"
            cheaper = alt if alt and alt["price"] < new else None
            flag("price", old, new, f"Price of {name} moved {pct:+.1f}% (from {old:.2f} to {new:.2f} EUR per {line['unit']}).",
                 _sub_fix(cheaper, qty) if cheaper and route == "approval" and subst != "never" else {"action": "requote", "price": new}, route)

    weeks = line["weeks"]
    if len(weeks) > 1 and weeks[0] and weeks == sorted(weeks):
        pct = round((weeks[-1] - weeks[0]) / weeks[0] * 100, 1)
        if pct >= CREEP_PCT:
            flag("qty_creep", weeks[0], weeks[-1],
                 f"Weekly quantity of {name} grew from {weeks[0]} to {weeks[-1]} {line['unit']} (+{pct}%) over {len(weeks)} weeks without a decision.",
                 {"action": "confirm_qty", "quantity": weeks[0]}, "approval")
    return flags


def load_state(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1))


def snapshot(offers_items, lines):
    out = {}
    for line, res in zip(lines, offers_items):
        offers = res.get("offers", [])
        mine = next((o for o in offers if o["product_id"] == line["product_id"]), None)
        alts = [
            {"product_id": o["product_id"], "name": o["product"]["name"], "price": o["unit_price_net"],
             "allergens": o["product"].get("allergens", []), "available_amount": o.get("available_amount"),
             "sales_block": o.get("sales_block")}
            for o in offers if o["product_id"] != line["product_id"]
        ]
        out[line["product_id"]] = {"found": mine is not None, "alternatives": alts}
        if mine:
            out[line["product_id"]].update(
                price=mine["unit_price_net"], available_amount=mine.get("available_amount"),
                available_date=mine.get("available_date"), sales_block=mine.get("sales_block"),
                allergens=mine["product"].get("allergens", []))
    return out


def check(customer, key, url, today=None, state_path=None, rebaseline=False, client=None):
    seed = SEEDS[customer]
    delivery = next_delivery(seed["weekday"], today).isoformat()
    state_path = state_path or os.environ.get("AIMO_DRIFT_STATE", "var/drift.json")
    c = client or httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=30)
    mandate = c.get("/v1/mandate").raise_for_status().json()
    need = {
        "customer": customer, "deliver_by": delivery,
        "items": [{"query": l["name"], "quantity": l["weeks"][-1], "unit": l["unit"], "constraints": {}} for l in seed["lines"]],
    }
    items = c.post("/v1/offers", json=need).raise_for_status().json()["items"]
    now = snapshot(items, seed["lines"])
    state = load_state(state_path)
    base = state.get(customer, {}).get("baseline", {})
    flags = []
    for line in seed["lines"]:
        flags += drift_rules(line, base.get(line["product_id"], {}), now[line["product_id"]], mandate, delivery)
    if rebaseline or customer not in state:
        state[customer] = {"baseline": {p: {"price": s.get("price")} for p, s in now.items()}, "week": delivery}
        save_state(state_path, state)
    order = {"customer": customer, "delivery_date": delivery,
             "lines": [{"product_id": l["product_id"], "name": l["name"], "unit": l["unit"], "quantity": l["weeks"][-1], "history": l["weeks"]} for l in seed["lines"]]}
    return {"standing_order": order, "flags": flags}


def apply(result, key, url):
    o = result["standing_order"]
    pending = [f for f in result["flags"] if f["route"] == "approval"]
    if pending:
        return {"status": "needs_approval", "flags": pending}
    fixes = {f["product_id"]: f["fix"] for f in result["flags"]}
    lines = []
    for l in o["lines"]:
        fx = fixes.get(l["product_id"], {})
        pid = fx["product_id"] if fx.get("action") == "substitute" else l["product_id"]
        lines.append({"product_id": pid, "quantity": fx.get("quantity", l["quantity"])})
    c = httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=30)
    q = c.post("/v1/quotes", json={"offers": lines, "delivery_date": o["delivery_date"]}).raise_for_status().json()
    return c.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"]}).raise_for_status().json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", required=True, choices=list(SEEDS))
    ap.add_argument("--key", default=os.environ.get("AIMO_AGENT_KEY"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--url", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--rebaseline", action="store_true")
    a = ap.parse_args()
    a.key = a.key or "demo-" + a.customer
    r = check(a.customer, a.key, a.url, rebaseline=a.rebaseline)
    print(json.dumps(r, indent=1))
    for f in r["flags"]:
        print(f"[{f['route']}] {f['why']} -> {f['fix']['action']}")
    if a.apply:
        print(json.dumps(apply(r, a.key, a.url), indent=1))


if __name__ == "__main__":
    main()
