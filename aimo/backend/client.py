"""Valio Aimo mock API client: cache, retry/backoff, chunking, polite pacing, fault injection.

Upstream (docs/valio-mock-openapi.json): POST /api/price and /api/availability with
{"customer", "requestedDate", "products": [<=1000 ids]}; header X-API-Key.

Caching is per product: (endpoint, customer, date, product_id) -> item, in memory and on disk
under data/cache/<endpoint>/<date>/<customer>/<id>.json. TTL defaults to 300 s
(AIMO_CACHE_TTL). With AIMO_OFFLINE=1 the disk cache is used regardless of age and the network
is never touched.

Fault injection (robustness demos, wind tunnel): env AIMO_FAULTS or per-request context var
(set by the gateway from header X-Aimo-Faults). Spec, ';'-separated:
    stockout:<id|*>            available=False, amount 0
    partial:<id|*>:<n>         available_amount = n
    price_shock:<id|*>:<f>     prices multiplied by f
    price_error:<id|*>         price missing
    cutoff:<id|*>              cutoff_passed=True
    sales_block:<id|*>         sales block
    error_rate:<p>             raise UpstreamError(503) with probability p
    rate_limit:<p>             raise UpstreamError(429) with probability p
    latency_ms:<n>             sleep n ms before answering
Every injected change is tagged in Offer.issues as "injected:<kind>".
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

from aimo import config
from aimo.models import Offer

log = logging.getLogger("aimo.backend")

MAX_IDS_PER_CALL = 1000
CACHE_TTL_S = float(os.environ.get("AIMO_CACHE_TTL", "300"))
MIN_INTERVAL_S = float(os.environ.get("AIMO_MIN_INTERVAL", "0.25"))
DEFAULT_UPSTREAM_CUSTOMER = os.environ.get("AIMO_UPSTREAM_CUSTOMER", "")
CACHE_DIR = config.DATA_DIR / "cache"

# Upstream sentinel values (observed 14 Nov): Terminal products always report 999999;
# some Stock products report 9999999999 or 666666666 (effectively unlimited).
TERMINAL_SENTINEL = 999999.0
LIMITED_STOCK_BELOW = 100000.0

HELSINKI = timezone(timedelta(hours=2))  # November: EET, no DST


class UpstreamError(RuntimeError):
    """The mock API failed (after retries) or a fault was injected."""

    def __init__(self, message: str, status_code: int = 502, retry_after: float | None = None, injected: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.injected = injected


# ------------------------------------------------------------------ helpers
def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        m = re.match(r"(.*)([+-]\d{2})(\d{2})$", value)
        if m:
            try:
                return datetime.fromisoformat(f"{m.group(1)}{m.group(2)}:{m.group(3)}")
            except ValueError:
                return None
        return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64] or "_"


def chunked(items: list[str], n: int = MAX_IDS_PER_CALL) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ------------------------------------------------------------------ faults
_faults_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("aimo_faults", default=None)


def set_request_faults(spec: str | None) -> contextvars.Token:
    """Set faults for the current request/task (gateway middleware uses header X-Aimo-Faults)."""
    return _faults_ctx.set(spec)


def reset_request_faults(token: contextvars.Token) -> None:
    _faults_ctx.reset(token)


@dataclass
class Faults:
    stockout: set[str] = field(default_factory=set)
    partial: dict[str, float] = field(default_factory=dict)
    price_shock: dict[str, float] = field(default_factory=dict)
    price_error: set[str] = field(default_factory=set)
    cutoff: set[str] = field(default_factory=set)
    sales_block: set[str] = field(default_factory=set)
    error_rate: float = 0.0
    rate_limit: float = 0.0
    latency_ms: int = 0
    spec: str = ""

    def empty(self) -> bool:
        return not self.spec

    @staticmethod
    def _hit(ids: set[str] | dict, pid: str) -> bool:
        return pid in ids or "*" in ids

    @staticmethod
    def _val(d: dict[str, float], pid: str) -> float | None:
        return d.get(pid, d.get("*"))


def parse_faults(spec: str | None) -> Faults:
    f = Faults(spec=(spec or "").strip())
    for part in f.spec.split(";"):
        bits = [b.strip() for b in part.strip().split(":")]
        if not bits or not bits[0]:
            continue
        kind, args = bits[0].lower(), bits[1:]
        try:
            if kind == "stockout" and args:
                f.stockout.add(args[0])
            elif kind == "partial" and len(args) >= 2:
                f.partial[args[0]] = float(args[1])
            elif kind == "price_shock" and len(args) >= 2:
                f.price_shock[args[0]] = float(args[1])
            elif kind == "price_error" and args:
                f.price_error.add(args[0])
            elif kind == "cutoff" and args:
                f.cutoff.add(args[0])
            elif kind == "sales_block" and args:
                f.sales_block.add(args[0])
            elif kind == "error_rate" and args:
                f.error_rate = float(args[0])
            elif kind == "rate_limit" and args:
                f.rate_limit = float(args[0])
            elif kind == "latency_ms" and args:
                f.latency_ms = int(float(args[0]))
            else:
                log.warning("ignoring unknown fault spec part %r", part)
        except ValueError:
            log.warning("ignoring malformed fault spec part %r", part)
    return f


def current_faults() -> Faults:
    spec = _faults_ctx.get()
    if spec is None:
        spec = os.environ.get("AIMO_FAULTS", "")
    return parse_faults(spec)


# ------------------------------------------------------------------ client
class ValioClient:
    """httpx client for the mock API with per-product cache, retries and pacing."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        cache_dir: Path | None = CACHE_DIR,
        ttl_s: float = CACHE_TTL_S,
        offline: bool | None = None,
        max_retries: int = 4,
    ):
        self.base_url = (base_url or config.AIMO_BASE_URL).rstrip("/")
        self._api_key = api_key if api_key is not None else config.AIMO_API_KEY
        self.cache_dir = cache_dir
        self.ttl_s = ttl_s
        self.offline = config.OFFLINE if offline is None else offline
        self.max_retries = max_retries
        self._mem: dict[tuple[str, str, str, str], tuple[float, dict, dict]] = {}
        self._lock = threading.RLock()
        self._last_call = 0.0
        self._http: httpx.Client | None = None
        self.stats = {"upstream_calls": 0, "cache_hits": 0, "retries": 0}

    # -- http
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                headers={"X-API-Key": self._api_key, "User-Agent": "aimo-trusted-trade-gateway/0.1"},
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
        return self._http

    def ping(self) -> dict:
        r = self._client().get("/api/ping")
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict) -> dict:
        if self.offline:
            raise UpstreamError("offline mode (AIMO_OFFLINE=1) and no cached response", status_code=503)
        if not self._api_key:
            raise UpstreamError("AIMO_API_KEY is not configured", status_code=503)
        attempt = 0
        while True:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            retry_after: float | None = None
            try:
                self.stats["upstream_calls"] += 1
                r = self._client().post(f"/api/{endpoint}", json=body)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After")
                    retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                    err = UpstreamError(f"upstream {endpoint} HTTP {r.status_code}", r.status_code, retry_after)
                else:
                    # 4xx other than 429: not retryable. Never echo request headers.
                    raise UpstreamError(f"upstream {endpoint} HTTP {r.status_code}: {r.text[:300]}", r.status_code)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = UpstreamError(f"upstream {endpoint} transport error: {type(e).__name__}", 504)
            if attempt >= self.max_retries:
                raise err
            delay = retry_after if retry_after is not None else min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
            log.warning("%s; retry %d/%d in %.2fs", err, attempt + 1, self.max_retries, delay)
            self.stats["retries"] += 1
            time.sleep(delay)
            attempt += 1

    # -- cache
    def _disk_path(self, endpoint: str, customer: str, d: str, pid: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / endpoint / d / _slug(customer) / f"{_slug(pid)}.json"

    def _cache_get(self, key: tuple[str, str, str, str], allow_stale: bool) -> tuple[float, dict, dict] | None:
        now = time.time()
        hit = self._mem.get(key)
        if hit is None:
            p = self._disk_path(*key)
            if p is not None and p.exists():
                try:
                    blob = json.loads(p.read_text())
                    hit = (float(blob["fetched_at"]), blob["item"], blob.get("meta", {}))
                    self._mem[key] = hit
                except Exception:  # corrupt cache file: ignore
                    hit = None
        if hit is None:
            return None
        if allow_stale or now - hit[0] <= self.ttl_s:
            return hit
        return None

    def _cache_put(self, key: tuple[str, str, str, str], fetched_at: float, item: dict, meta: dict) -> None:
        self._mem[key] = (fetched_at, item, meta)
        p = self._disk_path(*key)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": fetched_at, "item": item, "meta": meta}))
            tmp.replace(p)
        except OSError as e:  # cache is best effort
            log.debug("cache write failed: %s", e)

    # -- core
    def _fetch(self, endpoint: str, product_ids: list[str], requested_date: date, customer: str | None) -> dict:
        cust = customer or DEFAULT_UPSTREAM_CUSTOMER or "aimo-gateway"
        d = requested_date.isoformat()
        ids = list(dict.fromkeys(str(p) for p in product_ids if str(p).strip()))
        items: dict[str, tuple[float, dict, dict]] = {}
        with self._lock:
            missing = []
            for pid in ids:
                hit = self._cache_get((endpoint, cust, d, pid), allow_stale=self.offline)
                if hit is not None:
                    items[pid] = hit
                    self.stats["cache_hits"] += 1
                else:
                    missing.append(pid)
            for chunk in chunked(missing):
                resp = self._post(endpoint, {"customer": cust, "requestedDate": d, "products": chunk})
                fetched = time.time()
                meta = {k: v for k, v in resp.items() if k != "products"}
                for it in resp.get("products", []) or []:
                    pid = str(it.get("productId"))
                    self._cache_put((endpoint, cust, d, pid), fetched, it, meta)
                    items[pid] = (fetched, it, meta)
        products = []
        oldest = None
        meta_out: dict[str, Any] = {}
        for pid in ids:
            if pid in items:
                fetched, it, meta = items[pid]
                products.append({**it, "_fetched_at": fetched, "_meta": meta})
                oldest = fetched if oldest is None else min(oldest, fetched)
                meta_out = meta_out or meta
        return {**meta_out, "products": products, "_oldest_fetch": oldest, "_customer": cust}

    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._fetch("price", product_ids, requested_date, customer)

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._fetch("availability", product_ids, requested_date, customer)


_client: Any = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = ValioClient()
        return _client


def set_client(client) -> None:
    global _client
    with _client_lock:
        _client = client


# ------------------------------------------------------------------ merge -> Offer
def _price_val(p: dict | None, key: str = "value") -> float | None:
    if not p or p.get(key) is None:
        return None
    try:
        return float(p[key])
    except (TypeError, ValueError):
        return None


def merge_offer(
    pid: str,
    requested_date: date,
    price_item: dict | None,
    avail_item: dict | None,
    qty: float | None = None,
    now: datetime | None = None,
) -> Offer:
    issues: list[str] = []
    fetched_ts = [x.get("_fetched_at") for x in (price_item, avail_item) if x and x.get("_fetched_at")]
    fetched_at = datetime.fromtimestamp(min(fetched_ts), tz=timezone.utc) if fetched_ts else datetime.now(timezone.utc)

    # Estimated upstream "now": the availability response's timeNow plus time elapsed since fetch.
    if now is None:
        now = datetime.now(timezone.utc)
        if avail_item:
            tn = parse_dt((avail_item.get("_meta") or {}).get("timeNow"))
            if tn is not None and avail_item.get("_fetched_at"):
                now = tn + timedelta(seconds=max(0.0, time.time() - float(avail_item["_fetched_at"])))

    available = True
    amount: float | None = None
    avail_date: date | None = None
    closing: datetime | None = None
    cutoff = False
    logistic = None
    sales_block = False
    sales_unit = None

    if not avail_item:
        available = False
        issues.append("availability_not_found")
    else:
        logistic = avail_item.get("logisticModel")
        closing = parse_dt(avail_item.get("closingTime"))
        sales_block = bool(avail_item.get("salesBlock"))
        lines = avail_item.get("scheduleLines") or []
        if lines:
            sl = lines[0]
            sales_unit = sl.get("salesUnit")
            sales_block = sales_block or bool(sl.get("salesBlock"))
            avail_date = parse_date(sl.get("availableDate"))
            raw_amt = sl.get("availableAmount")
            amount = float(raw_amt) if raw_amt is not None else None
            if logistic == "Terminal" and amount is not None and amount >= TERMINAL_SENTINEL:
                amount = None
                issues.append("terminal_unknown_qty")
        else:
            available = False
            issues.append("no_schedule")
        if closing is not None and closing < now:
            cutoff = True
            available = False
            issues.append("cutoff_passed")
        if sales_block:
            available = False
            issues.append("sales_block")
        if avail_date is not None and avail_date > requested_date:
            available = False
            issues.append("late_delivery")
        if amount is not None and amount <= 0:
            available = False
            issues.append("out_of_stock")
        elif amount is not None and amount < LIMITED_STOCK_BELOW:
            issues.append("limited_stock")
            if qty is not None and amount < qty:
                issues.append("partial_stock")

    unit_price = gross = listp = comp = tax = None
    comp_unit = None
    valid_to = None
    if not price_item or price_item.get("error") or not price_item.get("catalogPrice"):
        available = False
        issues.append("price_error")
    else:
        cp = price_item.get("catalogPrice")
        unit_price = _price_val(cp)
        gross = _price_val(cp, "valueWithTax") or _price_val(price_item.get("priceWithTax"))
        listp = _price_val(price_item.get("listPrice"))
        comp = _price_val(price_item.get("comparisonPrice"))
        comp_unit = price_item.get("comparisonUnit")
        tax = _price_val(price_item.get("tax"), "taxRate")
        valid_to = parse_dt(cp.get("validTo"))
        if unit_price is None:
            available = False
            issues.append("price_error")

    return Offer(
        product_id=pid,
        requested_date=requested_date,
        available=available,
        available_amount=amount,
        available_date=avail_date,
        closing_time=closing,
        cutoff_passed=cutoff,
        logistic_model=logistic,
        sales_block=sales_block,
        sales_unit=sales_unit,
        unit_price=unit_price,
        unit_price_gross=gross,
        list_price=listp,
        comparison_price=comp,
        comparison_unit=comp_unit,
        tax_rate=tax,
        price_valid_to=valid_to,
        fetched_at=fetched_at,
        issues=issues,
    )


def apply_faults(offers: dict[str, Offer], faults: Faults, quantities: dict[str, float] | None = None) -> dict[str, Offer]:
    if faults.empty():
        return offers
    out: dict[str, Offer] = {}
    for pid, o in offers.items():
        o = o.model_copy(deep=True)
        if faults._hit(faults.stockout, pid):
            o.available, o.available_amount = False, 0.0
            o.issues += ["out_of_stock", "injected:stockout"]
        pv = faults._val(faults.partial, pid)
        if pv is not None:
            o.available_amount = pv
            o.issues += ["limited_stock", "injected:partial"]
            q = (quantities or {}).get(pid)
            if q is not None and pv < q:
                o.issues.append("partial_stock")
            if pv <= 0:
                o.available = False
        f = faults._val(faults.price_shock, pid)
        if f is not None and o.unit_price is not None:
            o.unit_price = round(o.unit_price * f, 4)
            o.unit_price_gross = round(o.unit_price_gross * f, 4) if o.unit_price_gross is not None else None
            o.comparison_price = round(o.comparison_price * f, 4) if o.comparison_price is not None else None
            o.issues.append(f"injected:price_shock:{f:g}")
        if faults._hit(faults.price_error, pid):
            o.unit_price = o.unit_price_gross = o.comparison_price = None
            o.available = False
            o.issues += ["price_error", "injected:price_error"]
        if faults._hit(faults.cutoff, pid):
            o.cutoff_passed, o.available = True, False
            o.issues += ["cutoff_passed", "injected:cutoff"]
        if faults._hit(faults.sales_block, pid):
            o.sales_block, o.available = True, False
            o.issues += ["sales_block", "injected:sales_block"]
        o.issues = list(dict.fromkeys(o.issues))
        out[pid] = o
    return out


def get_offers_with_meta(
    product_ids: list[str],
    requested_date: date,
    customer_id: str | None = None,
    quantities: dict[str, float] | None = None,
) -> tuple[dict[str, Offer], dict[str, Any]]:
    """Like get_offers, plus provenance meta {source, upstream, fetched_at, faults, ...}."""
    faults = current_faults()
    if faults.latency_ms:
        time.sleep(faults.latency_ms / 1000.0)
    if faults.rate_limit and random.random() < faults.rate_limit:
        raise UpstreamError("injected: upstream rate limit (429)", 429, retry_after=1.0, injected=True)
    if faults.error_rate and random.random() < faults.error_rate:
        raise UpstreamError("injected: upstream error (503)", 503, injected=True)

    ids = list(dict.fromkeys(str(p) for p in product_ids))
    client = get_client()
    before = dict(getattr(client, "stats", {}) or {})
    price = client.price(ids, requested_date, customer_id)
    avail = client.availability(ids, requested_date, customer_id)
    p_by = {str(x.get("productId")): x for x in price.get("products", []) or []}
    a_by = {str(x.get("productId")): x for x in avail.get("products", []) or []}
    offers = {
        pid: merge_offer(pid, requested_date, p_by.get(pid), a_by.get(pid), (quantities or {}).get(pid))
        for pid in ids
    }
    offers = apply_faults(offers, faults, quantities)
    after = dict(getattr(client, "stats", {}) or {})
    calls = after.get("upstream_calls", 0) - before.get("upstream_calls", 0)
    oldest = [x for x in (price.get("_oldest_fetch"), avail.get("_oldest_fetch")) if x]
    fetched_at = datetime.fromtimestamp(min(oldest), tz=timezone.utc) if oldest else datetime.now(timezone.utc)
    meta = {
        "source": "valio-aimo-mock-api",
        "upstream": getattr(client, "base_url", type(client).__name__),
        "endpoints": ["/api/price", "/api/availability"],
        "served_from": "live" if calls else "cache",
        "upstream_calls": calls,
        "fetched_at": fetched_at.isoformat(),
        "valid_for_s": int(getattr(client, "ttl_s", CACHE_TTL_S)),
        "upstream_time_now": avail.get("timeNow"),
        "faults": faults.spec or None,
    }
    return offers, meta


def get_offers(
    product_ids: list[str],
    requested_date: date,
    customer_id: str | None = None,
    quantities: dict[str, float] | None = None,
) -> dict[str, Offer]:
    return get_offers_with_meta(product_ids, requested_date, customer_id, quantities)[0]

