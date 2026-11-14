from datetime import date, datetime, timedelta
from typing import Callable

import pytest

from aimo import allocate, catalog, offers, substitute, valio
from aimo.models import (
    Constraint,
    Mandate,
    Need,
    NeedItem,
    Offer,
    OfferGroup,
    OffersResponse,
    Product,
    SimulateStock,
    UnitSize,
)

DELIVERY_DAY = date(2026, 11, 20)
ORDER_NOW = datetime(2026, 11, 17, 10, 0, tzinfo=offers.HEL)
CLOSING_STR = "2026-11-12T15:00:00+0200"


@pytest.fixture(autouse=True)
def isolated_offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "empty_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.setattr(valio, "_client", None)
    allocate._data.cache_clear()
    catalog.reset()
    offers._overrides.clear()
    yield
    offers._overrides.clear()
    catalog.reset()
    allocate._data.cache_clear()


def make_product(
    pid: str,
    name: str,
    *,
    brand: str = "Valio",
    vendor: str = "Valio Oy",
    category: str = "10400",
    sales_unit: str = "ST",
    size_value: float | None = 1.0,
    size_unit: str = "l",
    allergens: list[str] | None = None,
    labels: list[str] | None = None,
    origin: str = "FI",
) -> Product:
    unit_size = UnitSize(value=size_value, unit=size_unit) if size_value is not None else None
    return Product(
        product_id=pid,
        name=name,
        brand=brand,
        vendor=vendor,
        category=category,
        sales_unit=sales_unit,
        unit_size=unit_size,
        allergens=list(allergens) if allergens is not None else ["milk"],
        labels=list(labels) if labels is not None else ["free_from_lactose"],
        origin=origin,
    )


def price_row(pid: str, net: float, gross: float | None = None, currency: str = "EUR") -> dict:
    gross_val = gross if gross is not None else round(net * 1.14, 2)
    return {
        "productId": pid,
        "currency": currency,
        "catalogPrice": {"value": net},
        "priceWithTax": {"value": gross_val},
    }


def avail_row(
    pid: str,
    day: date = DELIVERY_DAY,
    amount: float = 500.0,
    *,
    closing: str | None = CLOSING_STR,
    sales_block: bool = False,
    sales_unit: str = "ST",
    extra_lines: list[dict] | None = None,
) -> dict:
    lines = [
        {
            "salesBlock": sales_block,
            "availableAmount": amount,
            "availableDate": day.isoformat(),
            "salesUnit": sales_unit,
        }
    ]
    if extra_lines:
        lines.extend(extra_lines)
    return {
        "productId": pid,
        "closingTime": closing,
        "salesBlock": sales_block,
        "scheduleLines": lines,
    }


def wire_mocks(
    monkeypatch,
    products: list[Product],
    prices: dict[str, dict],
    avail: dict[str, dict],
    *,
    reliability: dict[str, float] | None = None,
    co2: dict[str, float] | None = None,
    search_fn: Callable[[str, Constraint | None, int], list[Product]] | None = None,
    seen_searches: list[tuple[str, Constraint]] | None = None,
) -> None:
    by_id = {p.product_id: p for p in products}
    rel_map = reliability or {}
    co2_map = co2 or {}

    def default_search(q: str, constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        c = constraints or Constraint()
        if seen_searches is not None:
            seen_searches.append((q, c.model_copy(deep=True)))
        if search_fn is not None:
            return search_fn(q, c, limit)[:limit]
        return list(products[:limit])

    monkeypatch.setattr(catalog, "search", default_search)
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: rel_map.get(str(pid)))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: co2_map.get(str(pid)))
    monkeypatch.setattr(valio, "price", lambda cust, day, ids: {i: prices[i] for i in ids if i in prices})
    monkeypatch.setattr(valio, "availability", lambda cust, day, ids: {i: avail[i] for i in ids if i in avail})


def test_priced_sorts_high_match_by_unit_price_reliability_and_product_id(monkeypatch):
    p_expensive = make_product("p_exp", "Valio lactose free milk 1 L", size_value=1.0)
    p_rel_low = make_product("p_rel_low", "Valio lactose free milk 1 L", size_value=1.0)
    p_rel_high_b = make_product("p_rel_high_b", "Valio lactose free milk 1 L", size_value=1.0)
    p_rel_high_a = make_product("p_rel_high_a", "Valio lactose free milk 1 L", size_value=1.0)
    p_rel_none_b = make_product("p_rel_none_b", "Valio lactose free milk 1 L", size_value=1.0)
    p_rel_none_a = make_product("p_rel_none_a", "Valio lactose free milk 1 L", size_value=1.0)

    products = [p_expensive, p_rel_low, p_rel_high_b, p_rel_high_a, p_rel_none_b, p_rel_none_a]
    prices = {
        "p_exp": price_row("p_exp", 1.65),
        "p_rel_low": price_row("p_rel_low", 1.25),
        "p_rel_high_b": price_row("p_rel_high_b", 1.25),
        "p_rel_high_a": price_row("p_rel_high_a", 1.25),
        "p_rel_none_b": price_row("p_rel_none_b", 1.25),
        "p_rel_none_a": price_row("p_rel_none_a", 1.25),
    }
    avail = {p.product_id: avail_row(p.product_id) for p in products}
    rel = {
        "p_exp": 1.00,
        "p_rel_low": 0.88,
        "p_rel_high_b": 0.97,
        "p_rel_high_a": 0.97,
    }
    wire_mocks(monkeypatch, products, prices, avail, reliability=rel)

    scores = {
        "p_exp": 1.00,
        "p_rel_low": 0.99,
        "p_rel_high_b": 0.92,
        "p_rel_high_a": 0.90,
        "p_rel_none_b": 0.95,
        "p_rel_none_a": 0.95,
    }
    item = NeedItem(query="lactose-free milk", quantity=20, unit="L")
    ranked = offers._priced("hotel-aurora", DELIVERY_DAY, item, products, scores, now=ORDER_NOW)

    assert [o.product_id for o in ranked] == [
        "p_rel_high_a",
        "p_rel_high_b",
        "p_rel_low",
        "p_rel_none_a",
        "p_rel_none_b",
        "p_exp",
    ]


