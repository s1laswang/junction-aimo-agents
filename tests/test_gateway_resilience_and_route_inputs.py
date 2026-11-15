from __future__ import annotations

import json
import math
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

import aimo.catalog as cat
from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import human_auth, resilience, schemas, service
from aimo.gateway.app import create_app
from aimo.models import Need, Plan, PlanLine, Product, ProductSummary

from test_gateway import FixtureClient, H

DELIVERY_DAY = "2026-11-16"
PIN = "test-pin-123456"
BASE_NEED = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "quantity": 10,
    "unit": "L",
    "delivery_by": DELIVERY_DAY,
}


class _BrokenStr:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


def _stub_catalog(plan_calls: list[Need] | None = None) -> Any:
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
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
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
            reasons=["stubbed"],
            flags=[],
        )
        return Plan(
            need=need,
            lines=[ln],
            candidates=[],
            covered_amount=need.quantity,
            shortfall=0.0,
            total_net=ln.line_total,
            explanation="stub plan",
        )

    return SimpleNamespace(
        catalog_available=lambda: True,
        CATALOG_MISSING_MESSAGE="Catalog missing",
        search=lambda q, **kw: [summary],
        get_product=lambda pid: prod if str(pid) == "464306" else None,
        plan_need=_plan,
    )


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_APPROVER_PIN", PIN)
    for var in (
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_SEARCH_TIMEOUT_S",
        "AIMO_PLAN_TIMEOUT_S",
        "AIMO_SCREEN_TIMEOUT_S",
        "AIMO_BREAKER_RESET_S",
        "AIMO_ENV",
    ):
        monkeypatch.delenv(var, raising=False)
    human_auth.reset_for_tests()
    resilience._breakers.clear()
    bc.set_client(FixtureClient())
    yield
    human_auth.reset_for_tests()
    resilience._breakers.clear()
    bc.set_client(None)


@pytest.mark.parametrize(
    "name,env_key,default",
    [
        ("catalog.search", "AIMO_SEARCH_TIMEOUT_S", 4.0),
        ("catalog.plan_need", "AIMO_PLAN_TIMEOUT_S", 20.0),
        ("trust.screen_text", "AIMO_SCREEN_TIMEOUT_S", 3.0),
    ],
)
@pytest.mark.parametrize(
    "bad_raw",
    ["nan", "NaN", "inf", "-inf", "Infinity", "-5", "-0.01", "0", "0.0", "abc", "", "   ", "1e400"],
)
def test_timeout_for_rejects_bad_env_values(monkeypatch, name, env_key, default, bad_raw):
    monkeypatch.setenv(env_key, bad_raw)
    assert resilience.timeout_for(name, default) == default


def test_timeout_for_clamps_and_guards_invalid_defaults(monkeypatch):
    assert resilience.timeout_for("unknown.op", 7.5) == 7.5
    assert resilience.timeout_for("catalog.search", 4.0) == 4.0

    for bad_default in (0, -2.0, float("nan"), float("inf"), float("-inf"), True, False, "bad", None):
        val = resilience.timeout_for("catalog.search", bad_default)
        assert math.isfinite(val) and val == pytest.approx(5.0)
        val_unk = resilience.timeout_for(None, bad_default)
        assert math.isfinite(val_unk) and val_unk == pytest.approx(5.0)

    monkeypatch.setenv("AIMO_SEARCH_TIMEOUT_S", "0.001")
    assert resilience.timeout_for("catalog.search", 4.0) == pytest.approx(0.05)

    monkeypatch.setenv("AIMO_SEARCH_TIMEOUT_S", "9999")
    assert resilience.timeout_for("catalog.search", 4.0) == pytest.approx(300.0)

    monkeypatch.setenv("AIMO_SEARCH_TIMEOUT_S", "1.75")
    assert resilience.timeout_for("catalog.search", 4.0) == pytest.approx(1.75)


