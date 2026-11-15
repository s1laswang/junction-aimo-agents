"""Hardening (S2): sane quantities, bad-input 422 guards before planning, count-unit fraction checks,
plausibility approval guardrails, hostile customer-id rejection, and Helsinki audit timestamps.
Offline: recorded mock-API fixtures (tests/test_gateway.FixtureClient).
"""
from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import plan as catalog_plan
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog, resilience, service
from aimo.models import Need, PolicyDecision, Product, Quote, QuoteLine

from test_gateway import H, FixtureClient

D = "2026-11-16"
NEED = {"customer_id": "hotel-aurora", "product_query": "lactose-free milk", "unit": "L", "delivery_by": D}
IDENT = {"agent_id": "buyer-1", "customer_id": "hotel-aurora"}


def _synth_products() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]

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
            "baseUnit": sales,
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
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "FREE_FROM"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                ],
                "keyIngredients": [{"language": "en", "value": name_en}],
                "marketingTexts": [{"language": "en", "value": name_en}],
            },
        }

    return [
        _item("463038", "KI", "Valio Eila semi skimmed milk drink 10 l novobox lactose free", "Valio", "10400", [("KI", 2)], [{"synkkaPackagingType": "CASE"}]),
        _item("464306", "ST", "Valio Eila semi skimmed milk drink 1 l lactose free", "Valio", "10400", [("ST", 1)], base_1l),
        _item("469363", "ST", "Arla Semi skimmed milkdrink 1 L UHT lactose free", "Arla", "10400", [("ST", 1)], base_1l),
        _item("460573", "KG", "PP potato pre-washed Bellarosa", "PP", "21000", [("KG", 1)], base_1kg),
        _item("900002", "ST", "Terminal mineral water 0.5 l", "Valio", "10800", [("ST", 1)], base_1l),
    ]


