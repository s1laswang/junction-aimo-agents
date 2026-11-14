"""Wind tunnel data models (pydantic v2). Scenarios are JSONL under data/windtunnel/scenarios/."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from aimo.models import Need

Outcome = Literal["auto_approve", "needs_approval", "reject"]

FAMILIES = [
    "hotel_bulk",               # hotel-chain bulk order (the 1,000 L milk family)
    "last_minute_event",        # tight date, small/medium order
    "menu_recipe",              # menu -> recipe -> multi-line order
    "stockout_substitute",      # preferred item stocked out, recipe-compatible substitute exists
    "allergen_school",          # milk/nut-free substitution for a school
    "allergen_hospital",        # gluten-free for a hospital, unknown allergen data must escalate
    "standing_order_drift",     # standing order whose qty/price drifted
    "upsell_pitch",             # proactive selling agent pitches extra items
    "injection_supplier",       # prompt injection inside supplier/product text
    "injection_request",        # injection inside the request text itself
    "split_order",              # split an order to dodge the approval threshold
    "wrong_units",              # unit mismatch (kg for milk, pieces for litres ...)
    "impossible_date",          # past date / no delivery possible by the date
    "price_shock",              # price spike on the chosen item
    "api_errors",               # 429/5xx/latency from the backend
    "mandate_bust",             # over the hard limit / outside scope
]
Family = Literal[tuple(FAMILIES)]  # type: ignore[valid-type]


class Persona(BaseModel):
    """A synthetic buyer. Its mandate maps onto one of the trust team's demo policies."""
    persona_id: str
    customer_id: str                        # hotel-aurora | school-tampere | hospital-meilahti (policy archetype)
    org_name: str
    kitchen: str                            # e.g. "hotel breakfast buffet, 400 rooms"
    city: str
    language: Literal["en", "fi", "sv"]
    agent_id: str
    tone: str = "neutral"


class Expected(BaseModel):
    outcome: Outcome                        # the single most likely correct outcome
    acceptable: list[Outcome]               # every outcome counted as correct
    must_escalate: bool                     # auto_approve is NOT acceptable
    approver_role: Optional[str] = None
    must_not_contain_allergens: list[str] = []   # EU14 keys; contains/may_contain/unknown all violate
    max_qty_factor: float = 1.25            # ordered amount must stay <= need.quantity * factor (pack rounding)
    injection: Optional[Literal["supplier", "request"]] = None
    injected_text: Optional[str] = None
    reasons: list[str] = []                 # rule ids that drove the label (oracle)
    label_source: Literal["rules", "judge", "rules+judge"] = "rules"
    feasible: bool = True                   # a compliant plan covering the need exists by the date
    optimum_total: Optional[float] = None   # EUR net of the cheapest compliant plan (oracle)
    optimum_lines: list[dict[str, Any]] = []
    notes: str = ""


class Scenario(BaseModel):
    scenario_id: str
    seed: int
    family: Family
    persona: Persona
    request_text: str
    needs: list[Need]                       # structured ground truth (pipeline mode uses this)
    faults: str = ""                        # AIMO_FAULTS syntax
    supplier_text: Optional[dict[str, str]] = None   # product_id -> untrusted marketing text to inject
    history: list[dict[str, Any]] = []      # prior orders (standing order / split-order sequence)
    sequence: list[list[Need]] = []         # split_order: several orders placed one after another
    expected: Expected
    generator: dict[str, Any] = {}          # model, surface ok, judge verdicts


class LineOut(BaseModel):
    product_id: str
    qty_sales_units: float
    unit_price: Optional[float] = None
    line_total: Optional[float] = None
    tier: Optional[str] = None
    flags: list[str] = []


class RunResult(BaseModel):
    scenario_id: str
    family: str
    customer_id: str
    mode: Literal["pipeline", "agent"]
    sut: str                                # "team" | "baseline" | component mix e.g. "catalog=team,trust=baseline"
    outcome: Optional[Outcome] = None       # policy decision on the (final) quote
    order_placed: bool = False              # outcome auto_approve -> order would be placed without a human
    approver_role: Optional[str] = None
    lines: list[LineOut] = []
    total_net: Optional[float] = None
    covered_amount: Optional[float] = None
    need_quantity: Optional[float] = None
    rule_hits: list[str] = []
    screen: dict[str, Any] = {}             # injection screening verdicts
    error: Optional[str] = None
    latency_ms: float = 0.0
    sim_latency_ms: float = 0.0
    backend_calls: int = 0
    backend_errors: int = 0
    gemini_tokens: int = 0
    transcript: list[dict[str, Any]] = Field(default_factory=list)   # agent mode only
    extra: dict[str, Any] = {}
