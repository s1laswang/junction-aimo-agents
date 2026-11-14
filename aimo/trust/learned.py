"""Learned approvals: swaps a human approved with "remember" (the UI's "always allow this swap").

Only the human decision route writes here (gateway `decide(..., remember=True)` after the approver's
PIN/session check); agents have no write path. The policy engine reads it: a remembered swap is allowed
by the `substitution` rule without asking again. Allergen, claim, spend, split-order, price-jump and
injection rules still apply to it. Nothing here can unlock an allergen.

scope "product":      exactly this product (as a substitute for `from_product_id`, or for anything when
                      the approval had no usual product).
scope "product_type": any product of the same catalog category and product type that keeps every claim
                      the approved product had (e.g. another lactose-free 1 L milk pack).

Stored in a `learned_swaps` table in config.STATE_DB (resolved per call, so tests can point it elsewhere).
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Optional

_lock = threading.Lock()
SCOPES = ("product", "product_type")


def _db(create: bool = True) -> sqlite3.Connection:
    from aimo import config

    db_path = Path(config.STATE_DB)
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=10)
    con.row_factory = sqlite3.Row
    if create:
        con.execute(
            """CREATE TABLE IF NOT EXISTS learned_swaps (
        customer_id TEXT NOT NULL, from_product_id TEXT NOT NULL DEFAULT '', to_product_id TEXT NOT NULL,
        scope TEXT NOT NULL, kind TEXT, approver TEXT, approver_role TEXT, quote_id TEXT, order_id TEXT,
        note TEXT, created_at TEXT NOT NULL, revoked_at TEXT,
        PRIMARY KEY (customer_id, from_product_id, to_product_id, scope))"""
        )
    return con


def _kind(product_id: str) -> dict[str, Any]:
    """Catalog facts a product_type swap is matched on (empty when the catalog is not available)."""
    try:
        from aimo.catalog.substitution import product_types
        from aimo.catalog.textsearch import get_rows

        d = get_rows([product_id]).get(product_id) or {}
    except Exception:
        return {}
    if not d:
        return {}
    return {
        "category": d.get("category"),
        "types": sorted(product_types(d)),
        "claims": sorted(d.get("claims") or []),
        "content_unit": d.get("content_unit"),
        "name": d.get("name_en") or d.get("name"),
    }


def remember_swap(
    customer_id: str,
    from_product_id: Optional[str],
    to_product_id: str,
    scope: str = "product",
    approver: Optional[str] = None,
    approver_role: Optional[str] = None,
    quote_id: Optional[str] = None,
    order_id: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if not customer_id or not to_product_id:
        raise ValueError("customer_id and to_product_id are required")
    row = {
        "customer_id": customer_id,
        "from_product_id": from_product_id or "",
        "to_product_id": str(to_product_id),
        "scope": scope,
        "kind": json.dumps(_kind(str(to_product_id))),
        "approver": approver,
        "approver_role": approver_role,
        "quote_id": quote_id,
        "order_id": order_id,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revoked_at": None,
    }
    with _lock, closing(_db(create=True)) as con, con:
        con.execute(
            "INSERT OR REPLACE INTO learned_swaps VALUES (:customer_id, :from_product_id, :to_product_id, "
            ":scope, :kind, :approver, :approver_role, :quote_id, :order_id, :note, :created_at, :revoked_at)",
            row,
        )
    return _out(row)


def forget_swap(
    customer_id: str,
    to_product_id: str,
    from_product_id: Optional[str] = None,
    scope: Optional[str] = None,
) -> int:
    """Revoke (kept for the record, no longer matched). Returns the number of swaps revoked."""
    from aimo import config

    if not Path(config.STATE_DB).exists():
        return 0
    q = "UPDATE learned_swaps SET revoked_at = ? WHERE customer_id = ? AND to_product_id = ? AND revoked_at IS NULL"
    args: list[Any] = [datetime.now(timezone.utc).isoformat(timespec="seconds"), customer_id, str(to_product_id)]
    if from_product_id is not None:
        q += " AND from_product_id = ?"
        args.append(from_product_id)
    if scope is not None:
        q += " AND scope = ?"
        args.append(scope)
    try:
        with _lock, closing(_db(create=False)) as con, con:
            return con.execute(q, args).rowcount
    except sqlite3.Error:
        return 0


def _out(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["from_product_id"] = d.get("from_product_id") or None
    try:
        d["kind"] = json.loads(d.get("kind") or "{}")
    except (TypeError, ValueError):
        d["kind"] = {}
    return d


def list_learned(customer_id: str, include_revoked: bool = False) -> list[dict[str, Any]]:
    from aimo import config

    if not Path(config.STATE_DB).exists():
        return []
    try:
        with closing(_db(create=False)) as con:
            rows = con.execute(
                "SELECT * FROM learned_swaps WHERE customer_id = ?"
                + ("" if include_revoked else " AND revoked_at IS NULL")
                + " ORDER BY created_at",
                (customer_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_out(r) for r in rows]


def match(
    customer_id: str,
    product_id: str,
    reference_id: Optional[str] = None,
    product: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """The remembered swap that covers `product_id` (as a substitute for `reference_id`), or None.
    `product` = catalog facts of product_id ({category, types, claims, content_unit}) for product_type swaps."""
    pid = str(product_id)
    for s in list_learned(customer_id):
        if s["from_product_id"] and reference_id and s["from_product_id"] != reference_id:
            continue
        if s["to_product_id"] == pid:
            return s
        if s["scope"] == "product_type" and product:
            k = s["kind"] or {}
            if (
                k.get("category")
                and k.get("category") == product.get("category")
                and sorted(k.get("types") or []) == sorted(product.get("types") or [])
                and k.get("content_unit") == product.get("content_unit")
                and set(k.get("claims") or []) <= set(product.get("claims") or [])
            ):
                return s
    return None
