"""Minimal catalog fallback used ONLY while aimo.catalog is not available.

Loads the product JSON once (about 0.6 s) and does token matching on names. No units,
allergens or claims normalisation: that is the catalog engineer's job (aimo.catalog).
"""
from __future__ import annotations

import json
import re
import threading

from aimo import config
from aimo.models import Product, ProductSummary

_lock = threading.Lock()
_index: dict[str, dict] | None = None


def _names(p: dict) -> tuple[str | None, str | None]:
    en = fi = None
    for n in (p.get("synkkaData") or {}).get("names") or []:
        if n.get("language") == "en" and not en:
            en = n.get("value")
        if n.get("language") == "fi" and not fi:
            fi = n.get("value")
    return en, fi


def _load() -> dict[str, dict]:
    global _index
    with _lock:
        if _index is None:
            idx: dict[str, dict] = {}
            if config.PRODUCT_JSON.exists():
                for p in json.loads(config.PRODUCT_JSON.read_text()):
                    if p.get("deleted"):
                        continue
                    en, fi = _names(p)
                    s = p.get("synkkaData") or {}
                    idx[str(p["id"])] = {
                        "id": str(p["id"]),
                        "gtin": p.get("salesUnitGtin") or s.get("gtin"),
                        "name": en or fi or str(p["id"]),
                        "name_fi": fi,
                        "brand": s.get("brand"),
                        "vendor": p.get("vendorName"),
                        "category": p.get("category"),
                        "sales_unit": p.get("salesUnit") or "ST",
                        "temperature_condition": p.get("temperatureCondition"),
                        "country_of_origin": p.get("countryOfOrigin"),
                        "allowed_lot_size": p.get("allowedLotSize"),
                        "_text": f"{en or ''} {fi or ''} {s.get('brand') or ''}".lower(),
                    }
            _index = idx
        return _index


def _summary(rec: dict, score: float | None = None) -> ProductSummary:
    return ProductSummary(**{k: v for k, v in rec.items() if k in ProductSummary.model_fields}, score=score)


def search(query: str, *, claims: list[str] | None = None, category: str | None = None, limit: int = 20, **_) -> list[ProductSummary]:
    toks = [t for t in re.split(r"\W+", (query or "").lower()) if t]
    hits = []
    for rec in _load().values():
        if category and rec.get("category") != category:
            continue
        if not toks:
            continue
        words = set(re.split(r"\W+", rec["_text"]))
        n = sum(1.0 if t in words else 0.5 if len(t) > 3 and t in rec["_text"] else 0.0 for t in toks)
        if n:
            hits.append((n / len(toks), rec))
    hits.sort(key=lambda x: (-x[0], len(x[1]["name"]), x[1]["name"]))
    return [_summary(r, round(s, 3)) for s, r in hits[:limit]]


def get_product(product_id: str) -> Product | None:
    rec = _load().get(str(product_id))
    if rec is None:
        return None
    return Product(**{k: v for k, v in rec.items() if k in Product.model_fields})
