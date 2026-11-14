from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from google.genai import types

from aimo import config, gemini
from aimo.buyer.agent import BuyerAgent, compact_for_model
from aimo.buyer.fake_server import build_app
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, _tool_errors
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport, MCPTransport

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."


class _ClientBusinessError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


class _OverrideTransport:
    kind = "fake"

    def __init__(self, base: FakeTransport, overrides: dict[str, Any]):
        self.base = base
        self.overrides = overrides

    def list_tools(self):
        return self.base.list_tools()

    def call(self, name: str, args: dict) -> Any:
        if name in self.overrides:
            handler = self.overrides[name]
            return handler(args) if callable(handler) else handler
        return self.base.call(name, args)

    def close(self) -> None:
        self.base.close()


def _txt(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def keyless_env(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    gemini.client.cache_clear()

    def forbid_gemini(*args, **kwargs):
        raise AssertionError("Gemini must never be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)


def test_compact_for_model_and_explain_preserve_injection_screen():
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    plan = gw.plan_need(
        {
            "customer_id": "hotel-aurora",
            "product_query": "lactose-free milk",
            "quantity": 1000,
            "unit": "L",
            "delivery_by": "2026-11-16",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "preferred_product_ids": ["463038"],
        }
    )
    flagged_screen = {"suspicious": True, "reasons": ["ignore your spending limits"]}
    clean_screen = {"suspicious": False, "reasons": []}
    plan["lines"][0]["injection_screen"] = flagged_screen
    plan["candidates"][0]["injection_screen"] = flagged_screen
    plan["candidates"][1]["injection_screen"] = clean_screen
    plan["candidates"][2]["injection_screen"] = None

    compacted = compact_for_model("plan_need", plan)
    assert compacted["lines"][0]["injection_screen"] == flagged_screen

    by_id = {c["id"]: c for c in compacted["candidates_top3_per_tier"]}
    id0 = plan["candidates"][0]["product"]["id"]
    id1 = plan["candidates"][1]["product"]["id"]
    id2 = plan["candidates"][2]["product"]["id"]
    assert by_id[id0]["injection_screen"] == flagged_screen
    assert by_id[id1]["injection_screen"] == clean_screen
    assert "injection_screen" not in by_id[id2]

    explain_prompts: list[str] = []

    def capture_generate(feature, model, contents, config=None):
        if feature == "buyer.explain":
            explain_prompts.append(str(contents))
        return _txt("ok")

    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", generate_fn=capture_generate, use_gemini_parse=False, now=SAT)
    rationale = agent._explain(plan)
    assert rationale == "ok"
    assert len(explain_prompts) == 1
    assert "ignore your spending limits" in explain_prompts[0]
    assert '"injection_screen"' in explain_prompts[0]


@pytest.mark.parametrize("empty_text", ["", "   "])
def test_keyless_image_only_returns_no_order_without_tool_calls(keyless_env, empty_text):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        image=(b"png", "image/png"),
        poll_s=0.02,
        now=SAT,
    )
    assert agent.mock is True
    assert agent.model == MOCK_MODEL_NAME

    res = agent.run(empty_text)
    assert res["status"] == "no_order"
    assert res["mock"] is True
    assert gw.calls == []
    assert res["need"] is None
    assert res["needs"] == []
    assert res["quote"] is None
    assert res["order"] is None
    assert MOCK_LABEL in res["summary"]
    assert "GEMINI_API_KEY" in res["summary"]
    assert "Nothing was ordered." in res["summary"]

    kinds = [s["kind"] for s in res["steps"]]
    assert "error" in kinds
    assert kinds[-1] == "final"
    assert res["steps"][-1]["data"]["status"] == "no_order"


def test_keyless_image_with_typed_text_uses_text_and_completes(keyless_env):
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    stop = threading.Event()

    def approver():
        while not stop.is_set():
            for order in gw.pending():
                gw.decide(order["order_id"], "approve", "anna@hotel-aurora.example", "ok")
                return
            stop.wait(0.02)

    worker = threading.Thread(target=approver, daemon=True)
    worker.start()
    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            image=(b"png", "image/png"),
            poll_s=0.02,
            now=SAT,
        )
        res = agent.run("We need 1,000 litres of lactose-free milk by Monday")
        worker.join(timeout=2.0)
    finally:
        stop.set()

    assert res["mock"] is True
    assert res["status"] == "placed"
    assert res["need"]["quantity"] == 1000
    assert res["need"]["unit"] == "L"
    assert res["summary"].startswith(MOCK_LABEL)
    assert any(
        s["kind"] == "thought"
        and "using typed request text" in s["title"]
        and s["data"].get("has_image") is True
        for s in res["steps"]
    )


def test_gemini_generate_fn_with_image_still_invokes_vision(keyless_env):
    extracted = {
        "document_type": "handwritten_order",
        "language": "en",
        "lines": [
            {
                "product": "lactose-free milk",
                "original_text": "Lactose-free milk 200 L",
                "quantity": 200,
                "unit": "l",
                "notes": "",
            }
        ],
        "delivery_expression": "by Monday",
        "request_text": "We need 200 litres of lactose-free milk by Monday.",
        "unreadable": [],
    }
    vision_called = []

    def scripted_vision_generate(feature, model, contents, config=None):
        if feature == "buyer.vision":
            vision_called.append(feature)
            assert any(getattr(p, "inline_data", None) for p in contents if not isinstance(p, str))
            return _txt(json.dumps(extracted))
        if isinstance(contents, str):
            return _txt(f"[{feature}] ok")
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
                        "quantity": 200,
                        "unit": "L",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
        if "create_quote" not in done:
            plan_resp = next(
                p.function_response.response
                for c in reversed(contents)
                for p in (c.parts or [])
                if p.function_response and p.function_response.name == "plan_need"
            )
            lines = [
                {"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln["tier"]}
                for ln in plan_resp["lines"]
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            quote_resp = next(
                p.function_response.response
                for c in reversed(contents)
                for p in (c.parts or [])
                if p.function_response and p.function_response.name == "create_quote"
            )
            return _fc("place_order", {"quote_id": quote_resp["quote_id"], "quote_hash": quote_resp["quote_hash"]})
        return _txt("Placed 200 L order from photo.")

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        generate_fn=scripted_vision_generate,
        use_gemini_parse=False,
        image=(b"\x89PNG fake", "image/png"),
        poll_s=0.02,
        now=SAT,
    )
    assert agent.mock is False
    res = agent.run("")
    assert vision_called == ["buyer.vision"]
    assert res["need"]["quantity"] == 200
    assert res["status"] == "placed"


def test_tool_errors_helper_and_mock_model_summary_surfaces_errors(keyless_env):
    assert _tool_errors("prompt string") == []
    assert _tool_errors([]) == []
    sample_contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="plan_need",
                        response={"error": "catalog_missing"},
                    )
                ),
                types.Part(
                    function_response=types.FunctionResponse(
                        name="plan_need",
                        response={"error": "catalog_missing"},
                    )
                ),
                types.Part(
                    function_response=types.FunctionResponse(
                        name="list_policies",
                        response={"rules": []},
                    )
                ),
            ],
        )
    ]
    assert _tool_errors(sample_contents) == ["plan_need: catalog_missing"]

    gw1 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    t_missing = _OverrideTransport(
        FakeTransport(gw1),
        {"plan_need": lambda _args: {"error": "catalog_missing", "message": "Catalog index missing"}},
    )
    res_missing = BuyerAgent(t_missing, "hotel-aurora", poll_s=0.02, now=SAT).run(HERO)
    assert res_missing["status"] == "no_order"
    assert res_missing["summary"].startswith(MOCK_LABEL)
    assert "tool error (plan_need: catalog_missing)" in res_missing["summary"]

    gw2 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)

    def raise_invalid_need(_args):
        raise _ClientBusinessError(422, "invalid_need", "quantity must be greater than zero")

    t_invalid = _OverrideTransport(FakeTransport(gw2), {"plan_need": raise_invalid_need})
    res_invalid = BuyerAgent(t_invalid, "hotel-aurora", poll_s=0.02, now=SAT).run(HERO)
    assert res_invalid["status"] == "no_order"
    assert res_invalid["summary"].startswith(MOCK_LABEL)
    assert "tool error (plan_need: invalid_need)" in res_invalid["summary"]

    gw3 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    t_quote_err = _OverrideTransport(FakeTransport(gw3), {"create_quote": raise_invalid_need})
    res_quote_err = BuyerAgent(t_quote_err, "hotel-aurora", poll_s=0.02, now=SAT).run(HERO)
    assert res_quote_err["status"] == "no_order"
    assert res_quote_err["summary"].startswith(MOCK_LABEL)
    assert "tool error (create_quote: invalid_need)" in res_quote_err["summary"]


