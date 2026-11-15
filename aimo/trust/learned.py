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
MAX_ID_LEN = 128


def _require_id(val: Any, field: str) -> str:
    if isinstance(val, bool) or not isinstance(val, str):
        raise ValueError(f"{field} must be a string")
    if "\x00" in val:
        raise ValueError(f"{field} must not contain NUL bytes")
    if len(val) > MAX_ID_LEN:
        raise ValueError(f"{field} exceeds max length {MAX_ID_LEN}")
    cleaned = val.strip()
    if not cleaned:
        raise ValueError(f"{field} must be a non-empty string")
    return cleaned


def _clean_id(val: Any) -> Optional[str]:
    if isinstance(val, bool) or not isinstance(val, str):
        return None
    if "\x00" in val or len(val) > MAX_ID_LEN:
        return None
    cleaned = val.strip()
    if not cleaned:
        return None
    return cleaned


def _opt_str(val: Any, field: str, max_len: int = 1024) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, str):
        raise ValueError(f"{field} must be a string or None")
    if "\x00" in val:
        raise ValueError(f"{field} must not contain NUL bytes")
    if len(val) > max_len:
        raise ValueError(f"{field} exceeds max length {max_len}")
    return val


def _str_seq(val: Any) -> Optional[list[str]]:
    if val is None:
        return []
    if not isinstance(val, (list, tuple, set, frozenset)):
        return None
    out: list[str] = []
    for item in val:
        if isinstance(item, bool) or not isinstance(item, str):
            return None
        out.append(item)
    return out


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

        rows = get_rows([product_id])
        d = rows.get(product_id) if isinstance(rows, dict) else None
    except Exception:
        return {}
    if not isinstance(d, dict) or not d:
        return {}
    try:
        raw_types = product_types(d)
        types_seq = _str_seq(raw_types)
        claims_seq = _str_seq(d.get("claims"))
        return {
            "category": d.get("category"),
            "types": sorted(types_seq or []),
            "claims": sorted(claims_seq or []),
            "content_unit": d.get("content_unit"),
            "name": d.get("name_en") or d.get("name"),
        }
    except Exception:
        return {}


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
    if isinstance(scope, bool) or not isinstance(scope, str) or scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    cid = _require_id(customer_id, "customer_id")
    tid = _require_id(to_product_id, "to_product_id")
    fid = "" if from_product_id is None else _require_id(from_product_id, "from_product_id")
    row = {
        "customer_id": cid,
        "from_product_id": fid,
        "to_product_id": tid,
        "scope": scope,
        "kind": json.dumps(_kind(tid)),
        "approver": _opt_str(approver, "approver", max_len=256),
        "approver_role": _opt_str(approver_role, "approver_role", max_len=128),
        "quote_id": _opt_str(quote_id, "quote_id", max_len=128),
        "order_id": _opt_str(order_id, "order_id", max_len=128),
        "note": _opt_str(note, "note", max_len=1024),
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
    cid = _clean_id(customer_id)
    tid = _clean_id(to_product_id)
    if cid is None or tid is None:
        return 0
    q = "UPDATE learned_swaps SET revoked_at = ? WHERE customer_id = ? AND to_product_id = ? AND revoked_at IS NULL"
    args: list[Any] = [datetime.now(timezone.utc).isoformat(timespec="seconds"), cid, tid]
    if from_product_id is not None:
        fid = _clean_id(from_product_id)
        if fid is None:
            return 0
        q += " AND from_product_id = ?"
        args.append(fid)
    if scope is not None:
        if isinstance(scope, bool) or not isinstance(scope, str) or scope not in SCOPES:
            return 0
        q += " AND scope = ?"
        args.append(scope)
    from aimo import config

    with _lock:
        if not Path(config.STATE_DB).exists():
            return 0
        try:
            with closing(_db(create=False)) as con, con:
                return con.execute(q, args).rowcount
        except sqlite3.Error:
            return 0


def _out(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["from_product_id"] = d.get("from_product_id") or None
    raw_kind = d.get("kind")
    if isinstance(raw_kind, dict):
        parsed = raw_kind
    elif isinstance(raw_kind, str) and raw_kind:
        try:
            parsed = json.loads(raw_kind)
        except (TypeError, ValueError):
            parsed = {}
    else:
        parsed = {}
    d["kind"] = parsed if isinstance(parsed, dict) else {}
    return d


def list_learned(customer_id: str, include_revoked: bool = False) -> list[dict[str, Any]]:
    cid = _clean_id(customer_id)
    if cid is None:
        return []
    from aimo import config

    with _lock:
        if not Path(config.STATE_DB).exists():
            return []
        try:
            with closing(_db(create=False)) as con:
                rows = con.execute(
                    "SELECT * FROM learned_swaps WHERE customer_id = ?"
                    + ("" if bool(include_revoked) else " AND revoked_at IS NULL")
                    + " ORDER BY created_at",
                    (cid,),
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
    cid = _clean_id(customer_id)
    pid = _clean_id(product_id)
    if cid is None or pid is None:
        return None
    ref_id: Optional[str] = None
    if reference_id is not None:
        ref_id = _clean_id(reference_id)
        if ref_id is None:
            return None
    for s in list_learned(cid):
        if s["from_product_id"] and ref_id and s["from_product_id"] != ref_id:
            continue
        if s["to_product_id"] == pid:
            return s
        if s["scope"] == "product_type" and isinstance(product, dict):
            k = s.get("kind")
            if not isinstance(k, dict):
                continue
            k_cat = k.get("category")
            if not isinstance(k_cat, str) or not k_cat or k_cat != product.get("category"):
                continue
            k_types = _str_seq(k.get("types"))
            p_types = _str_seq(product.get("types"))
            k_claims = _str_seq(k.get("claims"))
            p_claims = _str_seq(product.get("claims"))
            if k_types is None or p_types is None or k_claims is None or p_claims is None:
                continue
            if (
                sorted(k_types) == sorted(p_types)
                and k.get("content_unit") == product.get("content_unit")
                and set(k_claims) <= set(p_claims)
            ):
                return s
    return None