def test_priced_ranks_mid_match_after_0_9_threshold_and_orders_mid_tier_by_match_score(monkeypatch):
    p_top_pricier = make_product("p_top", "Fresh apple juice 1 L", category="10800", allergens=[], labels=["vegan"])
    p_mid_higher = make_product("p_mid_89", "Apple drink 1 L", category="10800", allergens=[], labels=["vegan"])
    p_mid_lower = make_product("p_mid_60", "Apple beverage 1 L", category="10800", allergens=[], labels=["vegan"])
    p_mid_tie_cheap = make_product("p_mid_75_cheap", "Apple blend 1 L", category="10800", allergens=[], labels=["vegan"])
    p_mid_tie_dear = make_product("p_mid_75_dear", "Apple mix 1 L", category="10800", allergens=[], labels=["vegan"])

    products = [p_mid_lower, p_mid_tie_dear, p_top_pricier, p_mid_higher, p_mid_tie_cheap]
    prices = {
        "p_top": price_row("p_top", 2.40),
        "p_mid_89": price_row("p_mid_89", 1.90),
        "p_mid_75_cheap": price_row("p_mid_75_cheap", 0.95),
        "p_mid_75_dear": price_row("p_mid_75_dear", 1.10),
        "p_mid_60": price_row("p_mid_60", 0.50),
    }
    avail = {p.product_id: avail_row(p.product_id) for p in products}
    wire_mocks(monkeypatch, products, prices, avail, reliability={p.product_id: 0.99 for p in products})

    scores = {
        "p_top": 0.90,
        "p_mid_89": 0.899,
        "p_mid_75_cheap": 0.75,
        "p_mid_75_dear": 0.75,
        "p_mid_60": 0.50,
    }
    item = NeedItem(query="apple juice", quantity=10, unit="L")
    ranked = offers._priced("hotel-aurora", DELIVERY_DAY, item, products, scores, now=ORDER_NOW)

    assert [o.product_id for o in ranked] == [
        "p_top",
        "p_mid_89",
        "p_mid_75_cheap",
        "p_mid_75_dear",
        "p_mid_60",
    ]


def test_get_offers_drops_below_min_match_and_ranks_mid_after_high_match(monkeypatch):
    p_exact_dear = make_product("p_exact_dear", "Valio lactose free milk 1 L", size_value=1.0)
    p_exact_cheap = make_product("p_exact_cheap", "Valio lactose free semi-skimmed milk 1 L", size_value=1.0)
    p_mid_portion = make_product("p_mid_portion", "Valio lactose free portion milk 1 L", size_value=1.0)
    p_low_choc = make_product("p_low_choc", "Valio lactose free chocolate milk 1 L", size_value=1.0)
    p_low_coffee = make_product("p_low_coffee", "Valio lactose free coffee milk latte 1 L", size_value=1.0)
    p_low_minipack = make_product("p_low_minipack", "Valio lactose free milk 100 x 2 cl", size_value=2.0)
    p_zero_water = make_product("p_zero_water", "Spring water 1 L", allergens=[], labels=["free_from_lactose"])

    products = [
        p_low_choc,
        p_mid_portion,
        p_exact_dear,
        p_low_coffee,
        p_zero_water,
        p_exact_cheap,
        p_low_minipack,
    ]
    prices = {
        "p_exact_dear": price_row("p_exact_dear", 1.60),
        "p_exact_cheap": price_row("p_exact_cheap", 1.45),
        "p_mid_portion": price_row("p_mid_portion", 0.80),
        "p_low_choc": price_row("p_low_choc", 0.40),
        "p_low_coffee": price_row("p_low_coffee", 0.35),
        "p_low_minipack": price_row("p_low_minipack", 0.50),
        "p_zero_water": price_row("p_zero_water", 0.20),
    }
    avail = {p.product_id: avail_row(p.product_id, amount=200.0) for p in products}
    wire_mocks(monkeypatch, products, prices, avail)

    item = NeedItem(
        query="lactose-free milk",
        quantity=30,
        unit="L",
        constraints=Constraint(lactose_free=True),
    )
    assert substitute.match_score(item, p_exact_cheap) >= 0.9
    assert substitute.match_score(item, p_exact_dear) >= 0.9
    assert 0.5 <= substitute.match_score(item, p_mid_portion) < 0.9
    for dropped_prod in (p_low_choc, p_low_coffee, p_low_minipack, p_zero_water):
        assert substitute.match_score(item, dropped_prod) < offers.MIN_MATCH

    need = Need(customer="hotel-aurora", deliver_by=DELIVERY_DAY, items=[item])
    group = offers.get_offers(need, now=ORDER_NOW).items[0]

    assert [o.product_id for o in group.offers] == ["p_exact_cheap"]
    alt_ids = {a.product_id for a in group.alternatives}
    assert alt_ids == {"p_mid_portion", "p_exact_dear"}
    assert alt_ids.isdisjoint({"p_low_choc", "p_low_coffee", "p_low_minipack", "p_zero_water"})


def test_multipack_10l_novobox_ranks_ahead_of_1l_carton_by_unit_normalized_price(monkeypatch):
    novobox_10l = make_product(
        "464179",
        "Valio lactose free milk 10 L novobox",
        sales_unit="ST",
        size_value=10.0,
        size_unit="l",
    )
    carton_1l = make_product(
        "464201",
        "Valio lactose free milk 1 L carton",
        sales_unit="ST",
        size_value=1.0,
        size_unit="l",
    )
    jug_2l = make_product(
        "464300",
        "Valio lactose free milk 2000 ml jug",
        sales_unit="ST",
        size_value=2000.0,
        size_unit="ml",
    )

    products = [carton_1l, jug_2l, novobox_10l]
    prices = {
        "464179": price_row("464179", 11.80, 13.45),
        "464201": price_row("464201", 1.56, 1.78),
        "464300": price_row("464300", 2.70, 3.08),
    }
    avail = {p.product_id: avail_row(p.product_id, amount=500.0) for p in products}
    wire_mocks(
        monkeypatch,
        products,
        prices,
        avail,
        reliability={"464179": 0.96, "464201": 0.99, "464300": 0.97},
        co2={"464179": 0.95, "464201": 1.12, "464300": 1.05},
    )

    item = NeedItem(
        query="lactose-free milk",
        quantity=100,
        unit="L",
        constraints=Constraint(lactose_free=True),
    )
    for p in products:
        assert substitute.match_score(item, p) >= 0.9

    priced = offers._priced(
        "hotel-aurora",
        DELIVERY_DAY,
        item,
        products,
        {p.product_id: substitute.match_score(item, p) for p in products},
        now=ORDER_NOW,
    )
    assert [o.product_id for o in priced] == ["464179", "464300", "464201"]

    group = offers.get_offers(
        Need(customer="hotel-aurora", deliver_by=DELIVERY_DAY, items=[item]),
        now=ORDER_NOW,
    ).items[0]
    assert group.shortfall == 0.0
    assert group.options == []
    assert [o.product_id for o in group.offers] == ["464179"]
    chosen = group.offers[0]
    assert chosen.units_needed == 10.0
    assert chosen.unit_price_net == 11.80
    assert chosen.total_net == 118.00
    assert chosen.total_gross == 134.50


