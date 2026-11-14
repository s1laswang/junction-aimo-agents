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
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aimo.backend import clock
from aimo.gateway import resilience
from aimo.backend import orders as store
from aimo.backend.client import INJECTION_TEXT, UpstreamError, current_faults, get_offers_with_meta
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


def normalize_agent_id(customer_id: str, agent_id: str) -> str:
    """Bare agent name for this customer: "hotel-aurora/buyer-1" and "buyer-1" both -> "buyer-1".
    An id prefixed with ANOTHER customer is kept whole (the mandate lookup then rejects it)."""
    a = (agent_id or "").strip() or DEFAULT_AGENT
    prefix = f"{customer_id}/"
    while a.startswith(prefix):
        a = a[len(prefix):]
    return a or DEFAULT_AGENT


@dataclass
class Identity:
    agent_id: str = DEFAULT_AGENT
    customer_id: str = DEFAULT_CUSTOMER

    def __post_init__(self) -> None:
        self.customer_id = (self.customer_id or "").strip() or DEFAULT_CUSTOMER
        self.agent_id = normalize_agent_id(self.customer_id, self.agent_id)

    @property
    def full_agent_id(self) -> str:
        """`<customer>/<agent>` (trust convention); unchanged if the id already names a customer."""
        return self.agent_id if "/" in self.agent_id else f"{self.customer_id}/{self.agent_id}"

    @property
    def actor(self) -> str:
        return f"agent:{self.full_agent_id}"


def _now() -> datetime:
    """Gateway clock: real time, or the simulated demo clock (AIMO_NOW / X-Aimo-Now / replay), see aimo.backend.clock."""
    return clock.now()


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


def evaluate(q: Quote, m: Mandate, products: dict[str, Any] | None = None) -> PolicyDecision:
    t = _trust()
    try:
        kw: dict[str, Any] = {"now": _now()}
        if products:
            kw["products"] = products
        try:
            return t.evaluate(q, m, **kw)  # type: ignore[union-attr]
        except TypeError:  # older trust without products= / now=
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


def _demo_enabled() -> bool:
    return os.environ.get("AIMO_ENV", "dev").strip().lower() not in ("prod", "production")


def demo_tamper(seq: int, summary: str | None = None) -> dict[str, Any]:
    """DEMO ONLY: trust.tamper_for_demo, so the UI can show 'Verify chain' failing. 404 in production."""
    if not _demo_enabled():
        raise ServiceError(404, "not_found", "Not Found")
    t = _trust()
    try:
        res = t.tamper_for_demo(seq, summary=summary)  # type: ignore[union-attr]
    except _MISSING as e:
        raise ServiceError(501, "tamper_unavailable", "trust.tamper_for_demo is not available") from e
    except PermissionError as e:
        raise ServiceError(404, "not_found", "Not Found") from e
    except KeyError as e:
        raise ServiceError(404, "audit_event_not_found", f"no audit event #{seq}") from e
    return {"tampered": True, **(res or {}), "verify": audit_verify()}


def demo_untamper(seq: int) -> dict[str, Any]:
    if not _demo_enabled():
        raise ServiceError(404, "not_found", "Not Found")
    t = _trust()
    try:
        ok = t.untamper_for_demo(seq)  # type: ignore[union-attr]
    except _MISSING as e:
        raise ServiceError(501, "tamper_unavailable", "trust.untamper_for_demo is not available") from e
    except PermissionError as e:
        raise ServiceError(404, "not_found", "Not Found") from e
    except KeyError as e:
        raise ServiceError(404, "audit_event_not_found", f"no audit event #{seq}") from e
    return {"seq": seq, "restored": bool(ok), "verify": audit_verify()}


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
    res: list[ProductSummary] | None = None
    cat = _catalog()
    if cat is not None:
        def primary():
            return cat.search(query, claims=claims or [], category=category, content_unit=content_unit, limit=limit)

        def deterministic():  # FTS only: no Gemini semantic recall
            try:
                from aimo.catalog.textsearch import search as fts

                return fts(query, claims=claims or [], category=category, content_unit=content_unit, limit=limit,
                           semantic=False)
            except (ImportError, TypeError):
                return None
        try:
            res, _path = resilience.guarded("catalog.search", primary, deterministic,
                                            resilience.timeout_for("catalog.search", 4.0))
        except NotImplementedError:
            res = None
    if res is None:
        from aimo.gateway import fallback_catalog

        res = fallback_catalog.search(query, claims=claims or [], category=category, limit=limit)
    faults = current_faults()
    if not faults.inject_text:
        return res
    out: list[ProductSummary] = []
    for s in res:  # injected products come back as Product (a ProductSummary) carrying marketing_text
        if faults._hit(faults.inject_text, s.id):
            try:
                full = _raw_product(s.id)
            except ServiceError:
                full = Product(**s.model_dump())
            out.append(_inject_text(full.model_copy(update={"score": s.score})))
        else:
            out.append(s)
    return out


