from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone, tzinfo
import io
import json
import math
from pathlib import Path
import threading
import tokenize
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from aimo import config
from aimo.backend import catalog_store
from aimo.catalog import delivery, embeddings, netcheck, textsearch
from aimo.gateway import fallback_catalog
from aimo.gateway.app import create_app
from aimo.models import Offer, Product, ProductSummary

HELSINKI = ZoneInfo("Europe/Helsinki")
NOW_UTC = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)


class _BrokenTz(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "BROKEN"


def _synth_products() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]
    base_pcs = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH"}]
    return [
        {
            "id": "464306",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "temperatureCondition": 2,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [
                    {"language": "en", "value": "Valio Eila semi skimmed milk drink 1 l lactose free"},
                    {"language": "fi", "value": "Valio Eila kevytmaitojuoma 1 l laktoositon"},
                ],
                "unitConversions": base_1l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "type": "NUMBER", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "pasteurized lactose free milk"}],
            },
        },
        {
            "id": "500101",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10410",
            "vendorName": "Oatly",
            "allowedLotSize": 1.0,
            "temperatureCondition": 4,
            "countryOfOrigin": "SE",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Oatly",
                "names": [
                    {"language": "en", "value": "Oatly Oat Drink 1 l"},
                    {"language": "fi", "value": "Oatly Kaurajuoma 1 l"},
                ],
                "unitConversions": base_1l,
                "classifications": [
                    {"name": "nonAllergen", "type": "ENUM", "values": [{"id": "AM", "unit": "FREE_FROM"}, {"id": "AN", "unit": "FREE_FROM"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "VEGAN", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "type": "NUMBER", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "water, oats, rapeseed oil"}],
            },
        },
        {
            "id": "500202",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "18100",
            "vendorName": "Fria",
            "allowedLotSize": 1.0,
            "temperatureCondition": -18,
            "countryOfOrigin": "SE",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Fria",
                "names": [
                    {"language": "en", "value": "Gluten free white bread 1 kg"},
                    {"language": "fi", "value": "Gluteeniton paahtoleipä 1 kg"},
                ],
                "unitConversions": base_1kg,
                "classifications": [
                    {"name": "nonAllergen", "type": "ENUM", "values": [{"id": "AW", "unit": "FREE_FROM"}, {"id": "AM", "unit": "FREE_FROM"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_GLUTEN", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "type": "NUMBER", "values": [{"id": "FAT", "value": 3.0}]},
                ],
                "keyIngredients": [{"language": "en", "value": "rice starch, water"}],
            },
        },
        {
            "id": "600909",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "60100",
            "vendorName": "SteelCo",
            "allowedLotSize": 1.0,
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "SteelCo",
                "names": [
                    {"language": "en", "value": "Stainless milk jug 1 l"},
                    {"language": "fi", "value": "Teräksinen maitokannu 1 l"},
                ],
                "unitConversions": base_pcs,
                "classifications": [],
            },
        },
    ]


@pytest.fixture
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    pjson = tmp_path / "valio_aimo_junction26_product_data.json"
    cdb = tmp_path / "catalog.sqlite"
    rel_csv = tmp_path / "reliability.csv"
    co2_csv = tmp_path / "co2.csv"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    rel_csv.write_text("product_id,delivery_reliability\n464306,0.98\n500101,0.95\n", encoding="utf-8")
    co2_csv.write_text("category,co2_kg_per_kg\n10400,1.15\n10410,0.45\n", encoding="utf-8")

    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(config, "CO2_CSV", co2_csv)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(catalog_store, "CO2_CSV", co2_csv)
    monkeypatch.setattr(fallback_catalog, "_index", None)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")

    catalog_store.load_catalog(cdb)
    yield tmp_path

    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    monkeypatch.setattr(fallback_catalog, "_index", None)


def _base_offer(**overrides: Any) -> Offer:
    base: dict[str, Any] = {
        "product_id": "464306",
        "requested_date": date(2026, 11, 16),
        "available": False,
        "available_amount": 120.0,
        "available_date": date(2026, 11, 16),
        "closing_time": datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc),
        "cutoff_passed": True,
        "logistic_model": "Stock",
        "sales_block": False,
        "sales_unit": "ST",
        "unit_price": 1.25,
        "fetched_at": NOW_UTC,
        "issues": ["cutoff_passed"],
    }
    base.update(overrides)
    return Offer.model_construct(**base)


