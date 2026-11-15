from __future__ import annotations

import ast
from datetime import datetime
import io
import json
import math
from pathlib import Path
import time
import tokenize
from typing import Any
from urllib.parse import quote as url_quote

from google import genai
from google.genai import types
import pytest

from aimo import config, gemini
from aimo.buyer import mock_model as mock_model_mod
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, MockModel, mock_wanted
from aimo.buyer.need import HELSINKI, fallback_parse_items
from aimo.buyer.runs import RunRegistry, RunRequest
from aimo.buyer.tools import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "aimo" / "config.py"
GEMINI_PATH = ROOT / "aimo" / "gemini.py"
THIS_PATH = Path(__file__).resolve()
HYGIENE_TARGETS = (THIS_PATH, CONFIG_PATH, GEMINI_PATH)

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
MILK_200 = "We need 200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
HERO_1000 = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."

DAILY_PER_DAY = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_requests_per_model_per_day, limit: 10000, model: gemini-3.8-flash"
)
DAILY_PER_DAY_ALT = "429 RESOURCE_EXHAUSTED: daily per_day quota reached for gemini-3.8-flash"
TRANSIENT_429 = "429 RESOURCE_EXHAUSTED: rate limit per minute exceeded"


def _strict_no_nan(constant: str) -> Any:
    raise ValueError(f"Non-standard JSON constant forbidden: {constant}")


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            parsed = json.loads(line, parse_constant=_strict_no_nan)
            json.dumps(parsed, allow_nan=False)
            rows.append(parsed)
    return rows


def _read_ledger_rows(ledger_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ledger_dir.is_dir():
        return rows
    for fp in sorted(ledger_dir.glob("*.jsonl")):
        rows.extend(_read_jsonl_strict(fp))
    return rows


class AutoDecideGateway(FakeGateway):
    def __init__(
        self,
        customer_id: str = "hotel-aurora",
        agent_id: str = "hotel-aurora/buyer-1",
        scenario: str = "happy",
        now: datetime | None = None,
        *,
        auto_decision: str | None = None,
        approver: str = "anna@aurora",
    ) -> None:
        super().__init__(customer_id=customer_id, agent_id=agent_id, scenario=scenario, now=now)
        self.auto_decision = auto_decision
        self.approver = approver

    def get_order_status(self, order_id: str) -> dict:
        if self.auto_decision in ("approve", "reject"):
            cur = super().get_order_status(order_id)
            if cur.get("status") == "pending_approval":
                self.decide(order_id, self.auto_decision, self.approver, "approved-in-e2e-test")
        return super().get_order_status(order_id)


def _extract_request_text(prompt: str) -> str:
    marker = "Request:"
    idx = prompt.rfind(marker)
    return prompt[idx + len(marker) :].strip() if idx >= 0 else prompt.strip()


def _done_tools(contents: Any) -> list[str]:
    if isinstance(contents, str):
        return []
    return [
        fr.name
        for item in (contents or [])
        for part in (getattr(item, "parts", None) or [])
        if (fr := getattr(part, "function_response", None)) is not None and fr.name
    ]


def _last_fn_response(contents: Any, name: str) -> dict[str, Any] | None:
    if isinstance(contents, str):
        return None
    for item in reversed(list(contents or [])):
        for part in getattr(item, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr and fr.name == name:
                return fr.response
    return None


def _usage_meta(prompt_tokens: int = 120, cand_tokens: int = 32, thought_tokens: int = 8) -> types.GenerateContentResponseUsageMetadata:
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt_tokens,
        candidates_token_count=cand_tokens,
        thoughts_token_count=thought_tokens,
    )


def _build_buyer_response(
    contents: Any,
    cfg: Any,
    *,
    headers: dict[str, str] | None = None,
    http_body: str | None = None,
) -> types.GenerateContentResponse:
    http_resp = (
        types.HttpResponse(headers=dict(headers or {}), body=http_body)
        if (headers or http_body is not None)
        else None
    )
    mime = getattr(cfg, "response_mime_type", None)
    if mime == "application/json":
        req_text = _extract_request_text(contents if isinstance(contents, str) else str(contents))
        det_needs = fallback_parse_items(req_text, "hotel-aurora", SAT)
        payload = {
            "items": [
                {
                    "product_query": n.product_query,
                    "required_claims": list(n.required_claims),
                    "excluded_allergens": list(n.excluded_allergens),
                    "quantity": float(n.quantity),
                    "unit": n.unit,
                    "delivery_by": n.delivery_by.isoformat(),
                    "delivery_expression": "by Monday",
                    "site": "Hotel Aurora Helsinki",
                    "notes": "",
                }
                for n in det_needs
            ]
        }
        return types.GenerateContentResponse(
            sdk_http_response=http_resp,
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]))],
            usage_metadata=_usage_meta(90, 45, 6),
        )
    if getattr(cfg, "tools", None):
        done = _done_tools(contents)
        sig = b"thought-sig-e2e"
        if "plan_need" not in done:
            parts = [
                types.Part(text="INTERNAL_PLAN_THOUGHT", thought=True, thought_signature=sig),
                types.Part(text="Calling plan_need for the parsed request."),
                types.Part(
                    function_call=types.FunctionCall(
                        id="fc-plan-0",
                        name="plan_need",
                        args={"need": {"product_query": "lactose-free milk"}},
                    ),
                    thought_signature=sig,
                ),
            ]
            return types.GenerateContentResponse(
                sdk_http_response=http_resp,
                candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
                usage_metadata=_usage_meta(210, 40, 12),
            )
        quote_resp = _last_fn_response(contents, "create_quote")
        if quote_resp is None or "quote_id" not in quote_resp:
            plan_resp = _last_fn_response(contents, "plan_need") or {}
            lines = [
                {
                    "product_id": ln["product_id"],
                    "qty_sales_units": ln["qty_sales_units"],
                    "tier": ln.get("tier", "best"),
                }
                for ln in plan_resp.get("lines", [])
            ]
            parts = [
                types.Part(text="INTERNAL_QUOTE_THOUGHT", thought=True, thought_signature=sig),
                types.Part(text="Creating signed quote from planner lines."),
                types.Part(
                    function_call=types.FunctionCall(
                        id="fc-quote-1",
                        name="create_quote",
                        args={"delivery_date": "2026-11-16", "lines": lines},
                    ),
                    thought_signature=sig,
                ),
            ]
            return types.GenerateContentResponse(
                sdk_http_response=http_resp,
                candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
                usage_metadata=_usage_meta(260, 48, 16),
            )
        if "place_order" not in done:
            parts = [
                types.Part(text="INTERNAL_ORDER_THOUGHT", thought=True, thought_signature=sig),
                types.Part(text="Submitting signed quote for order placement."),
                types.Part(
                    function_call=types.FunctionCall(
                        id="fc-order-2",
                        name="place_order",
                        args={"quote_id": quote_resp["quote_id"], "quote_hash": quote_resp["quote_hash"]},
                    ),
                    thought_signature=sig,
                ),
            ]
            return types.GenerateContentResponse(
                sdk_http_response=http_resp,
                candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
                usage_metadata=_usage_meta(300, 36, 10),
            )
        return types.GenerateContentResponse(
            sdk_http_response=http_resp,
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Purchasing flow completed cleanly.")],
                    )
                )
            ],
            usage_metadata=_usage_meta(310, 20, 4),
        )
    prompt_str = contents if isinstance(contents, str) else str(contents)
    if "Facts JSON:" in prompt_str:
        audit_link = "/v1/audit/report/unknown"
        idx = prompt_str.find("Facts JSON:")
        try:
            facts = json.loads(prompt_str[idx + len("Facts JSON:") :].strip())
            audit_link = str(facts.get("audit_report_link") or audit_link)
        except Exception:
            pass
        text_out = f"Ordered Valio lactose-free milk via signed quote under customer policy. Audit: {audit_link}"
    else:
        text_out = "Selected Valio 1 L lactose-free milk because 10 L novobox cut-off had passed."
    return types.GenerateContentResponse(
        sdk_http_response=http_resp,
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text_out)]))],
        usage_metadata=_usage_meta(140, 28, 5),
    )


