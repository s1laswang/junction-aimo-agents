from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aimo import allocate, approvals, audit, catalog, keys, mandates, offers, orders, substitute, valio
from aimo.app import app
from aimo.models import (
    Constraint,
    Decision,
    Need,
    NeedItem,
    Order,
    PlaceOrderRequest,
    Product,
    QuoteLine,
    QuoteRequest,
    QuoteRequestLine,
    Reason,
    SimulateStock,
    Substitution,
    UnitSize,
)


def _product(
    pid: str,
    name: str = "Lactose free milk 1L",
    brand: str = "Valio",
    allergens: list[str] | None = None,
    labels: list[str] | None = None,
    size_l: float = 1.0,
    category: str = "10400",
    sales_unit: str = "ST",
) -> Product:
    return Product(
        product_id=pid,
        name=name,
        brand=brand,
        category=category,
        sales_unit=sales_unit,
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
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "unit_orders.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.setattr(valio, "_client", None)
    allocate._data.cache_clear()
    catalog.reset()
    offers._overrides.clear()
    offers.clear_simulation()
    keys._cache.clear()
    yield
    offers._overrides.clear()
    offers.clear_simulation()
    catalog.reset()
    allocate._data.cache_clear()
    keys._cache.clear()


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
):
    by_id = {p.product_id: p for p in products}
    rel_map = rel or {}
    co2_map = co2 or {}

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        c = constraints or Constraint()
        tokens = [
            tok
            for tok in q.lower().replace("-", " ").split()
            if tok.isalpha() and len(tok) >= 3 and tok not in {"free", "and", "the"}
        ]
        out: list[Product] = []
        for p in products:
            pname = p.name.lower()
            if tokens and not any(tok in pname for tok in tokens):
                continue
            if (c.exclude_allergens or c.lactose_free or c.gluten_free) and not substitute.allergen_safe(
                p, c, c.exclude_allergens
            ):
                continue
            if c.brands and not any(b.lower() in (p.brand or "").lower() for b in c.brands):
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return out

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: rel_map.get(str(pid)))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: co2_map.get(str(pid)))
    monkeypatch.setattr(valio, "price", lambda cust, day, ids: {i: prices[i] for i in ids if i in prices})
    monkeypatch.setattr(valio, "availability", lambda cust, day, ids: {i: avail[i] for i in ids if i in avail})


def _quote_and_place(
    customer: str,
    agent_id: str,
    day: date,
    lines: list[QuoteRequestLine],
    ask_human: bool = False,
    note: str | None = None,
) -> Order:
    q = orders.create_quote(customer, agent_id, QuoteRequest(offers=lines, delivery_date=day))
    return orders.place(customer, agent_id, PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws, ask_human=ask_human, note=note))


def _insert_order_row(
    order_id: str,
    customer: str,
    status: str,
    total_net: float,
    created_at: datetime,
    valid_until: datetime,
    delivery_date: date,
    product_id: str = "p_milk_1",
    agent_id: str = "buyer-aurora",
) -> Order:
    line = QuoteLine(
        product_id=product_id,
        name="Lactose free milk 1L",
        quantity=10.0,
        sales_unit="ST",
        unit_price_net=round(total_net / 10.0, 4),
        line_total_net=total_net,
    )
    o = Order(
        order_id=order_id,
        quote_id=f"q_{order_id}",
        customer=customer,
        agent_id=agent_id,
        lines=[line],
        total_net=total_net,
        status=status,
        decision=Decision(outcome="auto", reasons=[Reason(code="WITHIN_MANDATE", text="ok")]),
        delivery_date=delivery_date,
        valid_until=valid_until,
        created_at=created_at,
        updated_at=created_at,
    )
    con = orders._con()
    try:
        con.execute(
            "insert into orders values (?,?,?,?,?,?,?,?,NULL)",
            (
                o.order_id,
                o.quote_id,
                o.customer,
                o.status,
                created_at.isoformat(),
                valid_until.isoformat(),
                delivery_date.isoformat(),
                o.model_dump_json(),
            ),
        )
        con.commit()
    finally:
        con.close()
    return o


