"""Write Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116) XML.

    uv run python scripts/export_peppol_order_response.py --sample            # docs/peppol/*.xml from a fixed demo order
    uv run python scripts/export_peppol_order_response.py --order ORD-...     # a real order from data/state.sqlite
    uv run --with lxml python scripts/export_peppol_order_response.py --sample --xsd /path/UBL-2.3/xsd/maindoc/UBL-OrderResponse-2.3.xsd

Validation status: UBL 2.3 XSD (optional, --xsd, needs lxml) + the PEPPOL-T116-R001..R010 rules ported
to Python. The official Peppol schematron (incl. generated T116-basic.sch) is NOT run.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aimo.backend.peppol import check_t116_rules, order_response_xml  # noqa: E402
from aimo.models import Order, OrderResponseLine, PolicyDecision, Quote, QuoteLine  # noqa: E402

HEL = timezone(timedelta(hours=2))
T = datetime(2026, 11, 14, 14, 20, tzinfo=HEL)


def sample(code: str) -> tuple[Order, Quote]:
    """Demo order (synthetic, mirrors the hero scenario): 1,000 x 464306 @ EUR 1.01 + 50 x 469363."""
    q = Quote(quote_id="Q-20261114-demo0001", customer_id="hotel-aurora", agent_id="hotel-aurora/buyer-1",
              delivery_date=date(2026, 11, 16),
              lines=[QuoteLine(product_id="464306", product_name="Valio Hyvä suomalainen Arki Eila semi skimmed milk drink 1 l lactose free",
                               qty_sales_units=1000, sales_unit="ST", unit_price=1.01, line_total=1010.0, tier="best"),
                     QuoteLine(product_id="469363", product_name="Arla Semi skimmed milkdrink 1 L UHT lactose free Suomesta",
                               qty_sales_units=50, sales_unit="ST", unit_price=1.12, line_total=56.0, tier="good")],
              total_net=1066.0, created_at=T, expires_at=T + timedelta(minutes=15), quote_hash="0" * 64, kid="demo-kid")
    rl = {"AP": [OrderResponseLine(product_id="464306", code="AP", qty_confirmed=1000, note="confirmed"),
                 OrderResponseLine(product_id="469363", code="AP", qty_confirmed=50, note="confirmed")],
          "CA": [OrderResponseLine(product_id="464306", code="AP", qty_confirmed=1000, note="confirmed"),
                 OrderResponseLine(product_id="469363", code="CA", qty_confirmed=30,
                                   note="partial stock at placement: 30 of 50 ST confirmed")],
          "RE": [OrderResponseLine(product_id="464306", code="RE", qty_confirmed=0, note="not deliverable at placement: cutoff_passed"),
                 OrderResponseLine(product_id="469363", code="RE", qty_confirmed=0, note="not deliverable at placement: cutoff_passed")]}[code]
    o = Order(order_id="ORD-20261114-demo0001", quote_id=q.quote_id, quote_hash=q.quote_hash, customer_id=q.customer_id,
              agent_id=q.agent_id, status="rejected" if code == "RE" else "placed",
              decision=PolicyDecision(outcome="needs_approval", policy_id="hotel-aurora", policy_version="demo", hits=[]),
              approved_by="anna@hotel-aurora.example", response_code=code, response_lines=rl, created_at=T, updated_at=T)
    return o, q


def xsd_validate(xml_text: str, xsd: str) -> list[str]:
    from lxml import etree
    schema = etree.XMLSchema(etree.parse(xsd))
    doc = etree.fromstring(xml_text.encode())
    return [] if schema.validate(doc) else [str(e) for e in schema.error_log]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--order")
    ap.add_argument("--out", default=str(ROOT / "docs" / "peppol"))
    ap.add_argument("--xsd", help="UBL-OrderResponse-2.3.xsd for schema validation (needs lxml)")
    a = ap.parse_args(argv)
    docs: dict[str, str] = {}
    if a.sample:
        for code in ("AP", "CA", "RE"):
            docs[f"sample-order-response-advanced-{code}.xml"] = order_response_xml(*sample(code))
    if a.order:
        from aimo.backend import orders as store
        o = store.get_order(a.order)
        q = store.get_quote(o.quote_id) if o else None
        if not (o and q):
            print(f"order {a.order} not found", file=sys.stderr)
            return 2
        docs[f"{o.order_id}-order-response-advanced.xml"] = order_response_xml(o, q)
    if not docs:
        ap.error("--sample or --order")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name, x in docs.items():
        (out / name).write_text(x, encoding="utf-8")
        errs = check_t116_rules(x) + (xsd_validate(x, a.xsd) if a.xsd else [])
        bad += bool(errs)
        print(f"{out / name}: T116 rules {'OK' if not errs else errs}" + ("; UBL 2.3 XSD checked" if a.xsd else "; XSD not checked"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
