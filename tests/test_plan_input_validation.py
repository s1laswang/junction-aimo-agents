"""Unit tests for Need and lot input validation in aimo.catalog.plan and aimo.catalog.substitution."""
from __future__ import annotations

import math
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import aimo.catalog.offers as offers_mod
import aimo.catalog.plan as plan_mod
import aimo.catalog.textsearch as textsearch_mod
from aimo.catalog import units as U
from aimo.catalog.offers import build_offer, fallback_get_offers, get_offers
from aimo.catalog.plan import _late_split, _round_lot, _trim, fill, plan_need
from aimo.catalog.substitution import price_per_unit, rank_substitutes, validate_need
from aimo.models import Candidate, Need, Offer, Plan, ProductSummary


DELIVERY = date(2026, 11, 16)


def _need(**overrides) -> Need:
    data = {
        "raw_text": "",
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 10.0,
        "unit": "L",
        "delivery_by": DELIVERY,
        "preferred_product_ids": [],
        "preferences": {},
    }
    data.update(overrides)
    return Need.model_construct(**data)


def _row(pid: str = "464306", unit: str = "L") -> dict:
    return {
        "id": pid,
        "name": "Valio Eila milk drink 1 l lactose free",
        "name_en": "Valio Eila milk drink 1 l lactose free",
        "name_fi": None,
        "brand": "Valio",
        "fat_pct": 1.5,
        "content_amount": 1.0,
        "content_unit": unit,
        "sales_unit": "ST",
        "sales_unit_size": 1,
        "base_content_amount": 1.0,
        "claims": ["FREE_FROM_LACTOSE"],
        "labels": [],
        "category": "10400",
        "allergens": {"milk": "contains"},
        "delivery_reliability": 0.98,
        "co2_kg_per_kg": 1.1,
    }


def _offer(pid: str = "464306", unit: str = "L", amount: float = 50.0) -> Offer:
    now = datetime.now(timezone.utc)
    return Offer(
        product_id=pid,
        requested_date=DELIVERY,
        available=True,
        available_amount=amount,
        cutoff_passed=False,
        closing_time=now + timedelta(days=1),
        logistic_model="Stock",
        unit_price=1.20,
        comparison_price=1.20,
        comparison_unit=unit,
        fetched_at=now,
    )


BAD_QUANTITIES = [
    0,
    0.0,
    -0.0,
    -1,
    -0.0001,
    -500.5,
    math.nan,
    math.inf,
    -math.inf,
    True,
    False,
    1_000_000.01,
    1_000_001,
    10**50,
    10**400,
    -(10**400),
    None,
    "10",
    [10],
]

BAD_QUERIES = [
    "",
    " ",
    "   \t\n  ",
    None,
    123,
    ["milk"],
    "m" * 501,
    "x" * 2000,
]

BAD_CUSTOMER_IDS = [
    "",
    " ",
    "   \t\n  ",
    "c" * 129,
    None,
    123,
    True,
    False,
    ["hotel-aurora"],
    {"id": "hotel-aurora"},
]

BAD_STR_SEQUENCES = [
    None,
    123,
    True,
    False,
    {},
    {"milk": "free"},
    "FREE_FROM_LACTOSE",
    {"milk"},
    [123],
    [None],
    [True],
    ["milk", 99],
    ("milk", None),
]

BAD_PREFERENCES = [
    None,
    ["x"],
    ("x",),
    123,
    "brand=Valio",
    True,
    False,
    {"x"},
]

BAD_UNITS = [
    "",
    "ML",
    "G",
    "l",
    "kg",
    "pcs",
    "BOX",
    None,
    1,
]

BAD_DATES = [
    None,
    "2026-11-16",
    20261116,
    datetime(2026, 11, 16, 0, 0),
    datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc),
]


@pytest.mark.parametrize("bad_qty", BAD_QUANTITIES)
def test_bad_quantity_rejected_before_search_offers_or_division(monkeypatch, bad_qty):
    calls: list[str] = []

    def fail_candidates(*_a, **_kw):
        calls.append("find_candidates")
        raise AssertionError("find_candidates must not be called for invalid need")

    def fail_offers(*_a, **_kw):
        calls.append("offers_fn")
        raise AssertionError("offers_fn must not be called for invalid need")

    monkeypatch.setattr(plan_mod, "find_candidates", fail_candidates)
    bad = _need(quantity=bad_qty)

    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=fail_offers)
    assert calls == []

    with pytest.raises(ValueError):
        fill(bad, [])

    rows = {"464306": _row()}
    offers = {"464306": _offer(amount=50.0)}
    with pytest.raises(ValueError):
        rank_substitutes(bad, offers, rows=rows, relevance={"464306": 1.0})

    with pytest.raises(ValueError):
        rank_substitutes(bad)
    assert calls == []


@pytest.mark.parametrize("bad_query", BAD_QUERIES)
def test_bad_product_query_rejected_at_top(monkeypatch, bad_query):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(product_query=bad_query)

    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))

    with pytest.raises(ValueError):
        fill(bad, [])

    with pytest.raises(ValueError):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


@pytest.mark.parametrize("bad_unit", BAD_UNITS)
def test_bad_unit_rejected_at_top(monkeypatch, bad_unit):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(unit=bad_unit)

    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))

    with pytest.raises(ValueError):
        fill(bad, [])

    with pytest.raises(ValueError):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


