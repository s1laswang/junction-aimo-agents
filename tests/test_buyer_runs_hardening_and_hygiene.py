from __future__ import annotations

import ast
import base64
from datetime import datetime
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import time
import tokenize
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import URL

from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import runs
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport
from aimo.gateway import human_auth
from aimo.gateway.app import create_app

ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = ROOT / "aimo" / "buyer" / "runs.py"
THIS_PATH = Path(__file__).resolve()
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
SMALL = "We need 20 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."


@pytest.fixture(autouse=True)
def keyless_offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_live_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live Gemini calls are forbidden in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_live_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_live_gemini)


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> runs.RunRegistry:
    fresh = runs.RunRegistry()
    monkeypatch.setattr(runs, "registry", fresh)
    return fresh


def _standalone_app() -> FastAPI:
    app = FastAPI()
    app.include_router(runs.router)
    return app


def _read_sse_events(resp: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur: dict[str, str] = {}
    for raw in resp.iter_lines():
        line = raw.strip("\r")
        if not line:
            if "data" in cur:
                events.append(
                    {
                        "id": cur.get("id"),
                        "event": cur.get("event"),
                        "data": json.loads(cur["data"]),
                    }
                )
            cur = {}
            continue
        if line.startswith(":"):
            continue
        field, _, val = line.partition(":")
        cur[field] = val.lstrip(" ")
    if "data" in cur:
        events.append(
            {
                "id": cur.get("id"),
                "event": cur.get("event"),
                "data": json.loads(cur["data"]),
            }
        )
    return events


def _load_isolated_runs_module(mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, RUNS_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(mod_name, None)
    return mod


@pytest.mark.parametrize("target", [RUNS_PATH, THIS_PATH], ids=lambda p: p.name)
def test_hygiene_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    ast.parse(src, filename=str(target))
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has comment tokens: {comments[:3]}"
    assert chr(35) not in src, f"{target.name} contains hash character"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


def test_mcp_url_for_precedence_and_bad_input_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    assert runs.mcp_url_for() == "http://localhost:8000/mcp"
    assert runs.mcp_url_for(None) == "http://localhost:8000/mcp"
    assert runs.mcp_url_for("") == "http://localhost:8000/mcp"
    assert runs.mcp_url_for("   ") == "http://localhost:8000/mcp"
    assert runs.mcp_url_for("///") == "http://localhost:8000/mcp"
    for bad in (123, True, False, ["http://x"], {"url": "http://x"}):
        assert runs.mcp_url_for(bad) == "http://localhost:8000/mcp"  

    monkeypatch.setenv("AIMO_SERVE_URL", "  http://127.0.0.1:8765///  ")
    assert runs.mcp_url_for() == "http://127.0.0.1:8765/mcp"
    assert runs.mcp_url_for("   ") == "http://127.0.0.1:8765/mcp"
    assert runs.mcp_url_for("/") == "http://127.0.0.1:8765/mcp"

    assert runs.mcp_url_for("http://gateway.internal:9456/") == "http://gateway.internal:9456/mcp"
    assert runs.mcp_url_for("  http://gateway.internal:9456/// ") == "http://gateway.internal:9456/mcp"
    assert runs.mcp_url_for(URL("http://starlette.internal:9001/")) == "http://starlette.internal:9001/mcp"

    monkeypatch.setenv("AIMO_MCP_URL", "   ")
    assert runs.mcp_url_for("http://gateway.internal:9456/") == "http://gateway.internal:9456/mcp"

    monkeypatch.setenv("AIMO_MCP_URL", "  http://override.internal:7788/mcp  ")
    assert runs.mcp_url_for("http://gateway.internal:9456/") == "http://override.internal:7788/mcp"
    assert runs.mcp_url_for() == "http://override.internal:7788/mcp"

    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    monkeypatch.setenv("AIMO_SERVE_URL", "///")
    assert runs.mcp_url_for() == "http://localhost:8000/mcp"


def test_mcp_url_for_used_in_start_execute_and_create_run(
    clean_registry: runs.RunRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_mcp_urls: list[str | None] = []

    def spy_connect(kind: str, **kwargs: Any) -> FakeTransport:
        seen_mcp_urls.append(kwargs.get("mcp_url"))
        return FakeTransport(FakeGateway())

    monkeypatch.setattr("aimo.buyer.tools.connect", spy_connect)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    monkeypatch.setenv("AIMO_SERVE_URL", "http://127.0.0.1:8910/")

    direct_run = clean_registry.start(runs.RunRequest(text=SMALL, customer_id="hotel-aurora"))
    deadline = time.time() + 5.0
    while not direct_run.done() and time.time() < deadline:
        time.sleep(0.02)
    assert direct_run.done()
    assert seen_mcp_urls[-1] == "http://127.0.0.1:8910/mcp"

    manual_run = runs.Run("run-manualexec", runs.RunRequest(text=SMALL, customer_id="hotel-aurora"))
    monkeypatch.setenv("AIMO_SERVE_URL", "http://127.0.0.1:8911")
    clean_registry._execute(manual_run, None, {"now": SAT}, mcp_url="   ")
    assert manual_run.done()
    assert seen_mcp_urls[-1] == "http://127.0.0.1:8911/mcp"

    client = TestClient(_standalone_app(), base_url="http://api.host.local:9222")
    created = client.post("/v1/agent/runs", json={"text": SMALL, "customer_id": "hotel-aurora"})
    assert created.status_code == 200
    rid = created.json()["run_id"]
    r_obj = clean_registry.get(rid)
    deadline = time.time() + 5.0
    while not r_obj.done() and time.time() < deadline:
        time.sleep(0.02)
    assert r_obj.done()
    assert seen_mcp_urls[-1] == "http://api.host.local:9222/mcp"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 8),
        ("", 8),
        ("   ", 8),
        ("abc", 8),
        ("3.5", 8),
        ("nan", 8),
        ("inf", 8),
        (True, 8),
        (False, 8),
        ("0", 1),
        ("-1", 1),
        ("-500", 1),
        (0, 1),
        (-9, 1),
        ("1", 1),
        (" 14 ", 14),
        (24, 24),
    ],
)
def test_parse_max_runs_hardening(raw: Any, expected: int) -> None:
    assert runs._parse_max_runs(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 900.0),
        ("", 900.0),
        ("   ", 900.0),
        ("abc", 900.0),
        ("0", 900.0),
        ("0.0", 900.0),
        ("-0.0", 900.0),
        ("-1", 900.0),
        ("-0.001", 900.0),
        ("nan", 900.0),
        ("NaN", 900.0),
        ("inf", 900.0),
        ("-inf", 900.0),
        ("Infinity", 900.0),
        ("1e500", 900.0),
        (True, 900.0),
        (False, 900.0),
        ("0.05", 0.05),
        (" 42.5 ", 42.5),
        (120, 120.0),
    ],
)
def test_parse_answer_timeout_hardening(raw: Any, expected: float) -> None:
    res = runs._parse_answer_timeout(raw)
    assert math.isfinite(res) and res > 0.0
    assert res == expected


