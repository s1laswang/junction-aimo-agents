from datetime import datetime, timedelta, timezone

import pytest

from aimo import mandates
from aimo.models import Product, Quote, QuoteLine, Substitution
from aimo.policy import decide

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
MILK = Product(product_id="464073", name="Valio whole milk 1 l lactose free UHT", brand="Valio", category="10400", allergens=["AM"])
NUT = Product(product_id="1", name="Peanut butter", brand="X", category="1", allergens=["AP"])
OAT = Product(product_id="2", name="Oat drink", brand="Other", category="10400", allergens=[])
PRODUCTS = {p.product_id: p for p in (MILK, NUT, OAT)}


@pytest.fixture(autouse=True)
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))


def quote(total, pid="464073", valid=NOW + timedelta(hours=5)):
    return Quote(
        quote_id="q",
        customer="c",
        agent_id="a",
        delivery_date=NOW.date(),
        lines=[QuoteLine(product_id=pid, quantity=1, unit_price_net=total, line_total_net=total)],
        total_net=total,
        total_gross=total,
        issued_at=NOW,
        valid_until=valid,
    )


def codes(d):
    return [r.code for r in d.reasons]


def test_exact_order_spend_boundary():
    m = mandates.get("hotel-aurora")
    at_limit = decide(quote(m.per_order_eur), m, PRODUCTS, now=NOW)
    assert at_limit.outcome == "auto"
    assert codes(at_limit) == ["WITHIN_MANDATE"]

    over_by_cent = decide(quote(round(m.per_order_eur + 0.01, 2)), m, PRODUCTS, now=NOW)
    assert over_by_cent.outcome == "needs_approval"
    assert codes(over_by_cent) == ["OVER_ORDER_LIMIT"]


def test_exact_week_spend_boundary():
    m = mandates.get("hotel-aurora")
    order_net = 1000.0
    spent_exact = m.per_week_eur - order_net

    at_week_limit = decide(quote(order_net), m, PRODUCTS, spent_this_week=spent_exact, now=NOW)
    assert at_week_limit.outcome == "auto"
    assert codes(at_week_limit) == ["WITHIN_MANDATE"]

    over_week_by_cent = decide(quote(round(order_net + 0.01, 2)), m, PRODUCTS, spent_this_week=spent_exact, now=NOW)
    assert over_week_by_cent.outcome == "needs_approval"
    assert codes(over_week_by_cent) == ["OVER_WEEK_LIMIT"]


def test_combined_needs_approval_reasons():
    m = mandates.get("hotel-aurora")
    subs = [Substitution(original_id="464073", substitute_id="2", reason="out of stock")]
    d = decide(
        quote(m.per_order_eur + 50.0),
        m,
        PRODUCTS,
        spent_this_week=m.per_week_eur,
        substitutions=subs,
        now=NOW,
    )
    assert d.outcome == "needs_approval"
    assert set(codes(d)) == {"OVER_ORDER_LIMIT", "OVER_WEEK_LIMIT", "SUBSTITUTION_NEEDS_APPROVAL"}
    assert len(codes(d)) == 3


def test_combined_allergen_conflict_and_over_order_limit():
    m = mandates.get("hospital-tays")
    d = decide(quote(m.per_order_eur + 50.0, pid="1"), m, PRODUCTS, now=NOW)
    assert d.outcome == "blocked"
    assert set(codes(d)) == {"ALLERGEN_CONFLICT", "OVER_ORDER_LIMIT"}


@pytest.mark.parametrize("allergen", ["AM", "ML"])
def test_wide_dairy_group_blocks(allergen):
    m = mandates.get("hotel-aurora").model_copy(update={"exclude_allergens": ["dairy"]})
    p = Product(product_id="p", name="Dairy item", brand="Valio", allergens=[allergen])
    d = decide(quote(10.0, pid="p"), m, {"p": p}, now=NOW)
    assert d.outcome == "blocked"
    assert codes(d) == ["ALLERGEN_CONFLICT"]


@pytest.mark.parametrize("allergen", ["AW", "UW", "GB", "NR"])
def test_wide_gluten_group_blocks(allergen):
    m = mandates.get("hotel-aurora").model_copy(update={"exclude_allergens": ["gluten"]})
    p = Product(product_id="p", name="Grain item", brand="Valio", allergens=[allergen])
    d = decide(quote(10.0, pid="p"), m, {"p": p}, now=NOW)
    assert d.outcome == "blocked"
    assert codes(d) == ["ALLERGEN_CONFLICT"]


@pytest.mark.parametrize("allergen", ["AN", "AP", "may_contain:AN", "may:AP"])
def test_wide_nuts_group_blocks_direct_and_traces(allergen):
    m = mandates.get("hotel-aurora").model_copy(update={"exclude_allergens": ["nuts"]})
    p = Product(product_id="p", name="Snack item", brand="Valio", allergens=[allergen])
    d = decide(quote(10.0, pid="p"), m, {"p": p}, now=NOW)
    assert d.outcome == "blocked"
    assert codes(d) == ["ALLERGEN_CONFLICT"]


def test_cutoff_exact_boundary():
    m = mandates.get("hotel-aurora")
    at_cutoff = decide(quote(10.0, valid=NOW), m, PRODUCTS, now=NOW)
    assert at_cutoff.outcome == "auto"
    assert codes(at_cutoff) == ["WITHIN_MANDATE"]

    past_cutoff = decide(quote(10.0, valid=NOW - timedelta(microseconds=1)), m, PRODUCTS, now=NOW)
    assert past_cutoff.outcome == "blocked"
    assert codes(past_cutoff) == ["PAST_CUTOFF"]
