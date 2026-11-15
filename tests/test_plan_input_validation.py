"""Unit tests for Need and lot input validation in aimo.catalog.plan and aimo.catalog.substitution."""
from __future__ import annotations

import math
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import aimo.catalog.plan as plan_mod
import aimo.catalog.textsearch as textsearch_mod
from aimo.catalog import units as U
from aimo.catalog.plan import _round_lot, fill, plan_need
from aimo.catalog.substitution import price_per_unit, rank_substitutes, validate_need
from aimo.models import Need, Offer


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
