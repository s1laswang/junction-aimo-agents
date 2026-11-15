from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Iterator
import xml.etree.ElementTree as ET

import httpx
import pytest

from aimo.backend import clock
from aimo.backend.peppol import NS_CBC, check_t116_rules
from aimo.models import Quote
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "pitch" / "requote_beat.py"
TEST_FILE = Path(__file__).resolve()
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"
SECRET_PIN = "requote-secret-pin-4242"

SUMMARY_RE = re.compile(
    r"^REQUOTE (ORD-[A-Za-z0-9-]+) -> (ORD-[A-Za-z0-9-]+); "
    r"409 requote_needed \(EUR (\d+\.\d{2}) -> EUR (\d+\.\d{2})\); "
    r"Peppol OrderResponseCode AP; "
    r"audit verify ok=True \((\d+) events checked\); "
    r"served_from=replay$"
)


def _load_requote_module():
    spec = importlib.util.spec_from_file_location("requote_beat_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _keyless_subprocess_env(srv_dir: Path) -> dict[str, str]:
    clean = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
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
        }
    }
    clean.update(
        {
            "PYTHONPATH": str(ROOT),
            "AIMO_DATA_DIR": str(srv_dir / "data"),
            "AIMO_STATE_DB": str(srv_dir / "state.sqlite"),
            "AIMO_KEY_DIR": str(srv_dir / "keys"),
            "AIMO_REPLAY": str(REPLAY_DIR),
            "AIMO_OFFLINE": "0",
            "AIMO_APPROVER_PIN": SECRET_PIN,
            "AIMO_API_KEY": "",
            "VALIO_API_KEY": "",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
    )
    return clean


@pytest.fixture(scope="module")
def replay_gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    if not REPLAY_META.is_file():
        pytest.skip("S1 replay snapshot meta.json is not present")
    srv_dir = tmp_path_factory.mktemp("requote_beat_gateway")
    (srv_dir / "data").mkdir(parents=True, exist_ok=True)
    (srv_dir / "keys").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    log_path = srv_dir / "uvicorn.log"
    log_file = open(log_path, "w+", encoding="utf-8")
    env = _keyless_subprocess_env(srv_dir)
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
    for _ in range(150):
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        log_file.seek(0)
        proc.kill()
        tail = log_file.read()[-2500:]
        log_file.close()
        pytest.fail(f"aimo.gateway.app:app failed to start:\n{tail}")
    try:
        yield base, srv_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def _assert_requote_output_and_gateway_state(base: str, stdout: str, stderr: str) -> None:
    assert SECRET_PIN not in stdout
    assert SECRET_PIN not in stderr
    assert "REQUOTE BEAT OK" in stdout

    summary_lines = [
        ln.strip()
        for ln in stdout.splitlines()
        if ln.strip().startswith("REQUOTE ") and ln.strip() != "REQUOTE BEAT OK"
    ]
    assert len(summary_lines) == 1, stdout
    m = SUMMARY_RE.match(summary_lines[0])
    assert m is not None, summary_lines[0]
    old_oid, new_oid, old_eur, new_eur, checked_s = m.groups()
    assert float(new_eur) > float(old_eur)
    assert float(old_eur) == pytest.approx(1010.00)
    assert float(new_eur) == pytest.approx(1262.50)
    assert int(checked_s) >= 10

    with httpx.Client(base_url=base, timeout=15.0) as client:
        old_order = client.get(f"/v1/orders/{old_oid}").json()
        assert old_order["status"] == "expired"
        assert (old_order.get("provenance") or {}).get("requoted_as") == new_oid
        assert (old_order.get("provenance") or {}).get("requote_reason") == "quote_expired"

        new_order = client.get(f"/v1/orders/{new_oid}").json()
        assert new_order["status"] == "placed"
        assert new_order["response_code"] == "AP"
        assert new_order["approved_by"] == "anna@hotel-aurora.example"
        assert (new_order.get("provenance") or {}).get("supersedes") == old_oid

        jwks_resp = client.get("/.well-known/jwks.json")
        assert jwks_resp.status_code == 200
        jwks = jwks_resp.json()
        assert jwks.get("keys") and jwks["keys"][0]["kty"] == "OKP" and jwks["keys"][0]["crv"] == "Ed25519"

        new_quote_resp = client.get(f"/v1/quotes/{new_order['quote_id']}")
        assert new_quote_resp.status_code == 200
        new_quote_dict = new_quote_resp.json()
        quote_obj = Quote.model_validate(new_quote_dict)
        created_now = clock.parse(new_quote_dict["created_at"])
        ver = signing.verify_quote_detailed(quote_obj, jwks_=jwks, now=created_now)
        assert ver["ok"] is True, ver
        assert ver["signature_valid"] is True
        assert ver["hash_matches"] is True
        assert ver["expired"] is False

        peppol_resp = client.get(f"/v1/orders/{new_oid}/peppol-order-response.xml")
        assert peppol_resp.status_code == 200
        assert check_t116_rules(peppol_resp.text) == []
        xml_root = ET.fromstring(peppol_resp.text.encode("utf-8"))
        code = xml_root.findtext("cbc:OrderResponseCode", None, {"cbc": NS_CBC})
        assert code is not None and code.strip() == "AP"

        audit_resp = client.get("/v1/audit", params={"subject_id": old_oid})
        assert audit_resp.status_code == 200
        events = audit_resp.json()
        requoted = [e for e in events if e.get("action") == "order.requoted"]
        assert requoted, events
        assert (requoted[-1].get("data") or {}).get("new_order_id") == new_oid

        verify_resp = client.get("/v1/audit/verify")
        assert verify_resp.status_code == 200
        verify_body = verify_resp.json()
        assert verify_body["ok"] is True
        assert verify_body.get("broken_at") is None


def test_requote_beat_subprocess_against_live_replay_gateway(replay_gateway: tuple[str, Path]) -> None:
    base, srv_dir = replay_gateway
    env = _keyless_subprocess_env(srv_dir)
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert res.returncode == 0, res.stdout + "\n" + res.stderr
    _assert_requote_output_and_gateway_state(base, res.stdout, res.stderr)


def test_requote_beat_direct_main_against_live_replay_gateway(
    replay_gateway: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, srv_dir = replay_gateway
    for k, v in _keyless_subprocess_env(srv_dir).items():
        monkeypatch.setenv(k, v)
    mod = _load_requote_module()
    rc = mod.main(["--base", base, "--date", "2026-11-17"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + "\n" + captured.err
    assert "delivery date 2026-11-17" in captured.out
    _assert_requote_output_and_gateway_state(base, captured.out, captured.err)


def test_requote_beat_pin_resolution_and_failure_exit_without_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_requote_module()
    key_dir = tmp_path / "keys"
    key_dir.mkdir(parents=True)
    pin_from_json = "anna-json-pin-123456"
    (key_dir / "approver_pins.json").write_text(
        json.dumps({"anna@hotel-aurora.example": pin_from_json}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    assert mod._approver_pin() == pin_from_json

    legacy_dir = tmp_path / "legacy-keys"
    legacy_dir.mkdir(parents=True)
    legacy_pin = "anna-legacy-pin-654321"
    (legacy_dir / "approver_pin").write_text(legacy_pin + "\n", encoding="utf-8")
    monkeypatch.setenv("AIMO_KEY_DIR", str(legacy_dir))
    assert mod._approver_pin() == legacy_pin

    class BadHealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": false}')

    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), BadHealthHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        rc = mod.main(["--base", f"http://127.0.0.1:{port}"])
        sub_res = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", f"http://127.0.0.1:{port}"],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "AIMO_KEY_DIR": str(legacy_dir),
                "AIMO_APPROVER_PIN": "",
                "AIMO_APPROVER_PINS": "",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    finally:
        with contextlib.suppress(Exception):
            srv.shutdown()
            srv.server_close()
    captured = capsys.readouterr()
    assert rc == 1
    assert sub_res.returncode == 1
    assert "REQUOTE BEAT OK" not in captured.out
    assert "REQUOTE BEAT OK" not in sub_res.stdout
    assert legacy_pin not in captured.out and legacy_pin not in captured.err
    assert legacy_pin not in sub_res.stdout and legacy_pin not in sub_res.stderr


def test_requote_beat_files_have_no_comments_or_em_dashes() -> None:
    for path in (SCRIPT, TEST_FILE):
        text = path.read_text(encoding="utf-8")
        assert "\x23" not in text, f"Comment hash found in {path}"
        assert "\u2014" not in text, f"Em dash found in {path}"
        assert "\u2013" not in text, f"En dash found in {path}"