class HardenFixtureClient(FixtureClient):
    def __init__(self):
        super().__init__()
        self.price_raw = copy.deepcopy(self.price_raw)
        self.avail_raw = copy.deepcopy(self.avail_raw)
        self.price_raw["products"].extend([
            {
                "productId": "460573",
                "quantity": 1.0,
                "catalogPrice": {"value": 2.40, "valueWithTax": 2.72, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "priceWithTax": {"value": 2.72, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "comparisonPrice": {"value": 2.40, "valueWithTax": 2.72, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "comparisonUnit": "KG",
                "tax": {"taxRate": 13.5, "taxValue": 0.32},
                "netPriceFound": True,
                "error": False,
            },
            {
                "productId": "900002",
                "quantity": 1.0,
                "catalogPrice": {"value": 0.20, "valueWithTax": 0.23, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "priceWithTax": {"value": 0.23, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "comparisonPrice": {"value": 0.40, "valueWithTax": 0.46, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200", "currency": "EUR"},
                "comparisonUnit": "L",
                "tax": {"taxRate": 13.5, "taxValue": 0.03},
                "netPriceFound": True,
                "error": False,
            },
        ])
        self.avail_raw["products"].extend([
            {
                "productId": "460573",
                "closingTime": "2026-11-15T17:00:00+0200",
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [{"salesBlock": False, "availableAmount": 500.0, "availableDate": "2026-11-16", "salesUnit": "KG"}],
            },
            {
                "productId": "900002",
                "closingTime": "2026-11-15T17:00:00+0200",
                "salesBlock": False,
                "logisticModel": "Terminal",
                "scheduleLines": [{"salesBlock": False, "availableAmount": 999999.0, "availableDate": "2026-11-16", "salesUnit": "ST"}],
            },
        ])


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("harden-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, synth_catalog_dir):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    for k in ("AIMO_MCP_URL", "AIMO_SERVE_URL", "AIMO_NOW", "AIMO_NOW_FROZEN"):
        monkeypatch.delenv(k, raising=False)
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
    resilience.breaker("catalog.plan_need").reset()
    resilience.breaker("catalog.search").reset()
    bc.set_client(HardenFixtureClient())
    yield
    bc.set_client(None)
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def api():
    from aimo.gateway.app import create_app
    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"}, raise_server_exceptions=False)


def _quote(api, lines, **extra):
    return api.post("/v1/quotes", headers=H, json={"delivery_date": D, "lines": lines, **extra})


def _err(r, status, code=None):
    assert r.status_code == status, r.text
    body = r.json()
    assert isinstance(body, dict), body
    if code:
        assert body.get("error") == code and body.get("message"), body
    else:
        assert (body.get("error") and body.get("message")) or "detail" in body, body
    return body


@pytest.mark.parametrize("qty", [0, 0.0, -5, -0.01, "NaN", "Infinity", "-Infinity", 1e6 + 1, 1e9])
def test_plan_bad_quantities_rejected_before_planning_rest(api, monkeypatch, qty):
    monkeypatch.setattr(catalog_plan, "find_candidates", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")))
    r = api.post("/v1/plan", headers=H, json={**NEED, "quantity": qty})
    _err(r, 422)


def test_plan_raw_json_nan_and_inf_rejected_never_500(api, monkeypatch):
    monkeypatch.setattr(catalog_plan, "find_candidates", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")))
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = ('{"customer_id":"hotel-aurora","product_query":"milk","quantity":' + token + ',"unit":"L","delivery_by":"2026-11-16"}').encode()
        r = api.post("/v1/plan", headers={**H, "content-type": "application/json"}, content=raw)
        assert r.status_code in (400, 422), (token, r.status_code, r.text)


def test_plan_absurd_quantity_is_422(api):
    b = _err(api.post("/v1/plan", headers=H, json={**NEED, "quantity": 1e9}), 422, "absurd_quantity")
    assert "not plausible" in b["message"]


def test_plan_empty_and_overlong_query_are_422(api):
    _err(api.post("/v1/plan", headers=H, json={**NEED, "quantity": 10, "product_query": ""}), 422, "empty_need")
    _err(api.post("/v1/plan", headers=H, json={**NEED, "quantity": 10, "product_query": "   "}), 422, "empty_need")
    _err(api.post("/v1/plan", headers=H, json={**NEED, "quantity": 10, "product_query": "x" * 501}), 422, "query_too_long")


@pytest.mark.parametrize("qty", [0, 0.0, -5, float("nan"), float("inf"), float("-inf")])
def test_need_model_rejects_non_positive_and_non_finite_quantities(qty):
    with pytest.raises(ValidationError):
        Need(customer_id="hotel-aurora", product_query="milk", quantity=qty, unit="L", delivery_by=D)


@pytest.mark.parametrize("qty", [0, 0.0, -5, float("nan"), float("inf"), float("-inf"), 1e6 + 1, 1e9, True, False, None, "ten"])
def test_service_plan_need_rejects_bad_quantities_before_planning(monkeypatch, qty):
    monkeypatch.setattr(catalog_plan, "find_candidates", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")))
    constructed = Need.model_construct(
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        quantity=qty,
        unit="L",
        delivery_by=date.fromisoformat(D),
    )
    with pytest.raises(service.ServiceError) as ei_obj:
        service.plan_need(constructed, identity=IDENT)
    assert ei_obj.value.status == 422 and ei_obj.value.code in ("bad_quantity", "absurd_quantity")

    with pytest.raises(service.ServiceError) as ei_dict:
        service.plan_need({**NEED, "quantity": qty}, identity=IDENT)
    assert ei_dict.value.status == 422 and ei_dict.value.code in ("invalid_need", "bad_quantity", "absurd_quantity")


def test_service_plan_need_maps_catalog_value_error_to_422(monkeypatch):
    bad_unit_need = Need.model_construct(
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        quantity=10.0,
        unit="GALLONS",
        delivery_by=date.fromisoformat(D),
    )
    with pytest.raises(service.ServiceError) as ei:
        service.plan_need(bad_unit_need, identity=IDENT)
    assert ei.value.status == 422 and ei.value.code == "bad_quantity"
    assert not resilience.breaker("catalog.plan_need").is_open()


def test_fractional_count_unit_is_422_with_plain_reason(api):
    b = _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2.5}]), 422, "fractional_quantity")
    assert "ST is a count unit" in b["message"] and "2 or 3" in b["message"]
    ref = b["detail"]["refused"][0]
    assert ref["sales_unit"] == "ST" and ref["suggested_qty"] == [2, 3]

    b_sub = _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 0.5}]), 422, "fractional_quantity")
    assert b_sub["detail"]["refused"][0]["suggested_qty"] == [1]


def test_whole_count_units_still_quote(api):
    r = _quote(api, [{"product_id": "464306", "qty_sales_units": 3}])
    assert r.status_code == 200, r.text


def test_fractional_measure_unit_kg_is_allowed(api, monkeypatch):
    real = service.get_product

    def kg_prod(pid):
        if pid == "460573":
            try:
                p = real(pid)
                return p.model_copy(update={"sales_unit": "KG", "allowed_lot_size": 0.01})
            except service.ServiceError:
                return Product(id="460573", name="PP potato pre-washed Bellarosa", sales_unit="KG", allowed_lot_size=0.01)
        return real(pid)

    monkeypatch.setattr(service, "get_product", kg_prod)
    r = _quote(api, [{"product_id": "460573", "qty_sales_units": 2.5}])
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["lines"][0]["sales_unit"] == "KG" and q["lines"][0]["qty_sales_units"] == pytest.approx(2.5)


def test_fraction_allowed_only_by_a_real_fractional_lot_step(api, monkeypatch):
    real = service.get_product

    def half_box(pid):
        p = real(pid)
        return p.model_copy(update={"allowed_lot_size": 0.5}) if pid == "464306" else p

    monkeypatch.setattr(service, "get_product", half_box)
    assert _quote(api, [{"product_id": "464306", "qty_sales_units": 2.5}]).status_code == 200
    _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2.25}]), 422, "fractional_quantity")