@pytest.mark.parametrize(
    "bad_reset",
    ["nan", "NaN", "inf", "-inf", "0", "0.0", "-0.0", "-5", "-0.1", "abc", "", "   ", "1e400"],
)
def test_breaker_reset_s_and_fail_max_hardening(monkeypatch, bad_reset):
    monkeypatch.setenv("AIMO_BREAKER_RESET_S", bad_reset)
    b = resilience.Breaker("test.op")
    assert b.reset_s == pytest.approx(60.0)
    assert b.fail_max == 2
    b.fail("err1")
    assert not b.is_open()
    b.fail("err2")
    st = b.state()
    assert st["open"] is True
    assert isinstance(st["retry_in_s"], int) and st["retry_in_s"] >= 0

    for direct_bad in (0, 0.0, -10.0, float("nan"), float("inf"), float("-inf"), True, False, "bad"):
        b_direct = resilience.Breaker("direct.op", reset_s=direct_bad)
        assert b_direct.reset_s == pytest.approx(60.0)

    for bad_fm in (0, -1, -99, True, False, float("nan"), float("inf"), 2.5, "bad", "", None):
        b_fm = resilience.Breaker("fm.op", fail_max=bad_fm)
        assert b_fm.fail_max == 2
        b_fm.fail("e1")
        assert not b_fm.is_open()
        b_fm.fail("e2")
        assert b_fm.is_open()

    b_three = resilience.Breaker("three.op", fail_max=3, reset_s=15.5)
    assert b_three.fail_max == 3 and b_three.reset_s == pytest.approx(15.5)
    b_three.fail("e1")
    b_three.fail("e2")
    assert not b_three.is_open()
    b_three.fail("e3")
    assert b_three.is_open()

    b_huge = resilience.Breaker("huge.op", reset_s=1e30)
    assert b_huge.reset_s == pytest.approx(86400.0)
    b_huge.open_until = float("inf")
    assert b_huge.state()["retry_in_s"] == 0
    b_huge.open_until = float("nan")
    assert b_huge.state()["retry_in_s"] == 0


def test_guarded_handles_bad_timeout_args_and_exempts_validation_errors():
    for bad_timeout in (0, 0.0, -1.5, float("nan"), float("inf"), float("-inf"), True, False, "bad", None, 1e50):
        res, path = resilience.guarded("test.bad_timeout", lambda: "ok", lambda: "fb", bad_timeout)
        assert res == "ok" and path == "primary"

    fallback_called: list[int] = []

    def bad_pydantic():
        return Need(customer_id="hotel-aurora", product_query="milk", quantity=0, unit="L", delivery_by=DELIVERY_DAY)

    for _ in range(4):
        with pytest.raises(ValidationError):
            resilience.guarded("test.validation", bad_pydantic, lambda: fallback_called.append(1), 2.0)

    def bad_value_error():
        raise ValueError("need.quantity must be greater than 0")

    for _ in range(4):
        with pytest.raises(ValueError, match="greater than 0"):
            resilience.guarded("test.validation", bad_value_error, lambda: fallback_called.append(1), 2.0)

    b = resilience.breaker("test.validation")
    assert not b.is_open()
    assert fallback_called == []
    assert b.stats["errors"] == 0
    assert b.stats["fallback"] == 0
    assert b.stats["opened"] == 0
    assert b.stats["primary"] == 8

    for _ in range(2):
        res_j, path_j = resilience.guarded(
            "test.json_err",
            lambda: json.loads("{bad"),
            lambda: "fallback-json",
            2.0,
        )
        assert res_j == "fallback-json" and path_j == "fallback:error"
    assert resilience.breaker("test.json_err").is_open()

    for _ in range(2):
        res_r, path_r = resilience.guarded(
            "test.runtime",
            lambda: (_ for _ in ()).throw(RuntimeError("gemini failure")),
            lambda: "fallback-ok",
            2.0,
        )
        assert res_r == "fallback-ok" and path_r == "fallback:error"
    assert resilience.breaker("test.runtime").is_open()