def _inject_text(p: Product) -> Product:
    text = ((p.marketing_text or "").rstrip() + "\n\n" + INJECTION_TEXT).strip()
    labels = list(dict.fromkeys([*p.labels, "injected:inject_text"]))
    return p.model_copy(update={"marketing_text": text, "labels": labels})


def get_product(product_id: str) -> Product:
    p = _raw_product(product_id)
    faults = current_faults()
    if faults.inject_text and faults._hit(faults.inject_text, str(product_id)):
        p = _inject_text(p)
    return p


def _fallback_screen(text: str) -> dict[str, Any]:
    low = text.lower()
    hits = [k for k in ("ignore your", "ignore previous", "ai agent", "approval already granted", "system note") if k in low]
    return {"suspicious": bool(hits), "reasons": hits}


def screen_supplier_text(p: Product) -> dict[str, Any]:
    """trust.screen_text over untrusted supplier text (marketing + ingredients)."""
    text = " ".join(x for x in (p.marketing_text, p.ingredients_text) if x)
    if not text:
        return {"suspicious": False, "reasons": []}
    t = _trust()
    try:
        fn = t.screen_text  # type: ignore[union-attr]

        def regex_only():
            try:
                return fn(text, use_gemini=False)
            except TypeError:
                return _fallback_screen(text)
        res, _path = resilience.guarded("trust.screen_text", lambda: fn(text), regex_only,
                                        resilience.timeout_for("trust.screen_text", 3.0))
        return res
    except _MISSING:
        low = text.lower()
        hits = [k for k in ("ignore your", "ignore previous", "ai agent", "approval already granted", "system note")
                if k in low]
        return {"suspicious": bool(hits), "reasons": hits}


