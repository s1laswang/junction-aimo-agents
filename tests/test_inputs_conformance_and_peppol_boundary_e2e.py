from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import subprocess
import sys
import time
import tokenize
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import aimo.catalog as cat_pkg
from aimo import config
from aimo.backend import catalog_store, clock, orders as store, peppol
from aimo.backend import client as bc
from aimo.backend.peppol import (
    CUSTOMIZATION_ID,
    NS_CAC,
    NS_CBC,
    NS_UBL,
    PROFILE_ID,
    check_t116_rules,
    header_code,
    order_response_xml,
)
from aimo.catalog import textsearch
from aimo.catalog.substitution import validate_need
from aimo.gateway import human_auth, resilience, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import (
    AuditEvent,
    EU14,
    Need,
    Offer,
    Order,
    OrderResponseLine,
    PolicyDecision,
    Quote,
    QuoteLine,
    RuleHit,
)
from aimo.trust import policy as trust_policy
from aimo.trust import signing
from aimo.trust.audit import AuditLog
from conformance import __main__ as conf_main
from conformance import profile as conf_profile
from conformance import verify as conf_verify

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = date(2026, 11, 16)
NS = {"ubl": NS_UBL, "cac": NS_CAC, "cbc": NS_CBC}
H_HOTEL = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
PIN_HDR = {"X-Approver-Pin": "test-pin"}
HOTEL_APPROVER = "anna@hotel-aurora.example"
SCHOOL_APPROVER = "liisa@school-tampere.example"

NORDIC_XML_SPECIAL = 'Keittiö & Päiväkoti "Äänekoski" <Lämpö \'85°C\'> Hyvää ångkokt smörgås €'
NORDIC_PRODUCT_KG = 'Kaurahiutale & Ruisjauho <Luomu> "Äänekoski" \'Tuore\' ångad öljyllä 1 kg'
NORDIC_PRODUCT_L = 'Kaurajuoma & Piimäkorvike <UHT> "Åland" \'Hyvä\' äidinmaidoton öljy 1 L'
NORDIC_REASON = 'Syynä: määrä & laatu <tarkistettu> "keittiöpäällikkö" \'åland\' öljy-erä'


def _load_verify_quote_module():
    spec = importlib.util.spec_from_file_location("verify_quote_cli_mod", ROOT / "scripts" / "verify_quote.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VQ = _load_verify_quote_module()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _synkka_boundary_items() -> list[dict[str, Any]]:
    free_all = [{"id": code, "unit": "FREE_FROM"} for code in ("AM", "AN", "AW")]
    return [
        {
            "id": "910001",
            "gtin": "06410405910011",
            "deleted": False,
            "salesUnit": "KG",
            "baseUnit": "KG",
            "category": "14100",
            "vendorName": 'Valio & Åland "Kvarn" <Oy> \'Ab\'',
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "KG", "sizeInBaseUnits": 1, "gtin": "06410405910011"}],
            "synkkaData": {
                "brand": 'Äänekoski & Österbotten "Luomu"',
                "gtin": "06410405910011",
                "names": [{"language": "en", "value": NORDIC_PRODUCT_KG}],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}
                ],
                "classifications": [
                    {"name": "nonAllergen", "type": "ENUM", "values": free_all},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "VEGAN", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 2.0}]},
                ],
                "keyIngredients": [{"language": "en", "value": "gluten-free oats & water <pure> 'åland' ä ö"}],
                "marketingTexts": [{"language": "en", "value": f"{NORDIC_PRODUCT_KG} for foodservice."}],
            },
        },
        {
            "id": "910002",
            "gtin": "06410405910028",
            "deleted": False,
            "salesUnit": "L",
            "baseUnit": "L",
            "category": "10400",
            "vendorName": 'Kaslink & Äänekoski "Meijeri" <FI>',
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "L", "sizeInBaseUnits": 1, "gtin": "06410405910028"}],
            "synkkaData": {
                "brand": "Åland & Äänekoski",
                "gtin": "06410405910028",
                "names": [{"language": "en", "value": NORDIC_PRODUCT_L}],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}
                ],
                "classifications": [
                    {"name": "nonAllergen", "type": "ENUM", "values": free_all},
                    {
                        "name": "nutritionalClaim",
                        "type": "ENUM",
                        "values": [
                            {"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"},
                            {"id": "VEGAN", "unit": "FREE_FROM"},
                        ],
                    },
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "water, oats & rapeseed oil 'ä ö å'"}],
                "marketingTexts": [{"language": "en", "value": f"{NORDIC_PRODUCT_L} for kitchens."}],
            },
        },
        {
            "id": "910003",
            "gtin": "06410405910035",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio Oy",
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1, "gtin": "06410405910035"}],
            "synkkaData": {
                "brand": "Valio",
                "gtin": "06410405910035",
                "names": [{"language": "en", "value": "Oat drink carton 1 L lactose free"}],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}
                ],
                "classifications": [
                    {"name": "nonAllergen", "type": "ENUM", "values": free_all},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "water, oats"}],
                "marketingTexts": [{"language": "en", "value": "Oat drink carton 1 L."}],
            },
        },
    ]


