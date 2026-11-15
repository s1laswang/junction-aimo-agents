"""Per-agent price-lookup budgets (anti-scraping). Owner: gateway.

Each agent (customer + agent id) may look up live prices for at most AIMO_OFFER_BUDGET distinct products
(default 200) per AIMO_OFFER_BUDGET_WINDOW_S seconds (default 600 = 10 min), across get_offers, plan_need
and quotes. Re-checking the same products is free; walking the catalog is not. Over budget -> HTTP 429
`query_budget_exceeded` with Retry-After and a plain-language reason. AIMO_OFFER_BUDGET=0 disables.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any

_lock = threading.Lock()
_seen: dict[str, "OrderedDict[str, float]"] = {}


def limits() -> tuple[int, float]:
    try:
        n = int(os.environ.get("AIMO_OFFER_BUDGET", "200"))
        w = float(os.environ.get("AIMO_OFFER_BUDGET_WINDOW_S", "600"))
    except ValueError:
        n, w = 200, 600.0
    return n, w


def _key(customer_id: str, agent_id: str) -> str:
    return f"{customer_id}/{agent_id}"


def _prune(d: "OrderedDict[str, float]", now: float, window: float) -> None:
    while d:
        pid, ts = next(iter(d.items()))
        if now - ts <= window:
            break
        d.popitem(last=False)


def charge(customer_id: str, agent_id: str, product_ids: list[str], *, check_only: bool = False) -> dict[str, Any]:
    """Charge distinct product ids to the agent's window. Raises ServiceError(429) when it would exceed."""
    from aimo.gateway.service import ServiceError

    n, window = limits()
    if n <= 0:
        return {"limit": 0, "used": 0}
    now = time.time()
    k = _key(customer_id, agent_id)
    with _lock:
        d = _seen.setdefault(k, OrderedDict())
        _prune(d, now, window)
        new = [p for p in dict.fromkeys(str(x) for x in product_ids) if p not in d]
        if len(d) + len(new) > n:
            retry = int(max(1, window - (now - next(iter(d.values()))))) if d else int(window)
            raise ServiceError(
                429, "query_budget_exceeded",
                f"Agent {k} has looked up live prices for {len(d)} different products in the last {int(window // 60)} "
                f"minutes; this request would add {len(new)} more (limit {n}). The limit protects Valio Aimo's "
                f"customer-specific price list from bulk scraping. Re-checking products you already looked up is free. "
                f"Retry in {retry} s, or ask Valio Aimo for a bulk price-list agreement.",
                {"limit": n, "window_s": int(window), "used": len(d), "requested_new": len(new), "retry_after": retry})
        if not check_only:
            for p in new:
                d[p] = now
            for p in product_ids:  # refresh recency of re-checked ids
                if p in d:
                    d.move_to_end(p)
                    d[p] = now
        return {"limit": n, "window_s": int(window), "used": len(d)}


def usage(customer_id: str, agent_id: str) -> dict[str, Any]:
    n, window = limits()
    with _lock:
        d = _seen.get(_key(customer_id, agent_id), OrderedDict())
        _prune(d, time.time(), window)
        return {"limit": n, "window_s": int(window), "used": len(d)}


def reset() -> None:
    with _lock:
        _seen.clear()
