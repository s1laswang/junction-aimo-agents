"""Probe the Valio Aimo mock API for the quirks listed in docs/ASSUMPTIONS.md. 5 small upstream calls
(responses are cached 5 minutes by the backend client).

    uv run python docs/pitch/probe_mock_quirks.py

1. availability for the same 3 products on 3 requested dates: do closingTime / scheduleLines change?
2. price for the same products for two different customer ids: are prices customer-specific?
Prints a compact table; no secrets.
"""
from __future__ import annotations

from datetime import date

from aimo.backend import get_client

IDS = ["463038", "464306", "469363"]
DATES = [date(2026, 11, 16), date(2026, 11, 20), date(2026, 12, 1)]


def by_id(resp: dict) -> dict:
    return {str(x.get("productId")): x for x in resp.get("products") or []}


def main() -> None:
    c = get_client()
    print("== availability by requestedDate")
    for d in DATES:
        r = by_id(c.availability(IDS, d))
        for pid in IDS:
            row = r.get(pid, {})
            lines = [(s.get("availableDate"), s.get("availableAmount")) for s in row.get("scheduleLines") or []]
            print(f"  req={d}  {pid}  closingTime={row.get('closingTime')}  scheduleLines={lines}  "
                  f"model={row.get('logisticModel')}")
    print("== catalogPrice.value by customer")
    for cust in ("hotel-aurora", "school-tampere"):
        r = by_id(c.price(IDS, DATES[0], cust))
        vals = "  ".join(f"{pid}={((r.get(pid) or {}).get('catalogPrice') or {}).get('value')}" for pid in IDS)
        print(f"  customer={cust}  {vals}")


if __name__ == "__main__":
    main()