@pytest.mark.parametrize(
    "bad_max,bad_timeout,exp_max,exp_timeout",
    [
        ("not-an-int", "not-a-float", 8, 900.0),
        ("", "", 8, 900.0),
        ("   ", "   ", 8, 900.0),
        ("0", "0", 1, 900.0),
        ("-99", "-15.5", 1, 900.0),
        ("3", "nan", 3, 900.0),
        ("4", "inf", 4, 900.0),
        ("5", "-inf", 5, 900.0),
        ("6", "12.5", 6, 12.5),
    ],
)
def test_fresh_import_never_crashes_on_malformed_env(
    monkeypatch: pytest.MonkeyPatch,
    bad_max: str,
    bad_timeout: str,
    exp_max: int,
    exp_timeout: float,
) -> None:
    monkeypatch.setenv("AIMO_BUYER_MAX_RUNS", bad_max)
    monkeypatch.setenv("AIMO_BUYER_ANSWER_TIMEOUT", bad_timeout)
    mod = _load_isolated_runs_module(f"aimo_buyer_runs_iso_{ abs(hash((bad_max, bad_timeout))) }")
    assert mod.MAX_RUNS == exp_max
    assert mod.ANSWER_TIMEOUT_S == exp_timeout
    assert mod._sem.acquire(blocking=False) is True
    mod._sem.release()