def test_priced_sorts_on_time_ahead_of_late_and_skips_invalid_or_incompatible_products(monkeypatch):
    p_ontime = make_product("p_ontime", "Valio lactose free butter 500 g", size_value=500.0, size_unit="g")
    p_bulk = make_product("p_bulk", "Valio lactose free butter 5 kg", size_value=5.0, size_unit="kg")
    p_late_cheaper = make_product("p_late", "Valio lactose free butter 5 kg", size_value=5.0, size_unit="kg")
    p_wrong_dim = make_product("p_vol", "Valio lactose free milk 1 L", size_value=1.0, size_unit="l")
    p_err_price = make_product("p_err", "Valio lactose free butter 1 kg", size_value=1.0, size_unit="kg")
    p_no_avail = make_product("p_noav", "Valio lactose free butter 1 kg", size_value=1.0, size_unit="kg")

    products = [p_late_cheaper, p_ontime, p_wrong_dim, p_err_price, p_no_avail, p_bulk]
    prices = {
        "p_ontime": price_row("p_ontime", 4.00),
        "p_bulk": price_row("p_bulk", 32.50),
        "p_late": price_row("p_late", 25.00),
        "p_vol": price_row("p_vol", 1.00),
        "p_err": {"productId": "p_err", "error": "NO_PRICE"},
        "p_noav": price_row("p_noav", 3.00),
    }
    avail = {
        "p_ontime": avail_row("p_ontime", DELIVERY_DAY, 100.0),
        "p_bulk": avail_row("p_bulk", DELIVERY_DAY, 100.0),
        "p_late": avail_row("p_late", DELIVERY_DAY + timedelta(days=2), 100.0),
        "p_vol": avail_row("p_vol", DELIVERY_DAY, 100.0),
        "p_err": avail_row("p_err", DELIVERY_DAY, 100.0),
    }
    wire_mocks(monkeypatch, products, prices, avail)

    item = NeedItem(query="butter", quantity=10, unit="kg")
    ranked = offers._priced("hotel-aurora", DELIVERY_DAY, item, products, {p.product_id: 0.96 for p in products}, now=ORDER_NOW)

    assert [o.product_id for o in ranked] == ["p_bulk", "p_ontime", "p_late"]
    assert ranked[0].units_needed == 2.0
    assert ranked[1].units_needed == 20.0


def test_offer_group_covered_contract_tiers_and_tradeoff_notes(monkeypatch):
    p_chosen = make_product("p_chosen", "Valio lactose free milk 1 L carton", size_value=1.0)
    p_good = make_product("p_good", "Valio lactose free portion milk 10 L bag", size_value=10.0)
    p_better = make_product("p_better", "Valio lactose free milk 2 L", size_value=2.0)
    p_best = make_product("p_best", "Valio milk", size_value=1.0)
    p_extra = make_product("p_extra", "Valio lactose free milk 1 L reserve", size_value=1.0)
    p_blocked = make_product("p_blocked", "Valio lactose free milk 1 L blocked", size_value=1.0)
    p_zero = make_product("p_zero", "Valio lactose free milk 1 L empty", size_value=1.0)
    p_late = make_product("p_late", "Valio lactose free milk 10 L late", size_value=10.0)

    products = [p_chosen, p_good, p_better, p_best, p_extra, p_blocked, p_zero, p_late]
    prices = {
        "p_chosen": price_row("p_chosen", 1.20),
        "p_good": price_row("p_good", 9.00),
        "p_better": price_row("p_better", 2.80),
        "p_best": price_row("p_best", 1.80),
        "p_extra": price_row("p_extra", 1.75),
        "p_blocked": price_row("p_blocked", 0.60),
        "p_zero": price_row("p_zero", 0.50),
        "p_late": price_row("p_late", 7.00),
    }
    avail = {
        "p_chosen": avail_row("p_chosen", DELIVERY_DAY, 300.0),
        "p_good": avail_row("p_good", DELIVERY_DAY, 50.0),
        "p_better": avail_row("p_better", DELIVERY_DAY, 200.0),
        "p_best": avail_row("p_best", DELIVERY_DAY, 200.0),
        "p_extra": avail_row("p_extra", DELIVERY_DAY, 200.0),
        "p_blocked": avail_row("p_blocked", DELIVERY_DAY, 200.0, sales_block=True),
        "p_zero": avail_row("p_zero", DELIVERY_DAY, 0.0),
        "p_late": avail_row("p_late", DELIVERY_DAY + timedelta(days=2), 200.0),
    }
    rel = {
        "p_chosen": 0.95,
        "p_good": 0.85,
        "p_better": 0.99,
        "p_best": 1.00,
        "p_extra": 0.86,
        "p_blocked": 0.99,
        "p_zero": 0.99,
        "p_late": 0.99,
    }
    co2 = {
        "p_chosen": 1.15,
        "p_good": 0.98,
        "p_better": 1.05,
        "p_best": 1.10,
        "p_extra": 1.30,
    }
    wire_mocks(monkeypatch, products, prices, avail, reliability=rel, co2=co2)

    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="lactose-free milk", quantity=80, unit="L", constraints=Constraint(lactose_free=True))],
    )
    resp = offers.get_offers(need, now=ORDER_NOW)
    assert isinstance(resp, OffersResponse)
    assert len(resp.items) == 1
    group = resp.items[0]
    assert isinstance(group, OfferGroup)

    assert group.shortfall == 0.0
    assert group.options == []
    assert len(group.offers) == 1
    assert group.offers[0].product_id == "p_chosen"
    assert group.offers[0].units_needed == 80.0
    assert group.offers[0].substitute_for is None

    assert len(group.alternatives) == 3
    assert [a.tier for a in group.alternatives] == ["good", "better", "best"]
    assert [a.product_id for a in group.alternatives] == ["p_good", "p_better", "p_best"]
    assert {"p_chosen", "p_blocked", "p_zero", "p_late"}.isdisjoint({a.product_id for a in group.alternatives})

    good_alt, better_alt, best_alt = group.alternatives
    assert good_alt.notes[0].startswith("cheapest acceptable: 25% cheaper per L")
    assert "10 L packs instead of 1 L packs" in good_alt.notes[0]
    assert "reliability 0.85 vs 0.95" in good_alt.notes[0]
    assert "CO2 0.98 vs 1.15 kg/kg" in good_alt.notes[0]

    assert better_alt.notes[0].startswith("balanced: 17% pricier per L")
    assert "2 L packs instead of 1 L packs" in better_alt.notes[0]
    assert "reliability 0.99 vs 0.95" in better_alt.notes[0]

    assert best_alt.notes[0].startswith("closest match: 50% pricier per L")
    assert "reliability 1 vs 0.95" in best_alt.notes[0]


