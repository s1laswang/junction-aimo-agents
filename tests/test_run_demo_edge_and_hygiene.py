from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import json
import math
import sys
import threading
import tokenize
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from aimo import config, gemini
from aimo.buyer import run_demo
from aimo.buyer.need import HELSINKI
from aimo.models import AgentStep

ROOT = Path(__file__).resolve().parents[1]
ANNA = "anna@hotel-aurora.example"


@pytest.fixture(autouse=True)
def _keyless_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def _forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini network calls are forbidden in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", _forbid)
    monkeypatch.setattr("aimo.gemini.embed", _forbid)
    yield


def test_hygiene_zero_comments_and_zero_em_en_dashes() -> None:
    targets = [
        ROOT / "aimo" / "buyer" / "run_demo.py",
        Path(__file__).resolve(),
    ]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert comments == [], f"{path.name} contains comment tokens: {comments}"
        assert "\u2014" not in src, f"{path.name} contains em dash"
        assert "\u2013" not in src, f"{path.name} contains en dash"
    self_src = Path(__file__).resolve().read_text(encoding="utf-8")
    assert "\x23" not in self_src


def test_print_step_malformed_and_partial_step_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)

    class _BadDumpObj:
        def model_dump(self, mode: str = "json") -> Any:
            return "not-a-dict"

    class _GoodDumpObj:
        def model_dump(self, mode: str = "json") -> dict[str, Any]:
            return {"kind": None, "title": None, "data": "non-dict", "ts": None}

    samples: list[Any] = [
        None,
        "raw-string-step",
        42,
        {},
        {"kind": "", "title": "", "data": None},
        {"kind": None, "title": None, "data": ["list", "not", "dict"]},
        {"kind": 99, "title": 123, "data": 456, "ts": 789},
        _BadDumpObj(),
        _GoodDumpObj(),
        AgentStep(
            run_id="r1",
            seq=1,
            ts=datetime(2026, 11, 15, 6, 0, tzinfo=HELSINKI),
            kind="thought",
            title="Real Pydantic Step",
            data={},
        ),
    ]
    for item in samples:
        run_demo.print_step(item)

    out = capsys.readouterr().out
    assert "?             " in out
    assert "99            123" in out
    assert "Real Pydantic Step" in out


