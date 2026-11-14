import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aimo import approvals, audit, catalog, offers, orders, quotes as quotes_mod
from aimo.app import app
from aimo.models import PlaceOrderRequest, QuoteLine

A = {"X-Agent-Key": "demo-hotel-aurora"}
B = {"X-Agent-Key": "demo-hospital-tays"}
ADMIN = {"X-Admin-Key": "demo-admin"}
DAY = (date.today() + timedelta(days=3)).isoformat()


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    catalog.reset()
    box = {"ttl": timedelta(hours=2)}

    def price_lines(customer, delivery_date, lines):
        if delivery_date < date.today():
            from aimo.quotes import QuoteError
            raise QuoteError("INVALID_QUOTE", "Delivery date is in the past.")
        ql = [QuoteLine(product_id=l.product_id, quantity=l.quantity, unit_price_net=1.0, line_total_net=l.quantity) for l in lines]
        return ql, round(sum(l.quantity for l in lines) * 1.14, 2), datetime.now(timezone.utc) + box["ttl"]

    monkeypatch.setattr(offers, "price_lines", price_lines, raising=False)
    cl = TestClient(app)
    cl.box = box
    yield cl
    catalog.reset()


def quote(c, qty, headers=A, day=DAY):
    r = c.post("/v1/quotes", json={"offers": [{"product_id": "p1", "quantity": qty}], "delivery_date": day}, headers=headers)
    return r


def place(c, qty, headers=A, **kw):
    q = quote(c, qty, headers).json()
    r = c.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"], **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return q, r.json()


def decide(c, oid, approve, by="Laura Virtanen", headers=ADMIN):
    return c.post(f"/v1/approvals/{oid}", json={"approve": approve, "by": by, "note": "t"}, headers=headers)


def test_approve_twice(c):
    _, o = place(c, 3000)
    assert o["status"] == "pending_approval"
    assert decide(c, o["order_id"], True).status_code == 200
    r = decide(c, o["order_id"], True)
    assert r.status_code == 409 and r.json()["code"] == "NOT_PENDING"
    assert c.get(f"/v1/orders/{o['order_id']}", headers=A).json()["status"] == "confirmed"


def test_approve_after_expiry(c):
    c.box["ttl"] = timedelta(seconds=1)
    _, o = place(c, 3000)
    time.sleep(1.2)
    approvals.expire_stale()
    r = decide(c, o["order_id"], True)
    assert r.status_code == 409
    assert c.get(f"/v1/orders/{o['order_id']}", headers=A).json()["status"] == "expired"


def test_reject_then_replace_same_quote(c):
    q, o = place(c, 3000)
    assert decide(c, o["order_id"], False).json()["status"] == "rejected"
    r = c.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"]}, headers=A)
    assert r.status_code == 409 and r.json()["code"] == "QUOTE_USED"
    assert len(orders.list(customer="hotel-aurora")) == 1


def test_cross_customer(c):
    _, o = place(c, 3000)
    assert c.get(f"/v1/orders/{o['order_id']}", headers=B).status_code == 403
    assert c.get("/v1/orders?customer=hotel-aurora", headers=B).status_code == 403
    assert decide(c, o["order_id"], True, headers=B).status_code == 403
    assert decide(c, o["order_id"], True, headers=A).status_code == 403
    assert decide(c, o["order_id"], True, by="Helena Korhonen").status_code == 403
    assert c.get(f"/v1/orders/{o['order_id']}", headers=A).json()["status"] == "pending_approval"


def test_ask_human_tiny(c):
    _, o = place(c, 1, ask_human=True, note="check me")
    assert o["status"] == "pending_approval"
    assert [r["code"] for r in o["decision"]["reasons"]][0] == "AGENT_REQUESTED"
    assert "check me" in o["decision"]["reasons"][0]["text"]


def test_stockout_on_pending(c, monkeypatch):
    from aimo import allocate
    _, o = place(c, 3000)
    new = QuoteLine(product_id="alt", name="Alt", quantity=3000, unit_price_net=1.0, line_total_net=3000)
    monkeypatch.setattr(offers, "fresh_offers", lambda cu, d, ids: [], raising=False)
    monkeypatch.setattr(allocate, "stockout_plan", lambda lines, fresh, day: {"changed": True, "lines": [new], "changes": ["p1 replaced by alt"], "shortfall": 0})
    orders.react_stockout("p1")
    g = c.get(f"/v1/orders/{o['order_id']}", headers=A).json()
    assert g["status"] == "pending_approval"
    assert g["decision"]["reasons"][0]["code"] == "STOCKOUT"
    assert g["substitutions"][0]["substitute_id"] == "alt"
    assert decide(c, o["order_id"], True).json()["status"] == "confirmed"


def test_weekly_limit_crossing(c):
    outs = [place(c, 2400)[1] for _ in range(7)]
    assert [o["status"] for o in outs[:6]] == ["confirmed"] * 6
    assert outs[6]["status"] == "pending_approval"
    assert "OVER_WEEK_LIMIT" in [r["code"] for r in outs[6]["decision"]["reasons"]]


def test_quantity_zero(c):
    assert quote(c, 0).status_code == 422
    assert quote(c, -5).status_code == 422


def test_past_delivery(c):
    r = quote(c, 10, day=(date.today() - timedelta(days=1)).isoformat())
    assert r.status_code == 409 and r.json()["code"] == "INVALID_QUOTE"


