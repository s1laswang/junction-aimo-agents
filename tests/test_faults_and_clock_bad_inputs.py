"""Unit and FastAPI TestClient tests for adversarial X-Aimo-Faults, X-Aimo-Now, offer merge, and Peppol numbers."""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store, clock, peppol
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.models import Offer

FIX = Path(__file__).resolve().parent / "fixtures"
DELIVERY = date(2026, 11, 16)
HEADERS = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
MILK_NEED = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "required_claims": ["FREE_FROM_LACTOSE"],
    "quantity": 100,
    "unit": "L",
    "delivery_by": "2026-11-16",
    "preferred_product_ids": ["463038"],
}


class _FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        self.stats = {"upstream_calls": 0}

    def _sel(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        self.stats["upstream_calls"] += 1
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        return self._sel(self.avail_raw, ids)


def _synthetic_catalog_products() -> list[dict[str, Any]]:
    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": "10400",
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
                "marketingTexts": [{"language": "en", "value": "Lactose free milk drink for kitchens."}],
            },
        }

    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    return [
        _item(
            "463038",
            "KI",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            [("KI", 2), ("ST", 1)],
            [{"synkkaPackagingType": "CASE"}],
        ),
        _item(
            "464306",
            "ST",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            base_1l,
        ),
        _item(
            "469363",
            "ST",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            [("ST", 1)],
            base_1l,
        ),
    ]


@pytest.fixture
def isolated_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    used_synth_catalog = not config.PRODUCT_JSON.exists()
    if used_synth_catalog:
        pjson = tmp_path / "valio_aimo_junction26_product_data.json"
        cdb = tmp_path / "catalog.sqlite"
        pjson.write_text(json.dumps(_synthetic_catalog_products()), encoding="utf-8")
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    bc.set_client(_FixtureClient())
    from aimo.gateway.app import create_app

    api = TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"})
    try:
        yield api
    finally:
        bc.set_client(None)
        if used_synth_catalog:
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()


def test_parse_faults_latency_bounds_and_nonfinite():
    assert bc.parse_faults("latency_ms:inf").latency_ms == 0
    assert bc.parse_faults("latency_ms:-inf").latency_ms == 0
    assert bc.parse_faults("latency_ms:nan").latency_ms == 0
    assert bc.parse_faults("latency_ms:-500").latency_ms == 0
    assert bc.parse_faults("latency_ms:0").latency_ms == 0
    assert bc.parse_faults("latency_ms:250").latency_ms == 250
    assert bc.parse_faults("latency_ms:99999999").latency_ms == 5000
    assert bc.parse_faults("latency_ms:1e308").latency_ms == 5000
    assert bc.parse_faults("latency_ms:1e400").latency_ms == 0


def test_parse_faults_partial_price_shock_rates_and_caps():
    for bad in ("partial:*:nan", "partial:*:inf", "partial:*:-inf", "partial:*:-10", "partial:*:-0.01", "partial:*:1e400"):
        assert bc.parse_faults(bad).partial == {}
    assert bc.parse_faults("partial:*:0").partial == {"*": 0.0}
    assert bc.parse_faults("partial:464306:15.5").partial == {"464306": 15.5}

    for bad_ps in (
        "price_shock:*:nan",
        "price_shock:*:inf",
        "price_shock:*:-inf",
        "price_shock:*:0",
        "price_shock:*:-3",
        "price_shock:*:1000001",
        "price_shock:*:1e400",
    ):
        assert bc.parse_faults(bad_ps).price_shock == {}
    assert bc.parse_faults("price_shock:464306:1.4").price_shock == {"464306": 1.4}
    assert bc.parse_faults("price_shock:*:1000000").price_shock == {"*": 1000000.0}

    assert bc.parse_faults("error_rate:nan").error_rate == 0.0
    assert bc.parse_faults("error_rate:inf").error_rate == 0.0
    assert bc.parse_faults("error_rate:-1.5").error_rate == 0.0
    assert bc.parse_faults("error_rate:2.5").error_rate == 1.0
    assert bc.parse_faults("rate_limit:nan").rate_limit == 0.0
    assert bc.parse_faults("rate_limit:inf").rate_limit == 0.0
    assert bc.parse_faults("rate_limit:-0.2").rate_limit == 0.0
    assert bc.parse_faults("rate_limit:9.0").rate_limit == 1.0

    huge_spec = "garbage:val;" * 1000
    parsed_huge = bc.parse_faults(huge_spec)
    assert len(parsed_huge.spec) <= 2000

    many_parts = ";".join(f"stockout:p{i}" for i in range(100))
    parsed_many = bc.parse_faults(many_parts)
    assert len(parsed_many.stockout) == 64


