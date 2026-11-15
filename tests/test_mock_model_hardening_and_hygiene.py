from __future__ import annotations

import ast
from datetime import date, datetime
import io
from pathlib import Path
from types import SimpleNamespace
import tokenize
from typing import Any

from google.genai import types
import pytest

from aimo import config, gemini
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import (
    MOCK_LABEL,
    MOCK_MODEL_NAME,
    MockModel,
    _collect_plan_lines,
    _detail_candidate_ids,
    _inspected_products,
    _tool_errors,
    mock_wanted,
)
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport
from aimo.models import Need

ROOT = Path(__file__).resolve().parents[1]
MOCK_MODEL_PATH = ROOT / "aimo" / "buyer" / "mock_model.py"
THIS_PATH = Path(__file__).resolve()
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)


@pytest.fixture(autouse=True)
def keyless_offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_live_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live Gemini calls are forbidden in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_live_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_live_gemini)


def _user_contents() -> list[Any]:
    return [types.Content(role="user", parts=[types.Part(text="run")])]


def _fn_resp_part(name: str, response: Any) -> Any:
    return types.Part(function_response=types.FunctionResponse(name=name, response=response))


def _make_need(**overrides: Any) -> Need:
    fields: dict[str, Any] = {
        "raw_text": "100 L lactose-free milk by Monday",
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 100.0,
        "unit": "L",
        "delivery_by": date(2026, 11, 16),
        "preferred_product_ids": [],
        "preferences": {},
    }
    fields.update(overrides)
    return Need.model_construct(**fields)


@pytest.mark.parametrize("target", [MOCK_MODEL_PATH, THIS_PATH], ids=lambda p: p.name)
def test_hygiene_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    ast.parse(src, filename=str(target))
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has comment tokens: {comments[:3]}"
    assert "\x23" not in src, f"{target.name} contains hash character"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


@pytest.mark.parametrize("bad_cfg_key", [None, 123, True, False, "   "])
def test_mock_wanted_guards_non_string_config_key_and_modes(
    monkeypatch: pytest.MonkeyPatch,
    bad_cfg_key: Any,
) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", bad_cfg_key)
    monkeypatch.setenv("AIMO_LLM_MODE", "")
    assert mock_wanted() is True
    monkeypatch.setenv("AIMO_LLM_MODE", "  SCRIPTED ")
    assert mock_wanted() is True
    monkeypatch.setenv("AIMO_LLM_MODE", " LIVE ")
    assert mock_wanted() is False


@pytest.mark.parametrize("bad_plans", [None, [], ["not-a-dict"], "bad", 123, True, {1, 2}])
@pytest.mark.parametrize("bad_plan", [None, [], ["not-a-dict"], "bad", 123, True])
def test_collect_plan_lines_guards_non_dict_plans_and_plan(bad_plans: Any, bad_plan: Any) -> None:
    stub = SimpleNamespace(plans=bad_plans, plan=bad_plan, needs="not-a-list")
    assert _collect_plan_lines(stub) == []
    assert _collect_plan_lines(None) == []


