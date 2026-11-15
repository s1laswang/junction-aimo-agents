"""Shared data contract for every component. Owner: CEO.

Change only by APPENDING optional fields (and tell the CEO). Never rename or remove.
Field names are snake_case. Money is EUR, net (excl. VAT) unless the name says gross.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- catalog
class AllergenStatus(str, Enum):
    contains = "contains"          # structured data says CONTAINS
    may_contain = "may_contain"    # structured data says MAY_CONTAIN / traces
    free = "free"                  # structured data says FREE_FROM (nonAllergen)
    unknown = "unknown"            # no structured data. NEVER treat as safe.


# EU FIC 1169/2011 Annex II, the 14 allergen groups. Keys are used everywhere.
EU14 = [
    "gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts",
    "celery", "mustard", "sesame", "sulphites", "lupin", "molluscs",
]


class ProductSummary(BaseModel):
    id: str                                   # Valio Aimo product code, e.g. "464306"
    gtin: Optional[str] = None                # GS1 GTIN of the sales unit
    name: str                                 # English name if present, else Finnish
    name_fi: Optional[str] = None
    brand: Optional[str] = None
    vendor: Optional[str] = None
    category: Optional[str] = None
    sales_unit: str                           # technical code, e.g. ST, KI, RAS, BOT
    content_amount: Optional[float] = None    # litres or kg in ONE sales unit
    content_unit: Optional[Literal["L", "KG", "PCS"]] = None
    claims: list[str] = []                    # e.g. FREE_FROM_LACTOSE, VEGETARIAN, ORGANIC
    labels: list[str] = []                    # packagingMarkedLabel ids
    delivery_reliability: Optional[float] = None  # 0..1 from CSV
    co2_kg_per_kg: Optional[float] = None         # category average from CSV
    score: Optional[float] = None             # search relevance


class Product(ProductSummary):
    allergens: dict[str, AllergenStatus] = {}  # keys from EU14, missing key == unknown
    allergen_source: dict[str, str] = {}       # key -> "structured" | "inferred:gemini" | "none"
    ingredients_text: Optional[str] = None
    nutrition: dict[str, float] = {}           # per 100 g/ml, e.g. FAT, LACS, PRO-
    temperature_condition: Optional[int] = None
    country_of_origin: Optional[str] = None
    allowed_lot_size: Optional[float] = None
    marketing_text: Optional[str] = None       # UNTRUSTED supplier text; screen before LLM use


# ---------------------------------------------------------------- live offers
class Offer(BaseModel):
    product_id: str
    requested_date: date
    available: bool                           # orderable for requested_date right now
    available_amount: Optional[float] = None  # in sales units; None when unknown (Terminal)
    available_date: Optional[date] = None
    closing_time: Optional[datetime] = None
    cutoff_passed: bool = False               # closing_time < now -> cannot make requested_date
    logistic_model: Optional[str] = None      # "Stock" | "Terminal"
    sales_block: bool = False
    sales_unit: Optional[str] = None
    unit_price: Optional[float] = None        # net per sales unit (catalogPrice.value)
    unit_price_gross: Optional[float] = None
    list_price: Optional[float] = None
    comparison_price: Optional[float] = None  # net per L or KG
    comparison_unit: Optional[str] = None
    tax_rate: Optional[float] = None
    currency: str = "EUR"
    price_valid_to: Optional[datetime] = None
    fetched_at: datetime
    issues: list[str] = []                    # e.g. "cutoff_passed", "partial_stock", "price_error"
    provenance: Optional[dict[str, Any]] = None  # [gateway, appended] {source, endpoints, fetched_at, valid_for_s, faults}


# ---------------------------------------------------------------- need and plan
class Need(BaseModel):
    """Structured version of a free-text request (Gemini parses into this)."""
    raw_text: str = ""
    customer_id: str
    product_query: str                        # e.g. "lactose-free milk"
    required_claims: list[str] = []           # e.g. ["FREE_FROM_LACTOSE"]
    excluded_allergens: list[str] = []        # EU14 keys; contains/may_contain/unknown all excluded
    quantity: float = Field(..., gt=0, description="Total amount needed in `unit`; must be > 0 (0 or negative -> 422)")
    unit: Literal["L", "KG", "PCS"]
    delivery_by: date
    preferred_product_ids: list[str] = []
    preferences: dict[str, Any] = {}          # brand, fat_pct, pack, organic ...


Tier = Literal["preferred", "best", "better", "good", "rejected"]


class Candidate(BaseModel):
    product: ProductSummary
    offer: Optional[Offer] = None
    tier: Tier
    rank_score: float = 0.0
    reasons: list[str] = []                   # human-readable, shown in UI and audit
    flags: list[str] = []                     # "allergen_unknown", "outside_preference", "cutoff_passed"
    injection_screen: Optional[dict[str, Any]] = None  # prompt-injection screen of the supplier text: {suspicious, reasons, screened}


class PlanLine(BaseModel):
    product_id: str
    product_name: str
    tier: Tier
    qty_sales_units: float
    sales_unit: str
    covered_amount: float                     # in Need.unit
    unit_price: float
    line_total: float
    reasons: list[str] = []
    flags: list[str] = []
    injection_screen: Optional[dict[str, Any]] = None  # copied from the candidate: why this line is (not) flagged


class Plan(BaseModel):
    need: Need
    lines: list[PlanLine]
    candidates: list[Candidate]               # full good-better-best ranking incl. rejected
    covered_amount: float
    shortfall: float = 0.0
    total_net: float
    explanation: str = ""


# ---------------------------------------------------------------- quotes, policy, orders
class QuoteLine(BaseModel):
    product_id: str
    product_name: str
    qty_sales_units: float
    sales_unit: str
    unit_price: float
    line_total: float
    tier: Tier = "preferred"
    flags: list[str] = []


class Quote(BaseModel):
    quote_id: str
    customer_id: str
    agent_id: str
    delivery_date: date
    lines: list[QuoteLine]
    total_net: float
    total_gross: Optional[float] = None
    currency: str = "EUR"
    created_at: datetime
    expires_at: datetime                      # fixed price lock (default 15 min)
    need: Optional[Need] = None
    quote_hash: Optional[str] = None          # sha256 of canonical JSON (without hash/signature)
    signature: Optional[str] = None           # compact JWS (EdDSA) over quote_hash
    kid: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None  # [gateway, appended] price/availability snapshot source + times (signed with the quote)
    availability_snapshot: list[dict[str, Any]] = []  # [gateway, appended] per line: product_id, available_amount, closing_time, logistic_model, issues


class RuleHit(BaseModel):
    rule_id: str
    outcome: Literal["allow", "needs_approval", "reject"]
    message: str                              # plain language, shown to humans


class PolicyDecision(BaseModel):
    outcome: Literal["auto_approve", "needs_approval", "reject"]
    approver_role: Optional[str] = None
    hits: list[RuleHit] = []
    policy_id: str
    policy_version: str


OrderStatus = Literal["pending_approval", "approved", "placed", "rejected", "expired", "failed"]


class OrderResponseLine(BaseModel):
    product_id: str
    code: Literal["AP", "CA", "RE"]           # Peppol BIS: accepted / accepted with changes / rejected
    qty_confirmed: float
    note: str = ""


class Order(BaseModel):
    order_id: str
    quote_id: str
    quote_hash: str
    customer_id: str
    agent_id: str
    status: OrderStatus
    decision: PolicyDecision
    approved_by: Optional[str] = None
    approval_note: Optional[str] = None
    response_code: Optional[Literal["AP", "CA", "RE"]] = None
    response_lines: list[OrderResponseLine] = []
    created_at: datetime
    updated_at: datetime
    provenance: Optional[dict[str, Any]] = None  # [gateway, appended] availability re-check at placement
    approver_role: Optional[str] = None       # [gateway, appended] role the human approver acted in; approved_by is the plain id


class Mandate(BaseModel):
    """Which company's agent may do what. Production: a W3C VC / OAuth RAR token."""
    customer_id: str
    agent_id: str
    policy_id: str                            # which YAML policy applies
    scopes: list[str] = ["catalog:read", "quote:create", "order:submit"]
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------- audit and agent stream
class AuditEvent(BaseModel):
    seq: int
    ts: datetime
    actor: str                                # "agent:hotel-aurora/buyer-1" | "human:anna@aurora" | "system:gateway"
    action: str                               # e.g. quote.created, policy.evaluated, order.approved
    subject_id: str                           # quote_id / order_id / run_id
    summary: str                              # one human-readable sentence
    data: dict[str, Any] = {}
    prev_hash: str
    hash: str
    signature: str                            # JWS (EdDSA) over hash
    kid: str


class AgentStep(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    kind: Literal["thought", "tool_call", "tool_result", "plan", "quote", "decision",
                  "approval_wait", "order", "error", "final"]
    title: str
    data: dict[str, Any] = Field(default_factory=dict)
