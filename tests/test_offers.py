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
    offers.simulate(SimulateStock(product_id=top, available_amount=100))
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
    offers.simulate(SimulateStock(product_id=top, available_amount=100))
    try:
        g = offers.get_offers(milk()).items[0]
        assert len(g.offers) > 1
        f = g.offers[1]
        assert f.notes
        if f.match_score >= g.offers[0].match_score:
            assert f.substitute_for is None
    finally:
        offers.clear_simulation()