def _raw_product(product_id: str) -> Product:
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
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _order_lock(key: str) -> threading.Lock:
    """Serialises duplicate submits / double-clicked approvals for one quote or order."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _upstream(e: UpstreamError) -> ServiceError:
    """Agent-facing upstream error: plain language, safe-to-retry, and when."""
    sc = e.status_code
    ra = int(max(1, round(e.retry_after))) if e.retry_after else (5 if sc == 429 else 10)
    msg = str(e)
    if sc == 429:
        code, text = "upstream_rate_limited", (f"Valio Aimo's price and stock service is asking us to slow down (HTTP 429). "
                                              f"Nothing was quoted or ordered. Retry in {ra} s.")
    elif sc == 504 or "timed out" in msg:
        code, text = "upstream_timeout", (f"Valio Aimo's price and stock service did not answer in time. "
                                         f"Nothing was quoted or ordered. Retry in {ra} s.")
    elif "unreachable" in msg or "circuit open" in msg:
        code, text = "upstream_unreachable", (f"Valio Aimo's price and stock service is unreachable and no recorded "
                                             f"snapshot covers this request. Nothing was quoted or ordered. Retry in {ra} s.")
    elif "replay snapshot has no" in msg:
        code, text = "replay_no_data", f"Offline replay has no recorded data for this request: {msg}."
    else:
        code, text = "upstream_unavailable", (f"Valio Aimo's price and stock service returned an error (HTTP {sc}). "
                                             f"Nothing was quoted or ordered. Retry in {ra} s.")
    if e.injected:
        text += " (Simulated fault from X-Aimo-Faults.)"
    status = 503 if sc in (429, 503, 504) or sc >= 500 else 502
    return ServiceError(status, code, text, {"upstream_status": sc, "retry_after": ra, "injected": e.injected,
                                             "safe_to_retry": True, "nothing_committed": True, "cause": msg})


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
        plan, path = resilience.guarded("catalog.plan_need", lambda: cat.plan_need(need), lambda: _plan_deterministic(need),
                                        resilience.timeout_for("catalog.plan_need", 20.0))
        return plan
    except NotImplementedError as e:
        raise ServiceError(501, "plan_unavailable", "planner not implemented yet") from e
    except UpstreamError as e:
        raise _upstream(e) from e


def _plan_deterministic(need: Need) -> Plan:
    """Planner without any Gemini call: FTS candidates (+ usual products, same category, supplier substitution
    rules) -> live offers -> catalog.substitution.rank_substitutes -> catalog.plan.fill. Same tiers and split
    logic as aimo.catalog.plan_need, minus semantic recall and the recipe check."""
    from aimo.catalog import plan as P
    from aimo.catalog.substitution import rank_substitutes
    from aimo.catalog.textsearch import fts_search, get_rows

    n2 = need
    try:
        n2, _src = P.with_effective_query(need)
    except Exception:  # noqa: BLE001
        pass
    rel: dict[str, float] = {}
    for pid, sc in fts_search(n2.product_query, claims=n2.required_claims, content_unit=None, limit=80):
        rel[pid] = sc
    if n2.excluded_allergens:
        for pid, sc in fts_search(n2.product_query, claims=n2.required_claims, free_of=n2.excluded_allergens, limit=80):
            rel[pid] = max(rel.get(pid, -1e9), sc) + 2.0
    pref_rows = get_rows(list(n2.preferred_product_ids))
    for c in {r.get("category") for r in pref_rows.values() if r.get("category")}:
        for pid, sc in fts_search(n2.product_query, claims=n2.required_claims, category=c, limit=80):
            rel[pid] = max(rel.get(pid, -1e9), sc) + 1.0
    for pid in n2.preferred_product_ids:
        rel.setdefault(pid, 100.0)
    rows = get_rows(list(rel))
    order = sorted(rows.values(), key=lambda d: P._static_priority(n2, d, rel.get(d["id"], 0)))
    top = [d["id"] for d in order[:getattr(P, "MAX_OFFER_IDS", 30)]]
    top += [pid for pid in n2.preferred_product_ids if pid in rows and pid not in top]
    offers = get_offers_with_meta(top, n2.delivery_by, n2.customer_id)[0] if top else {}
    full = rank_substitutes(n2, offers, limit=200, rows={pid: rows[pid] for pid in top}, relevance=rel)
    plan = P.fill(n2, P._trim(full, 12))
    note = "Planned with the deterministic planner (Gemini semantic recall and recipe check skipped: slow or unavailable)."
    return plan.model_copy(update={"need": need, "explanation": f"{plan.explanation} {note}".strip()})


# ------------------------------------------------------------------ quotes
def _require_scope(ident: Identity, scope: str) -> Mandate:
    """403 unless the agent's mandate (trust) grants `scope`. Fallback mandates carry the default scopes."""
    mandate = mandate_for(ident)
    if scope not in (mandate.scopes or []):
        audit(ident.actor, "order.refused" if scope == "order:submit" else "request.refused", f"refused-{ident.customer_id}",
              f"Agent {ident.full_agent_id} has no '{scope}' permission from {ident.customer_id}; request refused.",
              {"scope": scope, "agent_id": ident.full_agent_id, "customer_id": ident.customer_id})
        raise ServiceError(403, "scope_missing",
                           f"agent {ident.full_agent_id} is not registered with scope {scope} for {ident.customer_id}")
    mexp = mandate.expires_at
    if mexp and (mexp if mexp.tzinfo else mexp.replace(tzinfo=timezone.utc)) <= _now():
        raise ServiceError(403, "mandate_expired", "agent mandate has expired")
    return mandate


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
    _require_scope(ident, "quote:create")
    if need is not None and need.customer_id != ident.customer_id:
        need = need.model_copy(update={"customer_id": ident.customer_id})

    # Live re-fetch (bypasses the browsing cache): the quote fixes these prices.
    try:
        offers, meta = get_offers_with_meta(list(qty_by), delivery_date, ident.customer_id, qty_by, fresh=True)
    except UpstreamError as e:
        raise _upstream(e) from e

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

    products: dict[str, Product] = {}
    for pid in qty_by:
        try:
            products[pid] = get_product(pid)
        except ServiceError:
            pass

    # Server-side tier + flags. What the agent claimed is kept for the audit, never used.
    from aimo.gateway.verify import verify_lines

    verified = verify_lines(ident.customer_id, delivery_date, qty_by, offers, need, products, screen_supplier_text)
    client_claims = [{"product_id": str(ln["product_id"]), "tier": ln.get("tier"), "flags": list(ln.get("flags") or [])}
                     for ln in lines if ln.get("tier") is not None or ln.get("flags")]

    qlines: list[QuoteLine] = []
    total = gross = 0.0
    for ln in lines:
        pid = str(ln["product_id"])
        o = offers[pid]
        q = float(ln["qty_sales_units"])
        lt = round(q * float(o.unit_price), 2)
        total += lt
        gross += q * float(o.unit_price_gross or o.unit_price)
        v = verified[pid]
        name = products[pid].name if pid in products else _product_name(pid)
        qlines.append(QuoteLine(product_id=pid, product_name=name, qty_sales_units=q, sales_unit=o.sales_unit or "ST",
                                unit_price=float(o.unit_price), line_total=lt, tier=v["tier"], flags=v["flags"]))

    mismatches = []
    for c in client_claims:
        v = verified[c["product_id"]]
        if (c["tier"] is not None and c["tier"] != v["tier"]) or (c["flags"] and set(c["flags"]) != set(v["flags"])):
            mismatches.append({"product_id": c["product_id"], "agent_tier": c["tier"], "verified_tier": v["tier"],
                               "agent_flags": c["flags"], "verified_flags": v["flags"]})

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
    provenance = {
        **meta, "price_locked_until": expires.isoformat(),
        "line_verification": {pid: {"tier": v["tier"], "method": v["method"], "reasons": v["reasons"],
                                    "reference": v.get("reference")}
                              for pid, v in verified.items()},
        "client_claims": client_claims,
    }
    quote = Quote(
        quote_id=_id("Q"), customer_id=ident.customer_id, agent_id=ident.agent_id, delivery_date=delivery_date,
        lines=qlines, total_net=round(total, 2), total_gross=round(gross, 2), created_at=created, expires_at=expires,
        need=need, provenance=provenance, availability_snapshot=snapshot,
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
    if mismatches:
        audit("system:gateway", "quote.claims_corrected", quote.quote_id,
              "Gateway verified the lines itself: " + "; ".join(
                  f"agent said {m['product_id']} is {m['agent_tier'] or 'untiered'}"
                  + (f" with flags {m['agent_flags']}" if m["agent_flags"] else "")
                  + f", gateway verified {m['verified_tier']}" + (f" with {m['verified_flags']}" if m["verified_flags"] else "")
                  for m in mismatches) + ".",
              {"mismatches": mismatches, "quote_hash": quote.quote_hash}, related_ids=[quote.quote_id])
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


def _finalize(order: Order, quote: Quote, actor: str,
              offers_meta: tuple[dict[str, Offer], dict[str, Any]] | None = None) -> Order:
    """Re-check availability and place the order in the mock backend (Peppol order response)."""
    try:
        offers, meta = offers_meta if offers_meta is not None else _quote_offers(quote)
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
                                     "updated_at": _now(), "provenance": {**(order.provenance or {}), **meta}})
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
    with _order_lock(f"quote:{quote_id}"):  # concurrent duplicate submits -> exactly one order
        return _submit_order(ident, quote_id, quote_hash)


