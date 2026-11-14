"""Unit tests for Need and lot input validation in aimo.catalog.plan and aimo.catalog.substitution."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

import aimo.catalog.plan as plan_mod
import aimo.catalog.textsearch as textsearch_mod
from aimo.catalog.plan import _round_lot, fill, plan_need
from aimo.catalog.substitution import rank_substitutes
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
