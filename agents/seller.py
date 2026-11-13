import argparse
import json
import os
from datetime import date, timedelta

import httpx

URL = os.environ.get("AIMO_URL", "http://localhost:8080")
KEYS = {
    "hotel-aurora": "demo-hotel-aurora",
    "hospital-tays": "demo-hospital-tays",
    "school-hervanta": "demo-school-hervanta",
}
SEED = {
    "hotel-aurora": ["Valio maito", "Valio voi", "Valio rahka"],
    "hospital-tays": ["Valio maito", "Valio rahka", "Valio jogurtti"],
    "school-hervanta": ["Valio maito", "Valio raejuusto", "Valio voi"],
}


def _call(method, path, key, url, **kw):
    r = httpx.request(method, url.rstrip("/") + "/v1" + path, headers={"X-Agent-Key": key}, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def usual_items(events):
    seen = {}
    for e in events:
        d = e.get("detail") or e
        lines = d.get("lines") or d.get("items") or []
        for ln in lines if isinstance(lines, list) else []:
            if not isinstance(ln, dict):
                continue
            pid = str(ln.get("product_id", ""))
            q = ln.get("name") or ln.get("query") or ln.get("product_name")
            if pid and q and pid not in seen:
                seen[pid] = {"product_id": pid, "query": q, "quantity": float(ln.get("quantity") or 1), "unit": ln.get("sales_unit") or ln.get("unit") or "kg"}
    return list(seen.values())


def allowed(offer, mandate):
    p = offer["product"]
    if set(a.lower() for a in p.get("allergens") or []) & set(a.lower() for a in mandate.get("exclude_allergens") or []):
        return False
    cats = mandate.get("allowed_categories")
    if cats and p.get("category") not in cats:
        return False
    if offer.get("sales_block"):
        return False
    return offer["total_net"] <= mandate.get("per_order_eur", float("inf"))


def rank(usual_id, offers, mandate, limit=1):
    ok = [o for o in offers if allowed(o, mandate) and o["available_amount"] >= o["units_needed"]]
    base = next((o for o in offers if str(o["product_id"]) == str(usual_id)), None)
    prefix = ""
    if base is None:
        if not ok:
            return []
        base = min(ok, key=lambda o: o["total_net"])
        if len(ok) == 1 or not usual_id:
            return [{"need_item": {"product_id": usual_id, "name": base["product"]["name"], "quantity": base["units_needed"]}, "offer": base, "score": 0.0, "why": f"{base['product']['name']}: cheapest available option, total net {base['total_net']:.2f} EUR for {base['units_needed']} units, {base['available_amount']} in stock, reliability {base.get('reliability')}, CO2 {base.get('co2_per_kg')} kg CO2e/kg"}]
        prefix = "your usual item was not in the offers, so the cheapest available option is the baseline; "
    out = []
    for o in ok:
        if o["product_id"] == base["product_id"]:
            continue
        reasons, score = [], 0.0
        if base["available_amount"] < base["units_needed"]:
            reasons.append(f"your usual item has only {base['available_amount']} available for {base['units_needed']} needed, this one has {o['available_amount']}")
            score += 3
        if o["total_net"] < base["total_net"]:
            save = base["total_net"] - o["total_net"]
            reasons.append(f"total net {o['total_net']:.2f} vs {base['total_net']:.2f} EUR for the same quantity (unit price {o['unit_price_net']:.2f} vs {base['unit_price_net']:.2f}), saves {save:.2f}")
            score += save / base["total_net"]
        br, orr = base.get("reliability"), o.get("reliability")
        if br is not None and orr is not None and orr > br:
            reasons.append(f"delivery reliability {orr:.2f} vs {br:.2f}")
            score += orr - br
        if base.get("co2_per_kg") and o.get("co2_per_kg") is not None and o["co2_per_kg"] < base["co2_per_kg"]:
            reasons.append(f"CO2 {o['co2_per_kg']:.2f} vs {base['co2_per_kg']:.2f} kg CO2e/kg")
            score += 0.1
        if reasons:
            out.append((score, o, base, reasons))
    out.sort(key=lambda t: -t[0])
    return [
        {"need_item": {"product_id": b["product_id"], "name": b["product"]["name"], "quantity": b["units_needed"]}, "offer": o, "score": sc, "why": f"{o['product']['name']}: {prefix}" + "; ".join(r)}
        for sc, o, b, r in out[:limit]
    ]


def propose(customer, key, url=URL, max_offers=3):
    mandate = _call("GET", "/mandate", key, url)
    orders = _call("GET", "/orders", key, url, params={"customer": customer})
    events = orders.get("orders", []) if isinstance(orders, dict) else orders
    items = usual_items(events)
    if not items:
        audit = _call("GET", "/audit", key, url, params={"customer": customer})
        items = usual_items(audit.get("events", []) if isinstance(audit, dict) else audit)
    if not items:
        items = [{"product_id": None, "query": q, "quantity": 100.0, "unit": "kg"} for q in SEED.get(customer, [])]
    deliver_by = (date.today() + timedelta(days=3)).isoformat()
    excl = mandate.get("exclude_allergens") or []
    props = []
    for it in items:
        need = {
            "customer": customer,
            "deliver_by": deliver_by,
            "items": [{"query": it["query"], "quantity": it["quantity"], "unit": it["unit"], "constraints": {"exclude_allergens": excl, "labels": [], "brands": []}}],
        }
        res = _call("POST", "/offers", key, url, json=need)
        for group in res.get("items", []):
            props += rank(it["product_id"], group["offers"], mandate)
    props.sort(key=lambda p: -p["score"])
    return props[:max_offers]


def accept(proposal, key, url=URL):
    o = proposal["offer"]
    delivery = (o.get("available_date") or (date.today() + timedelta(days=3)).isoformat())[:10]
    q = _call("POST", "/quotes", key, url, json={"offers": [{"product_id": o["product_id"], "quantity": o["units_needed"]}], "delivery_date": delivery})
    return _call("POST", "/orders", key, url, json={"quote_id": q["quote_id"], "jws": q["jws"]})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", required=True)
    ap.add_argument("--key", default=None)
    ap.add_argument("--url", default=URL)
    ap.add_argument("--accept", type=int)
    a = ap.parse_args()
    a.key = a.key or os.environ.get(a.customer.upper().replace("-", "_") + "_KEY", KEYS[a.customer])
    ps = propose(a.customer, a.key, a.url)
    for i, p in enumerate(ps):
        print(i, p["why"])
    if a.accept is not None:
        print(json.dumps(accept(ps[a.accept], a.key, a.url), indent=1))
