from datetime import date, datetime, time, timedelta, timezone

import pytest

from aimo import catalog, offers, substitute, valio
from aimo.models import (
    Constraint,
    Mandate,
    Need,
    NeedItem,
    Offer,
    Product,
    QuoteRequestLine,
    SimulateStock,
    UnitSize,
)
from aimo.quotes import QuoteError


def _milk_product(
    pid: str,
    name: str = "Lactose free milk 1L",
    brand: str = "Valio",
    allergens: list[str] | None = None,
    labels: list[str] | None = None,
    size_l: float = 1.0,
) -> Product:
    return Product(
        product_id=pid,
        name=name,
        brand=brand,
        category="10400",
        sales_unit="ST",
        unit_size=UnitSize(value=size_l, unit="l"),
        allergens=allergens if allergens is not None else ["milk"],
        labels=labels if labels is not None else ["free_from_lactose"],
    )


def _price_entry(pid: str, net: float, gross: float | None = None) -> dict:
    g = gross if gross is not None else round(net * 1.14, 2)
    return {
        "productId": pid,
        "currency": "EUR",
        "catalogPrice": {"value": net},
        "priceWithTax": {"value": g},
    }


def _avail_entry(
    pid: str,
    day: date,
    amount: float = 500.0,
    closing: str | None = "2026-11-12T15:00:00+0200",
    sales_block: bool = False,
    extra_lines: list[dict] | None = None,
) -> dict:
    lines = [
        {
            "salesBlock": sales_block,
            "availableAmount": amount,
            "availableDate": day.isoformat(),
            "salesUnit": "ST",
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


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "unit.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_data"))
    catalog.reset()
    offers.clear_simulation()
    yield
    offers.clear_simulation()
    catalog.reset()


@pytest.fixture
def future_day() -> date:
    return datetime.now(offers.HEL).date() + timedelta(days=5)


def _wire(
    monkeypatch,
    products: list[Product],
    prices: dict[str, dict],
    avail: dict[str, dict],
    rel: dict[str, float] | None = None,
    co2: dict[str, float] | None = None,
    seen_constraints: list[Constraint] | None = None,
):
    by_id = {p.product_id: p for p in products}
    rel_map = rel or {}
    co2_map = co2 or {}

    def fake_search(q: str, constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        if seen_constraints is not None and constraints is not None:
            seen_constraints.append(constraints)
        if "oat" in q.lower() and not any("oat" in p.name.lower() for p in products):
            return []
        return list(products[:limit])

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: rel_map.get(str(pid)))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: co2_map.get(str(pid)))
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {i: prices[i] for i in ids if i in prices})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {i: avail[i] for i in ids if i in avail})


def test_get_offers_normal_ranking(monkeypatch, future_day):
    p_exp = _milk_product("p_exp", "Lactose free milk 1L")
    p_best = _milk_product("p_best", "Lactose free milk 1L")
    p_low_rel = _milk_product("p_low_rel", "Lactose free milk 1L")
    _wire(
        monkeypatch,
        [p_exp, p_low_rel, p_best],
        {
            "p_exp": _price_entry("p_exp", 1.80),
            "p_best": _price_entry("p_best", 1.20),
            "p_low_rel": _price_entry("p_low_rel", 1.20),
        },
        {
            "p_exp": _avail_entry("p_exp", future_day, 500.0),
            "p_best": _avail_entry("p_best", future_day, 500.0),
            "p_low_rel": _avail_entry("p_low_rel", future_day, 500.0),
        },
        rel={"p_exp": 0.99, "p_best": 0.97, "p_low_rel": 0.80},
        co2={"p_exp": 1.1, "p_best": 1.0, "p_low_rel": 1.2},
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=50, unit="L")],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(10, 0), tzinfo=offers.HEL)
    group = offers.get_offers(need, now=now).items[0]

    assert group.shortfall == 0.0
    assert [o.product_id for o in group.offers] == ["p_best"]
    assert group.offers[0].units_needed == 50.0
    assert group.offers[0].total_net == 60.0
    assert group.offers[0].reliability == 0.97
    assert {a.product_id for a in group.alternatives} == {"p_low_rel", "p_exp"}
    assert all(a.tier in ("good", "better", "best") for a in group.alternatives)
    assert group.options == []


