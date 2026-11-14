"""Live offers for the catalog. Prefers the gateway's `aimo.backend.get_offers`; until that lands
(or if it fails), falls back to a small direct, cached, batched client for the mock API.

Fallback rules (polite to the mock API): <= 1000 ids per call, responses cached in
data/cache/catalog_offers/ for OFFER_TTL_S, no retries in a tight loop.
"""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import logging
import math
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from aimo.config import AIMO_API_KEY, AIMO_BASE_URL, DATA_DIR, OFFLINE
from aimo.models import Offer

log = logging.getLogger(__name__)
CACHE_DIR = DATA_DIR / "cache" / "catalog_offers"
OFFER_TTL_S = 300
UNLIMITED = 1e9          # Stock items report 9999999999 when effectively unlimited
TERMINAL_UNKNOWN = 999999.0


def _require_date(d: Any) -> date:
    if not isinstance(d, date) or isinstance(d, datetime):
        raise ValueError("requested_date must be a date")
    return d


def _clean_ids(product_ids: Any) -> list[str]:
    if not isinstance(product_ids, Iterable) or isinstance(product_ids, (str, bytes, bytearray, dict)):
        raise ValueError("product_ids must be an iterable of product id strings")
    out: list[str] = []
    for item in product_ids:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


def _pos_finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        fv = float(v)
    except OverflowError:
        return None
    return fv if (math.isfinite(fv) and fv > 0.0) else None


def _nonneg_finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        fv = float(v)
    except OverflowError:
        return None
    if not math.isfinite(fv) or fv < 0.0:
        return None
    return 0.0 if fv == 0.0 else fv


def _sanitize_offer(raw: Any, fallback_pid: str, requested_date: date) -> Optional[Offer]:
    if isinstance(raw, Offer):
        o = raw
        raw_up = o.unit_price
        raw_ug = o.unit_price_gross
        raw_lp = o.list_price
        raw_cp = o.comparison_price
        raw_tr = o.tax_rate
        raw_av = o.available_amount
    elif isinstance(raw, dict):
        d_copy = dict(raw)
        raw_up = d_copy.get("unit_price")
        raw_ug = d_copy.get("unit_price_gross")
        raw_lp = d_copy.get("list_price")
        raw_cp = d_copy.get("comparison_price")
        raw_tr = d_copy.get("tax_rate")
        raw_av = d_copy.get("available_amount")
        for k in ("unit_price", "unit_price_gross", "list_price", "comparison_price", "tax_rate", "available_amount"):
            d_copy[k] = None
        if not isinstance(d_copy.get("product_id"), str) or not d_copy["product_id"].strip():
            d_copy["product_id"] = fallback_pid
        if not isinstance(d_copy.get("requested_date"), date) or isinstance(d_copy.get("requested_date"), datetime):
            d_copy["requested_date"] = requested_date
        o = Offer.model_validate(d_copy)
    else:
        return None
    issues = list(o.issues or [])
    available = bool(o.available)
    up = _pos_finite(raw_up)
    if up is None:
        available = False
        if "price_error" not in issues:
            issues.append("price_error")
    ug = _pos_finite(raw_ug)
    lp = _pos_finite(raw_lp)
    cp = _pos_finite(raw_cp)
    tr = _nonneg_finite(raw_tr)
    if raw_av is None:
        av = None
    else:
        av = _nonneg_finite(raw_av)
        if av is None:
            available = False
            if "availability_error" not in issues:
                issues.append("availability_error")
        elif av == 0.0:
            available = False
            if "out_of_stock" not in issues:
                issues.append("out_of_stock")
    if o.cutoff_passed or o.sales_block or (o.available_date is not None and o.available_date > requested_date):
        available = False
    return o.model_copy(update={
        "unit_price": up,
        "unit_price_gross": ug,
        "list_price": lp,
        "comparison_price": cp,
        "tax_rate": tr,
        "available_amount": av,
        "available": available,
        "issues": list(dict.fromkeys(issues)),
    })


def get_offers(product_ids: list[str], requested_date: date, customer_id: str | None = None) -> dict[str, Offer]:
    """Gateway client if present (its errors, e.g. injected 429/5xx, propagate so the gateway can
    answer 503); otherwise the catalog's own direct fallback."""
    req_date = _require_date(requested_date)
    ids = _clean_ids(product_ids)
    if not ids:
        return {}
    fn = None
    try:
        from aimo import backend
        fn = getattr(backend, "get_offers", None)
    except ImportError:
        fn = None
    if fn is not None:
        try:
            try:
                res = fn(ids, req_date, customer_id=customer_id)
            except TypeError:
                res = fn(ids, req_date)
            out: dict[str, Offer] = {}
            for k, v in (res or {}).items():
                if not isinstance(k, str) or not k.strip():
                    continue
                ks = k.strip()
                clean = _sanitize_offer(v, ks, req_date)
                if clean is not None:
                    out[ks] = clean
            return out
        except (NotImplementedError, ModuleNotFoundError) as e:
            log.warning("aimo.backend.get_offers unavailable (%s); using catalog fallback client", type(e).__name__)
    return fallback_get_offers(ids, req_date)


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


