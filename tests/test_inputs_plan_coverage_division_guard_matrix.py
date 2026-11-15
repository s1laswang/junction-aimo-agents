from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import tokenize
from typing import Any

from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
import pytest

from aimo import catalog as cat_mod
from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store
from aimo.buyer.fakes import FakeGateway
from aimo.catalog import offers as offers_mod, plan as plan_mod, substitution as sub_mod, textsearch, units as units_mod
from aimo.catalog.plan import _late_split, _round_lot, fill, plan_need as catalog_plan_need
from aimo.catalog.substitution import price_per_unit, rank_substitutes, validate_need
from aimo.gateway import fallback_catalog, human_auth, mcp_server, resilience, service
from aimo.gateway.app import create_app
from aimo.models import Candidate, Need, Offer, Plan, ProductSummary
from test_fuzz import _synth_products
from test_gateway import FixtureClient

DELIVERY_ISO = "2026-11-16"
DELIVERY_DATE = date(2026, 11, 16)
HEL = timezone(timedelta(hours=2))
HEADERS = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}

BAD_QUANTITIES_CORE: list[tuple[str, Any]] = [
    ("zero_int", 0),
    ("neg_zero_float", -0.0),
    ("neg_one", -1),
    ("neg_tiny_1e12", -1e-12),
    ("neg_huge_1e9", -1e9),
    ("nan", float("nan")),
    ("pos_inf", float("inf")),
    ("neg_inf", float("-inf")),
    ("float_1e309", 1e309),
    ("int_overflow_10e309", 10**309),
    ("bool_true", True),
    ("bool_false", False),
    ("none", None),
    ("empty_str", ""),
    ("str_zero", "0"),
    ("str_neg_five", "-5"),
    ("empty_list", []),
    ("empty_dict", {}),
]

BAD_QUANTITIES_ALL: list[tuple[str, Any]] = [
    *BAD_QUANTITIES_CORE,
    ("over_max_1000000_01", 1_000_000.01),
]

DEGENERATE_ROW_VALUES: list[tuple[str, Any]] = [
    ("zero_int", 0),
    ("neg_zero_float", -0.0),
    ("neg_one", -1),
    ("neg_frac", -2.5),
    ("none", None),
    ("nan", float("nan")),
    ("pos_inf", float("inf")),
    ("neg_inf", float("-inf")),
    ("bool_false", False),
    ("bool_true", True),
    ("str_zero", "0"),
]


class CallSpy:
    def __init__(self, return_value: Any = None, side_effect: Any = None) -> None:
        self.call_count = 0
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._return_value = return_value
        self._side_effect = side_effect

    def cache_clear(self) -> None:
        return None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        self.calls.append((args, kwargs))
        if self._side_effect is not None:
            if isinstance(self._side_effect, BaseException) or (
                isinstance(self._side_effect, type) and issubclass(self._side_effect, BaseException)
            ):
                raise self._side_effect
            return self._side_effect(*args, **kwargs)
        return self._return_value


