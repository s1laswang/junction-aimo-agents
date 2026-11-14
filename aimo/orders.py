import json
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aimo import audit, db
from aimo.models import Decision, Order, PlaceOrderRequest, QuoteRequest, Reason, Substitution

HEL = ZoneInfo("Europe/Helsinki")

NEXT = {
    "draft": {"pending_approval", "approved", "blocked", "expired"},
    "pending_approval": {"approved", "rejected", "expired"},
    "approved": {"placed", "pending_approval"},
    "placed": {"confirmed", "pending_approval"},
    "confirmed": {"pending_approval"},
}


def _now():
    return datetime.now(timezone.utc)


def _lines(lines):
    return [{"product_id": l.product_id, "name": l.name, "quantity": l.quantity} for l in lines]


def _cat_get(catalog, pid):
    if not pid:
        return None
    try:
        return catalog.get(pid)
    except FileNotFoundError:
        return None


def _why_sub(l, products, catalog):
    orig = products.get(l.substitute_for) or _cat_get(catalog, l.substitute_for)
    new = products.get(l.product_id)
    a = orig.name if orig else l.substitute_for
    b = (new.name if new else None) or l.name or l.product_id
    return f"Substitute for {a}: {b}."


def _con():
    con = db.connect()
    try:
        con.execute(
            "create table if not exists orders (order_id text primary key, idem text unique not null,"
            " customer text not null, status text not null, created_at text not null,"
            " valid_until text not null, delivery_date text not null, data text not null,"
            " confirmation text)"
        )
        con.commit()
        return con
    except BaseException:
        con.close()
        raise


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
    try:
        r = con.execute("select * from orders where order_id=?", (order_id,)).fetchone()
        return _expire(con, r) if r else None
    finally:
        con.close()


def list(status=None, customer=None):
    con = _con()
    try:
        q = "select * from orders"
        w = [f"{k}=?" for k, v in (("status", status), ("customer", customer)) if v]
        a = [v for v in (status, customer) if v]
        if w:
            q += " where " + " and ".join(w)
        rows = con.execute(q + " order by created_at, rowid", a).fetchall()
        out = [_expire(con, r) for r in rows]
        return [o for o in out if status is None or o.status == status]
    finally:
        con.close()


def confirmation(order_id):
    con = _con()
    try:
        r = con.execute("select confirmation from orders where order_id=?", (order_id,)).fetchone()
    finally:
        con.close()
    return json.loads(r[0]) if r and r[0] else None


def set_status(order_id, status, actor="system", why=None):
    con = _con()
    try:
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
        reasons = [x.model_dump() for x in o.decision.reasons]
        why = why or "; ".join(x["text"] for x in reasons) or f"Order moved to {status}."
        audit.record(o.customer, actor, f"order.{status}", o.order_id, why, {"order_id": o.order_id, "quote_id": o.quote_id, "total_net": o.total_net, "reasons": reasons})
        if status == "placed":
            return _confirm(con, r["delivery_date"], o)
        return o
    finally:
        con.close()


def _confirm(con, delivery_date, o):
    conf = {"confirmation_id": "MOCK-" + uuid.uuid4().hex[:8].upper(), "delivery_date": delivery_date, "mock": True}
    o.status = "confirmed"
    o.updated_at = _now()
    _save(con, o)
    con.execute("update orders set confirmation=? where order_id=?", (json.dumps(conf), o.order_id))
    con.commit()
    audit.record(o.customer, "system", "order.confirmed", o.order_id, f"Confirmed for delivery {date.fromisoformat(delivery_date).strftime('%a %-d %b')} (simulated fulfilment, Valio has no order API).", {"order_id": o.order_id, "quote_id": o.quote_id, **conf})
    return o


def create_quote(customer, agent_id, req: QuoteRequest):
    from aimo import offers, quotes
    lines, total_gross, valid_until = offers.price_lines(customer, req.delivery_date, req.offers)
    return quotes.issue(customer, agent_id, lines, req.delivery_date, valid_until, total_gross)


def spent_this_week(customer):
    cut = (_now() - timedelta(days=7)).isoformat()
    con = _con()
    try:
        r = con.execute(
            "select data from orders where customer=? and created_at>=? and status in ('placed','approved','confirmed')",
            (customer, cut),
        ).fetchall()
    finally:
        con.close()
    return sum(_order(x).total_net for x in r)