def test_get_offers_cutoff_passed_shifts_date(monkeypatch, future_day):
    p1 = _milk_product("p1", "Lactose free milk 1L")
    _wire(
        monkeypatch,
        [p1],
        {"p1": _price_entry("p1", 1.30)},
        {"p1": _avail_entry("p1", future_day, 300.0, closing="2026-11-12T15:00:00+0200")},
    )
    late_now = datetime.combine(future_day - timedelta(days=1), time(16, 0), tzinfo=offers.HEL)
    need_strict = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=80, unit="L")],
    )
    g_strict = offers.get_offers(need_strict, now=late_now).items[0]
    assert g_strict.offers == []
    assert g_strict.shortfall == 80.0
    assert any(o["kind"] == "late" and o["date"] == future_day + timedelta(days=1) for o in g_strict.options)

    need_flex = Need(
        customer="hotel-aurora",
        deliver_by=future_day + timedelta(days=2),
        items=[NeedItem(query="lactose-free milk", quantity=80, unit="L")],
    )
    g_flex = offers.get_offers(need_flex, now=late_now).items[0]
    assert len(g_flex.offers) == 1
    assert g_flex.offers[0].available_date == future_day + timedelta(days=1)
    assert g_flex.offers[0].notes[0].startswith(f"cutoff passed for {future_day}")


def test_get_offers_partial_split_and_substitute_for(monkeypatch, future_day):
    p_primary = _milk_product("p_primary", "Lactose free milk 1L", brand="Valio")
    p_secondary = _milk_product("p_secondary", "Lactose free milk 1L organic extra fresh", brand="Arla")
    p_alt = _milk_product("p_alt", "Lactose free milk 1L classic", brand="Arla")
    _wire(
        monkeypatch,
        [p_primary, p_secondary, p_alt],
        {
            "p_primary": _price_entry("p_primary", 1.10),
            "p_secondary": _price_entry("p_secondary", 1.25),
            "p_alt": _price_entry("p_alt", 1.40),
        },
        {
            "p_primary": _avail_entry("p_primary", future_day, 40.0),
            "p_secondary": _avail_entry("p_secondary", future_day, 100.0),
            "p_alt": _avail_entry("p_alt", future_day, 100.0),
        },
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[
            NeedItem(
                query="lactose-free milk",
                quantity=100,
                unit="L",
                constraints=Constraint(brands=["Valio"]),
            )
        ],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)
    group = offers.get_offers(need, now=now).items[0]

    assert group.shortfall == 0.0
    assert [(o.product_id, o.units_needed) for o in group.offers] == [("p_primary", 40.0), ("p_secondary", 60.0)]
    assert group.offers[0].substitute_for is None
    assert group.offers[1].substitute_for == "p_primary"
    assert group.offers[1].tier == "good"
    assert [a.product_id for a in group.alternatives] == ["p_alt"]
    assert group.alternatives[0].tier == "good"


def test_get_offers_shortfall_rank_substitutes_fills_substitute_for(monkeypatch, future_day):
    p_primary = _milk_product("p_primary", "Lactose free milk 1L")
    p_sub = _milk_product("p_sub", "Lactose free milk 1L substitute", brand="Arla")
    _wire(
        monkeypatch,
        [p_primary],
        {"p_primary": _price_entry("p_primary", 1.15)},
        {"p_primary": _avail_entry("p_primary", future_day, 30.0)},
    )
    sub_offer = Offer(
        product_id="p_sub",
        product=p_sub,
        units_needed=70.0,
        unit_price_net=1.35,
        total_net=94.5,
        total_gross=107.73,
        available_amount=100.0,
        available_date=future_day,
        match_score=0.92,
        tier="good",
        notes=["cheapest acceptable"],
    )
    monkeypatch.setattr(
        substitute,
        "rank_substitutes",
        lambda item, pool, excl, original=None, limit=3: [sub_offer.model_copy(deep=True)],
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=100, unit="L")],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)
    group = offers.get_offers(need, now=now).items[0]

    assert [(o.product_id, o.units_needed) for o in group.offers] == [("p_primary", 30.0), ("p_sub", 70.0)]
    assert group.offers[0].substitute_for is None
    assert group.offers[1].substitute_for == "p_primary"


def test_get_offers_options_when_shortfall_positive(monkeypatch, future_day):
    p1 = _milk_product("p1", "Fresh milk 1L")
    later_day = future_day + timedelta(days=2)
    _wire(
        monkeypatch,
        [p1],
        {"p1": _price_entry("p1", 1.20)},
        {
            "p1": _avail_entry(
                "p1",
                future_day,
                40.0,
                extra_lines=[
                    {
                        "salesBlock": False,
                        "availableAmount": 200.0,
                        "availableDate": later_day.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            )
        },
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="milk", quantity=100, unit="L")],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)
    group = offers.get_offers(need, now=now).items[0]

    assert group.shortfall == 60.0
    by_kind = {o["kind"]: o for o in group.options}
    assert set(by_kind) == {"partial", "late", "split"}
    assert by_kind["partial"]["covered"] == 40.0
    assert by_kind["partial"]["shortfall"] == 60.0
    assert by_kind["late"]["date"] == later_day
    assert by_kind["late"]["days_late"] == 2
    assert by_kind["split"]["on_time"] == 40.0
    assert by_kind["split"]["later"] == 60.0