@pytest.mark.parametrize("bad_date", BAD_DATES)
def test_bad_delivery_by_rejected_at_top(monkeypatch, bad_date):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(delivery_by=bad_date)

    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))

    with pytest.raises(ValueError):
        fill(bad, [])

    with pytest.raises(ValueError):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


def test_none_need_rejected():
    with pytest.raises(ValueError):
        plan_need(None)
    with pytest.raises(ValueError):
        fill(None, [])
    with pytest.raises(ValueError):
        rank_substitutes(None)


@pytest.mark.parametrize("unit", ["L", "KG", "PCS"])
@pytest.mark.parametrize("qty", [0.001, 1, 10.5, 1_000_000, 1_000_000.0])
def test_valid_boundary_needs_plan_cleanly(monkeypatch, unit, qty):
    rows = {"464306": _row(unit=unit)}
    offers = {"464306": _offer(unit=unit, amount=2_000_000.0)}
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(plan_mod, "with_effective_query", lambda n: (n, "product_query"))
    monkeypatch.setattr(plan_mod, "find_candidates", lambda _n: (rows, {"464306": 1.0}))
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda ids: {i: rows[i] for i in ids if i in rows})

    valid = _need(quantity=qty, unit=unit)
    cands = rank_substitutes(valid, offers, rows=rows, relevance={"464306": 1.0})
    assert cands and cands[0].tier != "rejected"

    filled = fill(valid, cands)
    assert filled.covered_amount >= qty

    planned = plan_need(valid, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.covered_amount >= qty


@pytest.mark.parametrize("bad_units", [True, False, math.nan, math.inf, -math.inf, 10**400, None, "5"])
def test_round_lot_rejects_bool_and_non_finite(bad_units):
    with pytest.raises(ValueError):
        _round_lot(bad_units, 1.0)
    with pytest.raises(ValueError):
        _round_lot(bad_units, 6.0, up=False)


@pytest.mark.parametrize("non_positive", [0, 0.0, -0.0, -0.5, -1, -100.0])
@pytest.mark.parametrize("lot", [None, 0.01, 1.0, 6.0])
@pytest.mark.parametrize("up", [True, False])
def test_round_lot_returns_zero_for_non_positive_units(non_positive, lot, up):
    res = _round_lot(non_positive, lot, up=up)
    assert res == 0.0 and isinstance(res, float)


def test_round_lot_positive_units():
    assert _round_lot(10, 4.5) == pytest.approx(13.5)
    assert _round_lot(7, 6) == 12.0
    assert _round_lot(12, 6) == 12.0
    assert _round_lot(13, 6, up=False) == 12.0
    assert _round_lot(2.2, 0.01) == 3.0


def test_product_query_length_boundary():
    validate_need(_need(product_query="m" * 500))
    with pytest.raises(ValueError, match="need.product_query must be at most 500 characters"):
        validate_need(_need(product_query="m" * 501))


@pytest.mark.parametrize("bad_cid", BAD_CUSTOMER_IDS)
def test_bad_customer_id_rejected_at_top(monkeypatch, bad_cid):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(customer_id=bad_cid)

    with pytest.raises(ValueError, match="need.customer_id must be a non-empty string"):
        validate_need(bad)
    with pytest.raises(ValueError, match="need.customer_id must be a non-empty string"):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))
    with pytest.raises(ValueError, match="need.customer_id must be a non-empty string"):
        fill(bad, [])
    with pytest.raises(ValueError, match="need.customer_id must be a non-empty string"):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


def test_customer_id_max_length_boundary():
    validate_need(_need(customer_id="c" * 128))


@pytest.mark.parametrize("field_name", ["required_claims", "excluded_allergens", "preferred_product_ids"])
@pytest.mark.parametrize("bad_seq", BAD_STR_SEQUENCES)
def test_bad_string_sequence_fields_rejected_at_top(monkeypatch, field_name, bad_seq):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(**{field_name: bad_seq})

    with pytest.raises(ValueError):
        validate_need(bad)
    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))
    with pytest.raises(ValueError):
        fill(bad, [])
    with pytest.raises(ValueError):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


