from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from aimo import config
from aimo.backend.peppol import NS_CBC, check_t116_rules, header_code
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.mock_model import MOCK_MODEL_NAME
from aimo.buyer.need import now_helsinki
from aimo.buyer.tools import MCPTransport
from aimo.models import Order, Quote
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
APPROVER = "anna@hotel-aurora.example"


def _free_port() -> int:
    for port in range(8945, 9045):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port found")


@pytest.fixture(scope="module")
def toy_server(tmp_path_factory):
    server_dir = tmp_path_factory.mktemp("toy_scripted_buyer")
    port = _free_port()
    log_path = server_dir / "toywholesaler.log"
    log_file = open(log_path, "w+", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(server_dir),
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "AIMO_API_KEY": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "toywholesaler.app:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=log_file,
        stderr=log_file,
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
        log_file.seek(0)
        proc.kill()
        log_text = log_file.read()[-2000:]
        log_file.close()
        pytest.fail(f"toywholesaler server failed to start:\n{log_text}")
    try:
        yield base, server_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


@pytest.fixture(autouse=True)
def keyless_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    missing_product_json = tmp_path / "absent_valio_aimo_junction26_product_data.json"
    monkeypatch.setattr(config, "PRODUCT_JSON", missing_product_json)

    def _forbid_gemini(*args, **kwargs):
        raise AssertionError("Gemini API must not be called in keyless scripted mode")

    monkeypatch.setattr("aimo.gemini.generate", _forbid_gemini)


def _mcp_transport(base: str) -> MCPTransport:
    return MCPTransport(
        f"{base}/mcp",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
        timeout=30.0,
    )


def _peppol_code(base: str, order_id: str) -> str:
    resp = httpx.get(f"{base}/v1/orders/{order_id}/peppol-order-response.xml", timeout=10.0)
    assert resp.status_code == 200
    assert check_t116_rules(resp.text) == []
    root = ET.fromstring(resp.text.encode("utf-8"))
    code = root.findtext("cbc:OrderResponseCode", None, {"cbc": NS_CBC})
    assert code is not None
    return code.strip()


def _verify_quote_and_audit(base: str, quote_dict: dict, order_id: str, tmp_path: Path) -> None:
    jwks_resp = httpx.get(f"{base}/.well-known/jwks.json", timeout=10.0)
    assert jwks_resp.status_code == 200
    jwks = jwks_resp.json()
    assert jwks["keys"] and jwks["keys"][0]["kty"] == "OKP" and jwks["keys"][0]["crv"] == "Ed25519"

    quote_obj = Quote.model_validate(quote_dict)
    detailed = signing.verify_quote_detailed(quote_obj, jwks_=jwks)
    assert detailed["ok"] is True
    assert detailed["signature_valid"] is True
    assert detailed["hash_matches"] is True
    assert detailed["expired"] is False

    q_file = tmp_path / f"{quote_obj.quote_id}.json"
    jwks_file = tmp_path / "jwks.json"
    q_file.write_text(json.dumps(quote_dict), encoding="utf-8")
    jwks_file.write_text(json.dumps(jwks), encoding="utf-8")
    verifier = str(ROOT / "scripts" / "verify_quote.py")
    q_check = subprocess.run(
        [sys.executable, "-I", verifier, str(q_file), str(jwks_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert q_check.returncode == 0 and "VERIFIED" in q_check.stdout, q_check.stdout + q_check.stderr

    audit_ver = httpx.get(f"{base}/v1/audit/verify", timeout=10.0).json()
    assert audit_ver["ok"] is True
    assert audit_ver["broken_at"] is None
    assert audit_ver["checked"] >= 3
    assert audit_ver["kids"] == [jwks["keys"][0]["kid"]]

    events = httpx.get(f"{base}/v1/audit", params={"subject_id": order_id}, timeout=10.0).json()
    assert events
    ev_file = tmp_path / f"{order_id}-events.json"
    ev_file.write_text(json.dumps(events), encoding="utf-8")
    ev_check = subprocess.run(
        [sys.executable, "-I", verifier, str(ev_file), str(jwks_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ev_check.returncode == 0 and "VERIFIED" in ev_check.stdout, ev_check.stdout + ev_check.stderr


def _decide_when_pending(base: str, seen_ids: set[str], decision: str, check_hash_mismatch: bool = False) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def _worker() -> None:
        try:
            for _ in range(200):
                time.sleep(0.05)
                inbox = httpx.get(
                    f"{base}/v1/approvals",
                    params={"status": "pending", "customer_id": "hotel-aurora"},
                    timeout=5.0,
                ).json()
                fresh = [item for item in inbox if item["order"]["order_id"] not in seen_ids]
                if not fresh:
                    continue
                item = fresh[0]
                oid = item["order"]["order_id"]
                qhash = item["order"]["quote_hash"]
                outcome["pending_peppol"] = _peppol_code(base, oid)
                if check_hash_mismatch:
                    bad = httpx.post(
                        f"{base}/v1/approvals/{oid}/decision",
                        json={"decision": decision, "approver": APPROVER, "quote_hash": "0" * 64},
                        timeout=5.0,
                    )
                    outcome["mismatch_status"] = bad.status_code
                resp = httpx.post(
                    f"{base}/v1/approvals/{oid}/decision",
                    json={
                        "decision": decision,
                        "approver": APPROVER,
                        "quote_hash": qhash,
                        "note": f"human {decision} in toywholesaler e2e test",
                    },
                    timeout=10.0,
                )
                outcome["status_code"] = resp.status_code
                outcome["body"] = resp.json()
                return
            outcome["error"] = "timed out waiting for pending order in /v1/approvals"
        except BaseException as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread, outcome


def test_keyless_buyer_mcp_auto_approved_small_order(toy_server, tmp_path):
    base, _ = toy_server
    assert not config.PRODUCT_JSON.exists()
    target = now_helsinki().date() + timedelta(days=5)
    weekday = target.strftime("%A")

    transport = _mcp_transport(base)
    try:
        tool_names = {spec.name for spec in transport.list_tools()}
        assert {"plan_need", "create_quote", "place_order", "get_order_status", "get_audit_trail", "list_policies"} <= tool_names
        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.05, approval_timeout_s=10.0, audit_base_url=base)
        assert agent.mock is True
        assert agent.use_gemini_parse is False
        assert agent.model == MOCK_MODEL_NAME

        result = agent.run(f"We need 10 kg of sliced rye bread at Hotel Aurora Helsinki by {weekday}.")
        assert result["mock"] is True
        assert result["need"]["preferences"]["parsed_by"] == "regex"
        assert result["need"]["quantity"] == 10.0
        assert result["need"]["unit"] == "KG"
        assert result["need"]["delivery_by"] == target.isoformat()
        assert "rye bread" in result["need"]["product_query"].lower()

        assert result["plan"]["shortfall"] == 0
        assert result["quote"]["total_net"] <= 300.0
        assert result["status"] == "placed"
        assert result["order"]["status"] == "placed"
        assert result["order"]["decision"]["outcome"] == "auto_approve"
        assert result["order"]["response_code"] == "AP"
        assert not any(step["kind"] == "approval_wait" for step in result["steps"])
        assert _peppol_code(base, result["order"]["order_id"]) == "AP"

        trail = transport.call("get_audit_trail", {"subject_id": result["order"]["order_id"]})
        assert trail["verify"]["ok"] is True
        _verify_quote_and_audit(base, result["quote"], result["order"]["order_id"], tmp_path)
    finally:
        transport.close()


def test_keyless_buyer_mcp_over_limit_approved_via_decision_endpoint(toy_server, tmp_path):
    base, _ = toy_server
    assert not config.PRODUCT_JSON.exists()
    target = now_helsinki().date() + timedelta(days=5)
    existing = {
        item["order"]["order_id"]
        for item in httpx.get(f"{base}/v1/approvals", params={"status": "pending", "customer_id": "hotel-aurora"}, timeout=5.0).json()
    }

    transport = _mcp_transport(base)
    try:
        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.05, approval_timeout_s=15.0, audit_base_url=base)
        assert agent.mock is True
        thread, human = _decide_when_pending(base, existing, decision="approve", check_hash_mismatch=True)
        result = agent.run(f"We need 100 kg of sliced rye bread at Hotel Aurora Helsinki by {target.isoformat()}.")
        thread.join(timeout=5.0)

        assert "error" not in human, human
        assert human["pending_peppol"] == "AB"
        assert human["mismatch_status"] == 409
        assert human["status_code"] == 200

        assert result["mock"] is True
        assert result["need"]["preferences"]["parsed_by"] == "regex"
        assert result["quote"]["total_net"] > 300.0
        assert any(step["kind"] == "approval_wait" for step in result["steps"])
        assert result["status"] == "placed"
        assert result["order"]["status"] == "placed"
        assert result["order"]["approved_by"] == APPROVER
        assert result["order"]["approver_role"] == "procurement_manager"
        assert result["order"]["response_code"] == "AP"
        assert _peppol_code(base, result["order"]["order_id"]) == "AP"

        _verify_quote_and_audit(base, result["quote"], result["order"]["order_id"], tmp_path)
    finally:
        transport.close()


def test_keyless_buyer_mcp_over_limit_human_rejection(toy_server, tmp_path):
    base, _ = toy_server
    assert not config.PRODUCT_JSON.exists()
    target = now_helsinki().date() + timedelta(days=6)
    existing = {
        item["order"]["order_id"]
        for item in httpx.get(f"{base}/v1/approvals", params={"status": "pending", "customer_id": "hotel-aurora"}, timeout=5.0).json()
    }

    transport = _mcp_transport(base)
    try:
        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.05, approval_timeout_s=15.0, audit_base_url=base)
        assert agent.mock is True
        thread, human = _decide_when_pending(base, existing, decision="reject", check_hash_mismatch=False)
        result = agent.run(f"We need 110 kg of sliced rye bread at Hotel Aurora Helsinki by {target.isoformat()}.")
        thread.join(timeout=5.0)

        assert "error" not in human, human
        assert human["status_code"] == 200
        assert human["body"]["status"] == "rejected"

        assert result["mock"] is True
        assert any(step["kind"] == "approval_wait" for step in result["steps"])
        assert result["status"] == "rejected"
        assert result["order"]["status"] == "rejected"
        assert result["order"]["approved_by"] == APPROVER
        assert result["order"].get("response_code") in (None, "RE")
        assert header_code(Order.model_validate(result["order"])) == "RE"
        assert _peppol_code(base, result["order"]["order_id"]) == "RE"

        _verify_quote_and_audit(base, result["quote"], result["order"]["order_id"], tmp_path)
    finally:
        transport.close()