class _BoundaryBackendClient:
    base_url = "fixture://boundary-conformance-peppol"
    ttl_s = 300

    def __init__(self) -> None:
        self.sales_units = {"910001": "KG", "910002": "L", "910003": "ST"}
        self.comp_units = {"910001": "KGM", "910002": "LTR", "910003": "LTR"}
        self.prices = {"910001": 2.00, "910002": 1.50, "910003": 1.25}
        self.stock = {"910001": 2_000_000.0, "910002": 2_000_000.0, "910003": 2_000_000.0}
        self.closing_times = {pid: "2026-11-15T17:00:00+0200" for pid in self.sales_units}
        self.sales_blocks: set[str] = set()
        self.stats = {"upstream_calls": 0}

    def _now_iso(self) -> str:
        return clock.now().astimezone(timezone(timedelta(hours=2))).strftime("%Y-%m-%dT%H:%M:%S%z")

    def price(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._now_iso(), "currency": "EUR"}
        products = []
        for pid in ids:
            if pid not in self.sales_units:
                continue
            up = self.prices[pid]
            cu = self.comp_units[pid]
            products.append(
                {
                    "productId": pid,
                    "salesUnit": self.sales_units[pid],
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "comparisonUnit": cu,
                    "catalogPrice": {"value": up, "valueWithTax": round(up * 1.14, 4), "currency": "EUR"},
                    "comparisonPrice": {"value": up, "unit": cu},
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._now_iso()}
        products = []
        for pid in ids:
            if pid not in self.sales_units:
                continue
            sb = pid in self.sales_blocks
            amt = self.stock[pid]
            sched = (
                [
                    {
                        "salesBlock": sb,
                        "availableDate": requested_date.isoformat(),
                        "availableAmount": amt,
                        "salesUnit": self.sales_units[pid],
                    }
                ]
                if amt > 0
                else []
            )
            products.append(
                {
                    "productId": pid,
                    "closingTime": self.closing_times[pid],
                    "salesBlock": sb,
                    "logisticModel": "Stock",
                    "scheduleLines": sched,
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}


def _close_catalog_caches() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _reset_breakers() -> None:
    for brk in list(resilience._breakers.values()):
        brk.reset()


@pytest.fixture(scope="module")
def catalog_bundle(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("inputs_wave9_boundary_cat")
    pjson = d / "valio_aimo_junction26_product_data.json"
    cdb = d / "catalog.sqlite"
    rel_csv = d / "missing_rel.csv"
    co2_csv = d / "missing_co2.csv"
    key_dir = d / "keys"
    items = _synkka_boundary_items()
    pjson.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    mp = pytest.MonkeyPatch()
    mp.setattr(catalog_store, "PRODUCT_JSON", pjson)
    mp.setattr(catalog_store, "CATALOG_DB", cdb)
    mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    mp.setattr(catalog_store, "CO2_CSV", co2_csv)
    try:
        _close_catalog_caches()
        catalog_store.load_catalog(cdb, force=True)
    finally:
        mp.undo()
        _close_catalog_caches()

    with TestClient(create_app(with_mcp=False), raise_server_exceptions=False) as api:
        yield {
            "dir": d,
            "pjson": pjson,
            "cdb": cdb,
            "rel_csv": rel_csv,
            "co2_csv": co2_csv,
            "key_dir": key_dir,
            "api": api,
        }
    _close_catalog_caches()


@pytest.fixture
def boundary_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_bundle: dict[str, Any]):
    state_db = tmp_path / "state.sqlite"
    key_dir = catalog_bundle["key_dir"]

    for var in (
        "AIMO_FAULTS",
        "AIMO_CATALOG_FIXTURE",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
        "AIMO_AGENT_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_APPROVER_PINS",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-pin")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "PRODUCT_JSON", catalog_bundle["pjson"])
    monkeypatch.setattr(config, "CATALOG_DB", catalog_bundle["cdb"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", catalog_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", catalog_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", catalog_bundle["pjson"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", catalog_bundle["cdb"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", catalog_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", catalog_bundle["co2_csv"])
    monkeypatch.setattr(cat_pkg, "_SOURCE", "full")
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        service,
        "_catalog",
        lambda: SimpleNamespace(
            plan_need=service._plan_deterministic,
            search=lambda q, **kw: textsearch.search(q, semantic=False, **kw),
            get_product=cat_pkg.get_product,
        ),
    )

    _close_catalog_caches()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    human_auth.reset_for_tests()
    _reset_breakers()

    backend = _BoundaryBackendClient()
    bc.set_client(backend)
    try:
        yield catalog_bundle["api"], backend, state_db, key_dir
    finally:
        bc.set_client(None)
        store.close_all()
        human_auth.reset_for_tests()
        _reset_breakers()
        _close_catalog_caches()
        if hasattr(trust_policy, "_load_policy_cached"):
            trust_policy._load_policy_cached.cache_clear()


def _make_sample_signed_quote(
    *,
    qty: float = 10.0,
    sales_unit: str = "KG",
    unit_price: float = 2.0,
    customer_id: str = "hotel-aurora",
    product_id: str = "910001",
    product_name: str = NORDIC_PRODUCT_KG,
) -> Quote:
    now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    lt = round(qty * unit_price, 2)
    unsigned = Quote(
        quote_id="Q-20261114-b00ndary",
        customer_id=customer_id,
        agent_id="hotel-aurora/buyer-1",
        delivery_date=DELIVERY,
        lines=[
            QuoteLine(
                product_id=product_id,
                product_name=product_name,
                qty_sales_units=qty,
                sales_unit=sales_unit,
                unit_price=unit_price,
                line_total=lt,
                tier="preferred",
                flags=[],
            )
        ],
        total_net=lt,
        total_gross=round(lt * 1.14, 2),
        currency="EUR",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    return signing.sign_quote(unsigned)


def _assert_clean_4xx(resp, ctx: Any = None) -> dict[str, Any]:
    assert 400 <= resp.status_code < 500, (ctx, resp.status_code, resp.text[:400])
    assert "ZeroDivision" not in resp.text
    assert "Traceback (most recent call last)" not in resp.text
    body = resp.json()
    assert isinstance(body, dict)
    assert isinstance(body.get("error"), str) and body["error"].strip()
    assert isinstance(body.get("message"), str) and body["message"].strip()
    return body


def test_self_hygiene_zero_hash_comments_and_zero_em_en_dashes() -> None:
    raw = Path(__file__).read_text(encoding="utf-8")
    assert chr(35) not in raw
    assert chr(0x2014) not in raw
    assert chr(0x2013) not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []


class TestConformanceProfileAndMalformedOtapJwks:
    def test_profile_constants_and_gateway_discovery_match_normative_contract(self, boundary_env) -> None:
        api, _, _, _ = boundary_env
        assert conf_profile.PROFILE == "otap/0.1"
        assert conf_profile.PEPPOL_CODES == {"AP", "CA", "RE"}
        assert {"placed", "pending_approval", "rejected", "approved", "expired", "failed"} == conf_profile.ORDER_STATUSES
        assert set(conf_profile.MCP_TOOLS).isdisjoint(set(conf_profile.MCP_FORBIDDEN))

        import asyncio

        mcp_tools = asyncio.run(build_mcp().list_tools())
        mcp_names = {t.name for t in mcp_tools}
        assert len(mcp_names) == 13
        assert set(conf_profile.MCP_TOOLS) <= mcp_names
        assert mcp_names.isdisjoint(set(conf_profile.MCP_FORBIDDEN))

        otap_resp = api.get("/.well-known/otap.json")
        jwks_resp = api.get("/.well-known/jwks.json")
        spec_resp = api.get("/openapi.json")
        assert otap_resp.status_code == 200
        assert jwks_resp.status_code == 200
        assert spec_resp.status_code == 200

        otap_doc = otap_resp.json()
        jwks_doc = jwks_resp.json()
        spec_doc = spec_resp.json()

        assert [f for f in conf_profile.OTAP_FIELDS if f not in otap_doc] == []
        assert otap_doc["protocol"] == conf_profile.PROFILE
        assert [e for e in conf_profile.OTAP_ENDPOINTS if e not in otap_doc["endpoints"]] == []
        assert set(conf_profile.OPENAPI_PATHS) <= set(spec_doc["paths"])
        for k in jwks_doc["keys"]:
            assert conf_verify.thumbprint(k) == k["kid"]
            assert signing.thumbprint(k) == k["kid"]
            assert "d" not in k

    @pytest.mark.parametrize(
        "case_name,otap_status,otap_body,jwks_status,jwks_mutator,spec_body,expected_fails",
        [
            ("otap_500", 500, {}, 200, None, {"openapi": "3.1.0", "paths": {p: {} for p in conf_profile.OPENAPI_PATHS}}, {"D1", "D2", "D3", "D4", "D5"}),
            (
                "wrong_protocol_and_mcp_transport",
                200,
                {
                    **{f: "x" for f in conf_profile.OTAP_FIELDS},
                    "protocol": "otap/9.9",
                    "endpoints": {e: "/v1/x" for e in conf_profile.OTAP_ENDPOINTS},
                    "mcp": {"url": "http://mock.local/mcp", "transport": "sse"},
                    "jwks_url": "http://mock.local/.well-known/jwks.json",
                    "openapi_url": "http://mock.local/openapi.json",
                },
                200,
                None,
                {"openapi": "3.1.0", "paths": {p: {} for p in conf_profile.OPENAPI_PATHS}},
                {"D3", "D5"},
            ),
            (
                "missing_endpoints_and_openapi_v2",
                200,
                {
                    **{f: "x" for f in conf_profile.OTAP_FIELDS},
                    "protocol": conf_profile.PROFILE,
                    "endpoints": {"search_products": "/v1/products/search"},
                    "mcp": {"url": "http://mock.local/mcp", "transport": "streamable-http"},
                    "jwks_url": "http://mock.local/.well-known/jwks.json",
                    "openapi_url": "http://mock.local/openapi.json",
                },
                200,
                None,
                {"openapi": "2.0", "paths": {"/v1/quotes": {}}},
                {"D4", "D9", "D10"},
            ),
            (
                "jwks_wrong_thumbprint_and_leaked_private_d",
                200,
                {
                    **{f: "x" for f in conf_profile.OTAP_FIELDS},
                    "protocol": conf_profile.PROFILE,
                    "endpoints": {e: "/v1/x" for e in conf_profile.OTAP_ENDPOINTS},
                    "mcp": {"url": "http://mock.local/mcp", "transport": "streamable-http"},
                    "jwks_url": "http://mock.local/.well-known/jwks.json",
                    "openapi_url": "http://mock.local/openapi.json",
                },
                200,
                "tamper_kid_and_leak_d",
                {"openapi": "3.1.0", "paths": {p: {} for p in conf_profile.OPENAPI_PATHS}},
                {"D7", "D8"},
            ),
            (
                "jwks_rsa_non_okp_key",
                200,
                {
                    **{f: "x" for f in conf_profile.OTAP_FIELDS},
                    "protocol": conf_profile.PROFILE,
                    "endpoints": {e: "/v1/x" for e in conf_profile.OTAP_ENDPOINTS},
                    "mcp": {"url": "http://mock.local/mcp", "transport": "streamable-http"},
                    "jwks_url": "http://mock.local/.well-known/jwks.json",
                    "openapi_url": "http://mock.local/openapi.json",
                },
                200,
                "rsa_key",
                {"openapi": "3.1.0", "paths": {p: {} for p in conf_profile.OPENAPI_PATHS}},
                {"D6", "D7"},
            ),
        ],
    )
    def test_conformance_discovery_flags_malformed_otap_jwks_and_openapi_without_crashing(
        self,
        boundary_env,
        monkeypatch: pytest.MonkeyPatch,
        case_name: str,
        otap_status: int,
        otap_body: dict[str, Any],
        jwks_status: int,
        jwks_mutator: str | None,
        spec_body: dict[str, Any],
        expected_fails: set[str],
    ) -> None:
        _ = boundary_env
        valid_jwk = signing.public_jwk()
        if jwks_mutator == "tamper_kid_and_leak_d":
            jwks_payload = {"keys": [{**valid_jwk, "kid": "tampered-kid", "d": valid_jwk["x"]}]}
        elif jwks_mutator == "rsa_key":
            jwks_payload = {"keys": [{"kty": "RSA", "kid": "rsa-1", "n": "abcd", "e": "AQAB"}]}
        else:
            jwks_payload = {"keys": [valid_jwk]}

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/otap.json"):
                return httpx.Response(otap_status, json=otap_body)
            if request.url.path.endswith("/jwks.json"):
                return httpx.Response(jwks_status, json=jwks_payload)
            if request.url.path.endswith("/openapi.json"):
                return httpx.Response(200, json=spec_body)
            return httpx.Response(404, json={"error": "not_found"})

        transport = httpx.MockTransport(_handler)
        suite = conf_main.Suite("http://mock.local", "hotel-aurora", "buyer-1", quiet=True)
        suite.h.close()
        suite.h = httpx.Client(base_url="http://mock.local", transport=transport)

        def _mock_get(url: str, timeout: float = 30.0) -> httpx.Response:
            with httpx.Client(transport=transport) as c:
                return c.get(url, timeout=timeout)

        monkeypatch.setattr(conf_main.httpx, "get", _mock_get)
        try:
            suite.guard("D0", "discovery checks ran", lambda: conf_main.check_discovery(suite))
        finally:
            suite.h.close()

        by_id = {r["id"]: r["status"] for r in suite.rows}
        assert "D0" not in by_id, (case_name, suite.rows)
        failed_ids = {cid for cid, st in by_id.items() if st == "FAIL"}
        assert expected_fails <= failed_ids, (case_name, by_id)

    @pytest.mark.parametrize(
        "bad_jwks",
        [
            None,
            [],
            "not-a-jwks",
            123,
            True,
            {},
            {"keys": None},
            {"keys": "bad"},
            {"keys": {}},
            {"keys": []},
            {"keys": [None, 123, "str", {}]},
            {"keys": [{"kty": "RSA", "crv": "Ed25519", "kid": "k1", "x": _b64u(b"a" * 32)}]},
            {"keys": [{"kty": "OKP", "crv": "X25519", "kid": "k1", "x": _b64u(b"a" * 32)}]},
            {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "", "x": _b64u(b"a" * 32)}]},
            {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "k1", "x": ""}]},
            {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "k1", "x": "not!valid!b64"}]},
            {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "k1", "x": _b64u(b"short")}]},
            {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "k1", "x": _b64u(b"x" * 64)}]},
        ],
    )
    def test_malformed_jwks_cleanly_flagged_across_conformance_signing_and_verify_quote(
        self, boundary_env, bad_jwks: Any
    ) -> None:
        _ = boundary_env
        q = _make_sample_signed_quote()
        q_dict = q.model_dump(mode="json")

        cv = conf_verify.verify_quote(q_dict, bad_jwks)
        assert cv["ok"] is False
        assert cv["signature_valid"] is False
        assert cv["reasons"]

        if bad_jwks is not None:
            sv = signing.verify_quote_detailed(q_dict, jwks_=bad_jwks, now=q.created_at)
            assert sv["ok"] is False
            assert sv["signature_valid"] is False
            assert sv["reasons"]

        ev_res = conf_verify.verify_events([], bad_jwks)
        if not isinstance(bad_jwks, dict) or not isinstance(bad_jwks.get("keys"), list):
            assert ev_res["ok"] is False
            assert "invalid JWKS" in ev_res["problems"]

        if not isinstance(bad_jwks, dict) or not isinstance(bad_jwks.get("keys"), list):
            with pytest.raises(ValueError):
                VQ.verify_document(q_dict, bad_jwks)
        else:
            ok_vq, lines_vq = VQ.verify_document(q_dict, bad_jwks)
            assert ok_vq is False
            assert any("signature BAD" in ln for ln in lines_vq)

    @pytest.mark.parametrize(
        "bad_jwk",
        [
            None,
            "not-a-dict",
            [],
            {},
            {"kty": "RSA", "crv": "Ed25519", "x": _b64u(b"a" * 32)},
            {"kty": "OKP", "crv": "Ed448", "x": _b64u(b"a" * 32)},
            {"kty": "OKP", "crv": "Ed25519", "x": ""},
            {"kty": "OKP", "crv": "Ed25519", "x": 123},
            {"kty": "OKP", "crv": "Ed25519", "x": "===bad==="},
            {"kty": "OKP", "crv": "Ed25519", "x": _b64u(b"a" * 31)},
            {"kty": "OKP", "crv": "Ed25519", "x": _b64u(b"a" * 33)},
        ],
    )
    def test_thumbprint_rejects_malformed_okp_keys_in_both_implementations(self, bad_jwk: Any) -> None:
        with pytest.raises(ValueError):
            conf_verify.thumbprint(bad_jwk)
        with pytest.raises(signing.JWSError):
            signing.thumbprint(bad_jwk)