@pytest.mark.parametrize(
    "bad_qty",
    [None, True, False, float("nan"), float("inf"), float("-inf"), "ten", [5], {"v": 1}],
)
def test_print_step_thought_need_guards_bad_quantities_and_missing_keys(
    bad_qty: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    step = {
        "kind": "thought",
        "title": "Parsed need",
        "data": {"need": {"quantity": bad_qty}},
    }
    run_demo.print_step(step)
    out = capsys.readouterr().out
    flat = " ".join(out.split())
    assert "product_query='' quantity=? delivery_by=None claims=[] excluded_allergens=[] preferred=[]" in flat
    assert "quantity=1 " not in flat
    assert "quantity=0 " not in flat
    assert "nan" not in flat.lower()


def test_print_step_thought_non_dict_need_and_valid_need_and_extracted_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    run_demo.print_step({"kind": "thought", "title": "Null need", "data": {"need": None}})
    run_demo.print_step({"kind": "thought", "title": "Str need", "data": {"need": "lactose-free milk"}})
    run_demo.print_step(
        {
            "kind": "thought",
            "title": "Valid need",
            "data": {
                "need": {
                    "product_query": "oat drink",
                    "quantity": 200.0,
                    "unit": "L",
                    "delivery_by": "2026-11-18",
                    "required_claims": ["VEGAN"],
                    "excluded_allergens": ["milk", "nuts"],
                    "preferred_product_ids": ["472833"],
                },
                "parsed_by": "regex",
                "latency_s": 0.01,
            },
        }
    )
    run_demo.print_step(
        {
            "kind": "thought",
            "title": "Extracted from image",
            "data": {
                "lines": [
                    "not-a-dict",
                    {"product": "butter", "quantity": True},
                    {"original_text": "40 L cream", "product": "cream", "quantity": 40, "unit": "L"},
                ],
                "extracted": True,
                "request_text": "40 L cream",
            },
        }
    )
    run_demo.print_step(
        {
            "kind": "thought",
            "title": "Non-list lines",
            "data": {"lines": 123, "extracted": True},
        }
    )

    out = capsys.readouterr().out
    flat = " ".join(out.split())
    assert (
        "product_query='oat drink' quantity=200 L delivery_by=2026-11-18 "
        "claims=['VEGAN'] excluded_allergens=['milk', 'nuts'] preferred=['472833'] "
        "(parsed by regex, 0.01 s)"
    ) in flat
    assert '"butter" -> butter ?' in out
    assert '"40 L cream" -> cream 40 L' in out
    assert "Request: 40 L cream" in out


def test_print_step_plan_guards_non_dict_plan_lines_candidates_and_null_product(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)

    for bad_plan in (None, "not-a-dict", [1, 2], 42):
        run_demo.print_step({"kind": "plan", "title": "Bad plan", "data": {"plan": bad_plan}})

    step = {
        "kind": "plan",
        "title": "Plan edge cases",
        "data": {
            "plan": {
                "lines": [
                    None,
                    "bad-line",
                    {
                        "tier": None,
                        "product_id": "464306",
                        "qty_sales_units": True,
                        "sales_unit": None,
                        "unit_price": 1.01,
                        "line_total": 10.1,
                        "product_name": "Bool Qty Milk",
                        "flags": 999,
                        "injection_screen": {"suspicious": True, "reasons": 12345},
                    },
                    {
                        "tier": "better",
                        "product_id": "469363",
                        "qty_sales_units": float("nan"),
                        "sales_unit": "ST",
                        "unit_price": 1.15,
                        "line_total": 11.5,
                        "product_name": "NaN Qty Milk",
                        "injection_screen": {"suspicious": True, "reasons": "role override"},
                    },
                ],
                "candidates": [
                    None,
                    "bad-cand",
                    {"tier": "rejected", "product": None, "reasons": None},
                    {"tier": "rejected", "product": "463038", "reasons": 123},
                    {"tier": "rejected", "reasons": "cut-off passed"},
                    {"tier": "rejected", "product": {"id": "472833"}, "reasons": [None, "allergen", 7]},
                ],
                "explanation": "Warning: the request contains instructions aimed at AI agents (role override).",
            },
            "rationale": 12345,
        },
    }
    run_demo.print_step(step)
    out = capsys.readouterr().out
    assert "464306        ? x     @ 1.01 = EUR 10.1  Bool Qty Milk  [injection_screen: FLAGGED]" in out
    assert "469363        ? x ST  @ 1.15 = EUR 11.5  NaN Qty Milk  [injection_screen: FLAGGED (role override)]" in out
    assert "rejected  None  " in out
    assert "rejected  None  cut-off passed" in out
    assert "rejected  472833  allergen; 7" in out
    assert "Warning: the request contains instructions aimed at AI agents" in out
    assert "Why: 12345" in out


def test_print_step_needs_clarification_decision_and_final_guards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)

    run_demo.print_step({"kind": "needs_clarification", "title": "Q1", "data": {"options": None}})
    run_demo.print_step(
        {
            "kind": "needs_clarification",
            "title": "Q2",
            "data": {
                "options": [
                    None,
                    "not-a-dict",
                    {},
                    {"id": None, "label": None},
                    {"id": "d0", "label": "Mon 16 Nov"},
                ]
            },
        }
    )

    run_demo.print_step({"kind": "decision", "title": "D1", "data": {"decision": "allow"}})
    run_demo.print_step(
        {
            "kind": "decision",
            "title": "D2",
            "data": {
                "decision": {
                    "hits": [
                        None,
                        "bad-hit",
                        {"outcome": "reject", "rule_id": "allergens", "message": "contains milk"},
                    ]
                }
            },
        }
    )

    run_demo.print_step({"kind": "final", "title": "F1", "data": {"summary": None, "models_used": "bad"}})
    run_demo.print_step(
        {
            "kind": "final",
            "title": "F2",
            "data": {
                "summary": "Done.",
                "status": "placed",
                "latency_s": 0.2,
                "escalated": False,
                "models_used": [
                    None,
                    "not-a-dict",
                    {},
                    {"model": "gemini-3.8-flash"},
                    {"thinking": "HIGH"},
                    {"model": None, "thinking": ""},
                    {"model": "mock-replay (no Gemini)", "thinking": "NONE"},
                ],
            },
        }
    )

    out = capsys.readouterr().out
    assert "[?] " in out
    assert "[d0] Mon 16 Nov" in out
    assert "[reject] allergens: contains milk" in out
    assert "models: ?/?, gemini-3.8-flash/?, ?/HIGH, ?/?, mock-replay (no Gemini)/NONE" in out


