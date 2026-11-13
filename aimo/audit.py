import hashlib
import json
import os
import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from joserfc import jws

from aimo import db, keys
from aimo.models import AuditEvent

warnings.filterwarnings("ignore", message="EdDSA is deprecated")
HELSINKI = ZoneInfo("Europe/Helsinki")

GENESIS = "0" * 64
FIELDS = ("seq", "ts", "customer", "actor", "action", "subject", "detail", "why")
VERBS = {
    "quote.issued": "was issued a signed quote",
    "quote.redeemed": "redeemed a signed quote",
    "order.draft": "started an order",
    "order.placed": "placed an order",
    "order.pending_approval": "had an order held for human approval",
    "order.approved": "had an order approved",
    "order.confirmed": "had an order confirmed by the supplier",
    "order.rejected": "had an order rejected",
    "order.blocked": "had an order blocked",
    "order_rejected": "had an order refused at the door",
    "parse.request": "asked for products",
    "offers.compared": "compared offers",
    "order.expired": "had an order expire",
    "approval.approved": "approved an order",
    "approval.rejected": "rejected an order",
    "mandate_updated": "had its buying mandate updated",
}


def _db():
    con = db.connect()
    con.isolation_level = None
    con.execute(
        "create table if not exists audit (seq integer primary key, ts text not null,"
        " customer text not null, actor text not null, action text not null,"
        " subject text not null, detail text not null, why text not null,"
        " prev_hash text not null, hash text not null)"
    )
    return con


def _norm(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _dump(d):
    return json.dumps(d, sort_keys=True, default=str)


def _digest(prev, row):
    body = json.dumps({k: row[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def _event(r):
    return AuditEvent(
        seq=r[0], ts=r[1], customer=r[2], actor=r[3], action=r[4], subject=r[5],
        detail=_norm(json.loads(r[6])), why=r[7], prev_hash=r[8], hash=r[9],
    )


def record(customer, actor, action, subject, why, detail=None):
    con = _db()
    try:
        con.execute("begin immediate")
        last = con.execute("select seq, hash from audit order by seq desc limit 1").fetchone()
        seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
        row = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "customer": customer, "actor": actor, "action": action,
            "subject": str(subject), "detail": _norm(json.loads(_dump(detail or {}))), "why": why,
        }
        h = _digest(prev, row)
        con.execute(
            "insert into audit values (?,?,?,?,?,?,?,?,?,?)",
            (seq, row["ts"], customer, actor, action, row["subject"],
             _dump(row["detail"]), why, prev, h),
        )
        con.execute("commit")
        return _event((seq, row["ts"], customer, actor, action, row["subject"],
                       _dump(row["detail"]), why, prev, h))
    except BaseException:
        if con.in_transaction:
            con.execute("rollback")
        raise
    finally:
        con.close()


def events(customer=None, order_id=None):
    con = _db()
    try:
        rows = con.execute("select * from audit order by seq").fetchall()
    finally:
        con.close()
    out = [_event(r) for r in rows]
    if customer:
        out = [e for e in out if e.customer == customer]
    if order_id:
        ids = {order_id}
        for e in out:
            if order_id in (e.subject, e.detail.get("order_id")) and e.detail.get("quote_id"):
                ids.add(e.detail["quote_id"])
        out = [
            e for e in out
            if ids & {e.subject, e.detail.get("order_id"), e.detail.get("quote_id")}
        ]
    return out


def verify():
    con = _db()
    try:
        rows = con.execute("select * from audit order by seq").fetchall()
    finally:
        con.close()
    prev = GENESIS
    for n, r in enumerate(rows, 1):
        e = _event(r)
        row = {k: getattr(e, k) for k in FIELDS}
        row["ts"] = r[1]
        if e.seq != n or e.prev_hash != prev or _digest(prev, row) != e.hash:
            return False, e.seq
        prev = e.hash
    return True, None


def _name(slug):
    return slug.replace("-", " ").title()


def _who(e, view):
    kind, _, who = e.actor.partition(":")
    if kind == "agent":
        return f"{_name(e.customer)}'s buyer agent" + (f" ({who})" if view == "valio" and who else "")
    if kind == "human":
        return who.title() if who else "A person"
    return "The system"


def _money(d):
    for k in ("total_gross", "total_net", "amount"):
        if isinstance(d.get(k), (int, float)):
            return f"{d[k]:,.2f} {d.get('currency', 'EUR')}"
    return None


SYSTEM = {
    "order.approved": "Mandate check passed:",
    "order.pending_approval": "Order held for human approval:",
    "order.blocked": "Order blocked:",
    "order.confirmed": "Order confirmed by Valio (mock).",
    "order.expired": "Order expired:",
    "order.rejected": "Order rejected:",
    "order_rejected": "Refused:",
}


def narrate(events, view="buyer"):
    lines = []
    for e in events:
        ts = e.ts if isinstance(e.ts, datetime) else datetime.fromisoformat(str(e.ts).replace("Z", "+00:00"))
        verb = VERBS.get(e.action, e.action.replace(".", " ").replace("_", " "))
        who = _who(e, view)
        if view == "valio" and e.actor.startswith("human"):
            who += f" at {_name(e.customer)}"
        clock = f"{ts.astimezone(HELSINKI):%H:%M}"
        if e.action == "order_rejected" or (e.actor == "system" and e.action in SYSTEM):
            why = e.why.strip().rstrip(".").replace("Mock confirmation, delivery", "Delivery")
            s = f"{clock} {SYSTEM[e.action]}" + (f" {why}." if why else "")
        else:
            s = f"{clock} {who} {verb}"
            m = _money(e.detail)
            if m and m.split()[0].replace(",", "") not in e.why.replace(",", ""):
                s += f" for {m}"
            s += "."
            if e.why:
                s += " " + e.why.strip().rstrip(".") + "."
        if e.action in ("order.pending_approval", "order.blocked"):
            d = e.detail.get("decision")
            rs = e.detail.get("reasons") or (d.get("reasons") if isinstance(d, dict) else None) or []
            texts = [r.get("text", "") if isinstance(r, dict) else str(r) for r in rs]
            texts = [t for t in texts if t and t.rstrip(".") not in e.why]
            if texts:
                s += " Reasons: " + " ".join(t.rstrip(".") + "." for t in texts)
        if view == "valio":
            s += f" [{e.subject}, #{e.seq}]"
        lines.append(s)
    return "\n".join(lines)


def checkpoint():
    con = _db()
    try:
        r = con.execute("select seq, hash, ts from audit order by seq desc limit 1").fetchone()
    finally:
        con.close()
    head = {"seq": r[0], "hash": r[1], "ts": r[2]} if r else {"seq": 0, "hash": GENESIS, "ts": None}
    token = jws.serialize_compact(
        {"alg": "EdDSA", "kid": keys.kid()},
        json.dumps(head, sort_keys=True, separators=(",", ":")).encode(),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )
    return {**head, "jws": token}
