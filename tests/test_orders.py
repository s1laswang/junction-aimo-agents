import sys
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest

try:
    import aimo.audit
except ModuleNotFoundError:
    sys.modules["aimo.audit"] = types.ModuleType("aimo.audit")
    import aimo
    aimo.audit = sys.modules["aimo.audit"]

from aimo import orders
from aimo.models import Decision, PlaceOrderRequest, Quote, QuoteLine, Reason


class QuoteError(Exception):
    pass


def stub(monkeypatch, name, **attrs):
    m = types.ModuleType("aimo." + name)
    for k, v in attrs.items():
        setattr(m, k, v)
    monkeypatch.setitem(sys.modules, "aimo." + name, m)
    import aimo
    monkeypatch.setattr(aimo, name, m, raising=False)
    return m


def quote(qid, total=2.0, valid=timedelta(hours=1)):
    now = datetime.now(timezone.utc)
    return Quote(quote_id=qid, customer="hotel-aurora", agent_id="a1", delivery_date=date(2026, 11, 16),
                 lines=[QuoteLine(product_id="p", quantity=1, unit_price_net=total, line_total_net=total)],
                 total_net=total, total_gross=total * 1.1, issued_at=now, valid_until=now + valid)


@pytest.fixture
def env(monkeypatch):
    used = set()
    store = {}

    def redeem(qid, jws, customer, agent_id):
        if jws != "good" or qid in used:
            raise QuoteError(qid)
        used.add(qid)
        return store[qid]

    def decide(q, mandate, products, spent=0.0, substitutions=()):
        if q.total_net > 100:
            return Decision(outcome="needs_approval", reasons=[Reason(code="OVER_ORDER_LIMIT", text="over limit")])
        return Decision(outcome="auto", reasons=[Reason(code="WITHIN_MANDATE", text="within mandate")])

    stub(monkeypatch, "quotes", redeem=redeem)
    stub(monkeypatch, "policy", decide=decide)
    stub(monkeypatch, "catalog", get=lambda i: None)
    stub(monkeypatch, "mandates", get=lambda c, a=None: None)
    monkeypatch.setattr(orders.audit, "record", lambda *a, **k: None, raising=False)
    return store


def req(q):
    return PlaceOrderRequest(quote_id=q.quote_id, jws="good")


def test_under_limit_confirms(env):
    q = env["q1"] = quote("q1")
    o = orders.place("hotel-aurora", "a1", req(q))
    assert o.status == "confirmed"
    assert orders.confirmation(o.order_id)["delivery_date"] == "2026-11-16"
    assert orders.spent_this_week("hotel-aurora") >= 2.0


def test_over_limit_pending_then_approve(env):
    q = env["q2"] = quote("q2", 500)
    o = orders.place("hotel-aurora", "a1", req(q))
    assert o.status == "pending_approval"
    assert orders.list(status="pending_approval")[-1].order_id == o.order_id
    orders.set_status(o.order_id, "approved", "human:x", "ok")
    assert orders.set_status(o.order_id, "placed", "human:x", "ok").status == "confirmed"
    with pytest.raises(ValueError):
        orders.set_status(o.order_id, "rejected", "x")


def test_bad_or_used_jws_raises(env):
    q = env["q3"] = quote("q3")
    with pytest.raises(QuoteError):
        orders.place("hotel-aurora", "a1", PlaceOrderRequest(quote_id="q3", jws="bad"))
    orders.place("hotel-aurora", "a1", req(q))
    with pytest.raises(QuoteError):
        orders.place("hotel-aurora", "a1", req(q))


def test_expiry(env):
    q = env["q4"] = quote("q4", 500, timedelta(seconds=1))
    o = orders.place("hotel-aurora", "a1", req(q))
    time.sleep(1.2)
    assert orders.get(o.order_id).status == "expired"
