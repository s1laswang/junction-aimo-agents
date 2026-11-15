from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import types
from typing import Any, Iterator

import httpx
import pytest

from aimo.backend import clock

ROOT = Path(__file__).resolve().parents[1]
REQUOTE_SCRIPT = ROOT / "docs" / "pitch" / "requote_beat.py"
RECORD_REPLAY_SCRIPT = ROOT / "scripts" / "record_replay.py"
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"
OPERATOR_PIN = "wave4b-operator-pin-7788"
HOTEL_AGENT = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
PRICE_SHOCK = "price_shock:464306:1.25"


def _synthetic_catalog_products() -> list[dict[str, Any]]:
    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
        marketing: str,
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
                    {
                        "name": "nutritionalClaim",
                        "type": "ENUM",
                        "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}],
                    },
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": marketing}],
            },
        }

    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    return [
        _item(
            "469363",
            "ST",
            "Arla lactose free milk 1 L UHT",
            "Arla",
            [("ST", 1)],
            base_1l,
            "Arla lactose free milk 1 L UHT.",
        ),
        _item(
            "463038",
            "KI",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            [("KI", 2), ("ST", 1)],
            [{"synkkaPackagingType": "CASE", "volume": {"unit": "LTR", "value": 20.0}}],
            "Valio Eila lactose free dispenser milk drink for hotels and restaurants.",
        ),
        _item(
            "464306",
            "ST",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            base_1l,
            "Valio Eila semi-skimmed milk drink is lactose free and suitable for cooking.",
        ),
        _item(
            "462775",
            "ST",
            "Valio Eila whole milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            base_1l,
            "Valio Eila lactose free milk drink 1 L.",
        ),
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gateway_env_without_demo_faults(srv_dir: Path) -> dict[str, str]:
    drop = {
        "AIMO_API_KEY",
        "VALIO_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_APPROVER_PINS",
        "AIMO_NOW",
        "AIMO_NOW_FROZEN",
        "AIMO_OFFLINE",
        "AIMO_ENV",
        "AIMO_REPLAY_CLOCK",
        "AIMO_AGENT_KEYS",
        "AIMO_DEMO_FAULTS",
        "AIMO_FAULTS",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    data_dir = srv_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("AIMO_CATALOG_FIXTURE", "").strip() == "0":
        (data_dir / "valio_aimo_junction26_product_data.json").write_text(
            json.dumps(_synthetic_catalog_products()),
            encoding="utf-8",
        )
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "AIMO_DATA_DIR": str(data_dir),
            "AIMO_STATE_DB": str(srv_dir / "state.sqlite"),
            "AIMO_KEY_DIR": str(srv_dir / "keys"),
            "AIMO_REPLAY": str(REPLAY_DIR),
            "AIMO_OFFLINE": "0",
            "AIMO_SEMANTIC": "0",
            "AIMO_RECIPE_CHECK": "0",
            "AIMO_APPROVER_PIN": OPERATOR_PIN,
            "AIMO_API_KEY": "",
            "VALIO_API_KEY": "",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
    )
    assert "AIMO_DEMO_FAULTS" not in env
    return env


@pytest.fixture(scope="module")
def gateway_without_demo_faults(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path, dict[str, str]]]:
    assert REPLAY_META.is_file()
    srv_dir = tmp_path_factory.mktemp("gw_no_demo_faults")
    (srv_dir / "keys").mkdir(parents=True, exist_ok=True)
    env = _gateway_env_without_demo_faults(srv_dir)
    port = _free_port()
    log_path = srv_dir / "gateway.log"
    log_fh = open(log_path, "w+", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aimo.gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log_fh,
        stderr=log_fh,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.15)
    else:
        log_fh.seek(0)
        tail = log_fh.read()[-2500:]
        proc.kill()
        log_fh.close()
        pytest.fail(f"gateway failed to start without AIMO_DEMO_FAULTS:\n{tail}")
    try:
        yield base, srv_dir, env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()