@pytest.mark.parametrize("qty", [1e9, 1e6 + 1, 1e300])
def test_absurd_quantity_is_422_rest(api, qty):
    b = _err(_quote(api, [{"product_id": "464306", "qty_sales_units": qty}]), 422)
    assert b.get("error") in ("absurd_quantity", "validation_error")


@pytest.mark.parametrize("qty", [0, -3, 1e9, float("inf"), float("nan"), "lots", None, True])
def test_absurd_or_bad_quantity_is_422_in_process_and_mcp_path(qty):
    with pytest.raises(service.ServiceError) as ei:
        service.create_quote(D, [{"product_id": "464306", "qty_sales_units": qty}], identity=IDENT)
    assert ei.value.status == 422 and ei.value.code in ("absurd_quantity", "bad_quantity")


def test_split_lines_summing_past_the_cap_are_422():
    with pytest.raises(service.ServiceError) as ei:
        service.create_quote(D, [{"product_id": "464306", "qty_sales_units": 900_000}] * 2, identity=IDENT)
    assert ei.value.status == 422 and ei.value.code == "absurd_quantity"


def test_quote_with_bad_need_is_422(api):
    _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2}], need={**NEED, "quantity": 1e9}), 422)
    _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2}], need={**NEED, "quantity": 10, "product_query": ""}), 422)
    _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2}], need={**NEED, "quantity": 10, "product_query": "x" * 501}), 422)


def test_large_qty_against_standing_baseline_needs_approval(api, monkeypatch):
    monkeypatch.setattr(service, "_standing_baseline", lambda c, pid: 2.0 if pid == "464306" else None)
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 120}]).json()
    assert "unknown_stock_large_qty" in q["lines"][0]["flags"], q["lines"][0]
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval", o
    hits = {h["rule_id"]: h for h in o["decision"]["hits"]}
    assert hits["quantity.plausibility"]["outcome"] == "needs_approval"
    assert "60x" in hits["quantity.plausibility"]["message"] or "60x" in str(q["provenance"]["line_verification"])