def test_full_stockout_refill_via_offers_simulate_from_confirmed(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    p2 = _product("p_milk_2", "Lactose free milk 1L backup")
    _wire(
        monkeypatch,
        [p1, p2],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.20),
            "p_milk_2": _price_entry("p_milk_2", 1.45),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 200.0),
        },
    )

    placed = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=50)],
    )
    assert placed.status == "confirmed"
    assert placed.total_net == 60.0

    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=0.0))

    reopened = orders.get(placed.order_id)
    assert reopened is not None
    assert reopened.status == "pending_approval"
    assert reopened.decision.outcome == "needs_approval"
    assert [r.code for r in reopened.decision.reasons] == ["STOCKOUT"]
    assert "Stock dropped after the order:" in reopened.decision.reasons[0].text
    assert [(l.product_id, l.quantity, l.unit_price_net, l.line_total_net) for l in reopened.lines] == [
        ("p_milk_2", 50.0, 1.45, 72.5)
    ]
    assert reopened.total_net == 72.5
    assert reopened.substitutions == [
        Substitution(
            original_id="p_milk_1",
            substitute_id="p_milk_2",
            reason="Stock dropped, refilled from the next offer.",
        )
    ]

    evs = audit.events(order_id=placed.order_id)
    stockout_evs = [e for e in evs if e.action == "order.stockout"]
    assert len(stockout_evs) == 1
    assert stockout_evs[0].actor == "system"
    assert stockout_evs[0].detail["product_id"] == "p_milk_1"
    assert stockout_evs[0].detail["total_net"] == 72.5
    assert stockout_evs[0].detail["reasons"][0]["code"] == "STOCKOUT"
    assert audit.verify() == (True, None)


def test_full_stockout_refill_via_admin_simulate_endpoint_and_reapproval(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    p2 = _product("p_milk_2", "Lactose free milk 1L reserve")
    _wire(
        monkeypatch,
        [p1, p2],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.10),
            "p_milk_2": _price_entry("p_milk_2", 1.35),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 200.0),
        },
    )

    agent_headers = {"X-Agent-Key": "demo-hotel-aurora"}
    admin_headers = {"X-Admin-Key": "demo-admin"}
    with TestClient(app) as client:
        q_resp = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "p_milk_1", "quantity": 40}], "delivery_date": future_day.isoformat()},
            headers=agent_headers,
        )
        assert q_resp.status_code == 200
        quote_data = q_resp.json()

        o_resp = client.post(
            "/v1/orders",
            json={"quote_id": quote_data["quote_id"], "jws": quote_data["jws"]},
            headers=agent_headers,
        )
        assert o_resp.status_code == 200
        order_data = o_resp.json()
        oid = order_data["order_id"]
        assert order_data["status"] == "confirmed"
        assert order_data["total_net"] == 44.0

        sim_resp = client.post(
            "/v1/admin/simulate",
            json={"product_id": "p_milk_1", "available_amount": 0},
            headers=admin_headers,
        )
        assert sim_resp.status_code == 200
        assert sim_resp.json() == {"ok": True}

        got = client.get(f"/v1/orders/{oid}", headers=agent_headers).json()
        assert got["status"] == "pending_approval"
        assert got["decision"]["outcome"] == "needs_approval"
        assert [r["code"] for r in got["decision"]["reasons"]] == ["STOCKOUT"]
        assert [(l["product_id"], l["quantity"], l["line_total_net"]) for l in got["lines"]] == [
            ("p_milk_2", 40.0, 54.0)
        ]
        assert got["total_net"] == 54.0
        assert got["substitutions"] == [
            {
                "original_id": "p_milk_1",
                "substitute_id": "p_milk_2",
                "reason": "Stock dropped, refilled from the next offer.",
            }
        ]

        pending_ids = [x["order_id"] for x in client.get("/v1/approvals", headers=admin_headers).json()]
        assert oid in pending_ids

        trail = client.get(f"/v1/audit?order_id={oid}", headers=agent_headers).json()
        assert trail["verified"] == {"ok": True, "bad_seq": None}
        assert any(e["action"] == "order.stockout" for e in trail["events"])

        dec_resp = client.post(
            f"/v1/approvals/{oid}",
            json={"approve": True, "by": "Laura Virtanen", "note": "refill approved"},
            headers=admin_headers,
        )
        assert dec_resp.status_code == 200
        assert dec_resp.json()["status"] == "confirmed"

        clr_resp = client.delete("/v1/admin/simulate", headers=admin_headers)
        assert clr_resp.status_code == 200
        assert offers._overrides == {}