def test_offer_group_alternatives_with_one_or_two_candidates(monkeypatch):
    p_primary = make_product("p1", "Valio lactose free milk 1 L")
    p_second = make_product("p2", "Valio lactose free milk 1 L alt")
    p_third = make_product("p3", "Valio milk")

    wire_mocks(
        monkeypatch,
        [p_primary, p_second],
        {"p1": price_row("p1", 1.15), "p2": price_row("p2", 1.35)},
        {"p1": avail_row("p1", DELIVERY_DAY, 200.0), "p2": avail_row("p2", DELIVERY_DAY, 200.0)},
        reliability={"p1": 0.95, "p2": 0.98},
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="lactose-free milk", quantity=20, unit="L", constraints=Constraint(lactose_free=True))],
    )
    one_alt = offers.get_offers(need, now=ORDER_NOW).items[0].alternatives
    assert [(a.product_id, a.tier) for a in one_alt] == [("p2", "good")]
    assert one_alt[0].notes[0].startswith("cheapest acceptable:")

    wire_mocks(
        monkeypatch,
        [p_primary, p_second, p_third],
        {"p1": price_row("p1", 1.15), "p2": price_row("p2", 1.30), "p3": price_row("p3", 1.55)},
        {
            "p1": avail_row("p1", DELIVERY_DAY, 200.0),
            "p2": avail_row("p2", DELIVERY_DAY, 200.0),
            "p3": avail_row("p3", DELIVERY_DAY, 200.0),
        },
        reliability={"p1": 0.95, "p2": 0.92, "p3": 0.99},
    )
    two_alts = offers.get_offers(need, now=ORDER_NOW).items[0].alternatives
    assert [(a.product_id, a.tier) for a in two_alts] == [("p2", "good"), ("p3", "best")]
    assert two_alts[0].notes[0].startswith("cheapest acceptable:")
    assert two_alts[1].notes[0].startswith("closest match:")


def test_split_allocation_marks_lower_match_score_filler_with_substitute_for_tier_and_tradeoff(monkeypatch):
    p_primary = make_product("p_primary", "Valio milk", brand="Valio", size_value=1.0)
    p_lower_match = make_product(
        "p_lower",
        "Valio lactose free portion milk 2 L",
        brand="Valio",
        size_value=2.0,
    )
    p_standby = make_product("p_standby", "Valio lactose free milk 1 L extra", brand="Valio", size_value=1.0)

    products = [p_primary, p_lower_match, p_standby]
    prices = {
        "p_primary": price_row("p_primary", 1.20),
        "p_lower": price_row("p_lower", 2.00),
        "p_standby": price_row("p_standby", 1.80),
    }
    avail = {
        "p_primary": avail_row("p_primary", DELIVERY_DAY, 30.0),
        "p_lower": avail_row("p_lower", DELIVERY_DAY, 50.0),
        "p_standby": avail_row("p_standby", DELIVERY_DAY, 100.0),
    }
    rel = {"p_primary": 0.98, "p_lower": 0.90, "p_standby": 0.95}
    co2 = {"p_primary": 1.10, "p_lower": 1.00, "p_standby": 1.15}
    wire_mocks(monkeypatch, products, prices, avail, reliability=rel, co2=co2)

    item = NeedItem(query="lactose-free milk", quantity=100, unit="L", constraints=Constraint(lactose_free=True))
    assert substitute.match_score(item, p_primary) > substitute.match_score(item, p_standby) > substitute.match_score(item, p_lower_match)

    avail["p_standby"] = avail_row("p_standby", DELIVERY_DAY, 30.0)
    wire_mocks(monkeypatch, products, prices, avail, reliability=rel, co2=co2)

    group = offers.get_offers(
        Need(customer="hotel-aurora", deliver_by=DELIVERY_DAY, items=[item]),
        now=ORDER_NOW,
    ).items[0]

    assert group.shortfall == 0.0
    assert group.options == []
    assert [(o.product_id, o.units_needed) for o in group.offers] == [
        ("p_primary", 30.0),
        ("p_standby", 30.0),
        ("p_lower", 20.0),
    ]
    first, second, third = group.offers
    assert first.substitute_for is None
    assert first.tier is None

    assert second.substitute_for == "p_primary"
    assert second.tier == "good"
    assert second.notes[0] == substitute.tradeoff(second, first)
    assert "50% pricier per l" in second.notes[0]

    assert third.substitute_for == "p_primary"
    assert third.tier == "good"
    assert third.notes[0] == substitute.tradeoff(third, first)
    assert "17% cheaper per l" in third.notes[0]
    assert "2 l packs instead of 1 l packs" in third.notes[0]
    assert "reliability 0.9 vs 0.98" in third.notes[0]
    assert "CO2 1 vs 1.1 kg/kg" in third.notes[0]


def test_split_allocation_outside_brand_constraint_vs_same_brand_equal_match(monkeypatch):
    p_valio = make_product("p_valio", "Lactose free semi-skimmed milk 1 L", brand="Valio", size_value=1.0)
    p_arla = make_product("p_arla", "Milk", brand="Arla", size_value=1.0)
    p_valio_eq = make_product("p_valio_eq", "Milk", brand="Valio", size_value=1.0)

    wire_mocks(
        monkeypatch,
        [p_valio, p_arla],
        {"p_valio": price_row("p_valio", 1.10), "p_arla": price_row("p_arla", 1.35)},
        {"p_valio": avail_row("p_valio", DELIVERY_DAY, 25.0), "p_arla": avail_row("p_arla", DELIVERY_DAY, 80.0)},
        reliability={"p_valio": 0.96, "p_arla": 0.94},
    )
    brand_item = NeedItem(
        query="lactose-free milk",
        quantity=60,
        unit="L",
        constraints=Constraint(lactose_free=True, brands=["Valio"]),
    )
    assert substitute.match_score(brand_item, p_arla) >= substitute.match_score(brand_item, p_valio)

    brand_group = offers.get_offers(
        Need(customer="hotel-aurora", deliver_by=DELIVERY_DAY, items=[brand_item]),
        now=ORDER_NOW,
    ).items[0]
    assert [(o.product_id, o.units_needed) for o in brand_group.offers] == [("p_valio", 25.0), ("p_arla", 35.0)]
    assert brand_group.offers[0].substitute_for is None
    assert brand_group.offers[1].substitute_for == "p_valio"
    assert brand_group.offers[1].tier == "good"
    assert brand_group.offers[1].notes[0] == substitute.tradeoff(brand_group.offers[1], brand_group.offers[0])

    wire_mocks(
        monkeypatch,
        [p_valio, p_valio_eq],
        {"p_valio": price_row("p_valio", 1.10), "p_valio_eq": price_row("p_valio_eq", 1.35)},
        {"p_valio": avail_row("p_valio", DELIVERY_DAY, 25.0), "p_valio_eq": avail_row("p_valio_eq", DELIVERY_DAY, 80.0)},
        reliability={"p_valio": 0.96, "p_valio_eq": 0.94},
    )
    same_brand_group = offers.get_offers(
        Need(customer="hotel-aurora", deliver_by=DELIVERY_DAY, items=[brand_item]),
        now=ORDER_NOW,
    ).items[0]
    assert [(o.product_id, o.units_needed) for o in same_brand_group.offers] == [("p_valio", 25.0), ("p_valio_eq", 35.0)]
    assert same_brand_group.offers[1].substitute_for is None
    assert same_brand_group.offers[1].tier is None
    assert same_brand_group.offers[1].notes[0] == substitute.tradeoff(
        same_brand_group.offers[1], same_brand_group.offers[0]
    )


