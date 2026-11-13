import json
import os
import urllib.request
from datetime import datetime, timezone
from importlib import import_module


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
    print(f"[approvals] {event} order={order.order_id} customer={order.customer} total={order.total_net:.2f}")
    url = os.environ.get("AIMO_APPROVAL_WEBHOOK")
    if url:
        try:
            req = urllib.request.Request(url, json.dumps({"event": event, "order_id": order.order_id, "customer": order.customer}).encode(), {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3)
        except OSError:
            pass


def pending(customer=None):
    rows = _m("orders").list(status="pending_approval", customer=customer)
    return sorted(rows, key=lambda o: o.created_at)


def _expire(order, why):
    out = _m("orders").set_status(order.order_id, "expired", "system")
    _m("audit").record(order.customer, "system", "approval.expired", order.order_id, why, {"order_id": order.order_id})
    return out


def decide(order_id, approve, by, note=""):
    orders = _m("orders")
    order = orders.get(order_id)
    if order is None:
        raise ApprovalError("NOT_FOUND", f"Order {order_id} does not exist.")
    if order.status != "pending_approval":
        raise ApprovalError("NOT_PENDING", f"Order {order_id} is {order.status}, not waiting for approval.")
    mandate = _m("mandates").get(order.customer, order.agent_id)
    if mandate is None or by not in mandate.approvers:
        raise ApprovalError("NOT_APPROVER", f"{by} is not an approver for {order.customer}.")
    if _expired(order, _now()):
        _expire(order, f"Quote expired before {by} decided, so the order was not placed.")
        raise ApprovalError("QUOTE_EXPIRED", "The quote is no longer valid, order marked expired.")
    verb = "approved" if approve else "rejected"
    why = f"{by} {verb} the order of {order.total_net:.2f} EUR. Note: {note or 'none'}. Held for approval because: {_reasons(order)}."
    detail = {"order_id": order_id, "note": note}
    if approve:
        orders.set_status(order_id, "approved", f"human:{by}")
        out = orders.set_status(order_id, "placed", f"human:{by}")
    else:
        out = orders.set_status(order_id, "rejected", f"human:{by}")
    _m("audit").record(order.customer, f"human:{by}", f"approval.{verb}", order_id, why, detail)
    _notify(out, verb)
    return out


def expire_stale(now=None):
    now = now or _now()
    n = 0
    for o in pending():
        if _expired(o, now):
            _expire(o, "Quote validity ended while waiting for approval, order expired.")
            n += 1
    return n
