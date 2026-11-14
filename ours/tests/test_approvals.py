import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from aimo import approvals
from aimo.models import Decision, Mandate, Order, Quote, QuoteLine, Reason

NOW = datetime.now(timezone.utc)


@pytest.fixture
def env(monkeypatch):
    db, events, quotes = {}, [], {}
    orders = types.SimpleNamespace(
        get=lambda i: db.get(i),
        list=lambda status=None, customer=None: [o for o in db.values() if o.status == status and customer in (None, o.customer)],
    )

    def set_status(i, status, actor, why=None):
        db[i] = db[i].model_copy(update={"status": status})
        return db[i]

    orders.set_status = set_status
    audit = types.SimpleNamespace(record=lambda c, a, ac, s, w, d=None: events.append((a, ac, s, w)))
    m = Mandate(customer="hotel-aurora", agent_id="a1", per_order_eur=100, per_week_eur=500, approvers=["anna"])
    mandates = types.SimpleNamespace(get=lambda c, a=None: m)
    qmod = types.SimpleNamespace(get=lambda q: quotes.get(q))
    for n, mod in dict(orders=orders, audit=audit, mandates=mandates, quotes=qmod).items():
        monkeypatch.setitem(sys.modules, f"aimo.{n}", mod)

    def add(oid, valid=1, age=0):
        line = QuoteLine(product_id="p", quantity=1, unit_price_net=500, line_total_net=500)
        db[oid] = Order(order_id=oid, quote_id="q" + oid, customer="hotel-aurora", agent_id="a1", lines=[line], total_net=500,
                        status="pending_approval", decision=Decision(outcome="needs_approval", reasons=[Reason(code="OVER_ORDER_LIMIT", text="Over the 100 EUR limit")]),
                        created_at=NOW - timedelta(minutes=age), updated_at=NOW)
        quotes["q" + oid] = Quote(quote_id="q" + oid, customer="hotel-aurora", agent_id="a1", delivery_date=NOW.date(), lines=[line],
                                  total_net=500, total_gross=620, issued_at=NOW, valid_until=NOW + timedelta(hours=valid))

    return add, db, events


def err(code, *a):
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(*a)
    assert e.value.code == code


def test_approve_places_and_audits(env):
    add, db, events = env
    add("o1")
    assert approvals.decide("o1", True, "anna", "ok").status == "placed"
    assert events[0][:3] == ("human:anna", "approval.approved", "o1")
    assert "Over the 100 EUR limit" in events[0][3] and "ok" in events[0][3]
    err("NOT_PENDING", "o1", True, "anna")
    assert len(events) == 1


def test_reject(env):
    add, db, events = env
    add("o1")
    assert approvals.decide("o1", False, "anna", "too dear").status == "rejected"
    assert events[0][1] == "approval.rejected"


def test_wrong_approver_and_missing(env):
    add, db, events = env
    add("o1")
    err("NOT_APPROVER", "o1", True, "bob")
    err("NOT_FOUND", "zz", True, "anna")
    assert db["o1"].status == "pending_approval"


def test_expiry(env):
    add, db, events = env
    add("o1", valid=-1)
    add("o2", valid=2)
    err("QUOTE_EXPIRED", "o1", True, "anna")
    assert db["o1"].status == "expired"
    add("o3", valid=-1)
    assert approvals.expire_stale() == 1
    assert db["o3"].status == "expired" and db["o2"].status == "pending_approval"
    assert [o.order_id for o in approvals.pending()] == ["o2"]


def test_real_flow(monkeypatch, tmp_path):
    pytest.importorskip("aimo.orders")
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    from aimo import audit, orders, quotes
    from aimo.models import PlaceOrderRequest, Product

    try:
        import aimo.catalog
    except ImportError:
        cat = types.ModuleType("aimo.catalog")
        cat.get = lambda pid: Product(product_id=pid, name="Valio lactose-free milk", brand="Valio")
        monkeypatch.setitem(sys.modules, "aimo.catalog", cat)
        import aimo
        monkeypatch.setattr(aimo, "catalog", cat, raising=False)

    line = QuoteLine(product_id="p1", quantity=1000, sales_unit="L", unit_price_net=4.0, line_total_net=4000.0)
    q = quotes.issue("hotel-aurora", "buyer-aurora", [line], NOW.date() + timedelta(days=2), NOW + timedelta(hours=2), 4960.0)
    o = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws))
    assert o.status == "pending_approval"
    assert [x.order_id for x in approvals.pending("hotel-aurora")] == [o.order_id]
    err("NOT_APPROVER", o.order_id, True, "Nobody")
    out = approvals.decide(o.order_id, True, "Laura Virtanen", "fine")
    assert out.status == "confirmed"
    err("NOT_PENDING", o.order_id, True, "Laura Virtanen")
    actions = [e.action for e in audit.events(order_id=o.order_id)]
    for a in ("quote.issued", "order.placed", "order.pending_approval", "approval.approved", "order.placed", "order.confirmed"):
        assert a in actions
    assert audit.verify()[0]
