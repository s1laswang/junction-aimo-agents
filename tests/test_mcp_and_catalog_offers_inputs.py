from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import threading
import time
import tokenize
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError
import pytest

from aimo import catalog as cat_mod
from aimo import config
from aimo.backend import catalog_store, client as bc, clock, orders as store
from aimo.catalog import offers as offers_mod, textsearch
from aimo.catalog.offers import (
    BATCH_SIZE,
    MAX_ID_LEN,
    MAX_SCAN_ITEMS,
    TERMINAL_UNKNOWN,
    _avail_date,
    _before,
    _cache_path,
    _clean_ids,
    _clean_short_str,
    _dt,
    _has_bad_char,
    _nonneg_finite,
    _pos_finite,
    _post,
    _require_date,
    _sanitize_offer,
    build_offer,
    fallback_get_offers,
    get_offers,
)
from aimo.gateway import fallback_catalog, human_auth, mcp_server, resilience, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import (
    _call,
    _check_pos_qty,
    _clean_iso_date,
    _clean_json_obj,
    _dump,
    _err_json,
    _guard_create_quote,
    _guard_get_offers,
    _guard_get_product_id,
    _guard_need,
    _guard_order_id,
    _guard_place_order,
    _guard_search_products,
    _guard_subject_id,
    _has_bad_pref_val,
    _has_bad_text_char,
    _header_or_env,
    _headers,
    _identity,
    _require_catalog,
    _sanitize_header_str,
    build_mcp,
)
from aimo.gateway.schemas import QuoteLineIn
from aimo.models import Need, Offer
from test_fuzz import _synth_products
from test_gateway import FixtureClient, H

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = "2026-11-16"
DELIVERY_DATE = date(2026, 11, 16)

NEED_SEED: dict[str, Any] = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose free milk",
    "required_claims": ["FREE_FROM_LACTOSE"],
    "quantity": 10,
    "unit": "L",
    "delivery_by": DELIVERY,
}


def _reset_breakers() -> None:
    for b in list(resilience._breakers.values()):
        b.reset()


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("mcp-offers-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, synth_catalog_dir):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(offers_mod, "CACHE_DIR", tmp_path / "offer_cache")
    for var in (
        "AIMO_FAULTS",
        "AIMO_NOW",
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
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(FixtureClient())
    yield
    store.close_all()
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(None)
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _extract_mcp(res: Any) -> tuple[bool, Any, str]:
    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    text = " ".join(getattr(x, "text", "") for x in (getattr(res, "content", None) or []))
    if not text and sc is not None:
        text = json.dumps(sc)
    payload = sc
    if payload is None and text:
        cand = text[text.index("{"):] if "{" in text else text
        try:
            payload = json.loads(cand)
        except ValueError:
            payload = None
    return is_err, payload, text


async def _inproc_mcp_batch(srv: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, bool, Any, str]]:
    out: list[tuple[str, bool, Any, str]] = []
    async with Client(srv) as client:
        for name, args in calls:
            try:
                res = await client.call_tool(name, args)
                is_err, payload, txt = _extract_mcp(res)
            except Exception as exc:
                is_err, payload, txt = True, None, f"{type(exc).__name__}: {exc}"
            _reset_breakers()
            out.append((name, is_err, payload, txt))
    return out


def _valid_price_payload(unit_price: Any = 1.25, comparison_price: Any = 1.25) -> dict[str, Any]:
    return {
        "productId": "464306",
        "comparisonUnit": "L",
        "catalogPrice": {
            "value": unit_price,
            "valueWithTax": 1.425 if isinstance(unit_price, (int, float)) and not isinstance(unit_price, bool) else None,
            "currency": "EUR",
            "validTo": "2026-11-20T23:59:59Z",
        },
        "listPrice": {"value": 1.35},
        "comparisonPrice": {"value": comparison_price},
        "tax": {"taxRate": 14.0},
    }


def _valid_avail_payload(amount: Any = 50.0, avail_date: Any = "2026-11-16", model: str = "Stock") -> dict[str, Any]:
    return {
        "productId": "464306",
        "logisticModel": model,
        "salesBlock": False,
        "closingTime": "2026-11-15T15:00:00+0200",
        "scheduleLines": [
            {
                "salesUnit": "ST",
                "availableAmount": amount,
                "availableDate": avail_date,
                "salesBlock": False,
            }
        ],
    }


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    self_src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert "\u2014" not in self_src
    assert "\u2013" not in self_src
    comment_tokens = [
        tok for tok in tokenize.generate_tokens(io.StringIO(self_src).readline) if tok.type == tokenize.COMMENT
    ]
    assert comment_tokens == []
    for rel in ("aimo/gateway/mcp_server.py", "aimo/catalog/offers.py"):
        owned = (ROOT / rel).read_text(encoding="utf-8")
        assert "\u2014" not in owned


def test_mcp_catalog_missing_returns_structured_tool_error_and_exact_ordering(monkeypatch) -> None:
    monkeypatch.setattr(cat_mod, "catalog_available", lambda: False)
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = build_mcp()

    calls: list[tuple[str, dict[str, Any]]] = [
        ("search_products", {"query": "lactose free milk"}),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose free milk",
                    "quantity": 10,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose free milk",
                    "quantity": 0,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose free milk",
                    "quantity": -5,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose free milk",
                    "quantity": 2_000_000,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "rogue-corp",
                    "product_query": "lactose free milk",
                    "quantity": 10,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel\x00aurora",
                    "product_query": "lactose free milk",
                    "quantity": 10,
                    "unit": "L",
                    "delivery_by": DELIVERY,
                }
            },
        ),
        ("search_products", {"query": "   "}),
        ("search_products", {"query": "milk", "limit": 0}),
        ("search_products", {"query": "milk", "claims": [""]}),
    ]
    results = asyncio.run(_inproc_mcp_batch(srv, calls))

    _, err_search, payload_search, txt_search = results[0]
    assert err_search and "catalog_missing" in txt_search and "UnexpectedToolError" not in txt_search
    assert payload_search["error"] == "catalog_missing"
    assert payload_search["detail"]["fix"] == "uv run aimo fetch-data"
    assert payload_search["detail"]["needs"] == "AIMO_API_KEY"

    _, err_plan, payload_plan, txt_plan = results[1]
    assert err_plan and "catalog_missing" in txt_plan and "UnexpectedToolError" not in txt_plan
    assert payload_plan["error"] == "catalog_missing"

    _, err_zero_qty, _, txt_zero_qty = results[2]
    assert err_zero_qty and ("greater_than" in txt_zero_qty or "bad_quantity" in txt_zero_qty)
    assert "catalog_missing" not in txt_zero_qty

    _, err_neg_qty, _, txt_neg_qty = results[3]
    assert err_neg_qty and ("greater_than" in txt_neg_qty or "bad_quantity" in txt_neg_qty)
    assert "catalog_missing" not in txt_neg_qty

    _, err_absurd_qty, _, txt_absurd_qty = results[4]
    assert err_absurd_qty and "absurd_quantity" in txt_absurd_qty and "catalog_missing" not in txt_absurd_qty

    _, err_bad_cust, _, txt_bad_cust = results[5]
    assert err_bad_cust and "customer_mismatch" in txt_bad_cust and "catalog_missing" not in txt_bad_cust

    _, err_unsafe_cust, _, txt_unsafe_cust = results[6]
    assert err_unsafe_cust and "unknown_customer" in txt_unsafe_cust and "catalog_missing" not in txt_unsafe_cust

    _, err_blank_q, _, txt_blank_q = results[7]
    assert err_blank_q and "empty_query" in txt_blank_q and "catalog_missing" not in txt_blank_q

    _, err_bad_lim, _, txt_bad_lim = results[8]
    assert err_bad_lim and "bad_limit" in txt_bad_lim and "catalog_missing" not in txt_bad_lim

    _, err_bad_claims, _, txt_bad_claims = results[9]
    assert err_bad_claims and "bad_claims" in txt_bad_claims and "catalog_missing" not in txt_bad_claims

    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    unauth_res = asyncio.run(
        _inproc_mcp_batch(
            srv,
            [
                (
                    "plan_need",
                    {
                        "need": {
                            "customer_id": "rogue-corp",
                            "product_query": "lactose free milk",
                            "quantity": 10,
                            "unit": "L",
                            "delivery_by": DELIVERY,
                        }
                    },
                )
            ],
        )
    )
    _, err_unknown, _, txt_unknown = unauth_res[0]
    assert err_unknown and "unknown_customer" in txt_unknown and "catalog_missing" not in txt_unknown

    monkeypatch.setenv("AIMO_CUSTOMER_ID", "rogue-corp")
    bad_env_res = asyncio.run(_inproc_mcp_batch(srv, [("search_products", {"query": "lactose free milk"})]))
    assert bad_env_res[0][1] and "unknown_customer" in bad_env_res[0][3]
    assert "catalog_missing" not in bad_env_res[0][3]