class ScriptedGenaiHarness:
    def __init__(self) -> None:
        self.client_inits: list[dict[str, Any]] = []
        self.model_calls: list[dict[str, Any]] = []
        self.fault_rules: dict[str, Any] = {}
        self.response_headers: dict[str, str] = {}
        self.response_body: str | None = None
        self.response_wrapper: Any = None
        self.forbid_client: bool = False

    def make_client(self, **kwargs: Any) -> Any:
        self.client_inits.append(dict(kwargs))
        if self.forbid_client:
            raise AssertionError("genai.Client must never be instantiated in keyless auto mode")
        api_key = kwargs.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("No API key was provided. Please pass a valid API key.")
        harness = self

        class _Models:
            def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
                call_idx = sum(1 for c in harness.model_calls if c["model"] == model)
                is_tool_turn = bool(getattr(config, "tools", None))
                is_json_parse = getattr(config, "response_mime_type", None) == "application/json"
                kind = "parse" if is_json_parse else ("step" if is_tool_turn else "text")
                harness.model_calls.append(
                    {"model": model, "call_idx": call_idx, "kind": kind, "contents": contents, "config": config}
                )
                rule = harness.fault_rules.get(model)
                action = None
                if isinstance(rule, Exception):
                    action = rule
                elif isinstance(rule, list) and rule:
                    action = rule.pop(0)
                elif callable(rule):
                    action = rule(model=model, call_idx=call_idx, kind=kind, contents=contents, config=config)
                if isinstance(action, Exception):
                    raise action
                resp = _build_buyer_response(
                    contents,
                    config,
                    headers=harness.response_headers,
                    http_body=harness.response_body,
                )
                if callable(harness.response_wrapper):
                    return harness.response_wrapper(resp, model=model, kind=kind, call_idx=call_idx)
                return resp

        class _Client:
            def __init__(self, key: str) -> None:
                self.api_key = key
                self.models = _Models()

        return _Client(api_key.strip())


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sleeps: list[float] = []
    ledger_dir = tmp_path / "ledger"
    trace_path = tmp_path / "trace.jsonl"
    harness = ScriptedGenaiHarness()

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    for env_key in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_API_KEY",
        "VALIO_API_KEY",
        "AIMO_LLM_MODE",
        "GEMINI_FALLBACK_MODELS",
        "AIMO_GEMINI_TRACE",
    ):
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(genai, "Client", harness.make_client)

    gemini._clear_client_cache()
    gemini._exhausted.clear()
    gemini._tl.model = None
    yield harness, sleeps, ledger_dir, trace_path
    gemini._clear_client_cache()
    gemini._exhausted.clear()
    gemini._tl.model = None


