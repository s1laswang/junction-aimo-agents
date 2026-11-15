"""Hardening tests for peppol.py, netcheck.py, and the Judge Issue 13 input-validation acceptance matrix."""
from __future__ import annotations

import asyncio
import json
import math
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import aimo.catalog as cat_pkg
from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store, peppol
from aimo.backend.peppol import (
    CUSTOMIZATION_ID,
    NS_CAC,
    NS_CBC,
    NS_UBL,
    PROFILE_ID,
    _cbc,
    _party,
    check_t116_rules,
    header_code,
    order_response_xml,
)
from aimo.catalog import netcheck, plan as plan_mod, textsearch
from aimo.catalog.plan import _late_split, fill, plan_need
from aimo.gateway import fallback_catalog, human_auth, mcp_server as mcp_mod, resilience, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Candidate, Need, Offer, Order, OrderResponseLine, Plan, PolicyDecision, ProductSummary, Quote, QuoteLine, RuleHit
from aimo.trust import policy
from test_gateway import APPROVER, H, FixtureClient
from toywholesaler.backend import CATALOG as TOY_CATALOG, StaticBackendClient, product_json as toy_product_json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from export_peppol_order_response import sample

NS = {"ubl": NS_UBL, "cac": NS_CAC, "cbc": NS_CBC}
HEL = timezone(timedelta(hours=2))
DELIVERY_ISO = "2026-11-16"
DELIVERY_DATE = date(2026, 11, 16)
IDENT = {"agent_id": "buyer-1", "customer_id": "hotel-aurora"}
PIN = "test-pin"

ALL_XML_FORBIDDEN_CONTROL_CHARS = "".join(
    chr(c) for c in [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20)]
)
ALL_XML_FORBIDDEN_CHARS = ALL_XML_FORBIDDEN_CONTROL_CHARS + "\ud800\udfff\ufffe\uffff"

JUDGE_BAD_QUANTITIES_PYTHON: list[Any] = [
    0,
    0.0,
    -0.0,
    -1,
    -0.0001,
    -1e9,
    math.nan,
    math.inf,
    -math.inf,
    True,
    False,
    "0",
    "-5",
    "bad",
    1_000_000.01,
    1_000_001,
    1e9,
    10**400,
    -(10**400),
    None,
    [],
    {},
]

JUDGE_BAD_QUANTITIES_JSON: list[Any] = [
    0,
    -0.0,
    -1,
    -0.0001,
    -1e9,
    True,
    False,
    "0",
    "-5",
    "NaN",
    "Infinity",
    "-Infinity",
    1_000_000.01,
    1_000_001,
    1e9,
    None,
    [],
    {},
]

JUDGE_RAW_JSON_NUMBERS = ["0", "-0.0", "-1", "-0.0001", "-1e9", "NaN", "Infinity", "-Infinity", "1000001", "1e309"]


def _synth_catalog_items() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_1pcs = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "piece": {"unit": "PCS", "value": 1.0}}]

    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        cat: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": cat,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": name_en}],
            },
        }

    return [
        _item("463038", "KI", "Valio Eila semi skimmed milk drink 10 l novobox lactose free", "Valio", "10400", [("KI", 2), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]),
        _item("464306", "ST", "Valio Eila semi skimmed milk drink 1 l lactose free", "Valio", "10400", [("ST", 1)], base_1l),
        _item("469363", "ST", "Arla Semi skimmed milkdrink 1 L UHT lactose free", "Arla", "10400", [("ST", 1)], base_1l),
        _item("990001", "ST", "Portion milk roll 1 pcs lactose free", "Valio", "21100", [("ST", 1)], base_1pcs),
    ]


def _reset_breakers() -> None:
    for b in list(resilience._breakers.values()):
        b.reset()


def _has_forbidden_xml_codepoint(s: str) -> bool:
    for ch in s:
        cp = ord(ch)
        if cp <= 0x08 or cp in (0x0B, 0x0C) or 0x0E <= cp <= 0x1F or 0xD800 <= cp <= 0xDFFF or cp in (0xFFFE, 0xFFFF):
            return True
    return False


def _assert_clean_4xx(resp, label: Any = "") -> dict[str, Any]:
    assert 400 <= resp.status_code < 500, (label, resp.status_code, resp.text[:400])
    assert "ZeroDivision" not in resp.text, (label, resp.text[:400])
    assert "Traceback (most recent call last)" not in resp.text, (label, resp.text[:400])
    body = resp.json()
    assert isinstance(body, dict), (label, body)
    assert isinstance(body.get("error"), str) and body["error"].strip(), (label, body)
    assert isinstance(body.get("message"), str) and body["message"].strip(), (label, body)
    return body


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory: pytest.TempPathFactory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("wave7_judge_cat")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_catalog_items()), encoding="utf-8")
    return d