def test_stockout_transitions_placed_and_approved_orders(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    p2 = _product("p_milk_2", "Lactose free milk 1L second")
    _wire(
        monkeypatch,
        [p1, p2],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.00),
            "p_milk_2": _price_entry("p_milk_2", 1.25),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 200.0),
        },
    )

    approved_ord = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=20)],
        ask_human=True,
        note="hold first",
    )
    assert approved_ord.status == "pending_approval"
    approved_ord = orders.set_status(approved_ord.order_id, "approved", "human:Laura Virtanen", "approved")
    assert approved_ord.status == "approved"

    now = datetime.now(timezone.utc)
    placed_ord = _insert_order_row(
        order_id="ord_placed_state",
        customer="hotel-aurora",
        status="placed",
        total_net=30.0,
        created_at=now,
        valid_until=now + timedelta(days=2),
        delivery_date=future_day,
        product_id="p_milk_1",
    )
    assert orders.get(placed_ord.order_id).status == "placed"

    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=0.0))

    for oid, expected_qty, expected_net in (
        (approved_ord.order_id, 20.0, 25.0),
        (placed_ord.order_id, 10.0, 12.5),
    ):
        after = orders.get(oid)
        assert after.status == "pending_approval"
        assert after.decision.outcome == "needs_approval"
        assert [r.code for r in after.decision.reasons] == ["STOCKOUT"]
        assert [(l.product_id, l.quantity) for l in after.lines] == [("p_milk_2", expected_qty)]
        assert after.total_net == expected_net
        assert [(s.original_id, s.substitute_id) for s in after.substitutions] == [("p_milk_1", "p_milk_2")]
        assert any(e.action == "order.stockout" for e in audit.events(order_id=oid))


def test_partial_stock_drop_split_and_shortfall_narration(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    p2 = _product("p_milk_2", "Lactose free milk 1L alt")
    _wire(
        monkeypatch,
        [p1, p2],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.20),
            "p_milk_2": _price_entry("p_milk_2", 1.50),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 30.0),
        },
    )

    placed = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=100)],
    )
    assert placed.status == "confirmed"
    assert placed.total_net == 120.0

    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=40.0, available_date=future_day))

    updated = orders.get(placed.order_id)
    assert updated.status == "pending_approval"
    assert updated.decision.outcome == "needs_approval"
    assert [r.code for r in updated.decision.reasons] == ["STOCKOUT"]
    assert [(l.product_id, l.quantity, l.line_total_net) for l in updated.lines] == [
        ("p_milk_1", 40.0, 48.0),
        ("p_milk_2", 30.0, 45.0),
    ]
    assert updated.total_net == 93.0
    assert [(s.original_id, s.substitute_id) for s in updated.substitutions] == [("p_milk_1", "p_milk_2")]
    assert "Shortfall 30" in updated.decision.reasons[0].text

    stockout_evs = [e for e in audit.events(order_id=placed.order_id) if e.action == "order.stockout"]
    assert len(stockout_evs) == 1
    assert "Shortfall 30" in stockout_evs[0].why
    assert stockout_evs[0].detail["total_net"] == 93.0