def _configure_live_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-only-gemini-live-key-01",
    llm_mode: str = "auto",
) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", key.strip())
    monkeypatch.setenv("GEMINI_API_KEY", key)
    monkeypatch.setenv("AIMO_LLM_MODE", llm_mode)
    gemini._clear_client_cache()


@pytest.mark.parametrize("target", HYGIENE_TARGETS, ids=lambda p: p.name)
def test_hygiene_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    ast.parse(src, filename=str(target))
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} contains comment tokens: {comments[:3]}"
    assert "\x23" not in src, f"{target.name} contains hash character"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


def test_this_test_file_contains_no_secret_key_prefixes() -> None:
    src = THIS_PATH.read_text(encoding="utf-8")
    assert ("AI" + "za") not in src
    assert ("sk-" + "live") not in src
    assert ("Co-Authored-" + "By") not in src


@pytest.mark.parametrize("llm_mode", ["auto", "live"])
@pytest.mark.parametrize("daily_msg", [DAILY_PER_DAY, DAILY_PER_DAY_ALT])
def test_daily_quota_429_immediately_falls_back_marks_exhausted_and_completes_buyer_e2e(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
    llm_mode: str,
    daily_msg: str,
) -> None:
    harness, sleeps, ledger_dir, _ = isolated_env
    _configure_live_gemini_key(monkeypatch, key="  test-only-gemini-primary-key  ", llm_mode=llm_mode)
    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(daily_msg)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    assert agent.mock is False
    assert agent.model == "gemini-3.8-flash"

    res = agent.run(MILK_200)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert MOCK_LABEL not in res["summary"]
    assert res["need"]["preferences"]["parsed_by"] == "gemini"
    assert "gemini_error" not in res["need"]["preferences"]
    assert res["need"]["quantity"] == 200.0
    assert res["need"]["unit"] == "L"
    assert res["plan"]["covered_amount"] == 200.0
    assert res["plan"]["shortfall"] == 0.0
    assert res["quote"]["quote_id"].startswith("Q-")
    assert res["order"]["order_id"].startswith("O-")
    assert res["order"]["quote_id"] == res["quote"]["quote_id"]
    assert res["order"]["quote_hash"] == res["quote"]["quote_hash"]
    assert [name for name, _ in gw.calls] == ["list_policies", "plan_need", "create_quote", "place_order"]

    assert harness.client_inits == [{"api_key": "test-only-gemini-primary-key"}]
    called_models = [c["model"] for c in harness.model_calls]
    assert called_models == [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert sleeps == []
    assert "gemini-3.8-flash" in gemini._exhausted
    assert gemini._exhausted["gemini-3.8-flash"] > time.time()

    assert [m["model"] for m in res["models_used"]] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert [m["step"] for m in res["models_used"]] == [0, 1, 2]

    fallback_thoughts = [
        s
        for s in res["steps"]
        if s["kind"] == "thought" and s["data"].get("fallback_from") == "gemini-3.8-flash"
    ]
    assert len(fallback_thoughts) == 1
    assert fallback_thoughts[0]["data"]["model"] == "gemini-3.5-flash"

    rows = _read_ledger_rows(ledger_dir)
    assert len(rows) == 7
    assert all(r["run_id"] == res["run_id"] for r in rows)
    assert rows[0]["feature"] == "buyer.parse_need"
    assert rows[0]["model"] == "gemini-3.8-flash"
    assert rows[0]["ok"] is False
    assert rows[0]["error"] == "RuntimeError"
    assert rows[0]["meta"] == {}

    ok_rows = [r for r in rows if r["ok"] is True]
    assert len(ok_rows) == 6
    assert all(r["model"] == "gemini-3.5-flash" for r in ok_rows)
    assert all(r["meta"].get("fallback_from") == "gemini-3.8-flash" for r in ok_rows)
    assert {r["feature"] for r in ok_rows} == {
        "buyer.parse_need",
        "buyer.agent_step",
        "buyer.explain",
        "buyer.summary",
    }
    assert all(r["in"] > 0 and r["out"] > 0 for r in ok_rows)


def test_daily_quota_429_mid_run_with_pending_approval_and_escalation_e2e(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, _ = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="live")

    def primary_rule(*, kind: str, **_: Any) -> Exception | None:
        if kind == "parse":
            return None
        return RuntimeError(DAILY_PER_DAY)

    harness.fault_rules["gemini-3.8-flash"] = primary_rule

    gw = AutoDecideGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT, auto_decision="approve")
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=True, poll_s=0.125)

    res = agent.run(HERO_1000)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert res["escalated"] if "escalated" in res else agent.escalated is True
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == "anna@aurora"
    assert [name for name, _ in gw.calls] == [
        "list_policies",
        "plan_need",
        "create_quote",
        "place_order",
        "get_order_status",
    ]
    assert sleeps == [0.125]
    assert "gemini-3.8-flash" in gemini._exhausted

    assert [m["model"] for m in res["models_used"]] == [
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "gemini-3.5-flash",
    ]

    rows = _read_ledger_rows(ledger_dir)
    assert rows[0]["feature"] == "buyer.parse_need"
    assert rows[0]["model"] == "gemini-3.8-flash"
    assert rows[0]["ok"] is True
    assert "fallback_from" not in rows[0]["meta"]

    failed = [r for r in rows if not r["ok"]]
    assert len(failed) == 1
    assert failed[0]["feature"] == "buyer.agent_step"
    assert failed[0]["model"] == "gemini-3.8-flash"

    fallback_rows = [r for r in rows if r["ok"] and r["model"] == "gemini-3.5-flash"]
    assert len(fallback_rows) == 4
    assert all(r["meta"].get("fallback_from") == "gemini-3.8-flash" for r in fallback_rows)


