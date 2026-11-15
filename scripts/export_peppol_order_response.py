"""Write Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116) XML.

    uv run python scripts/export_peppol_order_response.py --sample            # docs/peppol/*.xml from a fixed demo order
    uv run python scripts/export_peppol_order_response.py --order ORD-...     # a real order from data/state.sqlite
    uv run --with lxml python scripts/export_peppol_order_response.py --sample --xsd /path/UBL-2.3/xsd/maindoc/UBL-OrderResponse-2.3.xsd

Validation status: UBL 2.3 XSD (optional, --xsd, needs lxml) + the PEPPOL-T116-R001..R010 rules ported
to Python. The official Peppol schematron (incl. generated T116-basic.sch) is NOT run.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aimo.backend.peppol import check_t116_rules, order_response_xml
from aimo.models import Order, OrderResponseLine, PolicyDecision, Quote, QuoteLine

HEL = timezone(timedelta(hours=2))
T = datetime(2026, 11, 14, 14, 20, tzinfo=HEL)
_VALID_SAMPLE_CODES = ("AP", "CA", "RE")
_MAX_ORDER_ID_CHARS = 128


def _has_ctrl(s: str) -> bool:
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in s)


def _clean_argv(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        return None
    if isinstance(argv, (str, bytes, bytearray, bool)) or not isinstance(argv, (list, tuple)):
        raise ValueError("argv must be a list or tuple of strings")
    out: list[str] = []
    for item in argv:
        if isinstance(item, bool) or not isinstance(item, str) or "\x00" in item:
            raise ValueError("argv items must be strings without null bytes")
        if any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise ValueError("argv items must not contain lone surrogates")
        out.append(item)
    return out


def _validate_order_id(raw: Any) -> str:
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueError("order_id must be a non-empty string")
    if not raw or len(raw) > _MAX_ORDER_ID_CHARS or _has_ctrl(raw) or any(c.isspace() for c in raw):
        raise ValueError("order_id is empty, too long, or contains whitespace/control characters")
    if "/" in raw or "\\" in raw or ".." in raw or raw in (".", ".."):
        raise ValueError("order_id must not contain path separators or '..'")
    return raw


def _validate_out_dir(raw: Any) -> Path:
    if isinstance(raw, bool) or not isinstance(raw, (Path, str)):
        raise ValueError("out path must be a Path or string")
    s = str(raw)
    if not s.strip() or _has_ctrl(s):
        raise ValueError("out path is empty or contains control characters")
    path = raw if isinstance(raw, Path) else Path(s)
    if path.exists() and not path.is_dir():
        raise ValueError("out path exists and is not a directory")
    return path


def _validate_xsd_path(raw: Any) -> Path:
    if isinstance(raw, bool) or not isinstance(raw, (Path, str)):
        raise ValueError("xsd path must be a Path or string")
    s = str(raw)
    if not s.strip() or _has_ctrl(s):
        raise ValueError("xsd path is empty or contains control characters")
    path = raw if isinstance(raw, Path) else Path(s)
    if not path.is_file():
        raise ValueError(f"xsd schema file not found: {path}")
    return path


def sample(code: str) -> tuple[Order, Quote]:
    """Demo order (synthetic, mirrors the hero scenario): 1,000 x 464306 @ EUR 1.01 + 50 x 469363."""
    if isinstance(code, bool) or not isinstance(code, str) or code not in _VALID_SAMPLE_CODES:
        raise ValueError(f"sample code must be one of {_VALID_SAMPLE_CODES}, got {code!r}")
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
    if isinstance(xml_text, bool) or not isinstance(xml_text, str) or not xml_text.strip() or "\x00" in xml_text:
        raise ValueError("xml_text must be a non-empty string without null bytes")
    xsd_path = _validate_xsd_path(xsd)
    from lxml import etree
    try:
        schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError, OSError) as exc:
        raise ValueError(f"invalid XSD schema {xsd_path}: {exc}") from exc
    try:
        doc = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        return [str(exc)]
    return [] if schema.validate(doc) else [str(e) for e in schema.error_log]


def main(argv: list[str] | None = None) -> int:
    clean_args = _clean_argv(argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--order")
    ap.add_argument("--out", default=str(ROOT / "docs" / "peppol"))
    ap.add_argument("--xsd", help="UBL-OrderResponse-2.3.xsd for schema validation (needs lxml)")
    a = ap.parse_args(clean_args)
    out = _validate_out_dir(a.out)
    xsd_path = str(_validate_xsd_path(a.xsd)) if a.xsd is not None else None
    docs: dict[str, str] = {}
    if a.sample:
        for code in _VALID_SAMPLE_CODES:
            docs[f"sample-order-response-advanced-{code}.xml"] = order_response_xml(*sample(code))
    if a.order is not None:
        order_id = _validate_order_id(a.order)
        from aimo.backend import orders as store
        o = store.get_order(order_id)
        q = store.get_quote(o.quote_id) if o else None
        if not (o and q):
            print(f"order {order_id} not found", file=sys.stderr)
            return 2
        safe_oid = _validate_order_id(o.order_id)
        docs[f"{safe_oid}-order-response-advanced.xml"] = order_response_xml(o, q)
    if not docs:
        ap.error("--sample or --order")
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name, x in docs.items():
        target = out / name
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            tmp.write_text(x, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        errs = check_t116_rules(x) + (xsd_validate(x, xsd_path) if xsd_path else [])
        bad += bool(errs)
        print(f"{target}: T116 rules {'OK' if not errs else errs}" + ("; UBL 2.3 XSD checked" if xsd_path else "; XSD not checked"))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
