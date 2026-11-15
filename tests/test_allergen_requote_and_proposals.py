from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.agents import seller
from aimo.backend import catalog_store, clock
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import get_product, textsearch
from aimo.gateway import human_auth, resilience, service
from aimo.gateway.app import create_app
from aimo.models import EU14, Need
from aimo.trust import learned
from aimo.trust import policy as trust_policy

DELIVERY = date(2026, 11, 16)
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}
PIN_HEADER = {"X-Approver-Pin": "test-pin"}
SCHOOL_APPROVER = "liisa@school-tampere.example"
SCHOOL_DIRECTOR = "jari@school-tampere.example"
HOSPITAL_APPROVER = "sara@hospital-meilahti.example"


def _synkka_item(
    pid: str,
    name_en: str,
    brand: str,
    category: str,
    unit_kind: str,
    unit_amount: float,
    allergen_cls: list[tuple[str, str]],
    claims: list[str] | None = None,
    ingredients: str = "water, oats",
    fat: float = 1.5,
) -> dict[str, Any]:
    classifications: list[dict[str, Any]] = []
    con_vals = [{"id": code, "unit": status} for code, status in allergen_cls if status in ("CONTAINS", "MAY_CONTAIN")]
    free_vals = [{"id": code, "unit": "FREE_FROM"} for code, status in allergen_cls if status == "FREE_FROM"]
    if con_vals:
        classifications.append({"name": "allergen", "type": "ENUM", "values": con_vals})
    if free_vals:
        classifications.append({"name": "nonAllergen", "type": "ENUM", "values": free_vals})
    if claims:
        classifications.append(
            {
                "name": "nutritionalClaim",
                "type": "ENUM",
                "values": [{"id": cl, "unit": "FREE_FROM"} for cl in claims],
            }
        )
    classifications.append({"name": "nutritionFact", "values": [{"id": "FAT", "value": fat}]})
    if unit_kind == "L":
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": unit_amount}}]
    else:
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": unit_amount}}]
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": brand,
        "allowedLotSize": 0.01,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": [{"language": "en", "value": name_en}],
            "unitConversions": conv,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "marketingTexts": [{"language": "en", "value": f"{name_en} for foodservice kitchens."}],
        },
    }


