from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import aimo.catalog as cat
from aimo import config
from aimo.backend import catalog_store, clock
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend.peppol import (
    CUSTOMIZATION_ID,
    NS_CAC,
    NS_CBC,
    PROFILE_ID,
    check_t116_rules,
    header_code,
    order_response_xml,
)
from aimo.catalog import get_product, textsearch
from aimo.gateway import extras, human_auth, resilience
from aimo.gateway.app import create_app
from aimo.models import EU14, Order, Quote
from aimo.trust import policy as trust_policy
from conformance import verify as V
from conformance.__main__ import run as run_conformance
from conformance.profile import MCP_FORBIDDEN, MCP_TOOLS, OPENAPI_PATHS, OTAP_ENDPOINTS, OTAP_FIELDS, PROFILE

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = date(2026, 11, 16)
NS = {"cac": NS_CAC, "cbc": NS_CBC}

H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
H_HOTEL = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
PIN_HEADER = {"X-Approver-Pin": "test-pin"}

SCHOOL_APPROVER = "liisa@school-tampere.example"
SCHOOL_DIRECTOR = "jari@school-tampere.example"
HOSPITAL_DIETITIAN = "sara@hospital-meilahti.example"
HOSPITAL_HEAD = "helena@hospital-meilahti.example"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
    gtin: str | None = None,
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
    synkka: dict[str, Any] = {
        "brand": brand,
        "names": [{"language": "en", "value": name_en}],
        "unitConversions": conv,
        "classifications": classifications,
        "keyIngredients": [{"language": "en", "value": ingredients}],
        "marketingTexts": [{"language": "en", "value": f"{name_en} for foodservice kitchens."}],
    }
    if gtin:
        synkka["gtin"] = gtin
    return {
        "id": pid,
        "gtin": gtin,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": brand,
        "allowedLotSize": 0.01,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1, "gtin": gtin}],
        "synkkaData": synkka,
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
            gtin="07394376616228",
        ),
        _synkka_item(
            "470863",
            "Oatly Oat Drink Semi 1 L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats 10%, rapeseed oil",
            fat=1.5,
            gtin="07394376616235",
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
            gtin="06408650009018",
        ),
        _synkka_item(
            "464306",
            "Valio Eila semi skimmed milk drink 1 L lactose free",
            "Valio",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
            gtin="06408430043064",
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
            "Fazer Aito unverified oat drink 1 L UHT",
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
            gtin="08008698003503",
        ),
        _synkka_item(
            "461765",
            "Moilas Gluten-free Dark Bread 600 g sliced frozen",
            "Moilas",
            "14100",
            "KG",
            0.6,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, rice flour, buckwheat, yeast",
            fat=3.0,
            gtin="06416046461765",
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
            "Bakery Trace Gluten Dark Sliced Bread 500 g",
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
            "Artisan Unverified Dark Sliced Bread 500 g",
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
            "501203",
            "Claim Only Gluten Free Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.5,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="rice flour, corn starch, water",
            fat=3.0,
        ),
    ]