def test_allergen_safe_substitutes_and_mandate_lifecycle(monkeypatch, future_day):
    p_primary = _product("p_oat_1", "Oat drink 1L", brand="Valio", allergens=["oats"], labels=["vegan"])
    p_unsafe = _product("p_oat_nut", "Oat drink 1L nutty", brand="Valio", allergens=["oats", "peanuts"], labels=["vegan"])
    p_safe_sub = _product("p_oat_2", "Oat drink 1L organic extra fresh", brand="Valio", allergens=["oats"], labels=["vegan"])
    _wire(
        monkeypatch,
        [p_primary, p_unsafe, p_safe_sub],
        {
            "p_oat_1": _price_entry("p_oat_1", 1.30),
            "p_oat_nut": _price_entry("p_oat_nut", 1.00),
            "p_oat_2": _price_entry("p_oat_2", 1.50),
        },
        {
            "p_oat_1": _avail_entry("p_oat_1", future_day, 20.0),
            "p_oat_nut": _avail_entry("p_oat_nut", future_day, 200.0),
            "p_oat_2": _avail_entry("p_oat_2", future_day, 200.0),
        },
    )

    now = datetime.combine(future_day - timedelta(days=3), time(9, 0), tzinfo=offers.HEL)
    school_mandate = mandates.get("school-hervanta", "buyer-hervanta")
    need = Need(
        customer="school-hervanta",
        deliver_by=future_day,
        items=[NeedItem(query="oat drink", quantity=50, unit="L", constraints=Constraint(brands=["Valio"]))],
    )
    group = offers.get_offers(need, mandate=school_mandate, now=now).items[0]
    assert [(o.product_id, o.units_needed, o.substitute_for) for o in group.offers] == [
        ("p_oat_1", 20.0, None),
        ("p_oat_2", 30.0, "p_oat_1"),
    ]
    assert all("peanuts" not in o.product.allergens for o in group.offers)

    req_lines = [
        QuoteRequestLine(product_id=o.product_id, quantity=o.units_needed, substitute_for=o.substitute_for)
        for o in group.offers
    ]
    school_order = _quote_and_place("school-hervanta", "buyer-hervanta", future_day, req_lines)
    assert school_order.status == "confirmed"
    assert [(s.original_id, s.substitute_id) for s in school_order.substitutions] == [("p_oat_1", "p_oat_2")]

    tays_order = _quote_and_place("hospital-tays", "buyer-tays", future_day, req_lines)
    assert tays_order.status == "pending_approval"
    assert any(r.code == "SUBSTITUTION_NEEDS_APPROVAL" for r in tays_order.decision.reasons)
    decided = approvals.decide(tays_order.order_id, True, "Helena Korhonen", "safe oat swap")
    assert decided.status == "confirmed"

    blocked_order = _quote_and_place(
        "hospital-tays",
        "buyer-tays",
        future_day,
        [QuoteRequestLine(product_id="p_oat_nut", quantity=10)],
    )
    assert blocked_order.status == "blocked"
    assert blocked_order.decision.outcome == "blocked"
    assert any(r.code == "ALLERGEN_CONFLICT" for r in blocked_order.decision.reasons)