@pytest.mark.parametrize(
    "bad_num",
    [True, False, float("nan"), float("inf"), float("-inf"), "bad", [1]],
)
def test_scenario_verdict_guards_non_finite_bool_and_non_numeric_values(bad_num: Any) -> None:
    r = {
        "status": "placed",
        "quote": {
            "total_net": bad_num,
            "lines": [
                {"product_id": "464306", "qty_sales_units": bad_num},
                {"product_id": "464201", "qty_sales_units": 400},
            ],
        },
        "plan": {"shortfall": bad_num, "candidates": []},
        "order": {"approved_by": ANNA},
    }
    v = run_demo.scenario_verdict("stockout", r)
    assert "2 line(s), shortfall ?" in v
    assert "EUR" not in v
    assert "partial stock split across 464306 x?, 464201 x400" in v
    assert "nan" not in v.lower()
    assert "inf" not in v.lower()


def test_scenario_verdict_guards_pending_approval_null_data_and_allergen_probe_types() -> None:
    assert run_demo.scenario_verdict("happy", None) == "?"
    assert run_demo.scenario_verdict("happy", {}) == "?"

    for steps_val in (
        None,
        ["not-a-dict", {"kind": "approval_wait", "data": None}],
        [{"kind": "approval_wait", "data": "not-a-dict"}],
        [{"kind": "approval_wait"}],
    ):
        v_wait = run_demo.scenario_verdict(
            "happy",
            {"status": "pending_approval", "steps": steps_val},
        )
        assert v_wait == "pending_approval; waiting for a human (approver); nothing ordered"

    v_role = run_demo.scenario_verdict(
        "happy",
        {
            "status": "pending_approval",
            "steps": [{"kind": "approval_wait", "data": {"approver_role": "head_chef"}}],
        },
    )
    assert v_role == "pending_approval; waiting for a human (head_chef); nothing ordered"

    for bad_probe in (True, "failed", 123, ["x"], {}):
        v_bad_pr = run_demo.scenario_verdict(
            "school-allergen",
            {"status": "placed", "allergen_probe": bad_probe},
        )
        assert "allergen probe" not in v_bad_pr

    for non_str_reason in (None, "", 123, ["milk"], {"err": "milk"}):
        v_pr_nostr = run_demo.scenario_verdict(
            "school-allergen",
            {
                "status": "placed",
                "allergen_probe": {"status": "rejected", "reason": non_str_reason},
            },
        )
        assert "allergen probe (464306, cow's milk): rejected" in v_pr_nostr
        assert "by policy" not in v_pr_nostr

    v_pr_str = run_demo.scenario_verdict(
        "school-allergen",
        {
            "status": "placed",
            "allergen_probe": {
                "status": "rejected",
                "reason": "'Valio Eila' (464306) contains milk, excluded for school",
            },
        },
    )
    assert "allergen probe (464306, cow's milk): rejected by policy (contains milk, excluded for school)" in v_pr_str

    v_malformed_nested = run_demo.scenario_verdict(
        "stockout",
        {
            "status": "placed",
            "need": {"preferred_product_ids": ["463038"], "excluded_allergens": ["milk"]},
            "quote": {"lines": [{"product_id": "464306", "qty_sales_units": None}], "total_net": 100.0},
            "plan": {
                "shortfall": None,
                "candidates": [
                    None,
                    {"tier": "rejected", "product": None, "reasons": None},
                    {"tier": "rejected", "product": "463038", "reasons": ["cut-off"]},
                    {"tier": "rejected", "product": {"id": "463038"}, "reasons": ["order cut-off passed"]},
                ],
            },
            "steps": [
                {"kind": "decision", "data": {"decision": {"hits": [None, {"rule_id": None, "message": None}]}}}
            ],
        },
    )
    assert "1 line(s), EUR 100.00, shortfall 0" in v_malformed_nested
    assert "cut-off substitution: usual 463038 -> 464306" in v_malformed_nested
    assert "excluded milk; allergen rule: not evaluated" in v_malformed_nested
    assert math.isfinite(100.0)


