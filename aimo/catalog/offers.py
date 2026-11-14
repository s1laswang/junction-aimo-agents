"""Live offers for the catalog. Prefers the gateway's `aimo.backend.get_offers`; until that lands
(or if it fails), falls back to a small direct, cached, batched client for the mock API.

Fallback rules (polite to the mock API): <= 1000 ids per call, responses cached in
data/cache/catalog_offers/ for OFFER_TTL_S, no retries in a tight loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime
from typing import Optional

from aimo.config import AIMO_API_KEY, AIMO_BASE_URL, DATA_DIR, OFFLINE
from aimo.models import Offer

log = logging.getLogger(__name__)
CACHE_DIR = DATA_DIR / "cache" / "catalog_offers"
OFFER_TTL_S = 300
UNLIMITED = 1e9          # Stock items report 9999999999 when effectively unlimited
TERMINAL_UNKNOWN = 999999.0


def get_offers(product_ids: list[str], requested_date: date, customer_id: str | None = None) -> dict[str, Offer]:
    """Gateway client if present (its errors, e.g. injected 429/5xx, propagate so the gateway can
    answer 503); otherwise the catalog's own direct fallback."""
    ids = list(dict.fromkeys(str(i) for i in product_ids if i))
    if not ids:
        return {}
    fn = None
    try:
        from aimo import backend  # gateway-owned
        fn = getattr(backend, "get_offers", None)
    except ImportError:
        fn = None
    if fn is not None:
        try:
            try:
                res = fn(ids, requested_date, customer_id=customer_id)
            except TypeError:
                res = fn(ids, requested_date)
            return {k: (v if isinstance(v, Offer) else Offer.model_validate(v)) for k, v in (res or {}).items()}
        except (NotImplementedError, ModuleNotFoundError) as e:
            log.warning("aimo.backend.get_offers unavailable (%s); using catalog fallback client", type(e).__name__)
    return fallback_get_offers(ids, requested_date)


def _cache_path(kind: str, ids: list[str], d: date):
    h = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{kind}-{d.isoformat()}-{h}.json"


def _post(kind: str, ids: list[str], d: date) -> dict:
    p = _cache_path(kind, ids, d)
    if p.exists() and (OFFLINE or time.time() - p.stat().st_mtime < OFFER_TTL_S):
        return json.loads(p.read_text())
    if OFFLINE:
        raise RuntimeError("AIMO_OFFLINE=1 and no cached offers")
    import httpx
    body = {"customer": "aimo-catalog", "requestedDate": d.isoformat(), "products": ids}
    r = httpx.post(f"{AIMO_BASE_URL}/api/{kind}", json=body, headers={"X-API-Key": AIMO_API_KEY}, timeout=30)
    r.raise_for_status()
    data = r.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return data


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("+0200", "+02:00").replace("+0300", "+03:00"))
    except ValueError:
        return None


def build_offer(pid: str, d: date, price: Optional[dict], avail: Optional[dict], time_now: Optional[datetime]) -> Offer:
    issues: list[str] = []
    now = time_now or datetime.now().astimezone()
    kw: dict = {}
    if price and not price.get("error"):
        cp = price.get("catalogPrice") or {}
        kw.update(unit_price=cp.get("value"), unit_price_gross=cp.get("valueWithTax"),
                  list_price=(price.get("listPrice") or {}).get("value"),
                  comparison_price=(price.get("comparisonPrice") or {}).get("value"),
                  comparison_unit=price.get("comparisonUnit"),
                  tax_rate=(price.get("tax") or {}).get("taxRate"),
                  currency=cp.get("currency") or "EUR", price_valid_to=_dt(cp.get("validTo")))
    else:
        issues.append("price_error")
    available = False
    if avail:
        closing = _dt(avail.get("closingTime"))
        sb = bool(avail.get("salesBlock"))
        lines = avail.get("scheduleLines") or []
        line = lines[0] if lines else {}
        sb = sb or bool(line.get("salesBlock"))
        amt = line.get("availableAmount")
        adate = date.fromisoformat(line["availableDate"][:10]) if line.get("availableDate") else None
        lm = avail.get("logisticModel")
        cutoff = bool(closing and closing < now)
        if lm == "Terminal" and amt == TERMINAL_UNKNOWN:
            amt = None
        kw.update(closing_time=closing, logistic_model=lm, sales_block=sb, sales_unit=line.get("salesUnit"),
                  available_amount=amt, available_date=adate, cutoff_passed=cutoff)
        if cutoff:
            issues.append("cutoff_passed")
        if sb:
            issues.append("sales_block")
        if not lines:
            issues.append("no_schedule")
        elif amt == 0:
            issues.append("out_of_stock")
        if adate and adate > d:
            issues.append("late")
        available = bool(lines) and not sb and not cutoff and (amt is None or amt > 0) and (adate is None or adate <= d)
    else:
        issues.append("availability_error")
    return Offer(product_id=pid, requested_date=d, available=available, fetched_at=datetime.now().astimezone(),
                 issues=issues, **kw)


def fallback_get_offers(ids: list[str], d: date) -> dict[str, Offer]:
    out: dict[str, Offer] = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        try:
            pr = _post("price", chunk, d)
            av = _post("availability", chunk, d)
        except Exception as e:  # noqa: BLE001
            log.warning("mock API offer fetch failed: %s", type(e).__name__)
            continue
        prices = {p["productId"]: p for p in pr.get("products") or []}
        avails = {p["productId"]: p for p in av.get("products") or []}
        now = _dt(av.get("timeNow"))
        for pid in chunk:
            out[pid] = build_offer(pid, d, prices.get(pid), avails.get(pid), now)
    return out
