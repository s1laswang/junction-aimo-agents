from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Constraint(BaseModel):
    lactose_free: bool | None = None
    gluten_free: bool | None = None
    exclude_allergens: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    origin: str | None = None
    max_unit_price: float | None = None
    brands: list[str] = Field(default_factory=list)


class NeedItem(BaseModel):
    query: str
    quantity: float
    unit: str
    constraints: Constraint = Field(default_factory=Constraint)


class Need(BaseModel):
    customer: str
    deliver_by: date
    items: list[NeedItem]
    raw_text: str | None = None


class UnitSize(BaseModel):
    value: float
    unit: str


class Product(BaseModel):
    product_id: str
    name: str
    brand: str | None = None
    vendor: str | None = None
    category: str | None = None
    sales_unit: str | None = None
    unit_size: UnitSize | None = None
    allergens: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    origin: str | None = None
    temperature: int | None = None


Tier = Literal["exact", "good", "better", "best"]


class Offer(BaseModel):
    product_id: str
    product: Product
    units_needed: float
    unit_price_net: float
    total_net: float
    total_gross: float
    currency: str = "EUR"
    available_amount: float
    available_date: date | None = None
    closing_time: str | None = None
    sales_block: str | None = None
    reliability: float | None = None
    co2_per_kg: float | None = None
    match_score: float = 0.0
    tier: Tier | None = None
    substitute_for: str | None = None
    notes: list[str] = Field(default_factory=list)


class OfferGroup(BaseModel):
    need_item: NeedItem
    offers: list[Offer]
    shortfall: float = 0.0


class OffersResponse(BaseModel):
    items: list[OfferGroup]


class QuoteLine(BaseModel):
    product_id: str
    name: str | None = None
    quantity: float
    sales_unit: str | None = None
    unit_price_net: float
    line_total_net: float
    substitute_for: str | None = None


class Quote(BaseModel):
    quote_id: str
    customer: str
    agent_id: str
    delivery_date: date
    lines: list[QuoteLine]
    total_net: float
    total_gross: float
    currency: str = "EUR"
    issued_at: datetime
    valid_until: datetime
    jws: str = ""


class QuoteRequestLine(BaseModel):
    product_id: str
    quantity: float
    substitute_for: str | None = None


class QuoteRequest(BaseModel):
    offers: list[QuoteRequestLine]
    delivery_date: date


OrderStatus = Literal["draft", "pending_approval", "approved", "placed", "confirmed", "rejected", "blocked", "expired"]


class Reason(BaseModel):
    code: str
    text: str


class Decision(BaseModel):
    outcome: Literal["auto", "needs_approval", "blocked"]
    reasons: list[Reason] = Field(default_factory=list)


class Substitution(BaseModel):
    original_id: str
    substitute_id: str
    reason: str


class Order(BaseModel):
    order_id: str
    quote_id: str
    customer: str
    agent_id: str
    lines: list[QuoteLine]
    total_net: float
    status: OrderStatus
    decision: Decision
    substitutions: list[Substitution] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PlaceOrderRequest(BaseModel):
    quote_id: str
    jws: str
    ask_human: bool = False
    note: str | None = None


class ApprovalDecision(BaseModel):
    approve: bool
    by: str
    note: str | None = None


class ParseRequest(BaseModel):
    text: str
    customer: str


class Mandate(BaseModel):
    customer: str
    agent_id: str
    per_order_eur: float
    per_week_eur: float
    exclude_allergens: list[str] = Field(default_factory=list)
    allowed_categories: list[str] | None = None
    substitutions: Literal["never", "within_prefs", "ask"] = "ask"
    preferred_brands: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)


class AuditEvent(BaseModel):
    seq: int
    ts: datetime
    customer: str
    actor: str
    action: str
    subject: str
    detail: dict = Field(default_factory=dict)
    why: str
    prev_hash: str
    hash: str


class AuditTrail(BaseModel):
    events: list[AuditEvent]
    text: str


class SimulateStock(BaseModel):
    product_id: str
    available_amount: float
    available_date: date | None = None