def test_write_montage_opens_file_with_explicit_utf8_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "sub" / "montage_utf8.md"
    real_open = builtins.open
    opened_encodings: list[str | None] = []

    def _spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(str(file)) == out_path and "w" in mode:
            opened_encodings.append(kwargs.get("encoding"))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy_open)

    rows = [
        (
            "happy",
            {
                "status": "placed",
                "customer_id": "hotel-aurora",
                "transport": "fake",
                "latency_s": 0.1,
                "run_id": "run-utf8",
                "order": {"approved_by": "äiti.älä@käytä-ääkkösiä.fi"},
            },
            True,
            "placed; 1 line(s), EUR 1,010.00; Ravintola Äänekoski €",
            {"calls": 0, "in": 0, "cached": 0, "out": 0, "thinking": 0, "cost_usd": 0.0},
        )
    ]
    tot = {
        "calls": 0,
        "in": 0,
        "cached": 0,
        "out": 0,
        "thinking": 0,
        "gemini_s": 0.0,
        "cost_usd": 0.0,
        "escalated_runs": 0,
        "escalated_steps": 0,
        "agent_steps": 0,
        "runs": 1,
    }
    args = types.SimpleNamespace(
        model=None,
        no_escalate=False,
        base_url="http://localhost:8000",
        thinking="LOW",
        baseline_pid=None,
        baseline_orders=5,
        baseline_label=None,
    )
    started = datetime(2026, 11, 15, 6, 0, tzinfo=HELSINKI)
    run_demo._write_montage(str(out_path), started, args, rows, tot, 0.1, 1, 1, None)

    assert opened_encodings == ["utf-8"]
    text = out_path.read_bytes().decode("utf-8")
    assert "Ravintola Äänekoski €" in text
    assert "äiti.älä@käytä-ääkkösiä.fi" in text


@pytest.mark.parametrize(
    "bad_run_payload",
    [
        {},
        {"ok": True},
        {"run_id": None},
        {"run_id": ""},
        {"run_id": "   "},
        {"run_id": True},
        {"run_id": False},
        ["not", "a", "dict"],
        ValueError("invalid json"),
    ],
)
def test_via_server_missing_or_invalid_run_id_returns_1_without_streaming(
    bad_run_payload: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    stream_called = False

    class _PostResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            if isinstance(bad_run_payload, Exception):
                raise bad_run_payload
            return bad_run_payload

    @contextlib.contextmanager
    def _unexpected_stream(*args: Any, **kwargs: Any):
        nonlocal stream_called
        stream_called = True
        yield None

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _PostResp())
    monkeypatch.setattr(httpx, "stream", _unexpected_stream)

    args = argparse.Namespace(
        scenario="happy",
        thinking="LOW",
        no_escalate=False,
        model=None,
        transport="auto",
        faults=None,
        no_auto_approve=True,
        auto_approve_as=None,
        approve_delay=0.01,
        answer="auto",
    )
    stop = threading.Event()
    rc = run_demo._via_server(args, "http://gw", "100 L milk", "hotel-aurora", stop, "approve")
    out = capsys.readouterr().out
    assert rc == 1
    assert stop.is_set()
    assert stream_called is False
    assert "missing run_id" in out


