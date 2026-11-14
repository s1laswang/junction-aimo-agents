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
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from aimo.models import Need, Quote, QuoteLine

log = logging.getLogger("aimo.agents.seller")

SELLER_ORG = "valio-aimo"
DEFAULT_SELLER = f"{SELLER_ORG}/seller-1"

_SCHEMA = """create table if not exists port_proposals (
  proposal_id text primary key, customer_id text not null, status text not null, created_at text not null,
  body text not null)"""


class ProposalError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _conn():
    from aimo.backend import orders as store
    c = store.conn()
    c.execute(_SCHEMA)
    return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seller_id(agent_id: Optional[str]) -> str:
    """Proposals are always made in Valio Aimo's name: `valio-aimo/<agent>`; anything else -> seller-1."""
    a = (agent_id or "").strip()
    return a if a.startswith(SELLER_ORG + "/") and len(a) > len(SELLER_ORG) + 1 else DEFAULT_SELLER


def is_seller(agent_id: Optional[str]) -> bool:
    return (agent_id or "").strip().startswith(SELLER_ORG + "/")


# ------------------------------------------------------------------ usual items
def usual_items(customer_id: str) -> list[dict[str, Any]]:
    """The customer's usual items: trust policy preferred_products + the standing order (drift seeds)."""
    items: dict[str, dict[str, Any]] = {}
    try:
        from aimo.gateway.verify import usual_products
        for pid in usual_products(customer_id):
            items[pid] = {"product_id": pid, "weekly_qty": None}
    except Exception:  # noqa: BLE001
        pass
    from aimo.agents.drift import SEEDS
    for l in SEEDS.get(customer_id, {}).get("lines", []):
        items[l["product_id"]] = {"product_id": l["product_id"], "weekly_qty": float(l["weeks"][-1])}
    return list(items.values())


def _excluded(customer_id: str) -> list[str]:
    try:
        from aimo.gateway.verify import policy_excluded_allergens
        return list(policy_excluded_allergens(customer_id))
    except Exception:  # noqa: BLE001
        return []


def _per_unit(offer: Any, content: Optional[float]) -> Optional[float]:
    """EUR per L / KG: the API's comparison price, else unit price / content."""
    if offer is None or offer.unit_price is None:
        return None
    if offer.comparison_price:
        return float(offer.comparison_price)
    return float(offer.unit_price) / content if content else None