def test_mcp_require_catalog_monkeypatch_bypass_and_edge_branches(monkeypatch) -> None:
    monkeypatch.setattr(cat_mod, "catalog_available", lambda: False)
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")

    monkeypatch.setattr(service, "search_products", lambda query, claims=None, limit=10: {"products": [{"id": "X1"}]})
    monkeypatch.setattr(service, "plan_need", lambda need, identity=None: {"lines": [], "covered_amount": 0.0})
    srv = build_mcp()
    res = asyncio.run(
        _inproc_mcp_batch(
            srv,
            [
                ("search_products", {"query": "milk"}),
                ("plan_need", {"need": dict(NEED_SEED)}),
            ],
        )
    )
    assert not res[0][1] and res[0][2]["products"] == [{"id": "X1"}]
    assert not res[1][1] and res[1][2]["covered_amount"] == 0.0

    monkeypatch.setattr(service, "_catalog", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    _require_catalog()
    monkeypatch.setattr(service, "_catalog", lambda: None)
    _require_catalog()
    monkeypatch.setattr(service, "_catalog", lambda: SimpleNamespace())
    _require_catalog()
    monkeypatch.setattr(service, "_catalog", lambda: SimpleNamespace(catalog_available=lambda: False))
    with pytest.raises(service.ServiceError) as exc_info:
        _require_catalog()
    assert exc_info.value.status == 503 and exc_info.value.code == "catalog_missing"


def test_mcp_json_header_identity_and_call_helpers_hardening(monkeypatch) -> None:
    dirty = {
        "bad\ud800key": ["ok", "surr\udfff", math.nan, math.inf, -math.inf],
        "nested": ("tuple_item", {"k": "val\x00ue"}),
    }
    cleaned = _clean_json_obj(dirty)
    encoded = _err_json(dirty)
    parsed = json.loads(encoded)
    assert json.dumps(parsed, allow_nan=False)
    assert len(cleaned["nested"]) == 2

    assert _sanitize_header_str(None, max_len=64) is None
    assert _sanitize_header_str(b"hotel\xffaurora\x00x", max_len=64) == "hotel\ufffdaurora\x7fx"
    assert _sanitize_header_str(["a", b"b", 3, True], max_len=32) == "a, b, 3, True"

    class _Unstringable:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    assert _sanitize_header_str(_Unstringable(), max_len=32) is None

    class _RaisingHeadersCtx:
        @property
        def headers(self):
            raise RuntimeError("broken headers")

    class _RaisingItemsHeaders:
        def items(self):
            raise RuntimeError("broken items")

    assert _headers(None) == {}
    assert _headers(_RaisingHeadersCtx()) == {}
    assert _headers(SimpleNamespace(headers=_RaisingItemsHeaders())) == {}

    many_headers = {f"  X-Hdr-{i} ": f"val\x00{i}" for i in range(250)}
    many_headers["   "] = "ignored"
    h_parsed = _headers(SimpleNamespace(headers=many_headers))
    assert len(h_parsed) <= 200
    assert h_parsed["x-hdr-0"] == "val\x7f0"

    for bad_agent in ("../evil", "buyer\\1", "buyer\x001", "a" * 200):
        ctx_bad = SimpleNamespace(headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": bad_agent})
        with pytest.raises(ToolError) as te:
            _identity(ctx_bad)
        body = json.loads(str(te.value))
        assert body["error"] == "invalid_agent_id"

    ctx_strict_no_agent = SimpleNamespace(headers={"X-Customer-Id": "hotel-aurora"})
    with pytest.raises(ToolError) as te_strict:
        _identity(ctx_strict_no_agent, strict=True)
    assert json.loads(str(te_strict.value))["error"] == "identity_required"

    class _ModelCheck(BaseModel):
        qty: float = Field(..., gt=0)

    def _make_val_err() -> ValidationError:
        try:
            _ModelCheck(qty=0)
        except ValidationError as exc:
            return exc
        raise AssertionError("unreachable")

    ctx_fault = SimpleNamespace(
        headers={
            "X-Customer-Id": "hotel-aurora",
            "X-Agent-Id": "buyer-1",
            "X-Aimo-Faults": "stockout:463038",
            "X-Aimo-Now": "2026-11-15T09:30:00Z",
        }
    )
    before_now = clock.now()

    for err_to_raise, expected_code in [
        (service.ServiceError(422, "bad_quantity", "must be > 0", {"n": math.nan}), "bad_quantity"),
        (_make_val_err(), "validation_error"),
        (FileNotFoundError("data/missing.json"), "catalog_missing"),
        (bc.UpstreamError("rate limit", 429, retry_after=3.0), "upstream_rate_limited"),
        (bc.UpstreamError("timed out", 504), "upstream_timeout"),
        (bc.UpstreamError("corrupt", "not-an-int"), "upstream_unavailable"),
        (ValueError("bad raw input"), "invalid_request"),
    ]:
        with pytest.raises(ToolError) as call_err:
            _call(ctx_fault, lambda e=err_to_raise: (_ for _ in ()).throw(e))
        err_payload = json.loads(str(call_err.value))
        assert err_payload["error"] == expected_code
        assert json.dumps(err_payload, allow_nan=False)

    assert not bc.current_faults().stockout
    assert abs((clock.now() - before_now).total_seconds()) < 5.0


def test_mcp_guard_functions_exhaustive_bad_inputs() -> None:
    ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    setattr(ident, "authenticated", {"customer": True, "agent": True})

    assert _clean_iso_date(date(2026, 11, 16), code="bad_date") == "2026-11-16"
    for bad_dt in (datetime(2026, 11, 16, 12, 0), 20261116, None, "2026-11-16\x00", "2026/11/16", "2026-02-29"):
        with pytest.raises(service.ServiceError) as exc:
            _clean_iso_date(bad_dt, code="bad_date")
        assert exc.value.code == "bad_date"

    for bad_q in (0, 0.0, -0.0, -1, math.nan, math.inf, -math.inf, True, False, "5", None, 10**400):
        with pytest.raises(service.ServiceError) as exc_q:
            _check_pos_qty(bad_q, "quantity", pid="464306")
        assert exc_q.value.code == "bad_quantity"

    with pytest.raises(service.ServiceError) as exc_abs:
        _check_pos_qty(1_000_001, "quantity", pid="464306")
    assert exc_abs.value.code == "absurd_quantity"

    for q, cl, lim, code in [
        (123, None, 10, "empty_query"),
        ("milk\x00", None, 10, "bad_query"),
        ("milk\ud800", None, 10, "bad_query"),
        ("x" * 501, None, 10, "query_too_long"),
        ("   ", None, 10, "empty_query"),
        ("milk", None, True, "bad_limit"),
        ("milk", None, 0, "bad_limit"),
        ("milk", None, 101, "bad_limit"),
        ("milk", "ORGANIC", 10, "bad_claims"),
        ("milk", ["c"] * 51, 10, "bad_claims"),
        ("milk", ["   "], 10, "bad_claims"),
        ("milk", ["BAD\x00"], 10, "bad_claims"),
    ]:
        with pytest.raises(service.ServiceError) as exc_s:
            _guard_search_products(q, cl, lim)
        assert exc_s.value.code == code

    assert _guard_get_product_id("  464306 ") == "464306"
    for bad_pid, code in [
        (None, "bad_product_id"),
        ("464\x00306", "bad_product_id"),
        ("   ", "product_not_found"),
        ("464 306", "product_not_found"),
        ("9" * 65, "product_not_found"),
    ]:
        with pytest.raises(service.ServiceError) as exc_p:
            _guard_get_product_id(bad_pid)
        assert exc_p.value.code == code

    ids_ok, dt_ok = _guard_get_offers([" 464306 ", "", "463038"], "2026-11-16")
    assert ids_ok == ["464306", "463038"] and dt_ok == "2026-11-16"
    for bad_pids, bad_d, code in [
        ("464306", DELIVERY, "no_products"),
        (["x"] * 1001, DELIVERY, "too_many_products"),
        ([123], DELIVERY, "bad_product_id"),
        (["464\x00306"], DELIVERY, "bad_product_id"),
        (["464 306"], DELIVERY, "bad_product_id"),
        (["   "], DELIVERY, "no_products"),
        (["464306"], "bad", "bad_date"),
    ]:
        with pytest.raises(service.ServiceError) as exc_go:
            _guard_get_offers(bad_pids, bad_d)
        assert exc_go.value.code == code

    constructed_bad_needs = [
        Need.model_construct(
            raw_text="",
            customer_id="  hotel-aurora  ",
            product_query="milk",
            required_claims=[],
            excluded_allergens=[],
            quantity=0.0,
            unit="L",
            delivery_by=DELIVERY_DATE,
            preferred_product_ids=[],
            preferences={},
        ),
        Need.model_construct(
            raw_text="",
            customer_id="hotel-aurora",
            product_query="milk",
            required_claims=[],
            excluded_allergens=[],
            quantity=10.0,
            unit="ML",
            delivery_by=DELIVERY_DATE,
            preferred_product_ids=[],
            preferences={},
        ),
        Need.model_construct(
            raw_text="",
            customer_id="hotel-aurora",
            product_query="milk",
            required_claims=[],
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=datetime(2026, 11, 16, 8, 0),
            preferred_product_ids=[],
            preferences={},
        ),
        {**NEED_SEED, "raw_text": "bad\x00text"},
        {**NEED_SEED, "raw_text": "r" * 4001},
        {**NEED_SEED, "preferred_product_ids": ["p"] * 201},
        {**NEED_SEED, "preferred_product_ids": ["bad id"]},
        {**NEED_SEED, "preferred_product_ids": ["bad\x00id"]},
        {**NEED_SEED, "required_claims": ["c"] * 51},
        {**NEED_SEED, "required_claims": ["bad\x00claim"]},
        {**NEED_SEED, "excluded_allergens": ["a"] * 51},
        {**NEED_SEED, "excluded_allergens": ["   "]},
        {**NEED_SEED, "preferences": {"score": math.nan}},
        {**NEED_SEED, "preferences": {"d": {"d": {"d": {"d": {"d": {"d": 1}}}}}}},
        {**NEED_SEED, "preferences": {f"k{i}": i for i in range(51)}},
        {**NEED_SEED, "product_query": "   ", "preferred_product_ids": []},
    ]
    for bn in constructed_bad_needs:
        with pytest.raises(service.ServiceError):
            _guard_need(bn, ident, check_customer_auth=True)

    trimmed_need = Need.model_construct(
        raw_text="",
        customer_id="  hotel-aurora  ",
        product_query="milk",
        required_claims=[],
        excluded_allergens=[],
        quantity=5.0,
        unit="L",
        delivery_by=DELIVERY_DATE,
        preferred_product_ids=[],
        preferences={},
    )
    unauth_ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    setattr(unauth_ident, "authenticated", {"customer": False, "agent": False})
    cleaned_n = _guard_need(trimmed_need, unauth_ident, check_customer_auth=True)
    assert cleaned_n.customer_id == "hotel-aurora"

    with pytest.raises(service.ServiceError) as exc_cum:
        _guard_create_quote(
            DELIVERY,
            [
                QuoteLineIn(product_id="464306", qty_sales_units=600_000),
                QuoteLineIn(product_id="464306", qty_sales_units=600_000),
            ],
            None,
            ident,
        )
    assert exc_cum.value.code == "absurd_quantity"

    with pytest.raises(service.ServiceError) as exc_many_lines:
        _guard_create_quote(
            DELIVERY,
            [{"product_id": "464306", "qty_sales_units": 1} for _ in range(201)],
            None,
            ident,
        )
    assert exc_many_lines.value.code == "too_many_lines"

    with pytest.raises(service.ServiceError) as exc_bad_ln:
        _guard_create_quote(DELIVERY, ["not-a-line"], None, ident)
    assert exc_bad_ln.value.code == "bad_line"

    for qid, qh, code in [
        (None, "a" * 64, "bad_quote_id"),
        ("Q\x001", "a" * 64, "bad_quote_id"),
        ("Q 1", "a" * 64, "bad_quote_id"),
        ("Q1", None, "bad_quote_hash"),
        ("Q1", "h\x001", "bad_quote_hash"),
        ("Q1", "   ", "bad_quote_hash"),
    ]:
        with pytest.raises(service.ServiceError) as exc_po:
            _guard_place_order(qid, qh)
        assert exc_po.value.code == code

    for bad_oid, code in [
        (123, "bad_order_id"),
        ("ORD\x001", "bad_order_id"),
        ("", "order_not_found"),
        ("../ORD-1", "order_not_found"),
        ("ORD/1", "order_not_found"),
        ("ORD\\1", "order_not_found"),
    ]:
        with pytest.raises(service.ServiceError) as exc_oid:
            _guard_order_id(bad_oid)
        assert exc_oid.value.code == code

    for bad_sid in (None, 123, "Q\x001", "s" * 4097):
        with pytest.raises(service.ServiceError) as exc_sid:
            _guard_subject_id(bad_sid)
        assert exc_sid.value.code == "bad_subject_id"


def test_catalog_offers_clean_ids_and_scalar_validators(monkeypatch) -> None:
    assert _has_bad_char("464306") is False
    assert _has_bad_char("464\x00306") is True
    assert _has_bad_char("464\x7f306") is True
    assert _has_bad_char("464\x85306") is True
    assert _has_bad_char("464\ud800306") is True

    assert _require_date(DELIVERY_DATE) == DELIVERY_DATE
    with pytest.raises(ValueError, match="requested_date must be a date"):
        _require_date(datetime(2026, 11, 16, 12, 0))

    assert _clean_ids([" 464306 ", "", None, 123, "463038", "464306\r\n"]) == ["464306", "463038"]

    for bad_id in ("464 306", "464\t306", "464\x00306", "464\ud800306", "9" * (MAX_ID_LEN + 1)):
        with pytest.raises(ValueError, match="invalid product id"):
            _clean_ids([bad_id])

    def _huge_gen():
        for i in range(MAX_SCAN_ITEMS + 50):
            yield None

    with pytest.raises(ValueError, match="exceeds maximum scan size"):
        _clean_ids(_huge_gen())

    monkeypatch.setattr(offers_mod, "MAX_OFFER_IDS", 4)
    with pytest.raises(ValueError, match="too many unique product_ids"):
        _clean_ids(["P1", "P2", "P3", "P4", "P5"])

    assert _pos_finite(2.5) == 2.5
    for v in (0, 0.0, -0.0, -1, True, False, math.nan, math.inf, -math.inf, 10**400, "2.5", None):
        assert _pos_finite(v) is None

    z = _nonneg_finite(-0.0)
    assert z == 0.0 and math.copysign(1.0, z) == 1.0
    assert _nonneg_finite(0) == 0.0
    assert _nonneg_finite(14) == 14.0
    for v in (-0.01, True, False, math.nan, math.inf, -math.inf, 10**400, "0", None):
        assert _nonneg_finite(v) is None


def test_catalog_offers_sanitize_offer_and_get_offers_batching(monkeypatch) -> None:
    import aimo.backend as backend_mod

    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    assert _sanitize_offer(None, "464306", DELIVERY_DATE) is None
    assert _sanitize_offer("bad", "464306", DELIVERY_DATE) is None
    assert _sanitize_offer({"fetched_at": "not-a-datetime"}, "464306", DELIVERY_DATE) is None

    corrupt_constructed = Offer.model_construct(
        product_id="464306",
        requested_date=DELIVERY_DATE,
        available=True,
        available_amount=0.0,
        available_date="not-a-valid-date",
        cutoff_passed=False,
        sales_block=False,
        unit_price=1.50,
        unit_price_gross=-2.0,
        list_price=math.nan,
        comparison_price=math.inf,
        tax_rate=-14.0,
        fetched_at=now,
        issues=[None, 123, "bad\x00issue", "custom_issue", "custom_issue"],
    )
    san = _sanitize_offer(corrupt_constructed, "464306", DELIVERY_DATE)
    assert san is not None
    assert san.available is False
    assert san.available_date is None
    assert san.available_amount == 0.0
    assert san.unit_price == 1.50
    assert san.unit_price_gross is None
    assert san.list_price is None
    assert san.comparison_price is None
    assert san.tax_rate is None
    assert "out_of_stock" in san.issues
    assert "availability_error" in san.issues
    assert "custom_issue" in san.issues
    assert san.issues.count("custom_issue") == 1

    late_constructed = Offer.model_construct(
        product_id="464306",
        requested_date=DELIVERY_DATE,
        available=True,
        available_amount=10.0,
        available_date="2026-11-20T08:00:00Z",
        cutoff_passed=False,
        sales_block=False,
        unit_price=1.50,
        fetched_at=now,
        issues="corrupt-non-list",
    )
    san_late = _sanitize_offer(late_constructed, "464306", DELIVERY_DATE)
    assert san_late is not None
    assert san_late.available_date == date(2026, 11, 20)
    assert san_late.available is False

    for bad_cid in (123, True, "c" * 129, "hotel\x00aurora", "hotel\ud800aurora"):
        with pytest.raises(ValueError, match="invalid customer_id"):
            get_offers(["464306"], DELIVERY_DATE, customer_id=bad_cid)

    chunks_seen: list[int] = []

    def _two_arg_backend(ids: list[str], req_date: date):
        chunks_seen.append(len(ids))
        if len(chunks_seen) == 2:
            return ["corrupt-non-mapping"]
        out: dict[Any, Any] = {
            "bad key": {"unit_price": 1.0, "fetched_at": now, "available": True},
            "bad\x00key": {"unit_price": 1.0, "fetched_at": now, "available": True},
            123: {"unit_price": 1.0, "fetched_at": now, "available": True},
        }
        for pid in ids[:2]:
            out[pid] = {
                "product_id": "   ",
                "requested_date": "not-a-date",
                "available": True,
                "available_amount": 10.0,
                "unit_price": 2.25,
                "fetched_at": now,
                "issues": [],
            }
        return out

    monkeypatch.setattr(backend_mod, "get_offers", _two_arg_backend)
    many_ids = [f"P{i:05d}" for i in range(BATCH_SIZE * 2 + 25)]
    batched_out = get_offers(many_ids, DELIVERY_DATE, customer_id="hotel-aurora")
    assert chunks_seen == [BATCH_SIZE, BATCH_SIZE, 25]
    assert len(batched_out) == 4
    assert batched_out["P00000"].unit_price == 2.25

    monkeypatch.setattr(
        backend_mod,
        "get_offers",
        lambda *_a, **_kw: (_ for _ in ()).throw(NotImplementedError("not ready")),
    )
    monkeypatch.setattr(offers_mod, "fallback_get_offers", lambda ids, d: {"464306": san})
    fb_res = get_offers(["464306"], DELIVERY_DATE)
    assert "464306" in fb_res


def test_catalog_offers_dt_avail_date_before_and_build_offer_edge_cases() -> None:
    for raw_dt in (
        None,
        123,
        "",
        "   ",
        "2026-11-15T12:00:00\x00Z",
        "x" * 70,
        "not-iso",
        "2026-13-40T12:00:00Z",
        "2026-11-15T25:00:00Z",
    ):
        assert _dt(raw_dt) is None

    z_dt = _dt("2026-11-15T12:00:00z")
    assert z_dt is not None and z_dt.tzinfo is not None
    off_dt = _dt("2026-11-15T15:00:00+0200")
    assert off_dt is not None and off_dt.utcoffset() == timedelta(hours=2)

    assert _avail_date(None) == (True, None)
    assert _avail_date("") == (True, None)
    assert _avail_date("   \t\n ") == (True, None)
    assert _avail_date(DELIVERY_DATE) == (True, DELIVERY_DATE)
    assert _avail_date("2026-11-16T06:00:00Z") == (True, DELIVERY_DATE)
    for bad_ad in (
        datetime(2026, 11, 16, 6, 0),
        123,
        "2026-11-16extra",
        "2026/11/16",
        "2026-02-30",
        "2026-11-16\x00",
        "d" * 70,
    ):
        assert _avail_date(bad_ad) == (False, None)

    naive_a = datetime(2026, 11, 15, 10, 0)
    aware_b = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    assert _before(naive_a, aware_b) is True
    assert _before(aware_b, naive_a) is False

    for bad_pid in ("464 306", "464\x00306", "9" * 65):
        with pytest.raises(ValueError):
            build_offer(bad_pid, DELIVERY_DATE, None, None, None)

    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    term_offer = build_offer(
        " 464306 ",
        DELIVERY_DATE,
        {
            **_valid_price_payload(3.50),
            "comparisonUnit": "L\x00BAD",
            "catalogPrice": {"value": 3.50, "currency": "EUR\x00"},
        },
        {
            **_valid_avail_payload(amount=TERMINAL_UNKNOWN, model="Terminal"),
            "closingTime": "2026-11-15T18:00:00Z",
        },
        now,
    )
    assert term_offer.product_id == "464306"
    assert term_offer.available is True
    assert term_offer.available_amount is None
    assert term_offer.currency == "EUR"
    assert term_offer.comparison_unit is None

    no_sched = build_offer(
        "464306",
        DELIVERY_DATE,
        _valid_price_payload(2.0),
        {"logisticModel": "Stock", "scheduleLines": ["not-a-dict"]},
        now,
    )
    assert no_sched.available is False
    assert "no_schedule" in no_sched.issues

    blocked_late = build_offer(
        "464306",
        DELIVERY_DATE,
        {"error": "upstream_price_err"},
        {
            "logisticModel": "Stock\x00",
            "closingTime": "2026-11-15T08:00:00Z",
            "scheduleLines": [
                {
                    "salesUnit": "ST\x00",
                    "availableAmount": 10.0,
                    "availableDate": "2026-11-19",
                    "salesBlock": True,
                }
            ],
        },
        now,
    )
    assert blocked_late.available is False
    assert blocked_late.logistic_model is None
    assert blocked_late.sales_unit is None
    assert {"price_error", "cutoff_passed", "sales_block", "late"} <= set(blocked_late.issues)


def test_catalog_offers_cache_security_corruption_offline_and_concurrency(tmp_path, monkeypatch) -> None:
    import httpx

    cache_dir = tmp_path / "offers_cache"
    monkeypatch.setattr(offers_mod, "CACHE_DIR", cache_dir)

    for bad_kind in ("", "../evil", "price/../../etc", "price\x00", 123, "k" * 33):
        with pytest.raises(ValueError, match="invalid offer cache kind"):
            _cache_path(bad_kind, ["464306"], DELIVERY_DATE)

    p = _cache_path("price", ["464306"], DELIVERY_DATE)
    assert p.parent == cache_dir

    monkeypatch.setattr(offers_mod, "OFFLINE", True)
    with pytest.raises(RuntimeError, match="AIMO_OFFLINE=1"):
        _post("price", ["464306"], DELIVERY_DATE)

    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="AIMO_OFFLINE=1"):
        _post("price", ["464306"], DELIVERY_DATE)

    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="AIMO_OFFLINE=1"):
        _post("price", ["464306"], DELIVERY_DATE)

    p.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RuntimeError, match="AIMO_OFFLINE=1"):
        _post("price", ["464306"], DELIVERY_DATE)

    monkeypatch.setattr(offers_mod, "OFFLINE", False)

    class _FakeResp:
        def __init__(self, data: Any):
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._data

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResp(["not-an-object"]))
    with pytest.raises(ValueError, match="JSON object"):
        _post("price", ["464306"], DELIVERY_DATE)

    call_lock = threading.Lock()
    http_calls = 0

    def _fake_httpx_post(url: str, json: dict[str, Any], headers: dict[str, Any], timeout: int):
        nonlocal http_calls
        with call_lock:
            http_calls += 1
        pids = json["products"]
        if url.endswith("/api/price"):
            return _FakeResp({"products": [{**_valid_price_payload(1.75), "productId": pid} for pid in pids]})
        return _FakeResp(
            {
                "timeNow": "2026-11-15T09:00:00Z",
                "products": [{**_valid_avail_payload(40.0), "productId": pid} for pid in pids],
            }
        )

    monkeypatch.setattr(httpx, "post", _fake_httpx_post)
    monkeypatch.setattr(offers_mod, "OFFER_TTL_S", 0)
    healed = fallback_get_offers(["464306"], DELIVERY_DATE)
    assert healed["464306"].available is True

    workers = 12
    barrier = threading.Barrier(workers)

    def _offer_worker(idx: int) -> dict[str, Offer]:
        barrier.wait(timeout=10)
        out: dict[str, Offer] = {}
        for step in range(8):
            pids = ["464306", f"4630{idx % 4}{step % 3}"]
            res = fallback_get_offers(pids, DELIVERY_DATE)
            assert set(res) == set(pids)
            assert res["464306"].available is True
            assert res["464306"].unit_price == pytest.approx(1.75)
            out.update(res)
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        worker_results = [f.result(timeout=30) for f in [pool.submit(_offer_worker, i) for i in range(workers)]]

    assert len(worker_results) == workers
    assert http_calls > 0
    leftover_tmps = list(cache_dir.glob("*.tmp"))
    assert leftover_tmps == []
    for cached_file in cache_dir.glob("*.json"):
        loaded = json.loads(cached_file.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

    monkeypatch.setattr(offers_mod, "OFFER_TTL_S", 300)
    before_cached_calls = http_calls
    cached_hit = fallback_get_offers(["464306", "463000"], DELIVERY_DATE)
    assert cached_hit["464306"].unit_price == pytest.approx(1.75)
    assert http_calls == before_cached_calls


def test_mcp_http_concurrent_mixed_valid_and_bad_inputs_never_500() -> None:
    app = create_app(with_mcp=True)
    with TestClient(app, raise_server_exceptions=False) as tc:
        workers = 8
        barrier = threading.Barrier(workers)

        def _mcp_worker(idx: int) -> list[int]:
            base_hdrs = {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                **H,
            }
            barrier.wait(timeout=10)
            r_init = tc.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": f"worker-{idx}", "version": "0.1.0"},
                    },
                },
                headers=base_hdrs,
            )
            assert r_init.status_code == 200, r_init.text[:200]
            sid = r_init.headers.get("mcp-session-id")
            hdrs = {**base_hdrs, **({"mcp-session-id": sid} if sid else {})}
            tc.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdrs)

            codes: list[int] = []
            ops = [
                ("search_products", {"query": "milk", "limit": 3}, False),
                ("search_products", {"query": "   "}, True),
                ("plan_need", {"need": {**NEED_SEED, "quantity": 0}}, True),
                ("plan_need", {"need": {**NEED_SEED, "quantity": -2}}, True),
                ("plan_need", {"need": {**NEED_SEED, "quantity": 2_000_000}}, True),
                ("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY}, False),
                ("get_offers", {"product_ids": [], "requested_date": DELIVERY}, True),
                ("get_order_status", {"order_id": "../bad"}, True),
            ]
            for step, (tool_name, args, expect_err) in enumerate(ops, start=2):
                r = tc.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": step, "method": "tools/call", "params": {"name": tool_name, "arguments": args}},
                    headers=hdrs,
                )
                assert r.status_code == 200, (tool_name, r.status_code, r.text[:200])
                ctype = r.headers.get("content-type", "")
                if ctype.startswith("text/event-stream"):
                    data_lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
                    msg = json.loads(data_lines[0])
                else:
                    msg = r.json()
                res_obj = msg.get("result") or {}
                is_err = bool("error" in msg or res_obj.get("isError") or res_obj.get("is_error"))
                txt = json.dumps(msg)
                assert "Traceback (most recent call last)" not in txt
                assert "ZeroDivision" not in txt
                assert is_err is expect_err, (tool_name, args, txt[:200])
                codes.append(r.status_code)
            return codes

        with ThreadPoolExecutor(max_workers=workers) as pool:
            all_codes = [f.result(timeout=45) for f in [pool.submit(_mcp_worker, i) for i in range(workers)]]

        flat = [c for sub in all_codes for c in sub]
        assert len(flat) == workers * 8
        assert all(c == 200 for c in flat)