def test_tuple_string_sequences_accepted(monkeypatch):
    rows = {"464306": _row()}
    rows["464306"]["allergens"] = {"milk": "contains", "nuts": "free"}
    rows["464306"]["allergen_source"] = {"milk": "structured", "nuts": "structured"}
    offers = {"464306": _offer()}
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(plan_mod, "with_effective_query", lambda n: (n, "product_query"))
    monkeypatch.setattr(plan_mod, "find_candidates", lambda _n: (rows, {"464306": 1.0}))
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda ids: {i: rows[i] for i in ids if i in rows})

    valid = _need(
        required_claims=("FREE_FROM_LACTOSE",),
        excluded_allergens=("nuts",),
        preferred_product_ids=("464306",),
    )
    validate_need(valid)
    planned = plan_need(valid, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.covered_amount == 10.0


@pytest.mark.parametrize("bad_prefs", BAD_PREFERENCES)
def test_bad_preferences_rejected_at_top(monkeypatch, bad_prefs):
    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    bad = _need(preferences=bad_prefs)

    with pytest.raises(ValueError):
        validate_need(bad)
    with pytest.raises(ValueError):
        plan_need(bad, offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")))
    with pytest.raises(ValueError):
        fill(bad, [])
    with pytest.raises(ValueError):
        rank_substitutes(bad, {"464306": _offer()}, rows={"464306": _row()}, relevance={"464306": 1.0})


@pytest.mark.parametrize("bad_val", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None, "20", 10**400])
def test_snap_guards_non_positive_and_non_finite(bad_val):
    res = U._snap(bad_val)
    assert res == 0.0 and isinstance(res, float)


def test_snap_extreme_and_normal_positive_values():
    assert U._snap(19.94) == 20.0
    assert U._snap(4.98) == 5.0
    assert U._snap(0.333) == 0.33
    assert U._snap(0.123) == 0.123
    assert U._snap(1e-6) > 0.0
    assert math.isfinite(U._snap(1e308))
    assert math.isfinite(U._snap(sys.float_info.max))
    assert U._snap(sys.float_info.min) > 0.0


@pytest.mark.parametrize("bad_size", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None, "20", [20], 10**400])
def test_sales_unit_size_and_compute_content_guard_bad_inputs(monkeypatch, bad_size):
    raw_diff_unit = {
        "id": "x",
        "salesUnit": "KI",
        "baseUnit": "ST",
        "units": [{"unitId": "KI", "sizeInBaseUnits": bad_size}],
        "synkkaData": {
            "names": [{"language": "en", "value": "12 x 1 L milk"}],
            "unitConversions": [],
        },
    }
    assert U.sales_unit_size(raw_diff_unit) is None
    c = U.compute_content(raw_diff_unit)
    assert c.amount == 12.0
    assert c.unit == "L"
    assert c.base_amount == 12.0
    assert c.sales_unit_size == 1.0

    monkeypatch.setattr(U, "sales_unit_size", lambda _r: bad_size)
    c_patched = U.compute_content(raw_diff_unit)
    assert c_patched.amount == 12.0
    assert c_patched.base_amount == 12.0
    assert c_patched.sales_unit_size == 1.0


def test_compute_content_guards_non_dict_zero_names_and_overflow(monkeypatch):
    for non_dict in (None, 123, "raw", [1, 2]):
        assert U.sales_unit_size(non_dict) is None
        c = U.compute_content(non_dict)
        assert c.amount == 1.0 and c.unit == "PCS"

    zero_mp = {
        "id": "z",
        "salesUnit": "KI",
        "baseUnit": "ST",
        "units": [{"unitId": "KI", "sizeInBaseUnits": 6}],
        "synkkaData": {"names": [{"language": "en", "value": "12 x 0 L drink"}]},
    }
    cz = U.compute_content(zero_mp)
    assert cz.unit == "PCS" and cz.amount == 6.0

    bad_conv = {
        "id": "bc",
        "salesUnit": "ST",
        "baseUnit": "ST",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"language": "en", "value": "Valio milk 1 l"}],
            "unitConversions": [
                {
                    "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                    "volume": {"unit": "LTR", "value": -5.0},
                    "netWeight": {"unit": "KGM", "value": math.nan},
                }
            ],
        },
    }
    cbc = U.compute_content(bad_conv)
    assert cbc.amount == 1.0 and cbc.unit == "L"

    huge_mp = {
        "id": "ov",
        "salesUnit": "KI",
        "baseUnit": "ST",
        "synkkaData": {"names": [{"language": "en", "value": "2 x 1 L milk"}]},
    }
    monkeypatch.setattr(U, "sales_unit_size", lambda _r: 1e308)
    monkeypatch.setattr(U, "parse_name_quantity", lambda _n: (U.Qty(1e-308, "L"), 2))
    cov_under = U.compute_content(huge_mp)
    assert cov_under.base_amount is None


@pytest.mark.parametrize("bad_raw_text", [123, True, False, ["milk"], {"text": "milk"}])
def test_bad_raw_text_rejected_at_top(bad_raw_text):
    with pytest.raises(ValueError, match="need.raw_text must be a string"):
        validate_need(_need(raw_text=bad_raw_text))


@pytest.mark.parametrize("bad_price", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None, "1.5", 10**400])
def test_reconcile_with_price_guards_bad_unit_or_comparison_price(bad_price):
    assert U.reconcile_with_price(10.0, "L", bad_price, 1.5, "L") == (10.0, "L", None)
    assert U.reconcile_with_price(10.0, "L", 15.0, bad_price, "L") == (10.0, "L", None)
    assert U.reconcile_with_price(bad_price, "L", bad_price, 1.5, "L") == (None, "L", None)


@pytest.mark.parametrize("bad_content", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None, "10", 10**400])
def test_reconcile_with_price_guards_bad_static_content_and_derives_from_valid_price(bad_content):
    amt, unit, note = U.reconcile_with_price(bad_content, "L", 31.31, 1.57, "L")
    assert amt == 20.0
    assert unit == "L"
    assert note is not None


def test_reconcile_with_price_guards_division_overflow_and_underflow():
    assert U.reconcile_with_price(10.0, "L", 1e308, 1e-308, "L") == (10.0, "L", None)
    assert U.reconcile_with_price(10.0, "L", 1e-308, 1e308, "L") == (10.0, "L", None)


@pytest.mark.parametrize("bad_num", [0, 0.0, -0.0, -1.5, math.nan, math.inf, -math.inf, True, False, None, "1.2", 10**400])
def test_price_per_unit_guards_non_positive_and_non_finite(bad_num):
    base = _offer()
    bad_up = Offer.model_construct(**{**base.model_dump(), "unit_price": bad_num})
    assert price_per_unit(bad_up, 1.0, "L", "L") is None

    bad_cp = Offer.model_construct(**{**base.model_dump(), "comparison_price": bad_num})
    assert price_per_unit(bad_cp, 2.0, "L", "L") == pytest.approx(0.6)
    assert price_per_unit(bad_cp, bad_num, "L", "L") is None

    overflow_offer = base.model_copy(update={"unit_price": 1e308, "comparison_price": None})
    assert price_per_unit(overflow_offer, 1e-308, "L", "L") is None

    underflow_offer = base.model_copy(update={"unit_price": 1e-308, "comparison_price": None})
    assert price_per_unit(underflow_offer, 1e308, "L", "L") is None


