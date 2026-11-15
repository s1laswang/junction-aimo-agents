from __future__ import annotations

from datetime import datetime
import gc
import io
import json
import math
from pathlib import Path
import string
import tokenize
from typing import Any
import warnings

from google.genai import types
import pytest

from aimo import gemini
from aimo.buyer import costs, prompts
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)

OWNED_FILES = (
    ROOT / "aimo" / "buyer" / "costs.py",
    ROOT / "aimo" / "buyer" / "prompts.py",
    Path(__file__).resolve(),
)

EXPECTED_PROMPT_PLACEHOLDERS: dict[str, set[str]] = {
    "SYSTEM": {
        "customer_name",
        "segment",
        "agent_id",
        "site",
        "approver",
        "usual",
        "allergens",
        "mandate",
    },
    "TASK": {
        "customer_name",
        "raw_text",
        "need_json",
        "today",
        "delivery_by",
        "extra",
    },
    "DRIFT": {"ratio", "std", "unit"},
    "EXPLAIN": {"plan_json"},
    "SUMMARY": {"facts_json"},
    "MULTI": {"n"},
    "NUDGE": set(),
}


def _fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }


def _fc(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
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
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)])
            )
        ]
    )


@pytest.mark.parametrize("target", OWNED_FILES, ids=lambda p: p.name)
def test_owned_files_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has {len(comments)} comment token(s): {comments[:3]}"
    assert "\x23" not in src, f"{target.name} contains hash character"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


def test_costs_constants_pricing_clamping_and_unpriced_models() -> None:
    assert costs.CACHED_FRACTION == 0.10
    assert costs.PRICES_PER_M == {
        "gemini-3.8-flash": (0.75, 3.75),
        "gemini-4-hydrogen": (1.5, 7.5),
        "gemini-4-carbon": (2.0, 10.0),
        "gemini-4-argon": (2.0, 10.0),
    }

    flash_row = {
        "model": "gemini-3.8-flash",
        "in": 50_000,
        "cached": 20_000,
        "out": 1_500,
        "thinking": 500,
    }
    assert costs.row_cost(flash_row) == pytest.approx(
        (30_000 * 0.75 + 20_000 * 0.75 * 0.10 + 2_000 * 3.75) / 1e6
    )

    clamped = {
        "model": "gemini-3.8-flash",
        "in": 4_000,
        "cached": 10_000,
        "out": 0,
        "thinking": 0,
    }
    assert costs.row_cost(clamped) == pytest.approx((10_000 * 0.75 * 0.10) / 1e6)

    str_nums = {
        "model": "gemini-4-hydrogen",
        "in": "20000",
        "cached": "5000.0",
        "out": "1000",
        "thinking": "500",
    }
    assert costs.row_cost(str_nums) == pytest.approx(
        (15_000 * 1.5 + 5_000 * 1.5 * 0.10 + 1_500 * 7.5) / 1e6
    )

    for unpriced_model in (
        MOCK_MODEL_NAME,
        "mock-scripted",
        "mock",
        "unknown-future-model",
        "",
        None,
        123,
        ["unhashable-model"],
        {"nested": "model"},
    ):
        assert costs.row_cost({"model": unpriced_model, "in": 10_000, "out": 1_000}) is None

    for non_dict in (None, [], "row", 123, True, 3.14):
        assert costs.row_cost(non_dict) is None


@pytest.mark.parametrize(
    "bad_val",
    [
        None,
        True,
        False,
        -1,
        -999.9,
        -0.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "NaN",
        "inf",
        "-Infinity",
        "not-a-number",
        "",
        "   ",
        [],
        {},
        10**400,
    ],
)
def test_row_cost_coerces_bad_token_values_to_zero_without_nan_or_bool_leak(
    bad_val: Any,
) -> None:
    row = {
        "model": "gemini-4-carbon",
        "in": bad_val,
        "cached": bad_val,
        "out": bad_val,
        "thinking": bad_val,
    }
    c = costs.row_cost(row)
    assert c is not None
    assert math.isfinite(c)
    assert c == 0.0

    mixed = {
        "model": "gemini-4-argon",
        "in": 10_000,
        "cached": bad_val,
        "out": bad_val,
        "thinking": 1_000,
    }
    cm = costs.row_cost(mixed)
    assert cm is not None and math.isfinite(cm)
    assert cm == pytest.approx((10_000 * 2.0 + 1_000 * 10.0) / 1e6)

    overflow_row = {
        "model": "gemini-4-carbon",
        "in": 1e308,
        "cached": 0,
        "out": 1e308,
        "thinking": 1e308,
    }
    co = costs.row_cost(overflow_row)
    assert co is not None and math.isfinite(co) and co >= 0.0


