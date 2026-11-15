"""Mock order backend: SQLite store for quotes, orders and approvals. Owner: gateway.

Valio Aimo exposes no order API, so we provide one. Order responses follow Peppol BIS
Order Response semantics: AP (accepted), CA (accepted with changes), RE (rejected).

DB file: aimo.config.STATE_DB (shared with trust's audit log, which uses its own tables).
Tests may point `DB_PATH` at a temp file.
"""
from __future__ import annotations

import json
import math
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
    try:
        from aimo.backend import clock
        return clock.now()
    except Exception:
        return datetime.now(timezone.utc)


def close_all() -> None:
    with _lock:
        conns = list(_conns.values())
        _conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


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


def _valid_id(val: Any) -> bool:
    if not isinstance(val, str) or not val or not val.strip() or len(val) > 256 or "\x00" in val:
        return False
    try:
        val.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _strip_bad_chars(val: str) -> str:
    cleaned = val.replace("\x00", "")
    try:
        cleaned.encode("utf-8")
        return cleaned
    except UnicodeEncodeError:
        return cleaned.encode("utf-8", "replace").decode("utf-8")


def _clean_field(val: Any, max_len: int = 256) -> str:
    if val is None or isinstance(val, bool):
        return ""
    raw = val if isinstance(val, str) else str(val)
    return _strip_bad_chars(raw)[:max_len]


def _parse_quote(raw: Any) -> Quote | None:
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        return Quote.model_validate_json(raw)
    except Exception:
        return None


def _parse_order(raw: Any) -> Order | None:
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        return Order.model_validate_json(raw)
    except Exception:
        return None


def _valid_filter_str(val: Any) -> bool:
    if not isinstance(val, str) or len(val) > 256 or "\x00" in val:
        return False
    try:
        val.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _coerce_limit(limit: Any) -> int:
    if isinstance(limit, bool):
        return 200
    if isinstance(limit, int):
        return max(1, min(1000, limit))
    if isinstance(limit, float):
        if math.isnan(limit):
            return 200
        if math.isinf(limit):
            return 1000 if limit > 0 else 1
        if limit <= 0:
            return 1
        return 200
    if isinstance(limit, str):
        s = limit.strip()
        if s and (s.isdigit() or (s[0] in "+-" and s[1:].isdigit())):
            return max(1, min(1000, int(s)))
        return 200
    return 200


def _json_clean(val: Any) -> Any:
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, str):
        return _strip_bad_chars(val)
    if isinstance(val, (list, tuple)):
        return [_json_clean(x) for x in val]
    if isinstance(val, dict):
        return {_strip_bad_chars(str(k)): _json_clean(v) for k, v in val.items()}
    return _strip_bad_chars(str(val))