def _patch_single_plan(monkeypatch, rows: dict):
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(plan_mod, "with_effective_query", lambda n: (n, "product_query"))
    monkeypatch.setattr(plan_mod, "find_candidates", lambda _n: (rows, {pid: 1.0 for pid in rows}))
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda ids: {i: rows[i] for i in ids if i in rows})


@pytest.mark.parametrize("bad_price", [0, 0.0, -0.0, -0.01, -5.0, math.nan, math.inf, -math.inf, True, False, "1.2", 10**400])
def test_corrupted_offer_unit_price_rejected_without_crash(monkeypatch, bad_price):
    rows = {"464306": _row()}
    bad_offer = Offer.model_construct(**{**_offer().model_dump(), "unit_price": bad_price})
    offers = {"464306": bad_offer}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert len(cands) == 1
    assert cands[0].tier == "rejected"
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.lines == []
    assert filled.covered_amount == 0.0
    assert filled.total_net == 0.0
    assert filled.shortfall == 10.0

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.lines == []
    assert planned.total_net == 0.0
    assert planned.shortfall == 10.0


@pytest.mark.parametrize("bad_cmp", [0, 0.0, -0.0, -1.5, math.nan, math.inf, -math.inf, True, False, None, "1.5", 10**400])
def test_corrupted_offer_comparison_price_falls_back_to_unit_price(monkeypatch, bad_cmp):
    rows = {"464306": _row()}
    offer = Offer.model_construct(**{**_offer().model_dump(), "unit_price": 1.5, "comparison_price": bad_cmp})
    offers = {"464306": offer}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert cands and cands[0].tier != "rejected"
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.covered_amount == 10.0
    assert filled.total_net == pytest.approx(15.0)

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.covered_amount == 10.0
    assert planned.total_net == pytest.approx(15.0)


@pytest.mark.parametrize("bad_avail", [0, 0.0, -0.0, -1.0, -500.0, math.nan, -math.inf, True, False, "50", -(10**400)])
def test_corrupted_offer_available_amount_rejected_cleanly(monkeypatch, bad_avail):
    rows = {"464306": _row()}
    offer = Offer.model_construct(**{**_offer().model_dump(), "available": True, "available_amount": bad_avail})
    offers = {"464306": offer}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert len(cands) == 1
    assert cands[0].tier == "rejected"
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.lines == []
    assert filled.shortfall == 10.0

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.lines == []
    assert planned.shortfall == 10.0


@pytest.mark.parametrize("huge_avail", [math.inf, 1e20, 10**400])
def test_infinite_or_huge_available_amount_plans_cleanly(monkeypatch, huge_avail):
    rows = {"464306": _row()}
    offer = Offer.model_construct(**{**_offer().model_dump(), "available_amount": huge_avail})
    offers = {"464306": offer}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert cands and cands[0].tier != "rejected"
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.covered_amount == 10.0
    assert math.isfinite(filled.total_net)

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.covered_amount == 10.0
    assert math.isfinite(planned.total_net)


@pytest.mark.parametrize("bad_content", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None])
def test_corrupted_row_content_amount_without_comparison_price_rejected_cleanly(monkeypatch, bad_content):
    r = _row()
    r["content_amount"] = bad_content
    rows = {"464306": r}
    offer = _offer().model_copy(update={"comparison_price": None})
    offers = {"464306": offer}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert len(cands) == 1
    assert cands[0].tier == "rejected"
    assert cands[0].product.content_amount is None
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.lines == []
    assert filled.shortfall == 10.0

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.lines == []
    assert planned.shortfall == 10.0


@pytest.mark.parametrize("bad_co2", [0, 0.0, -0.0, -2.5, math.nan, math.inf, -math.inf, None])
@pytest.mark.parametrize("bad_rel", [0.0, -0.5, 2.5, math.nan, math.inf, -math.inf, None])
def test_corrupted_co2_and_delivery_reliability_keep_finite_scores(monkeypatch, bad_co2, bad_rel):
    r = _row()
    r["co2_kg_per_kg"] = bad_co2
    r["delivery_reliability"] = bad_rel
    rows = {"464306": r}
    offers = {"464306": _offer()}
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=60.0)

    cands = rank_substitutes(need, offers, rows=rows, relevance={"464306": 1.0})
    assert cands and cands[0].tier != "rejected"
    assert math.isfinite(cands[0].rank_score)

    filled = fill(need, cands)
    assert filled.covered_amount == 50.0
    assert filled.shortfall == 10.0
    assert math.isfinite(filled.total_net)

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert planned.covered_amount == 50.0
    assert math.isfinite(planned.total_net)


def test_rank_substitutes_guards_score_division_overflow_underflow_and_bad_relevance():
    r_tiny = _row(pid="tiny-p")
    r_tiny["co2_kg_per_kg"] = 1e-308
    r_huge = _row(pid="huge-p")
    r_huge["co2_kg_per_kg"] = 1e308
    rows = {"tiny-p": r_tiny, "huge-p": r_huge}
    offers = {
        "tiny-p": _offer(pid="tiny-p").model_copy(update={"unit_price": 1e-308, "comparison_price": 1e-308}),
        "huge-p": _offer(pid="huge-p").model_copy(update={"unit_price": 1e308, "comparison_price": 1e308}),
    }
    need = _need(quantity=10.0)
    cands = rank_substitutes(need, offers, rows=rows, relevance={"tiny-p": math.nan, "huge-p": math.inf})
    assert len(cands) == 2
    assert all(math.isfinite(c.rank_score) for c in cands)


