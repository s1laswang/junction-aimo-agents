"""Policy archetypes as documented in docs/interfaces.md ("Demo customers", owner: trust).

The oracle uses these numbers to label scenarios. They are the CONTRACT, not trust's implementation; when
trust's YAML lands, `sync_from_trust()` reads thresholds from it if the keys are recognisable and the results
report records which source was used. Anything not stated in interfaces.md is None (= unknown -> the oracle
marks spend-driven labels ambiguous rather than guessing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CustomerSpec:
    customer_id: str
    policy_id: str
    auto_limit: Optional[float]               # EUR net per order auto-approved at or below
    approval_limit: Optional[float]           # above this: reject
    approver_role: Optional[str]              # role for spend approval
    excluded_allergens: list[str] = field(default_factory=list)
    substitution: str = "approval"            # "best_ok" (best tier allowed, others need approval) | "approval" (any)
    substitution_role: Optional[str] = None
    unknown_allergen: str = "reject"          # what happens when an excluded allergen's status is unknown
    unknown_allergen_role: Optional[str] = None
    categories: Optional[list[str]] = None    # category scope (names, mapping to codes UNVERIFIED)
    source: str = "docs/interfaces.md"


CUSTOMERS: dict[str, CustomerSpec] = {
    "hotel-aurora": CustomerSpec(
        customer_id="hotel-aurora", policy_id="hotel-aurora", auto_limit=500.0, approval_limit=5000.0,
        approver_role="procurement_manager", excluded_allergens=[], substitution="best_ok",
        substitution_role="procurement_manager", unknown_allergen="n/a", categories=["dairy", "bakery", "produce"]),
    "school-tampere": CustomerSpec(
        customer_id="school-tampere", policy_id="school-tampere", auto_limit=300.0, approval_limit=None,
        approver_role="kitchen_manager", excluded_allergens=["milk", "nuts"], substitution="approval",
        substitution_role="kitchen_manager", unknown_allergen="reject"),
    "hospital-meilahti": CustomerSpec(
        customer_id="hospital-meilahti", policy_id="hospital-meilahti", auto_limit=None, approval_limit=None,
        approver_role=None, excluded_allergens=["gluten"], substitution="approval", substitution_role="dietitian",
        unknown_allergen="needs_approval", unknown_allergen_role="dietitian"),
}


def sync_from_trust() -> dict[str, str]:
    """Best effort: pull numeric thresholds from trust's policy YAML if available. Returns {customer: source}."""
    out = {}
    try:
        from aimo import trust  # noqa: F401
        load = getattr(trust, "load_policy", None)
    except Exception:  # noqa: BLE001
        return out
    if not load:
        return out
    for cid, spec in CUSTOMERS.items():
        try:
            pol = load(spec.policy_id)
        except Exception:  # noqa: BLE001
            continue
        flat = _flatten(pol)
        auto = _first(flat, ("auto_approve_max", "auto_approve_limit", "auto_limit", "auto_approve_eur", "auto_max_eur"))
        upper = _first(flat, ("reject_above", "approval_limit", "max_order_eur", "hard_limit", "reject_over"))
        if auto is not None:
            spec.auto_limit = float(auto)
            spec.source = "trust-yaml"
        if upper is not None:
            spec.approval_limit = float(upper)
        out[cid] = spec.source
    return out


def _flatten(d, prefix="") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}."))
            out[str(k)] = v if not isinstance(v, (dict, list)) else out.get(str(k), None)
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flatten(v, f"{prefix}{i}."))
    return out


def _first(flat: dict, keys: tuple[str, ...]):
    for k in keys:
        v = flat.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None