def test_textsearch_exposes_reentrant_db_lock_and_per_thread_connections(isolated_catalog: Path) -> None:
    assert hasattr(textsearch, "_db_lock")
    assert isinstance(textsearch._db_lock, type(threading.RLock()))
    with textsearch._db_lock:
        with textsearch._db_lock:
            assert True
    textsearch._con.cache_clear()
    assert textsearch._con.cache_info().currsize == 0
    c_main = textsearch._con()
    assert textsearch._con() is c_main
    assert textsearch._con.cache_info().currsize == 1
    holder: list[Any] = []
    t = threading.Thread(target=lambda: holder.append(textsearch._con()))
    t.start()
    t.join()
    assert len(holder) == 1 and holder[0] is not c_main
    assert textsearch._con.cache_info().currsize == 2
    textsearch._con.cache_clear()
    assert textsearch._con.cache_info().currsize == 0


def test_textsearch_24_thread_concurrent_stress(isolated_catalog: Path) -> None:
    from aimo.catalog.plan import plan_need
    from aimo.models import Need

    workers = 24
    iterations = 15
    barrier = threading.Barrier(workers)
    queries = [
        ("lactose-free milk", ["FREE_FROM_LACTOSE"], None, "L"),
        ("oat drink", None, None, "L"),
        ("gluten free bread", ["FREE_FROM_GLUTEN"], None, "KG"),
        ("milk jug", None, "60100", None),
        ("", ["FREE_FROM_LACTOSE"], None, None),
    ]

    def _offers_fn(ids: list[str], d: date, customer_id: str | None = None) -> dict[str, Offer]:
        return {
            pid: Offer(
                product_id=pid,
                requested_date=d,
                available=True,
                available_amount=500.0,
                available_date=d,
                logistic_model="Stock",
                unit_price=1.2,
                fetched_at=NOW_UTC,
            )
            for pid in ids
        }

    def _worker(tid: int) -> int:
        barrier.wait(timeout=10)
        checked = 0
        for step in range(iterations):
            q, cl, cat_id, cu = queries[(tid + step) % len(queries)]
            if q:
                res = textsearch.search(q, claims=cl, category=cat_id, content_unit=cu, limit=10, semantic=False)
                assert isinstance(res, list)
                assert len(res) >= 1
            fts = textsearch.fts_search(q, claims=cl, category=cat_id, content_unit=cu, limit=10, free_of=["milk"] if q == "oat drink" else None)
            assert isinstance(fts, list)
            assert len(fts) >= 1
            rows = textsearch.get_rows(["464306", "500101", "500202", "", "bad\x00id", "missing"])
            assert {"464306", "500101", "500202"} <= set(rows.keys())
            assert isinstance(textsearch._nonfood_categories(), frozenset)
            if step % 3 == 0:
                need = Need(
                    customer_id="school-tampere",
                    product_query="oat drink",
                    excluded_allergens=["milk", "nuts"],
                    quantity=20.0,
                    unit="L",
                    delivery_by=date(2026, 11, 18),
                    preferred_product_ids=["500101"],
                )
                planned = plan_need(need, offers_fn=_offers_fn)
                assert planned.lines and planned.lines[0].product_id == "500101"
            checked += 1
        return checked

    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = list(pool.map(_worker, range(workers)))
    assert counts == [iterations] * workers