@pytest.mark.parametrize(
    ("raw_qty", "expected_code"),
    [
        (0, "bad_quantity"),
        (0.0, "bad_quantity"),
        (-0.0, "bad_quantity"),
        (-1, "bad_quantity"),
        (-1e-9, "bad_quantity"),
        (-1e100, "bad_quantity"),
        (math.nan, "bad_quantity"),
        (math.inf, "bad_quantity"),
        (-math.inf, "bad_quantity"),
        (True, "bad_quantity"),
        (False, "bad_quantity"),
        ("10", "bad_quantity"),
        (None, "bad_quantity"),
        ([], "bad_quantity"),
        ({}, "bad_quantity"),
        (10**400, "bad_quantity"),
        (1_000_000.01, "absurd_quantity"),
        (2_000_000, "absurd_quantity"),
        (1e12, "absurd_quantity"),
    ],
)
def test_check_pos_qty_parametrized_rejections(raw_qty: Any, expected_code: str) -> None:
    with pytest.raises(service.ServiceError) as exc_info:
        _check_pos_qty(raw_qty, "quantity")
    assert exc_info.value.status == 422
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize("good_qty", [1e-6, 0.25, 1, 10.5, 999_999.9, 1_000_000, 1_000_000.0])
def test_check_pos_qty_parametrized_valid_boundaries(good_qty: Any) -> None:
    res = _check_pos_qty(good_qty, "quantity", pid="464306")
    assert isinstance(res, float)
    assert res == float(good_qty)


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        (date(2026, 11, 16), "2026-11-16"),
        ("2026-11-16", "2026-11-16"),
        (" 2026-11-16 ", "2026-11-16"),
        ("2024-02-29", "2024-02-29"),
    ],
)
def test_clean_iso_date_parametrized_valid(raw_date: Any, expected: str) -> None:
    assert _clean_iso_date(raw_date, code="bad_date") == expected