def _submit_order(ident: Identity, quote_id: str, quote_hash: str) -> Order:
    quote = get_quote(quote_id)
    # Only the agent that obtained the quote may submit it. Checked before ANY order row exists, so a
    # refused caller can never create the per-quote idempotency record and block the real buyer.
    if quote.customer_id != ident.customer_id:
        audit(ident.actor, "order.refused", quote_id,
              f"{ident.full_agent_id} tried to submit quote {quote_id} of customer {quote.customer_id}; refused (wrong customer).",
              {"reason": "customer_mismatch", "quote_customer": quote.customer_id}, related_ids=[quote_id])
        raise ServiceError(403, "customer_mismatch", "this quote belongs to another customer")
    owner = Identity(agent_id=quote.agent_id, customer_id=quote.customer_id)
    if ident.agent_id != owner.agent_id:
        audit(ident.actor, "order.refused", quote_id,
              f"{ident.full_agent_id} tried to submit quote {quote_id}, which was issued to {owner.full_agent_id}; "
              f"refused: only the quoting agent may order it. No order was created.",
              {"reason": "agent_mismatch", "quote_agent": owner.full_agent_id, "caller": ident.full_agent_id},
              related_ids=[quote_id])
        raise ServiceError(403, "agent_mismatch",
                           f"quote {quote_id} was issued to {owner.full_agent_id}; only that agent may place it")
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

    mandate = _require_scope(ident, "order:submit")  # 403 + audit, no order row

    products: dict[str, Any] = {}
    for ln in quote.lines:
        try:
            products[ln.product_id] = get_product(ln.product_id)
        except ServiceError:
            pass
    decision = evaluate(quote, mandate, products=products or None)
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
    order = order.model_copy(update={"status": "approved", "approved_by": f"policy:{decision.policy_id}"})
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