def _parse_fallback_event(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


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
    if not _valid_id(quote_id):
        return None
    with _lock:
        row = conn().execute("SELECT body FROM gw_quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return _parse_quote(row[0]) if row else None


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
    if not _valid_id(order_id):
        return None
    with _lock:
        row = conn().execute("SELECT body FROM gw_orders WHERE order_id=?", (order_id,)).fetchone()
    return _parse_order(row[0]) if row else None


def list_orders(status: str | None = None, customer_id: str | None = None, limit: int = 200) -> list[Order]:
    if status is not None and not _valid_filter_str(status):
        return []
    if customer_id is not None and not _valid_filter_str(customer_id):
        return []
    lim = _coerce_limit(limit)
    sql, args = "SELECT body FROM gw_orders WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if customer_id:
        sql += " AND customer_id=?"
        args.append(customer_id)
    sql += " ORDER BY created_at DESC"
    out: list[Order] = []
    with _lock:
        cur = conn().execute(sql, args)
        try:
            for r in cur:
                parsed = _parse_order(r[0])
                if parsed is not None:
                    out.append(parsed)
                    if len(out) >= lim:
                        break
        finally:
            cur.close()
    return out


def order_for_quote(quote_id: str) -> Order | None:
    if not _valid_id(quote_id):
        return None
    with _lock:
        rows = conn().execute(
            "SELECT body FROM gw_orders WHERE quote_id=? ORDER BY created_at DESC", (quote_id,)
        ).fetchall()
    for r in rows:
        parsed = _parse_order(r[0])
        if parsed is not None:
            return parsed
    return None


# ------------------------------------------------------------------ approvals
def record_approval(order_id: str, decision: str, approver: str, note: str | None, quote_hash: str) -> dict[str, Any]:
    oid = _clean_field(order_id, 256)
    dec = _clean_field(decision, 64)
    app = _clean_field(approver, 256)
    qh = _clean_field(quote_hash, 256)
    nt = None if note is None or isinstance(note, bool) else _clean_field(note, 4096)
    ts = _now().isoformat()
    with _lock:
        conn().execute(
            "INSERT INTO gw_approvals (order_id, decision, approver, note, quote_hash, ts) VALUES (?,?,?,?,?,?)",
            (oid, dec, app, nt, qh, ts),
        )
    return {"order_id": oid, "decision": dec, "approver": app, "note": nt,
            "quote_hash": qh, "ts": ts}


def approvals_for(order_id: str) -> list[dict[str, Any]]:
    if not _valid_id(order_id):
        return []
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
    offer_map = offers if isinstance(offers, dict) else {}
    for ql in quote.lines:
        o = offer_map.get(ql.product_id)
        if o is None:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="product not returned by availability re-check"))
            continue
        blocking = [i for i in (o.issues or []) if i in ("cutoff_passed", "sales_block", "out_of_stock",
                                                         "late_delivery", "availability_not_found", "no_schedule")]
        if blocking:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: " + ", ".join(blocking)))
            continue
        req_raw = ql.qty_sales_units
        if isinstance(req_raw, bool) or not isinstance(req_raw, (int, float)) or not math.isfinite(float(req_raw)) or float(req_raw) <= 0:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: availability_error"))
            continue
        req_qty = float(req_raw)
        avail = o.available_amount
        if avail is None:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="AP", qty_confirmed=req_qty,
                                           note="confirmed"))
            continue
        if isinstance(avail, bool) or not isinstance(avail, (int, float)):
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: availability_error"))
            continue
        av = float(avail)
        if math.isnan(av) or av < 0:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: availability_error"))
        elif not math.isfinite(av):
            lines.append(OrderResponseLine(product_id=ql.product_id, code="AP", qty_confirmed=req_qty,
                                           note="confirmed"))
        elif av == 0:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="RE", qty_confirmed=0,
                                           note="not deliverable at placement: no stock"))
        elif av < req_qty:
            lines.append(OrderResponseLine(
                product_id=ql.product_id, code="CA", qty_confirmed=av,
                note=f"partial stock at placement: {av:g} of {req_qty:g} {ql.sales_unit} confirmed"))
        else:
            lines.append(OrderResponseLine(product_id=ql.product_id, code="AP", qty_confirmed=req_qty,
                                           note="confirmed"))
    codes = {ln.code for ln in lines}
    header = "AP" if codes == {"AP"} else "RE" if codes == {"RE"} or not codes else "CA"
    return header, lines


# ------------------------------------------------------------------ minimal audit fallback (until aimo.trust lands)
def fallback_audit_append(event: dict[str, Any]) -> dict[str, Any]:
    """Hash-chained, UNSIGNED audit event. Used only when aimo.trust.audit is unavailable."""
    import hashlib

    raw_event = event if isinstance(event, dict) else {}
    cleaned = _json_clean(
        {
            k: v
            for k, v in raw_event.items()
            if _strip_bad_chars(str(k)) not in ("seq", "ts", "prev_hash", "hash", "signature", "kid")
        }
    )
    with _lock:
        c = conn()
        cur = c.execute("SELECT seq, body FROM gw_audit_fallback ORDER BY seq DESC")
        try:
            seq = 1
            prev = "0" * 64
            first = True
            for r in cur:
                if first:
                    seq = (int(r[0]) + 1) if isinstance(r[0], int) and not isinstance(r[0], bool) else 1
                    first = False
                parsed = _parse_fallback_event(r[1])
                if parsed is not None:
                    h = parsed.get("hash")
                    if isinstance(h, str) and h:
                        prev = _strip_bad_chars(h)
                        break
        finally:
            cur.close()
        ev = {"seq": seq, "ts": _now().isoformat(), **cleaned, "prev_hash": prev, "signature": "", "kid": "unsigned-fallback"}
        canon = json.dumps({k: v for k, v in ev.items() if k not in ("hash", "signature", "kid")},
                           sort_keys=True, separators=(",", ":"), default=str)
        ev["hash"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        c.execute("INSERT INTO gw_audit_fallback VALUES (?,?)", (seq, json.dumps(ev, default=str)))
    return ev


def fallback_audit_events(subject_id: str | None = None) -> list[dict[str, Any]]:
    if subject_id is not None and not _valid_filter_str(subject_id):
        return []
    with _lock:
        rows = conn().execute("SELECT body FROM gw_audit_fallback ORDER BY seq").fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        parsed = _parse_fallback_event(r[0])
        if parsed is None:
            continue
        if subject_id is None or parsed.get("subject_id") == subject_id:
            out.append(parsed)
    return out
