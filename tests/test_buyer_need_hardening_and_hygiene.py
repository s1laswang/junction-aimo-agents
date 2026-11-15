from __future__ import annotations

import ast
from datetime import date, datetime, timezone
import io
import json
import math
from pathlib import Path
import tokenize
from typing import Any

from pydantic import ValidationError
import pytest

from aimo import config, gemini
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.need import (
    HELSINKI,
    _to_number,
    apply_profile,
    detect_allergens,
    detect_claims,
    fallback_parse,
    fallback_parse_items,
    guess_query,
    parse_need,
    parse_quantity,
    parse_request,
    resolve_date,
    split_items,
)
from aimo.buyer.tools import FakeTransport
from aimo.models import Need

ROOT = Path(__file__).resolve().parents[1]
NEED_PATH = ROOT / "aimo" / "buyer" / "need.py"
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
        raise AssertionError("Live Gemini calls are forbidden in unit tests")

    monkeypatch.setattr(gemini, "generate", forbid_live_gemini)
    monkeypatch.setattr(gemini, "embed", forbid_live_gemini)


@pytest.mark.parametrize("target", [NEED_PATH, THIS_PATH], ids=lambda p: p.name)
def test_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
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


@pytest.mark.parametrize(
    "bad_iso_text",
    [
        "2026-02-30",
        "2026-00-10",
        "2026-13-01",
        "2026-13-45",
        "2026-02-29",
        "2026-04-31",
        "0000-01-01",
        "Deliver by 2026-02-30 please",
        "Toimitus 2026-13-01 mennessä",
        "Leverans senast 2026-00-10",
    ],
)
def test_resolve_date_invalid_iso_dates_do_not_crash_and_fall_through(bad_iso_text: str) -> None:
    assert resolve_date(bad_iso_text, SAT) is None
    need = parse_need(f"100 L lactose-free milk {bad_iso_text}", "hotel-aurora", SAT, use_gemini=False)
    assert need.delivery_by == date(2026, 11, 16)
    assert need.preferences.get("delivery_date_assumed") is True


@pytest.mark.parametrize(
    "text,expected_date",
    [
        ("Ref 2026-02-30, deliver by 2026-11-20", date(2026, 11, 20)),
        ("Batch 2026-13-01, deliver by Monday", date(2026, 11, 16)),
        ("Viite 2026-00-10, toimitus keskiviikoksi", date(2026, 11, 18)),
        ("Order 2026-02-30, leverans till fredag", date(2026, 11, 20)),
        ("Code 99.99.2026, toimitus 18.11.", date(2026, 11, 18)),
        ("Batch 31 November 2026, deliver by 19 November", date(2026, 11, 19)),
        ("Leap year delivery 2028-02-29", date(2028, 2, 29)),
        ("by 2026-11-22", date(2026, 11, 22)),
        ("by 21st November", date(2026, 11, 21)),
        ("16. marraskuuta mennessä", date(2026, 11, 16)),
        ("till 16:e november", date(2026, 11, 16)),
        ("day after tomorrow", date(2026, 11, 16)),
        ("ylihuomenna", date(2026, 11, 16)),
        ("i övermorgon", date(2026, 11, 16)),
        ("tomorrow", date(2026, 11, 15)),
        ("huomenna", date(2026, 11, 15)),
        ("imorgon", date(2026, 11, 15)),
        ("today", date(2026, 11, 14)),
        ("tänään", date(2026, 11, 14)),
        ("idag", date(2026, 11, 14)),
        ("next week", date(2026, 11, 16)),
        ("ensi viikolla", date(2026, 11, 16)),
        ("nästa vecka", date(2026, 11, 16)),
    ],
)
def test_resolve_date_fallthrough_and_valid_en_fi_sv_dates(text: str, expected_date: date) -> None:
    res = resolve_date(text, SAT)
    assert res is not None
    assert res[0] == expected_date