def test_shortfall_populates_partial_late_and_split_options_with_why(monkeypatch):
    p_milk = make_product("p_milk", "Valio lactose free milk 1 L", size_value=1.0)
    late_day = DELIVERY_DAY + timedelta(days=3)

    wire_mocks(
        monkeypatch,
        [p_milk],
        {"p_milk": price_row("p_milk", 1.25)},
        {
            "p_milk": avail_row(
                "p_milk",
                DELIVERY_DAY,
                35.0,
                extra_lines=[
                    {
                        "salesBlock": False,
                        "availableAmount": 120.0,
                        "availableDate": late_day.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            )
        },
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=100, unit="L")],
    )
    group = offers.get_offers(need, now=ORDER_NOW).items[0]

    assert group.shortfall == 65.0
    assert [(o.product_id, o.units_needed) for o in group.offers] == [("p_milk", 35.0)]
    assert group.alternatives == []

    by_kind = {opt["kind"]: opt for opt in group.options}
    assert set(by_kind) == {"partial", "late", "split"}
    assert by_kind["partial"] == {
        "kind": "partial",
        "date": DELIVERY_DAY,
        "covered": 35.0,
        "shortfall": 65.0,
        "why": f"35 L of 100 L by {DELIVERY_DAY}, 65 L short",
    }
    assert by_kind["late"] == {
        "kind": "late",
        "date": late_day,
        "days_late": 3,
        "why": f"all 100 L available by {late_day}, 3 days after {DELIVERY_DAY}",
    }
    assert by_kind["split"] == {
        "kind": "split",
        "date": late_day,
        "on_time": 35.0,
        "later": 65.0,
        "days_late": 3,
        "why": f"35 L by {DELIVERY_DAY}, remaining 65 L on {late_day}, 3 days late",
    }


def test_shortfall_options_partial_only_and_late_only_modes(monkeypatch):
    p_milk = make_product("p_milk", "Valio lactose free milk 1 L", size_value=1.0)
    next_day = DELIVERY_DAY + timedelta(days=1)

    wire_mocks(
        monkeypatch,
        [p_milk],
        {"p_milk": price_row("p_milk", 1.25)},
        {"p_milk": avail_row("p_milk", DELIVERY_DAY, 20.0)},
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=50, unit="L")],
    )
    partial_group = offers.get_offers(need, now=ORDER_NOW).items[0]
    assert partial_group.shortfall == 30.0
    assert [o["kind"] for o in partial_group.options] == ["partial"]
    assert partial_group.options[0]["why"] == f"20 L of 50 L by {DELIVERY_DAY}, 30 L short"

    wire_mocks(
        monkeypatch,
        [p_milk],
        {"p_milk": price_row("p_milk", 1.25)},
        {"p_milk": avail_row("p_milk", next_day, 80.0)},
    )
    late_group = offers.get_offers(need, now=ORDER_NOW).items[0]
    assert late_group.offers == []
    assert late_group.shortfall == 50.0
    assert [o["kind"] for o in late_group.options] == ["late"]
    assert late_group.options[0]["days_late"] == 1
    assert late_group.options[0]["why"] == f"all 50 L available by {next_day}, 1 day after {DELIVERY_DAY}"


def test_shortfall_safe_alternatives_dairy_milk_excluded_swaps_to_oat_drink(monkeypatch):
    p_oat_carton = make_product(
        "p_oat_1l",
        "Oddlygood oat drink 1 L",
        brand="Oddlygood",
        category="10400",
        size_value=1.0,
        allergens=["oats"],
        labels=["vegan", "free_from_lactose"],
    )
    p_oat_bulk = make_product(
        "p_oat_10l",
        "Oddlygood oat drink 10 L",
        brand="Oddlygood",
        category="10400",
        size_value=10.0,
        allergens=["oats"],
        labels=["vegan", "free_from_lactose"],
    )

    def search_by_query(q: str, c: Constraint, limit: int) -> list[Product]:
        if "oat" in q.lower():
            return [p_oat_carton, p_oat_bulk]
        return []

    wire_mocks(
        monkeypatch,
        [p_oat_carton, p_oat_bulk],
        {"p_oat_1l": price_row("p_oat_1l", 1.85), "p_oat_10l": price_row("p_oat_10l", 14.50)},
        {
            "p_oat_1l": avail_row("p_oat_1l", DELIVERY_DAY, 200.0),
            "p_oat_10l": avail_row("p_oat_10l", DELIVERY_DAY, 50.0),
        },
        search_fn=search_by_query,
    )

    need = Need(
        customer="hospital-tays",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=30, unit="L", constraints=Constraint(exclude_allergens=["milk"]))],
    )
    group = offers.get_offers(need, now=ORDER_NOW).items[0]

    assert group.shortfall == 30.0
    assert len(group.offers) == 1
    chosen = group.offers[0]
    assert chosen.product_id == "p_oat_10l"
    assert chosen.units_needed == 3.0
    assert chosen.tier == "good"
    assert chosen.substitute_for is None
    assert chosen.notes[0] == "Swap for milk: dairy free, slightly sweeter"


def test_shortfall_safe_alternatives_for_gluten_flour_and_partial_remaining_quantity(monkeypatch):
    p_gf_flour = make_product(
        "p_gf_flour",
        "Virtasalmen gluten-free flour mix 1 kg",
        brand="Virtasalmen",
        category="10500",
        size_value=1.0,
        size_unit="kg",
        allergens=["may_contain:soy"],
        labels=["free_from_gluten"],
    )
    p_lf_milk = make_product(
        "p_lf_milk",
        "Valio lactose free milk 1 L",
        brand="Valio",
        category="10400",
        size_value=1.0,
        size_unit="l",
        allergens=["milk"],
        labels=["free_from_lactose"],
    )
    p_plain_milk_partial = make_product(
        "p_lf_named",
        "Laktoositon maito 1 L",
        brand="Valio",
        category="10400",
        size_value=1.0,
        size_unit="l",
        allergens=["milk"],
        labels=["free_from_lactose"],
    )

    def search_fn(q: str, c: Constraint, limit: int) -> list[Product]:
        ql = q.lower()
        if "gluten-free flour" in ql:
            return [p_gf_flour]
        if ql == "wheat flour":
            return []
        if "lactose-free milk" in ql:
            return [p_lf_milk]
        if ql == "milk":
            return [p_plain_milk_partial]
        return []

    wire_mocks(
        monkeypatch,
        [p_gf_flour, p_lf_milk, p_plain_milk_partial],
        {
            "p_gf_flour": price_row("p_gf_flour", 3.40),
            "p_lf_milk": price_row("p_lf_milk", 1.35),
            "p_lf_named": price_row("p_lf_named", 1.15),
        },
        {
            "p_gf_flour": avail_row("p_gf_flour", DELIVERY_DAY, 100.0),
            "p_lf_milk": avail_row("p_lf_milk", DELIVERY_DAY, 100.0),
            "p_lf_named": avail_row("p_lf_named", DELIVERY_DAY, 15.0),
        },
        search_fn=search_fn,
    )

    need = Need(
        customer="school-hervanta",
        deliver_by=DELIVERY_DAY,
        items=[
            NeedItem(query="wheat flour", quantity=12, unit="kg", constraints=Constraint(exclude_allergens=["gluten"])),
            NeedItem(query="milk", quantity=40, unit="L", constraints=Constraint(lactose_free=True)),
        ],
    )
    resp = offers.get_offers(need, now=ORDER_NOW)
    flour_group, milk_group = resp.items

    assert flour_group.shortfall == 12.0
    assert len(flour_group.offers) == 1
    assert flour_group.offers[0].product_id == "p_gf_flour"
    assert flour_group.offers[0].units_needed == 12.0
    assert flour_group.offers[0].tier == "good"
    assert flour_group.offers[0].substitute_for is None
    assert flour_group.offers[0].notes[0] == "Swap for wheat flour: 1:1 mix, batter a little denser"

    assert milk_group.shortfall == 25.0
    assert [(o.product_id, o.units_needed) for o in milk_group.offers] == [
        ("p_lf_named", 15.0),
        ("p_lf_milk", 25.0),
    ]
    assert milk_group.offers[1].tier == "good"
    assert milk_group.offers[1].substitute_for is None
    assert milk_group.offers[1].notes[0] == "Swap for milk: same recipe, lactose free"