def test_requote_beat_and_override_guards_without_demo_faults(
    gateway_without_demo_faults: tuple[str, Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, _srv_dir, env = gateway_without_demo_faults
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    requote_mod = _load_module("requote_beat_guard_mod", REQUOTE_SCRIPT)
    rc = requote_mod.main(["--base", base])
    out = capsys.readouterr()
    assert rc == 0, out.out + "\n" + out.err
    assert "REQUOTE BEAT OK" in out.out
    assert OPERATOR_PIN not in out.out
    assert OPERATOR_PIN not in out.err

    with httpx.Client(base_url=base, headers=HOTEL_AGENT, timeout=30.0) as agent_client:
        health = agent_client.get("/healthz").json()
        now_dt = clock.parse((health.get("clock") or {}).get("now"))
        assert now_dt is not None
        delivery_date = clock.next_delivery_day(now_dt).isoformat()

        r_agent_fault = agent_client.post(
            "/v1/offers",
            headers={"X-Aimo-Faults": "stockout:464306"},
            json={"product_ids": ["464306"], "requested_date": delivery_date, "customer_id": "hotel-aurora"},
        )
        assert r_agent_fault.status_code == 200, r_agent_fault.text
        offer_agent = r_agent_fault.json()[0]
        assert offer_agent["available"] is True
        assert not any(str(i).startswith("injected:") for i in offer_agent.get("issues", []))

        far_future = (now_dt + timedelta(days=30)).isoformat()
        r_agent_now = agent_client.post(
            "/v1/offers",
            headers={"X-Aimo-Now": far_future},
            json={"product_ids": ["464306"], "requested_date": delivery_date, "customer_id": "hotel-aurora"},
        )
        assert r_agent_now.status_code == 200, r_agent_now.text
        assert r_agent_now.json()[0]["cutoff_passed"] is False

        q = agent_client.post(
            "/v1/quotes",
            json={
                "customer_id": "hotel-aurora",
                "delivery_date": delivery_date,
                "lines": [{"product_id": "464306", "qty_sales_units": 1000}],
            },
        ).json()
        o = agent_client.post(
            "/v1/orders",
            json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
        ).json()
        assert o["status"] == "pending_approval"

        created_dt = clock.parse(q["created_at"])
        assert created_dt is not None
        expired_iso = (created_dt + timedelta(minutes=20)).isoformat()

        r_unstripped_agent = agent_client.post(
            f"/v1/approvals/{o['order_id']}/decision",
            headers={
                "X-Approver-Pin": OPERATOR_PIN,
                "X-Aimo-Now": expired_iso,
                "X-Aimo-Faults": PRICE_SHOCK,
            },
            json={"decision": "approve", "approver": "anna@hotel-aurora.example", "quote_hash": q["quote_hash"]},
        )
        assert r_unstripped_agent.status_code == 403, r_unstripped_agent.text
        assert r_unstripped_agent.json()["error"] == "agent_cannot_approve"

        r_human_override = agent_client.post(
            f"/v1/approvals/{o['order_id']}/decision",
            headers={
                "X-Agent-Id": "",
                "X-Customer-Id": "",
                "X-Approver-Pin": OPERATOR_PIN,
                "X-Aimo-Now": expired_iso,
                "X-Aimo-Faults": PRICE_SHOCK,
            },
            json={"decision": "approve", "approver": "anna@hotel-aurora.example", "quote_hash": q["quote_hash"]},
        )
        assert r_human_override.status_code == 409, r_human_override.text
        assert r_human_override.json()["error"] == "requote_needed"
        assert r_human_override.json()["detail"]["reason"] == "quote_expired"

        q_partial = agent_client.post(
            "/v1/quotes",
            json={
                "customer_id": "hotel-aurora",
                "delivery_date": delivery_date,
                "lines": [{"product_id": "464306", "qty_sales_units": 1000}],
            },
        ).json()
        o_partial = agent_client.post(
            "/v1/orders",
            json={"quote_id": q_partial["quote_id"], "quote_hash": q_partial["quote_hash"]},
        ).json()
        assert o_partial["status"] == "pending_approval"

        r_partial = agent_client.post(
            f"/v1/approvals/{o_partial['order_id']}/decision",
            headers={
                "X-Agent-Id": "",
                "X-Customer-Id": "",
                "X-Approver-Pin": OPERATOR_PIN,
                "X-Aimo-Faults": "partial:464306:600",
            },
            json={
                "decision": "approve",
                "approver": "anna@hotel-aurora.example",
                "quote_hash": q_partial["quote_hash"],
            },
        )
        assert r_partial.status_code == 409, r_partial.text
        partial_body = r_partial.json()
        assert partial_body["error"] == "requote_needed"
        assert partial_body["detail"]["reason"] == "partial_stock"
        old_partial = agent_client.get(f"/v1/orders/{o_partial['order_id']}").json()
        assert old_partial["status"] == "expired"
        assert not old_partial.get("response_lines")


def test_conformance_suite_passes_44_of_44_without_demo_faults(
    gateway_without_demo_faults: tuple[str, Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conformance.__main__ as conf_main

    base, _srv_dir, env = gateway_without_demo_faults
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    out_json = tmp_path / "conformance-no-demo-faults.json"
    rc = conf_main.main(["--base", base, "--quiet", "--json", str(out_json)])
    assert rc == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    summary = report["summary"]
    failed_rows = [r for r in report["results"] if r["status"] != "PASS"]
    assert summary["fail"] == 0, failed_rows
    assert summary["skip"] == 0, failed_rows
    assert summary["pass"] == 44, summary
    assert summary["total"] == 44, summary
    assert len(report["mcp_tools"]) == 13


def test_record_replay_and_windtunnel_pass_approver_pin_on_agent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    record_mod = _load_module("record_replay_guard_mod", RECORD_REPLAY_SCRIPT)
    from aimo.models import Need
    from windtunnel import run as wt_run
    from windtunnel.schema import Expected, Persona, Scenario

    seen_record_posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    seen_record_gets: list[tuple[str, dict[str, str]]] = []

    def fake_post(url: str, *, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 0):
        seen_record_posts.append((url, dict(headers or {}), dict(json or {})))
        return httpx.Response(200, json={"run_id": "run-rec-1"})

    def fake_get(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 0):
        seen_record_gets.append((url, dict(headers or {})))
        return httpx.Response(200, json={"status": "finished"})

    monkeypatch.setenv("AIMO_APPROVER_PIN", "env-pin-9001")
    monkeypatch.setattr(record_mod, "AGENT_RUNS", [("hotel-aurora", "100 L milk", None)])
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    record_mod.agent_runs("http://127.0.0.1:19999", "fallback-pin")
    assert len(seen_record_posts) == 1
    rec_url, rec_headers, _rec_body = seen_record_posts[0]
    assert rec_url == "http://127.0.0.1:19999/v1/agent/runs"
    assert rec_headers["X-Customer-Id"] == "hotel-aurora"
    assert rec_headers["X-Approver-Pin"] == "env-pin-9001"
    assert len(seen_record_gets) == 1
    assert seen_record_gets[0][1]["X-Customer-Id"] == "hotel-aurora"
    assert seen_record_gets[0][1]["X-Approver-Pin"] == "env-pin-9001"

    seen_record_posts.clear()
    seen_record_gets.clear()
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    record_mod.agent_runs("http://127.0.0.1:19999", "fallback-pin-77")
    assert seen_record_posts[0][1]["X-Approver-Pin"] == "fallback-pin-77"
    assert seen_record_gets[0][1]["X-Approver-Pin"] == "fallback-pin-77"

    captured_wt_requests: list[httpx.Request] = []

    def wt_handler(request: httpx.Request) -> httpx.Response:
        captured_wt_requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/agent/runs":
            return httpx.Response(200, json={"run_id": "wt-run-1"})
        if request.method == "GET" and request.url.path == "/v1/agent/runs/wt-run-1/events":
            sse = 'data: {"kind": "final", "title": "done", "data": {"summary": "ok"}}\n\n'
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    real_client = httpx.Client

    def mock_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(wt_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", mock_client)
    monkeypatch.setenv("AIMO_APPROVER_PIN", "wt-pin-4455")

    sc = Scenario(
        scenario_id="WT-AUTH-1",
        seed=1,
        family="hotel_bulk",
        persona=Persona(
            persona_id="hotel-1",
            customer_id="hotel-aurora",
            org_name="Hotel Aurora",
            kitchen="hotel breakfast buffet",
            city="Helsinki",
            language="en",
            agent_id="buyer-1",
        ),
        request_text="Order 20 L lactose-free milk for Monday.",
        needs=[
            Need(
                customer_id="hotel-aurora",
                product_query="lactose-free milk",
                quantity=20.0,
                unit="L",
                delivery_by="2026-11-16",
            )
        ],
        faults="stockout:463038",
        expected=Expected(outcome="auto_approve", acceptable=["auto_approve"], must_escalate=False),
    )
    res = wt_run.run_agent_one(sc, "http://windtunnel.local")
    assert res.error is None
    run_posts = [r for r in captured_wt_requests if r.method == "POST" and r.url.path == "/v1/agent/runs"]
    assert len(run_posts) == 1
    post_hdrs = run_posts[0].headers
    assert post_hdrs.get("x-customer-id") == "hotel-aurora"
    assert post_hdrs.get("x-agent-id") == "buyer-1"
    assert post_hdrs.get("x-aimo-faults") == "stockout:463038"
    assert post_hdrs.get("x-approver-pin") == "wt-pin-4455"
