from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HKI = timezone(timedelta(hours=2))
ALLOWED_GF = {"NB1011", "NB1012", "NB1025", "NB1026"}
FORBIDDEN_GLUTEN_BREADS = {f"NB{i}" for i in range(1001, 1011)}
FORBIDDEN_GLUTEN_FLOURS = {f"NB{i}" for i in range(1019, 1025)} | {"NB1027"}
FORBIDDEN_GLUTEN_ALL = FORBIDDEN_GLUTEN_BREADS | FORBIDDEN_GLUTEN_FLOURS | {"NB1016", "NB1017", "NB1018", "NB1030"}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _delivery_date() -> str:
    return (datetime.now(HKI).date() + timedelta(days=7)).isoformat()


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    d = tmp_path_factory.mktemp("toy_allergen")
    port = _port()
    log = open(d / "server.log", "w+", encoding="utf-8")
    env = {**os.environ, "AIMO_DATA_DIR": str(d), "GEMINI_API_KEY": ""}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "toywholesaler.app:app", "--port", str(port)],
        env=env,
        stdout=log,
        stderr=log,
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        log.seek(0)
        srv.kill()
        out = log.read()[-2000:]
        log.close()
        pytest.fail("toy wholesaler did not start:\n" + out)
    yield base, d
    srv.terminate()
    srv.wait(10)
    log.close()


