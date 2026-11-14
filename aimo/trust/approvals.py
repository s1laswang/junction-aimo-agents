"""Human approvals bound to the exact signed quote, plus helpers that write consistent, readable
audit events for the gateway and buyer (log_request, log_quote, log_decision, log_order)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from aimo.models import Need, Order, PolicyDecision, Quote
from aimo.trust.audit import AuditLog, audit
from aimo.trust.policy import day_text, eur, load_policy, qty_text, role_text
from aimo.trust.signing import verify_quote_detailed


def _log(audit_log: Optional[AuditLog]) -> AuditLog:
    return audit_log or audit


def _approver_role_in_directory(policy: dict, approver: str) -> Optional[str]:
    for a in policy.get("approvers", []):
        if approver in (a.get("id"), a.get("name")):
            return a.get("role")
    return None


def check_approval(quote: Quote, decision: PolicyDecision, approver: str, approver_role: str, quote_hash: str, *,
                   decision_kind: str = "approve", note: str = "", order_id: Optional[str] = None,
                   now: Optional[datetime] = None, audit_log: Optional[AuditLog] = None) -> dict:
    """Validate a human approval (or rejection) of an order.
    Approve requires: the policy said needs_approval; quote_hash equals the signed quote's hash and the
    signature verifies; the quote has not expired; approver_role is the role the decision names (or an
    override role); a known approver's directory role matches. Reject only needs a valid role and hash.
    Returns {"ok": bool, "reason": str}. Every call is audited."""
    reasons: list[str] = []
    try:
        pol = load_policy(decision.policy_id)
    except KeyError:
        pol = {"approver_roles": [], "override_roles": []}
    allowed = {r for r in [decision.approver_role, *pol.get("override_roles", [])] if r}
    if decision_kind == "reject":
        allowed |= set(pol.get("approver_roles", []))
    if not approver:
        reasons.append("no approver identity given")
    dir_role = _approver_role_in_directory(pol, approver) if approver else None
    if dir_role and dir_role != approver_role:
        reasons.append(f"{approver} is registered as {role_text(dir_role)}, not {role_text(approver_role)}")
    if approver_role not in allowed:
        reasons.append(f"a {role_text(approver_role)} cannot decide this order (needs: "
                       f"{' or '.join(role_text(r) for r in sorted(allowed)) or 'nobody'})")
    v = verify_quote_detailed(quote, now=now)
    if quote_hash != quote.quote_hash or quote_hash != v["quote_hash"]:
        reasons.append("the approval is for a different version of the quote (hash mismatch); re-review the current quote")
    elif not v["ok"]:
        reasons.append("the quote signature does not verify: " + "; ".join(r for r in v["reasons"] if "expired" not in r))
    if decision_kind == "approve":
        if decision.outcome == "reject":
            reasons.append("the policy rejected this order; a hard rule cannot be overridden by approval")
        elif decision.outcome == "auto_approve":
            pass  # nothing to approve; allowed (idempotent)
        if v["expired"]:
            reasons.append("the quote's price lock has expired; request a fresh quote and approve that")
    ok = not reasons
    reason = "ok" if ok else "; ".join(reasons)
    action = ("approval.verified" if ok else "approval.refused")
    verb = "approved" if decision_kind == "approve" else "rejected"
    who = f"{approver} ({role_text(approver_role)})"
    summary = (f"{who} {verb} quote {quote.quote_id} ({eur(quote.total_net)}), bound to hash {quote_hash[:12]}…"
               + (f": \"{note}\"" if note else ".")) if ok else \
        f"{who} tried to {decision_kind} quote {quote.quote_id} but it was refused: {reason}."
    _log(audit_log).append(f"human:{approver or 'unknown'}", action, order_id or quote.quote_id, summary,
                           {"approver": approver, "approver_role": approver_role, "decision": decision_kind,
                            "note": note, "quote_id": quote.quote_id, "quote_hash": quote_hash, "ok": ok,
                            "reason": reason},
                           related_ids=[quote.quote_id] if order_id else [])
    return {"ok": ok, "reason": reason}


# ------------------------------------------------------------------ audit helpers (consistent wording)
def agent_actor(customer_id: str, agent_id: str) -> str:
    return f"agent:{agent_id if '/' in agent_id else f'{customer_id}/{agent_id}'}"


def log_request(need: Need, agent_id: str, run_id: str, audit_log: Optional[AuditLog] = None):
    actor = agent_actor(need.customer_id, agent_id)
    extra = ""
    if need.excluded_allergens:
        extra = f", excluding {', '.join(need.excluded_allergens)}"
    summary = (f"Buyer agent {actor.split(':', 1)[1]} asked for {qty_text(need.quantity)} {need.unit} "
               f"{need.product_query} for {day_text(need.delivery_by)}{extra}.")
    return _log(audit_log).append(actor, "request.received", run_id, summary, {"need": need.model_dump(mode="json")})


def log_quote(quote: Quote, run_id: Optional[str] = None, audit_log: Optional[AuditLog] = None):
    items = "; ".join(f"{qty_text(ln.qty_sales_units)} × {ln.product_name} at {eur(ln.unit_price)}" for ln in quote.lines[:3])
    if len(quote.lines) > 3:
        items += f"; +{len(quote.lines) - 3} more"
    exp = quote.expires_at if quote.expires_at.tzinfo else quote.expires_at.replace(tzinfo=timezone.utc)
    from aimo.trust.audit import _local
    summary = (f"Valio Aimo issued quote {quote.quote_id} to {quote.agent_id}: {items} = {eur(quote.total_net)} net, "
               f"delivery {day_text(quote.delivery_date)}, price locked until {_local(exp, '%H:%M')}"
               + (f", signed (hash {quote.quote_hash[:12]}…)." if quote.quote_hash else "."))
    return _log(audit_log).append("system:gateway", "quote.created", quote.quote_id, summary,
                                  {"quote": quote.model_dump(mode="json"), "quote_hash": quote.quote_hash,
                                   "customer_id": quote.customer_id, "total_net": quote.total_net},
                                  related_ids=[run_id] if run_id else [])


def log_decision(quote: Quote, decision: PolicyDecision, order_id: Optional[str] = None,
                 audit_log: Optional[AuditLog] = None):
    flagged = [h for h in decision.hits if h.outcome != "allow"]
    head = {"auto_approve": "AUTO-APPROVED", "needs_approval": f"NEEDS APPROVAL by a {role_text(decision.approver_role)}",
            "reject": "REJECTED"}[decision.outcome]
    if flagged:
        why = flagged[0].message
        if len(flagged) > 1:
            why += " Also: " + ", ".join(dict.fromkeys(h.rule_id.split(":")[0].replace("_", " ") for h in flagged[1:])) + "."
    else:
        why = f"All {len(decision.hits)} checks passed."
    summary = f"Policy {decision.policy_id} v{decision.policy_version} evaluated quote {quote.quote_id}: {head}. {why}"
    return _log(audit_log).append("system:policy-engine", "policy.evaluated", order_id or quote.quote_id, summary,
                                  {"decision": decision.model_dump(mode="json"), "quote_id": quote.quote_id,
                                   "quote_hash": quote.quote_hash},
                                  related_ids=[quote.quote_id] if order_id else [])


def log_order(order: Order, quote: Optional[Quote] = None, actor: str = "system:gateway", note: str = "",
              audit_log: Optional[AuditLog] = None):
    """Action order.<status>. order.placed events carry customer_id and total_net (used by period_spend)."""
    total = quote.total_net if quote else None
    words = {"pending_approval": f"is waiting for a {role_text(order.decision.approver_role)} to approve",
             "approved": f"was approved by {order.approved_by}", "placed": "was placed with Valio Aimo",
             "rejected": "was rejected", "expired": "expired before approval", "failed": "failed"}
    summary = f"Order {order.order_id} " + words.get(order.status, order.status)
    if total is not None:
        summary += f" ({eur(total)} net)"
    if order.response_code:
        summary += f"; supplier response {order.response_code} " + {"AP": "(accepted)", "CA": "(accepted with changes)",
                                                                     "RE": "(rejected)"}[order.response_code]
    summary += f": {note}" if note else "."
    data: dict[str, Any] = {"order": order.model_dump(mode="json"), "customer_id": order.customer_id,
                            "quote_id": order.quote_id, "quote_hash": order.quote_hash}
    if total is not None:
        data["total_net"] = total
    if order.approved_by:
        data["approver"] = order.approved_by
    return _log(audit_log).append(actor, f"order.{order.status}", order.order_id, summary, data,
                                  related_ids=[order.quote_id])