def test_mandate_allergen_merging_drops_unsafe_candidates_and_prepends_audit_note(monkeypatch):
    p_peanut = make_product(
        "p_peanut",
        "Oddlygood oat drink 1 L",
        brand="Oddlygood",
        allergens=["oats", "peanuts"],
        labels=["vegan"],
    )
    p_trace_egg = make_product(
        "p_trace_egg",
        "Oddlygood oat drink 1 L barista",
        brand="Oddlygood",
        allergens=["oats", "may_contain:egg"],
        labels=["vegan"],
    )
    p_no_meta = Product(
        product_id="p_no_meta",
        name="Oddlygood oat drink 1 L unverified",
        brand="Oddlygood",
        category="10400",
        sales_unit="ST",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=[],
        labels=[],
    )
    p_soy = make_product(
        "p_soy",
        "Oddlygood oat drink 1 L soy",
        brand="Oddlygood",
        allergens=["oats", "soy"],
        labels=["vegan"],
    )
    p_safe = make_product(
        "p_safe",
        "Oddlygood oat drink 1 L pure",
        brand="Oddlygood",
        allergens=["oats"],
        labels=["vegan"],
    )

    products = [p_peanut, p_trace_egg, p_no_meta, p_soy, p_safe]
    prices = {
        "p_peanut": price_row("p_peanut", 0.90),
        "p_trace_egg": price_row("p_trace_egg", 0.95),
        "p_no_meta": price_row("p_no_meta", 1.00),
        "p_soy": price_row("p_soy", 1.05),
        "p_safe": price_row("p_safe", 1.60),
    }
    avail = {p.product_id: avail_row(p.product_id, DELIVERY_DAY, 150.0) for p in products}
    seen_searches: list[tuple[str, Constraint]] = []
    wire_mocks(monkeypatch, products, prices, avail, seen_searches=seen_searches)

    item_constraints = Constraint(exclude_allergens=["soy", "egg"])
    mandate = Mandate(
        customer="school-hervanta",
        agent_id="agent-hervanta",
        per_order_eur=1500.0,
        per_week_eur=5000.0,
        exclude_allergens=["egg", "nuts"],
    )
    need = Need(
        customer="school-hervanta",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="oat drink", quantity=25, unit="L", constraints=item_constraints)],
    )

    group = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]

    assert item_constraints.exclude_allergens == ["soy", "egg"]
    assert mandate.exclude_allergens == ["egg", "nuts"]
    assert seen_searches[0][1].exclude_allergens == ["soy", "egg", "nuts"]
    assert group.need_item.constraints.exclude_allergens == ["soy", "egg", "nuts"]
    assert [o.product_id for o in group.offers] == ["p_safe"]
    assert group.offers[0].notes[0] == "Excluded 4 products containing soy, egg, nuts (mandate)"
    assert group.alternatives == []


def test_mandate_milk_exclusion_drops_dairy_and_falls_back_to_oat_with_both_notes(monkeypatch):
    p_dairy_a = make_product("p_dairy_a", "Valio lactose free milk 1 L", allergens=["milk"], labels=["free_from_lactose"])
    p_dairy_b = make_product("p_dairy_b", "Valio milk 10 L", size_value=10.0, allergens=["milk"], labels=[])
    p_oat = make_product(
        "p_oat",
        "Oddlygood oat drink 1 L",
        brand="Oddlygood",
        allergens=["oats"],
        labels=["vegan"],
    )

    def search_fn(q: str, c: Constraint, limit: int) -> list[Product]:
        if "oat" in q.lower():
            return [p_oat]
        return [p_dairy_a, p_dairy_b]

    wire_mocks(
        monkeypatch,
        [p_dairy_a, p_dairy_b, p_oat],
        {
            "p_dairy_a": price_row("p_dairy_a", 1.10),
            "p_dairy_b": price_row("p_dairy_b", 10.50),
            "p_oat": price_row("p_oat", 1.75),
        },
        {
            "p_dairy_a": avail_row("p_dairy_a", DELIVERY_DAY, 200.0),
            "p_dairy_b": avail_row("p_dairy_b", DELIVERY_DAY, 200.0),
            "p_oat": avail_row("p_oat", DELIVERY_DAY, 200.0),
        },
        search_fn=search_fn,
    )

    mandate = Mandate(
        customer="hospital-tays",
        agent_id="agent-tays",
        per_order_eur=2000.0,
        per_week_eur=8000.0,
        exclude_allergens=["milk"],
    )
    need = Need(
        customer="hospital-tays",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=20, unit="L")],
    )
    group = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]

    assert [o.product_id for o in group.offers] == ["p_oat"]
    assert group.offers[0].tier == "good"
    assert group.offers[0].substitute_for is None
    assert group.offers[0].notes[0] == "Excluded 2 products containing milk (mandate)"
    assert group.offers[0].notes[1] == "Swap for milk: dairy free, slightly sweeter"


def test_mandate_no_dropped_candidates_omits_excluded_note_and_simulation_reranks_cleanly(monkeypatch):
    p_novobox = make_product(
        "p_novo",
        "Oddlygood oat drink 10 L",
        brand="Oddlygood",
        size_value=10.0,
        allergens=["oats"],
        labels=["vegan"],
    )
    p_carton = make_product(
        "p_carton",
        "Oddlygood oat drink 1 L",
        brand="Oddlygood",
        size_value=1.0,
        allergens=["oats"],
        labels=["vegan"],
    )
    wire_mocks(
        monkeypatch,
        [p_novobox, p_carton],
        {"p_novo": price_row("p_novo", 12.00), "p_carton": price_row("p_carton", 1.60)},
        {"p_novo": avail_row("p_novo", DELIVERY_DAY, 20.0), "p_carton": avail_row("p_carton", DELIVERY_DAY, 200.0)},
    )
    mandate = Mandate(
        customer="hotel-aurora",
        agent_id="agent-aurora",
        per_order_eur=2000.0,
        per_week_eur=8000.0,
        exclude_allergens=["milk", "soy"],
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="oat drink", quantity=50, unit="L")],
    )

    initial = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]
    assert [o.product_id for o in initial.offers] == ["p_novo"]
    assert not any(n.startswith("Excluded ") for n in initial.offers[0].notes)
    assert [(a.product_id, a.tier) for a in initial.alternatives] == [("p_carton", "good")]

    offers._overrides["p_novo"] = SimulateStock(product_id="p_novo", available_amount=2.0, available_date=DELIVERY_DAY)
    split = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]
    assert [(o.product_id, o.units_needed) for o in split.offers] == [("p_novo", 2.0), ("p_carton", 30.0)]
    assert split.shortfall == 0.0
    assert split.alternatives == []