def test_daily_quota_429_respects_custom_fallback_chain_and_google_api_key(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, _ = isolated_env
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("GOOGLE_API_KEY", "  test-only-google-env-key  ")
    monkeypatch.setenv("AIMO_LLM_MODE", "auto")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite")
    gemini._clear_client_cache()

    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(DAILY_PER_DAY)
    harness.fault_rules["gemini-3.5-flash"] = RuntimeError(
        "429 RESOURCE_EXHAUSTED: generate_requests_per_model_per_day for gemini-3.5-flash"
    )

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    res = agent.run(MILK_200)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert sleeps == []
    assert harness.client_inits == [{"api_key": "test-only-google-env-key"}]
    assert {"gemini-3.8-flash", "gemini-3.5-flash"} <= set(gemini._exhausted)
    assert [m["model"] for m in res["models_used"]] == [
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    ok_rows = [r for r in _read_ledger_rows(ledger_dir) if r["ok"]]
    assert len(ok_rows) == 6
    assert all(r["model"] == "gemini-3.1-flash-lite" for r in ok_rows)
    assert all(r["meta"].get("fallback_from") == "gemini-3.8-flash" for r in ok_rows)


@pytest.mark.parametrize("llm_mode", ["auto", "live"])
def test_double_transient_429_switches_to_fallback_and_completes_buyer_e2e(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
    llm_mode: str,
) -> None:
    harness, sleeps, ledger_dir, _ = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode=llm_mode)
    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(TRANSIENT_429)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    res = agent.run(MILK_200)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert res["need"]["preferences"]["parsed_by"] == "gemini"
    assert res["quote"]["quote_id"].startswith("Q-")
    assert res["order"]["order_id"].startswith("O-")
    assert [name for name, _ in gw.calls] == ["list_policies", "plan_need", "create_quote", "place_order"]

    assert "gemini-3.8-flash" not in gemini._exhausted
    assert len(sleeps) == 6
    assert all(1.0 <= s < 2.0 for s in sleeps)

    expected_sequence = ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash"] * 6
    assert [c["model"] for c in harness.model_calls] == expected_sequence
    assert [m["model"] for m in res["models_used"]] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]

    rows = _read_ledger_rows(ledger_dir)
    assert len(rows) == 18
    for group_idx in range(6):
        chunk = rows[group_idx * 3 : (group_idx + 1) * 3]
        assert [(r["model"], r["ok"], r["meta"].get("fallback_from")) for r in chunk] == [
            ("gemini-3.8-flash", False, None),
            ("gemini-3.8-flash", False, None),
            ("gemini-3.5-flash", True, "gemini-3.8-flash"),
        ]


def test_double_transient_429_cascades_across_two_models_to_third_and_completes_e2e(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, _ = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="live")
    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(TRANSIENT_429)
    harness.fault_rules["gemini-3.5-flash"] = RuntimeError(TRANSIENT_429)

    gw = AutoDecideGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT, auto_decision="approve")
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False, poll_s=0.05)
    res = agent.run(HERO_1000)

    assert res["status"] == "placed"
    assert res["order"]["approved_by"] == "anna@aurora"
    assert gemini._exhausted == {}
    assert len(sleeps) == 13
    assert [m["model"] for m in res["models_used"]] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
    ]

    ok_rows = [r for r in _read_ledger_rows(ledger_dir) if r["ok"]]
    assert len(ok_rows) == 6
    assert all(r["model"] == "gemini-4-hydrogen" for r in ok_rows)
    assert all(r["meta"].get("fallback_from") == "gemini-3.8-flash" for r in ok_rows)


def test_double_transient_429_on_single_step_recovers_primary_on_subsequent_steps(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, _, _ = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="auto")
    harness.fault_rules["gemini-3.8-flash"] = [
        None,
        RuntimeError(TRANSIENT_429),
        RuntimeError(TRANSIENT_429),
    ]

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    res = agent.run(MILK_200)

    assert res["status"] == "placed"
    assert sleeps == pytest.approx([sleeps[0]]) and len(sleeps) == 1
    assert [m["model"] for m in res["models_used"]] == [
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
    ]