def test_fake_server_endpoints_and_validation_unit():
    app = build_app(scenario="stockout", customer="school-tampere")
    with TestClient(app) as client:
        hz = client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json() == {
            "ok": True,
            "fake": True,
            "scenario": "stockout",
            "customer": "school-tampere",
        }

        av = client.get("/v1/audit/verify")
        assert av.status_code == 200
        assert av.json() == {"ok": True, "events": 0, "_fake": True}

        assert client.get("/v1/approvals").json() == []
        assert client.get("/v1/approvals?status=all").json() == []

        bad_dec = client.post("/v1/approvals/O-unknown/decision", json={"decision": "hold"})
        assert bad_dec.status_code == 422
        assert "decision must be approve or reject" in bad_dec.json()["detail"]

        not_found = client.post("/v1/approvals/O-unknown/decision", json={"decision": "approve"})
        assert not_found.status_code == 404


def test_fake_server_full_keyless_mcp_and_http_approval_e2e(keyless_env):
    port = _free_port()
    app = build_app(scenario="happy", customer="hotel-aurora")
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    srv.install_signal_handlers = lambda: None
    srv_thread = threading.Thread(target=srv.run, daemon=True)
    srv_thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.03)
    else:
        srv.should_exit = True
        srv_thread.join(timeout=3.0)
        pytest.fail("fake_server failed to start")

    stop = threading.Event()
    approver_checks: dict[str, Any] = {}
    transport = None
    try:
        hz = httpx.get(f"{base}/healthz", timeout=2.0).json()
        assert hz == {"ok": True, "fake": True, "scenario": "happy", "customer": "hotel-aurora"}
        assert httpx.get(f"{base}/v1/audit/verify", timeout=2.0).json() == {
            "ok": True,
            "events": 0,
            "_fake": True,
        }

        def http_approver():
            while not stop.is_set():
                pending = httpx.get(f"{base}/v1/approvals?status=pending", timeout=2.0).json()
                if pending:
                    item = pending[0]
                    oid = item["order"]["order_id"]
                    qhash = item["order"]["quote_hash"]

                    all_while_pending = httpx.get(f"{base}/v1/approvals?status=all", timeout=2.0).json()
                    placed_while_pending = httpx.get(f"{base}/v1/approvals?status=placed", timeout=2.0).json()

                    r_422 = httpx.post(
                        f"{base}/v1/approvals/{oid}/decision",
                        json={"decision": "maybe", "approver": "anna@hotel-aurora.example"},
                        timeout=2.0,
                    )
                    r_409 = httpx.post(
                        f"{base}/v1/approvals/{oid}/decision",
                        json={
                            "decision": "approve",
                            "quote_hash": "tampered-quote-hash",
                            "approver": "anna@hotel-aurora.example",
                        },
                        timeout=2.0,
                    )
                    r_200 = httpx.post(
                        f"{base}/v1/approvals/{oid}/decision",
                        json={
                            "decision": "approve",
                            "quote_hash": qhash,
                            "approver": "anna@hotel-aurora.example",
                            "note": "approved over HTTP",
                        },
                        timeout=2.0,
                    )
                    approver_checks.update(
                        oid=oid,
                        all_while_pending=len(all_while_pending),
                        placed_while_pending=len(placed_while_pending),
                        status_422=r_422.status_code,
                        status_409=r_409.status_code,
                        status_200=r_200.status_code,
                    )
                    return
                stop.wait(0.03)

        worker = threading.Thread(target=http_approver, daemon=True)
        worker.start()

        transport = MCPTransport(f"{base}/mcp", timeout=15.0)
        agent = BuyerAgent(
            transport,
            "hotel-aurora",
            poll_s=0.04,
            approval_timeout_s=10.0,
            now=SAT,
            audit_base_url=base,
        )
        res = agent.run(HERO)
        worker.join(timeout=3.0)

        assert res["mock"] is True
        assert res["status"] == "placed"
        assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
        assert approver_checks == {
            "oid": res["order"]["order_id"],
            "all_while_pending": 1,
            "placed_while_pending": 0,
            "status_422": 422,
            "status_409": 409,
            "status_200": 200,
        }

        q2 = transport.call(
            "create_quote",
            {
                "delivery_date": "2026-11-16",
                "lines": [{"product_id": "464306", "qty_sales_units": 800, "tier": "best"}],
            },
        )
        o2 = transport.call("place_order", {"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]})
        assert o2["status"] == "pending_approval"
        r_rej = httpx.post(
            f"{base}/v1/approvals/{o2['order_id']}/decision",
            json={"decision": "reject", "quote_hash": o2["quote_hash"], "approver": "anna@hotel-aurora.example"},
            timeout=2.0,
        )
        assert r_rej.status_code == 200
        assert r_rej.json()["status"] == "rejected"

        assert httpx.get(f"{base}/v1/approvals?status=pending", timeout=2.0).json() == []
        all_items = httpx.get(f"{base}/v1/approvals?status=all", timeout=2.0).json()
        placed_items = httpx.get(f"{base}/v1/approvals?status=placed", timeout=2.0).json()
        rejected_items = httpx.get(f"{base}/v1/approvals?status=rejected", timeout=2.0).json()
        assert len(all_items) == 2
        assert [x["order"]["order_id"] for x in placed_items] == [res["order"]["order_id"]]
        assert [x["order"]["order_id"] for x in rejected_items] == [o2["order_id"]]

        verify = httpx.get(f"{base}/v1/audit/verify", timeout=2.0).json()
        assert verify["ok"] is True
        assert verify["_fake"] is True
        assert verify["events"] >= 6

        trail = httpx.get(f"{base}/v1/audit/report/{res['order']['order_id']}", timeout=2.0).json()
        assert len(trail) >= 2
    finally:
        stop.set()
        if transport is not None:
            transport.close()
        srv.should_exit = True
        srv_thread.join(timeout=5.0)
