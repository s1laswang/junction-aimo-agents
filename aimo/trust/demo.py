"""Trust-layer walkthrough of the M1 slice, no network:  uv run python -m aimo.trust.demo
Builds the hotel-aurora 1,000 L milk quote, signs and verifies it, evaluates the policy, records the
audit trail, approves, renders the story, then tampers and shows verify() catching it.
Uses a throwaway audit DB (pass --real to write to aimo.config.STATE_DB)."""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aimo.models import Need, Order, Quote, QuoteLine
from aimo.trust.approvals import check_approval, log_decision, log_order, log_quote, log_request
from aimo.trust.audit import AuditLog, audit
from aimo.trust.policy import evaluate, explain, get_mandate
from aimo.trust.signing import sign_quote, verify_quote


def main(argv: list[str]) -> None:
    log = audit if "--real" in argv else AuditLog(Path(tempfile.mkdtemp()) / "demo-audit.sqlite")
    now = datetime.now(timezone.utc)
    monday = (now + timedelta(days=(7 - now.weekday()) % 7 or 7)).date()
    need = Need(raw_text="We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
                customer_id="hotel-aurora", product_query="lactose-free milk", required_claims=["FREE_FROM_LACTOSE"],
                quantity=1000, unit="L", delivery_by=monday)
    q = Quote(quote_id="Q-DEMO-1", customer_id="hotel-aurora", agent_id="hotel-aurora/buyer-1", delivery_date=monday,
              lines=[QuoteLine(product_id="464306",
                               product_name="Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free",
                               qty_sales_units=1000, sales_unit="ST", unit_price=1.01, line_total=1010.0, tier="best")],
              total_net=1010.0, created_at=now, expires_at=now + timedelta(minutes=15), need=need)
    run_id, order_id = "RUN-DEMO-1", "O-DEMO-1"
    log_request(need, "buyer-1", run_id, audit_log=log)
    q = sign_quote(q)
    print(f"quote_hash {q.quote_hash}\nkid        {q.kid}\nverify_quote -> {verify_quote(q)}")
    log_quote(q, run_id=run_id, audit_log=log)
    d = evaluate(q, get_mandate("hotel-aurora", "buyer-1"), audit_log=log)
    print("\n" + explain(d))
    for h in d.hits:
        print(f"  {h.outcome:<15} {h.rule_id:<26} {h.message}")
    log_decision(q, d, order_id=order_id, audit_log=log)
    order = Order(order_id=order_id, quote_id=q.quote_id, quote_hash=q.quote_hash, customer_id=q.customer_id,
                  agent_id=q.agent_id, status="pending_approval", decision=d, created_at=now, updated_at=now)
    log_order(order, q, audit_log=log)
    r = check_approval(q, d, "anna@hotel-aurora.example", "procurement_manager", q.quote_hash,
                       note="OK, conference week", order_id=order_id, audit_log=log)
    print(f"\ncheck_approval -> {r}")
    order = order.model_copy(update={"status": "placed", "approved_by": "anna@hotel-aurora.example", "response_code": "AP"})
    log_order(order, q, audit_log=log)
    print(f"verify -> {log.verify()}\n")
    print(log.render(order_id))
    seq = next(e.seq for e in log.events(order_id) if e.action == "approval.verified")
    t = log.tamper_for_demo(seq, summary="mallory@evil.example (procurement manager) approved quote Q-DEMO-1.")
    print(f"TAMPER event #{seq}: '{t['new_summary']}'")
    print(f"verify -> {log.verify()}")
    log.untamper_for_demo(seq)
    print(f"restored, verify -> {log.verify()}")


if __name__ == "__main__":
    main(sys.argv[1:])