def test_gateway_routes_survive_corrupt_resilience_env_vars(monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    monkeypatch.setenv("AIMO_BREAKER_RESET_S", "0")
    monkeypatch.setenv("AIMO_SEARCH_TIMEOUT_S", "nan")
    monkeypatch.setenv("AIMO_PLAN_TIMEOUT_S", "-5")
    monkeypatch.setenv("AIMO_SCREEN_TIMEOUT_S", "inf")

    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    r_search = client.get("/v1/products/search", params={"q": "milk"}, headers=H)
    assert r_search.status_code == 200

    r_plan = client.post("/v1/plan", json=BASE_NEED, headers=H)
    assert r_plan.status_code == 200

    r_health = client.get("/healthz")
    assert r_health.status_code == 200
    assert r_health.json()["ok"] is True


@pytest.mark.parametrize(
    "bad_qty",
    [
        True,
        False,
        0,
        0.0,
        -0.0,
        -1,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        1_000_001,
        1e308,
        None,
        "",
        "abc",
        [],
        {},
    ],
)
def test_quote_line_in_rejects_bool_zero_negative_nan_inf_and_invalid_quantities(monkeypatch, bad_qty):
    with pytest.raises(ValidationError):
        schemas.QuoteLineIn(product_id="464306", qty_sales_units=bad_qty)

    if not (isinstance(bad_qty, float) and not math.isfinite(bad_qty)):
        monkeypatch.setattr(service, "_catalog", _stub_catalog)
        client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
        r = client.post(
            "/v1/quotes",
            headers=H,
            json={"delivery_date": DELIVERY_DAY, "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}]},
        )
        assert r.status_code == 422, (bad_qty, r.status_code, r.text)
        body = r.json()
        assert body["error"] in {"validation_error", "bad_quantity", "absurd_quantity"} and body["message"]


def test_quote_line_in_accepts_valid_numeric_quantities():
    assert schemas.QuoteLineIn(product_id="464306", qty_sales_units=1).qty_sales_units == 1.0
    assert schemas.QuoteLineIn(product_id="464306", qty_sales_units=2.5).qty_sales_units == 2.5
    assert schemas.QuoteLineIn(product_id="464306", qty_sales_units="10").qty_sales_units == 10.0


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "   ",
        "464\x00306",
        "464\x07306",
        "464\x1b306",
        "464\x7f306",
        "464\x85306",
        "464\x9f306",
        "464\ud800306",
        "464 306",
        "464\n306",
        "464\t306",
        "x" * 65,
    ],
)
def test_schemas_reject_control_chars_and_nul_bytes_in_ids(monkeypatch, bad_id):
    with pytest.raises(ValidationError):
        schemas.OffersRequest(product_ids=[bad_id], requested_date=date.fromisoformat(DELIVERY_DAY))

    with pytest.raises(ValidationError):
        schemas.QuoteLineIn(product_id=bad_id, qty_sales_units=1)

    with pytest.raises(ValidationError):
        schemas.OrderRequest(quote_id="x" * 101 if bad_id == "x" * 65 else bad_id, quote_hash="0" * 64)

    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    if "\ud800" not in bad_id:
        r_off = client.post("/v1/offers", headers=H, json={"product_ids": [bad_id], "requested_date": DELIVERY_DAY})
        assert r_off.status_code == 422, (bad_id, r_off.status_code, r_off.text)
        r_q = client.post(
            "/v1/quotes",
            headers=H,
            json={"delivery_date": DELIVERY_DAY, "lines": [{"product_id": bad_id, "qty_sales_units": 1}]},
        )
        assert r_q.status_code == 422, (bad_id, r_q.status_code, r_q.text)


@pytest.mark.parametrize("ctrl", ["\x00", "\x07", "\x1b", "\x7f", "\x85", "\n", "\r", "\t"])
def test_schemas_reject_control_chars_in_agent_hash_and_decision_fields(ctrl):
    with pytest.raises(ValidationError):
        schemas.QuoteRequest(
            agent_id=f"buyer{ctrl}1",
            delivery_date=date.fromisoformat(DELIVERY_DAY),
            lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=1)],
        )

    with pytest.raises(ValidationError):
        schemas.OrderRequest(quote_id="Q-123", quote_hash=f"hash{ctrl}val")

    with pytest.raises(ValidationError):
        schemas.DecisionRequest(decision="approve", approver=f"anna{ctrl}@aurora", quote_hash="0" * 64)

    with pytest.raises(ValidationError):
        schemas.DecisionRequest(decision="approve", approver="anna@aurora", approver_role=f"mgr{ctrl}", quote_hash="0" * 64)

    with pytest.raises(ValidationError):
        schemas.DecisionRequest(decision="approve", approver="anna@aurora", quote_hash=f"hash{ctrl}")