def test_wait_answer_handles_fast_timeout_and_corrupted_module_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    r = runs.Run("run-timeout", runs.RunRequest(text=SMALL, customer_id="hotel-aurora"))
    r.status = "running"
    monkeypatch.setattr(runs, "ANSWER_TIMEOUT_S", 0.02)
    q = {"question_id": "q1", "options": [{"id": "d0", "label": "Monday"}]}
    assert r.wait_answer(q) is None
    assert r.status == "running"
    assert r.pending_question is None

    for bad_val in (-5.0, 0.0, float("nan"), float("inf"), True, "bad"):
        monkeypatch.setattr(runs, "ANSWER_TIMEOUT_S", bad_val)
        r.answers.put({"option_id": "d0"})
        got = r.wait_answer(q)
        assert got == {"option_id": "d0"}
        assert r.status == "running"
        assert r.pending_question is None


@pytest.mark.parametrize("bad_limit", ["0", "-1", "-50", "201", "500", "999999", "abc", ""])
def test_list_runs_query_bounds_reject_out_of_range_with_422(
    clean_registry: runs.RunRegistry,
    bad_limit: str,
) -> None:
    client = TestClient(_standalone_app())
    resp = client.get("/v1/agent/runs", params={"limit": bad_limit})
    assert resp.status_code == 422, (bad_limit, resp.status_code, resp.text)


def test_list_runs_valid_bounds_sorting_and_direct_call(clean_registry: runs.RunRegistry) -> None:
    for idx in range(5):
        r = runs.Run(f"run-item-{idx}", runs.RunRequest(text=f"order {idx}", customer_id="hotel-aurora"))
        r.created = 1000.0 + idx
        r.status = "finished"
        r.result = {"status": "placed", "summary": f"done {idx}", "mock": True}
        clean_registry.runs[r.run_id] = r

    assert len(runs.list_runs()) == 5
    assert [x["run_id"] for x in runs.list_runs(2)] == ["run-item-4", "run-item-3"]

    client = TestClient(_standalone_app())
    r_default = client.get("/v1/agent/runs")
    assert r_default.status_code == 200
    assert [x["run_id"] for x in r_default.json()] == [f"run-item-{i}" for i in range(4, -1, -1)]
    assert all("steps" not in x for x in r_default.json())

    r_one = client.get("/v1/agent/runs", params={"limit": 1})
    assert r_one.status_code == 200
    assert [x["run_id"] for x in r_one.json()] == ["run-item-4"]

    r_max = client.get("/v1/agent/runs", params={"limit": 200})
    assert r_max.status_code == 200
    assert len(r_max.json()) == 5


def test_safe_run_id_preserves_normal_and_strips_controls_and_caps_at_80() -> None:
    assert runs._safe_run_id("run-doesnotexist") == "run-doesnotexist"
    assert runs._safe_run_id("run-nonexistent") == "run-nonexistent"
    assert runs._safe_run_id("run-a1b2c3d4e5") == "run-a1b2c3d4e5"
    assert runs._safe_run_id("run-\x00evil\r\n\t\x1b\x7f\x85\x9f\ud800id") == "run-evilid"
    long_id = "run-" + ("z" * 150)
    sanitized = runs._safe_run_id(long_id)
    assert len(sanitized) == 80
    assert sanitized == long_id[:80]
    assert runs._safe_run_id("\x00\r\n\x1b\x7f") == ""
    assert runs._safe_run_id(None) == ""
    assert runs._safe_run_id(12345) == ""