_BLOCKING = ("cutoff_passed", "sales_block", "out_of_stock", "late_delivery", "availability_not_found", "no_schedule")


def _quote_offers(quote: Quote) -> tuple[dict[str, Offer], dict[str, Any]]:
    qty_by: dict[str, float] = {}
    for ln in quote.lines:
        qty_by[ln.product_id] = qty_by.get(ln.product_id, 0) + ln.qty_sales_units
    return get_offers_with_meta(list(qty_by), quote.delivery_date, quote.customer_id, qty_by, fresh=True)


def _requote_reason(quote: Quote, offers: dict[str, Offer] | None) -> str | None:
    if _expired(quote):
        return "quote_expired"
    for ln in quote.lines:
        o = (offers or {}).get(ln.product_id)
        if o is not None and any(i in o.issues for i in _BLOCKING):
            return "cutoff_passed" if "cutoff_passed" in o.issues else "line_unavailable"
    return None


def _requote_error(old: Order, new_order: Order | None, new_quote: Quote | None, reason: str, note: str = "") -> ServiceError:
    why = {"quote_expired": "the quote's 15-minute price lock expired while it waited for approval",
           "cutoff_passed": "a line's order cut-off passed while the order waited for approval",
           "line_unavailable": "a line can no longer be delivered for the date"}.get(reason, reason)
    detail: dict[str, Any] = {"reason": reason, "old_order_id": old.order_id, "old_quote_id": old.quote_id}
    if new_order is not None and new_quote is not None:
        old_q = store.get_quote(old.quote_id)
        detail.update({
            "new_order": new_order.model_dump(mode="json"), "new_quote": new_quote.model_dump(mode="json"),
            "changes": {"old_total_net": old_q.total_net if old_q else None, "new_total_net": new_quote.total_net,
                        "old_lines": [{"product_id": l.product_id, "qty": l.qty_sales_units, "unit_price": l.unit_price}
                                      for l in (old_q.lines if old_q else [])],
                        "new_lines": [{"product_id": l.product_id, "qty": l.qty_sales_units, "unit_price": l.unit_price,
                                       "tier": l.tier} for l in new_quote.lines]},
            "approve_with": {"method": "POST", "path": f"/v1/approvals/{new_order.order_id}/decision",
                             "body": {"decision": "approve", "quote_hash": new_quote.quote_hash}},
        })
        msg = (f"Re-quote needed: {why}. A fresh quote {new_quote.quote_id} (€{new_quote.total_net:.2f} net) is ready as "
               f"order {new_order.order_id}; approve that one in one step.")
    else:
        msg = f"Re-quote needed: {why}. No automatic replacement was possible{': ' + note if note else ''}; ask the agent to re-plan."
    return ServiceError(409, "requote_needed", msg, detail)


