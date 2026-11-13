import sys
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest

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

    stub(monkeypatch, "quotes", redeem=redeem, QuoteError=QuoteError)
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
    assert o.delivery_date == date(2026, 11, 16) and o.valid_until
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


def test_ask_human_and_substitutions(env):
    q = env["q5"] = quote("q5")
    q.lines[0].substitute_for = "orig"
    o = orders.place("hotel-aurora", "a1", PlaceOrderRequest(quote_id="q5", jws="good", ask_human=True, note="check"))
    assert o.status == "pending_approval"
    assert o.decision.reasons[0].code == "AGENT_REQUESTED"
    assert o.substitutions[0].original_id == "orig"


def test_audit_reasons(env, monkeypatch):
    ev = []
    monkeypatch.setattr(orders.audit, "record", lambda *a, **k: ev.append((a, k)), raising=False)
    q = env["q6"] = quote("q6", 500)
    orders.place("hotel-aurora", "a1", req(q))
    a, k = [e for e in ev if e[0][2] == "order.pending_approval"][0]
    assert a[4] == "over limit"
    assert (k.get("detail") or a[5])["reasons"][0]["code"] == "OVER_ORDER_LIMIT"
