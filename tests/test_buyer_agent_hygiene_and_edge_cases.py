from __future__ import annotations

import ast
from datetime import date, datetime
import io
import json
from pathlib import Path
import tokenize
from typing import Any

from google.genai import types
import pytest

from aimo import config, gemini
import aimo.buyer.agent as buyer_agent_mod
from aimo.buyer.agent import BuyerAgent, _valid_quantity
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL
from aimo.buyer.need import HELSINKI
from aimo.buyer.tool_schemas import TOOL_SCHEMAS
from aimo.buyer.tools import FakeTransport, ToolSpec
from aimo.models import Need

ROOT = Path(__file__).resolve().parents[1]
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
MULTI_CREAM_BUTTER = "We need 40 litres of lactose-free whipping cream and 20 kg of lactose-free butter by Monday."
APPROVER = "anna@hotel-aurora.example"


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
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


def _last_fn_response(contents: list[Any], name: str) -> dict[str, Any] | None:
    for item in reversed(contents):
        for part in getattr(item, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr and fr.name == name:
                return fr.response
    return None


def _order_dict(
    order_id: str,
    quote_id: str,
    quote_hash: str,
    status: str,
    *,
    approved_by: str | None = None,
    response_code: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "order_id": order_id,
        "quote_id": quote_id,
        "quote_hash": quote_hash,
        "customer_id": "hotel-aurora",
        "agent_id": "buyer-1",
        "status": status,
        "decision": {
            "outcome": "needs_approval",
            "approver_role": "procurement_manager",
            "hits": [
                {
                    "rule_id": "spend.approval",
                    "outcome": "needs_approval",
                    "message": "Total exceeds EUR 500 auto-approve limit.",
                }
            ],
            "policy_id": "hotel-aurora",
            "policy_version": "1",
        },
        "approved_by": approved_by,
        "response_code": response_code,
        "response_lines": [],
        "created_at": "2026-11-14T13:00:00+00:00",
        "updated_at": "2026-11-14T13:05:00+00:00",
    }
    if provenance is not None:
        out["provenance"] = provenance
    return out


class _BusinessQuoteError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("message") or body.get("error") or "refused"))
        self.status = status
        self._body = body

    def to_dict(self) -> dict[str, Any]:
        return dict(self._body)


class _MultiItemReplanTransport:
    kind = "stub"

    def __init__(
        self,
        *,
        plans_by_query: dict[str, list[dict[str, Any]]],
        quote_failures: list[Any],
        status_sequences: dict[str, list[Any]] | None = None,
    ) -> None:
        self.plans_by_query = {k: [dict(p) for p in seq] for k, seq in plans_by_query.items()}
        self.quote_failures = list(quote_failures)
        self.status_sequences = {
            oid: list(seq)
            for oid, seq in (
                status_sequences
                or {
                    "O-1": [
                        _order_dict("O-1", "Q-1", "hash-q1", "placed", approved_by=APPROVER, response_code="AP")
                    ]
                }
            ).items()
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if name == "list_policies":
            return {"policies": [{"customer_id": "hotel-aurora", "policy_id": "hotel-aurora"}], "learned_swaps": []}
        if name == "plan_need":
            need = dict(args["need"])
            pq = str(need.get("product_query") or "").lower()
            key = "cream" if "cream" in pq else ("butter" if "butter" in pq else pq)
            seq = self.plans_by_query[key]
            chosen = seq.pop(0) if len(seq) > 1 else dict(seq[0])
            return {"need": need, **chosen}
        if name == "create_quote":
            if self.quote_failures:
                failure = self.quote_failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return dict(failure)
            q_lines = []
            total = 0.0
            for ln in args["lines"]:
                qty = float(ln["qty_sales_units"])
                price = 4.5 if str(ln["product_id"]) == "401201" else 8.0
                lt = round(qty * price, 2)
                total = round(total + lt, 2)
                q_lines.append(
                    {
                        "product_id": str(ln["product_id"]),
                        "product_name": f"Product {ln['product_id']}",
                        "qty_sales_units": qty,
                        "sales_unit": "ST",
                        "unit_price": price,
                        "line_total": lt,
                        "tier": ln.get("tier") or "best",
                        "flags": [],
                    }
                )
            return {
                "quote_id": "Q-1",
                "quote_hash": "hash-q1",
                "customer_id": "hotel-aurora",
                "agent_id": "buyer-1",
                "delivery_date": args["delivery_date"],
                "lines": q_lines,
                "total_net": total,
                "total_gross": round(total * 1.14, 2),
                "created_at": "2026-11-14T13:00:00+00:00",
                "expires_at": "2026-11-14T13:15:00+00:00",
            }
        if name == "place_order":
            return _order_dict("O-1", args["quote_id"], args["quote_hash"], "pending_approval")
        if name == "get_order_status":
            oid = args["order_id"]
            seq = self.status_sequences[oid]
            nxt = seq.pop(0) if len(seq) > 1 else seq[0]
            return dict(nxt) if isinstance(nxt, dict) else nxt
        raise AssertionError(f"Unexpected tool call: {name}")

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def keyless_isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini network calls must not run in unit tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)