def _requote(order: Order, quote: Quote, reason: str, human: str) -> ServiceError:
    """Build a fresh, re-priced quote for the same lines (or a re-plan from the need) as a new pending order."""
    owner = Identity(agent_id=quote.agent_id, customer_id=quote.customer_id)
    lines = [{"product_id": l.product_id, "qty_sales_units": l.qty_sales_units} for l in quote.lines]
    new_q: Quote | None = None
    note = ""
    try:
        new_q = make_quote(owner, quote.delivery_date, lines, quote.need)
    except ServiceError as e:
        note = e.message
        if e.status == 409 and quote.need is not None:
            try:
                plan = plan_need(quote.need, identity=owner)
                if plan.lines:
                    new_q = make_quote(owner, quote.delivery_date,
                                       [{"product_id": l.product_id, "qty_sales_units": l.qty_sales_units} for l in plan.lines],
                                       quote.need)
            except ServiceError as e2:
                note = e2.message
    new_order = None
    if new_q is not None:
        products = {}
        for ln in new_q.lines:
            try:
                products[ln.product_id] = get_product(ln.product_id)
            except ServiceError:
                pass
        decision = evaluate(new_q, mandate_for(owner), products=products or None)
        now = _now()
        status = "rejected" if decision.outcome == "reject" else "pending_approval"  # a human is reviewing: keep it with them
        new_order = Order(order_id=_id("ORD"), quote_id=new_q.quote_id, quote_hash=new_q.quote_hash or "",
                          customer_id=new_q.customer_id, agent_id=owner.agent_id, status=status, decision=decision,
                          created_at=now, updated_at=now,
                          provenance={"supersedes": order.order_id, "requote_reason": reason})
        store.save_order(new_order)
        if not _trust_log("log_decision", new_q, decision, new_order.order_id):
            audit("system:policy-engine", "policy.evaluated", new_order.order_id,
                  f"Policy {decision.policy_id} on re-quote {new_q.quote_id}: {decision.outcome}.",
                  {"quote_id": new_q.quote_id, "decision": decision.model_dump()}, related_ids=[new_q.quote_id])
    old = order.model_copy(update={"status": "expired", "updated_at": _now(), "provenance": {
        **(order.provenance or {}), "requote_reason": reason,
        "requoted_as": new_order.order_id if new_order else None, "requote_quote_id": new_q.quote_id if new_q else None}})
    store.save_order(old)
    audit("system:gateway", "order.requoted", order.order_id,
          (f"Order {order.order_id} could not be approved ({reason.replace('_', ' ')}); the gateway re-priced it as quote "
           f"{new_q.quote_id} (€{quote.total_net:.2f} -> €{new_q.total_net:.2f} net), new order {new_order.order_id} "
           f"is waiting for approval." if new_q and new_order else
           f"Order {order.order_id} could not be approved ({reason.replace('_', ' ')}) and no automatic re-quote was "
           f"possible: {note}"),
          {"reason": reason, "old_quote_id": quote.quote_id, "new_quote_id": new_q.quote_id if new_q else None,
           "new_order_id": new_order.order_id if new_order else None, "requested_by": human},
          related_ids=[x for x in (quote.quote_id, new_q.quote_id if new_q else None,
                                   new_order.order_id if new_order else None) if x])
    return _requote_error(old, new_order, new_q, reason, note)