@pytest.mark.parametrize(
    "bad_date",
    [
        None,
        123,
        True,
        False,
        datetime(2026, 11, 16, 12, 0),
        "",
        "   ",
        "2026/11/16",
        "2026-2-5",
        "2026-02-29",
        "2026-13-01",
        "2026-00-10",
        "2026-11-31",
        "2026-11-16T00:00:00Z",
        "2026-11-16\n",
        "2026-11-16\t",
        "2026-11-16\x00",
        "2026-11-16\x7f",
    ],
)
def test_clean_iso_date_parametrized_rejections(bad_date: Any) -> None:
    with pytest.raises(service.ServiceError) as exc_info:
        _clean_iso_date(bad_date, code="bad_date")
    assert exc_info.value.status == 422
    assert exc_info.value.code == "bad_date"


@pytest.mark.parametrize(
    ("query", "claims", "limit", "expected_code"),
    [
        (None, None, 10, "empty_query"),
        (42, None, 10, "empty_query"),
        (["milk"], None, 10, "empty_query"),
        ("", None, 10, "empty_query"),
        ("   \t\r\n ", None, 10, "empty_query"),
        ("milk\x00", None, 10, "bad_query"),
        ("milk\x07", None, 10, "bad_query"),
        ("milk\x7f", None, 10, "bad_query"),
        ("milk\x85", None, 10, "bad_query"),
        ("milk\ud800", None, 10, "bad_query"),
        ("m" * 501, None, 10, "query_too_long"),
        ("milk", None, True, "bad_limit"),
        ("milk", None, False, "bad_limit"),
        ("milk", None, 0, "bad_limit"),
        ("milk", None, -5, "bad_limit"),
        ("milk", None, 101, "bad_limit"),
        ("milk", None, 5.5, "bad_limit"),
        ("milk", None, "10", "bad_limit"),
        ("milk", "ORGANIC", 10, "bad_claims"),
        ("milk", {}, 10, "bad_claims"),
        ("milk", ["ORGANIC"] * 51, 10, "bad_claims"),
        ("milk", [123], 10, "bad_claims"),
        ("milk", [""], 10, "bad_claims"),
        ("milk", ["   "], 10, "bad_claims"),
        ("milk", ["c" * 101], 10, "bad_claims"),
        ("milk", ["ORGANIC\x00"], 10, "bad_claims"),
        ("milk", ["ORGANIC\n"], 10, "bad_claims"),
    ],
)
def test_guard_search_products_parametrized_rejections(
    query: Any,
    claims: Any,
    limit: Any,
    expected_code: str,
) -> None:
    with pytest.raises(service.ServiceError) as exc_info:
        _guard_search_products(query, claims, limit)
    assert exc_info.value.status == 422
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("query", "claims", "limit"),
    [
        ("m", None, 1),
        ("m" * 500, [], 100),
        ("milk\nwith\ttabs", ["FREE_FROM_LACTOSE"] * 50, 50),
    ],
)
def test_guard_search_products_parametrized_valid_boundaries(query: str, claims: Any, limit: int) -> None:
    _guard_search_products(query, claims, limit)