def _catalog_items() -> list[dict[str, Any]]:
    return [
        _synkka_item(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats 10%, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "470863",
            "Oatly Oat Drink Semi 1L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats 10%, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "479001",
            "Kaslink Safe Oat Drink 1 L UHT",
            "Kaslink",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, Finnish oats, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "464306",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "469363",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "460396",
            "Valio skimmed milk 10 l novobox",
            "Valio",
            "10400",
            "L",
            10.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            ingredients="milk, vitamin D",
            fat=0.1,
        ),
        _synkka_item(
            "471424",
            "Oddlygood Barista oat drink 1 L hazelnut UHT",
            "Oddlygood",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "CONTAINS"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
            ingredients="water, gluten-free oats, hazelnut paste",
            fat=1.5,
        ),
        _synkka_item(
            "471425",
            "Trace Dairy Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "MAY_CONTAIN"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "471426",
            "Trace Nut Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "MAY_CONTAIN"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472833",
            "Fazer Aito oat drink 1 L UHT",
            "Fazer",
            "10400",
            "L",
            1.0,
            [],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472899",
            "Inferred Free Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "473328",
            "Schar Vital dark sliced bread 350 g gluten free",
            "Schar",
            "14100",
            "KG",
            0.35,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, sourdough, maize starch, flaxseed",
            fat=3.0,
        ),
        _synkka_item(
            "461765",
            "Moilas Gluten-free Dark Bread 600g sliced frozen",
            "Moilas",
            "14100",
            "KG",
            0.6,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, rice flour, buckwheat, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "473399",
            "Schar Vital dark sliced bread 500 g gluten free",
            "Schar",
            "14100",
            "KG",
            0.5,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, sourdough, maize starch, flaxseed",
            fat=3.0,
        ),
        _synkka_item(
            "501100",
            "Vaasan Ruispalat rye bread 500 g",
            "Vaasan",
            "14100",
            "KG",
            0.5,
            [("AW", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            ingredients="wholegrain rye, water, wheat gluten, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501102",
            "Bakery Dark Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [("AW", "MAY_CONTAIN"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="maize starch, water, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501101",
            "Artisan Country Dark Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [],
            ingredients="flour, water, salt, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501199",
            "Inferred Dark Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="rice flour, water, salt",
            fat=3.0,
        ),
        _synkka_item(
            "502200",
            "Myllyn Paras wheat flour 2 kg",
            "Myllyn Paras",
            "14200",
            "KG",
            2.0,
            [("AW", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            ingredients="wheat flour",
            fat=1.5,
        ),
        _synkka_item(
            "462775",
            "Arla Milkdrink 1L lactose free fat free UHT",
            "Arla",
            "10400",
            "L",
            1.0,
            [("AW", "FREE_FROM"), ("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="skimmed milk, vitamin D",
            fat=0.1,
        ),
        _synkka_item(
            "462776",
            "Valio Milkdrink 1L lactose free fat free UHT",
            "Valio",
            "10400",
            "L",
            1.0,
            [("AW", "FREE_FROM"), ("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="skimmed milk, vitamin D",
            fat=0.1,
        ),
        _synkka_item(
            "462780",
            "Cereal Milkdrink 1L lactose free fat free UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AW", "MAY_CONTAIN"), ("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="skimmed milk, cereal extract",
            fat=0.1,
        ),
        _synkka_item(
            "462781",
            "Unverified Milkdrink 1L lactose free fat free UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="skimmed milk",
            fat=0.1,
        ),
    ]


class _ConfigurableBackendClient:
    base_url = "fixture://requote-proposals-mock"
    ttl_s = 300

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._units: dict[str, tuple[str, float]] = {}
        for it in items:
            pid = it["id"]
            conv = it["synkkaData"]["unitConversions"][0]
            if "volume" in conv:
                self._units[pid] = ("LTR", float(conv["volume"]["value"]))
            else:
                self._units[pid] = ("KGM", float(conv["netWeight"]["value"]))
        self.prices: dict[str, float] = {pid: 1.80 for pid in self._units}
        self.stock: dict[str, float] = {pid: 5000.0 for pid in self._units}
        self.closing_times: dict[str, str] = {pid: "2026-11-15T17:00:00+0200" for pid in self._units}
        self.sales_blocks: set[str] = set()
        self.stats = {"upstream_calls": 0}

    def _time_now_iso(self) -> str:
        return clock.now().astimezone(timezone(timedelta(hours=2))).strftime("%Y-%m-%dT%H:%M:%S%z")

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._time_now_iso()}
        products = []
        for pid in ids:
            if pid not in self._units:
                continue
            cunit, camt = self._units[pid]
            up = self.prices[pid]
            cmp_val = round(up / camt, 4) if camt else up
            products.append(
                {
                    "productId": pid,
                    "salesUnit": "ST",
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "comparisonUnit": cunit,
                    "catalogPrice": {"value": up, "currency": "EUR"},
                    "comparisonPrice": {"value": cmp_val, "unit": cunit},
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}

    def availability(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._time_now_iso()}
        products = []
        for pid in ids:
            if pid not in self._units:
                continue
            sb = pid in self.sales_blocks
            products.append(
                {
                    "productId": pid,
                    "closingTime": self.closing_times[pid],
                    "salesBlock": sb,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": sb,
                            "availableDate": requested_date.isoformat(),
                            "availableAmount": self.stock[pid],
                            "salesUnit": "ST",
                        }
                    ],
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}


def _reset_breakers() -> None:
    for b in resilience._breakers.values():
        b.reset()


def _seed_inferred_rows(cdb: Path) -> None:
    textsearch._con.cache_clear()
    catalog_store.load_catalog(cdb)
    con = sqlite3.connect(str(cdb))
    try:
        oat_allergens = {g: ("free" if g in ("milk", "nuts") else "unknown") for g in EU14}
        oat_sources = {g: ("inferred:gemini" if g in ("milk", "nuts") else "none") for g in EU14}
        con.execute(
            "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
            (json.dumps(oat_allergens), json.dumps(oat_sources), "472899"),
        )
        bread_allergens = {g: ("free" if g in ("gluten", "milk", "nuts") else "unknown") for g in EU14}
        bread_sources = {
            g: ("inferred:gemini" if g == "gluten" else ("structured" if g in ("milk", "nuts") else "none"))
            for g in EU14
        }
        con.execute(
            "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
            (json.dumps(bread_allergens), json.dumps(bread_sources), "501199"),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture(scope="module")
def built_catalog(tmp_path_factory: pytest.TempPathFactory):
    cat_dir = tmp_path_factory.mktemp("requote_catalog")
    items = _catalog_items()
    pjson = cat_dir / "valio_aimo_junction26_product_data.json"
    cdb = cat_dir / "catalog.sqlite"
    key_dir = cat_dir / "keys"
    pjson.write_text(json.dumps(items), encoding="utf-8")

    orig_pjson = catalog_store.PRODUCT_JSON
    orig_cdb = catalog_store.CATALOG_DB
    orig_rel = catalog_store.RELIABILITY_CSV
    orig_co2 = catalog_store.CO2_CSV
    catalog_store.PRODUCT_JSON = pjson
    catalog_store.CATALOG_DB = cdb
    catalog_store.RELIABILITY_CSV = cat_dir / "missing_rel.csv"
    catalog_store.CO2_CSV = cat_dir / "missing_co2.csv"
    try:
        _seed_inferred_rows(cdb)
    finally:
        catalog_store.PRODUCT_JSON = orig_pjson
        catalog_store.CATALOG_DB = orig_cdb
        catalog_store.RELIABILITY_CSV = orig_rel
        catalog_store.CO2_CSV = orig_co2
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    with TestClient(create_app(with_mcp=False)) as client_app:
        yield pjson, cdb, key_dir, items, client_app
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def requote_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    built_catalog: tuple[Path, Path, Path, list[dict[str, Any]], TestClient],
):
    pjson, cdb, key_dir, items, api = built_catalog
    state_db = tmp_path / "state.sqlite"

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-pin")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])

    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    trust_policy._load_policy_cached.cache_clear()
    human_auth.reset_for_tests()
    _reset_breakers()
    client = _ConfigurableBackendClient(items)
    bc.set_client(client)
    try:
        yield api, client
    finally:
        bc.set_client(None)
        human_auth.reset_for_tests()
        _reset_breakers()
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        trust_policy._load_policy_cached.cache_clear()


def _assert_only_structured_free(lines: list[dict[str, Any]], excluded: tuple[str, ...]) -> None:
    assert lines, "expected at least one replacement line"
    for ln in lines:
        prod = get_product(ln["product_id"])
        assert prod is not None
        assert "allergen_unknown" not in ln.get("flags", [])
        for g in excluded:
            st = prod.allergens.get(g)
            val = st.value if hasattr(st, "value") else st
            assert val == "free", (ln["product_id"], g, val)
            src = prod.allergen_source.get(g) or ""
            assert src.startswith("structured"), (ln["product_id"], g, src)


@pytest.mark.parametrize("trigger", ["quote_expired", "line_unavailable", "cutoff_passed"])
def test_requote_school_tampere_replan_selects_only_structured_free(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
    trigger: str,
) -> None:
    api, backend = requote_env
    for unsafe_id in ("464306", "469363", "460396", "471424", "471425", "471426", "472833", "472899"):
        backend.prices[unsafe_id] = 0.25
        backend.stock[unsafe_id] = 9000.0
    backend.prices["470863"] = 1.90
    backend.prices["470889"] = 2.10
    backend.prices["479001"] = 2.20

    need_payload = {
        "customer_id": "school-tampere",
        "product_query": "oat drink",
        "quantity": 40,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }
    rq = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "need": need_payload,
            "lines": [{"product_id": "470863", "qty_sales_units": 40}],
        },
    )
    assert rq.status_code == 200, rq.text
    quote = rq.json()
    assert quote["need"]["excluded_allergens"] == []

    ro = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert ro.status_code == 200, ro.text
    order = ro.json()
    assert order["status"] == "pending_approval"

    decide_headers = dict(PIN_HEADER)
    if trigger == "quote_expired":
        backend.stock["470863"] = 0.0
        decide_headers["X-Aimo-Now"] = "2026-11-14T14:25:00+02:00"
    elif trigger == "line_unavailable":
        backend.stock["470863"] = 0.0
    else:
        backend.closing_times["470863"] = "2026-11-14T13:30:00+0200"

    rd = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=decide_headers,
        json={
            "decision": "approve",
            "approver": SCHOOL_APPROVER,
            "quote_hash": quote["quote_hash"],
        },
    )
    assert rd.status_code == 409, rd.text
    err = rd.json()
    assert err["error"] == "requote_needed"
    detail = err["detail"]
    assert detail["reason"] == trigger
    assert "new_quote" in detail and "new_order" in detail

    new_quote = detail["new_quote"]
    new_order = detail["new_order"]
    _assert_only_structured_free(new_quote["lines"], ("milk", "nuts"))
    replacement_ids = {ln["product_id"] for ln in new_quote["lines"]}
    assert replacement_ids <= {"470889", "479001"}
    assert replacement_ids.isdisjoint({"464306", "469363", "460396", "471424", "471425", "471426", "472833", "472899"})
    assert new_order["status"] == "pending_approval"
    assert new_order["decision"]["outcome"] != "reject"

    approve_headers = dict(PIN_HEADER)
    if trigger == "quote_expired":
        approve_headers["X-Aimo-Now"] = "2026-11-14T14:26:00+02:00"
    ra = api.post(
        detail["approve_with"]["path"],
        headers=approve_headers,
        json={**detail["approve_with"]["body"], "approver": SCHOOL_DIRECTOR},
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["status"] == "placed"


@pytest.mark.parametrize("trigger", ["quote_expired", "line_unavailable", "cutoff_passed"])
def test_requote_hospital_meilahti_replan_selects_only_structured_free(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
    trigger: str,
) -> None:
    api, backend = requote_env
    for unsafe_id in ("501100", "501102", "501101", "501199", "502200"):
        backend.prices[unsafe_id] = 0.20
        backend.stock[unsafe_id] = 9000.0
    backend.prices["461765"] = 2.40
    backend.prices["473328"] = 2.60
    backend.prices["473399"] = 2.70

    need_payload = {
        "customer_id": "hospital-meilahti",
        "product_query": "dark bread",
        "quantity": 6.0,
        "unit": "KG",
        "delivery_by": DELIVERY.isoformat(),
    }
    rq = api.post(
        "/v1/quotes",
        headers=H_HOSPITAL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "need": need_payload,
            "lines": [{"product_id": "461765", "qty_sales_units": 10}],
        },
    )
    assert rq.status_code == 200, rq.text
    quote = rq.json()
    assert quote["need"]["excluded_allergens"] == []

    ro = api.post(
        "/v1/orders",
        headers=H_HOSPITAL,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert ro.status_code == 200, ro.text
    order = ro.json()
    assert order["status"] == "pending_approval"

    decide_headers = dict(PIN_HEADER)
    if trigger == "quote_expired":
        backend.stock["461765"] = 0.0
        decide_headers["X-Aimo-Now"] = "2026-11-14T14:25:00+02:00"
    elif trigger == "line_unavailable":
        backend.stock["461765"] = 0.0
    else:
        backend.closing_times["461765"] = "2026-11-14T13:30:00+0200"

    rd = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=decide_headers,
        json={
            "decision": "approve",
            "approver": HOSPITAL_APPROVER,
            "quote_hash": quote["quote_hash"],
        },
    )
    assert rd.status_code == 409, rd.text
    err = rd.json()
    assert err["error"] == "requote_needed"
    detail = err["detail"]
    assert detail["reason"] == trigger
    assert "new_quote" in detail and "new_order" in detail

    new_quote = detail["new_quote"]
    new_order = detail["new_order"]
    _assert_only_structured_free(new_quote["lines"], ("gluten",))
    replacement_ids = {ln["product_id"] for ln in new_quote["lines"]}
    assert replacement_ids <= {"473328", "473399"}
    assert replacement_ids.isdisjoint({"501100", "501102", "501101", "501199", "502200"})
    assert new_order["status"] == "pending_approval"
    assert new_order["decision"]["outcome"] != "reject"


@pytest.mark.parametrize(
    "customer,headers,approver,initial_pid,initial_qty,query,unit,safe_pids",
    [
        (
            "school-tampere",
            H_SCHOOL,
            SCHOOL_APPROVER,
            "470863",
            30,
            "oat drink",
            "L",
            ("470863", "470889", "479001"),
        ),
        (
            "hospital-meilahti",
            H_HOSPITAL,
            HOSPITAL_APPROVER,
            "461765",
            10,
            "dark bread",
            "KG",
            ("461765", "473328", "473399"),
        ),
    ],
)
@pytest.mark.parametrize("trigger", ["quote_expired", "line_unavailable", "cutoff_passed"])
def test_requote_no_safe_substitute_in_stock_omits_new_quote(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
    customer: str,
    headers: dict[str, str],
    approver: str,
    initial_pid: str,
    initial_qty: int,
    query: str,
    unit: str,
    safe_pids: tuple[str, ...],
    trigger: str,
) -> None:
    api, backend = requote_env
    for pid in backend.prices:
        if pid not in safe_pids:
            backend.prices[pid] = 0.15
            backend.stock[pid] = 9000.0

    rq = api.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "need": {
                "customer_id": customer,
                "product_query": query,
                "quantity": 6.0 if unit == "KG" else 30.0,
                "unit": unit,
                "delivery_by": DELIVERY.isoformat(),
            },
            "lines": [{"product_id": initial_pid, "qty_sales_units": initial_qty}],
        },
    )
    assert rq.status_code == 200, rq.text
    quote = rq.json()

    ro = api.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert ro.status_code == 200, ro.text
    order = ro.json()
    assert order["status"] == "pending_approval"

    decide_headers = dict(PIN_HEADER)
    for spid in safe_pids:
        if trigger == "cutoff_passed":
            backend.closing_times[spid] = "2026-11-14T13:30:00+0200"
        else:
            backend.stock[spid] = 0.0
    if trigger == "quote_expired":
        decide_headers["X-Aimo-Now"] = "2026-11-14T14:25:00+02:00"

    rd = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=decide_headers,
        json={
            "decision": "approve",
            "approver": approver,
            "quote_hash": quote["quote_hash"],
        },
    )
    assert rd.status_code == 409, rd.text
    err = rd.json()
    assert err["error"] == "requote_needed"
    detail = err["detail"]
    assert detail["reason"] == trigger
    assert "new_quote" not in detail
    assert "new_order" not in detail

    expired_order = api.get(f"/v1/orders/{order['order_id']}").json()
    assert expired_order["status"] == "expired"
    all_orders = store.list_orders(None, customer)
    assert [o.order_id for o in all_orders] == [order["order_id"]]


def test_requote_deterministic_fallback_still_blocks_unsafe_substitutes(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, backend = requote_env
    import aimo.catalog as cat

    rq = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "need": {
                "customer_id": "school-tampere",
                "product_query": "oat drink",
                "quantity": 25,
                "unit": "L",
                "delivery_by": DELIVERY.isoformat(),
            },
            "lines": [{"product_id": "470863", "qty_sales_units": 25}],
        },
    )
    quote = rq.json()
    order = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    ).json()
    assert order["status"] == "pending_approval"

    monkeypatch.setattr(cat, "plan_need", lambda _need: (_ for _ in ()).throw(TimeoutError("planner timeout")))
    _reset_breakers()
    backend.stock["470863"] = 0.0
    backend.prices["464306"] = 0.10
    backend.prices["472833"] = 0.10
    backend.prices["470889"] = 1.95

    rd = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=PIN_HEADER,
        json={"decision": "approve", "approver": SCHOOL_APPROVER, "quote_hash": quote["quote_hash"]},
    )
    assert rd.status_code == 409, rd.text
    detail = rd.json()["detail"]
    assert "new_quote" in detail
    _assert_only_structured_free(detail["new_quote"]["lines"], ("milk", "nuts"))

    for spid in ("470889", "479001"):
        backend.stock[spid] = 0.0
    _reset_breakers()
    new_oid = detail["new_order"]["order_id"]
    new_qh = detail["new_quote"]["quote_hash"]
    rd_none = api.post(
        f"/v1/approvals/{new_oid}/decision",
        headers=PIN_HEADER,
        json={"decision": "approve", "approver": SCHOOL_APPROVER, "quote_hash": new_qh},
    )
    assert rd_none.status_code == 409, rd_none.text
    assert "new_quote" not in rd_none.json()["detail"]
    assert "new_order" not in rd_none.json()["detail"]