@pytest.mark.parametrize(
    "bad_query",
    [None, 0, 123, True, False, ["milk"], {"q": "milk"}, b"milk", "\x00", "milk\x00", "laktoositon\x00maito"],
)
def test_textsearch_query_parsers_reject_non_str_and_null_bytes(bad_query: Any) -> None:
    assert textsearch.clean_query(bad_query) == ""
    assert textsearch.parse_query(bad_query) == []
    assert textsearch.query_wants_nonfood(bad_query) is False


def test_textsearch_clean_and_parse_query_preserve_valid_semantics() -> None:
    assert textsearch.clean_query("oat drink, pupils have milk and nut allergies") == "oat drink"
    assert textsearch.clean_query("lactose-free milk") == "lactose-free milk"
    assert textsearch.clean_query("milk allergy") == ""
    huge = "milk " * 3000
    assert "milk" in textsearch.clean_query(huge)
    assert textsearch.query_wants_nonfood("stainless milk jug") is True
    assert textsearch.query_wants_nonfood("lactose free milk") is False


@pytest.mark.parametrize("bad_ids", [None, 123, True, False, "464306", b"464306", {"464306": 1}])
def test_get_rows_non_collection_ids_return_empty_dict(isolated_catalog: Path, bad_ids: Any) -> None:
    assert textsearch.get_rows(bad_ids) == {}


def test_get_rows_ignores_bad_items_and_chunks_cleanly(isolated_catalog: Path) -> None:
    mixed: list[Any] = [None, 123, True, False, "", "   ", "\x00", "464306\x00", "x" * 400, ["464306"], "464306", "500101", "464306"]
    rows = textsearch.get_rows(mixed)
    assert set(rows.keys()) == {"464306", "500101"}

    many = [f"missing-{i}" for i in range(620)] + ["464306", "bad\x00", "500202"]
    chunked = textsearch.get_rows(many)
    assert set(chunked.keys()) == {"464306", "500202"}


@pytest.mark.parametrize(
    "bad_query",
    [None, 123, True, False, ["milk"], {"q": "milk"}, "\x00", "milk\x00"],
)
def test_fts_semantic_and_search_reject_bad_queries(isolated_catalog: Path, bad_query: Any) -> None:
    assert textsearch.fts_search(bad_query, claims=["FREE_FROM_LACTOSE"]) == []
    assert textsearch.semantic_hits(bad_query, claims=["FREE_FROM_LACTOSE"]) == []
    assert textsearch.search(bad_query, claims=["FREE_FROM_LACTOSE"], semantic=False) == []


@pytest.mark.parametrize("bad_limit", [True, False, None, "5", [], {}, 0, -1, -5, 0.2, float("nan"), float("-inf")])
def test_textsearch_rejects_bad_or_non_positive_limits_and_pools(isolated_catalog: Path, bad_limit: Any) -> None:
    assert textsearch.fts_search("milk", limit=bad_limit) == []
    assert textsearch.fts_search("milk", limit=5, pool=bad_limit) == []
    assert textsearch.semantic_hits("milk", k=bad_limit) == []
    assert textsearch.search("milk", limit=bad_limit, semantic=False) == []