def _reset_breakers() -> None:
    for b in list(resilience._breakers.values()):
        b.reset()


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("coverage-guard-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(scope="module")
def http_client():
    app = create_app(with_mcp=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


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
    monkeypatch.setenv("AIMO_OFFLINE", "1")
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
        textsearch._con_cache_clear()
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
        textsearch._con_cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _force_missing_catalog(monkeypatch, tmp_path: Path) -> CallSpy:
    missing = tmp_path / "absent_catalog_dir"
    monkeypatch.setattr(cat_mod, "catalog_available", lambda: False)
    monkeypatch.setattr(config, "PRODUCT_JSON", missing / "products.json")
    monkeypatch.setattr(config, "CATALOG_DB", missing / "catalog.sqlite")
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", missing / "products.json")
    monkeypatch.setattr(catalog_store, "CATALOG_DB", missing / "catalog.sqlite")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    con_spy = CallSpy(side_effect=FileNotFoundError("catalog db missing"))
    monkeypatch.setattr(textsearch, "_con", con_spy)
    monkeypatch.setattr(plan_mod, "_con", con_spy)
    return con_spy


def _constructed_need(qty: Any, customer_id: str = "hotel-aurora") -> Need:
    return Need.model_construct(
        raw_text="need lactose free milk",
        customer_id=customer_id,
        product_query="lactose free milk",
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        quantity=qty,
        unit="L",
        delivery_by=DELIVERY_DATE,
        preferred_product_ids=[],
        preferences={},
    )


def _need_dict(qty: Any, customer_id: str = "hotel-aurora") -> dict[str, Any]:
    return {
        "raw_text": "need lactose free milk",
        "customer_id": customer_id,
        "product_query": "lactose free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": qty,
        "unit": "L",
        "delivery_by": DELIVERY_ISO,
        "preferred_product_ids": [],
        "preferences": {},
    }


def _raw_plan_json_bytes(label: str, qty: Any) -> bytes:
    base = _need_dict(10)
    prefix = json.dumps(base)[:-1]
    if label == "nan":
        q_tok = "NaN"
    elif label in ("pos_inf", "float_1e309"):
        q_tok = "1e309" if label == "float_1e309" else "Infinity"
    elif label == "neg_inf":
        q_tok = "-Infinity"
    elif label == "int_overflow_10e309":
        q_tok = "1" + ("0" * 309)
    elif label == "neg_zero_float":
        q_tok = "-0.0"
    else:
        return json.dumps({**base, "quantity": qty}).encode("utf-8")
    stripped = json.dumps({k: v for k, v in base.items() if k != "quantity"})[:-1]
    _ = prefix
    return f'{stripped},"quantity":{q_tok}}}'.encode("utf-8")


def _sample_offer(
    pid: str = "464306",
    *,
    available: bool = True,
    available_amount: Any = 100.0,
    available_date: date | None = DELIVERY_DATE,
    cutoff_passed: bool = False,
    closing_time: datetime | None = None,
    unit_price: Any = 1.25,
    comparison_price: Any = None,
    comparison_unit: str | None = None,
) -> Offer:
    now = datetime(2026, 11, 14, 12, 0, tzinfo=HEL)
    ct = closing_time or datetime(2026, 11, 15, 16, 0, tzinfo=HEL)
    return Offer.model_construct(
        product_id=pid,
        requested_date=DELIVERY_DATE,
        available=available,
        available_amount=available_amount,
        available_date=available_date,
        closing_time=ct,
        cutoff_passed=cutoff_passed,
        logistic_model="Stock",
        sales_block=False,
        sales_unit="ST",
        unit_price=unit_price,
        unit_price_gross=unit_price,
        list_price=unit_price,
        comparison_price=comparison_price,
        comparison_unit=comparison_unit,
        tax_rate=14.0,
        currency="EUR",
        price_valid_to=now + timedelta(days=2),
        fetched_at=now,
        issues=["cutoff_passed"] if cutoff_passed else [],
        provenance=None,
    )


def _sample_row(
    pid: str = "464306",
    *,
    name: str = "Valio Eila lactose free milk drink 1 L",
    content_amount: Any = 1.0,
    content_unit: Any = "L",
    allowed_lot_size: Any = 1.0,
    co2_kg_per_kg: Any = 1.1,
) -> dict[str, Any]:
    return {
        "id": pid,
        "gtin": "06408430000012",
        "name": name,
        "name_en": name,
        "name_fi": "Valio Eila laktoositon maitojuoma 1 L",
        "name_sv": "",
        "brand": "Valio",
        "vendor": "Valio Oy",
        "category": "10400",
        "sales_unit": "ST",
        "base_unit": "ST",
        "sales_unit_size": 1.0,
        "base_content_amount": content_amount,
        "content_amount": content_amount,
        "content_per_sales_unit": content_amount,
        "content_unit": content_unit,
        "allowed_lot_size": allowed_lot_size,
        "claims": ["FREE_FROM_LACTOSE"],
        "labels": [],
        "allergens": {"milk": "contains"},
        "allergen_source": {"milk": "structured"},
        "allergen_conflicts": {},
        "ingredients_text": "milk, vitamin D",
        "marketing_text": "Fresh Finnish milk drink",
        "nutrition": {"FAT": 1.5},
        "fat_pct": 1.5,
        "delivery_reliability": 0.98,
        "co2_kg_per_kg": co2_kg_per_kg,
    }


def _extract_mcp(res: Any) -> tuple[bool, Any, str]:
    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    text = " ".join(getattr(x, "text", "") for x in (getattr(res, "content", None) or []))
    if not text and sc is not None:
        text = json.dumps(sc)
    payload = sc
    if payload is None and text and "{" in text:
        try:
            payload = json.loads(text[text.index("{") :])
        except ValueError:
            payload = None
    return is_err, payload, text


async def _run_mcp_calls(srv: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, bool, Any, str]]:
    out: list[tuple[str, bool, Any, str]] = []
    async with Client(srv) as client:
        for label, args in calls:
            try:
                res = await client.call_tool("plan_need", args)
                is_err, payload, txt = _extract_mcp(res)
            except Exception as exc:
                is_err, payload, txt = True, None, f"{type(exc).__name__}: {exc}"
            _reset_breakers()
            out.append((label, is_err, payload, txt))
    return out


def test_file_hygiene_zero_comments_and_zero_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src
    comments = [tok for tok in tokenize.generate_tokens(io.StringIO(src).readline) if tok.type == tokenize.COMMENT]
    assert comments == []


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_ALL, ids=[x[0] for x in BAD_QUANTITIES_ALL])
def test_validate_need_and_division_helpers_reject_before_coverage_division(monkeypatch, label: str, bad_qty: Any) -> None:
    _ = label
    bad_need = _constructed_need(bad_qty)
    with pytest.raises(ValueError):
        validate_need(bad_need)

    eff_content_spy = CallSpy(side_effect=AssertionError("effective_content must not run"))
    ppu_spy = CallSpy(side_effect=AssertionError("price_per_unit must not run"))
    round_lot_spy = CallSpy(side_effect=AssertionError("_round_lot must not run"))
    monkeypatch.setattr(sub_mod, "effective_content", eff_content_spy)
    monkeypatch.setattr(sub_mod, "price_per_unit", ppu_spy)
    monkeypatch.setattr(plan_mod, "_round_lot", round_lot_spy)

    rows = {"464306": _sample_row("464306")}
    offers = {"464306": _sample_offer("464306")}
    with pytest.raises(ValueError):
        rank_substitutes(bad_need, offers=offers, rows=rows, relevance={"464306": 10.0})

    summary = ProductSummary.model_construct(
        id="464306",
        name="Milk 1 L",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        labels=[],
    )
    cand = Candidate.model_construct(
        product=summary,
        offer=offers["464306"],
        tier="best",
        rank_score=0.9,
        reasons=["ok"],
        flags=[],
        injection_screen={"suspicious": False, "reasons": [], "layers": ["regex"]},
    )
    with pytest.raises(ValueError):
        fill(bad_need, [cand])

    seed_plan = Plan.model_construct(
        need=_constructed_need(10.0),
        lines=[],
        candidates=[cand],
        covered_amount=0.0,
        shortfall=10.0,
        total_net=0.0,
        explanation="",
    )
    with pytest.raises(ValueError):
        _late_split(bad_need, seed_plan, [cand], rows, {"464306": 10.0}, {})

    assert eff_content_spy.call_count == 0
    assert ppu_spy.call_count == 0
    assert round_lot_spy.call_count == 0


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_CORE, ids=[x[0] for x in BAD_QUANTITIES_CORE])
def test_need_pydantic_model_rejects_nonpositive_and_nonfinite_quantities(label: str, bad_qty: Any) -> None:
    _ = label
    with pytest.raises(ValidationError):
        Need.model_validate(_need_dict(bad_qty))


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_ALL, ids=[x[0] for x in BAD_QUANTITIES_ALL])
def test_catalog_plan_need_rejects_before_offers_fn_division_and_missing_db(
    monkeypatch, tmp_path: Path, label: str, bad_qty: Any
) -> None:
    _ = label
    con_spy = _force_missing_catalog(monkeypatch, tmp_path)
    offers_spy = CallSpy(return_value={})
    default_offers_spy = CallSpy(return_value={})
    find_spy = CallSpy(return_value=({}, {}))
    rank_spy = CallSpy(return_value=[])
    fill_spy = CallSpy()
    monkeypatch.setattr(offers_mod, "get_offers", default_offers_spy)
    monkeypatch.setattr(plan_mod, "find_candidates", find_spy)
    monkeypatch.setattr(plan_mod, "rank_substitutes", rank_spy)
    monkeypatch.setattr(plan_mod, "fill", fill_spy)

    bad_need = _constructed_need(bad_qty)
    with pytest.raises(ValueError):
        catalog_plan_need(bad_need, offers_fn=offers_spy)
    with pytest.raises(ValueError):
        catalog_plan_need(bad_need, offers_fn=None)

    assert offers_spy.call_count == 0
    assert default_offers_spy.call_count == 0
    assert find_spy.call_count == 0
    assert rank_spy.call_count == 0
    assert fill_spy.call_count == 0
    assert con_spy.call_count == 0


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_ALL, ids=[x[0] for x in BAD_QUANTITIES_ALL])
def test_service_plan_need_and_deterministic_reject_422_before_offers_and_missing_db(
    monkeypatch, tmp_path: Path, label: str, bad_qty: Any
) -> None:
    _ = label
    con_spy = _force_missing_catalog(monkeypatch, tmp_path)
    upstream_offers_spy = CallSpy(return_value=({}, {}))
    cat_offers_spy = CallSpy(return_value={})
    cat_plan_spy = CallSpy()
    rank_spy = CallSpy(return_value=[])
    fill_spy = CallSpy()
    monkeypatch.setattr(bc, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(service, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(offers_mod, "get_offers", cat_offers_spy)
    monkeypatch.setattr(cat_mod, "plan_need", cat_plan_spy)
    monkeypatch.setattr(sub_mod, "rank_substitutes", rank_spy)
    monkeypatch.setattr(plan_mod, "fill", fill_spy)

    ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    bad_need_obj = _constructed_need(bad_qty)
    bad_need_dict = _need_dict(bad_qty)

    with pytest.raises(service.ServiceError) as exc_obj:
        service.plan_need(bad_need_obj, identity=ident)
    assert exc_obj.value.status == 422
    assert exc_obj.value.code in ("bad_quantity", "absurd_quantity", "invalid_need")
    assert exc_obj.value.code != "catalog_missing"

    with pytest.raises(service.ServiceError) as exc_dict:
        service.plan_need(bad_need_dict, identity=ident)
    assert exc_dict.value.status == 422
    assert exc_dict.value.code in ("bad_quantity", "absurd_quantity", "invalid_need")
    assert exc_dict.value.code != "catalog_missing"

    with pytest.raises(service.ServiceError) as exc_det:
        service._plan_deterministic(bad_need_obj)
    assert exc_det.value.status == 422
    assert exc_det.value.code in ("bad_quantity", "absurd_quantity")
    assert exc_det.value.code != "catalog_missing"

    assert upstream_offers_spy.call_count == 0
    assert cat_offers_spy.call_count == 0
    assert cat_plan_spy.call_count == 0
    assert rank_spy.call_count == 0
    assert fill_spy.call_count == 0
    assert con_spy.call_count == 0


def test_http_post_v1_plan_positive_control_missing_catalog_is_503_for_valid_qty(
    monkeypatch, tmp_path: Path, http_client: TestClient
) -> None:
    _force_missing_catalog(monkeypatch, tmp_path)
    resp = http_client.post("/v1/plan", json=_need_dict(10), headers=HEADERS)
    assert resp.status_code == 503
    assert resp.json()["error"] == "catalog_missing"


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_ALL, ids=[x[0] for x in BAD_QUANTITIES_ALL])
def test_http_post_v1_plan_returns_422_never_503_or_500_when_catalog_missing(
    monkeypatch, tmp_path: Path, http_client: TestClient, label: str, bad_qty: Any
) -> None:
    con_spy = _force_missing_catalog(monkeypatch, tmp_path)
    upstream_offers_spy = CallSpy(return_value=({}, {}))
    cat_offers_spy = CallSpy(return_value={})
    svc_plan_spy = CallSpy()
    rank_spy = CallSpy(return_value=[])
    fill_spy = CallSpy()
    monkeypatch.setattr(bc, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(service, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(offers_mod, "get_offers", cat_offers_spy)
    monkeypatch.setattr(service, "plan_need", svc_plan_spy)
    monkeypatch.setattr(sub_mod, "rank_substitutes", rank_spy)
    monkeypatch.setattr(plan_mod, "fill", fill_spy)

    raw_body = _raw_plan_json_bytes(label, bad_qty)
    resp = http_client.post(
        "/v1/plan",
        content=raw_body,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, f"{label}: got {resp.status_code} {resp.text}"
    body = resp.json()
    assert isinstance(body.get("error"), str) and body["error"]
    assert isinstance(body.get("message"), str) and body["message"]
    assert body["error"] != "catalog_missing"
    assert "catalog_missing" not in resp.text
    assert upstream_offers_spy.call_count == 0
    assert cat_offers_spy.call_count == 0
    assert svc_plan_spy.call_count == 0
    assert rank_spy.call_count == 0
    assert fill_spy.call_count == 0
    assert con_spy.call_count == 0


def test_mcp_plan_need_matrix_rejects_before_offers_division_and_missing_db(monkeypatch, tmp_path: Path) -> None:
    con_spy = _force_missing_catalog(monkeypatch, tmp_path)
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = mcp_server.build_mcp()

    ctrl = asyncio.run(_run_mcp_calls(srv, [("valid_control", {"need": _need_dict(10)})]))
    _, ctrl_err, ctrl_payload, ctrl_txt = ctrl[0]
    assert ctrl_err is True
    assert ctrl_payload is not None and ctrl_payload["error"] == "catalog_missing"
    assert "catalog_missing" in ctrl_txt

    upstream_offers_spy = CallSpy(return_value=({}, {}))
    cat_offers_spy = CallSpy(return_value={})
    svc_plan_spy = CallSpy()
    rank_spy = CallSpy(return_value=[])
    fill_spy = CallSpy()
    monkeypatch.setattr(bc, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(service, "get_offers_with_meta", upstream_offers_spy)
    monkeypatch.setattr(offers_mod, "get_offers", cat_offers_spy)
    monkeypatch.setattr(service, "plan_need", svc_plan_spy)
    monkeypatch.setattr(sub_mod, "rank_substitutes", rank_spy)
    monkeypatch.setattr(plan_mod, "fill", fill_spy)

    batch = [(label, {"need": _need_dict(bad_qty)}) for label, bad_qty in BAD_QUANTITIES_ALL]
    outcomes = asyncio.run(_run_mcp_calls(srv, batch))
    assert len(outcomes) == len(BAD_QUANTITIES_ALL)
    for label, is_err, payload, txt in outcomes:
        assert is_err is True, f"MCP accepted {label}"
        assert "catalog_missing" not in txt, f"{label} hit catalog check before quantity validation: {txt}"
        assert "UnexpectedToolError" not in txt, f"{label} raised unhandled error: {txt}"
        assert "ZeroDivisionError" not in txt, f"{label} hit ZeroDivisionError: {txt}"
        if payload is not None and "error" in payload:
            assert payload["error"] != "catalog_missing"

    ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    for label, bad_qty in BAD_QUANTITIES_ALL:
        with pytest.raises(service.ServiceError) as exc_d:
            mcp_server._guard_need(_need_dict(bad_qty), ident, check_customer_auth=True)
        assert exc_d.value.status == 422, label
        with pytest.raises(service.ServiceError) as exc_n:
            mcp_server._guard_need(_constructed_need(bad_qty), ident, check_customer_auth=True)
        assert exc_n.value.status == 422, label

    assert upstream_offers_spy.call_count == 0
    assert cat_offers_spy.call_count == 0
    assert svc_plan_spy.call_count == 0
    assert rank_spy.call_count == 0
    assert fill_spy.call_count == 0
    assert con_spy.call_count == 0


@pytest.mark.parametrize(("label", "bad_qty"), BAD_QUANTITIES_CORE, ids=[x[0] for x in BAD_QUANTITIES_CORE])
def test_fake_gateway_plan_need_rejects_before_offers_and_division(
    monkeypatch, tmp_path: Path, label: str, bad_qty: Any
) -> None:
    _ = label
    con_spy = _force_missing_catalog(monkeypatch, tmp_path)
    fg = FakeGateway(customer_id="hotel-aurora", agent_id="hotel-aurora/buyer-1")
    offer_spy = CallSpy(side_effect=fg._offer)
    eval_spy = CallSpy(side_effect=fg._eval_candidate)
    monkeypatch.setattr(fg, "_offer", offer_spy)
    monkeypatch.setattr(fg, "_eval_candidate", eval_spy)

    payload = _need_dict(bad_qty)
    with pytest.raises(ValueError):
        fg.plan_need(payload)
    with pytest.raises(ValueError):
        fg.call("plan_need", {"need": payload})

    assert offer_spy.call_count == 0
    assert eval_spy.call_count == 0
    assert con_spy.call_count == 0


@pytest.mark.parametrize(("label", "bad_val"), DEGENERATE_ROW_VALUES, ids=[x[0] for x in DEGENERATE_ROW_VALUES])
def test_degenerate_content_per_sales_unit_never_triggers_zero_division_in_plan_need(
    monkeypatch, label: str, bad_val: Any
) -> None:
    _ = label
    bad_row = _sample_row("900001", name="Degenerate lactose free milk", content_amount=bad_val, allowed_lot_size=1.0)
    good_row = _sample_row("900002", name="Healthy lactose free milk 1 L", content_amount=1.0, allowed_lot_size=1.0)

    def _mock_fts_only_bad(*_a: Any, **_kw: Any) -> list[tuple[str, float]]:
        return [("900001", 12.0)]

    def _mock_rows_only_bad(ids: list[str]) -> dict[str, dict[str, Any]]:
        return {pid: bad_row for pid in ids if pid == "900001"}

    def _offers_only_bad(ids: list[str], _d: Any, **_kw: Any) -> dict[str, Offer]:
        return {
            pid: _sample_offer(pid, unit_price=1.50, comparison_price=0.0, comparison_unit="L")
            for pid in ids
        }

    monkeypatch.setattr(textsearch, "fts_search", _mock_fts_only_bad)
    monkeypatch.setattr(textsearch, "get_rows", _mock_rows_only_bad)

    need = Need(
        customer_id="hotel-aurora",
        product_query="lactose free milk",
        required_claims=["FREE_FROM_LACTOSE"],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY_DATE,
    )
    plan_only_bad = catalog_plan_need(need, offers_fn=_offers_only_bad)
    assert plan_only_bad.lines == []
    assert plan_only_bad.covered_amount == 0.0
    assert plan_only_bad.shortfall == 10.0
    assert all(c.tier == "rejected" for c in plan_only_bad.candidates)

    both_rows = {"900001": bad_row, "900002": good_row}
    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [("900001", 15.0), ("900002", 11.0)])
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {pid: both_rows[pid] for pid in ids if pid in both_rows})

    def _offers_both(ids: list[str], _d: Any, **_kw: Any) -> dict[str, Offer]:
        return {pid: _sample_offer(pid, unit_price=1.20, comparison_price=None) for pid in ids}

    plan_mixed = catalog_plan_need(need, offers_fn=_offers_both)
    assert [ln.product_id for ln in plan_mixed.lines] == ["900002"]
    assert plan_mixed.covered_amount == 10.0
    assert plan_mixed.shortfall == 0.0

    bad_summary = ProductSummary.model_construct(
        id="900001",
        name="Degenerate milk",
        sales_unit="ST",
        content_amount=bad_val,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        labels=[],
    )
    bad_cand = Candidate.model_construct(
        product=bad_summary,
        offer=_sample_offer("900001", unit_price=1.50),
        tier="best",
        rank_score=0.9,
        reasons=["ok"],
        flags=[],
        injection_screen={"suspicious": False, "reasons": [], "layers": ["regex"]},
    )
    filled = fill(need, [bad_cand])
    assert filled.lines == []
    assert filled.shortfall == 10.0

    late_offer_obj = _sample_offer(
        "900001",
        available=False,
        available_amount=50.0,
        available_date=DELIVERY_DATE + timedelta(days=2),
        cutoff_passed=True,
        unit_price=1.50,
    )
    late_cand = Candidate.model_construct(
        product=bad_summary,
        offer=late_offer_obj,
        tier="rejected",
        rank_score=0.5,
        reasons=["Rejected: order cut-off passed (Sun 15 Nov 16:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
        injection_screen={"suspicious": False, "reasons": [], "layers": ["regex"]},
    )
    split_need = need.model_copy(update={"preferences": {"allow_split_delivery": True}})
    late_plan, late_notes = _late_split(
        split_need,
        filled.model_copy(update={"need": split_need}),
        [late_cand],
        {"900001": bad_row},
        {"900001": 12.0},
        {},
    )
    assert late_plan.lines == []
    assert late_notes == []


@pytest.mark.parametrize(("label", "bad_lot"), DEGENERATE_ROW_VALUES, ids=[x[0] for x in DEGENERATE_ROW_VALUES])
def test_degenerate_allowed_lot_size_never_triggers_zero_division_in_plan_need(
    monkeypatch, label: str, bad_lot: Any
) -> None:
    _ = label
    assert _round_lot(7.2, bad_lot, up=True) == 8.0
    assert _round_lot(7.8, bad_lot, up=False) == 7.0

    primary_row = _sample_row(
        "910001",
        name="Primary lactose free milk 1 L",
        content_amount=1.0,
        allowed_lot_size=bad_lot,
        co2_kg_per_kg=0.0,
    )
    late_row = _sample_row(
        "910002",
        name="Secondary lactose free milk 1 L",
        content_amount=1.0,
        allowed_lot_size=bad_lot,
        co2_kg_per_kg=-1.0,
    )
    rows_map = {"910001": primary_row, "910002": late_row}
    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [("910001", 14.0), ("910002", 12.0)])
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {pid: rows_map[pid] for pid in ids if pid in rows_map})

    def _offers_partial_and_late(ids: list[str], req_date: date, **_kw: Any) -> dict[str, Offer]:
        out: dict[str, Offer] = {}
        for pid in ids:
            if pid == "910001":
                out[pid] = _sample_offer(
                    pid,
                    available=True,
                    available_amount=4.0,
                    available_date=req_date,
                    cutoff_passed=False,
                    unit_price=1.10,
                    comparison_price=0.0,
                    comparison_unit="L",
                )
            elif pid == "910002":
                is_late_recheck = req_date > DELIVERY_DATE
                out[pid] = _sample_offer(
                    pid,
                    available=is_late_recheck,
                    available_amount=20.0,
                    available_date=DELIVERY_DATE + timedelta(days=2),
                    cutoff_passed=not is_late_recheck,
                    unit_price=1.20,
                    comparison_price=-1.0,
                    comparison_unit="L",
                )
        return out

    need = Need(
        customer_id="hotel-aurora",
        product_query="lactose free milk",
        required_claims=["FREE_FROM_LACTOSE"],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY_DATE,
        preferences={"allow_split_delivery": True},
    )
    plan = catalog_plan_need(need, offers_fn=_offers_partial_and_late)
    assert math.isfinite(plan.covered_amount) and plan.covered_amount == 10.0
    assert math.isfinite(plan.shortfall) and plan.shortfall == 0.0
    assert [ln.qty_sales_units for ln in plan.lines] == [4.0, 6.0]

    monkeypatch.setattr(
        service,
        "get_offers_with_meta",
        lambda ids, req_date, *_a, **_kw: (_offers_partial_and_late(ids, req_date), {"source": "mock"}),
    )
    det_plan = service._plan_deterministic(need)
    assert math.isfinite(det_plan.covered_amount) and det_plan.covered_amount == 4.0
    assert math.isfinite(det_plan.shortfall) and det_plan.shortfall == 6.0


@pytest.mark.parametrize(("label", "bad_val"), DEGENERATE_ROW_VALUES, ids=[x[0] for x in DEGENERATE_ROW_VALUES])
def test_degenerate_price_and_allergen_swap_rows_never_trigger_zero_division(
    monkeypatch, label: str, bad_val: Any
) -> None:
    _ = label
    amt, unit, note = units_mod.reconcile_with_price(bad_val, "L", 2.50, bad_val, "L")
    assert amt is None and unit == "L" and note is None

    degenerate_offer = _sample_offer(
        "920001",
        available=True,
        available_amount=bad_val,
        unit_price=bad_val,
        comparison_price=bad_val,
        comparison_unit="L",
    )
    assert price_per_unit(degenerate_offer, bad_val, "L", "L") is None

    milk_row = _sample_row(
        "920001",
        name="Dairy milk 1 L",
        content_amount=bad_val,
        allowed_lot_size=bad_val,
    )
    oat_row = _sample_row(
        "920002",
        name="Oatly oat drink 1 L",
        content_amount=bad_val,
        allowed_lot_size=bad_val,
    )
    oat_row["allergens"] = {"milk": "free", "nuts": "free", "gluten": "free"}
    oat_row["allergen_source"] = {"milk": "structured", "nuts": "structured", "gluten": "structured"}
    oat_row["claims"] = ["VEGAN"]
    rows_map = {"920001": milk_row, "920002": oat_row}

    monkeypatch.setattr(plan_mod, "_profile_usual_products", lambda _cid: ["920002"])
    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [("920001", 12.0)])
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {pid: rows_map[pid] for pid in ids if pid in rows_map})

    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        quantity=12.0,
        unit="L",
        delivery_by=DELIVERY_DATE,
    )
    plan = catalog_plan_need(
        need,
        offers_fn=lambda ids, _d, **_kw: {
            pid: _sample_offer(pid, unit_price=1.80, comparison_price=0.0, comparison_unit="L")
            for pid in ids
        },
    )
    assert plan.lines == []
    assert plan.covered_amount == 0.0
    assert plan.shortfall == 12.0

