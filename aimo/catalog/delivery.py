"""Delivery-date logic: earliest feasible date per candidate, late options, split deliveries.

Mock API facts (verified 14 Nov by requesting 16, 17, 20 and 25 Nov): availability does NOT depend
on requestedDate. availableDate, closingTime and availableAmount are the same for every date. So:
  - more stock on a later date cannot be learned from the API. A partial-stock line stays partial.
  - for a product whose order cut-off has passed, the next feasible delivery is ESTIMATED with the
    product's own lead time (availableDate - closingTime date), counted from now and skipping Sundays.
    It is always labelled "estimated" in reasons.
A line delivered after need.delivery_by is never accepted silently. It carries the flags `late_option`
and `delivery_date:<iso>`, and it needs a human unless the customer allowed split delivery
(need.preferences["allow_split_delivery"] = true). The flag stays even then.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from aimo.models import Offer


def earliest_feasible(offer: Optional[Offer], now: Optional[datetime] = None) -> tuple[Optional[date], bool]:
    """(earliest delivery date, estimated?) for one offer. None if it cannot be delivered at all."""
    if offer is None or offer.sales_block or offer.unit_price is None:
        return None, False
    if offer.available_amount == 0:
        return None, False
    if not offer.cutoff_passed:
        return offer.available_date, False
    now = now or datetime.now().astimezone()
    if offer.available_date and offer.closing_time:
        lead = max(1, (offer.available_date - offer.closing_time.date()).days)
    else:
        lead = 4
    start = now.date() + timedelta(days=1 if (offer.closing_time and now.time() > offer.closing_time.time()) else 0)
    d = start + timedelta(days=lead)
    while d.weekday() == 6:      # no Sunday deliveries (assumption, labelled estimated)
        d += timedelta(days=1)
    return d, True


def late_offer(offer: Offer, d: date) -> Offer:
    """A copy of the offer that is orderable for date d (used to rank late options)."""
    return offer.model_copy(update={"cutoff_passed": False, "available": True, "available_date": d,
                                    "requested_date": d, "issues": [*offer.issues, "estimated_date"]})


def fmt(d: date) -> str:
    return f"{d:%a %d %b}"