def test_aimo_gemini_trace_writes_strict_jsonl_and_redacts_all_keys_encoded_variants_and_headers(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, ledger_dir, trace_path = isolated_env

    gemini_secret = "test-only-gemini-tok/alpha+1=2"
    google_secret = "test-only-google-tok/beta+3=4"
    aimo_secret = "test-only-aimo-tok/gamma+5=6"
    valio_secret = "test-only-valio-tok/delta+7=8"
    raw_secrets = (gemini_secret, google_secret, aimo_secret, valio_secret)
    encoded_secrets = tuple(url_quote(s, safe="") for s in raw_secrets)
    assert all(enc != raw for enc, raw in zip(encoded_secrets, raw_secrets))

    header_goog_val = "unrelated-header-goog-token-9001"
    header_api_val = "unrelated-header-api-token-9002"

    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_secret)
    monkeypatch.setattr(config, "AIMO_API_KEY", aimo_secret)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_secret)
    monkeypatch.setenv("GOOGLE_API_KEY", google_secret)
    monkeypatch.setenv("AIMO_API_KEY", aimo_secret)
    monkeypatch.setenv("VALIO_API_KEY", valio_secret)
    monkeypatch.setenv("AIMO_LLM_MODE", "live")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))
    gemini._clear_client_cache()

    leak_blob = (
        f"raw=({gemini_secret}|{google_secret}|{aimo_secret}|{valio_secret}) "
        f"enc=({encoded_secrets[0]}|{encoded_secrets[1]}|{encoded_secrets[2]}|{encoded_secrets[3]})"
    )

    def primary_leaking_fault(*, kind: str, call_idx: int, **_: Any) -> Exception:
        if kind == "parse":
            return RuntimeError(f"{TRANSIENT_429} {leak_blob}")
        return RuntimeError(f"{DAILY_PER_DAY} {leak_blob} call={call_idx}")

    harness.fault_rules["gemini-3.8-flash"] = primary_leaking_fault
    harness.response_headers = {
        "x-goog-api-key": header_goog_val,
        "X-Api-Key": header_api_val,
        "x-upstream-leak": leak_blob,
    }
    harness.response_body = f"upstream-echo: {leak_blob}"

    class _NanSafeDumpWrapper:
        def __init__(self, inner: types.GenerateContentResponse, call_idx: int) -> None:
            self._inner = inner
            self._call_idx = call_idx
            self.candidates = inner.candidates
            self.text = inner.text
            self.usage_metadata = inner.usage_metadata

        def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
            dumped = self._inner.model_dump(mode=mode, exclude_none=exclude_none)
            hdrs = dict(dumped.get("sdk_http_response", {}).get("headers", {}))
            if self._call_idx % 2 == 1:
                hdrs["X-Goog-Api-Key"] = hdrs.pop("x-goog-api-key", header_goog_val)
                hdrs["x-api-key"] = hdrs.pop("X-Api-Key", header_api_val)
            dumped["sdk_http_response"] = {
                "headers": hdrs,
                "body": dumped.get("sdk_http_response", {}).get("body", ""),
            }
            dumped["non_finite_probe"] = [math.nan, math.inf, -math.inf]
            dumped["echo_leak"] = leak_blob
            return dumped

    harness.response_wrapper = lambda resp, call_idx, **_: _NanSafeDumpWrapper(resp, call_idx)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    res = agent.run(MILK_200)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert trace_path.is_file()

    raw_trace_text = trace_path.read_text(encoding="utf-8")
    raw_ledger_text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(ledger_dir.glob("*.jsonl")))

    for forbidden in (*raw_secrets, *encoded_secrets, header_goog_val, header_api_val):
        assert forbidden not in raw_trace_text, f"Unredacted token leaked in trace: {forbidden}"
        assert forbidden not in raw_ledger_text, f"Unredacted token leaked in ledger: {forbidden}"

    trace_rows = _read_jsonl_strict(trace_path)
    ledger_rows = _read_ledger_rows(ledger_dir)
    assert len(trace_rows) == 9
    assert len(ledger_rows) == 9
    assert all(r["run_id"] == res["run_id"] for r in trace_rows)
    assert all(r["ts"].endswith("Z") and isinstance(r["latency_s"], float) for r in trace_rows)

    failed_trace = [r for r in trace_rows if r["ok"] is False]
    ok_trace = [r for r in trace_rows if r["ok"] is True]
    assert len(failed_trace) == 3
    assert len(ok_trace) == 6

    assert [(r["feature"], r["model"], r["attempt"]) for r in failed_trace] == [
        ("buyer.parse_need", "gemini-3.8-flash", 0),
        ("buyer.parse_need", "gemini-3.8-flash", 1),
        ("buyer.agent_step", "gemini-3.8-flash", 0),
    ]
    for f_row in failed_trace:
        assert f_row["error"].count("[REDACTED]") == 8
        assert "request" not in f_row and "response" not in f_row

    assert {r["feature"] for r in ok_trace} == {
        "buyer.parse_need",
        "buyer.agent_step",
        "buyer.explain",
        "buyer.summary",
    }
    saw_lower_headers = False
    saw_mixed_headers = False
    for s_row in ok_trace:
        assert s_row["requested_model"] == "gemini-3.8-flash"
        assert s_row["model"] == "gemini-3.5-flash"
        assert s_row["meta"]["fallback_from"] == "gemini-3.8-flash"
        assert "request" in s_row and "usage" in s_row
        resp_dump = s_row["response"]
        assert resp_dump["non_finite_probe"] == [None, None, None]
        assert resp_dump["echo_leak"].count("[REDACTED]") == 8
        http_dump = resp_dump["sdk_http_response"]
        assert http_dump["body"].count("[REDACTED]") == 8
        hdrs = http_dump["headers"]
        assert hdrs["x-upstream-leak"].count("[REDACTED]") == 8
        if "x-goog-api-key" in hdrs:
            saw_lower_headers = True
            assert hdrs["x-goog-api-key"] == "[REDACTED]"
            assert hdrs["X-Api-Key"] == "[REDACTED]"
        else:
            saw_mixed_headers = True
            assert hdrs["X-Goog-Api-Key"] == "[REDACTED]"
            assert hdrs["x-api-key"] == "[REDACTED]"
    assert saw_lower_headers and saw_mixed_headers