class _ConfigurableBackendClient:
    base_url = "fixture://allergen-conformance-peppol"
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
        self.prices: dict[str, float] = {pid: 2.00 for pid in self._units}
        self.prices["473328"] = 2.50
        self.prices["461765"] = 2.60
        self.prices["473399"] = 2.70
        for unsafe_pid in (
            "464306",
            "471425",
            "471424",
            "471426",
            "472833",
            "472899",
            "501100",
            "501102",
            "501101",
            "501199",
            "501203",
        ):
            self.prices[unsafe_pid] = 0.35
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
        meta = {"timeNow": self._time_now_iso(), "currency": "EUR"}
        products = []
        for pid in ids:
            if pid not in self._units:
                continue
            cunit, camt = self._units[pid]
            up = self.prices[pid]
            gross = round(up * 1.14, 2)
            cmp_val = round(up / camt, 4) if camt else up
            products.append(
                {
                    "productId": pid,
                    "salesUnit": "ST",
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "comparisonUnit": cunit,
                    "catalogPrice": {"value": up, "valueWithTax": gross, "currency": "EUR"},
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
            amt = self.stock[pid]
            sched = (
                [
                    {
                        "salesBlock": sb,
                        "availableDate": requested_date.isoformat(),
                        "availableAmount": amt,
                        "salesUnit": "ST",
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


def _close_cached_catalog_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _reset_breakers() -> None:
    for b in resilience._breakers.values():
        b.reset()


def _seed_inferred_rows(cdb: Path) -> None:
    _close_cached_catalog_con()
    catalog_store.load_catalog(cdb, force=True)
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


def _write_replay_snapshot(replay_dir: Path, items: list[dict[str, Any]], day: date) -> None:
    replay_dir.mkdir(parents=True, exist_ok=True)
    client = _ConfigurableBackendClient(items)
    all_ids = [it["id"] for it in items]
    time_now = "2026-11-14T14:00:00+0200"
    price_doc = client.price(all_ids, day)
    price_doc.pop("_oldest_fetch", None)
    for row in price_doc["products"]:
        row.pop("_fetched_at", None)
        row.pop("_meta", None)
    avail_doc = client.availability(all_ids, day)
    avail_doc.pop("_oldest_fetch", None)
    avail_doc["timeNow"] = time_now
    for row in avail_doc["products"]:
        row.pop("_fetched_at", None)
        row.pop("_meta", None)
    (replay_dir / "meta.json").write_text(
        json.dumps({"time_now": time_now, "dates": [day.isoformat()], "n_ids": len(all_ids)}),
        encoding="utf-8",
    )
    (replay_dir / f"price_{day.isoformat()}.json").write_text(json.dumps(price_doc), encoding="utf-8")
    (replay_dir / f"availability_{day.isoformat()}.json").write_text(json.dumps(avail_doc), encoding="utf-8")


@pytest.fixture(scope="module")
def built_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    cat_dir = tmp_path_factory.mktemp("allergen_conformance_catalog")
    items = _catalog_items()
    pjson = cat_dir / "valio_aimo_junction26_product_data.json"
    cdb = cat_dir / "catalog.sqlite"
    rel_csv = cat_dir / "valio_aimo_junction26_delivery_reliability.csv"
    co2_csv = cat_dir / "valio_aimo_junction26_carbon_footprint.csv"
    key_dir = cat_dir / "keys"
    replay_dir = cat_dir / "replay"
    pjson.write_text(json.dumps(items), encoding="utf-8")

    mp = pytest.MonkeyPatch()
    mp.setattr(catalog_store, "PRODUCT_JSON", pjson)
    mp.setattr(catalog_store, "CATALOG_DB", cdb)
    mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    mp.setattr(catalog_store, "CO2_CSV", co2_csv)
    try:
        _seed_inferred_rows(cdb)
    finally:
        mp.undo()
        _close_cached_catalog_con()

    _write_replay_snapshot(replay_dir, items, DELIVERY)
    with TestClient(create_app(with_mcp=False)) as api:
        yield {
            "cat_dir": cat_dir,
            "pjson": pjson,
            "cdb": cdb,
            "rel_csv": rel_csv,
            "co2_csv": co2_csv,
            "key_dir": key_dir,
            "replay_dir": replay_dir,
            "items": items,
            "api": api,
        }
    _close_cached_catalog_con()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built_bundle: dict[str, Any]):
    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", built_bundle["key_dir"])
    monkeypatch.setattr(config, "PRODUCT_JSON", built_bundle["pjson"])
    monkeypatch.setattr(config, "CATALOG_DB", built_bundle["cdb"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", built_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", built_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", built_bundle["pjson"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", built_bundle["cdb"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", built_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", built_bundle["co2_csv"])
    monkeypatch.setattr(cat, "_SOURCE", "full")

    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
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

    _close_cached_catalog_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    human_auth.reset_for_tests()
    _reset_breakers()
    backend = _ConfigurableBackendClient(built_bundle["items"])
    bc.set_client(backend)
    try:
        yield built_bundle["api"], backend, state_db
    finally:
        bc.set_client(None)
        store.close_all()
        human_auth.reset_for_tests()
        _reset_breakers()
        _close_cached_catalog_con()
        if hasattr(trust_policy, "_load_policy_cached"):
            trust_policy._load_policy_cached.cache_clear()


@pytest.fixture(scope="module")
def live_gateway(tmp_path_factory: pytest.TempPathFactory, built_bundle: dict[str, Any]):
    srv_dir = tmp_path_factory.mktemp("live_allergen_gw")
    port = _free_port()
    log_path = srv_dir / "uvicorn.log"
    srv_env = {
        **os.environ,
        "AIMO_DATA_DIR": str(built_bundle["cat_dir"]),
        "AIMO_STATE_DB": str(srv_dir / "state.sqlite"),
        "AIMO_KEY_DIR": str(srv_dir / "keys"),
        "AIMO_REPLAY": str(built_bundle["replay_dir"]),
        "AIMO_OFFLINE": "0",
        "AIMO_CATALOG_FIXTURE": "0",
        "AIMO_APPROVER_PIN": "test-pin",
        "AIMO_NOW": "2026-11-14T14:00:00+02:00",
        "AIMO_SEMANTIC": "0",
        "AIMO_RECIPE_CHECK": "0",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
    }
    srv_env.pop("AIMO_AGENT_KEY", None)
    srv_env.pop("AIMO_AGENT_KEYS", None)
    srv_env.pop("AIMO_APPROVER_PINS", None)
    srv_env.pop("AIMO_FAULTS", None)
    log_fh = open(log_path, "w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aimo.gateway.app:app", "--host", "127.0.0.1", "--port", str(port)],
        env=srv_env,
        stdout=log_fh,
        stderr=log_fh,
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        log_fh.seek(0)
        proc.kill()
        log_fh.close()
        pytest.fail("live_gateway did not start:\n" + log_fh.read()[-2000:])
    try:
        yield base, srv_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()


def _parse_xml(xml_text: str) -> ET.Element:
    return ET.fromstring(xml_text.encode("utf-8"))


def _assert_structured_free(pid: str, excluded: tuple[str, ...]) -> None:
    prod = get_product(pid)
    assert prod is not None, f"missing product {pid}"
    for group in excluded:
        st = prod.allergens.get(group)
        val = st.value if hasattr(st, "value") else str(st or "unknown")
        src = prod.allergen_source.get(group) or ""
        assert val == "free", (pid, group, val)
        assert src == "structured", (pid, group, src)


def _assert_quote_crypto(q: dict[str, Any], jwks: dict[str, Any], expected_customer: str) -> None:
    verdict = V.verify_quote(q, jwks)
    assert verdict["ok"] is True, verdict
    assert verdict["hash_matches"] is True
    assert verdict["signature_valid"] is True
    assert verdict["reasons"] == []
    assert V.quote_hash(q) == q["quote_hash"]
    header, payload = V.verify_jws(q["signature"], jwks)
    assert header["alg"] == "EdDSA"
    assert header["kid"] == q["kid"]
    key_map = {k["kid"]: k for k in jwks["keys"]}
    assert q["kid"] in key_map
    assert V.thumbprint(key_map[q["kid"]]) == q["kid"]
    assert payload["quote_id"] == q["quote_id"]
    assert payload["quote_hash"] == q["quote_hash"]
    assert payload["customer_id"] == expected_customer


def _mcp_list_tools(headers: dict[str, str]) -> list[Any]:
    async def _run() -> list[Any]:
        app = create_app(with_mcp=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as http:
                async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                    return list((await client.list_tools()).tools)

    return asyncio.run(_run())


def _mcp_call(headers: dict[str, str], tool_name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async def _run() -> tuple[bool, Any]:
        app = create_app(with_mcp=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as http:
                async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                    res = await client.call_tool(tool_name, args)
                    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                    if sc is not None and not is_err:
                        if isinstance(sc, dict) and set(sc) == {"result"}:
                            return False, sc["result"]
                        return False, sc
                    txt = "\n".join(getattr(c, "text", "") or "" for c in (res.content or []))
                    try:
                        return is_err, json.loads(txt)
                    except Exception:
                        brace = txt.find("{")
                        if brace >= 0:
                            try:
                                return is_err, json.loads(txt[brace:])
                            except Exception:
                                pass
                        return is_err, txt

    return asyncio.run(_run())


def test_discovery_manifest_jwks_openapi_and_13_mcp_tools(env) -> None:
    api, _, _ = env
    r_otap = api.get("/.well-known/otap.json")
    assert r_otap.status_code == 200
    otap = r_otap.json()
    assert [f for f in OTAP_FIELDS if f not in otap] == []
    assert otap["protocol"] == PROFILE
    assert [e for e in OTAP_ENDPOINTS if e not in otap["endpoints"]] == []
    assert otap["mcp"]["transport"] == "streamable-http"
    assert otap["mcp"]["server_name"] == "valio-aimo-trusted-trade"
    pol_ids = {p["customer_id"] for p in otap["policies"]}
    assert {"hotel-aurora", "school-tampere", "hospital-meilahti"} <= pol_ids

    r_jwks = api.get("/.well-known/jwks.json")
    assert r_jwks.status_code == 200
    jwks = r_jwks.json()
    keys = jwks.get("keys") or []
    assert keys
    for k in keys:
        assert k["kty"] == "OKP"
        assert k["crv"] == "Ed25519"
        assert k["alg"] == "EdDSA"
        assert "d" not in k
        assert k["kid"] == V.thumbprint(k)

    r_spec = api.get("/openapi.json")
    assert r_spec.status_code == 200
    spec = r_spec.json()
    assert str(spec.get("openapi", "")).startswith("3.")
    paths = set((spec.get("paths") or {}).keys())
    assert set(OPENAPI_PATHS) <= paths
    assert "/v1/plan" in paths
    assert "/v1/orders/{order_id}/peppol-order-response.xml" in paths
    assert "/v1/auth/approver-session" in paths

    for hdrs in (H_SCHOOL, H_HOSPITAL):
        tools = _mcp_list_tools(hdrs)
        by_name = {t.name: t for t in tools}
        assert len(by_name) == 13
        expected_names = set(MCP_TOOLS) | set(extras.EXTRA_TOOLS)
        assert set(by_name) == expected_names
        assert not (set(by_name) & set(MCP_FORBIDDEN))
        for tname, req_args in MCP_TOOLS.items():
            schema = getattr(by_name[tname], "input_schema", None) or getattr(by_name[tname], "inputSchema", None) or {}
            props = set((schema.get("properties") or {}).keys())
            req = set(schema.get("required") or [])
            assert schema.get("type") == "object"
            assert set(req_args) <= props
            assert set(req_args) <= req
            assert (by_name[tname].description or "").strip()


@pytest.mark.parametrize(
    "customer_id,headers,query,unit,qty,excluded",
    [
        ("school-tampere", H_SCHOOL, "oat drink", "L", 40.0, ("milk", "nuts")),
        ("school-tampere", H_SCHOOL, "milk", "L", 25.0, ("milk", "nuts")),
        ("hospital-meilahti", H_HOSPITAL, "gluten free bread", "KG", 7.0, ("gluten",)),
        ("hospital-meilahti", H_HOSPITAL, "bread", "KG", 7.0, ("gluten",)),
    ],
)
def test_allergen_filtered_plans_produce_ed25519_and_rfc7638_verified_quotes(
    env,
    tmp_path: Path,
    customer_id: str,
    headers: dict[str, str],
    query: str,
    unit: str,
    qty: float,
    excluded: tuple[str, ...],
) -> None:
    api, _, _ = env
    jwks = api.get("/.well-known/jwks.json").json()

    r_plan = api.post(
        "/v1/plan",
        headers=headers,
        json={
            "customer_id": customer_id,
            "product_query": query,
            "quantity": qty,
            "unit": unit,
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert plan["lines"]
    assert set(excluded) <= set(plan["need"]["excluded_allergens"])
    for ln in plan["lines"]:
        assert "allergen_unknown" not in (ln.get("flags") or [])
        _assert_structured_free(ln["product_id"], excluded)

    r_quote = api.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, customer_id)

    fetched = api.get(f"/v1/quotes/{q['quote_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["quote_hash"] == q["quote_hash"]

    for mutated in (
        {**q, "total_net": round(float(q["total_net"]) + 0.01, 2)},
        {**q, "customer_id": "hotel-aurora"},
        {**q, "lines": [{**q["lines"][0], "qty_sales_units": float(q["lines"][0]["qty_sales_units"]) + 1}]},
    ):
        bad_verdict = V.verify_quote(mutated, jwks)
        assert bad_verdict["ok"] is False
        assert bad_verdict["hash_matches"] is False

    q_file = tmp_path / f"{customer_id}_{query.replace(' ', '_')}_q.json"
    jwks_file = tmp_path / "jwks.json"
    q_file.write_text(json.dumps(q), encoding="utf-8")
    jwks_file.write_text(json.dumps(jwks), encoding="utf-8")
    verifier = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "verify_quote.py"), str(q_file), str(jwks_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verifier.returncode == 0 and "VERIFIED" in verifier.stdout, verifier.stdout + verifier.stderr


@pytest.mark.parametrize(
    "customer_id,query",
    [
        ("school-tampere", "Oatly Oat Drink Whole"),
        ("hospital-meilahti", "Schar Vital dark sliced bread 350 g"),
    ],
)
def test_otap_conformance_suite_passes_for_allergen_customers(
    live_gateway: tuple[str, Path],
    customer_id: str,
    query: str,
) -> None:
    base, _ = live_gateway
    report = run_conformance(base, customer=customer_id, agent="buyer-1", query=query, day=DELIVERY, quiet=True)
    failures = [row for row in report["results"] if row["status"] == "FAIL"]
    skips = [row for row in report["results"] if row["status"] == "SKIP"]
    assert not failures, failures
    assert not skips, skips
    assert report["profile"] == PROFILE
    assert report["summary"]["pass"] >= 40
    assert len(report["mcp_tools"]) == 13


def test_safe_school_plan_auto_approved_under_300_exports_valid_peppol_ap(env) -> None:
    api, backend, _ = env
    backend.prices["470889"] = 2.10
    jwks = api.get("/.well-known/jwks.json").json()

    r_plan = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "preferred_product_ids": ["470889"],
            "quantity": 100.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert [ln["product_id"] for ln in plan["lines"]] == ["470889"]
    _assert_structured_free("470889", ("milk", "nuts"))

    r_quote = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    assert q["total_net"] == 210.0
    assert q["lines"][0]["tier"] == "preferred"
    _assert_quote_crypto(q, jwks, "school-tampere")

    r_order = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    order = r_order.json()
    assert order["status"] == "placed"
    assert order["decision"]["outcome"] == "auto_approve"
    assert order["response_code"] == "AP"
    assert len(order["response_lines"]) == 1
    assert order["response_lines"][0]["product_id"] == "470889"
    assert order["response_lines"][0]["code"] == "AP"
    assert order["response_lines"][0]["qty_confirmed"] == 100.0
    for rl in order["response_lines"]:
        _assert_structured_free(rl["product_id"], ("milk", "nuts"))

    r_xml = api.get(f"/v1/orders/{order['order_id']}/peppol-order-response.xml")
    assert r_xml.status_code == 200
    assert r_xml.headers["content-type"].startswith("application/xml")
    assert check_t116_rules(r_xml.text) == []
    root = _parse_xml(r_xml.text)
    assert root.findtext("cbc:CustomizationID", None, NS) == CUSTOMIZATION_ID
    assert root.findtext("cbc:ProfileID", None, NS) == PROFILE_ID
    assert root.findtext("cbc:OrderResponseCode", None, NS) == "AP"
    assert root.findtext("cbc:CustomerReference", None, NS) == q["quote_id"]
    assert root.findtext("cac:OrderReference/cbc:ID", None, NS) == order["order_id"]
    assert root.findtext("cac:BuyerCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName", None, NS) == "school-tampere"
    assert len(root.findall("cac:OrderLine", NS)) == 0
    assert q["quote_hash"] in r_xml.text
    assert q["kid"] in r_xml.text
    for unsafe_id in ("464306", "471425", "471424", "471426", "472833", "472899"):
        assert unsafe_id not in r_xml.text

    assert api.get("/v1/audit/verify").json()["ok"] is True
    all_events = api.get("/v1/audit").json()
    assert V.verify_events(all_events, jwks, contiguous=True)["ok"] is True


def test_safe_school_substitute_pending_then_human_session_approval_peppol_ab_to_ap(env) -> None:
    api, backend, _ = env
    backend.stock["470889"] = 0.0
    backend.prices["470863"] = 1.95
    backend.prices["479001"] = 2.05
    jwks = api.get("/.well-known/jwks.json").json()

    r_plan = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "preferred_product_ids": ["470889"],
            "quantity": 80.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert plan["lines"]
    planned_ids = [ln["product_id"] for ln in plan["lines"]]
    assert "470889" not in planned_ids
    for pid in planned_ids:
        _assert_structured_free(pid, ("milk", "nuts"))

    r_quote = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, "school-tampere")
    assert all(ln["tier"] in ("best", "better", "good") for ln in q["lines"])

    r_order = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    pending = r_order.json()
    assert pending["status"] == "pending_approval"
    assert pending["decision"]["outcome"] == "needs_approval"
    assert pending["decision"]["approver_role"] == "kitchen_manager"

    ab_xml = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml")
    assert ab_xml.status_code == 200
    assert check_t116_rules(ab_xml.text) == []
    assert _parse_xml(ab_xml.text).findtext("cbc:OrderResponseCode", None, NS) == "AB"

    sess_res = api.post(
        "/v1/auth/approver-session",
        headers=PIN_HEADER,
        json={"approver": SCHOOL_APPROVER},
    )
    assert sess_res.status_code == 200, sess_res.text
    session_token = sess_res.json()["session"]
    assert sess_res.json()["approver"] == SCHOOL_APPROVER

    r_approve = api.post(
        f"/v1/approvals/{pending['order_id']}/decision",
        headers={"X-Approver-Session": session_token},
        json={"decision": "approve", "quote_hash": q["quote_hash"], "note": "Verified milk-and-nut-free oat substitute"},
    )
    assert r_approve.status_code == 200, r_approve.text
    placed = r_approve.json()
    assert placed["status"] == "placed"
    assert placed["approved_by"] == SCHOOL_APPROVER
    assert placed["approver_role"] == "kitchen_manager"
    assert placed["response_code"] == "AP"
    assert placed["response_lines"]
    for rl in placed["response_lines"]:
        assert rl["code"] == "AP"
        _assert_structured_free(rl["product_id"], ("milk", "nuts"))

    ap_xml = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml")
    assert ap_xml.status_code == 200
    assert check_t116_rules(ap_xml.text) == []
    ap_root = _parse_xml(ap_xml.text)
    assert ap_root.findtext("cbc:OrderResponseCode", None, NS) == "AP"
    assert len(ap_root.findall("cac:OrderLine", NS)) == 0
    assert q["quote_hash"] in ap_xml.text

    trail = api.get("/v1/audit", params={"subject_id": pending["order_id"]}).json()
    assert V.verify_events(trail, jwks)["ok"] is True
    actors = {ev["actor"].split(":")[0] for ev in trail}
    assert {"agent", "human", "system"} <= actors
    assert V.verify_events(api.get("/v1/audit").json(), jwks, contiguous=True)["ok"] is True


def test_safe_school_partial_stock_at_placement_exports_peppol_ca_with_only_milk_and_nut_free_lines(env) -> None:
    api, backend, _ = env
    backend.stock["470889"] = 60.0
    backend.stock["470863"] = 60.0
    backend.prices["470889"] = 2.00
    backend.prices["470863"] = 2.05
    jwks = api.get("/.well-known/jwks.json").json()

    r_plan = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "preferred_product_ids": ["470889"],
            "quantity": 100.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert [ln["product_id"] for ln in plan["lines"]] == ["470889", "470863"]
    for ln in plan["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))

    r_quote = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, "school-tampere")

    r_order = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    pending = r_order.json()
    assert pending["status"] == "pending_approval"

    backend.stock["470863"] = 25.0
    sess_token = api.post("/v1/auth/approver-session", headers=PIN_HEADER, json={"approver": SCHOOL_APPROVER}).json()["session"]
    r_approve = api.post(
        f"/v1/approvals/{pending['order_id']}/decision",
        headers={"X-Approver-Session": sess_token},
        json={"decision": "approve", "quote_hash": q["quote_hash"]},
    )
    assert r_approve.status_code == 200, r_approve.text
    placed = r_approve.json()
    assert placed["status"] == "placed"
    assert placed["response_code"] == "CA"

    ca_xml_resp = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml")
    assert ca_xml_resp.status_code == 200
    assert check_t116_rules(ca_xml_resp.text) == []
    ca_root = _parse_xml(ca_xml_resp.text)
    assert ca_root.findtext("cbc:OrderResponseCode", None, NS) == "CA"
    xml_lines = ca_root.findall("cac:OrderLine/cac:LineItem", NS)
    assert len(xml_lines) == 2
    assert [li.findtext("cbc:LineStatusCode", None, NS) for li in xml_lines] == ["5", "3"]
    changed_qty = xml_lines[1].find("cbc:Quantity", NS)
    assert changed_qty is not None and changed_qty.text == "25" and changed_qty.get("unitCode") == "H87"
    xml_pids = [li.findtext("cac:Item/cac:SellersItemIdentification/cbc:ID", None, NS) for li in xml_lines]
    assert xml_pids == ["470889", "470863"]
    for pid in xml_pids:
        assert pid is not None
        _assert_structured_free(pid, ("milk", "nuts"))

    direct_xml = order_response_xml(
        Order.model_validate(placed),
        Quote.model_validate(q),
        gtins={"470889": "07394376616228", "470863": "07394376616235"},
    )
    assert check_t116_rules(direct_xml) == []
    gtin_ids = [
        el.text
        for el in _parse_xml(direct_xml).findall(
            "cac:OrderLine/cac:LineItem/cac:Item/cac:StandardItemIdentification/cbc:ID",
            NS,
        )
    ]
    assert gtin_ids == ["07394376616228", "07394376616235"]


def test_safe_hospital_substitute_plan_dietitian_session_approval_peppol_ap(env) -> None:
    api, backend, _ = env
    backend.stock["473328"] = 0.0
    jwks = api.get("/.well-known/jwks.json").json()

    r_plan = api.post(
        "/v1/plan",
        headers=H_HOSPITAL,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "dark sliced bread",
            "preferred_product_ids": ["473328"],
            "quantity": 6.0,
            "unit": "KG",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan = r_plan.json()
    assert plan["lines"]
    for ln in plan["lines"]:
        assert ln["product_id"] in ("461765", "473399")
        _assert_structured_free(ln["product_id"], ("gluten",))

    r_quote = api.post(
        "/v1/quotes",
        headers=H_HOSPITAL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, "hospital-meilahti")

    r_order = api.post(
        "/v1/orders",
        headers=H_HOSPITAL,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    pending = r_order.json()
    assert pending["status"] == "pending_approval"
    assert pending["decision"]["approver_role"] == "dietitian"
    assert _parse_xml(api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml").text).findtext(
        "cbc:OrderResponseCode", None, NS
    ) == "AB"

    sess_token = api.post(
        "/v1/auth/approver-session",
        headers=PIN_HEADER,
        json={"approver": HOSPITAL_DIETITIAN},
    ).json()["session"]
    r_approve = api.post(
        f"/v1/approvals/{pending['order_id']}/decision",
        headers={"X-Approver-Session": sess_token},
        json={"decision": "approve", "quote_hash": q["quote_hash"], "note": "Clinical gluten-free check passed"},
    )
    assert r_approve.status_code == 200, r_approve.text
    placed = r_approve.json()
    assert placed["status"] == "placed"
    assert placed["approved_by"] == HOSPITAL_DIETITIAN
    assert placed["response_code"] == "AP"
    for rl in placed["response_lines"]:
        _assert_structured_free(rl["product_id"], ("gluten",))

    ap_xml = api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml").text
    assert check_t116_rules(ap_xml) == []
    assert _parse_xml(ap_xml).findtext("cbc:OrderResponseCode", None, NS) == "AP"


@pytest.mark.parametrize(
    "customer_id,headers,pid,expected_flag,primary_approver,override_approver",
    [
        ("school-tampere", H_SCHOOL, "464306", "contains:milk", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "471425", "may_contain:milk", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "471424", "contains:nuts", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "471426", "may_contain:nuts", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "472833", "allergen_unknown", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "472899", "allergen_unknown", SCHOOL_APPROVER, SCHOOL_DIRECTOR),
        ("hospital-meilahti", H_HOSPITAL, "501100", "contains:gluten", HOSPITAL_DIETITIAN, HOSPITAL_HEAD),
        ("hospital-meilahti", H_HOSPITAL, "501102", "may_contain:gluten", HOSPITAL_DIETITIAN, HOSPITAL_HEAD),
        ("hospital-meilahti", H_HOSPITAL, "501101", "allergen_unknown", HOSPITAL_DIETITIAN, HOSPITAL_HEAD),
        ("hospital-meilahti", H_HOSPITAL, "501199", "allergen_unknown", HOSPITAL_DIETITIAN, HOSPITAL_HEAD),
    ],
)
def test_adversarial_unsafe_direct_quote_marks_rejected_blocks_order_and_never_exports_peppol_ap(
    env,
    customer_id: str,
    headers: dict[str, str],
    pid: str,
    expected_flag: str,
    primary_approver: str,
    override_approver: str,
) -> None:
    api, _, _ = env
    jwks = api.get("/.well-known/jwks.json").json()

    r_quote = api.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": pid, "qty_sales_units": 5, "tier": "preferred", "flags": []}],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, customer_id)

    qline = q["lines"][0]
    assert qline["tier"] == "rejected"
    assert expected_flag in qline["flags"]
    verif = q["provenance"]["line_verification"][pid]
    assert verif["tier"] == "rejected"
    assert q["provenance"]["client_claims"] == [{"product_id": pid, "tier": "preferred", "flags": []}]

    q_events = api.get("/v1/audit", params={"subject_id": q["quote_id"]}).json()
    assert any(ev["action"] == "quote.claims_corrected" for ev in q_events)

    r_order = api.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    order = r_order.json()
    assert order["status"] == "rejected"
    assert order["decision"]["outcome"] == "reject"
    assert order.get("response_code") in (None, "RE")
    assert header_code(Order.model_validate(order)) == "RE"
    assert any(h["outcome"] == "reject" for h in order["decision"]["hits"])

    inbox = api.get("/v1/approvals", params={"status": "pending", "customer_id": customer_id}, headers=PIN_HEADER).json()
    assert all(item["order"]["order_id"] != order["order_id"] for item in inbox)

    pin_attempt = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=PIN_HEADER,
        json={"decision": "approve", "approver": primary_approver, "quote_hash": q["quote_hash"]},
    )
    assert pin_attempt.status_code == 409
    assert pin_attempt.json()["error"] == "not_pending"

    override_session = api.post(
        "/v1/auth/approver-session",
        headers=PIN_HEADER,
        json={"approver": override_approver},
    ).json()["session"]
    sess_attempt = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers={"X-Approver-Session": override_session},
        json={"decision": "approve", "quote_hash": q["quote_hash"], "note": "Emergency override attempt"},
    )
    assert sess_attempt.status_code == 409
    assert sess_attempt.json()["error"] == "not_pending"

    agent_attempt = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers=headers,
        json={"decision": "approve", "approver": primary_approver, "quote_hash": q["quote_hash"]},
    )
    assert agent_attempt.status_code == 403
    assert agent_attempt.json()["error"] == "agent_cannot_approve"

    reloaded = api.get(f"/v1/orders/{order['order_id']}").json()
    assert reloaded["status"] == "rejected"
    assert reloaded["approved_by"] is None

    re_xml_resp = api.get(f"/v1/orders/{order['order_id']}/peppol-order-response.xml")
    assert re_xml_resp.status_code == 200
    assert check_t116_rules(re_xml_resp.text) == []
    re_root = _parse_xml(re_xml_resp.text)
    assert re_root.findtext("cbc:OrderResponseCode", None, NS) == "RE"
    assert re_root.findtext("cbc:OrderResponseCode", None, NS) != "AP"
    assert len(re_root.findall("cac:OrderLine", NS)) == 0
    notes = [n.text or "" for n in re_root.findall("cbc:Note", NS)]
    assert any("Order rejected." in n for n in notes)
    assert any(n.startswith("Reason:") for n in notes)


def test_hospital_claim_only_gluten_free_bread_escalates_and_unsafe_mcp_direct_quote_rejects(env) -> None:
    api, _, _ = env
    r_claim = api.post(
        "/v1/quotes",
        headers=H_HOSPITAL,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": "501203", "qty_sales_units": 4, "tier": "preferred"}],
        },
    )
    assert r_claim.status_code == 200, r_claim.text
    q_claim = r_claim.json()
    o_claim = api.post(
        "/v1/orders",
        headers=H_HOSPITAL,
        json={"quote_id": q_claim["quote_id"], "quote_hash": q_claim["quote_hash"]},
    ).json()
    assert o_claim["status"] in ("pending_approval", "rejected")
    assert o_claim["status"] != "placed"
    assert _parse_xml(api.get(f"/v1/orders/{o_claim['order_id']}/peppol-order-response.xml").text).findtext(
        "cbc:OrderResponseCode", None, NS
    ) in ("AB", "RE")

    err_q, mcp_q = _mcp_call(
        H_SCHOOL,
        "create_quote",
        {"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 3}]},
    )
    assert err_q is False
    assert mcp_q["lines"][0]["tier"] == "rejected"
    assert "contains:milk" in mcp_q["lines"][0]["flags"]

    err_o, mcp_o = _mcp_call(
        H_SCHOOL,
        "place_order",
        {"quote_id": mcp_q["quote_id"], "quote_hash": mcp_q["quote_hash"]},
    )
    assert err_o is False
    assert mcp_o["status"] == "rejected"
    assert _parse_xml(api.get(f"/v1/orders/{mcp_o['order_id']}/peppol-order-response.xml").text).findtext(
        "cbc:OrderResponseCode", None, NS
    ) == "RE"


def test_tampered_quote_hash_and_stored_quote_tamper_rejected_cleanly(env) -> None:
    api, backend, state_db = env
    backend.stock["470889"] = 0.0
    jwks = api.get("/.well-known/jwks.json").json()

    q1 = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "470863", "qty_sales_units": 10}]},
    ).json()
    q2 = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "479001", "qty_sales_units": 10}]},
    ).json()
    _assert_quote_crypto(q1, jwks, "school-tampere")
    _assert_quote_crypto(q2, jwks, "school-tampere")

    flipped = ("0" if q1["quote_hash"][0] != "0" else "1") + q1["quote_hash"][1:]
    for bad_hash in ("0" * 64, flipped, q2["quote_hash"]):
        r_bad = api.post(
            "/v1/orders",
            headers=H_SCHOOL,
            json={"quote_id": q1["quote_id"], "quote_hash": bad_hash},
        )
        assert r_bad.status_code == 409
        body = r_bad.json()
        assert body["error"] == "quote_hash_mismatch"
        assert body["detail"]["expected"] == q1["quote_hash"]

    is_err, mcp_err = _mcp_call(
        H_SCHOOL,
        "place_order",
        {"quote_id": q1["quote_id"], "quote_hash": flipped},
    )
    assert is_err is True
    assert mcp_err["error"] == "quote_hash_mismatch"

    r_pending = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q1["quote_id"], "quote_hash": q1["quote_hash"]},
    )
    assert r_pending.status_code == 200
    pending = r_pending.json()
    assert pending["status"] == "pending_approval"

    sess_token = api.post("/v1/auth/approver-session", headers=PIN_HEADER, json={"approver": SCHOOL_APPROVER}).json()["session"]
    r_bad_approve = api.post(
        f"/v1/approvals/{pending['order_id']}/decision",
        headers={"X-Approver-Session": sess_token},
        json={"decision": "approve", "quote_hash": q2["quote_hash"]},
    )
    assert r_bad_approve.status_code == 409
    assert r_bad_approve.json()["error"] == "quote_hash_mismatch"
    assert api.get(f"/v1/orders/{pending['order_id']}").json()["status"] == "pending_approval"
    assert _parse_xml(api.get(f"/v1/orders/{pending['order_id']}/peppol-order-response.xml").text).findtext(
        "cbc:OrderResponseCode", None, NS
    ) == "AB"

    con = sqlite3.connect(str(state_db))
    try:
        raw_row = con.execute("SELECT body FROM gw_quotes WHERE quote_id = ?", (q2["quote_id"],)).fetchone()
        assert raw_row is not None
        tampered_doc = json.loads(raw_row[0])
        tampered_doc["lines"][0]["product_id"] = "464306"
        tampered_doc["total_net"] = 0.01
        con.execute("UPDATE gw_quotes SET body = ? WHERE quote_id = ?", (json.dumps(tampered_doc), q2["quote_id"]))
        con.commit()
    finally:
        con.close()

    assert V.verify_quote(api.get(f"/v1/quotes/{q2['quote_id']}").json(), jwks)["ok"] is False
    r_sig_bad = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]},
    )
    assert r_sig_bad.status_code == 409
    assert r_sig_bad.json()["error"] == "quote_signature_invalid"


