"""Customer identity and agent-key verification for gateway HTTP and MCP entrypoints."""
from __future__ import annotations

import hmac
import json
import os
from typing import Optional

from aimo.gateway import service


def known_customer_ids() -> set[str]:
    ids: set[str] = set()
    try:
        customers = service.list_customers()
    except Exception:
        customers = service.FALLBACK_CUSTOMERS
    for c in customers:
        if isinstance(c, dict) and c.get("customer_id"):
            s = str(c["customer_id"]).strip()
            if s:
                ids.add(s)
    t = service._trust()
    if t is not None and hasattr(t, "policy_ids"):
        try:
            for x in t.policy_ids():
                s = str(x).strip()
                if s:
                    ids.add(s)
        except Exception:
            pass
    return ids


MAX_CUSTOMER_ID_LEN = 128


def _clean_customer_id(customer_id: Optional[str]) -> str:
    if customer_id is None:
        return ""
    return str(customer_id).strip(" \t\r\n")


def _is_unsafe_customer_id(cid: str) -> bool:
    return (
        len(cid) > MAX_CUSTOMER_ID_LEN
        or ".." in cid
        or "/" in cid
        or "\\" in cid
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in cid)
    )


def _safe_customer_preview(cid: str) -> str:
    cleaned = "".join(ch if ord(ch) >= 32 and ord(ch) != 127 else "?" for ch in cid)
    return cleaned[:80]


def is_known_customer(customer_id: Optional[str]) -> bool:
    cid = _clean_customer_id(customer_id)
    if not cid or _is_unsafe_customer_id(cid):
        return False
    return cid in known_customer_ids()


def require_known_customer(
    customer_id: Optional[str],
    *,
    missing_message: str = "A known customer is required (send X-Customer-Id).",
) -> str:
    cid = _clean_customer_id(customer_id)
    if not cid:
        raise service.ServiceError(401, "identity_required", missing_message)
    if _is_unsafe_customer_id(cid):
        preview = _safe_customer_preview(cid)
        raise service.ServiceError(
            403,
            "unknown_customer",
            f"unknown customer {preview!r}; call GET /v1/customers for valid customer ids",
            {"customer_id": preview},
        )
    known = known_customer_ids()
    if cid not in known:
        preview = _safe_customer_preview(cid)
        raise service.ServiceError(
            403,
            "unknown_customer",
            f"unknown customer {preview!r}; call GET /v1/customers for valid customer ids",
            {"customer_id": preview, "known_customers": sorted(known)},
        )
    return cid


def _configured_agent_keys() -> dict[str, str]:
    raw = os.environ.get("AIMO_AGENT_KEYS", "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in parsed.items()
                if str(k).strip() and v is not None and str(v).strip()
            }
    out: dict[str, str] = {}
    for entry in raw.split(","):
        part = entry.strip()
        if ":" in part and "=" in part:
            sep = ":" if part.index(":") < part.index("=") else "="
        elif "=" in part:
            sep = "="
        elif ":" in part:
            sep = ":"
        else:
            continue
        k, v = part.split(sep, 1)
        ks, vs = k.strip(), v.strip()
        if ks and vs:
            out[ks] = vs
    return out


def check_agent_key(customer_id: str, x_agent_key: Optional[str] = None) -> None:
    keys = _configured_agent_keys()
    if not keys:
        return
    cid = (customer_id or "").strip()
    expected = keys.get(cid) or keys.get("*")
    if not expected:
        return
    provided = (x_agent_key or "").strip()
    if not provided:
        raise service.ServiceError(
            401,
            "agent_key_required",
            f"X-Agent-Key header is required for customer {cid!r}.",
        )
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise service.ServiceError(
            403,
            "invalid_agent_key",
            f"Invalid X-Agent-Key for customer {cid!r}.",
        )


def resolve_customer_identity(
    *,
    x_customer_id: Optional[str] = None,
    body_customer_id: Optional[str] = None,
    x_agent_id: Optional[str] = None,
    body_agent_id: Optional[str] = None,
    x_agent_key: Optional[str] = None,
    require_headers: bool = False,
    defer_unknown_customer: bool = False,
) -> service.Identity:
    hc = _clean_customer_id(x_customer_id)
    bc = _clean_customer_id(body_customer_id)
    if require_headers and (not (x_agent_id and x_agent_id.strip()) or not hc):
        raise service.ServiceError(
            401,
            "identity_required",
            "Quotes and orders need X-Customer-Id and X-Agent-Id (production: OAuth 2.1 bearer).",
        )
    if not hc and not bc:
        raise service.ServiceError(
            401,
            "identity_required",
            "A known customer is required (send X-Customer-Id).",
        )
    if hc and _is_unsafe_customer_id(hc):
        require_known_customer(hc)
    if bc and _is_unsafe_customer_id(bc):
        if hc and not defer_unknown_customer and not is_known_customer(hc):
            require_known_customer(hc)
        require_known_customer(bc)
    cid = hc or bc
    if hc and bc and hc != bc:
        if not defer_unknown_customer and not is_known_customer(hc):
            require_known_customer(hc)
        raise service.ServiceError(
            403,
            "customer_mismatch",
            f"request names customer {body_customer_id!r} but the caller is authenticated as {hc!r}",
        )
    if not defer_unknown_customer:
        require_known_customer(cid)
    check_agent_key(cid, x_agent_key)
    ha = (x_agent_id or "").strip()
    ba = (body_agent_id or "").strip()
    aid = ha or ba or service.DEFAULT_AGENT
    ident = service.Identity(agent_id=aid, customer_id=cid)
    if ha and ba:
        service.claim_identity(ident, None, body_agent_id)
    setattr(ident, "authenticated", {"customer": bool(hc), "agent": bool(ha)})
    return ident
