"""Proactive seller agent (Valio Aimo side). Owner: port.

Ported from Momin's junction26-valio `agents/seller.py`, with credit: his idea of a seller agent that looks at
a customer's usual items and proposes a better offer, his filters (excluded allergens, sales block, enough
stock) and his ranking and "why" wording (saves X EUR, delivery reliability A vs B, CO2 A vs B).

What changed for our contract, and the point for judges: **a seller agent can never place an order on a
customer's behalf.** It can only create a *proposal*:
  - the proposal carries a NON-binding policy preview: the customer's own policy evaluated on a draft of the
    quote the buyer would get (so the seller sees "needs procurement_manager approval" up front);
  - only the customer's own registered agent (or a human acting for it) can accept. Accepting runs OUR normal
    path: signed quote (prices re-fetched live) -> place_order -> the customer's policy decides
    (auto-approve / needs human approval / reject). Anything outside the customer's preferences needs approval;
  - a `valio-aimo/*` agent calling accept, create_quote or place_order for a customer is refused (403): it has
    no mandate in any customer policy;
  - every proposal is audit-logged as `agent:valio-aimo/seller-1`.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from aimo.gateway.service import ServiceError
from aimo.models import Need, Quote, QuoteLine

log = logging.getLogger("aimo.agents.seller")

SELLER_ORG = "valio-aimo"
DEFAULT_SELLER = f"{SELLER_ORG}/seller-1"
MAX_QTY = 1_000_000.0
MAX_PRODUCT_ID_LEN = 64
MAX_PROPOSAL_ID_LEN = 128
MAX_CUSTOMER_ID_LEN = 128

_SCHEMA = """create table if not exists port_proposals (
  proposal_id text primary key, customer_id text not null, status text not null, created_at text not null,
  body text not null)"""

_proposal_locks: dict[str, threading.Lock] = {}
_proposal_locks_guard = threading.Lock()


def _proposal_lock(proposal_id: str) -> threading.Lock:
    with _proposal_locks_guard:
        lk = _proposal_locks.get(proposal_id)
        if lk is None:
            if len(_proposal_locks) >= 4096:
                for k in [k for k, v in _proposal_locks.items() if not v.locked()][:1024]:
                    _proposal_locks.pop(k, None)
            lk = threading.Lock()
            _proposal_locks[proposal_id] = lk
        return lk


class ProposalError(ServiceError):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(status, code, message, detail)


def _has_ctrl(value: str) -> bool:
    return any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value)


def _finite_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        f = float(val)
    except (ValueError, TypeError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _finite_pos(val: Any) -> Optional[float]:
    f = _finite_float(val)
    return f if f is not None and f > 0 else None


def _finite_nonneg(val: Any) -> Optional[float]:
    f = _finite_float(val)
    return f if f is not None and f >= 0 else None


def _catalog_missing_error() -> ProposalError:
    from aimo.catalog import CATALOG_MISSING_MESSAGE

    return ProposalError(
        503,
        "catalog_missing",
        CATALOG_MISSING_MESSAGE,
        {"fix": "uv run aimo fetch-data", "needs": "AIMO_API_KEY"},
    )


def _validate_product_id(raw: Any, field: str = "product_id") -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ProposalError(422, "bad_product_id", f"{field} must be a Valio Aimo product code (1-64 characters)")
    if _has_ctrl(raw):
        raise ProposalError(422, "bad_product_id", f"{field} contains invalid control characters")
    pid = raw.strip()
    if not pid or len(pid) > MAX_PRODUCT_ID_LEN or any(ch.isspace() for ch in pid):
        raise ProposalError(422, "bad_product_id", f"{field} must be a Valio Aimo product code (1-64 characters)")
    return pid


def _validate_qty(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ProposalError(422, "bad_quantity", f"qty_sales_units must be a finite number > 0 (got {raw!r})")
    try:
        q = float(raw)
    except (ValueError, TypeError, OverflowError) as e:
        raise ProposalError(422, "bad_quantity", "qty_sales_units must be a finite number > 0") from e
    if not math.isfinite(q) or q <= 0 or q > MAX_QTY:
        raise ProposalError(
            422,
            "bad_quantity",
            f"qty_sales_units must be > 0 and <= 1,000,000 (got {raw!r})",
        )
    return q


def _parse_delivery_date(raw: Any, customer_id: str) -> date:
    if raw is None:
        from aimo.agents.drift import SEEDS, next_delivery

        return next_delivery(SEEDS.get(customer_id, {}).get("weekday", 3))
    if isinstance(raw, bool):
        raise ProposalError(422, "bad_delivery_date", f"delivery_date must be YYYY-MM-DD (got {raw!r})")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        raise ProposalError(422, "bad_delivery_date", f"delivery_date must be YYYY-MM-DD (got {type(raw).__name__})")
    if _has_ctrl(raw) or len(raw) > 64:
        raise ProposalError(422, "bad_delivery_date", "delivery_date must be YYYY-MM-DD")
    s = raw.strip()
    if not s:
        raise ProposalError(422, "bad_delivery_date", "delivery_date must be YYYY-MM-DD")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s)
        if len(s) > 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]) and s[10] in ("T", "t", " "):
            norm = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
            return datetime.fromisoformat(norm).date()
    except ValueError as e:
        raise ProposalError(422, "bad_delivery_date", f"invalid delivery_date {s[:32]!r}; expected YYYY-MM-DD") from e
    raise ProposalError(422, "bad_delivery_date", f"invalid delivery_date {s[:32]!r}; expected YYYY-MM-DD")


def _validate_proposal_id(raw: Any) -> str:
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ProposalError(422, "bad_proposal_id", "proposal_id must be a non-empty string (1-128 characters)")
    if _has_ctrl(raw):
        raise ProposalError(422, "bad_proposal_id", "proposal_id contains invalid control characters")
    pid = raw.strip()
    if (
        not pid
        or len(pid) > MAX_PROPOSAL_ID_LEN
        or any(ch.isspace() for ch in pid)
        or ".." in pid
        or "/" in pid
        or "\\" in pid
    ):
        raise ProposalError(422, "bad_proposal_id", "proposal_id must be 1-128 characters")
    return pid


def _conn():
    from aimo.backend import orders as store
    with store._lock:
        c = store.conn()
        c.execute(_SCHEMA)
        return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seller_id(agent_id: Optional[str]) -> str:
    """Proposals are always made in Valio Aimo's name: `valio-aimo/<agent>`; anything else -> seller-1."""
    a = agent_id.strip() if isinstance(agent_id, str) else ""
    if not a or len(a) > MAX_CUSTOMER_ID_LEN or _has_ctrl(a) or any(ch.isspace() for ch in a):
        return DEFAULT_SELLER
    return a if a.startswith(SELLER_ORG + "/") and len(a) > len(SELLER_ORG) + 1 else DEFAULT_SELLER


