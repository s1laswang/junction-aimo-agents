from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest
from google.genai import types

from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, MockModel, mock_wanted
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport, MCPTransport
from aimo.gateway import service
from aimo.gateway.mcp_server import build_mcp
from aimo.trust.signing import verify_quote_detailed

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
DRIFT_REQ = "Standing order for next week: 4,000 litres of lactose-free milk by Monday."
STOCKOUT_REQ = "We need 1,500 litres of lactose-free milk at Hotel Aurora Helsinki by Monday for a conference."
SCHOOL_REQ = "We need 200 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies."


def _fixture_client_cls():
    try:
        from test_gateway import FixtureClient
        return FixtureClient
    except ImportError:
        from tests.test_gateway import FixtureClient
        return FixtureClient


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
    for c in reversed(contents):
        for p in getattr(c, "parts", None) or []:
            if getattr(p, "function_response", None) and p.function_response.name == name:
                return p.function_response.response
    return None


def _set_api_keys(monkeypatch, gemini_key: str, google_key: str) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


@pytest.fixture
def keyless_env(monkeypatch):
    _set_api_keys(monkeypatch, "", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    gemini.client.cache_clear()

    def forbid_gemini(*args, **kwargs):
        raise AssertionError("aimo.gemini.generate must never be called in keyless mode")

    def forbid_embed(*args, **kwargs):
        raise AssertionError("aimo.gemini.embed must never be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_embed)


def _start_fake_approver(gw: FakeGateway, decision: str = "approve", approver: str = "anna@hotel-aurora.example"):
    stop = threading.Event()
    decided: list[str] = []

    def worker():
        while not stop.is_set():
            for o in gw.pending():
                gw.decide(o["order_id"], decision, approver, f"test-{decision}")
                decided.append(o["order_id"])
                return
            stop.wait(0.02)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return stop, decided, t


@pytest.mark.parametrize("key_var", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
@pytest.mark.parametrize("llm_mode", [None, "auto"])
def test_gemini_main_path_intact_and_routes_to_gemini_generate(monkeypatch, key_var, llm_mode):
    _set_api_keys(
        monkeypatch,
        "fake-live-key" if key_var == "GEMINI_API_KEY" else "",
        "fake-live-key" if key_var == "GOOGLE_API_KEY" else "",
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
            return _txt(f"Live Gemini {feature} response.")
        done = [
            p.function_response.name
            for c in contents
            for p in (getattr(c, "parts", None) or [])
            if getattr(p, "function_response", None)
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
            lines = [
                {"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln["tier"]}
                for ln in plan["lines"]
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            quote = _last_fn_response(contents, "create_quote")
            return _fc("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
        return _txt("Order placed via live Gemini path.")

    monkeypatch.setattr("aimo.gemini.generate", fake_gemini_generate)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        assert agent.mock is False
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
    assert res["quote"] and res["quote"]["quote_id"]
    assert res["order"] and res["order"]["status"] == "placed"
    assert decided == [res["order"]["order_id"]]
    assert agent.escalated is True

    features = [f for f, _ in calls]
    models = {m for _, m in calls}
    assert "buyer.parse_need" in features
    assert "buyer.agent_step" in features
    assert "buyer.explain" in features
    assert "buyer.summary" in features
    assert config.GEMINI_AGENT_MODEL in models
    assert config.GEMINI_REASONING_MODEL in models


def test_live_mode_with_no_key_never_silently_mocks(monkeypatch):
    _set_api_keys(monkeypatch, "", "")
    monkeypatch.setenv("AIMO_LLM_MODE", "live")

    assert mock_wanted() is False

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
    assert agent.mock is False
    assert agent.use_gemini_parse is True
    assert agent.escalate is True
    assert agent.model == config.GEMINI_AGENT_MODEL
    assert not isinstance(agent._generate, MockModel)

    def missing_key_error(*args, **kwargs):
        raise RuntimeError("GEMINI_API_KEY missing in forced live mode")

    monkeypatch.setattr("aimo.gemini.generate", missing_key_error)
    res = agent.run(HERO)
    assert res["mock"] is False
    assert res["status"] == "error"


def test_forced_mock_mode_overrides_present_api_key(monkeypatch):
    _set_api_keys(monkeypatch, "fake-live-key", "fake-live-key")
    monkeypatch.setenv("AIMO_LLM_MODE", "mock")
    monkeypatch.setattr(
        "aimo.gemini.generate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Gemini must not run when AIMO_LLM_MODE=mock")),
    )

    assert mock_wanted() is True

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        assert agent.mock is True
        assert agent.use_gemini_parse is False
        assert agent.escalate is False
        assert agent.model == MOCK_MODEL_NAME
        res = agent.run(HERO)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["mock"] is True
    assert res["status"] == "placed"
    assert decided == [res["order"]["order_id"]]


def test_keyless_scripted_buyer_happy_path_waits_for_approval_and_places(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve", approver="anna@hotel-aurora.example")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        assert agent.mock is True
        assert agent.use_gemini_parse is False
        assert agent.escalate is False
        assert agent.model == MOCK_MODEL_NAME
        assert isinstance(agent._generate, MockModel)

        res = agent.run(HERO)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert res["plan"] and len(res["plan"]["lines"]) >= 1
    assert res["quote"] and res["quote"]["quote_id"] and res["quote"]["quote_hash"]
    kinds = [s["kind"] for s in res["steps"]]
    assert "plan" in kinds
    assert "quote" in kinds
    assert "decision" in kinds
    assert "approval_wait" in kinds
    assert "order" in kinds
    assert "final" in kinds
    assert res["status"] == "placed"
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
    assert decided == [res["order"]["order_id"]]
    assert res["summary"].startswith(MOCK_LABEL)


def test_keyless_scripted_buyer_handles_human_rejection(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="reject", approver="anna@hotel-aurora.example")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        res = agent.run(HERO)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    kinds = [s["kind"] for s in res["steps"]]
    assert "approval_wait" in kinds
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert res["status"] == "rejected"
    assert res["order"]["status"] == "rejected"
    assert decided == [res["order"]["order_id"]]
    assert agent.blocked is True
    assert sum(1 for name, _ in gw.calls if name == "place_order") == 1
    assert "__blocked__" in agent._guard_args("place_order", {"quote_id": "x", "quote_hash": "y"})


def test_keyless_scripted_buyer_holds_standing_order_drift(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
    res = agent.run(DRIFT_REQ)

    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert agent.drift is not None
    assert res["quote"] is not None
    assert res["order"] is None
    assert res["status"] == "needs_confirmation"
    assert not any(name == "place_order" for name, _ in gw.calls)


def test_keyless_scripted_buyer_splits_lines_on_stockout(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="stockout", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve", approver="anna@hotel-aurora.example")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        res = agent.run(STOCKOUT_REQ)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert len(res["plan"]["lines"]) >= 2
    assert len(res["quote"]["lines"]) >= 2
    assert "approval_wait" in [s["kind"] for s in res["steps"]]
    assert res["status"] == "placed"
    assert decided == [res["order"]["order_id"]]


def test_keyless_scripted_buyer_locks_school_excluded_allergens(keyless_env):
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve", approver="liisa@school-tampere.example")
    try:
        agent = BuyerAgent(FakeTransport(gw), "school-tampere", poll_s=0.03, now=SAT)
        res = agent.run(SCHOOL_REQ)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])

    plan_calls = [args["need"] for name, args in gw.calls if name == "plan_need"]
    quote_calls = [args for name, args in gw.calls if name == "create_quote"]
    assert plan_calls and {"milk", "nuts"} <= set(plan_calls[0]["excluded_allergens"])
    assert quote_calls and {"milk", "nuts"} <= set(quote_calls[0]["need"]["excluded_allergens"])

    tampered = agent._guard_args(
        "plan_need",
        {"need": {"customer_id": "hotel-aurora", "product_query": "oat drink", "excluded_allergens": [], "quantity": 1}},
    )
    assert tampered["need"]["customer_id"] == "school-tampere"
    assert {"milk", "nuts"} <= set(tampered["need"]["excluded_allergens"])
    assert tampered["need"]["quantity"] == 200

    quoted_ids = [ln["product_id"] for ln in res["quote"]["lines"]]
    assert quoted_ids and "469040" not in quoted_ids and "471424" not in quoted_ids
    assert res["status"] == "placed"
    assert decided == [res["order"]["order_id"]]

    gw_profile_only = FakeGateway("school-tampere", "school-tampere/buyer-1", scenario="happy", now=SAT)
    stop2, _, worker2 = _start_fake_approver(gw_profile_only, decision="approve", approver="liisa@school-tampere.example")
    try:
        agent2 = BuyerAgent(FakeTransport(gw_profile_only), "school-tampere", poll_s=0.03, now=SAT)
        res2 = agent2.run("We need 100 litres of oat drink for Wednesday's school lunch.")
        worker2.join(timeout=2.0)
    finally:
        stop2.set()
    assert {"milk", "nuts"} <= set(res2["need"]["excluded_allergens"])
    assert res2["status"] == "placed"


def test_keyless_scripted_buyer_multi_item_plan_and_quote(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    stop, decided, worker = _start_fake_approver(gw, decision="approve", approver="anna@hotel-aurora.example")
    try:
        agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT)
        res = agent.run("We need 100 litres of lactose-free milk and 50 litres of oat drink by Monday.")
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert len(res["needs"]) == 2
    assert sum(1 for name, _ in gw.calls if name == "plan_need") == 2
    assert len(res["quote"]["lines"]) == 2
    assert res["status"] == "placed"
    assert decided == [res["order"]["order_id"]]


def _run_fixture_gateway_approval_flow(transport, tmp_path, monkeypatch):
    FixtureClient = _fixture_client_cls()
    db_file = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    monkeypatch.setattr(store, "DB_PATH", db_file)
    monkeypatch.setattr(config, "STATE_DB", db_file)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    bc.set_client(FixtureClient())

    stop = threading.Event()
    approved_orders: list[str] = []
    approver_errors: list[Exception] = []
    decide_fn = getattr(service, "decide_approval", service.decide)

    def approve_worker():
        while not stop.is_set():
            try:
                pending = service.list_approvals("pending", "hotel-aurora")
                for item in pending:
                    order = item["order"]
                    if order["status"] == "pending_approval":
                        decide_fn(
                            order["order_id"],
                            decision="approve",
                            approver="anna@hotel-aurora.example",
                            quote_hash=order["quote_hash"],
                            note="Approved in recorded-fixture test",
                            approver_role="procurement_manager",
                        )
                        approved_orders.append(order["order_id"])
                        return
            except Exception as exc:
                approver_errors.append(exc)
                return
            stop.wait(0.03)

    t = threading.Thread(target=approve_worker, daemon=True)
    t.start()
    try:
        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.05, approval_timeout_s=10.0, now=SAT)
        res = agent.run(HERO)
        t.join(timeout=3.0)
        assert not approver_errors, approver_errors

        assert res["mock"] is True
        assert res["need"]["preferences"]["parsed_by"] == "regex"
        assert res["quote"] and res["quote"]["total_net"] > 500
        stored_quote = service.get_quote(res["quote"]["quote_id"])
        sig_check = verify_quote_detailed(stored_quote)
        assert sig_check["ok"] is True
        assert sig_check["signature_valid"] is True
        assert stored_quote.signature and len(stored_quote.signature.split(".")) == 3

        assert "approval_wait" in [s["kind"] for s in res["steps"]]
        assert res["status"] == "placed"
        assert res["order"]["status"] == "placed"
        assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
        assert approved_orders == [res["order"]["order_id"]]

        trail = service.get_audit_trail(res["order"]["order_id"])
        assert trail["verify"]["ok"] is True
        actions = [e["action"] for e in trail["events"]]
        assert "quote.created" in actions
        assert "policy.evaluated" in actions
        assert "order.placed" in actions
    finally:
        stop.set()
        transport.close()
        bc.set_client(None)


@pytest.mark.skipif(not config.PRODUCT_JSON.exists(), reason="product data not present")
def test_fixture_gateway_inproc_transport_ed25519_approval_and_audit(keyless_env, tmp_path, monkeypatch):
    transport = InProcessTransport("buyer-1", "hotel-aurora", mandate=mandate_for("hotel-aurora"))
    _run_fixture_gateway_approval_flow(transport, tmp_path, monkeypatch)


@pytest.mark.skipif(not config.PRODUCT_JSON.exists(), reason="product data not present")
def test_fixture_gateway_mcp_transport_ed25519_approval_and_audit(keyless_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    transport = MCPTransport(build_mcp(), timeout=30.0)
    _run_fixture_gateway_approval_flow(transport, tmp_path, monkeypatch)
