"""Human approvals bound to the exact signed quote, plus helpers that write consistent, readable
audit events for the gateway and buyer (log_request, log_quote, log_decision, log_order)."""
from __future__ import annotations

from datetime import date, datetime, timezone
import math
from typing import Any, Optional

from aimo.models import Need, Order, PolicyDecision, Quote
from aimo.trust.audit import AuditLog, audit
from aimo.trust.policy import day_text, eur, load_policy, policy_ids, qty_text, role_text
from aimo.trust.signing import _to_aware_utc, verify_quote_detailed

RESPONSE_LABELS = {
    "AP": "(accepted)",
    "CA": "(accepted with changes)",
    "RE": "(rejected)",
    "AB": "(acknowledged)",
}


def _log(audit_log: Optional[AuditLog]) -> AuditLog:
    return audit_log or audit


def _safe_role_text(role: Any) -> str:
    return role_text(role if isinstance(role, str) and role else None)


def _finite_number(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
        return float(val)
    return None


def _safe_eur(val: Any) -> str:
    num = _finite_number(val)
    return eur(num if num is not None else 0.0)


def _safe_qty_text(val: Any) -> str:
    num = _finite_number(val)
    return qty_text(num) if num is not None else str(val)


def _safe_day_text(val: Any) -> str:
    if isinstance(val, (date, datetime)):
        return day_text(val)
    return str(val) if val is not None else "unknown"


def _json_safe(val: Any) -> Any:
    if hasattr(val, "model_dump"):
        try:
            val = val.model_dump(mode="json", warnings=False)
        except TypeError:
            try:
                val = val.model_dump(mode="json")
            except Exception:
                val = str(val)
        except Exception:
            val = str(val)
    if isinstance(val, dict):
        return {str(k): _json_safe(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_safe(v) for v in val]
    if isinstance(val, bool) or val is None or isinstance(val, (int, str)):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else str(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val)


def _approver_role_in_directory(policy: dict, approver: str) -> Optional[str]:
    if not isinstance(policy, dict) or not isinstance(approver, str) or not approver:
        return None
    entries = policy.get("approvers", [])
    if not isinstance(entries, list):
        return None
    for a in entries:
        if isinstance(a, dict) and approver in (a.get("id"), a.get("name")):
            role = a.get("role")
            return role if isinstance(role, str) and role else None
    return None


def _is_agent_identity(approver: str, quote: Quote) -> bool:
    """True if `approver` names an AI agent: an "agent:" actor, the quote's own agent, or any agent id
    registered in any policy (full "customer/agent" or bare "agent")."""
    if not isinstance(approver, str):
        return False
    a = approver.strip().lower()
    if not a:
        return False
    if a.startswith("agent:"):
        return True
    ids: set[str] = set()
    q_agent = getattr(quote, "agent_id", None)
    if isinstance(q_agent, str) and q_agent:
        ids.add(q_agent.lower())
        ids.add(q_agent.split("/")[-1].lower())
    for pid in policy_ids():
        try:
            agents = load_policy(pid).get("agents", [])
            if not isinstance(agents, list):
                continue
            for ag in agents:
                if not isinstance(ag, dict):
                    continue
                aid = ag.get("agent_id")
                if isinstance(aid, str) and aid:
                    ids.add(aid.lower())
                    ids.add(aid.split("/")[-1].lower())
        except Exception:
            continue
    return a in ids or a.split("/")[-1] in ids


def check_approval(
    quote: Quote,
    decision: PolicyDecision,
    approver: str,
    approver_role: str,
    quote_hash: str,
    *,
    decision_kind: str = "approve",
    note: str = "",
    order_id: Optional[str] = None,
    now: Optional[datetime] = None,
    audit_log: Optional[AuditLog] = None,
) -> dict:
    """Validate a human approval (or rejection) of an order.
    Approve requires: the approver is a person in the customer's approver directory (never an AI agent)
    whose registered role is the role the decision names or an override role (a role claimed in the
    request never overrides the directory); the policy said needs_approval; quote_hash equals the signed
    quote's hash and the signature verifies; the quote has not expired. Reject needs a directory person
    with any approver role and the right hash. Returns {"ok": bool, "reason": str}. Every call is audited."""
    reasons: list[str] = []
    if approver is None:
        clean_approver = ""
    elif isinstance(approver, bool) or not isinstance(approver, str):
        clean_approver = ""
        reasons.append("approver must be a string")
    else:
        clean_approver = approver.strip()
        if clean_approver.lower().startswith("human:"):
            clean_approver = clean_approver[6:].strip()

    if approver_role is None or approver_role == "":
        claimed_role = ""
    elif isinstance(approver_role, bool) or not isinstance(approver_role, str):
        claimed_role = ""
        reasons.append("approver_role must be a string")
    else:
        claimed_role = approver_role.strip()

    if isinstance(decision_kind, bool) or not isinstance(decision_kind, str) or decision_kind not in ("approve", "reject"):
        valid_kind = False
        kind_label = str(decision_kind) if isinstance(decision_kind, (str, int, float, bool)) else "invalid"
        reasons.append("decision_kind must be 'approve' or 'reject'")
    else:
        valid_kind = True
        kind_label = decision_kind

    if note is None:
        note_str = ""
    elif isinstance(note, bool) or not isinstance(note, str):
        note_str = ""
        reasons.append("note must be a string")
    else:
        note_str = note

    pol_id = getattr(decision, "policy_id", None)
    pol: dict[str, Any] = {"approver_roles": [], "override_roles": [], "approvers": []}
    if isinstance(pol_id, str) and pol_id:
        try:
            loaded = load_policy(pol_id)
            if isinstance(loaded, dict):
                pol = loaded
        except Exception:
            pass

    dec_role = getattr(decision, "approver_role", None)
    raw_overrides = pol.get("override_roles", [])
    overrides = [r for r in raw_overrides if isinstance(r, str) and r] if isinstance(raw_overrides, list) else []
    allowed = {r for r in ([dec_role] if isinstance(dec_role, str) and dec_role else []) + overrides if r}
    if valid_kind and kind_label == "reject":
        raw_app_roles = pol.get("approver_roles", [])
        if isinstance(raw_app_roles, list):
            allowed |= {r for r in raw_app_roles if isinstance(r, str) and r}
    need_text = " or ".join(_safe_role_text(r) for r in sorted(allowed)) or "nobody"

    q_cid = getattr(quote, "customer_id", None)
    cid_str = q_cid if isinstance(q_cid, str) and q_cid else "unknown"
    dir_role = _approver_role_in_directory(pol, clean_approver) if clean_approver else None
    effective_role = claimed_role
    if not clean_approver:
        if "approver must be a string" not in reasons:
            reasons.append("no approver identity given")
    elif _is_agent_identity(clean_approver, quote):
        reasons.append(
            f"{clean_approver} is an AI agent identity; agents can never approve orders, only a named person "
            f"in the customer's approver directory can"
        )
    elif dir_role is None:
        reasons.append(f"{clean_approver} is not in {cid_str}'s approver directory (needs a registered {need_text})")
    else:
        if claimed_role and dir_role != claimed_role:
            reasons.append(f"{clean_approver} is registered as {_safe_role_text(dir_role)}, not {_safe_role_text(claimed_role)}")
        if dir_role not in allowed:
            reasons.append(f"{clean_approver} is a {_safe_role_text(dir_role)}, who cannot decide this order (needs: {need_text})")
        effective_role = dir_role

    v = verify_quote_detailed(quote, now=now)
    q_hash_attr = getattr(quote, "quote_hash", None)
    if (
        isinstance(quote_hash, bool)
        or not isinstance(quote_hash, str)
        or not quote_hash
        or not isinstance(q_hash_attr, str)
        or quote_hash != q_hash_attr
        or quote_hash != v["quote_hash"]
    ):
        reasons.append("the approval is for a different version of the quote (hash mismatch); re-review the current quote")
    elif not v["ok"]:
        reasons.append("the quote signature does not verify: " + "; ".join(r for r in v["reasons"] if "expired" not in r))

    dec_outcome = getattr(decision, "outcome", None)
    if not valid_kind or kind_label == "approve":
        if dec_outcome == "reject":
            reasons.append("the policy rejected this order; a hard rule cannot be overridden by approval")
        elif dec_outcome not in ("needs_approval", "auto_approve"):
            reasons.append(f"invalid policy outcome {dec_outcome!r}")
        if v["expired"]:
            reasons.append("the quote's price lock has expired; request a fresh quote and approve that")

    ok = not reasons
    reason = "ok" if ok else "; ".join(reasons)
    action = "approval.verified" if ok else "approval.refused"
    verb = "approved" if kind_label == "approve" else "rejected"
    who = f"{clean_approver} ({_safe_role_text(effective_role)})"
    q_id_attr = getattr(quote, "quote_id", None)
    qid_str = q_id_attr if isinstance(q_id_attr, str) and q_id_attr else "unknown"
    qhash_prefix = quote_hash[:12] if isinstance(quote_hash, str) else ""
    summary = (
        f"{who} {verb} quote {qid_str} ({_safe_eur(getattr(quote, 'total_net', 0.0))}), bound to hash {qhash_prefix}…"
        + (f': "{note_str}"' if note_str else ".")
    ) if ok else f"{who} tried to {kind_label} quote {qid_str} but it was refused: {reason}."
    oid_str = order_id if isinstance(order_id, str) and order_id.strip() else None
    qhash_data = quote_hash if isinstance(quote_hash, str) else None
    _log(audit_log).append(
        f"human:{clean_approver or 'unknown'}",
        action,
        oid_str or qid_str,
        summary,
        {
            "approver": clean_approver,
            "approver_role": effective_role,
            "decision": kind_label,
            "note": note_str,
            "quote_id": qid_str,
            "quote_hash": qhash_data,
            "ok": ok,
            "reason": reason,
        },
        related_ids=[qid_str] if oid_str and qid_str != "unknown" else [],
    )
    return {"ok": ok, "reason": reason}


def agent_actor(customer_id: str, agent_id: str) -> str:
    cid = customer_id if isinstance(customer_id, str) else ""
    aid = agent_id if isinstance(agent_id, str) else ""
    return f"agent:{aid if '/' in aid else f'{cid}/{aid}'}"


def log_request(need: Need, agent_id: str, run_id: str, audit_log: Optional[AuditLog] = None):
    actor = agent_actor(getattr(need, "customer_id", ""), agent_id)
    extra = ""
    raw_excl = getattr(need, "excluded_allergens", None)
    if isinstance(raw_excl, (list, tuple)) and raw_excl:
        extra = f", excluding {', '.join(str(x) for x in raw_excl)}"
    summary = (
        f"Buyer agent {actor.split(':', 1)[1]} asked for {_safe_qty_text(getattr(need, 'quantity', 0))} "
        f"{getattr(need, 'unit', '')} {getattr(need, 'product_query', '')} "
        f"for {_safe_day_text(getattr(need, 'delivery_by', None))}{extra}."
    )
    rid = run_id if isinstance(run_id, str) and run_id else "unknown"
    return _log(audit_log).append(actor, "request.received", rid, summary, {"need": _json_safe(need)})


def log_quote(quote: Quote, run_id: Optional[str] = None, audit_log: Optional[AuditLog] = None):
    raw_lines = getattr(quote, "lines", None)
    lines = raw_lines if isinstance(raw_lines, list) else []
    items = "; ".join(
        f"{_safe_qty_text(getattr(ln, 'qty_sales_units', 0))} × {getattr(ln, 'product_name', '')} at {_safe_eur(getattr(ln, 'unit_price', 0.0))}"
        for ln in lines[:3]
    )
    if len(lines) > 3:
        items += f"; +{len(lines) - 3} more"
    try:
        exp = _to_aware_utc(getattr(quote, "expires_at", None))
    except (ValueError, TypeError, OverflowError):
        exp = datetime.now(timezone.utc)
    from aimo.trust.audit import _local

    qhash = getattr(quote, "quote_hash", None)
    qhash_str = qhash if isinstance(qhash, str) and qhash else None
    qid = getattr(quote, "quote_id", None)
    qid_str = qid if isinstance(qid, str) and qid else "unknown"
    summary = (
        f"Valio Aimo issued quote {qid_str} to {getattr(quote, 'agent_id', '')}: {items} = {_safe_eur(getattr(quote, 'total_net', 0.0))} net, "
        f"delivery {_safe_day_text(getattr(quote, 'delivery_date', None))}, price locked until {_local(exp, '%H:%M')}"
        + (f", signed (hash {qhash_str[:12]}…)." if qhash_str else ".")
    )
    rel = [run_id] if isinstance(run_id, str) and run_id else []
    return _log(audit_log).append(
        "system:gateway",
        "quote.created",
        qid_str,
        summary,
        {
            "quote": _json_safe(quote),
            "quote_hash": qhash_str,
            "customer_id": getattr(quote, "customer_id", None) if isinstance(getattr(quote, "customer_id", None), str) else None,
            "total_net": _finite_number(getattr(quote, "total_net", None)),
        },
        related_ids=rel,
    )


def log_decision(
    quote: Quote,
    decision: PolicyDecision,
    order_id: Optional[str] = None,
    audit_log: Optional[AuditLog] = None,
):
    raw_hits = getattr(decision, "hits", None)
    hits = raw_hits if isinstance(raw_hits, list) else []
    flagged = [h for h in hits if getattr(h, "outcome", None) != "allow"]
    raw_outcome = getattr(decision, "outcome", None)
    outcome_str = raw_outcome if isinstance(raw_outcome, str) and raw_outcome else "unknown"
    head = {
        "auto_approve": "AUTO-APPROVED",
        "needs_approval": f"NEEDS APPROVAL by a {_safe_role_text(getattr(decision, 'approver_role', None))}",
        "reject": "REJECTED",
    }.get(outcome_str, outcome_str.upper())
    if flagged:
        first_msg = getattr(flagged[0], "message", "")
        why = first_msg if isinstance(first_msg, str) else str(first_msg)
        if len(flagged) > 1:
            extra_rules = dict.fromkeys(
                str(getattr(h, "rule_id", "")).split(":")[0].replace("_", " ")
                for h in flagged[1:]
                if getattr(h, "rule_id", None)
            )
            if extra_rules:
                why += " Also: " + ", ".join(extra_rules) + "."
    else:
        why = f"All {len(hits)} checks passed."
    qid = getattr(quote, "quote_id", None)
    qid_str = qid if isinstance(qid, str) and qid else "unknown"
    oid_str = order_id if isinstance(order_id, str) and order_id.strip() else None
    qhash = getattr(quote, "quote_hash", None)
    qhash_str = qhash if isinstance(qhash, str) else None
    summary = (
        f"Policy {getattr(decision, 'policy_id', 'unknown')} v{getattr(decision, 'policy_version', '0')} "
        f"evaluated quote {qid_str}: {head}. {why}"
    )
    return _log(audit_log).append(
        "system:policy-engine",
        "policy.evaluated",
        oid_str or qid_str,
        summary,
        {"decision": _json_safe(decision), "quote_id": qid_str, "quote_hash": qhash_str},
        related_ids=[qid_str] if oid_str and qid_str != "unknown" else [],
    )


def log_order(
    order: Order,
    quote: Optional[Quote] = None,
    actor: str = "system:gateway",
    note: str = "",
    audit_log: Optional[AuditLog] = None,
):
    """Action order.<status>. order.placed events carry customer_id and total_net (used by period_spend)."""
    total = _finite_number(getattr(quote, "total_net", None)) if quote is not None else None
    dec = getattr(order, "decision", None)
    role_str = _safe_role_text(getattr(dec, "approver_role", None))
    raw_status = getattr(order, "status", None)
    status_str = raw_status if isinstance(raw_status, str) and raw_status else "unknown"
    words = {
        "pending_approval": f"is waiting for a {role_str} to approve",
        "approved": f"was approved by {getattr(order, 'approved_by', None)}",
        "placed": "was placed with Valio Aimo",
        "rejected": "was rejected",
        "expired": "expired before approval",
        "failed": "failed",
    }
    oid = getattr(order, "order_id", None)
    oid_str = oid if isinstance(oid, str) and oid else "unknown"
    summary = f"Order {oid_str} " + words.get(status_str, status_str)
    if total is not None:
        summary += f" ({eur(total)} net)"
    raw_rc = getattr(order, "response_code", None)
    if raw_rc is not None and raw_rc != "" and not isinstance(raw_rc, bool):
        rc_str = raw_rc if isinstance(raw_rc, str) else str(raw_rc)
        rc_label = RESPONSE_LABELS.get(rc_str, "")
        summary += f"; supplier response {rc_str}" + (f" {rc_label}" if rc_label else "")
    note_str = note if isinstance(note, str) else ("" if note is None else str(note))
    summary += f": {note_str}" if note_str else "."
    qid = getattr(order, "quote_id", None)
    qid_str = qid if isinstance(qid, str) and qid else "unknown"
    qhash = getattr(order, "quote_hash", None)
    data: dict[str, Any] = {
        "order": _json_safe(order),
        "customer_id": getattr(order, "customer_id", None) if isinstance(getattr(order, "customer_id", None), str) else None,
        "quote_id": qid_str,
        "quote_hash": qhash if isinstance(qhash, str) else None,
    }
    if total is not None:
        data["total_net"] = total
    approved_by = getattr(order, "approved_by", None)
    if isinstance(approved_by, str) and approved_by:
        data["approver"] = approved_by
    actor_str = actor if isinstance(actor, str) and actor.strip() else "system:gateway"
    return _log(audit_log).append(
        actor_str,
        f"order.{status_str}",
        oid_str,
        summary,
        data,
        related_ids=[qid_str] if qid_str != "unknown" else [],
    )