def test_get_offers_merges_mandate_exclude_allergens(monkeypatch, future_day):
    p_unsafe = Product(
        product_id="p_unsafe",
        name="Oat drink 1L nutty",
        brand="Oddlygood",
        category="10400",
        sales_unit="ST",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["oats", "peanuts"],
        labels=["vegan"],
    )
    p_safe = Product(
        product_id="p_safe",
        name="Oat drink 1L",
        brand="Oddlygood",
        category="10400",
        sales_unit="ST",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["oats"],
        labels=["vegan"],
    )
    seen: list[Constraint] = []
    _wire(
        monkeypatch,
        [p_unsafe, p_safe],
        {
            "p_unsafe": _price_entry("p_unsafe", 1.00),
            "p_safe": _price_entry("p_safe", 1.50),
        },
        {
            "p_unsafe": _avail_entry("p_unsafe", future_day, 200.0),
            "p_safe": _avail_entry("p_safe", future_day, 200.0),
        },
        seen_constraints=seen,
    )
    mandate = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-1",
        per_order_eur=2500.0,
        per_week_eur=10000.0,
        exclude_allergens=["soy", "nuts"],
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[
            NeedItem(
                query="oat drink",
                quantity=20,
                unit="L",
                constraints=Constraint(exclude_allergens=["soy"]),
            )
        ],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)
    group = offers.get_offers(need, mandate=mandate, now=now).items[0]

    assert seen and seen[0].exclude_allergens == ["soy", "nuts"]
    assert group.need_item.constraints.exclude_allergens == ["soy", "nuts"]
    assert [o.product_id for o in group.offers] == ["p_safe"]
    assert group.offers[0].notes[0] == "Excluded 1 products containing soy, nuts (mandate)"


def test_price_lines_happy_path_and_missing_catalog_file(monkeypatch, future_day):
    p1 = _milk_product("p1", "Milk 1L")
    p2 = _milk_product("p2", "Milk 1L Alt")
    monkeypatch.setattr(
        valio,
        "price",
        lambda c, d, ids: {
            "p1": _price_entry("p1", 1.50, 1.71),
            "p2": _price_entry("p2", 2.00, 2.28),
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {
            "p1": _avail_entry("p1", future_day, 100.0, closing="2026-11-12T15:00:00+0200"),
            "p2": _avail_entry("p2", future_day, 100.0, closing="2026-11-12T12:00:00+0200"),
        },
    )

    req_lines = [
        QuoteRequestLine(product_id="p1", quantity=10),
        QuoteRequestLine(product_id="p2", quantity=5, substitute_for="p0"),
    ]
    lines_no_data, gross_no_data, valid_no_data = offers.price_lines("hotel-aurora", future_day, req_lines)
    assert lines_no_data[0].name is None
    assert lines_no_data[0].sales_unit is None
    assert gross_no_data == 28.5
    assert offers.fresh_offers("hotel-aurora", future_day, ["p1"]) == []

    by_id = {"p1": p1, "p2": p2}
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    lines, gross, valid = offers.price_lines("hotel-aurora", future_day, req_lines)

    assert len(lines) == 2
    assert lines[0].name == "Milk 1L"
    assert lines[0].sales_unit == "ST"
    assert lines[0].line_total_net == 15.0
    assert lines[0].substitute_for is None
    assert lines[1].name == "Milk 1L Alt"
    assert lines[1].line_total_net == 10.0
    assert lines[1].substitute_for == "p0"
    assert gross == 28.5
    assert valid == datetime.combine(future_day - timedelta(days=1), time(12, 0), tzinfo=offers.HEL)
    assert valid == valid_no_data


def test_price_lines_past_delivery_and_helsinki_midnight(monkeypatch):
    past = datetime.now(offers.HEL).date() - timedelta(days=1)
    with pytest.raises(QuoteError) as exc:
        offers.price_lines("hotel-aurora", past, [QuoteRequestLine(product_id="p1", quantity=1)])
    assert exc.value.code == "INVALID_QUOTE"

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 11, 15, 22, 30, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(offers, "datetime", FrozenDateTime)
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"p1": _price_entry("p1", 1.20, 1.37)})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {"p1": _avail_entry("p1", d, 50.0)})

    with pytest.raises(QuoteError) as hel_exc:
        offers.price_lines("hotel-aurora", date(2026, 11, 15), [QuoteRequestLine(product_id="p1", quantity=2)])
    assert hel_exc.value.code == "INVALID_QUOTE"

    lines, gross, _ = offers.price_lines(
        "hotel-aurora",
        date(2026, 11, 16),
        [QuoteRequestLine(product_id="p1", quantity=2)],
    )
    assert lines[0].line_total_net == 2.4
    assert gross == 2.74


