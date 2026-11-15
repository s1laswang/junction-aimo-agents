"""Live offers for the catalog. Prefers the gateway's `aimo.backend.get_offers`; until that lands
(or if it fails), falls back to a small direct, cached, batched client for the mock API.

Fallback rules (polite to the mock API): <= 1000 ids per call, responses cached in
data/cache/catalog_offers/ for OFFER_TTL_S, no retries in a tight loop.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import logging
import math
import os
import re
import threading
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
MAX_ID_LEN = 64
MAX_OFFER_IDS = 10_000
MAX_SCAN_ITEMS = 50_000
BATCH_SIZE = 1000
_KIND_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ISO_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?$"
)


def _has_bad_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _require_date(d: Any) -> date:
    if not isinstance(d, date) or isinstance(d, datetime):
        raise ValueError("requested_date must be a date")
    return d


def _clean_ids(product_ids: Any) -> list[str]:
    if not isinstance(product_ids, Iterable) or isinstance(product_ids, (str, bytes, bytearray, dict)):
        raise ValueError("product_ids must be an iterable of product id strings")
    seen: dict[str, None] = {}
    scanned = 0
    for item in product_ids:
        scanned += 1
        if scanned > MAX_SCAN_ITEMS:
            raise ValueError(f"product_ids exceeds maximum scan size ({MAX_SCAN_ITEMS})")
        if not isinstance(item, str):
            continue
        s = item.strip(" \t\r\n")
        if not s:
            continue
        if len(s) > MAX_ID_LEN or any(ch.isspace() for ch in s) or _has_bad_char(s):
            raise ValueError("invalid product id in product_ids")
        if s not in seen:
            if len(seen) >= MAX_OFFER_IDS:
                raise ValueError(f"too many unique product_ids (max {MAX_OFFER_IDS})")
            seen[s] = None
    return list(seen)


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
        try:
            o = Offer.model_validate(d_copy)
        except Exception:
            return None
    else:
        return None
    issues = [x for x in o.issues if isinstance(x, str) and x.strip() and not _has_bad_char(x)] if isinstance(o.issues, (list, tuple)) else []
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
    adate = o.available_date
    if adate is not None and (not isinstance(adate, date) or isinstance(adate, datetime)):
        ok_ad, parsed_ad = _avail_date(adate)
        adate = parsed_ad if ok_ad else None
        if not ok_ad:
            available = False
            if "availability_error" not in issues:
                issues.append("availability_error")
    if o.cutoff_passed or o.sales_block or (adate is not None and adate > requested_date):
        available = False
    return o.model_copy(update={
        "unit_price": up,
        "unit_price_gross": ug,
        "list_price": lp,
        "comparison_price": cp,
        "tax_rate": tr,
        "available_amount": av,
        "available_date": adate,
        "available": available,
        "issues": list(dict.fromkeys(issues)),
    })


def get_offers(product_ids: list[str], requested_date: date, customer_id: str | None = None) -> dict[str, Offer]:
    """Gateway client if present (its errors, e.g. injected 429/5xx, propagate so the gateway can
    answer 503); otherwise the catalog's own direct fallback."""
    req_date = _require_date(requested_date)
    if customer_id is not None and (
        not isinstance(customer_id, str) or len(customer_id) > 128 or _has_bad_char(customer_id)
    ):
        raise ValueError("invalid customer_id")
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
            out: dict[str, Offer] = {}
            for i in range(0, len(ids), BATCH_SIZE):
                chunk = ids[i:i + BATCH_SIZE]
                try:
                    res = fn(chunk, req_date, customer_id=customer_id)
                except TypeError:
                    res = fn(chunk, req_date)
                if not isinstance(res, Mapping):
                    continue
                for k, v in res.items():
                    if not isinstance(k, str) or not k.strip():
                        continue
                    ks = k.strip()
                    if len(ks) > MAX_ID_LEN or any(ch.isspace() for ch in ks) or _has_bad_char(ks):
                        continue
                    clean = _sanitize_offer(v, ks, req_date)
                    if clean is not None:
                        out[ks] = clean
            return out
        except (NotImplementedError, ModuleNotFoundError) as e:
            log.warning("aimo.backend.get_offers unavailable (%s); using catalog fallback client", type(e).__name__)
    return fallback_get_offers(ids, req_date)


def _cache_path(kind: str, ids: list[str], d: date):
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ValueError("invalid offer cache kind")
    req_date = _require_date(d)
    clean = _clean_ids(ids)
    h = hashlib.sha1(",".join(sorted(clean)).encode("utf-8", errors="replace")).hexdigest()[:16]
    return CACHE_DIR / f"{kind}-{req_date.isoformat()}-{h}.json"