def test_resolve_date_accepts_naive_datetime_date_and_utc() -> None:
    naive_sat = datetime(2026, 11, 14, 15, 0)
    utc_sat = datetime(2026, 11, 14, 13, 0, tzinfo=timezone.utc)
    only_date = date(2026, 11, 14)
    for ref in (naive_sat, utc_sat, only_date):
        res = resolve_date("by Monday", ref)
        assert res is not None
        assert res[0] == date(2026, 11, 16)


@pytest.mark.parametrize(
    "text,expected_qty,expected_unit",
    [
        ("0 L", 0.0, "L"),
        ("0,0 kg", 0.0, "KG"),
        ("0.0 pcs", 0.0, "PCS"),
        ("-0 L", 0.0, "L"),
        ("0 dl", 0.0, "L"),
        ("0 ml", 0.0, "L"),
        ("0 g", 0.0, "KG"),
        ("-5 pcs", -5.0, "PCS"),
        ("-2,5 kg", -2.5, "KG"),
        ("-1,000 L", -1000.0, "L"),
        ("1,000 litres", 1000.0, "L"),
        ("1 000 litraa", 1000.0, "L"),
        ("1\u00a0000 liter", 1000.0, "L"),
        ("1.000,5 liter", 1000.5, "L"),
        ("1,000.5 litres", 1000.5, "L"),
        ("2,5 kg", 2.5, "KG"),
        ("500 g", 0.5, "KG"),
        ("250 ml", 0.25, "L"),
        ("5 dl", 0.5, "L"),
        ("24 kpl", 24.0, "PCS"),
        ("12 förp", 12.0, "PCS"),
    ],
)
def test_parse_quantity_preserves_zero_negative_decimal_and_thousands(
    text: str,
    expected_qty: float,
    expected_unit: str,
) -> None:
    got = parse_quantity(text)
    assert got is not None
    assert math.isfinite(got[0])
    assert got[0] == pytest.approx(expected_qty)
    assert got[1] == expected_unit


def test_to_number_and_parse_quantity_overflow_and_nonfinite_guards() -> None:
    huge_digits = "9" * 400
    for bad in (huge_digits, "-" + huge_digits, 10**400, True, False, None, "", "   ", "nan", "inf", "-inf", "abc"):
        val = _to_number(bad)
        assert not math.isfinite(val)

    assert parse_quantity(f"{huge_digits} L") is None
    assert parse_quantity(f"-{huge_digits} kg") is None
    recovered = parse_quantity(f"ref {huge_digits} L, order 25 L milk")
    assert recovered == (25.0, "L")


@pytest.mark.parametrize(
    "bad_qty_text",
    [
        "0 L",
        "0,0 kg",
        "-5 pcs",
        "0 L lactose-free milk by Monday",
        "0,0 kg voita maanantaiksi",
        "-5 pcs ruisleipää keskiviikoksi",
        "40 L cream and 0,0 kg butter by Monday",
        "40 L cream\n0 kg butter",
        "40 L cream\n-5 pcs bread",
    ],
)
def test_zero_and_negative_quantities_rejected_in_parse_and_buyer_preplan(bad_qty_text: str) -> None:
    with pytest.raises(ValidationError):
        parse_need(bad_qty_text, "hotel-aurora", SAT, use_gemini=False)
    with pytest.raises(ValidationError):
        parse_request(bad_qty_text, "hotel-aurora", SAT, use_gemini=False)
    with pytest.raises(ValidationError):
        fallback_parse(bad_qty_text, "hotel-aurora", SAT)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.01, now=SAT).run(bad_qty_text)
    assert res["status"] == "error"
    assert gw.calls == []


def test_multiline_valid_order_splits_all_lines() -> None:
    needs = parse_request("40 L lactose-free cream\n20 kg lactose-free butter\nby Monday", "hotel-aurora", SAT, use_gemini=False)
    assert [(n.product_query, n.quantity, n.unit) for n in needs] == [
        ("lactose-free cream", 40.0, "L"),
        ("lactose-free butter", 20.0, "KG"),
    ]
    assert all(n.delivery_by == date(2026, 11, 16) for n in needs)


