"""Keyless bad-input hardening tests for aimo.gateway.app and aimo.gateway.schemas."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

import aimo.catalog as cat
from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import app as gw_app
from aimo.gateway import human_auth, resilience, schemas, service
from aimo.gateway.app import create_app
from aimo.models import Need, Plan, PlanLine, Product, ProductSummary

from test_gateway import FixtureClient, H

DELIVERY = "2026-11-16"
PIN = "test-w2-wave7-pin"
ANNA = "anna@hotel-aurora.example"
BASE_NEED: dict[str, Any] = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "quantity": 10.0,
    "unit": "L",
    "delivery_by": DELIVERY,
}


class _Unstringable:
    def __str__(self) -> str:
        raise RuntimeError("unstringable")


def _make_stub_catalog(plan_calls: list[Need] | None = None) -> Any:
    prod = Product(
        id="464306",
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )
    summary = ProductSummary(
        id="464306",
        name=prod.name,
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )

    def _plan(need: Need) -> Plan:
        if plan_calls is not None:
            plan_calls.append(need)
        ln = PlanLine(
            product_id="464306",
            product_name=prod.name,
            tier="best",
            qty_sales_units=need.quantity,
            sales_unit="ST",
            covered_amount=need.quantity,
            unit_price=1.01,
            line_total=round(need.quantity * 1.01, 2),
            reasons=["matched"],
            flags=[],
        )
        return Plan(
            need=need,
            lines=[ln],
            candidates=[],
            covered_amount=need.quantity,
            shortfall=0.0,
            total_net=ln.line_total,
            explanation="ok",
        )

    return SimpleNamespace(
        catalog_available=lambda: True,
        CATALOG_MISSING_MESSAGE="Catalog missing",
        search=lambda q, **kw: [summary],
        get_product=lambda pid: prod if str(pid) == "464306" else None,
        plan_need=_plan,
    )


@pytest.fixture(autouse=True)
def keyless_isolated_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setenv("AIMO_APPROVER_PIN", PIN)
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
        "AIMO_API_KEY",
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_APPROVER_PINS",
        "AIMO_ENV",
        "AIMO_MCP_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(var, raising=False)
    human_auth.reset_for_tests()
    resilience._breakers.clear()
    bc.set_client(FixtureClient())
    yield
    human_auth.reset_for_tests()
    resilience._breakers.clear()
    bc.set_client(None)


def _assert_4xx_json(resp, expected_status: int | tuple[int, ...] | None = None) -> dict[str, Any]:
    if isinstance(expected_status, int):
        assert resp.status_code == expected_status, (resp.status_code, resp.text[:300])
    elif isinstance(expected_status, tuple):
        assert resp.status_code in expected_status, (resp.status_code, resp.text[:300])
    else:
        assert 400 <= resp.status_code < 500, (resp.status_code, resp.text[:300])
    body = resp.json()
    assert isinstance(body, dict)
    assert isinstance(body.get("error"), str) and body["error"]
    assert isinstance(body.get("message"), str) and body["message"]
    return body


@pytest.mark.parametrize("good_text", ["", "Milk 1 L", "Täysmaito 1,5 % €", "Line 1\nLine 2\tTabbed\r\nOK"])
def test_unsafe_text_validator_allows_printable_unicode_and_whitespace(good_text: str):
    assert schemas._has_unsafe_text_char(good_text) is False
    assert schemas._reject_unsafe_text_chars(good_text) == good_text


@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b", "\x1f", "\x7f", "\x85", "\x9f", "\ud800", "\udfff"])
def test_unsafe_text_validator_rejects_controls_and_surrogates(bad_char: str):
    sample = f"prefix{bad_char}suffix"
    assert schemas._has_unsafe_text_char(sample) is True
    with pytest.raises(ValueError):
        schemas._reject_unsafe_text_chars(sample)
    with pytest.raises(ValidationError):
        schemas.QuoteLineIn(product_id="464306", qty_sales_units=1, product_name=sample)
    if 0xD800 <= ord(bad_char) <= 0xDFFF:
        with pytest.raises(ValidationError):
            schemas.DecisionRequest(decision="approve", approver=ANNA, quote_hash="0" * 64, note=sample)
    else:
        dec = schemas.DecisionRequest(decision="approve", approver=ANNA, quote_hash="0" * 64, note=sample)
        assert dec.note == sample


def test_quote_line_product_name_and_decision_note_bounds():
    ln = schemas.QuoteLineIn(product_id="464306", qty_sales_units=2, product_name="Valio Eila\t1 L")
    assert ln.product_name == "Valio Eila\t1 L"
    with pytest.raises(ValidationError):
        schemas.QuoteLineIn(product_id="464306", qty_sales_units=2, product_name="x" * 301)

    dec = schemas.DecisionRequest(decision="approve", approver=ANNA, quote_hash="a" * 64, note="Line 1\nLine 2")
    assert dec.note == "Line 1\nLine 2"
    with pytest.raises(ValidationError):
        schemas.DecisionRequest(decision="approve", approver=ANNA, quote_hash="a" * 64, note="x" * 2001)


@pytest.mark.parametrize(
    "bad_date",
    [
        0,
        86400,
        -86400,
        1731715200,
        0.0,
        1.5,
        float("nan"),
        float("inf"),
        True,
        False,
        datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc),
        "2026-11-16\x00",
        "2026-11-16\x1b",
        "2026-13-01",
        "not-a-date",
        "",
        None,
    ],
)
def test_iso_date_rejects_numbers_booleans_datetimes_and_malformed_dates(bad_date: Any):
    with pytest.raises(ValidationError):
        schemas.OffersRequest(product_ids=["464306"], requested_date=bad_date)
    with pytest.raises(ValidationError):
        schemas.QuoteRequest(
            delivery_date=bad_date,
            lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=1)],
        )


@pytest.mark.parametrize("valid_date", [date(2026, 11, 16), "2026-11-16", "0001-01-01", "2099-12-31", "9999-12-31"])
def test_iso_date_accepts_date_objects_and_iso_strings(valid_date: Any):
    off = schemas.OffersRequest(product_ids=["464306"], requested_date=valid_date)
    assert isinstance(off.requested_date, date)
    qr = schemas.QuoteRequest(
        delivery_date=valid_date,
        lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=1)],
    )
    assert isinstance(qr.delivery_date, date)


@pytest.mark.parametrize(
    "bad_prefs",
    [
        "not-a-dict",
        ["brand"],
        123,
        {f"k{i}": i for i in range(51)},
        {"items": list(range(51))},
        {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}},
        {"bad\x00key": "v"},
        {"k" * 101: "v"},
        {"brand": "Valio\x00"},
        {"brand": "Valio\x07"},
        {"brand": "Valio\ud800"},
        {"brand": "x" * 1001},
        {"huge_int": 10**13},
        {"neg_huge_int": -(10**13)},
        {"overflow_int": 10**400},
        {"nan_val": float("nan")},
        {"inf_val": float("inf")},
        {"obj": {1, 2}},
        {"fat_pct": True},
        {"fat_pct": False},
        {"fat_pct": "nan"},
        {"fat_pct": "inf"},
        {"fat_pct": "-inf"},
        {"fat_pct": "abc"},
        {"fat_pct": ""},
        {"fat_pct": "   "},
        {"fat_pct": -0.1},
        {"fat_pct": 100.1},
        {"fat_pct": 10**400},
        {"fat_pct": [1.5]},
        {"fat_pct": {"v": 1.5}},
    ],
)
def test_validate_preferences_rejects_hostile_structures_and_fat_pct(bad_prefs: Any):
    with pytest.raises(ValueError):
        schemas.validate_preferences(bad_prefs)


@pytest.mark.parametrize(
    "good_prefs",
    [
        {},
        {"brand": "Valio", "fat_pct": 1.5, "organic": True, "pack": "10 L"},
        {"fat_pct": 0},
        {"fat_pct": 100},
        {"fat_pct": "2.5"},
        {"fat_pct": None},
        {"injection_flags": [{"tool": "get_product", "reasons": ["ok"]}]},
        {"notes": "Multi-line\npreference\ttext"},
    ],
)
def test_validate_preferences_accepts_valid_shapes(good_prefs: dict[str, Any]):
    schemas.validate_preferences(good_prefs)


def test_quote_request_validate_need_enforces_unit_date_lists_and_preferences():
    def _constructed(**kw: Any) -> Need:
        base = {
            "raw_text": "",
            "customer_id": "hotel-aurora",
            "product_query": "milk",
            "required_claims": [],
            "excluded_allergens": [],
            "quantity": 5.0,
            "unit": "L",
            "delivery_by": date(2026, 11, 16),
            "preferred_product_ids": [],
            "preferences": {},
        }
        base.update(kw)
        return Need.model_construct(**base)

    invalid_needs = [
        _constructed(unit="GALLONS"),
        _constructed(unit=None),
        _constructed(delivery_by="2026-11-16"),
        _constructed(delivery_by=datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)),
        _constructed(product_query="milk\x07"),
        _constructed(product_query="milk\ud800"),
        _constructed(raw_text="note\x1b"),
        _constructed(raw_text="note\udfff"),
        _constructed(required_claims=["CLAIM"] * 51),
        _constructed(required_claims=["BAD\x00CLAIM"]),
        _constructed(required_claims=["x" * 101]),
        _constructed(required_claims=[123]),
        _constructed(excluded_allergens=["milk"] * 51),
        _constructed(excluded_allergens=["milk\x07"]),
        _constructed(excluded_allergens=["x" * 101]),
        _constructed(excluded_allergens=[True]),
        _constructed(preferred_product_ids=[f"id-{i}" for i in range(201)]),
        _constructed(preferred_product_ids=["   "]),
        _constructed(preferred_product_ids=["464 306"]),
        _constructed(preferred_product_ids=["464\ud800"]),
        _constructed(preferences={"fat_pct": 10**400}),
        _constructed(preferences={"fat_pct": "nan"}),
        _constructed(product_query="   ", preferred_product_ids=[]),
    ]
    for bad_need in invalid_needs:
        with pytest.raises(ValidationError):
            schemas.QuoteRequest(
                delivery_date=date(2026, 11, 16),
                lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=1)],
                need=bad_need,
            )


def test_json_clean_payload_preserves_lengths_and_scrubs_non_json_values():
    long_msg = "A" * 600
    many_items = [f"item-{i}" for i in range(60)]
    dirty = {
        "error": "test_err",
        "message": long_msg + "\ud800",
        "detail": {
            "items": many_items,
            "nan": float("nan"),
            "inf": float("inf"),
            "ninf": float("-inf"),
            "huge_int": 10**200,
            "normal_int": 42,
            "flag": True,
            "none": None,
            "broken": _Unstringable(),
        },
    }
    cleaned = schemas.json_clean_payload(dirty)
    encoded = json.dumps(cleaned, ensure_ascii=False, allow_nan=False).encode("utf-8")
    parsed = json.loads(encoded)
    assert len(parsed["message"]) == 601
    assert len(parsed["detail"]["items"]) == 60
    assert parsed["detail"]["normal_int"] == 42
    assert parsed["detail"]["flag"] is True
    assert parsed["detail"]["none"] is None
    assert parsed["detail"]["broken"] == "_Unstringable"


def test_install_error_handlers_covers_unicode_encode_overflow_recursion_and_crlf_headers():
    mini = FastAPI()
    schemas.install_error_handlers(mini)

    @mini.get("/unicode-encode")
    def _uenc():
        return "\ud800".encode("utf-8")

    @mini.get("/overflow")
    def _ovf():
        return float(10**400)

    @mini.get("/recursion")
    def _rec():
        raise RecursionError("too deep")

    @mini.get("/crlf-http")
    def _crlf():
        raise StarletteHTTPException(
            status_code=True,
            detail="bad header test",
            headers={
                "X-Clean": "ok",
                "X-CRLF": "line1\r\nEvil: 1",
                "X-Null": "a\x00b",
                "X-Euro": "€",
                "X-Latin1": "äö",
                "   ": "blank",
            },
        )

    client = TestClient(mini, raise_server_exceptions=False)
    r_ue = client.get("/unicode-encode")
    assert _assert_4xx_json(r_ue, 422)["error"] == "invalid_encoding"

    r_ov = client.get("/overflow")
    assert _assert_4xx_json(r_ov, 422)["error"] == "invalid_request"

    r_rc = client.get("/recursion")
    assert _assert_4xx_json(r_rc, 400)["error"] == "invalid_json"

    r_ch = client.get("/crlf-http")
    assert _assert_4xx_json(r_ch, 400)["error"] == "bad_request"
    assert r_ch.headers.get("X-Clean") == "ok"
    assert "X-CRLF" not in r_ch.headers
    assert "X-Null" not in r_ch.headers
    assert "X-Euro" not in r_ch.headers
    assert "X-Latin1" not in r_ch.headers


def test_http_plan_rejects_bad_need_fields_before_planner_or_catalog_check(monkeypatch: pytest.MonkeyPatch, tmp_path):
    plan_calls: list[Need] = []
    monkeypatch.setattr(service, "_catalog", lambda: _make_stub_catalog(plan_calls))
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    bad_payloads: list[tuple[dict[str, Any], str]] = [
        ({**BASE_NEED, "quantity": 0}, "bad_quantity"),
        ({**BASE_NEED, "quantity": -0.0}, "bad_quantity"),
        ({**BASE_NEED, "quantity": -3.5}, "bad_quantity"),
        ({**BASE_NEED, "quantity": True}, "validation_error"),
        ({**BASE_NEED, "quantity": 1_000_001}, "absurd_quantity"),
        ({**BASE_NEED, "product_query": "   "}, "empty_need"),
        ({**BASE_NEED, "product_query": "x" * 501}, "query_too_long"),
        ({**BASE_NEED, "product_query": "milk\x07"}, "invalid_need"),
        ({**BASE_NEED, "product_query": "milk\x1b"}, "invalid_need"),
        ({**BASE_NEED, "raw_text": "hello\x07world"}, "invalid_need"),
        ({**BASE_NEED, "preferred_product_ids": [f"id-{i}" for i in range(201)]}, "invalid_need"),
        ({**BASE_NEED, "preferred_product_ids": ["   "]}, "invalid_need"),
        ({**BASE_NEED, "preferred_product_ids": ["464 306"]}, "invalid_need"),
        ({**BASE_NEED, "preferred_product_ids": ["464\x07306"]}, "invalid_need"),
        ({**BASE_NEED, "required_claims": [f"C{i}" for i in range(51)]}, "invalid_need"),
        ({**BASE_NEED, "required_claims": ["FREE\x00"]}, "invalid_need"),
        ({**BASE_NEED, "required_claims": ["x" * 101]}, "invalid_need"),
        ({**BASE_NEED, "excluded_allergens": [f"a{i}" for i in range(51)]}, "invalid_need"),
        ({**BASE_NEED, "excluded_allergens": ["milk\x07"]}, "invalid_need"),
        ({**BASE_NEED, "excluded_allergens": ["x" * 101]}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": 10**400}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": "nan"}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": "inf"}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": "-inf"}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": -1}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": 105}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": True}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"fat_pct": "abc"}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {f"k{i}": i for i in range(51)}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}}, "invalid_need"),
        ({**BASE_NEED, "preferences": {"brand": "Valio\x07"}}, "invalid_need"),
    ]
    for payload, _ in bad_payloads:
        r = client.post("/v1/plan", headers=H, json=payload)
        _assert_4xx_json(r, 422)

    raw_surrogate_cases = [
        b'{"customer_id":"hotel-aurora","product_query":"milk \\ud800","quantity":10,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","raw_text":"\\ud800","quantity":10,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","preferred_product_ids":["\\ud800"],"quantity":10,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","preferences":{"k":"\\ud800"},"quantity":10,"unit":"L","delivery_by":"2026-11-16"}',
    ]
    for raw in raw_surrogate_cases:
        r_raw = client.post("/v1/plan", headers={**H, "Content-Type": "application/json"}, content=raw)
        err = _assert_4xx_json(r_raw, 422)
        assert err["error"] == "invalid_need"

    assert plan_calls == []
    assert not resilience.breaker("catalog.plan_need").is_open()

    monkeypatch.setattr(service, "_catalog", lambda: cat)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", tmp_path / "missing_products.json")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    assert not cat.catalog_available()

    client_missing = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    for missing_cat_bad in (
        {**BASE_NEED, "preferences": {"fat_pct": 10**400}},
        {**BASE_NEED, "preferred_product_ids": [f"id-{i}" for i in range(250)]},
        {**BASE_NEED, "quantity": -1},
    ):
        r_m = client_missing.post("/v1/plan", headers=H, json=missing_cat_bad)
        _assert_4xx_json(r_m, 422)


def test_healthz_survives_corrupt_upstream_client_attributes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "_catalog", _make_stub_catalog)
    fc = FixtureClient()
    bc.set_client(fc)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    for bad_down in (float("inf"), float("-inf"), float("nan"), True, False, "bad", None, -50.0, time.time() + 1e20):
        setattr(fc, "down_until", bad_down)
        setattr(fc, "last_error", "upstream \ud800 failure")
        r = client.get("/healthz")
        assert r.status_code == 200, (bad_down, r.status_code, r.text)
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["upstream"]["retry_in_s"], int)
        assert 0 <= body["upstream"]["retry_in_s"] <= 86400

    setattr(fc, "last_error", _Unstringable())
    r2 = client.get("/healthz")
    assert r2.status_code == 200
    assert r2.json()["upstream"]["last_error"] == "_Unstringable"


def test_offers_sanitizes_non_latin1_and_control_provenance_headers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "_catalog", _make_stub_catalog)
    monkeypatch.setenv("AIMO_FAULTS", "price_shock:464306:1.1;note:€-äö-\U0001f512")
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    r = client.post("/v1/offers", headers=H, json={"product_ids": ["464306"], "requested_date": DELIVERY})
    assert r.status_code == 200, r.text
    assert "X-Aimo-Faults-Applied" in r.headers
    assert len(r.json()) == 1

    none_meta: Any = None
    assert gw_app._prov_headers(none_meta) == {
        "X-Aimo-Source": "None",
        "X-Aimo-Served-From": "None",
        "X-Aimo-Fetched-At": "None",
        "X-Aimo-Valid-For": "None",
    }
    crlf_hdrs = gw_app._prov_headers({"source": "live\r\nInjected: 1", "faults": "shock:€äö\x00"})
    assert "\r" not in crlf_hdrs["X-Aimo-Source"] and "\n" not in crlf_hdrs["X-Aimo-Source"]
    assert "\x00" not in crlf_hdrs["X-Aimo-Faults-Applied"]
    assert crlf_hdrs["X-Aimo-Faults-Applied"].isascii()

    for numeric_date in (0, 86400, True, False):
        r_bad = client.post("/v1/offers", headers=H, json={"product_ids": ["464306"], "requested_date": numeric_date})
        _assert_4xx_json(r_bad, 422)


def test_service_error_handler_sanitizes_surrogates_nan_and_bad_retry_after():
    app = create_app(with_mcp=False)

    @app.get("/_test/svc-dirty")
    def _svc_dirty():
        raise service.ServiceError(
            503,
            "upstream_unavailable",
            "upstream message with \ud800 surrogate",
            {"retry_after": float("inf"), "nan": float("nan"), "obj": _Unstringable()},
        )

    @app.get("/_test/svc-good-ra")
    def _svc_good_ra():
        raise service.ServiceError(503, "upstream_unavailable", "retry soon", {"retry_after": 12.7})

    @app.get("/_test/svc-bad-status")
    def _svc_bad_status():
        bad_status: Any = True
        raise service.ServiceError(bad_status, "weird_status", "boolean status")

    client = TestClient(app, raise_server_exceptions=False)
    r1 = client.get("/_test/svc-dirty")
    assert r1.status_code == 503
    assert "Retry-After" not in r1.headers
    assert r1.json()["detail"]["obj"] == "_Unstringable"

    r2 = client.get("/_test/svc-good-ra")
    assert r2.status_code == 503
    assert r2.headers.get("Retry-After") == "12"

    r3 = client.get("/_test/svc-bad-status")
    assert r3.status_code == 500
    assert r3.json()["error"] == "weird_status"


def test_route_control_char_and_approver_edge_cases(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "_catalog", _make_stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    pin_hdr = {"X-Approver-Pin": PIN}

    for ctrl in ("\x07", "\x1b", "\x7f"):
        assert client.get("/v1/products/search", headers=H, params={"q": f"milk{ctrl}"}).status_code == 422
        assert client.get("/v1/products/search", headers=H, params={"q": "milk", "claims": f"FREE{ctrl}"}).status_code == 422
        assert client.get("/v1/products/search", headers=H, params={"q": "milk", "category": f"104{ctrl}"}).status_code == 422
        assert client.get("/v1/products/search", headers=H, params={"q": "milk", "content_unit": f"L{ctrl}"}).status_code == 422
        assert client.get("/v1/audit", params={"subject_id": f"Q-1{ctrl}"}).status_code == 422
        assert client.get("/v1/approvals", headers=pin_hdr, params={"customer_id": f"hotel{ctrl}"}).status_code == 422
        assert client.post("/v1/demo/tamper/1", headers=pin_hdr, params={"summary": f"bad{ctrl}text"}).status_code == 422

    assert client.get("/v1/products/464%20306", headers=H).status_code == 404
    assert client.get("/v1/products/464%07306", headers=H).status_code == 404
    assert client.get("/v1/quotes/Q-1%07", headers=H).status_code == 404
    assert client.get("/v1/orders/O-1%07", headers=H).status_code == 404
    assert client.get("/v1/orders/O-1%07/peppol-order-response.xml", headers=H).status_code == 404
    assert client.get("/v1/audit/report/O-1%07").status_code == 422

    r_human_blank_cid = client.get("/v1/approvals", headers=pin_hdr, params={"customer_id": "   "})
    assert r_human_blank_cid.status_code == 200

    q = client.post(
        "/v1/quotes",
        headers=H,
        json={"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
    ).json()
    o = client.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()

    r_ws_approver = client.post(
        f"/v1/approvals/{o['order_id']}/decision",
        headers=pin_hdr,
        json={"decision": "approve", "approver": "   ", "quote_hash": q["quote_hash"]},
    )
    err_ws = _assert_4xx_json(r_ws_approver, 422)
    assert err_ws["error"] == "approver_required"

    r_ctrl_oid_agent = client.post(
        "/v1/approvals/ORD%00evil/decision",
        headers={"X-Agent-Id": "buyer-1"},
        json={"decision": "approve", "approver": ANNA, "quote_hash": q["quote_hash"]},
    )
    assert _assert_4xx_json(r_ctrl_oid_agent, 403)["error"] == "agent_cannot_approve"
    assert client.get("/v1/audit/verify").json()["ok"] is True


def test_customers_masking_and_mcp_allowed_hosts_resilience(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        service,
        "list_customers",
        lambda: [
            "not-a-dict",
            {
                "customer_id": "hotel-aurora",
                "name": "Hotel Aurora",
                "approvers": [
                    "malformed-entry",
                    {"id": ANNA, "name": "Anna", "role": "procurement_manager"},
                ],
            },
        ],
    )
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    r = client.get("/v1/customers")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["approvers"] == [{"name": "Anna", "role": "procurement_manager"}]

    for raw_hosts in ("   ,  , ", "localhost, 127.0.0.1, "):
        monkeypatch.setenv("AIMO_MCP_ALLOWED_HOSTS", raw_hosts)
        app_mcp = create_app(with_mcp=True)
        c_mcp = TestClient(app_mcp, raise_server_exceptions=False)
        assert c_mcp.get("/healthz").status_code == 200


def test_auth_status_and_error_codes_preserved_before_bad_input_checks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "_catalog", _make_stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    rogue = {"X-Customer-Id": "rogue-corp", "X-Agent-Id": "buyer-1"}
    school = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    pin_hdr = {"X-Approver-Pin": PIN}

    assert _assert_4xx_json(client.get("/v1/products/search", params={"q": "   "}), 401)["error"] == "identity_required"
    assert _assert_4xx_json(client.get("/v1/products/search", params={"q": "milk\x07"}), 401)["error"] == "identity_required"
    assert _assert_4xx_json(client.get("/v1/products/search", headers=rogue, params={"q": "   "}), 403)["error"] == "unknown_customer"
    assert _assert_4xx_json(
        client.get("/v1/products/search", headers=school, params={"q": "   ", "customer_id": "hotel-aurora"}), 403
    )["error"] == "customer_mismatch"

    assert _assert_4xx_json(client.get("/v1/products/464%07306", headers=rogue), 403)["error"] == "unknown_customer"

    bad_plan_anon = {**BASE_NEED, "customer_id": "", "product_query": "   ", "preferences": {"fat_pct": "nan"}}
    assert _assert_4xx_json(client.post("/v1/plan", json=bad_plan_anon), 401)["error"] == "identity_required"
    bad_plan_rogue = {**BASE_NEED, "customer_id": "rogue-corp", "raw_text": "bad\x07", "preferences": {"fat_pct": "nan"}}
    assert _assert_4xx_json(client.post("/v1/plan", json=bad_plan_rogue), 403)["error"] == "unknown_customer"
    bad_plan_mis = {**BASE_NEED, "customer_id": "hotel-aurora", "product_query": "   ", "preferences": {"fat_pct": 10**400}}
    assert _assert_4xx_json(client.post("/v1/plan", headers=school, json=bad_plan_mis), 403)["error"] == "customer_mismatch"

    assert _assert_4xx_json(client.get("/v1/approvals", params={"status": "bogus", "customer_id": "bad\x07"}), 401)["error"] == "identity_required"
    assert _assert_4xx_json(client.get("/v1/approvals", headers=rogue, params={"status": "bogus"}), 403)["error"] == "unknown_customer"
    assert _assert_4xx_json(
        client.get("/v1/approvals", headers=school, params={"status": "bogus", "customer_id": "hotel-aurora"}), 403
    )["error"] == "customer_mismatch"

    assert _assert_4xx_json(
        client.post("/v1/approvals/ORD%07/decision", json={"decision": "approve", "approver": "", "quote_hash": "0" * 64}), 401
    )["error"] == "approver_auth_required"
    assert _assert_4xx_json(
        client.post("/v1/demo/tamper/1", params={"summary": "bad\x07"}), 401
    )["error"] == "approver_auth_required"
    assert _assert_4xx_json(
        client.post("/v1/demo/tamper/1", headers={"X-Agent-Id": "buyer-1"}, params={"summary": "bad\x07"}), 403
    )["error"] == "agent_cannot_approve"
    assert _assert_4xx_json(
        client.post("/v1/auth/approver-session", headers={"X-Agent-Id": "buyer-1"}, json=["not-dict"]), 403
    )["error"] == "agent_cannot_approve"

    assert _assert_4xx_json(
        client.post("/v1/approvals/O-missing/decision", headers=pin_hdr, json={"decision": "approve", "approver": ANNA, "quote_hash": "0" * 64}),
        404,
    )["error"] == "order_not_found"

    q = client.post(
        "/v1/quotes",
        headers=H,
        json={"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
    ).json()
    o = client.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    r_conflict = client.post(
        f"/v1/approvals/{o['order_id']}/decision",
        headers=pin_hdr,
        json={"decision": "approve", "approver": ANNA, "quote_hash": "f" * 64},
    )
    assert r_conflict.status_code == 409