def test_healthy_candidate_wins_alongside_corrupted_candidates(monkeypatch):
    bad_row_content = _row(pid="bad-content")
    bad_row_content["content_amount"] = math.nan
    bad_row_price = _row(pid="bad-price")
    bad_row_avail = _row(pid="bad-avail")
    good_row = _row(pid="good-milk")
    good_row["co2_kg_per_kg"] = math.nan
    rows = {
        "bad-content": bad_row_content,
        "bad-price": bad_row_price,
        "bad-avail": bad_row_avail,
        "good-milk": good_row,
    }
    offers = {
        "bad-content": _offer(pid="bad-content").model_copy(update={"comparison_price": None}),
        "bad-price": Offer.model_construct(**{**_offer(pid="bad-price").model_dump(), "unit_price": -3.0}),
        "bad-avail": Offer.model_construct(**{**_offer(pid="bad-avail").model_dump(), "available_amount": -math.inf}),
        "good-milk": _offer(pid="good-milk", amount=25.0),
    }
    _patch_single_plan(monkeypatch, rows)
    need = _need(quantity=10.0)

    planned = plan_need(need, offers_fn=lambda _ids, _d, customer_id=None: offers)
    assert [(l.product_id, l.covered_amount) for l in planned.lines] == [("good-milk", 10.0)]
    assert planned.shortfall == 0.0
    assert planned.total_net == pytest.approx(12.0)


def _cand(
    pid: str = "464306",
    tier: str = "best",
    rank_score: object = 0.85,
    content_amount: object = 1.0,
    content_unit: str = "L",
    unit_price: object = 1.20,
    available_amount: object = 50.0,
    cutoff_passed: bool = False,
    available: bool = True,
    reasons: list[str] | None = None,
    flags: list[str] | None = None,
) -> Candidate:
    now = datetime.now(timezone.utc)
    prod = ProductSummary.model_construct(
        id=pid,
        name=f"Milk {pid}",
        sales_unit="ST",
        content_amount=content_amount,
        content_unit=content_unit,
        claims=["FREE_FROM_LACTOSE"],
        labels=[],
    )
    off = Offer.model_construct(
        product_id=pid,
        requested_date=DELIVERY,
        available=available,
        available_amount=available_amount,
        available_date=DELIVERY,
        closing_time=now - timedelta(days=2) if cutoff_passed else now + timedelta(days=1),
        cutoff_passed=cutoff_passed,
        logistic_model="Stock",
        sales_block=False,
        unit_price=unit_price,
        comparison_price=1.20,
        comparison_unit=content_unit,
        currency="EUR",
        fetched_at=now,
        issues=[],
    )
    return Candidate.model_construct(
        product=prod,
        offer=off,
        tier=tier,
        rank_score=rank_score,
        reasons=list(reasons) if reasons is not None else ["Exact match"],
        flags=list(flags) if flags is not None else [],
        injection_screen=None,
    )


@pytest.mark.parametrize("bad_limit", [0, -1, -100, True, False, 1.5, math.nan, math.inf, -math.inf, None, "12", [12]])
def test_trim_and_plan_need_reject_invalid_limit(monkeypatch, bad_limit):
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        _trim([], bad_limit)

    monkeypatch.setattr(
        plan_mod,
        "find_candidates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not search on bad limit")),
    )
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        plan_need(
            _need(quantity=10.0),
            offers_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch offers")),
            limit=bad_limit,
        )


def test_trim_respects_positive_integer_limits():
    ok_cands = [_cand(pid=f"ok-{i}", tier="best") for i in range(5)]
    rej_cands = [_cand(pid=f"rej-{i}", tier="rejected", reasons=["Rejected: out of stock"]) for i in range(10)]
    trimmed_1 = _trim([*ok_cands, *rej_cands], 1)
    assert [c.product.id for c in trimmed_1 if c.tier != "rejected"] == ["ok-0"]
    assert len([c for c in trimmed_1 if c.tier == "rejected"]) == 6

    trimmed_16 = _trim([*ok_cands, *rej_cands], 16)
    assert len([c for c in trimmed_16 if c.tier != "rejected"]) == 5
    assert len([c for c in trimmed_16 if c.tier == "rejected"]) == 8


@pytest.mark.parametrize("bad_content", [0, 0.0, -0.0, -5.0, math.nan, math.inf, -math.inf, True, False, None, "1.0", 10**400])
def test_fill_direct_skips_corrupted_content_amount(monkeypatch, bad_content):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0)
    plan = fill(need, [_cand(content_amount=bad_content)])
    assert plan.lines == []
    assert plan.covered_amount == 0.0
    assert plan.shortfall == 10.0


@pytest.mark.parametrize("bad_price", [0, 0.0, -0.0, -0.01, -5.0, math.nan, math.inf, -math.inf, True, False, None, "1.2", 10**400])
def test_fill_direct_skips_corrupted_unit_price(monkeypatch, bad_price):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0)
    plan = fill(need, [_cand(unit_price=bad_price)])
    assert plan.lines == []
    assert plan.total_net == 0.0
    assert plan.shortfall == 10.0


