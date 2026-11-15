from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from google.genai import types

from aimo import config, gemini
from aimo.backend import clock
from aimo.backend.peppol import NS_CBC, check_t116_rules
from aimo.buyer import run_demo
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, MockModel, mock_wanted
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport
from aimo.models import Quote
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
RYE_OVER_LIMIT = "We need 100 kg of sliced rye bread at Hotel Aurora Helsinki by Friday."
RYE_REJECT = "We need 105 kg of sliced rye bread at Hotel Aurora Helsinki by Friday."
RYE_AUTO = "We need 10 kg of sliced rye bread at Hotel Aurora Helsinki by Friday."
MILK_AUTO = "We need 20 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
APPROVER = "anna@hotel-aurora.example"
APPROVER_PIN = "test-pin-12chars-ok"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _set_api_keys(monkeypatch: pytest.MonkeyPatch, gemini_key: str, google_key: str) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


@pytest.fixture
def keyless_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    _set_api_keys(monkeypatch, "", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AIMO_BUYER_TRANSPORT", raising=False)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_generate(*args, **kwargs):
        calls.append("generate")
        raise AssertionError("aimo.gemini.generate must not be called in keyless mode")

    def forbid_embed(*args, **kwargs):
        calls.append("embed")
        raise AssertionError("aimo.gemini.embed must not be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_generate)
    monkeypatch.setattr("aimo.gemini.embed", forbid_embed)
    return calls


@contextlib.contextmanager
def _live_server(
    app_target: str,
    srv_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[str, Path]]:
    data_dir = srv_dir / "data"
    key_dir = srv_dir / "keys"
    state_db = srv_dir / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    log_path = srv_dir / "uvicorn.log"
    log_file = open(log_path, "w+", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(data_dir),
        "AIMO_STATE_DB": str(state_db),
        "AIMO_KEY_DIR": str(key_dir),
        "AIMO_APPROVER_PIN": APPROVER_PIN,
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "AIMO_API_KEY": "",
        "VALIO_API_KEY": "",
    }
    env.pop("AIMO_CATALOG_FIXTURE", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            app_target,
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
        pytest.fail(f"{app_target} failed to start:\n{tail}")
    try:
        yield base, srv_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


@pytest.fixture(scope="module")
def toy_live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    srv_dir = tmp_path_factory.mktemp("keyless_toy_server")
    with _live_server("toywholesaler.app:app", srv_dir) as handle:
        yield handle


@pytest.fixture(scope="module")
def replay_live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    if not REPLAY_META.is_file():
        pytest.skip("S1 replay snapshot meta.json is not present")
    srv_dir = tmp_path_factory.mktemp("keyless_replay_server")
    extra = {"AIMO_REPLAY": str(REPLAY_DIR), "AIMO_OFFLINE": "0"}
    with _live_server("aimo.gateway.app:app", srv_dir, extra_env=extra) as handle:
        yield handle


def _verify_signed_quote_peppol_and_audit(base: str, quote_dict: dict, order_id: str, expected_code: str) -> None:
    jwks_resp = httpx.get(f"{base}/.well-known/jwks.json", timeout=10.0)
    assert jwks_resp.status_code == 200
    jwks = jwks_resp.json()
    assert jwks.get("keys") and jwks["keys"][0]["kty"] == "OKP" and jwks["keys"][0]["crv"] == "Ed25519"

    health = httpx.get(f"{base}/healthz", timeout=10.0).json()
    srv_now = clock.parse((health.get("clock") or {}).get("now"))
    quote_obj = Quote.model_validate(quote_dict)
    detailed = signing.verify_quote_detailed(quote_obj, jwks_=jwks, now=srv_now)
    assert detailed["ok"] is True, detailed
    assert detailed["signature_valid"] is True
    assert detailed["hash_matches"] is True
    assert detailed["expired"] is False

    peppol_resp = httpx.get(f"{base}/v1/orders/{order_id}/peppol-order-response.xml", timeout=10.0)
    assert peppol_resp.status_code == 200
    assert check_t116_rules(peppol_resp.text) == []
    root = ET.fromstring(peppol_resp.text.encode("utf-8"))
    code = root.findtext("cbc:OrderResponseCode", None, {"cbc": NS_CBC})
    assert code is not None and code.strip() == expected_code

    audit_resp = httpx.get(f"{base}/v1/audit/verify", timeout=10.0)
    assert audit_resp.status_code == 200
    audit_ver = audit_resp.json()
    assert audit_ver["ok"] is True
    assert audit_ver.get("broken_at") is None
    assert audit_ver.get("checked", 0) >= 3


def _collect_sse_events(base: str, events_url: str, headers: dict[str, str]) -> list[dict]:
    events: list[dict] = []
    cur: dict[str, str] = {}
    with httpx.stream("GET", f"{base}{events_url}", headers=headers, timeout=30.0) as resp:
        assert resp.status_code == 200
        for raw_line in resp.iter_lines():
            line = raw_line.strip("\r")
            if not line:
                if "data" in cur:
                    step_data = json.loads(cur["data"])
                    events.append(
                        {
                            "id": cur.get("id"),
                            "event": cur.get("event"),
                            "data": step_data,
                        }
                    )
                    if cur.get("event") == "final" or step_data.get("kind") == "final":
                        return events
                cur = {}
                continue
            if line.startswith(":"):
                continue
            field, _, val = line.partition(":")
            cur[field] = val.lstrip(" ")
    if "data" in cur:
        events.append({"id": cur.get("id"), "event": cur.get("event"), "data": json.loads(cur["data"])})
    return events


def _start_http_approver_safety_net(base: str, customer_id: str) -> threading.Event:
    stop = threading.Event()
    seen: set[str] = set()
    human_headers = {"X-Approver-Pin": APPROVER_PIN}

    def worker() -> None:
        while not stop.is_set():
            try:
                resp = httpx.get(
                    f"{base}/v1/approvals",
                    params={"status": "pending", "customer_id": customer_id},
                    headers=human_headers,
                    timeout=5.0,
                )
                items = resp.json() if resp.status_code == 200 else []
                for item in items if isinstance(items, list) else []:
                    order = item.get("order", item)
                    oid = order.get("order_id")
                    if not oid or oid in seen:
                        continue
                    seen.add(oid)
                    httpx.post(
                        f"{base}/v1/approvals/{oid}/decision",
                        headers=human_headers,
                        json={
                            "decision": "approve",
                            "approver": APPROVER,
                            "quote_hash": order.get("quote_hash"),
                            "note": "approved in SSE e2e test",
                        },
                        timeout=10.0,
                    )
            except Exception:
                pass
            stop.wait(0.05)

    threading.Thread(target=worker, daemon=True).start()
    return stop


def test_file_conventions_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


@pytest.mark.parametrize("scenario", ["happy", "stockout", "school-allergen", "injection", "drift", "event"])
def test_all_six_fake_scenarios_keyless_end_to_end(
    scenario: str,
    keyless_env: list[str],
    tmp_path: Path,
) -> None:
    assert set(run_demo.SCENARIOS) == {"happy", "stockout", "school-allergen", "injection", "drift", "event"}
    assert mock_wanted() is True

    out_json = tmp_path / f"{scenario}.json"
    rc = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            scenario,
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "10",
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))

    ok, why = run_demo.verdict(scenario, res)
    assert ok is True, f"{scenario}: {why}"
    assert res["mock"] is True
    assert res["transport"] == "fake"
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert all(n["preferences"]["parsed_by"] == "regex" for n in res["needs"])
    assert MOCK_LABEL in res["summary"]
    assert res["summary"].startswith(MOCK_LABEL)
    assert {m["model"] for m in res["models_used"]} == {MOCK_MODEL_NAME}

    step_kinds = [s["kind"] for s in res["steps"]]
    initial_statuses = [s["data"].get("status") for s in res["steps"] if s["kind"] == "decision"]
    if "pending_approval" in initial_statuses:
        assert "approval_wait" in step_kinds

    if scenario == "happy":
        assert "pending_approval" in initial_statuses
        assert "approval_wait" in step_kinds
        assert res["status"] == "placed"
        assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
    elif scenario == "stockout":
        assert "pending_approval" in initial_statuses
        assert "approval_wait" in step_kinds
        assert res["status"] == "placed"
        assert len(res["plan"]["lines"]) >= 2
        assert len(res["quote"]["lines"]) >= 2
    elif scenario == "school-allergen":
        assert "pending_approval" in initial_statuses
        assert "approval_wait" in step_kinds
        assert res["status"] == "placed"
        assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
        assert {"milk", "nuts"} <= set(res["quote"]["need"]["excluded_allergens"])
        plan_needs = [
            s["data"]["args"]["need"]
            for s in res["steps"]
            if s["kind"] == "tool_call" and s["data"].get("name") == "plan_need"
        ]
        assert plan_needs and {"milk", "nuts"} <= set(plan_needs[0]["excluded_allergens"])
        quoted_ids = {ln["product_id"] for ln in res["quote"]["lines"]}
        assert quoted_ids and "469040" not in quoted_ids and "471424" not in quoted_ids
        assert all("oat" in ln["product_name"].lower() for ln in res["quote"]["lines"])
    elif scenario == "injection":
        assert "pending_approval" in initial_statuses
        assert "approval_wait" in step_kinds
        assert res["status"] == "placed"
        inj_thoughts = [
            s
            for s in res["steps"]
            if s["kind"] == "thought" and "injection" in s["title"].lower() and "redacted" in s["title"].lower()
        ]
        assert len(inj_thoughts) >= 1
        gp_results = [
            s["data"].get("result")
            for s in res["steps"]
            if s["kind"] == "tool_result" and s["data"].get("name") == "get_product"
        ]
        assert any(
            isinstance(r, dict)
            and r.get("_untrusted_text_flagged") is True
            and "[REDACTED" in str(r.get("marketing_text", ""))
            for r in gp_results
        )
        decision_hits = [
            h
            for s in res["steps"]
            if s["kind"] == "decision"
            for h in (s["data"].get("decision") or {}).get("hits", [])
        ]
        assert any(
            "injection" in (h.get("rule_id") or "") and h.get("outcome") in ("needs_approval", "reject")
            for h in decision_hits
        )
        assert max(ln["qty_sales_units"] for ln in res["quote"]["lines"]) < 10000
    elif scenario == "drift":
        assert res["status"] == "needs_confirmation"
        assert res["quote"] is not None
        assert res["order"] is None
        assert "approval_wait" not in step_kinds
        unblocked_place = [
            s
            for s in res["steps"]
            if s["kind"] == "tool_call" and s["data"].get("name") == "place_order" and not s["data"].get("blocked")
        ]
        blocked_place = [
            s
            for s in res["steps"]
            if s["kind"] == "tool_call" and s["data"].get("name") == "place_order" and s["data"].get("blocked")
        ]
        assert unblocked_place == []
        assert len(blocked_place) == 1
    elif scenario == "event":
        assert res["status"] == "placed"
        assert len(res["needs"]) == 2
        queries = [n["product_query"].lower() for n in res["needs"]]
        assert any("cream" in q for q in queries)
        assert any("butter" in q for q in queries)
        assert all("FREE_FROM_LACTOSE" in n["required_claims"] for n in res["needs"])
        plan_calls = [
            s for s in res["steps"] if s["kind"] == "tool_call" and s["data"].get("name") == "plan_need"
        ]
        assert len(plan_calls) == 2
        assert len(res["quote"]["lines"]) >= 2

    assert keyless_env == []


def test_run_scenario_direct_keyless_smoke(keyless_env: list[str]) -> None:
    parser = argparse.Namespace(
        text=None,
        customer=None,
        base_url="http://127.0.0.1:9",
        reject=False,
        transport="fake",
        faults=None,
        approve_delay=0.05,
        no_auto_approve=False,
        model=None,
        thinking="LOW",
        no_escalate=False,
        max_steps=14,
        approval_timeout=10.0,
        image=None,
        answer="auto",
    )
    res = run_demo.run_scenario(parser, "drift", None)
    ok, why = run_demo.verdict("drift", res)
    assert ok is True, why
    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert MOCK_LABEL in res["summary"]
    assert keyless_env == []


def test_live_http_mcp_run_demo_places_order_and_verifies_jwks_peppol_audit(
    toy_live_server: tuple[str, Path],
    keyless_env: list[str],
    tmp_path: Path,
) -> None:
    base, _ = toy_live_server
    out_json = tmp_path / "placed.json"
    rc = run_demo.main(
        [
            "--base-url",
            base,
            "--text",
            RYE_OVER_LIMIT,
            "--auto-approve-as",
            APPROVER,
            "--approve-delay",
            "0.1",
            "--approval-timeout",
            "15",
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))

    assert res["status"] == "placed"
    assert res["transport"] == "mcp"
    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert MOCK_LABEL in res["summary"]
    assert any(s["kind"] == "approval_wait" for s in res["steps"])
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == APPROVER

    _verify_signed_quote_peppol_and_audit(base, res["quote"], res["order"]["order_id"], "AP")
    assert keyless_env == []


def test_live_http_mcp_run_demo_human_rejection_peppol_re_and_single_order(
    toy_live_server: tuple[str, Path],
    keyless_env: list[str],
    tmp_path: Path,
) -> None:
    base, _ = toy_live_server
    human_headers = {"X-Approver-Pin": APPROVER_PIN}
    before_orders = {
        item["order"]["order_id"]
        for item in httpx.get(
            f"{base}/v1/approvals",
            params={"status": "all", "customer_id": "hotel-aurora"},
            headers=human_headers,
            timeout=5.0,
        ).json()
    }

    out_json = tmp_path / "rejected.json"
    rc = run_demo.main(
        [
            "--base-url",
            base,
            "--text",
            RYE_REJECT,
            "--auto-approve-as",
            APPROVER,
            "--reject",
            "--approve-delay",
            "0.1",
            "--approval-timeout",
            "15",
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))

    assert res["status"] == "rejected"
    assert res["transport"] == "mcp"
    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert any(s["kind"] == "approval_wait" for s in res["steps"])
    assert res["order"]["status"] == "rejected"

    place_calls = [
        s for s in res["steps"] if s["kind"] == "tool_call" and s["data"].get("name") == "place_order"
    ]
    quote_calls = [
        s for s in res["steps"] if s["kind"] == "tool_call" and s["data"].get("name") == "create_quote"
    ]
    assert len(place_calls) == 1
    assert len(quote_calls) == 1

    after_orders = {
        item["order"]["order_id"]
        for item in httpx.get(
            f"{base}/v1/approvals",
            params={"status": "all", "customer_id": "hotel-aurora"},
            headers=human_headers,
            timeout=5.0,
        ).json()
    }
    assert after_orders - before_orders == {res["order"]["order_id"]}

    _verify_signed_quote_peppol_and_audit(base, res["quote"], res["order"]["order_id"], "RE")
    assert keyless_env == []


def test_live_http_agent_runs_sse_stream_keyless(
    toy_live_server: tuple[str, Path],
    keyless_env: list[str],
) -> None:
    base, _ = toy_live_server
    cust_headers = {"X-Customer-Id": "hotel-aurora", "X-Approver-Pin": APPROVER_PIN}
    stop = _start_http_approver_safety_net(base, "hotel-aurora")
    try:
        created = httpx.post(
            f"{base}/v1/agent/runs",
            headers=cust_headers,
            json={"text": RYE_AUTO, "customer_id": "hotel-aurora"},
            timeout=10.0,
        )
        assert created.status_code == 200, created.text
        body = created.json()
        run_id = body["run_id"]
        assert run_id.startswith("run-")
        events = _collect_sse_events(base, body["events_url"], headers=cust_headers)
    finally:
        stop.set()

    kinds = [e["event"] for e in events]
    for expected in ("thought", "tool_call", "plan", "quote", "decision", "final"):
        assert expected in kinds, (expected, kinds)
    assert kinds[-1] == "final"

    first_data = events[0]["data"]["data"]
    assert first_data["mock"] is True
    assert first_data["model"] == MOCK_MODEL_NAME
    assert any(MOCK_LABEL in e["data"]["title"] for e in events)
    assert any(e["data"].get("data", {}).get("parsed_by") == "regex" for e in events)

    final_data = events[-1]["data"]["data"]
    assert final_data["status"] == "placed"
    assert MOCK_LABEL in final_data["summary"]
    assert {m["model"] for m in final_data["models_used"]} == {MOCK_MODEL_NAME}

    detail = httpx.get(f"{base}/v1/agent/runs/{run_id}", headers=cust_headers, timeout=10.0)
    assert detail.status_code == 200
    view = detail.json()
    assert view["mock"] is True
    assert view["status"] == "finished"
    assert view["order_status"] == "placed"
    assert keyless_env == []


@pytest.mark.skipif(not REPLAY_META.is_file(), reason="S1 replay snapshot meta.json is not present")
def test_live_replay_gateway_run_demo_and_sse_keyless(
    replay_live_server: tuple[str, Path],
    keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base, _ = replay_live_server
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_OFFLINE", "0")

    out_json = tmp_path / "replay_placed.json"
    rc = run_demo.main(
        [
            "--base-url",
            base,
            "--scenario",
            "happy",
            "--auto-approve-as",
            APPROVER,
            "--approve-delay",
            "0.1",
            "--approval-timeout",
            "15",
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert res["status"] == "placed"
    assert res["transport"] == "mcp"
    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert MOCK_LABEL in res["summary"]
    _verify_signed_quote_peppol_and_audit(base, res["quote"], res["order"]["order_id"], "AP")

    cust_headers = {"X-Customer-Id": "hotel-aurora", "X-Approver-Pin": APPROVER_PIN}
    stop = _start_http_approver_safety_net(base, "hotel-aurora")
    try:
        created = httpx.post(
            f"{base}/v1/agent/runs",
            headers=cust_headers,
            json={"text": MILK_AUTO, "customer_id": "hotel-aurora", "clarify": False},
            timeout=10.0,
        )
        assert created.status_code == 200, created.text
        body = created.json()
        events = _collect_sse_events(base, body["events_url"], headers=cust_headers)
    finally:
        stop.set()

    kinds = [e["event"] for e in events]
    for expected in ("thought", "tool_call", "plan", "quote", "decision", "final"):
        assert expected in kinds, (expected, kinds)
    assert events[0]["data"]["data"]["mock"] is True
    assert events[-1]["data"]["data"]["status"] == "placed"
    assert keyless_env == []


def _fc(name: str, args: dict) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ]
    )


def _txt(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


def _last_fn_response(contents: list, name: str) -> dict | None:
    for item in reversed(contents):
        for part in getattr(item, "parts", None) or []:
            if getattr(part, "function_response", None) and part.function_response.name == name:
                return part.function_response.response
    return None


@pytest.mark.parametrize("key_var", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
@pytest.mark.parametrize("llm_mode", [None, "auto"])
def test_primary_gemini_path_preserved_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
    key_var: str,
    llm_mode: str | None,
) -> None:
    _set_api_keys(
        monkeypatch,
        "live-gemini-test-key" if key_var == "GEMINI_API_KEY" else "",
        "live-google-test-key" if key_var == "GOOGLE_API_KEY" else "",
    )
    if llm_mode is None:
        monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    else:
        monkeypatch.setenv("AIMO_LLM_MODE", llm_mode)

    assert mock_wanted() is False

    calls: list[tuple[str, str]] = []

    def fake_gemini_generate(feature, model, contents, config=None, run_id="", retries=5, meta=None):
        calls.append((feature, model))
        if feature == "buyer.parse_need":
            payload = {
                "items": [
                    {
                        "product_query": "lactose-free milk",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "excluded_allergens": [],
                        "quantity": 1000,
                        "unit": "L",
                        "delivery_by": "2026-11-16",
                        "delivery_expression": "by Monday",
                        "site": "Hotel Aurora Helsinki",
                        "notes": "",
                    }
                ]
            }
            return _txt(json.dumps(payload))
        if feature in ("buyer.explain", "buyer.summary"):
            return _txt(f"Gemini live {feature} text.")
        done = [
            part.function_response.name
            for item in contents
            for part in (getattr(item, "parts", None) or [])
            if getattr(part, "function_response", None)
        ]
        if "plan_need" not in done:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "product_query": "lactose-free milk",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "quantity": 1000,
                        "unit": "L",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
        if "create_quote" not in done:
            plan = _last_fn_response(contents, "plan_need")
            assert plan is not None
            lines = [
                {"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln["tier"]}
                for ln in plan["lines"]
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            quote = _last_fn_response(contents, "create_quote")
            assert quote is not None
            return _fc("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
        return _txt("Order placed through primary Gemini agent path.")

    monkeypatch.setattr("aimo.gemini.generate", fake_gemini_generate)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop = threading.Event()

    def approve_worker() -> None:
        while not stop.is_set():
            for pending in gw.pending():
                gw.decide(pending["order_id"], "approve", APPROVER, "approved in gemini path test")
                return
            stop.wait(0.02)

    worker = threading.Thread(target=approve_worker, daemon=True)
    worker.start()
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        assert agent.mock is False
        assert not isinstance(agent._generate, MockModel)
        assert agent.use_gemini_parse is True
        assert agent.escalate is True
        assert agent.model == config.GEMINI_AGENT_MODEL
        assert agent.reasoning_model == config.GEMINI_REASONING_MODEL

        res = agent.run(HERO)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["mock"] is False
    assert res["status"] == "placed"
    assert res["need"]["preferences"]["parsed_by"] == "gemini"
    assert MOCK_LABEL not in res["summary"]
    assert agent.escalated is True

    features = [f for f, _ in calls]
    models = {m for _, m in calls}
    assert "buyer.parse_need" in features
    assert "buyer.agent_step" in features
    assert "buyer.explain" in features
    assert "buyer.summary" in features
    assert config.GEMINI_AGENT_MODEL in models
    assert config.GEMINI_REASONING_MODEL in models
