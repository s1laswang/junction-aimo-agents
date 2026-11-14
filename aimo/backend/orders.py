"""Mock order backend: SQLite store for quotes, orders and approvals. Owner: gateway.

Valio Aimo exposes no order API, so we provide one. Order responses follow Peppol BIS
Order Response semantics: AP (accepted), CA (accepted with changes), RE (rejected).

DB file: aimo.config.STATE_DB (shared with trust's audit log, which uses its own tables).
Tests may point `DB_PATH` at a temp file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aimo import config
from aimo.models import Offer, Order, OrderResponseLine, Quote

DB_PATH: Path = config.STATE_DB
_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS gw_quotes (
    quote_id    TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    quote_hash  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gw_orders (
    order_id    TEXT PRIMARY KEY,
    quote_id    TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS gw_orders_status ON gw_orders(status);
CREATE TABLE IF NOT EXISTS gw_approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    decision    TEXT NOT NULL,
    approver    TEXT NOT NULL,
    note        TEXT,
    quote_hash  TEXT NOT NULL,
    ts          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gw_audit_fallback (
    seq         INTEGER PRIMARY KEY,
    body        TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def conn() -> sqlite3.Connection:
    path = str(DB_PATH)
    with _lock:
        c = _conns.get(path)
        if c is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            _conns[path] = c
        return c


# ------------------------------------------------------------------ quotes
def save_quote(q: Quote) -> Quote:
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO gw_quotes VALUES (?,?,?,?,?,?,?)",
            (q.quote_id, q.customer_id, q.agent_id, q.quote_hash, q.created_at.isoformat(),
             q.expires_at.isoformat(), q.model_dump_json()),
        )
    return q


def get_quote(quote_id: str) -> Quote | None:
    with _lock:
        row = conn().execute("SELECT body FROM gw_quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return Quote.model_validate_json(row[0]) if row else None


# ------------------------------------------------------------------ orders
def save_order(o: Order) -> Order:
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO gw_orders VALUES (?,?,?,?,?,?,?)",
            (o.order_id, o.quote_id, o.customer_id, o.status, o.created_at.isoformat(),
             o.updated_at.isoformat(), o.model_dump_json()),
        )
    return o


def get_order(order_id: str) -> Order | None:
    with _lock:
        row = conn().execute("SELECT body FROM gw_orders WHERE order_id=?", (order_id,)).fetchone()
    return Order.model_validate_json(row[0]) if row else None


def list_orders(status: str | None = None, customer_id: str | None = None, limit: int = 200) -> list[Order]:
    sql, args = "SELECT body FROM gw_orders WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if customer_id:
        sql += " AND customer_id=?"
        args.append(customer_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = conn().execute(sql, args).fetchall()
    return [Order.model_validate_json(r[0]) for r in rows]


def order_for_quote(quote_id: str) -> Order | None:
    with _lock:
        row = conn().execute(
            "SELECT body FROM gw_orders WHERE quote_id=? ORDER BY created_at DESC LIMIT 1", (quote_id,)
        ).fetchone()
    return Order.model_validate_json(row[0]) if row else None


# ------------------------------------------------------------------ approvals
def record_approval(order_id: str, decision: str, approver: str, note: str | None, quote_hash: str) -> dict[str, Any]:
    ts = _now().isoformat()
    with _lock:
        conn().execute(
            "INSERT INTO gw_approvals (order_id, decision, approver, note, quote_hash, ts) VALUES (?,?,?,?,?,?)",
            (order_id, decision, approver, note, quote_hash, ts),
        )
    return {"order_id": order_id, "decision": decision, "approver": approver, "note": note,
            "quote_hash": quote_hash, "ts": ts}


def approvals_for(order_id: str) -> list[dict[str, Any]]:
    with _lock:
        rows = conn().execute(
            "SELECT order_id, decision, approver, note, quote_hash, ts FROM gw_approvals WHERE order_id=? ORDER BY id",
            (order_id,),
        ).fetchall()
    keys = ["order_id", "decision", "approver", "note", "quote_hash", "ts"]
    return [dict(zip(keys, r)) for r in rows]


# ------------------------------------------------------------------ fulfilment (Peppol order response)
def confirm_lines(quote: Quote, offers: dict[str, Offer]) -> tuple[str, list[OrderResponseLine]]:
    """Re-check live availability at placement and build the order response.

    Per line: AP when fully confirmed, CA when only part of the quantity can be confirmed,
    RE when the product can no longer be delivered for the date. Header: AP when all lines
    are AP, RE when all are RE, otherwise CA.
    Prices are NOT re-checked: the signed quote fixed them until expires_at.
    """
    lines: list[OrderResponseLine] = []
    for ql in quote.lines:
        o = offers.get(ql.product_id)
        if o is None:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="product not returned by availability re-check"))
            continue
        blocking = [i for i in o.issues if i in ("cutoff_passed", "sales_block", "out_of_stock",
                                                 "late_delivery", "availability_not_found", "no_schedule")]
        if blocking or (o.available_amount is not None and o.available_amount <= 0):
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: " + ", ".join(blocking or ["no stock"])))
        elif o.available_amount is not None and o.available_amount < ql.qty_sales_units:
            lines.append(OrderResponseLine(
                product_id=ql.product_id, code="CA", qty_confirmed=float(o.available_amount),
                note=f"partial stock at placement: {o.available_amount:g} of {ql.qty_sales_units:g} {ql.sales_unit} confirmed"))
        else:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="AP", qty_confirmed=ql.qty_sales_units,
                                           note="confirmed"))
    codes = {ln.code for ln in lines}
    header = "AP" if codes == {"AP"} else "RE" if codes == {"RE"} or not codes else "CA"
    return header, lines


# ------------------------------------------------------------------ minimal audit fallback (until aimo.trust lands)
def fallback_audit_append(event: dict[str, Any]) -> dict[str, Any]:
    """Hash-chained, UNSIGNED audit event. Used only when aimo.trust.audit is unavailable."""
    import hashlib

    with _lock:
        c = conn()
        row = c.execute("SELECT seq, body FROM gw_audit_fallback ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (row[0] + 1) if row else 1
        prev = json.loads(row[1])["hash"] if row else "0" * 64
        ev = {"seq": seq, "ts": _now().isoformat(), **event, "prev_hash": prev, "signature": "", "kid": "unsigned-fallback"}
        canon = json.dumps({k: v for k, v in ev.items() if k not in ("hash", "signature", "kid")},
                           sort_keys=True, separators=(",", ":"), default=str)
        ev["hash"] = hashlib.sha256(canon.encode()).hexdigest()
        c.execute("INSERT INTO gw_audit_fallback VALUES (?,?)", (seq, json.dumps(ev, default=str)))
    return ev


def fallback_audit_events(subject_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        rows = conn().execute("SELECT body FROM gw_audit_fallback ORDER BY seq").fetchall()
    evs = [json.loads(r[0]) for r in rows]
    return [e for e in evs if subject_id is None or e.get("subject_id") == subject_id]