def test_rank_substitutes_without_base_offer_and_custom_limit():
    item = NeedItem(query="lactose-free milk", quantity=40, unit="L", constraints=Constraint(lactose_free=True))

    def mk_offer(pid: str, name: str, net: float, size_l: float, rel: float | None, co2: float | None) -> Offer:
        prod = make_product(pid, name, size_value=size_l, size_unit="l")
        units = 40.0 / size_l
        return Offer(
            product_id=pid,
            product=prod,
            units_needed=units,
            unit_price_net=net,
            total_net=round(units * net, 2),
            total_gross=round(units * net * 1.14, 2),
            available_amount=100.0,
            available_date=DELIVERY_DAY,
            reliability=rel,
            co2_per_kg=co2,
        )

    c_good = mk_offer("c_good", "Valio lactose free portion milk 10 L", 10.50, 10.0, 0.88, 0.96)
    c_better = mk_offer("c_better", "Valio lactose free milk 2 L", 2.60, 2.0, 0.99, 1.04)
    c_best = mk_offer("c_best", "Valio milk", 1.75, 1.0, 1.00, 1.12)
    c_fourth = mk_offer("c_fourth", "Valio lactose free milk 1 L extra", 1.70, 1.0, 0.82, 1.25)

    ranked_all = substitute.rank_substitutes(item, [c_fourth, c_best, c_better, c_good], [], original=None, limit=3)
    assert [(o.product_id, o.tier) for o in ranked_all] == [
        ("c_good", "good"),
        ("c_better", "better"),
        ("c_best", "best"),
    ]
    assert ranked_all[0].notes[0] == "cheapest acceptable: 1.05 EUR per L, 10 L packs, reliability 0.88, CO2 0.96 kg/kg"
    assert ranked_all[1].notes[0] == "balanced: 1.30 EUR per L, 2 L packs, reliability 0.99, CO2 1.04 kg/kg"
    assert ranked_all[2].notes[0] == "closest match: 1.75 EUR per L, 1 L packs, reliability 1, CO2 1.12 kg/kg"

    ranked_two = substitute.rank_substitutes(item, [c_fourth, c_best, c_better, c_good], [], original=None, limit=2)
    assert [(o.product_id, o.tier) for o in ranked_two] == [("c_good", "good"), ("c_better", "better")]


def test_tradeoff_formatting_sales_unit_match_same_price_and_zero_base():
    p_prk_a = make_product("a", "Oat bread pack", category="10600", sales_unit="PRK", size_value=None)
    p_prk_b = make_product("b", "Oat bread pack alt", category="10600", sales_unit="PRK", size_value=None)
    o_a = Offer(
        product_id="a",
        product=p_prk_a,
        units_needed=5.0,
        unit_price_net=2.50,
        total_net=12.50,
        total_gross=14.25,
        available_amount=50.0,
        reliability=0.95,
        co2_per_kg=0.80,
    )
    o_b_same = Offer(
        product_id="b",
        product=p_prk_b,
        units_needed=5.0,
        unit_price_net=2.50,
        total_net=12.50,
        total_gross=14.25,
        available_amount=50.0,
        reliability=0.95,
        co2_per_kg=0.80,
    )
    assert substitute.tradeoff(o_b_same, o_a) == "same price per PRK"

    o_zero = o_a.model_copy(update={"unit_price_net": 0.0, "total_net": 0.0, "total_gross": 0.0})
    o_zero_b = o_b_same.model_copy(update={"unit_price_net": 0.0, "total_net": 0.0, "total_gross": 0.0})
    assert substitute.tradeoff(o_zero_b, o_zero) == "same price, pack size, reliability and CO2 as Oat bread pack"


def test_split_same_product_across_two_schedule_lines_does_not_mark_substitute_for(monkeypatch):
    p_milk = make_product("p_milk", "Valio lactose free milk 1 L", size_value=1.0)
    day_before = DELIVERY_DAY - timedelta(days=1)

    wire_mocks(
        monkeypatch,
        [p_milk],
        {"p_milk": price_row("p_milk", 1.25)},
        {
            "p_milk": avail_row(
                "p_milk",
                day_before,
                30.0,
                extra_lines=[
                    {
                        "salesBlock": False,
                        "availableAmount": 80.0,
                        "availableDate": DELIVERY_DAY.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            )
        },
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=70, unit="L")],
    )
    group = offers.get_offers(need, now=ORDER_NOW).items[0]

    assert group.shortfall == 0.0
    assert [(o.product_id, o.available_date, o.units_needed) for o in group.offers] == [
        ("p_milk", day_before, 30.0),
        ("p_milk", DELIVERY_DAY, 40.0),
    ]
    assert all(o.substitute_for is None and o.tier is None for o in group.offers)


def test_skipped_blocked_and_empty_candidates_recorded_after_mandate_audit_note(monkeypatch):
    p_unsafe = make_product("p_unsafe", "Valio oat drink nutty 1 L", allergens=["oats", "peanuts"], labels=["vegan"])
    p_blocked = make_product("p_blocked", "Oddlygood oat drink 1 L blocked", allergens=["oats"], labels=["vegan"])
    p_empty = make_product("p_empty", "Oddlygood oat drink 1 L empty", allergens=["oats"], labels=["vegan"])
    p_good = make_product("p_good", "Oddlygood oat drink 1 L ready", allergens=["oats"], labels=["vegan"])

    products = [p_unsafe, p_blocked, p_empty, p_good]
    prices = {
        "p_unsafe": price_row("p_unsafe", 0.90),
        "p_blocked": price_row("p_blocked", 1.10),
        "p_empty": price_row("p_empty", 1.20),
        "p_good": price_row("p_good", 1.50),
    }
    avail = {
        "p_unsafe": avail_row("p_unsafe", DELIVERY_DAY, 100.0),
        "p_blocked": avail_row("p_blocked", DELIVERY_DAY, 100.0, sales_block=True),
        "p_empty": avail_row("p_empty", DELIVERY_DAY, 0.0),
        "p_good": avail_row("p_good", DELIVERY_DAY, 100.0),
    }
    wire_mocks(monkeypatch, products, prices, avail)

    mandate = Mandate(
        customer="school-hervanta",
        agent_id="agent-hervanta",
        per_order_eur=1000.0,
        per_week_eur=4000.0,
        exclude_allergens=["nuts"],
    )
    need = Need(
        customer="school-hervanta",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="oat drink", quantity=15, unit="L")],
    )
    group = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]

    assert [o.product_id for o in group.offers] == ["p_good"]
    notes = group.offers[0].notes
    assert notes[0] == "Excluded 1 products containing nuts (mandate)"
    assert any("skipped Oddlygood oat drink 1 L blocked (p_blocked): sales blocked: blocked" in n for n in notes)
    assert any("skipped Oddlygood oat drink 1 L empty (p_empty): no stock" in n for n in notes)