def test_textsearch_clamps_huge_limits_and_pools(isolated_catalog: Path) -> None:
    fts = textsearch.fts_search("milk", limit=10**12, pool=10**12)
    assert len(fts) >= 1
    fts_inf = textsearch.fts_search("milk", limit=float("inf"), pool=float("inf"))
    assert len(fts_inf) >= 1
    res = textsearch.search("milk", limit=10**12, semantic=False)
    assert len(res) >= 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claims": "FREE_FROM_LACTOSE"},
        {"claims": 123},
        {"claims": True},
        {"claims": [None]},
        {"claims": [123]},
        {"claims": [""]},
        {"claims": ["   "]},
        {"claims": ["FREE_FROM_LACTOSE\x00"]},
        {"category": 10400},
        {"category": True},
        {"category": ["10400"]},
        {"category": "10400\x00"},
        {"content_unit": 1},
        {"content_unit": True},
        {"content_unit": ["L"]},
        {"content_unit": "L\x00"},
    ],
)
def test_textsearch_rejects_malformed_filters(isolated_catalog: Path, kwargs: dict[str, Any]) -> None:
    assert textsearch.fts_search("milk", **kwargs) == []
    assert textsearch.semantic_hits("milk", **kwargs) == []
    assert textsearch.search("milk", semantic=False, **kwargs) == []
    fsql, fargs = textsearch._filter_sql(
        kwargs.get("claims"),
        kwargs.get("category"),
        kwargs.get("content_unit"),
    )
    assert fsql == " and 0 = 1" and fargs == []


@pytest.mark.parametrize("bad_free_of", ["milk", 123, True, [123], [None], ["milk\x00"]])
def test_fts_search_rejects_malformed_free_of(isolated_catalog: Path, bad_free_of: Any) -> None:
    assert textsearch.fts_search("oat drink", free_of=bad_free_of) == []
    fsql, fargs = textsearch._filter_sql([], None, None, bad_free_of)
    assert fsql == " and 0 = 1" and fargs == []