@pytest.mark.parametrize("bad_avail", [0, 0.0, -0.0, -1.0, -50.0, math.nan, -math.inf, True, False, "50", -(10**400)])
def test_fill_direct_skips_corrupted_or_non_positive_available_amount(monkeypatch, bad_avail):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0)
    plan = fill(need, [_cand(available_amount=bad_avail)])
    assert plan.lines == []
    assert plan.covered_amount == 0.0
    assert plan.shortfall == 10.0


@pytest.mark.parametrize("uncapped_avail", [None, math.inf, 10**400])
def test_fill_direct_treats_infinite_available_amount_as_uncapped_without_round_lot(monkeypatch, uncapped_avail):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {"464306": {"allowed_lot_size": 6.0}})
    seen_units: list[float] = []
    real_round_lot = plan_mod._round_lot

    def spy_round_lot(units, lot, up=True):
        seen_units.append(units)
        return real_round_lot(units, lot, up=up)

    monkeypatch.setattr(plan_mod, "_round_lot", spy_round_lot)
    need = _need(quantity=10.0)
    plan = fill(need, [_cand(available_amount=uncapped_avail)])
    assert len(plan.lines) == 1
    assert plan.lines[0].qty_sales_units == 12.0
    assert "partial_stock" not in plan.lines[0].flags
    assert seen_units == [10.0]


def test_fill_direct_caps_finite_partial_stock_with_lot_rounding(monkeypatch):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {"464306": {"allowed_lot_size": 6.0}})
    need = _need(quantity=20.0)
    plan = fill(need, [_cand(available_amount=14.0)])
    assert len(plan.lines) == 1
    assert plan.lines[0].qty_sales_units == 12.0
    assert "partial_stock" in plan.lines[0].flags
    assert plan.shortfall == 8.0


@pytest.mark.parametrize("bad_score", [math.nan, math.inf, -math.inf, True, False, None, "0.9", 10**400])
def test_fill_direct_sorts_corrupt_rank_score_behind_finite_candidates(monkeypatch, bad_score):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0)
    corrupt = _cand(pid="corrupt-score", rank_score=bad_score, available_amount=50.0)
    healthy = _cand(pid="healthy-score", rank_score=0.5, available_amount=50.0)
    plan = fill(need, [corrupt, healthy])
    assert [l.product_id for l in plan.lines] == ["healthy-score"]


def test_fill_direct_skips_overflowing_arithmetic(monkeypatch):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=1000.0)
    tiny_content = _cand(pid="tiny-c", content_amount=1e-308, unit_price=1.0)
    huge_price = _cand(pid="huge-p", content_amount=1e-5, unit_price=1e308)
    healthy = _cand(pid="ok-p", content_amount=1.0, unit_price=1.5)
    plan = fill(need, [tiny_content, huge_price, healthy])
    assert [l.product_id for l in plan.lines] == ["ok-p"]
    assert math.isfinite(plan.total_net)


def _empty_plan(need: Need) -> Plan:
    return fill(need, [])


