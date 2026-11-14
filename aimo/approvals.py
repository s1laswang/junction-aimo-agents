import threading
from datetime import datetime, timezone
from importlib import import_module

_LOCK = threading.RLock()


class ApprovalError(Exception):
    def __init__(self, code, text):
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


def _m(name):
    return import_module(f"aimo.{name}")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _expired(order, now):
    q = _m("quotes").get(order.quote_id)
    return q is not None and _aware(q.valid_until) <= _aware(now)


def _reasons(order):
    return "; ".join(r.text for r in order.decision.reasons) or "no reasons recorded"


def _notify(order, event):
    print(f"[approvals] {event} order={order.order_id} customer={order.customer} total={order.total_net:,.2f}")


def pending(customer=None):
    rows = _m("orders").list(status="pending_approval", customer=customer)
    return sorted(rows, key=lambda o: o.created_at)


def _expire(order, why):
    return _m("orders").set_status(order.order_id, "expired", "system", why)


def decide(order_id, approve, by, note=""):
    orders = _m("orders")
    with getattr(orders, "_LOCK", _LOCK):
        order = orders.get(order_id)
        if order is None:
            raise ApprovalError("NOT_FOUND", f"Order {order_id} does not exist.")
        if order.status != "pending_approval":
            raise ApprovalError("NOT_PENDING", f"Order {order_id} is {order.status}, not waiting for approval.")
        mandates = _m("mandates")
        mandate = mandates.get(order.customer, order.agent_id) or mandates.get(order.customer)
        if mandate is None or by not in mandate.approvers:
            raise ApprovalError("NOT_APPROVER", f"{by} is not an approver for {order.customer}.")
        if _expired(order, _now()):
            _expire(order, f"Quote expired before {by} decided, so the order was not placed.")
            raise ApprovalError("QUOTE_EXPIRED", "The quote is no longer valid, order marked expired.")
        verb = "approved" if approve else "rejected"
        why = f"{by} {verb} the order of {order.total_net:,.2f} EUR. Note: {note or 'none'}. Held for approval because: {_reasons(order)}."
        detail = {"order_id": order_id, "note": note}
        actor = f"human:{by}"
        _m("audit").record(order.customer, actor, f"approval.{verb}", order_id, why, detail)
        try:
            if approve:
                orders.set_status(order_id, "approved", actor, f"Approved by {by}.")
                out = orders.set_status(order_id, "placed", actor, "Placed after human approval.")
            else:
                out = orders.set_status(order_id, "rejected", actor, f"Rejected by {by}.")
        except ValueError:
            raise ApprovalError("NOT_PENDING", f"Order {order_id} was already decided.")
    _notify(out, verb)
    return out


def expire_stale(now=None):
    orders = _m("orders")
    with getattr(orders, "_LOCK", _LOCK):
        now = now or _now()
        n = 0
        for o in pending():
            if _expired(o, now):
                _expire(o, "Quote validity ended while waiting for approval, order expired.")
                n += 1
        return n