def _existing_requote(order: Order) -> ServiceError | None:
    prov = order.provenance or {}
    if order.status != "expired" or "requote_reason" not in prov:
        return None
    new_order = store.get_order(prov["requoted_as"]) if prov.get("requoted_as") else None
    new_q = store.get_quote(prov["requote_quote_id"]) if prov.get("requote_quote_id") else None
    return _requote_error(order, new_order, new_q, prov["requote_reason"])


def _remember(order: Order, quote: Quote, approver: str, role: str, scope: str, note: str | None) -> dict[str, Any]:
    """Learned approvals: tell trust to remember the approved swaps (aimo.trust.learned.remember_swap)."""
    try:
        from aimo.trust import learned  # type: ignore[attr-defined]

        fn = learned.remember_swap
    except (ImportError, AttributeError):
        return {"remembered": False, "reason": "aimo.trust.learned is not available yet"}
    verif = (quote.provenance or {}).get("line_verification") or {}
    done, errors = [], []
    for ln in quote.lines:
        if ln.tier == "preferred":
            continue
        ref = (verif.get(ln.product_id) or {}).get("reference")
        kw = dict(customer_id=quote.customer_id, from_product_id=ref, to_product_id=ln.product_id, scope=scope,
                  approver=approver, approver_role=role or None, quote_id=quote.quote_id, order_id=order.order_id,
                  note=note)
        try:
            try:
                res = fn(**kw)
            except TypeError:
                res = fn(quote.customer_id, ref, ln.product_id, scope)
            done.append({"product_id": ln.product_id, "from_product_id": ref, "scope": scope,
                         "result": res if isinstance(res, (dict, list, str, bool, type(None))) else str(res)})
        except Exception as e:  # noqa: BLE001  learning must never block the order
            errors.append({"product_id": ln.product_id, "error": str(e)})
    return {"remembered": bool(done), "swaps": done, "errors": errors}


def learned_for(customer_id: str) -> dict[str, Any]:
    try:
        from aimo.trust import learned  # type: ignore[attr-defined]
    except ImportError:
        return {"customer_id": customer_id, "available": False, "swaps": []}
    for fn in ("list_learned", "learned_for", "get_learned", "list_swaps"):
        f = getattr(learned, fn, None)
        if f is not None:
            try:
                res = f(customer_id)
            except Exception as e:  # noqa: BLE001
                return {"customer_id": customer_id, "available": True, "swaps": [], "error": str(e)}
            return {"customer_id": customer_id, "available": True, "swaps": json.loads(json.dumps(res, default=str))}
    return {"customer_id": customer_id, "available": False, "swaps": []}