def place(customer, agent_id, req: PlaceOrderRequest):
    from aimo import catalog, mandates, policy, quotes
    try:
        quote = quotes.redeem(req.quote_id, req.jws, customer, agent_id)
    except quotes.QuoteError as e:
        audit.record(customer, f"agent:{agent_id}", "order.rejected", req.quote_id, getattr(e, "text", str(e)), {"quote_id": req.quote_id, "code": getattr(e, "code", None)})
        raise
    con = _con()
    try:
        r = con.execute("select * from orders where idem=?", (quote.quote_id,)).fetchone()
        if r:
            return _expire(con, r)
        now = _now()
        o = Order(
            order_id="ord_" + uuid.uuid4().hex[:12], quote_id=quote.quote_id, customer=customer,
            agent_id=agent_id, lines=quote.lines, total_net=quote.total_net, status="draft",
            delivery_date=quote.delivery_date, valid_until=quote.valid_until,
            decision=Decision(outcome="needs_approval"), created_at=now, updated_at=now,
        )
        con.execute(
            "insert into orders values (?,?,?,?,?,?,?,?,NULL)",
            (o.order_id, quote.quote_id, customer, o.status, now.isoformat(), quote.valid_until.isoformat(),
             quote.delivery_date.isoformat(), o.model_dump_json()),
        )
        con.commit()
        detail = {"order_id": o.order_id, "quote_id": quote.quote_id, "total_net": o.total_net, "lines": _lines(quote.lines)}
        audit.record(customer, f"agent:{agent_id}", "order.placed", o.order_id, "Agent submitted an order against a signed quote.", detail)
        pids = [pid for l in quote.lines for pid in (l.product_id, l.substitute_for) if pid]
        products = {pid: _cat_get(catalog, pid) for pid in dict.fromkeys(pids)}
        subs = [Substitution(original_id=l.substitute_for, substitute_id=l.product_id, reason=_why_sub(l, products, catalog)) for l in quote.lines if l.substitute_for]
        o.substitutions = subs
        mandate = mandates.get(customer, agent_id) or mandates.get(customer)
        o.decision = policy.decide(quote, mandate, products, spent_this_week(customer), subs)
        if req.ask_human and o.decision.outcome == "auto":
            o.decision = Decision(outcome="needs_approval", reasons=[Reason(code="AGENT_REQUESTED", text=req.note or "The agent asked for human approval.")])
        _save(con, o)
        con.commit()
    finally:
        con.close()
    why = "; ".join(x.text for x in o.decision.reasons) or o.decision.outcome
    out = o.decision.outcome
    if out == "blocked":
        return set_status(o.order_id, "blocked", "system", why)
    if out == "needs_approval":
        return set_status(o.order_id, "pending_approval", "system", why)
    set_status(o.order_id, "approved", "system", "Within mandate: " + why)
    return set_status(o.order_id, "placed", "system", "Placed automatically within mandate.")


def react_stockout(product_id):
    from aimo import allocate, offers
    pid = str(product_id)
    today = datetime.now(HEL).date()
    con = _con()
    try:
        out = []
        rows = con.execute("select * from orders where status in ('placed','approved','confirmed','pending_approval')").fetchall()
        for r in rows:
            o = _expire(con, r)
            if o.status == "expired" or all(str(l.product_id) != pid for l in o.lines):
                continue
            day = date.fromisoformat(r["delivery_date"])
            if day < today:
                continue
            fresh = offers.fresh_offers(o.customer, day, [str(l.product_id) for l in o.lines])
            plan = allocate.stockout_plan(o.lines, fresh, day)
            if not plan["changed"]:
                continue
            old = {str(l.product_id) for l in o.lines}
            o.lines = plan["lines"]
            o.total_net = round(sum(l.line_total_net for l in o.lines), 2)
            seen = {(str(s.original_id), str(s.substitute_id)) for s in o.substitutions}
            for l in o.lines:
                lid = str(l.product_id)
                if lid not in old and (pid, lid) not in seen:
                    seen.add((pid, lid))
                    o.substitutions.append(Substitution(original_id=pid, substitute_id=lid, reason="Stock dropped, refilled from the next offer."))
            why = "Stock dropped after the order: " + "; ".join(plan["changes"]) + (f". Shortfall {plan['shortfall']:g}." if plan["shortfall"] else "")
            o.decision = Decision(outcome="needs_approval", reasons=[Reason(code="STOCKOUT", text=why)])
            o.status = "pending_approval"
            o.updated_at = _now()
            con.execute("update orders set status=?, data=?, confirmation=NULL where order_id=?", (o.status, o.model_dump_json(), o.order_id))
            con.commit()
            audit.record(o.customer, "system", "order.stockout", o.order_id, why, {"order_id": o.order_id, "quote_id": o.quote_id, "product_id": pid, "total_net": o.total_net, "reasons": [x.model_dump() for x in o.decision.reasons], "lines": _lines(o.lines)})
            out.append(o)
        return out
    finally:
        con.close()
