from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from aimo import config, gemini
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer.fake_server import build_app
from aimo.buyer.fakes import INJECTION, PRODUCTS as FAKE_PRODUCTS
from aimo.buyer.tools import MCPTransport, ToolError
from aimo.catalog import gemini_allergens, textsearch
from aimo.catalog.allergens import blocking, effective_excluded_allergens
from aimo.catalog.plan import plan_need as catalog_plan_need
from aimo.gateway import resilience, service
from aimo.models import EU14, Need

DELIVERY = date(2026, 11, 18)
DELIVERY_ISO = "2026-11-18"


def _parse_mcp_result(res: Any) -> Any:
    text = "\n".join(getattr(c, "text", "") or "" for c in (getattr(res, "content", None) or []))
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        raise ToolError(text or "mcp tool error")
    sc = getattr(res, "structured_content", None)
    if sc is None:
        sc = getattr(res, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    return json.loads(text)


class _FakeAppHarness:
    def __init__(self, http: httpx.AsyncClient, mcp_client: Client) -> None:
        self.http = http
        self.mcp = mcp_client

    async def tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        res = await self.mcp.call_tool(name, args or {})
        return _parse_mcp_result(res)

    async def get_json(self, path: str, expected_status: int = 200) -> Any:
        r = await self.http.get(path)
        assert r.status_code == expected_status, r.text
        return r.json()

    async def post_json(self, path: str, payload: dict[str, Any], expected_status: int = 200) -> Any:
        r = await self.http.post(path, json=payload)
        assert r.status_code == expected_status, r.text
        return r.json()


@asynccontextmanager
async def _mount_fake_app(scenario: str, customer: str):
    app = build_app(scenario=scenario, customer=customer)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as http:
            async with Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)) as mcp_client:
                yield _FakeAppHarness(http, mcp_client)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _synkka_item(
    pid: str,
    name_en: str,
    brand: str,
    category: str,
    unit_kind: str,
    unit_amount: float,
    allergen_cls: list[tuple[str, str]],
    *,
    claims: list[str] | None = None,
    allowed_lot_size: float = 0.01,
    fat_value: float = 1.5,
    ingredients: str = "water, oats",
) -> dict[str, Any]:
    classifications: list[dict[str, Any]] = []
    con_vals = [
        {"id": code, "unit": status}
        for code, status in allergen_cls
        if status in ("CONTAINS", "MAY_CONTAIN")
    ]
    free_vals = [
        {"id": code, "unit": "FREE_FROM"}
        for code, status in allergen_cls
        if status == "FREE_FROM"
    ]
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
    classifications.append({"name": "nutritionFact", "values": [{"id": "FAT", "value": fat_value}]})
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
        "allowedLotSize": allowed_lot_size,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": [{"language": "en", "value": name_en}],
            "unitConversions": conv,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "marketingTexts": [{"language": "en", "value": f"{name_en} for professional kitchens."}],
        },
    }