@pytest.mark.parametrize(
    "bad_qty",
    [True, False, 0, 0.0, -5, -0.001, float("nan"), float("inf"), float("-inf"), 1_000_001, None, "ten"],
)
def test_quote_request_validate_need_rejects_constructed_bad_quantities(bad_qty):
    constructed = Need.model_construct(
        customer_id="hotel-aurora",
        product_query="milk",
        quantity=bad_qty,
        unit="L",
        delivery_by=date.fromisoformat(DELIVERY_DAY),
        preferred_product_ids=[],
    )
    with pytest.raises(ValidationError):
        schemas.QuoteRequest(
            delivery_date=date.fromisoformat(DELIVERY_DAY),
            lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=2)],
            need=constructed,
        )


def test_quote_request_validate_need_rejects_non_string_or_control_fields():
    for bad_need in (
        Need.model_construct(
            customer_id="hotel-aurora",
            product_query=12345,
            quantity=10.0,
            unit="L",
            delivery_by=date.fromisoformat(DELIVERY_DAY),
            preferred_product_ids=[],
        ),
        Need.model_construct(
            customer_id="hotel-aurora",
            product_query="milk",
            raw_text=["bad"],
            quantity=10.0,
            unit="L",
            delivery_by=date.fromisoformat(DELIVERY_DAY),
            preferred_product_ids=[],
        ),
        Need.model_construct(
            customer_id="hotel-aurora",
            product_query="milk\x00bad",
            quantity=10.0,
            unit="L",
            delivery_by=date.fromisoformat(DELIVERY_DAY),
            preferred_product_ids=[],
        ),
        Need.model_construct(
            customer_id="hotel-aurora",
            product_query="milk",
            quantity=10.0,
            unit="L",
            delivery_by=date.fromisoformat(DELIVERY_DAY),
            preferred_product_ids=["464\x00306"],
        ),
    ):
        with pytest.raises(ValidationError):
            schemas.QuoteRequest(
                delivery_date=date.fromisoformat(DELIVERY_DAY),
                lines=[schemas.QuoteLineIn(product_id="464306", qty_sales_units=2)],
                need=bad_need,
            )


def test_offers_and_quote_customer_id_unconstrained_returns_403_unknown_customer(monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    for hostile_cid in ("rogue-corp", "../../etc/passwd", "x" * 5000, "hotel\x00aurora"):
        r_off = client.post(
            "/v1/offers",
            json={"product_ids": ["464306"], "requested_date": DELIVERY_DAY, "customer_id": hostile_cid},
        )
        assert r_off.status_code == 403, (hostile_cid[:20], r_off.status_code, r_off.text)
        assert r_off.json()["error"] == "unknown_customer"

        r_q = client.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "rogue-corp", "X-Agent-Id": "buyer-1"},
            json={
                "customer_id": "rogue-corp",
                "delivery_date": DELIVERY_DAY,
                "lines": [{"product_id": "464306", "qty_sales_units": 1}],
            },
        )
        assert r_q.status_code == 403
        assert r_q.json()["error"] == "unknown_customer"


