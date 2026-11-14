"""Request bodies for the gateway (HTTP and MCP share them). Response models are in aimo.models."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from aimo.models import Need, Tier


class OffersRequest(BaseModel):
    product_ids: list[str] = Field(..., min_length=1, max_length=1000, description="Valio Aimo product codes (max 1,000)")
    requested_date: date = Field(..., description="Requested delivery date (YYYY-MM-DD)")
    customer_id: Optional[str] = Field(None, description="Defaults to the X-Customer-Id header")

    model_config = {"json_schema_extra": {"examples": [
        {"product_ids": ["463038", "464306", "469363"], "requested_date": "2026-11-16", "customer_id": "hotel-aurora"}]}}


class QuoteLineIn(BaseModel):
    product_id: str = Field(..., description="Valio Aimo product code")
    qty_sales_units: float = Field(..., gt=0, description="Quantity in the product's sales unit (e.g. ST = piece, KI = box)")
    tier: Optional[Tier] = Field(None, description="preferred | best | better | good (from plan_need); drives substitution rules")
    flags: list[str] = Field(default_factory=list, description="Planner flags carried into policy evaluation")
    product_name: Optional[str] = None


class QuoteRequest(BaseModel):
    customer_id: Optional[str] = Field(None, description="Defaults to X-Customer-Id")
    agent_id: Optional[str] = Field(None, description="Defaults to X-Agent-Id")
    delivery_date: date
    lines: list[QuoteLineIn] = Field(..., min_length=1)
    need: Optional[Need] = None

    model_config = {"json_schema_extra": {"examples": [
        {"customer_id": "hotel-aurora", "agent_id": "buyer-1", "delivery_date": "2026-11-16",
         "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]}]}}


class OrderRequest(BaseModel):
    quote_id: str
    quote_hash: str = Field(..., description="Must equal the quote's quote_hash: binds the order to the exact signed quote")

    model_config = {"json_schema_extra": {"examples": [{"quote_id": "Q-20261114-1a2b3c4d", "quote_hash": "<sha256 from the quote>"}]}}


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = Field(..., description="Human approver identity, e.g. anna@aurora")
    note: Optional[str] = None
    quote_hash: str = Field(..., description="The quote_hash the approver reviewed; 409 if it does not match the order")
    approver_role: Optional[str] = Field(None, description="Role the approver acts in (e.g. procurement_manager). "
                                         "Defaults to the role in the customer's approver directory.")

    model_config = {"json_schema_extra": {"examples": [
        {"decision": "approve", "approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager",
         "note": "OK for Monday", "quote_hash": "<sha256>"}]}}
