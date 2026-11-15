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
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aimo.models import Order, Quote

NS_UBL = "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CUSTOMIZATION_ID = "urn:fdc:peppol.eu:poacc:trns:order_response_advanced:3"
PROFILE_ID = "urn:fdc:peppol.eu:poacc:bis:advanced_ordering:3"
LINE_STATUS = {"AP": "5", "CA": "3", "RE": "7"}
# Catalog sales unit -> UN/ECE Recommendation 20 unit code (assumption; confirm with Valio Aimo master data)
UNITS = {"ST": "H87", "KPL": "H87", "PCS": "H87", "KI": "CS", "PK": "PK", "KG": "KGM", "L": "LTR", "LTR": "LTR"}
HELSINKI = timezone(timedelta(hours=2))

PLACEHOLDER_SELLER = {"endpoint_scheme": "0216", "endpoint_id": "PLACEHOLDER-SELLER-OVT", "name": "Valio Aimo (demo seller)"}

ET.register_namespace("", NS_UBL)
ET.register_namespace("cac", NS_CAC)
ET.register_namespace("cbc", NS_CBC)


def _cbc(parent: ET.Element, tag: str, text: Any = None, **attrs: str) -> ET.Element:
    e = ET.SubElement(parent, f"{{{NS_CBC}}}{tag}", attrs)
    if text is not None:
        e.text = str(text)
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


def _party(parent: ET.Element, role: str, p: dict[str, str]) -> None:
    party = _cac(_cac(parent, role), "Party")
    _cbc(party, "EndpointID", p["endpoint_id"], schemeID=p["endpoint_scheme"])
    _cbc(_cac(party, "PartyIdentification"), "ID", p["endpoint_id"], schemeID=p["endpoint_scheme"])
    _cbc(_cac(party, "PartyLegalEntity"), "RegistrationName", p["name"])


def header_code(order: Order) -> str:
    if order.response_code in ("AP", "CA", "RE"):
        return order.response_code
    if order.status == "rejected":
        return "RE"
    return "AB"  # received, not processed yet (pending approval / approved but not placed)


