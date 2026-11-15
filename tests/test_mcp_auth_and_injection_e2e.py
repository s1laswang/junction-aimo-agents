"""End-to-end MCP auth, prompt-injection screening, toywholesaler portability, and conformance suite tests."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from aimo import config
from aimo.agents.drift import next_delivery
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import human_auth
from aimo.gateway.mcp_server import _identity, build_mcp
from conformance.__main__ import default_day, run as run_conformance
from conformance.profile import MCP_FORBIDDEN

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
DELIVERY = "2026-11-16"
AUTO_APPROVE_DELIVERY = "2026-11-20"
APPROVER_PIN = "conformance-pin-2026"
AURORA_AGENT_KEY = "aurora-mcp-secret-2026"
WILDCARD_AGENT_KEY = "wildcard-mcp-secret-2026"

EXPECTED_13_MCP_TOOLS = {
    "search_products",
    "get_product",
    "get_offers",
    "plan_need",
    "create_quote",
    "place_order",
    "get_order_status",
    "get_audit_trail",
    "list_policies",
    "list_recipes",
    "plan_recipe",
    "check_standing_order_drift",
    "propose_offer",
}

CUSTOMER_SCOPED_MCP_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("search_products", {"query": "lactose free milk"}),
    ("get_product", {"product_id": "464306"}),
    ("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY}),
    (
        "plan_need",
        {
            "need": {
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "required_claims": ["FREE_FROM_LACTOSE"],
                "quantity": 20,
                "unit": "L",
                "delivery_by": DELIVERY,
                "preferred_product_ids": ["463038"],
            }
        },
    ),
    (
        "create_quote",
        {
            "delivery_date": DELIVERY,
            "lines": [{"product_id": "464306", "qty_sales_units": 5}],
        },
    ),
    ("place_order", {"quote_id": "Q-nonexistent", "quote_hash": "0" * 64}),
    ("list_policies", {}),
    ("plan_recipe", {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": DELIVERY}),
    ("check_standing_order_drift", {}),
]

H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _synthetic_valio_catalog_products() -> list[dict[str, Any]]:
    def _item(
        pid: str,
        sales: str,
        category: str,
        name_en: str,
        brand: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
        claims: list[str],
        allergens: list[str],
        free_allergens: list[str],
        marketing: str,
        ingredients: str,
    ) -> dict[str, Any]:
        allergen_values = [{"id": code, "unit": "CONTAINS"} for code in allergens] + [
            {"id": code, "unit": "FREE_FROM"} for code in free_allergens
        ]
        cls: list[dict[str, Any]] = []
        if allergen_values:
            cls.append({"name": "allergen", "type": "ENUM", "values": allergen_values})
        if claims:
            cls.append(
                {
                    "name": "nutritionalClaim",
                    "type": "ENUM",
                    "values": [{"id": claim, "unit": "FREE_FROM"} for claim in claims],
                }
            )
        cls.append({"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]})
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": category,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": unit_id, "sizeInBaseUnits": size} for unit_id, size in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": cls,
                "keyIngredients": [{"language": "en", "value": ingredients}],
                "marketingTexts": [{"language": "en", "value": marketing}],
            },
        }

    vol_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    vol_10l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 10.0}}]
    wt_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]
    wt_500g = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 0.5}}]
    return [
        _item(
            "463038",
            "KI",
            "10400",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            [("KI", 2), ("ST", 1)],
            [{"synkkaPackagingType": "CASE"}],
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            ["AW", "AN", "AP", "AE", "AS", "AY"],
            "Valio Eila lactose free dispenser milk drink for hotels and restaurants.",
            "milk, vitamin D",
        ),
        _item(
            "464306",
            "ST",
            "10400",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            vol_1l,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            ["AW", "AN", "AP", "AE", "AS", "AY"],
            "Valio Eila lactose free semi-skimmed milk drink 1 L.",
            "milk, vitamin D",
        ),
        _item(
            "469363",
            "ST",
            "10400",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            [("ST", 1)],
            vol_1l,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            ["AW", "AN", "AP", "AE", "AS", "AY"],
            "Arla lactose free semi-skimmed milk drink 1 L UHT.",
            "milk, vitamin D",
        ),
        _item(
            "462775",
            "ST",
            "10400",
            "Valio Eila whole milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            vol_1l,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            ["AW", "AN", "AP", "AE", "AS", "AY"],
            "Valio Eila lactose free whole milk drink 1 L.",
            "milk, vitamin D",
        ),
        _item(
            "900101",
            "ST",
            "11370",
            "Elovena oat flakes 1 kg",
            "Elovena",
            [("ST", 1)],
            wt_1kg,
            [],
            ["AW"],
            ["AM", "AN", "AP", "AE"],
            "Wholegrain Finnish oat flakes for porridge.",
            "wholegrain oats",
        ),
        _item(
            "402105",
            "ST",
            "10500",
            "Valio Eila lactose-free butter 500 g",
            "Valio",
            [("ST", 1)],
            wt_500g,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            ["AW", "AN", "AP", "AE"],
            "Lactose free Finnish butter for cooking and baking.",
            "cream, salt",
        ),
        _item(
            "460396",
            "CS",
            "10400",
            "Valio skimmed milk 10 l novobox",
            "Valio",
            [("CS", 1)],
            vol_10l,
            [],
            ["AM"],
            ["AW", "AN", "AP"],
            "Valio skimmed milk 10 L dispenser pack.",
            "skimmed milk, vitamin D",
        ),
        _item(
            "463982",
            "KI",
            "10800",
            "Kieku Barn Eggs L180 12300g",
            "Kieku",
            [("KI", 1)],
            wt_1kg,
            [],
            ["AE"],
            ["AM", "AW", "AN", "AP"],
            "Fresh Finnish barn eggs.",
            "eggs",
        ),
        _item(
            "460468",
            "KI",
            "17100",
            "Paulig Presidentti Tumma Paahto 18x300g",
            "Paulig",
            [("KI", 1)],
            wt_1kg,
            [],
            [],
            ["AM", "AW", "AN", "AP", "AE"],
            "Dark roast ground coffee for foodservice.",
            "roasted coffee",
        ),
        _item(
            "460884",
            "KI",
            "11200",
            "Reuter & Stolt Croissant butter 18 % 80 x 90g",
            "Reuter & Stolt",
            [("KI", 1)],
            wt_1kg,
            [],
            ["AW", "AM", "AE"],
            ["AN", "AP"],
            "Butter croissants ready to bake.",
            "wheat flour, butter, eggs",
        ),
    ]


def _prepare_valio_data_dir(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    product_json_path = data_dir / "valio_aimo_junction26_product_data.json"
    product_json_path.write_text(
        json.dumps(_synthetic_valio_catalog_products(), ensure_ascii=False),
        encoding="utf-8",
    )

    price_fix = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
    avail_fix = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
    price_by_id = {p["productId"]: p for p in price_fix["products"]}
    avail_by_id = {p["productId"]: p for p in avail_fix["products"]}

    extra_specs = [
        ("900101", 2.40, "KG", 2.40, "ST", 5000.0),
        ("402105", 3.80, "KG", 7.60, "ST", 5000.0),
        ("460396", 14.20, "L", 1.42, "CS", 500.0),
        ("463982", 38.00, "KG", 3.09, "KI", 500.0),
        ("460468", 89.00, "KG", 16.48, "KI", 500.0),
        ("460884", 54.00, "KG", 7.50, "KI", 500.0),
    ]
    for pid, net, cmp_unit, cmp_val, sales_unit, stock in extra_specs:
        gross = round(net * 1.135, 2)
        cmp_gross = round(cmp_val * 1.135, 2)
        price_by_id[pid] = {
            "productId": pid,
            "quantity": 1.0,
            "catalogPrice": {
                "value": net,
                "valueWithTax": gross,
                "validFrom": "2026-09-01T00:00:00+0300",
                "validTo": "9999-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "priceWithTax": {
                "value": gross,
                "validFrom": "2026-09-01T00:00:00+0300",
                "validTo": "9999-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "comparisonPrice": {
                "value": cmp_val,
                "valueWithTax": cmp_gross,
                "validFrom": "2026-09-01T00:00:00+0300",
                "validTo": "9999-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "comparisonUnit": cmp_unit,
            "tax": {"taxRate": 13.5, "taxValue": round(gross - net, 2)},
            "netPriceFound": False,
            "error": False,
        }
        avail_by_id[pid] = {
            "productId": pid,
            "closingTime": "2099-12-31T17:00:00+0200",
            "salesBlock": False,
            "logisticModel": "Stock",
            "scheduleLines": [
                {
                    "salesBlock": False,
                    "availableAmount": stock,
                    "availableDate": DELIVERY,
                    "salesUnit": sales_unit,
                }
            ],
        }

    snapshot_dates = sorted(
        {
            DELIVERY,
            AUTO_APPROVE_DELIVERY,
            default_day().isoformat(),
            next_delivery(0).isoformat(),
            next_delivery(1).isoformat(),
            next_delivery(3).isoformat(),
            (date.today() + timedelta(days=7)).isoformat(),
        }
    )
    replay_dir = data_dir / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    (replay_dir / "meta.json").write_text(
        json.dumps({"time_now": "2026-11-14T14:00:00+02:00", "dates": snapshot_dates}),
        encoding="utf-8",
    )

    for day_iso in snapshot_dates:
        p_doc = {
            "customerId": "123",
            "currency": "EUR",
            "pricingDate": f"{day_iso}T00:00:00+0200",
            "products": list(price_by_id.values()),
        }
        a_products = []
        for pid, raw_a in avail_by_id.items():
            item = json.loads(json.dumps(raw_a))
            for sl in item.get("scheduleLines", []):
                sl["availableDate"] = day_iso
            if pid != "463038" or day_iso != DELIVERY:
                item["closingTime"] = "2099-12-31T17:00:00+0200"
            a_products.append(item)
        a_doc = {
            "timeNow": "2026-11-14T14:00:00+0200",
            "requestedDate": f"{day_iso}T00:00:00+0200",
            "products": a_products,
        }
        (replay_dir / f"price_{day_iso}.json").write_text(json.dumps(p_doc), encoding="utf-8")
        (replay_dir / f"availability_{day_iso}.json").write_text(json.dumps(a_doc), encoding="utf-8")

    return replay_dir


def _boot_uvicorn(app_target: str, work_dir: Path, extra_env: dict[str, str]):
    port = _free_port()
    log_path = work_dir / "uvicorn.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(work_dir),
        "AIMO_APPROVER_PIN": APPROVER_PIN,
        "AIMO_SEMANTIC": "0",
        "AIMO_RECIPE_CHECK": "0",
        "GEMINI_API_KEY": "",
    }
    for clear_key in ("AIMO_AGENT_KEYS", "AIMO_AGENT_KEY", "AIMO_CUSTOMER_ID", "AIMO_AGENT_ID", "AIMO_FAULTS"):
        env.pop(clear_key, None)
    env.update(extra_env)

    log_file = open(log_path, "w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_target, "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=log_file,
        stderr=log_file,
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                return proc, log_file, base
        except Exception:
            time.sleep(0.2)
    log_file.seek(0)
    proc.kill()
    tail = log_file.read()[-2500:]
    log_file.close()
    pytest.fail(f"{app_target} failed to start:\n{tail}")


def _stop_uvicorn(proc: subprocess.Popen, log_file) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


@pytest.fixture(scope="module")
def live_valio_gateway(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("valio_mcp_e2e")
    replay_dir = _prepare_valio_data_dir(data_dir)
    proc, log_file, base = _boot_uvicorn(
        "aimo.gateway.app:app",
        data_dir,
        {
            "AIMO_REPLAY": str(replay_dir),
            "AIMO_NOW": "2026-11-14T14:00:00+02:00",
            "AIMO_NOW_FROZEN": "1",
        },
    )
    try:
        yield base, data_dir, replay_dir
    finally:
        _stop_uvicorn(proc, log_file)


@pytest.fixture(scope="module")
def live_valio_keyed_gateway(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("valio_mcp_keyed")
    replay_dir = _prepare_valio_data_dir(data_dir)
    agent_keys = json.dumps({"hotel-aurora": AURORA_AGENT_KEY, "*": WILDCARD_AGENT_KEY})
    proc, log_file, base = _boot_uvicorn(
        "aimo.gateway.app:app",
        data_dir,
        {
            "AIMO_REPLAY": str(replay_dir),
            "AIMO_NOW": "2026-11-14T14:00:00+02:00",
            "AIMO_NOW_FROZEN": "1",
            "AIMO_AGENT_KEYS": agent_keys,
        },
    )
    try:
        yield base
    finally:
        _stop_uvicorn(proc, log_file)


@pytest.fixture(scope="module")
def live_toy_gateway(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("toy_mcp_e2e")
    proc, log_file, base = _boot_uvicorn("toywholesaler.app:app", data_dir, {})
    try:
        yield base
    finally:
        _stop_uvicorn(proc, log_file)


@pytest.fixture
def inproc_valio_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    replay_dir = _prepare_valio_data_dir(tmp_path)
    pjson = tmp_path / "valio_aimo_junction26_product_data.json"
    cdb = tmp_path / "catalog.sqlite"
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    monkeypatch.setenv("AIMO_REPLAY", str(replay_dir))
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    for k in ("AIMO_FAULTS", "AIMO_AGENT_KEYS", "AIMO_AGENT_KEY", "AIMO_CUSTOMER_ID", "AIMO_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    human_auth.reset_for_tests()
    bc.set_client(None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    try:
        yield tmp_path, replay_dir
    finally:
        bc.set_client(None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _extract_mcp_result(res: Any) -> tuple[bool, Any, str]:
    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    chunks = [getattr(item, "text", "") for item in (getattr(res, "content", None) or [])]
    txt = " ".join(c for c in chunks if c).strip()
    if sc is not None:
        return is_err, sc, json.dumps(sc, ensure_ascii=False)
    parsed: Any = txt
    if txt:
        try:
            parsed = json.loads(txt)
        except ValueError:
            parsed = txt
    return is_err, parsed, txt


async def _http_mcp_list(mcp_url: str, headers: dict[str, str]) -> list[str]:
    http = create_mcp_http_client(headers=headers)
    async with http:
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            tools = await client.list_tools()
            return [t.name for t in tools.tools]


async def _http_mcp_call(mcp_url: str, headers: dict[str, str], name: str, args: dict[str, Any]) -> tuple[bool, Any, str]:
    http = create_mcp_http_client(headers=headers)
    async with http:
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            try:
                res = await client.call_tool(name, args)
            except Exception as exc:
                return True, {"error": str(exc)}, str(exc)
            return _extract_mcp_result(res)


async def _http_mcp_batch(
    mcp_url: str,
    headers: dict[str, str],
    calls: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, bool, Any, str]]:
    out: list[tuple[str, bool, Any, str]] = []
    http = create_mcp_http_client(headers=headers)
    async with http:
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            for name, args in calls:
                try:
                    res = await client.call_tool(name, args)
                    is_err, payload, txt = _extract_mcp_result(res)
                except Exception as exc:
                    is_err, payload, txt = True, {"error": str(exc)}, str(exc)
                out.append((name, is_err, payload, txt))
    return out


async def _inproc_mcp_batch(mcp_srv: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, bool, Any, str]]:
    out: list[tuple[str, bool, Any, str]] = []
    async with Client(mcp_srv) as client:
        for name, args in calls:
            try:
                res = await client.call_tool(name, args)
                is_err, payload, txt = _extract_mcp_result(res)
            except Exception as exc:
                is_err, payload, txt = True, {"error": str(exc)}, str(exc)
            out.append((name, is_err, payload, txt))
    return out


def _stdio_env(data_dir: Path, replay_dir: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(data_dir),
        "AIMO_REPLAY": str(replay_dir),
        "AIMO_NOW": "2026-11-14T14:00:00+02:00",
        "AIMO_NOW_FROZEN": "1",
        "AIMO_SEMANTIC": "0",
        "AIMO_RECIPE_CHECK": "0",
        "GEMINI_API_KEY": "",
    }
    for k in ("AIMO_AGENT_KEYS", "AIMO_AGENT_KEY", "AIMO_CUSTOMER_ID", "AIMO_AGENT_ID", "AIMO_FAULTS"):
        env.pop(k, None)
    env.update(extra)
    return env


def test_mcp_tool_registry_13_tools_and_zero_approval_tools(live_valio_gateway, live_toy_gateway, inproc_valio_env):
    valio_base, _, _ = live_valio_gateway
    data_dir, replay_dir = inproc_valio_env

    http_valio_tools = asyncio.run(_http_mcp_list(f"{valio_base}/mcp", H_AURORA))
    assert len(http_valio_tools) == 13
    assert set(http_valio_tools) == EXPECTED_13_MCP_TOOLS

    http_toy_tools = asyncio.run(_http_mcp_list(f"{live_toy_gateway}/mcp", H_AURORA))
    assert len(http_toy_tools) == 13
    assert set(http_toy_tools) == EXPECTED_13_MCP_TOOLS

    async def _list_inproc_and_stdio() -> tuple[list[str], list[str]]:
        async with Client(build_mcp()) as inproc_client:
            inproc_names = [t.name for t in (await inproc_client.list_tools()).tools]
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aimo.gateway.mcp_server"],
            env=_stdio_env(data_dir, replay_dir, {"AIMO_CUSTOMER_ID": "hotel-aurora", "AIMO_AGENT_ID": "buyer-1"}),
            cwd=str(ROOT),
        )
        async with Client(params) as stdio_client:
            stdio_names = [t.name for t in (await stdio_client.list_tools()).tools]
        return inproc_names, stdio_names

    inproc_tools, stdio_tools = asyncio.run(_list_inproc_and_stdio())
    assert len(inproc_tools) == 13 and set(inproc_tools) == EXPECTED_13_MCP_TOOLS
    assert len(stdio_tools) == 13 and set(stdio_tools) == EXPECTED_13_MCP_TOOLS

    for registered in (http_valio_tools, http_toy_tools, inproc_tools, stdio_tools):
        assert not [n for n in registered if n in MCP_FORBIDDEN]
        assert not [n for n in registered if "approv" in n.lower() or "decid" in n.lower() or "tamper" in n.lower()]

    for forbidden_name in MCP_FORBIDDEN:
        is_err, _, _ = asyncio.run(_http_mcp_call(f"{valio_base}/mcp", H_AURORA, forbidden_name, {}))
        assert is_err


def test_all_13_mcp_tools_functional_over_http_and_stdio(live_valio_gateway, inproc_valio_env):
    valio_base, _, _ = live_valio_gateway
    data_dir, replay_dir = inproc_valio_env

    async def _exercise_all_13(client: Client) -> None:
        r_search = _extract_mcp_result(await client.call_tool("search_products", {"query": "lactose free milk", "limit": 5}))
        assert not r_search[0] and any(p["id"] == "464306" for p in r_search[1]["products"])

        r_prod = _extract_mcp_result(await client.call_tool("get_product", {"product_id": "464306"}))
        assert not r_prod[0] and r_prod[1]["id"] == "464306" and "FREE_FROM_LACTOSE" in r_prod[1]["claims"]

        r_offers = _extract_mcp_result(
            await client.call_tool("get_offers", {"product_ids": ["463038", "464306"], "requested_date": DELIVERY})
        )
        assert not r_offers[0] and len(r_offers[1]["offers"]) == 2 and r_offers[1]["provenance"]

        need_payload = {
            "customer_id": "hotel-aurora",
            "product_query": "lactose-free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": 10,
            "unit": "L",
            "delivery_by": DELIVERY,
            "preferred_product_ids": ["463038"],
        }
        r_plan = _extract_mcp_result(await client.call_tool("plan_need", {"need": need_payload}))
        assert not r_plan[0] and r_plan[1]["lines"]
        for ln in r_plan[1]["lines"]:
            assert isinstance(ln["injection_screen"], dict) and ln["injection_screen"]["suspicious"] is False
        for cand in r_plan[1]["candidates"]:
            assert isinstance(cand["injection_screen"], dict)

        r_quote = _extract_mcp_result(
            await client.call_tool(
                "create_quote",
                {
                    "delivery_date": AUTO_APPROVE_DELIVERY,
                    "lines": [{"product_id": "463038", "qty_sales_units": 2, "tier": "preferred"}],
                },
            )
        )
        assert not r_quote[0]
        quote = r_quote[1]
        assert quote["quote_id"] and quote["quote_hash"] and quote["signature"]

        r_order = _extract_mcp_result(
            await client.call_tool("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
        )
        assert not r_order[0]
        order = r_order[1]
        assert order["status"] == "placed" and order["decision"]["outcome"] == "auto_approve"

        r_status = _extract_mcp_result(await client.call_tool("get_order_status", {"order_id": order["order_id"]}))
        assert not r_status[0] and r_status[1]["order_id"] == order["order_id"] and r_status[1]["status"] == "placed"

        r_audit = _extract_mcp_result(await client.call_tool("get_audit_trail", {"subject_id": order["order_id"]}))
        assert not r_audit[0] and r_audit[1]["verify"]["ok"] is True and len(r_audit[1]["events"]) >= 3

        r_pol = _extract_mcp_result(await client.call_tool("list_policies", {}))
        assert not r_pol[0] and r_pol[1]["policies"] and r_pol[1]["learned_swaps"]["customer_id"] == "hotel-aurora"

        r_recipes = _extract_mcp_result(await client.call_tool("list_recipes", {}))
        assert not r_recipes[0] and any(rcp["id"] == "oat-porridge" for rcp in r_recipes[1]["recipes"])

        r_plan_recipe = _extract_mcp_result(
            await client.call_tool("plan_recipe", {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": DELIVERY})
        )
        assert not r_plan_recipe[0]
        assert r_plan_recipe[1]["customer_id"] == "hotel-aurora"
        assert r_plan_recipe[1]["injection_flagged_lines"] == []
        for ing in r_plan_recipe[1]["ingredients"]:
            for ln in ing["plan"]["lines"]:
                assert isinstance(ln["injection_screen"], dict) and ln["injection_screen"]["suspicious"] is False
            for cand in ing["plan"]["candidates"]:
                assert isinstance(cand["injection_screen"], dict) and cand["injection_screen"]["suspicious"] is False

        r_drift = _extract_mcp_result(await client.call_tool("check_standing_order_drift", {}))
        assert not r_drift[0] and r_drift[1]["customer_id"] == "hotel-aurora" and "flags" in r_drift[1]

        r_prop = _extract_mcp_result(
            await client.call_tool(
                "propose_offer",
                {
                    "customer_id": "hotel-aurora",
                    "product_id": "464306",
                    "qty_sales_units": 12,
                    "delivery_date": DELIVERY,
                    "note": "weekly lactose-free milk",
                },
            )
        )
        assert not r_prop[0]
        assert r_prop[1]["proposal_id"] and r_prop[1]["customer_id"] == "hotel-aurora"
        assert r_prop[1]["policy_preview"]["binding"] is False

    async def _run_both_transports() -> None:
        http = create_mcp_http_client(headers=H_AURORA)
        async with http:
            async with Client(streamable_http_client(f"{valio_base}/mcp", http_client=http)) as http_client:
                await _exercise_all_13(http_client)

        stdio_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aimo.gateway.mcp_server"],
            env=_stdio_env(data_dir, replay_dir, {"AIMO_CUSTOMER_ID": "hotel-aurora", "AIMO_AGENT_ID": "buyer-1"}),
            cwd=str(ROOT),
        )
        async with Client(stdio_params) as stdio_client:
            await _exercise_all_13(stdio_client)

    asyncio.run(_run_both_transports())


def test_mcp_http_and_stdio_reject_unknown_and_unsafe_customers(live_valio_gateway, inproc_valio_env, monkeypatch):
    valio_base, _, _ = live_valio_gateway
    data_dir, replay_dir = inproc_valio_env
    mcp_url = f"{valio_base}/mcp"

    http_bad_customers = [
        "rogue-corp",
        "../etc/passwd",
        "hotel-aurora/../../etc/passwd",
        "hotel\\aurora",
        "c" * 129,
        "x" * 200,
    ]
    for bad_cid in http_bad_customers:
        hdrs = {"X-Customer-Id": bad_cid, "X-Agent-Id": "buyer-1"}
        results = asyncio.run(_http_mcp_batch(mcp_url, hdrs, CUSTOMER_SCOPED_MCP_TOOLS))
        for tool_name, is_err, _, txt in results:
            assert is_err and "unknown_customer" in txt, (bad_cid, tool_name, is_err, txt)

    arg_bad_customers = [
        "rogue-corp",
        "../etc/passwd",
        "hotel\\aurora",
        "\x00",
        "hotel-aurora\x07",
        "\x1f",
        "\x7f",
        "c" * 135,
    ]
    arg_calls: list[tuple[str, dict[str, Any]]] = []
    for bad_cid in arg_bad_customers:
        arg_calls.extend(
            [
                (
                    "plan_need",
                    {
                        "need": {
                            "customer_id": bad_cid,
                            "product_query": "lactose-free milk",
                            "quantity": 10,
                            "unit": "L",
                            "delivery_by": DELIVERY,
                        }
                    },
                ),
                ("check_standing_order_drift", {"customer_id": bad_cid}),
                ("propose_offer", {"customer_id": bad_cid, "product_id": "464306", "qty_sales_units": 5}),
            ]
        )
    arg_results = asyncio.run(_http_mcp_batch(mcp_url, {}, arg_calls))
    for tool_name, is_err, _, txt in arg_results:
        assert is_err and "unknown_customer" in txt, (tool_name, is_err, txt)

    srv = build_mcp()
    env_bad_customers = [
        "rogue-corp",
        "../etc/passwd",
        "hotel\\aurora",
        "\x1f",
        "hotel-aurora\x1f",
        "\x07",
        "\x7f",
        "z" * 129,
    ]
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    for bad_cid in env_bad_customers:
        monkeypatch.setenv("AIMO_CUSTOMER_ID", bad_cid)
        results = asyncio.run(_inproc_mcp_batch(srv, CUSTOMER_SCOPED_MCP_TOOLS))
        for tool_name, is_err, _, txt in results:
            assert is_err and "unknown_customer" in txt, (bad_cid, tool_name, is_err, txt)

    class _FakeCtx:
        def __init__(self, headers: dict[str, str]):
            self.headers = headers

    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    for ctrl_cid in ("\x00", "hotel-aurora\x00", "hotel\naurora", "\x1f", "\x7f", "../etc/passwd", "q" * 129):
        with pytest.raises(Exception) as exc_info:
            _identity(_FakeCtx({"x-customer-id": ctrl_cid, "x-agent-id": "buyer-1"}), strict=True)
        assert "unknown_customer" in str(exc_info.value)

    async def _stdio_rejects_control_and_unknown() -> None:
        for stdio_bad in ("rogue-corp", "../etc/passwd", "\x1f", "k" * 129):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "aimo.gateway.mcp_server"],
                env=_stdio_env(data_dir, replay_dir, {"AIMO_CUSTOMER_ID": stdio_bad, "AIMO_AGENT_ID": "buyer-1"}),
                cwd=str(ROOT),
            )
            async with Client(params) as stdio_client:
                for tool_name, tool_args in CUSTOMER_SCOPED_MCP_TOOLS:
                    res = await stdio_client.call_tool(tool_name, tool_args)
                    is_err, _, txt = _extract_mcp_result(res)
                    assert is_err and "unknown_customer" in txt, (stdio_bad, tool_name, is_err, txt)

    asyncio.run(_stdio_rejects_control_and_unknown())


def test_mcp_strict_identity_required_on_create_quote_and_place_order(live_valio_gateway, inproc_valio_env, monkeypatch):
    valio_base, _, _ = live_valio_gateway
    mcp_url = f"{valio_base}/mcp"
    strict_calls = [
        ("create_quote", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 5}]}),
        ("place_order", {"quote_id": "Q-1", "quote_hash": "0" * 64}),
    ]

    incomplete_header_sets = [
        {},
        {"X-Customer-Id": "hotel-aurora"},
        {"X-Agent-Id": "buyer-1"},
        {"X-Customer-Id": "", "X-Agent-Id": "buyer-1"},
        {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": ""},
    ]
    for hdrs in incomplete_header_sets:
        results = asyncio.run(_http_mcp_batch(mcp_url, hdrs, strict_calls))
        for tool_name, is_err, _, txt in results:
            assert is_err and "identity_required" in txt, (hdrs, tool_name, is_err, txt)

    srv = build_mcp()
    env_cases = [
        (None, None),
        ("hotel-aurora", None),
        (None, "buyer-1"),
        ("   ", "buyer-1"),
        ("hotel-aurora", "   "),
    ]
    for cid_val, aid_val in env_cases:
        if cid_val is None:
            monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
        else:
            monkeypatch.setenv("AIMO_CUSTOMER_ID", cid_val)
        if aid_val is None:
            monkeypatch.delenv("AIMO_AGENT_ID", raising=False)
        else:
            monkeypatch.setenv("AIMO_AGENT_ID", aid_val)
        results = asyncio.run(_inproc_mcp_batch(srv, strict_calls))
        for tool_name, is_err, _, txt in results:
            assert is_err and "identity_required" in txt, (cid_val, aid_val, tool_name, is_err, txt)


def test_mcp_agent_keys_enforcement_over_http_and_stdio(live_valio_keyed_gateway, inproc_valio_env, monkeypatch):
    mcp_url = f"{live_valio_keyed_gateway}/mcp"
    data_dir, replay_dir = inproc_valio_env

    missing_res = asyncio.run(_http_mcp_batch(mcp_url, H_AURORA, CUSTOMER_SCOPED_MCP_TOOLS))
    for tool_name, is_err, _, txt in missing_res:
        assert is_err and "agent_key_required" in txt, (tool_name, is_err, txt)

    wrong_hdrs = {**H_AURORA, "X-Agent-Key": "wrong-agent-key"}
    wrong_res = asyncio.run(_http_mcp_batch(mcp_url, wrong_hdrs, CUSTOMER_SCOPED_MCP_TOOLS))
    for tool_name, is_err, _, txt in wrong_res:
        assert is_err and "invalid_agent_key" in txt, (tool_name, is_err, txt)

    wildcard_on_aurora = {**H_AURORA, "X-Agent-Key": WILDCARD_AGENT_KEY}
    is_err_wc, _, txt_wc = asyncio.run(_http_mcp_call(mcp_url, wildcard_on_aurora, "search_products", {"query": "milk"}))
    assert is_err_wc and "invalid_agent_key" in txt_wc

    school_missing = asyncio.run(_http_mcp_call(mcp_url, H_SCHOOL, "search_products", {"query": "milk"}))
    assert school_missing[0] and "agent_key_required" in school_missing[2]

    school_right = asyncio.run(
        _http_mcp_call(mcp_url, {**H_SCHOOL, "X-Agent-Key": WILDCARD_AGENT_KEY}, "search_products", {"query": "milk"})
    )
    assert not school_right[0] and school_right[1]["products"]

    right_hdrs = {**H_AURORA, "X-Agent-Key": AURORA_AGENT_KEY}
    anon_prod = asyncio.run(_http_mcp_call(mcp_url, {}, "get_product", {"product_id": "464306"}))
    assert not anon_prod[0] and anon_prod[1]["id"] == "464306"

    ok_calls = [
        ("search_products", {"query": "lactose free milk"}),
        ("get_product", {"product_id": "464306"}),
        ("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY}),
        ("list_policies", {}),
        ("plan_recipe", {"recipe_id": "oat-porridge", "portions": 15, "delivery_by": DELIVERY}),
        ("check_standing_order_drift", {}),
        ("create_quote", {"delivery_date": AUTO_APPROVE_DELIVERY, "lines": [{"product_id": "463038", "qty_sales_units": 2}]}),
    ]
    ok_res = asyncio.run(_http_mcp_batch(mcp_url, right_hdrs, ok_calls))
    for tool_name, is_err, _, txt in ok_res:
        assert not is_err, (tool_name, txt)
    created_quote = ok_res[-1][2]
    placed = asyncio.run(
        _http_mcp_call(
            mcp_url,
            right_hdrs,
            "place_order",
            {"quote_id": created_quote["quote_id"], "quote_hash": created_quote["quote_hash"]},
        )
    )
    assert not placed[0] and placed[1]["status"] == "placed"

    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"hotel-aurora": AURORA_AGENT_KEY}))
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = build_mcp()

    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    inproc_missing = asyncio.run(_inproc_mcp_batch(srv, CUSTOMER_SCOPED_MCP_TOOLS))
    for tool_name, is_err, _, txt in inproc_missing:
        assert is_err and "agent_key_required" in txt, (tool_name, txt)

    monkeypatch.setenv("AIMO_AGENT_KEY", "bad-stdio-key")
    inproc_wrong = asyncio.run(_inproc_mcp_batch(srv, CUSTOMER_SCOPED_MCP_TOOLS))
    for tool_name, is_err, _, txt in inproc_wrong:
        assert is_err and "invalid_agent_key" in txt, (tool_name, txt)

    async def _stdio_valid_key() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aimo.gateway.mcp_server"],
            env=_stdio_env(
                data_dir,
                replay_dir,
                {
                    "AIMO_AGENT_KEYS": json.dumps({"hotel-aurora": AURORA_AGENT_KEY}),
                    "AIMO_CUSTOMER_ID": "hotel-aurora",
                    "AIMO_AGENT_ID": "buyer-1",
                    "AIMO_AGENT_KEY": AURORA_AGENT_KEY,
                },
            ),
            cwd=str(ROOT),
        )
        async with Client(params) as stdio_client:
            res = await stdio_client.call_tool("get_product", {"product_id": "464306"})
            is_err, payload, _ = _extract_mcp_result(res)
            assert not is_err and payload["id"] == "464306"

    asyncio.run(_stdio_valid_key())


def test_mcp_http_fault_injection_screen_on_plan_need_and_plan_recipe_forces_pending_approval(live_valio_gateway):
    valio_base, _, _ = live_valio_gateway
    mcp_url = f"{valio_base}/mcp"
    milk_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 20,
        "unit": "L",
        "delivery_by": DELIVERY,
        "preferred_product_ids": ["463038"],
    }

    clean_batch = asyncio.run(
        _http_mcp_batch(
            mcp_url,
            H_AURORA,
            [
                ("plan_need", {"need": milk_need}),
                (
                    "create_quote",
                    {
                        "delivery_date": AUTO_APPROVE_DELIVERY,
                        "lines": [{"product_id": "463038", "qty_sales_units": 2, "tier": "preferred"}],
                    },
                ),
            ],
        )
    )
    assert not clean_batch[0][1] and not clean_batch[1][1]
    clean_plan = clean_batch[0][2]
    clean_quote = clean_batch[1][2]
    clean_line = next(ln for ln in clean_plan["lines"] if ln["product_id"] == "464306")
    assert clean_line["injection_screen"]["suspicious"] is False
    assert "regex" in clean_line["injection_screen"]["layers"]
    assert "injected" not in clean_line["injection_screen"]

    err_clean_ord, clean_order, _ = asyncio.run(
        _http_mcp_call(
            mcp_url,
            H_AURORA,
            "place_order",
            {"quote_id": clean_quote["quote_id"], "quote_hash": clean_quote["quote_hash"]},
        )
    )
    assert not err_clean_ord
    assert clean_order["status"] == "placed"
    assert clean_order["decision"]["outcome"] == "auto_approve"

    err_inj_usual, inj_usual_batch, _ = (
        False,
        asyncio.run(
            _http_mcp_batch(
                mcp_url,
                {**H_AURORA, "X-Aimo-Faults": "inject_text:463038"},
                [
                    (
                        "create_quote",
                        {
                            "delivery_date": AUTO_APPROVE_DELIVERY,
                            "lines": [{"product_id": "463038", "qty_sales_units": 2, "tier": "preferred"}],
                        },
                    )
                ],
            )
        ),
        "",
    )
    assert not err_inj_usual and not inj_usual_batch[0][1]
    q_usual_inj = inj_usual_batch[0][2]
    err_ord_usual_inj, ord_usual_inj, _ = asyncio.run(
        _http_mcp_call(
            mcp_url,
            {**H_AURORA, "X-Aimo-Faults": "inject_text:463038"},
            "place_order",
            {"quote_id": q_usual_inj["quote_id"], "quote_hash": q_usual_inj["quote_hash"]},
        )
    )
    assert not err_ord_usual_inj
    assert ord_usual_inj["status"] == "pending_approval"
    assert [
        h["rule_id"] for h in ord_usual_inj["decision"]["hits"] if h["outcome"] == "needs_approval"
    ] == ["injection:463038"]

    inj_headers = {**H_AURORA, "X-Aimo-Faults": "inject_text:464306"}

    async def _run_injected_need_and_recipe() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        http = create_mcp_http_client(headers=inj_headers)
        async with http:
            async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
                err_pn, p_need, txt_pn = _extract_mcp_result(await client.call_tool("plan_need", {"need": milk_need}))
                assert not err_pn, txt_pn

                err_qn, q_need, txt_qn = _extract_mcp_result(
                    await client.call_tool(
                        "create_quote",
                        {
                            "delivery_date": DELIVERY,
                            "lines": [{"product_id": "464306", "qty_sales_units": 20, "tier": "best"}],
                            "need": milk_need,
                        },
                    )
                )
                assert not err_qn, txt_qn

                err_on, o_need, txt_on = _extract_mcp_result(
                    await client.call_tool(
                        "place_order",
                        {"quote_id": q_need["quote_id"], "quote_hash": q_need["quote_hash"]},
                    )
                )
                assert not err_on, txt_on

                err_pr, p_rec, txt_pr = _extract_mcp_result(
                    await client.call_tool(
                        "plan_recipe",
                        {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": DELIVERY},
                    )
                )
                assert not err_pr, txt_pr

                qr = p_rec["quote_request"]
                err_qr, q_rec, txt_qr = _extract_mcp_result(
                    await client.call_tool(
                        "create_quote",
                        {"delivery_date": qr["delivery_date"], "lines": qr["lines"]},
                    )
                )
                assert not err_qr, txt_qr

                err_or, o_rec, txt_or = _extract_mcp_result(
                    await client.call_tool(
                        "place_order",
                        {"quote_id": q_rec["quote_id"], "quote_hash": q_rec["quote_hash"]},
                    )
                )
                assert not err_or, txt_or
                return p_need, o_need, p_rec, o_rec

    inj_plan, inj_need_order, inj_recipe, inj_recipe_order = asyncio.run(_run_injected_need_and_recipe())

    ln_464 = next(ln for ln in inj_plan["lines"] if ln["product_id"] == "464306")
    assert ln_464["injection_screen"]["suspicious"] is True
    assert ln_464["injection_screen"]["injected"] is True
    assert ln_464["injection_screen"]["reasons"]
    assert "regex" in ln_464["injection_screen"]["layers"]
    assert "supplier_text_injection_suspected" in ln_464["flags"]
    assert "injected:inject_text" in ln_464["flags"]
    assert any("prompt-injection screen" in r.lower() for r in ln_464["reasons"])
    assert "prompt-injection screen" in inj_plan["explanation"].lower()

    cands_by_id = {c["product"]["id"]: c for c in inj_plan["candidates"]}
    assert "464306" in cands_by_id
    c_464 = cands_by_id["464306"]
    assert c_464["injection_screen"]["suspicious"] is True
    assert c_464["injection_screen"]["injected"] is True
    assert c_464["injection_screen"]["reasons"]
    assert "regex" in c_464["injection_screen"]["layers"]
    assert "supplier_text_injection_suspected" in c_464["flags"]
    if "469363" in cands_by_id:
        assert cands_by_id["469363"]["injection_screen"]["suspicious"] is False

    assert inj_need_order["status"] == "pending_approval"
    assert inj_need_order["decision"]["outcome"] == "needs_approval"
    need_inj_hits = [
        h for h in inj_need_order["decision"]["hits"] if "injection" in h["rule_id"] and h["outcome"] == "needs_approval"
    ]
    assert need_inj_hits and "464306" in need_inj_hits[0]["rule_id"]

    assert inj_recipe["injection_flagged_lines"]
    flagged_464 = next(x for x in inj_recipe["injection_flagged_lines"] if x["product_id"] == "464306")
    assert flagged_464["injection_screen"]["suspicious"] is True
    assert flagged_464["injection_screen"]["injected"] is True
    assert flagged_464["injection_screen"]["reasons"]
    assert "regex" in flagged_464["injection_screen"]["layers"]
    assert "prompt-injection screen flagged supplier text on 464306" in inj_recipe["summary"]

    milk_ing = next(ing for ing in inj_recipe["ingredients"] if ing["ingredient"] == "milk")
    rec_ln_464 = next(ln for ln in milk_ing["plan"]["lines"] if ln["product_id"] == "464306")
    assert rec_ln_464["injection_screen"]["suspicious"] is True
    assert rec_ln_464["injection_screen"]["injected"] is True
    assert rec_ln_464["injection_screen"]["reasons"]
    assert "regex" in rec_ln_464["injection_screen"]["layers"]

    rec_cand_464 = next(c for c in milk_ing["plan"]["candidates"] if c["product_id"] == "464306")
    assert rec_cand_464["injection_screen"]["suspicious"] is True
    assert rec_cand_464["injection_screen"]["injected"] is True
    assert rec_cand_464["injection_screen"]["reasons"]
    assert "regex" in rec_cand_464["injection_screen"]["layers"]

    assert inj_recipe_order["status"] == "pending_approval"
    assert inj_recipe_order["decision"]["outcome"] == "needs_approval"
    rec_inj_hits = [
        h for h in inj_recipe_order["decision"]["hits"] if "injection" in h["rule_id"] and h["outcome"] == "needs_approval"
    ]
    assert rec_inj_hits and "464306" in rec_inj_hits[0]["rule_id"]


def test_toywholesaler_portability_auth_and_plan_injection_screen(live_toy_gateway):
    base = live_toy_gateway
    toy_day = (date.today() + timedelta(days=5)).isoformat()

    for known_hdr, query, pid in [
        (H_AURORA, "rye bread", "NB1001"),
        (H_HOSPITAL, "gluten-free bread", "NB1011"),
    ]:
        cid = known_hdr["X-Customer-Id"]
        r_search = httpx.get(f"{base}/v1/products/search", params={"q": query}, headers=known_hdr, timeout=10.0)
        assert r_search.status_code == 200, (cid, r_search.text)
        assert any(p["id"] == pid for p in r_search.json())

        r_plan = httpx.post(
            f"{base}/v1/plan",
            headers=known_hdr,
            json={
                "customer_id": cid,
                "product_query": query,
                "quantity": 10,
                "unit": "KG",
                "delivery_by": toy_day,
            },
            timeout=10.0,
        )
        assert r_plan.status_code == 200, (cid, r_plan.text)
        plan_body = r_plan.json()
        assert plan_body["lines"] and plan_body["candidates"]
        for ln in plan_body["lines"]:
            assert isinstance(ln["injection_screen"], dict)
            assert ln["injection_screen"]["suspicious"] is False
            assert "regex" in ln["injection_screen"]["layers"]
        for cand in plan_body["candidates"]:
            assert isinstance(cand["injection_screen"], dict)
            assert cand["injection_screen"]["suspicious"] is False
            assert "regex" in cand["injection_screen"]["layers"]

        r_quote = httpx.post(
            f"{base}/v1/quotes",
            headers=known_hdr,
            json={"delivery_date": toy_day, "lines": [{"product_id": pid, "qty_sales_units": 2}]},
            timeout=10.0,
        )
        assert r_quote.status_code == 200, (cid, r_quote.text)
        assert r_quote.json()["quote_hash"] and r_quote.json()["signature"]

    for rejected_cid in ("school-tampere", "rogue-corp", "../etc/passwd", "c" * 140):
        bad_hdr = {"X-Customer-Id": rejected_cid, "X-Agent-Id": "buyer-1"}

        r_s = httpx.get(f"{base}/v1/products/search", params={"q": "rye bread"}, headers=bad_hdr, timeout=10.0)
        assert r_s.status_code == 403 and r_s.json()["error"] == "unknown_customer", (rejected_cid, r_s.text)

        r_p = httpx.post(
            f"{base}/v1/plan",
            headers=bad_hdr,
            json={
                "customer_id": rejected_cid,
                "product_query": "rye bread",
                "quantity": 5,
                "unit": "KG",
                "delivery_by": toy_day,
            },
            timeout=10.0,
        )
        assert r_p.status_code == 403 and r_p.json()["error"] == "unknown_customer", (rejected_cid, r_p.text)

        r_q = httpx.post(
            f"{base}/v1/quotes",
            headers=bad_hdr,
            json={"delivery_date": toy_day, "lines": [{"product_id": "NB1001", "qty_sales_units": 2}]},
            timeout=10.0,
        )
        assert r_q.status_code == 403 and r_q.json()["error"] == "unknown_customer", (rejected_cid, r_q.text)

        err_mcp, _, txt_mcp = asyncio.run(
            _http_mcp_call(f"{base}/mcp", bad_hdr, "search_products", {"query": "rye bread"})
        )
        assert err_mcp and "unknown_customer" in txt_mcp, (rejected_cid, txt_mcp)

    r_inj_toy = httpx.post(
        f"{base}/v1/plan",
        headers={**H_AURORA, "X-Aimo-Faults": "inject_text:NB1001"},
        json={
            "customer_id": "hotel-aurora",
            "product_query": "rye bread",
            "quantity": 10,
            "unit": "KG",
            "delivery_by": toy_day,
            "preferred_product_ids": ["NB1001"],
        },
        timeout=10.0,
    )
    assert r_inj_toy.status_code == 200, r_inj_toy.text
    inj_toy_plan = r_inj_toy.json()
    nb_line = next(ln for ln in inj_toy_plan["lines"] if ln["product_id"] == "NB1001")
    assert nb_line["injection_screen"]["suspicious"] is True
    assert nb_line["injection_screen"]["injected"] is True
    assert nb_line["injection_screen"]["reasons"]
    assert "regex" in nb_line["injection_screen"]["layers"]
    nb_cand = next(c for c in inj_toy_plan["candidates"] if c["product"]["id"] == "NB1001")
    assert nb_cand["injection_screen"]["suspicious"] is True
    assert nb_cand["injection_screen"]["injected"] is True


def test_conformance_passes_against_both_valio_and_toywholesaler_gateways(
    live_valio_gateway,
    live_toy_gateway,
    monkeypatch,
):
    valio_base, _, _ = live_valio_gateway
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)

    rep_valio = run_conformance(valio_base, query="lactose free milk", day=date.fromisoformat(DELIVERY), quiet=True)
    valio_non_pass = [row for row in rep_valio["results"] if row["status"] != "PASS"]
    assert not valio_non_pass, valio_non_pass
    assert rep_valio["name"] == "Valio Aimo Trusted Trade Gateway"
    assert rep_valio["summary"]["fail"] == 0
    assert rep_valio["summary"]["skip"] == 0
    assert rep_valio["summary"]["pass"] == 44
    assert rep_valio["summary"]["total"] == 44

    rep_toy = run_conformance(live_toy_gateway, query="rye bread", quiet=True)
    toy_non_pass = [row for row in rep_toy["results"] if row["status"] != "PASS"]
    assert not toy_non_pass, toy_non_pass
    assert rep_toy["name"] == "Nordic Bakery Supply Trusted Trade Gateway"
    assert rep_toy["summary"]["fail"] == 0
    assert rep_toy["summary"]["skip"] == 0
    assert rep_toy["summary"]["pass"] == 44
    assert rep_toy["summary"]["total"] == 44


def test_mcp_contextvar_fault_preservation_inproc(inproc_valio_env, monkeypatch):
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = build_mcp()
    milk_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 15,
        "unit": "L",
        "delivery_by": DELIVERY,
        "preferred_product_ids": ["463038"],
    }

    tok = bc.set_request_faults("inject_text:464306")
    try:
        batch = asyncio.run(
            _inproc_mcp_batch(
                srv,
                [
                    ("plan_need", {"need": milk_need}),
                    ("plan_recipe", {"recipe_id": "oat-porridge", "portions": 10, "delivery_by": DELIVERY}),
                    (
                        "create_quote",
                        {
                            "delivery_date": DELIVERY,
                            "lines": [{"product_id": "464306", "qty_sales_units": 15, "tier": "best"}],
                            "need": milk_need,
                        },
                    ),
                ],
            )
        )
        assert not batch[0][1] and not batch[1][1] and not batch[2][1]
        plan_out, recipe_out, quote_out = batch[0][2], batch[1][2], batch[2][2]
        ord_res = asyncio.run(
            _inproc_mcp_batch(
                srv,
                [("place_order", {"quote_id": quote_out["quote_id"], "quote_hash": quote_out["quote_hash"]})],
            )
        )
        assert not ord_res[0][1]
        order_out = ord_res[0][2]
    finally:
        bc.reset_request_faults(tok)

    ln_464 = next(ln for ln in plan_out["lines"] if ln["product_id"] == "464306")
    assert ln_464["injection_screen"]["suspicious"] is True
    assert ln_464["injection_screen"]["injected"] is True
    assert recipe_out["injection_flagged_lines"]
    assert order_out["status"] == "pending_approval"
    assert any("injection" in h["rule_id"] and h["outcome"] == "needs_approval" for h in order_out["decision"]["hits"])


def test_toywholesaler_mcp_and_body_customer_portability(live_toy_gateway):
    base = live_toy_gateway
    mcp_url = f"{base}/mcp"
    toy_day = (date.today() + timedelta(days=5)).isoformat()

    assert httpx.get(f"{base}/v1/products/search", params={"q": "rye bread"}, timeout=10.0).status_code == 401
    assert (
        httpx.get(
            f"{base}/v1/products/search",
            params={"q": "rye bread", "customer_id": "school-tampere"},
            timeout=10.0,
        ).status_code
        == 403
    )
    assert (
        httpx.post(
            f"{base}/v1/plan",
            headers=H_AURORA,
            json={
                "customer_id": "hospital-meilahti",
                "product_query": "rye bread",
                "quantity": 5,
                "unit": "KG",
                "delivery_by": toy_day,
            },
            timeout=10.0,
        ).status_code
        == 403
    )

    for ctrl_body_cid in ("school-tampere", "rogue-corp", "hotel\x00aurora", "\x1f"):
        r_ctrl = httpx.post(
            f"{base}/v1/plan",
            json={
                "customer_id": ctrl_body_cid,
                "product_query": "rye bread",
                "quantity": 5,
                "unit": "KG",
                "delivery_by": toy_day,
            },
            timeout=10.0,
        )
        assert r_ctrl.status_code == 403 and r_ctrl.json()["error"] == "unknown_customer"

    toy_mcp_bad_calls = [
        ("search_products", {"query": "rye bread"}),
        ("get_product", {"product_id": "NB1001"}),
        ("get_offers", {"product_ids": ["NB1001"], "requested_date": toy_day}),
        (
            "plan_need",
            {
                "need": {
                    "customer_id": "school-tampere",
                    "product_query": "rye bread",
                    "quantity": 5,
                    "unit": "KG",
                    "delivery_by": toy_day,
                }
            },
        ),
        ("create_quote", {"delivery_date": toy_day, "lines": [{"product_id": "NB1001", "qty_sales_units": 2}]}),
        ("place_order", {"quote_id": "Q-none", "quote_hash": "0" * 64}),
        ("list_policies", {}),
    ]
    for bad_toy_hdr in (H_SCHOOL, {"X-Customer-Id": "rogue-corp", "X-Agent-Id": "buyer-1"}):
        res_list = asyncio.run(_http_mcp_batch(mcp_url, bad_toy_hdr, toy_mcp_bad_calls))
        for tool_name, is_err, _, txt in res_list:
            assert is_err and "unknown_customer" in txt, (bad_toy_hdr, tool_name, txt)

    err_plan_mcp, toy_plan_mcp, txt_plan_mcp = asyncio.run(
        _http_mcp_call(
            mcp_url,
            {**H_AURORA, "X-Aimo-Faults": "inject_text:NB1001"},
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "rye bread",
                    "quantity": 10,
                    "unit": "KG",
                    "delivery_by": toy_day,
                    "preferred_product_ids": ["NB1001"],
                }
            },
        )
    )
    assert not err_plan_mcp, txt_plan_mcp
    assert toy_plan_mcp["lines"] and toy_plan_mcp["candidates"]
    for ln in toy_plan_mcp["lines"]:
        assert isinstance(ln["injection_screen"], dict)
    nb_mcp_ln = next(ln for ln in toy_plan_mcp["lines"] if ln["product_id"] == "NB1001")
    assert nb_mcp_ln["injection_screen"]["suspicious"] is True
    assert nb_mcp_ln["injection_screen"]["injected"] is True


def test_mcp_catalog_missing_returns_structured_tool_error(tmp_path, monkeypatch):
    from aimo import catalog as cat_mod

    monkeypatch.setattr(cat_mod, "catalog_available", lambda: False)
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    srv = build_mcp()

    calls = [
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
    ]
    results = asyncio.run(_inproc_mcp_batch(srv, calls))
    _, err_search, _, txt_search = results[0]
    assert err_search and "catalog_missing" in txt_search and "UnexpectedToolError" not in txt_search

    _, err_plan, _, txt_plan = results[1]
    assert err_plan and "catalog_missing" in txt_plan and "UnexpectedToolError" not in txt_plan

    _, err_bad_qty, _, txt_bad_qty = results[2]
    assert err_bad_qty and ("greater_than" in txt_bad_qty or "bad_quantity" in txt_bad_qty) and "catalog_missing" not in txt_bad_qty

    _, err_absurd_qty, _, txt_absurd_qty = results[3]
    assert err_absurd_qty and "absurd_quantity" in txt_absurd_qty and "catalog_missing" not in txt_absurd_qty

    _, err_bad_cust, _, txt_bad_cust = results[4]
    assert err_bad_cust and "customer_mismatch" in txt_bad_cust and "catalog_missing" not in txt_bad_cust

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