def _plan_payload(pid: str, name: str, qty: float, price: float, shortfall: float = 0.0) -> dict[str, Any]:
    total = round(qty * price, 2)
    return {
        "lines": [
            {
                "product_id": pid,
                "product_name": name,
                "tier": "best",
                "qty_sales_units": qty,
                "sales_unit": "ST",
                "covered_amount": qty,
                "unit_price": price,
                "line_total": total,
                "reasons": [f"Planned {qty:g} units of {name}."],
                "flags": ["partial_stock"] if shortfall > 0 else [],
            }
        ],
        "candidates": [],
        "covered_amount": qty,
        "shortfall": shortfall,
        "total_net": total,
        "explanation": f"Planned {qty:g} units of {name}.",
    }


def _construct_need(
    product_query: str,
    quantity: Any,
    unit: str = "L",
    customer_id: str = "hotel-aurora",
) -> Need:
    return Need.model_construct(
        raw_text=f"{quantity} {unit} {product_query}",
        customer_id=customer_id,
        product_query=product_query,
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        quantity=quantity,
        unit=unit,
        delivery_by=date(2026, 11, 16),
        preferred_product_ids=[],
        preferences={"parsed_by": "regex"},
    )


def test_agent_and_test_file_hygiene_zero_comments_and_zero_dashes() -> None:
    targets = [
        ROOT / "aimo" / "buyer" / "agent.py",
        Path(__file__).resolve(),
    ]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        ast.parse(src, filename=str(path))
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comment_tokens = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert comment_tokens == [], f"Unexpected comments in {path}: {comment_tokens}"
        assert "\x23" not in src, f"Hash character found in {path}"
        assert "\u2014" not in src, f"Em dash found in {path}"
        assert "\u2013" not in src, f"En dash found in {path}"


def test_valid_quantity_helper_rejects_bool_none_nonfinite_overflow_and_nonpositive() -> None:
    for invalid in (
        True,
        False,
        None,
        "",
        "10",
        [],
        {},
        0,
        0.0,
        -0.0,
        -1,
        -0.0001,
        -250.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
        -(10**400),
    ):
        assert _valid_quantity(invalid) is False, f"Expected False for {invalid!r}"

    for valid in (0.1, 1, 1.0, 25, 40.5, 1000.0):
        assert _valid_quantity(valid) is True, f"Expected True for {valid!r}"