def is_seller(agent_id: Optional[str]) -> bool:
    a = agent_id.strip() if isinstance(agent_id, str) else ""
    return a.startswith(SELLER_ORG + "/")


def usual_items(customer_id: str) -> list[dict[str, Any]]:
    """The customer's usual items: trust policy preferred_products + the standing order (drift seeds)."""
    if isinstance(customer_id, bool) or not isinstance(customer_id, str) or not customer_id.strip():
        return []
    items: dict[str, dict[str, Any]] = {}
    try:
        from aimo.gateway.verify import usual_products
        for pid in usual_products(customer_id):
            if isinstance(pid, str) and pid.strip():
                items[pid] = {"product_id": pid, "weekly_qty": None}
    except Exception:
        pass
    from aimo.agents.drift import SEEDS
    for l in SEEDS.get(customer_id, {}).get("lines", []):
        weeks = l.get("weeks") or []
        wq = _finite_pos(weeks[-1]) if weeks else None
        items[l["product_id"]] = {"product_id": l["product_id"], "weekly_qty": wq}
    return list(items.values())


def _excluded(customer_id: str) -> list[str]:
    try:
        from aimo.gateway.verify import policy_excluded_allergens
        return list(policy_excluded_allergens(customer_id))
    except Exception:
        return []


def _per_unit(offer: Any, content: Optional[float]) -> Optional[float]:
    """EUR per L / KG: the API's comparison price, else unit price / content."""
    if offer is None:
        return None
    comp = _finite_pos(getattr(offer, "comparison_price", None))
    if comp is not None:
        return comp
    up = _finite_pos(getattr(offer, "unit_price", None))
    cnt = _finite_pos(content)
    if up is None or cnt is None:
        return None
    res = up / cnt
    return res if math.isfinite(res) and res > 0 else None


