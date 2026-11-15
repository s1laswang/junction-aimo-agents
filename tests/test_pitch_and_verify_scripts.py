from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFY_QUOTE_SCRIPT = ROOT / "scripts" / "verify_quote.py"
LEDGER_STATS_SCRIPT = ROOT / "docs" / "pitch" / "ledger_stats.py"
QA_MILK_FLOW_SCRIPT = ROOT / "docs" / "pitch" / "qa_milk_flow.py"
PERMISSIONS_ATTACK_SCRIPT = ROOT / "docs" / "pitch" / "permissions_attack.py"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
APPROVER_PIN = "pitch-verify-pin-16chars"
AGENT_HEADERS = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _isolated_env(scratch_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    data_dir = scratch_dir / "data"
    key_dir = scratch_dir / "keys"
    home_dir = scratch_dir / "home"
    state_db = scratch_dir / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "HOME": str(home_dir),
        "AIMO_DATA_DIR": str(data_dir),
        "AIMO_STATE_DB": str(state_db),
        "AIMO_KEY_DIR": str(key_dir),
        "AIMO_SEMANTIC": "0",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "AIMO_API_KEY": "",
        "VALIO_API_KEY": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for var in ("AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_FAULTS", "AIMO_REPLAY_CLOCK", "AIMO_APPROVER_PINS"):
        env.pop(var, None)
    if extra:
        env.update(extra)
    return env


@contextlib.contextmanager
def _scratch(base_tmp: Path, extra: dict[str, str] | None = None) -> Iterator[tuple[Path, dict[str, str]]]:
    work = base_tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        yield work, _isolated_env(work, extra)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def local_replay_gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    srv_dir = tmp_path_factory.mktemp("pitch_replay_gateway")
    port = _free_port()
    log_path = srv_dir / "uvicorn.log"
    log_file = open(log_path, "w+", encoding="utf-8")
    env = _isolated_env(
        srv_dir,
        {
            "AIMO_REPLAY": "1",
            "AIMO_OFFLINE": "0",
            "AIMO_APPROVER_PIN": APPROVER_PIN,
        },
    )
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
        env=env,
        stdout=log_file,
        stderr=log_file,
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if proc.poll() is not None:
                break
            try:
                resp = httpx.get(f"{base}/healthz", timeout=2.0)
                if resp.status_code == 200 and resp.json().get("ok") is True:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            log_file.seek(0)
            tail = log_file.read()[-2500:]
            pytest.fail(f"ephemeral replay gateway did not start on {base}:\n{tail}")
        if proc.poll() is not None:
            log_file.seek(0)
            tail = log_file.read()[-2500:]
            pytest.fail(f"ephemeral replay gateway exited early on {base}:\n{tail}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_file.close()
        shutil.rmtree(srv_dir, ignore_errors=True)


def _generate_toywholesaler_bundle(scratch_dir: Path) -> dict:
    out_file = scratch_dir / "toy_bundle.json"
    code = (
        "import json, pathlib; "
        "from fastapi.testclient import TestClient; "
        "import toywholesaler.app as tw; "
        "c = TestClient(tw.app); "
        "h = {'X-Customer-Id': 'hotel-aurora', 'X-Agent-Id': 'buyer-1'}; "
        "qr = c.post('/v1/quotes', headers=h, json={'delivery_date': '2026-12-02', "
        "'lines': [{'product_id': 'NB1001', 'qty_sales_units': 2}]}); "
        "assert qr.status_code == 200, qr.text; "
        "jwks = c.get('/.well-known/jwks.json'); "
        "assert jwks.status_code == 200, jwks.text; "
        "ev = c.get('/v1/audit'); "
        "assert ev.status_code == 200, ev.text; "
        f"pathlib.Path({str(out_file)!r}).write_text("
        "json.dumps({'quote': qr.json(), 'jwks': jwks.json(), 'events': ev.json()}), encoding='utf-8')"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_isolated_env(scratch_dir, {"AIMO_APPROVER_PIN": APPROVER_PIN}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return json.loads(out_file.read_text(encoding="utf-8"))


def _run_verify_quote(doc_path: Path, jwks_arg: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(VERIFY_QUOTE_SCRIPT), str(doc_path), jwks_arg],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_file_has_zero_comments_and_zero_em_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_ledger_stats_script_on_sanitized_evidence(tmp_path: Path) -> None:
    assert LEDGER_STATS_SCRIPT.is_file()
    assert EVIDENCE_DIR.is_dir()
    with _scratch(tmp_path) as (_, env):
        md_res = subprocess.run(
            [sys.executable, str(LEDGER_STATS_SCRIPT), "--dir", str(EVIDENCE_DIR)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert md_res.returncode == 0, md_res.stdout + md_res.stderr
        assert "rows=105 unparseable=0" in md_res.stdout
        assert "105 calls (105 ok, 0 errors)" in md_res.stdout
        assert "gemini-3.8-flash" in md_res.stdout
        assert "gemini-4-hydrogen" in md_res.stdout
        for feature in ("buyer.agent_step", "buyer.parse_need", "buyer.explain", "buyer.summary"):
            assert feature in md_res.stdout

        json_res = subprocess.run(
            [sys.executable, str(LEDGER_STATS_SCRIPT), "--dir", str(EVIDENCE_DIR), "--json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert json_res.returncode == 0, json_res.stdout + json_res.stderr
        payload = json.loads(json_res.stdout)
        assert payload["rows"] == 105
        assert payload["unparseable"] == 0
        assert payload["total"]["calls"] == 105
        assert payload["total"]["ok"] == 105
        assert payload["total"]["errors"] == 0
        by_model = {entry["key"]: entry["calls"] for entry in payload["by_model"]}
        assert by_model == {"gemini-3.8-flash": 93, "gemini-4-hydrogen": 12}


def test_qa_milk_flow_script_against_replay_gateway(local_replay_gateway: str, tmp_path: Path) -> None:
    assert QA_MILK_FLOW_SCRIPT.is_file()
    with _scratch(
        tmp_path,
        {
            "AIMO_REPLAY": "1",
            "AIMO_OFFLINE": "0",
            "AIMO_APPROVER_PIN": APPROVER_PIN,
        },
    ) as (_, env):
        res = subprocess.run(
            [sys.executable, str(QA_MILK_FLOW_SCRIPT), "--base", local_replay_gateway],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "MILK FLOW OK" in res.stdout
        assert "OrderResponseCode AP" in res.stdout
        assert "audit verify ok=True" in res.stdout
        assert "served_from=replay" in res.stdout


def test_permissions_attack_script_against_replay_gateway(local_replay_gateway: str, tmp_path: Path) -> None:
    assert PERMISSIONS_ATTACK_SCRIPT.is_file()
    with httpx.Client(base_url=local_replay_gateway, headers=AGENT_HEADERS, timeout=20.0) as client:
        quote_resp = client.post(
            "/v1/quotes",
            json={
                "customer_id": "hotel-aurora",
                "agent_id": "buyer-1",
                "delivery_date": "2026-11-16",
                "lines": [{"product_id": "464306", "qty_sales_units": 600, "tier": "best"}],
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose-free milk",
                    "required_claims": ["FREE_FROM_LACTOSE"],
                    "quantity": 600,
                    "unit": "L",
                    "delivery_by": "2026-11-16",
                    "preferred_product_ids": ["463038"],
                },
            },
        )
        assert quote_resp.status_code == 200, quote_resp.text
        quote = quote_resp.json()
        assert float(quote["total_net"]) > 500.0

        order_resp = client.post(
            "/v1/orders",
            json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        )
        assert order_resp.status_code == 200, order_resp.text
        order = order_resp.json()
        assert order["status"] == "pending_approval"
        order_id = order["order_id"]

    with _scratch(tmp_path, {"AIMO_APPROVER_PIN": ""}) as (_, env):
        res = subprocess.run(
            [sys.executable, str(PERMISSIONS_ATTACK_SCRIPT), "--base", local_replay_gateway],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Agent's MCP tools (13):" in res.stdout
        assert "Approval tools on MCP: none" in res.stdout
        assert "403" in res.stdout
        assert "agent_cannot_approve" in res.stdout
        assert "approval.refused" in res.stdout
        assert "PERMISSIONS ATTACK REFUSED AND AUDITED" in res.stdout

    with httpx.Client(base_url=local_replay_gateway, timeout=10.0) as client:
        still_pending = client.get(f"/v1/orders/{order_id}")
        assert still_pending.status_code == 200
        assert still_pending.json()["status"] == "pending_approval"
        chain = client.get("/v1/audit/verify")
        assert chain.status_code == 200
        assert chain.json()["ok"] is True


def test_verify_quote_script_valio_vs_toywholesaler_tamper_and_audit(
    local_replay_gateway: str,
    tmp_path: Path,
) -> None:
    assert VERIFY_QUOTE_SCRIPT.is_file()
    with _scratch(tmp_path) as (work, env):
        with httpx.Client(base_url=local_replay_gateway, headers=AGENT_HEADERS, timeout=20.0) as client:
            vq_resp = client.post(
                "/v1/quotes",
                json={
                    "customer_id": "hotel-aurora",
                    "agent_id": "buyer-1",
                    "delivery_date": "2026-11-16",
                    "lines": [{"product_id": "464306", "qty_sales_units": 10, "tier": "best"}],
                },
            )
            assert vq_resp.status_code == 200, vq_resp.text
            valio_quote = vq_resp.json()

            vjwks_resp = client.get("/.well-known/jwks.json")
            assert vjwks_resp.status_code == 200
            valio_jwks = vjwks_resp.json()

            vevents_resp = client.get("/v1/audit")
            assert vevents_resp.status_code == 200
            valio_events = vevents_resp.json()
            assert isinstance(valio_events, list) and len(valio_events) >= 1

        toy_bundle = _generate_toywholesaler_bundle(work / "toy_scratch")
        toy_quote = toy_bundle["quote"]
        toy_jwks = toy_bundle["jwks"]
        toy_events = toy_bundle["events"]
        assert isinstance(toy_events, list) and len(toy_events) >= 1

        assert valio_jwks["keys"][0]["kid"] != toy_jwks["keys"][0]["kid"]

        valio_quote_path = work / "valio_quote.json"
        valio_jwks_path = work / "valio_jwks.json"
        valio_events_path = work / "valio_events.json"
        valio_pack_path = work / "valio_evidence_pack.json"
        valio_tampered_path = work / "valio_tampered.json"

        toy_quote_path = work / "toy_quote.json"
        toy_jwks_path = work / "toy_jwks.json"
        toy_events_path = work / "toy_events.json"
        toy_tampered_path = work / "toy_tampered.json"

        valio_quote_path.write_text(json.dumps(valio_quote), encoding="utf-8")
        valio_jwks_path.write_text(json.dumps(valio_jwks), encoding="utf-8")
        valio_events_path.write_text(json.dumps(valio_events), encoding="utf-8")
        valio_pack_path.write_text(
            json.dumps({"quote": valio_quote, "events": valio_events}),
            encoding="utf-8",
        )
        valio_tampered_path.write_text(
            json.dumps({**valio_quote, "total_net": round(float(valio_quote["total_net"]) + 7.5, 2)}),
            encoding="utf-8",
        )

        toy_quote_path.write_text(json.dumps(toy_quote), encoding="utf-8")
        toy_jwks_path.write_text(json.dumps(toy_jwks), encoding="utf-8")
        toy_events_path.write_text(json.dumps(toy_events), encoding="utf-8")
        toy_tampered_path.write_text(
            json.dumps({**toy_quote, "total_net": round(float(toy_quote["total_net"]) + 7.5, 2)}),
            encoding="utf-8",
        )

        v_ok = _run_verify_quote(valio_quote_path, str(valio_jwks_path), env)
        assert v_ok.returncode == 0, v_ok.stdout + v_ok.stderr
        assert "hash OK" in v_ok.stdout
        assert "signature OK" in v_ok.stdout
        assert "VERIFIED" in v_ok.stdout

        v_url_ok = _run_verify_quote(valio_quote_path, f"{local_replay_gateway}/.well-known/jwks.json", env)
        assert v_url_ok.returncode == 0, v_url_ok.stdout + v_url_ok.stderr
        assert "VERIFIED" in v_url_ok.stdout

        t_ok = _run_verify_quote(toy_quote_path, str(toy_jwks_path), env)
        assert t_ok.returncode == 0, t_ok.stdout + t_ok.stderr
        assert "hash OK" in t_ok.stdout
        assert "signature OK" in t_ok.stdout
        assert "VERIFIED" in t_ok.stdout

        v_against_toy = _run_verify_quote(valio_quote_path, str(toy_jwks_path), env)
        assert v_against_toy.returncode != 0
        assert "hash OK" in v_against_toy.stdout
        assert "signature BAD" in v_against_toy.stdout
        assert "FAILED" in v_against_toy.stdout

        t_against_valio = _run_verify_quote(toy_quote_path, str(valio_jwks_path), env)
        assert t_against_valio.returncode != 0
        assert "hash OK" in t_against_valio.stdout
        assert "signature BAD" in t_against_valio.stdout
        assert "FAILED" in t_against_valio.stdout

        v_tampered = _run_verify_quote(valio_tampered_path, str(valio_jwks_path), env)
        assert v_tampered.returncode != 0
        assert "hash MISMATCH" in v_tampered.stdout
        assert "FAILED" in v_tampered.stdout

        t_tampered = _run_verify_quote(toy_tampered_path, str(toy_jwks_path), env)
        assert t_tampered.returncode != 0
        assert "hash MISMATCH" in t_tampered.stdout
        assert "FAILED" in t_tampered.stdout

        v_events_ok = _run_verify_quote(valio_events_path, str(valio_jwks_path), env)
        assert v_events_ok.returncode == 0, v_events_ok.stdout + v_events_ok.stderr
        assert "event " in v_events_ok.stdout
        assert "BAD" not in v_events_ok.stdout
        assert "VERIFIED" in v_events_ok.stdout

        v_pack_ok = _run_verify_quote(valio_pack_path, str(valio_jwks_path), env)
        assert v_pack_ok.returncode == 0, v_pack_ok.stdout + v_pack_ok.stderr
        assert "hash OK" in v_pack_ok.stdout
        assert "signature OK" in v_pack_ok.stdout
        assert "VERIFIED" in v_pack_ok.stdout

        t_events_ok = _run_verify_quote(toy_events_path, str(toy_jwks_path), env)
        assert t_events_ok.returncode == 0, t_events_ok.stdout + t_events_ok.stderr
        assert "event " in t_events_ok.stdout
        assert "BAD" not in t_events_ok.stdout
        assert "VERIFIED" in t_events_ok.stdout

        v_events_wrong_jwks = _run_verify_quote(valio_events_path, str(toy_jwks_path), env)
        assert v_events_wrong_jwks.returncode != 0
        assert "FAILED" in v_events_wrong_jwks.stdout