def test_normal_qty_within_baseline_is_not_flagged(api, monkeypatch):
    monkeypatch.setattr(service, "_standing_baseline", lambda c, pid: 20.0 if pid == "464306" else None)
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 40}]).json()
    assert "unknown_stock_large_qty" not in q["lines"][0]["flags"]
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert "quantity.plausibility" not in {h["rule_id"] for h in o["decision"]["hits"]}, o["decision"]


def test_terminal_unknown_stock_over_1000_needs_approval(api, monkeypatch):
    real = service.get_product

    def terminal_prod(pid):
        if pid == "900002":
            return Product(
                id="900002",
                name="Terminal mineral water 0.5 l",
                sales_unit="ST",
                category="10800",
                allowed_lot_size=0.01,
                allergens={"milk": "free", "gluten": "free", "nuts": "free"},
                allergen_source={"milk": "structured", "gluten": "structured", "nuts": "structured"},
            )
        return real(pid)

    monkeypatch.setattr(service, "get_product", terminal_prod)
    r = _quote(api, [{"product_id": "900002", "qty_sales_units": 1500}])
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["total_net"] == pytest.approx(300.0)
    assert "unknown_stock_large_qty" in q["lines"][0]["flags"], q["lines"][0]
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval", o
    hits = {h["rule_id"]: h for h in o["decision"]["hits"]}
    assert hits["quantity.plausibility"]["outcome"] == "needs_approval"


def test_unknown_stock_large_qty_reason():
    terminal = SimpleNamespace(available_amount=None, issues=["terminal_unknown_qty"], logistic_model="Terminal")
    stock = SimpleNamespace(available_amount=5000.0, issues=[], logistic_model="Stock")
    assert service._large_qty_reason("nobody", "x", 5000, terminal)
    assert service._large_qty_reason("nobody", "x", 50, terminal) is None
    assert service._large_qty_reason("nobody", "x", 5000, stock) is None


def test_quantity_guard_never_auto_and_keeps_reject():
    now = datetime.now(timezone.utc)
    ln = QuoteLine(
        product_id="1",
        product_name="x",
        qty_sales_units=5000,
        sales_unit="ST",
        unit_price=1,
        line_total=5000,
        flags=["unknown_stock_large_qty"],
    )
    q = Quote(
        quote_id="Q",
        customer_id="hotel-aurora",
        agent_id="buyer-1",
        delivery_date=D,
        lines=[ln],
        total_net=1,
        total_gross=1,
        created_at=now,
        expires_at=now,
    )
    auto = PolicyDecision(outcome="auto_approve", policy_id="p", policy_version="1")
    d = service._quantity_guard(q, auto)
    assert d.outcome == "needs_approval" and d.approver_role
    rej = PolicyDecision(outcome="reject", policy_id="p", policy_version="1")
    d_rej = service._quantity_guard(q, rej)
    assert d_rej.outcome == "reject"
    assert any(h.rule_id == "quantity.plausibility" for h in d_rej.hits)


def test_hostile_customer_ids_are_403_unknown_customer_never_500(api):
    auth_ident = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    auth_ident.authenticated = {"customer": True, "agent": True}
    with pytest.raises(service.ServiceError) as ei_mis:
        service.plan_need({**NEED, "customer_id": "school-tampere", "quantity": 10}, identity=auth_ident)
    assert ei_mis.value.status == 403 and ei_mis.value.code == "customer_mismatch"

    for cid in ("x" * 20_000, "../../etc/passwd", "../hotel-aurora", "a/b", "unknown-corp"):
        with pytest.raises(service.ServiceError) as ei:
            service.mandate_for(service.Identity(customer_id=cid))
        assert ei.value.status == 403 and ei.value.code == "unknown_customer"

        with pytest.raises(service.ServiceError) as ei_plan:
            service.plan_need({**NEED, "customer_id": cid, "quantity": 10})
        assert ei_plan.value.status == 403 and ei_plan.value.code == "unknown_customer"

        with pytest.raises(service.ServiceError) as ei_off:
            service.fetch_offers(["464306"], date.fromisoformat(D), customer_id=cid)
        assert ei_off.value.status == 403 and ei_off.value.code == "unknown_customer"

        with pytest.raises(service.ServiceError) as ei_ord:
            service.submit_order(service.Identity(customer_id=cid), "Q-missing", "0" * 64)
        assert ei_ord.value.status == 403 and ei_ord.value.code == "unknown_customer"

        hdr = {**H, "X-Customer-Id": cid}
        for r in (
            api.post("/v1/quotes", headers=hdr, json={"delivery_date": D, "lines": [{"product_id": "464306", "qty_sales_units": 1}]}),
            api.post("/v1/orders", headers=hdr, json={"quote_id": "Q-missing", "quote_hash": "0" * 64}),
            api.post("/v1/offers", headers=hdr, json={"product_ids": ["464306"], "requested_date": D}),
            api.post("/v1/plan", headers=hdr, json={**NEED, "quantity": 10}),
            api.get("/v1/products/search", headers=hdr, params={"q": "milk"}),
        ):
            assert r.status_code == 403, (cid[:20], r.status_code, r.text[:200])
            assert r.json()["error"] == "unknown_customer"