def test_cross_customer_and_cross_agent_order_attempts_rejected_with_403(env) -> None:
    api, _, _ = env
    jwks = api.get("/.well-known/jwks.json").json()

    q_school = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "470889", "qty_sales_units": 10}]},
    ).json()
    q_hosp = api.post(
        "/v1/quotes",
        headers=H_HOSPITAL,
        json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "473328", "qty_sales_units": 10}]},
    ).json()
    _assert_quote_crypto(q_school, jwks, "school-tampere")
    _assert_quote_crypto(q_hosp, jwks, "hospital-meilahti")

    for intruder_headers in (
        H_HOSPITAL,
        H_HOTEL,
        {"X-Customer-Id": "intruder-kitchen", "X-Agent-Id": "buyer-1"},
    ):
        r_cross = api.post(
            "/v1/orders",
            headers=intruder_headers,
            json={"quote_id": q_school["quote_id"], "quote_hash": q_school["quote_hash"]},
        )
        assert r_cross.status_code == 403, r_cross.text
        assert r_cross.json()["error"] == "customer_mismatch"

    r_rev = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q_hosp["quote_id"], "quote_hash": q_hosp["quote_hash"]},
    )
    assert r_rev.status_code == 403
    assert r_rev.json()["error"] == "customer_mismatch"

    r_body_mismatch = api.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "customer_id": "hospital-meilahti",
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": "470889", "qty_sales_units": 5}],
        },
    )
    assert r_body_mismatch.status_code == 403
    assert r_body_mismatch.json()["error"] == "customer_mismatch"

    is_err, mcp_cross = _mcp_call(
        H_HOSPITAL,
        "place_order",
        {"quote_id": q_school["quote_id"], "quote_hash": q_school["quote_hash"]},
    )
    assert is_err is True
    assert mcp_cross["error"] == "customer_mismatch"

    r_wrong_agent = api.post(
        "/v1/orders",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-intruder"},
        json={"quote_id": q_school["quote_id"], "quote_hash": q_school["quote_hash"]},
    )
    assert r_wrong_agent.status_code == 403
    assert r_wrong_agent.json()["error"] == "agent_mismatch"

    r_legit = api.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": q_school["quote_id"], "quote_hash": q_school["quote_hash"]},
    )
    assert r_legit.status_code == 200, r_legit.text
    placed = r_legit.json()
    assert placed["status"] == "placed"
    assert placed["customer_id"] == "school-tampere"
    assert placed["response_code"] == "AP"

    audit_events = api.get("/v1/audit", params={"subject_id": q_school["quote_id"]}).json()
    refusals = [ev for ev in audit_events if ev["action"] == "order.refused"]
    assert any((ev.get("data") or {}).get("reason") == "customer_mismatch" for ev in refusals)
    assert any((ev.get("data") or {}).get("reason") == "agent_mismatch" for ev in refusals)
    assert V.verify_events(api.get("/v1/audit").json(), jwks, contiguous=True)["ok"] is True


