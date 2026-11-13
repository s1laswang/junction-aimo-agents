import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from aimo.models import Constraint, Need, NeedItem, SimulateStock

offers = pytest.importorskip("aimo.offers")
pytest.importorskip("aimo.catalog")
pytest.importorskip("aimo.substitute")

DATA = Path(os.environ.get("AIMO_DATA_DIR", "data"))
CACHE = Path(os.environ.get("AIMO_CACHE_DIR", "var/cache"))
TODAY = date.today()
MONDAY = TODAY + timedelta(days=(0 - TODAY.weekday()) % 7 or 7)

pytestmark = pytest.mark.skipif(
    not (DATA / "valio_aimo_junction26_product_data.json").exists()
    or not (
        os.environ.get("AIMO_LIVE") == "1"
        or any(CACHE.glob(f"price__*__{MONDAY.isoformat()}.json"))
    ),
    reason="needs data dir and a filled valio cache for next Monday (or AIMO_LIVE=1, one batched fetch)",
)

NOT_PLAIN_MILK = re.compile(
    r"chocolate|suklaa|cocoa|kaakao|strawberry|banana|vanilla|powder|jauhe|yogh|jogurt|ice cream|jäätelö|oat|soy|soya|alpro|almond|cream|kerma",
    re.I,
)
LACTOSE_FREE = re.compile(r"lactose[- ]free|laktoositon", re.I)
LITRES = {"l": 1.0, "dl": 0.1, "cl": 0.01, "ml": 0.001}


def milk_need(customer="hotel-aurora", constraints=None):
    return Need(
        customer=customer,
        deliver_by=MONDAY,
        items=[
            NeedItem(
                query="laktoositon maito",
                quantity=1000,
                unit="L",
                constraints=constraints or Constraint(lactose_free=True),
            )
        ],
        raw_text="1000 l laktoositon maito",
    )


@pytest.fixture(scope="module")
def group():
    r = offers.get_offers(milk_need())
    assert len(r.items) == 1
    return r.items[0]


def test_returns_offers(group):
    assert group.offers


def test_every_offer_is_lactose_free_milk_with_real_price(group):
    for o in group.offers:
        assert LACTOSE_FREE.search(o.product.name) or "lactose_free" in [l.lower() for l in o.product.labels], o.product.name
        assert not NOT_PLAIN_MILK.search(o.product.name), o.product.name
        assert o.unit_price_net > 0 and o.total_net > 0 and o.total_gross >= o.total_net


def test_volume_covered_or_shortfall_explains(group):
    litres = 0.0
    for o in group.offers:
        us = o.product.unit_size
        if us and us.unit.lower() in LITRES:
            litres += o.units_needed * us.value * LITRES[us.unit.lower()]
        else:
            pytest.skip(f"no litre unit size for {o.product.name}")
    assert litres >= 1000 or group.shortfall > 0
    if group.shortfall == 0:
        assert litres >= 1000


def test_reliability_and_co2_filled_when_csv_has_them(group):
    def ids(f):
        return {l.split(",")[0] for l in (DATA / f).read_text().splitlines()[1:]}

    rel = ids("valio_aimo_junction26_delivery_reliability.csv")
    co2 = ids("valio_aimo_junction26_carbon_footprint.csv")
    for o in group.offers:
        if o.product_id in rel:
            assert o.reliability is not None, o.product_id
        if o.product_id in co2:
            assert o.co2_per_kg is not None, o.product_id


def test_stock_out_on_top_product_leads_to_split_or_substitute():
    top = offers.get_offers(milk_need()).items[0].offers[0].product_id
    offers.simulate(SimulateStock(product_id=top, available_amount=0))
    try:
        g = offers.get_offers(milk_need()).items[0]
    finally:
        offers.clear_simulation()
    others = [o for o in g.offers if o.product_id != top]
    assert g.shortfall > 0 or others or any(o.substitute_for for o in g.offers)
    for o in g.offers:
        if o.product_id == top:
            assert o.available_amount == 0 or o.units_needed == 0
    for o in g.offers:
        if o.substitute_for:
            assert o.substitute_for == top


def test_hospital_never_sees_excluded_allergens():
    banned = ["peanut", "nuts", "egg"]
    need = Need(
        customer="hospital-tays",
        deliver_by=MONDAY,
        items=[NeedItem(query="pancake", quantity=50, unit="kg", constraints=Constraint(exclude_allergens=banned))],
    )
    for g in offers.get_offers(need).items:
        for o in g.offers:
            have = [a.lower() for a in o.product.allergens]
            assert not any(b in a for b in banned for a in have), (o.product.name, have)