@pytest.fixture
def isolated_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_catalog_dir):
    for var in (
        "AIMO_FAULTS",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_APPROVER_PIN", PIN)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if synth_catalog_dir is not None:
        pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
        cdb = synth_catalog_dir / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()

    client_backend = FixtureClient()
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(client_backend)
    monkeypatch.setattr(
        service,
        "_catalog",
        lambda: SimpleNamespace(
            plan_need=service._plan_deterministic,
            search=lambda q, **kw: textsearch.search(q, semantic=False, **kw),
            get_product=cat_pkg.get_product,
        ),
    )
    app = create_app(with_mcp=True)
    try:
        with TestClient(app, headers={"X-Approver-Pin": PIN}, raise_server_exceptions=False) as tc:
            yield tc, client_backend
    finally:
        human_auth.reset_for_tests()
        _reset_breakers()
        bc.set_client(None)
        if synth_catalog_dir is not None:
            monkeypatch.setattr(fallback_catalog, "_index", None)
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def toy_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pjson = tmp_path / "toy_products.json"
    cdb = tmp_path / "toy_catalog.sqlite"
    sdb = tmp_path / "toy_state.sqlite"
    kdir = tmp_path / "toy_keys"
    pjson.write_text(json.dumps(toy_product_json(), ensure_ascii=False), encoding="utf-8")

    for var in (
        "AIMO_FAULTS",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(config, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(config, "STATE_DB", sdb)
    monkeypatch.setattr(config, "KEY_DIR", kdir)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(store, "DB_PATH", sdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(cat_pkg, "_SOURCE", "local")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    monkeypatch.setattr(policy, "POLICY_DIR", ROOT / "toywholesaler" / "policies")
    monkeypatch.setattr(mcp_mod, "SERVER_NAME", "nordic-bakery-supply-trusted-trade")

    policy._load_policy_cached.cache_clear()
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    catalog_store.load_catalog(cdb, force=True)
    backend = StaticBackendClient()
    bc.set_client(backend)
    human_auth.reset_for_tests()
    _reset_breakers()
    try:
        with TestClient(create_app(with_mcp=True), raise_server_exceptions=False) as tc:
            yield tc, backend
    finally:
        bc.set_client(None)
        human_auth.reset_for_tests()
        _reset_breakers()
        policy._load_policy_cached.cache_clear()
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


class TestPeppolHardening:
    @pytest.mark.parametrize("code", ["AB", "AP", "CA", "RE"])
    def test_control_chars_and_surrogates_round_trip_clean_xml(self, code: str) -> None:
        base_code = "AP" if code == "AB" else code
        o, q = sample(base_code)
        poison = f"A{ALL_XML_FORBIDDEN_CHARS}B\tC\nD Hyvä €"
        bad_lines = [
            ql.model_copy(
                update={
                    "product_name": f"Milk {ALL_XML_FORBIDDEN_CHARS} 1 L Hyvä",
                    "product_id": f"{ql.product_id}{ALL_XML_FORBIDDEN_CONTROL_CHARS}",
                }
            )
            for ql in q.lines
        ]
        q_dirty = Quote.model_construct(
            **{
                **q.model_dump(),
                "quote_id": f"Q-dirty{ALL_XML_FORBIDDEN_CHARS}",
                "quote_hash": f"{'a' * 64}{ALL_XML_FORBIDDEN_CONTROL_CHARS}",
                "kid": f"kid-1{ALL_XML_FORBIDDEN_CHARS}",
                "currency": f"EUR{ALL_XML_FORBIDDEN_CONTROL_CHARS}",
                "lines": bad_lines,
            }
        )
        dirty_rl = [
            OrderResponseLine.model_construct(
                product_id=f"{rl.product_id}{ALL_XML_FORBIDDEN_CONTROL_CHARS}",
                code=rl.code,
                qty_confirmed=rl.qty_confirmed,
                note=f"note {poison}",
            )
            for rl in o.response_lines
        ]
        dirty_hits = [
            RuleHit.model_construct(
                rule_id="r1",
                outcome="reject",
                message=f"policy hit {poison}",
            )
        ]
        o_dirty = Order.model_construct(
            **{
                **o.model_dump(),
                "order_id": f"ORD-dirty{ALL_XML_FORBIDDEN_CHARS}",
                "customer_id": f"hotel-aurora{ALL_XML_FORBIDDEN_CHARS}",
                "status": "pending_approval" if code == "AB" else o.status,
                "response_code": None if code == "AB" else code,
                "response_lines": [] if code == "AB" else dirty_rl,
                "approval_note": f"approver said {poison}",
                "decision": PolicyDecision.model_construct(
                    outcome="reject" if code == "RE" else "needs_approval",
                    policy_id="hotel-aurora",
                    policy_version="1",
                    hits=dirty_hits,
                ),
            }
        )
        buyer = {
            "endpoint_scheme": f"0216{ALL_XML_FORBIDDEN_CHARS}",
            "endpoint_id": f"BUYER-OVT{ALL_XML_FORBIDDEN_CHARS}",
            "name": f"Hotel Aurora{ALL_XML_FORBIDDEN_CHARS}",
        }
        seller = {
            "endpoint_scheme": f"0216{ALL_XML_FORBIDDEN_CHARS}",
            "endpoint_id": f"SELLER-OVT{ALL_XML_FORBIDDEN_CHARS}",
            "name": f"Valio Aimo{ALL_XML_FORBIDDEN_CHARS}",
        }
        gtins = {
            "464306": f"06410405092113{ALL_XML_FORBIDDEN_CHARS}",
            f"469363{ALL_XML_FORBIDDEN_CONTROL_CHARS}": f" 06410405099990{ALL_XML_FORBIDDEN_CHARS} ",
        }

        xml_out = order_response_xml(o_dirty, q_dirty, buyer=buyer, seller=seller, gtins=gtins)
        assert not _has_forbidden_xml_codepoint(xml_out)
        encoded = xml_out.encode("utf-8")
        parsed = ET.fromstring(encoded)
        assert parsed.findtext("cbc:OrderResponseCode", None, NS) == code
        assert check_t116_rules(xml_out) == []

    def test_cbc_sanitizes_both_text_and_attributes(self) -> None:
        root = ET.Element(f"{{{NS_UBL}}}OrderResponse")
        _cbc(root, "PriceAmount", f"12.5{ALL_XML_FORBIDDEN_CHARS}", currencyID=f"EUR{ALL_XML_FORBIDDEN_CHARS}", optionalNone=None)
        serialized = ET.tostring(root, encoding="unicode")
        assert not _has_forbidden_xml_codepoint(serialized)
        parsed = ET.fromstring(serialized.encode("utf-8"))
        el = parsed.find("cbc:PriceAmount", NS)
        assert el is not None
        assert el.text == "12.5"
        assert el.get("currencyID") == "EUR"
        assert "optionalNone" not in el.attrib

    @pytest.mark.parametrize(
        "bad_party",
        [
            None,
            {},
            "not-a-dict",
            123,
            [],
            {"name": "Only Name"},
            {"endpoint_id": "ONLY-ENDPOINT"},
            {"endpoint_scheme": "0088"},
            {"name": "", "endpoint_id": "   ", "endpoint_scheme": ALL_XML_FORBIDDEN_CHARS},
            {"name": True, "endpoint_id": False, "endpoint_scheme": None},
        ],
    )
    def test_party_and_order_response_handle_partial_or_non_dict_parties(self, bad_party: Any) -> None:
        root = ET.Element(f"{{{NS_UBL}}}OrderResponse")
        _party(root, "SellerSupplierParty", bad_party)
        _party(root, "BuyerCustomerParty", bad_party)
        for role in ("SellerSupplierParty", "BuyerCustomerParty"):
            p = root.find(f"cac:{role}/cac:Party", NS)
            assert p is not None
            eid = p.find("cbc:EndpointID", NS)
            pid = p.find("cac:PartyIdentification/cbc:ID", NS)
            rname = p.find("cac:PartyLegalEntity/cbc:RegistrationName", NS)
            assert eid is not None and (eid.text or "").strip() and (eid.get("schemeID") or "").strip()
            assert pid is not None and (pid.text or "").strip() and (pid.get("schemeID") or "").strip()
            assert rname is not None and (rname.text or "").strip()

        o, q = sample("CA")
        xml_out = order_response_xml(o, q, buyer=bad_party, seller=bad_party)
        assert check_t116_rules(xml_out) == []

    @pytest.mark.parametrize(
        "bad_gtins",
        [
            None,
            {},
            "not-a-dict",
            ["06410405092113"],
            123,
            True,
            {"464306": ""},
            {"464306": "   "},
            {"464306": ALL_XML_FORBIDDEN_CHARS},
            {"464306": None},
            {"464306": 6410405092113},
            {"464306": True},
            {123: "06410405092113"},
        ],
    )
    def test_non_dict_or_blank_gtins_ignored_cleanly(self, bad_gtins: Any) -> None:
        o, q = sample("CA")
        xml_out = order_response_xml(o, q, gtins=bad_gtins)
        assert check_t116_rules(xml_out) == []
        parsed = ET.fromstring(xml_out.encode("utf-8"))
        assert parsed.findall(".//cac:StandardItemIdentification", NS) == []

    def test_valid_gtin_trimmed_and_emitted_on_ca(self) -> None:
        o, q = sample("CA")
        xml_out = order_response_xml(o, q, gtins={"464306": " 06410405092113\x01 "})
        assert check_t116_rules(xml_out) == []
        parsed = ET.fromstring(xml_out.encode("utf-8"))
        std = parsed.find(".//cac:StandardItemIdentification/cbc:ID", NS)
        assert std is not None
        assert std.text == "06410405092113"
        assert std.get("schemeID") == "0160"

    def test_naive_datetimes_treated_as_utc_and_null_order_quote_rejected(self) -> None:
        o, q = sample("AP")
        naive_utc = datetime(2026, 11, 14, 12, 20, 5)
        xml_issued = order_response_xml(o, q, issued_at=naive_utc)
        r1 = ET.fromstring(xml_issued.encode("utf-8"))
        assert r1.findtext("cbc:IssueDate", None, NS) == "2026-11-14"
        assert r1.findtext("cbc:IssueTime", None, NS) == "14:20:05+02:00"
        assert check_t116_rules(xml_issued) == []

        o_naive = Order.model_construct(**{**o.model_dump(), "updated_at": naive_utc})
        xml_updated = order_response_xml(o_naive, q)
        r2 = ET.fromstring(xml_updated.encode("utf-8"))
        assert r2.findtext("cbc:IssueTime", None, NS) == "14:20:05+02:00"

        o_fallback_created = Order.model_construct(**{**o.model_dump(), "updated_at": None, "created_at": naive_utc})
        xml_created = order_response_xml(o_fallback_created, q)
        r3 = ET.fromstring(xml_created.encode("utf-8"))
        assert r3.findtext("cbc:IssueTime", None, NS) == "14:20:05+02:00"

        with pytest.raises(ValueError):
            order_response_xml(None, q)
        with pytest.raises(ValueError):
            order_response_xml(o, None)
        with pytest.raises(ValueError):
            order_response_xml("not-an-order", q)
        with pytest.raises(ValueError):
            order_response_xml(o, "not-a-quote")
        with pytest.raises(ValueError):
            header_code(None)
        with pytest.raises(ValueError):
            order_response_xml(o, q, issued_at=date(2026, 11, 14))
        with pytest.raises(ValueError):
            order_response_xml(o, q, issued_at="2026-11-14T12:00:00Z")

    @pytest.mark.parametrize(
        "bad_xml",
        [
            None,
            123,
            b"<OrderResponse/>",
            [],
            {},
            "",
            "   \n\t  ",
            "<OrderResponse>" + ("a" * 2_000_001) + "</OrderResponse>",
            "<OrderResponse><unclosed>",
            "not xml at all <<<",
            "<?xml version='1.0'?><OrderResponse>\x01</OrderResponse>",
            "<OrderResponse>\ud800</OrderResponse>",
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><OrderResponse>&lol;</OrderResponse>',
            '<!doctype OrderResponse SYSTEM "file:///etc/passwd"><OrderResponse/>',
            '<!ENTITY xxe SYSTEM "file:///etc/passwd"><OrderResponse/>',
        ],
    )
    def test_check_t116_rules_rejects_malformed_dtd_entity_surrogate_and_oversized(self, bad_xml: Any) -> None:
        assert check_t116_rules(bad_xml) == ["invalid_xml"]

    def test_check_t116_rules_handles_unnamespaced_elements_without_index_error(self) -> None:
        errs_plain = check_t116_rules("<foo><bar>1</bar><baz/></foo>")
        assert "PEPPOL-T116-R006" in errs_plain
        assert any("bar" in e and "baz" in e for e in errs_plain)

        valid_ap = order_response_xml(*sample("AP"))
        injected_unnamespaced = valid_ap.replace(
            "</OrderResponse>",
            '  <RogueUnnamespaced xmlns="">x</RogueUnnamespaced>\n</OrderResponse>',
        )
        errs_rogue = check_t116_rules(injected_unnamespaced)
        assert any("RogueUnnamespaced" in e for e in errs_rogue)

    def test_gateway_peppol_endpoint_handles_control_char_notes_and_bad_ids(self, isolated_gateway) -> None:
        tc, _ = isolated_gateway
        q1 = tc.post(
            "/v1/quotes",
            headers=H,
            json={"delivery_date": DELIVERY_ISO, "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
        )
        assert q1.status_code == 200, q1.text
        q1_data = q1.json()
        o1 = tc.post("/v1/orders", headers=H, json={"quote_id": q1_data["quote_id"], "quote_hash": q1_data["quote_hash"]})
        assert o1.status_code == 200, o1.text
        oid1 = o1.json()["order_id"]

        ab_resp = tc.get(f"/v1/orders/{oid1}/peppol-order-response.xml")
        assert ab_resp.status_code == 200
        assert check_t116_rules(ab_resp.text) == []
        assert ET.fromstring(ab_resp.text.encode("utf-8")).findtext("cbc:OrderResponseCode", None, NS) == "AB"

        dec_ap = tc.post(
            f"/v1/approvals/{oid1}/decision",
            json={
                "decision": "approve",
                "quote_hash": q1_data["quote_hash"],
                "note": "Approved\x01\x0b\x0c for Monday\ufffe delivery",
                **APPROVER,
            },
        )
        assert dec_ap.status_code == 200, dec_ap.text
        ap_resp = tc.get(f"/v1/orders/{oid1}/peppol-order-response.xml")
        assert ap_resp.status_code == 200
        assert not _has_forbidden_xml_codepoint(ap_resp.text)
        assert check_t116_rules(ap_resp.text) == []
        assert ET.fromstring(ap_resp.text.encode("utf-8")).findtext("cbc:OrderResponseCode", None, NS) == "AP"
        assert "Approved for Monday delivery" in ap_resp.text

        q2 = tc.post(
            "/v1/quotes",
            headers=H,
            json={"delivery_date": DELIVERY_ISO, "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
        ).json()
        oid2 = tc.post(
            "/v1/orders", headers=H, json={"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]}
        ).json()["order_id"]
        dec_re = tc.post(
            f"/v1/approvals/{oid2}/decision",
            json={
                "decision": "reject",
                "quote_hash": q2["quote_hash"],
                "note": "Budget\x00\x07\x1f cap exceeded",
                **APPROVER,
            },
        )
        assert dec_re.status_code == 200, dec_re.text
        re_resp = tc.get(f"/v1/orders/{oid2}/peppol-order-response.xml")
        assert re_resp.status_code == 200
        assert not _has_forbidden_xml_codepoint(re_resp.text)
        assert check_t116_rules(re_resp.text) == []
        assert ET.fromstring(re_resp.text.encode("utf-8")).findtext("cbc:OrderResponseCode", None, NS) == "RE"
        assert "Reason: Budget cap exceeded" in re_resp.text

        for bad_oid in ("ORD-missing", "%20%20", "bad%00id", "x" * 160):
            r_bad = tc.get(f"/v1/orders/{bad_oid}/peppol-order-response.xml")
            _assert_clean_4xx(r_bad, bad_oid)


class TestNetcheckNeverRaises:
    @pytest.mark.parametrize(
        "bad_timeout",
        [
            math.nan,
            math.inf,
            -math.inf,
            -1,
            -0.0001,
            0,
            -0.0,
            0.001,
            1.5,
            50.0,
            10**400,
            -(10**400),
            True,
            False,
            None,
            "bad",
            "1.5",
            [],
            {},
            object(),
        ],
    )
    def test_gemini_online_clamps_timeout_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, bad_timeout: Any
    ) -> None:
        monkeypatch.setenv("AIMO_SEMANTIC", "1")
        monkeypatch.delenv("AIMO_OFFLINE", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-probe-key")
        monkeypatch.setenv("AIMO_GEMINI_PROBE_HOST", "generativelanguage.googleapis.com")
        monkeypatch.setattr(netcheck, "_state", {"t": 0.0, "ok": False})

        seen_timeouts: list[float] = []

        class _DummyConn:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def _spy_conn(addr, timeout=None, *_a, **_kw):
            assert not isinstance(timeout, bool)
            assert isinstance(timeout, float)
            assert math.isfinite(timeout)
            assert 0.05 <= timeout <= 10.0
            seen_timeouts.append(timeout)
            return _DummyConn()

        monkeypatch.setattr(netcheck.socket, "create_connection", _spy_conn)
        res = netcheck.gemini_online(timeout_s=bad_timeout)
        assert res is True
        assert len(seen_timeouts) == 1

    @pytest.mark.parametrize(
        "bad_host",
        [
            "",
            "   ",
            "host with spaces.com",
            "bad\x00host.com",
            "bad\nhost.com",
            "bad\x1fhost.com",
            "a" * 254,
            ("a" * 64) + ".com",
            "..double-dot.com",
            ".leading-dot.com",
            "surrogate\ud800.com",
            None,
            123,
            True,
            [],
        ],
    )
    def test_gemini_online_rejects_bad_hosts_via_attr_and_env_without_socket_call(
        self, monkeypatch: pytest.MonkeyPatch, bad_host: Any
    ) -> None:
        monkeypatch.setenv("AIMO_SEMANTIC", "1")
        monkeypatch.delenv("AIMO_OFFLINE", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-probe-key")

        def _must_not_connect(*_a, **_kw):
            raise AssertionError("socket.create_connection must not be called for invalid host")

        monkeypatch.setattr(netcheck.socket, "create_connection", _must_not_connect)
        monkeypatch.setattr(netcheck, "_state", {"t": 0.0, "ok": False})
        monkeypatch.setattr(netcheck, "_HOST", bad_host)
        assert netcheck.gemini_online(1.5) is False

        if isinstance(bad_host, str) and "\x00" not in bad_host:
            try:
                bad_host.encode("utf-8")
                can_setenv = True
            except UnicodeEncodeError:
                can_setenv = False
            if can_setenv:
                monkeypatch.setattr(netcheck, "_HOST", netcheck._INIT_HOST)
                monkeypatch.setenv("AIMO_GEMINI_PROBE_HOST", bad_host)
                monkeypatch.setattr(netcheck, "_state", {"t": 0.0, "ok": False})
                assert netcheck.gemini_online(1.5) is False

    @pytest.mark.parametrize(
        "corrupt_state,corrupt_ttl,exc_to_raise",
        [
            ({}, 60.0, OSError("network unreachable")),
            ({"t": "corrupted", "ok": "yes"}, "bad-ttl", ValueError("invalid timeout")),
            ({"t": math.nan, "ok": True}, math.nan, OverflowError("timeout overflow")),
            ({"t": math.inf, "ok": True}, -5.0, TypeError("bad arg")),
            ({"t": True, "ok": True}, True, RuntimeError("unexpected socket failure")),
            (None, 60.0, OSError("down")),
        ],
    )
    def test_gemini_online_and_mark_offline_survive_corrupted_state_and_socket_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        corrupt_state: Any,
        corrupt_ttl: Any,
        exc_to_raise: Exception,
    ) -> None:
        monkeypatch.setenv("AIMO_SEMANTIC", "1")
        monkeypatch.delenv("AIMO_OFFLINE", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
        monkeypatch.setenv("AIMO_GEMINI_PROBE_HOST", "generativelanguage.googleapis.com")
        monkeypatch.setattr(netcheck, "_state", corrupt_state)
        monkeypatch.setattr(netcheck, "TTL_S", corrupt_ttl)

        def _raising_conn(*_a, **_kw):
            raise exc_to_raise

        monkeypatch.setattr(netcheck.socket, "create_connection", _raising_conn)
        assert netcheck.gemini_online(0.1) is False
        netcheck.mark_offline()


class TestJudgeIssue13AcceptanceMatrix:
    @pytest.mark.parametrize("bad_qty", JUDGE_BAD_QUANTITIES_PYTHON)
    def test_planner_entrypoints_reject_bad_quantity_before_offers_or_coverage_division(
        self, monkeypatch: pytest.MonkeyPatch, bad_qty: Any
    ) -> None:
        upstream_calls: list[str] = []

        def _spy_candidates(*_a, **_kw):
            upstream_calls.append("find_candidates")
            raise AssertionError("find_candidates must not run when quantity is invalid")

        def _spy_offers(*_a, **_kw):
            upstream_calls.append("offers_fn")
            raise AssertionError("offers_fn must not run when quantity is invalid")

        monkeypatch.setattr(plan_mod, "find_candidates", _spy_candidates)

        bad_need = Need.model_construct(
            raw_text="",
            customer_id="hotel-aurora",
            product_query="lactose-free milk",
            required_claims=["FREE_FROM_LACTOSE"],
            excluded_allergens=[],
            quantity=bad_qty,
            unit="L",
            delivery_by=DELIVERY_DATE,
            preferred_product_ids=[],
            preferences={},
        )

        with pytest.raises(ValueError):
            plan_need(bad_need, offers_fn=_spy_offers)
        assert upstream_calls == []

        cand = Candidate(
            product=ProductSummary(
                id="464306",
                name="Valio Eila milk drink 1 l lactose free",
                sales_unit="ST",
                content_amount=1.0,
                content_unit="L",
                claims=["FREE_FROM_LACTOSE"],
            ),
            offer=Offer(
                product_id="464306",
                requested_date=DELIVERY_DATE,
                available=True,
                available_amount=100.0,
                unit_price=1.01,
                comparison_price=1.01,
                comparison_unit="L",
                fetched_at=datetime.now(timezone.utc),
            ),
            tier="best",
            rank_score=0.95,
        )
        with pytest.raises(ValueError):
            fill(bad_need, [cand])

        seed_plan = Plan.model_construct(
            need=bad_need,
            lines=[],
            candidates=[cand],
            covered_amount=0.0,
            shortfall=10.0,
            total_net=0.0,
            explanation="",
        )
        with pytest.raises(ValueError):
            _late_split(bad_need, seed_plan, [cand], {}, {}, {})
        assert upstream_calls == []

    @pytest.mark.parametrize("bad_qty", JUDGE_BAD_QUANTITIES_JSON)
    def test_gateway_http_plan_and_quotes_reject_bad_quantities_with_4xx_and_zero_offer_calls(
        self, isolated_gateway, bad_qty: Any
    ) -> None:
        tc, backend = isolated_gateway
        before = backend.stats["upstream_calls"]

        r_plan = tc.post(
            "/v1/plan",
            headers=H,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "required_claims": ["FREE_FROM_LACTOSE"],
                "quantity": bad_qty,
                "unit": "L",
                "delivery_by": DELIVERY_ISO,
            },
        )
        _assert_clean_4xx(r_plan, ("v1/plan", bad_qty))
        assert backend.stats["upstream_calls"] == before

        r_quote_line = tc.post(
            "/v1/quotes",
            headers=H,
            json={
                "delivery_date": DELIVERY_ISO,
                "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}],
            },
        )
        _assert_clean_4xx(r_quote_line, ("v1/quotes.line", bad_qty))
        assert backend.stats["upstream_calls"] == before

        r_quote_need = tc.post(
            "/v1/quotes",
            headers=H,
            json={
                "delivery_date": DELIVERY_ISO,
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose-free milk",
                    "quantity": bad_qty,
                    "unit": "L",
                    "delivery_by": DELIVERY_ISO,
                },
                "lines": [{"product_id": "464306", "qty_sales_units": 5}],
            },
        )
        _assert_clean_4xx(r_quote_need, ("v1/quotes.need", bad_qty))
        assert backend.stats["upstream_calls"] == before

    @pytest.mark.parametrize("raw_token", JUDGE_RAW_JSON_NUMBERS)
    def test_gateway_raw_json_nan_inf_nonpositive_and_overflow_are_4xx(
        self, isolated_gateway, raw_token: str
    ) -> None:
        tc, backend = isolated_gateway
        before = backend.stats["upstream_calls"]
        hdrs = {**H, "Content-Type": "application/json"}

        raw_plan = (
            '{"customer_id":"hotel-aurora","product_query":"lactose-free milk",'
            '"unit":"L","delivery_by":"2026-11-16","quantity":' + raw_token + "}"
        )
        r_plan = tc.post("/v1/plan", headers=hdrs, content=raw_plan.encode("utf-8"))
        _assert_clean_4xx(r_plan, ("raw-plan", raw_token))
        assert backend.stats["upstream_calls"] == before

        raw_quote = (
            '{"delivery_date":"2026-11-16","lines":[{"product_id":"464306","qty_sales_units":' + raw_token + "}]}"
        )
        r_quote = tc.post("/v1/quotes", headers=hdrs, content=raw_quote.encode("utf-8"))
        _assert_clean_4xx(r_quote, ("raw-quote", raw_token))
        assert backend.stats["upstream_calls"] == before

    @pytest.mark.parametrize("frac_qty", [0.5, 2.25, 2.5])
    def test_fractional_pcs_count_units_rejected_on_quotes_and_safe_on_plan(
        self, isolated_gateway, frac_qty: float
    ) -> None:
        tc, _ = isolated_gateway
        r_quote = tc.post(
            "/v1/quotes",
            headers=H,
            json={
                "delivery_date": DELIVERY_ISO,
                "lines": [{"product_id": "464306", "qty_sales_units": frac_qty}],
            },
        )
        body = _assert_clean_4xx(r_quote, ("fractional-quote", frac_qty))
        assert r_quote.status_code == 422
        assert body["error"] == "fractional_quantity"

        r_plan = tc.post(
            "/v1/plan",
            headers=H,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "milk",
                "quantity": frac_qty,
                "unit": "PCS",
                "delivery_by": DELIVERY_ISO,
            },
        )
        assert r_plan.status_code < 500, r_plan.text
        if r_plan.status_code == 200:
            pdata = r_plan.json()
            for ln in pdata.get("lines", []):
                if ln.get("sales_unit") in ("ST", "PCS", "KI"):
                    qsu = ln["qty_sales_units"]
                    assert abs(qsu - round(qsu)) < 1e-9

    @pytest.mark.parametrize(
        "bad_offers_body",
        [
            {},
            {"product_ids": [], "requested_date": DELIVERY_ISO},
            {"product_ids": [""], "requested_date": DELIVERY_ISO},
            {"product_ids": ["   "], "requested_date": DELIVERY_ISO},
            {"product_ids": ["464306\x00"], "requested_date": DELIVERY_ISO},
            {"product_ids": ["464 306"], "requested_date": DELIVERY_ISO},
            {"product_ids": [True], "requested_date": DELIVERY_ISO},
            {"product_ids": [None], "requested_date": DELIVERY_ISO},
            {"product_ids": [f"P{i}" for i in range(1001)], "requested_date": DELIVERY_ISO},
            {"product_ids": ["464306"], "requested_date": "not-a-date"},
            {"product_ids": ["464306"], "requested_date": DELIVERY_ISO, "customer_id": "unknown-corp"},
        ],
    )
    def test_v1_offers_bad_inputs_return_clean_4xx_and_zero_offer_calls(
        self, isolated_gateway, bad_offers_body: dict[str, Any]
    ) -> None:
        tc, backend = isolated_gateway
        before = backend.stats["upstream_calls"]
        hdrs = {"X-Agent-Id": "buyer-1"} if "customer_id" in bad_offers_body else H
        r = tc.post("/v1/offers", headers=hdrs, json=bad_offers_body)
        _assert_clean_4xx(r, ("v1/offers", bad_offers_body))
        assert backend.stats["upstream_calls"] == before

    def test_mcp_plan_need_and_create_quote_reject_all_bad_quantities_and_fractional_pcs(
        self, isolated_gateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, backend = isolated_gateway
        monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
        monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")

        async def _exercise_mcp() -> None:
            async with Client(build_mcp()) as client:
                for bad_qty in JUDGE_BAD_QUANTITIES_JSON:
                    before = backend.stats["upstream_calls"]
                    try:
                        res = await client.call_tool(
                            "plan_need",
                            {
                                "need": {
                                    "customer_id": "hotel-aurora",
                                    "product_query": "lactose-free milk",
                                    "required_claims": ["FREE_FROM_LACTOSE"],
                                    "quantity": bad_qty,
                                    "unit": "L",
                                    "delivery_by": DELIVERY_ISO,
                                }
                            },
                        )
                        is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                        txt = " ".join(getattr(x, "text", "") for x in (getattr(res, "content", None) or []))
                    except Exception as exc:
                        is_err, txt = True, str(exc)
                    assert is_err, ("mcp.plan_need", bad_qty, txt)
                    assert "ZeroDivision" not in txt and "Traceback (most recent call last)" not in txt
                    assert backend.stats["upstream_calls"] == before

                    try:
                        res_q = await client.call_tool(
                            "create_quote",
                            {
                                "delivery_date": DELIVERY_ISO,
                                "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}],
                            },
                        )
                        is_err_q = bool(getattr(res_q, "is_error", False) or getattr(res_q, "isError", False))
                        txt_q = " ".join(getattr(x, "text", "") for x in (getattr(res_q, "content", None) or []))
                    except Exception as exc:
                        is_err_q, txt_q = True, str(exc)
                    assert is_err_q, ("mcp.create_quote", bad_qty, txt_q)
                    assert "ZeroDivision" not in txt_q and "Traceback (most recent call last)" not in txt_q
                    assert backend.stats["upstream_calls"] == before

                for frac_qty in (0.5, 2.5):
                    try:
                        res_frac = await client.call_tool(
                            "create_quote",
                            {
                                "delivery_date": DELIVERY_ISO,
                                "lines": [{"product_id": "464306", "qty_sales_units": frac_qty}],
                            },
                        )
                        is_err_f = bool(getattr(res_frac, "is_error", False) or getattr(res_frac, "isError", False))
                        txt_f = " ".join(getattr(x, "text", "") for x in (getattr(res_frac, "content", None) or []))
                    except Exception as exc:
                        is_err_f, txt_f = True, str(exc)
                    assert is_err_f and "fractional" in txt_f.lower(), (frac_qty, txt_f)

                ok_res = await client.call_tool("list_policies", {})
                assert not bool(getattr(ok_res, "is_error", False) or getattr(ok_res, "isError", False))

        asyncio.run(_exercise_mcp())

    @pytest.mark.parametrize("bad_qty", JUDGE_BAD_QUANTITIES_JSON)
    def test_toywholesaler_post_v1_plan_and_quotes_reject_bad_quantities_without_500_or_offer_calls(
        self, toy_gateway, bad_qty: Any
    ) -> None:
        tc, backend = toy_gateway
        before = backend.stats["upstream_calls"]

        r_plan = tc.post(
            "/v1/plan",
            headers=H,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "rye bread",
                "quantity": bad_qty,
                "unit": "KG",
                "delivery_by": "2026-12-02",
            },
        )
        _assert_clean_4xx(r_plan, ("toy/v1/plan", bad_qty))
        assert backend.stats["upstream_calls"] == before

        r_quote = tc.post(
            "/v1/quotes",
            headers=H,
            json={
                "delivery_date": "2026-12-02",
                "lines": [{"product_id": "NB1001", "qty_sales_units": bad_qty}],
            },
        )
        _assert_clean_4xx(r_quote, ("toy/v1/quotes", bad_qty))
        assert backend.stats["upstream_calls"] == before

    def test_toywholesaler_fractional_pcs_rejected_and_positive_plan_works(self, toy_gateway) -> None:
        tc, backend = toy_gateway
        r_frac = tc.post(
            "/v1/quotes",
            headers=H,
            json={
                "delivery_date": "2026-12-02",
                "lines": [{"product_id": "NB1001", "qty_sales_units": 2.5}],
            },
        )
        b_frac = _assert_clean_4xx(r_frac, "toy-fractional")
        assert r_frac.status_code == 422 and b_frac["error"] == "fractional_quantity"

        before = backend.stats["upstream_calls"]
        r_ok = tc.post(
            "/v1/plan",
            headers=H,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "rye bread",
                "quantity": 5.0,
                "unit": "KG",
                "delivery_by": "2026-12-02",
            },
        )
        assert r_ok.status_code == 200, r_ok.text
        assert backend.stats["upstream_calls"] > before
        data = r_ok.json()
        assert data["covered_amount"] >= 5.0

    def test_valio_gateway_positive_calibration_and_http_mcp_bad_quantities(self, isolated_gateway) -> None:
        tc, backend = isolated_gateway
        mcp_headers = {
            **H,
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        init_resp = tc.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "judge-issue13-matrix", "version": "0.1.0"},
                },
            },
        )
        assert init_resp.status_code == 200, init_resp.text
        sid = init_resp.headers.get("mcp-session-id")
        call_headers = {**mcp_headers, **({"mcp-session-id": sid} if sid else {})}
        tc.post("/mcp", headers=call_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        def _parse_mcp_http(r) -> dict[str, Any]:
            assert r.status_code == 200, r.text
            if r.headers.get("content-type", "").startswith("text/event-stream"):
                data_lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
                return json.loads(data_lines[0])
            return r.json()

        req_id = 10
        for bad_qty in (0, -0.0, -1, -1e9, True, False, "0", "-5", "NaN", "Infinity", 1_000_001):
            before = backend.stats["upstream_calls"]
            req_id += 1
            r_p = tc.post(
                "/mcp",
                headers=call_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "plan_need",
                        "arguments": {
                            "need": {
                                "customer_id": "hotel-aurora",
                                "product_query": "lactose-free milk",
                                "required_claims": ["FREE_FROM_LACTOSE"],
                                "quantity": bad_qty,
                                "unit": "L",
                                "delivery_by": DELIVERY_ISO,
                            }
                        },
                    },
                },
            )
            msg_p = _parse_mcp_http(r_p)
            assert "error" in msg_p or bool((msg_p.get("result") or {}).get("isError")), (bad_qty, msg_p)
            assert "ZeroDivision" not in json.dumps(msg_p)
            assert backend.stats["upstream_calls"] == before

            req_id += 1
            r_q = tc.post(
                "/mcp",
                headers=call_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "create_quote",
                        "arguments": {
                            "delivery_date": DELIVERY_ISO,
                            "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}],
                        },
                    },
                },
            )
            msg_q = _parse_mcp_http(r_q)
            assert "error" in msg_q or bool((msg_q.get("result") or {}).get("isError")), (bad_qty, msg_q)
            assert "ZeroDivision" not in json.dumps(msg_q)
            assert backend.stats["upstream_calls"] == before

        before_pos = backend.stats["upstream_calls"]
        r_pos = tc.post(
            "/v1/plan",
            headers=H,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "required_claims": ["FREE_FROM_LACTOSE"],
                "quantity": 10.0,
                "unit": "L",
                "delivery_by": DELIVERY_ISO,
            },
        )
        assert r_pos.status_code == 200, r_pos.text
        assert backend.stats["upstream_calls"] > before_pos