def find_cheaper(customer_id: str, delivery: date, offers_fn: Optional[Callable] = None,
                 only: Optional[str] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Momin's `explain`/`rank` on our catalog: for each usual item, allergen-safe, orderable, same-spec
    candidates (our good/better/best ranking) that beat the usual item on price per L/KG."""
    from aimo.catalog import get_product
    from aimo.catalog.plan import find_candidates
    from aimo.catalog.substitution import rank_substitutes

    if isinstance(customer_id, bool) or not isinstance(customer_id, str) or not customer_id.strip() or _has_ctrl(customer_id):
        raise ProposalError(422, "bad_customer_id", "customer_id must be a non-empty string")
    cid = customer_id.strip()
    if len(cid) > MAX_CUSTOMER_ID_LEN:
        raise ProposalError(422, "bad_customer_id", "customer_id exceeds maximum length")
    clean_only = _validate_product_id(only, "replaces_product_id")
    delivery = _parse_delivery_date(delivery, cid)

    excluded = _excluded(cid)
    needs: list[tuple[dict[str, Any], Any, Need, dict, dict]] = []
    want: list[str] = []
    try:
        for it in usual_items(cid):
            if clean_only and it["product_id"] != clean_only:
                continue
            p = get_product(it["product_id"])
            p_amt = _finite_pos(getattr(p, "content_amount", None)) if p is not None else None
            if p is None or p.content_unit not in ("L", "KG", "PCS") or p_amt is None:
                continue
            wq = _finite_pos(it.get("weekly_qty"))
            units_in = wq if wq is not None else 1.0
            need_qty = units_in * p_amt
            if not math.isfinite(need_qty) or need_qty <= 0 or need_qty > MAX_QTY:
                continue
            need = Need(customer_id=cid, product_query=p.name, quantity=need_qty,
                        unit=p.content_unit, delivery_by=delivery, preferred_product_ids=[p.id],
                        excluded_allergens=excluded, required_claims=[c for c in p.claims if c.startswith("FREE_FROM_")])
            rows, rel = find_candidates(need)
            top = [pid for pid, _ in sorted(rel.items(), key=lambda kv: -kv[1])[:25]]
            if p.id not in top:
                top.append(p.id)
            want += top
            needs.append((it, p, need, {k: rows[k] for k in top if k in rows}, rel))
    except FileNotFoundError as e:
        raise _catalog_missing_error() from e
    if not want:
        return [], ["no usual items on file for this customer"]
    ids = list(dict.fromkeys(want))
    if offers_fn is not None:
        offers = offers_fn(ids, delivery, customer_id=cid)
    else:
        from aimo.backend.client import get_offers_with_meta
        offers, _ = get_offers_with_meta(ids, delivery, cid)
    if not isinstance(offers, dict):
        offers = {}
    props, notes = [], []
    for it, p, need, rows, rel in needs:
        p_amt = _finite_pos(getattr(p, "content_amount", None))
        n_qty = _finite_pos(need.quantity)
        if p_amt is None or n_qty is None:
            continue
        base = offers.get(p.id)
        base_pu = _per_unit(base, p_amt)
        try:
            cands = rank_substitutes(need, {k: v for k, v in offers.items() if k in rows}, limit=40, rows=rows,
                                     relevance=rel)
        except FileNotFoundError as e:
            raise _catalog_missing_error() from e
        best = None
        for c in cands:
            if (
                c.product.id == p.id
                or c.tier not in ("best", "better")
                or c.offer is None
                or c.offer.available is not True
                or _finite_pos(getattr(c.offer, "unit_price", None)) is None
            ):
                continue
            c_amt = _finite_pos(getattr(c.product, "content_amount", None))
            if c.product.content_unit != p.content_unit or c_amt is None:
                continue
            raw_units = -(-n_qty // c_amt)
            if not math.isfinite(raw_units) or raw_units <= 0 or raw_units > MAX_QTY:
                continue
            units = float(raw_units)
            if c.offer.available_amount is not None:
                raw_av = c.offer.available_amount
                if isinstance(raw_av, bool) or not isinstance(raw_av, (int, float)):
                    continue
                av = float(raw_av)
                if math.isnan(av) or av < 0 or (math.isfinite(av) and av < units):
                    continue
            pu = _per_unit(c.offer, c_amt)
            if pu is None:
                continue
            reasons, score = [], 0.0
            if base is None or base.available is not True:
                reasons.append(f"your usual {p.name} is not orderable for {delivery:%a %d %b}"
                               + (f" ({', '.join(base.issues)})" if base is not None and base.issues else ""))
                score += 3.0
            save_val: Optional[float] = None
            if base_pu is not None and pu < base_pu:
                save_cand = (base_pu - pu) * n_qty
                rel_gain = (base_pu - pu) / base_pu
                if math.isfinite(save_cand) and math.isfinite(rel_gain):
                    save_val = save_cand
                    reasons.append(f"€{pu:.2f} vs €{base_pu:.2f} per {p.content_unit}, saves about €{save_cand:.2f} a week "
                                   f"on {n_qty:g} {p.content_unit}")
                    score += rel_gain
            br = _finite_nonneg(getattr(p, "delivery_reliability", None))
            cr = _finite_nonneg(getattr(c.product, "delivery_reliability", None))
            if br is not None and cr is not None and cr > br:
                reasons.append(f"delivery reliability {cr:.2f} vs {br:.2f}")
                score += cr - br
            p_co2 = _finite_pos(getattr(p, "co2_kg_per_kg", None))
            c_co2 = _finite_nonneg(getattr(c.product, "co2_kg_per_kg", None))
            if p_co2 is not None and c_co2 is not None and c_co2 < p_co2:
                reasons.append(f"CO2 {c_co2:.2f} vs {p_co2:.2f} kg CO2e/kg")
                score += 0.1
            if not math.isfinite(score) or not reasons or (base_pu is not None and pu >= base_pu and score < 3):
                continue
            if best is None or score > best[0]:
                best = (score, c, units, reasons, save_val)
        if best is None:
            notes.append(f"your {p.name} is already the best offer we found for {delivery:%a %d %b}")
            continue
        score, c, units, reasons, save = best
        props.append({"score": round(score, 4), "replaces": {"product_id": p.id, "name": p.name},
                      "product": c.product, "offer": c.offer, "tier": c.tier, "qty_sales_units": float(units),
                      "why": [f"{c.product.name}: " + "; ".join(reasons), *c.reasons[:1]],
                      "estimated_saving_eur": round(save, 2) if save is not None and save > 0 else None})
    props.sort(key=lambda x: -x["score"])
    return props, notes


def policy_preview(customer_id: str, delivery: date, lines: list[dict[str, Any]], offers: dict[str, Any]) -> dict:
    """The customer's own policy on a DRAFT of the quote the buyer would get (unsigned, not stored, not binding).
    Same verification (server-side tiers/flags) and same trust.evaluate as POST /v1/quotes + /v1/orders."""
    from aimo.gateway import service
    from aimo.gateway.verify import verify_lines

    qty_by: dict[str, float] = {}
    for l in lines:
        pid = _validate_product_id(l.get("product_id"))
        q = _validate_qty(l.get("qty_sales_units"))
        if pid is None or q is None:
            raise ProposalError(422, "bad_quantity", "proposal line requires valid product_id and qty_sales_units")
        qty_by[pid] = q
    products = {}
    for pid in qty_by:
        try:
            products[pid] = service.get_product(pid)
        except Exception:
            pass
    verified = verify_lines(customer_id, delivery, qty_by, offers, None, products, service.screen_supplier_text)
    qlines = []
    for pid, q in qty_by.items():
        o = offers[pid]
        up = _finite_pos(getattr(o, "unit_price", None))
        if up is None:
            raise ProposalError(409, "not_orderable", f"{pid} has no valid unit price")
        qlines.append(QuoteLine(product_id=pid, product_name=products[pid].name if pid in products else pid,
                                qty_sales_units=q, sales_unit=o.sales_unit or "ST", unit_price=up,
                                line_total=round(q * up, 2), tier=verified[pid]["tier"],
                                flags=verified[pid]["flags"]))
    now = _now().replace(microsecond=0)
    draft = Quote(quote_id="DRAFT-" + uuid.uuid4().hex[:8], customer_id=customer_id, agent_id="buyer-1",
                  delivery_date=delivery, lines=qlines, total_net=round(sum(l.line_total for l in qlines), 2),
                  created_at=now, expires_at=now + timedelta(minutes=15))
    mandate = service.mandate_for(service.Identity(agent_id="buyer-1", customer_id=customer_id))
    d = service.evaluate(draft, mandate, products=products or None)
    return {"binding": False, "outcome": d.outcome, "approver_role": d.approver_role,
            "total_net": draft.total_net, "policy_id": d.policy_id, "policy_version": d.policy_version,
            "tiers": {pid: v["tier"] for pid, v in verified.items()},
            "hits": [h.model_dump() for h in d.hits if h.outcome != "allow"],
            "note": "Preview only: the buyer's own quote and order are evaluated again, live, when it accepts."}


def _save(p: dict[str, Any]) -> None:
    from aimo.backend import orders as store
    payload = json.dumps(p, default=str)
    with store._lock:
        _conn().execute("insert or replace into port_proposals values (?,?,?,?,?)",
                        (p["proposal_id"], p["customer_id"], p["status"], p["created_at"], payload))


def get_proposal(proposal_id: str) -> dict[str, Any]:
    from aimo.backend import orders as store
    pid = _validate_proposal_id(proposal_id)
    with store._lock:
        r = _conn().execute("select body from port_proposals where proposal_id=?", (pid,)).fetchone()
    if r is None:
        raise ProposalError(404, "proposal_not_found", f"proposal {pid!r} not found")
    try:
        data = json.loads(r[0])
    except (TypeError, ValueError) as e:
        raise ProposalError(422, "corrupt_proposal", f"proposal {pid!r} body is invalid") from e
    if not isinstance(data, dict):
        raise ProposalError(422, "corrupt_proposal", f"proposal {pid!r} body is invalid")
    return data


_UNSET = object()


def list_proposals(
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    proposal_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    from aimo.backend import orders as store
    q, args = "select body from port_proposals where 1=1", []
    if proposal_id is not _UNSET:
        pid = _validate_proposal_id(proposal_id)
        q, args = q + " and proposal_id=?", [*args, pid]
    if customer_id is not None:
        if isinstance(customer_id, bool) or not isinstance(customer_id, str):
            raise ProposalError(422, "bad_customer_id", "customer_id must be a string")
        if _has_ctrl(customer_id) or len(customer_id) > MAX_CUSTOMER_ID_LEN:
            raise ProposalError(422, "bad_customer_id", "customer_id must be 1-128 characters")
        cid = customer_id.strip()
        if customer_id != "" and not cid:
            raise ProposalError(422, "bad_customer_id", "customer_id must not be blank")
        if cid:
            q, args = q + " and customer_id=?", [*args, cid]
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, str):
            raise ProposalError(422, "bad_status", "status must be a string")
        if _has_ctrl(status) or len(status) > 64:
            raise ProposalError(422, "bad_status", "status must be 1-64 characters")
        st = status.strip()
        if status != "" and not st:
            raise ProposalError(422, "bad_status", "status must not be blank")
        if st and st != "all":
            q, args = q + " and status=?", [*args, st]
    with store._lock:
        rows = _conn().execute(q + " order by created_at desc", args).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            item = json.loads(r[0])
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _audit(actor: str, action: str, subject: str, summary: str, data: dict, related: Optional[list[str]] = None):
    try:
        from aimo.gateway import service
        service.audit(actor, action, subject, summary, data, related_ids=related)
    except Exception:
        log.exception("proposal audit failed")


def propose_offer(customer_id: str, *, product_id: Optional[str] = None, qty_sales_units: Optional[float] = None,
                  replaces_product_id: Optional[str] = None, delivery_date: Any = None, note: str = "",
                  agent_id: Optional[str] = None, offers_fn: Optional[Callable] = None) -> dict[str, Any]:
    """Create a proposal. Without product_id the seller picks a cheaper equivalent of a usual item
    (kind `cheaper_equivalent`); with product_id it proposes that product (kind `seller_pick`, e.g. a seasonal item)."""
    from aimo.gateway import service

    seller = seller_id(agent_id)
    if isinstance(customer_id, bool) or not isinstance(customer_id, str):
        raise ProposalError(422, "bad_customer_id", "customer_id must be a non-empty string")
    cid = customer_id.strip()
    if not cid:
        raise ProposalError(422, "bad_customer_id", "customer_id must not be blank")
    if len(cid) > MAX_CUSTOMER_ID_LEN or _has_ctrl(cid) or ".." in cid or "/" in cid or "\\" in cid:
        raise ProposalError(404, "unknown_customer", f"unknown customer {cid[:64]!r}")
    try:
        service.mandate_for(service.Identity(agent_id="buyer-1", customer_id=cid))
    except service.ServiceError as e:
        raise ProposalError(404, "unknown_customer", f"unknown customer {cid!r}") from e

    delivery = _parse_delivery_date(delivery_date, cid)
    qty = _validate_qty(qty_sales_units)
    clean_pid = _validate_product_id(product_id, "product_id")
    clean_replaces = _validate_product_id(replaces_product_id, "replaces_product_id")

    if note is None:
        clean_note = ""
    elif isinstance(note, bool) or not isinstance(note, str):
        raise ProposalError(422, "bad_note", "note must be a string")
    elif "\x00" in note or len(note) > 2000:
        raise ProposalError(422, "bad_note", "note must be at most 2,000 characters without NUL bytes")
    else:
        clean_note = note

    if clean_pid is not None:
        from aimo.catalog import get_product

        try:
            p = get_product(clean_pid)
        except FileNotFoundError as e:
            raise _catalog_missing_error() from e
        if p is None:
            raise ProposalError(404, "product_not_found", f"product {clean_pid!r} not in the catalog")
        if offers_fn is not None:
            offers = offers_fn([p.id], delivery, customer_id=cid)
        else:
            from aimo.backend.client import get_offers_with_meta
            offers, _ = get_offers_with_meta([p.id], delivery, cid)
        if not isinstance(offers, dict):
            offers = {}
        o = offers.get(p.id)
        if o is None or o.available is not True or _finite_pos(getattr(o, "unit_price", None)) is None:
            raise ProposalError(409, "not_orderable", f"{p.name} is not orderable for {delivery}",
                                {"issues": o.issues if o is not None else ["no_offer"]})
        cand = {"product": p, "offer": o, "tier": None, "qty_sales_units": qty if qty is not None else 1.0,
                "why": [f"{p.name}: proposed by the seller" + (f" ({clean_note})" if clean_note else "")],
                "replaces": {"product_id": clean_replaces, "name": None} if clean_replaces else None,
                "estimated_saving_eur": None}
        kind = "seller_pick"
    else:
        try:
            props, notes = find_cheaper(cid, delivery, offers_fn, only=clean_replaces)
        except FileNotFoundError as e:
            raise _catalog_missing_error() from e
        if not props:
            raise ProposalError(404, "no_better_offer", "no better offer found for this customer's usual items",
                                {"notes": notes})
        cand, kind = props[0], "cheaper_equivalent"
        offers = {cand["product"].id: cand["offer"]}
        if qty is not None:
            cand["qty_sales_units"] = qty
    p, o = cand["product"], cand["offer"]
    lines = [{"product_id": p.id, "qty_sales_units": cand["qty_sales_units"]}]
    try:
        preview = policy_preview(cid, delivery, lines, offers)
    except Exception as e:
        log.warning("policy preview failed: %s", e)
        preview = {"binding": False, "outcome": "unknown", "error": str(e)[:200]}
    pid = f"P-{_now():%Y%m%d}-{uuid.uuid4().hex[:8]}"
    prop = {
        "proposal_id": pid, "customer_id": cid, "seller_id": seller, "kind": kind, "status": "proposed",
        "created_at": _now().isoformat(timespec="seconds"), "delivery_date": delivery.isoformat(),
        "product": {"product_id": p.id, "name": p.name, "brand": p.brand, "sales_unit": p.sales_unit,
                    "content_amount": p.content_amount, "content_unit": p.content_unit},
        "replaces": cand.get("replaces"), "qty_sales_units": cand["qty_sales_units"],
        "offer": {"unit_price": o.unit_price, "comparison_price": o.comparison_price,
                  "comparison_unit": o.comparison_unit, "available_amount": o.available_amount,
                  "fetched_at": o.fetched_at.isoformat() if o.fetched_at else None},
        "why": cand["why"], "estimated_saving_eur": cand.get("estimated_saving_eur"), "note": clean_note or None,
        "policy_preview": preview,
        "seller_can_order": False,
        "how_to_accept": ("Only the customer's own agent can accept: POST /v1/proposals/{id}/accept with its "
                          "X-Customer-Id/X-Agent-Id, or create_quote(quote_request) + place_order over MCP. "
                          "The customer's policy decides; the seller cannot place orders."),
        "quote_request": {"delivery_date": delivery.isoformat(), "lines": lines},
        "provenance": {"source": "aimo.agents.seller (ported from Momin's junction26-valio agents/seller.py)",
                       "agent": f"agent:{seller}", "offer_source": (o.provenance or {}).get("source") if o.provenance
                       else "valio-aimo-mock-api"},
    }
    _save(prop)
    _audit(f"agent:{seller}", "proposal.created", pid,
           f"Seller agent {seller} proposed {cand['qty_sales_units']:g} x {p.name} ({p.id}) to {cid}"
           + (f" instead of {prop['replaces']['product_id']}" if prop.get("replaces") else "")
           + f" for {delivery:%a %d %b}. Policy preview (not binding): {preview.get('outcome')}"
           + (f", approver {preview.get('approver_role')}" if preview.get("approver_role") else "")
           + ". The seller cannot order; the buyer decides.",
           {"customer_id": cid, "product_id": p.id, "kind": kind, "policy_preview": preview.get("outcome")})
    return prop


def accept_proposal(proposal_id: str, identity: Any) -> dict[str, Any]:
    """Buyer side only. Runs the normal quote -> order path under the BUYER's identity and mandate."""
    from aimo.gateway import service

    pid = _validate_proposal_id(proposal_id)
    if identity is None or not hasattr(identity, "agent_id") or not hasattr(identity, "customer_id"):
        raise ProposalError(401, "identity_required", "buyer identity is required")
    with _proposal_lock(pid):
        prop = get_proposal(pid)
        if is_seller(identity.agent_id) or is_seller(getattr(identity, "full_agent_id", "")):
            _audit(f"agent:{identity.agent_id}", "proposal.accept_refused", pid,
                   f"Seller agent {identity.agent_id} tried to accept proposal {pid}; refused: a seller can "
                   f"never order on a customer's behalf.", {"customer_id": prop["customer_id"]})
            raise ProposalError(403, "seller_cannot_order", "a seller agent can only propose; the customer's own agent "
                                                             "must accept and the customer's policy decides")
        if identity.customer_id != prop["customer_id"]:
            raise ProposalError(403, "customer_mismatch", "this proposal was made to another customer")
        if prop["status"] != "proposed":
            raise ProposalError(409, "not_open", f"proposal is {prop['status']}")
        qr = prop["quote_request"]
        quote = service.make_quote(identity, date.fromisoformat(qr["delivery_date"]), qr["lines"], None)
        order = service.submit_order(identity, quote.quote_id, quote.quote_hash or "")
        prop.update(status="accepted", accepted_by=f"agent:{identity.full_agent_id}", quote_id=quote.quote_id,
                    order_id=order.order_id, order_status=order.status, decision=order.decision.model_dump(mode="json"),
                    accepted_at=_now().isoformat(timespec="seconds"))
        _save(prop)
    _audit(identity.actor, "proposal.accepted", pid,
           f"{identity.full_agent_id} accepted proposal {pid} from {prop['seller_id']}: quote {quote.quote_id}, "
           f"order {order.order_id} is {order.status} under {prop['customer_id']}'s policy.",
           {"quote_id": quote.quote_id, "order_id": order.order_id, "status": order.status},
           related=[quote.quote_id, order.order_id])
    return {"proposal": prop, "quote": quote.model_dump(mode="json"), "order": order.model_dump(mode="json")}


def decline_proposal(proposal_id: str, identity: Any, reason: str = "") -> dict[str, Any]:
    pid = _validate_proposal_id(proposal_id)
    if identity is None or not hasattr(identity, "agent_id") or not hasattr(identity, "customer_id"):
        raise ProposalError(401, "identity_required", "buyer identity is required")
    if reason is None:
        clean_reason = ""
    elif isinstance(reason, bool) or not isinstance(reason, str):
        raise ProposalError(422, "bad_reason", "reason must be a string")
    elif _has_ctrl(reason) or len(reason) > 2000:
        raise ProposalError(422, "bad_reason", "reason must be at most 2,000 characters without control characters")
    else:
        clean_reason = reason
    with _proposal_lock(pid):
        prop = get_proposal(pid)
        if is_seller(identity.agent_id) or is_seller(getattr(identity, "full_agent_id", "")) or identity.customer_id != prop["customer_id"]:
            raise ProposalError(403, "not_your_proposal", "only the customer's side can decline")
        if prop["status"] != "proposed":
            raise ProposalError(409, "not_open", f"proposal is {prop['status']}")
        prop.update(status="declined", declined_by=f"agent:{identity.full_agent_id}", decline_reason=clean_reason or None)
        _save(prop)
    _audit(identity.actor, "proposal.declined", pid,
           f"{identity.full_agent_id} declined proposal {pid}" + (f": {clean_reason}" if clean_reason else "."), {})
    return prop