@pytest.mark.parametrize(
    "bad_qty",
    [
        0,
        0.0,
        -0.0,
        -1,
        -5.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        True,
        False,
    ],
)
def test_preplan_guard_rejects_invalid_quantities_on_single_item_with_zero_plan_calls(
    monkeypatch: pytest.MonkeyPatch,
    bad_qty: Any,
) -> None:
    monkeypatch.setattr(
        buyer_agent_mod,
        "parse_request",
        lambda *args, **kwargs: [_construct_need("lactose-free milk", bad_qty, "L")],
    )

    gw_mock = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent_mock = BuyerAgent(FakeTransport(gw_mock), "hotel-aurora", poll_s=0.01, now=SAT)
    assert agent_mock.mock is True
    res_mock = agent_mock.run(HERO)

    assert res_mock["status"] == "error"
    assert res_mock["plan"] is None
    assert res_mock["quote"] is None
    assert res_mock["order"] is None
    assert res_mock["need"] is None
    assert res_mock["needs"] == []
    assert gw_mock.calls == []
    assert [s for s in res_mock["steps"] if s["kind"] == "tool_call"] == []
    assert any(s["kind"] == "error" for s in res_mock["steps"])
    assert res_mock["steps"][-1]["kind"] == "final"
    assert res_mock["steps"][-1]["data"]["status"] == "error"
    assert res_mock["summary"].startswith(MOCK_LABEL)
    json.dumps(res_mock, allow_nan=False)

    model_invocations: list[str] = []

    def eager_planner(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        model_invocations.append(feature)
        if isinstance(contents, str):
            return _txt("Summary")
        return _fc("plan_need", {"need": {"product_query": "lactose-free milk", "quantity": 100, "unit": "L"}})

    gw_live = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent_live = BuyerAgent(
        FakeTransport(gw_live),
        "hotel-aurora",
        generate_fn=eager_planner,
        use_gemini_parse=False,
        poll_s=0.01,
        now=SAT,
    )
    assert agent_live.mock is False
    res_live = agent_live.run(HERO)

    assert res_live["status"] == "error"
    assert res_live["plan"] is None
    assert res_live["quote"] is None
    assert res_live["order"] is None
    assert gw_live.calls == []
    assert model_invocations == []
    json.dumps(res_live, allow_nan=False)


@pytest.mark.parametrize(
    "bad_qty",
    [
        0,
        -2.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        True,
        False,
    ],
)
@pytest.mark.parametrize("bad_index", [0, 1])
def test_preplan_guard_rejects_invalid_quantities_on_multi_item_with_zero_plan_calls(
    monkeypatch: pytest.MonkeyPatch,
    bad_qty: Any,
    bad_index: int,
) -> None:
    cream_qty = bad_qty if bad_index == 0 else 40.0
    butter_qty = bad_qty if bad_index == 1 else 20.0
    monkeypatch.setattr(
        buyer_agent_mod,
        "parse_request",
        lambda *args, **kwargs: [
            _construct_need("lactose-free whipping cream", cream_qty, "L"),
            _construct_need("lactose-free butter", butter_qty, "KG"),
        ],
    )

    for use_custom_gen in (False, True):
        gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
        gen_fn = (
            (lambda feature, model, contents, config=None: _fc("plan_need", {"need": {"product_query": "cream"}}))
            if use_custom_gen
            else None
        )
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            generate_fn=gen_fn,
            use_gemini_parse=False,
            poll_s=0.01,
            now=SAT,
        )
        res = agent.run(MULTI_CREAM_BUTTER)
        assert res["status"] == "error"
        assert res["plan"] is None
        assert res["quote"] is None
        assert res["order"] is None
        assert res["need"] is None
        assert res["needs"] == []
        assert [c for c in gw.calls if c[0] == "plan_need"] == []
        assert gw.calls == []
        json.dumps(res, allow_nan=False)


def test_preplan_guard_raw_zero_negative_and_direct_tool_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw_text in (
        "We need 0 litres of lactose-free milk by Monday.",
        "We need -10 litres of lactose-free milk by Monday.",
        "We need 40 litres of lactose-free cream and 0 kg of lactose-free butter by Monday.",
        "We need 40 litres of lactose-free cream and -4 kg of lactose-free butter by Monday.",
    ):
        gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
        res = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.01, now=SAT).run(raw_text)
        assert res["status"] == "error"
        assert [name for name, _ in gw.calls if name == "plan_need"] == []
        assert gw.calls == []

    monkeypatch.setattr(buyer_agent_mod, "parse_request", lambda *args, **kwargs: [])
    gw_empty = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res_empty = BuyerAgent(FakeTransport(gw_empty), "hotel-aurora", poll_s=0.01, now=SAT).run(HERO)
    assert res_empty["status"] == "error"
    assert gw_empty.calls == []

    gw_direct = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent_direct = BuyerAgent(
        FakeTransport(gw_direct),
        "hotel-aurora",
        generate_fn=lambda **kw: _txt("unused"),
        use_gemini_parse=False,
        now=SAT,
    )
    for bad in (0.0, -3.0, float("nan"), float("inf"), None, True):
        agent_direct.needs = [
            _construct_need("lactose-free whipping cream", 40.0, "L"),
            _construct_need("lactose-free butter", bad, "KG"),
        ]
        agent_direct.need = agent_direct.needs[0]
        plan_resp = agent_direct._tool("plan_need", {"need": {"product_query": "lactose-free whipping cream"}})
        quote_resp = agent_direct._tool("create_quote", {"delivery_date": "2026-11-16", "lines": []})
        assert plan_resp["error"] == "blocked_by_buyer_harness"
        assert quote_resp["error"] == "blocked_by_buyer_harness"
    assert gw_direct.calls == []