@pytest.mark.parametrize(
    "req_text,auto_decision,expected_tools",
    [
        (MILK_200, None, ["list_policies", "plan_need", "create_quote", "place_order"]),
        (
            HERO_1000,
            "approve",
            ["list_policies", "plan_need", "create_quote", "place_order", "get_order_status"],
        ),
    ],
)
def test_keyless_auto_switch_uses_mock_model_never_calls_genai_client_and_writes_zero_rows(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
    req_text: str,
    auto_decision: str | None,
    expected_tools: list[str],
) -> None:
    harness, sleeps, ledger_dir, trace_path = isolated_env
    harness.forbid_client = True

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    monkeypatch.setenv("AIMO_LLM_MODE", "auto")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))

    assert mock_wanted() is True

    gw = AutoDecideGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT, auto_decision=auto_decision)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, poll_s=0.05)

    assert agent.mock is True
    assert isinstance(agent._generate, MockModel)
    assert agent.model == MOCK_MODEL_NAME
    assert agent.use_gemini_parse is False

    res = agent.run(req_text)

    assert res["status"] == "placed"
    assert res["mock"] is True
    assert res["summary"].startswith(MOCK_LABEL)
    assert res["quote"]["quote_id"].startswith("Q-")
    assert res["order"]["order_id"].startswith("O-")
    assert res["order"]["status"] == "placed"
    assert [name for name, _ in gw.calls] == expected_tools
    assert [m["model"] for m in res["models_used"]] == [
        MOCK_MODEL_NAME,
        MOCK_MODEL_NAME,
        MOCK_MODEL_NAME,
    ]
    assert harness.client_inits == []
    assert harness.model_calls == []
    assert _read_ledger_rows(ledger_dir) == []
    assert not ledger_dir.exists()
    assert not trace_path.exists()
    if auto_decision is None:
        assert sleeps == []
    else:
        assert sleeps == [0.05]


def test_keyless_live_mode_fails_cleanly_without_silently_running_mock_model(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, trace_path = isolated_env

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    monkeypatch.setenv("AIMO_LLM_MODE", "live")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))

    mock_inits: list[Any] = []
    orig_mock_init = MockModel.__init__

    def _spy_mock_init(self, *args: Any, **kwargs: Any) -> None:
        mock_inits.append((args, kwargs))
        orig_mock_init(self, *args, **kwargs)

    monkeypatch.setattr(mock_model_mod.MockModel, "__init__", _spy_mock_init)

    assert mock_wanted() is False

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT)
    assert agent.mock is False
    assert agent._generate is None
    assert agent.model == "gemini-3.8-flash"

    res = agent.run(MILK_200)

    assert mock_inits == []
    assert res["status"] == "error"
    assert res["mock"] is False
    assert res["quote"] is None
    assert res["order"] is None
    assert res["models_used"] == []
    assert gw.quotes == {}
    assert gw.orders == {}
    assert [name for name, _ in gw.calls] == ["list_policies"]
    assert "ValueError" in res["summary"]
    assert MOCK_LABEL not in res["summary"]
    assert MOCK_MODEL_NAME not in json.dumps(res)
    assert sleeps == []
    assert harness.client_inits == [{}, {}]
    assert harness.model_calls == []

    step_kinds = [s["kind"] for s in res["steps"]]
    assert "error" in step_kinds
    assert step_kinds[-1] == "final"
    assert res["steps"][-1]["data"]["status"] == "error"

    ledger_rows = _read_ledger_rows(ledger_dir)
    trace_rows = _read_jsonl_strict(trace_path)
    assert len(ledger_rows) == 2
    assert len(trace_rows) == 2
    assert all(r["ok"] is False and r["error"] == "ValueError" for r in ledger_rows)
    assert [r["feature"] for r in trace_rows] == ["buyer.parse_need", "buyer.agent_step"]

    reg = RunRegistry()
    gw_run = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    run = reg.start(
        RunRequest(text=MILK_200, customer_id="hotel-aurora", transport="fake"),
        transport_factory=lambda _req: FakeTransport(gw_run),
        agent_kwargs={"now": SAT},
    )
    deadline = time.monotonic() + 5.0
    with run.cond:
        while not run.done() and time.monotonic() < deadline:
            run.cond.wait(timeout=0.5)
    view = run.view()
    assert run.status == "error"
    assert view["order_status"] == "error"
    assert view["mock"] is False
    assert MOCK_LABEL not in (view["summary"] or "")
    assert mock_inits == []
    assert gw_run.orders == {}