def test_via_server_ignores_malformed_and_non_dict_sse_frames_and_completes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    answer_posts: list[dict[str, Any]] = []

    class _PostResp:
        def __init__(self, payload: dict[str, Any]):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _StreamCtx:
        def __init__(self, lines: list[Any]):
            self._lines = lines

        def __enter__(self) -> _StreamCtx:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            return iter(self._lines)

    sse_lines: list[Any] = [
        "",
        ": keepalive",
        "event: step",
        "data:",
        "data:    ",
        "data: {broken json",
        "data: [DONE]",
        "data: null",
        "data: 123",
        'data: ["list-frame"]',
        'data: "string-frame"',
        "data: {}",
        json.dumps({"not": "sse-prefixed"}),
        "data: " + json.dumps({"kind": "needs_clarification", "title": "Bad data", "data": "not-a-dict"}),
        "data: " + json.dumps({"kind": "needs_clarification", "title": "Bad opts", "data": {"options": ["bad", {}]}}),
        "data: "
        + json.dumps(
            {
                "kind": "needs_clarification",
                "title": "Good opts",
                "data": {"question_id": "q-ok", "options": [{"id": "opt-1", "label": "Monday"}]},
            }
        ),
        "data: "
        + json.dumps(
            {
                "kind": "plan",
                "title": "Plan",
                "data": {
                    "plan": {
                        "lines": [],
                        "candidates": [{"tier": "rejected", "product": None, "reasons": ["allergen"]}],
                    }
                },
            }
        ),
        "data: "
        + json.dumps(
            {
                "kind": "final",
                "title": "Done",
                "data": {"status": "placed", "summary": "Placed cleanly."},
            }
        ),
    ]

    def _fake_post(url: str, json: dict[str, Any] | None = None, headers: dict | None = None, timeout: float = 10):
        if url.endswith("/v1/agent/runs"):
            return _PostResp({"run_id": "run-sse-edge"})
        if url.endswith("/answer") and json is not None:
            answer_posts.append(json)
        return _PostResp({"ok": True})

    @contextlib.contextmanager
    def _fake_stream(method: str, url: str, headers: dict | None = None, timeout: Any = None):
        yield _StreamCtx(sse_lines)

    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setattr(httpx, "stream", _fake_stream)

    args = argparse.Namespace(
        scenario="happy",
        thinking="LOW",
        no_escalate=False,
        model=None,
        transport="auto",
        faults=None,
        no_auto_approve=True,
        auto_approve_as=None,
        approve_delay=0.01,
        answer="auto",
    )
    stop = threading.Event()
    rc = run_demo._via_server(args, "http://gw", "100 L milk", "hotel-aurora", stop, "approve")
    out = capsys.readouterr().out
    assert rc == 0
    assert stop.is_set()
    assert answer_posts == [{"option_id": "opt-1", "customer_id": "hotel-aurora", "question_id": "q-ok"}]
    assert "Placed cleanly." in out

    @contextlib.contextmanager
    def _stream_non_dict_final(method: str, url: str, headers: dict | None = None, timeout: Any = None):
        yield _StreamCtx(["data: " + json.dumps({"kind": "final", "title": "End", "data": "non-dict"})])

    monkeypatch.setattr(httpx, "stream", _stream_non_dict_final)
    stop2 = threading.Event()
    assert run_demo._via_server(args, "http://gw", "100 L milk", "hotel-aurora", stop2, "approve") == 0
    assert stop2.is_set()


def test_answer_fn_verdict_and_cli_flags_preserved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auto_fn = run_demo._answer_fn("auto")
    assert auto_fn(None) is None
    assert auto_fn({}) is None
    assert auto_fn({"options": ["bad", {}, {"id": ""}]}) is None
    assert auto_fn({"options": [{"id": "c1", "label": None}]}) == {"option_id": "c1"}

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "c2")
    ask_fn = run_demo._answer_fn("ask")
    assert ask_fn({"options": [None, {}, {"id": "c2", "label": "Choice 2"}]}) == {"option_id": "c2"}
    monkeypatch.setattr("builtins.input", lambda prompt="": "   ")
    assert ask_fn({}) is None

    assert run_demo.verdict("happy", None) == (False, "?")
    for sc_name in (*sorted(run_demo.SCENARIOS), "custom"):
        ok, why = run_demo.verdict(
            sc_name,
            {
                "status": "error",
                "quote": {"total_net": float("nan"), "lines": ["bad", {"qty_sales_units": True}]},
                "plan": {"candidates": ["bad", {"tier": "rejected"}]},
                "steps": ["bad", {"kind": "decision", "data": None}],
            },
        )
        assert ok is False
        assert isinstance(why, str) and why

    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run_demo.main(["--help"])
    assert exc.value.code == 0
    help_out = capsys.readouterr().out
    for flag in (
        "--scenario",
        "--text",
        "--customer",
        "--auto-approve-as",
        "--no-auto-approve",
        "--reject",
        "--approve-delay",
        "--transport",
        "--base-url",
        "--via-server",
        "--model",
        "--thinking",
        "--no-escalate",
        "--max-steps",
        "--approval-timeout",
        "--image",
        "--answer",
        "--json",
        "--faults",
        "--montage",
        "--montage-out",
        "--baseline-pid",
        "--baseline-orders",
        "--baseline-label",
    ):
        assert flag in help_out