def find_cheaper(customer_id: str, delivery: date, offers_fn: Optional[Callable] = None,
                 only: Optional[str] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Momin's `explain`/`rank` on our catalog: for each usual item, allergen-safe, orderable, same-spec
    candidates (our good/better/best ranking) that beat the usual item on price per L/KG."""
    from aimo.catalog import get_product
    from aimo.catalog.plan import find_candidates
    from aimo.catalog.substitution import rank_substitutes

    excluded = _excluded(customer_id)
    needs: list[tuple[dict[str, Any], Any, Need, dict, dict]] = []
    want: list[str] = []
    for it in usual_items(customer_id):
        if only and it["product_id"] != only:
            continue
        p = get_product(it["product_id"])
        if p is None or p.content_unit not in ("L", "KG", "PCS") or not p.content_amount:
            continue
        units = it["weekly_qty"] or 1.0
        need = Need(customer_id=customer_id, product_query=p.name, quantity=units * p.content_amount,
                    unit=p.content_unit, delivery_by=delivery, preferred_product_ids=[p.id],
                    excluded_allergens=excluded, required_claims=[c for c in p.claims if c.startswith("FREE_FROM_")])
        rows, rel = find_candidates(need)
        top = [pid for pid, _ in sorted(rel.items(), key=lambda kv: -kv[1])[:25]]
        if p.id not in top:
            top.append(p.id)
        want += top
        needs.append((it, p, need, {k: rows[k] for k in top if k in rows}, rel))
    if not want:
        return [], ["no usual items on file for this customer"]
    ids = list(dict.fromkeys(want))
    if offers_fn is not None:
        offers = offers_fn(ids, delivery, customer_id=customer_id)
    else:
        from aimo.backend.client import get_offers_with_meta  # one batched call for all usual items
        offers, _ = get_offers_with_meta(ids, delivery, customer_id)
    props, notes = [], []
    for it, p, need, rows, rel in needs:
        base = offers.get(p.id)
        base_pu = _per_unit(base, p.content_amount)
        cands = rank_substitutes(need, {k: v for k, v in offers.items() if k in rows}, limit=40, rows=rows,
                                 relevance=rel)
        best = None
        for c in cands:
            if c.product.id == p.id or c.tier not in ("best", "better") or c.offer is None or not c.offer.available:
                continue
            if c.product.content_unit != p.content_unit or not c.product.content_amount:
                continue
            units = -(-need.quantity // c.product.content_amount)
            if c.offer.available_amount is not None and c.offer.available_amount < units:
                continue
            pu = _per_unit(c.offer, c.product.content_amount)
            if pu is None:
                continue
            reasons, score = [], 0.0
            if base is None or not base.available:
                reasons.append(f"your usual {p.name} is not orderable for {delivery:%a %d %b}"
                               + (f" ({', '.join(base.issues)})" if base is not None and base.issues else ""))
                score += 3
            if base_pu and pu < base_pu:
                save = (base_pu - pu) * need.quantity
                reasons.append(f"€{pu:.2f} vs €{base_pu:.2f} per {p.content_unit}, saves about €{save:.2f} a week "
                               f"on {need.quantity:g} {p.content_unit}")
                score += (base_pu - pu) / base_pu
            br, cr = p.delivery_reliability, c.product.delivery_reliability
            if br is not None and cr is not None and cr > br:
                reasons.append(f"delivery reliability {cr:.2f} vs {br:.2f}")
                score += cr - br
            if p.co2_kg_per_kg and c.product.co2_kg_per_kg is not None and c.product.co2_kg_per_kg < p.co2_kg_per_kg:
                reasons.append(f"CO2 {c.product.co2_kg_per_kg:.2f} vs {p.co2_kg_per_kg:.2f} kg CO2e/kg")
                score += 0.1
            if not reasons or (base_pu and pu >= base_pu and score < 3):
                continue
            if best is None or score > best[0]:
                best = (score, c, units, reasons, (base_pu - pu) * need.quantity if base_pu else None)
        if best is None:
            notes.append(f"your {p.name} is already the best offer we found for {delivery:%a %d %b}")
            continue
        score, c, units, reasons, save = best
        props.append({"score": round(score, 4), "replaces": {"product_id": p.id, "name": p.name},
                      "product": c.product, "offer": c.offer, "tier": c.tier, "qty_sales_units": float(units),
                      "why": [f"{c.product.name}: " + "; ".join(reasons), *c.reasons[:1]],
                      "estimated_saving_eur": round(save, 2) if save else None})
    props.sort(key=lambda x: -x["score"])
    return props, notes


# ------------------------------------------------------------------ policy preview
def policy_preview(customer_id: str, delivery: date, lines: list[dict[str, Any]], offers: dict[str, Any]) -> dict:
    """The customer's own policy on a DRAFT of the quote the buyer would get (unsigned, not stored, not binding).
    Same verification (server-side tiers/flags) and same trust.evaluate as POST /v1/quotes + /v1/orders."""
    from aimo.gateway import service
    from aimo.gateway.verify import verify_lines

    qty_by = {l["product_id"]: float(l["qty_sales_units"]) for l in lines}
    products = {}
    for pid in qty_by:
        try:
            products[pid] = service.get_product(pid)
        except Exception:  # noqa: BLE001
            pass
    verified = verify_lines(customer_id, delivery, qty_by, offers, None, products, service.screen_supplier_text)
    qlines = []
    for pid, q in qty_by.items():
        o = offers[pid]
        qlines.append(QuoteLine(product_id=pid, product_name=products[pid].name if pid in products else pid,
                                qty_sales_units=q, sales_unit=o.sales_unit or "ST", unit_price=float(o.unit_price),
                                line_total=round(q * float(o.unit_price), 2), tier=verified[pid]["tier"],
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


# ------------------------------------------------------------------ proposals
def _save(p: dict[str, Any]) -> None:
    _conn().execute("insert or replace into port_proposals values (?,?,?,?,?)",
                    (p["proposal_id"], p["customer_id"], p["status"], p["created_at"], json.dumps(p, default=str)))


def get_proposal(proposal_id: str) -> dict[str, Any]:
    r = _conn().execute("select body from port_proposals where proposal_id=?", (proposal_id,)).fetchone()
    if r is None:
        raise ProposalError(404, "proposal_not_found", f"proposal {proposal_id!r} not found")
    return json.loads(r[0])


def list_proposals(customer_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
    q, args = "select body from port_proposals where 1=1", []
    if customer_id:
        q, args = q + " and customer_id=?", [*args, customer_id]
    if status and status != "all":
        q, args = q + " and status=?", [*args, status]
    return [json.loads(r[0]) for r in _conn().execute(q + " order by created_at desc", args).fetchall()]


def _audit(actor: str, action: str, subject: str, summary: str, data: dict, related: Optional[list[str]] = None):
    try:
        from aimo.gateway import service
        service.audit(actor, action, subject, summary, data, related_ids=related)
    except Exception:  # noqa: BLE001
        log.exception("proposal audit failed")


def propose_offer(customer_id: str, *, product_id: Optional[str] = None, qty_sales_units: Optional[float] = None,
                  replaces_product_id: Optional[str] = None, delivery_date: Any = None, note: str = "",
                  agent_id: Optional[str] = None, offers_fn: Optional[Callable] = None) -> dict[str, Any]:
    """Create a proposal. Without product_id the seller picks a cheaper equivalent of a usual item
    (kind `cheaper_equivalent`); with product_id it proposes that product (kind `seller_pick`, e.g. a seasonal item)."""
    from aimo.gateway import service

    seller = seller_id(agent_id)
    try:
        service.mandate_for(service.Identity(agent_id="buyer-1", customer_id=customer_id))
    except service.ServiceError as e:
        raise ProposalError(404, "unknown_customer", f"unknown customer {customer_id!r}") from e
    if delivery_date:
        delivery = date.fromisoformat(str(delivery_date)[:10])
    else:
        from aimo.agents.drift import SEEDS, next_delivery
        delivery = next_delivery(SEEDS.get(customer_id, {}).get("weekday", 3))
    if product_id:
        from aimo.catalog import get_product
        p = get_product(str(product_id))
        if p is None:
            raise ProposalError(404, "product_not_found", f"product {product_id!r} not in the catalog")
        if offers_fn is not None:
            offers = offers_fn([p.id], delivery, customer_id=customer_id)
        else:
            from aimo.backend.client import get_offers_with_meta
            offers, _ = get_offers_with_meta([p.id], delivery, customer_id)
        o = offers.get(p.id)
        if o is None or not o.available or o.unit_price is None:
            raise ProposalError(409, "not_orderable", f"{p.name} is not orderable for {delivery}",
                                {"issues": o.issues if o is not None else ["no_offer"]})
        cand = {"product": p, "offer": o, "tier": None, "qty_sales_units": float(qty_sales_units or 1),
                "why": [f"{p.name}: proposed by the seller" + (f" ({note})" if note else "")],
                "replaces": {"product_id": replaces_product_id, "name": None} if replaces_product_id else None,
                "estimated_saving_eur": None}
        kind = "seller_pick"
    else:
        props, notes = find_cheaper(customer_id, delivery, offers_fn, only=replaces_product_id)
        if not props:
            raise ProposalError(404, "no_better_offer", "no better offer found for this customer's usual items",
                                {"notes": notes})
        cand, kind = props[0], "cheaper_equivalent"
        offers = {cand["product"].id: cand["offer"]}
        if qty_sales_units:
            cand["qty_sales_units"] = float(qty_sales_units)
    p, o = cand["product"], cand["offer"]
    lines = [{"product_id": p.id, "qty_sales_units": cand["qty_sales_units"]}]
    try:
        preview = policy_preview(customer_id, delivery, lines, offers)
    except Exception as e:  # noqa: BLE001  never block a proposal on the preview; the real check runs at accept
        log.warning("policy preview failed: %s", e)
        preview = {"binding": False, "outcome": "unknown", "error": str(e)[:200]}
    pid = f"P-{_now():%Y%m%d}-{uuid.uuid4().hex[:8]}"
    prop = {
        "proposal_id": pid, "customer_id": customer_id, "seller_id": seller, "kind": kind, "status": "proposed",
        "created_at": _now().isoformat(timespec="seconds"), "delivery_date": delivery.isoformat(),
        "product": {"product_id": p.id, "name": p.name, "brand": p.brand, "sales_unit": p.sales_unit,
                    "content_amount": p.content_amount, "content_unit": p.content_unit},
        "replaces": cand.get("replaces"), "qty_sales_units": cand["qty_sales_units"],
        "offer": {"unit_price": o.unit_price, "comparison_price": o.comparison_price,
                  "comparison_unit": o.comparison_unit, "available_amount": o.available_amount,
                  "fetched_at": o.fetched_at.isoformat() if o.fetched_at else None},
        "why": cand["why"], "estimated_saving_eur": cand.get("estimated_saving_eur"), "note": note or None,
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
           f"Seller agent {seller} proposed {cand['qty_sales_units']:g} x {p.name} ({p.id}) to {customer_id}"
           + (f" instead of {prop['replaces']['product_id']}" if prop.get("replaces") else "")
           + f" for {delivery:%a %d %b}. Policy preview (not binding): {preview.get('outcome')}"
           + (f", approver {preview.get('approver_role')}" if preview.get("approver_role") else "")
           + ". The seller cannot order; the buyer decides.",
           {"customer_id": customer_id, "product_id": p.id, "kind": kind, "policy_preview": preview.get("outcome")})
    return prop


def accept_proposal(proposal_id: str, identity: Any) -> dict[str, Any]:
    """Buyer side only. Runs the normal quote -> order path under the BUYER's identity and mandate."""
    from aimo.gateway import service

    prop = get_proposal(proposal_id)
    if is_seller(identity.agent_id) or is_seller(getattr(identity, "full_agent_id", "")):
        _audit(f"agent:{identity.agent_id}", "proposal.accept_refused", proposal_id,
               f"Seller agent {identity.agent_id} tried to accept proposal {proposal_id}; refused: a seller can "
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
    _audit(identity.actor, "proposal.accepted", proposal_id,
           f"{identity.full_agent_id} accepted proposal {proposal_id} from {prop['seller_id']}: quote {quote.quote_id}, "
           f"order {order.order_id} is {order.status} under {prop['customer_id']}'s policy.",
           {"quote_id": quote.quote_id, "order_id": order.order_id, "status": order.status},
           related=[quote.quote_id, order.order_id])
    return {"proposal": prop, "quote": quote.model_dump(mode="json"), "order": order.model_dump(mode="json")}


def decline_proposal(proposal_id: str, identity: Any, reason: str = "") -> dict[str, Any]:
    prop = get_proposal(proposal_id)
    if is_seller(identity.agent_id) or identity.customer_id != prop["customer_id"]:
        raise ProposalError(403, "not_your_proposal", "only the customer's side can decline")
    if prop["status"] != "proposed":
        raise ProposalError(409, "not_open", f"proposal is {prop['status']}")
    prop.update(status="declined", declined_by=f"agent:{identity.full_agent_id}", decline_reason=reason or None)
    _save(prop)
    _audit(identity.actor, "proposal.declined", proposal_id,
           f"{identity.full_agent_id} declined proposal {proposal_id}" + (f": {reason}" if reason else "."), {})
    return prop
