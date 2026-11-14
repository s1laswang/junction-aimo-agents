"""Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116) export. Offline.

The UBL 2.3 XSD check runs only when AIMO_UBL_XSD points at UBL-OrderResponse-2.3.xsd and lxml is
installed (uv run --with lxml ...). The official Peppol schematron is not run (see aimo/backend/peppol.py)."""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend.peppol import CUSTOMIZATION_ID, NS_CAC, NS_CBC, PROFILE_ID, check_t116_rules, order_response_xml

from test_gateway import APPROVER, H, FixtureClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from export_peppol_order_response import sample  # noqa: E402

NS = {"cac": NS_CAC, "cbc": NS_CBC}


def _parse(x):
    return ET.fromstring(x.encode())


@pytest.mark.parametrize("code,n_lines", [("AP", 0), ("CA", 2), ("RE", 0)])
def test_sample_order_responses_follow_t116_rules(code, n_lines):
    x = order_response_xml(*sample(code))
    assert check_t116_rules(x) == []
    r = _parse(x)
    assert r.findtext("cbc:CustomizationID", None, NS) == CUSTOMIZATION_ID
    assert r.findtext("cbc:ProfileID", None, NS) == PROFILE_ID
    assert r.findtext("cbc:OrderResponseCode", None, NS) == code
    assert len(r.findall("cac:OrderLine", NS)) == n_lines


def test_ca_lines_carry_status_and_changed_quantity():
    r = _parse(order_response_xml(*sample("CA")))
    lines = r.findall("cac:OrderLine/cac:LineItem", NS)
    assert [l.findtext("cbc:LineStatusCode", None, NS) for l in lines] == ["5", "3"]
    q = lines[1].find("cbc:Quantity", NS)
    assert q.text == "30" and q.get("unitCode") == "H87"


def test_rule_checker_catches_violations():
    x = order_response_xml(*sample("CA")).replace(">CA<", ">AP<", 1)   # AP must not carry lines
    assert "PEPPOL-T116-R008" in check_t116_rules(x)


def test_committed_sample_files_are_current():
    for code in ("AP", "CA", "RE"):
        f = ROOT / "docs" / "peppol" / f"sample-order-response-advanced-{code}.xml"
        assert f.read_text(encoding="utf-8") == order_response_xml(*sample(code))


@pytest.mark.skipif(not os.environ.get("AIMO_UBL_XSD"), reason="set AIMO_UBL_XSD=.../UBL-OrderResponse-2.3.xsd")
def test_samples_validate_against_ubl_23_xsd():
    etree = pytest.importorskip("lxml.etree")
    schema = etree.XMLSchema(etree.parse(os.environ["AIMO_UBL_XSD"]))
    for code in ("AP", "CA", "RE"):
        assert schema.validate(etree.fromstring(order_response_xml(*sample(code)).encode())), schema.error_log


def test_gateway_route_ab_then_ap(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    bc.set_client(FixtureClient())
    try:
        from aimo.gateway.app import create_app
        api = TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"})
        q = api.post("/v1/quotes", headers=H, json={"delivery_date": "2026-11-16",
                                                    "lines": [{"product_id": "464306", "qty_sales_units": 1000}]}).json()
        o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
        r = api.get(f"/v1/orders/{o['order_id']}/peppol-order-response.xml")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
        assert _parse(r.text).findtext("cbc:OrderResponseCode", None, NS) == "AB" and check_t116_rules(r.text) == []
        api.post(f"/v1/approvals/{o['order_id']}/decision", json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER})
        x = api.get(f"/v1/orders/{o['order_id']}/peppol-order-response.xml").text
        assert _parse(x).findtext("cbc:OrderResponseCode", None, NS) == "AP" and check_t116_rules(x) == []
        assert q["quote_hash"] in x
        assert api.get("/v1/orders/nope/peppol-order-response.xml").status_code == 404
    finally:
        bc.set_client(None)
