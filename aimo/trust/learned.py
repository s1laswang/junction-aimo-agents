"""Learned approvals: when a human approves a substitution and ticks "remember", the swap is stored
(table learned_swaps in aimo.config.STATE_DB) and audited (policy.learned_swap_added). Later quotes with
the same swap are allowed by the substitution rule with "Allowed: you approved this swap on <date>
(<approver>)". This is how the system gets less chatty without loosening the written policy.

Only a person in the customer's approver directory can teach a swap (never an AI agent identity).
Scopes:
  "product"  from one usual product id to one substitute id (default)
  "type"     from any usual product of a kind (category code or product_type) to the substitute
Learned swaps expire after `max_age_days` (default 90) and can be revoked with forget_swap().
They never override allergen, claim, delivery, spend or injection rules: only the substitution rule.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_swaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id TEXT NOT NULL,
  from_key TEXT NOT NULL,
  to_product_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  approver TEXT NOT NULL,
  approver_role TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS learned_swaps_cust ON learned_swaps(customer_id, to_product_id);
"""
DEFAULT_MAX_AGE_DAYS = 90


def _db(db_path: str | Path | None = None) -> sqlite3.Connection:
    from aimo import config
    path = Path(db_path or config.STATE_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def remember_swap(customer_id: str, from_product_or_type: str, to_product_id: str, approver: str,
                  scope: str = "product", *, note: str = "", max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                  audit_log: Any = None, db_path: str | Path | None = None) -> dict:
    """Store a human-approved swap. Raises PermissionError if `approver` is not a directory person of this
    customer (agents can never teach swaps). Returns the stored row."""
    from aimo.trust.approvals import _approver_role_in_directory
    from aimo.trust.policy import load_policy, role_text
    if scope not in ("product", "type"):
        raise ValueError("scope must be 'product' or 'type'")
    who = (approver or "").strip()
    if who.lower().startswith("human:"):
        who = who[6:]
    pol = load_policy(customer_id)
    role = _approver_role_in_directory(pol, who) if who else None
    agent_ids = {a["agent_id"].lower() for a in pol.get("agents", [])} | \
        {a["agent_id"].split("/")[-1].lower() for a in pol.get("agents", [])}
    if not who or who.lower().startswith("agent:") or who.lower() in agent_ids or role is None:
        raise PermissionError(f"{approver!r} is not in {customer_id}'s approver directory; only a named person can "
                              f"teach the system a substitution")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=max_age_days) if max_age_days else None
    with _lock:
        con = _db(db_path)
        try:
            cur = con.execute("INSERT INTO learned_swaps (customer_id, from_key, to_product_id, scope, approver, "
                              "approver_role, note, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (customer_id, str(from_product_or_type), str(to_product_id), scope, who, role, note,
                               now.isoformat(), exp.isoformat() if exp else None))
            row = _row(con.execute("SELECT * FROM learned_swaps WHERE id = ?", (cur.lastrowid,)).fetchone())
        finally:
            con.close()
    from aimo.trust.audit import audit
    what = f"any product of kind {from_product_or_type}" if scope == "type" else f"product {from_product_or_type}"
    until = f"until {exp:%d %b %Y}" if exp else "until revoked"
    (audit_log or audit).append(
        f"human:{who}", "policy.learned_swap_added", f"swap:{customer_id}:{row['id']}",
        f"{who} ({role_text(role)}) taught the policy for {customer_id}: {to_product_id} may replace {what} "
        f"without asking again {until}.",
        {"swap": row, "customer_id": customer_id})
    return row


def forget_swap(swap_id: int, approver: str, *, audit_log: Any = None, db_path: str | Path | None = None) -> bool:
    with _lock:
        con = _db(db_path)
        try:
            r = con.execute("SELECT * FROM learned_swaps WHERE id = ? AND revoked_at IS NULL", (swap_id,)).fetchone()
            if r is None:
                return False
            con.execute("UPDATE learned_swaps SET revoked_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), swap_id))
        finally:
            con.close()
    from aimo.trust.audit import audit
    (audit_log or audit).append(f"human:{approver}", "policy.learned_swap_revoked", f"swap:{r['customer_id']}:{swap_id}",
                                f"{approver} revoked learned swap #{swap_id} ({r['to_product_id']}).", {"swap_id": swap_id})
    return True


def list_learned(customer_id: str, *, include_inactive: bool = False, db_path: str | Path | None = None) -> list[dict]:
    con = _db(db_path)
    try:
        rows = [_row(r) for r in con.execute("SELECT * FROM learned_swaps WHERE customer_id = ? ORDER BY id",
                                             (customer_id,)).fetchall()]
    finally:
        con.close()
    if include_inactive:
        return rows
    now = datetime.now(timezone.utc).isoformat()
    return [r for r in rows if not r["revoked_at"] and (not r["expires_at"] or r["expires_at"] > now)]


def is_learned(customer_id: str, to_product_id: str, from_keys: list[str], *, now: Optional[datetime] = None,
               db_path: str | Path | None = None) -> Optional[dict]:
    """The active learned swap that lets `to_product_id` replace one of `from_keys` (usual product ids and
    their kinds), or None."""
    try:
        con = _db(db_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        rows = con.execute("SELECT * FROM learned_swaps WHERE customer_id = ? AND to_product_id = ? AND revoked_at "
                           "IS NULL ORDER BY id DESC", (customer_id, str(to_product_id))).fetchall()
    finally:
        con.close()
    t = (now or datetime.now(timezone.utc)).isoformat()
    keys = {str(k) for k in from_keys if k}
    for r in rows:
        if r["expires_at"] and r["expires_at"] <= t:
            continue
        if r["from_key"] in keys:
            return _row(r)
    return None