@pytest.mark.parametrize(
    ("bad_need", "check_auth", "expected_status", "expected_code"),
    [
        (None, True, 422, "invalid_need"),
        ("milk", True, 422, "invalid_need"),
        (["milk"], True, 422, "invalid_need"),
        ({**NEED_SEED, "customer_id": 123}, True, 422, "invalid_need"),
        ({**NEED_SEED, "customer_id": "rogue-corp"}, True, 403, "customer_mismatch"),
        ({**NEED_SEED, "customer_id": "hotel\x00aurora"}, True, 403, "unknown_customer"),
        ({**NEED_SEED, "customer_id": "hotel\x00aurora"}, False, 422, "invalid_need"),
        ({**NEED_SEED, "customer_id": "c" * 129}, False, 422, "invalid_need"),
        ({k: v for k, v in NEED_SEED.items() if k != "quantity"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "quantity": 0}, True, 422, "bad_quantity"),
        ({**NEED_SEED, "quantity": -10}, True, 422, "bad_quantity"),
        ({**NEED_SEED, "quantity": math.nan}, True, 422, "bad_quantity"),
        ({**NEED_SEED, "quantity": math.inf}, True, 422, "bad_quantity"),
        ({**NEED_SEED, "quantity": True}, True, 422, "bad_quantity"),
        ({**NEED_SEED, "quantity": 1_000_001}, True, 422, "absurd_quantity"),
        ({**NEED_SEED, "unit": "ML"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "unit": None}, True, 422, "invalid_need"),
        ({k: v for k, v in NEED_SEED.items() if k != "delivery_by"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "delivery_by": "2026-02-29"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "product_query": None}, True, 422, "invalid_need"),
        ({**NEED_SEED, "product_query": "milk\x00"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "product_query": "m" * 501}, True, 422, "query_too_long"),
        ({**NEED_SEED, "product_query": "   ", "preferred_product_ids": []}, True, 422, "empty_need"),
        ({**NEED_SEED, "raw_text": "bad\x00raw"}, True, 422, "invalid_need"),
        ({**NEED_SEED, "raw_text": "r" * 4001}, True, 422, "invalid_need"),
        ({**NEED_SEED, "preferred_product_ids": ["464 306"]}, True, 422, "bad_product_id"),
        ({**NEED_SEED, "preferred_product_ids": ["9" * 65]}, True, 422, "bad_product_id"),
        ({**NEED_SEED, "required_claims": ["   "]}, True, 422, "bad_claims"),
        ({**NEED_SEED, "excluded_allergens": ["a" * 65]}, True, 422, "invalid_need"),
        ({**NEED_SEED, "preferences": {"bad\x00key": 1}}, True, 422, "invalid_need"),
        ({**NEED_SEED, "preferences": {"v": "x" * 1001}}, True, 422, "invalid_need"),
        ({**NEED_SEED, "preferences": {"arr": list(range(201))}}, True, 422, "invalid_need"),
    ],
)
def test_guard_need_parametrized_rejections(
    bad_need: Any,
    check_auth: bool,
    expected_status: int,
    expected_code: str,
) -> None:
    ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    setattr(ident, "authenticated", {"customer": True, "agent": True})
    with pytest.raises(service.ServiceError) as exc_info:
        _guard_need(bad_need, ident, check_customer_auth=check_auth)
    assert exc_info.value.status == expected_status
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("delivery_date", "lines", "need", "expected_code"),
    [
        ("bad-date", [{"product_id": "464306", "qty_sales_units": 1}], None, "bad_date"),
        (DELIVERY, None, None, "no_lines"),
        (DELIVERY, [], None, "no_lines"),
        (DELIVERY, "not-a-list", None, "no_lines"),
        (DELIVERY, [{"product_id": None, "qty_sales_units": 1}], None, "bad_product_id"),
        (DELIVERY, [{"product_id": "464\x00306", "qty_sales_units": 1}], None, "bad_product_id"),
        (DELIVERY, [{"product_id": "   ", "qty_sales_units": 1}], None, "bad_product_id"),
        (DELIVERY, [{"product_id": "464 306", "qty_sales_units": 1}], None, "bad_product_id"),
        (DELIVERY, [{"product_id": "9" * 65, "qty_sales_units": 1}], None, "bad_product_id"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 0}], None, "bad_quantity"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": -3}], None, "bad_quantity"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": math.nan}], None, "bad_quantity"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1_000_001}], None, "absurd_quantity"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "tier": "gold"}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "tier": 123}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "flags": None}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "flags": "flag"}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "flags": ["f"] * 51}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "flags": [123]}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "flags": ["bad\x00"]}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "product_name": 123}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "product_name": "n" * 301}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1, "product_name": "bad\x00"}], None, "bad_line"),
        (DELIVERY, [{"product_id": "464306", "qty_sales_units": 1}], {**NEED_SEED, "quantity": 0}, "bad_quantity"),
    ],
)
def test_guard_create_quote_parametrized_rejections(
    delivery_date: Any,
    lines: Any,
    need: Any,
    expected_code: str,
) -> None:
    ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    with pytest.raises(service.ServiceError) as exc_info:
        _guard_create_quote(delivery_date, lines, need, ident)
    assert exc_info.value.status == 422
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    "bad_iterable",
    [None, 123, 3.14, True, "464306", b"464306", bytearray(b"464306"), {"464306": 1}],
)
def test_catalog_offers_clean_ids_rejects_non_list_or_mapping_types(bad_iterable: Any) -> None:
    with pytest.raises(ValueError, match="iterable"):
        _clean_ids(bad_iterable)


