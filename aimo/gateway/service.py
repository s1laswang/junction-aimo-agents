"""Shared gateway logic. HTTP routes (app.py) and MCP tools (mcp_server.py) are thin wrappers.

Integrations with catalog and trust are imported lazily per docs/interfaces.md. Until those
land (ImportError / NotImplementedError / AttributeError) we use minimal, clearly-labelled
fallbacks so the end-to-end slice keeps running:
  - catalog -> aimo.gateway.fallback_catalog (name token search)
  - trust.sign_quote -> sha256 quote_hash, NO signature (signature=None, kid="unsigned-fallback")
  - trust.evaluate -> hotel-aurora-style thresholds (policy_id "gateway-fallback")
  - trust.audit -> unsigned hash chain in SQLite (gw_audit_fallback)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aimo.backend import orders as store
from aimo.backend.client import UpstreamError, get_offers_with_meta
from aimo.models import (
    AuditEvent,
    Mandate,
    Need,
    Offer,
    Order,
    Plan,
    PolicyDecision,
    Product,
    ProductSummary,
    Quote,
    QuoteLine,
    RuleHit,
)

log = logging.getLogger("aimo.gateway")

VERSION = "0.1.0"
QUOTE_TTL_MIN = float(os.environ.get("AIMO_QUOTE_TTL_MIN", "15"))
DEFAULT_CUSTOMER = os.environ.get("AIMO_CUSTOMER_ID", "hotel-aurora")
DEFAULT_AGENT = os.environ.get("AIMO_AGENT_ID", "buyer-1")
_MISSING = (ImportError, NotImplementedError, AttributeError)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail

    def to_dict(self) -> dict[str, Any]:
        d = {"error": self.code, "message": self.message}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass
class Identity:
    agent_id: str = DEFAULT_AGENT
    customer_id: str = DEFAULT_CUSTOMER

    @property
    def actor(self) -> str:
        return f"agent:{self.customer_id}/{self.agent_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}-{_now():%Y%m%d}-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------ integration adapters
def _catalog():
    try:
        import aimo.catalog as cat

        if hasattr(cat, "search"):
            return cat
    except ImportError:
        pass
    return None


def _trust():
    try:
        import aimo.trust as t

        return t
    except ImportError:
        return None


def integration_status() -> dict[str, str]:
    st: dict[str, str] = {}
    cat = _catalog()
    st["catalog"] = "aimo.catalog" if cat else "fallback"
    t = _trust()
    for fn in ("sign_quote", "verify_quote", "evaluate", "jwks"):
        st[f"trust.{fn}"] = "aimo.trust" if (t and hasattr(t, fn)) else "fallback"
    st["trust.audit"] = "aimo.trust" if (t and hasattr(t, "audit")) else "fallback"
    return st


def canonical_quote_hash(q: Quote) -> str:
    body = q.model_dump(mode="json", exclude={"quote_hash", "signature", "kid"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sign_quote(q: Quote) -> Quote:
    t = _trust()
    try:
        return t.sign_quote(q)  # type: ignore[union-attr]
    except _MISSING:
        log.warning("trust.sign_quote unavailable: quote is hashed but NOT signed (TODO)")
        return q.model_copy(update={"quote_hash": canonical_quote_hash(q), "signature": None, "kid": "unsigned-fallback"})


def verify_quote(q: Quote) -> bool:
    t = _trust()
    if q.kid != "unsigned-fallback":
        try:
            return bool(t.verify_quote(q))  # type: ignore[union-attr]
        except _MISSING:
            pass
    return q.quote_hash == canonical_quote_hash(q)


def mandate_for(ident: Identity) -> Mandate:
    t = _trust()
    for fn in ("get_mandate", "mandate_for"):
        try:
            m = getattr(t, fn)(ident.customer_id, ident.agent_id)
            if m is not None:
                return m
        except _MISSING:
            continue
        except KeyError:
            raise ServiceError(403, "unknown_customer", f"no mandate for customer {ident.customer_id!r}")
    return Mandate(customer_id=ident.customer_id, agent_id=ident.agent_id, policy_id=ident.customer_id)


def _fallback_evaluate(q: Quote, m: Mandate) -> PolicyDecision:
    hits = []
    if q.total_net <= 500:
        outcome, role = "auto_approve", None
        hits.append(RuleHit(rule_id="spend.auto", outcome="allow", message=f"Total €{q.total_net:.2f} is within the €500 auto-approve limit."))
    elif q.total_net <= 5000:
        outcome, role = "needs_approval", "procurement_manager"
        hits.append(RuleHit(rule_id="spend.approval", outcome="needs_approval",
                            message=f"Total €{q.total_net:.2f} is over the €500 auto-approve limit; the procurement manager must approve."))
    else:
        outcome, role = "reject", None
        hits.append(RuleHit(rule_id="spend.max", outcome="reject", message=f"Total €{q.total_net:.2f} exceeds the €5,000 hard limit."))
    if outcome == "auto_approve" and any(ln.tier in ("better", "good") for ln in q.lines):
        outcome, role = "needs_approval", "procurement_manager"
        hits.append(RuleHit(rule_id="substitution.tier", outcome="needs_approval", message="A substitute outside the best tier needs approval."))
    return PolicyDecision(outcome=outcome, approver_role=role, hits=hits, policy_id="gateway-fallback", policy_version="0")


def evaluate(q: Quote, m: Mandate) -> PolicyDecision:
    t = _trust()
    try:
        return t.evaluate(q, m)  # type: ignore[union-attr]
    except _MISSING:
        log.warning("trust.evaluate unavailable: using gateway fallback thresholds")
        return _fallback_evaluate(q, m)


def audit(actor: str, action: str, subject_id: str, summary: str, data: dict[str, Any] | None = None,
          related_ids: list[str] | None = None) -> None:
    data = json.loads(json.dumps(data or {}, default=str))
    t = _trust()
    try:
        if related_ids:
            t.audit.append(actor, action, subject_id, summary, data, related_ids=related_ids)  # type: ignore[union-attr]
        else:
            t.audit.append(actor, action, subject_id, summary, data)  # type: ignore[union-attr]
        return
    except _MISSING:
        pass
    except Exception:  # never let audit break the trade flow; but log loudly
        log.exception("trust.audit.append failed; writing to fallback chain")
    store.fallback_audit_append({"actor": actor, "action": action, "subject_id": subject_id, "summary": summary, "data": data})


def _trust_log(fn_name: str, *args, **kwargs) -> bool:
    """Call a trust audit helper (log_quote, log_decision, log_order). False if unavailable."""
    t = _trust()
    try:
        getattr(t, fn_name)(*args, **kwargs)
        return True
    except _MISSING:
        return False
    except Exception:
        log.exception("trust.%s failed; using gateway audit wording", fn_name)
        return False


def _log_order(order: Order, quote: Quote | None, actor: str, note: str, fb_action: str, fb_summary: str,
               fb_data: dict[str, Any] | None = None) -> None:
    if _trust_log("log_order", order, quote, actor=actor, note=note):
        return
    audit(actor, fb_action, order.order_id, fb_summary, {"quote_id": order.quote_id, **(fb_data or {})},
          related_ids=[order.quote_id])


def audit_events(subject_id: str | None = None) -> list[dict[str, Any]]:
    t = _trust()
    subjects = _related_subjects(subject_id)
    try:
        evs: list[Any] = []
        if subject_id is None:
            evs = list(t.audit.events())  # type: ignore[union-attr]
        else:
            for s in subjects:
                evs.extend(t.audit.events(subject_id=s))  # type: ignore[union-attr]
        out = [e.model_dump(mode="json") if isinstance(e, AuditEvent) else dict(e) for e in evs]
    except _MISSING:
        out = [e for s in (subjects if subject_id else [None]) for e in store.fallback_audit_events(s)]
    out = list({e.get("seq"): e for e in out}.values())  # de-dup (trust already follows related ids)
    out.sort(key=lambda e: e.get("seq", 0))
    return out


def _related_subjects(subject_id: str | None) -> list[str]:
    """An order's trail includes its quote's events (and vice versa)."""
    if not subject_id:
        return []
    subs = [subject_id]
    o = store.get_order(subject_id)
    if o:
        subs.insert(0, o.quote_id)
    else:
        o2 = store.order_for_quote(subject_id)
        if o2:
            subs.append(o2.order_id)
    return subs


def audit_verify() -> dict[str, Any]:
    t = _trust()
    try:
        return t.audit.verify()  # type: ignore[union-attr]
    except _MISSING:
        evs = store.fallback_audit_events()
        prev = "0" * 64
        for i, e in enumerate(evs):
            canon = json.dumps({k: v for k, v in e.items() if k not in ("hash", "signature", "kid")},
                               sort_keys=True, separators=(",", ":"), default=str)
            if e["prev_hash"] != prev or hashlib.sha256(canon.encode()).hexdigest() != e["hash"]:
                return {"ok": False, "checked": i, "broken_at": e["seq"], "signed": False}
            prev = e["hash"]
        return {"ok": True, "checked": len(evs), "broken_at": None, "signed": False}


def audit_report(subject_id: str) -> str:
    t = _trust()
    try:
        return t.audit.render(subject_id)  # type: ignore[union-attr]
    except _MISSING:
        evs = audit_events(subject_id)
        lines = [f"# Audit trail for {subject_id}", "", "_Fallback renderer: trust module not loaded; events are hash-chained but unsigned._", ""]
        for e in evs:
            lines.append(f"- **{str(e['ts'])[11:19]}** `{e['action']}` by {e['actor']}: {e['summary']}")
        return "\n".join(lines) + "\n"


def jwks() -> dict[str, Any]:
    t = _trust()
    try:
        return t.jwks()  # type: ignore[union-attr]
    except _MISSING:
        return {"keys": [], "note": "trust module not loaded: quotes are unsigned (fallback)"}


# ------------------------------------------------------------------ catalog
def find_products(query: str, claims: list[str] | None = None, category: str | None = None,
                    content_unit: str | None = None, limit: int = 20) -> list[ProductSummary]:
    limit = max(1, min(int(limit or 20), 100))
    cat = _catalog()
    if cat is not None:
        try:
            return cat.search(query, claims=claims or [], category=category, content_unit=content_unit, limit=limit)
        except NotImplementedError:
            pass
    from aimo.gateway import fallback_catalog

    return fallback_catalog.search(query, claims=claims or [], category=category, limit=limit)


def get_product(product_id: str) -> Product:
    p = None
    cat = _catalog()
    if cat is not None:
        try:
            p = cat.get_product(product_id)
        except (NotImplementedError, AttributeError):
            p = None
    if p is None:
        from aimo.gateway import fallback_catalog

        p = fallback_catalog.get_product(product_id)
    if p is None:
        raise ServiceError(404, "product_not_found", f"product {product_id!r} not found in catalog")
    return p


def _product_name(pid: str) -> str:
    try:
        return get_product(pid).name
    except ServiceError:
        return pid


# ------------------------------------------------------------------ offers / plan
def _upstream(e: UpstreamError) -> ServiceError:
    status = 503 if e.status_code in (429, 503, 504) or e.status_code >= 500 else 502
    return ServiceError(status, "upstream_unavailable", str(e),
                        {"upstream_status": e.status_code, "retry_after": e.retry_after or 2, "injected": e.injected})


def fetch_offers(product_ids: list[str], requested_date: date, customer_id: str | None = None,
               quantities: dict[str, float] | None = None) -> tuple[list[Offer], dict[str, Any]]:
    ids = [str(p).strip() for p in product_ids if str(p).strip()]
    if not ids:
        raise ServiceError(400, "no_products", "product_ids must contain at least one id")
    if len(ids) > 1000:
        raise ServiceError(400, "too_many_products", "max 1,000 product ids per call (upstream limit)")
    try:
        offers, meta = get_offers_with_meta(ids, requested_date, customer_id, quantities)
    except UpstreamError as e:
        raise _upstream(e) from e
    out = [offers[p].model_copy(update={"provenance": meta}) for p in dict.fromkeys(ids)]
    return out, meta


def plan_need(need: Any, identity: Any = None, customer_id: str | None = None, **_: Any) -> Plan:
    need = _as_need(need, resolve_identity(identity, None, customer_id))
    cat = _catalog()
    if cat is None or not hasattr(cat, "plan_need"):
        raise ServiceError(501, "plan_unavailable", "planner (aimo.catalog.plan_need) is not available yet")
    try:
        return cat.plan_need(need)
    except NotImplementedError as e:
        raise ServiceError(501, "plan_unavailable", "planner not implemented yet") from e
    except UpstreamError as e:
        raise _upstream(e) from e


# ------------------------------------------------------------------ quotes
def make_quote(ident: Identity, delivery_date: date, lines: list[dict[str, Any]], need: Need | None = None) -> Quote:
    if not lines:
        raise ServiceError(400, "no_lines", "a quote needs at least one line")
    qty_by: dict[str, float] = {}
    for ln in lines:
        q = float(ln.get("qty_sales_units") or 0)
        if q <= 0:
            raise ServiceError(400, "bad_quantity", f"qty_sales_units must be > 0 for {ln.get('product_id')}")
        pid = str(ln["product_id"])
        qty_by[pid] = qty_by.get(pid, 0) + q

    offers_list, meta = fetch_offers(list(qty_by), delivery_date, ident.customer_id, qty_by)
    offers = {o.product_id: o for o in offers_list}

    refused = []
    for pid, q in qty_by.items():
        o = offers[pid]
        if not o.available:
            refused.append({"product_id": pid, "reason": "unavailable", "issues": o.issues,
                            "closing_time": o.closing_time.isoformat() if o.closing_time else None})
        elif o.available_amount is not None and o.available_amount < q:
            refused.append({"product_id": pid, "reason": "partial_stock", "requested": q,
                            "available_amount": o.available_amount, "issues": o.issues})
    if refused:
        audit(ident.actor, "quote.refused", f"refused-{ident.customer_id}",
              f"Quote request for {len(qty_by)} product(s) refused: " +
              "; ".join(f"{r['product_id']} {r['reason']}" for r in refused),
              {"refused": refused, "delivery_date": delivery_date})
        raise ServiceError(409, "lines_unavailable",
                           "some lines cannot be delivered for this date; re-plan (split or substitute) and re-quote",
                           {"refused": refused})

    qlines: list[QuoteLine] = []
    total = gross = 0.0
    for ln in lines:
        pid = str(ln["product_id"])
        o = offers[pid]
        q = float(ln["qty_sales_units"])
        lt = round(q * float(o.unit_price), 2)
        total += lt
        gross += q * float(o.unit_price_gross or o.unit_price)
        info = [i for i in o.issues if i in ("limited_stock", "terminal_unknown_qty") or i.startswith("injected:")]
        qlines.append(QuoteLine(
            product_id=pid, product_name=ln.get("product_name") or _product_name(pid), qty_sales_units=q,
            sales_unit=o.sales_unit or "ST", unit_price=float(o.unit_price), line_total=lt,
            tier=ln.get("tier") or "preferred", flags=list(dict.fromkeys(list(ln.get("flags") or []) + info)),
        ))

    created = _now().replace(microsecond=0)
    expires = created + timedelta(minutes=QUOTE_TTL_MIN)
    valid_tos = [o.price_valid_to for o in offers.values() if o.price_valid_to]
    if valid_tos:
        expires = min(expires, min(valid_tos))
    snapshot = [{
        "product_id": o.product_id, "available_amount": o.available_amount,
        "available_date": o.available_date.isoformat() if o.available_date else None,
        "closing_time": o.closing_time.isoformat() if o.closing_time else None,
        "logistic_model": o.logistic_model, "unit_price": o.unit_price, "issues": o.issues,
    } for o in offers.values()]
    quote = Quote(
        quote_id=_id("Q"), customer_id=ident.customer_id, agent_id=ident.agent_id, delivery_date=delivery_date,
        lines=qlines, total_net=round(total, 2), total_gross=round(gross, 2), created_at=created, expires_at=expires,
        need=need, provenance={**meta, "price_locked_until": expires.isoformat()}, availability_snapshot=snapshot,
    )
    quote = sign_quote(quote)
    store.save_quote(quote)
    if not _trust_log("log_quote", quote):
        audit(ident.actor, "quote.created", quote.quote_id,
              f"Quote {quote.quote_id} for {ident.customer_id}: {len(qlines)} line(s), €{quote.total_net:.2f} net, "
              f"delivery {delivery_date.isoformat()}, price fixed until {expires:%H:%M} UTC"
              + ("" if quote.signature else " (UNSIGNED: trust module not loaded)"),
              {"quote_hash": quote.quote_hash, "kid": quote.kid, "total_net": quote.total_net,
               "lines": [l.model_dump(mode="json") for l in qlines], "provenance": quote.provenance})
    return quote


def get_quote(quote_id: str) -> Quote:
    q = store.get_quote(quote_id)
    if q is None:
        raise ServiceError(404, "quote_not_found", f"quote {quote_id!r} not found")
    return q


# ------------------------------------------------------------------ orders
def _check_quote(q: Quote, quote_hash: str) -> None:
    if quote_hash != q.quote_hash:
        raise ServiceError(409, "quote_hash_mismatch", "quote_hash does not match the stored quote",
                           {"expected": q.quote_hash})
    if not verify_quote(q):
        raise ServiceError(409, "quote_signature_invalid", "quote signature/hash verification failed")


def _expired(q: Quote) -> bool:
    exp = q.expires_at if q.expires_at.tzinfo else q.expires_at.replace(tzinfo=timezone.utc)
    return exp <= _now()


def _finalize(order: Order, quote: Quote, actor: str) -> Order:
    """Re-check availability and place the order in the mock backend (Peppol order response)."""
    qty_by: dict[str, float] = {}
    for ln in quote.lines:
        qty_by[ln.product_id] = qty_by.get(ln.product_id, 0) + ln.qty_sales_units
    try:
        offers, meta = get_offers_with_meta(list(qty_by), quote.delivery_date, quote.customer_id, qty_by)
    except UpstreamError as e:
        order = order.model_copy(update={"status": "approved", "updated_at": _now()})
        store.save_order(order)
        audit("system:gateway", "order.placement_failed", order.order_id,
              f"Order {order.order_id} approved but placement failed (upstream unavailable); retry POST /v1/orders.",
              {"error": str(e), "quote_id": quote.quote_id}, related_ids=[quote.quote_id])
        raise _upstream(e) from e
    code, rlines = store.confirm_lines(quote, offers)
    status = "rejected" if code == "RE" else "placed"
    order = order.model_copy(update={"status": status, "response_code": code, "response_lines": rlines,
                                     "updated_at": _now(), "provenance": meta})
    store.save_order(order)
    words = {"AP": "accepted", "CA": "accepted with changes", "RE": "rejected by supplier"}[code]
    detail = "; ".join(f"{l.product_id} {l.code} {l.qty_confirmed:g}" + (f" ({l.note})" if l.code != "AP" else "")
                       for l in rlines)
    _log_order(order, quote, "system:order-backend", detail,
               "order.placed" if status == "placed" else "order.rejected_by_supplier",
               f"Order {order.order_id} {words} ({code}): {detail}",
               {"quote_hash": quote.quote_hash, "response_code": code,
                "response_lines": [l.model_dump() for l in rlines], "placed_by": actor})
    return order


def submit_order(ident: Identity, quote_id: str, quote_hash: str) -> Order:
    quote = get_quote(quote_id)
    if quote.customer_id != ident.customer_id:
        raise ServiceError(403, "customer_mismatch", "this quote belongs to another customer")
    _check_quote(quote, quote_hash)

    existing = store.order_for_quote(quote_id)
    if existing is not None:
        if existing.status == "approved":  # approved but placement failed earlier: retry placement
            if _expired(quote):
                raise ServiceError(409, "quote_expired", "re-quote needed: the quote's price lock has expired")
            return _finalize(existing, quote, ident.actor)
        return existing  # idempotent: same quote -> same order

    if _expired(quote):
        audit(ident.actor, "order.refused", quote_id, f"Order for quote {quote_id} refused: quote expired at {quote.expires_at:%H:%M} UTC.")
        raise ServiceError(409, "quote_expired", "re-quote needed: the quote's price lock has expired",
                           {"expires_at": quote.expires_at.isoformat()})

    mandate = mandate_for(ident)
    trust_policy = integration_status()["trust.evaluate"] == "aimo.trust"
    if not trust_policy and "order:submit" not in (mandate.scopes or []):
        raise ServiceError(403, "scope_missing", "agent mandate lacks scope order:submit")
    mexp = mandate.expires_at
    if mexp and (mexp if mexp.tzinfo else mexp.replace(tzinfo=timezone.utc)) <= _now():
        raise ServiceError(403, "mandate_expired", "agent mandate has expired")

    decision = evaluate(quote, mandate)
    now = _now()
    order = Order(order_id=_id("ORD"), quote_id=quote_id, quote_hash=quote.quote_hash or "", customer_id=quote.customer_id,
                  agent_id=ident.agent_id, status="pending_approval", decision=decision, created_at=now, updated_at=now)
    if not _trust_log("log_decision", quote, decision, order.order_id):
        audit("system:policy-engine", "policy.evaluated", order.order_id,
              f"Policy {decision.policy_id} v{decision.policy_version} on quote {quote_id} (€{quote.total_net:.2f}): "
              f"{decision.outcome}" + (f", approver {decision.approver_role}" if decision.approver_role else "") + ". "
              + " ".join(h.message for h in decision.hits if h.outcome != "allow"),
              {"quote_id": quote_id, "decision": decision.model_dump()}, related_ids=[quote_id])

    if decision.outcome == "reject":
        order = order.model_copy(update={"status": "rejected"})
        store.save_order(order)
        why = " ".join(h.message for h in decision.hits if h.outcome == "reject")
        _log_order(order, quote, ident.actor, why, "order.rejected", f"Order {order.order_id} rejected by policy. {why}")
        return order
    if decision.outcome == "needs_approval":
        store.save_order(order)
        _log_order(order, quote, ident.actor, "", "order.pending_approval",
                   f"Order {order.order_id} submitted by {ident.actor}; waiting for "
                   f"{decision.approver_role or 'a human'} approval.", {"quote_hash": quote.quote_hash})
        return order
    order = order.model_copy(update={"status": "approved", "approved_by": f"auto (policy {decision.policy_id})"})
    store.save_order(order)
    _log_order(order, quote, "system:policy-engine", "within the agent's mandate, no human needed",
               "order.auto_approved", f"Order {order.order_id} auto-approved within mandate.")
    return _finalize(order, quote, ident.actor)


def get_order(order_id: str) -> Order:
    o = store.get_order(order_id)
    if o is None:
        raise ServiceError(404, "order_not_found", f"order {order_id!r} not found")
    return o


def list_approvals(status: str | None = "pending", customer_id: str | None = None) -> list[dict[str, Any]]:
    st = {"pending": "pending_approval", None: None, "": None, "all": None}.get(status, status)
    out = []
    for o in store.list_orders(st, customer_id):
        q = store.get_quote(o.quote_id)
        out.append({"order": o.model_dump(mode="json"), "quote": q.model_dump(mode="json") if q else None,
                    "decision": o.decision.model_dump(mode="json"), "expired": bool(q and _expired(q)),
                    "approvals": store.approvals_for(o.order_id)})
    return out


def _directory_role(customer_id: str, approver: str) -> str | None:
    for c in list_customers():
        if c.get("customer_id") == customer_id:
            for a in c.get("approvers", []) or []:
                if approver in (a.get("id"), a.get("name")):
                    return a.get("role")
    return None


def decide(order_id: str, decision: str, approver: str, quote_hash: str, note: str | None = None,
           approver_role: str | None = None) -> Order:
    if decision not in ("approve", "reject"):
        raise ServiceError(400, "bad_decision", "decision must be 'approve' or 'reject'")
    if not approver:
        raise ServiceError(400, "approver_required", "approver is required")
    order = get_order(order_id)
    if order.status != "pending_approval":
        raise ServiceError(409, "not_pending", f"order is {order.status}, not pending_approval")
    quote = get_quote(order.quote_id)
    role = approver_role or _directory_role(order.customer_id, approver) or order.decision.approver_role or ""
    expired = _expired(quote)
    human = f"human:{approver}"

    t = _trust()
    try:
        chk = t.check_approval(quote, order.decision, approver, role, quote_hash,  # type: ignore[union-attr]
                               decision_kind=decision, note=note or "", order_id=order_id)
    except _MISSING:
        chk = None
        if quote_hash != order.quote_hash:
            audit(human, "approval.refused", order_id,
                  f"{approver} tried to {decision} order {order_id} with a different quote_hash.",
                  {"quote_hash": quote_hash, "expected": order.quote_hash}, related_ids=[quote.quote_id])
        elif not verify_quote(quote):
            raise ServiceError(409, "quote_signature_invalid", "quote signature/hash verification failed")

    if quote_hash != order.quote_hash:
        raise ServiceError(409, "quote_hash_mismatch",
                           "approval is bound to a different quote_hash: the quote changed, re-review it",
                           {"expected": order.quote_hash})
    if expired and decision == "approve":
        order = order.model_copy(update={"status": "expired", "updated_at": _now()})
        store.save_order(order)
        _log_order(order, quote, human, "the quote's price lock expired before approval; re-quote needed",
                   "order.expired", f"{approver} tried to approve order {order_id}, but its quote expired; re-quote needed.")
        raise ServiceError(409, "quote_expired", "re-quote needed: the quote's price lock expired before the decision",
                           {"expires_at": quote.expires_at.isoformat()})
    if chk is not None and not chk.get("ok"):
        raise ServiceError(403, "approval_refused", chk.get("reason") or "approval refused by trust layer")

    store.record_approval(order_id, decision, approver, note, quote_hash)
    who = f"{approver} ({role})" if role else approver
    if decision == "reject":
        order = order.model_copy(update={"status": "rejected", "approved_by": who, "approval_note": note,
                                         "updated_at": _now()})
        store.save_order(order)
        _log_order(order, quote, human, note or f"rejected by {who}", "order.rejected_by_human",
                   f"{who} rejected order {order_id}." + (f" Note: {note}" if note else ""), {"quote_hash": quote_hash})
        return order
    order = order.model_copy(update={"status": "approved", "approved_by": who, "approval_note": note, "updated_at": _now()})
    store.save_order(order)
    _log_order(order, quote, human, note or "", "order.approved",
               f"{who} approved order {order_id} (quote {quote.quote_id}, €{quote.total_net:.2f}, hash {quote_hash[:12]}…)."
               + (f" Note: {note}" if note else ""), {"quote_hash": quote_hash})
    return _finalize(order, quote, human)


# ------------------------------------------------------------------ customers / policies
FALLBACK_CUSTOMERS = [
    {"customer_id": "hotel-aurora", "name": "Hotel Aurora Helsinki", "policy_id": "hotel-aurora",
     "summary": "Auto-approve up to €500 per order; €500–5,000 needs procurement_manager; over €5,000 rejected. "
                "Substitutions must keep claims; best tier allowed, better/good need approval. Scope: dairy, bakery, produce."},
    {"customer_id": "school-tampere", "name": "Tampere school kitchen", "policy_id": "school-tampere",
     "summary": "Excluded allergens milk and nuts; auto-approve up to €300; any substitution needs kitchen_manager; "
                "unknown allergen rejects the line."},
    {"customer_id": "hospital-meilahti", "name": "Meilahti hospital kitchen", "policy_id": "hospital-meilahti",
     "summary": "Excluded gluten; dietitian approves any substitution; unknown allergen needs dietitian approval."},
]


def list_customers() -> list[dict[str, Any]]:
    t = _trust()
    for fn in ("list_customers", "customers"):
        try:
            return list(getattr(t, fn)())
        except _MISSING:
            continue
    return FALLBACK_CUSTOMERS


def _policies() -> list[dict[str, Any]]:
    t = _trust()
    try:
        return list(t.list_policies())  # type: ignore[union-attr]
    except _MISSING:
        return list_customers()


# ------------------------------------------------------------------ discovery
def manifest(base_url: str) -> dict[str, Any]:
    b = base_url.rstrip("/")
    return {
        "name": "Valio Aimo Trusted Trade Gateway",
        "description": "Open interface for any company's AI purchasing agent to search, quote and order from Valio Aimo "
                       "inside customer-controlled guardrails, with signed quotes and a hash-chained audit trail.",
        "version": VERSION,
        "protocol": "otap/0.1",
        "openapi_url": f"{b}/openapi.json",
        "docs_url": f"{b}/docs",
        "mcp": {"url": f"{b}/mcp", "transport": "streamable-http", "server_name": "valio-aimo-trusted-trade",
                "stdio": "python -m aimo.gateway.mcp_server"},
        "jwks_url": f"{b}/.well-known/jwks.json",
        "identity": {"headers": ["X-Agent-Id", "X-Customer-Id"],
                     "production": "OAuth 2.1 bearer tokens with scopes catalog:read quote:create order:submit (MCP auth spec)"},
        "endpoints": {
            "search_products": "GET /v1/products/search", "get_product": "GET /v1/products/{id}",
            "get_offers": "POST /v1/offers", "plan_need": "POST /v1/plan", "create_quote": "POST /v1/quotes",
            "get_quote": "GET /v1/quotes/{id}", "place_order": "POST /v1/orders", "get_order": "GET /v1/orders/{id}",
            "approvals": "GET /v1/approvals", "decide": "POST /v1/approvals/{order_id}/decision",
            "audit": "GET /v1/audit", "audit_verify": "GET /v1/audit/verify", "audit_report": "GET /v1/audit/report/{subject_id}",
            "customers": "GET /v1/customers", "agent_runs": "POST /v1/agent/runs",
        },
        "supported_standards": ["MCP", "OpenAPI 3.1", "GS1 GTIN", "Peppol BIS Order Response semantics (AP/CA/RE)",
                                "JWS/EdDSA (Ed25519)", "hash-chained audit"],
        "policies": [{"customer_id": c.get("customer_id"), "policy_id": c.get("policy_id"), "summary": c.get("summary")}
                     for c in list_customers()],
        "quote_ttl_minutes": QUOTE_TTL_MIN,
        "fault_injection": {"header": "X-Aimo-Faults", "example": "stockout:463038;price_shock:464306:1.4;error_rate:0.2;latency_ms:800"},
        "integrations": integration_status(),
    }


# ================================================================== MCP-shaped tool functions
# Exactly the MCP tool names and arguments from docs/interfaces.md, JSON in / JSON out. Used by
# aimo.gateway.mcp_server and by the buyer's in-process transport (aimo.buyer.tools.InProcessTransport),
# so every transport returns the same JSON. Identity: pass `identity` (Identity or dict) or
# agent_id/customer_id; defaults come from env AIMO_AGENT_ID / AIMO_CUSTOMER_ID.
def resolve_identity(identity: Any = None, agent_id: str | None = None, customer_id: str | None = None) -> Identity:
    if isinstance(identity, Identity):
        base = identity
    elif isinstance(identity, dict):
        base = Identity(agent_id=identity.get("agent_id") or DEFAULT_AGENT,
                        customer_id=identity.get("customer_id") or DEFAULT_CUSTOMER)
    else:
        base = Identity()
    return Identity(agent_id=agent_id or base.agent_id, customer_id=customer_id or base.customer_id)


def _as_date(d: Any) -> date:
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except ValueError as e:
        raise ServiceError(400, "bad_date", f"expected YYYY-MM-DD, got {d!r}") from e


def _as_need(need: Any, ident: Identity) -> Need | None:
    if need is None or isinstance(need, Need):
        return need
    d = dict(need)
    d.setdefault("customer_id", ident.customer_id)
    return Need.model_validate(d)


def _j(x: Any) -> Any:
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    if isinstance(x, list):
        return [_j(i) for i in x]
    return x


def search_products(query: str, claims: list[str] | None = None, limit: int = 10, category: str | None = None,
                    content_unit: str | None = None, **_: Any) -> dict[str, Any]:
    return {"products": _j(find_products(query, claims, category, content_unit, limit))}


def get_offers(product_ids: list[str], requested_date: Any, customer_id: str | None = None, identity: Any = None,
               agent_id: str | None = None, **_: Any) -> dict[str, Any]:
    ident = resolve_identity(identity, agent_id, customer_id)
    offers, meta = fetch_offers(product_ids, _as_date(requested_date), ident.customer_id)
    return {"offers": _j(offers), "provenance": meta}


def create_quote(delivery_date: Any, lines: list[Any], need: Any = None, identity: Any = None,
                 agent_id: str | None = None, customer_id: str | None = None, **_: Any) -> dict[str, Any]:
    ident = resolve_identity(identity, agent_id, customer_id)
    ls = [l.model_dump() if hasattr(l, "model_dump") else dict(l) for l in lines]
    return _j(make_quote(ident, _as_date(delivery_date), ls, _as_need(need, ident)))


def place_order(quote_id: str, quote_hash: str, identity: Any = None, agent_id: str | None = None,
                customer_id: str | None = None, **_: Any) -> dict[str, Any]:
    return _j(submit_order(resolve_identity(identity, agent_id, customer_id), quote_id, quote_hash))


def get_order_status(order_id: str, **_: Any) -> dict[str, Any]:
    return _j(get_order(order_id))


def get_audit_trail(subject_id: str, **_: Any) -> dict[str, Any]:
    return {"events": audit_events(subject_id), "report": audit_report(subject_id), "verify": audit_verify()}


def list_policies(**_: Any) -> dict[str, Any]:
    return {"policies": _policies()}