def test_unaffected_orders_not_reopened_on_stockout(monkeypatch, future_day):
    p_milk_1 = _product("p_milk_1", "Lactose free milk 1L")
    p_milk_2 = _product("p_milk_2", "Lactose free milk 1L backup")
    p_butter = _product("p_butter", "Unsalted butter 500g", size_l=0.5, category="10500")
    p_unsafe = _product("p_milk_egg", "Milk custard 1L", allergens=["milk", "egg"], labels=[])
    _wire(
        monkeypatch,
        [p_milk_1, p_milk_2, p_butter, p_unsafe],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.10),
            "p_milk_2": _price_entry("p_milk_2", 1.40),
            "p_butter": _price_entry("p_butter", 3.00),
            "p_milk_egg": _price_entry("p_milk_egg", 2.00),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 200.0),
            "p_butter": _avail_entry("p_butter", future_day, 200.0),
            "p_milk_egg": _avail_entry("p_milk_egg", future_day, 200.0),
        },
    )

    other_prod_order = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_butter", quantity=15)],
    )
    assert other_prod_order.status == "confirmed"

    to_reject = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=50)],
        ask_human=True,
    )
    rejected_order = approvals.decide(to_reject.order_id, False, "Laura Virtanen", "not needed")
    assert rejected_order.status == "rejected"

    blocked_order = _quote_and_place(
        "hospital-tays",
        "buyer-tays",
        future_day,
        [QuoteRequestLine(product_id="p_milk_egg", quantity=10), QuoteRequestLine(product_id="p_milk_1", quantity=50)],
    )
    assert blocked_order.status == "blocked"

    now = datetime.now(timezone.utc)
    expired_order = _insert_order_row(
        order_id="ord_already_expired",
        customer="hotel-aurora",
        status="expired",
        total_net=55.0,
        created_at=now - timedelta(hours=3),
        valid_until=now - timedelta(hours=1),
        delivery_date=future_day,
        product_id="p_milk_1",
    )

    stale_pending_order = _insert_order_row(
        order_id="ord_stale_pending",
        customer="hotel-aurora",
        status="pending_approval",
        total_net=55.0,
        created_at=now - timedelta(hours=2),
        valid_until=now - timedelta(minutes=5),
        delivery_date=future_day,
        product_id="p_milk_1",
    )

    covered_order = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=25)],
    )
    assert covered_order.status == "confirmed"

    uncovered_order = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=60)],
    )
    assert uncovered_order.status == "confirmed"

    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=40.0, available_date=future_day))

    expected_unaffected = [
        (other_prod_order.order_id, "confirmed"),
        (rejected_order.order_id, "rejected"),
        (blocked_order.order_id, "blocked"),
        (expired_order.order_id, "expired"),
        (stale_pending_order.order_id, "expired"),
        (covered_order.order_id, "confirmed"),
    ]
    for oid, expected_status in expected_unaffected:
        got = orders.get(oid)
        assert got.status == expected_status
        assert all(r.code != "STOCKOUT" for r in got.decision.reasons)
        assert all(e.action != "order.stockout" for e in audit.events(order_id=oid))

    assert any(e.action == "order.expired" for e in audit.events(order_id=stale_pending_order.order_id))

    reopened = orders.get(uncovered_order.order_id)
    assert reopened.status == "pending_approval"
    assert [r.code for r in reopened.decision.reasons] == ["STOCKOUT"]
    assert [(l.product_id, l.quantity) for l in reopened.lines] == [("p_milk_1", 40.0), ("p_milk_2", 20.0)]


def test_repeat_simulate_does_not_duplicate_substitutions(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    p2 = _product("p_milk_2", "Lactose free milk 1L backup")
    prev_day = future_day - timedelta(days=1)
    _wire(
        monkeypatch,
        [p1, p2],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.10),
            "p_milk_2": _price_entry("p_milk_2", 1.30),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry(
                "p_milk_2",
                prev_day,
                30.0,
                extra_lines=[
                    {
                        "salesBlock": False,
                        "availableAmount": 100.0,
                        "availableDate": future_day.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            ),
        },
    )

    ord_full = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=60)],
    )
    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=0.0, available_date=future_day))
    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=0.0, available_date=future_day))

    after_full = orders.get(ord_full.order_id)
    assert [(l.product_id, l.quantity) for l in after_full.lines] == [("p_milk_2", 30.0), ("p_milk_2", 30.0)]
    pairs_full = [(s.original_id, s.substitute_id) for s in after_full.substitutions]
    assert pairs_full == [("p_milk_1", "p_milk_2")]
    assert len([e for e in audit.events(order_id=ord_full.order_id) if e.action == "order.stockout"]) == 1

    offers.clear_simulation()
    ord_partial = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=80)],
    )
    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=50.0, available_date=future_day))
    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=50.0, available_date=future_day))
    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=20.0, available_date=future_day))

    after_partial = orders.get(ord_partial.order_id)
    pairs_partial = [(s.original_id, s.substitute_id) for s in after_partial.substitutions]
    assert pairs_partial == [("p_milk_1", "p_milk_2")]