def decide(order_id: str, decision: str, approver: str, quote_hash: str, note: str | None = None,
           approver_role: str | None = None, remember: bool = False, remember_scope: str = "product") -> Order:
    if decision not in ("approve", "reject"):
        raise ServiceError(400, "bad_decision", "decision must be 'approve' or 'reject'")
    if not approver:
        raise ServiceError(400, "approver_required", "approver is required")
    if remember_scope not in ("product", "product_type"):
        raise ServiceError(400, "bad_remember_scope", "remember_scope must be 'product' or 'product_type'")
    with _order_lock(order_id):
        order = get_order(order_id)
        if order.status != "pending_approval":
            prev = store.approvals_for(order_id)
            if prev and prev[-1]["decision"] == decision and prev[-1]["approver"] == approver \
                    and prev[-1]["quote_hash"] == quote_hash and order.status in ("placed", "rejected", "approved"):
                return order  # duplicate click: same decision, same approver, same quote -> same result
            rq = _existing_requote(order)
            if rq is not None and decision == "approve":
                raise rq
            raise ServiceError(409, "not_pending", f"order is {order.status}, not pending_approval")
        quote = get_quote(order.quote_id)
        role = approver_role or _directory_role(order.customer_id, approver) or order.decision.approver_role or ""
        human = f"human:{approver}"

        t = _trust()
        try:
            chk = t.check_approval(quote, order.decision, approver, role, quote_hash,  # type: ignore[union-attr]
                                   decision_kind=decision, note=note or "", order_id=order_id, now=_now())
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
        offers_meta = None
        if decision == "approve":
            if not _expired(quote):
                try:
                    offers_meta = _quote_offers(quote)
                except UpstreamError as e:
                    raise _upstream(e) from e
            reason = _requote_reason(quote, offers_meta[0] if offers_meta else None)
            if reason:
                raise _requote(order, quote, reason, human)
        if chk is not None and not chk.get("ok"):
            raise ServiceError(403, "approval_refused", chk.get("reason") or "approval refused by trust layer")

        store.record_approval(order_id, decision, approver, note, quote_hash)
        who = f"{approver} ({role})" if role else approver  # wording for fallback audit only
        if decision == "reject":
            order = order.model_copy(update={"status": "rejected", "approved_by": approver, "approver_role": role or None,
                                             "approval_note": note, "updated_at": _now()})
            store.save_order(order)
            _log_order(order, quote, human, note or f"rejected by {who}", "order.rejected_by_human",
                       f"{who} rejected order {order_id}." + (f" Note: {note}" if note else ""), {"quote_hash": quote_hash})
            return order
        learned_res = _remember(order, quote, approver, role, remember_scope, note) if remember else None
        prov = dict(order.provenance or {})
        if learned_res is not None:
            prov["learned"] = learned_res
        order = order.model_copy(update={"status": "approved", "approved_by": approver, "approver_role": role or None,
                                         "approval_note": note, "updated_at": _now(), "provenance": prov or None})
        store.save_order(order)
        _log_order(order, quote, human, note or "", "order.approved",
                   f"{who} approved order {order_id} (quote {quote.quote_id}, €{quote.total_net:.2f}, hash {quote_hash[:12]}…)."
                   + (f" Note: {note}" if note else ""), {"quote_hash": quote_hash})
        if learned_res is not None:
            audit(human, "approval.remembered" if learned_res.get("remembered") else "approval.remember_failed", order_id,
                  (f"{approver} asked to remember this approval ({remember_scope}): "
                   + (", ".join(f"{s['from_product_id'] or 'usual'} -> {s['product_id']}" for s in learned_res.get("swaps", []))
                      or learned_res.get("reason", "nothing to remember (no substitutes)"))),
                  learned_res, related_ids=[quote.quote_id])
        return _finalize(order, quote, human, offers_meta)


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
    """The authenticated identity (`identity`: HTTP headers / MCP session / transport) wins. A
    customer_id / agent_id argument that disagrees with it is refused (403), never silently used."""
    if isinstance(identity, Identity):
        base = identity
    elif isinstance(identity, dict) and (identity.get("customer_id") or identity.get("agent_id")):
        base = Identity(agent_id=identity.get("agent_id") or DEFAULT_AGENT,
                        customer_id=identity.get("customer_id") or DEFAULT_CUSTOMER)
    else:
        return Identity(agent_id=agent_id or DEFAULT_AGENT, customer_id=customer_id or DEFAULT_CUSTOMER)
    return claim_identity(base, customer_id, agent_id)


def claim_identity(auth: Identity, customer_id: str | None, agent_id: str | None) -> Identity:
    """403 if a claimed customer/agent (e.g. in a request body) disagrees with the authenticated one."""
    if customer_id and customer_id.strip() != auth.customer_id:
        raise ServiceError(403, "customer_mismatch",
                           f"request names customer {customer_id!r} but the caller is authenticated as {auth.customer_id!r}")
    if agent_id and normalize_agent_id(auth.customer_id, agent_id) != auth.agent_id:
        raise ServiceError(403, "agent_mismatch",
                           f"request names agent {agent_id!r} but the caller is authenticated as {auth.full_agent_id!r}")
    return auth


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


def list_policies(identity: Any = None, **_: Any) -> dict[str, Any]:
    ident = resolve_identity(identity)
    return {"policies": _policies(), "learned_swaps": learned_for(ident.customer_id)}