def test_collect_plan_lines_guards_non_dict_plan_entries_mixed_keys_and_non_list_lines() -> None:
    cancelled_need = _make_need(preferences={"cancelled_by_clarification": True})
    active_need = _make_need(preferences="not-a-dict")
    stub = SimpleNamespace(
        needs=[cancelled_need, active_need],
        plans={
            0: {"lines": [{"product_id": "464306", "qty_sales_units": 999}]},
            1: {"lines": [{"product_id": "464306", "qty_sales_units": 10, "tier": "best"}]},
            "str_key": None,
            None: "not-a-dict",
            (1, 2): {"lines": [{"product_id": "401201", "qty_sales_units": 5}]},
            2: 123,
            3: ["not-a-dict"],
            4: {"lines": None},
            5: {"lines": "not-a-list"},
            6: {"lines": 42},
            7: {"lines": {"product_id": "464306", "qty_sales_units": 50}},
            8: {"lines": ({"product_id": "464306", "qty_sales_units": 50},)},
            9: {
                "lines": [
                    None,
                    "not-a-dict",
                    123,
                    ["nested"],
                    True,
                    {"product_id": "464306", "qty_sales_units": 2.5},
                ]
            },
        },
        plan={"lines": "also-not-a-list"},
    )
    out = _collect_plan_lines(stub)
    assert out == [
        {"product_id": "464306", "qty_sales_units": 12.5, "tier": "best"},
        {"product_id": "401201", "qty_sales_units": 5},
    ]

    bool_key_stub = SimpleNamespace(
        needs=[cancelled_need],
        plans={False: {"lines": [{"product_id": "401201", "qty_sales_units": 3}]}},
        plan=None,
    )
    assert _collect_plan_lines(bool_key_stub) == [{"product_id": "401201", "qty_sales_units": 3}]

    fallback_bad_lines = SimpleNamespace(plans="not-a-dict", plan={"lines": "not-a-list"}, needs=None)
    assert _collect_plan_lines(fallback_bad_lines) == []


@pytest.mark.parametrize(
    "bad_qty",
    [
        True,
        False,
        0,
        0.0,
        -1,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        "10",
        "abc",
        [10],
        {"qty": 10},
        10**400,
    ],
)
def test_collect_plan_lines_rejects_bool_nan_inf_non_positive_and_non_numeric_qty(bad_qty: Any) -> None:
    stub = SimpleNamespace(
        plans={0: {"lines": [{"product_id": "464306", "qty_sales_units": bad_qty}]}},
        plan=None,
        needs=[],
    )
    assert _collect_plan_lines(stub) == []


@pytest.mark.parametrize("bad_pid", [None, True, False, "", "   ", float("nan"), ["464306"], {"id": "464306"}])
def test_collect_plan_lines_rejects_invalid_product_ids_and_overflow_sums(bad_pid: Any) -> None:
    stub = SimpleNamespace(
        plans={0: {"lines": [{"product_id": bad_pid, "qty_sales_units": 10}]}},
        plan=None,
        needs=[],
    )
    assert _collect_plan_lines(stub) == []

    overflow_stub = SimpleNamespace(
        plans={
            0: {"lines": [{"product_id": "464306", "qty_sales_units": 1e308, "tier": True}]},
            1: {"lines": [{"product_id": "464306", "qty_sales_units": 1e308}]},
        },
        plan=None,
        needs=[],
    )
    res = _collect_plan_lines(overflow_stub)
    assert res == [{"product_id": "464306", "qty_sales_units": 1e308}]


