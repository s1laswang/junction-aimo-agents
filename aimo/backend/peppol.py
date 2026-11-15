"""Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116) export for a gateway Order.

Spec: https://docs.peppol.eu/poacc/upgrade-3/profiles/65-advanced-ordering/ (section 7 and 10.2)
  CustomizationID urn:fdc:peppol.eu:poacc:trns:order_response_advanced:3
  ProfileID       urn:fdc:peppol.eu:poacc:bis:advanced_ordering:3
  UBL 2.3 namespace urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2
Header OrderResponseCode (UNCL 4343 subset): AB received/not processed, AP accepted, CA accepted
with changes, RE rejected. AP / RE / AB carry NO lines (PEPPOL-T116-R008..R010); CA carries ALL
lines (R007) with LineStatusCode 5 accepted, 3 changed (new quantity), 7 not accepted.

Mapping from our gateway: Order.response_code AP/CA/RE (aimo.backend.orders.confirm_lines);
an order still waiting for policy/human approval is exported as AB. Party identifiers are
PLACEHOLDERS (no real Valio Aimo / customer Peppol IDs are known to us): pass real ones in
production. Unit codes map the catalog's sales units to UN/ECE Rec 20 (assumption, see UNITS).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from aimo.models import Order, Quote

NS_UBL = "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CUSTOMIZATION_ID = "urn:fdc:peppol.eu:poacc:trns:order_response_advanced:3"
PROFILE_ID = "urn:fdc:peppol.eu:poacc:bis:advanced_ordering:3"
LINE_STATUS = {"AP": "5", "CA": "3", "RE": "7"}
UNITS = {"ST": "H87", "KPL": "H87", "PCS": "H87", "KI": "CS", "PK": "PK", "KG": "KGM", "L": "LTR", "LTR": "LTR"}
HELSINKI = timezone(timedelta(hours=2))
MAX_XML_CHARS = 2_000_000

PLACEHOLDER_SELLER = {"endpoint_scheme": "0216", "endpoint_id": "PLACEHOLDER-SELLER-OVT", "name": "Valio Aimo (demo seller)"}

_BAD_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_DTD_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

ET.register_namespace("", NS_UBL)
ET.register_namespace("cac", NS_CAC)
ET.register_namespace("cbc", NS_CBC)


def _xml_str(val: Any) -> str:
    if val is None:
        return ""
    return _BAD_XML_RE.sub("", str(val))


def _clean_nonempty(val: Any, fallback: str) -> str:
    if val is None or isinstance(val, bool):
        return fallback
    s = _xml_str(val).strip()
    return s if s else fallback


def _cbc(parent: ET.Element, tag: str, text: Any = None, **attrs: Any) -> ET.Element:
    clean_attrs = {_xml_str(k): _xml_str(v) for k, v in attrs.items() if v is not None}
    e = ET.SubElement(parent, f"{{{NS_CBC}}}{tag}", clean_attrs)
    if text is not None:
        e.text = _xml_str(text)
    return e


def _cac(parent: ET.Element, tag: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{NS_CAC}}}{tag}")


def _num(x: float) -> str:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("Peppol numeric value must be finite")
    try:
        val = float(x)
    except OverflowError as exc:
        raise ValueError("Peppol numeric value must be finite") from exc
    if not math.isfinite(val):
        raise ValueError("Peppol numeric value must be finite")
    if val == 0.0:
        return "0"
    s = f"{val:.4f}".rstrip("0").rstrip(".") if val != int(val) else str(int(val))
    return "0" if s in ("", "-0") else s


def _party(parent: ET.Element, role: str, p: Any) -> None:
    src = p if isinstance(p, dict) else {}
    is_seller = role == "SellerSupplierParty"
    def_id = PLACEHOLDER_SELLER["endpoint_id"] if is_seller else "PLACEHOLDER-BUYER-UNKNOWN"
    def_name = PLACEHOLDER_SELLER["name"] if is_seller else "Unknown Buyer"
    scheme = _clean_nonempty(src.get("endpoint_scheme"), PLACEHOLDER_SELLER["endpoint_scheme"])
    eid = _clean_nonempty(src.get("endpoint_id"), def_id)
    name = _clean_nonempty(src.get("name"), def_name)
    party = _cac(_cac(parent, role), "Party")
    _cbc(party, "EndpointID", eid, schemeID=scheme)
    _cbc(_cac(party, "PartyIdentification"), "ID", eid, schemeID=scheme)
    _cbc(_cac(party, "PartyLegalEntity"), "RegistrationName", name)


def _to_helsinki(dt: Any) -> datetime:
    if not isinstance(dt, datetime):
        raise ValueError("timestamp must be a datetime")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HELSINKI)


def header_code(order: Order) -> str:
    if order is None or not isinstance(order, Order):
        raise ValueError("order must be an Order")
    if order.response_code in ("AP", "CA", "RE"):
        return order.response_code
    if order.status == "rejected":
        return "RE"
    return "AB"


def order_response_xml(order: Order, quote: Quote, *, buyer: Optional[dict[str, str]] = None,
                       seller: Optional[dict[str, str]] = None, gtins: Optional[dict[str, str]] = None,
                       issued_at: Optional[datetime] = None) -> str:
    """Peppol T116 Order Response Advanced (UBL 2.3) for one gateway order, as a UTF-8 XML string."""
    if order is None or not isinstance(order, Order):
        raise ValueError("order must be an Order")
    if quote is None or not isinstance(quote, Quote):
        raise ValueError("quote must be a Quote")

    cid = _clean_nonempty(order.customer_id, "UNKNOWN-CUSTOMER")
    oid = _clean_nonempty(order.order_id, "ORD-UNKNOWN")
    qid = _clean_nonempty(quote.quote_id, "Q-UNKNOWN")
    cur = _clean_nonempty(quote.currency, "EUR")
    qhash = _clean_nonempty(quote.quote_hash, "-")
    qkid = _clean_nonempty(quote.kid, "-")

    default_buyer = {"endpoint_scheme": "0216", "endpoint_id": f"PLACEHOLDER-BUYER-{cid}", "name": cid}
    seller_cfg = (
        {**PLACEHOLDER_SELLER, **{k: v for k, v in seller.items() if isinstance(k, str) and _clean_nonempty(v, "")}}
        if isinstance(seller, dict)
        else dict(PLACEHOLDER_SELLER)
    )
    buyer_cfg = (
        {**default_buyer, **{k: v for k, v in buyer.items() if isinstance(k, str) and _clean_nonempty(v, "")}}
        if isinstance(buyer, dict)
        else default_buyer
    )

    gtin_map: dict[str, str] = {}
    if isinstance(gtins, dict):
        for k, v in gtins.items():
            if isinstance(k, str) and isinstance(v, str):
                pk = _xml_str(k).strip()
                gv = _xml_str(v).strip()
                if pk and gv:
                    gtin_map[pk] = gv

    raw_ts = issued_at if issued_at is not None else (
        order.updated_at if order.updated_at is not None else order.created_at
    )
    ts = _to_helsinki(raw_ts)
    dd = quote.delivery_date
    if isinstance(dd, datetime):
        delivery_iso = dd.date().isoformat()
    elif isinstance(dd, date):
        delivery_iso = dd.isoformat()
    else:
        raise ValueError("quote.delivery_date must be a date")

    code = header_code(order)
    resp_lines = list(order.response_lines) if isinstance(order.response_lines, (list, tuple)) else []
    quote_lines = list(quote.lines) if isinstance(quote.lines, (list, tuple)) else []

    root = ET.Element(f"{{{NS_UBL}}}OrderResponse")
    _cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", PROFILE_ID)
    _cbc(root, "ID", f"{oid}-R")
    _cbc(root, "IssueDate", ts.date().isoformat())
    _cbc(root, "IssueTime", ts.strftime("%H:%M:%S%z")[:-2] + ":" + ts.strftime("%z")[-2:])
    _cbc(root, "OrderResponseCode", code)
    notes = {"AB": "Order received; waiting for the customer's approval policy / human approver.",
             "AP": "Order accepted without amendment.",
             "CA": "Order accepted with changes on line level (see lines).",
             "RE": "Order rejected."}
    _cbc(root, "Note", notes[code])
    if code == "RE":
        hits = (
            list(order.decision.hits)
            if getattr(order, "decision", None) is not None and isinstance(order.decision.hits, (list, tuple))
            else []
        )
        why = (
            "; ".join(
                f"{_clean_nonempty(l.product_id, '?')}: {_xml_str(l.note).strip()}"
                for l in resp_lines
                if getattr(l, "note", None) and _xml_str(l.note).strip()
            )
            or " ".join(
                _xml_str(h.message).strip()
                for h in hits
                if getattr(h, "outcome", None) == "reject" and _xml_str(getattr(h, "message", "")).strip()
            )
            or (_xml_str(order.approval_note).strip() if order.approval_note else "")
        )
        if why:
            _cbc(root, "Note", f"Reason: {why}"[:500])
    if code == "CA":
        changed = "; ".join(
            f"line {i} ({_clean_nonempty(l.product_id, '?')}): {_xml_str(l.note).strip()}"
            for i, l in enumerate(
                [by for q in quote_lines for by in resp_lines if by.product_id == q.product_id], 1
            )
            if l.code != "AP" and getattr(l, "note", None) and _xml_str(l.note).strip()
        )
        if changed:
            _cbc(root, "Note", changed[:500])
    if code != "RE" and order.approval_note:
        an = _xml_str(order.approval_note).strip()
        if an:
            _cbc(root, "Note", f"Approver note: {an}"[:500])
    _cbc(root, "Note", f"Signed quote {qid}, sha256 {qhash}, JWS kid {qkid}")
    _cbc(root, "DocumentCurrencyCode", cur)
    _cbc(root, "CustomerReference", qid)
    _cbc(_cac(root, "OrderReference"), "ID", oid)
    _party(root, "SellerSupplierParty", seller_cfg)
    _party(root, "BuyerCustomerParty", buyer_cfg)
    period = _cac(_cac(root, "Delivery"), "PromisedDeliveryPeriod")
    _cbc(period, "StartDate", delivery_iso)
    _cbc(period, "EndDate", delivery_iso)

    if code == "CA":
        by_pid = {l.product_id: l for l in resp_lines}
        for i, ql in enumerate(quote_lines, start=1):
            rl = by_pid.get(ql.product_id)
            lc = rl.code if (rl is not None and rl.code in LINE_STATUS) else "AP"
            ol = _cac(root, "OrderLine")
            li = _cac(ol, "LineItem")
            _cbc(li, "ID", i)
            _cbc(li, "LineStatusCode", LINE_STATUS[lc])
            unit = UNITS.get(str(ql.sales_unit or "").strip().upper(), "C62")
            if lc == "CA" and rl is not None:
                _cbc(li, "Quantity", _num(rl.qty_confirmed), unitCode=unit)
            price = _cac(li, "Price")
            _cbc(price, "PriceAmount", _num(ql.unit_price), currencyID=cur)
            item = _cac(li, "Item")
            pid_clean = _clean_nonempty(ql.product_id, str(i))
            pname_clean = _clean_nonempty(ql.product_name, pid_clean)[:200]
            _cbc(item, "Name", pname_clean)
            _cbc(_cac(item, "SellersItemIdentification"), "ID", pid_clean)
            gtin_val = gtin_map.get(pid_clean) or (gtin_map.get(ql.product_id) if isinstance(ql.product_id, str) else None)
            if gtin_val:
                _cbc(_cac(item, "StandardItemIdentification"), "ID", gtin_val, schemeID="0160")
            _cbc(_cac(ol, "OrderLineReference"), "LineID", i)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _local_tag(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def check_t116_rules(xml_text: str) -> list[str]:
    """The T116-specific schematron asserts from OpenPEPPOL/poacc-upgrade-3 rules/sch/parts/PEPPOL-M-T116.sch
    (R001-R010), re-implemented in Python. NOT a substitute for the official validator: the generated
    T116-basic.sch (cardinalities, code lists) and the common rules are not run here."""
    if not isinstance(xml_text, str) or not xml_text.strip() or len(xml_text) > MAX_XML_CHARS:
        return ["invalid_xml"]
    if _DTD_ENTITY_RE.search(xml_text):
        return ["invalid_xml"]
    try:
        r = ET.fromstring(xml_text.encode("utf-8"))
    except (ET.ParseError, UnicodeEncodeError, ValueError):
        return ["invalid_xml"]
    ns = {"ubl": NS_UBL, "cac": NS_CAC, "cbc": NS_CBC}
    errs = []
    cid = (r.findtext("cbc:CustomizationID", "", ns) or "").strip()
    if not cid.startswith(CUSTOMIZATION_ID):
        errs.append("PEPPOL-T116-R006")
    cur = r.findtext("cbc:DocumentCurrencyCode", None, ns)
    if any(p.get("currencyID") != cur for p in r.iter(f"{{{NS_CBC}}}PriceAmount")):
        errs.append("PEPPOL-T116-R005")
    for role, rid in (("BuyerCustomerParty", "R001"), ("SellerSupplierParty", "R002")):
        p = r.find(f"cac:{role}/cac:Party", ns)
        if p is None or not (p.findtext("cac:PartyLegalEntity/cbc:RegistrationName", None, ns)
                             or p.findtext("cac:PartyIdentification/cbc:ID", None, ns)):
            errs.append(f"PEPPOL-T116-{rid}")
    for per in r.iter(f"{{{NS_CAC}}}PromisedDeliveryPeriod"):
        s, e = per.findtext("cbc:StartDate", None, ns), per.findtext("cbc:EndDate", None, ns)
        if s and e and e < s:
            errs.append("PEPPOL-T116-R004")
    code = (r.findtext("cbc:OrderResponseCode", "", ns) or "").strip()
    n = len(r.findall("cac:OrderLine", ns))
    if code == "CA" and n == 0:
        errs.append("PEPPOL-T116-R007")
    for c, rid in (("AP", "R008"), ("RE", "R009"), ("AB", "R010")):
        if code == c and n:
            errs.append(f"PEPPOL-T116-{rid}")
    ids = [li.findtext("cbc:ID", "", ns) for li in r.findall("cac:OrderLine/cac:LineItem", ns)]
    if len(ids) != len(set(ids)):
        errs.append("PEPPOL-T116-R003")
    extra = sorted({_local_tag(e.tag) for e in r.iter() if e is not r and _local_tag(e.tag) not in T116_ELEMENTS})
    if extra:
        errs.append(f"not in T116 syntax: {', '.join(extra)}")
    return errs


T116_ELEMENTS = set("""CustomizationID ProfileID ID SalesOrderID IssueDate IssueTime OrderResponseCode Note
DocumentCurrencyCode CustomerReference OrderReference OrderChangeDocumentReference SellerSupplierParty Party EndpointID
PartyIdentification PartyLegalEntity RegistrationName BuyerCustomerParty Delivery PromisedDeliveryPeriod StartDate
StartTime EndDate EndTime OrderLine LineItem LineStatusCode Quantity MaximumBackorderQuantity Price PriceAmount
BaseQuantity Item Name BuyersItemIdentification SellersItemIdentification StandardItemIdentification
SellerSubstitutedLineItem CommodityClassification ItemClassificationCode ClassifiedTaxCategory Percent TaxScheme
AdditionalItemProperty NameCode Value ValueQuantity ValueQualifier OrderLineReference LineID""".split())
