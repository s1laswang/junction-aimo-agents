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


ALLERGEN_ALIASES = {
    "nuts": {"nuts", "nut", "peanuts", "peanut", "tree nuts"},
    "nut": {"nuts", "nut", "peanuts", "peanut", "tree nuts"},
    "peanut": {"peanut", "peanuts"},
    "peanuts": {"peanut", "peanuts"},
    "milk": {"milk", "lactose"},
    "dairy": {"dairy", "milk", "lactose"},
    "gluten": {"gluten", "wheat", "barley", "rye", "oats"},
    "egg": {"egg", "eggs"},
    "eggs": {"egg", "eggs"},
}


def _norm_allergens(items, expand=False):
    out = set()
    for a in items or []:
        s = str(a).strip().lower()
        for prefix in ("may_contain:", "may:"):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                break
        if s:
            out |= ALLERGEN_ALIASES.get(s, {s}) if expand else {s}
    return out


def _safe(alt, excluded):
    return not (_norm_allergens(alt.get("allergens", [])) & _norm_allergens(excluded, expand=True))


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
    excluded = _norm_allergens(mandate.get("exclude_allergens", []), expand=True)
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

    bad = sorted(_norm_allergens(after.get("allergens", [])) & excluded)
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
                 f"Weekly quantity of {name} grew from {weeks[0]} to {weeks[-1]} {line['unit']} (+{pct}%) over {len(weeks)} weeks (seeded demo history) without a decision.",
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
    every = [o for g in offers_items for o in g.get("offers", [])]
    out = {}
    for line, res in zip(lines, offers_items):
        pid = line["product_id"]
        mine = next((o for o in every if o["product_id"] == pid), None)
        alts = [
            {"product_id": o["product_id"], "name": o["product"]["name"], "price": o["unit_price_net"],
             "allergens": o["product"].get("allergens", []), "available_amount": o.get("available_amount"),
             "sales_block": o.get("sales_block")}
            for o in res.get("offers", []) if o["product_id"] != pid
        ]
        out[pid] = {"found": mine is not None, "alternatives": alts}
        if mine:
            out[pid].update(
                price=mine["unit_price_net"], available_amount=mine.get("available_amount"),
                available_date=mine.get("available_date"), sales_block=mine.get("sales_block"),
                allergens=mine["product"].get("allergens", []))
    return out


def product_of(c, pid):
    try:
        return c.get(f"/v1/products/{pid}").raise_for_status().json()
    except httpx.HTTPError:
        return {}


def exact_quote(c, line, product, delivery):
    try:
        q = c.post("/v1/quotes", json={"offers": [{"product_id": line["product_id"], "quantity": line["weeks"][-1]}], "delivery_date": delivery})
        q.raise_for_status()
        ql = next(l for l in q.json()["lines"] if l["product_id"] == line["product_id"])
    except (httpx.HTTPError, StopIteration, KeyError):
        return None
    return {"found": True, "price": ql["unit_price_net"], "available_amount": None, "available_date": None,
            "sales_block": None, "allergens": product.get("allergens", [])}


def check(customer, key, url, today=None, state_path=None, rebaseline=False, client=None):
    seed = SEEDS[customer]
    delivery = next_delivery(seed["weekday"], today).isoformat()
    state_path = state_path or os.environ.get("AIMO_DRIFT_STATE", "var/drift.json")
    c = client or httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=30)
    mandate = c.get("/v1/mandate").raise_for_status().json()
    prods = {l["product_id"]: product_of(c, l["product_id"]) for l in seed["lines"]}
    need = {
        "customer": customer, "deliver_by": delivery,
        "items": [{"query": prods[l["product_id"]].get("name") or l["name"], "quantity": l["weeks"][-1], "unit": l["unit"], "constraints": {}} for l in seed["lines"]],
    }
    items = c.post("/v1/offers", json=need).raise_for_status().json()["items"]
    now = snapshot(items, seed["lines"])
    for l in seed["lines"]:
        pid = l["product_id"]
        if not now[pid]["found"]:
            q = exact_quote(c, l, prods[pid], delivery)
            if q:
                now[pid].update(q)
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