@pytest.mark.parametrize(
    "customer_id,headers,safe_pid,unsafe_pid,expected_flag,override_approver",
    [
        ("school-tampere", H_SCHOOL, "470889", "464306", "contains:milk", SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "470889", "471424", "contains:nuts", SCHOOL_DIRECTOR),
        ("school-tampere", H_SCHOOL, "470889", "472833", "allergen_unknown", SCHOOL_DIRECTOR),
        ("hospital-meilahti", H_HOSPITAL, "473328", "501100", "contains:gluten", HOSPITAL_HEAD),
    ],
)
def test_mixed_safe_and_unsafe_quote_poisons_whole_order_to_rejected_re(
    env,
    customer_id: str,
    headers: dict[str, str],
    safe_pid: str,
    unsafe_pid: str,
    expected_flag: str,
    override_approver: str,
) -> None:
    api, _, _ = env
    jwks = api.get("/.well-known/jwks.json").json()

    r_quote = api.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [
                {"product_id": safe_pid, "qty_sales_units": 5, "tier": "preferred"},
                {"product_id": unsafe_pid, "qty_sales_units": 2, "tier": "preferred"},
            ],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    q = r_quote.json()
    _assert_quote_crypto(q, jwks, customer_id)

    by_pid = {ln["product_id"]: ln for ln in q["lines"]}
    assert by_pid[safe_pid]["tier"] == "preferred"
    assert by_pid[unsafe_pid]["tier"] == "rejected"
    assert expected_flag in by_pid[unsafe_pid]["flags"]

    order = api.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    ).json()
    assert order["status"] == "rejected"
    assert header_code(Order.model_validate(order)) == "RE"

    override_session = api.post(
        "/v1/auth/approver-session",
        headers=PIN_HEADER,
        json={"approver": override_approver},
    ).json()["session"]
    r_override = api.post(
        f"/v1/approvals/{order['order_id']}/decision",
        headers={"X-Approver-Session": override_session},
        json={"decision": "approve", "quote_hash": q["quote_hash"]},
    )
    assert r_override.status_code == 409
    assert r_override.json()["error"] == "not_pending"

    xml_text = api.get(f"/v1/orders/{order['order_id']}/peppol-order-response.xml").text
    assert check_t116_rules(xml_text) == []
    assert _parse_xml(xml_text).findtext("cbc:OrderResponseCode", None, NS) == "RE"


