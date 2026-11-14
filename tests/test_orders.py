import sqlite3
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from aimo import audit as real_audit, orders
from aimo.models import Decision, Mandate, PlaceOrderRequest, Quote, QuoteLine, Reason


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
    assert "Mock" not in str(o)
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


def test_stockout_reopens_order(env, monkeypatch):
    future = datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=2)
    q = env["q7"] = quote("q7").model_copy(update={"delivery_date": future})
    o = orders.place("hotel-aurora", "a1", req(q))
    assert o.status == "confirmed"
    first_conf = orders.confirmation(o.order_id)
    assert first_conf is not None and first_conf["confirmation_id"].startswith("MOCK-")
    new = QuoteLine(product_id="alt", name="Alt", quantity=1, unit_price_net=3, line_total_net=3)
    stub(monkeypatch, "offers", fresh_offers=lambda c, d, ids: [])
    stub(monkeypatch, "allocate", stockout_plan=lambda lines, fresh, day: {"changed": True, "lines": [new], "changes": ["p replaced by alt"], "shortfall": 0})
    res = orders.react_stockout("p")
    assert o.order_id in [x.order_id for x in res]
    assert orders.confirmation(o.order_id) is None
    g = orders.get(o.order_id)
    assert g.status == "pending_approval"
    assert g.decision.reasons[0].code == "STOCKOUT"
    assert g.substitutions[0].substitute_id == "alt"
    assert g.total_net == 3
    orders.set_status(o.order_id, "approved", "human:x", "ok")
    reconfirmed = orders.set_status(o.order_id, "placed", "human:x", "ok")
    assert reconfirmed.status == "confirmed"
    second_conf = orders.confirmation(o.order_id)
    assert second_conf is not None
    assert second_conf["confirmation_id"] != first_conf["confirmation_id"]


def test_stockout_skips_expired_past_and_matches_str_product_id(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "exp.db"))
    now = datetime.now(timezone.utc)
    hel_today = datetime.now(ZoneInfo("Europe/Helsinki")).date()
    q_exp = env["q_exp"] = Quote(
        quote_id="q_exp", customer="hotel-aurora", agent_id="a1", delivery_date=hel_today + timedelta(days=2),
        lines=[QuoteLine(product_id="400123", quantity=1, unit_price_net=500.0, line_total_net=500.0)],
        total_net=500.0, total_gross=550.0, issued_at=now, valid_until=now + timedelta(milliseconds=60),
    )
    q_past = env["q_past"] = Quote(
        quote_id="q_past", customer="hotel-aurora", agent_id="a1", delivery_date=hel_today - timedelta(days=1),
        lines=[QuoteLine(product_id="400123", quantity=1, unit_price_net=10.0, line_total_net=10.0)],
        total_net=10.0, total_gross=11.0, issued_at=now, valid_until=now + timedelta(hours=1),
    )
    q_ok = env["q_ok"] = Quote(
        quote_id="q_ok", customer="hotel-aurora", agent_id="a1", delivery_date=hel_today + timedelta(days=2),
        lines=[QuoteLine(product_id="400123", quantity=1, unit_price_net=10.0, line_total_net=10.0)],
        total_net=10.0, total_gross=11.0, issued_at=now, valid_until=now + timedelta(hours=1),
    )
    o_exp = orders.place("hotel-aurora", "a1", req(q_exp))
    o_past = orders.place("hotel-aurora", "a1", req(q_past))
    o_ok = orders.place("hotel-aurora", "a1", req(q_ok))
    assert o_exp.status == "pending_approval"
    assert o_past.status == "confirmed"
    assert o_ok.status == "confirmed"
    time.sleep(0.1)
    alt = QuoteLine(product_id="400999", name="Alt", quantity=1, unit_price_net=12.0, line_total_net=12.0)
    stub(monkeypatch, "offers", fresh_offers=lambda c, d, ids: [])
    stub(monkeypatch, "allocate", stockout_plan=lambda lines, fresh, day: {"changed": True, "lines": [alt], "changes": ["400123 replaced by 400999"], "shortfall": 0})
    reopened = orders.react_stockout(400123)
    assert [x.order_id for x in reopened] == [o_ok.order_id]
    assert orders.get(o_exp.order_id).status == "expired"
    assert orders.get(o_past.order_id).status == "confirmed"
    assert orders.confirmation(o_past.order_id) is not None
    assert orders.get(o_ok.order_id).status == "pending_approval"
    assert orders.confirmation(o_ok.order_id) is None
    assert reopened[0].substitutions[0].original_id == "400123"


