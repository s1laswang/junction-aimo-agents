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
import math
from typing import Any, Optional

from aimo.models import Offer


def _as_date(val: Any) -> Optional[date]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return None


def _normalize_date(d: Any) -> date:
    if isinstance(d, bool) or not isinstance(d, date):
        raise ValueError("expected a date")
    if isinstance(d, datetime):
        return d.date()
    return d


def _is_aware(dt: datetime) -> bool:
    try:
        return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None
    except Exception:
        return False


def _valid_price(val: Any) -> bool:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    try:
        fv = float(val)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(fv) and fv > 0.0


def _valid_available_amount(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    try:
        fv = float(val)
    except OverflowError:
        return val > 0
    except (TypeError, ValueError):
        return False
    return not math.isnan(fv) and fv > 0.0


def earliest_feasible(offer: Optional[Offer], now: Optional[datetime] = None) -> tuple[Optional[date], bool]:
    """(earliest delivery date, estimated?) for one offer. None if it cannot be delivered at all."""
    if offer is None or isinstance(offer, (bool, int, float, str, bytes, list, tuple, set, dict)):
        return None, False
    if not hasattr(offer, "unit_price") or bool(getattr(offer, "sales_block", False)):
        return None, False
    if not _valid_price(getattr(offer, "unit_price", None)):
        return None, False
    if not _valid_available_amount(getattr(offer, "available_amount", None)):
        return None, False
    if now is not None:
        if isinstance(now, bool) or not isinstance(now, (datetime, date)):
            return None, False
        now_dt: Optional[datetime] = now if isinstance(now, datetime) else datetime(now.year, now.month, now.day)
    else:
        now_dt = None
    avail_date = _as_date(getattr(offer, "available_date", None))
    if not getattr(offer, "cutoff_passed", False):
        return avail_date, False
    if now_dt is None:
        from aimo.backend import clock
        try:
            now_dt = clock.now().astimezone(clock.HELSINKI_TZ)
        except OverflowError:
            return None, False
    ct_raw = getattr(offer, "closing_time", None)
    ct_date: Optional[date] = None
    after_cutoff = False
    if isinstance(ct_raw, datetime):
        try:
            ct_cmp = ct_raw.astimezone(now_dt.tzinfo) if (_is_aware(now_dt) and _is_aware(ct_raw)) else ct_raw
        except OverflowError:
            ct_cmp = ct_raw
        ct_date = ct_cmp.date()
        after_cutoff = now_dt.replace(tzinfo=None).time() > ct_cmp.replace(tzinfo=None).time()
    elif isinstance(ct_raw, date) and not isinstance(ct_raw, bool):
        ct_date = ct_raw
    if avail_date is not None and ct_date is not None:
        lead = min(max(1, (avail_date - ct_date).days), 365)
    else:
        lead = 4
    try:
        start = now_dt.date() + timedelta(days=1 if after_cutoff else 0)
        d = start + timedelta(days=lead)
        while d.weekday() == 6:
            d += timedelta(days=1)
    except OverflowError:
        return None, False
    return d, True


def late_offer(offer: Offer, d: date) -> Offer:
    """A copy of the offer that is orderable for date d (used to rank late options)."""
    if not isinstance(offer, Offer):
        raise ValueError("offer must be an Offer")
    nd = _normalize_date(d)
    raw_issues = offer.issues if isinstance(offer.issues, (list, tuple)) else []
    issues = list(dict.fromkeys([*(i for i in raw_issues if isinstance(i, str)), "estimated_date"]))
    return offer.model_copy(update={"cutoff_passed": False, "available": True, "available_date": nd,
                                    "requested_date": nd, "issues": issues})


def fmt(d: date) -> str:
    nd = _normalize_date(d)
    return f"{nd:%a %d %b}"
