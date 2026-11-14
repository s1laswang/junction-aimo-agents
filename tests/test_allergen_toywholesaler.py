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
