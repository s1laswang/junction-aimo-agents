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


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=os.environ["VALIO_API_BASE"].rstrip("/"),
            headers={"X-API-Key": os.environ["VALIO_API_KEY"]},
            timeout=30,
        )
    return _client


def _cache_file(endpoint: str, customer: str, day: date) -> Path:
    root = Path(os.environ.get("AIMO_CACHE_DIR", "var/cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{endpoint}__{customer}__{day.isoformat()}.json"


def _post(endpoint: str, body: dict) -> dict:
    for attempt in range(4):
        try:
            r = _http().post(f"/{endpoint}", json=body)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.json()
        except httpx.TransportError:
            if attempt == 3:
                raise
        time.sleep(0.5 * 2**attempt)
    r.raise_for_status()
    return {}


def _fetch(endpoint: str, customer: str, day: date, ids) -> dict[str, dict]:
    ids = [str(i) for i in dict.fromkeys(ids)]
    path = _cache_file(endpoint, customer, day)
    store = json.loads(path.read_text()) if path.exists() else {}
    missing = [i for i in ids if i not in store]
    for n in range(0, len(missing), CHUNK):
        chunk = missing[n : n + CHUNK]
        data = _post(endpoint, {"customer": customer, "requestedDate": day.isoformat(), "products": chunk})
        for p in data.get("products", []):
            store[str(p["productId"])] = p
    if missing:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store))
        tmp.replace(path)
    return {i: store[i] for i in ids if i in store}


def price(customer: str, requested_date: date, ids) -> dict[str, dict]:
    return _fetch("price", customer, requested_date, ids)


def availability(customer: str, requested_date: date, ids) -> dict[str, dict]:
    return _fetch("availability", customer, requested_date, ids)