@pytest.mark.parametrize(
    "pid,price_map",
    [
        ("p_missing", {}),
        ("p_err", {"p_err": {"productId": "p_err", "error": "NOT_FOUND"}}),
        ("p_nocat", {"p_nocat": {"productId": "p_nocat", "catalogPrice": None}}),
    ],
)
def test_price_lines_missing_or_errored_price_raises_quote_error(monkeypatch, future_day, pid, price_map):
    monkeypatch.setattr(valio, "price", lambda c, d, ids: price_map)
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {})

    with pytest.raises(QuoteError) as exc:
        offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id=pid, quantity=3)])
    assert exc.value.code == "INVALID_QUOTE"
    assert exc.value.text == f"No price available for product {pid}."


def test_simulate_and_clear_simulation_override_availability(monkeypatch, future_day):
    p1 = _milk_product("p1", "Lactose free milk 1L")
    p2 = _milk_product("p2", "Lactose free milk 1L backup")
    _wire(
        monkeypatch,
        [p1, p2],
        {"p1": _price_entry("p1", 1.10), "p2": _price_entry("p2", 1.40)},
        {
            "p1": _avail_entry("p1", future_day, 500.0),
            "p2": _avail_entry("p2", future_day, 500.0),
        },
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=50, unit="L")],
    )
    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)

    assert [o.product_id for o in offers.get_offers(need, now=now).items[0].offers] == ["p1"]

    offers.simulate(SimulateStock(product_id="p1", available_amount=0.0))
    after_stockout = offers.get_offers(need, now=now).items[0]
    assert [o.product_id for o in after_stockout.offers] == ["p2"]

    offers.simulate(SimulateStock(product_id="p1", available_amount=20.0, available_date=future_day))
    after_partial = offers.get_offers(need, now=now).items[0]
    assert [(o.product_id, o.units_needed) for o in after_partial.offers] == [("p1", 20.0), ("p2", 30.0)]

    offers.clear_simulation()
    restored = offers.get_offers(need, now=now).items[0]
    assert [(o.product_id, o.units_needed) for o in restored.offers] == [("p1", 50.0)]


def test_quotes_route_returns_409_on_missing_price(monkeypatch, future_day):
    from fastapi.testclient import TestClient
    from aimo.app import app

    monkeypatch.setattr(valio, "price", lambda c, d, ids: {})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {})
    with TestClient(app) as client:
        resp = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "p_missing", "quantity": 2}], "delivery_date": future_day.isoformat()},
            headers={"X-Agent-Key": "demo-hotel-aurora"},
        )
    assert resp.status_code == 409
    assert resp.json() == {"code": "INVALID_QUOTE", "text": "No price available for product p_missing."}


def test_price_lines_rejects_simulated_stockout_and_sales_block(monkeypatch, future_day):
    monkeypatch.setattr(
        valio,
        "price",
        lambda c, d, ids: {"p1": _price_entry("p1", 1.50, 1.71), "p2": _price_entry("p2", 2.00, 2.28)},
    )
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {})

    lines_empty_avail, gross, _ = offers.price_lines(
        "hotel-aurora", future_day, [QuoteRequestLine(product_id="p1", quantity=5)]
    )
    assert len(lines_empty_avail) == 1
    assert gross == 8.55

    offers.simulate(SimulateStock(product_id="p1", available_amount=0.0))
    with pytest.raises(QuoteError) as exc_zero:
        offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="p1", quantity=1)])
    assert exc_zero.value.code == "OUT_OF_STOCK"
    assert exc_zero.value.text == "Insufficient stock for product p1."

    offers.simulate(SimulateStock(product_id="p1", available_amount=4.0))
    with pytest.raises(QuoteError) as exc_short:
        offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="p1", quantity=5)])
    assert exc_short.value.code == "OUT_OF_STOCK"

    ok_lines, _, _ = offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="p1", quantity=4)])
    assert ok_lines[0].quantity == 4

    offers.clear_simulation()
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {"p2": _avail_entry("p2", future_day, 100.0, sales_block=True)},
    )
    with pytest.raises(QuoteError) as exc_block:
        offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="p2", quantity=2)])
    assert exc_block.value.code == "OUT_OF_STOCK"
    assert exc_block.value.text == "Insufficient stock for product p2."


def test_quotes_route_returns_409_on_simulated_stockout(monkeypatch, future_day):
    from fastapi.testclient import TestClient
    from aimo.app import app

    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"p1": _price_entry("p1", 1.50, 1.71)})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {"p1": _avail_entry("p1", future_day, 100.0)})
    offers.simulate(SimulateStock(product_id="p1", available_amount=0.0))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "p1", "quantity": 2}], "delivery_date": future_day.isoformat()},
            headers={"X-Agent-Key": "demo-hotel-aurora"},
        )
    assert resp.status_code == 409
    assert resp.json() == {"code": "OUT_OF_STOCK", "text": "Insufficient stock for product p1."}


