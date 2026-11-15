"""Verify the 409 requote_needed price-lock beat against a running gateway over HTTP.

    uv run aimo serve --port 8000
    uv run python docs/pitch/requote_beat.py --base http://localhost:8000

Beat 1 proves that approving inside the 15-minute price lock honours the signed fixed price even
under an upstream price shock. Beat 2 advances past lock expiry under a +25% price shock on 464306,
verifies HTTP 409 requote_needed with old-vs-new diff and replacement signed quote, approves the
replacement order in one step, and verifies Peppol AP plus order.requoted in the signed audit chain.
Prints no secrets.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sys
import time

import httpx

HOTEL = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
APPROVER = "anna@hotel-aurora.example"
PRICE_SHOCK = "price_shock:464306:1.25"


class _CheckFailed(Exception):
    pass


def step(name: str) -> float:
    print(f"\n== {name}")
    return time.time()


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise _CheckFailed(msg)


def _ensure_repo_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def _approver_pin() -> str:
    root = _ensure_repo_path()
    key_dir = Path(
        os.environ.get("AIMO_KEY_DIR")
        or (Path(os.environ.get("AIMO_DATA_DIR", str(root / "data"))) / "keys")
    )
    try:
        from aimo import config
        from aimo.gateway.human_auth import local_pin

        prev = config.KEY_DIR
        config.KEY_DIR = key_dir
        try:
            pin = local_pin(APPROVER)
        finally:
            config.KEY_DIR = prev
        if pin:
            return pin
    except Exception:
        pass
    env_pin = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env_pin:
        return env_pin
    pins_file = key_dir / "approver_pins.json"
    if pins_file.exists():
        pins = json.loads(pins_file.read_text(encoding="utf-8"))
        if isinstance(pins, dict) and pins:
            return str(pins.get(APPROVER) or next(iter(pins.values()))).strip()
    return (key_dir / "approver_pin").read_text(encoding="utf-8").strip()


def _run_flow(base: str, requested_date: str | None) -> int:
    _ensure_repo_path()
    from aimo.backend import clock

    pin = _approver_pin()
    check(bool(pin), "approver credential resolved (not printed)")

    with httpx.Client(base_url=base, headers=HOTEL, timeout=60.0) as c:
        t = step("GET /healthz")
        r_h = c.get("/healthz")
        check(r_h.status_code == 200, f"status {r_h.status_code} in {time.time() - t:.2f}s")
        h = r_h.json()
        check(h.get("ok") is True, f"integrations={h.get('integrations')}")

        delivery_date = requested_date
        if not delivery_date:
            now_dt = clock.parse((h.get("clock") or {}).get("now"))
            delivery_date = clock.next_delivery_day(now_dt).isoformat()
        print(f"delivery date {delivery_date} (gateway clock)")

        need = {
            "customer_id": "hotel-aurora",
            "product_query": "lactose-free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": 1000,
            "unit": "L",
            "delivery_by": delivery_date,
            "preferred_product_ids": ["463038"],
        }

        t = step("Beat 1: POST /v1/plan -> /v1/quotes -> /v1/orders (inside 15-min lock)")
        r_plan = c.post("/v1/plan", json=need)
        check(r_plan.status_code == 200, f"plan status {r_plan.status_code} in {time.time() - t:.2f}s")
        plan = r_plan.json()
        check(bool(plan.get("lines")), "plan has lines")

        lines = [
            {
                "product_id": ln["product_id"],
                "qty_sales_units": ln["qty_sales_units"],
                "tier": ln.get("tier"),
                "flags": ln.get("flags") or [],
            }
            for ln in plan["lines"]
        ]
        quote_req = {
            "customer_id": "hotel-aurora",
            "agent_id": "buyer-1",
            "delivery_date": delivery_date,
            "lines": lines,
            "need": need,
        }

        r_qa = c.post("/v1/quotes", json=quote_req)
        check(r_qa.status_code == 200, f"quote_a status {r_qa.status_code}")
        q_a = r_qa.json()
        check(bool(q_a.get("signature")) and bool(q_a.get("quote_hash")), f"quote_a {q_a.get('quote_id')} signed")

        r_oa = c.post("/v1/orders", json={"quote_id": q_a["quote_id"], "quote_hash": q_a["quote_hash"]})
        check(r_oa.status_code == 200, f"order_a status {r_oa.status_code}")
        order_a = r_oa.json()
        check(order_a.get("status") == "pending_approval", f"order_a {order_a.get('order_id')} pending_approval")

        t = step("Beat 1: approve order_a before expires_at under upstream price shock")
        human_in_window = {
            "X-Agent-Id": "",
            "X-Customer-Id": "",
            "X-Approver-Pin": pin,
            "X-Aimo-Faults": PRICE_SHOCK,
        }
        r_da = c.post(
            f"/v1/approvals/{order_a['order_id']}/decision",
            headers=human_in_window,
            json={"decision": "approve", "approver": APPROVER, "quote_hash": q_a["quote_hash"]},
        )
        check(r_da.status_code == 200, f"approve order_a status {r_da.status_code} in {time.time() - t:.2f}s")
        placed_a = r_da.json()
        check(placed_a.get("status") == "placed", f"order_a status={placed_a.get('status')}")
        check(
            placed_a.get("quote_id") == q_a["quote_id"] and placed_a.get("quote_hash") == q_a["quote_hash"],
            f"order_a bound to locked quote {q_a['quote_id']}",
        )
        r_qa_check = c.get(f"/v1/quotes/{placed_a['quote_id']}")
        check(r_qa_check.status_code == 200, f"GET locked quote status {r_qa_check.status_code}")
        locked_total = float(r_qa_check.json().get("total_net") or 0.0)
        check(
            abs(locked_total - float(q_a["total_net"])) < 1e-6,
            f"locked total_net EUR {locked_total:.2f} preserved despite {PRICE_SHOCK}",
        )

        step("Beat 2: create fresh quote q_old and pending order o_old")
        r_qold = c.post("/v1/quotes", json=quote_req)
        check(r_qold.status_code == 200, f"q_old status {r_qold.status_code}")
        q_old = r_qold.json()
        check(bool(q_old.get("signature")) and bool(q_old.get("quote_hash")), f"q_old {q_old.get('quote_id')} signed")

        r_oold = c.post("/v1/orders", json={"quote_id": q_old["quote_id"], "quote_hash": q_old["quote_hash"]})
        check(r_oold.status_code == 200, f"o_old status {r_oold.status_code}")
        o_old = r_oold.json()
        check(o_old.get("status") == "pending_approval", f"o_old {o_old.get('order_id')} pending_approval")

        created_dt = clock.parse(q_old.get("created_at"))
        check(created_dt is not None, f"parsed q_old created_at={q_old.get('created_at')}")
        assert created_dt is not None
        expired_now = created_dt + timedelta(minutes=20)

        t = step("Beat 2: approve o_old at +20m with price shock (expect 409 requote_needed)")
        human_expired = {
            "X-Agent-Id": "",
            "X-Customer-Id": "",
            "X-Approver-Pin": pin,
            "X-Aimo-Now": expired_now.isoformat(),
            "X-Aimo-Faults": PRICE_SHOCK,
        }
        r_409 = c.post(
            f"/v1/approvals/{o_old['order_id']}/decision",
            headers=human_expired,
            json={"decision": "approve", "approver": APPROVER, "quote_hash": q_old["quote_hash"]},
        )
        check(r_409.status_code == 409, f"status {r_409.status_code} in {time.time() - t:.2f}s")
        err_body = r_409.json()
        detail = err_body.get("detail") or {}
        changes = detail.get("changes") or {}
        new_order = detail.get("new_order") or {}
        new_quote = detail.get("new_quote") or {}
        check(err_body.get("error") == "requote_needed", f"error={err_body.get('error')}")
        check(detail.get("reason") == "quote_expired", f"reason={detail.get('reason')}")
        check(
            bool(new_order.get("order_id")) and new_order.get("status") == "pending_approval",
            f"new_order={new_order.get('order_id')} status={new_order.get('status')}",
        )
        check(
            bool(new_quote.get("quote_id")) and bool(new_quote.get("quote_hash")) and bool(new_quote.get("signature")),
            f"new_quote={new_quote.get('quote_id')} signed",
        )
        old_total = float(changes.get("old_total_net") or 0.0)
        new_total = float(changes.get("new_total_net") or 0.0)
        check(
            changes.get("old_total_net") is not None
            and changes.get("new_total_net") is not None
            and new_total > old_total,
            f"re-priced diff EUR {old_total:.2f} -> EUR {new_total:.2f}",
        )

        t = step("Beat 2: one-step human approval of replacement new_order")
        r_approve_new = c.post(
            f"/v1/approvals/{new_order['order_id']}/decision",
            headers=human_expired,
            json={"decision": "approve", "approver": APPROVER, "quote_hash": new_quote["quote_hash"]},
        )
        check(r_approve_new.status_code == 200, f"status {r_approve_new.status_code} in {time.time() - t:.2f}s")
        placed_new = r_approve_new.json()
        check(placed_new.get("status") == "placed", f"new_order status={placed_new.get('status')}")

        step("GET /v1/orders/{new_order_id}/peppol-order-response.xml")
        r_peppol = c.get(f"/v1/orders/{new_order['order_id']}/peppol-order-response.xml")
        m_code = re.search(r"<cbc:OrderResponseCode[^>]*>([^<]+)<", r_peppol.text)
        peppol = m_code.group(1).strip() if m_code else None
        check(r_peppol.status_code == 200 and peppol == "AP", f"OrderResponseCode={peppol}")

        step("GET /v1/audit for o_old (expect order.requoted) and GET /v1/audit/verify")
        r_audit = c.get("/v1/audit", params={"subject_id": o_old["order_id"]})
        check(r_audit.status_code == 200, f"audit status {r_audit.status_code}")
        actions = [ev.get("action") for ev in r_audit.json()]
        check("order.requoted" in actions, f"audit actions include order.requoted ({len(actions)} events)")

        r_verify = c.get("/v1/audit/verify")
        check(r_verify.status_code == 200, f"audit/verify status {r_verify.status_code}")
        v = r_verify.json()
        check(v.get("ok") is True, json.dumps(v))

        served = "replay" if (h.get("replay") or {}).get("forced") else "live"
        print(
            f"\nREQUOTE {o_old['order_id']} -> {new_order['order_id']}; "
            f"409 requote_needed (EUR {old_total:.2f} -> EUR {new_total:.2f}); "
            f"Peppol OrderResponseCode {peppol}; "
            f"audit verify ok={v.get('ok')} ({v.get('checked', '?')} events checked); "
            f"served_from={served}"
        )
        print("\nREQUOTE BEAT OK")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument(
        "--date",
        default=None,
        help="delivery date (default: next feasible weekday on the gateway's clock)",
    )
    a = ap.parse_args(argv)
    try:
        return _run_flow(a.base, a.date)
    except _CheckFailed:
        return 1
    except Exception as exc:
        print(f"  FAIL {exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