def test_safe_alternatives_cream_butter_pasta_oats_and_blocked_when_swap_unsafe(monkeypatch):
    p_oat_cook = make_product(
        "p_oat_cook",
        "Oddlygood oat cooking 1 L",
        brand="Oddlygood",
        category="10410",
        size_value=1.0,
        size_unit="l",
        allergens=["oats"],
        labels=["vegan"],
    )
    p_marg = make_product(
        "p_marg",
        "Keiju lactose free margarine 1 kg",
        brand="Keiju",
        category="10420",
        size_value=1.0,
        size_unit="kg",
        allergens=[],
        labels=["vegan", "free_from_lactose"],
    )
    p_gf_pasta = make_product(
        "p_gf_pasta",
        "Semper gluten-free pasta 1 kg",
        brand="Semper",
        category="10510",
        size_value=1.0,
        size_unit="kg",
        allergens=[],
        labels=["free_from_gluten"],
    )
    p_gf_oats = make_product(
        "p_gf_oats",
        "Provenance gluten-free oat flakes 1 kg",
        brand="Provenance",
        category="10520",
        size_value=1.0,
        size_unit="kg",
        allergens=["oats"],
        labels=["free_from_gluten"],
    )
    p_oat_drink = make_product(
        "p_oat_drink",
        "Oddlygood oat drink 1 L",
        brand="Oddlygood",
        category="10400",
        size_value=1.0,
        size_unit="l",
        allergens=["oats"],
        labels=["vegan"],
    )

    def search_fn(q: str, c: Constraint, limit: int) -> list[Product]:
        ql = q.lower()
        if ql == "oat cooking":
            return [p_oat_cook]
        if ql == "lactose free margarine":
            return [p_marg]
        if ql == "gluten-free pasta":
            return [p_gf_pasta]
        if ql == "gluten-free oats":
            return [p_gf_oats]
        if ql == "oat drink":
            return [p_oat_drink]
        return []

    all_prods = [p_oat_cook, p_marg, p_gf_pasta, p_gf_oats, p_oat_drink]
    wire_mocks(
        monkeypatch,
        all_prods,
        {p.product_id: price_row(p.product_id, 2.20) for p in all_prods},
        {p.product_id: avail_row(p.product_id, DELIVERY_DAY, 100.0) for p in all_prods},
        search_fn=search_fn,
    )

    need = Need(
        customer="hospital-tays",
        deliver_by=DELIVERY_DAY,
        items=[
            NeedItem(query="cooking cream", quantity=10, unit="L", constraints=Constraint(exclude_allergens=["milk"])),
            NeedItem(query="butter", quantity=5, unit="kg", constraints=Constraint(exclude_allergens=["dairy"])),
            NeedItem(query="lasagne", quantity=8, unit="kg", constraints=Constraint(exclude_allergens=["wheat"])),
            NeedItem(query="oat flakes", quantity=6, unit="kg", constraints=Constraint(gluten_free=True)),
            NeedItem(query="milk", quantity=10, unit="L", constraints=Constraint(exclude_allergens=["milk", "oats"])),
            NeedItem(query="milk", quantity=10, unit="L", constraints=Constraint(exclude_allergens=["egg"])),
        ],
    )
    groups = offers.get_offers(need, now=ORDER_NOW).items

    assert groups[0].offers[0].product_id == "p_oat_cook"
    assert groups[0].offers[0].tier == "good"
    assert groups[0].offers[0].notes[0] == "Swap for cooking cream: dairy free, thinner sauce"

    assert groups[1].offers[0].product_id == "p_marg"
    assert groups[1].offers[0].tier == "good"
    assert groups[1].offers[0].notes[0] == "Swap for butter: dairy free, for baking and cooking"

    assert groups[2].offers[0].product_id == "p_gf_pasta"
    assert groups[2].offers[0].tier == "good"
    assert groups[2].offers[0].notes[0] == "Swap for lasagne: same cooking use, shorter cook time"

    assert groups[3].offers[0].product_id == "p_gf_oats"
    assert groups[3].offers[0].tier == "good"
    assert groups[3].offers[0].notes[0] == "Swap for oat flakes: certified gluten free"

    assert groups[4].offers == []
    assert groups[4].shortfall == 10.0

    assert groups[5].offers == []
    assert groups[5].shortfall == 10.0


def test_fresh_offers_ranks_by_normalized_price_and_deduplicates_across_seeds(monkeypatch):
    p_seed_a = make_product("p_a", "Valio lactose free milk 1 L", sales_unit="ST", size_value=1.0)
    p_seed_b = make_product("p_b", "Valio lactose free milk 10 L", sales_unit="ST", size_value=10.0)

    wire_mocks(
        monkeypatch,
        [p_seed_a, p_seed_b],
        {"p_a": price_row("p_a", 1.50), "p_b": price_row("p_b", 1.20)},
        {"p_a": avail_row("p_a", DELIVERY_DAY, 100.0), "p_b": avail_row("p_b", DELIVERY_DAY, 100.0)},
    )
    res = offers.fresh_offers("hotel-aurora", DELIVERY_DAY, ["p_a", "p_b", "missing_pid"])
    assert [o.product_id for o in res] == ["p_b", "p_a"]


def test_unknown_mandate_allergen_fails_closed_with_full_shortfall(monkeypatch):
    p_milk = make_product("p_milk", "Valio lactose free milk 1 L", allergens=["milk"], labels=["free_from_lactose"])
    wire_mocks(
        monkeypatch,
        [p_milk],
        {"p_milk": price_row("p_milk", 1.25)},
        {"p_milk": avail_row("p_milk", DELIVERY_DAY, 100.0)},
    )
    mandate = Mandate(
        customer="hospital-tays",
        agent_id="agent-tays",
        per_order_eur=1000.0,
        per_week_eur=4000.0,
        exclude_allergens=["unregistered_allergen"],
    )
    need = Need(
        customer="hospital-tays",
        deliver_by=DELIVERY_DAY,
        items=[NeedItem(query="milk", quantity=25, unit="L")],
    )
    group = offers.get_offers(need, mandate=mandate, now=ORDER_NOW).items[0]

    assert group.offers == []
    assert group.alternatives == []
    assert group.shortfall == 25.0