@pytest.mark.parametrize(
    ("raw_str", "max_len", "expected"),
    [
        ("  EUR \r\n", 16, "EUR"),
        ("Stock", 32, "Stock"),
        (None, 32, None),
        (123, 32, None),
        ("   ", 32, None),
        ("x" * 33, 32, None),
        ("EUR\x00", 16, None),
        ("EUR\ud800", 16, None),
    ],
)
def test_catalog_offers_clean_short_str_parametrized(raw_str: Any, max_len: int, expected: str | None) -> None:
    assert _clean_short_str(raw_str, max_len) == expected


def test_catalog_offers_cache_order_independence_write_oserror_and_upstream_failure(tmp_path, monkeypatch) -> None:
    import httpx

    cache_dir = tmp_path / "cache_edge"
    monkeypatch.setattr(offers_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(offers_mod, "OFFLINE", False)

    p1 = _cache_path("price", ["464306", "463038", "464306"], DELIVERY_DATE)
    p2 = _cache_path("price", ["463038", "464306"], DELIVERY_DATE)
    assert p1 == p2

    class _FakeOkResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"products": [_valid_price_payload(2.10)]}

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeOkResp())
    monkeypatch.setattr(offers_mod.os, "replace", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full")))
    res_data = _post("price", ["464306"], DELIVERY_DATE)
    assert res_data["products"][0]["catalogPrice"]["value"] == 2.10
    assert list(cache_dir.glob("*.tmp")) == []

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("mock down")),
    )
    degraded = fallback_get_offers(["464306"], DELIVERY_DATE)
    assert degraded == {}


