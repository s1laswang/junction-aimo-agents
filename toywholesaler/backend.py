"""Nordic Bakery Supply backend: static prices and stock, in the same raw shape as the Valio Aimo
/api/price and /api/availability responses, so the unchanged aimo gateway can use it via
`aimo.backend.set_client(StaticBackendClient())`. Also converts the compact synthetic catalog into the
product-JSON shape that `aimo.backend.catalog_store.load_catalog()` indexes.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CATALOG = json.loads((HERE / "catalog.json").read_text())
HKI = timezone(timedelta(hours=2))          # Europe/Helsinki in November (EET)
TAX = 13.5                                  # Finnish VAT rate for food
SOURCE = "nordic-bakery-supply-static"
_ZERO_MULT_RE = re.compile(r"(?<![\d.,])0+\s*[x×*]\s*\d", re.IGNORECASE)


def _iso(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}{dt.strftime('%z')}"


def _content_kg(name: str) -> float | None:
    if not isinstance(name, str) or not name.strip() or _ZERO_MULT_RE.search(name):
        return None
    from aimo.catalog.units import parse_name_quantity
    q, mult = parse_name_quantity(name)
    if q is None or q.unit != "KG":
        return None
    if isinstance(q.amount, bool) or not isinstance(q.amount, (int, float)):
        return None
    try:
        base_amt = float(q.amount)
    except OverflowError:
        return None
    if not math.isfinite(base_amt) or base_amt <= 0.0:
        return None
    if mult is None:
        factor = 1.0
    else:
        if isinstance(mult, bool) or not isinstance(mult, (int, float)):
            return None
        try:
            factor = float(mult)
        except OverflowError:
            return None
        if not math.isfinite(factor) or factor <= 0.0:
            return None
    amt = base_amt * factor
    if not math.isfinite(amt) or amt <= 0.0:
        return None
    return amt


def _validate_request(product_ids: Any, requested_date: Any) -> list[str]:
    if not isinstance(requested_date, date) or isinstance(requested_date, datetime):
        raise ValueError("requested_date must be a date")
    if isinstance(product_ids, (str, bytes, bytearray, dict, bool)):
        raise ValueError("product_ids must be a non-empty iterable of product ID strings")
    try:
        raw_ids = list(product_ids)
    except TypeError as exc:
        raise ValueError("product_ids must be a non-empty iterable of product ID strings") from exc
    if not raw_ids:
        raise ValueError("product_ids must not be empty")
    out: list[str] = []
    for pid in raw_ids:
        if isinstance(pid, bool) or not isinstance(pid, str):
            raise ValueError("each product_id must be a non-empty string")
        cleaned = pid.strip()
        if not cleaned or len(cleaned) > 64 or any(ch.isspace() for ch in cleaned) or "\x00" in cleaned:
            raise ValueError("each product_id must be a non-empty string without whitespace")
        out.append(cleaned)
    return out


class StaticBackendClient:
    """BackendClient protocol (aimo/backend/__init__.py): price() / availability() -> raw JSON."""

    base_url = "static://nordic-bakery-supply"
    ttl_s = 300

    def __init__(self) -> None:
        self.products = {p["id"]: p for p in CATALOG["products"]}
        self.stats = {"upstream_calls": 0}

    def _wrap(self, items: list[dict], meta: dict) -> dict:
        now = time.time()
        self.stats["upstream_calls"] += 1
        return {**meta, "products": [{**i, "_fetched_at": now, "_meta": meta} for i in items], "_oldest_fetch": now}

    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict:
        ids = _validate_request(product_ids, requested_date)
        valid_from, valid_to = "2026-11-01T00:00:00+0200", "9999-12-31T00:00:00+0200"
        items = []
        for pid in ids:
            p = self.products.get(pid)
            if p is None:
                items.append({"productId": pid, "error": True, "errorMessage": "unknown product"})
                continue
            raw_price = p.get("price")
            if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
                items.append({"productId": pid, "error": True, "errorMessage": "invalid price"})
                continue
            try:
                net = float(raw_price)
            except OverflowError:
                items.append({"productId": pid, "error": True, "errorMessage": "invalid price"})
                continue
            if not math.isfinite(net) or net <= 0.0:
                items.append({"productId": pid, "error": True, "errorMessage": "invalid price"})
                continue
            gross = round(net * (1 + TAX / 100), 2)
            if not math.isfinite(gross) or gross <= 0.0:
                items.append({"productId": pid, "error": True, "errorMessage": "invalid price"})
                continue
            kg = _content_kg(p.get("en", ""))
            item = {"productId": p["id"], "quantity": 1.0,
                    "catalogPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to,
                                     "currency": "EUR"},
                    "priceWithTax": {"value": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "listPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to,
                                  "currency": "EUR"},
                    "tax": {"taxRate": TAX, "taxValue": round(gross - net, 2)}, "netPriceFound": False, "error": False}
            if kg is not None and math.isfinite(kg) and kg > 0:
                comp_net = round(net / kg, 2)
                comp_gross = round(gross / kg, 2)
                if math.isfinite(comp_net) and math.isfinite(comp_gross) and comp_net > 0 and comp_gross > 0:
                    item["comparisonPrice"] = {"value": comp_net, "valueWithTax": comp_gross,
                                               "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"}
                    item["comparisonUnit"] = "KG"
            items.append(item)
        return self._wrap(items, {"customerId": customer or "demo", "currency": "EUR",
                                  "pricingDate": _iso(datetime.combine(requested_date, datetime.min.time(), HKI))})

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None,
                     fresh: bool = False) -> dict:
        ids = _validate_request(product_ids, requested_date)
        items = []
        for pid in ids:
            p = self.products.get(pid)
            if p is None:
                continue
            terminal = p.get("logistic") == "Terminal"
            days_back = 3 if terminal else 1
            if requested_date.toordinal() <= days_back:
                close = datetime.combine(date.min, datetime.min.time(), HKI)
            else:
                close = datetime.combine(requested_date - timedelta(days=days_back), datetime.min.time(), HKI).replace(hour=14)
            raw_stock = p.get("stock", 0)
            if isinstance(raw_stock, (int, float)) and not isinstance(raw_stock, bool):
                try:
                    stock_val = float(raw_stock)
                except OverflowError:
                    stock_val = 0.0
            else:
                stock_val = 0.0
            if not math.isfinite(stock_val) or stock_val <= 0.0:
                stock_val = 0.0
            items.append({"productId": p["id"], "closingTime": _iso(close), "salesBlock": False,
                          "logisticModel": "Terminal" if terminal else "Stock",
                          "scheduleLines": [{"salesBlock": False, "availableAmount": stock_val,
                                             "availableDate": requested_date.isoformat(), "salesUnit": "ST"}]})
        return self._wrap(items, {"timeNow": _iso(datetime.now(HKI)),
                                  "requestedDate": _iso(datetime.combine(requested_date, datetime.min.time(), HKI))})


def product_json() -> list[dict[str, Any]]:
    """Compact synthetic catalog -> the product-JSON shape catalog_store.normalize() reads."""
    from aimo.catalog.allergens import GS1_GROUPS

    out = []
    for p in CATALOG["products"]:
        contains = set(p.get("contains") or [])
        groups_contained = {g for g, (hd, members) in GS1_GROUPS.items() if contains & set(members)}
        allergen_vals = [{"id": c, "unit": "CONTAINS"} for c in sorted(contains)]
        allergen_vals += [{"id": GS1_GROUPS[g][0][0], "unit": "FREE_FROM"} for g in GS1_GROUPS if g not in groups_contained]
        out.append({
            "id": p["id"], "deleted": False, "salesUnit": "ST", "baseUnit": "ST", "category": p["category"],
            "vendorName": CATALOG["vendor"], "allowedLotSize": 1, "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "names": [{"language": "en", "value": p["en"]}, {"language": "fi", "value": p["fi"]}],
                "brand": p["brand"],
                "classifications": [{"name": "allergen", "values": allergen_vals},
                                    {"name": "nutritionalClaim", "values": [{"id": c} for c in p.get("claims") or []]}],
                "keyIngredients": [{"language": "en", "value": p["ingredients"]}],
                "marketingTexts": [{"language": "en", "value": f"{p['en']} from {CATALOG['vendor']}."}],
            },
        })
    return out