def test_get_run_and_events_422_on_null_or_overlong_and_sanitized_404(
    clean_registry: runs.RunRegistry,
) -> None:
    client = TestClient(_standalone_app())

    for path_suffix in ("", "/events"):
        ok_404 = client.get(f"/v1/agent/runs/run-doesnotexist{path_suffix}")
        assert ok_404.status_code == 404
        assert ok_404.json()["detail"] == "unknown run run-doesnotexist"

        hundred_id = "r" * 100
        capped_404 = client.get(f"/v1/agent/runs/{hundred_id}{path_suffix}")
        assert capped_404.status_code == 404
        detail = capped_404.json()["detail"]
        assert detail == "unknown run " + ("r" * 80)

        ctrl_404 = client.get(f"/v1/agent/runs/run-%09%1bevil{path_suffix}")
        assert ctrl_404.status_code == 404
        assert "\t" not in ctrl_404.json()["detail"]
        assert "\x1b" not in ctrl_404.json()["detail"]
        assert ctrl_404.json()["detail"] == "unknown run run-evil"

        null_422 = client.get(f"/v1/agent/runs/run-%00evil{path_suffix}")
        assert null_422.status_code == 422
        assert null_422.json()["detail"] == "invalid run_id"

        over_128 = "r" * 129
        long_422 = client.get(f"/v1/agent/runs/{over_128}{path_suffix}")
        assert long_422.status_code == 422
        assert long_422.json()["detail"] == "invalid run_id"

        ws_422 = client.get(f"/v1/agent/runs/%20%20{path_suffix}")
        assert ws_422.status_code == 422
        assert ws_422.json()["detail"] == "invalid run_id"

        at_128 = "r" * 128
        at_128_resp = client.get(f"/v1/agent/runs/{at_128}{path_suffix}")
        assert at_128_resp.status_code == 404


def test_answer_run_checks_auth_before_422_invalid_run_id_on_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    human_auth.reset_for_tests()
    bc.set_client(None)
    reg = runs.RunRegistry()
    monkeypatch.setattr(reg, "_execute", lambda run, *a, **kw: setattr(run, "status", "finished"))
    monkeypatch.setattr(runs, "registry", reg)

    app = create_app(with_mcp=False)
    client = TestClient(app)

    bad_urls = [
        "/v1/agent/runs/run-%00evil/answer",
        f"/v1/agent/runs/{'x' * 129}/answer",
        f"/v1/agent/runs/{'x' * 400}/answer",
        "/v1/agent/runs/%20%20/answer",
    ]
    for bad_url in bad_urls:
        r_anon = client.post(bad_url, json={"option_id": "d0"})
        assert r_anon.status_code == 401, (bad_url, r_anon.status_code, r_anon.text)
        assert r_anon.json()["error"] == "identity_required"

        r_rogue = client.post(
            bad_url,
            headers={"X-Customer-Id": "rogue-corp"},
            json={"option_id": "d0"},
        )
        assert r_rogue.status_code == 403, (bad_url, r_rogue.status_code, r_rogue.text)
        assert r_rogue.json()["error"] == "unknown_customer"

        r_mismatch = client.post(
            bad_url,
            headers={"X-Customer-Id": "hotel-aurora"},
            json={"option_id": "d0", "customer_id": "school-tampere"},
        )
        assert r_mismatch.status_code == 403
        assert r_mismatch.json()["error"] == "customer_mismatch"

        r_auth = client.post(
            bad_url,
            headers={"X-Customer-Id": "hotel-aurora"},
            json={"option_id": "d0"},
        )
        assert r_auth.status_code == 422, (bad_url, r_auth.status_code, r_auth.text)
        assert r_auth.json()["error"] == "invalid_run_id"

    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"hotel-aurora": "secret-123"}))
    for bad_url in bad_urls:
        r_no_key = client.post(
            bad_url,
            headers={"X-Customer-Id": "hotel-aurora"},
            json={"option_id": "d0"},
        )
        assert r_no_key.status_code == 401
        assert r_no_key.json()["error"] == "agent_key_required"

        r_bad_key = client.post(
            bad_url,
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong"},
            json={"option_id": "d0"},
        )
        assert r_bad_key.status_code == 403
        assert r_bad_key.json()["error"] == "invalid_agent_key"

        r_ok_key = client.post(
            bad_url,
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "secret-123"},
            json={"option_id": "d0"},
        )
        assert r_ok_key.status_code == 422
        assert r_ok_key.json()["error"] == "invalid_run_id"

    r_capped_404 = client.post(
        f"/v1/agent/runs/{'z' * 110}/answer",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "secret-123"},
        json={"option_id": "d0"},
    )
    assert r_capped_404.status_code == 404
    assert r_capped_404.json()["error"] == "run_not_found"
    assert r_capped_404.json()["message"] == "unknown run " + ("z" * 80)

    r_bad_limit = client.get("/v1/agent/runs", params={"limit": 0})
    assert r_bad_limit.status_code == 422
    assert r_bad_limit.json().get("error") and r_bad_limit.json().get("message")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 0),
        ("", 0),
        ("   ", 0),
        ("\t\r\n", 0),
        ("0", 0),
        ("3", 3),
        ("  3  ", 3),
        ("\t 12 \r\n", 12),
        ("0007", 7),
        ("-1", 0),
        ("-99", 0),
        ("+3", 0),
        ("1.5", 0),
        ("abc", 0),
        ("3a", 0),
        ("12\x003", 0),
        ("\xb2", 0),
        ("\xb3", 0),
        ("\u2460", 0),
        ("\u0663", 0),
        ("\xa03\xa0", 0),
        ("9" * 5000, 0),
        (5, 0),
    ],
)
def test_parse_last_event_id_unit_matrix(raw: Any, expected: int) -> None:
    assert runs._parse_last_event_id(raw) == expected