@pytest.mark.parametrize("bad_field", ["content_amount", "unit_price", "available_amount"])
@pytest.mark.parametrize("bad_val", [0, 0.0, -0.0, -5.0, math.nan, -math.inf, True, False, "bad"])
def test_late_split_skips_corrupted_candidates_cleanly(monkeypatch, bad_field, bad_val):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0, preferences={"allow_split_delivery": True})
    base_plan = _empty_plan(need)
    cutoff_cand = _cand(
        pid="late-1",
        tier="rejected",
        cutoff_passed=True,
        available=False,
        reasons=["Rejected: order cut-off passed (Thu 14 Nov 15:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
    )
    forged_ranked = _cand(pid="late-1", tier="best", **{bad_field: bad_val})
    monkeypatch.setattr(plan_mod, "rank_substitutes", lambda *_a, **_kw: [forged_ranked])

    out_plan, notes = _late_split(need, base_plan, [cutoff_cand], {"late-1": _row("late-1")}, {"late-1": 1.0}, {})
    assert out_plan.lines == []
    assert notes == []


@pytest.mark.parametrize("bad_inf_field", ["content_amount", "unit_price"])
def test_late_split_skips_infinite_content_and_unit_price(monkeypatch, bad_inf_field):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0, preferences={"allow_split_delivery": True})
    base_plan = _empty_plan(need)
    cutoff_cand = _cand(
        pid="late-inf",
        tier="rejected",
        cutoff_passed=True,
        available=False,
        reasons=["Rejected: order cut-off passed (Thu 14 Nov 15:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
    )
    forged_ranked = _cand(pid="late-inf", tier="best", **{bad_inf_field: math.inf})
    monkeypatch.setattr(plan_mod, "rank_substitutes", lambda *_a, **_kw: [forged_ranked])

    out_plan, notes = _late_split(need, base_plan, [cutoff_cand], {"late-inf": _row("late-inf")}, {"late-inf": 1.0}, {})
    assert out_plan.lines == []
    assert notes == []


@pytest.mark.parametrize("uncapped_avail", [None, math.inf, 10**400])
def test_late_split_treats_infinite_available_amount_as_uncapped(monkeypatch, uncapped_avail):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {"late-ok": {"allowed_lot_size": 6.0}})
    need = _need(quantity=10.0, preferences={"allow_split_delivery": True})
    base_plan = _empty_plan(need)
    cutoff_cand = _cand(
        pid="late-ok",
        tier="rejected",
        cutoff_passed=True,
        available=False,
        available_amount=uncapped_avail,
        reasons=["Rejected: order cut-off passed (Thu 14 Nov 15:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
    )
    ranked_cand = _cand(pid="late-ok", tier="best", available_amount=uncapped_avail)
    monkeypatch.setattr(plan_mod, "rank_substitutes", lambda *_a, **_kw: [ranked_cand])

    out_plan, notes = _late_split(need, base_plan, [cutoff_cand], {"late-ok": _row("late-ok")}, {"late-ok": 1.0}, {})
    assert len(out_plan.lines) == 1
    assert out_plan.lines[0].qty_sales_units == 12.0
    assert out_plan.shortfall == 0.0
    assert notes


@pytest.mark.parametrize("bad_score", [math.nan, math.inf, -math.inf, True, False, None, "0.9", 10**400])
def test_late_split_sorts_corrupt_rank_score_behind_finite_candidates(monkeypatch, bad_score):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0, preferences={"allow_split_delivery": True})
    base_plan = _empty_plan(need)
    c1 = _cand(
        pid="late-corrupt",
        tier="rejected",
        cutoff_passed=True,
        available=False,
        reasons=["Rejected: order cut-off passed (Thu 14 Nov 15:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
    )
    c2 = _cand(
        pid="late-healthy",
        tier="rejected",
        cutoff_passed=True,
        available=False,
        reasons=["Rejected: order cut-off passed (Thu 14 Nov 15:00) for delivery Mon 16 Nov"],
        flags=["cutoff_passed"],
    )
    r_corrupt = _cand(pid="late-corrupt", tier="best", rank_score=bad_score)
    r_healthy = _cand(pid="late-healthy", tier="best", rank_score=0.6)
    monkeypatch.setattr(plan_mod, "rank_substitutes", lambda *_a, **_kw: [r_corrupt, r_healthy])

    out_plan, _ = _late_split(
        need,
        base_plan,
        [c1, c2],
        {"late-corrupt": _row("late-corrupt"), "late-healthy": _row("late-healthy")},
        {"late-corrupt": 1.0, "late-healthy": 1.0},
        {},
    )
    assert [l.product_id for l in out_plan.lines] == ["late-healthy"]


@pytest.mark.parametrize("bad_sf", [math.nan, math.inf, -math.inf, -1.0, True, False, None, "10", 10**400])
def test_late_split_rejects_corrupt_shortfall(monkeypatch, bad_sf):
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda _ids: {})
    need = _need(quantity=10.0)
    bad_plan = Plan.model_construct(
        need=need,
        lines=[],
        candidates=[],
        covered_amount=0.0,
        shortfall=bad_sf,
        total_net=0.0,
        explanation="",
    )
    with pytest.raises(ValueError):
        _late_split(need, bad_plan, [], {}, {}, {})


def _valid_price_payload(unit_price: object = 1.25, comparison_price: object = 1.25) -> dict:
    return {
        "productId": "464306",
        "catalogPrice": {"value": unit_price, "valueWithTax": 1.425, "currency": "EUR", "validTo": "2026-12-01T00:00:00+0200"},
        "listPrice": {"value": 1.30},
        "comparisonPrice": {"value": comparison_price},
        "comparisonUnit": "L",
        "tax": {"taxRate": 14.0},
    }


def _valid_avail_payload(amount: object = 50.0, avail_date: object = "2026-11-16") -> dict:
    return {
        "productId": "464306",
        "logisticModel": "Stock",
        "salesBlock": False,
        "closingTime": "2026-11-15T15:00:00+0200",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": amount, "availableDate": avail_date, "salesBlock": False}],
    }


@pytest.mark.parametrize("bad_price", [0, 0.0, -0.0, -1.5, math.nan, math.inf, -math.inf, True, False, None, "1.2", 10**400])
def test_build_offer_guards_bad_unit_price(bad_price):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    offer = build_offer("464306", DELIVERY, _valid_price_payload(unit_price=bad_price), _valid_avail_payload(50.0), now)
    assert offer.unit_price is None
    assert offer.available is False
    assert "price_error" in offer.issues


@pytest.mark.parametrize("bad_cmp", [0, 0.0, -0.0, -1.5, math.nan, math.inf, -math.inf, True, False, None, "1.2", 10**400])
def test_build_offer_guards_bad_comparison_and_secondary_prices(bad_cmp):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    pr = _valid_price_payload(unit_price=1.25, comparison_price=bad_cmp)
    pr["catalogPrice"]["valueWithTax"] = bad_cmp
    pr["listPrice"]["value"] = bad_cmp
    if bad_cmp not in (0, 0.0, -0.0):
        pr["tax"]["taxRate"] = bad_cmp
    offer = build_offer("464306", DELIVERY, pr, _valid_avail_payload(50.0), now)
    assert offer.unit_price == pytest.approx(1.25)
    assert offer.comparison_price is None
    assert offer.unit_price_gross is None
    assert offer.list_price is None
    if bad_cmp not in (0, 0.0, -0.0):
        assert offer.tax_rate is None
    assert offer.available is True


@pytest.mark.parametrize("bad_avail", [-0.01, -5.0, math.nan, math.inf, -math.inf, True, False, "50", 10**400, -(10**400)])
def test_build_offer_guards_corrupt_available_amount(bad_avail):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    offer = build_offer("464306", DELIVERY, _valid_price_payload(1.25), _valid_avail_payload(amount=bad_avail), now)
    assert offer.available_amount is None
    assert offer.available is False
    assert "availability_error" in offer.issues