def test_stockout_reopens_confirmed_clears_and_regenerates_confirmation(c, monkeypatch):
    from aimo import allocate
    _, o1 = place(c, 10)
    _, o2 = place(c, 12)
    assert o1["status"] == "confirmed" and o2["status"] == "confirmed"
    c1 = orders.confirmation(o1["order_id"])
    c2 = orders.confirmation(o2["order_id"])
    assert c1 is not None and c2 is not None
    alt = QuoteLine(product_id="alt", name="Alt", quantity=10, unit_price_net=1.0, line_total_net=10.0)
    monkeypatch.setattr(offers, "fresh_offers", lambda cu, d, ids: [], raising=False)
    monkeypatch.setattr(allocate, "stockout_plan", lambda lines, fresh, day: {"changed": True, "lines": [alt], "changes": ["p1 replaced by alt"], "shortfall": 0})
    reopened = orders.react_stockout("p1")
    assert {x.order_id for x in reopened} == {o1["order_id"], o2["order_id"]}
    assert orders.confirmation(o1["order_id"]) is None
    assert orders.confirmation(o2["order_id"]) is None
    r_ok = decide(c, o1["order_id"], True)
    assert r_ok.status_code == 200 and r_ok.json()["status"] == "confirmed"
    c1_new = orders.confirmation(o1["order_id"])
    assert c1_new is not None and c1_new["confirmation_id"] != c1["confirmation_id"]
    r_no = decide(c, o2["order_id"], False)
    assert r_no.status_code == 200 and r_no.json()["status"] == "rejected"
    assert orders.confirmation(o2["order_id"]) is None


def test_stockout_skips_expired_and_past_order(c, monkeypatch):
    from zoneinfo import ZoneInfo
    from aimo import allocate
    c.box["ttl"] = timedelta(milliseconds=60)
    _, o_exp = place(c, 3000)
    c.box["ttl"] = timedelta(hours=2)
    _, o_ok = place(c, 10)
    _, o_past = place(c, 10)
    past_day = (datetime.now(ZoneInfo("Europe/Helsinki")).date() - timedelta(days=1)).isoformat()
    con = sqlite3.connect(os.environ["AIMO_DB"])
    try:
        con.execute("update orders set delivery_date=? where order_id=?", (past_day, o_past["order_id"]))
        con.commit()
    finally:
        con.close()
    time.sleep(0.1)
    alt = QuoteLine(product_id="alt", name="Alt", quantity=10, unit_price_net=1.0, line_total_net=10.0)
    monkeypatch.setattr(offers, "fresh_offers", lambda cu, d, ids: [], raising=False)
    monkeypatch.setattr(allocate, "stockout_plan", lambda lines, fresh, day: {"changed": True, "lines": [alt], "changes": ["p1 replaced by alt"], "shortfall": 0})
    reopened = orders.react_stockout("p1")
    assert [x.order_id for x in reopened] == [o_ok["order_id"]]
    assert c.get(f"/v1/orders/{o_exp['order_id']}", headers=A).json()["status"] == "expired"
    assert orders.get(o_past["order_id"]).status == "confirmed"
    assert orders.confirmation(o_past["order_id"]) is not None
    assert c.get(f"/v1/orders/{o_ok['order_id']}", headers=A).json()["status"] == "pending_approval"


def test_mandate_fallback_for_custom_agent_id(c):
    ql = [QuoteLine(product_id="p1", quantity=50, unit_price_net=1.0, line_total_net=50.0)]
    q1 = quotes_mod.issue("hotel-aurora", "other-agent-id", ql, date.fromisoformat(DAY), datetime.now(timezone.utc) + timedelta(hours=2), 57.0)
    o1 = orders.place("hotel-aurora", "other-agent-id", PlaceOrderRequest(quote_id=q1.quote_id, jws=q1.jws))
    assert o1.status == "confirmed"
    ql_big = [QuoteLine(product_id="p1", quantity=3000, unit_price_net=1.0, line_total_net=3000.0)]
    q2 = quotes_mod.issue("hotel-aurora", "other-agent-id", ql_big, date.fromisoformat(DAY), datetime.now(timezone.utc) + timedelta(hours=2), 3420.0)
    o2 = orders.place("hotel-aurora", "other-agent-id", PlaceOrderRequest(quote_id=q2.quote_id, jws=q2.jws))
    assert o2.status == "pending_approval"
    assert "OVER_ORDER_LIMIT" in [r.code for r in o2.decision.reasons]


def test_repeated_order_ops_leak_zero_locks(c, monkeypatch):
    from aimo import allocate
    alt = QuoteLine(product_id="alt", name="Alt", quantity=5, unit_price_net=1.0, line_total_net=5.0)
    monkeypatch.setattr(offers, "fresh_offers", lambda cu, d, ids: [], raising=False)
    monkeypatch.setattr(allocate, "stockout_plan", lambda lines, fresh, day: {"changed": True, "lines": [alt], "changes": ["p1 replaced by alt"], "shortfall": 0})
    for _ in range(4):
        _, o_auto = place(c, 5)
        assert orders.confirmation(o_auto["order_id"]) is not None
        _, o_pend = place(c, 3000)
        assert c.get("/v1/orders", headers=A).status_code == 200
        assert decide(c, o_pend["order_id"], True).status_code == 200
    orders.react_stockout("p1")
    for o in orders.list(status="pending_approval", customer="hotel-aurora"):
        assert decide(c, o.order_id, True).status_code == 200
    probe = sqlite3.connect(os.environ["AIMO_DB"], timeout=0.05)
    try:
        probe.execute("begin immediate")
        probe.execute("commit")
    finally:
        probe.close()
    assert audit.verify() == (True, None)