@pytest.mark.parametrize("weird_text", [None, "", "   \t\n  ", ",,, ;;; ---", 0, False, True, 42, [], {}])
def test_non_string_and_empty_inputs_across_all_need_functions(weird_text: Any) -> None:
    pq = parse_quantity(weird_text)
    assert pq is None
    rd = resolve_date(weird_text, SAT)
    assert rd is None
    assert detect_claims(weird_text) == []
    assert detect_allergens(weird_text) == []
    assert split_items(weird_text) == []

    gq = guess_query(weird_text)
    assert isinstance(gq, str) and len(gq.strip()) > 0

    fb_items = fallback_parse_items(weird_text, "hotel-aurora", SAT)
    assert len(fb_items) == 1
    assert fb_items[0].product_query.strip() != ""
    assert fb_items[0].quantity == 1.0
    assert fb_items[0].unit == "PCS"

    fb_one = fallback_parse(weird_text, "hotel-aurora", SAT)
    assert fb_one.product_query.strip() != ""

    req_items = parse_request(weird_text, "hotel-aurora", SAT, use_gemini=False)
    assert len(req_items) == 1
    assert req_items[0].product_query.strip() != ""

    one_need = parse_need(weird_text, "hotel-aurora", SAT, use_gemini=False)
    assert one_need.product_query.strip() != ""


def test_extremely_long_text_caps_product_query_length() -> None:
    long_word = "kaurajuoma" * 120
    need = parse_need(long_word, "hotel-aurora", SAT, use_gemini=False)
    assert 0 < len(need.product_query) <= 500


@pytest.mark.parametrize(
    "bad_profile",
    [
        {},
        {"customer_id": None},
        {"customer_id": "   ", "policy_id": "school-tampere"},
        {
            "excluded_allergens": None,
            "preferred_product_ids": None,
            "preferences": None,
            "standing_order": None,
            "usual_match": None,
        },
        {
            "customer_id": "custom-1",
            "excluded_allergens": "Milk",
            "preferred_product_ids": "463038",
            "preferences": {"brand": "Valio"},
            "usual_match": "milk",
            "standing_order": {"qty_per_week": 0, "unit": "L"},
        },
        {
            "customer_id": "custom-2",
            "excluded_allergens": ["nuts", None, "  ", 99],
            "preferred_product_ids": [463038, None, "", True],
            "preferences": {"brand": None},
            "usual_match": "milk",
            "standing_order": {"qty_per_week": "0", "unit": "L"},
        },
        {
            "customer_id": "custom-3",
            "preferred_product_ids": ["463038"],
            "usual_match": "milk",
            "preferences": "not-a-dict",
            "standing_order": {"qty_per_week": "not-a-number", "unit": "L"},
        },
        {
            "customer_id": "custom-4",
            "preferred_product_ids": ["463038"],
            "usual_match": "[",
            "preferences": {"brand": 123},
            "standing_order": {"qty_per_week": -50, "unit": "L"},
        },
        {
            "customer_id": "custom-5",
            "preferred_product_ids": ["463038"],
            "usual_match": "milk",
            "standing_order": {"qty_per_week": True, "unit": "L"},
        },
        {
            "customer_id": "custom-6",
            "preferred_product_ids": ["463038"],
            "usual_match": "milk",
            "standing_order": {"qty_per_week": float("nan"), "unit": "L"},
        },
        {
            "customer_id": "custom-7",
            "preferred_product_ids": ["463038"],
            "usual_match": "milk",
            "standing_order": {"qty_per_week": float("inf"), "unit": "L"},
        },
        {
            "customer_id": "custom-8",
            "preferred_product_ids": ["463038"],
            "usual_match": "milk",
            "standing_order": "300 L per week",
        },
    ],
)
def test_malformed_profile_dicts_never_crash_apply_profile_or_parse_request(bad_profile: dict[str, Any]) -> None:
    needs = parse_request("300 L lactose-free milk by Monday", bad_profile, SAT, use_gemini=False)
    assert len(needs) == 1
    need = needs[0]
    assert isinstance(need.customer_id, str) and need.customer_id.strip() != ""
    assert "standing_order_ratio" not in need.preferences

    butter_need = parse_need("20 kg butter by Monday", bad_profile, SAT, use_gemini=False)
    assert butter_need.unit == "KG"

    non_dict_applied = apply_profile(need, "invalid-profile-object")
    assert non_dict_applied == need