def test_consecutive_buyer_runs_skip_exhausted_primary_until_ttl_expires(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, trace_path = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="auto")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))

    primary_fail = [True]

    def primary_gate(**_: Any) -> Exception | None:
        if primary_fail[0]:
            return RuntimeError(DAILY_PER_DAY)
        return None

    harness.fault_rules["gemini-3.8-flash"] = primary_gate

    gw1 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res1 = BuyerAgent(FakeTransport(gw1), "hotel-aurora", now=SAT, escalate=False).run(MILK_200)
    assert res1["status"] == "placed"
    assert "gemini-3.8-flash" in gemini._exhausted

    calls_after_r1 = len(harness.model_calls)
    gw2 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res2 = BuyerAgent(FakeTransport(gw2), "hotel-aurora", now=SAT, escalate=False).run(MILK_200)
    assert res2["status"] == "placed"
    r2_calls = [c["model"] for c in harness.model_calls[calls_after_r1:]]
    assert r2_calls == ["gemini-3.5-flash"] * 6
    assert [m["model"] for m in res2["models_used"]] == ["gemini-3.5-flash"] * 3

    primary_fail[0] = False
    gemini._exhausted["gemini-3.8-flash"] = time.time() - 1.0
    calls_after_r2 = len(harness.model_calls)
    gw3 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res3 = BuyerAgent(FakeTransport(gw3), "hotel-aurora", now=SAT, escalate=False).run(MILK_200)
    assert res3["status"] == "placed"
    r3_calls = [c["model"] for c in harness.model_calls[calls_after_r2:]]
    assert r3_calls == ["gemini-3.8-flash"] * 6
    assert [m["model"] for m in res3["models_used"]] == ["gemini-3.8-flash"] * 3
    assert sleeps == []

    r3_ledger = [r for r in _read_ledger_rows(ledger_dir) if r["run_id"] == res3["run_id"]]
    r3_trace = [r for r in _read_jsonl_strict(trace_path) if r["run_id"] == res3["run_id"]]
    assert len(r3_ledger) == 6 and len(r3_trace) == 6
    assert all("fallback_from" not in (r.get("meta") or {}) for r in r3_ledger)
    assert all("fallback_from" not in (r.get("meta") or {}) for r in r3_trace)


def test_school_allergen_hardblock_and_injection_screen_preserved_under_gemini_fallback(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, ledger_dir, trace_path = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="live")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))
    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(DAILY_PER_DAY)

    gw_school = AutoDecideGateway(
        "school-tampere",
        "school-tampere/buyer-1",
        scenario="injection",
        now=SAT,
        auto_decision="approve",
        approver="maija@tampere",
    )
    agent = BuyerAgent(
        FakeTransport(gw_school),
        "school-tampere",
        now=SAT,
        escalate=False,
        clarify=False,
        poll_s=0.02,
    )
    res = agent.run("We need 100 litres of milk at Tampere school kitchen by Monday.")

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert set(res["need"]["excluded_allergens"]) >= {"milk", "nuts"}
    planned_lines = res["plan"]["lines"]
    assert planned_lines
    for line in planned_lines:
        assert line["product_id"] not in ("463038", "464306", "469363", "462775")
        assert "allergen_safe_alternative" in line["flags"]
        assert isinstance(line["injection_screen"], dict)
        assert "suspicious" in line["injection_screen"]
    assert res["order"]["approved_by"] == "maija@tampere"
    assert [m["model"] for m in res["models_used"]] == ["gemini-3.5-flash"] * 3

    ok_ledger = [r for r in _read_ledger_rows(ledger_dir) if r["ok"]]
    ok_trace = [r for r in _read_jsonl_strict(trace_path) if r["ok"]]
    assert len(ok_ledger) == 6 and len(ok_trace) == 6
    assert all(r["meta"].get("fallback_from") == "gemini-3.8-flash" for r in ok_ledger)