@pytest.mark.parametrize(
    "refusal_mode",
    [
        "dict_lines_unavailable",
        "exc_lines_unavailable",
        "exc_lot_size_mismatch",
        "dict_fractional_quantity",
    ],
)
@pytest.mark.parametrize("reverse_replan_order", [False, True])
def test_multi_item_cream_and_butter_replan_requires_all_active_items_before_requote(
    refusal_mode: str,
    reverse_replan_order: bool,
) -> None:
    if refusal_mode == "dict_lines_unavailable":
        failure_obj: Any = {
            "error": "lines_unavailable",
            "message": "cream and butter stock dropped before quote lock",
        }
    elif refusal_mode == "exc_lines_unavailable":
        failure_obj = _BusinessQuoteError(409, {"error": "lines_unavailable", "message": "stock changed"})
    elif refusal_mode == "exc_lot_size_mismatch":
        failure_obj = _BusinessQuoteError(409, {"error": "lot_size_mismatch", "message": "lot size changed"})
    else:
        failure_obj = {"error": "fractional_quantity", "message": "integer units required"}

    transport = _MultiItemReplanTransport(
        plans_by_query={
            "cream": [
                _plan_payload("401201", "Valio Eila whipping cream 1 L", 40.0, 4.5, shortfall=0.0),
                _plan_payload("401201", "Valio Eila whipping cream 1 L", 25.0, 4.5, shortfall=15.0),
            ],
            "butter": [
                _plan_payload("402105", "Valio Eila butter 1 kg", 20.0, 8.0, shortfall=0.0),
                _plan_payload("402105", "Valio Eila butter 1 kg", 12.0, 8.0, shortfall=8.0),
            ],
        },
        quote_failures=[failure_obj],
    )

    first_replan_q = "lactose-free butter" if reverse_replan_order else "lactose-free whipping cream"
    second_replan_q = "lactose-free whipping cream" if reverse_replan_order else "lactose-free butter"
    expected_remaining = "cream" if reverse_replan_order else "butter"

    state = {"phase": 0}
    observed_mid_block: list[dict[str, Any]] = []

    def model_fn(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        if isinstance(contents, str):
            return _txt("Multi-item replan completed.")
        phase = state["phase"]
        state["phase"] += 1
        if phase == 0:
            return _fc("plan_need", {"need": {"product_query": "lactose-free whipping cream"}})
        if phase == 1:
            return _fc("plan_need", {"need": {"product_query": "lactose-free butter"}})
        if phase == 2:
            return _fc(
                "create_quote",
                {
                    "delivery_date": "2026-11-16",
                    "lines": [
                        {"product_id": "401201", "qty_sales_units": 40.0},
                        {"product_id": "402105", "qty_sales_units": 20.0},
                    ],
                },
            )
        if phase == 3:
            return _fc(
                "create_quote",
                {
                    "delivery_date": "2026-11-16",
                    "lines": [
                        {"product_id": "401201", "qty_sales_units": 25.0},
                        {"product_id": "402105", "qty_sales_units": 12.0},
                    ],
                },
            )
        if phase == 4:
            blocked_zero = _last_fn_response(contents, "create_quote")
            assert blocked_zero is not None and blocked_zero.get("error") == "blocked_by_buyer_harness"
            return _fc("plan_need", {"need": {"product_query": first_replan_q}})
        if phase == 5:
            return _fc(
                "create_quote",
                {
                    "delivery_date": "2026-11-16",
                    "lines": [
                        {"product_id": "401201", "qty_sales_units": 25.0},
                        {"product_id": "402105", "qty_sales_units": 20.0},
                    ],
                },
            )
        if phase == 6:
            blocked_one = _last_fn_response(contents, "create_quote")
            assert blocked_one is not None and blocked_one.get("error") == "blocked_by_buyer_harness"
            observed_mid_block.append(blocked_one)
            return _fc("plan_need", {"need": {"product_query": second_replan_q}})
        if phase == 7:
            return _fc(
                "create_quote",
                {
                    "delivery_date": "2026-11-16",
                    "lines": [
                        {"product_id": "401201", "qty_sales_units": 25.0},
                        {"product_id": "402105", "qty_sales_units": 12.0},
                    ],
                },
            )
        if phase == 8:
            q = _last_fn_response(contents, "create_quote")
            assert q is not None and q.get("quote_id") == "Q-1"
            return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Done.")

    agent = BuyerAgent(
        transport,
        "hotel-aurora",
        generate_fn=model_fn,
        use_gemini_parse=False,
        clarify=False,
        poll_s=0.01,
        approval_timeout_s=5.0,
        now=SAT,
    )
    res = agent.run(MULTI_CREAM_BUTTER)

    assert res["status"] == "placed"
    assert agent._replan_needed is False
    assert agent._replan_pending == set()
    assert len(observed_mid_block) == 1
    assert expected_remaining in observed_mid_block[0]["message"].lower()

    transport_quote_calls = [args for name, args in transport.calls if name == "create_quote"]
    assert len(transport_quote_calls) == 2
    transport_plan_calls = [args for name, args in transport.calls if name == "plan_need"]
    assert len(transport_plan_calls) == 4

    blocked_quote_steps = [
        s
        for s in res["steps"]
        if s["kind"] == "tool_call" and s["data"].get("name") == "create_quote" and s["data"].get("blocked")
    ]
    assert len(blocked_quote_steps) == 2

    assert [(ln["product_id"], ln["qty_sales_units"]) for ln in res["quote"]["lines"]] == [
        ("401201", 25.0),
        ("402105", 12.0),
    ]
    assert res["plan"]["shortfall"] == pytest.approx(23.0)


def test_multi_item_replan_skips_cancelled_clarification_item_and_preserves_single_item() -> None:
    transport = _MultiItemReplanTransport(
        plans_by_query={
            "cream": [
                _plan_payload("401201", "Valio Eila whipping cream 1 L", 40.0, 4.5),
                _plan_payload("401201", "Valio Eila whipping cream 1 L", 30.0, 4.5, shortfall=10.0),
            ],
            "butter": [
                _plan_payload("402105", "Valio Eila butter 1 kg", 20.0, 8.0),
                _plan_payload("402105", "Valio Eila butter 1 kg", 15.0, 8.0, shortfall=5.0),
            ],
        },
        quote_failures=[{"error": "lines_unavailable", "message": "replan active items"}],
    )
    agent = BuyerAgent(
        transport,
        "hotel-aurora",
        generate_fn=lambda **kw: _txt("ok"),
        use_gemini_parse=False,
        explain=False,
        now=SAT,
    )
    cancelled = _construct_need("milk", 50.0, "L").model_copy(
        update={"preferences": {"parsed_by": "regex", "cancelled_by_clarification": True}}
    )
    cream = _construct_need("lactose-free whipping cream", 40.0, "L")
    butter = _construct_need("lactose-free butter", 20.0, "KG")
    agent.needs = [cancelled, cream, butter]
    agent.need = cancelled

    agent._tool("plan_need", {"need": {"product_query": "lactose-free whipping cream"}})
    agent._tool("plan_need", {"need": {"product_query": "lactose-free butter"}})
    refused = agent._tool("create_quote", {"delivery_date": "2026-11-16", "lines": []})
    assert refused["error"] == "lines_unavailable"
    assert agent._replan_pending == {1, 2}
    assert agent._replan_needed is True

    agent._tool("plan_need", {"need": {"product_query": "lactose-free whipping cream"}})
    assert agent._replan_pending == {2}
    assert agent._replan_needed is True
    mid_quote = agent._tool("create_quote", {"delivery_date": "2026-11-16", "lines": []})
    assert mid_quote["error"] == "blocked_by_buyer_harness"

    agent._tool("plan_need", {"need": {"product_query": "lactose-free butter"}})
    assert agent._replan_pending == set()
    assert agent._replan_needed is False
    ok_quote = agent._tool("create_quote", {"delivery_date": "2026-11-16", "lines": []})
    assert ok_quote["quote_id"] == "Q-1"
    assert [(ln["product_id"], ln["qty_sales_units"]) for ln in ok_quote["lines"]] == [
        ("401201", 30.0),
        ("402105", 15.0),
    ]


def test_wait_for_approval_survives_transient_nondict_payloads_before_placed() -> None:
    transport = _MultiItemReplanTransport(
        plans_by_query={
            "cream": [_plan_payload("401201", "Valio Eila whipping cream 1 L", 200.0, 4.5)],
        },
        quote_failures=[],
        status_sequences={
            "O-1": [
                None,
                [],
                ["unexpected-list-entry"],
                "transient-string",
                503,
                True,
                _order_dict("O-1", "Q-1", "hash-q1", "pending_approval"),
                None,
                _order_dict("O-1", "Q-1", "hash-q1", "placed", approved_by=APPROVER, response_code="AP"),
            ]
        },
    )
    agent = BuyerAgent(
        transport,
        "hotel-aurora",
        poll_s=0.005,
        approval_timeout_s=5.0,
        now=SAT,
    )
    res = agent.run("We need 200 litres of lactose-free whipping cream at Hotel Aurora Helsinki by Monday.")

    assert res["status"] == "placed"
    assert res["order"]["order_id"] == "O-1"
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == APPROVER
    assert res["order"]["response_code"] == "AP"

    poll_errors = [
        s
        for s in res["steps"]
        if s["kind"] == "error" and "get_order_status returned " in s["title"]
    ]
    assert len(poll_errors) == 7
    observed_titles = " | ".join(s["title"] for s in poll_errors)
    for expected_type in ("NoneType", "list", "str", "int", "bool"):
        assert expected_type in observed_titles


def test_wait_for_approval_nondict_payloads_on_timeout_preserve_order_metadata() -> None:
    transport_direct = _MultiItemReplanTransport(
        plans_by_query={
            "cream": [_plan_payload("401201", "Valio Eila whipping cream 1 L", 200.0, 4.5)],
        },
        quote_failures=[],
        status_sequences={
            "O-1": [None, [], "not-a-dict"],
        },
    )
    agent_direct = BuyerAgent(
        transport_direct,
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=0.05,
        now=SAT,
    )
    res_direct = agent_direct.run("We need 200 litres of lactose-free whipping cream at Hotel Aurora Helsinki by Monday.")

    assert res_direct["status"] == "pending_approval"
    assert res_direct["order"]["order_id"] == "O-1"
    assert res_direct["order"]["quote_id"] == "Q-1"
    assert res_direct["order"]["quote_hash"] == "hash-q1"
    assert res_direct["order"]["timed_out"] is True
    assert any(
        s["kind"] == "error" and "get_order_status returned " in s["title"]
        for s in res_direct["steps"]
    )
    assert any(
        s["kind"] == "error" and "No human decision within" in s["title"]
        for s in res_direct["steps"]
    )

    transport_handoff = _MultiItemReplanTransport(
        plans_by_query={
            "cream": [_plan_payload("401201", "Valio Eila whipping cream 1 L", 200.0, 4.5)],
        },
        quote_failures=[],
        status_sequences={
            "O-1": [
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={
                        "requote_reason": "quote_expired",
                        "requoted_as": "O-2",
                        "requote_quote_id": "Q-2",
                        "requote_quote_hash": "hash-q2",
                    },
                )
            ],
            "O-2": [None, ["malformed"]],
        },
    )
    agent_handoff = BuyerAgent(
        transport_handoff,
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=0.06,
        now=SAT,
    )
    res_handoff = agent_handoff.run("We need 200 litres of lactose-free whipping cream at Hotel Aurora Helsinki by Monday.")

    assert res_handoff["status"] == "pending_approval"
    assert res_handoff["order"]["order_id"] == "O-2"
    assert res_handoff["order"]["quote_id"] == "Q-2"
    assert res_handoff["order"]["quote_hash"] == "hash-q2"
    assert res_handoff["order"]["timed_out"] is True
    assert res_handoff["quote"]["quote_id"] == "Q-2"
    assert res_handoff["quote"]["quote_hash"] == "hash-q2"
