"""Full /v1 auth surface audit, run answer customer auth matrix, MCP HTTP auth, and live conformance."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import types
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import runs as buyer_runs
from aimo.gateway import human_auth, service
from aimo.gateway.app import create_app
from aimo.models import Need, Plan, PlanLine, Product, ProductSummary

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
DELIVERY_DAY = date(2026, 11, 16)
TOY_APPROVER_PIN = "conformance-pin-2026"

H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_ROGUE = {"X-Customer-Id": "rogue-corp", "X-Agent-Id": "buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}

CUSTOMER_MUTATING_ROUTES = {
    ("POST", "/v1/offers"),
    ("POST", "/v1/plan"),
    ("POST", "/v1/quotes"),
    ("POST", "/v1/orders"),
    ("POST", "/v1/recipes/plan"),
    ("POST", "/v1/proposals/{proposal_id}/accept"),
    ("POST", "/v1/proposals/{proposal_id}/decline"),
    ("POST", "/v1/agent/runs"),
    ("POST", "/v1/agent/runs/{run_id}/answer"),
}

SELLER_MUTATING_ROUTES = {
    ("POST", "/v1/proposals"),
}

HUMAN_MUTATING_ROUTES = {
    ("POST", "/v1/approvals/{order_id}/decision"),
    ("POST", "/v1/auth/approver-session"),
    ("POST", "/v1/demo/tamper/{seq}"),
    ("POST", "/v1/demo/untamper/{seq}"),
    ("POST", "/v1/voice/prompt"),
    ("POST", "/v1/voice/transcribe"),
    ("POST", "/v1/voice/intent"),
}


class FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self):
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))

    def _sel(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: Any, customer: str | None = None) -> dict[str, Any]:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: Any, customer: str | None = None) -> dict[str, Any]:
        return self._sel(self.avail_raw, ids)


def _stub_plan(need: Need) -> Plan:
    line = PlanLine(
        product_id="464306",
        product_name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        tier="best",
        qty_sales_units=need.quantity,
        sales_unit="ST",
        covered_amount=need.quantity,
        unit_price=1.01,
        line_total=round(need.quantity * 1.01, 2),
        reasons=["stubbed for auth surface test"],
        flags=[],
    )
    return Plan(
        need=need,
        lines=[line],
        candidates=[],
        covered_amount=need.quantity,
        shortfall=0.0,
        total_net=line.line_total,
        explanation="stub plan",
    )


def _stub_catalog() -> Any:
    prod = Product(
        id="464306",
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )
    summary = ProductSummary(
        id="464306",
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )
    return types.SimpleNamespace(
        search=lambda q, **kw: [summary],
        get_product=lambda pid: prod if str(pid) == "464306" else None,
        plan_need=_stub_plan,
    )


@pytest.fixture
def isolated_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    for var in (
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
    ):
        monkeypatch.delenv(var, raising=False)
    human_auth.reset_for_tests()
    bc.set_client(FixtureClient())
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    reg = buyer_runs.RunRegistry()
    monkeypatch.setattr(reg, "_execute", lambda run, *a, **kw: setattr(run, "status", "finished"))
    monkeypatch.setattr(buyer_runs, "registry", reg)
    app = create_app(with_mcp=False)
    yield app, TestClient(app), reg
    bc.set_client(None)


def _assert_err_json(resp, expected_status: int, expected_error: str | None = None) -> dict[str, Any]:
    assert resp.status_code == expected_status, (resp.status_code, resp.text)
    body = resp.json()
    assert isinstance(body, dict), body
    assert isinstance(body.get("error"), str) and body["error"], body
    assert isinstance(body.get("message"), str) and body["message"], body
    if expected_error is not None:
        assert body["error"] == expected_error, body
    return body


def test_every_mutating_v1_route_covered_and_enforced(isolated_gateway):
    app, client, _ = isolated_gateway
    paths = app.openapi()["paths"]
    discovered_mutating = {
        (method.upper(), path)
        for path, methods in paths.items()
        if path.startswith("/v1")
        for method in methods
        if method.lower() in {"post", "put", "patch", "delete"}
    }

    assert CUSTOMER_MUTATING_ROUTES.isdisjoint(SELLER_MUTATING_ROUTES)
    assert CUSTOMER_MUTATING_ROUTES.isdisjoint(HUMAN_MUTATING_ROUTES)
    assert SELLER_MUTATING_ROUTES.isdisjoint(HUMAN_MUTATING_ROUTES)
    covered = CUSTOMER_MUTATING_ROUTES | SELLER_MUTATING_ROUTES | HUMAN_MUTATING_ROUTES
    assert discovered_mutating == covered, {
        "uncovered": sorted(discovered_mutating - covered),
        "missing_from_openapi": sorted(covered - discovered_mutating),
    }

    d_iso = DELIVERY_DAY.isoformat()
    base_need = {
        "product_query": "lactose-free milk",
        "quantity": 10,
        "unit": "L",
        "delivery_by": d_iso,
    }
    customer_calls: list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]] = [
        (
            ("POST", "/v1/offers"),
            {"path": "/v1/offers", "json": {"product_ids": ["464306"], "requested_date": d_iso}},
            {"path": "/v1/offers", "json": {"product_ids": ["464306"], "requested_date": d_iso}},
        ),
        (
            ("POST", "/v1/plan"),
            {"path": "/v1/plan", "json": {**base_need, "customer_id": ""}},
            {"path": "/v1/plan", "json": {**base_need, "customer_id": "rogue-corp"}},
        ),
        (
            ("POST", "/v1/quotes"),
            {
                "path": "/v1/quotes",
                "json": {"delivery_date": d_iso, "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
            },
            {
                "path": "/v1/quotes",
                "json": {"delivery_date": d_iso, "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
            },
        ),
        (
            ("POST", "/v1/orders"),
            {"path": "/v1/orders", "json": {"quote_id": "Q-missing", "quote_hash": "0" * 64}},
            {"path": "/v1/orders", "json": {"quote_id": "Q-missing", "quote_hash": "0" * 64}},
        ),
        (
            ("POST", "/v1/recipes/plan"),
            {"path": "/v1/recipes/plan", "json": {"recipe_id": "lasagne", "portions": 20, "delivery_by": d_iso}},
            {"path": "/v1/recipes/plan", "json": {"recipe_id": "lasagne", "portions": 20, "delivery_by": d_iso}},
        ),
        (
            ("POST", "/v1/proposals/{proposal_id}/accept"),
            {"path": "/v1/proposals/P-missing/accept"},
            {"path": "/v1/proposals/P-missing/accept"},
        ),
        (
            ("POST", "/v1/proposals/{proposal_id}/decline"),
            {"path": "/v1/proposals/P-missing/decline", "json": {"reason": "no"}},
            {"path": "/v1/proposals/P-missing/decline", "json": {"reason": "no"}},
        ),
        (
            ("POST", "/v1/agent/runs"),
            {"path": "/v1/agent/runs", "json": {"text": "10 L lactose-free milk by Monday"}},
            {"path": "/v1/agent/runs", "json": {"text": "10 L lactose-free milk by Monday"}},
        ),
        (
            ("POST", "/v1/agent/runs/{run_id}/answer"),
            {"path": "/v1/agent/runs/run-missing/answer", "json": {"option_id": "d0"}},
            {"path": "/v1/agent/runs/run-missing/answer", "json": {"option_id": "d0"}},
        ),
    ]
    assert {spec_key for spec_key, _, _ in customer_calls} == CUSTOMER_MUTATING_ROUTES

    extra_customer_routes: list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]] = [
        (
            ("GET", "/v1/products/search"),
            {"path": "/v1/products/search", "params": {"q": "milk"}},
            {"path": "/v1/products/search", "params": {"q": "milk"}},
        ),
        (
            ("GET", "/v1/drift/{customer_id}"),
            {"path": "/v1/drift/hotel-aurora", "params": {"rebaseline": "true"}},
            {"path": "/v1/drift/hotel-aurora", "params": {"rebaseline": "true"}},
        ),
    ]

    for (method, template), anon_kw, rogue_kw in customer_calls + extra_customer_routes:
        r_anon = client.request(method, anon_kw["path"], **{k: v for k, v in anon_kw.items() if k != "path"})
        _assert_err_json(r_anon, 401, "identity_required")

        r_rogue = client.request(
            method,
            rogue_kw["path"],
            headers=H_ROGUE,
            **{k: v for k, v in rogue_kw.items() if k != "path"},
        )
        _assert_err_json(r_rogue, 403, "unknown_customer")

    _assert_err_json(
        client.post("/v1/proposals", json={"customer_id": "hotel-aurora"}),
        401,
        "identity_required",
    )
    _assert_err_json(
        client.post("/v1/proposals", headers=H_AURORA, json={"customer_id": "hotel-aurora"}),
        403,
        "seller_required",
    )
    _assert_err_json(
        client.post("/v1/proposals", headers=H_SELLER, json={"customer_id": "rogue-corp"}),
        403,
        "unknown_customer",
    )

    human_calls: list[tuple[tuple[str, str], dict[str, Any], bool]] = [
        (
            ("POST", "/v1/approvals/{order_id}/decision"),
            {
                "path": "/v1/approvals/O-missing/decision",
                "json": {
                    "decision": "approve",
                    "approver": "anna@hotel-aurora.example",
                    "quote_hash": "0" * 64,
                },
            },
            True,
        ),
        (
            ("POST", "/v1/auth/approver-session"),
            {"path": "/v1/auth/approver-session", "json": {"approver": "anna@hotel-aurora.example"}},
            True,
        ),
        (
            ("POST", "/v1/demo/tamper/{seq}"),
            {"path": "/v1/demo/tamper/1"},
            False,
        ),
        (
            ("POST", "/v1/demo/untamper/{seq}"),
            {"path": "/v1/demo/untamper/1"},
            False,
        ),
        (
            ("POST", "/v1/voice/prompt"),
            {"path": "/v1/voice/prompt", "json": {"order_id": "O-missing"}},
            True,
        ),
        (
            ("POST", "/v1/voice/transcribe"),
            {"path": "/v1/voice/transcribe", "files": {"audio": ("sample.webm", b"00", "audio/webm")}},
            True,
        ),
        (
            ("POST", "/v1/voice/intent"),
            {"path": "/v1/voice/intent", "json": {"transcript": "yes approve it", "order_id": "O-missing"}},
            True,
        ),
    ]
    assert {spec_key for spec_key, _, _ in human_calls} == HUMAN_MUTATING_ROUTES

    for (method, _template), kw, must_forbid_agent in human_calls:
        req_kw = {k: v for k, v in kw.items() if k != "path"}
        r_anon = client.request(method, kw["path"], **req_kw)
        _assert_err_json(r_anon, 401)

        r_agent = client.request(method, kw["path"], headers={"X-Agent-Id": "buyer-1"}, **req_kw)
        if must_forbid_agent:
            _assert_err_json(r_agent, 403, "agent_cannot_approve")
        else:
            assert r_agent.status_code in (401, 403), (kw["path"], r_agent.status_code, r_agent.text)
            body = r_agent.json()
            assert isinstance(body.get("error"), str) and isinstance(body.get("message"), str)


def test_discovery_and_docs_open_without_headers(isolated_gateway):
    _, client, _ = isolated_gateway
    for open_path in (
        "/.well-known/otap.json",
        "/.well-known/jwks.json",
        "/healthz",
        "/openapi.json",
        "/docs",
        "/ui/",
    ):
        resp = client.get(open_path)
        assert resp.status_code == 200, (open_path, resp.status_code, resp.text[:200])


def test_agent_run_answer_pending_question_auth_matrix(isolated_gateway, monkeypatch):
    _, client, reg = isolated_gateway
    assert getattr(reg.start, "__func__", None) is buyer_runs.RunRegistry.start

    question = {
        "question_id": "q-delivery-date",
        "type": "date",
        "prompt": "Which delivery day?",
        "options": [
            {"id": "d0", "label": "Monday 2026-11-16"},
            {"id": "d1", "label": "Tuesday 2026-11-17"},
        ],
    }

    def pause_for_answer(run: buyer_runs.Run, *args: Any, **kwargs: Any) -> None:
        run.status = "running"
        ans = run.wait_answer(question)
        run.result = {"status": "placed" if ans else "needs_clarification", "answer": ans}
        run.status = "finished"
        with run.cond:
            run.cond.notify_all()

    monkeypatch.setattr(reg, "_execute", pause_for_answer)

    started = client.post(
        "/v1/agent/runs",
        headers={"X-Customer-Id": "hotel-aurora"},
        json={"text": "We need 100 litres of lactose-free milk next week."},
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    run = reg.get(run_id)

    deadline = time.time() + 3.0
    while run.pending_question is None and time.time() < deadline:
        time.sleep(0.01)
    assert run.status == "waiting_for_answer" and run.pending_question == question

    url = f"/v1/agent/runs/{run_id}/answer"

    _assert_err_json(client.post(url, json={"option_id": "d0"}), 401, "identity_required")
    _assert_err_json(
        client.post(url, headers={"X-Customer-Id": "rogue-corp"}, json={"option_id": "d0"}),
        403,
        "unknown_customer",
    )
    _assert_err_json(
        client.post(url, json={"option_id": "d0", "customer_id": "rogue-corp"}),
        403,
        "unknown_customer",
    )
    _assert_err_json(
        client.post(url, headers={"X-Customer-Id": "school-tampere"}, json={"option_id": "d0"}),
        403,
        "customer_mismatch",
    )
    _assert_err_json(
        client.post(url, json={"option_id": "d0", "customer_id": "school-tampere"}),
        403,
        "customer_mismatch",
    )
    _assert_err_json(
        client.post(
            url,
            headers={"X-Customer-Id": "hotel-aurora"},
            json={"option_id": "d0", "customer_id": "school-tampere"},
        ),
        403,
        "customer_mismatch",
    )

    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"hotel-aurora": "aurora-secret-key"}))
    _assert_err_json(
        client.post(url, headers={"X-Customer-Id": "hotel-aurora"}, json={"option_id": "d0"}),
        401,
        "agent_key_required",
    )
    _assert_err_json(
        client.post(
            url,
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong-key"},
            json={"option_id": "d0"},
        ),
        403,
        "invalid_agent_key",
    )

    valid_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "aurora-secret-key"}
    _assert_err_json(
        client.post("/v1/agent/runs/run-nonexistent/answer", headers=valid_headers, json={"option_id": "d0"}),
        404,
        "run_not_found",
    )
    _assert_err_json(
        client.post(url, headers=valid_headers, json={"option_id": "d0", "question_id": "wrong-q"}),
        409,
        "question_mismatch",
    )
    _assert_err_json(
        client.post(url, headers=valid_headers, json={"customer_id": "hotel-aurora"}),
        422,
        "answer_required",
    )
    _assert_err_json(
        client.post(url, headers=valid_headers, json={"option_id": "not-an-option"}),
        422,
        "unknown_option",
    )

    ok = client.post(
        url,
        headers=valid_headers,
        json={"option_id": "d0", "question_id": "q-delivery-date", "customer_id": "hotel-aurora"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "run_id": run_id, "question_id": "q-delivery-date"}

    done_deadline = time.time() + 3.0
    while not run.done() and time.time() < done_deadline:
        time.sleep(0.01)
    assert run.done()
    assert run.result == {"status": "placed", "answer": {"option_id": "d0", "question_id": "q-delivery-date"}}

    _assert_err_json(
        client.post(url, headers=valid_headers, json={"option_id": "d0"}),
        409,
        "not_waiting_for_answer",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_toy_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("toy_auth_surface")
    port = _free_port()
    log_path = data_dir / "toy_server.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(data_dir),
        "AIMO_APPROVER_PIN": TOY_APPROVER_PIN,
        "GEMINI_API_KEY": "",
    }
    env.pop("AIMO_AGENT_KEYS", None)
    env.pop("AIMO_AGENT_KEY", None)
    env.pop("AIMO_CUSTOMER_ID", None)
    env.pop("AIMO_AGENT_ID", None)
    with open(log_path, "w+", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "toywholesaler.app:app", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=log_file,
            stderr=log_file,
            cwd=ROOT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(120):
                try:
                    if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.2)
            else:
                log_file.seek(0)
                proc.kill()
                pytest.fail("toywholesaler failed to start:\n" + log_file.read()[-2000:])
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_mcp_streamable_http_auth_surface(live_toy_server):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    mcp_url = f"{live_toy_server}/mcp"

    async def mcp_call(headers: dict[str, str], tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        http = create_mcp_http_client(headers=headers)
        async with http:
            async with Client(streamable_http_client(mcp_url, http_client=http)) as c:
                try:
                    res = await c.call_tool(tool_name, args)
                except Exception as exc:
                    return True, str(exc)
                is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                txt = json.dumps(sc) if sc else " ".join(getattr(x, "text", "") for x in (res.content or []))
                return is_err, txt

    async def mcp_list(headers: dict[str, str]) -> list[str]:
        http = create_mcp_http_client(headers=headers)
        async with http:
            async with Client(streamable_http_client(mcp_url, http_client=http)) as c:
                return [t.name for t in (await c.list_tools()).tools]

    tool_names = asyncio.run(mcp_list(H_AURORA))
    assert len(tool_names) == 13
    assert not [n for n in tool_names if "approv" in n or "decid" in n]

    ok_err, ok_txt = asyncio.run(mcp_call(H_AURORA, "search_products", {"query": "rye bread"}))
    assert not ok_err and "NB1001" in ok_txt

    for tool_name, tool_args in [
        ("search_products", {"query": "rye bread"}),
        ("create_quote", {"delivery_date": "2026-12-02", "lines": [{"product_id": "NB1001", "qty_sales_units": 1}]}),
        ("place_order", {"quote_id": "Q-1", "quote_hash": "0" * 64}),
    ]:
        is_err, txt = asyncio.run(mcp_call(H_ROGUE, tool_name, tool_args))
        assert is_err and "unknown_customer" in txt, (tool_name, is_err, txt)

    for strict_tool, strict_args in [
        ("create_quote", {"delivery_date": "2026-12-02", "lines": [{"product_id": "NB1001", "qty_sales_units": 1}]}),
        ("place_order", {"quote_id": "Q-1", "quote_hash": "0" * 64}),
    ]:
        is_err, txt = asyncio.run(mcp_call({}, strict_tool, strict_args))
        assert is_err and "identity_required" in txt, (strict_tool, is_err, txt)


def test_toywholesaler_conformance_44_and_permissions_attack(live_toy_server, monkeypatch):
    from conformance.__main__ import default_day, run

    assert len(TOY_APPROVER_PIN) >= 12
    monkeypatch.setenv("AIMO_APPROVER_PIN", TOY_APPROVER_PIN)

    rep = run(live_toy_server, query="rye bread", quiet=True)
    failures = [r for r in rep["results"] if r["status"] != "PASS"]
    assert not failures, failures
    assert rep["summary"]["fail"] == 0
    assert rep["summary"]["skip"] == 0
    assert rep["summary"]["pass"] == 44
    assert rep["summary"]["total"] == 44

    day = default_day().isoformat()
    q_resp = httpx.post(
        f"{live_toy_server}/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": day, "lines": [{"product_id": "NB1001", "qty_sales_units": 150}]},
        timeout=15.0,
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()

    o_resp = httpx.post(
        f"{live_toy_server}/v1/orders",
        headers=H_AURORA,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        timeout=15.0,
    )
    assert o_resp.status_code == 200, o_resp.text
    assert o_resp.json()["status"] == "pending_approval", o_resp.json()

    attack = subprocess.run(
        [
            sys.executable,
            str(ROOT / "docs" / "pitch" / "permissions_attack.py"),
            "--base",
            live_toy_server,
            "--customer",
            "hotel-aurora",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert attack.returncode == 0, attack.stdout + "\n" + attack.stderr
    assert "PERMISSIONS ATTACK REFUSED AND AUDITED" in attack.stdout