class TestUnicodeNordicAndXmlSpecialCharactersE2E:
    def test_canonical_json_parity_on_nordic_and_xml_special_payloads(self) -> None:
        payload = {
            "customer": NORDIC_XML_SPECIAL,
            "product": NORDIC_PRODUCT_KG,
            "reason": NORDIC_REASON,
            "xml_entities": "&amp; &lt; &gt; &quot; &apos; ]]> <tag attr=\"1\" s='2'>äöåÄÖÅ</tag>",
            "boundary_min": 0.01,
            "boundary_max_float": 1_000_000.0,
            "boundary_max_int": 1_000_000,
            "nested": [{"z": 2.0, "a": "äöå & < > \" '"}],
        }
        c_conf = conf_verify.canonical_json(payload)
        c_sign = signing.canonical_json(payload)
        c_vq = VQ.canon(payload)
        assert c_conf == c_sign == c_vq
        assert '"boundary_max_float":1000000' in c_conf
        assert '"boundary_min":0.01' in c_conf
        assert "äöå" in c_conf

    @pytest.mark.parametrize("code", ["AB", "AP", "CA", "RE"])
    def test_peppol_order_response_escapes_xml_specials_and_preserves_nordic_chars_across_all_codes(
        self, boundary_env, code: str
    ) -> None:
        _ = boundary_env
        q = _make_sample_signed_quote(
            qty=12.5,
            sales_unit="KG",
            unit_price=3.20,
            customer_id='asiakas-äänekoski & "åland" <oy> \'ab\'',
            product_id='P-&-<>-"-\'-äöå',
            product_name=NORDIC_PRODUCT_KG,
        )
        line_code = "CA" if code == "CA" else ("RE" if code == "RE" else "AP")
        confirmed = 7.25 if code == "CA" else (0.0 if code == "RE" else 12.5)
        order = Order(
            order_id='ORD-&-<>-"-\'-äöå',
            quote_id=q.quote_id,
            quote_hash=q.quote_hash or "",
            customer_id=q.customer_id,
            agent_id=q.agent_id,
            status="pending_approval" if code == "AB" else ("rejected" if code == "RE" else "placed"),
            decision=PolicyDecision(
                outcome="reject" if code == "RE" else ("needs_approval" if code == "AB" else "auto_approve"),
                policy_id="hotel-aurora",
                policy_version="1",
                hits=[
                    RuleHit(
                        rule_id="policy.nordic",
                        outcome="reject" if code == "RE" else "allow",
                        message=NORDIC_REASON,
                    )
                ],
            ),
            approval_note=f"Hyväksytty & tarkistettu: {NORDIC_XML_SPECIAL}",
            response_code=None if code == "AB" else code,
            response_lines=[]
            if code == "AB"
            else [
                OrderResponseLine(
                    product_id=q.lines[0].product_id,
                    code=line_code,
                    qty_confirmed=confirmed,
                    note=NORDIC_REASON,
                )
            ],
            created_at=q.created_at,
            updated_at=q.created_at + timedelta(minutes=1),
        )
        buyer_party = {
            "endpoint_scheme": '0216-&-"-\'-<>',
            "endpoint_id": 'OVT-ÄÄNEKOSKI-&-"<>',
            "name": NORDIC_XML_SPECIAL,
        }
        seller_party = {
            "endpoint_scheme": "0216",
            "endpoint_id": 'OVT-VALIO-ÅLAND-&-"<>',
            "name": 'Valio Aimo & Åland "Tukku" <Oy> \'Ab\' äöå',
        }
        gtins = {q.lines[0].product_id: '06410405910011-&-"<>äöå'}

        xml_text = order_response_xml(order, q, buyer=buyer_party, seller=seller_party, gtins=gtins)
        assert check_t116_rules(xml_text) == []

        root = ET.fromstring(xml_text.encode("utf-8"))
        assert root.findtext("cbc:CustomizationID", None, NS) == CUSTOMIZATION_ID
        assert root.findtext("cbc:ProfileID", None, NS) == PROFILE_ID
        assert root.findtext("cbc:OrderResponseCode", None, NS) == code

        buyer_name = root.findtext("cac:BuyerCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName", None, NS)
        seller_name = root.findtext("cac:SellerSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName", None, NS)
        assert buyer_name == NORDIC_XML_SPECIAL
        assert seller_name == 'Valio Aimo & Åland "Tukku" <Oy> \'Ab\' äöå'

        notes_texts = [el.text or "" for el in root.findall("cbc:Note", NS)]
        if code == "RE":
            assert any("Syynä: määrä & laatu <tarkistettu>" in n for n in notes_texts)
        else:
            assert any(NORDIC_XML_SPECIAL in n for n in notes_texts)

        if code == "CA":
            item_name = root.findtext("cac:OrderLine/cac:LineItem/cac:Item/cbc:Name", None, NS)
            qty_el = root.find("cac:OrderLine/cac:LineItem/cbc:Quantity", NS)
            assert item_name == NORDIC_PRODUCT_KG
            assert qty_el is not None and qty_el.text == "7.25" and qty_el.get("unitCode") == "KGM"
        else:
            assert root.findall("cac:OrderLine", NS) == []

    def test_gateway_e2e_nordic_and_xml_specials_plan_quote_approval_audit_peppol_and_cli_verify(
        self, boundary_env, tmp_path: Path
    ) -> None:
        api, backend, _, _ = boundary_env
        backend.prices["910001"] = 25.0
        jwks = api.get("/.well-known/jwks.json").json()

        r_plan = api.post(
            "/v1/plan",
            headers=H_SCHOOL,
            json={
                "customer_id": "school-tampere",
                "raw_text": f"Tilaus: {NORDIC_XML_SPECIAL}",
                "product_query": 'Kaurahiutale & Ruisjauho <Luomu> "Äänekoski" \'Tuore\' ångad öljyllä',
                "preferred_product_ids": ["910001"],
                "quantity": 20.0,
                "unit": "KG",
                "delivery_by": DELIVERY.isoformat(),
            },
        )
        assert r_plan.status_code == 200, r_plan.text
        plan = r_plan.json()
        assert plan["lines"]
        assert plan["lines"][0]["product_id"] == "910001"
        assert "Äänekoski" in plan["lines"][0]["product_name"]
        assert "&" in plan["lines"][0]["product_name"] and "<Luomu>" in plan["lines"][0]["product_name"]

        r_quote = api.post(
            "/v1/quotes",
            headers=H_SCHOOL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": "910001", "qty_sales_units": 20.0}],
                "need": plan["need"],
            },
        )
        assert r_quote.status_code == 200, r_quote.text
        q = r_quote.json()
        assert conf_verify.verify_quote(q, jwks)["ok"] is True
        assert signing.verify_quote_detailed(q, jwks_=jwks)["ok"] is True

        r_order = api.post(
            "/v1/orders",
            headers=H_SCHOOL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert r_order.status_code == 200, r_order.text
        pending = r_order.json()
        assert pending["status"] == "pending_approval"

        ab_xml = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml")
        assert ab_xml.status_code == 200
        assert check_t116_rules(ab_xml.text) == []

        sess = api.post("/v1/auth/approver-session", headers=PIN_HDR, json={"approver": SCHOOL_APPROVER}).json()["session"]
        r_dec = api.post(
            f"/v1/approvals/{pending['order_id']}/decision",
            headers={"X-Approver-Session": sess},
            json={"decision": "approve", "quote_hash": q["quote_hash"], "note": NORDIC_XML_SPECIAL},
        )
        assert r_dec.status_code == 200, r_dec.text
        placed = r_dec.json()
        assert placed["status"] == "placed"
        assert placed["response_code"] == "AP"

        ap_xml = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml")
        assert ap_xml.status_code == 200
        assert check_t116_rules(ap_xml.text) == []
        ap_root = ET.fromstring(ap_xml.text.encode("utf-8"))
        ap_notes = [el.text or "" for el in ap_root.findall("cbc:Note", NS)]
        assert any(NORDIC_XML_SPECIAL in n for n in ap_notes)

        events = api.get("/v1/audit").json()
        assert conf_verify.verify_events(events, jwks, contiguous=True)["ok"] is True
        ok_vq, _ = VQ.verify_document({"quote": q, "events": events}, jwks)
        assert ok_vq is True

        pack_path = tmp_path / "nordic_evidence.json"
        jwks_path = tmp_path / "jwks.json"
        pack_path.write_text(json.dumps({"quote": q, "events": events}, ensure_ascii=False), encoding="utf-8")
        jwks_path.write_text(json.dumps(jwks), encoding="utf-8")
        assert VQ.main([str(pack_path), str(jwks_path)]) == 0

        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "verify_quote.py"), str(pack_path), str(jwks_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0 and "VERIFIED" in proc.stdout, proc.stdout + proc.stderr


class TestBoundaryQuantitiesVsRejectedInputs:
    @pytest.mark.parametrize(
        "valid_qty,unit_price,expected_xml_qty",
        [
            (0.01, 10.0, "0.01"),
            (1_000_000, 0.0002, "1000000"),
            (1_000_000.0, 0.0002, "1000000"),
        ],
    )
    def test_valid_boundary_quantities_0_01_and_1_000_000_succeed_e2e_and_pass_t116(
        self,
        boundary_env,
        tmp_path: Path,
        valid_qty: float,
        unit_price: float,
        expected_xml_qty: str,
    ) -> None:
        api, backend, _, _ = boundary_env
        backend.prices["910001"] = unit_price
        backend.stock["910001"] = 2_000_000.0
        jwks = api.get("/.well-known/jwks.json").json()

        r_plan = api.post(
            "/v1/plan",
            headers=H_HOTEL,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "Kaurahiutale",
                "preferred_product_ids": ["910001"],
                "quantity": valid_qty,
                "unit": "KG",
                "delivery_by": DELIVERY.isoformat(),
            },
        )
        assert r_plan.status_code == 200, r_plan.text
        plan = r_plan.json()
        assert plan["covered_amount"] >= float(valid_qty)
        assert plan["shortfall"] == 0.0

        r_quote = api.post(
            "/v1/quotes",
            headers=H_HOTEL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": "910001", "qty_sales_units": valid_qty}],
            },
        )
        assert r_quote.status_code == 200, r_quote.text
        q = r_quote.json()
        assert q["lines"][0]["qty_sales_units"] == pytest.approx(float(valid_qty))

        cv = conf_verify.verify_quote(q, jwks)
        assert cv["ok"] is True and cv["hash_matches"] is True and cv["signature_valid"] is True
        sv = signing.verify_quote_detailed(q, jwks_=jwks)
        assert sv["ok"] is True
        ok_vq, _ = VQ.verify_document(q, jwks)
        assert ok_vq is True

        q_path = tmp_path / f"quote_{expected_xml_qty}.json"
        j_path = tmp_path / f"jwks_{expected_xml_qty}.json"
        q_path.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
        j_path.write_text(json.dumps(jwks), encoding="utf-8")
        assert VQ.main([str(q_path), str(j_path)]) == 0

        r_order = api.post(
            "/v1/orders",
            headers=H_HOTEL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert r_order.status_code == 200, r_order.text
        ord_data = r_order.json()
        if ord_data["status"] == "pending_approval":
            ab_xml = api.get(f"/v1/orders/{ord_data['order_id']}/peppol-order-response.xml")
            assert ab_xml.status_code == 200
            assert check_t116_rules(ab_xml.text) == []
            sess = api.post("/v1/auth/approver-session", headers=PIN_HDR, json={"approver": HOTEL_APPROVER}).json()["session"]
            r_app = api.post(
                f"/v1/approvals/{ord_data['order_id']}/decision",
                headers={"X-Approver-Session": sess},
                json={"decision": "approve", "quote_hash": q["quote_hash"]},
            )
            assert r_app.status_code == 200, r_app.text
            ord_data = r_app.json()

        assert ord_data["status"] == "placed"
        assert ord_data["response_code"] == "AP"

        ap_xml = api.get(f"/v1/orders/{ord_data['order_id']}/peppol-order-response.xml")
        assert ap_xml.status_code == 200
        assert check_t116_rules(ap_xml.text) == []

        ca_order = Order.model_validate(
            {
                **ord_data,
                "response_code": "CA",
                "response_lines": [
                    {
                        "product_id": "910001",
                        "code": "CA",
                        "qty_confirmed": valid_qty,
                        "note": f"Confirmed boundary qty {expected_xml_qty} KG",
                    }
                ],
            }
        )
        ca_xml = order_response_xml(ca_order, Quote.model_validate(q))
        assert check_t116_rules(ca_xml) == []
        ca_root = ET.fromstring(ca_xml.encode("utf-8"))
        qty_node = ca_root.find("cac:OrderLine/cac:LineItem/cbc:Quantity", NS)
        assert qty_node is not None
        assert qty_node.text == expected_xml_qty
        assert qty_node.get("unitCode") == "KGM"

    def test_count_unit_1_000_000_valid_and_0_01_fractional_count_unit_cleanly_422(self, boundary_env) -> None:
        api, backend, _, _ = boundary_env
        backend.prices["910003"] = 0.0002
        backend.stock["910003"] = 2_000_000.0
        jwks = api.get("/.well-known/jwks.json").json()

        r_ok = api.post(
            "/v1/quotes",
            headers=H_HOTEL,
            json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "910003", "qty_sales_units": 1_000_000}]},
        )
        assert r_ok.status_code == 200, r_ok.text
        q_ok = r_ok.json()
        assert conf_verify.verify_quote(q_ok, jwks)["ok"] is True

        r_frac = api.post(
            "/v1/quotes",
            headers=H_HOTEL,
            json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "910003", "qty_sales_units": 0.01}]},
        )
        err = _assert_clean_4xx(r_frac, "count_unit_0.01")
        assert r_frac.status_code == 422
        assert err["error"] == "fractional_quantity"

    @pytest.mark.parametrize(
        "bad_qty",
        [
            0,
            0.0,
            -0.0,
            -0.01,
            -1,
            -1_000_000,
            math.nan,
            math.inf,
            -math.inf,
            1_000_000.01,
            1_000_001,
            True,
            False,
            "0",
            "-0.01",
            "NaN",
            "Infinity",
            "-Infinity",
            None,
            [],
            {},
        ],
    )
    def test_rejected_quantities_blocked_before_offers_on_need_plan_and_quotes(
        self, boundary_env, bad_qty: Any
    ) -> None:
        api, backend, _, _ = boundary_env

        with pytest.raises(Exception):
            parsed_need = Need(
                customer_id="hotel-aurora",
                product_query="Kaurahiutale",
                quantity=bad_qty,
                unit="KG",
                delivery_by=DELIVERY,
            )
            validate_need(parsed_need)

        if isinstance(bad_qty, float) and not math.isfinite(bad_qty):
            raw_tok = "NaN" if math.isnan(bad_qty) else ("Infinity" if bad_qty > 0 else "-Infinity")
            before = backend.stats["upstream_calls"]
            r_p = api.post(
                "/v1/plan",
                headers={**H_HOTEL, "Content-Type": "application/json"},
                content=(
                    '{"customer_id":"hotel-aurora","product_query":"Kaurahiutale","unit":"KG",'
                    '"delivery_by":"2026-11-16","quantity":' + raw_tok + "}"
                ).encode("utf-8"),
            )
            _assert_clean_4xx(r_p, ("raw_plan", raw_tok))
            r_q = api.post(
                "/v1/quotes",
                headers={**H_HOTEL, "Content-Type": "application/json"},
                content=(
                    '{"delivery_date":"2026-11-16","lines":[{"product_id":"910001","qty_sales_units":' + raw_tok + "}]}"
                ).encode("utf-8"),
            )
            _assert_clean_4xx(r_q, ("raw_quote", raw_tok))
            assert backend.stats["upstream_calls"] == before
            return

        before = backend.stats["upstream_calls"]
        r_plan = api.post(
            "/v1/plan",
            headers=H_HOTEL,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "Kaurahiutale",
                "quantity": bad_qty,
                "unit": "KG",
                "delivery_by": DELIVERY.isoformat(),
            },
        )
        _assert_clean_4xx(r_plan, ("plan", bad_qty))
        assert backend.stats["upstream_calls"] == before

        r_quote = api.post(
            "/v1/quotes",
            headers=H_HOTEL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": "910001", "qty_sales_units": bad_qty}],
            },
        )
        _assert_clean_4xx(r_quote, ("quote", bad_qty))
        assert backend.stats["upstream_calls"] == before

    @pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
    def test_non_finite_floats_rejected_across_canonical_json_verifiers_and_peppol(
        self, boundary_env, tmp_path: Path, non_finite: float
    ) -> None:
        _ = boundary_env
        jwks = signing.jwks()
        q = _make_sample_signed_quote()
        q_bad = {**q.model_dump(mode="json"), "total_net": non_finite}

        with pytest.raises(ValueError):
            conf_verify.canonical_json(q_bad)
        with pytest.raises(ValueError):
            signing.canonical_json(q_bad)
        with pytest.raises(ValueError):
            VQ.canon(q_bad)

        cv = conf_verify.verify_quote(q_bad, jwks)
        assert cv["ok"] is False and cv["hash_matches"] is False
        sv = signing.verify_quote_detailed(q_bad, jwks_=jwks, now=q.created_at)
        assert sv["ok"] is False and sv["hash_matches"] is False
        ok_vq, lines_vq = VQ.verify_document(q_bad, jwks)
        assert ok_vq is False and any("hash MISMATCH" in ln for ln in lines_vq)

        raw_const = "NaN" if math.isnan(non_finite) else ("Infinity" if non_finite > 0 else "-Infinity")
        bad_doc = tmp_path / "nonfinite_quote.json"
        jwks_file = tmp_path / "jwks.json"
        bad_doc.write_text('{"quote_id":"Q-1","total_net":' + raw_const + "}", encoding="utf-8")
        jwks_file.write_text(json.dumps(jwks), encoding="utf-8")
        assert VQ.main([str(bad_doc), str(jwks_file)]) == 1

        bad_ca_order = Order.model_construct(
            order_id="ORD-nan",
            quote_id=q.quote_id,
            quote_hash=q.quote_hash or "",
            customer_id=q.customer_id,
            agent_id=q.agent_id,
            status="placed",
            decision=PolicyDecision(outcome="auto_approve", policy_id="hotel-aurora", policy_version="1", hits=[]),
            response_code="CA",
            response_lines=[OrderResponseLine.model_construct(product_id="910001", code="CA", qty_confirmed=non_finite, note="")],
            created_at=q.created_at,
            updated_at=q.created_at,
        )
        with pytest.raises(ValueError, match="finite"):
            order_response_xml(bad_ca_order, q)
        with pytest.raises(ValueError, match="finite"):
            peppol._num(non_finite)
        with pytest.raises(ValueError, match="finite"):
            peppol._num(True)