def test_trace_and_ledger_scrub_secrets_in_request_contents_meta_and_run_id(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, ledger_dir, trace_path = isolated_env

    gem_key = "test-only-gem-secret/one+1=1"
    goo_key = "test-only-goo-secret/two+2=2"
    aim_key = "test-only-aim-secret/three+3=3"
    val_key = "test-only-val-secret/four+4=4"
    all_raw = (gem_key, goo_key, aim_key, val_key)
    all_enc = tuple(url_quote(k, safe="") for k in all_raw)

    monkeypatch.setattr(config, "GEMINI_API_KEY", gem_key)
    monkeypatch.setattr(config, "AIMO_API_KEY", aim_key)
    monkeypatch.setenv("GEMINI_API_KEY", gem_key)
    monkeypatch.setenv("GOOGLE_API_KEY", goo_key)
    monkeypatch.setenv("AIMO_API_KEY", aim_key)
    monkeypatch.setenv("VALIO_API_KEY", val_key)
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))
    gemini._clear_client_cache()

    harness.response_headers = {
        " x-goog-api-key ": "padded-goog-header-secret-777",
        " X-API-KEY ": "padded-api-header-secret-888",
    }

    prompt_with_secrets = (
        f"Check milk order {gem_key} {goo_key} {aim_key} {val_key} "
        f"{all_enc[0]} {all_enc[1]} {all_enc[2]} {all_enc[3]}"
    )
    meta_with_secrets = {
        "note": f"{gem_key}|{all_enc[1]}",
        "x-goog-api-key": "meta-goog-header-secret-555",
        "x-api-key": "meta-api-header-secret-666",
    }
    r = gemini.generate(
        "buyer.custom_probe",
        "gemini-3.8-flash",
        prompt_with_secrets,
        run_id=f"run-{aim_key}-{all_enc[3]}",
        meta=meta_with_secrets,
    )
    assert r.text

    trace_raw = trace_path.read_text(encoding="utf-8")
    ledger_raw = "\n".join(p.read_text(encoding="utf-8") for p in sorted(ledger_dir.glob("*.jsonl")))
    for tok in (
        *all_raw,
        *all_enc,
        "padded-goog-header-secret-777",
        "padded-api-header-secret-888",
        "meta-goog-header-secret-555",
        "meta-api-header-secret-666",
    ):
        assert tok not in trace_raw
        assert tok not in ledger_raw

    t_rows = _read_jsonl_strict(trace_path)
    l_rows = _read_ledger_rows(ledger_dir)
    assert len(t_rows) == 1 and len(l_rows) == 1
    assert t_rows[0]["run_id"] == "run-[REDACTED]-[REDACTED]"
    assert l_rows[0]["run_id"] == "run-[REDACTED]-[REDACTED]"
    assert t_rows[0]["meta"]["x-goog-api-key"] == "[REDACTED]"
    assert t_rows[0]["meta"]["x-api-key"] == "[REDACTED]"
    assert l_rows[0]["meta"]["x-goog-api-key"] == "[REDACTED]"
    assert l_rows[0]["meta"]["x-api-key"] == "[REDACTED]"
    assert t_rows[0]["request"]["contents"].count("[REDACTED]") == 8


def test_all_fallback_models_exhausted_fails_cleanly_without_mock_model(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, sleeps, ledger_dir, trace_path = isolated_env
    _configure_live_gemini_key(monkeypatch, llm_mode="auto")
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))

    for m in ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-4-hydrogen"):
        harness.fault_rules[m] = RuntimeError(f"429 RESOURCE_EXHAUSTED: per_day quota reached for {m}")

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", now=SAT, escalate=False)
    res = agent.run(MILK_200)

    assert res["status"] == "error"
    assert res["mock"] is False
    assert MOCK_LABEL not in res["summary"]
    assert res["quote"] is None and res["order"] is None
    assert gw.quotes == {} and gw.orders == {}
    assert sleeps == []
    assert set(gemini._exhausted) == {"gemini-3.8-flash", "gemini-3.5-flash", "gemini-4-hydrogen"}

    t_rows = _read_jsonl_strict(trace_path)
    l_rows = _read_ledger_rows(ledger_dir)
    assert len(t_rows) == 4 and len(l_rows) == 4
    assert all(r["ok"] is False for r in t_rows)
    assert [r["model"] for r in t_rows] == [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
    ]


def test_run_registry_keyless_auto_vs_keyed_quota_fallback_e2e(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, ledger_dir, trace_path = isolated_env
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    monkeypatch.setenv("AIMO_LLM_MODE", "auto")
    harness.forbid_client = True

    reg_mock = RunRegistry()
    gw_mock = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    run_mock = reg_mock.start(
        RunRequest(text=MILK_200, customer_id="hotel-aurora", transport="fake"),
        transport_factory=lambda _req: FakeTransport(gw_mock),
        agent_kwargs={"now": SAT},
    )
    deadline = time.monotonic() + 5.0
    with run_mock.cond:
        while not run_mock.done() and time.monotonic() < deadline:
            run_mock.cond.wait(timeout=0.5)
    v_mock = run_mock.view()
    assert run_mock.status == "finished"
    assert v_mock["order_status"] == "placed"
    assert v_mock["mock"] is True
    assert harness.client_inits == []
    assert _read_ledger_rows(ledger_dir) == []
    assert not trace_path.exists()

    harness.forbid_client = False
    _configure_live_gemini_key(monkeypatch, llm_mode="auto")
    harness.fault_rules["gemini-3.8-flash"] = RuntimeError(DAILY_PER_DAY)

    reg_live = RunRegistry()
    gw_live = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    run_live = reg_live.start(
        RunRequest(text=MILK_200, customer_id="hotel-aurora", transport="fake", escalate=False),
        transport_factory=lambda _req: FakeTransport(gw_live),
        agent_kwargs={"now": SAT},
    )
    deadline = time.monotonic() + 5.0
    with run_live.cond:
        while not run_live.done() and time.monotonic() < deadline:
            run_live.cond.wait(timeout=0.5)
    v_live = run_live.view()
    assert run_live.status == "finished"
    assert v_live["order_status"] == "placed"
    assert v_live["mock"] is False
    assert [m["model"] for m in run_live.result["models_used"]] == ["gemini-3.5-flash"] * 3
    assert len(_read_jsonl_strict(trace_path)) == 7
    assert len(_read_ledger_rows(ledger_dir)) == 7