def apply(result, key, url, ask_human=True):
    o = result["standing_order"]
    pending = [f for f in result["flags"] if f["route"] == "approval"]
    if pending and not ask_human:
        return {"status": "needs_approval", "flags": pending}
    fixes = {f["product_id"]: f["fix"] for f in result["flags"]}
    lines = []
    for l in o["lines"]:
        fx = fixes.get(l["product_id"], {})
        line = {"product_id": l["product_id"], "quantity": fx.get("quantity", l["quantity"])}
        if fx.get("action") == "substitute":
            line["product_id"] = fx["product_id"]
            line["substitute_for"] = l["product_id"]
        lines.append(line)
    c = httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=30)
    q = c.post("/v1/quotes", json={"offers": lines, "delivery_date": o["delivery_date"]}).raise_for_status().json()
    body = {"quote_id": q["quote_id"], "jws": q["jws"]}
    if pending:
        body.update(ask_human=True, note="; ".join(f["why"] for f in pending))
    return c.post("/v1/orders", json=body).raise_for_status().json()


OPEN = ("placed", "confirmed", "approved")


def line_fix(line, group, delivery):
    pid, qty = line["product_id"], line["quantity"]
    offers = group.get("offers", [])
    mine = next((o for o in offers if o["product_id"] == pid), None)
    blocked = mine is None or mine.get("sales_block")
    avail = 0 if blocked else (mine.get("available_amount") or 0)
    late = mine.get("available_date") if mine else None
    if not blocked and avail >= qty and not (late and late[:10] > delivery):
        return None
    name = line.get("name") or pid
    subs = [o for o in offers if o.get("substitute_for") == pid]
    keep = min(avail, qty) if not (late and late[:10] > delivery) else 0
    if subs or keep:
        lines = ([{"product_id": pid, "quantity": keep}] if keep else []) + [
            {"product_id": o["product_id"], "quantity": o["units_needed"], "substitute_for": pid} for o in subs]
        why = (f"{name} is no longer available for {delivery}: " if not keep else f"Only {keep} of {qty} of {name} available for {delivery}: ") + (
            f"keeping {keep} and substituting {', '.join(o['product']['name'] for o in subs)}." if keep and subs else
            f"substituting {', '.join(o['product']['name'] for o in subs)}." if subs else f"reducing to {keep}.")
        return {"after": keep, "fix": {"action": "split" if keep and subs else "substitute" if subs else "reduce_qty", "lines": lines}, "why": why, "date": delivery}
    if late and late[:10] > delivery:
        return {"after": late[:10], "fix": {"action": "move_date", "date": late[:10], "lines": [{"product_id": pid, "quantity": qty}]},
                "why": f"{name} is only available from {late[:10]}, after the {delivery} delivery; moving the date.", "date": late[:10]}
    return {"after": 0, "fix": {"action": "none", "lines": []}, "why": f"{name} is not available for {delivery} and no substitute was found.", "date": delivery}


def watch_orders(customer, key, url, today=None, state_path=None, client=None):
    c = client or httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=30)
    state_path = state_path or os.environ.get("AIMO_DRIFT_STATE", "var/drift.json")
    state = load_state(state_path)
    done = state.setdefault("_watched", {})
    orders = c.get("/v1/orders", params={"customer": customer}).raise_for_status().json()
    out = []
    for o in orders:
        if o["order_id"] in done:
            continue
        if o["status"] == "pending_approval":
            so = next((r for r in o.get("decision", {}).get("reasons", []) if r.get("code") == "STOCKOUT"), None)
            if so:
                out.append({"order_id": o["order_id"], "product_id": None, "kind": "stockout_handled", "before": None, "after": None,
                            "fix": {"action": "gateway_reacted", "substitutions": o.get("substitutions", [])}, "why": so["text"], "new_order_id": None})
            continue
        if o["status"] not in OPEN:
            continue
        delivery = (o.get("delivery_date") or (today or date.today()) + timedelta(days=1))
        delivery = delivery if isinstance(delivery, str) else delivery.isoformat()
        need = {"customer": customer, "deliver_by": delivery, "items": [
            {"query": l.get("name") or l["product_id"], "quantity": l["quantity"], "unit": l.get("sales_unit") or "", "constraints": {}} for l in o["lines"]]}
        groups = c.post("/v1/offers", json=need).raise_for_status().json()["items"]
        res, new_lines, dates = [], [], {delivery}
        for l, g in zip(o["lines"], groups):
            f = line_fix(l, g, delivery)
            if f is None:
                new_lines.append({"product_id": l["product_id"], "quantity": l["quantity"]})
                continue
            new_lines += f["fix"]["lines"]
            dates.add(f["date"])
            res.append({"order_id": o["order_id"], "product_id": l["product_id"], "before": l["quantity"], "after": f["after"],
                        "fix": f["fix"], "why": f["why"], "new_order_id": None})
        if not res:
            continue
        if new_lines and any(r["fix"]["action"] != "none" for r in res):
            q = c.post("/v1/quotes", json={"offers": new_lines, "delivery_date": max(dates)}).raise_for_status().json()
            note = f"Replaces order {o['order_id']}: " + " ".join(r["why"] for r in res)
            new = c.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"], "ask_human": True, "note": note}).raise_for_status().json()
            for r in res:
                r["new_order_id"] = new["order_id"]
            done[o["order_id"]] = new["order_id"]
            save_state(state_path, state)
        out += res
    return out