def test_place_mandate_fallback_when_agent_differs(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "mandate.db"))
    fallback = Mandate(
        customer="hotel-aurora", agent_id="buyer-aurora", per_order_eur=2500.0, per_week_eur=15000.0,
        approvers=["Laura Virtanen"],
    )
    calls = []
    seen = []

    def get_mandate(customer, agent_id=None):
        calls.append((customer, agent_id))
        if customer == "hotel-aurora" and agent_id is None:
            return fallback
        return None

    def decide_capture(q, mandate, products, spent=0.0, substitutions=()):
        seen.append(mandate)
        return Decision(outcome="auto", reasons=[Reason(code="WITHIN_MANDATE", text="within mandate")])

    stub(monkeypatch, "mandates", get=get_mandate)
    stub(monkeypatch, "policy", decide=decide_capture)
    now = datetime.now(timezone.utc)
    q = env["q_fb"] = Quote(
        quote_id="q_fb", customer="hotel-aurora", agent_id="other-agent", delivery_date=date(2026, 11, 16),
        lines=[QuoteLine(product_id="p", quantity=1, unit_price_net=10.0, line_total_net=10.0)],
        total_net=10.0, total_gross=11.0, issued_at=now, valid_until=now + timedelta(hours=1),
    )
    o = orders.place("hotel-aurora", "other-agent", req(q))
    assert o.status == "confirmed"
    assert calls == [("hotel-aurora", "other-agent"), ("hotel-aurora", None)]
    assert seen == [fallback]


def test_zero_connection_or_lock_leaks(env, monkeypatch, tmp_path):
    db_path = tmp_path / "leaks.db"
    monkeypatch.setenv("AIMO_DB", str(db_path))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setattr(orders.audit, "record", real_audit.record)
    opened = []
    closed = []

    class TrackedCon(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def tracked_connect():
        con = sqlite3.connect(db_path, check_same_thread=False, timeout=0.2, factory=TrackedCon)
        con.row_factory = sqlite3.Row
        opened.append(con)
        return con

    monkeypatch.setattr(orders.db, "connect", tracked_connect)
    alt = QuoteLine(product_id="alt", name="Alt", quantity=1, unit_price_net=5.0, line_total_net=5.0)
    stub(monkeypatch, "offers", fresh_offers=lambda c, d, ids: [])
    stub(monkeypatch, "allocate", stockout_plan=lambda lines, fresh, day: {"changed": True, "lines": [alt], "changes": ["p replaced"], "shortfall": 0})
    future = datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=2)
    for idx in range(5):
        qid = f"q_leak_{idx}"
        q = env[qid] = quote(qid, 10.0 if idx % 2 == 0 else 500.0).model_copy(update={"delivery_date": future})
        o = orders.place("hotel-aurora", "a1", req(q))
        assert orders.get(o.order_id) is not None
        assert orders.list(customer="hotel-aurora")
        _ = orders.confirmation(o.order_id)
        _ = orders.spent_this_week("hotel-aurora")
    orders.react_stockout("p")
    with pytest.raises(KeyError):
        orders.set_status("ord_missing", "approved")
    first = orders.list(customer="hotel-aurora")[0]
    with pytest.raises(ValueError):
        orders.set_status(first.order_id, "confirmed")
    assert len(opened) > 10
    assert len(opened) == len(closed)
    for con in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("select 1")
    probe = sqlite3.connect(db_path, timeout=0.05)
    try:
        probe.execute("begin immediate")
        probe.execute("commit")
    finally:
        probe.close()
    assert real_audit.verify() == (True, None)
