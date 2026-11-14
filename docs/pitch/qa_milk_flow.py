"""QA: run the 1,000 L lactose-free milk flow against a running gateway over plain HTTP.

    uv run aimo serve --port 8020            # in another terminal
    uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8020

Steps: healthz -> manifest -> search -> offers -> plan -> quote -> order (expect pending_approval)
-> wrong-hash approval (expect 409) -> approve -> audit report -> audit verify.
Exits non-zero on the first unexpected result. Prints no secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import time

import httpx

HOTEL = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
NEED = {"customer_id": "hotel-aurora", "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"], "quantity": 1000, "unit": "L",
        "delivery_by": None, "preferred_product_ids": ["463038"]}


def step(name: str) -> float:
    print(f"\n== {name}")
    return time.time()


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        sys.exit(1)


def _approver_pin() -> str:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from aimo.gateway.human_auth import local_pin
        pin = local_pin("anna@hotel-aurora.example")
        if pin:
            return pin
    except Exception:
        pass
    env_pin = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env_pin:
        return env_pin
    key_dir = Path(os.environ.get("AIMO_KEY_DIR") or (Path(os.environ.get("AIMO_DATA_DIR", "data")) / "keys"))
    pins_file = key_dir / "approver_pins.json"
    if pins_file.exists():
        pins = json.loads(pins_file.read_text(encoding="utf-8"))
        if isinstance(pins, dict) and pins:
            return str(pins.get("anna@hotel-aurora.example") or next(iter(pins.values()))).strip()
    return (key_dir / "approver_pin").read_text(encoding="utf-8").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8020")
    ap.add_argument("--date", default=None, help="delivery date (default: next feasible weekday on the gateway's clock)")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, headers=HOTEL, timeout=60)
    if not a.date:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        from aimo.backend import clock
        now = clock.parse(c.get("/healthz").json().get("clock", {}).get("now"))
        a.date = clock.next_delivery_day(now).isoformat()
    NEED["delivery_by"] = a.date
    print(f"delivery date {a.date} (gateway clock)")

    t = step("GET /healthz")
    h = c.get("/healthz").json()
    check(h.get("ok") is True, f"integrations={h.get('integrations')}")

    step("GET /.well-known/otap.json")
    m = c.get("/.well-known/otap.json").json()
    check("mcp" in m and "jwks_url" in m, f"protocol={m.get('protocol')} mcp={m['mcp']['url']}")

    t = step("GET /v1/products/search?q=lactose-free milk")
    r = c.get("/v1/products/search", params={"q": "lactose-free milk", "limit": 5})
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s")
    prods = r.json() if isinstance(r.json(), list) else r.json().get("products", [])
    for p in prods[:5]:
        print(f"       {p.get('id')}  {p.get('name')}")

    t = step("POST /v1/offers 463038, 464306, 469363")
    r = c.post("/v1/offers", json={"product_ids": ["463038", "464306", "469363"],
                                   "requested_date": a.date, "customer_id": "hotel-aurora"})
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s, "
          f"served_from={r.headers.get('X-Aimo-Served-From')}")
    for o in r.json():
        print(f"       {o.get('product_id')}  price={o.get('unit_price')} available={o.get('available')} "
              f"amount={o.get('available_amount')} cutoff_passed={o.get('cutoff_passed')} issues={o.get('issues')}")

    t = step("POST /v1/plan (1,000 L lactose-free milk, usual 463038)")
    r = c.post("/v1/plan", json=NEED)
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s")
    plan = r.json()
    for ln in plan.get("lines", []):
        print(f"       line {ln.get('tier')} {ln.get('product_id')} x{ln.get('qty_sales_units')} = {ln.get('line_total')}")
    print(f"       total_net={plan.get('total_net')} shortfall={plan.get('shortfall')}")
    check(bool(plan.get("lines")), "plan has lines")

    t = step("POST /v1/quotes")
    lines = [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln.get("tier"),
              "flags": ln.get("flags") or []}
             for ln in plan["lines"]]
    r = c.post("/v1/quotes", json={"customer_id": "hotel-aurora", "agent_id": "buyer-1",
                                   "delivery_date": a.date, "lines": lines, "need": NEED})
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s")
    q = r.json()
    print(f"       quote {q['quote_id']} total_net={q.get('total_net')} kid={q.get('kid')} expires_at={q.get('expires_at')}")
    print(f"       quote_hash={q.get('quote_hash')}")
    check(bool(q.get("signature")), "quote is signed")

    t = step("POST /v1/orders")
    r = c.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s")
    o = r.json()
    print(f"       order {o['order_id']} status={o.get('status')}")
    dec = o.get("decision") or {}
    for hit in dec.get("hits", dec.get("rule_hits", [])) or []:
        if hit.get("outcome") != "auto_approve":
            print(f"       rule {hit.get('rule_id')}: {hit.get('outcome')} - {hit.get('message')}")
    check(o.get("status") == "pending_approval", "needs approval")

    step("GET /v1/approvals?status=pending")
    r = c.get("/v1/approvals", params={"status": "pending"})
    check(any(x["order"]["order_id"] == o["order_id"] for x in r.json()), "order is in the approval inbox")

    step("POST decision as the AGENT naming a real approver (expect 403)")
    bad = {"decision": "approve", "approver": "anna@hotel-aurora.example",
           "approver_role": "procurement_manager", "note": "QA", "quote_hash": q["quote_hash"]}
    r = c.post(f"/v1/approvals/{o['order_id']}/decision", json=bad)
    check(r.status_code == 403, f"status {r.status_code} {r.json().get('error')}")

    # the human approver: no agent identity, plus the operator's approver credential
    pin = _approver_pin()
    human = {"X-Agent-Id": "", "X-Customer-Id": "", "X-Approver-Pin": pin}
    step("POST decision with a wrong quote_hash (expect 409)")
    r = c.post(f"/v1/approvals/{o['order_id']}/decision", json=dict(bad, quote_hash="0" * 64), headers=human)
    check(r.status_code == 409, f"status {r.status_code} {r.json().get('error')}")

    t = step("POST decision approve (human approver)")
    r = c.post(f"/v1/approvals/{o['order_id']}/decision", json=bad, headers=human)
    check(r.status_code == 200, f"status {r.status_code} in {time.time() - t:.2f}s")
    o2 = r.json()
    print(f"       status={o2.get('status')} response_code={o2.get('response_code')} approved_by={o2.get('approved_by')}")
    check(o2.get("status") == "placed", "order placed")

    step("GET /v1/audit/report/{order_id}")
    r = c.get(f"/v1/audit/report/{o['order_id']}")
    check(r.status_code == 200, f"{len(r.text.splitlines())} lines of markdown")
    print("\n".join("       " + s for s in r.text.splitlines()[:25]))

    step("GET /v1/orders/{order_id}/peppol-order-response.xml (UBL 2.3 Order Response, Peppol T116)")
    r = c.get(f"/v1/orders/{o['order_id']}/peppol-order-response.xml")
    import re
    m = re.search(r"<cbc:OrderResponseCode[^>]*>([^<]+)<", r.text)
    peppol = m.group(1).strip() if m else None
    check(r.status_code == 200 and bool(peppol), f"OrderResponseCode={peppol} ({len(r.text)} bytes of XML)")

    step("GET /v1/audit/verify")
    v = c.get("/v1/audit/verify").json()
    check(v.get("ok") is True, json.dumps(v))
    served = h.get("replay", {}).get("mode") if h.get("replay", {}).get("forced") else "live"
    print(f"\nORDER {o['order_id']} placed for {a.date}; total_net EUR {q.get('total_net')}; Peppol OrderResponseCode "
          f"{peppol}; audit verify ok={v.get('ok')} ({v.get('events', v.get('n', v.get('checked', '?')))} events); "
          f"served_from={'replay' if served != 'live' else 'live'}")
    print("\nMILK FLOW OK")


if __name__ == "__main__":
    main()