class TestTamperedQuotesJwsHashChainsAndPeppolRules:
    def test_tampered_quote_hashes_and_fields_flagged_across_all_verifiers_and_409_on_gateway(
        self, boundary_env, tmp_path: Path
    ) -> None:
        api, backend, _, _ = boundary_env
        backend.prices["910001"] = 5.0
        jwks = api.get("/.well-known/jwks.json").json()

        r_q = api.post(
            "/v1/quotes",
            headers=H_HOTEL,
            json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "910001", "qty_sales_units": 200.0}]},
        )
        assert r_q.status_code == 200, r_q.text
        q = r_q.json()

        mutated_price = {**q, "total_net": round(float(q["total_net"]) - 100.0, 2)}
        rehashed_without_key = {**mutated_price, "quote_hash": conf_verify.quote_hash(mutated_price)}
        mutated_nordic = {
            **q,
            "lines": [{**q["lines"][0], "product_name": q["lines"][0]["product_name"].replace("Ä", "A")}],
        }
        wrong_kid_field = {**q, "kid": "spoofed-kid"}
        missing_hash = {**q, "quote_hash": ""}
        non_str_sig = {**q, "signature": 12345}

        for label, bad_q in (
            ("mutated_price", mutated_price),
            ("rehashed_without_key", rehashed_without_key),
            ("mutated_nordic", mutated_nordic),
            ("wrong_kid_field", wrong_kid_field),
            ("missing_hash", missing_hash),
            ("non_str_sig", non_str_sig),
        ):
            cv = conf_verify.verify_quote(bad_q, jwks)
            sv = signing.verify_quote_detailed(bad_q, jwks_=jwks)
            assert cv["ok"] is False, (label, cv)
            assert sv["ok"] is False, (label, sv)
            if label != "wrong_kid_field":
                ok_vq, _ = VQ.verify_document(bad_q, jwks)
                assert ok_vq is False, label
                bad_file = tmp_path / f"bad_{label}.json"
                jwks_file = tmp_path / "jwks.json"
                bad_file.write_text(json.dumps(bad_q, ensure_ascii=False), encoding="utf-8")
                jwks_file.write_text(json.dumps(jwks), encoding="utf-8")
                assert VQ.main([str(bad_file), str(jwks_file)]) == 1

        assert conf_verify.verify_quote(rehashed_without_key, jwks)["hash_matches"] is True
        assert conf_verify.verify_quote(rehashed_without_key, jwks)["signature_valid"] is False

        r_wrong_order = api.post(
            "/v1/orders",
            headers=H_HOTEL,
            json={"quote_id": q["quote_id"], "quote_hash": rehashed_without_key["quote_hash"]},
        )
        assert r_wrong_order.status_code == 409

        r_pending = api.post(
            "/v1/orders",
            headers=H_HOTEL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert r_pending.status_code == 200
        oid = r_pending.json()["order_id"]
        assert r_pending.json()["status"] == "pending_approval"

        sess = api.post("/v1/auth/approver-session", headers=PIN_HDR, json={"approver": HOTEL_APPROVER}).json()["session"]
        r_wrong_dec = api.post(
            f"/v1/approvals/{oid}/decision",
            headers={"X-Approver-Session": sess},
            json={"decision": "approve", "quote_hash": "0" * 64},
        )
        assert r_wrong_dec.status_code == 409

    def test_adversarial_jws_signatures_alg_none_spoofed_kid_bitflip_and_bad_tokens_refused(
        self, boundary_env
    ) -> None:
        _ = boundary_env
        jwks = signing.jwks()
        legit_kid = signing.kid()
        q = _make_sample_signed_quote()
        q_dict = q.model_dump(mode="json")
        h64, p64, s64 = q_dict["signature"].split(".")

        sig_bytes = bytearray(signing.b64url_decode(s64))
        sig_bytes[0] ^= 0xFF
        bitflipped_tok = f"{h64}.{p64}.{_b64u(bytes(sig_bytes))}"

        alg_none_hdr = _b64u(json.dumps({"alg": "none", "kid": legit_kid}).encode("utf-8"))
        alg_none_empty = f"{alg_none_hdr}.{p64}."
        alg_none_zeros = f"{alg_none_hdr}.{p64}.{_b64u(b'0' * 64)}"

        alg_hs256_hdr = _b64u(json.dumps({"alg": "HS256", "kid": legit_kid}).encode("utf-8"))
        alg_hs256_tok = f"{alg_hs256_hdr}.{p64}.{s64}"

        rogue_priv = Ed25519PrivateKey.generate()
        rogue_spoof_hdr = _b64u(json.dumps({"alg": "EdDSA", "kid": legit_kid, "typ": "quote+jws"}).encode("utf-8"))
        rogue_spoof_in = f"{rogue_spoof_hdr}.{p64}"
        rogue_spoof_sig = _b64u(rogue_priv.sign(rogue_spoof_in.encode("ascii")))
        rogue_spoof_tok = f"{rogue_spoof_in}.{rogue_spoof_sig}"

        rogue_unknown_hdr = _b64u(json.dumps({"alg": "EdDSA", "kid": "rogue-kid-999"}).encode("utf-8"))
        rogue_unknown_in = f"{rogue_unknown_hdr}.{p64}"
        rogue_unknown_tok = f"{rogue_unknown_in}.{_b64u(rogue_priv.sign(rogue_unknown_in.encode('ascii')))}"

        short_sig_tok = f"{h64}.{p64}.{_b64u(b'x' * 32)}"
        array_hdr_tok = f"{_b64u(b'[1,2]')}.{p64}.{s64}"
        nan_payload_tok = f"{h64}.{_b64u(b'{\"quote_hash\":NaN}')}.{s64}"

        bad_tokens = [
            "",
            None,
            123,
            "only-one-part",
            "two.parts",
            "four.parts.here.extra",
            "..",
            "bad+b64/char===.x.y",
            bitflipped_tok,
            alg_none_empty,
            alg_none_zeros,
            alg_hs256_tok,
            rogue_spoof_tok,
            rogue_unknown_tok,
            short_sig_tok,
            array_hdr_tok,
            nan_payload_tok,
        ]
        for tok in bad_tokens:
            with pytest.raises(ValueError):
                conf_verify.verify_jws(tok, jwks)
            with pytest.raises(signing.JWSError):
                signing.verify_jws(tok, jwks)
            assert VQ.jws(tok, {k["kid"]: k for k in jwks["keys"]}) == {}

            tampered_q = {**q_dict, "signature": tok}
            assert conf_verify.verify_quote(tampered_q, jwks)["ok"] is False
            assert signing.verify_quote_detailed(tampered_q, jwks_=jwks)["ok"] is False
            ok_vq, _ = VQ.verify_document(tampered_q, jwks)
            assert ok_vq is False

    def test_broken_hash_chains_and_tampered_audit_events_cleanly_flagged(
        self, boundary_env, tmp_path: Path
    ) -> None:
        _, _, _, _ = boundary_env
        jwks = signing.jwks()
        audit_db = tmp_path / "chain_test.sqlite"
        log = AuditLog(audit_db)

        e1 = log.append("agent:hotel-aurora/buyer-1", "quote.created", "Q-chain-1", f"Luotu {NORDIC_XML_SPECIAL}", {"qty": 0.01})
        e2 = log.append("system:gateway", "policy.evaluated", "ORD-chain-1", "Arvioitu käytäntö", {"qty": 1_000_000}, related_ids=["Q-chain-1"])
        e3 = log.append("human:anna@hotel-aurora.example", "order.approved", "ORD-chain-1", NORDIC_REASON, {"decision": "approve"})

        intact = [ev.model_dump(mode="json") for ev in (e1, e2, e3)]
        assert log.verify(jwks)["ok"] is True
        assert conf_verify.verify_events(intact, jwks, contiguous=True)["ok"] is True
        ok_intact, _ = VQ.verify_document(intact, jwks)
        assert ok_intact is True

        log.tamper_for_demo(2, summary="Tampered summary after signing")
        broken_live = log.verify(jwks)
        assert broken_live["ok"] is False and broken_live["broken_at"] == 2
        tampered_rows = [ev.model_dump(mode="json") for ev in log.events()]
        assert conf_verify.verify_events(tampered_rows, jwks, contiguous=True)["ok"] is False
        assert VQ.verify_document(tampered_rows, jwks)[0] is False

        assert log.untamper_for_demo(2) is True
        assert log.verify(jwks)["ok"] is True

        rehashed_unsigned = [dict(x) for x in intact]
        rehashed_unsigned[1] = {**rehashed_unsigned[1], "summary": "Attacker rehashed without key"}
        rehashed_unsigned[1]["hash"] = conf_verify.event_hash(rehashed_unsigned[1])
        rehashed_unsigned[2] = {**rehashed_unsigned[2], "prev_hash": rehashed_unsigned[1]["hash"]}
        rehashed_unsigned[2]["hash"] = conf_verify.event_hash(rehashed_unsigned[2])

        bad_genesis = [{**intact[0], "prev_hash": "f" * 64}, intact[1], intact[2]]
        broken_link = [intact[0], {**intact[1], "prev_hash": "1" * 64}, intact[2]]
        seq_gap = [intact[0], intact[2]]
        starts_at_two = [intact[1], intact[2]]
        transplanted_sig = [intact[0], {**intact[1], "signature": intact[0]["signature"]}, intact[2]]
        bool_seq = [{**intact[0], "seq": True}, intact[1]]
        ctrl_action = [{**intact[0], "action": "quote\x00created"}, intact[1]]

        for label, bad_chain in (
            ("rehashed_unsigned", rehashed_unsigned),
            ("bad_genesis", bad_genesis),
            ("broken_link", broken_link),
            ("seq_gap", seq_gap),
            ("starts_at_two", starts_at_two),
            ("transplanted_sig", transplanted_sig),
            ("bool_seq", bool_seq),
        ):
            res_cv = conf_verify.verify_events(bad_chain, jwks, contiguous=True)
            assert res_cv["ok"] is False and res_cv["problems"], (label, res_cv)

        for label, bad_chain in (
            ("rehashed_unsigned", rehashed_unsigned),
            ("bad_genesis", bad_genesis),
            ("broken_link", broken_link),
            ("transplanted_sig", transplanted_sig),
            ("bool_seq", bool_seq),
            ("ctrl_action", ctrl_action),
        ):
            ok_vq, _ = VQ.verify_document(bad_chain, jwks)
            assert ok_vq is False, label

    def test_verify_quote_cli_bad_arguments_and_malformed_sources_never_crash(self, tmp_path: Path) -> None:
        valid_doc = tmp_path / "doc.json"
        valid_jwks = tmp_path / "jwks.json"
        valid_doc.write_text(json.dumps({"quote_id": "Q-1"}), encoding="utf-8")
        valid_jwks.write_text(json.dumps({"keys": []}), encoding="utf-8")

        assert VQ.main([]) == 2
        assert VQ.main([str(valid_doc)]) == 2
        assert VQ.main([str(valid_doc), str(valid_jwks), "extra"]) == 2
        assert VQ.main([123, str(valid_jwks)]) == 2
        assert VQ.main([str(tmp_path / "missing.json"), str(valid_jwks)]) == 1

        for bad_url in (
            "http://user:pass@example.com/jwks.json",
            "https://..badhost/jwks.json",
            "https://.leadingdot/jwks.json",
            "https://example.com:99999/jwks.json",
            "https://example.com/jwks with space.json",
            "https://example.com/bad\x00ctrl.json",
        ):
            assert VQ.main([str(valid_doc), bad_url]) == 1

    def test_check_t116_rules_flags_all_schematron_r001_to_r010_and_malformed_xml(self, boundary_env) -> None:
        _ = boundary_env
        q = _make_sample_signed_quote(qty=10.0, sales_unit="KG", unit_price=2.0)
        q2 = q.model_copy(
            update={
                "lines": [
                    q.lines[0],
                    QuoteLine(
                        product_id="910002",
                        product_name=NORDIC_PRODUCT_L,
                        qty_sales_units=5.0,
                        sales_unit="L",
                        unit_price=1.5,
                        line_total=7.5,
                        tier="preferred",
                        flags=[],
                    ),
                ]
            }
        )
        base_ap_order = Order(
            order_id="ORD-t116",
            quote_id=q.quote_id,
            quote_hash=q.quote_hash or "",
            customer_id=q.customer_id,
            agent_id=q.agent_id,
            status="placed",
            decision=PolicyDecision(outcome="auto_approve", policy_id="hotel-aurora", policy_version="1", hits=[]),
            response_code="AP",
            response_lines=[OrderResponseLine(product_id="910001", code="AP", qty_confirmed=10.0, note="ok")],
            created_at=q.created_at,
            updated_at=q.created_at,
        )
        base_ca_order = base_ap_order.model_copy(
            update={
                "response_code": "CA",
                "response_lines": [
                    OrderResponseLine(product_id="910001", code="CA", qty_confirmed=6.0, note="partial"),
                    OrderResponseLine(product_id="910002", code="AP", qty_confirmed=5.0, note="ok"),
                ],
            }
        )

        ap_xml = order_response_xml(base_ap_order, q)
        ca_xml = order_response_xml(base_ca_order, q2)
        assert check_t116_rules(ap_xml) == []
        assert check_t116_rules(ca_xml) == []

        r001_xml = ET.fromstring(ap_xml.encode("utf-8"))
        bp = r001_xml.find("cac:BuyerCustomerParty/cac:Party", NS)
        assert bp is not None
        for child in list(bp):
            bp.remove(child)
        assert "PEPPOL-T116-R001" in check_t116_rules(ET.tostring(r001_xml, encoding="unicode"))

        r002_xml = ET.fromstring(ap_xml.encode("utf-8"))
        sp = r002_xml.find("cac:SellerSupplierParty/cac:Party", NS)
        assert sp is not None
        for child in list(sp):
            sp.remove(child)
        assert "PEPPOL-T116-R002" in check_t116_rules(ET.tostring(r002_xml, encoding="unicode"))

        r003_xml = ET.fromstring(ca_xml.encode("utf-8"))
        line_ids = r003_xml.findall("cac:OrderLine/cac:LineItem/cbc:ID", NS)
        assert len(line_ids) == 2
        line_ids[1].text = line_ids[0].text
        assert "PEPPOL-T116-R003" in check_t116_rules(ET.tostring(r003_xml, encoding="unicode"))

        r004_xml = ap_xml.replace("<cbc:EndDate>2026-11-16</cbc:EndDate>", "<cbc:EndDate>2026-11-15</cbc:EndDate>", 1)
        assert "PEPPOL-T116-R004" in check_t116_rules(r004_xml)

        r005_xml = ca_xml.replace('currencyID="EUR"', 'currencyID="SEK"', 1)
        assert "PEPPOL-T116-R005" in check_t116_rules(r005_xml)

        r006_xml = ap_xml.replace(CUSTOMIZATION_ID, "urn:wrong:customization", 1)
        assert "PEPPOL-T116-R006" in check_t116_rules(r006_xml)

        r007_xml = ap_xml.replace(">AP<", ">CA<", 1)
        assert "PEPPOL-T116-R007" in check_t116_rules(r007_xml)

        assert "PEPPOL-T116-R008" in check_t116_rules(ca_xml.replace(">CA<", ">AP<", 1))
        assert "PEPPOL-T116-R009" in check_t116_rules(ca_xml.replace(">CA<", ">RE<", 1))
        assert "PEPPOL-T116-R010" in check_t116_rules(ca_xml.replace(">CA<", ">AB<", 1))

        rogue_tag_xml = ap_xml.replace("</OrderResponse>", "<ForbiddenExtraTag>1</ForbiddenExtraTag></OrderResponse>")
        assert any("ForbiddenExtraTag" in e for e in check_t116_rules(rogue_tag_xml))

        for malformed in (
            "",
            "   ",
            None,
            123,
            "<OrderResponse><broken>",
            '<!DOCTYPE foo [<!ENTITY xxe "bad">]><OrderResponse/>',
            "<OrderResponse>" + ("x" * (peppol.MAX_XML_CHARS + 10)) + "</OrderResponse>",
        ):
            assert check_t116_rules(malformed) == ["invalid_xml"]

    def test_conformance_peppol_check_audit_and_confirm_lines_boundary_primitives(
        self, boundary_env
    ) -> None:
        _ = boundary_env
        jwks = signing.jwks()

        for non_obj in (None, True, 42, "str", [1, 2]):
            assert conf_verify.verify_quote(non_obj, jwks)["ok"] is False
            assert signing.verify_quote_detailed(non_obj, jwks_=jwks)["ok"] is False
        assert conf_verify.verify_events("not-a-list", jwks)["ok"] is False
        assert conf_verify.verify_events(["not-a-dict", {"seq": "bad"}], jwks)["ok"] is False

        with pytest.raises(ValueError):
            conf_verify.b64d(123)
        with pytest.raises(ValueError):
            conf_verify.b64e("not-bytes")
        with pytest.raises(ValueError):
            conf_verify.sha256_hex(123)
        with pytest.raises(ValueError):
            conf_verify.quote_hash(["not-dict"])
        with pytest.raises(ValueError):
            conf_verify.event_hash({"seq": 1})

        q_small = _make_sample_signed_quote(qty=0.02, sales_unit="KG", unit_price=10.0)
        expired_check = signing.verify_quote_detailed(
            q_small,
            jwks_=jwks,
            now=q_small.expires_at + timedelta(minutes=5),
        )
        assert expired_check["ok"] is True and expired_check["expired"] is True
        bad_exp_check = signing.verify_quote_detailed(
            {**q_small.model_dump(mode="json"), "expires_at": "not-a-timestamp"},
            jwks_=jwks,
        )
        assert bad_exp_check["ok"] is False and bad_exp_check["expired"] is True

        partial_offer = Offer(
            product_id="910001",
            requested_date=DELIVERY,
            available=True,
            available_amount=0.01,
            sales_unit="KG",
            unit_price=10.0,
            fetched_at=q_small.created_at,
        )
        hdr_ca, rlines_ca = store.confirm_lines(q_small, {"910001": partial_offer})
        assert hdr_ca == "CA"
        assert len(rlines_ca) == 1 and rlines_ca[0].code == "CA" and rlines_ca[0].qty_confirmed == pytest.approx(0.01)

        ord_ca = Order(
            order_id="ORD-partial-001",
            quote_id=q_small.quote_id,
            quote_hash=q_small.quote_hash or "",
            customer_id=q_small.customer_id,
            agent_id=q_small.agent_id,
            status="placed",
            decision=PolicyDecision(outcome="auto_approve", policy_id="hotel-aurora", policy_version="1", hits=[]),
            response_code=hdr_ca,
            response_lines=rlines_ca,
            created_at=q_small.created_at,
            updated_at=q_small.created_at,
        )
        xml_ca = order_response_xml(ord_ca, q_small)
        assert check_t116_rules(xml_ca) == []
        assert ET.fromstring(xml_ca.encode("utf-8")).findtext("cac:OrderLine/cac:LineItem/cbc:Quantity", None, NS) == "0.01"

        suite = conf_main.Suite("http://mock.local", "hotel-aurora", "buyer-1", quiet=True)
        try:
            conf_main.peppol_check(suite, ord_ca.model_dump(mode="json"), "P1")
            conf_main.peppol_check(suite, {"status": "placed", "response_code": None, "response_lines": []}, "P2")
            conf_main.peppol_check(
                suite,
                {"status": "placed", "response_code": "XX", "response_lines": [{"code": "XX"}]},
                "P3",
            )
        finally:
            suite.h.close()
        rows_by_id = {r["id"]: r["status"] for r in suite.rows}
        assert rows_by_id == {"P1": "PASS", "P2": "FAIL", "P3": "FAIL"}