def test_validation_error_body_handles_non_serializable_surrogates_and_huge_lists():
    hostile_errors = [
        {
            "loc": ("body", "need", True, _BrokenStr(), 10**30, "k" * 500, "surr\ud800loc"),
            "msg": "bad \ud800 value " * 80,
            "type": "value_error",
            "ctx": {
                "error": ValueError("custom \ud800 failure"),
                "broken": _BrokenStr(),
                "nan": float("nan"),
                "inf": float("inf"),
                "ninf": float("-inf"),
                "surrogate": "lone\udfffchar",
                "raw_bytes": b"\xff\xfe",
                "tags": {"a", "b"},
                "big_int": 10**40,
                "nested": {"a": {"b": {"c": {"d": {"e": {"f": ValueError("deep")}}}}}},
            },
        },
        ValueError("bare exception entry"),
        "string error entry",
        None,
        {"loc": 99, "msg": "", "ctx": {"error": RuntimeError("from ctx")}},
    ]
    body = schemas.validation_error_body(hostile_errors)
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    parsed = json.loads(encoded)
    assert parsed["error"] == "validation_error"
    assert parsed["message"].startswith("Invalid request.")
    assert len(parsed["detail"]) == 5

    empty_body = schemas.validation_error_body(None)
    assert empty_body == {"error": "validation_error", "message": "Invalid request.", "detail": []}

    huge_errors = [
        {"loc": ("body", "lines", i, "qty_sales_units"), "msg": "Input should be > 0", "type": "greater_than", "ctx": {"error": ValueError(i)}}
        for i in range(2500)
    ]
    huge_body = schemas.validation_error_body(huge_errors)
    huge_encoded = json.dumps(huge_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    huge_parsed = json.loads(huge_encoded)
    assert len(huge_parsed["detail"]) == 50
    assert "(+2495 more)" in huge_parsed["message"]
    assert len(huge_parsed["message"]) <= 1000

    gen_body = schemas.validation_error_body({"loc": ("body", i), "msg": "bad"} for i in range(5000))
    assert len(gen_body["detail"]) == 50
    assert json.dumps(gen_body, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_install_error_handlers_turns_decoding_and_validation_edges_into_clean_4xx():
    mini = FastAPI()
    schemas.install_error_handlers(mini)

    @mini.get("/boom-req")
    def _boom_req():
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "field"),
                    "msg": "Value error, broken \ud800",
                    "type": "value_error",
                    "ctx": {"error": ValueError("non-serializable \ud800"), "nan": float("nan"), "obj": _BrokenStr()},
                }
                for _ in range(300)
            ]
        )

    @mini.get("/boom-raw")
    def _boom_raw():
        raise RequestValidationError([ValueError("raw non-dict validation item")])

    @mini.get("/boom-json")
    def _boom_json():
        return json.loads("{unterminated")

    @mini.get("/boom-utf8")
    def _boom_utf8():
        return b"\xff\xfe".decode("utf-8")

    @mini.get("/boom-val")
    def _boom_val():
        raise ValueError("bad input \ud800 value")

    @mini.get("/boom-disconnect")
    def _boom_disconnect():
        raise ClientDisconnect()

    @mini.get("/boom-http")
    def _boom_http():
        raise StarletteHTTPException(
            status_code=400,
            detail={"error": "bad_input", "message": "surrogate \ud800 detail", "extra": float("nan")},
            headers={"X-Safe": "1", "X-Bad": " emoji-\U0001f512"},
        )

    c = TestClient(mini, raise_server_exceptions=False)
    for path, expected_status in (
        ("/boom-req", 422),
        ("/boom-raw", 422),
        ("/boom-json", 400),
        ("/boom-utf8", 400),
        ("/boom-val", 422),
        ("/boom-disconnect", 400),
        ("/boom-http", 400),
    ):
        r = c.get(path)
        assert r.status_code == expected_status, (path, r.status_code, r.text)
        data = r.json()
        assert isinstance(data.get("error"), str) and data["error"]
        assert isinstance(data.get("message"), str) and data["message"]