@pytest.mark.parametrize("zero_avail", [0, 0.0, -0.0])
def test_build_offer_zero_available_amount_marks_out_of_stock(zero_avail):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    offer = build_offer("464306", DELIVERY, _valid_price_payload(1.25), _valid_avail_payload(amount=zero_avail), now)
    assert offer.available_amount == 0.0
    assert offer.available is False
    assert "out_of_stock" in offer.issues


def test_build_offer_guards_corrupt_schedule_dates_and_naive_timestamps():
    now_naive = datetime(2026, 11, 14, 12, 0)
    bad_date_offer = build_offer(
        "464306",
        DELIVERY,
        _valid_price_payload(1.25),
        _valid_avail_payload(amount=20.0, avail_date="not-a-date"),
        now_naive,
    )
    assert bad_date_offer.available_date is None
    assert bad_date_offer.available is False
    assert "availability_error" in bad_date_offer.issues


@pytest.mark.parametrize("bad_date", [None, "", "bad-date", "2026-11-16", 20261116, True, False, datetime(2026, 11, 16, 12, 0)])
def test_offers_functions_reject_invalid_requested_date(bad_date):
    with pytest.raises(ValueError, match="requested_date must be a date"):
        get_offers(["464306"], bad_date)
    with pytest.raises(ValueError, match="requested_date must be a date"):
        fallback_get_offers(["464306"], bad_date)
    with pytest.raises(ValueError, match="requested_date must be a date"):
        build_offer("464306", bad_date, None, None, None)


@pytest.mark.parametrize("bad_pid", ["", "   ", "\t\n", None, 123, True, False])
def test_build_offer_rejects_empty_or_non_string_pid(bad_pid):
    with pytest.raises(ValueError, match="pid must be a non-empty string"):
        build_offer(bad_pid, DELIVERY, None, None, None)


@pytest.mark.parametrize("bad_ids", [None, 123, True, False, "464306", b"464306", {"464306": 1}])
def test_get_offers_rejects_non_iterable_or_bare_string_product_ids(bad_ids):
    with pytest.raises(ValueError, match="product_ids must be an iterable of product id strings"):
        get_offers(bad_ids, DELIVERY)
    with pytest.raises(ValueError, match="product_ids must be an iterable of product id strings"):
        fallback_get_offers(bad_ids, DELIVERY)


def test_get_offers_skips_blank_and_non_string_ids_and_sanitizes_backend_offers(monkeypatch):
    import aimo.backend as backend_mod

    assert get_offers([], DELIVERY) == {}
    assert get_offers(["", "   ", "\t\n", None, 123, True, False, math.nan], DELIVERY) == {}
    assert fallback_get_offers(["", "  ", None, True, 99], DELIVERY) == {}

    seen_ids: list[list[str]] = []
    now = datetime.now(timezone.utc)

    def fake_backend_get_offers(ids, d, customer_id=None):
        seen_ids.append(list(ids))
        return {
            "464306": Offer.model_construct(
                product_id="464306",
                requested_date=d,
                available=True,
                available_amount=math.nan,
                cutoff_passed=False,
                sales_block=False,
                unit_price=-3.0,
                comparison_price=math.inf,
                fetched_at=now,
                issues=[],
            ),
            "469363": {
                "product_id": "469363",
                "requested_date": d,
                "available": True,
                "available_amount": True,
                "unit_price": True,
                "comparison_price": False,
                "fetched_at": now,
                "issues": [],
            },
            "   ": _offer("blank"),
        }

    monkeypatch.setattr(backend_mod, "get_offers", fake_backend_get_offers)
    res = get_offers([" 464306 ", "", "  ", None, True, 99, "469363", "464306"], DELIVERY)
    assert seen_ids == [["464306", "469363"]]
    assert set(res.keys()) == {"464306", "469363"}
    assert res["464306"].unit_price is None
    assert res["464306"].comparison_price is None
    assert res["464306"].available_amount is None
    assert res["464306"].available is False
    assert "price_error" in res["464306"].issues
    assert "availability_error" in res["464306"].issues
    assert res["469363"].unit_price is None
    assert res["469363"].comparison_price is None
    assert res["469363"].available_amount is None
    assert res["469363"].available is False


def test_fallback_get_offers_handles_corrupt_upstream_products(monkeypatch):
    def fake_post(kind: str, _ids: list[str], _d: date) -> dict:
        if kind == "price":
            return {
                "products": [
                    {"error": "missing-id"},
                    "not-a-dict",
                    _valid_price_payload(unit_price=math.nan, comparison_price=-2.0),
                    {**_valid_price_payload(unit_price=2.50, comparison_price=math.inf), "productId": "469363"},
                ]
            }
        return {
            "timeNow": "2026-11-14T12:00:00Z",
            "products": [
                None,
                _valid_avail_payload(amount=-10.0),
                {**_valid_avail_payload(amount=25.0), "productId": "469363"},
            ],
        }

    monkeypatch.setattr(offers_mod, "_post", fake_post)
    out = fallback_get_offers(["464306", "  ", None, "469363"], DELIVERY)
    assert set(out.keys()) == {"464306", "469363"}
    assert out["464306"].available is False
    assert out["464306"].unit_price is None
    assert out["464306"].available_amount is None
    assert out["469363"].available is True
    assert out["469363"].unit_price == pytest.approx(2.50)
    assert out["469363"].comparison_price is None
    assert out["469363"].available_amount == 25.0