def order_response_xml(order: Order, quote: Quote, *, buyer: Optional[dict[str, str]] = None,
                       seller: Optional[dict[str, str]] = None, gtins: Optional[dict[str, str]] = None,
                       issued_at: Optional[datetime] = None) -> str:
    """Peppol T116 Order Response Advanced (UBL 2.3) for one gateway order, as a UTF-8 XML string."""
    seller = seller or PLACEHOLDER_SELLER
    buyer = buyer or {"endpoint_scheme": "0216", "endpoint_id": f"PLACEHOLDER-BUYER-{order.customer_id}",
                      "name": order.customer_id}
    gtins = gtins or {}
    ts = (issued_at or order.updated_at).astimezone(HELSINKI)
    code = header_code(order)

    root = ET.Element(f"{{{NS_UBL}}}OrderResponse")
    _cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", PROFILE_ID)
    _cbc(root, "ID", f"{order.order_id}-R")
    _cbc(root, "IssueDate", ts.date().isoformat())
    _cbc(root, "IssueTime", ts.strftime("%H:%M:%S%z")[:-2] + ":" + ts.strftime("%z")[-2:])
    _cbc(root, "OrderResponseCode", code)
    notes = {"AB": "Order received; waiting for the customer's approval policy / human approver.",
             "AP": "Order accepted without amendment.",
             "CA": "Order accepted with changes on line level (see lines).",
             "RE": "Order rejected."}
    _cbc(root, "Note", notes[code])
    if code == "RE":
        why = "; ".join(f"{l.product_id}: {l.note}" for l in order.response_lines if l.note) or \
              " ".join(h.message for h in order.decision.hits if h.outcome == "reject")
        if why:
            _cbc(root, "Note", f"Reason: {why}"[:500])
    if code == "CA":
        changed = "; ".join(f"line {i} ({l.product_id}): {l.note}" for i, l in
                            enumerate([by for q in quote.lines for by in order.response_lines if by.product_id == q.product_id], 1)
                            if l.code != "AP" and l.note)
        if changed:
            _cbc(root, "Note", changed[:500])
    # T116 has no AdditionalDocumentReference: the signed quote is referenced in a header note
    _cbc(root, "Note", f"Signed quote {quote.quote_id}, sha256 {quote.quote_hash or '-'}, JWS kid {quote.kid or '-'}")
    _cbc(root, "DocumentCurrencyCode", quote.currency)
    _cbc(root, "CustomerReference", quote.quote_id)
    _cbc(_cac(root, "OrderReference"), "ID", order.order_id)
    _party(root, "SellerSupplierParty", seller)
    _party(root, "BuyerCustomerParty", buyer)
    period = _cac(_cac(root, "Delivery"), "PromisedDeliveryPeriod")
    _cbc(period, "StartDate", quote.delivery_date.isoformat())
    _cbc(period, "EndDate", quote.delivery_date.isoformat())

    if code == "CA":  # all lines (PEPPOL-T116-R007); AP/RE/AB: none (R008-R010)
        by_pid = {l.product_id: l for l in order.response_lines}
        for i, ql in enumerate(quote.lines, start=1):
            rl = by_pid.get(ql.product_id)
            lc = rl.code if rl else "AP"
            ol = _cac(root, "OrderLine")
            li = _cac(ol, "LineItem")
            _cbc(li, "ID", i)
            _cbc(li, "LineStatusCode", LINE_STATUS[lc])
            unit = UNITS.get((ql.sales_unit or "").upper(), "C62")
            if lc == "CA":
                _cbc(li, "Quantity", _num(rl.qty_confirmed), unitCode=unit)
            price = _cac(li, "Price")
            _cbc(price, "PriceAmount", _num(ql.unit_price), currencyID=quote.currency)
            item = _cac(li, "Item")
            _cbc(item, "Name", ql.product_name[:200])
            _cbc(_cac(item, "SellersItemIdentification"), "ID", ql.product_id)
            if gtins.get(ql.product_id):
                _cbc(_cac(item, "StandardItemIdentification"), "ID", gtins[ql.product_id], schemeID="0160")
            _cbc(_cac(ol, "OrderLineReference"), "LineID", i)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ------------------------------------------------------------------ PEPPOL-T116-* rules (ported, not the full schematron)
def check_t116_rules(xml_text: str) -> list[str]:
    """The T116-specific schematron asserts from OpenPEPPOL/poacc-upgrade-3 rules/sch/parts/PEPPOL-M-T116.sch
    (R001-R010), re-implemented in Python. NOT a substitute for the official validator: the generated
    T116-basic.sch (cardinalities, code lists) and the common rules are not run here."""
    ns = {"ubl": NS_UBL, "cac": NS_CAC, "cbc": NS_CBC}
    r = ET.fromstring(xml_text.encode())
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
    extra = sorted({e.tag.split("}")[1] for e in r.iter() if e is not r and e.tag.split("}")[1] not in T116_ELEMENTS})
    if extra:
        errs.append(f"not in T116 syntax: {', '.join(extra)}")
    return errs


# Element names in the Peppol T116 syntax tree (docs.peppol.eu/poacc/upgrade-3/syntax/OrderResponseAdvanced/tree/,
# read 14 Nov 2026). A name check only: it does not check WHERE an element appears.
T116_ELEMENTS = set("""CustomizationID ProfileID ID SalesOrderID IssueDate IssueTime OrderResponseCode Note
DocumentCurrencyCode CustomerReference OrderReference OrderChangeDocumentReference SellerSupplierParty Party EndpointID
PartyIdentification PartyLegalEntity RegistrationName BuyerCustomerParty Delivery PromisedDeliveryPeriod StartDate
StartTime EndDate EndTime OrderLine LineItem LineStatusCode Quantity MaximumBackorderQuantity Price PriceAmount
BaseQuantity Item Name BuyersItemIdentification SellersItemIdentification StandardItemIdentification
SellerSubstitutedLineItem CommodityClassification ItemClassificationCode ClassifiedTaxCategory Percent TaxScheme
AdditionalItemProperty NameCode Value ValueQuantity ValueQualifier OrderLineReference LineID""".split())