def test_detail_candidate_ids_guards_non_list_candidates_non_dict_entries_and_non_dict_product() -> None:
    assert _detail_candidate_ids(None) == []
    assert _detail_candidate_ids(SimpleNamespace(plans="bad", plan="bad", need="bad")) == []

    stub_fallback_to_plans = SimpleNamespace(
        plan={"candidates": "not-a-list", "lines": "not-a-list"},
        plans={
            "bad": "not-a-dict",
            0: {"candidates": 123, "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
            1: {
                "candidates": [
                    None,
                    "not-a-dict",
                    42,
                    ["list-cand"],
                    True,
                    {"product": None, "product_id": "469363", "tier": "better"},
                    {"product": "not-a-dict", "id": "462775", "tier": "good"},
                    {"product": 999, "product_id": True, "id": "   "},
                    {"product": {"id": True}, "product_id": "464306", "tier": "best"},
                    {"product": ["bad"], "product_id": "463038", "tier": "rejected"},
                ]
            },
        },
        need=SimpleNamespace(preferred_product_ids=[True, None, "  ", "463038"]),
        needs=[],
    )
    assert _detail_candidate_ids(stub_fallback_to_plans) == ["469363", "464306"]

    stub_bad_need_prefs = SimpleNamespace(
        plan={"candidates": {"id": "469363"}, "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
        plans={0: {"candidates": None, "lines": [{"product_id": "464306", "qty_sales_units": 5}]}},
        need={"preferred_product_ids": "464306"},
        needs=[],
    )
    assert _detail_candidate_ids(stub_bad_need_prefs) == ["464306"]


class _BrokenIterable:
    def __iter__(self):
        raise RuntimeError("broken iterator")


@pytest.mark.parametrize(
    "bad_contents",
    [
        None,
        42,
        3.14,
        True,
        False,
        object(),
        "get_product",
        b"get_product",
        bytearray(b"x"),
        {"parts": []},
        _BrokenIterable(),
    ],
)
def test_inspected_products_and_tool_errors_guard_non_iterable_contents(bad_contents: Any) -> None:
    assert _inspected_products(bad_contents) == set()
    assert _tool_errors(bad_contents) == []


def test_inspected_products_guards_none_entries_bad_parts_and_non_dict_fc_args() -> None:
    contents = [
        None,
        42,
        "raw-str",
        {"parts": "dict-entry"},
        SimpleNamespace(parts=None),
        SimpleNamespace(parts=123),
        SimpleNamespace(parts="str-parts"),
        SimpleNamespace(parts={"k": "v"}),
        SimpleNamespace(parts=_BrokenIterable()),
        SimpleNamespace(
            parts=[
                None,
                99,
                "part-str",
                SimpleNamespace(function_call="not-an-obj", function_response=123),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args=None)),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args="product_id=111")),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args=["222"])),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args=123)),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args=True)),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args={"product_id": True})),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args={"product_id": "   "})),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product")),
                SimpleNamespace(function_call=SimpleNamespace(name="get_product", args={"product_id": " 469363 "})),
                SimpleNamespace(function_response=SimpleNamespace(name="get_product", response="not-a-dict")),
                SimpleNamespace(function_response=SimpleNamespace(name="get_product", response=None)),
                SimpleNamespace(function_response=SimpleNamespace(name="get_product")),
                SimpleNamespace(function_response=SimpleNamespace(name="get_product", response={"id": True})),
                SimpleNamespace(
                    function_response=SimpleNamespace(name="get_product", response={"product_id": "464306"})
                ),
            ]
        ),
    ]
    assert _inspected_products(contents) == {"469363", "464306"}


def test_tool_errors_guards_none_entries_non_objects_and_non_dict_responses() -> None:
    contents = [
        None,
        123,
        "str-entry",
        SimpleNamespace(parts=None),
        SimpleNamespace(parts=999),
        SimpleNamespace(parts="str-parts"),
        SimpleNamespace(
            parts=[
                None,
                "bad-part",
                42,
                SimpleNamespace(function_response="not-an-obj"),
                SimpleNamespace(function_response=SimpleNamespace(name="plan_need", response="not-a-dict")),
                SimpleNamespace(function_response=SimpleNamespace(name="plan_need", response=["list"])),
                SimpleNamespace(function_response=SimpleNamespace(name="plan_need")),
                SimpleNamespace(
                    function_response=SimpleNamespace(
                        name="plan_need",
                        response={"detail": "ok", "lines": []},
                    )
                ),
                SimpleNamespace(
                    function_response=SimpleNamespace(
                        name="create_quote",
                        response={"detail": "bad request"},
                    )
                ),
                SimpleNamespace(
                    function_response=SimpleNamespace(
                        name=None,
                        response={"isError": True},
                    )
                ),
            ]
        ),
    ]
    assert _tool_errors(contents) == ["create_quote: bad request", "unknown_tool: tool_error"]


