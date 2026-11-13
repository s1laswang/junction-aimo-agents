from datetime import datetime, timedelta, timezone

import pytest

from aimo import audit, mandates
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
    return Quote(quote_id="q", customer="c", agent_id="a", delivery_date=NOW.date(), lines=[QuoteLine(product_id=pid, quantity=1, unit_price_net=total, line_total_net=total)],
                 total_net=total, total_gross=total, issued_at=NOW, valid_until=valid)


def codes(d):
    return [r.code for r in d.reasons]


def test_keys():
    c, a, m = mandates.agent_for_key("demo-hotel-aurora")
    assert c == "hotel-aurora" and m.approvers
    assert mandates.agent_for_key("nope") is None
    assert len(mandates.all()) == 3


def test_auto():
    d = decide(quote(1000.0), mandates.get("hotel-aurora"), PRODUCTS, now=NOW)
    assert d.outcome == "auto" and codes(d) == ["WITHIN_MANDATE"]
    assert "per-order limit" in d.reasons[0].text


def test_over_limits():
    m = mandates.get("hotel-aurora")
    d = decide(quote(m.per_order_eur + 1), m, PRODUCTS, now=NOW)
    assert d.outcome == "needs_approval" and "OVER_ORDER_LIMIT" in codes(d)
    d = decide(quote(100.0), m, PRODUCTS, spent_this_week=m.per_week_eur, now=NOW)
    assert d.outcome == "needs_approval" and codes(d) == ["OVER_WEEK_LIMIT"]


def test_allergen_blocked_beats_limit():
    m = mandates.get("hospital-tays")
    d = decide(quote(m.per_order_eur + 1, pid="1"), m, PRODUCTS, now=NOW)
    assert d.outcome == "blocked" and {"ALLERGEN_CONFLICT", "OVER_ORDER_LIMIT"} <= set(codes(d))


def test_substitution_rules():
    s = [Substitution(original_id="464073", substitute_id="2", reason="out of stock")]
    assert decide(quote(10.0), mandates.get("hospital-tays"), PRODUCTS, substitutions=s, now=NOW).outcome == "needs_approval"
    assert decide(quote(10.0), mandates.get("hotel-aurora"), PRODUCTS, substitutions=s, now=NOW).outcome == "needs_approval"
    ok = [Substitution(original_id="2", substitute_id="464073", reason="stock")]
    assert decide(quote(10.0), mandates.get("hotel-aurora"), PRODUCTS, substitutions=ok, now=NOW).outcome == "auto"
    never = mandates.get("hotel-aurora").model_copy(update={"substitutions": "never"})
    assert decide(quote(10.0), never, PRODUCTS, substitutions=s, now=NOW).outcome == "blocked"


def test_category_and_cutoff():
    m = mandates.get("hotel-aurora").model_copy(update={"allowed_categories": ["999"]})
    assert "CATEGORY_NOT_ALLOWED" in codes(decide(quote(10.0), m, PRODUCTS, now=NOW))
    d = decide(quote(10.0, valid=NOW - timedelta(minutes=1)), mandates.get("hotel-aurora"), PRODUCTS, now=NOW)
    assert d.outcome == "blocked" and "PAST_CUTOFF" in codes(d)


def test_unknown_product_and_traces():
    h = mandates.get("hospital-tays")
    d = decide(quote(10.0, pid="999"), h, PRODUCTS, now=NOW)
    assert d.outcome == "needs_approval" and codes(d) == ["UNKNOWN_PRODUCT"]
    assert decide(quote(10.0, pid="999"), mandates.get("hotel-aurora"), PRODUCTS, now=NOW).outcome == "auto"
    trace = Product(product_id="3", name="Bar", brand="Valio", allergens=["may:AP"])
    d = decide(quote(10.0, pid="3"), h, {"3": trace}, now=NOW)
    assert d.outcome == "blocked" and "traces" in d.reasons[0].text


def test_put_persists_and_audits():
    m = mandates.get("hotel-aurora").model_copy(update={"per_order_eur": 5000.0})
    mandates.put(m)
    assert mandates.get("hotel-aurora").per_order_eur == 5000.0
    assert len(mandates.all()) == 3
    ev = audit.events(customer="hotel-aurora")[-1]
    assert ev.action == "mandate_updated" and "per_order_eur" in ev.why and "->" in ev.why
