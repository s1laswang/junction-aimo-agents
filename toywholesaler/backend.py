"""Nordic Bakery Supply backend: static prices and stock, in the same raw shape as the Valio Aimo
/api/price and /api/availability responses, so the unchanged aimo gateway can use it via
`aimo.backend.set_client(StaticBackendClient())`. Also converts the compact synthetic catalog into the
product-JSON shape that `aimo.backend.catalog_store.load_catalog()` indexes.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CATALOG = json.loads((HERE / "catalog.json").read_text())
HKI = timezone(timedelta(hours=2))          # Europe/Helsinki in November (EET)
TAX = 13.5                                  # Finnish VAT rate for food
SOURCE = "nordic-bakery-supply-static"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _content_kg(name: str) -> float | None:
    from aimo.catalog.units import parse_name_quantity
    q, mult = parse_name_quantity(name)
    if q is None:
        return None
    amt = q.amount * (mult or 1)
    return amt if q.unit == "KG" else None


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
        valid_from, valid_to = "2026-11-01T00:00:00+0200", "9999-12-31T00:00:00+0200"
        items = []
        for pid in product_ids:
            p = self.products.get(str(pid))
            if p is None:
                items.append({"productId": str(pid), "error": True, "errorMessage": "unknown product"})
                continue
            net = float(p["price"])
            gross = round(net * (1 + TAX / 100), 2)
            kg = _content_kg(p["en"])
            item = {"productId": p["id"], "quantity": 1.0,
                    "catalogPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to,
                                     "currency": "EUR"},
                    "priceWithTax": {"value": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "listPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to,
                                  "currency": "EUR"},
                    "tax": {"taxRate": TAX, "taxValue": round(gross - net, 2)}, "netPriceFound": False, "error": False}
            if kg:
                item["comparisonPrice"] = {"value": round(net / kg, 2), "valueWithTax": round(gross / kg, 2),
                                           "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"}
                item["comparisonUnit"] = "KG"
            items.append(item)
        return self._wrap(items, {"customerId": customer or "demo", "currency": "EUR",
                                  "pricingDate": _iso(datetime.combine(requested_date, datetime.min.time(), HKI))})

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None,
                     fresh: bool = False) -> dict:
        items = []
        for pid in product_ids:
            p = self.products.get(str(pid))
            if p is None:
                continue
            terminal = p.get("logistic") == "Terminal"
            # Stock items close the day before at 14:00; Terminal (made-to-order) items three days before.
            close = datetime.combine(requested_date - timedelta(days=3 if terminal else 1),
                                     datetime.min.time(), HKI).replace(hour=14)
            items.append({"productId": p["id"], "closingTime": _iso(close), "salesBlock": False,
                          "logisticModel": "Terminal" if terminal else "Stock",
                          "scheduleLines": [{"salesBlock": False, "availableAmount": float(p["stock"]),
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