def test_sse_last_event_id_whitespace_and_hostile_headers(
    clean_registry: runs.RunRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeGateway()
    orig_start = clean_registry.start

    def fast_fake_start(req: runs.RunRequest, **kw: Any) -> runs.Run:
        return orig_start(
            req,
            transport_factory=lambda _r: FakeTransport(gw),
            agent_kwargs={"poll_s": 0.05, "now": SAT},
            mcp_url=kw.get("mcp_url"),
        )

    monkeypatch.setattr(clean_registry, "start", fast_fake_start)
    client = TestClient(_standalone_app())
    run_id = client.post("/v1/agent/runs", json={"text": SMALL, "customer_id": "hotel-aurora"}).json()["run_id"]

    with client.stream("GET", f"/v1/agent/runs/{run_id}/events") as full_resp:
        full_events = _read_sse_events(full_resp)
    assert len(full_events) >= 6

    with client.stream(
        "GET",
        f"/v1/agent/runs/{run_id}/events",
        headers={"Last-Event-ID": "  2 \t "},
    ) as ws_resp:
        assert ws_resp.status_code == 200
        ws_events = _read_sse_events(ws_resp)
    assert ws_events == full_events[2:]

    for bad_hdr in (b"-1", b"-999", b"abc", b"\xb2", b"   ", b"9" * 5000):
        with client.stream(
            "GET",
            f"/v1/agent/runs/{run_id}/events",
            headers=[(b"last-event-id", bad_hdr)],
        ) as replay_resp:
            assert replay_resp.status_code == 200
            assert _read_sse_events(replay_resp) == full_events


def test_answer_run_rejects_whitespace_only_text_and_resumes_on_real_text(
    clean_registry: runs.RunRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = {
        "question_id": "q-date",
        "type": "date",
        "prompt": "Which delivery day?",
        "options": [
            {"id": "d0", "label": "Monday 2026-11-16"},
            {"id": "d1", "label": "Tuesday 2026-11-17"},
        ],
    }

    def pause_exec(run: runs.Run, *args: Any, **kwargs: Any) -> None:
        run.status = "running"
        ans = run.wait_answer(question)
        run.result = {"status": "placed" if ans else "needs_clarification", "answer": ans, "mock": True}
        run.status = "finished"
        with run.cond:
            run.cond.notify_all()

    monkeypatch.setattr(clean_registry, "_execute", pause_exec)
    client = TestClient(_standalone_app())

    started = client.post(
        "/v1/agent/runs",
        headers={"X-Customer-Id": "hotel-aurora"},
        json={"text": "We need 100 L lactose-free milk next week."},
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    run = clean_registry.get(run_id)

    deadline = time.time() + 3.0
    while run.pending_question is None and time.time() < deadline:
        time.sleep(0.01)
    assert run.status == "waiting_for_answer"

    url = f"/v1/agent/runs/{run_id}/answer"
    hdrs = {"X-Customer-Id": "hotel-aurora"}

    for empty_payload in (
        {},
        {"text": ""},
        {"text": "   "},
        {"text": "\t\r\n  "},
        {"option_id": "", "text": ""},
        {"option_id": "", "text": "   "},
        {"option_id": "   ", "text": ""},
        {"option_id": "   ", "text": "   "},
    ):
        resp = client.post(url, headers=hdrs, json=empty_payload)
        assert resp.status_code == 422, (empty_payload, resp.status_code, resp.text)
        assert resp.json()["error"] == "answer_required"
        assert run.status == "waiting_for_answer"

    bad_opt = client.post(url, headers=hdrs, json={"option_id": "bad\x00opt\r\n" + ("x" * 120)})
    assert bad_opt.status_code == 422
    assert bad_opt.json()["error"] == "unknown_option"
    assert "\x00" not in bad_opt.json()["message"]
    assert len(bad_opt.json()["message"]) <= len("unknown option ") + 80

    ok = client.post(url, headers=hdrs, json={"text": "  Monday 2026-11-16  "})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "run_id": run_id, "question_id": "q-date"}

    done_deadline = time.time() + 3.0
    while not run.done() and time.time() < done_deadline:
        time.sleep(0.01)
    assert run.done()
    assert run.result == {
        "status": "placed",
        "answer": {"text": "  Monday 2026-11-16  "},
        "mock": True,
    }


def test_create_run_bad_inputs_and_execute_error_recovery(
    clean_registry: runs.RunRegistry,
) -> None:
    client = TestClient(_standalone_app())

    assert client.post("/v1/agent/runs", content=b"   ").json()["error"] == "invalid_body"
    assert client.post("/v1/agent/runs", content=b"{bad").json()["error"] == "invalid_json"
    assert client.post("/v1/agent/runs", content=b"[]").json()["error"] == "invalid_body"

    deep = b'{"a":' * 5000 + b"1" + b"}" * 5000
    r_deep = client.post("/v1/agent/runs", content=deep, headers={"Content-Type": "application/json"})
    assert r_deep.status_code == 422
    assert r_deep.json()["error"] in ("invalid_json", "validation_error")

    r_b64 = client.post(
        "/v1/agent/runs",
        json={"text": "10 L milk", "customer_id": "hotel-aurora", "image_b64": "not!valid!base64"},
    )
    assert r_b64.status_code == 422
    assert r_b64.json()["error"] == "invalid_image"

    r_empty = client.post("/v1/agent/runs", json={"text": "   ", "customer_id": "hotel-aurora"})
    assert r_empty.status_code == 422
    assert r_empty.json()["error"] == "text_required"

    huge_b64 = base64.b64encode(b"a" * 100).decode("ascii") * 300_000
    r_huge = client.post(
        "/v1/agent/runs",
        json={"text": "10 L milk", "customer_id": "hotel-aurora", "image_b64": huge_b64},
    )
    assert r_huge.status_code == 413
    assert r_huge.json()["error"] == "image_too_large"

    r_surrogate = client.post(
        "/v1/agent/runs",
        content=b'{"text":"10 L milk","customer_id":"rogue-\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert r_surrogate.status_code == 403
    assert r_surrogate.json()["error"] == "unknown_customer"

    class BrokenCloseTransport:
        def list_tools(self) -> list[Any]:
            raise RuntimeError("boom\ud800transport")

        def close(self) -> None:
            raise RuntimeError("close failed")

    err_run = clean_registry.start(
        runs.RunRequest(text=SMALL, customer_id="hotel-aurora"),
        transport_factory=lambda _r: BrokenCloseTransport(),
    )
    deadline = time.time() + 3.0
    while not err_run.done() and time.time() < deadline:
        time.sleep(0.02)
    assert err_run.status == "error"
    view = err_run.view()
    assert view["steps"][-1]["kind"] == "final"
    assert view["steps"][-1]["data"]["status"] == "error"