def test_mcp_helpers_dump_pref_val_env_and_stdio_main(monkeypatch) -> None:
    class _Item(BaseModel):
        code: str
        d: date

    dumped = _dump([_Item(code="A", d=DELIVERY_DATE), {"raw": 1}])
    assert dumped == [{"code": "A", "d": "2026-11-16"}, {"raw": 1}]

    assert _has_bad_text_char("clean\ntext\twith\rcr") is False
    assert _has_bad_text_char("bad\x00char") is True
    assert _has_bad_pref_val({"ok": [1, "two", 3.5]}) is False
    assert _has_bad_pref_val({123: "non-str-key"}) is True

    monkeypatch.setattr(mcp_server.os, "environ", {"AIMO_CUSTOMER_ID": "hotel\x00aurora"})
    sanitized_env = _header_or_env({}, "x-customer-id", "AIMO_CUSTOMER_ID")
    assert sanitized_env == "hotel\x7faurora"
    assert _header_or_env({"x-customer-id": "from-hdr"}, "x-customer-id", "AIMO_CUSTOMER_ID") == "from-hdr"

    ran_transports: list[str] = []

    class _FakeMcp:
        def run(self, transport: str) -> None:
            ran_transports.append(transport)

    monkeypatch.setattr(mcp_server, "build_mcp", lambda: _FakeMcp())
    mcp_server.main()
    assert ran_transports == ["stdio"]