@pytest.mark.parametrize("blank_query", ["", "   ", "\t\n"])
def test_plan_blank_product_query_with_preferred_ids_returns_422_with_and_without_catalog(monkeypatch, tmp_path, blank_query):
    plan_calls: list[Need] = []
    monkeypatch.setattr(service, "_catalog", lambda: _stub_catalog(plan_calls))
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    payload = {
        **BASE_NEED,
        "product_query": blank_query,
        "preferred_product_ids": ["463038"],
    }
    r_with_cat = client.post("/v1/plan", headers=H, json=payload)
    assert r_with_cat.status_code == 422, r_with_cat.text
    assert r_with_cat.json()["error"] == "empty_need"
    assert plan_calls == []
    assert not resilience.breaker("catalog.plan_need").is_open()

    monkeypatch.setattr(service, "_catalog", lambda: cat)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", tmp_path / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    assert not cat.catalog_available()

    client_no_cat = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    r_no_cat = client_no_cat.post("/v1/plan", headers=H, json=payload)
    assert r_no_cat.status_code == 422, r_no_cat.text
    assert r_no_cat.json()["error"] == "empty_need"

    r_valid_no_cat = client_no_cat.post("/v1/plan", headers=H, json=BASE_NEED)
    assert r_valid_no_cat.status_code == 503
    assert r_valid_no_cat.json()["error"] == "catalog_missing"


@pytest.mark.parametrize("raw_payload", [b"null", b"[]", b'["anna@hotel-aurora.example"]', b'"str"', b"123", b"true"])
def test_approver_session_non_object_json_bodies_return_422(monkeypatch, raw_payload):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    for hdrs in ({"content-type": "application/json"}, {"content-type": "application/json", "X-Approver-Pin": PIN}):
        r = client.post("/v1/auth/approver-session", content=raw_payload, headers=hdrs)
        assert r.status_code == 422, (raw_payload, hdrs, r.status_code, r.text)
        body = r.json()
        assert body["error"] == "invalid_body" and body["message"]

    r_agent = client.post(
        "/v1/auth/approver-session",
        content=raw_payload,
        headers={"content-type": "application/json", "X-Agent-Id": "buyer-1"},
    )
    assert r_agent.status_code == 403
    assert r_agent.json()["error"] == "agent_cannot_approve"


@pytest.mark.parametrize("bad_approver", [123, True, ["anna"], {"id": "anna"}, "anna\x00@aurora", "anna\x1b@aurora"])
def test_approver_session_bad_approver_field_returns_422(monkeypatch, bad_approver):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    r = client.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": PIN},
        json={"approver": bad_approver},
    )
    assert r.status_code == 422, (bad_approver, r.status_code, r.text)
    assert r.json()["error"] == "invalid_approver"

    r_ok = client.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": PIN},
        json={"approver": "anna@hotel-aurora.example"},
    )
    assert r_ok.status_code == 200 and r_ok.json().get("session")


@pytest.mark.parametrize("bad_seq", [0, -1, -999])
def test_demo_tamper_and_untamper_reject_non_positive_and_huge_seq(monkeypatch, bad_seq):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": PIN}, raise_server_exceptions=False)

    for path in (f"/v1/demo/tamper/{bad_seq}", f"/v1/demo/untamper/{bad_seq}"):
        r = client.post(path)
        assert r.status_code == 404, (path, r.status_code, r.text)
        body = r.json()
        assert body["error"] == "audit_event_not_found"

    for huge_path in (f"/v1/demo/tamper/{10**30}", f"/v1/demo/untamper/{10**30}"):
        r_huge = client.post(huge_path)
        assert r_huge.status_code == 404
        assert r_huge.json()["error"] == "audit_event_not_found"

    r_nul_sum = client.post("/v1/demo/tamper/1", params={"summary": "bad\x00summary"})
    assert r_nul_sum.status_code == 422
    assert r_nul_sum.json()["error"] == "invalid_summary"

    monkeypatch.setenv("AIMO_ENV", "production")
    for path in (f"/v1/demo/tamper/{bad_seq}", f"/v1/demo/untamper/{bad_seq}"):
        assert client.post(path).status_code == 404


@pytest.mark.parametrize(
    "valid_status",
    [None, "", "pending", "pending_approval", "approved", "placed", "rejected", "expired", "failed", "all"],
)
def test_approvals_accepts_all_valid_statuses(monkeypatch, valid_status):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    params = {} if valid_status is None else {"status": valid_status}

    r_human = client.get("/v1/approvals", params=params, headers={"X-Approver-Pin": PIN})
    assert r_human.status_code == 200, (valid_status, r_human.status_code, r_human.text)
    assert isinstance(r_human.json(), list)

    r_agent = client.get("/v1/approvals", params=params, headers=H)
    assert r_agent.status_code == 200, (valid_status, r_agent.status_code, r_agent.text)
    assert isinstance(r_agent.json(), list)