def test_mcp_streamable_http_allergen_plan_quote_order_and_peppol_export(env) -> None:
    api, backend, _ = env
    backend.prices["470889"] = 2.00
    jwks = api.get("/.well-known/jwks.json").json()

    err_p, plan = _mcp_call(
        H_SCHOOL,
        "plan_need",
        {
            "need": {
                "customer_id": "school-tampere",
                "product_query": "oat drink",
                "preferred_product_ids": ["470889"],
                "quantity": 50.0,
                "unit": "L",
                "delivery_by": DELIVERY.isoformat(),
            }
        },
    )
    assert err_p is False
    assert plan["lines"]
    for ln in plan["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))

    err_q, quote = _mcp_call(
        H_SCHOOL,
        "create_quote",
        {
            "delivery_date": DELIVERY.isoformat(),
            "lines": [{"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"]} for ln in plan["lines"]],
            "need": plan["need"],
        },
    )
    assert err_q is False
    _assert_quote_crypto(quote, jwks, "school-tampere")
    assert quote["total_net"] == 100.0

    err_o, order = _mcp_call(
        H_SCHOOL,
        "place_order",
        {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert err_o is False
    assert order["status"] == "placed"
    assert order["response_code"] == "AP"

    err_st, status_doc = _mcp_call(H_SCHOOL, "get_order_status", {"order_id": order["order_id"]})
    assert err_st is False
    assert status_doc["status"] == "placed"
    assert status_doc["response_code"] == "AP"

    err_au, trail = _mcp_call(H_SCHOOL, "get_audit_trail", {"subject_id": order["order_id"]})
    assert err_au is False
    assert trail["verify"]["ok"] is True
    assert V.verify_events(trail["events"], jwks)["ok"] is True

    peppol_xml = api.get(f"/v1/orders/{order['order_id']}/peppol-order-response.xml").text
    assert check_t116_rules(peppol_xml) == []
    assert _parse_xml(peppol_xml).findtext("cbc:OrderResponseCode", None, NS) == "AP"