def _dt(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    txt = s.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    m = re.match(r"^(.*)([+-]\d{2})(\d{2})$", txt)
    if m:
        txt = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        return None


def _avail_date(s: Any) -> tuple[bool, Optional[date]]:
    if s is None or s == "":
        return True, None
    if isinstance(s, date) and not isinstance(s, datetime):
        return True, s
    if not isinstance(s, str):
        return False, None
    txt = s.strip()
    if not txt:
        return True, None
    try:
        return True, date.fromisoformat(txt[:10])
    except ValueError:
        return False, None


def _before(a: datetime, b: datetime) -> bool:
    if (a.tzinfo is None) == (b.tzinfo is None):
        return a < b
    az = a if a.tzinfo is not None else a.replace(tzinfo=timezone.utc)
    bz = b if b.tzinfo is not None else b.replace(tzinfo=timezone.utc)
    return az < bz


def build_offer(pid: str, d: date, price: Optional[dict], avail: Optional[dict], time_now: Optional[datetime]) -> Offer:
    if not isinstance(pid, str) or not pid.strip():
        raise ValueError("pid must be a non-empty string")
    req_date = _require_date(d)
    issues: list[str] = []
    now = time_now if isinstance(time_now, datetime) else datetime.now().astimezone()
    kw: dict[str, Any] = {}
    has_valid_price = False
    if isinstance(price, dict) and not price.get("error"):
        cp = price.get("catalogPrice") if isinstance(price.get("catalogPrice"), dict) else {}
        lp = price.get("listPrice") if isinstance(price.get("listPrice"), dict) else {}
        cmp_d = price.get("comparisonPrice") if isinstance(price.get("comparisonPrice"), dict) else {}
        tax_d = price.get("tax") if isinstance(price.get("tax"), dict) else {}
        up = _pos_finite(cp.get("value"))
        ug = _pos_finite(cp.get("valueWithTax"))
        list_val = _pos_finite(lp.get("value"))
        cmp_val = _pos_finite(cmp_d.get("value"))
        tax_val = _nonneg_finite(tax_d.get("taxRate"))
        curr = cp.get("currency")
        cu = price.get("comparisonUnit")
        kw.update(
            unit_price=up,
            unit_price_gross=ug,
            list_price=list_val,
            comparison_price=cmp_val,
            comparison_unit=cu.strip() if isinstance(cu, str) and cu.strip() else None,
            tax_rate=tax_val,
            currency=curr.strip() if isinstance(curr, str) and curr.strip() else "EUR",
            price_valid_to=_dt(cp.get("validTo")),
        )
        if up is not None:
            has_valid_price = True
        else:
            issues.append("price_error")
    else:
        issues.append("price_error")
    available = False
    if isinstance(avail, dict) and avail:
        closing = _dt(avail.get("closingTime"))
        sb = bool(avail.get("salesBlock"))
        lines_raw = avail.get("scheduleLines")
        lines = [x for x in lines_raw if isinstance(x, dict)] if isinstance(lines_raw, list) else []
        line = lines[0] if lines else {}
        sb = sb or bool(line.get("salesBlock"))
        raw_amt = line.get("availableAmount")
        amt_valid = True
        if raw_amt is None:
            amt: Optional[float] = None
        else:
            amt = _nonneg_finite(raw_amt)
            if amt is None:
                amt_valid = False
        ok_date, adate = _avail_date(line.get("availableDate"))
        lm_raw = avail.get("logisticModel")
        lm = lm_raw if isinstance(lm_raw, str) else None
        su_raw = line.get("salesUnit")
        su = su_raw if isinstance(su_raw, str) else None
        cutoff = bool(closing and _before(closing, now))
        if lm == "Terminal" and amt == TERMINAL_UNKNOWN:
            amt = None
        kw.update(
            closing_time=closing,
            logistic_model=lm,
            sales_block=sb,
            sales_unit=su,
            available_amount=amt,
            available_date=adate,
            cutoff_passed=cutoff,
        )
        if cutoff:
            issues.append("cutoff_passed")
        if sb:
            issues.append("sales_block")
        if not lines:
            issues.append("no_schedule")
        elif not amt_valid or not ok_date:
            issues.append("availability_error")
        elif amt == 0.0:
            issues.append("out_of_stock")
        if adate and adate > req_date:
            issues.append("late")
        available = (
            has_valid_price
            and bool(lines)
            and amt_valid
            and ok_date
            and not sb
            and not cutoff
            and (amt is None or amt > 0.0)
            and (adate is None or adate <= req_date)
        )
    else:
        issues.append("availability_error")
    return Offer(
        product_id=pid.strip(),
        requested_date=req_date,
        available=available,
        fetched_at=datetime.now().astimezone(),
        issues=issues,
        **kw,
    )


def fallback_get_offers(ids: list[str], d: date) -> dict[str, Offer]:
    req_date = _require_date(d)
    clean = _clean_ids(ids)
    if not clean:
        return {}
    out: dict[str, Offer] = {}
    for i in range(0, len(clean), 1000):
        chunk = clean[i:i + 1000]
        try:
            pr = _post("price", chunk, req_date)
            av = _post("availability", chunk, req_date)
        except Exception as e:
            log.warning("mock API offer fetch failed: %s", type(e).__name__)
            continue
        pr_list = pr.get("products") if isinstance(pr, dict) else None
        av_list = av.get("products") if isinstance(av, dict) else None
        prices = {
            pid: p for p in (pr_list if isinstance(pr_list, list) else [])
            if isinstance(p, dict) and isinstance(pid := p.get("productId"), str) and pid.strip()
        }
        avails = {
            pid: p for p in (av_list if isinstance(av_list, list) else [])
            if isinstance(p, dict) and isinstance(pid := p.get("productId"), str) and pid.strip()
        }
        now = _dt(av.get("timeNow")) if isinstance(av, dict) else None
        for pid in chunk:
            out[pid] = build_offer(pid, req_date, prices.get(pid), avails.get(pid), now)
    return out