def _post(kind: str, ids: list[str], d: date) -> dict:
    clean = _clean_ids(ids)
    p = _cache_path(kind, clean, d)
    try:
        st = p.stat()
        if OFFLINE or time.time() - st.st_mtime < OFFER_TTL_S:
            cached = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return cached
            log.warning("ignoring non-object cached offers in %s", p.name)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        log.warning("ignoring unreadable cached offers %s (%s)", p.name, type(e).__name__)
    if OFFLINE:
        raise RuntimeError("AIMO_OFFLINE=1 and no cached offers")
    import httpx
    body = {"customer": "aimo-catalog", "requestedDate": d.isoformat(), "products": clean}
    r = httpx.post(f"{AIMO_BASE_URL}/api/{kind}", json=body, headers={"X-API-Key": AIMO_API_KEY}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("upstream offers response must be a JSON object")
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("catalog offer cache write failed: %s", type(e).__name__)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return data


def _dt(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    txt = s.strip(" \t\r\n")
    if not txt or len(txt) > 64 or _has_bad_char(txt) or not _ISO_DT_RE.match(txt):
        return None
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    m = re.match(r"^(.*)([+-]\d{2})(\d{2})$", txt)
    if m:
        txt = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is not None:
            dt.astimezone(timezone.utc)
        return dt
    except (ValueError, OverflowError, TypeError):
        return None


def _avail_date(s: Any) -> tuple[bool, Optional[date]]:
    if s is None or s == "":
        return True, None
    if isinstance(s, date) and not isinstance(s, datetime):
        return True, s
    if not isinstance(s, str):
        return False, None
    txt = s.strip(" \t\r\n")
    if not txt:
        return True, None
    if len(txt) > 64 or _has_bad_char(txt):
        return False, None
    head = txt[:10]
    if len(head) != 10 or head[4] != "-" or head[7] != "-":
        return False, None
    if len(txt) > 10 and txt[10] not in ("T", "t", " "):
        return False, None
    try:
        return True, date.fromisoformat(head)
    except (ValueError, OverflowError):
        return False, None


def _before(a: datetime, b: datetime) -> bool:
    try:
        if (a.tzinfo is None) == (b.tzinfo is None):
            return a < b
        az = a if a.tzinfo is not None else a.replace(tzinfo=timezone.utc)
        bz = b if b.tzinfo is not None else b.replace(tzinfo=timezone.utc)
        return az < bz
    except OverflowError:
        return False


def _clean_short_str(val: Any, max_len: int = 32) -> Optional[str]:
    if not isinstance(val, str):
        return None
    s = val.strip(" \t\r\n")
    if not s or len(s) > max_len or _has_bad_char(s):
        return None
    return s


def build_offer(pid: str, d: date, price: Optional[dict], avail: Optional[dict], time_now: Optional[datetime]) -> Offer:
    if not isinstance(pid, str):
        raise ValueError("pid must be a non-empty string")
    clean_pid = pid.strip(" \t\r\n")
    if not clean_pid or len(clean_pid) > MAX_ID_LEN or any(ch.isspace() for ch in clean_pid) or _has_bad_char(clean_pid):
        raise ValueError("pid must be a non-empty string without whitespace or control characters")
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
        curr = _clean_short_str(cp.get("currency"), 16)
        cu = _clean_short_str(price.get("comparisonUnit"), 32)
        kw.update(
            unit_price=up,
            unit_price_gross=ug,
            list_price=list_val,
            comparison_price=cmp_val,
            comparison_unit=cu,
            tax_rate=tax_val,
            currency=curr or "EUR",
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
        lm = _clean_short_str(avail.get("logisticModel"), 32)
        su = _clean_short_str(line.get("salesUnit"), 32)
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
        product_id=clean_pid,
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
    for i in range(0, len(clean), BATCH_SIZE):
        chunk = clean[i:i + BATCH_SIZE]
        try:
            pr = _post("price", chunk, req_date)
            av = _post("availability", chunk, req_date)
        except Exception as e:
            log.warning("mock API offer fetch failed: %s", type(e).__name__)
            continue
        pr_list = pr.get("products") if isinstance(pr, dict) else None
        av_list = av.get("products") if isinstance(av, dict) else None
        prices = {
            pid.strip(" \t\r\n"): p for p in (pr_list if isinstance(pr_list, list) else [])
            if isinstance(p, dict) and isinstance(pid := p.get("productId"), str) and pid.strip(" \t\r\n")
        }
        avails = {
            pid.strip(" \t\r\n"): p for p in (av_list if isinstance(av_list, list) else [])
            if isinstance(p, dict) and isinstance(pid := p.get("productId"), str) and pid.strip(" \t\r\n")
        }
        now = _dt(av.get("timeNow")) if isinstance(av, dict) else None
        for pid in chunk:
            out[pid] = build_offer(pid, req_date, prices.get(pid), avails.get(pid), now)
    return out