@pytest.mark.parametrize("bad_price", [True, False, 0, 0.0, -0.0, -1.5, math.nan, math.inf, -math.inf, 10**400, "abc", None])
def test_price_val_and_merge_offer_reject_bad_prices(bad_price):
    assert bc._price_val({"value": bad_price}) is None
    pr = {
        "productId": "464306",
        "catalogPrice": {"value": bad_price, "valueWithTax": 1.2, "validTo": "2026-12-01T00:00:00+0200"},
    }
    av = {
        "productId": "464306",
        "logisticModel": "Stock",
        "salesBlock": False,
        "closingTime": "2026-11-15T15:00:00+0200",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": 50, "availableDate": "2026-11-16", "salesBlock": False}],
    }
    offer = bc.merge_offer("464306", DELIVERY, pr, av, now=datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc))
    assert offer.unit_price is None
    assert offer.available is False
    assert "price_error" in offer.issues


@pytest.mark.parametrize("bad_amt", [True, False, math.nan, math.inf, -math.inf, -1, -0.01, "abc", 10**400])
def test_merge_offer_rejects_bad_available_amount_and_timestamps(bad_amt):
    pr = {
        "productId": "464306",
        "_fetched_at": 1e25,
        "catalogPrice": {"value": 1.25, "valueWithTax": 1.4, "validTo": "2026-12-01T00:00:00+0200"},
        "tax": {"taxRate": 0.0},
    }
    av = {
        "productId": "464306",
        "_fetched_at": math.nan,
        "_meta": {"timeNow": "2026-11-14T14:00:00+0200"},
        "logisticModel": "Stock",
        "salesBlock": False,
        "closingTime": "2026-11-15T15:00:00+0200",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": bad_amt, "availableDate": "2026-11-16", "salesBlock": False}],
    }
    offer = bc.merge_offer("464306", DELIVERY, pr, av)
    assert offer.available_amount == 0.0
    assert offer.available is False
    assert "availability_error" in offer.issues
    assert offer.tax_rate == 0.0
    assert offer.fetched_at.tzinfo is not None


@pytest.mark.parametrize("bad_oldest", [1e25, -1e25, math.nan, math.inf, -math.inf, True, "bad"])
def test_get_offers_with_meta_guards_corrupt_oldest_fetch(bad_oldest):
    class _CorruptTsClient(_FixtureClient):
        def _sel(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
            res = super()._sel(raw, ids)
            res["_oldest_fetch"] = bad_oldest
            for p in res["products"]:
                p["_fetched_at"] = bad_oldest
            return res

    bc.set_client(_CorruptTsClient())
    try:
        offers, meta = bc.get_offers_with_meta(["464306"], DELIVERY)
        assert offers["464306"].available is True
        assert datetime.fromisoformat(meta["fetched_at"]).tzinfo is not None
    finally:
        bc.set_client(None)


def test_apply_faults_guards_price_shock_overflow_and_direct_nonfinite():
    base = Offer(
        product_id="464306",
        requested_date=DELIVERY,
        available=True,
        available_amount=100.0,
        unit_price=1e305,
        unit_price_gross=1e305,
        comparison_price=1e305,
        fetched_at=datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc),
    )
    overflow_faults = bc.Faults(price_shock={"*": 1e6}, spec="price_shock:*:1e6")
    out = bc.apply_faults({"464306": base}, overflow_faults)
    assert out["464306"].unit_price is None
    assert out["464306"].available is False
    assert "price_error" in out["464306"].issues

    normal = base.model_copy(update={"unit_price": 1.5, "unit_price_gross": 1.71, "comparison_price": 1.5})
    bad_direct = bc.Faults(price_shock={"*": math.nan}, partial={"*": math.nan}, spec="manual")
    out_nan = bc.apply_faults({"464306": normal}, bad_direct)
    assert out_nan["464306"].unit_price == 1.5
    assert out_nan["464306"].available_amount == 100.0


@pytest.mark.parametrize(
    "bad_now",
    [
        "9999-12-31T23:59:59Z",
        "9999-12-31T23:59:59+02:00",
        "0001-01-01T00:00:00Z",
        "0001-01-01T00:00:00+05:00",
        "1999-12-31T23:59:59Z",
        "2101-01-01T00:00:00Z",
        "not-a-timestamp",
        "2026-11-14T14:00:00Z" + ("x" * 60),
        "z" * 10240,
    ],
)
def test_clock_parse_and_next_delivery_day_bounds(bad_now):
    assert clock.parse(bad_now) is None
    tok = clock.set_request_now(bad_now)
    try:
        d = clock.next_delivery_day()
        assert 2000 <= d.year <= 2101
    finally:
        clock.reset_request_now(tok)


