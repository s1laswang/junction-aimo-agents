import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from aimo.models import AuditEvent

GENESIS = "0" * 64
FIELDS = ("seq", "ts", "customer", "actor", "action", "subject", "detail", "why")
VERBS = {
    "quote.issued": "was issued a signed quote",
    "quote.redeemed": "redeemed a signed quote",
    "order.placed": "placed an order",
    "order.auto": "had an order placed automatically",
    "order.pending_approval": "sent an order for human approval",
    "order.blocked": "had an order blocked",
    "approval.approved": "approved an order",
    "approval.rejected": "rejected an order",
    "approval.expired": "saw an approval expire",
}


def _db():
    path = os.environ.get("AIMO_DB", "var/aimo.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.execute(
        "create table if not exists audit (seq integer primary key, ts text not null,"
        " customer text not null, actor text not null, action text not null,"
        " subject text not null, detail text not null, why text not null,"
        " prev_hash text not null, hash text not null)"
    )
    return con


def _digest(prev, row):
    body = json.dumps({k: row[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def _event(r):
    return AuditEvent(
        seq=r[0], ts=r[1], customer=r[2], actor=r[3], action=r[4], subject=r[5],
        detail=json.loads(r[6]), why=r[7], prev_hash=r[8], hash=r[9],
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
            "subject": str(subject), "detail": detail or {}, "why": why,
        }
        h = _digest(prev, row)
        con.execute(
            "insert into audit values (?,?,?,?,?,?,?,?,?,?)",
            (seq, row["ts"], customer, actor, action, row["subject"],
             json.dumps(row["detail"], sort_keys=True), why, prev, h),
        )
        con.execute("commit")
        return _event((seq, row["ts"], customer, actor, action, row["subject"],
                       json.dumps(row["detail"]), why, prev, h))
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


def narrate(events, view="buyer"):
    lines = []
    for e in events:
        ts = e.ts if isinstance(e.ts, datetime) else datetime.fromisoformat(str(e.ts).replace("Z", "+00:00"))
        verb = VERBS.get(e.action, e.action.replace(".", " ").replace("_", " "))
        who = _who(e, view)
        if view == "valio" and e.actor.startswith("human"):
            who += f" at {_name(e.customer)}"
        s = f"{ts:%H:%M} {who} {verb}"
        m = _money(e.detail)
        if m:
            s += f" for {m}"
        s += "."
        if e.why:
            s += " " + e.why.strip().rstrip(".") + "."
        if view == "valio":
            s += f" [{e.subject}, #{e.seq}]"
        lines.append(s)
    return "\n".join(lines)
