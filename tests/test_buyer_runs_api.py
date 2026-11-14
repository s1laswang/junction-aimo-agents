from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aimo import config
from aimo.buyer import runs
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
SMALL = "We need 20 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."


@pytest.fixture
def keyless_env(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)

    def forbid_gemini(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Gemini must not be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.client", forbid_gemini)
    return calls


@pytest.fixture
def clean_registry(monkeypatch):
    fresh = runs.RunRegistry()
    monkeypatch.setattr(runs, "registry", fresh)
    return fresh


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(runs.router)
    return app


def _approve_soon(gw: FakeGateway, decision: str = "approve") -> None:
    def worker():
        for _ in range(200):
            time.sleep(0.03)
            for order in gw.pending():
                gw.decide(order["order_id"], decision, "anna@aurora", "approved in test")
                return

    threading.Thread(target=worker, daemon=True).start()


def _read_sse_events(resp) -> list[dict]:
    events: list[dict] = []
    cur: dict[str, str] = {}
    for raw in resp.iter_lines():
        line = raw.strip("\r")
        if not line:
            if "data" in cur:
                events.append({
                    "id": cur.get("id"),
                    "event": cur.get("event"),
                    "data": json.loads(cur["data"]),
                })
            cur = {}
            continue
        if line.startswith(":"):
            continue
        field, _, val = line.partition(":")
        cur[field] = val.lstrip(" ")
    if "data" in cur:
        events.append({
            "id": cur.get("id"),
            "event": cur.get("event"),
            "data": json.loads(cur["data"]),
        })
    return events


def test_keyless_ui_run_streams_steps_and_sets_mock(keyless_env, clean_registry, monkeypatch):
    gw = FakeGateway()
    _approve_soon(gw)
    orig_start = clean_registry.start

    def fast_fake_start(req, **kw):
        return orig_start(
            req,
            transport_factory=lambda _r: FakeTransport(gw),
            agent_kwargs={"poll_s": 0.05, "now": SAT},
            mcp_url=kw.get("mcp_url"),
        )

    monkeypatch.setattr(clean_registry, "start", fast_fake_start)
    client = TestClient(_build_app())

    created = client.post(
        "/v1/agent/runs",
        json={
            "text": HERO,
            "customer_id": "hotel-aurora",
            "model": "gemini-3.8-flash",
            "thinking": "MEDIUM",
        },
    )
    assert created.status_code == 200
    payload = created.json()
    run_id = payload["run_id"]
    assert run_id.startswith("run-")
    assert payload["events_url"] == f"/v1/agent/runs/{run_id}/events"

    with client.stream("GET", payload["events_url"]) as stream_resp:
        assert stream_resp.status_code == 200
        events = _read_sse_events(stream_resp)

    kinds = [e["event"] for e in events]
    for expected_kind in (
        "thought",
        "tool_call",
        "tool_result",
        "plan",
        "quote",
        "decision",
        "approval_wait",
        "order",
        "final",
    ):
        assert expected_kind in kinds, expected_kind
    assert kinds[-1] == "final"
    assert [e["id"] for e in events] == [str(i) for i in range(1, len(events) + 1)]

    first_step = events[0]["data"]
    assert first_step["data"]["mock"] is True
    assert first_step["data"]["model"] == MOCK_MODEL_NAME
    assert any(MOCK_LABEL in e["data"]["title"] for e in events)

    final_step = events[-1]["data"]
    assert final_step["data"]["status"] == "placed"
    assert final_step["data"]["summary"].startswith(MOCK_LABEL)
    assert {m["model"] for m in final_step["data"]["models_used"]} == {MOCK_MODEL_NAME}

    detail = client.get(f"/v1/agent/runs/{run_id}")
    assert detail.status_code == 200
    view = detail.json()
    assert view["run_id"] == run_id
    assert view["status"] == "finished"
    assert view["mock"] is True
    assert view["order_status"] == "placed"
    assert view["summary"].startswith(MOCK_LABEL)
    assert view["n_steps"] == len(events) == len(view["steps"])

    listed = client.get("/v1/agent/runs")
    assert listed.status_code == 200
    items = listed.json()
    matching = [r for r in items if r["run_id"] == run_id]
    assert len(matching) == 1
    assert matching[0]["mock"] is True
    assert matching[0]["order_status"] == "placed"
    assert matching[0]["status"] == "finished"
    assert "steps" not in matching[0]

    assert keyless_env == []


def test_sse_last_event_id_resume_and_404_unknown_run(keyless_env, clean_registry, monkeypatch):
    client = TestClient(_build_app())

    missing_get = client.get("/v1/agent/runs/run-doesnotexist")
    assert missing_get.status_code == 404
    assert "run-doesnotexist" in missing_get.json()["detail"]

    missing_sse = client.get("/v1/agent/runs/run-doesnotexist/events")
    assert missing_sse.status_code == 404
    assert "run-doesnotexist" in missing_sse.json()["detail"]

    gw = FakeGateway()
    orig_start = clean_registry.start

    def fast_fake_start(req, **kw):
        return orig_start(
            req,
            transport_factory=lambda _r: FakeTransport(gw),
            agent_kwargs={"poll_s": 0.05, "now": SAT},
            mcp_url=kw.get("mcp_url"),
        )

    monkeypatch.setattr(clean_registry, "start", fast_fake_start)
    run_id = client.post("/v1/agent/runs", json={"text": SMALL, "customer_id": "hotel-aurora"}).json()["run_id"]

    with client.stream("GET", f"/v1/agent/runs/{run_id}/events") as full_resp:
        full_events = _read_sse_events(full_resp)

    assert len(full_events) >= 6
    cutoff = 3
    with client.stream(
        "GET",
        f"/v1/agent/runs/{run_id}/events",
        headers={"Last-Event-ID": str(cutoff)},
    ) as resumed_resp:
        assert resumed_resp.status_code == 200
        resumed = _read_sse_events(resumed_resp)

    assert [e["id"] for e in resumed] == [str(i) for i in range(cutoff + 1, len(full_events) + 1)]
    assert resumed == full_events[cutoff:]
    assert resumed[-1]["event"] == "final"

    with client.stream(
        "GET",
        f"/v1/agent/runs/{run_id}/events",
        headers={"Last-Event-ID": str(len(full_events))},
    ) as done_resp:
        assert done_resp.status_code == 200
        assert _read_sse_events(done_resp) == []


def test_dynamic_base_url_propagates_to_mcp_url(keyless_env, clean_registry, monkeypatch):
    seen_mcp_urls: list[str] = []

    def spy_connect(kind: str, **kwargs):
        seen_mcp_urls.append(kwargs["mcp_url"])
        return FakeTransport(FakeGateway())

    monkeypatch.setattr("aimo.buyer.tools.connect", spy_connect)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)

    client = TestClient(_build_app(), base_url="http://gateway.internal:9456")
    resp = client.post(
        "/v1/agent/runs",
        json={"text": SMALL, "customer_id": "hotel-aurora", "model": "gemini-3.8-flash"},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    with client.stream("GET", f"/v1/agent/runs/{run_id}/events") as stream_resp:
        events = _read_sse_events(stream_resp)
    assert events[-1]["event"] == "final"

    view = client.get(f"/v1/agent/runs/{run_id}").json()
    assert view["mock"] is True
    assert view["order_status"] == "placed"
    assert seen_mcp_urls == ["http://gateway.internal:9456/mcp"]

    monkeypatch.setenv("AIMO_MCP_URL", "http://custom-mcp.internal:7788/mcp")
    resp2 = client.post("/v1/agent/runs", json={"text": SMALL, "customer_id": "hotel-aurora"})
    run_id2 = resp2.json()["run_id"]
    with client.stream("GET", f"/v1/agent/runs/{run_id2}/events") as stream_resp2:
        events2 = _read_sse_events(stream_resp2)
    assert events2[-1]["event"] == "final"
    assert seen_mcp_urls[-1] == "http://custom-mcp.internal:7788/mcp"

    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    direct_run = clean_registry.start(runs.RunRequest(text=SMALL, customer_id="hotel-aurora"))
    deadline = time.time() + 5.0
    while not direct_run.done() and time.time() < deadline:
        time.sleep(0.02)
    assert direct_run.done()
    assert direct_run.view()["mock"] is True
    assert direct_run.view()["order_status"] == "placed"
    assert seen_mcp_urls[-1] == "http://localhost:8000/mcp"
    assert keyless_env == []


def test_non_mock_run_preserves_requested_model(clean_registry, monkeypatch):
    from google.genai import types

    monkeypatch.setattr(config, "GEMINI_API_KEY", "x")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    seen_models: list[str] = []

    def fake_generate(feature, model, contents, config=None):
        seen_models.append(model)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="No order needed.")])
                )
            ]
        )

    orig_start = clean_registry.start

    def start_with_gen(req, **kw):
        return orig_start(
            req,
            transport_factory=lambda _r: FakeTransport(FakeGateway()),
            agent_kwargs={"generate_fn": fake_generate, "use_gemini_parse": False, "now": SAT},
            mcp_url=kw.get("mcp_url"),
        )

    monkeypatch.setattr(clean_registry, "start", start_with_gen)
    client = TestClient(_build_app())
    resp = client.post(
        "/v1/agent/runs",
        json={"text": SMALL, "customer_id": "hotel-aurora", "model": "gemini-3.8-flash"},
    )
    run_id = resp.json()["run_id"]
    with client.stream("GET", f"/v1/agent/runs/{run_id}/events") as stream_resp:
        events = _read_sse_events(stream_resp)
    assert events[-1]["event"] == "final"
    assert events[0]["data"]["data"]["model"] == "gemini-3.8-flash"
    assert events[0]["data"]["data"]["mock"] is False
    view = client.get(f"/v1/agent/runs/{run_id}").json()
    assert view["mock"] is False
    assert "gemini-3.8-flash" in seen_models

