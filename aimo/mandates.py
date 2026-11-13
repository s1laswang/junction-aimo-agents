import json
import os

from aimo.models import Mandate

_MANDATES = [
    Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=3000.0,
        per_week_eur=15000.0,
        exclude_allergens=[],
        allowed_categories=None,
        substitutions="within_prefs",
        preferred_brands=["Valio"],
        approvers=["Laura Virtanen", "Mikko Heikkinen"],
    ),
    Mandate(
        customer="hospital-tays",
        agent_id="buyer-tays",
        per_order_eur=1500.0,
        per_week_eur=8000.0,
        exclude_allergens=["peanut", "nuts", "egg"],
        allowed_categories=None,
        substitutions="ask",
        preferred_brands=["Valio"],
        approvers=["Helena Korhonen", "Antti Lahtinen"],
    ),
    Mandate(
        customer="school-hervanta",
        agent_id="buyer-hervanta",
        per_order_eur=600.0,
        per_week_eur=3000.0,
        exclude_allergens=["peanut", "nuts"],
        allowed_categories=None,
        substitutions="within_prefs",
        preferred_brands=["Valio"],
        approvers=["Sari Nieminen (kitchen manager)"],
    ),
]

_DEMO_KEYS = {
    "demo-hotel-aurora": ["hotel-aurora", "buyer-aurora"],
    "demo-hospital-tays": ["hospital-tays", "buyer-tays"],
    "demo-school-hervanta": ["school-hervanta", "buyer-hervanta"],
}


def _keys():
    raw = os.environ.get("AIMO_AGENT_KEYS")
    return json.loads(raw) if raw else _DEMO_KEYS


def all():
    return list(_MANDATES)


def get(customer, agent_id=None):
    for m in _MANDATES:
        if m.customer == customer and (agent_id is None or m.agent_id == agent_id):
            return m
    return None


def agent_for_key(key):
    hit = _keys().get(key)
    if not hit:
        return None
    m = get(hit[0], hit[1])
    return (hit[0], hit[1], m) if m else None