def test_semantic_hits_sanitizes_corrupt_embedding_pairs(isolated_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netcheck, "semantic_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "available", lambda: True)
    monkeypatch.setattr(
        embeddings,
        "semantic_search",
        lambda _q, k=120: [
            None,
            ("bad\x00id", 0.99),
            ("", 0.95),
            (123, 0.9),
            ("464306", float("nan")),
            ("464306", float("inf")),
            ("464306", True),
            ("500101", 0.88),
        ],
    )
    hits = textsearch.semantic_hits("oat drink", k=5)
    assert hits == [("500101", 0.88)]


@pytest.mark.parametrize("bad_float", [float("nan"), float("inf"), float("-inf"), True, False, "bad", [], {}])
def test_summary_from_row_sanitizes_corrupt_floats_and_fields(bad_float: Any) -> None:
    row: dict[str, Any] = {
        "id": "464306",
        "gtin": 12345,
        "name": "Milk\x00Drink",
        "name_fi": ["bad"],
        "brand": True,
        "vendor": {"v": 1},
        "category": 10400,
        "sales_unit": "ST\x00",
        "content_amount": bad_float,
        "content_unit": "INVALID",
        "claims": ["FREE_FROM_LACTOSE", 123, None, "BAD\x00", ""],
        "labels": "NOT_A_LIST",
        "delivery_reliability": bad_float,
        "co2_kg_per_kg": bad_float,
    }
    summary = textsearch.summary_from_row(row, score=bad_float)
    assert isinstance(summary, ProductSummary)
    assert summary.id == "464306"
    assert summary.name == "MilkDrink"
    assert summary.sales_unit == "ST"
    assert summary.gtin is None
    assert summary.brand is None
    assert summary.vendor is None
    assert summary.category is None
    assert summary.content_amount is None
    assert summary.content_unit is None
    assert summary.claims == ["FREE_FROM_LACTOSE"]
    assert summary.labels == []
    assert summary.delivery_reliability is None
    assert summary.co2_kg_per_kg is None
    assert summary.score is None
    assert json.dumps(summary.model_dump(), allow_nan=False)


def test_summary_from_row_bounds_and_invalid_rows() -> None:
    s = textsearch.summary_from_row(
        {
            "id": "500101",
            "name": "Oatly",
            "sales_unit": "ST",
            "content_amount": -2.0,
            "content_unit": "L",
            "delivery_reliability": 1.5,
            "co2_kg_per_kg": -0.3,
        },
        score=-3.25,
    )
    assert s.content_amount is None
    assert s.delivery_reliability is None
    assert s.co2_kg_per_kg is None
    assert s.score == -3.25

    for bad_row in (None, 123, [], {}, {"id": None}, {"id": True}, {"id": "   "}, {"id": "\x00"}):
        with pytest.raises(ValueError):
            textsearch.summary_from_row(bad_row)

    assert textsearch.food_class(None) == "unknown"
    assert textsearch.food_class({"category": ["unhashable"], "name_en": 123, "name_fi": True}) == "unknown"


@pytest.mark.parametrize("bad_offer", [None, True, False, 0, "offer", [], {}])
def test_earliest_feasible_rejects_non_offer_inputs(bad_offer: Any) -> None:
    assert delivery.earliest_feasible(bad_offer, NOW_UTC) == (None, False)


@pytest.mark.parametrize("bad_price", [None, True, False, 0, 0.0, -0.0, -2.5, float("nan"), float("inf"), float("-inf"), "1.25"])
def test_earliest_feasible_rejects_bad_unit_price(bad_price: Any) -> None:
    o = _base_offer(unit_price=bad_price, cutoff_passed=False, available=True)
    assert delivery.earliest_feasible(o, NOW_UTC) == (None, False)


@pytest.mark.parametrize("bad_amount", [True, False, 0, 0.0, -0.0, -5.0, float("nan"), float("-inf"), "10"])
def test_earliest_feasible_rejects_bad_available_amount(bad_amount: Any) -> None:
    o = _base_offer(available_amount=bad_amount, cutoff_passed=False, available=True)
    assert delivery.earliest_feasible(o, NOW_UTC) == (None, False)


def test_earliest_feasible_accepts_none_and_plus_inf_available_amount() -> None:
    for amt in (None, 25.0, float("inf")):
        o = _base_offer(available_amount=amt, cutoff_passed=False, available=True)
        assert delivery.earliest_feasible(o, NOW_UTC) == (date(2026, 11, 16), False)


def test_earliest_feasible_normalizes_datetime_available_date_and_mixed_timezones() -> None:
    dt_avail = datetime(2026, 11, 16, 8, 30, tzinfo=timezone.utc)
    o_ok = _base_offer(available_date=dt_avail, cutoff_passed=False, available=True)
    d_ok, est_ok = delivery.earliest_feasible(o_ok, NOW_UTC)
    assert type(d_ok) is date and d_ok == date(2026, 11, 16) and est_ok is False

    o_late = _base_offer(available_date=dt_avail, cutoff_passed=True)
    d_late, est_late = delivery.earliest_feasible(o_late, NOW_UTC)
    assert type(d_late) is date and d_late == date(2026, 11, 19) and est_late is True

    naive_now = datetime(2026, 11, 14, 20, 0)
    naive_ct = datetime(2026, 11, 12, 15, 0)
    broken_now = datetime(2026, 11, 14, 20, 0, tzinfo=_BrokenTz())
    helsinki_now = datetime(2026, 11, 14, 22, 0, tzinfo=HELSINKI)
    for now_arg in (naive_now, NOW_UTC, broken_now, helsinki_now, date(2026, 11, 14)):
        for ct_arg in (naive_ct, datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc), date(2026, 11, 12), None):
            cand = _base_offer(available_date=dt_avail, closing_time=ct_arg, cutoff_passed=True)
            d_res, est_res = delivery.earliest_feasible(cand, now_arg)
            assert type(d_res) is date and d_res.weekday() != 6 and est_res is True


@pytest.mark.parametrize("bad_now", [True, False, "2026-11-14", 12345, [], {}])
def test_earliest_feasible_rejects_invalid_now_and_date_overflow(bad_now: Any) -> None:
    o = _base_offer(cutoff_passed=True)
    assert delivery.earliest_feasible(o, bad_now) == (None, False)
    assert delivery.earliest_feasible(o, datetime(9999, 12, 31, 23, 0, tzinfo=timezone.utc)) == (None, False)
    o_extreme_ct = _base_offer(closing_time=datetime(9999, 12, 31, 23, 30, tzinfo=timezone.utc), cutoff_passed=True)
    d_ct, est_ct = delivery.earliest_feasible(o_extreme_ct, datetime(2026, 11, 14, 22, 0, tzinfo=HELSINKI))
    assert type(d_ct) is date and est_ct is True