def demo(url, admin_key="demo-admin"):
    for cust in SEEDS:
        print(f"== standing order check: {cust}")
        r = check(cust, "demo-" + cust, url)
        print(f"delivery {r['standing_order']['delivery_date']}, {len(r['flags'])} flag(s)")
        for f in r["flags"]:
            print(f"  [{f['route']}] {f['why']} -> {f['fix']['action']}")
    cust = "hotel-aurora"
    key = "demo-" + cust
    c = httpx.Client(base_url=url, headers={"X-Agent-Key": key}, timeout=60)
    adm = {"X-Admin-Key": admin_key}
    line = SEEDS[cust]["lines"][0]
    day = next_delivery(SEEDS[cust]["weekday"]).isoformat()
    need = {"customer": cust, "deliver_by": day, "items": [{"query": line["name"], "quantity": 2, "unit": line["unit"], "constraints": {}}]}
    offer = c.post("/v1/offers", json=need).raise_for_status().json()["items"][0]["offers"][0]
    q = c.post("/v1/quotes", json={"offers": [{"product_id": offer["product_id"], "quantity": offer["units_needed"]}], "delivery_date": day}).raise_for_status().json()
    o = c.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"]}).raise_for_status().json()
    if o["status"] == "pending_approval":
        by = c.get("/v1/mandate").json()["approvers"][0]
        o = c.post(f"/v1/approvals/{o['order_id']}", headers=adm, json={"approve": True, "by": by, "note": "demo"}).raise_for_status().json()
    print(f"== stock-out story: order {o['order_id']} is {o['status']} ({offer['product']['name']})")
    pid = o["lines"][0]["product_id"]
    httpx.post(url + "/v1/admin/simulate", headers=adm, json={"product_id": pid, "available_amount": 0}).raise_for_status()
    try:
        print(f"supplier stock-out simulated on {pid}")
        after = c.get(f"/v1/orders/{o['order_id']}").json()
        print(f"gateway reaction: order is now {after['status']}")
        for rs in after["decision"]["reasons"]:
            print(f"  {rs['code']}: {rs['text']}")
        for sb in after.get("substitutions", []):
            print(f"  substitution {sb['original_id']} -> {sb['substitute_id']}: {sb['reason']}")
        for w in watch_orders(cust, key, url, state_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "drift-demo.json")):
            print(f"watcher: {w.get('kind', w['fix']['action'])} on {w['order_id']}: {w['why']} (new order: {w['new_order_id']})")
    finally:
        httpx.delete(url + "/v1/admin/simulate", headers=adm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", choices=list(SEEDS))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--key", default=os.environ.get("AIMO_AGENT_KEY"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hold", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--url", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--rebaseline", action="store_true")
    a = ap.parse_args()
    if a.demo:
        demo(a.url)
        return
    if not a.customer:
        ap.error("--customer or --demo required")
    a.key = a.key or "demo-" + a.customer
    if a.watch:
        print(json.dumps(watch_orders(a.customer, a.key, a.url), indent=1))
        return
    r = check(a.customer, a.key, a.url, rebaseline=a.rebaseline)
    print(json.dumps(r, indent=1))
    for f in r["flags"]:
        print(f"[{f['route']}] {f['why']} -> {f['fix']['action']}")
    if a.apply:
        print(json.dumps(apply(r, a.key, a.url, not a.hold), indent=1))


if __name__ == "__main__":
    main()
