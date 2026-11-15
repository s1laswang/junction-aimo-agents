from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
QA_MILK_FLOW_SCRIPT = ROOT / "docs" / "pitch" / "qa_milk_flow.py"
PERMISSIONS_ATTACK_SCRIPT = ROOT / "docs" / "pitch" / "permissions_attack.py"
REQUOTE_BEAT_SCRIPT = ROOT / "docs" / "pitch" / "requote_beat.py"
VERIFY_QUOTE_SCRIPT = ROOT / "scripts" / "verify_quote.py"
EXPORT_PEPPOL_SCRIPT = ROOT / "scripts" / "export_peppol_order_response.py"
ANNA = "anna@hotel-aurora.example"
MIKKO = "mikko@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
SARA = "sara@hospital-meilahti.example"
HOTEL_AGENT = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
SCHOOL_AGENT = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
SELLER_AGENT = {"X-Agent-Id": "valio-aimo/seller-1"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _isolated_env(
    scratch_dir: Path,
    *,
    approver_pin: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    data_dir = scratch_dir / "data"
    key_dir = scratch_dir / "keys"
    home_dir = scratch_dir / "home"
    state_db = scratch_dir / "state.sqlite"
    wt_dir = scratch_dir / "wt"
    for directory in (data_dir, key_dir, home_dir, wt_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "HOME": str(home_dir),
        "AIMO_DATA_DIR": str(data_dir),
        "AIMO_STATE_DB": str(state_db),
        "AIMO_KEY_DIR": str(key_dir),
        "WT_DIR": str(wt_dir),
        "AIMO_REPLAY": "1",
        "AIMO_OFFLINE": "0",
        "AIMO_CATALOG_FIXTURE": "1",
        "AIMO_SEMANTIC": "0",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "AIMO_API_KEY": "",
        "VALIO_API_KEY": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for var in (
        "AIMO_NOW",
        "AIMO_NOW_FROZEN",
        "AIMO_FAULTS",
        "AIMO_DEMO_FAULTS",
        "AIMO_REPLAY_CLOCK",
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_ENV",
    ):
        env.pop(var, None)
    if approver_pin is not None:
        env["AIMO_APPROVER_PIN"] = approver_pin
    if extra:
        env.update(extra)
    return env


@contextlib.contextmanager
def _running_gateway(
    scratch_dir: Path,
    *,
    approver_pin: str | None = None,
    extra: dict[str, str] | None = None,
) -> Iterator[tuple[str, dict[str, str], Path]]:
    port = _free_port()
    env = _isolated_env(scratch_dir, approver_pin=approver_pin, extra=extra)
    log_path = scratch_dir / "uvicorn.log"
    log_file = open(log_path, "w+", encoding="utf-8")
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
            pytest.fail(f"replay gateway did not start on {base}:\n{tail}")
        if proc.poll() is not None:
            log_file.seek(0)
            tail = log_file.read()[-2500:]
            pytest.fail(f"replay gateway exited early on {base}:\n{tail}")
        yield base, env, Path(env["AIMO_KEY_DIR"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_file.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def shared_gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, str], Path]]:
    srv_dir = tmp_path_factory.mktemp("pitch_shared_gw")
    with _running_gateway(srv_dir, approver_pin=None, extra={"AIMO_DEMO_FAULTS": "1"}) as bundle:
        base, _, key_dir = bundle
        httpx.get(f"{base}/v1/customers", timeout=10.0)
        assert (key_dir / "approver_pins.json").exists() or True
        yield bundle


def _pins_from_key_dir(base: str, key_dir: Path) -> dict[str, str]:
    pins_path = key_dir / "approver_pins.json"
    if not pins_path.is_file():
        httpx.post(
            f"{base}/v1/auth/approver-session",
            headers={"X-Approver-Pin": "probe-init-pins-0000"},
            json={"approver": ANNA},
            timeout=10.0,
        )
    assert pins_path.is_file()
    return json.loads(pins_path.read_text(encoding="utf-8"))


def _next_delivery_date(base: str) -> str:
    from aimo.backend import clock

    health = httpx.get(f"{base}/healthz", timeout=10.0).json()
    return clock.next_delivery_day(clock.parse(health["clock"]["now"])).isoformat()


def _create_pending_hotel_order(base: str, delivery_date: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    ddate = delivery_date or _next_delivery_date(base)
    need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 1000,
        "unit": "L",
        "delivery_by": ddate,
        "preferred_product_ids": ["463038"],
    }
    with httpx.Client(base_url=base, headers=HOTEL_AGENT, timeout=20.0) as client:
        plan_resp = client.post("/v1/plan", json=need)
        assert plan_resp.status_code == 200, plan_resp.text
        plan = plan_resp.json()
        lines = [
            {
                "product_id": ln["product_id"],
                "qty_sales_units": ln["qty_sales_units"],
                "tier": ln.get("tier"),
                "flags": ln.get("flags") or [],
            }
            for ln in plan["lines"]
        ]
        quote_resp = client.post(
            "/v1/quotes",
            json={
                "customer_id": "hotel-aurora",
                "agent_id": "buyer-1",
                "delivery_date": ddate,
                "lines": lines,
                "need": need,
            },
        )
        assert quote_resp.status_code == 200, quote_resp.text
        quote = quote_resp.json()
        order_resp = client.post(
            "/v1/orders",
            json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        )
        assert order_resp.status_code == 200, order_resp.text
        order = order_resp.json()
        assert order["status"] == "pending_approval"
        return quote, order


def _extract_order_id(stdout: str) -> str:
    match = re.search(r"\bORDER (ORD-[A-Za-z0-9_-]+) placed\b", stdout)
    assert match is not None, stdout
    return match.group(1)


def _assert_milk_flow_output_and_gateway_state(stdout: str, base: str) -> str:
    assert "status 403 agent_cannot_approve" in stdout
    assert "status 409 quote_hash_mismatch" in stdout
    assert "OK   status 200" in stdout
    assert f"status=placed response_code=AP approved_by={ANNA}" in stdout
    assert "OrderResponseCode=AP" in stdout
    assert '"ok": true' in stdout
    assert "audit verify ok=True" in stdout
    assert "MILK FLOW OK" in stdout

    order_id = _extract_order_id(stdout)
    with httpx.Client(base_url=base, timeout=15.0) as client:
        order_resp = client.get(f"/v1/orders/{order_id}")
        assert order_resp.status_code == 200
        order_data = order_resp.json()
        assert order_data["status"] == "placed"
        assert order_data["approved_by"] == ANNA
        assert order_data["response_code"] == "AP"

        xml_resp = client.get(f"/v1/orders/{order_id}/peppol-order-response.xml")
        assert xml_resp.status_code == 200
        xml_match = re.search(r"<cbc:OrderResponseCode[^>]*>([^<]+)<", xml_resp.text)
        assert xml_match is not None and xml_match.group(1).strip() == "AP"

        report_resp = client.get(f"/v1/audit/report/{order_id}")
        assert report_resp.status_code == 200
        assert "approval.refused" in report_resp.text
        assert ANNA in report_resp.text

        verify_resp = client.get("/v1/audit/verify")
        assert verify_resp.status_code == 200
        assert verify_resp.json()["ok"] is True

    return order_id


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


def test_qa_milk_flow_approver_pin_resolution_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("qa_milk_flow_mod", QA_MILK_FLOW_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from aimo.gateway import human_auth

    def _boom(_approver_id: str | None = None) -> str:
        raise RuntimeError("force file fallback")

    key_dir = tmp_path / "fallback_keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(human_auth, "local_pin", _boom)
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)

    (key_dir / "approver_pin").write_text("legacy-file-pin-1234\n", encoding="utf-8")
    assert mod._approver_pin() == "legacy-file-pin-1234"

    (key_dir / "approver_pins.json").write_text(
        json.dumps({MIKKO: "mikko-json-pin-5678", ANNA: "anna-json-pin-9012"}),
        encoding="utf-8",
    )
    assert mod._approver_pin() == "anna-json-pin-9012"

    monkeypatch.setenv("AIMO_APPROVER_PIN", "env-override-pin-3456")
    assert mod._approver_pin() == "env-override-pin-3456"


def test_qa_milk_flow_without_approver_pin_env_uses_per_approver_files(tmp_path: Path) -> None:
    assert QA_MILK_FLOW_SCRIPT.is_file()
    with _running_gateway(tmp_path / "gw_no_env_pin", approver_pin=None) as (base, env, key_dir):
        assert "AIMO_APPROVER_PIN" not in env
        assert "AIMO_APPROVER_PINS" not in env

        first = subprocess.run(
            [sys.executable, str(QA_MILK_FLOW_SCRIPT), "--base", base],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert first.returncode == 0, first.stdout + first.stderr
        first_oid = _assert_milk_flow_output_and_gateway_state(first.stdout, base)

        pins_file = key_dir / "approver_pins.json"
        pin_file = key_dir / "approver_pin"
        assert pins_file.is_file()
        assert pin_file.is_file()
        assert (pins_file.stat().st_mode & 0o777) == 0o600
        assert (pin_file.stat().st_mode & 0o777) == 0o600

        pins = json.loads(pins_file.read_text(encoding="utf-8"))
        for approver_id in (ANNA, MIKKO, LIISA, SARA):
            assert approver_id in pins
            assert len(pins[approver_id]) >= 12
        assert len(set(pins.values())) == len(pins)
        anna_pin = pin_file.read_text(encoding="utf-8").strip()
        assert anna_pin == pins[ANNA]

        with httpx.Client(base_url=base, timeout=15.0) as client:
            wrong_person = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": pins[MIKKO]},
                json={"approver": ANNA},
            )
            assert wrong_person.status_code == 401
            assert wrong_person.json()["error"] == "approver_auth_required"

            anna_ok = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": anna_pin},
                json={"approver": ANNA},
            )
            assert anna_ok.status_code == 200
            assert anna_ok.json()["approver"] == ANNA

        second = subprocess.run(
            [sys.executable, str(QA_MILK_FLOW_SCRIPT), "--base", base],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        second_oid = _assert_milk_flow_output_and_gateway_state(second.stdout, base)
        assert second_oid != first_oid


def test_qa_milk_flow_with_approver_pin_env_override(tmp_path: Path) -> None:
    assert QA_MILK_FLOW_SCRIPT.is_file()
    override_pin = "env-override-approver-pin-2026"
    gw_dir = tmp_path / "gw_with_env_pin"
    stale_keys = gw_dir / "keys"
    stale_keys.mkdir(parents=True, exist_ok=True)
    stale_pin = "stale-disk-approver-pin-0000"
    (stale_keys / "approver_pin").write_text(stale_pin + "\n", encoding="utf-8")
    (stale_keys / "approver_pins.json").write_text(
        json.dumps({ANNA: stale_pin, MIKKO: "mikko-disk-approver-pin-1111"}, indent=2) + "\n",
        encoding="utf-8",
    )

    with _running_gateway(gw_dir, approver_pin=override_pin) as (base, env, _):
        assert env.get("AIMO_APPROVER_PIN") == override_pin
        res = subprocess.run(
            [sys.executable, str(QA_MILK_FLOW_SCRIPT), "--base", base],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        _assert_milk_flow_output_and_gateway_state(res.stdout, base)

        with httpx.Client(base_url=base, timeout=15.0) as client:
            stale_try = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": stale_pin},
                json={"approver": ANNA},
            )
            assert stale_try.status_code == 401

            override_try = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": override_pin},
                json={"approver": ANNA},
            )
            assert override_try.status_code == 200
            assert override_try.json()["approver"] == ANNA


def test_permissions_attack_script_e2e_refused_and_audited(
    shared_gateway: tuple[str, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    assert PERMISSIONS_ATTACK_SCRIPT.is_file()
    base, _, _ = shared_gateway
    attacker_env = _isolated_env(tmp_path / "attacker_client", approver_pin=None)
    try:
        no_pending = subprocess.run(
            [
                sys.executable,
                str(PERMISSIONS_ATTACK_SCRIPT),
                "--base",
                base,
                "--customer",
                "hospital-meilahti",
            ],
            cwd=ROOT,
            env=attacker_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert no_pending.returncode == 2
        assert "Agent's MCP tools (13):" in no_pending.stdout
        assert "Approval tools on MCP: none" in no_pending.stdout
        assert "No pending order for hospital-meilahti." in no_pending.stdout

        delivery_date = _next_delivery_date(base)
        _, order = _create_pending_hotel_order(base, delivery_date)
        oid = order["order_id"]

        res = subprocess.run(
            [
                sys.executable,
                str(PERMISSIONS_ATTACK_SCRIPT),
                "--base",
                base,
                "--customer",
                "hotel-aurora",
                "--agent",
                "buyer-1",
            ],
            cwd=ROOT,
            env=attacker_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Agent's MCP tools (13):" in res.stdout
        assert "Approval tools on MCP: none" in res.stdout
        assert f"Pending order {oid}" in res.stdout
        assert "HTTP 403" in res.stdout
        assert "agent_cannot_approve" in res.stdout
        assert "approval.refused" in res.stdout
        assert "PERMISSIONS ATTACK REFUSED AND AUDITED" in res.stdout

        with httpx.Client(base_url=base, timeout=15.0) as client:
            still_pending = client.get(f"/v1/orders/{oid}")
            assert still_pending.status_code == 200
            assert still_pending.json()["status"] == "pending_approval"

            report = client.get(f"/v1/audit/report/{oid}")
            assert report.status_code == 200
            assert "approval.refused" in report.text
            assert "agent:buyer-1" in report.text

            verify = client.get("/v1/audit/verify")
            assert verify.status_code == 200
            assert verify.json()["ok"] is True

        with httpx.Client(base_url=base, headers=SCHOOL_AGENT, timeout=20.0) as school_client:
            school_need = {
                "customer_id": "school-tampere",
                "product_query": "oat drink",
                "quantity": 400,
                "unit": "L",
                "delivery_by": delivery_date,
            }
            sp_resp = school_client.post("/v1/plan", json=school_need)
            assert sp_resp.status_code == 200, sp_resp.text
            school_plan = sp_resp.json()
            school_lines = [
                {
                    "product_id": ln["product_id"],
                    "qty_sales_units": ln["qty_sales_units"],
                    "tier": ln.get("tier"),
                    "flags": ln.get("flags") or [],
                }
                for ln in school_plan["lines"]
            ]
            sq_resp = school_client.post(
                "/v1/quotes",
                json={
                    "customer_id": "school-tampere",
                    "agent_id": "buyer-1",
                    "delivery_date": delivery_date,
                    "lines": school_lines,
                    "need": school_need,
                },
            )
            assert sq_resp.status_code == 200, sq_resp.text
            sq = sq_resp.json()
            so_resp = school_client.post(
                "/v1/orders",
                json={"quote_id": sq["quote_id"], "quote_hash": sq["quote_hash"]},
            )
            assert so_resp.status_code == 200, so_resp.text
            soid = so_resp.json()["order_id"]
            assert so_resp.json()["status"] == "pending_approval"

        school_attack = subprocess.run(
            [
                sys.executable,
                str(PERMISSIONS_ATTACK_SCRIPT),
                "--base",
                base,
                "--customer",
                "school-tampere",
                "--agent",
                "buyer-1",
            ],
            cwd=ROOT,
            env=attacker_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert school_attack.returncode == 0, school_attack.stdout + school_attack.stderr
        assert f"AS {LIISA}" in school_attack.stdout
        assert "HTTP 403" in school_attack.stdout
        assert "agent_cannot_approve" in school_attack.stdout
        assert "PERMISSIONS ATTACK REFUSED AND AUDITED" in school_attack.stdout

        with httpx.Client(base_url=base, timeout=15.0) as client:
            school_report = client.get(f"/v1/audit/report/{soid}")
            assert school_report.status_code == 200
            assert "approval.refused" in school_report.text
            verify2 = client.get("/v1/audit/verify")
            assert verify2.status_code == 200
            assert verify2.json()["ok"] is True
    finally:
        shutil.rmtree(tmp_path / "attacker_client", ignore_errors=True)


def test_requote_beat_script_and_offline_quote_audit_peppol_verifiers(
    shared_gateway: tuple[str, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    assert REQUOTE_BEAT_SCRIPT.is_file()
    assert VERIFY_QUOTE_SCRIPT.is_file()
    assert EXPORT_PEPPOL_SCRIPT.is_file()
    base, gw_env, key_dir = shared_gateway
    pins = _pins_from_key_dir(base, key_dir)
    anna_pin = pins[ANNA]

    rq_res = subprocess.run(
        [sys.executable, str(REQUOTE_BEAT_SCRIPT), "--base", base],
        cwd=ROOT,
        env=gw_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rq_res.returncode == 0, rq_res.stdout + rq_res.stderr
    assert "REQUOTE BEAT OK" in rq_res.stdout
    assert "409 requote_needed" in rq_res.stdout
    assert "Peppol OrderResponseCode AP" in rq_res.stdout
    assert "audit verify ok=True" in rq_res.stdout

    m_rq = re.search(r"REQUOTE (ORD-[A-Za-z0-9_-]+) -> (ORD-[A-Za-z0-9_-]+);", rq_res.stdout)
    assert m_rq is not None, rq_res.stdout
    old_oid, new_oid = m_rq.group(1), m_rq.group(2)
    assert old_oid != new_oid

    work = tmp_path / "offline_verify"
    work.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(base_url=base, timeout=15.0) as client:
            old_order = client.get(f"/v1/orders/{old_oid}").json()
            assert old_order["status"] == "expired"
            new_order = client.get(f"/v1/orders/{new_oid}").json()
            assert new_order["status"] == "placed"
            assert new_order["approved_by"] == ANNA
            assert new_order["response_code"] == "AP"

            new_quote = client.get(f"/v1/quotes/{new_order['quote_id']}").json()
            jwks = client.get("/.well-known/jwks.json").json()
            events = client.get("/v1/audit").json()
            assert isinstance(events, list) and len(events) >= 3
            live_xml = client.get(f"/v1/orders/{new_oid}/peppol-order-response.xml").text

            sess_resp = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": anna_pin},
                json={"approver": ANNA},
            )
            assert sess_resp.status_code == 200, sess_resp.text
            anna_session = sess_resp.json()["session"]

        quote_file = work / "quote.json"
        tampered_quote_file = work / "quote_tampered.json"
        events_file = work / "events.json"
        pack_file = work / "evidence_pack.json"
        jwks_file = work / "jwks.json"

        quote_file.write_text(json.dumps(new_quote), encoding="utf-8")
        tampered_quote_file.write_text(
            json.dumps({**new_quote, "total_net": round(float(new_quote["total_net"]) + 12.34, 2)}),
            encoding="utf-8",
        )
        events_file.write_text(json.dumps(events), encoding="utf-8")
        pack_file.write_text(json.dumps({"quote": new_quote, "events": events}), encoding="utf-8")
        jwks_file.write_text(json.dumps(jwks), encoding="utf-8")

        vq_url = _run_verify_quote(quote_file, f"{base}/.well-known/jwks.json", gw_env)
        assert vq_url.returncode == 0, vq_url.stdout + vq_url.stderr
        assert "hash OK, signature OK" in vq_url.stdout
        assert "VERIFIED" in vq_url.stdout

        vq_file = _run_verify_quote(quote_file, str(jwks_file), gw_env)
        assert vq_file.returncode == 0, vq_file.stdout + vq_file.stderr
        assert "VERIFIED" in vq_file.stdout

        ve_ok = _run_verify_quote(events_file, str(jwks_file), gw_env)
        assert ve_ok.returncode == 0, ve_ok.stdout + ve_ok.stderr
        assert "BAD" not in ve_ok.stdout
        assert "VERIFIED" in ve_ok.stdout

        vp_ok = _run_verify_quote(pack_file, str(jwks_file), gw_env)
        assert vp_ok.returncode == 0, vp_ok.stdout + vp_ok.stderr
        assert "hash OK, signature OK" in vp_ok.stdout
        assert "VERIFIED" in vp_ok.stdout

        vq_bad = _run_verify_quote(tampered_quote_file, str(jwks_file), gw_env)
        assert vq_bad.returncode != 0
        assert "hash MISMATCH" in vq_bad.stdout
        assert "FAILED" in vq_bad.stdout

        target_seq = int(events[-1]["seq"])
        with httpx.Client(base_url=base, timeout=15.0) as client:
            agent_tamper = client.post(f"/v1/demo/tamper/{target_seq}", headers=HOTEL_AGENT)
            assert agent_tamper.status_code == 403
            assert agent_tamper.json()["error"] == "agent_cannot_approve"

            anon_tamper = client.post(f"/v1/demo/tamper/{target_seq}")
            assert anon_tamper.status_code == 401
            assert anon_tamper.json()["error"] == "approver_auth_required"

            tamper_ok = client.post(
                f"/v1/demo/tamper/{target_seq}",
                headers={"X-Approver-Session": anna_session},
            )
            assert tamper_ok.status_code == 200, tamper_ok.text
            try:
                broken_verify = client.get("/v1/audit/verify")
                assert broken_verify.status_code == 200
                assert broken_verify.json()["ok"] is False

                tampered_events_path = work / "events_tampered.json"
                tampered_events_path.write_text(json.dumps(client.get("/v1/audit").json()), encoding="utf-8")
                ve_broken = _run_verify_quote(tampered_events_path, str(jwks_file), gw_env)
                assert ve_broken.returncode != 0
                assert "BAD" in ve_broken.stdout
                assert "FAILED" in ve_broken.stdout
            finally:
                untamper_ok = client.post(
                    f"/v1/demo/untamper/{target_seq}",
                    headers={"X-Approver-Session": anna_session},
                )
                assert untamper_ok.status_code == 200, untamper_ok.text

            restored_verify = client.get("/v1/audit/verify")
            assert restored_verify.status_code == 200
            assert restored_verify.json()["ok"] is True

        peppol_out = work / "peppol_out"
        sample_exp = subprocess.run(
            [sys.executable, str(EXPORT_PEPPOL_SCRIPT), "--sample", "--out", str(peppol_out)],
            cwd=ROOT,
            env=gw_env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert sample_exp.returncode == 0, sample_exp.stdout + sample_exp.stderr
        for code in ("AP", "CA", "RE"):
            xml_path = peppol_out / f"sample-order-response-advanced-{code}.xml"
            assert xml_path.is_file()
            assert f">{code}<" in xml_path.read_text(encoding="utf-8")
        assert sample_exp.stdout.count("T116 rules OK") == 3

        order_exp = subprocess.run(
            [sys.executable, str(EXPORT_PEPPOL_SCRIPT), "--order", new_oid, "--out", str(peppol_out)],
            cwd=ROOT,
            env=gw_env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert order_exp.returncode == 0, order_exp.stdout + order_exp.stderr
        assert "T116 rules OK" in order_exp.stdout
        exported_xml = (peppol_out / f"{new_oid}-order-response-advanced.xml").read_text(encoding="utf-8")
        assert exported_xml == live_xml

        missing_exp = subprocess.run(
            [sys.executable, str(EXPORT_PEPPOL_SCRIPT), "--order", "ORD-missing-9999", "--out", str(peppol_out)],
            cwd=ROOT,
            env=gw_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert missing_exp.returncode == 2
        assert "not found" in missing_exp.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_known_customer_enforcement_across_v1_and_agent_runs(
    shared_gateway: tuple[str, dict[str, str], Path],
) -> None:
    base, _, key_dir = shared_gateway
    anna_pin = _pins_from_key_dir(base, key_dir)[ANNA]
    ddate = _next_delivery_date(base)

    with httpx.Client(base_url=base, timeout=20.0) as client:
        assert client.get("/v1/products/search", params={"q": "milk"}).status_code == 401
        assert client.get("/v1/products/search", params={"q": "milk"}, headers={"X-Customer-Id": "ghost-corp"}).status_code == 403
        assert client.get("/v1/products/search", params={"q": "milk"}, headers={"X-Customer-Id": "../etc/passwd"}).status_code == 403
        assert client.get("/v1/products/464306", headers={"X-Customer-Id": "ghost-corp"}).status_code == 403
        assert client.get("/v1/products/464306", headers={"X-Customer-Id": "hotel-aurora"}).status_code == 200

        offers_body = {"product_ids": ["464306"], "requested_date": ddate}
        r = client.post("/v1/offers", json=offers_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post("/v1/offers", headers={"X-Customer-Id": "ghost-corp"}, json=offers_body)
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/offers",
            headers={"X-Customer-Id": "hotel-aurora"},
            json={**offers_body, "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        plan_body = {"customer_id": "", "product_query": "lactose-free milk", "quantity": 10, "unit": "L", "delivery_by": ddate}
        r = client.post("/v1/plan", json=plan_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post("/v1/plan", headers={"X-Customer-Id": "ghost-corp"}, json={**plan_body, "customer_id": "ghost-corp"})
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/plan",
            headers={"X-Customer-Id": "hotel-aurora"},
            json={**plan_body, "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        quote_body = {"delivery_date": ddate, "lines": [{"product_id": "464306", "qty_sales_units": 5, "tier": "best"}]}
        r = client.post("/v1/quotes", json=quote_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post("/v1/quotes", headers={"X-Customer-Id": "hotel-aurora"}, json=quote_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "ghost-corp", "X-Agent-Id": "buyer-1"},
            json=quote_body,
        )
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/quotes",
            headers=HOTEL_AGENT,
            json={**quote_body, "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        valid_q = client.post("/v1/quotes", headers=HOTEL_AGENT, json=quote_body).json()
        order_body = {"quote_id": valid_q["quote_id"], "quote_hash": valid_q["quote_hash"]}
        r = client.post("/v1/orders", json=order_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post(
            "/v1/orders",
            headers={"X-Customer-Id": "ghost-corp", "X-Agent-Id": "buyer-1"},
            json=order_body,
        )
        assert r.status_code == 403 and r.json()["error"] in ("unknown_customer", "customer_mismatch")
        r = client.post("/v1/orders", headers=SCHOOL_AGENT, json=order_body)
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        recipe_body = {"recipe_id": "pancakes", "portions": 20, "delivery_by": ddate}
        r = client.post("/v1/recipes/plan", json=recipe_body)
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post("/v1/recipes/plan", headers={"X-Customer-Id": "ghost-corp"}, json=recipe_body)
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/recipes/plan",
            headers=HOTEL_AGENT,
            json={**recipe_body, "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        r = client.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"})
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.get(
            "/v1/drift/ghost-corp",
            params={"rebaseline": "true"},
            headers={"X-Customer-Id": "ghost-corp"},
        )
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.get(
            "/v1/drift/hotel-aurora",
            params={"rebaseline": "true"},
            headers={"X-Customer-Id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        prop_req = {
            "customer_id": "hotel-aurora",
            "product_id": "464306",
            "qty_sales_units": 5,
            "replaces_product_id": "463038",
            "delivery_date": ddate,
        }
        assert client.post("/v1/proposals", json=prop_req).status_code == 401
        r = client.post("/v1/proposals", headers=HOTEL_AGENT, json=prop_req)
        assert r.status_code == 403 and r.json()["error"] == "seller_required"
        r = client.post("/v1/proposals", headers=SELLER_AGENT, json={**prop_req, "customer_id": "ghost-corp"})
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"

        prop_ok = client.post("/v1/proposals", headers=SELLER_AGENT, json=prop_req)
        assert prop_ok.status_code == 200, prop_ok.text
        prop_id = prop_ok.json()["proposal_id"]

        for sub in ("accept", "decline"):
            assert client.post(f"/v1/proposals/{prop_id}/{sub}").status_code == 401
            r_sel = client.post(
                f"/v1/proposals/{prop_id}/{sub}",
                headers={"X-Agent-Id": "valio-aimo/seller-1", "X-Customer-Id": "hotel-aurora"},
            )
            assert r_sel.status_code == 403 and r_sel.json()["error"] == "seller_cannot_order"
            r_unk = client.post(
                f"/v1/proposals/{prop_id}/{sub}",
                headers={"X-Agent-Id": "buyer-1", "X-Customer-Id": "ghost-corp"},
            )
            assert r_unk.status_code == 403 and r_unk.json()["error"] == "unknown_customer"

        assert client.get("/v1/approvals").status_code == 401
        assert client.get("/v1/approvals", headers={"X-Customer-Id": "ghost-corp"}).status_code == 403
        assert client.get(
            "/v1/approvals",
            params={"customer_id": "school-tampere"},
            headers={"X-Customer-Id": "hotel-aurora"},
        ).status_code == 403

        assert client.get("/v1/customers/ghost-corp/learned").status_code == 403
        assert client.get(
            "/v1/customers/hotel-aurora/learned",
            headers={"X-Customer-Id": "school-tampere"},
        ).status_code == 403
        assert client.get(
            "/v1/customers/hotel-aurora/learned",
            headers={"X-Customer-Id": "hotel-aurora"},
        ).status_code == 200

        pin_only = {"X-Approver-Pin": anna_pin}
        r = client.post("/v1/agent/runs", headers=pin_only, json={"text": "20 L milk by Monday"})
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post(
            "/v1/agent/runs",
            headers={**pin_only, "X-Customer-Id": "ghost-corp"},
            json={"text": "20 L milk by Monday"},
        )
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/agent/runs",
            headers={**pin_only, "X-Customer-Id": "hotel-aurora"},
            json={"text": "20 L milk by Monday", "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"

        r = client.post("/v1/agent/runs/run-missing/answer", headers=pin_only, json={"text": "Monday"})
        assert r.status_code == 401 and r.json()["error"] == "identity_required"
        r = client.post(
            "/v1/agent/runs/run-missing/answer",
            headers={**pin_only, "X-Customer-Id": "ghost-corp"},
            json={"text": "Monday"},
        )
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
        r = client.post(
            "/v1/agent/runs/run-missing/answer",
            headers={**pin_only, "X-Customer-Id": "hotel-aurora"},
            json={"text": "Monday", "customer_id": "school-tampere"},
        )
        assert r.status_code == 403 and r.json()["error"] == "customer_mismatch"


def test_agent_key_enforcement_across_v1_and_agent_runs(tmp_path: Path) -> None:
    secret_key = "hotel-agent-secret-2026"
    with _running_gateway(
        tmp_path / "gw_agent_keys",
        approver_pin=None,
        extra={"AIMO_AGENT_KEYS": f"hotel-aurora={secret_key}"},
    ) as (base, _, key_dir):
        anna_pin = _pins_from_key_dir(base, key_dir)[ANNA]
        ddate = _next_delivery_date(base)
        with httpx.Client(base_url=base, timeout=20.0) as client:
            no_key = client.get(
                "/v1/products/search",
                params={"q": "milk"},
                headers={"X-Customer-Id": "hotel-aurora"},
            )
            assert no_key.status_code == 401 and no_key.json()["error"] == "agent_key_required"

            bad_key = client.get(
                "/v1/products/search",
                params={"q": "milk"},
                headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong-key"},
            )
            assert bad_key.status_code == 403 and bad_key.json()["error"] == "invalid_agent_key"

            good_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1", "X-Agent-Key": secret_key}
            ok_search = client.get("/v1/products/search", params={"q": "milk"}, headers=good_headers)
            assert ok_search.status_code == 200

            ok_plan = client.post(
                "/v1/plan",
                headers=good_headers,
                json={"customer_id": "hotel-aurora", "product_query": "lactose-free milk", "quantity": 10, "unit": "L", "delivery_by": ddate},
            )
            assert ok_plan.status_code == 200

            run_no_key = client.post(
                "/v1/agent/runs",
                headers={"X-Customer-Id": "hotel-aurora", "X-Approver-Pin": anna_pin},
                json={"text": "10 L lactose-free milk by Monday", "customer_id": "hotel-aurora"},
            )
            assert run_no_key.status_code == 401 and run_no_key.json()["error"] == "agent_key_required"

            run_bad_key = client.post(
                "/v1/agent/runs",
                headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong", "X-Approver-Pin": anna_pin},
                json={"text": "10 L lactose-free milk by Monday", "customer_id": "hotel-aurora"},
            )
            assert run_bad_key.status_code == 403 and run_bad_key.json()["error"] == "invalid_agent_key"

            school_no_key_needed = client.get(
                "/v1/products/search",
                params={"q": "oat"},
                headers={"X-Customer-Id": "school-tampere"},
            )
            assert school_no_key_needed.status_code == 200


def test_human_approver_session_binding_lockout_isolation_and_voice_auth(
    shared_gateway: tuple[str, dict[str, str], Path],
) -> None:
    base, _, key_dir = shared_gateway
    pins = _pins_from_key_dir(base, key_dir)
    anna_pin = pins[ANNA]
    liisa_pin = pins[LIISA]

    quote, order = _create_pending_hotel_order(base)
    oid = order["order_id"]
    qhash = quote["quote_hash"]

    with httpx.Client(base_url=base, timeout=20.0) as client:
        masked = client.get("/v1/customers", headers=HOTEL_AGENT).json()
        for cust in masked:
            for appr in cust.get("approvers") or []:
                assert "id" not in appr
                assert "name" in appr and "role" in appr

        unmasked_pin = client.get("/v1/customers", headers={"X-Approver-Pin": anna_pin}).json()
        assert any(
            appr.get("id") == ANNA
            for cust in unmasked_pin
            for appr in (cust.get("approvers") or [])
        )

        agent_signin = client.post(
            "/v1/auth/approver-session",
            headers={**HOTEL_AGENT, "X-Approver-Pin": anna_pin},
            json={"approver": ANNA},
        )
        assert agent_signin.status_code == 403 and agent_signin.json()["error"] == "agent_cannot_approve"

        unknown_signin = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": anna_pin},
            json={"approver": "nobody@hotel-aurora.example"},
        )
        assert unknown_signin.status_code == 404 and unknown_signin.json()["error"] == "unknown_approver"

        anna_sess_resp = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": anna_pin},
            json={"approver": ANNA},
        )
        assert anna_sess_resp.status_code == 200
        anna_session = anna_sess_resp.json()["session"]

        liisa_sess_resp = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": liisa_pin},
            json={"approver": LIISA},
        )
        assert liisa_sess_resp.status_code == 200
        liisa_session = liisa_sess_resp.json()["session"]

        unmasked_sess = client.get("/v1/customers", headers={"X-Approver-Session": anna_session}).json()
        assert any(
            appr.get("id") == ANNA
            for cust in unmasked_sess
            for appr in (cust.get("approvers") or [])
        )

        assert client.post(
            "/v1/voice/prompt",
            headers={**HOTEL_AGENT, "X-Approver-Session": anna_session},
            json={"order_id": oid, "audio": False},
        ).status_code == 403
        vp_pin_only = client.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Pin": anna_pin},
            json={"order_id": oid, "audio": False},
        )
        assert vp_pin_only.status_code == 401 and vp_pin_only.json()["error"] == "approver_session_required"

        vp_unk_cust = client.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Session": anna_session, "X-Customer-Id": "ghost-corp"},
            json={"order_id": oid, "audio": False},
        )
        assert vp_unk_cust.status_code == 403 and vp_unk_cust.json()["error"] == "unknown_customer"

        vp_cross = client.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Session": liisa_session},
            json={"order_id": oid, "audio": False},
        )
        assert vp_cross.status_code == 403 and vp_cross.json()["error"] == "forbidden_approver"

        vp_ok = client.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Session": anna_session},
            json={"order_id": oid, "audio": False},
        )
        assert vp_ok.status_code == 200
        assert vp_ok.json()["order_id"] == oid
        assert vp_ok.json()["approver"] == ANNA

        vi_ok = client.post(
            "/v1/voice/intent",
            headers={"X-Approver-Session": anna_session},
            json={"order_id": oid, "transcript": "yes please approve this order"},
        )
        assert vi_ok.status_code == 200
        assert vi_ok.json()["decides"] is False
        assert vi_ok.json()["intent"] == "approve"
        assert client.get(f"/v1/orders/{oid}").json()["status"] == "pending_approval"

        for _ in range(5):
            bad_mikko = client.post(
                "/v1/auth/approver-session",
                headers={"X-Approver-Pin": "wrong-mikko-passphrase-0000"},
                json={"approver": MIKKO},
            )
            assert bad_mikko.status_code in (401, 429)
        locked_mikko = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": pins[MIKKO]},
            json={"approver": MIKKO},
        )
        assert locked_mikko.status_code == 429 and locked_mikko.json()["error"] == "approver_locked"

        liisa_still_ok = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": liisa_pin},
            json={"approver": LIISA},
        )
        assert liisa_still_ok.status_code == 200

        mismatch_dec = client.post(
            f"/v1/approvals/{oid}/decision",
            headers={"X-Approver-Session": anna_session},
            json={"decision": "approve", "approver": MIKKO, "quote_hash": qhash},
        )
        assert mismatch_dec.status_code == 403 and mismatch_dec.json()["error"] == "approver_mismatch"

        wrong_org_dec = client.post(
            f"/v1/approvals/{oid}/decision",
            headers={"X-Approver-Session": liisa_session},
            json={"decision": "approve", "approver": LIISA, "quote_hash": qhash},
        )
        assert wrong_org_dec.status_code == 403

        ok_dec = client.post(
            f"/v1/approvals/{oid}/decision",
            headers={"X-Approver-Session": anna_session},
            json={"decision": "approve", "quote_hash": qhash, "note": "signed session approval"},
        )
        assert ok_dec.status_code == 200, ok_dec.text
        placed = ok_dec.json()
        assert placed["status"] == "placed"
        assert placed["approved_by"] == ANNA
        assert placed["response_code"] == "AP"

        auth_audit = client.get("/v1/audit", params={"subject_id": "auth:approver-session"}).json()
        assert any(ev.get("action") == "approval.refused" for ev in auth_audit)
        assert client.get("/v1/audit/verify").json()["ok"] is True


def test_plan_lines_injection_screen_across_http_recipes_mcp_agent_runs_and_demo(
    shared_gateway: tuple[str, dict[str, str], Path],
) -> None:
    base, gw_env, key_dir = shared_gateway
    anna_pin = _pins_from_key_dir(base, key_dir)[ANNA]
    ddate = _next_delivery_date(base)

    need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 300,
        "unit": "L",
        "delivery_by": ddate,
        "preferred_product_ids": ["463038"],
    }

    with httpx.Client(base_url=base, timeout=25.0) as client:
        clean_plan_resp = client.post("/v1/plan", headers=HOTEL_AGENT, json=need)
        assert clean_plan_resp.status_code == 200, clean_plan_resp.text
        clean_plan = clean_plan_resp.json()
        assert clean_plan["lines"]
        for item in [*clean_plan["lines"], *clean_plan["candidates"]]:
            screen = item.get("injection_screen")
            assert isinstance(screen, dict)
            assert screen.get("suspicious") is False
            assert isinstance(screen.get("reasons"), list)

        inj_headers = {**HOTEL_AGENT, "X-Aimo-Faults": "inject_text:464306"}
        inj_plan_resp = client.post("/v1/plan", headers=inj_headers, json=need)
        assert inj_plan_resp.status_code == 200, inj_plan_resp.text
        inj_plan = inj_plan_resp.json()
        line_464306 = next(ln for ln in inj_plan["lines"] if ln["product_id"] == "464306")
        assert isinstance(line_464306.get("injection_screen"), dict)
        assert line_464306["injection_screen"]["suspicious"] is True
        assert len(line_464306["injection_screen"].get("reasons") or []) >= 1
        assert "supplier_text_injection_suspected" in (line_464306.get("flags") or [])

        q_inj = client.post(
            "/v1/quotes",
            headers=inj_headers,
            json={
                "customer_id": "hotel-aurora",
                "agent_id": "buyer-1",
                "delivery_date": ddate,
                "lines": [
                    {
                        "product_id": ln["product_id"],
                        "qty_sales_units": ln["qty_sales_units"],
                        "tier": ln.get("tier"),
                        "flags": ln.get("flags") or [],
                    }
                    for ln in inj_plan["lines"]
                ],
                "need": need,
            },
        ).json()
        o_inj = client.post(
            "/v1/orders",
            headers=inj_headers,
            json={"quote_id": q_inj["quote_id"], "quote_hash": q_inj["quote_hash"]},
        ).json()
        assert o_inj["status"] == "pending_approval"
        assert any(
            str(h.get("rule_id") or "").startswith("injection") and h.get("outcome") == "needs_approval"
            for h in (o_inj.get("decision") or {}).get("hits", [])
        )
        client.post(
            f"/v1/approvals/{o_inj['order_id']}/decision",
            headers={"X-Approver-Pin": anna_pin},
            json={"decision": "reject", "approver": ANNA, "quote_hash": q_inj["quote_hash"]},
        )

        clean_rec = client.post(
            "/v1/recipes/plan",
            headers=HOTEL_AGENT,
            json={"recipe_id": "pancakes", "portions": 40, "delivery_by": ddate},
        )
        assert clean_rec.status_code == 200, clean_rec.text
        rec_lines = [
            ln
            for ing in clean_rec.json().get("ingredients", [])
            for ln in ((ing.get("plan") or {}).get("lines") or [])
        ]
        assert rec_lines
        for ln in rec_lines:
            assert isinstance(ln.get("injection_screen"), dict)
            assert ln["injection_screen"]["suspicious"] is False

        inj_rec = client.post(
            "/v1/recipes/plan",
            headers={**HOTEL_AGENT, "X-Aimo-Faults": "inject_text:*"},
            json={"recipe_id": "pancakes", "portions": 40, "delivery_by": ddate},
        )
        assert inj_rec.status_code == 200, inj_rec.text
        inj_rec_lines = [
            ln
            for ing in inj_rec.json().get("ingredients", [])
            for ln in ((ing.get("plan") or {}).get("lines") or [])
        ]
        assert inj_rec_lines
        assert any(
            (ln.get("injection_screen") or {}).get("suspicious") is True
            and "supplier_text_injection_suspected" in (ln.get("flags") or [])
            for ln in inj_rec_lines
        )

    from aimo.buyer.tools import MCPTransport

    mcp_clean = MCPTransport(
        f"{base}/mcp",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
    )
    try:
        mcp_plan = mcp_clean.call("plan_need", {"need": need})
        assert mcp_plan.get("lines")
        for ln in mcp_plan["lines"]:
            assert isinstance(ln.get("injection_screen"), dict)
            assert ln["injection_screen"]["suspicious"] is False
    finally:
        mcp_clean.close()

    mcp_inj = MCPTransport(
        f"{base}/mcp",
        headers={
            "X-Customer-Id": "hotel-aurora",
            "X-Agent-Id": "buyer-1",
            "X-Aimo-Faults": "inject_text:464306",
        },
    )
    try:
        mcp_inj_plan = mcp_inj.call("plan_need", {"need": need})
        mcp_464306 = next(ln for ln in mcp_inj_plan["lines"] if ln["product_id"] == "464306")
        assert mcp_464306["injection_screen"]["suspicious"] is True
    finally:
        mcp_inj.close()

    run_headers = {"X-Customer-Id": "hotel-aurora", "X-Approver-Pin": anna_pin}
    with httpx.Client(base_url=base, timeout=25.0) as client:
        clar_start = client.post(
            "/v1/agent/runs",
            headers=run_headers,
            json={
                "text": "We need 600 litres of lactose-free milk next week.",
                "customer_id": "hotel-aurora",
                "clarify": True,
            },
        )
        assert clar_start.status_code == 200, clar_start.text
        clar_id = clar_start.json()["run_id"]

        pending_q = None
        for _ in range(100):
            rv = client.get(f"/v1/agent/runs/{clar_id}", headers=run_headers).json()
            if rv.get("pending_question"):
                pending_q = rv["pending_question"]
                break
            if rv.get("status") in ("finished", "error"):
                break
            time.sleep(0.1)
        assert pending_q is not None
        first_opt = pending_q["options"][0]["id"]

        ans_resp = client.post(
            f"/v1/agent/runs/{clar_id}/answer",
            headers=run_headers,
            json={
                "option_id": first_opt,
                "question_id": pending_q.get("question_id"),
                "customer_id": "hotel-aurora",
            },
        )
        assert ans_resp.status_code == 200, ans_resp.text

        decided_clar = False
        for _ in range(150):
            rv = client.get(f"/v1/agent/runs/{clar_id}", headers=run_headers).json()
            wait_steps = [s for s in rv.get("steps", []) if s.get("kind") == "approval_wait"]
            if wait_steps and not decided_clar:
                wdata = wait_steps[-1]["data"]
                client.post(
                    f"/v1/approvals/{wdata['order_id']}/decision",
                    headers={"X-Approver-Pin": anna_pin},
                    json={"decision": "approve", "approver": ANNA, "quote_hash": wdata["quote_hash"]},
                )
                decided_clar = True
            if rv.get("status") in ("finished", "error"):
                break
            time.sleep(0.15)
        rv_clar = client.get(f"/v1/agent/runs/{clar_id}", headers=run_headers).json()
        assert rv_clar["status"] == "finished", rv_clar
        clar_plan_steps = [s for s in rv_clar["steps"] if s.get("kind") == "plan"]
        assert clar_plan_steps
        for ln in clar_plan_steps[-1]["data"]["plan"]["lines"]:
            assert isinstance(ln.get("injection_screen"), dict)
            assert ln["injection_screen"]["suspicious"] is False

        inj_start = client.post(
            "/v1/agent/runs",
            headers=run_headers,
            json={
                "text": "We need 300 litres of lactose-free milk by Monday.",
                "customer_id": "hotel-aurora",
                "faults": "inject_text:464306",
                "clarify": False,
            },
        )
        assert inj_start.status_code == 200, inj_start.text
        inj_run_id = inj_start.json()["run_id"]

        decided_inj = False
        for _ in range(150):
            rv = client.get(f"/v1/agent/runs/{inj_run_id}", headers=run_headers).json()
            wait_steps = [s for s in rv.get("steps", []) if s.get("kind") == "approval_wait"]
            if wait_steps and not decided_inj:
                wdata = wait_steps[-1]["data"]
                client.post(
                    f"/v1/approvals/{wdata['order_id']}/decision",
                    headers={"X-Approver-Pin": anna_pin},
                    json={"decision": "reject", "approver": ANNA, "quote_hash": wdata["quote_hash"]},
                )
                decided_inj = True
            if rv.get("status") in ("finished", "error"):
                break
            time.sleep(0.15)
        rv_inj = client.get(f"/v1/agent/runs/{inj_run_id}", headers=run_headers).json()
        assert rv_inj["status"] == "finished", rv_inj
        inj_plan_steps = [s for s in rv_inj["steps"] if s.get("kind") == "plan"]
        assert inj_plan_steps
        assert any(
            (ln.get("injection_screen") or {}).get("suspicious") is True
            for ln in inj_plan_steps[-1]["data"]["plan"]["lines"]
        )

        listed_runs = client.get("/v1/agent/runs", headers=run_headers)
        assert listed_runs.status_code == 200, listed_runs.text
        listed_ids = {item["run_id"] for item in listed_runs.json()}
        assert clar_id in listed_ids
        assert inj_run_id in listed_ids

    demo_env = {**gw_env, "AIMO_APPROVER_PIN": anna_pin}
    demo_happy = subprocess.run(
        [
            sys.executable,
            "-m",
            "aimo.cli",
            "demo",
            "--base-url",
            base,
            "--scenario",
            "happy",
            "--auto-approve-as",
            ANNA,
            "--approve-delay",
            "0.2",
            "--approval-timeout",
            "25",
        ],
        cwd=ROOT,
        env=demo_env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert demo_happy.returncode == 0, demo_happy.stdout + demo_happy.stderr
    assert "[injection_screen: clean]" in demo_happy.stdout

    demo_inj = subprocess.run(
        [
            sys.executable,
            "-m",
            "aimo.cli",
            "demo",
            "--base-url",
            base,
            "--scenario",
            "injection",
            "--auto-approve-as",
            ANNA,
            "--reject",
            "--approve-delay",
            "0.2",
            "--approval-timeout",
            "25",
        ],
        cwd=ROOT,
        env=demo_env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert demo_inj.returncode == 0, demo_inj.stdout + demo_inj.stderr
    assert "[injection_screen: FLAGGED" in demo_inj.stdout


def test_conformance_entrypoint_against_shared_gateway(
    shared_gateway: tuple[str, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    base, gw_env, key_dir = shared_gateway
    anna_pin = _pins_from_key_dir(base, key_dir)[ANNA]
    conf_dir = tmp_path / "conf_out"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_json = conf_dir / "conformance.json"
    try:
        conf_env = {**gw_env, "AIMO_APPROVER_PIN": anna_pin}
        res = subprocess.run(
            [sys.executable, "-m", "conformance", "--base", base, "--json", str(conf_json)],
            cwd=ROOT,
            env=conf_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert conf_json.is_file()
        report = json.loads(conf_json.read_text(encoding="utf-8"))
        assert report["summary"]["fail"] == 0, report["summary"]
        failed = [r for r in report.get("results", []) if r.get("status") == "FAIL"]
        assert not failed, failed
        statuses = {r.get("id"): r.get("status") for r in report.get("results", [])}
        for cid in ("A4", "A5", "A6", "O1", "O2", "O3"):
            assert statuses.get(cid) == "PASS", (cid, statuses)
    finally:
        shutil.rmtree(conf_dir, ignore_errors=True)