def test_mcp_all_13_tools_recovery_after_interleaved_bad_inputs(monkeypatch) -> None:
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = build_mcp()

    seq: list[tuple[str, dict[str, Any], bool]] = [
        ("list_policies", {}, False),
        ("list_recipes", {}, False),
        ("search_products", {"query": "   "}, True),
        ("search_products", {"query": "milk", "limit": 3}, False),
        ("get_product", {"product_id": "bad id"}, True),
        ("get_product", {"product_id": "464306"}, False),
        ("get_offers", {"product_ids": [], "requested_date": DELIVERY}, True),
        ("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY}, False),
        ("plan_need", {"need": {**NEED_SEED, "quantity": 0}}, True),
        ("plan_need", {"need": {**NEED_SEED, "quantity": -10}}, True),
        ("plan_need", {"need": dict(NEED_SEED)}, False),
        ("create_quote", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 0}]}, True),
        ("create_quote", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 2}]}, False),
        ("place_order", {"quote_id": "   ", "quote_hash": "0" * 64}, True),
        ("get_order_status", {"order_id": "../evil"}, True),
        ("get_audit_trail", {"subject_id": "s" * 5000}, True),
        ("plan_recipe", {"recipe_id": "unknown-dish-xyz", "portions": 10, "delivery_by": DELIVERY}, True),
        ("check_standing_order_drift", {"customer_id": "rogue-corp"}, True),
        ("propose_offer", {"customer_id": "rogue-corp", "product_id": "464306", "qty_sales_units": 5}, True),
    ]

    results = asyncio.run(_inproc_mcp_batch(srv, [(n, a) for n, a, _ in seq]))
    assert len(results) == len(seq)
    quote_obj: dict[str, Any] | None = None
    for (tool_name, _args, expect_err), (_got_name, is_err, payload, txt) in zip(seq, results):
        assert is_err is expect_err, (tool_name, is_err, expect_err, txt[:200])
        assert "Traceback (most recent call last)" not in txt
        assert "ZeroDivision" not in txt
        if tool_name == "create_quote" and not expect_err:
            quote_obj = payload

    assert quote_obj is not None and "quote_id" in quote_obj and "quote_hash" in quote_obj
    follow = asyncio.run(
        _inproc_mcp_batch(
            srv,
            [
                ("place_order", {"quote_id": quote_obj["quote_id"], "quote_hash": quote_obj["quote_hash"]}),
            ],
        )
    )
    assert follow[0][1] is False
    order_id = follow[0][2]["order_id"]
    tail = asyncio.run(
        _inproc_mcp_batch(
            srv,
            [
                ("get_order_status", {"order_id": order_id}),
                ("get_audit_trail", {"subject_id": order_id}),
            ],
        )
    )
    assert tail[0][1] is False and tail[0][2]["order_id"] == order_id
    assert tail[1][1] is False and tail[1][2]["verify"]["ok"] is True

