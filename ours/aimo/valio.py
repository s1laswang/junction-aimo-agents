import json
import os
import time
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

CHUNK = 1000
_client: httpx.Client | None = None


class UpstreamUnavailable(Exception):
    code = "UPSTREAM_UNAVAILABLE"
    text = "The Valio price and availability service is busy, try again shortly."

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.code}: {detail}")


def _http() -> httpx.Client:
    global _client
    if _client is None:
        base = os.environ.get("VALIO_API_BASE", "").strip()
        key = os.environ.get("VALIO_API_KEY", "").strip()
        if not base or not key:
            raise UpstreamUnavailable("VALIO_API_BASE or VALIO_API_KEY not configured")
        _client = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"X-API-Key": key},
            timeout=30,
        )
    return _client


def _cache_file(endpoint: str, customer: str, day: date) -> Path:
    root = Path(os.environ.get("AIMO_CACHE_DIR", "var/cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{endpoint}__{customer}__{day.isoformat()}.json"


def _wait(r, attempt: int) -> float:
    try:
        return min(float(r.headers["Retry-After"]), 10.0)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.5 * 2**attempt


def _post(endpoint: str, body: dict) -> dict:
    last = "no response"
    for attempt in range(3):
        r = None
        try:
            r = _http().post(f"/{endpoint}", json=body)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.json()
            last = f"HTTP {r.status_code}"
        except httpx.TransportError as e:
            last = type(e).__name__
        except httpx.HTTPStatusError as e:
            raise UpstreamUnavailable(f"HTTP {e.response.status_code}")
        if attempt < 2:
            time.sleep(_wait(r, attempt))
    raise UpstreamUnavailable(last)


def _stale(endpoint: str, customer: str, ids) -> dict[str, dict]:
    root = Path(os.environ.get("AIMO_CACHE_DIR", "var/cache"))
    out = {}
    for f in sorted(root.glob(f"{endpoint}__{customer}__*.json"), reverse=True):
        try:
            store = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for i in ids:
            if i in store and i not in out:
                out[i] = store[i]
    return out


def _fetch(endpoint: str, customer: str, day: date, ids) -> dict[str, dict]:
    ids = [str(i) for i in dict.fromkeys(ids)]
    path = _cache_file(endpoint, customer, day)
    store = json.loads(path.read_text()) if path.exists() else {}
    missing = [i for i in ids if i not in store]
    if not missing:
        return {i: store[i] for i in ids if i in store}
    try:
        for n in range(0, len(missing), CHUNK):
            chunk = missing[n : n + CHUNK]
            data = _post(endpoint, {"customer": customer, "requestedDate": day.isoformat(), "products": chunk})
            for p in data.get("products", []):
                store[str(p["productId"])] = p
    except UpstreamUnavailable:
        stale = _stale(endpoint, customer, [i for i in missing if i not in store])
        if not stale and not any(i in store for i in ids):
            raise
        store = {**stale, **store}
    if missing:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store))
        tmp.replace(path)
    return {i: store[i] for i in ids if i in store}


def price(customer: str, requested_date: date, ids) -> dict[str, dict]:
    return _fetch("price", customer, requested_date, ids)


def availability(customer: str, requested_date: date, ids) -> dict[str, dict]:
    return _fetch("availability", customer, requested_date, ids)
