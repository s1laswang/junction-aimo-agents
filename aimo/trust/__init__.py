"""Trust layer: policy engine, signed quotes, approvals, hash-chained signed audit log, injection screen.
See docs/interfaces.md (contract) and docs/TRUST.md (protocol and rationale)."""
from aimo.trust.approvals import (agent_actor, check_approval, log_decision, log_order, log_quote,
                                  log_request)
from aimo.trust.audit import AuditLog, audit
from aimo.trust.policy import evaluate, explain, get_mandate, list_customers, load_policy, policy_ids
from aimo.trust.screening import screen_text
from aimo.trust.signing import (canonical_json, jwks, kid, quote_hash, sign_jws, sign_quote, verify_jws,
                                verify_quote, verify_quote_detailed)


def tamper_for_demo(seq: int, summary: str | None = None) -> dict:
    """DEV ONLY: rewrite one audit event so /v1/audit/verify can be shown catching it."""
    return audit.tamper_for_demo(seq, summary=summary)


def untamper_for_demo(seq: int) -> bool:
    return audit.untamper_for_demo(seq)


def evidence_pack(subject_id: str) -> dict:
    return audit.evidence_pack(subject_id)


__all__ = [
    "AuditLog", "agent_actor", "audit", "canonical_json", "check_approval", "evaluate", "evidence_pack", "explain",
    "get_mandate", "jwks", "kid", "list_customers", "load_policy", "log_decision", "log_order", "log_quote",
    "log_request", "policy_ids", "quote_hash", "screen_text", "sign_jws", "sign_quote", "tamper_for_demo",
    "untamper_for_demo", "verify_jws", "verify_quote", "verify_quote_detailed",
]
