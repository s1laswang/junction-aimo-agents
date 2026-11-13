import json
import uuid
from datetime import datetime, timezone

from datetime import timedelta

from aimo import audit, db
from aimo.models import Decision, Order, Quote, QuoteRequest, PlaceOrderRequest

NEXT = {
    "draft": {"pending_approval", "approved", "blocked", "expired"},
    "pending_approval": {"approved", "rejected", "expired"},
    "approved": {"placed"},
    "placed": {"confirmed"},
}


def _now():
    return datetime.now(timezone.utc)


def _con():
    con = db.connect()
    con.execute(
        "create table if not exists orders (order_id text primary key, idem text unique not null,"
        " customer text not null, status text not null, created_at text not null,"
        " valid_until text not null, delivery_date text not null, data text not null,"
        " confirmation text)"
    )
    return con


def _order(r):
    return Order.model_validate_json(r["data"])


def _save(con, o, **extra):
    con.execute("update orders set status=?, data=? where order_id=?", (o.status, o.model_dump_json(), o.order_id))


def _expire(con, r):
    if r["status"] in ("draft", "pending_approval"):
        vu = datetime.fromisoformat(r["valid_until"])
        if vu.tzinfo is None:
            vu = vu.replace(tzinfo=timezone.utc)
        if vu <= _now():
            o = _order(r)
            o.status = "expired"
            o.updated_at = _now()
            _save(con, o)
            con.commit()
            audit.record(o.customer, "system", "order.expired", o.order_id, "The quote validity ended before the order was decided.", {"valid_until": r["valid_until"]})
            return o
    return _order(r)


def get(order_id):
    con = _con()
    r = con.execute("select * from orders where order_id=?", (order_id,)).fetchone()
    return _expire(con, r) if r else None


def list(status=None, customer=None):
    con = _con()
    q = "select * from orders"
    w = [f"{k}=?" for k, v in (("status", status), ("customer", customer)) if v]
    a = [v for v in (status, customer) if v]
    if w:
        q += " where " + " and ".join(w)
    rows = con.execute(q + " order by created_at, rowid", a).fetchall()
    out = [_expire(con, r) for r in rows]
    return [o for o in out if status is None or o.status == status]


def confirmation(order_id):
    r = _con().execute("select confirmation from orders where order_id=?", (order_id,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def set_status(order_id, status, actor="system", why=None):
    con = _con()
    r = con.execute("select * from orders where order_id=?", (order_id,)).fetchone()
    if not r:
        raise KeyError(order_id)
    o = _expire(con, r)
    if status not in NEXT.get(o.status, ()):
        raise ValueError(f"{o.status} -> {status} not allowed")
    o.status = status
    o.updated_at = _now()
    _save(con, o)
    con.commit()
    audit.record(o.customer, actor, f"order.{status}", o.order_id, why or f"Order moved to {status}.", {"order_id": o.order_id, "quote_id": o.quote_id, "total_net": o.total_net})
    if status == "placed":
        return _confirm(con, r["delivery_date"], o)
    return o


def _confirm(con, delivery_date, o):
    conf = {"confirmation_id": "MOCK-" + uuid.uuid4().hex[:8].upper(), "delivery_date": delivery_date, "mock": True}
    o.status = "confirmed"
    o.updated_at = _now()
    _save(con, o)
    con.execute("update orders set confirmation=? where order_id=?", (json.dumps(conf), o.order_id))
    con.commit()
    audit.record(o.customer, "system", "order.confirmed", o.order_id, f"Mock confirmation, delivery {delivery_date}.", {"order_id": o.order_id, "quote_id": o.quote_id, **conf})
    return o


def create_quote(customer, agent_id, req: QuoteRequest):
    from aimo import offers, quotes
    lines, total_gross, valid_until = offers.price_lines(customer, req)
    return quotes.issue(customer, agent_id, lines, req.delivery_date, valid_until, total_gross)


def spent_this_week(customer):
    cut = (_now() - timedelta(days=7)).isoformat()
    r = _con().execute(
        "select data from orders where customer=? and created_at>=? and status in ('placed','approved','confirmed')",
        (customer, cut),
    ).fetchall()
    return sum(_order(x).total_net for x in r)


def place(customer, agent_id, req: PlaceOrderRequest):
    from aimo import catalog, mandates, policy, quotes
    quote = quotes.redeem(req.quote_id, req.jws, customer, agent_id)
    con = _con()
    r = con.execute("select * from orders where idem=?", (quote.quote_id,)).fetchone()
    if r:
        return _expire(con, r)
    now = _now()
    o = Order(
        order_id="ord_" + uuid.uuid4().hex[:12], quote_id=quote.quote_id, customer=customer,
        agent_id=agent_id, lines=quote.lines, total_net=quote.total_net, status="draft",
        decision=Decision(outcome="needs_approval"), created_at=now, updated_at=now,
    )
    con.execute(
        "insert into orders values (?,?,?,?,?,?,?,?,NULL)",
        (o.order_id, quote.quote_id, customer, o.status, now.isoformat(), quote.valid_until.isoformat(),
         quote.delivery_date.isoformat(), o.model_dump_json()),
    )
    con.commit()
    detail = {"order_id": o.order_id, "quote_id": quote.quote_id, "total_net": o.total_net}
    audit.record(customer, f"agent:{agent_id}", "order.placed", o.order_id, "Agent submitted an order against a signed quote.", detail)
    products = {l.product_id: catalog.get(l.product_id) for l in quote.lines}
    o.decision = policy.decide(quote, mandates.get(customer, agent_id), products, spent_this_week(customer))
    _save(con, o)
    con.commit()
    why = "; ".join(x.text for x in o.decision.reasons) or o.decision.outcome
    out = o.decision.outcome
    if out == "blocked":
        return set_status(o.order_id, "blocked", "system", why)
    if out == "needs_approval":
        return set_status(o.order_id, "pending_approval", "system", why)
    set_status(o.order_id, "approved", "system", why)
    return set_status(o.order_id, "placed", "system", "Placed automatically within mandate.")