def test_proactive_seller_find_cheaper_and_propose_offer_never_propose_unsafe_items(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
) -> None:
    api, backend = requote_env
    for unsafe_id in (
        "464306",
        "469363",
        "460396",
        "471424",
        "471425",
        "471426",
        "472833",
        "472899",
        "501100",
        "501102",
        "501101",
        "501199",
        "502200",
        "462780",
        "462781",
    ):
        backend.prices[unsafe_id] = 0.05
        backend.stock[unsafe_id] = 9000.0

    backend.prices["470889"] = 3.50
    backend.prices["470863"] = 3.20
    backend.prices["479001"] = 1.60

    backend.prices["473328"] = 3.50
    backend.prices["461765"] = 4.80
    backend.prices["473399"] = 2.00
    backend.prices["462775"] = 2.80
    backend.prices["462776"] = 1.50

    school_props, _ = seller.find_cheaper("school-tampere", DELIVERY)
    assert school_props
    for item in school_props:
        prod = get_product(item["product"].id)
        assert prod is not None
        for g in ("milk", "nuts"):
            assert prod.allergens[g].value == "free"
            assert prod.allergen_source[g].startswith("structured")

    r_school_prop = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "school-tampere", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_school_prop.status_code == 200, r_school_prop.text
    sp = r_school_prop.json()
    assert sp["kind"] == "cheaper_equivalent"
    _assert_only_structured_free([{"product_id": sp["product"]["product_id"], "flags": []}], ("milk", "nuts"))

    hosp_props, _ = seller.find_cheaper("hospital-meilahti", DELIVERY)
    assert hosp_props
    for item in hosp_props:
        prod = get_product(item["product"].id)
        assert prod is not None
        assert prod.allergens["gluten"].value == "free"
        assert prod.allergen_source["gluten"].startswith("structured")

    r_hosp_prop = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hospital-meilahti", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_hosp_prop.status_code == 200, r_hosp_prop.text
    hp = r_hosp_prop.json()
    assert hp["kind"] == "cheaper_equivalent"
    _assert_only_structured_free([{"product_id": hp["product"]["product_id"], "flags": []}], ("gluten",))

    backend.stock["479001"] = 0.0
    backend.prices["470863"] = 3.50
    backend.prices["470889"] = 3.50
    school_none, _ = seller.find_cheaper("school-tampere", DELIVERY)
    assert school_none == []
    r_school_none = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "school-tampere", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_school_none.status_code == 404
    assert r_school_none.json()["error"] == "no_better_offer"

    backend.stock["473399"] = 0.0
    backend.stock["462776"] = 0.0
    backend.prices["473328"] = 3.50
    backend.prices["461765"] = 6.00
    hosp_none, _ = seller.find_cheaper("hospital-meilahti", DELIVERY)
    assert hosp_none == []
    r_hosp_none = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hospital-meilahti", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_hosp_none.status_code == 404
    assert r_hosp_none.json()["error"] == "no_better_offer"