def test_spent_this_week_window_statuses_and_customers(future_day):
    now = datetime.now(timezone.utc)
    valid = now + timedelta(days=2)

    _insert_order_row("ord_w_confirmed", "hotel-aurora", "confirmed", 100.00, now - timedelta(days=1), valid, future_day)
    _insert_order_row("ord_w_approved", "hotel-aurora", "approved", 200.50, now - timedelta(days=3), valid, future_day)
    _insert_order_row("ord_w_placed", "hotel-aurora", "placed", 50.25, now - timedelta(days=6), valid, future_day)

    _insert_order_row("ord_w_draft", "hotel-aurora", "draft", 999.00, now - timedelta(days=1), valid, future_day)
    _insert_order_row("ord_w_pending", "hotel-aurora", "pending_approval", 400.00, now - timedelta(days=1), valid, future_day)
    _insert_order_row("ord_w_rejected", "hotel-aurora", "rejected", 500.00, now - timedelta(days=2), valid, future_day)
    _insert_order_row("ord_w_blocked", "hotel-aurora", "blocked", 600.00, now - timedelta(days=2), valid, future_day)
    _insert_order_row("ord_w_expired", "hotel-aurora", "expired", 700.00, now - timedelta(days=2), valid, future_day)

    _insert_order_row("ord_other_cust", "hospital-tays", "confirmed", 800.00, now - timedelta(days=1), valid, future_day, agent_id="buyer-tays")

    _insert_order_row("ord_old_conf", "hotel-aurora", "confirmed", 1000.00, now - timedelta(days=8), valid, future_day)
    _insert_order_row("ord_old_appr", "hotel-aurora", "approved", 1100.00, now - timedelta(days=9), valid, future_day)
    _insert_order_row("ord_old_plac", "hotel-aurora", "placed", 1200.00, now - timedelta(days=14), valid, future_day)

    assert orders.spent_this_week("hotel-aurora") == pytest.approx(350.75)
    assert orders.spent_this_week("hospital-tays") == pytest.approx(800.00)
    assert orders.spent_this_week("school-hervanta") == 0.0


def test_auto_placed_confirmation_and_order_lifecycle(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    _wire(
        monkeypatch,
        [p1],
        {"p_milk_1": _price_entry("p_milk_1", 1.50)},
        {"p_milk_1": _avail_entry("p_milk_1", future_day, 500.0)},
    )

    auto_order = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=20)],
    )
    assert auto_order.status == "confirmed"
    assert auto_order.total_net == 30.0
    assert orders.spent_this_week("hotel-aurora") == pytest.approx(30.0)

    conf = orders.confirmation(auto_order.order_id)
    assert conf is not None
    assert conf["confirmation_id"].startswith("MOCK-")
    assert len(conf["confirmation_id"]) == 13
    assert conf["mock"] is True
    assert conf["delivery_date"] == future_day.isoformat()

    held_order = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=10)],
        ask_human=True,
    )
    assert held_order.status == "pending_approval"
    assert orders.confirmation(held_order.order_id) is None
    assert orders.confirmation("ord_nonexistent") is None
    assert orders.spent_this_week("hotel-aurora") == pytest.approx(30.0)

    evs = audit.events(order_id=auto_order.order_id)
    actions = [e.action for e in evs]
    assert "quote.issued" in actions
    assert "quote.redeemed" in actions
    assert "order.approved" in actions
    assert "order.confirmed" in actions

    conf_ev = [e for e in evs if e.action == "order.confirmed"][0]
    assert conf_ev.detail["confirmation_id"] == conf["confirmation_id"]
    assert conf_ev.detail["mock"] is True
    assert conf_ev.detail["delivery_date"] == future_day.isoformat()

    with pytest.raises(ValueError):
        orders.set_status(auto_order.order_id, "rejected", "system")
    with pytest.raises(KeyError):
        orders.set_status("ord_missing", "approved", "system")


