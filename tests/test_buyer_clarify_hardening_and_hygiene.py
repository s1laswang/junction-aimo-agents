from __future__ import annotations

import ast
from datetime import date, datetime
import io
import math
from pathlib import Path
import random
import tokenize
from typing import Any

import pytest

from aimo import config, gemini
from aimo.buyer import clarify
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL
from aimo.buyer.need import HELSINKI, parse_request
from aimo.buyer.profiles import get_profile
from aimo.buyer.tools import FakeTransport
from aimo.models import Need, ProductSummary

ROOT = Path(__file__).resolve().parents[1]
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)

OWNED_FILES = (
    ROOT / "aimo" / "buyer" / "clarify.py",
    Path(__file__).resolve(),
)


@pytest.fixture(autouse=True)
def keyless_forbid_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    gemini.client.cache_clear()

    def forbid_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("aimo.gemini.generate must never be called in keyless tests")

    def forbid_embed(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("aimo.gemini.embed must never be called in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_generate)
    monkeypatch.setattr("aimo.gemini.embed", forbid_embed)


def _make_need(
    product_query: str = "oat drink",
    quantity: float = 50.0,
    unit: str = "L",
    delivery_by: date = date(2026, 11, 18),
    customer_id: str = "cafe-x",
    excluded_allergens: list[str] | None = None,
    required_claims: list[str] | None = None,
    preferred_product_ids: list[str] | None = None,
    preferences: dict[str, Any] | None = None,
) -> Need:
    return Need(
        raw_text=f"{quantity} {unit} {product_query}",
        customer_id=customer_id,
        product_query=product_query,
        required_claims=list(required_claims or []),
        excluded_allergens=list(excluded_allergens or []),
        quantity=quantity,
        unit=unit,
        delivery_by=delivery_by,
        preferred_product_ids=list(preferred_product_ids or []),
        preferences=dict(preferences or {}),
    )


@pytest.mark.parametrize("target", OWNED_FILES, ids=lambda p: p.name)
def test_owned_files_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    ast.parse(src, filename=str(target))
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has {len(comments)} comment token(s)"
    assert chr(35) not in src, f"{target.name} contains hash character"
    assert chr(0x2014) not in src, f"{target.name} contains em dash"
    assert chr(0x2013) not in src, f"{target.name} contains en dash"


@pytest.mark.parametrize(
    "bad_input",
    [None, True, False, 0, 1, -1, 1.5, float("nan"), float("inf"), float("-inf"), [], {}, (), set(), b"milk", object()],
)
def test_variant_key_and_conflict_base_non_string_guards(bad_input: Any) -> None:
    assert clarify.variant_key(bad_input) is None
    assert clarify._conflict_base(bad_input) == "drink"


def test_variant_key_and_conflict_base_valid_mappings() -> None:
    assert clarify.variant_key("") is None
    assert clarify.variant_key("   ") is None
    assert clarify.variant_key("Oatly Oat Drink") is None
    assert clarify.variant_key("Valio Maito 1,5 %") == "1.5 % fat"
    assert clarify.variant_key("Valio Kevytmaito") == "semi-skimmed (1.5 %)"
    assert clarify.variant_key("Arla Rasvaton maito") == "fat-free / skimmed"
    assert clarify.variant_key("Valio Täysmaito") == "whole (3.5 %)"
    assert clarify.variant_key("Valio Kuohukerma") == "whipping cream"
    assert clarify.variant_key("Valio Ruokakerma") == "cooking cream"
    assert clarify.variant_key("Light milk drink") == "low-fat"
    assert clarify.variant_key("Valio Hapankerma") == "sour"
    assert clarify.variant_key("Oatly iKaffe Barista") == "barista"

    assert clarify._conflict_base("") == "drink"
    assert clarify._conflict_base("oat item") == "drink"
    assert clarify._conflict_base("lactose-free milk") == "drink"
    assert clarify._conflict_base("buttermilk") == "drink"
    assert clarify._conflict_base("rasvaton piimä") == "drink"
    assert clarify._conflict_base("whipping cream") == "cream"
    assert clarify._conflict_base("vispgrädde") == "cream"
    assert clarify._conflict_base("kuohukerma") == "cream"
    assert clarify._conflict_base("maustamaton jogurtti") == "yogurt"
    assert clarify._conflict_base("maitorahka") == "yogurt"
    assert clarify._conflict_base("kvarg") == "yogurt"
    assert clarify._conflict_base("suolaton voi") == "butter"
    assert clarify._conflict_base("smör") == "butter"
    assert clarify._conflict_base("emmental juusto") == "cheese"
    assert clarify._conflict_base("kermajuusto") == "cheese"
    assert clarify._conflict_base("ost") == "cheese"


@pytest.mark.parametrize(
    "bad_amount",
    [True, False, 0, 0.0, -0.0, -1, -2.5, float("nan"), float("inf"), float("-inf"), 10**400, None, "1", "1.5", [], {}],
)
def test_fmt_pack_rejects_bool_non_finite_non_positive_and_bad_units(bad_amount: Any) -> None:
    assert clarify._fmt_pack({"content_amount": bad_amount, "content_unit": "L"}) == "pack size unknown"


def test_fmt_pack_non_dict_and_valid_packs() -> None:
    for non_dict in (None, True, False, 123, "1 L", [], ("L", 1)):
        assert clarify._fmt_pack(non_dict) == "pack size unknown"
    for bad_unit in (None, "", "ML", "G", "LITRE", True, False, 1, []):
        assert clarify._fmt_pack({"content_amount": 1.0, "content_unit": bad_unit}) == "pack size unknown"
    assert clarify._fmt_pack({"content_amount": 1.0, "content_unit": "L"}) == "1 L"
    assert clarify._fmt_pack({"content_amount": 1.5, "content_unit": "L"}) == "1.5 L"
    assert clarify._fmt_pack({"content_amount": 0.5, "content_unit": "KG"}) == "0.5 KG"
    assert clarify._fmt_pack({"content_amount": 12, "content_unit": "PCS"}) == "12 PCS"


def test_products_and_call_search_malformed_inputs_and_exceptions() -> None:
    def boom_search(_q: str, _c: list[str]) -> Any:
        raise RuntimeError("catalog offline")

    assert clarify._call_search(None, "milk", []) == []
    assert clarify._call_search("not-callable", "milk", []) == []
    assert clarify._call_search(boom_search, "milk", []) == []

    for bad_res in (None, True, 42, "bad", {"products": None}, {"products": 123}, {"items": []}):
        assert clarify._products(bad_res) == []

    pydantic_prod = ProductSummary(
        id="470863",
        name="Oatly Oat Drink Semi 1L",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["VEGAN"],
    )

    class BrokenDump:
        def model_dump(self) -> dict[str, Any]:
            raise ValueError("broken dump")

    mixed = {
        "products": [
            None,
            "raw",
            123,
            BrokenDump(),
            {},
            {"id": None, "name": "Missing id"},
            {"id": "   ", "name": "Blank id"},
            {"id": True, "name": "Bool id"},
            {"id": 463038, "name": "Int id"},
            {
                "id": "  p_malformed_fields  ",
                "name": None,
                "claims": ["VEGAN", 123, None],
                "content_amount": True,
                "content_unit": "LITRES",
                "brand": 999,
            },
            {
                "id": "p_nan",
                "content_amount": float("nan"),
                "content_unit": "L",
            },
            {
                "id": "p_inf",
                "content_amount": float("inf"),
                "content_unit": "KG",
            },
            pydantic_prod,
        ]
    }
    cleaned = clarify._products(mixed)
    assert [c["id"] for c in cleaned] == ["p_malformed_fields", "p_nan", "p_inf", "470863"]
    assert cleaned[0]["name"] == ""
    assert cleaned[0]["claims"] == ["VEGAN"]
    assert cleaned[0]["content_amount"] is None
    assert cleaned[0]["content_unit"] is None
    assert cleaned[0]["brand"] == ""
    assert cleaned[1]["content_amount"] is None
    assert cleaned[2]["content_amount"] is None
    assert cleaned[3]["content_amount"] == 1.0
    assert cleaned[3]["content_unit"] == "L"


def test_find_questions_guards_bad_needs_now_text_and_non_positive_quantities() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    for bad_needs in (None, [], (), "needs", 123, [None, "bad", 42]):
        assert clarify.find_questions(bad_needs, prof, "We need milk next week.", SAT, search_fn) == []

    for bad_qty in (0, 0.0, -5.0, True, False, float("nan"), float("inf"), float("-inf"), "10", None):
        bad_need = Need.model_construct(
            raw_text="bad",
            customer_id="cafe-x",
            product_query="milk",
            required_claims=[],
            excluded_allergens=[],
            quantity=bad_qty,
            unit="L",
            delivery_by=date(2026, 11, 16),
            preferred_product_ids=[],
            preferences={},
        )
        assert clarify.find_questions([bad_need], prof, "We need milk next week.", SAT, search_fn) == []

    flagged_need = _make_need(preferences={"non_positive_quantity": True, "delivery_date_assumed": True})
    assert clarify.find_questions([flagged_need], prof, "We need oat drink next week.", SAT, search_fn) == []

    assumed_need = _make_need(preferences={"delivery_date_assumed": True})
    for now_arg in (None, "2026-11-14", 12345, date(2026, 11, 14), date.max, datetime.max):
        qs = clarify.find_questions([assumed_need], None, None, now_arg, None)
        assert len(qs) == 1
        assert qs[0]["question_id"] == "q_date"
        assert len(qs[0]["options"]) == 3
        for opt in qs[0]["options"]:
            parsed = date.fromisoformat(opt["patch"]["delivery_by"])
            assert isinstance(parsed, date)

    weird_need = Need.model_construct(
        raw_text="",
        customer_id="cafe-x",
        product_query="   ",
        required_claims=None,
        excluded_allergens=None,
        quantity=10.0,
        unit="INVALID",
        delivery_by=date(2026, 11, 18),
        preferred_product_ids=None,
        preferences=None,
    )
    assert clarify.find_questions([weird_need], None, 999, SAT, None) == []


@pytest.mark.parametrize(
    "bad_profile",
    [
        None,
        "not-a-dict",
        ["list"],
        42,
        {"standing_order": None, "usual_match": "milk"},
        {"standing_order": "weekly", "usual_match": "milk"},
        {"standing_order": {"qty_per_week": True, "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": False, "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": 0, "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": -20, "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": float("nan"), "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": float("inf"), "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": "lots", "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": 300, "unit": "GALLONS"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": 300, "unit": None}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": 300, "unit": "L"}, "usual_match": "[unclosed("},
        {"standing_order": {"qty_per_week": 300, "unit": "L"}, "usual_match": "*invalid"},
        {"standing_order": {"qty_per_week": 300, "unit": "L"}, "usual_match": 12345},
        {"standing_order": {"qty_per_week": 300, "unit": "L"}, "usual_match": ""},
    ],
)
def test_find_questions_standing_order_and_invalid_regex_usual_match_guards(bad_profile: Any) -> None:
    mq_need = _make_need(product_query="lactose-free milk", quantity=1.0, unit="PCS")
    qs = clarify.find_questions([mq_need], bad_profile, "We need lactose-free milk by Monday.", SAT, lambda _q, _c: [])
    assert len(qs) == 1
    assert qs[0]["question_id"] == "q_unit_0"
    assert [o["patch"] for o in qs[0]["options"]] == [
        {"quantity": 10.0, "unit": "L"},
        {"quantity": 50.0, "unit": "L"},
        {"quantity": 100.0, "unit": "L"},
    ]
    assert all("usual weekly order" not in o["label"] for o in qs[0]["options"])


def test_find_questions_search_exceptions_and_malformed_catalog_hits() -> None:
    def raising_search(_q: str, _c: list[str]) -> Any:
        raise ConnectionError("boom")

    conflict_need = _make_need(
        product_query="butter",
        quantity=1.0,
        unit="PCS",
        customer_id="school-tampere",
        excluded_allergens=["milk"],
    )
    qs_conf_err = clarify.find_questions([conflict_need], None, "We need butter by Wednesday.", SAT, raising_search)
    assert len(qs_conf_err) == 1
    assert qs_conf_err[0]["type"] == "conflict"
    assert qs_conf_err[0]["options"] == [{"id": "skip", "label": "Leave out the butter", "patch": {"cancel": True}}]

    def malformed_conflict_search(q: str, _c: list[str]) -> Any:
        if q.startswith("oat"):
            return "not-a-list"
        return [
            {"name": "Vegan Spread Missing ID", "claims": ["VEGAN"]},
            {"id": "v_spread_1", "claims": ["VEGAN"], "content_unit": "KG", "content_amount": True},
            {"id": "v_spread_2", "name": "   ", "claims": ["VEGAN"], "content_unit": "KG", "content_amount": float("nan")},
        ]

    qs_conf_alt = clarify.find_questions([conflict_need], None, "We need butter by Wednesday.", SAT, malformed_conflict_search)
    assert len(qs_conf_alt) == 1
    opts_alt = qs_conf_alt[0]["options"]
    assert opts_alt[0]["product_id"] == "v_spread_1"
    assert opts_alt[0]["label"] == "Plant-based instead: v_spread_1"
    assert opts_alt[0]["patch"]["quantity"] == 10.0
    assert opts_alt[0]["patch"]["unit"] == "KG"
    assert opts_alt[0]["patch"]["product_query"] == "vegan butter"

    pack_need = _make_need(product_query="milk", quantity=20.0, unit="PCS")
    assert clarify.find_questions([pack_need], None, "We need 20 packs of milk by Monday.", SAT, raising_search) == []

    def malformed_pack_search(_q: str, _c: list[str]) -> Any:
        return [
            None,
            {"id": "", "content_amount": 1.0, "content_unit": "L"},
            {"id": "bad_bool", "content_amount": True, "content_unit": "L"},
            {"id": "bad_zero", "content_amount": 0.0, "content_unit": "L"},
            {"id": "bad_neg", "content_amount": -1.0, "content_unit": "L"},
            {"id": "bad_nan", "content_amount": float("nan"), "content_unit": "L"},
            {"id": "bad_inf", "content_amount": float("inf"), "content_unit": "L"},
            {"id": "bad_overflow", "content_amount": 1e308, "content_unit": "L"},
            {"id": "good_1l", "content_amount": 1.0, "content_unit": "L"},
            {"id": "good_10l", "name": "Bulk Milk 10L", "content_amount": 10.0, "content_unit": "L"},
        ]

    qs_pack = clarify.find_questions([pack_need], None, "We need 20 packs of milk by Monday.", SAT, malformed_pack_search)
    assert len(qs_pack) == 1
    assert [o["product_id"] for o in qs_pack[0]["options"]] == ["good_1l", "good_10l"]
    assert qs_pack[0]["options"][0]["label"] == "20 x 1 L: good_1l (= 20 L)"
    assert qs_pack[0]["options"][0]["patch"] == {"unit": "L", "quantity": 20.0, "preferred_product_ids": ["good_1l"]}
    assert qs_pack[0]["options"][1]["patch"] == {"unit": "L", "quantity": 200.0, "preferred_product_ids": ["good_10l"]}

    var_need = _make_need(product_query="lactose-free milk", quantity=50.0, unit="L", preferences={"brand": 12345})
    assert clarify.find_questions([var_need], None, "We need 50 litres of lactose-free milk by Monday.", SAT, raising_search) == []

    def malformed_variant_search(_q: str, _c: list[str]) -> Any:
        return [
            {"id": "no_name"},
            {"id": "v_skim", "name": "Fat-free milk 1L", "brand": None},
            {"id": "v_semi", "name": "Semi-skimmed milk 1L", "brand": ["bad"]},
        ]

    qs_var = clarify.find_questions([var_need], None, "We need 50 litres of lactose-free milk by Monday.", SAT, malformed_variant_search)
    assert len(qs_var) == 1
    assert qs_var[0]["type"] == "variant"
    assert {o["product_id"] for o in qs_var[0]["options"]} == {"v_skim", "v_semi"}


def test_resolve_idxs_guards_malformed_questions_and_item_index_types() -> None:
    n0 = _make_need(product_query="milk")
    n1 = _make_need(product_query="butter")
    needs = [n0, n1]

    assert clarify._resolve_idxs([], {"item_index": 0}) == []
    assert clarify._resolve_idxs(None, {"item_index": 0}) == []
    assert clarify._resolve_idxs(123, {"item_index": 0}) == []
    assert clarify._resolve_idxs(needs, None) == [0, 1]
    assert clarify._resolve_idxs(needs, "bad") == [0, 1]
    assert clarify._resolve_idxs(needs, {"item_index": None}) == [0, 1]

    for bad_idx in (True, False, "1", 1.5, float("nan"), [], {}):
        assert clarify._resolve_idxs(needs, {"item_index": bad_idx}) == [0]
        assert clarify._resolve_idxs(
            needs,
            {"item_index": bad_idx, "question": "How much butter do you need?"},
        ) == [1]

    assert clarify._resolve_idxs(
        needs,
        {"item_index": 0, "question": "How much butter do you need?"},
    ) == [1]
    assert clarify._resolve_idxs(needs, {"item_index": -10}) == [0]
    assert clarify._resolve_idxs(needs, {"item_index": 99}) == [1]


def test_apply_answer_non_dict_missing_fields_and_option_bool_id_traps() -> None:
    need = _make_need(product_query="oat drink", quantity=10.0, unit="L")
    valid_q = {
        "question_id": "q_unit_0",
        "type": "unit",
        "item_index": 0,
        "question": "How much oat drink do you need?",
        "options": [{"id": "q0", "label": "50 L", "patch": {"quantity": 50.0, "unit": "L"}}],
    }

    for bad_q in (None, "", 123, True, [], ()):
        assert clarify.apply_answer([need], bad_q, {"option_id": "q0"}) == ([need], "")
    for bad_ans in (None, {}, "", 123, True, [], ()):
        assert clarify.apply_answer([need], valid_q, bad_ans) == ([need], "")
    for bad_needs in (None, [], (), "bad", 123, [None, "not-a-need"]):
        assert clarify.apply_answer(bad_needs, valid_q, {"option_id": "q0"}) == ([], "")

    mixed_out, mixed_lbl = clarify.apply_answer(["garbage", need], valid_q, {"option_id": "q0"})
    assert mixed_lbl == "50 L"
    assert len(mixed_out) == 1
    assert mixed_out[0].quantity == 50.0

    missing_meta_q = {
        "question_id": None,
        "type": "unit",
        "item_index": True,
        "options": [
            None,
            "not-a-dict",
            {"label": "No ID", "patch": {"quantity": 99.0, "unit": "L"}},
            {"id": True, "label": "Bool True ID", "patch": {"quantity": 88.0, "unit": "L"}},
            {"id": False, "label": "Bool False ID", "patch": {"quantity": 77.0, "unit": "L"}},
            {"id": 1, "patch": {"quantity": 25.0, "unit": "L"}},
            {"id": "no_patch", "label": "Missing Patch"},
            {"id": "empty_patch", "label": "Empty Patch", "patch": {}},
        ],
    }
    assert clarify.apply_answer([need], missing_meta_q, {"option_id": True}) == ([need], "")
    assert clarify.apply_answer([need], missing_meta_q, {"option_id": False}) == ([need], "")
    assert clarify.apply_answer([need], missing_meta_q, {"option_id": 0}) == ([need], "")
    assert clarify.apply_answer([need], missing_meta_q, {"option_id": "no_patch"}) == ([need], "")
    assert clarify.apply_answer([need], missing_meta_q, {"option_id": "empty_patch"}) == ([need], "")

    int_id_out, int_id_lbl = clarify.apply_answer([need], missing_meta_q, {"option_id": 1})
    assert int_id_lbl == "1"
    assert int_id_out[0].quantity == 25.0
    assert int_id_out[0].preferences["clarified"] == ["q_clarify"]


def test_apply_answer_numeric_and_non_string_text_answers() -> None:
    need = _make_need(product_query="oat drink", quantity=1.0, unit="PCS")
    q_how_much = {
        "question_id": "q_unit_0",
        "type": "unit",
        "item_index": 0,
        "question": "How much oat drink do you need?",
        "options": [
            {"id": "q0", "label": "10 L", "patch": {"quantity": 10.0, "unit": "L"}},
            {"id": "q1", "label": "50 L", "patch": {"quantity": 50.0, "unit": "L"}},
            {"id": "q2", "label": "100 L", "patch": {"quantity": 100.0, "unit": "L"}},
        ],
    }

    out_50, lbl_50 = clarify.apply_answer([need], q_how_much, {"text": 50})
    assert lbl_50 == "50 L"
    assert out_50[0].quantity == 50.0
    assert out_50[0].unit == "L"

    out_75, lbl_75 = clarify.apply_answer([need], q_how_much, {"text": 75})
    assert lbl_75 == "free text: 75"
    assert out_75[0].quantity == 75.0
    assert out_75[0].unit == "L"

    out_float, lbl_float = clarify.apply_answer([need], q_how_much, {"text": 12.5})
    assert lbl_float == "free text: 12.5"
    assert out_float[0].quantity == 12.5
    assert out_float[0].unit == "L"

    out_large, lbl_large = clarify.apply_answer([need], q_how_much, {"text": 1234567})
    assert lbl_large == "free text: 1234567"
    assert out_large[0].quantity == 1234567.0
    assert out_large[0].unit == "L"

    for non_str in (["50"], {"qty": 50}, ("50",), object()):
        assert clarify.apply_answer([need], q_how_much, {"text": non_str}) == ([need], "")


@pytest.mark.parametrize(
    "bad_text_val",
    [
        True,
        False,
        0,
        0.0,
        -0.0,
        -1,
        -25.5,
        10**400,
        float("nan"),
        float("inf"),
        float("-inf"),
        "0",
        "0.0",
        "0,00",
        "+0",
        "-0",
        "-5",
        "- 5",
        "-0.25",
        "0 L",
        "-3 kg",
        "nan",
        "NaN",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "Infinity",
        "-Infinity",
        "1e500",
        "-1e3",
        "0e5",
        "1e-400",
    ],
)
def test_apply_answer_rejects_non_positive_bool_and_non_finite_text_quantities(bad_text_val: Any) -> None:
    need = _make_need(product_query="oat drink", quantity=20.0, unit="L")
    q_unit = {
        "question_id": "q_unit_0",
        "type": "unit",
        "item_index": 0,
        "question": "How much oat drink do you need?",
        "options": [{"id": "q0", "label": "10 L", "patch": {"quantity": 10.0, "unit": "L"}}],
    }
    out, lbl = clarify.apply_answer([need], q_unit, {"text": bad_text_val})
    assert "rejected non-positive quantity" in lbl
    assert len(out) == 1
    assert math.isfinite(out[0].quantity) and out[0].quantity > 0
    assert out[0].preferences.get("cancelled_by_clarification") is True
    assert out[0].preferences.get("non_positive_quantity") is True
    assert out[0].preferences.get("clarified") == ["q_unit_0"]


@pytest.mark.parametrize(
    "bad_patch_qty",
    [True, False, 0, 0.0, -1, -0.01, float("nan"), float("inf"), float("-inf"), 10**400, None, "abc", [], {}],
)
def test_apply_answer_rejects_non_positive_bool_and_non_finite_patch_quantities(bad_patch_qty: Any) -> None:
    need = _make_need(product_query="oat drink", quantity=20.0, unit="L")
    q_bad_patch = {
        "question_id": "q_unit_0",
        "type": "unit",
        "item_index": 0,
        "question": "How much oat drink do you need?",
        "options": [{"id": "bad", "label": "Bad Qty", "patch": {"quantity": bad_patch_qty, "unit": "L"}}],
    }
    out, lbl = clarify.apply_answer([need], q_bad_patch, {"option_id": "bad"})
    assert lbl == "rejected non-positive quantity"
    assert out[0].quantity == 20.0
    assert out[0].preferences.get("cancelled_by_clarification") is True
    assert out[0].preferences.get("non_positive_quantity") is True


def test_apply_answer_preserves_parenthesized_zero_percent_variant_and_date_ranges() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    prof = get_profile("cafe-x")
    var_text = "We need 50 litres of lactose-free milk by Monday."
    var_needs = parse_request(var_text, prof, SAT, use_gemini=False)
    q_var = clarify.find_questions(var_needs, prof, var_text, SAT, lambda q, c: gw.search_products(q, c))[0]

    for zero_pct_txt in ("(0%)", "fat-free (0 %)", "rasvaton (0,0%)"):
        updated, lbl = clarify.apply_answer(var_needs, q_var, {"text": zero_pct_txt})
        assert "rejected non-positive quantity" not in lbl
        assert lbl.startswith("fat-free")
        assert updated[0].preferred_product_ids == ["462775"]

    nw_text = "We need 50 litres of oat drink next week."
    nw_needs = parse_request(nw_text, prof, SAT, use_gemini=False)
    q_nw = clarify.find_questions(nw_needs, prof, nw_text, SAT, lambda q, c: gw.search_products(q, c))[0]
    d_range, lbl_range = clarify.apply_answer(nw_needs, q_nw, {"text": "16.11. - 18.11."})
    assert "rejected non-positive quantity" not in lbl_range
    assert d_range[0].delivery_by == date(2026, 11, 16)


def test_apply_answer_invalid_delivery_by_and_malformed_patch_fields() -> None:
    need = _make_need(
        product_query="oat drink",
        quantity=50.0,
        unit="L",
        preferences={"delivery_date_assumed": True},
    )

    for bad_iso in ("not-a-date", "2026-13-40", "2026-02-30", "", "   ", None, True, 20261116, [], {}):
        q_bad_date = {
            "question_id": "q_date",
            "type": "date",
            "item_index": None,
            "question": "When do you need the delivery?",
            "options": [{"id": "d0", "label": "Bad Date", "patch": {"delivery_by": bad_iso}}],
        }
        assert clarify.apply_answer([need], q_bad_date, {"option_id": "d0"}) == ([need], "")

    q_nw_broken_opt = {
        "question_id": "q_date",
        "type": "date",
        "item_index": None,
        "question": "Which day next week should it arrive?",
        "options": [{"id": "d0", "label": "Broken", "patch": {"delivery_by": "0001-01-01"}}],
    }
    resolved_d, resolved_lbl = clarify.apply_answer([need], q_nw_broken_opt, {"text": "2026-11-20"})
    assert resolved_lbl == "free text: 2026-11-20"
    assert resolved_d[0].delivery_by == date(2026, 11, 20)
    assert "delivery_date_assumed" not in resolved_d[0].preferences

    bad_patches = [
        {"unit": "GALLONS"},
        {"unit": None},
        {"product_query": ""},
        {"product_query": "   "},
        {"product_query": 123},
        {"required_claims": "VEGAN"},
        {"preferred_product_ids": "470863"},
        {"preferred_product_ids": ["", "   ", 123]},
        {"unrecognized_key": 123},
    ]
    for bp in bad_patches:
        q_bp = {
            "question_id": "q_bad",
            "type": "variant",
            "item_index": 0,
            "options": [{"id": "o0", "label": "Bad", "patch": bp}],
        }
        assert clarify.apply_answer([need], q_bp, {"option_id": "o0"}) == ([need], "")

    q_false_cancel = {
        "question_id": "q_fc",
        "type": "unit",
        "item_index": 0,
        "options": [{"id": "o0", "label": "Not Cancelled", "patch": {"cancel": "false", "quantity": 30.0, "unit": "L"}}],
    }
    fc_out, fc_lbl = clarify.apply_answer([need], q_false_cancel, {"option_id": "o0"})
    assert fc_lbl == "Not Cancelled"
    assert fc_out[0].quantity == 30.0
    assert fc_out[0].preferences.get("cancelled_by_clarification") is not True

    weird_existing = Need.model_construct(
        raw_text="oat",
        customer_id="cafe-x",
        product_query="oat drink",
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        quantity=10.0,
        unit="L",
        delivery_by=date(2026, 11, 18),
        preferred_product_ids=12345,
        preferences="not-a-dict",
    )
    q_pref = {
        "question_id": "q_var",
        "type": "variant",
        "item_index": 0,
        "options": [
            {
                "id": "v0",
                "label": "Oat",
                "patch": {"preferred_product_ids": ["470863"], "required_claims": ["VEGAN", 999]},
            }
        ],
    }
    w_out, w_lbl = clarify.apply_answer([weird_existing], q_pref, {"option_id": "v0"})
    assert w_lbl == "Oat"
    assert w_out[0].preferred_product_ids == ["470863"]
    assert w_out[0].required_claims == ["VEGAN"]
    assert w_out[0].preferences == {"clarified": ["q_var"]}


def test_deterministic_fuzz_clarify_never_raises_or_produces_invalid_quantities() -> None:
    rng = random.Random(20261115)
    base_need = _make_need(product_query="milk", quantity=1.0, unit="PCS", excluded_allergens=["milk"])
    alt_need = _make_need(product_query="lactose-free milk", quantity=20.0, unit="PCS", preferences={"delivery_date_assumed": True})

    need_pool: list[Any] = [None, [], "bad", [None], [base_need], [alt_need], [base_need, alt_need]]
    profile_pool: list[Any] = [
        None,
        "bad",
        {},
        {"standing_order": {"qty_per_week": True, "unit": "L"}, "usual_match": "milk"},
        {"standing_order": {"qty_per_week": float("nan"), "unit": "L"}, "usual_match": "[bad"},
        {"standing_order": {"qty_per_week": 200, "unit": "L"}, "usual_match": "milk"},
    ]
    text_pool: list[Any] = [None, 123, True, "", "milk next week", "20 packs of milk by Monday", "milk"]
    now_pool: list[Any] = [None, "bad", SAT, SAT.date(), date.max]
    search_pool: list[Any] = [
        None,
        lambda _q, _c: (_ for _ in ()).throw(RuntimeError("err")),
        lambda _q, _c: "not-a-list",
        lambda _q, _c: [
            None,
            {},
            {"id": "p1", "content_amount": True, "content_unit": "L", "claims": ["VEGAN"]},
            {"id": "p2", "name": "1.5% milk", "content_amount": 1.0, "content_unit": "L"},
            {"id": "p3", "name": "0% milk", "content_amount": float("inf"), "content_unit": "L"},
        ],
    ]
    answer_pool: list[Any] = [
        None,
        "bad",
        {},
        {"option_id": None},
        {"option_id": True},
        {"option_id": "p0"},
        {"option_id": "d0"},
        {"option_id": "skip"},
        {"text": 50},
        {"text": 0},
        {"text": -10},
        {"text": True},
        {"text": float("nan")},
        {"text": float("inf")},
        {"text": "by Friday"},
        {"text": ["bad"]},
    ]

    for _ in range(250):
        ns = rng.choice(need_pool)
        pr = rng.choice(profile_pool)
        tx = rng.choice(text_pool)
        nw = rng.choice(now_pool)
        sr = rng.choice(search_pool)
        qs = clarify.find_questions(ns, pr, tx, nw, sr)
        assert isinstance(qs, list)
        assert len(qs) <= clarify.MAX_QUESTIONS
        q_target = qs[0] if qs else rng.choice([None, "bad", {}, {"options": [None, {"patch": {"delivery_by": "bad"}}]}])
        ans = rng.choice(answer_pool)
        updated, lbl = clarify.apply_answer(ns, q_target, ans)
        assert isinstance(updated, list)
        assert isinstance(lbl, str)
        for item in updated:
            assert isinstance(item, Need)
            assert not isinstance(item.quantity, bool)
            assert math.isfinite(item.quantity) and item.quantity > 0


def test_keyless_buyer_agent_with_numeric_and_non_finite_answers() -> None:
    gw_ok = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent_ok = BuyerAgent(
        FakeTransport(gw_ok),
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
        answer_fn=lambda _q: {"text": 40},
    )
    res_ok = agent_ok.run("We need lactose-free whipping cream by Monday.")
    assert res_ok["mock"] is True
    assert res_ok["status"] == "placed"
    assert res_ok["need"]["quantity"] == 40.0
    assert res_ok["need"]["unit"] == "L"
    assert res_ok["summary"].startswith(MOCK_LABEL)

    for bad_val in (0, -5, True, False, "true", "false", float("nan"), float("inf")):
        gw_bad = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
        agent_bad = BuyerAgent(
            FakeTransport(gw_bad),
            "hotel-aurora",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=lambda _q, v=bad_val: {"text": v},
        )
        res_bad = agent_bad.run("We need lactose-free whipping cream by Monday.")
        assert res_bad["mock"] is True
        assert res_bad["status"] == "no_order"
        assert res_bad["plan"] is None
        assert res_bad["quote"] is None
        assert res_bad["order"] is None
        assert "non-positive quantity rejected" in res_bad["summary"]