def test_late_offer_and_fmt_normalize_datetime_reject_bad_dates_and_dedupe_issues() -> None:
    o = _base_offer(issues=["cutoff_passed", "estimated_date", "estimated_date"])
    dt = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    updated = delivery.late_offer(o, dt)
    assert type(updated.available_date) is date and updated.available_date == date(2026, 11, 19)
    assert type(updated.requested_date) is date and updated.requested_date == date(2026, 11, 19)
    assert updated.available is True and updated.cutoff_passed is False
    assert updated.issues == ["cutoff_passed", "estimated_date"]

    updated_twice = delivery.late_offer(updated, date(2026, 11, 20))
    assert updated_twice.issues.count("estimated_date") == 1
    assert delivery.fmt(dt) == "Thu 19 Nov"
    assert delivery.fmt(date(2026, 11, 16)) == "Mon 16 Nov"

    for bad_d in (True, False, None, "2026-11-19", 20261119, [], {}):
        with pytest.raises(ValueError):
            delivery.fmt(bad_d)
        with pytest.raises(ValueError):
            delivery.late_offer(o, bad_d)

    with pytest.raises(ValueError):
        delivery.late_offer(None, date(2026, 11, 19))


@pytest.mark.parametrize("raw_content", ["{corrupt json", "{}", '"not-a-list"', "123", "true", "null"])
def test_fallback_catalog_load_handles_corrupt_or_non_list_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_content: str
) -> None:
    pjson = tmp_path / "products.json"
    pjson.write_text(raw_content, encoding="utf-8")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(fallback_catalog, "_index", None)
    assert fallback_catalog._load() == {}
    assert fallback_catalog.search("milk") == []
    assert fallback_catalog.get_product("464306") is None


def test_fallback_catalog_load_handles_oserror_and_binary_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PRODUCT_JSON", tmp_path)
    monkeypatch.setattr(fallback_catalog, "_index", None)
    assert fallback_catalog._load() == {}

    bad_bytes = tmp_path / "bad.json"
    bad_bytes.write_bytes(b"\xff\xfe\x00\x01")
    monkeypatch.setattr(config, "PRODUCT_JSON", bad_bytes)
    monkeypatch.setattr(fallback_catalog, "_index", None)
    assert fallback_catalog._load() == {}


def test_fallback_catalog_sanitizes_hostile_items_and_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        "["
        'null, 123, "str", [], '
        '{"deleted": true, "id": "del-1"}, '
        '{"id": ""}, {"id": "   "}, {"id": true}, {"id": "bad\\u0000id"}, '
        '{"id": "p-nondict-synkka", "synkkaData": "bad", "salesUnit": 123, "allowedLotSize": -2, "temperatureCondition": true}, '
        '{"id": "p-nonlist-names", "synkkaData": {"names": "bad", "brand": ["not-str"]}, "allowedLotSize": NaN, "temperatureCondition": "cold"}, '
        '{"id": "p-inf-lot", "synkkaData": {"names": [123, {"language": "en", "value": "Clean Milk 1 L"}], "brand": "Valio"}, "allowedLotSize": Infinity, "temperatureCondition": 4.0}, '
        '{"id": "p-valid-lot", "synkkaData": {"names": [{"language": "fi", "value": "Kaurajuoma 1 L"}], "brand": "Oatly"}, "allowedLotSize": 0.5, "temperatureCondition": 2}'
        "]"
    )
    pjson = tmp_path / "hostile_products.json"
    pjson.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(fallback_catalog, "_index", None)

    idx = fallback_catalog._load()
    assert set(idx.keys()) == {"p-nondict-synkka", "p-nonlist-names", "p-inf-lot", "p-valid-lot"}

    p1 = fallback_catalog.get_product("p-nondict-synkka")
    assert isinstance(p1, Product)
    assert p1.sales_unit == "ST"
    assert p1.allowed_lot_size is None
    assert p1.temperature_condition is None

    p2 = fallback_catalog.get_product("p-nonlist-names")
    assert isinstance(p2, Product)
    assert p2.brand is None
    assert p2.allowed_lot_size is None
    assert p2.temperature_condition is None

    p3 = fallback_catalog.get_product("p-inf-lot")
    assert isinstance(p3, Product)
    assert p3.name == "Clean Milk 1 L"
    assert p3.brand == "Valio"
    assert p3.allowed_lot_size is None
    assert p3.temperature_condition == 4

    p4 = fallback_catalog.get_product("p-valid-lot")
    assert isinstance(p4, Product)
    assert p4.allowed_lot_size == 0.5
    assert p4.temperature_condition == 2
    assert json.dumps(p4.model_dump(), allow_nan=False)


