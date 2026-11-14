import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx

from tests.fixtures.mini_catalog import start_mock_valio_server, write_mini_data

SCENARIOS = [
    "s0_guardrails",
    "s1_milk",
    "s2_need_recipe_order",
    "s3_event_substitution",
    "s4_stockout",
    "s5_allergen_safe",
    "s6_proactive_seller",
    "s7_drift",
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mini_catalog_files_and_mock_server(tmp_path):
    data_dir = tmp_path / "data"
    write_mini_data(data_dir)
    products = json.loads((data_dir / "valio_aimo_junction26_product_data.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in products}
    seed_ids = {"460396", "461578", "460573", "462775", "461765", "460200", "463982", "460468", "460884"}
    assert {"464073", "464201", "463335", "480001"} | seed_ids <= ids
    assert (data_dir / "valio_aimo_junction26_delivery_reliability.csv").exists()
    assert (data_dir / "valio_aimo_junction26_carbon_footprint.csv").exists()
    valio_base, server = start_mock_valio_server()
    try:
        pr = httpx.post(
            f"{valio_base}/price",
            json={"customer": "hotel-aurora", "requestedDate": "2026-11-16", "products": ["464073", "470002"]},
            timeout=10,
        )
        assert pr.status_code == 200
        pr_by_id = {x["productId"]: x for x in pr.json()["products"]}
        assert pr_by_id["464073"]["catalogPrice"]["value"] == 1.64
        assert pr_by_id["464073"]["priceWithTax"]["value"] > 1.64
        av = httpx.post(
            f"{valio_base}/availability",
            json={"customer": "hotel-aurora", "requestedDate": "2026-11-16", "products": ["470002", "470003"]},
            timeout=10,
        )
        assert av.status_code == 200
        av_by_id = {x["productId"]: x for x in av.json()["products"]}
        assert av_by_id["470002"]["closingTime"].endswith("T17:00:00+02:00")
        assert av_by_id["470002"]["scheduleLines"][0]["availableDate"] == "2026-11-16"
        assert av_by_id["470002"]["scheduleLines"][0]["availableAmount"] == 10.0
        assert av_by_id["470003"]["scheduleLines"][0]["availableAmount"] == 5000.0
    finally:
        server.shutdown()
        server.server_close()


def test_all_scenarios_e2e(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    write_mini_data(data_dir)
    valio_base, server = start_mock_valio_server()
    try:
        monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("AIMO_CACHE_DIR", str(cache_dir))
        monkeypatch.setenv("VALIO_API_BASE", valio_base)
        monkeypatch.setenv("VALIO_API_KEY", "mock-key")
        monkeypatch.setenv("LLM_BASE_URL", "")
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("LLM_MODEL", "")
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / "scenarios" / "run_all.py"), "--base", base, "--boot"],
            cwd=str(root),
            env={**os.environ},
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        for name in SCENARIOS:
            assert f"PASS  {name}" in proc.stdout, output
        assert "8/8 passed" in proc.stdout, output
        assert proc.returncode == 0, output
    finally:
        server.shutdown()
        server.server_close()
