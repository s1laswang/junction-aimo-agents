import argparse
import json
import os
from datetime import date, timedelta

import httpx

URL = os.environ.get("AIMO_URL", "http://localhost:8080")
KEYS = {
    "hotel-aurora": "HOTEL_AURORA_KEY",
    "hospital-tays": "HOSPITAL_TAYS_KEY",
    "school-hervanta": "SCHOOL_HERVANTA_KEY",
}


def _call(method, path, key, url, **kw):
    r = httpx.request(method, url.rstrip("/") + "/v1" + path, headers={"X-Agent-Key": key}, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def usual_items(events):
    seen = {}
    for e in events:
        d = e.get("detail") or {}
        for ln in d.get("lines") or d.get("items") or []:
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
    base = next((o for o in offers if str(o["product_id"]) == str(usual_id)), None)
    if not base:
        return []
    out = []
    for o in offers:
        if str(o["product_id"]) == str(usual_id) or not allowed(o, mandate):
            continue
        if o["available_amount"] < o["units_needed"]:
            continue
        reasons, score = [], 0.0
        if base["available_amount"] < base["units_needed"]:
            reasons.append(f"your usual item has only {base['available_amount']} available for {base['units_needed']} needed, this one has {o['available_amount']}")
            score += 3
        if o["unit_price_net"] < base["unit_price_net"]:
            save = base["unit_price_net"] - o["unit_price_net"]
            reasons.append(f"net price {o['unit_price_net']:.2f} vs {base['unit_price_net']:.2f} per unit, saves {save:.2f}")
            score += save / base["unit_price_net"]
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
        {"need_item": {"product_id": b["product_id"], "name": b["product"]["name"], "quantity": b["units_needed"]}, "offer": o, "why": f"{o['product']['name']}: " + "; ".join(r)}
        for _, o, b, r in out[:limit]
    ]


def propose(customer, key, url=URL, max_offers=3):
    mandate = _call("GET", "/mandate", key, url)
    audit = _call("GET", "/audit", key, url, params={"customer": customer})
    events = audit.get("events", []) if isinstance(audit, dict) else audit
    deliver_by = (date.today() + timedelta(days=3)).isoformat()
    excl = mandate.get("exclude_allergens") or []
    items = usual_items(events)
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
    props.sort(key=lambda p: -len(p["why"]))
    return props[:max_offers]


def accept(proposal, key, url=URL):
    o = proposal["offer"]
    delivery = (o.get("available_date") or (date.today() + timedelta(days=3)).isoformat())[:10]
    q = _call("POST", "/quotes", key, url, json={"offers": [{"product_id": o["product_id"], "quantity": o["units_needed"]}], "delivery_date": delivery})
    return _call("POST", "/orders", key, url, json={"quote_id": q["quote_id"], "jws": q["jws"]})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", required=True)
    ap.add_argument("--key", default=os.environ.get("AGENT_KEY") or os.environ.get(KEYS.get("", ""), ""))
    ap.add_argument("--url", default=URL)
    ap.add_argument("--accept", type=int)
    a = ap.parse_args()
    ps = propose(a.customer, a.key or os.environ.get(KEYS.get(a.customer, ""), ""), a.url)
    for i, p in enumerate(ps):
        print(i, p["why"])
    if a.accept is not None:
        print(json.dumps(accept(ps[a.accept], a.key, a.url), indent=1))