def test_multi_line_order_stockout_preserves_other_lines(monkeypatch, future_day):
    p_milk_1 = _product("p_milk_1", "Lactose free milk 1L")
    p_milk_2 = _product("p_milk_2", "Lactose free milk 1L backup")
    p_butter = _product("p_butter", "Unsalted butter 500g", size_l=0.5, category="10500")
    _wire(
        monkeypatch,
        [p_milk_1, p_milk_2, p_butter],
        {
            "p_milk_1": _price_entry("p_milk_1", 1.20),
            "p_milk_2": _price_entry("p_milk_2", 1.50),
            "p_butter": _price_entry("p_butter", 3.00),
        },
        {
            "p_milk_1": _avail_entry("p_milk_1", future_day, 200.0),
            "p_milk_2": _avail_entry("p_milk_2", future_day, 200.0),
            "p_butter": _avail_entry("p_butter", future_day, 200.0),
        },
    )

    placed = _quote_and_place(
        "hotel-aurora",
        "buyer-aurora",
        future_day,
        [
            QuoteRequestLine(product_id="p_milk_1", quantity=50),
            QuoteRequestLine(product_id="p_butter", quantity=10),
        ],
    )
    assert placed.status == "confirmed"
    assert placed.total_net == 90.0

    offers.simulate(SimulateStock(product_id="p_milk_1", available_amount=0.0, available_date=future_day))

    after = orders.get(placed.order_id)
    assert after.status == "pending_approval"
    assert [(l.product_id, l.quantity, l.line_total_net) for l in after.lines] == [
        ("p_milk_2", 50.0, 75.0),
        ("p_butter", 10.0, 30.0),
    ]
    assert after.total_net == 105.0
    assert [(s.original_id, s.substitute_id) for s in after.substitutions] == [("p_milk_1", "p_milk_2")]


def test_list_auto_expires_stale_and_weekly_spend_triggers_approval(monkeypatch, future_day):
    p1 = _product("p_milk_1", "Lactose free milk 1L")
    _wire(
        monkeypatch,
        [p1],
        {"p_milk_1": _price_entry("p_milk_1", 10.00)},
        {"p_milk_1": _avail_entry("p_milk_1", future_day, 5000.0)},
    )

    now = datetime.now(timezone.utc)
    _insert_order_row(
        "ord_stale_list",
        "school-hervanta",
        "pending_approval",
        100.0,
        now - timedelta(hours=2),
        now - timedelta(minutes=1),
        future_day,
        agent_id="buyer-hervanta",
    )
    assert orders.list(status="pending_approval", customer="school-hervanta") == []
    assert [o.order_id for o in orders.list(status="expired", customer="school-hervanta")] == ["ord_stale_list"]

    _insert_order_row(
        "ord_prior_spend",
        "school-hervanta",
        "confirmed",
        2700.0,
        now - timedelta(days=2),
        now + timedelta(days=2),
        future_day,
        agent_id="buyer-hervanta",
    )
    assert orders.spent_this_week("school-hervanta") == pytest.approx(2700.0)

    over_week = _quote_and_place(
        "school-hervanta",
        "buyer-hervanta",
        future_day,
        [QuoteRequestLine(product_id="p_milk_1", quantity=40)],
    )
    assert over_week.status == "pending_approval"
    assert over_week.decision.outcome == "needs_approval"
    assert any(r.code == "OVER_WEEK_LIMIT" for r in over_week.decision.reasons)