def test_fallback_catalog_search_and_get_product_guard_bad_inputs_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = [
        {
            "id": f"prod-{i:03d}",
            "category": "10400",
            "synkkaData": {"brand": "Valio", "names": [{"language": "en", "value": f"Valio milk item {i}"}]},
        }
        for i in range(230)
    ]
    pjson = tmp_path / "many_products.json"
    pjson.write_text(json.dumps(many), encoding="utf-8")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(fallback_catalog, "_index", None)

    for bad_q in (None, 123, True, False, ["milk"], "\x00", "milk\x00", "   "):
        assert fallback_catalog.search(bad_q) == []

    for bad_lim in (True, False, None, "10", [], 0, -1, -5, 0.5, float("nan"), float("-inf")):
        assert fallback_catalog.search("milk", limit=bad_lim) == []

    assert len(fallback_catalog.search("milk", limit=250)) == 200
    assert len(fallback_catalog.search("milk", limit=10**9)) == 200
    assert len(fallback_catalog.search("milk", limit=float("inf"))) == 200
    assert len(fallback_catalog.search("milk", limit=5)) == 5

    assert fallback_catalog.search("milk", category="10400\x00") == []
    assert fallback_catalog.search("milk", category=10400) == []
    assert fallback_catalog.search("milk", claims="ORGANIC") == []
    assert fallback_catalog.search("milk", claims=["ORGANIC\x00"]) == []

    for bad_pid in (None, 464306, True, False, "", "   ", "prod-001\x00", "\x00"):
        assert fallback_catalog.get_product(bad_pid) is None
    assert fallback_catalog.get_product("prod-001") is not None


def test_gateway_v1_products_null_byte_queries_never_500(isolated_catalog: Path) -> None:
    client = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    for path in (
        "/v1/products?q=milk%00",
        "/v1/products?q=milk&category=10400%00",
        "/v1/products?q=milk&content_unit=L%00",
        "/v1/products?q=milk&claim=FREE_FROM_LACTOSE%00",
        "/v1/products/464306%00",
    ):
        resp = client.get(path)
        assert resp.status_code < 500, (path, resp.status_code, resp.text)


def test_owned_files_hygiene_zero_em_dashes_and_test_zero_comments() -> None:
    em_dash = chr(0x2014)
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "aimo/catalog/textsearch.py",
        "aimo/catalog/delivery.py",
        "aimo/gateway/fallback_catalog.py",
        "tests/test_textsearch_delivery_fallback_inputs.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert em_dash not in text, f"Em dash found in {rel}"

    self_text = Path(__file__).read_text(encoding="utf-8")
    comments = [tok.string for tok in tokenize.generate_tokens(io.StringIO(self_text).readline) if tok.type == tokenize.COMMENT]
    assert comments == []