def test_ledger_rows_missing_dir_sorted_files_no_leak_and_malformed_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "does-not-exist"
    monkeypatch.setattr(gemini, "LEDGER_DIR", missing_dir)
    assert costs.ledger_rows() == []
    assert costs.ledger_rows(run_ids=["r1"], pid=10, feature_prefix="buyer.") == []

    monkeypatch.setattr(gemini, "LEDGER_DIR", None)
    assert costs.ledger_rows() == []

    not_a_dir = tmp_path / "regular-file.txt"
    not_a_dir.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(gemini, "LEDGER_DIR", not_a_dir)
    assert costs.ledger_rows() == []

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gemini, "LEDGER_DIR", str(ledger_dir))

    (ledger_dir / "z-node.jsonl").write_text(
        json.dumps({"feature": "buyer.summary", "run_id": "r-z", "pid": 30}) + "\n",
        encoding="utf-8",
    )
    (ledger_dir / "a-node.jsonl").write_bytes(
        b"\xff\xfe{invalid-utf8-and-json\n"
        + b"\n"
        + b"   \n"
        + b"[]\n"
        + b"null\n"
        + b'"plain-string"\n'
        + b"12345\n"
        + b"true\n"
        + b"NaN\n"
        + b"Infinity\n"
        + b"{broken-json\n"
        + (
            json.dumps(
                {
                    "feature": None,
                    "run_id": "r-null-feat",
                    "pid": 10,
                }
            )
            + "\n"
        ).encode("utf-8")
        + (
            json.dumps(
                {
                    "feature": 999,
                    "run_id": "r-int-feat",
                    "pid": 10,
                }
            )
            + "\n"
        ).encode("utf-8")
        + (
            json.dumps(
                {
                    "feature": "buyer.parse_need",
                    "run_id": ["unhashable-run-id"],
                    "pid": True,
                }
            )
            + "\n"
        ).encode("utf-8")
        + (
            json.dumps(
                {
                    "feature": "buyer.agent_step",
                    "run_id": "r-a",
                    "pid": 1,
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    (ledger_dir / "m-node.jsonl").write_text(
        json.dumps({"feature": "buyer.explain", "run_id": "r-m", "pid": 20}) + "\n",
        encoding="utf-8",
    )
    (ledger_dir / "dir-pretending.jsonl").mkdir()

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        rows = costs.ledger_rows()
        gc.collect()

    assert [r.get("run_id") for r in rows] == [
        ["unhashable-run-id"],
        "r-a",
        "r-m",
        "r-z",
    ]
    assert [r["run_id"] for r in costs.ledger_rows(run_ids=(x for x in ["r-z", "r-a"]))] == [
        "r-a",
        "r-z",
    ]
    assert [r["run_id"] for r in costs.ledger_rows(run_ids="r-m")] == ["r-m"]
    assert [r["run_id"] for r in costs.ledger_rows(run_ids=[["unhashable"], "r-a"])] == ["r-a"]
    assert [r["run_id"] for r in costs.ledger_rows(pid=1)] == ["r-a"]
    assert costs.ledger_rows(pid=0) == []
    assert len(costs.ledger_rows(feature_prefix="")) == 6


def test_summarize_resilience_on_non_dict_rows_meta_and_non_finite_values() -> None:
    empty_expected = {
        "calls": 0,
        "runs": 0,
        "in": 0,
        "out": 0,
        "thinking": 0,
        "cached": 0,
        "gemini_s": 0.0,
        "cost_usd": 0,
        "unpriced_calls": 0,
        "agent_steps": 0,
        "escalated_steps": 0,
        "escalated_runs": 0,
        "failed": 0,
    }
    assert costs.summarize([]) == empty_expected
    assert costs.summarize(None) == empty_expected
    assert costs.summarize("not-a-list") == empty_expected
    assert costs.summarize({"not": "a-list"}) == empty_expected
    assert costs.summarize(12345) == empty_expected

    malformed_rows: list[Any] = [
        None,
        "str-row",
        123,
        ["list-row"],
        True,
        {
            "run_id": "run-ok",
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "meta": "not-a-dict-meta",
            "in": 2_000,
            "cached": 500,
            "out": 100,
            "thinking": 50,
            "latency_s": 0.4,
            "ok": True,
        },
        {
            "run_id": "run-ok",
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "meta": 42,
            "in": True,
            "cached": False,
            "out": -200,
            "thinking": None,
            "latency_s": float("nan"),
            "ok": False,
        },
        {
            "run_id": "run-ok",
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "meta": ["escalated"],
            "in": float("nan"),
            "cached": float("inf"),
            "out": "bad",
            "thinking": float("-inf"),
            "latency_s": float("inf"),
            "ok": True,
        },
        {
            "run_id": "run-esc",
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "meta": {"escalated": True},
            "in": "3000",
            "cached": "1000",
            "out": "200",
            "thinking": "100",
            "latency_s": "0.6",
            "ok": True,
        },
        {
            "run_id": ["unhashable-id"],
            "feature": "buyer.agent_step",
            "model": "gemini-4-carbon",
            "meta": True,
            "in": 1_000,
            "cached": 2_000,
            "out": 50,
            "thinking": 0,
            "latency_s": -5.0,
            "ok": True,
        },
        {
            "run_id": "   ",
            "feature": "buyer.summary",
            "model": "mock-scripted",
            "meta": None,
            "in": 500,
            "cached": 0,
            "out": 80,
            "thinking": 0,
            "latency_s": True,
            "ok": True,
        },
    ]

    s = costs.summarize(malformed_rows)
    assert s["calls"] == 6
    assert s["runs"] == 2
    assert s["in"] == 6_500
    assert s["cached"] == 3_500
    assert s["out"] == 430
    assert s["thinking"] == 150
    assert isinstance(s["in"], int)
    assert isinstance(s["out"], int)
    assert isinstance(s["thinking"], int)
    assert isinstance(s["cached"], int)
    assert s["gemini_s"] == 1.0
    assert math.isfinite(s["gemini_s"])
    assert math.isfinite(s["cost_usd"]) and s["cost_usd"] > 0.0
    assert s["unpriced_calls"] == 1
    assert s["agent_steps"] == 5
    assert s["escalated_steps"] == 2
    assert s["escalated_runs"] == 1
    assert s["escalated_runs"] <= s["runs"]
    assert s["failed"] == 1

    overflow_summary = costs.summarize(
        [
            {"model": "gemini-4-carbon", "in": 1e308, "out": 1e308, "latency_s": 1e308},
            {"model": "gemini-4-carbon", "in": 1e308, "out": 1e308, "latency_s": 1e308},
        ]
    )
    for k in ("in", "out", "thinking", "cached", "gemini_s", "cost_usd"):
        assert math.isfinite(overflow_summary[k]) and overflow_summary[k] >= 0


def test_prompts_placeholders_and_brace_safe_formatting_contracts() -> None:
    all_seen: set[str] = set()
    for name, expected in EXPECTED_PROMPT_PLACEHOLDERS.items():
        template = getattr(prompts, name)
        assert isinstance(template, str) and template.strip()
        found = _fields(template)
        assert found == expected, f"{name} placeholder mismatch: {found} != {expected}"
        all_seen |= found

    assert all_seen == {
        "customer_name",
        "segment",
        "agent_id",
        "site",
        "approver",
        "usual",
        "allergens",
        "mandate",
        "raw_text",
        "need_json",
        "today",
        "delivery_by",
        "extra",
        "ratio",
        "std",
        "unit",
        "plan_json",
        "facts_json",
        "n",
    }

    assert "needs_approval" in prompts.SYSTEM
    assert "good-better-best" in prompts.TASK
    assert "standing-order drift" in prompts.DRIFT
    assert "good-better-best" in prompts.EXPLAIN
    assert "suspicious supplier text was detected and ignored" in prompts.SUMMARY
    assert "plan_need" in prompts.MULTI
    assert "Please continue" in prompts.NUDGE

    sys_txt = prompts.SYSTEM.format(
        customer_name="Hotel Aurora {VIP}",
        segment="hotel",
        agent_id="hotel-aurora/buyer-1",
        site="Helsinki",
        approver="anna@hotel-aurora.example",
        usual="463038",
        allergens="none",
        mandate='{"auto_approve_max_net": 500}',
    )
    assert '{"auto_approve_max_net": 500}' in sys_txt

    drift_txt = prompts.DRIFT.format(ratio=10, std=200, unit="L")
    multi_txt = prompts.MULTI.format(n=2)
    task_txt = prompts.TASK.format(
        customer_name="Hotel Aurora",
        raw_text='Need 40 L cream {"note": "urgent"}',
        need_json='[{"product_query": "cream", "quantity": 40}]',
        today="Saturday 2026-11-14 15:00",
        delivery_by="2026-11-15",
        extra=drift_txt + multi_txt,
    )
    assert "10x" in task_txt and "This request has 2 items" in task_txt
    assert _fields(prompts.NUDGE) == set()

    exp_txt = prompts.EXPLAIN.format(plan_json='{"lines": [{"product_id": "464306"}]}')
    sum_txt = prompts.SUMMARY.format(facts_json='{"order_status": "placed", "nested": {"x": 1}}')
    assert '{"lines": [{"product_id": "464306"}]}' in exp_txt
    assert '{"order_status": "placed", "nested": {"x": 1}}' in sum_txt


def test_buyer_agent_prompt_assembly_end_to_end_under_offline_no_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    captured_system: list[str] = []
    captured_user_texts: list[str] = []
    captured_features: dict[str, str] = {}
    step_calls = 0

    def scripted_multi_and_nudge(
        feature: str,
        model: str,
        contents: Any,
        config: Any = None,
    ) -> types.GenerateContentResponse:
        nonlocal step_calls
        if isinstance(contents, str):
            captured_features[feature] = contents
            return _txt(f"{feature} ok")
        sys_inst = getattr(config, "system_instruction", None)
        if isinstance(sys_inst, str):
            captured_system.append(sys_inst)
        for msg in contents:
            for part in getattr(msg, "parts", None) or []:
                if getattr(part, "text", None):
                    captured_user_texts.append(part.text)
        step_calls += 1
        if step_calls == 1:
            return types.GenerateContentResponse(candidates=[])
        if step_calls == 2:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "cream",
                        "quantity": 40,
                        "unit": "L",
                        "delivery_by": "2026-11-15",
                    }
                },
            )
        if step_calls == 3:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "butter",
                        "quantity": 20,
                        "unit": "KG",
                        "delivery_by": "2026-11-15",
                    }
                },
            )
        return _txt("Finished planning multi-item conference request.")

    agent_multi = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        generate_fn=scripted_multi_and_nudge,
        use_gemini_parse=False,
        poll_s=0.02,
        now=SAT,
    )
    res_multi = agent_multi.run(
        "Conference dinner for 150 tomorrow evening: 40 L cream, 20 kg butter, lactose-free."
    )
    assert res_multi["status"] == "no_order"
    assert captured_system and "Hotel Aurora" in captured_system[0]
    assert any("This request has 2 items" in t for t in captured_user_texts)
    assert any(prompts.NUDGE == t for t in captured_user_texts)
    assert "buyer.explain" in captured_features
    assert "buyer.summary" in captured_features

    drift_tasks: list[str] = []

    def scripted_drift(
        feature: str,
        model: str,
        contents: Any,
        config: Any = None,
    ) -> types.GenerateContentResponse:
        if isinstance(contents, str):
            return _txt("drift summary")
        for msg in contents:
            for part in getattr(msg, "parts", None) or []:
                if getattr(part, "text", None):
                    drift_tasks.append(part.text)
        return _txt("Holding order for drift confirmation.")

    agent_drift = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        generate_fn=scripted_drift,
        use_gemini_parse=False,
        poll_s=0.02,
        now=SAT,
    )
    agent_drift.run("We need 3,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
    assert any("standing-order drift" in t for t in drift_tasks)