@pytest.mark.parametrize("bad_status", ["bogus", "PENDING", "   ", "-1", "open", "x" * 300])
def test_approvals_rejects_invalid_status_with_422(monkeypatch, bad_status):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)

    r_anon = client.get("/v1/approvals", params={"status": bad_status})
    assert r_anon.status_code == 401
    assert r_anon.json()["error"] == "identity_required"

    r_human = client.get("/v1/approvals", params={"status": bad_status}, headers={"X-Approver-Pin": PIN})
    assert r_human.status_code == 422, (bad_status, r_human.status_code, r_human.text)
    assert r_human.json()["error"] == "bad_status"

    r_agent = client.get("/v1/approvals", params={"status": bad_status}, headers=H)
    assert r_agent.status_code == 422, (bad_status, r_agent.status_code, r_agent.text)
    assert r_agent.json()["error"] == "bad_status"


def test_v1_routes_reject_nul_surrogate_and_malformed_inputs_with_4xx_json(monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    client = TestClient(create_app(with_mcp=False), headers=H, raise_server_exceptions=False)

    for params in (
        {"q": "milk\x00"},
        {"q": "milk", "claims": "FREE\x00"},
        {"q": "milk", "category": "104\x00"},
        {"q": "milk", "content_unit": "L\x00"},
        {"q": "milk", "limit": "true"},
        {"q": "milk", "limit": "0"},
    ):
        r_s = client.get("/v1/products/search", params=params)
        assert 400 <= r_s.status_code < 500, (params, r_s.status_code, r_s.text)
        assert r_s.json().get("error") and r_s.json().get("message")

    for bad_plan in (
        {**BASE_NEED, "quantity": True},
        {**BASE_NEED, "quantity": False},
        {**BASE_NEED, "quantity": 0},
        {**BASE_NEED, "quantity": -10},
        {**BASE_NEED, "product_query": "milk\x00"},
        {**BASE_NEED, "raw_text": "hello\x00world"},
        {**BASE_NEED, "preferred_product_ids": ["464\x00306"]},
        {**BASE_NEED, "preferred_product_ids": ["x" * 65]},
    ):
        r_p = client.post("/v1/plan", json=bad_plan)
        assert r_p.status_code == 422, (bad_plan, r_p.status_code, r_p.text)
        assert r_p.json().get("error") and r_p.json().get("message")

    for path in (
        "/v1/quotes/%00",
        "/v1/quotes/" + "x" * 300,
        "/v1/orders/%00",
        "/v1/orders/" + "x" * 300,
        "/v1/orders/%00/peppol-order-response.xml",
        "/v1/audit/report/%00",
        "/v1/audit/report/" + "x" * 300,
    ):
        r_get = client.get(path)
        assert 400 <= r_get.status_code < 500, (path, r_get.status_code, r_get.text)
        assert r_get.json().get("error") and r_get.json().get("message")

    r_aud = client.get("/v1/audit", params={"subject_id": "Q-\x00"})
    assert r_aud.status_code == 422 and r_aud.json().get("error") == "invalid_subject_id"

    surrogate_raw = b'{"customer_id":"hotel-aurora","product_query":"\\ud800","quantity":"\\ud800","unit":"L","delivery_by":"2026-11-16"}'
    for post_path in ("/v1/plan", "/v1/offers", "/v1/quotes", "/v1/orders", "/v1/auth/approver-session"):
        for raw in (b"{bad-json", b"\xff\xfe", surrogate_raw):
            r_raw = client.post(post_path, content=raw, headers={**H, "content-type": "application/json"})
            assert 400 <= r_raw.status_code < 500, (post_path, raw[:20], r_raw.status_code, r_raw.text)
            data = r_raw.json()
            assert isinstance(data.get("error"), str) and isinstance(data.get("message"), str)
