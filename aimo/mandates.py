import json
import os

from aimo import audit, db
from aimo.models import Mandate

_MANDATES = [
    Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=2500.0,
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
        approvers=["Sari Nieminen"],
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


def _con():
    con = db.connect()
    con.execute("create table if not exists mandates (customer text, agent_id text, body text, primary key (customer, agent_id))")
    con.executemany("insert or ignore into mandates values (?,?,?)", [(m.customer, m.agent_id, m.model_dump_json()) for m in _MANDATES])
    con.commit()
    return con


def all():
    con = _con()
    try:
        return [Mandate.model_validate_json(r[0]) for r in con.execute("select body from mandates order by rowid")]
    finally:
        con.close()


def get(customer, agent_id=None):
    for m in all():
        if m.customer == customer and (agent_id is None or m.agent_id == agent_id):
            return m
    return None


def put(m, actor="human:admin"):
    old = get(m.customer, m.agent_id)
    con = _con()
    try:
        con.execute("insert or replace into mandates values (?,?,?)", (m.customer, m.agent_id, m.model_dump_json()))
        con.commit()
    finally:
        con.close()
    if old is None:
        why = f"Mandate for {m.customer}/{m.agent_id} created"
    else:
        diff = [f"{k} {getattr(old, k)} -> {v}" for k, v in m.model_dump().items() if getattr(old, k) != v]
        why = f"Mandate for {m.customer}/{m.agent_id} updated: " + ("; ".join(diff) if diff else "no changes")
    audit.record(m.customer, actor, "mandate_updated", m.customer, why, {"agent_id": m.agent_id, "new": m.model_dump()})
    return m


def agent_for_key(key):
    hit = _keys().get(key)
    if not hit:
        return None
    m = get(hit[0], hit[1])
    return (hit[0], hit[1], m) if m else None