@pytest.mark.parametrize(
    "customer,buyer_headers,product_id,expected_allergen_word",
    [
        ("school-tampere", H_SCHOOL, "464306", "milk"),
        ("school-tampere", H_SCHOOL, "460396", "milk"),
        ("school-tampere", H_SCHOOL, "471424", "nuts"),
        ("school-tampere", H_SCHOOL, "471425", "milk"),
        ("school-tampere", H_SCHOOL, "471426", "nuts"),
        ("school-tampere", H_SCHOOL, "472833", "unknown"),
        ("hospital-meilahti", H_HOSPITAL, "501100", "gluten"),
        ("hospital-meilahti", H_HOSPITAL, "502200", "gluten"),
        ("hospital-meilahti", H_HOSPITAL, "501102", "gluten"),
    ],
)
def test_seller_pick_unsafe_proposal_previews_reject_and_buyer_accept_rejects_order(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
    customer: str,
    buyer_headers: dict[str, str],
    product_id: str,
    expected_allergen_word: str,
) -> None:
    api, _ = requote_env

    rp = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={
            "customer_id": customer,
            "product_id": product_id,
            "qty_sales_units": 4,
            "delivery_date": DELIVERY.isoformat(),
            "note": "special promo",
        },
    )
    assert rp.status_code == 200, rp.text
    prop = rp.json()
    assert prop["kind"] == "seller_pick"
    assert prop["seller_can_order"] is False
    assert prop["policy_preview"]["outcome"] == "reject"
    preview_allergen_hits = [
        h for h in prop["policy_preview"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]
    assert preview_allergen_hits
    assert expected_allergen_word in preview_allergen_hits[0]["message"].lower()

    r_seller_accept = api.post(
        f"/v1/proposals/{prop['proposal_id']}/accept",
        headers={"X-Customer-Id": customer, **H_SELLER},
    )
    assert r_seller_accept.status_code == 403
    assert r_seller_accept.json()["error"] == "seller_cannot_order"

    ra = api.post(f"/v1/proposals/{prop['proposal_id']}/accept", headers=buyer_headers)
    assert ra.status_code == 200, ra.text
    accepted = ra.json()
    assert accepted["quote"]["lines"][0]["tier"] == "rejected"
    assert accepted["order"]["status"] == "rejected"
    assert accepted["order"]["decision"]["outcome"] == "reject"
    assert accepted["proposal"]["order_status"] == "rejected"

    order_allergen_hits = [
        h for h in accepted["order"]["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]
    assert order_allergen_hits
    assert expected_allergen_word in order_allergen_hits[0]["message"].lower()

    if product_id in ("464306", "501100"):
        svc_prop = seller.propose_offer(
            customer,
            product_id=product_id,
            qty_sales_units=2,
            delivery_date=DELIVERY.isoformat(),
            agent_id="valio-aimo/seller-2",
        )
        assert svc_prop["policy_preview"]["outcome"] == "reject"
        svc_accepted = seller.accept_proposal(
            svc_prop["proposal_id"],
            service.Identity(agent_id="buyer-1", customer_id=customer),
        )
        assert svc_accepted["order"]["status"] == "rejected"
        assert svc_accepted["order"]["decision"]["outcome"] == "reject"


def test_learned_swaps_cannot_weaken_allergen_hardblock_school_tampere(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
) -> None:
    api, backend = requote_env
    unsafe_school = ("464306", "471424", "471425", "471426", "472833", "472899")

    for pid in unsafe_school:
        backend.prices[pid] = 0.15
        for scope in ("product", "product_type"):
            learned.remember_swap(
                "school-tampere",
                "470889",
                pid,
                scope=scope,
                approver=SCHOOL_APPROVER,
                approver_role="kitchen_manager",
            )
            learned.remember_swap(
                "school-tampere",
                None,
                pid,
                scope=scope,
                approver=SCHOOL_APPROVER,
                approver_role="kitchen_manager",
            )
    saved_targets = {row["to_product_id"] for row in learned.list_learned("school-tampere")}
    assert set(unsafe_school) <= saved_targets

    for spid in ("470889", "470863", "479001"):
        backend.stock[spid] = 0.0

    r_plan = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "excluded_allergens": [],
            "quantity": 30,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert plan["lines"] == []
    for cand in plan["candidates"]:
        if cand["product"]["id"] in unsafe_school:
            assert cand["tier"] == "rejected"

    det_plan = service._plan_deterministic(
        Need(
            customer_id="school-tampere",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=30,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    assert det_plan.lines == []

    mandate = trust_policy.get_mandate("school-tampere", "buyer-1")
    for pid in unsafe_school:
        rq = api.post(
            "/v1/quotes",
            headers=H_SCHOOL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": pid, "qty_sales_units": 5}],
            },
        )
        assert rq.status_code == 200, rq.text
        q = rq.json()
        assert q["lines"][0]["tier"] == "rejected"

        ro = api.post(
            "/v1/orders",
            headers=H_SCHOOL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert ro.status_code == 200, ro.text
        order = ro.json()
        assert order["status"] == "rejected", (pid, order)
        assert order["decision"]["outcome"] == "reject", (pid, order["decision"])
        allergen_hits = [
            h for h in order["decision"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
        ]
        assert allergen_hits, (pid, order["decision"]["hits"])

        stored_q = service.get_quote(q["quote_id"])
        direct_dec = trust_policy.evaluate(stored_q, mandate)
        assert direct_dec.outcome == "reject", (pid, direct_dec)


def test_learned_swaps_cannot_weaken_allergen_hardblock_hospital_meilahti(
    requote_env: tuple[TestClient, _ConfigurableBackendClient],
) -> None:
    api, backend = requote_env
    hard_reject_hosp = ("501100", "502200", "501102")
    unknown_hosp = ("501101", "501199")
    all_unsafe_hosp = (*hard_reject_hosp, *unknown_hosp)

    for pid in all_unsafe_hosp:
        backend.prices[pid] = 0.15
        for scope in ("product", "product_type"):
            learned.remember_swap(
                "hospital-meilahti",
                "473328",
                pid,
                scope=scope,
                approver=HOSPITAL_APPROVER,
                approver_role="dietitian",
            )
            learned.remember_swap(
                "hospital-meilahti",
                None,
                pid,
                scope=scope,
                approver=HOSPITAL_APPROVER,
                approver_role="dietitian",
            )
    saved_targets = {row["to_product_id"] for row in learned.list_learned("hospital-meilahti")}
    assert set(all_unsafe_hosp) <= saved_targets

    for spid in ("473328", "461765", "473399"):
        backend.stock[spid] = 0.0

    r_plan = api.post(
        "/v1/plan",
        headers=H_HOSPITAL,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "bread",
            "excluded_allergens": [],
            "quantity": 5.0,
            "unit": "KG",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert plan["lines"] == []
    for cand in plan["candidates"]:
        if cand["product"]["id"] in all_unsafe_hosp:
            assert cand["tier"] == "rejected"

    det_plan = service._plan_deterministic(
        Need(
            customer_id="hospital-meilahti",
            product_query="bread",
            excluded_allergens=[],
            quantity=5.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    assert det_plan.lines == []

    mandate = trust_policy.get_mandate("hospital-meilahti", "buyer-1")
    for pid in hard_reject_hosp:
        rq = api.post(
            "/v1/quotes",
            headers=H_HOSPITAL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": pid, "qty_sales_units": 4}],
            },
        )
        assert rq.status_code == 200, rq.text
        q = rq.json()
        assert q["lines"][0]["tier"] == "rejected"

        ro = api.post(
            "/v1/orders",
            headers=H_HOSPITAL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert ro.status_code == 200, ro.text
        order = ro.json()
        assert order["status"] == "rejected", (pid, order)
        assert order["decision"]["outcome"] == "reject", (pid, order["decision"])
        allergen_hits = [
            h for h in order["decision"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
        ]
        assert allergen_hits, (pid, order["decision"]["hits"])

        stored_q = service.get_quote(q["quote_id"])
        assert trust_policy.evaluate(stored_q, mandate).outcome == "reject"

    for pid in unknown_hosp:
        rq = api.post(
            "/v1/quotes",
            headers=H_HOSPITAL,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": pid, "qty_sales_units": 4}],
            },
        )
        assert rq.status_code == 200, rq.text
        q = rq.json()
        assert q["lines"][0]["tier"] == "rejected"
        assert "allergen_unknown" in q["lines"][0]["flags"]

        ro = api.post(
            "/v1/orders",
            headers=H_HOSPITAL,
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        )
        assert ro.status_code == 200, ro.text
        order = ro.json()
        assert order["status"] in ("rejected", "pending_approval")
        assert order["status"] != "placed"
        assert order["decision"]["outcome"] != "auto_approve"
        allergen_hits = [
            h for h in order["decision"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] in ("reject", "needs_approval")
        ]
        assert allergen_hits
        assert "gluten" in allergen_hits[0]["message"].lower()
        assert "unknown" in allergen_hits[0]["message"].lower()