def test_custom_customer_non_string_and_standing_order_valid_ratio() -> None:
    for raw_customer in (None, "", "   ", False, 0):
        need = parse_need("50 L oat drink by Wednesday", raw_customer, SAT, use_gemini=False)
        assert need.customer_id == "unknown"

    valid_prof = {
        "policy_id": "hotel-custom",
        "excluded_allergens": ["Milk", "nuts"],
        "preferred_product_ids": ["463038"],
        "usual_match": "milk",
        "preferences": {"brand": "Valio"},
        "standing_order": {"qty_per_week": "150", "unit": "L"},
    }
    need_valid = parse_need("300 L lactose-free milk by Monday", valid_prof, SAT, use_gemini=False)
    assert need_valid.customer_id == "hotel-custom"
    assert set(need_valid.excluded_allergens) == {"milk", "nuts"}
    assert need_valid.preferred_product_ids == ["463038"]
    assert need_valid.preferences["standing_order_ratio"] == 2.0


@pytest.mark.parametrize("failure_mode", ["quota_exc", "bad_json", "empty_items", "none_text"])
def test_gemini_parse_fallback_on_stubbed_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    calls: list[str] = []

    class _StubResp:
        def __init__(self, text: str | None) -> None:
            self.text = text

    def stub_generate(feature: str, model: str, contents: Any, config: Any = None, **kwargs: Any) -> Any:
        calls.append(feature)
        if failure_mode == "quota_exc":
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        if failure_mode == "bad_json":
            return _StubResp("{not-valid-json")
        if failure_mode == "empty_items":
            return _StubResp('{"items": []}')
        return _StubResp(None)

    monkeypatch.setattr(gemini, "generate", stub_generate)
    custom_prof = {"name": None, "segment": None, "excluded_allergens": None}
    needs = parse_request(
        "We need 120 litres of oat drink for Wednesday lunch, pupils have milk and nut allergies.",
        custom_prof,
        SAT,
        use_gemini=True,
    )
    assert len(needs) == 1
    need = needs[0]
    assert need.preferences["parsed_by"] == "regex"
    assert "gemini_error" in need.preferences
    assert need.customer_id == "unknown"
    assert need.product_query == "oat drink"
    assert need.quantity == 120.0
    assert need.unit == "L"
    assert need.delivery_by == date(2026, 11, 18)
    assert set(need.excluded_allergens) == {"milk", "nuts"}
    expected_calls = 1 if failure_mode in ("quota_exc", "empty_items") else 2
    assert len(calls) == expected_calls


def test_gemini_parse_cross_checks_date_quantity_claims_and_allergens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubResp:
        text = json.dumps(
            {
                "items": [
                    {
                        "product_query": "   ",
                        "required_claims": [],
                        "excluded_allergens": [],
                        "quantity": 50,
                        "unit": "PCS",
                        "delivery_by": "2026-02-30",
                        "delivery_expression": "by Monday",
                        "site": "Hotel Aurora",
                        "notes": "dispenser pack",
                    }
                ]
            }
        )

    monkeypatch.setattr(gemini, "generate", lambda *args, **kwargs: _StubResp())
    need = parse_need(
        "We need 200 litres of lactose-free milk by Monday, milk allergy guest.",
        "hotel-aurora",
        SAT,
        use_gemini=True,
    )
    assert isinstance(need, Need)
    assert need.preferences["parsed_by"] == "gemini"
    assert need.product_query == "lactose-free milk"
    assert need.quantity == 200.0
    assert need.unit == "L"
    assert "quantity_note" in need.preferences
    assert need.delivery_by == date(2026, 11, 16)
    assert "FREE_FROM_LACTOSE" in need.required_claims
    assert "milk" in need.excluded_allergens
    assert need.preferences["site"] == "Hotel Aurora"
    assert need.preferences["notes"] == "dispenser pack"