def _parity_catalog_items() -> list[dict[str, Any]]:
    all_five_free = [
        ("AM", "FREE_FROM"),
        ("AN", "FREE_FROM"),
        ("AW", "FREE_FROM"),
        ("AY", "FREE_FROM"),
        ("AE", "FREE_FROM"),
        ("AP", "FREE_FROM"),
    ]
    return [
        _synkka_item(
            "910001",
            "Nordic Classic Oat Drink 1 L",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "CONTAINS"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "910002",
            "Nordic Gold Oat Drink 1 L",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "MAY_CONTAIN"), ("AW", "MAY_CONTAIN"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "910003",
            "Nordic Unlabelled Oat Drink 1 L",
            "Generic",
            "10400",
            "L",
            1.0,
            [],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "910004",
            "Nordic Inferred Oat Drink 1 L",
            "Generic",
            "10400",
            "L",
            1.0,
            [],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS"), ("AY", "CONTAINS"), ("AE", "FREE_FROM")],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "910006",
            "Oatly Oat Drink Everyday 1 L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS"), ("AY", "FREE_FROM"), ("AE", "MAY_CONTAIN")],
            claims=["VEGAN"],
        ),
        _synkka_item(
            "910007",
            "Pure Harvest Gluten Free Oat Drink 1 L",
            "PureHarvest",
            "10400",
            "L",
            1.0,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
        ),
        _synkka_item(
            "910008",
            "Pure Harvest Reserve Oat Drink 1 L",
            "PureHarvest",
            "10400",
            "L",
            1.0,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
        ),
        _synkka_item(
            "463038",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            "10400",
            "L",
            10.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
        ),
        _synkka_item(
            "464306",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
        ),
        _synkka_item(
            "402105",
            "Valio Eila kitchen butter 500 g lactose free",
            "Valio",
            "10500",
            "KG",
            0.5,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            fat_value=80.0,
            ingredients="cream, salt",
        ),
        _synkka_item(
            "402999",
            "Unlabelled kitchen butter 500 g",
            "Generic",
            "10500",
            "KG",
            0.5,
            [],
            fat_value=80.0,
            ingredients="vegetable oil, water",
        ),
        _synkka_item(
            "501001",
            "Vaasan Wheat Sliced Bread 500 g",
            "Vaasan",
            "14100",
            "KG",
            0.5,
            [("AW", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            fat_value=2.0,
            ingredients="wheat flour, water, yeast",
        ),
        _synkka_item(
            "501002",
            "Bakery Trace Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [("AW", "MAY_CONTAIN"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "FREE_FROM")],
            fat_value=2.0,
            ingredients="maize starch, water, yeast",
        ),
        _synkka_item(
            "501003",
            "Village Unlabelled Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [],
            fat_value=2.0,
            ingredients="starch, water, yeast",
        ),
        _synkka_item(
            "501004",
            "Village Inferred Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [],
            fat_value=2.0,
            ingredients="rice flour, water, yeast",
        ),
        _synkka_item(
            "473328",
            "Schar Vital dark sliced bread 500 g gluten free",
            "Schar",
            "14100",
            "KG",
            0.5,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AY", "FREE_FROM"), ("AE", "CONTAINS")],
            claims=["FREE_FROM_GLUTEN"],
            fat_value=2.0,
            ingredients="water, maize starch, egg",
        ),
        _synkka_item(
            "473329",
            "Semper Pure Gluten Free Sliced Bread 500 g",
            "Semper",
            "14100",
            "KG",
            0.5,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
            fat_value=2.0,
            ingredients="water, maize starch, flaxseed",
        ),
        _synkka_item(
            "920001",
            "Orchard Classic Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "CONTAINS"), ("AY", "CONTAINS"), ("AE", "FREE_FROM")],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples, cream",
        ),
        _synkka_item(
            "920002",
            "Orchard Gold Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "MAY_CONTAIN"), ("AW", "MAY_CONTAIN"), ("AY", "FREE_FROM"), ("AE", "MAY_CONTAIN")],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples",
        ),
        _synkka_item(
            "920003",
            "Orchard Unverified Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            [],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples",
        ),
        _synkka_item(
            "920004",
            "Orchard Inferred Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            [],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples",
        ),
        _synkka_item(
            "920005",
            "Orchard Safe PackSix Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples",
        ),
        _synkka_item(
            "920006",
            "Orchard Safe SubLot Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
            allowed_lot_size=6.0,
            fat_value=0.2,
            ingredients="apples",
        ),
        _synkka_item(
            "920007",
            "Orchard Safe PackFive Apple Puree 1 kg",
            "Orchard",
            "21100",
            "KG",
            1.0,
            all_five_free,
            claims=["FREE_FROM_GLUTEN", "VEGAN"],
            allowed_lot_size=5.0,
            fat_value=0.2,
            ingredients="apples",
        ),
    ]


DEFAULT_OFFER_SPEC: dict[str, tuple[float, float, float, str]] = {
    "910001": (0.70, 500.0, 1.0, "L"),
    "910002": (0.80, 500.0, 1.0, "L"),
    "910003": (0.90, 500.0, 1.0, "L"),
    "910004": (0.95, 500.0, 1.0, "L"),
    "470889": (1.85, 30.0, 1.0, "L"),
    "910006": (1.90, 35.0, 1.0, "L"),
    "910007": (2.05, 40.0, 1.0, "L"),
    "910008": (2.55, 100.0, 1.0, "L"),
    "463038": (10.90, 20.0, 10.0, "L"),
    "464306": (1.01, 500.0, 1.0, "L"),
    "402105": (4.40, 500.0, 0.5, "KG"),
    "402999": (3.10, 500.0, 0.5, "KG"),
    "501001": (1.00, 500.0, 0.5, "KG"),
    "501002": (1.10, 500.0, 0.5, "KG"),
    "501003": (1.20, 500.0, 0.5, "KG"),
    "501004": (1.25, 500.0, 0.5, "KG"),
    "473328": (3.20, 8.0, 0.5, "KG"),
    "473329": (3.50, 60.0, 0.5, "KG"),
    "920001": (1.10, 500.0, 1.0, "KG"),
    "920002": (1.20, 500.0, 1.0, "KG"),
    "920003": (1.30, 500.0, 1.0, "KG"),
    "920004": (1.35, 500.0, 1.0, "KG"),
    "920005": (2.40, 15.0, 1.0, "KG"),
    "920006": (2.45, 4.0, 1.0, "KG"),
    "920007": (2.60, 100.0, 1.0, "KG"),
}


class _MatrixBackendClient:
    base_url = "fixture://parity-matrix-client"
    ttl_s = 300

    def __init__(self, specs: dict[str, tuple[float, float, float, str]]) -> None:
        self.specs = dict(specs)

    def set_stock(self, pid: str, avail: float) -> None:
        price, _old_avail, amt, unit = self.specs[pid]
        self.specs[pid] = (price, avail, amt, unit)

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        products = []
        for pid in ids:
            if pid not in self.specs:
                continue
            unit_price, _avail, content_amt, unit = self.specs[pid]
            cmp_unit = "LTR" if unit == "L" else "KGM"
            products.append(
                {
                    "productId": pid,
                    "salesUnit": "ST",
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "catalogPrice": {"value": unit_price, "currency": "EUR"},
                    "comparisonPrice": {"value": round(unit_price / content_amt, 4), "unit": cmp_unit},
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
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        products = []
        for pid in ids:
            if pid not in self.specs:
                continue
            _price, avail, _amt, _unit = self.specs[pid]
            products.append(
                {
                    "productId": pid,
                    "closingTime": "2026-11-17T17:00:00+0200",
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableDate": requested_date.isoformat(),
                            "availableAmount": avail,
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


@pytest.fixture(scope="module")
def _parity_static_catalog(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    base_dir = tmp_path_factory.mktemp("parity-static-catalog")
    items = _parity_catalog_items()
    pjson = base_dir / "valio_aimo_junction26_product_data.json"
    cdb = base_dir / "catalog.sqlite"
    gem_cache = base_dir / "gemini_allergens.json"
    pjson.write_text(json.dumps(items), encoding="utf-8")

    not_found_map = {g: "not_found" for g in EU14}
    gem_cache.write_text(
        json.dumps(
            {
                "id:910003": {"allergens": not_found_map, "source": "inferred:gemini"},
                "id:501003": {"allergens": not_found_map, "source": "inferred:gemini"},
                "id:920003": {"allergens": not_found_map, "source": "inferred:gemini"},
                "id:402999": {"allergens": not_found_map, "source": "inferred:gemini"},
            }
        ),
        encoding="utf-8",
    )

    old_pjson = catalog_store.PRODUCT_JSON
    old_cdb = catalog_store.CATALOG_DB
    old_rel = catalog_store.RELIABILITY_CSV
    old_co2 = catalog_store.CO2_CSV
    try:
        catalog_store.PRODUCT_JSON = pjson
        catalog_store.CATALOG_DB = cdb
        catalog_store.RELIABILITY_CSV = base_dir / "missing_rel.csv"
        catalog_store.CO2_CSV = base_dir / "missing_co2.csv"
        catalog_store.load_catalog(cdb, force=True)
    finally:
        catalog_store.PRODUCT_JSON = old_pjson
        catalog_store.CATALOG_DB = old_cdb
        catalog_store.RELIABILITY_CSV = old_rel
        catalog_store.CO2_CSV = old_co2

    inferred_free_allergens = json.dumps({g: "free" for g in EU14})
    inferred_free_source = json.dumps({g: "inferred:gemini" for g in EU14})
    con = sqlite3.connect(str(cdb))
    for pid in ("910004", "501004", "920004"):
        con.execute(
            "update products set allergens = ?, allergen_source = ? where id = ?",
            (inferred_free_allergens, inferred_free_source, pid),
        )
    con.commit()
    con.close()
    return pjson, cdb, gem_cache


@pytest.fixture
def parity_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _parity_static_catalog: tuple[Path, Path, Path],
):
    pjson, cdb, gem_cache = _parity_static_catalog

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    monkeypatch.setattr(gemini_allergens, "CACHE", gem_cache)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    _reset_breakers()

    client = _MatrixBackendClient(DEFAULT_OFFER_SPEC)
    bc.set_client(client)
    try:
        yield client
    finally:
        bc.set_client(None)
        _reset_breakers()
        monkeypatch.setattr(gemini_allergens, "_mem", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _run_three_planners(need: Need, monkeypatch: pytest.MonkeyPatch):
    _reset_breakers()
    plan_primary = catalog_plan_need(need)
    plan_det = service._plan_deterministic(need)

    import aimo.catalog as cat

    def _raise_timeout(_need: Any) -> Any:
        raise TimeoutError("simulated primary catalog.plan_need timeout")

    with monkeypatch.context() as mp:
        mp.setattr(cat, "plan_need", _raise_timeout)
        _reset_breakers()
        st_before = resilience.breaker("catalog.plan_need").state()
        plan_timeout = service.plan_need(need, customer_id=need.customer_id)
        st_after_one = resilience.breaker("catalog.plan_need").state()
        assert st_after_one["timeouts"] - st_before["timeouts"] == 1
        assert st_after_one["fallback"] - st_before["fallback"] == 1
        plan_breaker_open = service.plan_need(need, customer_id=need.customer_id)
        st_after_two = resilience.breaker("catalog.plan_need").state()
        assert st_after_two["open"] is True
        plan_circuit = service.plan_need(need, customer_id=need.customer_id)
        st_after_three = resilience.breaker("catalog.plan_need").state()
        assert st_after_three["fallback"] - st_before["fallback"] == 3

    _reset_breakers()
    return plan_primary, plan_det, plan_timeout, plan_breaker_open, plan_circuit


def _assert_cross_planner_parity(
    plans: tuple[Any, ...],
    expected_excl: list[str],
    expected_lines: list[tuple[str, float]],
    expected_shortfall: float,
) -> None:
    all_pids = {
        c.product.id
        for pl in plans
        for c in pl.candidates
    } | {
        ln.product_id
        for pl in plans
        for ln in pl.lines
    }
    rows = textsearch.get_rows(sorted(all_pids))

    ref = plans[0]
    ref_sig = [
        (ln.product_id, ln.qty_sales_units, ln.covered_amount, ln.unit_price, ln.line_total, ln.tier)
        for ln in ref.lines
    ]
    assert [(ln.product_id, ln.qty_sales_units) for ln in ref.lines] == expected_lines

    for pl in plans:
        assert pl.need.excluded_allergens == expected_excl
        sig = [
            (ln.product_id, ln.qty_sales_units, ln.covered_amount, ln.unit_price, ln.line_total, ln.tier)
            for ln in pl.lines
        ]
        assert sig == ref_sig
        assert pl.covered_amount == ref.covered_amount
        assert pl.shortfall == expected_shortfall
        assert pl.total_net == ref.total_net

        for ln_ref, ln_cur in zip(ref.lines, pl.lines):
            assert ("allergen_safe_alternative" in ln_cur.flags) == ("allergen_safe_alternative" in ln_ref.flags)
            assert ("partial_stock" in ln_cur.flags) == ("partial_stock" in ln_ref.flags)

        for ln in pl.lines:
            assert "allergen_unknown" not in ln.flags
            row = rows[ln.product_id]
            assert not blocking(
                row.get("allergens") or {},
                expected_excl,
                allergen_source=row.get("allergen_source"),
            )
            for g in expected_excl:
                assert row["allergens"][g] == "free"
                assert str(row["allergen_source"].get(g) or "").startswith("structured")

        for cand in pl.candidates:
            row = rows.get(cand.product.id)
            if row is None:
                continue
            is_blocked = bool(
                blocking(
                    row.get("allergens") or {},
                    expected_excl,
                    allergen_source=row.get("allergen_source"),
                )
            )
            if is_blocked:
                assert cand.tier == "rejected", (cand.product.id, cand.tier, expected_excl)
            elif cand.tier != "rejected":
                for g in expected_excl:
                    assert row["allergens"][g] == "free"
                    assert str(row["allergen_source"].get(g) or "").startswith("structured")


def test_file_hygiene_no_comments_or_em_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_fake_server_school_tampere_omitted_and_alias_exclusions_http_and_mcp() -> None:
    async def _scenario() -> None:
        async with _mount_fake_app("happy", "school-tampere") as srv:
            hz = await srv.get_json("/healthz")
            assert hz == {"ok": True, "fake": True, "scenario": "happy", "customer": "school-tampere"}
            assert await srv.get_json("/v1/audit/verify") == {"ok": True, "events": 0, "_fake": True}
            assert await srv.get_json("/v1/approvals") == []

            pol = await srv.tool("list_policies")
            assert pol["customer_id"] == "school-tampere"
            assert any("milk, nuts" in r and "hard block" in r for r in pol["rules"])

            hits_search = await srv.tool("search_products", {"query": "oat", "limit": 10})
            assert {"470889", "470863"} <= {h["id"] for h in hits_search}

            plan_omitted = await srv.tool(
                "plan_need",
                {
                    "need": {
                        "product_query": "oat drink",
                        "quantity": 100,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                        "preferred_product_ids": ["470889"],
                    }
                },
            )
            assert plan_omitted["need"]["excluded_allergens"] == ["milk", "nuts"]
            assert [ln["product_id"] for ln in plan_omitted["lines"]] == ["470889"]
            for ln in plan_omitted["lines"]:
                assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["milk"] == "free"
                assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["nuts"] == "free"
            cands_omitted = {c["product"]["id"]: c for c in plan_omitted["candidates"]}
            assert cands_omitted["469040"]["tier"] == "rejected"
            assert cands_omitted["471424"]["tier"] == "rejected"

            q_pref = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [
                        {
                            "product_id": ln["product_id"],
                            "qty_sales_units": ln["qty_sales_units"],
                            "tier": ln["tier"],
                            "flags": ln["flags"],
                        }
                        for ln in plan_omitted["lines"]
                    ],
                    "need": plan_omitted["need"],
                },
            )
            o_pref = await srv.tool("place_order", {"quote_id": q_pref["quote_id"], "quote_hash": q_pref["quote_hash"]})
            assert o_pref["status"] == "placed"
            assert o_pref["response_code"] == "AP"

            plan_alias = await srv.tool(
                "plan_need",
                {
                    "need": {
                        "customer_id": "school-tampere",
                        "product_query": "oat drink",
                        "excluded_allergens": ["dairy", "tree nuts"],
                        "quantity": 120,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert plan_alias["need"]["excluded_allergens"] == ["milk", "nuts"]
            assert [ln["product_id"] for ln in plan_alias["lines"]] == ["470863"]
            q_sub = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [
                        {
                            "product_id": "470863",
                            "qty_sales_units": 120,
                            "tier": plan_alias["lines"][0]["tier"],
                            "flags": plan_alias["lines"][0]["flags"],
                        }
                    ],
                    "need": plan_alias["need"],
                },
            )
            o_sub = await srv.tool("place_order", {"quote_id": q_sub["quote_id"], "quote_hash": q_sub["quote_hash"]})
            assert o_sub["status"] == "pending_approval"
            assert o_sub["decision"]["approver_role"] == "kitchen_manager"

            pending = await srv.get_json("/v1/approvals?status=pending")
            assert [x["order"]["order_id"] for x in pending] == [o_sub["order_id"]]
            await srv.post_json(
                "/v1/approvals/O-nonexistent/decision",
                {"decision": "approve"},
                expected_status=404,
            )
            await srv.post_json(
                f"/v1/approvals/{o_sub['order_id']}/decision",
                {"decision": "defer"},
                expected_status=422,
            )
            await srv.post_json(
                f"/v1/approvals/{o_sub['order_id']}/decision",
                {"decision": "approve", "quote_hash": "wrong-quote-hash"},
                expected_status=409,
            )
            decided = await srv.post_json(
                f"/v1/approvals/{o_sub['order_id']}/decision",
                {
                    "decision": "approve",
                    "quote_hash": o_sub["quote_hash"],
                    "approver": "liisa@school-tampere.example",
                    "note": "verified milk-free and nut-free oat drink",
                },
            )
            assert decided["status"] == "placed"
            assert await srv.get_json("/v1/approvals?status=pending") == []

            plan_wheat_oat = await srv.tool(
                "plan_need",
                {
                    "need": {
                        "customer_id": "school-tampere",
                        "product_query": "oat drink",
                        "excluded_allergens": ["dairy", "tree nuts", "wheat"],
                        "quantity": 50,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert plan_wheat_oat["need"]["excluded_allergens"] == ["milk", "nuts", "gluten"]
            assert plan_wheat_oat["lines"] == []
            assert plan_wheat_oat["shortfall"] == 50.0

            plan_wheat_bread = await srv.tool(
                "plan_need",
                {
                    "need": {
                        "customer_id": "school-tampere",
                        "product_query": "bread",
                        "excluded_allergens": ["wheat"],
                        "quantity": 7.0,
                        "unit": "KG",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert plan_wheat_bread["need"]["excluded_allergens"] == ["gluten", "milk", "nuts"]
            assert [ln["product_id"] for ln in plan_wheat_bread["lines"]] == ["473328"]

            verify = await srv.get_json("/v1/audit/verify")
            assert verify["ok"] is True
            assert verify["_fake"] is True
            assert verify["events"] >= 5

    asyncio.run(_scenario())


def test_fake_server_school_tampere_literal_dairy_queries_milk_cream_butter() -> None:
    async def _scenario() -> None:
        async with _mount_fake_app("happy", "school-tampere") as srv:
            for excl_arg in (None, ["dairy", "tree nuts"]):
                milk_need: dict[str, Any] = {
                    "product_query": "milk",
                    "quantity": 50,
                    "unit": "L",
                    "delivery_by": DELIVERY_ISO,
                }
                if excl_arg is not None:
                    milk_need["excluded_allergens"] = excl_arg
                milk_plan = await srv.tool("plan_need", {"need": milk_need})
                assert milk_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
                assert [ln["product_id"] for ln in milk_plan["lines"]] == ["470889"]
                assert "allergen_safe_alternative" in milk_plan["lines"][0]["flags"]
                milk_cands = {c["product"]["id"]: c for c in milk_plan["candidates"]}
                for dairy_id in ("463038", "464306", "469363", "462775"):
                    assert milk_cands[dairy_id]["tier"] == "rejected"
                    assert "allergen_contains" in milk_cands[dairy_id]["flags"]

                cream_need: dict[str, Any] = {
                    "product_query": "cream",
                    "quantity": 20,
                    "unit": "L",
                    "delivery_by": DELIVERY_ISO,
                }
                if excl_arg is not None:
                    cream_need["excluded_allergens"] = excl_arg
                cream_plan = await srv.tool("plan_need", {"need": cream_need})
                assert cream_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
                cream_cands = {c["product"]["id"]: c for c in cream_plan["candidates"]}
                assert cream_cands["401201"]["tier"] == "rejected"
                assert "allergen_contains" in cream_cands["401201"]["flags"]
                for ln in cream_plan["lines"]:
                    assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["milk"] == "free"
                    assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["nuts"] == "free"
                    assert "allergen_safe_alternative" in ln["flags"]

                butter_need: dict[str, Any] = {
                    "product_query": "butter",
                    "quantity": 10,
                    "unit": "KG",
                    "delivery_by": DELIVERY_ISO,
                }
                if excl_arg is not None:
                    butter_need["excluded_allergens"] = excl_arg
                butter_plan = await srv.tool("plan_need", {"need": butter_need})
                assert butter_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
                assert butter_plan["lines"] == []
                assert butter_plan["covered_amount"] == 0.0
                assert butter_plan["shortfall"] == 10.0
                butter_cands = {c["product"]["id"]: c for c in butter_plan["candidates"]}
                assert butter_cands["402105"]["tier"] == "rejected"
                assert "allergen_contains" in butter_cands["402105"]["flags"]

            milk_need_quote = {
                "customer_id": "school-tampere",
                "product_query": "milk",
                "quantity": 40,
                "unit": "L",
                "delivery_by": DELIVERY_ISO,
            }
            milk_plan_q = await srv.tool("plan_need", {"need": milk_need_quote})
            quote = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [
                        {
                            "product_id": ln["product_id"],
                            "qty_sales_units": ln["qty_sales_units"],
                            "tier": ln["tier"],
                            "flags": ln["flags"],
                        }
                        for ln in milk_plan_q["lines"]
                    ],
                    "need": milk_plan_q["need"],
                },
            )
            assert "allergen_safe_alternative" in quote["lines"][0]["flags"]
            order = await srv.tool("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
            assert order["status"] == "pending_approval"
            assert order["decision"]["outcome"] == "needs_approval"
            assert order["decision"]["approver_role"] == "kitchen_manager"
            assert any(
                h["rule_id"] == "substitution.allergen_safe_alternative"
                for h in order["decision"]["hits"]
            )

            await srv.post_json(
                f"/v1/approvals/{order['order_id']}/decision",
                {
                    "decision": "approve",
                    "quote_hash": order["quote_hash"],
                    "approver": "liisa@school-tampere.example",
                    "note": "confirmed milk-free alternative",
                },
            )
            ord_status = await srv.tool("get_order_status", {"order_id": order["order_id"]})
            assert ord_status["status"] == "placed"
            trail = await srv.tool("get_audit_trail", {"subject_id": order["order_id"]})
            assert len(trail) >= 2
            report_http = await srv.get_json(f"/v1/audit/report/{order['order_id']}")
            assert len(report_http) >= 2
            verify = await srv.get_json("/v1/audit/verify")
            assert verify["ok"] is True
            assert verify["events"] >= 3

    asyncio.run(_scenario())


def test_fake_server_hospital_meilahti_omitted_and_alias_exclusions_and_forced_quotes() -> None:
    async def _scenario() -> None:
        async with _mount_fake_app("happy", "hospital-meilahti") as srv:
            hz = await srv.get_json("/healthz")
            assert hz == {"ok": True, "fake": True, "scenario": "happy", "customer": "hospital-meilahti"}

            pol = await srv.tool("list_policies")
            assert pol["customer_id"] == "hospital-meilahti"
            assert any("gluten" in r and "coeliac" in r for r in pol["rules"])

            for excl_arg, expected_excl in (
                (None, ["gluten"]),
                (["wheat"], ["gluten"]),
                (["dairy", "tree nuts", "wheat"], ["milk", "nuts", "gluten"]),
            ):
                bread_need: dict[str, Any] = {
                    "product_query": "bread",
                    "quantity": 7.0,
                    "unit": "KG",
                    "delivery_by": DELIVERY_ISO,
                    "preferred_product_ids": ["473328"],
                }
                if excl_arg is not None:
                    bread_need["excluded_allergens"] = excl_arg
                bread_plan = await srv.tool("plan_need", {"need": bread_need})
                assert bread_plan["need"]["excluded_allergens"] == expected_excl
                assert [ln["product_id"] for ln in bread_plan["lines"]] == ["473328"]
                assert bread_plan["lines"][0]["qty_sales_units"] == 20.0
                assert bread_plan["covered_amount"] == 7.0

            q_bread = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [{"product_id": "473328", "qty_sales_units": 20.0, "tier": "preferred"}],
                },
            )
            o_bread = await srv.tool("place_order", {"quote_id": q_bread["quote_id"], "quote_hash": q_bread["quote_hash"]})
            assert o_bread["status"] == "placed"

            for query, unit in (("oat drink", "L"), ("milk", "L"), ("cream", "L"), ("butter", "KG")):
                blocked_plan = await srv.tool(
                    "plan_need",
                    {
                        "need": {
                            "product_query": query,
                            "excluded_allergens": ["dairy", "wheat"],
                            "quantity": 15,
                            "unit": unit,
                            "delivery_by": DELIVERY_ISO,
                        }
                    },
                )
                assert blocked_plan["need"]["excluded_allergens"] == ["milk", "gluten"]
                assert blocked_plan["lines"] == []
                assert blocked_plan["shortfall"] == 15.0

            q_unsafe_gluten = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [{"product_id": "470889", "qty_sales_units": 10.0}],
                },
            )
            o_unsafe_gluten = await srv.tool(
                "place_order",
                {"quote_id": q_unsafe_gluten["quote_id"], "quote_hash": q_unsafe_gluten["quote_hash"]},
            )
            assert o_unsafe_gluten["status"] == "rejected"
            assert o_unsafe_gluten["decision"]["outcome"] == "reject"
            assert any(
                h["rule_id"] == "allergens:470889" and h["outcome"] == "reject"
                for h in o_unsafe_gluten["decision"]["hits"]
            )
            assert await srv.get_json("/v1/approvals?status=pending") == []

            q_unknown_gluten = await srv.tool(
                "create_quote",
                {
                    "delivery_date": DELIVERY_ISO,
                    "lines": [{"product_id": "469040", "qty_sales_units": 2.0}],
                    "need": {
                        "customer_id": "hospital-meilahti",
                        "product_query": "oat drink",
                        "excluded_allergens": ["wheat"],
                        "quantity": 20.0,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                    },
                },
            )
            o_unknown_gluten = await srv.tool(
                "place_order",
                {"quote_id": q_unknown_gluten["quote_id"], "quote_hash": q_unknown_gluten["quote_hash"]},
            )
            assert o_unknown_gluten["status"] == "pending_approval"
            assert o_unknown_gluten["decision"]["approver_role"] == "dietitian"
            assert any(
                h["rule_id"] == "allergens:469040" and h["outcome"] == "needs_approval"
                for h in o_unknown_gluten["decision"]["hits"]
            )
            pending = await srv.get_json("/v1/approvals?status=pending")
            assert [x["order"]["order_id"] for x in pending] == [o_unknown_gluten["order_id"]]

            rej_resp = await srv.post_json(
                f"/v1/approvals/{o_unknown_gluten['order_id']}/decision",
                {
                    "decision": "reject",
                    "quote_hash": o_unknown_gluten["quote_hash"],
                    "approver": "sara@hospital-meilahti.example",
                    "note": "missing structured gluten declaration",
                },
            )
            assert rej_resp["status"] == "rejected"
            assert await srv.get_json("/v1/approvals?status=pending") == []
            rejected_all = await srv.get_json("/v1/approvals?status=rejected")
            assert {x["order"]["order_id"] for x in rejected_all} == {
                o_unsafe_gluten["order_id"],
                o_unknown_gluten["order_id"],
            }
            verify = await srv.get_json("/v1/audit/verify")
            assert verify["ok"] is True
            assert verify["events"] >= 7

    asyncio.run(_scenario())


def test_fake_server_stockout_injection_and_live_mcp_transport() -> None:
    async def _stockout_checks() -> None:
        async with _mount_fake_app("stockout", "school-tampere") as srv_school:
            offers = await srv_school.tool(
                "get_offers",
                {"product_ids": ["464306", "470889", "470863"], "requested_date": DELIVERY_ISO},
            )
            by_id = {o["product_id"]: o for o in offers}
            assert by_id["464306"]["available_amount"] == 600
            assert "partial_stock" in by_id["464306"]["issues"]

            split_plan = await srv_school.tool(
                "plan_need",
                {
                    "need": {
                        "product_query": "milk",
                        "quantity": 1600,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert split_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
            assert [(ln["product_id"], ln["qty_sales_units"]) for ln in split_plan["lines"]] == [
                ("470889", 1000.0),
                ("470863", 400.0),
            ]
            assert split_plan["covered_amount"] == 1400.0
            assert split_plan["shortfall"] == 200.0
            for ln in split_plan["lines"]:
                assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["milk"] == "free"
                assert FAKE_PRODUCTS[ln["product_id"]]["allergens"]["nuts"] == "free"
                assert "allergen_safe_alternative" in ln["flags"]

        async with _mount_fake_app("stockout", "hospital-meilahti") as srv_hosp:
            hosp_plan = await srv_hosp.tool(
                "plan_need",
                {
                    "need": {
                        "product_query": "bread",
                        "excluded_allergens": ["wheat"],
                        "quantity": 800.0,
                        "unit": "KG",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert hosp_plan["need"]["excluded_allergens"] == ["gluten"]
            assert [(ln["product_id"], ln["qty_sales_units"]) for ln in hosp_plan["lines"]] == [("473328", 2000.0)]
            assert hosp_plan["covered_amount"] == 700.0
            assert hosp_plan["shortfall"] == 100.0

        async with _mount_fake_app("injection", "hospital-meilahti") as srv_hosp_inj:
            hz_inj = await srv_hosp_inj.get_json("/healthz")
            assert hz_inj == {
                "ok": True,
                "fake": True,
                "scenario": "injection",
                "customer": "hospital-meilahti",
            }
            hosp_inj_plan = await srv_hosp_inj.tool(
                "plan_need",
                {
                    "need": {
                        "product_query": "milk",
                        "quantity": 30.0,
                        "unit": "L",
                        "delivery_by": DELIVERY_ISO,
                    }
                },
            )
            assert hosp_inj_plan["need"]["excluded_allergens"] == ["gluten"]
            assert hosp_inj_plan["lines"] == []
            assert hosp_inj_plan["shortfall"] == 30.0
            hosp_inj_cands = {c["product"]["id"]: c for c in hosp_inj_plan["candidates"]}
            assert hosp_inj_cands["469363"]["tier"] == "rejected"
            assert hosp_inj_cands["469363"]["injection_screen"]["suspicious"] is True
            assert "allergen_unknown" in hosp_inj_cands["469363"]["flags"]
            assert "supplier_text_injection_suspected" in hosp_inj_cands["469363"]["flags"]

    asyncio.run(_stockout_checks())

    port = _free_port()
    app = build_app(scenario="injection", customer="school-tampere")
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    srv.install_signal_handlers = lambda: None
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.01)
    else:
        srv.should_exit = True
        thread.join(timeout=3.0)
        pytest.fail("fake_server injection instance did not start")

    transport = None
    try:
        transport = MCPTransport(f"{base}/mcp", timeout=10.0)
        prod_inj = transport.call("get_product", {"product_id": "469363"})
        assert prod_inj["marketing_text"] == INJECTION

        inj_plan = transport.call(
            "plan_need",
            {
                "need": {
                    "product_query": "milk",
                    "quantity": 50,
                    "unit": "L",
                    "delivery_by": DELIVERY_ISO,
                }
            },
        )
        assert inj_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
        assert [(ln["product_id"], ln["qty_sales_units"]) for ln in inj_plan["lines"]] == [("470889", 50.0)]
        assert inj_plan["lines"][0]["injection_screen"]["suspicious"] is False
        cands_by_id = {c["product"]["id"]: c for c in inj_plan["candidates"]}
        assert cands_by_id["469363"]["tier"] == "rejected"
        assert cands_by_id["469363"]["injection_screen"]["suspicious"] is True
        assert "allergen_contains" in cands_by_id["469363"]["flags"]
        assert "supplier_text_injection_suspected" in cands_by_id["469363"]["flags"]

        for bad_pid in ("469363", "464306", "401201", "402105", "471424"):
            q_bad = transport.call(
                "create_quote",
                {"delivery_date": DELIVERY_ISO, "lines": [{"product_id": bad_pid, "qty_sales_units": 5.0}]},
            )
            if bad_pid == "469363":
                assert "supplier_text_injection_suspected" in q_bad["lines"][0]["flags"]
            o_bad = transport.call(
                "place_order",
                {"quote_id": q_bad["quote_id"], "quote_hash": q_bad["quote_hash"]},
            )
            assert o_bad["status"] == "rejected"
            assert o_bad["decision"]["outcome"] == "reject"
            hit_ids = {h["rule_id"]: h["outcome"] for h in o_bad["decision"]["hits"]}
            assert hit_ids[f"allergens:{bad_pid}"] == "reject"
            if bad_pid == "469363":
                assert hit_ids["security.prompt_injection"] == "needs_approval"

        assert httpx.get(f"{base}/v1/approvals?status=pending", timeout=2.0).json() == []
        rejected_http = httpx.get(f"{base}/v1/approvals?status=rejected", timeout=2.0).json()
        assert len(rejected_http) == 5
        verify = httpx.get(f"{base}/v1/audit/verify", timeout=2.0).json()
        assert verify["ok"] is True
        assert verify["events"] == 10
    finally:
        if transport is not None:
            transport.close()
        srv.should_exit = True
        thread.join(timeout=5.0)


def test_parity_school_tampere_omitted_combined_split_and_literal_milk(
    parity_env: _MatrixBackendClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    need_split = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=80.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plans_split = _run_three_planners(need_split, monkeypatch)
    _assert_cross_planner_parity(
        plans_split,
        expected_excl=["milk", "nuts"],
        expected_lines=[("470889", 30.0), ("910006", 35.0), ("910007", 15.0)],
        expected_shortfall=0.0,
    )
    for pl in plans_split:
        cand_map = {c.product.id: c for c in pl.candidates}
        for trap_id in ("910001", "910002", "910003", "910004"):
            assert cand_map[trap_id].tier == "rejected"
        assert "allergen_unknown" in cand_map["910003"].flags
        assert "allergen_unknown" in cand_map["910004"].flags

    need_combined = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=["soy", "eggs"],
        quantity=60.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plans_combined = _run_three_planners(need_combined, monkeypatch)
    _assert_cross_planner_parity(
        plans_combined,
        expected_excl=["soy", "eggs", "milk", "nuts"],
        expected_lines=[("910007", 40.0), ("910008", 20.0)],
        expected_shortfall=0.0,
    )

    need_literal_milk = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=25.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plans_milk = _run_three_planners(need_literal_milk, monkeypatch)
    _assert_cross_planner_parity(
        plans_milk,
        expected_excl=["milk", "nuts"],
        expected_lines=[("470889", 25.0)],
        expected_shortfall=0.0,
    )
    for pl in plans_milk:
        assert "allergen_safe_alternative" in pl.lines[0].flags

    need_butter = Need(
        customer_id="school-tampere",
        product_query="butter",
        excluded_allergens=[],
        quantity=10.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    plans_butter = _run_three_planners(need_butter, monkeypatch)
    _assert_cross_planner_parity(
        plans_butter,
        expected_excl=["milk", "nuts"],
        expected_lines=[],
        expected_shortfall=10.0,
    )

    from aimo.gateway.app import create_app

    api = TestClient(create_app(with_mcp=False))
    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    payload = {
        "customer_id": "school-tampere",
        "product_query": "oat drink",
        "excluded_allergens": ["soy", "eggs"],
        "quantity": 60.0,
        "unit": "L",
        "delivery_by": DELIVERY_ISO,
    }
    r_pri = api.post("/v1/plan", headers=headers, json=payload)
    assert r_pri.status_code == 200, r_pri.text
    j_pri = r_pri.json()

    import aimo.catalog as cat

    with monkeypatch.context() as mp:
        mp.setattr(cat, "plan_need", lambda _n: (_ for _ in ()).throw(TimeoutError("http timeout")))
        _reset_breakers()
        r_fb = api.post("/v1/plan", headers=headers, json=payload)
        assert r_fb.status_code == 200, r_fb.text
        j_fb = r_fb.json()

    assert j_pri["need"]["excluded_allergens"] == ["soy", "eggs", "milk", "nuts"]
    assert j_fb["need"]["excluded_allergens"] == ["soy", "eggs", "milk", "nuts"]
    assert [(x["product_id"], x["qty_sales_units"]) for x in j_pri["lines"]] == [("910007", 40.0), ("910008", 20.0)]
    assert [(x["product_id"], x["qty_sales_units"]) for x in j_fb["lines"]] == [("910007", 40.0), ("910008", 20.0)]
    assert "deterministic planner" in j_fb["explanation"]


def test_parity_hospital_meilahti_and_hotel_aurora_omitted_and_combined(
    parity_env: _MatrixBackendClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hosp_omitted = Need(
        customer_id="hospital-meilahti",
        product_query="sliced bread",
        excluded_allergens=[],
        quantity=10.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    plans_hosp_omitted = _run_three_planners(hosp_omitted, monkeypatch)
    _assert_cross_planner_parity(
        plans_hosp_omitted,
        expected_excl=["gluten"],
        expected_lines=[("473328", 8.0), ("473329", 12.0)],
        expected_shortfall=0.0,
    )
    for pl in plans_hosp_omitted:
        cand_map = {c.product.id: c for c in pl.candidates}
        for trap_id in ("501001", "501002", "501003", "501004"):
            assert cand_map[trap_id].tier == "rejected"
        assert "allergen_unknown" in cand_map["501003"].flags
        assert "allergen_unknown" in cand_map["501004"].flags

    hosp_combined = Need(
        customer_id="hospital-meilahti",
        product_query="sliced bread",
        excluded_allergens=["egg", "dairy"],
        quantity=10.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    plans_hosp_combined = _run_three_planners(hosp_combined, monkeypatch)
    _assert_cross_planner_parity(
        plans_hosp_combined,
        expected_excl=["eggs", "milk", "gluten"],
        expected_lines=[("473329", 20.0)],
        expected_shortfall=0.0,
    )

    hotel_omitted = Need(
        customer_id="hotel-aurora",
        product_query="lactose free milk",
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        quantity=250.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plans_hotel_omitted = _run_three_planners(hotel_omitted, monkeypatch)
    _assert_cross_planner_parity(
        plans_hotel_omitted,
        expected_excl=[],
        expected_lines=[("463038", 20.0), ("464306", 50.0)],
        expected_shortfall=0.0,
    )

    hotel_caller_excl = Need(
        customer_id="hotel-aurora",
        product_query="oat drink",
        excluded_allergens=["dairy", "tree nuts", "soya", "eggs"],
        quantity=55.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plans_hotel_excl = _run_three_planners(hotel_caller_excl, monkeypatch)
    _assert_cross_planner_parity(
        plans_hotel_excl,
        expected_excl=["milk", "nuts", "soy", "eggs"],
        expected_lines=[("910007", 40.0), ("910008", 15.0)],
        expected_shortfall=0.0,
    )


@pytest.mark.parametrize(
    "customer_id,caller_excl,expected_excl",
    [
        ("school-tampere", [], ["milk", "nuts"]),
        ("school-tampere", ["soy", "eggs"], ["soy", "eggs", "milk", "nuts"]),
        ("hospital-meilahti", [], ["gluten"]),
        ("hospital-meilahti", ["dairy", "peanuts"], ["milk", "peanuts", "gluten"]),
        ("hotel-aurora", ["dairy", "tree nuts", "wheat"], ["milk", "nuts", "gluten"]),
    ],
)
def test_parity_lot_size_rounding_and_total_safe_stockout_across_customers(
    parity_env: _MatrixBackendClient,
    monkeypatch: pytest.MonkeyPatch,
    customer_id: str,
    caller_excl: list[str],
    expected_excl: list[str],
) -> None:
    assert effective_excluded_allergens(customer_id, caller_excl) == expected_excl

    need_lot = Need(
        customer_id=customer_id,
        product_query="apple puree",
        excluded_allergens=caller_excl,
        quantity=20.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    plans_lot = _run_three_planners(need_lot, monkeypatch)
    _assert_cross_planner_parity(
        plans_lot,
        expected_excl=expected_excl,
        expected_lines=[("920005", 12.0), ("920007", 10.0)],
        expected_shortfall=0.0,
    )
    for pl in plans_lot:
        assert pl.covered_amount == 22.0
        assert pl.total_net == 54.80
        assert pl.lines[0].qty_sales_units % 6 == 0
        assert pl.lines[1].qty_sales_units % 5 == 0
        cand_map = {c.product.id: c for c in pl.candidates}
        for trap_id in ("920001", "920002", "920003", "920004"):
            assert cand_map[trap_id].tier == "rejected"
        assert "allergen_unknown" in cand_map["920003"].flags
        assert "allergen_unknown" in cand_map["920004"].flags

    for safe_pid in ("920005", "920006", "920007"):
        parity_env.set_stock(safe_pid, 0.0)

    plans_stockout = _run_three_planners(need_lot, monkeypatch)
    _assert_cross_planner_parity(
        plans_stockout,
        expected_excl=expected_excl,
        expected_lines=[],
        expected_shortfall=20.0,
    )
    for pl in plans_stockout:
        assert pl.lines == []
        assert pl.covered_amount == 0.0
        assert pl.total_net == 0.0
        assert pl.shortfall == 20.0
