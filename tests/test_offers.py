import os
import tempfile

os.environ.setdefault('AIMO_DB', os.path.join(tempfile.mkdtemp(), 't.db'))
from datetime import date

import pytest

from aimo import offers
from aimo.models import Need, NeedItem, QuoteRequestLine, SimulateStock

catalog = pytest.importorskip("aimo.catalog")

D = date(2026, 11, 16)


def milk():
    return Need(customer="hotel-aurora", deliver_by=D, items=[NeedItem(query="lactose-free milk", quantity=1000, unit="L")])


def test_milk_offers_priced():
    offers.clear_simulation()
    g = offers.get_offers(milk()).items[0]
    assert g.offers and g.shortfall == 0
    o = g.offers[0]
    assert o.unit_price_net > 0 and o.units_needed >= 1 and o.total_gross >= o.total_net


def test_stockout_changes_result():
    offers.clear_simulation()
    top = offers.get_offers(milk()).items[0].offers[0].product_id
    offers.simulate(SimulateStock(product_id=top, available_amount=0))
    try:
        g = offers.get_offers(milk()).items[0]
        assert top not in {o.product_id for o in g.offers if o.substitute_for is None} or g.shortfall > 0
        assert g.shortfall > 0 or g.offers
    finally:
        offers.clear_simulation()


def test_price_lines():
    top = offers.get_offers(milk()).items[0].offers[0]
    lines, gross, valid = offers.price_lines("hotel-aurora", D, [QuoteRequestLine(product_id=top.product_id, quantity=10)])
    assert lines[0].line_total_net == round(10 * top.unit_price_net, 2)
    assert gross > lines[0].line_total_net and valid.tzinfo


def test_alternatives_and_options():
    offers.clear_simulation()
    g = offers.get_offers(milk()).items[0]
    alts = g.alternatives
    assert 1 <= len(alts) <= 5
    ids = [g.offers[0].product_id] + [a.product_id for a in alts]
    assert len(set(ids)) == len(ids)
    for a in alts:
        assert a.tier and a.notes and a.total_net > 0
        assert "maitorahka" not in a.product.name.lower() and "kahvi" not in a.product.name.lower()
    assert g.options == []
    top = g.offers[0].product_id
    offers.simulate(SimulateStock(product_id=top, available_amount=40))
    try:
        g2 = offers.get_offers(milk()).items[0]
        if g2.shortfall > 0:
            assert g2.options
    finally:
        offers.clear_simulation()


def test_cutoff_rule():
    c = offers.cutoff("2026-11-12T15:00:00+0200", "2026-11-16", D)
    assert c.isoformat() == "2026-11-15T15:00:00+02:00"
    assert offers.cutoff("2026-11-12T15:00:00+0200", None, date(2026, 12, 1)).isoformat() == "2026-11-30T15:00:00+02:00"


def test_far_delivery_not_expired():
    from datetime import timedelta
    far = date.today() + timedelta(days=5)
    n = Need(customer="hotel-aurora", deliver_by=far, items=[NeedItem(query="lactose-free milk", quantity=100, unit="L")])
    g = offers.get_offers(n).items[0]
    assert g.offers and g.offers[0].notes[0].startswith("cutoff computed from Valio closing time")
    lines, _, valid = offers.price_lines("hotel-aurora", far, [QuoteRequestLine(product_id=g.offers[0].product_id, quantity=5)])
    assert valid.date() <= far - timedelta(days=1)
    from datetime import datetime, timezone
    assert valid > datetime.now(timezone.utc)


def test_split_filler_same_brand_not_substitute():
    offers.clear_simulation()
    top = offers.get_offers(milk()).items[0].offers[0].product_id
    offers.simulate(SimulateStock(product_id=top, available_amount=40))
    try:
        g = offers.get_offers(milk()).items[0]
        assert len(g.offers) > 1
        f = g.offers[1]
        assert f.notes
        if f.match_score >= g.offers[0].match_score:
            assert f.substitute_for is None
    finally:
        offers.clear_simulation()


def test_cutoff_passed_shifts_date(monkeypatch):
    from datetime import datetime, timedelta
    tomorrow = date.today()
    n = Need(customer="hotel-aurora", deliver_by=tomorrow, items=[NeedItem(query="lactose-free milk", quantity=100, unit="L")])
    monkeypatch.setattr(offers.valio, "availability", lambda c, d, ids: {i: {"productId": i, "closingTime": "2026-11-12T15:00:00+0200", "salesBlock": False, "scheduleLines": [{"salesBlock": False, "availableAmount": 9999.0, "availableDate": tomorrow.isoformat(), "salesUnit": "ST"}]} for i in ids})
    g = offers.get_offers(n).items[0]
    assert all(o.available_date <= tomorrow for o in g.offers + g.alternatives)
    assert g.shortfall > 0 and g.options and g.options[0]["date"] > tomorrow


def test_past_delivery_rejected():
    from datetime import timedelta
    from aimo.quotes import QuoteError
    with pytest.raises(QuoteError) as e:
        offers.price_lines("hotel-aurora", date.today() - timedelta(days=1), [QuoteRequestLine(product_id="464073", quantity=1)])
    assert e.value.code == "INVALID_QUOTE"


def test_fillers_sized_to_shortfall():
    offers.clear_simulation()
    top = offers.get_offers(milk()).items[0].offers[0]
    offers.simulate(SimulateStock(product_id=top.product_id, available_amount=40))
    try:
        g = offers.get_offers(milk()).items[0]
        total_l = sum(o.units_needed for o in g.offers)
        assert g.offers[0].units_needed == 40 and len(g.offers) > 1
        assert all(o.available_date <= D and "2026-11-12" not in str(o.closing_time) for o in g.offers)
    finally:
        offers.clear_simulation()


def test_allergen_swap():
    n = Need(customer="hotel-aurora", deliver_by=D, items=[NeedItem(query="lactose-free milk", quantity=20, unit="L", constraints=__import__("aimo.models", fromlist=["Constraint"]).Constraint(exclude_allergens=["milk"]))])
    g = offers.get_offers(n).items[0]
    assert g.offers and any(n.startswith("Swap for") for n in g.offers[0].notes) and g.offers[0].tier == "good" and g.offers[0].substitute_for is None


def test_primary_is_cheapest_equivalent_and_stable():
    from aimo import allocate
    offers.clear_simulation()
    a = offers.get_offers(milk()).items[0]
    b = offers.get_offers(milk()).items[0]
    assert [o.product_id for o in a.offers] == [o.product_id for o in b.offers]
    assert [o.product_id for o in a.alternatives] == [o.product_id for o in b.alternatives]
    top = a.offers[0]
    assert top.match_score >= 0.9
    per = lambda o: o.unit_price_net / allocate._ratio(o.product, "L")
    for x in a.alternatives:
        if x.match_score >= 0.9:
            assert per(top) <= per(x) + 1e-9
    assert len({x.tier for x in a.alternatives}) == len(a.alternatives)


def test_exclusion_note():
    from aimo.models import Constraint
    n = Need(customer="hotel-aurora", deliver_by=D, items=[NeedItem(query="lactose-free milk", quantity=20, unit="L", constraints=Constraint(exclude_allergens=["egg", "peanuts"]))])
    g = offers.get_offers(n).items[0]
    assert g.offers
    hit = [x for x in g.offers[0].notes if x.startswith("Excluded")]
    assert not hit or "egg" in hit[0]
