"""Buyer-side customer profiles: who the agent works for and what it may do.

The authoritative policy lives with trust (aimo/trust/policies, exposed through the
`list_policies` tool and GET /v1/customers). This file only holds what the BUYER side knows
about itself: its agent identity, delivery site, usual products and standing orders, plus a
fallback mandate summary copied from docs/interfaces.md ("Demo customers") so the agent can
still explain its limits when list_policies is unavailable.
"""
from __future__ import annotations

from typing import Any

from aimo.models import Mandate

PROFILES: dict[str, dict[str, Any]] = {
    "hotel-aurora": {
        "customer_id": "hotel-aurora",
        "name": "Hotel Aurora Helsinki",
        "segment": "hotel chain",
        "agent_id": "hotel-aurora/buyer-1",
        "policy_id": "hotel-aurora",
        "site": "Hotel Aurora Helsinki, breakfast kitchen",
        "approver": "anna@aurora (Procurement Manager)",
        "preferred_product_ids": ["463038"],
        "usual_match": r"^(?!.*\b(?:oat|kaura\w*|havre\w*|soy|soija\w*|almond|manteli\w*|coconut|kookos\w*|rice|riisi\w*)\b).*(?:milk|mai[dt]o|mjölk|eila)",
        "preferences": {"brand": "Valio", "pack": "10 l novobox for the milk dispenser",
                        "fat_pct": 1.5},
        "excluded_allergens": [],
        "standing_order": {"product_id": "463038", "qty_per_week": 300, "unit": "L"},
        "mandate_summary": (
            "Auto-approve orders up to EUR 500 net. EUR 500-5,000 needs the procurement_manager. "
            "Over EUR 5,000 is rejected. Substitutions must keep the same claims; tier 'best' is "
            "allowed, 'better'/'good' need approval. Category scope: dairy, bakery, produce."),
    },
    "school-tampere": {
        "customer_id": "school-tampere",
        "name": "Tampere school kitchen",
        "segment": "school kitchen",
        "agent_id": "school-tampere/buyer-1",
        "policy_id": "school-tampere",
        "site": "Tampere school central kitchen",
        "approver": "kitchen manager",
        "preferred_product_ids": [],
        "preferences": {},
        "excluded_allergens": ["milk", "nuts"],
        "standing_order": None,
        "mandate_summary": (
            "Pupils with milk and nut allergies: milk and nuts are excluded allergens. Auto-approve "
            "up to EUR 300 net. Any substitution needs the kitchen_manager. A line whose allergen "
            "status is unknown is rejected."),
    },
    "hospital-meilahti": {
        "customer_id": "hospital-meilahti",
        "name": "Meilahti hospital kitchen",
        "segment": "hospital",
        "agent_id": "hospital-meilahti/buyer-1",
        "policy_id": "hospital-meilahti",
        "site": "Meilahti hospital diet kitchen",
        "approver": "dietitian",
        "preferred_product_ids": [],
        "preferences": {},
        "excluded_allergens": ["gluten"],
        "standing_order": None,
        "mandate_summary": (
            "Gluten is an excluded allergen. The dietitian approves any substitution. Unknown "
            "allergen status needs approval by the dietitian."),
    },
}


def get_profile(customer_id: str) -> dict[str, Any]:
    cid = customer_id if isinstance(customer_id, str) else str(customer_id or "").strip()
    if cid in PROFILES:
        return PROFILES[cid]
    return {
        "customer_id": cid,
        "name": cid,
        "segment": "unknown",
        "agent_id": f"{cid}/buyer-1",
        "policy_id": cid,
        "site": cid,
        "approver": "a human approver",
        "preferred_product_ids": [],
        "preferences": {},
        "excluded_allergens": [],
        "standing_order": None,
        "mandate_summary": "Unknown: call list_policies.",
    }


def mandate_for(customer_id: str) -> Mandate:
    p = get_profile(customer_id)
    return Mandate(customer_id=p["customer_id"], agent_id=p["agent_id"], policy_id=p["policy_id"])