def test_product_offers_and_search_bad_input_guards(api):
    for non_str_pid in (None, 123, []):
        with pytest.raises(service.ServiceError) as ei_type:
            service.get_product(non_str_pid)
        assert ei_type.value.status == 422 and ei_type.value.code == "bad_product_id"

    for missing_pid in ("", "   ", "does-not-exist", "x" * 65, "x" * 2000, "464\x00306"):
        with pytest.raises(service.ServiceError) as ei_nf:
            service.get_product(missing_pid)
        assert ei_nf.value.status == 404 and ei_nf.value.code == "product_not_found"

    _err(api.get("/v1/products/does-not-exist", headers=H), 404, "product_not_found")
    _err(api.get("/v1/products/" + "x" * 65, headers=H), 404, "product_not_found")

    for bad_pid in ("x" * 65, "464 306", "464\x00306"):
        with pytest.raises(service.ServiceError) as ei_o:
            service.get_offers([bad_pid], D, identity=IDENT)
        assert ei_o.value.status == 422 and ei_o.value.code == "bad_product_id"
        _err(api.post("/v1/offers", headers=H, json={"product_ids": [bad_pid], "requested_date": D}), 422)

    for bad_ids, expected in (([], "no_products"), (["", "   "], "no_products"), (["464306"] * 1001, "too_many_products")):
        with pytest.raises(service.ServiceError) as ei_ids:
            service.get_offers(bad_ids, D, identity=IDENT)
        assert ei_ids.value.status == 422 and ei_ids.value.code == expected
    _err(api.post("/v1/offers", headers=H, json={"product_ids": [""], "requested_date": D}), 422)

    for bad_q in ("", "   ", "x" * 501):
        with pytest.raises(service.ServiceError) as ei_q:
            service.search_products(bad_q)
        assert ei_q.value.status == 422
        _err(api.get("/v1/products/search", headers=H, params={"q": bad_q}), 422)

    for bad_lim in (0, -5, 101):
        with pytest.raises(service.ServiceError) as ei_l:
            service.search_products("milk", limit=bad_lim)
        assert ei_l.value.status == 422 and ei_l.value.code == "bad_limit"
        with pytest.raises(service.ServiceError) as ei_fl:
            service.find_products("milk", limit=bad_lim)
        assert ei_fl.value.status == 422 and ei_fl.value.code == "bad_limit"


def test_helsinki_timestamp_helper_and_expired_order_audit(api, monkeypatch):
    assert service._hel(datetime(2026, 11, 14, 12, 16, tzinfo=timezone.utc)) == "14:16"
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 10}]).json()
    real_now = service._now
    monkeypatch.setattr(service, "_now", lambda: real_now() + timedelta(hours=1))
    r = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    _err(r, 409, "quote_expired")
    monkeypatch.setattr(service, "_now", real_now)
    evs = [e for e in api.get("/v1/audit", params={"subject_id": q["quote_id"]}).json() if e["action"] == "order.refused"]
    assert evs and "Helsinki" in evs[-1]["summary"]
