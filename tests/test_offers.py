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