@pytest.mark.parametrize("bad_days", [True, False, 0, -5, -999999, 999999999, math.inf, math.nan, "bad", None])
def test_clock_next_delivery_day_clamps_min_days(bad_days):
    anchor = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    d = clock.next_delivery_day(at=anchor, min_days=bad_days)
    assert date(2026, 11, 15) <= d <= date(2027, 12, 10)
    assert d.weekday() < 5


@pytest.mark.parametrize("bad_num", [True, False, math.nan, math.inf, -math.inf, 10**400, None, "12"])
def test_peppol_num_rejects_bool_and_nonfinite_with_value_error(bad_num):
    with pytest.raises(ValueError, match="Peppol numeric value must be finite"):
        peppol._num(bad_num)


def test_peppol_num_formats_valid_finite_values():
    assert peppol._num(0) == "0"
    assert peppol._num(-0.0) == "0"
    assert peppol._num(-0.00001) == "0"
    assert peppol._num(30) == "30"
    assert peppol._num(1.01) == "1.01"
    assert peppol._num(2.5000) == "2.5"


@pytest.mark.parametrize(
    "fault_header",
    [
        "latency_ms:inf",
        "latency_ms:nan",
        "latency_ms:-500",
        "price_shock:*:nan",
        "price_shock:*:inf",
        "price_shock:*:0",
        "price_shock:*:-3",
        "partial:*:nan",
        "partial:*:-10",
        "error_rate:nan",
        "rate_limit:inf",
        "garbage:1:2;unknown_fault:xyz;" * 400,
    ],
)
def test_adversarial_fault_headers_never_500_across_gateway_routes(isolated_gateway: TestClient, fault_header: str):
    req_headers = {**HEADERS, "X-Aimo-Faults": fault_header}

    r_health = isolated_gateway.get("/healthz", headers=req_headers)
    assert r_health.status_code == 200
    assert r_health.json()["ok"] is True

    r_offers = isolated_gateway.post(
        "/v1/offers",
        headers=req_headers,
        json={"product_ids": ["464306", "469363"], "requested_date": "2026-11-16"},
    )
    assert r_offers.status_code == 200, r_offers.text
    offers_payload = r_offers.json()
    assert len(offers_payload) == 2
    for item in offers_payload:
        assert item["unit_price"] is not None and item["unit_price"] > 0

    r_plan = isolated_gateway.post("/v1/plan", headers=req_headers, json=MILK_NEED)
    assert r_plan.status_code == 200, r_plan.text
    assert r_plan.json()["lines"]

    r_quote = isolated_gateway.post(
        "/v1/quotes",
        headers=req_headers,
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
    )
    assert r_quote.status_code == 200, r_quote.text
    assert r_quote.json()["total_net"] > 0


def test_latency_ms_huge_header_capped_to_5s_without_sleeping_in_test(
    isolated_gateway: TestClient, monkeypatch: pytest.MonkeyPatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(bc.time, "sleep", lambda s: sleeps.append(s))
    req_headers = {**HEADERS, "X-Aimo-Faults": "latency_ms:99999999"}
    r = isolated_gateway.post(
        "/v1/offers",
        headers=req_headers,
        json={"product_ids": ["464306"], "requested_date": "2026-11-16"},
    )
    assert r.status_code == 200
    assert sleeps == [5.0]


@pytest.mark.parametrize(
    "now_header",
    [
        "9999-12-31T23:59:59Z",
        "0001-01-01T00:00:00Z",
        "garbage-not-iso-datetime",
        "2026-11-14T14:00:00Z" + ("x" * 10240),
    ],
)
def test_adversarial_now_headers_never_500_across_gateway_routes(isolated_gateway: TestClient, now_header: str):
    req_headers = {
        **HEADERS,
        "X-Aimo-Now": now_header,
        "X-Aimo-Faults": "latency_ms:inf;price_shock:*:nan;partial:*:nan;rate_limit:inf",
    }

    r_health = isolated_gateway.get("/healthz", headers=req_headers)
    assert r_health.status_code == 200
    assert r_health.json()["clock"]["mode"] != "header"

    r_offers = isolated_gateway.post(
        "/v1/offers",
        headers=req_headers,
        json={"product_ids": ["464306"], "requested_date": "2026-11-16"},
    )
    assert r_offers.status_code == 200, r_offers.text

    r_plan = isolated_gateway.post("/v1/plan", headers=req_headers, json=MILK_NEED)
    assert r_plan.status_code == 200, r_plan.text

    r_quote = isolated_gateway.post(
        "/v1/quotes",
        headers=req_headers,
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
    )
    assert r_quote.status_code == 200, r_quote.text