@pytest.mark.parametrize(
    "bad_qty",
    [
        True,
        False,
        0,
        -3.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        "100",
        "ten",
        [100],
        {"q": 100},
        10**400,
    ],
)
def test_mock_model_call_guards_active_and_single_need_bad_quantities(bad_qty: Any) -> None:
    bad_need = _make_need(quantity=bad_qty)
    stub_active = SimpleNamespace(
        needs=[bad_need],
        need=bad_need,
        plans={},
        plan=None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    resp = MockModel(stub_active)("buyer.agent_step", MOCK_MODEL_NAME, _user_contents())
    part = resp.candidates[0].content.parts[0]
    assert part.function_call is None
    assert part.text.startswith(MOCK_LABEL)
    assert "non-positive quantity rejected" in part.text

    stub_single_only = SimpleNamespace(
        needs=[],
        need=bad_need,
        plans={},
        plan=None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    resp_single = MockModel(stub_single_only)("buyer.agent_step", MOCK_MODEL_NAME, _user_contents())
    part_single = resp_single.candidates[0].content.parts[0]
    assert part_single.function_call is None
    assert "non-positive quantity rejected" in part_single.text


@pytest.mark.parametrize("bad_plans", [None, [], ["bad"], "not-a-dict", 123, True])
def test_mock_model_call_guards_none_or_non_dict_plans(bad_plans: Any) -> None:
    need = _make_need()
    stub = SimpleNamespace(
        needs=[need],
        need=need,
        plans=bad_plans,
        plan=None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    mm = MockModel(stub)
    step1 = mm("buyer.agent_step", MOCK_MODEL_NAME, _user_contents())
    fc1 = step1.candidates[0].content.parts[0].function_call
    assert fc1 is not None and fc1.name == "plan_need"

    after_plan_contents = [
        types.Content(role="user", parts=[types.Part(text="run")]),
        types.Content(role="user", parts=[_fn_resp_part("plan_need", {"lines": []})]),
    ]
    step2 = mm("buyer.agent_step", MOCK_MODEL_NAME, after_plan_contents)
    part2 = step2.candidates[0].content.parts[0]
    assert part2.function_call is None
    assert part2.text.startswith(MOCK_LABEL)


@pytest.mark.parametrize(
    "bad_quote",
    [
        {},
        {"quote_id": "Q-1"},
        {"quote_hash": "hash-1"},
        {"quote_id": "Q-1", "quote_hash": None},
        {"quote_id": None, "quote_hash": "hash-1"},
        {"quote_id": "", "quote_hash": "hash-1"},
        {"quote_id": "   ", "quote_hash": "hash-1"},
        {"quote_id": "Q-1", "quote_hash": "   "},
        {"quote_id": True, "quote_hash": "hash-1"},
        {"quote_id": "Q-1", "quote_hash": True},
        {"quote_id": 123, "quote_hash": "hash-1"},
        "not-a-dict",
        123,
        ["Q-1"],
    ],
)
def test_mock_model_call_guards_quote_missing_quote_id_or_quote_hash_before_place_order(
    bad_quote: Any,
) -> None:
    need = _make_need()
    stub = SimpleNamespace(
        needs=[need],
        need=need,
        plans={0: {"lines": [{"product_id": "464306", "qty_sales_units": 100.0}], "shortfall": 0.0}},
        plan=None,
        quote=bad_quote,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    contents = [
        types.Content(role="user", parts=[types.Part(text="run")]),
        types.Content(
            role="user",
            parts=[
                _fn_resp_part("plan_need", stub.plans[0]),
                _fn_resp_part("create_quote", {"quote_id": "Q-1"}),
            ],
        ),
    ]
    resp = MockModel(stub)("buyer.agent_step", MOCK_MODEL_NAME, contents)
    part = resp.candidates[0].content.parts[0]
    assert part.function_call is None
    assert part.text.startswith(MOCK_LABEL)

    stub.quote = {"quote_id": " Q-valid ", "quote_hash": " sha256-valid "}
    resp_ok = MockModel(stub)("buyer.agent_step", MOCK_MODEL_NAME, contents)
    fc_ok = resp_ok.candidates[0].content.parts[0].function_call
    assert fc_ok is not None
    assert fc_ok.name == "place_order"
    assert fc_ok.args == {"quote_id": "Q-valid", "quote_hash": "sha256-valid"}


@pytest.mark.parametrize(
    ("deliveries", "fallback_delivery", "expected_iso"),
    [
        ([date(2026, 11, 18)], None, "2026-11-18"),
        (["2026-11-17"], None, "2026-11-17"),
        (["  2026-11-19T14:30:00+02:00  "], None, "2026-11-19"),
        ([datetime(2026, 11, 20, 8, 0, tzinfo=HELSINKI)], None, "2026-11-20"),
        ([None], None, None),
        (["not-an-iso-date"], None, None),
        (["2026-02-30"], None, None),
        ([True], None, None),
        ([date(2026, 11, 19), "2026-11-17", None], None, "2026-11-17"),
        ([None, datetime(2026, 11, 18, 9, 0), "2026-11-20"], None, "2026-11-18"),
        ([], "2026-11-16", "2026-11-16"),
        ([], date(2026, 11, 16), "2026-11-16"),
        ([], None, None),
    ],
)
def test_mock_model_call_guards_delivery_by_date_iso_string_datetime_and_none(
    deliveries: list[Any],
    fallback_delivery: Any,
    expected_iso: str | None,
) -> None:
    needs = [_make_need(delivery_by=d) for d in deliveries]
    plans = (
        {i: {"lines": [{"product_id": "464306", "qty_sales_units": 10.0}]} for i in range(len(needs))}
        if needs
        else {}
    )
    fallback_need = _make_need(delivery_by=fallback_delivery) if fallback_delivery is not None or not needs else needs[0]
    stub = SimpleNamespace(
        needs=needs,
        need=fallback_need if not needs else needs[0],
        plans=plans,
        plan={"lines": [{"product_id": "464306", "qty_sales_units": 10.0}]} if not needs else None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    plan_resps = [_fn_resp_part("plan_need", {"lines": [{"product_id": "464306", "qty_sales_units": 10.0}]}) for _ in needs]
    contents = [types.Content(role="user", parts=[types.Part(text="run"), *plan_resps])]
    resp = MockModel(stub)("buyer.agent_step", MOCK_MODEL_NAME, contents)
    fc = resp.candidates[0].content.parts[0].function_call
    assert fc is not None
    assert fc.name == "create_quote"
    assert fc.args["delivery_date"] == expected_iso


def test_mock_model_call_handles_malformed_contents_non_dict_order_and_none_agent() -> None:
    resp_none_agent = MockModel(None)("buyer.agent_step", MOCK_MODEL_NAME, 12345)
    assert resp_none_agent.candidates[0].content.parts[0].text.startswith(MOCK_LABEL)

    stub_weird = SimpleNamespace(
        needs="not-a-list",
        need={"quantity": 10, "delivery_by": "2026-11-16", "raw_text": 999},
        plans=None,
        plan={"lines": [{"product_id": "464306", "qty_sales_units": 10}]},
        quote=None,
        order="not-a-dict",
        blocked=False,
        drift=None,
        injections=[],
    )
    weird_contents = [
        None,
        "bad",
        SimpleNamespace(parts=123),
        SimpleNamespace(parts=[None, SimpleNamespace(function_response="bad")]),
    ]
    resp_weird = MockModel(stub_weird)("buyer.agent_step", MOCK_MODEL_NAME, weird_contents)
    fc = resp_weird.candidates[0].content.parts[0].function_call
    assert fc is not None and fc.name == "create_quote"
    assert fc.args["delivery_date"] == "2026-11-16"


@pytest.mark.parametrize(
    ("bad_total_net", "expected_eur"),
    [
        (None, "EUR 0.00"),
        (True, "EUR 0.00"),
        (False, "EUR 0.00"),
        (float("nan"), "EUR 0.00"),
        (float("inf"), "EUR 0.00"),
        (float("-inf"), "EUR 0.00"),
        ("12.34", "EUR 0.00"),
        ("not-a-number", "EUR 0.00"),
        ([12.34], "EUR 0.00"),
        ({"v": 12.34}, "EUR 0.00"),
        (10**400, "EUR 0.00"),
        (0, "EUR 0.00"),
        (123.456, "EUR 123.46"),
    ],
)
def test_mock_model_summary_guards_total_net_none_bool_nan_inf_and_non_numeric(
    bad_total_net: Any,
    expected_eur: str,
) -> None:
    stub = SimpleNamespace(
        needs=[],
        need=None,
        plans={},
        plan=None,
        quote={"quote_id": "Q-net", "total_net": bad_total_net},
        order={"order_id": "O-net", "status": "placed"},
        blocked=False,
        drift=None,
        injections=[],
    )
    summary = MockModel(stub)._summary("buyer.summary")
    assert summary.startswith(MOCK_LABEL)
    assert f"quote Q-net {expected_eur}" in summary
    assert "nan" not in summary.lower()
    assert "inf" not in summary.lower()

    stub_missing_key = SimpleNamespace(
        needs=[],
        need=None,
        plans={},
        plan=None,
        quote={"quote_id": "Q-missing"},
        order={"order_id": "O-net", "status": "placed"},
        blocked=False,
        drift=None,
        injections=[],
    )
    assert "quote Q-missing EUR 0.00" in MockModel(stub_missing_key)._summary("buyer.summary")


@pytest.mark.parametrize(
    "bad_shortfall",
    [
        None,
        True,
        False,
        0,
        -5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "10",
        "bad",
        [10],
        {"sf": 10},
        10**400,
    ],
)
def test_mock_model_summary_guards_shortfall_bool_nan_inf_and_non_numeric(bad_shortfall: Any) -> None:
    stub_single = SimpleNamespace(
        needs=[],
        need=None,
        plans="not-a-dict",
        plan={"lines": [{"product_id": "464306", "qty_sales_units": 10}], "shortfall": bad_shortfall},
        quote={"quote_id": "Q-sf", "total_net": 10.1},
        order={"order_id": "O-sf", "status": "placed"},
        blocked=False,
        drift=None,
        injections=[],
    )
    s1 = MockModel(stub_single)._summary("buyer.summary")
    assert "shortfall" not in s1

    stub_mixed_plans = SimpleNamespace(
        needs=[],
        need=None,
        plans={
            0: {"shortfall": 12.5, "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
            1: {"shortfall": bad_shortfall, "lines": []},
            "bad": "not-a-dict",
            None: None,
        },
        plan="not-a-dict",
        quote={"quote_id": "Q-sf2", "total_net": 10.1},
        order={"order_id": "O-sf2", "status": "placed"},
        blocked=False,
        drift=None,
        injections=[],
    )
    s2 = MockModel(stub_mixed_plans)._summary("buyer.summary")
    assert "shortfall 12.5" in s2


def test_mock_model_summary_guards_broken_callbacks_and_malformed_order_fields() -> None:
    def broken_req_flags() -> list[str]:
        raise RuntimeError("boom")

    stub = SimpleNamespace(
        needs=[SimpleNamespace(preferences="bad", quantity="bad")],
        need=None,
        plans={},
        plan=None,
        quote="not-a-dict",
        order={"status": True, "approved_by": 123},
        blocked=False,
        drift={"ratio": 12.0},
        injections=[],
        _request_injection_flags=broken_req_flags,
    )
    mm = MockModel(stub)
    mm._last_errors = "not-a-list"
    summary = mm._summary("buyer.summary")
    assert summary.startswith(MOCK_LABEL)
    assert "status no_order" in summary
    assert "non-positive quantity rejected" in summary
    assert MockModel(stub)._summary("buyer.explain").startswith(MOCK_LABEL)


def test_keyless_buyer_agent_happy_e2e_regression() -> None:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.01, approval_timeout_s=2.0, now=SAT)
    res = agent.run("We need 100 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
    assert res["mock"] is True
    assert agent.model == MOCK_MODEL_NAME
    assert res["status"] == "placed"
    assert res["summary"].startswith(MOCK_LABEL)