def test_hospital_meilahti_policy_and_gluten_free_bread_plan(toy):
    base, _ = toy
    customers = {c["customer_id"]: c for c in httpx.get(base + "/v1/customers", timeout=5).json()}
    hosp = customers["hospital-meilahti"]
    assert hosp["excluded_allergens"] == ["gluten"]
    assert [p["product_id"] for p in hosp["preferred_products"]] == ["NB1011"]

    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
    resp = httpx.post(
        base + "/v1/plan",
        headers=headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "gluten-free bread",
            "excluded_allergens": ["gluten"],
            "preferred_product_ids": ["NB1011"],
            "quantity": 60,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    line_ids = {ln["product_id"] for ln in plan["lines"]}
    assert line_ids
    assert line_ids <= ALLOWED_GF
    assert line_ids <= {"NB1011", "NB1012"}
    assert line_ids.isdisjoint(FORBIDDEN_GLUTEN_ALL)
    assert plan["lines"][0]["product_id"] == "NB1011"
    assert plan["lines"][0]["tier"] == "preferred"

    for pid in line_ids:
        prod = httpx.get(f"{base}/v1/products/{pid}", timeout=5).json()
        assert "FREE_FROM_GLUTEN" in prod["claims"]
        assert prod["allergens"]["gluten"] == "free"

    for cand in plan["candidates"]:
        if cand["product"]["id"] in FORBIDDEN_GLUTEN_ALL:
            assert cand["tier"] == "rejected"


def test_hospital_meilahti_flour_plan_returns_only_gluten_free_flours(toy):
    base, _ = toy
    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
    resp = httpx.post(
        base + "/v1/plan",
        headers=headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "flour",
            "excluded_allergens": ["gluten"],
            "quantity": 250,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    line_ids = {ln["product_id"] for ln in plan["lines"]}
    assert line_ids
    assert line_ids <= ALLOWED_GF
    assert line_ids <= {"NB1025", "NB1026"}
    assert line_ids.isdisjoint(FORBIDDEN_GLUTEN_ALL)

    for pid in line_ids:
        prod = httpx.get(f"{base}/v1/products/{pid}", timeout=5).json()
        assert "FREE_FROM_GLUTEN" in prod["claims"]
        assert prod["allergens"]["gluten"] == "free"

    for cand in plan["candidates"]:
        if cand["product"]["id"] in FORBIDDEN_GLUTEN_ALL:
            assert cand["tier"] == "rejected"


def test_hospital_meilahti_forced_rye_bread_order_rejected_and_audited(toy):
    base, _ = toy
    nb1001 = httpx.get(base + "/v1/products/NB1001", timeout=5).json()
    assert nb1001["allergens"]["gluten"] == "contains"

    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
    q_resp = httpx.post(
        base + "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": _delivery_date(),
            "lines": [{"product_id": "NB1001", "qty_sales_units": 2}],
        },
        timeout=10,
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    assert quote["quote_hash"] and quote["signature"]

    o_resp = httpx.post(
        base + "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        timeout=10,
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "rejected"
    assert order["decision"]["outcome"] == "reject"
    assert order["decision"]["policy_id"] == "hospital-meilahti"

    allergen_hits = [
        h
        for h in order["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]
    assert allergen_hits
    msg = allergen_hits[0]["message"].lower()
    assert "gluten" in msg
    assert "nb1001" in msg

    verify = httpx.get(base + "/v1/audit/verify", timeout=5).json()
    assert verify["ok"] is True
    assert verify["broken_at"] is None
    assert verify["checked"] >= 3

    trail = httpx.get(base + "/v1/audit", params={"subject_id": order["order_id"]}, timeout=5).json()
    actions = {ev["action"] for ev in trail}
    assert "order.rejected" in actions


def test_hotel_aurora_multi_allergen_bread_and_hospital_rye_bread_empty_plan(toy):
    base, _ = toy
    aurora_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
    aurora_resp = httpx.post(
        base + "/v1/plan",
        headers=aurora_headers,
        json={
            "customer_id": "hotel-aurora",
            "product_query": "bread",
            "excluded_allergens": ["milk", "eggs", "sesame"],
            "quantity": 50,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert aurora_resp.status_code == 200, aurora_resp.text
    aurora_plan = aurora_resp.json()
    aurora_line_ids = {ln["product_id"] for ln in aurora_plan["lines"]}
    assert aurora_line_ids
    forbidden_mes = {"NB1005", "NB1007", "NB1008", "NB1010", "NB1012", "NB1014", "NB1015", "NB1018"}
    assert aurora_line_ids.isdisjoint(forbidden_mes)

    for pid in aurora_line_ids:
        prod = httpx.get(f"{base}/v1/products/{pid}", timeout=5).json()
        for allergen in ("milk", "eggs", "sesame"):
            assert prod["allergens"][allergen] == "free"

    for cand in aurora_plan["candidates"]:
        if cand["tier"] != "rejected":
            prod = httpx.get(f"{base}/v1/products/{cand['product']['id']}", timeout=5).json()
            for allergen in ("milk", "eggs", "sesame"):
                assert prod["allergens"][allergen] == "free"

    hosp_headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
    rye_resp = httpx.post(
        base + "/v1/plan",
        headers=hosp_headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "rye bread",
            "excluded_allergens": ["gluten"],
            "quantity": 10,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert rye_resp.status_code == 200, rye_resp.text
    rye_plan = rye_resp.json()
    assert rye_plan["lines"] == []
    assert rye_plan["covered_amount"] == 0

    by_id = {c["product"]["id"]: c for c in rye_plan["candidates"]}
    assert "NB1001" in by_id and by_id["NB1001"]["tier"] == "rejected"
    assert "NB1002" in by_id and by_id["NB1002"]["tier"] == "rejected"
    assert "gluten" in " ".join(by_id["NB1001"]["reasons"]).lower()
    assert "gluten" in " ".join(by_id["NB1002"]["reasons"]).lower()


def test_hospital_meilahti_omitted_excluded_allergens_bread_and_flour_hard_block(toy):
    base, _ = toy
    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}

    bread_resp = httpx.post(
        base + "/v1/plan",
        headers=headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "bread",
            "quantity": 60,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert bread_resp.status_code == 200, bread_resp.text
    bread_plan = bread_resp.json()
    assert "gluten" in bread_plan["need"]["excluded_allergens"]
    bread_ids = {ln["product_id"] for ln in bread_plan["lines"]}
    assert bread_ids
    assert bread_ids <= ALLOWED_GF
    assert bread_ids <= {"NB1011", "NB1012"}
    assert bread_ids.isdisjoint(FORBIDDEN_GLUTEN_ALL)
    for pid in bread_ids:
        prod = httpx.get(f"{base}/v1/products/{pid}", timeout=5).json()
        assert prod["allergens"]["gluten"] == "free"
        assert prod["allergen_source"]["gluten"].startswith("structured")
    for cand in bread_plan["candidates"]:
        cid = cand["product"]["id"]
        if cid in FORBIDDEN_GLUTEN_ALL:
            assert cand["tier"] == "rejected"
        if cand["tier"] != "rejected":
            prod = httpx.get(f"{base}/v1/products/{cid}", timeout=5).json()
            assert prod["allergens"]["gluten"] == "free"

    flour_resp = httpx.post(
        base + "/v1/plan",
        headers=headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "flour",
            "quantity": 250,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert flour_resp.status_code == 200, flour_resp.text
    flour_plan = flour_resp.json()
    assert "gluten" in flour_plan["need"]["excluded_allergens"]
    flour_ids = {ln["product_id"] for ln in flour_plan["lines"]}
    assert flour_ids
    assert flour_ids <= ALLOWED_GF
    assert flour_ids <= {"NB1025", "NB1026"}
    assert flour_ids.isdisjoint(FORBIDDEN_GLUTEN_ALL)
    for pid in flour_ids:
        prod = httpx.get(f"{base}/v1/products/{pid}", timeout=5).json()
        assert prod["allergens"]["gluten"] == "free"
        assert prod["allergen_source"]["gluten"].startswith("structured")
    for cand in flour_plan["candidates"]:
        cid = cand["product"]["id"]
        if cid in FORBIDDEN_GLUTEN_ALL:
            assert cand["tier"] == "rejected"
        if cand["tier"] != "rejected":
            prod = httpx.get(f"{base}/v1/products/{cid}", timeout=5).json()
            assert prod["allergens"]["gluten"] == "free"

    rye_resp = httpx.post(
        base + "/v1/plan",
        headers=headers,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "rye bread",
            "quantity": 10,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert rye_resp.status_code == 200, rye_resp.text
    rye_plan = rye_resp.json()
    assert "gluten" in rye_plan["need"]["excluded_allergens"]
    assert rye_plan["lines"] == []
    assert rye_plan["covered_amount"] == 0

    school_raw = httpx.post(
        base + "/v1/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "school-tampere",
            "product_query": "bread",
            "quantity": 10,
            "unit": "KG",
            "delivery_by": _delivery_date(),
        },
        timeout=10,
    )
    assert school_raw.status_code == 403
    assert school_raw.json()["error"] == "unknown_customer"


@pytest.fixture(scope="module")
def _toy_school_static(tmp_path_factory):
    import json as _json
    import shutil

    from aimo.backend import catalog_store
    from aimo.catalog.allergens import GS1_GROUPS
    from aimo.gateway.app import create_app
    from aimo.gateway.extras import install
    from toywholesaler.backend import CATALOG, StaticBackendClient, product_json

    data_dir = tmp_path_factory.mktemp("toy_school_static")
    pol_dir = data_dir / "policies"
    pol_dir.mkdir(parents=True, exist_ok=True)
    for yf in (ROOT / "toywholesaler" / "policies").glob("*.yaml"):
        shutil.copy2(yf, pol_dir / yf.name)
    shutil.copy2(ROOT / "aimo" / "trust" / "policies" / "school-tampere.yaml", pol_dir / "school-tampere.yaml")

    nut_item = {
        "id": "NB1031",
        "en": "Almond croissant 10 x 65 g frozen",
        "fi": "Mantelicroissant 10 x 65 g pakaste",
        "brand": "Kallio Leipomo",
        "category": "B150",
        "price": 6.40,
        "stock": 180,
        "contains": ["AW", "UW", "SA"],
        "claims": [],
        "ingredients": "wheat flour, roasted almonds, rapeseed oil, water, sugar, yeast, salt",
    }
    contains_set = set(nut_item["contains"])
    groups_contained = {g for g, (_, members) in GS1_GROUPS.items() if contains_set & set(members)}
    allergen_vals = [{"id": c, "unit": "CONTAINS"} for c in sorted(contains_set)]
    allergen_vals += [{"id": GS1_GROUPS[g][0][0], "unit": "FREE_FROM"} for g in GS1_GROUPS if g not in groups_contained]
    nut_raw = {
        "id": nut_item["id"],
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": nut_item["category"],
        "vendorName": CATALOG["vendor"],
        "allowedLotSize": 1,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"language": "en", "value": nut_item["en"]}, {"language": "fi", "value": nut_item["fi"]}],
            "brand": nut_item["brand"],
            "classifications": [{"name": "allergen", "values": allergen_vals}],
            "keyIngredients": [{"language": "en", "value": nut_item["ingredients"]}],
            "marketingTexts": [{"language": "en", "value": f"{nut_item['en']} from {CATALOG['vendor']}."}],
        },
    }

    pjson = data_dir / "valio_aimo_junction26_product_data.json"
    pjson.write_text(_json.dumps([*product_json(), nut_raw], ensure_ascii=False), encoding="utf-8")
    cat_db = data_dir / "catalog.sqlite"
    key_dir = data_dir / "keys"
    rel_csv = data_dir / "rel.csv"
    co2_csv = data_dir / "co2.csv"

    orig_pjson = catalog_store.PRODUCT_JSON
    orig_rel = catalog_store.RELIABILITY_CSV
    orig_co2 = catalog_store.CO2_CSV
    orig_cat = catalog_store.CATALOG_DB
    catalog_store.PRODUCT_JSON = pjson
    catalog_store.RELIABILITY_CSV = rel_csv
    catalog_store.CO2_CSV = co2_csv
    catalog_store.CATALOG_DB = cat_db
    try:
        catalog_store.load_catalog(cat_db, force=True)
    finally:
        catalog_store.PRODUCT_JSON = orig_pjson
        catalog_store.RELIABILITY_CSV = orig_rel
        catalog_store.CO2_CSV = orig_co2
        catalog_store.CATALOG_DB = orig_cat

    backend = StaticBackendClient()
    backend.products = {**backend.products, nut_item["id"]: nut_item}
    app = install(create_app(with_mcp=False))
    return data_dir, pol_dir, pjson, cat_db, key_dir, rel_csv, co2_csv, backend, app


@pytest.fixture
def toy_with_school(_toy_school_static, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aimo import config
    from aimo.backend import catalog_store, client as client_mod, orders as store
    from aimo.catalog import allergens as allergens_mod, textsearch
    from aimo.trust import policy as trust_policy

    data_dir, pol_dir, pjson, cat_db, key_dir, rel_csv, co2_csv, backend, app = _toy_school_static
    state_db = tmp_path / "state.sqlite"

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(config, "CO2_CSV", co2_csv)
    monkeypatch.setattr(config, "CATALOG_DB", cat_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(catalog_store, "CO2_CSV", co2_csv)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cat_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(trust_policy, "POLICY_DIR", pol_dir)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])

    prev_client = client_mod._client
    client_mod.set_client(backend)
    trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        client_mod.set_client(prev_client)
        trust_policy._load_policy_cached.cache_clear()
        allergens_mod._last_policy_dir = None
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


@pytest.mark.parametrize(
    "query,unit,expect_lines",
    [
        ("milk", "L", False),
        ("milk", "KG", False),
        ("almond croissant", "KG", False),
        ("oat", "KG", True),
    ],
)
def test_school_tampere_omitted_excluded_allergens_hard_block_on_toywholesaler(
    toy_with_school, query, unit, expect_lines
):
    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    resp = toy_with_school.post(
        "/v1/plan",
        headers=headers,
        json={
            "customer_id": "school-tampere",
            "product_query": query,
            "quantity": 10,
            "unit": unit,
            "delivery_by": _delivery_date(),
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert {"milk", "nuts"} <= set(plan["need"]["excluded_allergens"])

    if expect_lines:
        assert plan["lines"]
        line_ids = {ln["product_id"] for ln in plan["lines"]}
        assert line_ids <= {"NB1006", "NB1024"}
    else:
        assert plan["lines"] == []
        assert plan["covered_amount"] == 0

    for ln in plan["lines"]:
        prod = toy_with_school.get(f"/v1/products/{ln['product_id']}").json()
        assert prod["allergens"]["milk"] == "free"
        assert prod["allergens"]["nuts"] == "free"
        assert prod["allergen_source"]["milk"].startswith("structured")
        assert prod["allergen_source"]["nuts"].startswith("structured")

    for cand in plan["candidates"]:
        prod = toy_with_school.get(f"/v1/products/{cand['product']['id']}").json()
        if cand["tier"] != "rejected":
            assert prod["allergens"]["milk"] == "free"
            assert prod["allergens"]["nuts"] == "free"
        if prod["allergens"]["milk"] != "free" or prod["allergens"]["nuts"] != "free":
            assert cand["tier"] == "rejected"


@pytest.mark.parametrize(
    "unsafe_pid,expected_allergen",
    [
        ("NB1015", "milk"),
        ("NB1031", "nuts"),
    ],
)
def test_school_tampere_forced_milk_and_nut_orders_rejected_on_toywholesaler(
    toy_with_school, unsafe_pid, expected_allergen
):
    prod = toy_with_school.get(f"/v1/products/{unsafe_pid}").json()
    assert prod["allergens"][expected_allergen] == "contains"

    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    q_resp = toy_with_school.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": _delivery_date(),
            "lines": [{"product_id": unsafe_pid, "qty_sales_units": 2}],
        },
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()

    o_resp = toy_with_school.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "rejected"
    assert order["decision"]["outcome"] == "reject"
    assert order["decision"]["policy_id"] == "school-tampere"
    hits = [
        h for h in order["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]
    assert hits
    assert expected_allergen in hits[0]["message"].lower()
