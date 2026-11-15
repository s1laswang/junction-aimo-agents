"""Minimal catalog fallback used ONLY while aimo.catalog is not available.

Loads the product JSON once (about 0.6 s) and does token matching on names. No units,
allergens or claims normalisation: that is the catalog engineer's job (aimo.catalog).
"""
from __future__ import annotations

import json
import math
import re
import threading
from typing import Any

from aimo import config
from aimo.models import Product, ProductSummary

_lock = threading.Lock()
_index: dict[str, dict] | None = None


def _opt_str(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    cleaned = val.replace("\x00", "").strip()
    return cleaned if cleaned else None


def _clean_lot_size(val: Any) -> float | None:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fv = float(val)
    except (OverflowError, TypeError, ValueError):
        return None
    return fv if (math.isfinite(fv) and fv > 0.0) else None


def _clean_temp(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val) and int(val) == val:
        return int(val)
    return None


def _clean_limit(val: Any, max_val: int = 200) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return 0
    if isinstance(val, float):
        if math.isnan(val):
            return 0
        if math.isinf(val):
            return max_val if val > 0 else 0
    if val <= 0:
        return 0
    return min(int(val), max_val)


def _clean_score(val: Any) -> float | None:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fv = float(val)
    except (OverflowError, TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def _names(p: dict) -> tuple[str | None, str | None]:
    en = fi = None
    s = p.get("synkkaData")
    if not isinstance(s, dict):
        return None, None
    names = s.get("names")
    if not isinstance(names, list):
        return None, None
    for n in names:
        if not isinstance(n, dict):
            continue
        lang = n.get("language")
        val = _opt_str(n.get("value"))
        if not val:
            continue
        if lang == "en" and not en:
            en = val
        if lang == "fi" and not fi:
            fi = val
    return en, fi


def _load() -> dict[str, dict]:
    global _index
    with _lock:
        if _index is None:
            idx: dict[str, dict] = {}
            pjson = getattr(config, "PRODUCT_JSON", None)
            raw_items: Any = []
            if pjson is not None:
                try:
                    if pjson.is_file():
                        raw_items = json.loads(pjson.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    raw_items = []
            if isinstance(raw_items, list):
                for p in raw_items:
                    if not isinstance(p, dict) or p.get("deleted"):
                        continue
                    raw_id = p.get("id")
                    if raw_id is None or isinstance(raw_id, (bool, list, dict, set, tuple)):
                        continue
                    pid = str(raw_id).strip()
                    if not pid or "\x00" in pid:
                        continue
                    en, fi = _names(p)
                    s_raw = p.get("synkkaData")
                    s = s_raw if isinstance(s_raw, dict) else {}
                    brand = _opt_str(s.get("brand"))
                    gtin = _opt_str(p.get("salesUnitGtin")) or _opt_str(s.get("gtin"))
                    idx[pid] = {
                        "id": pid,
                        "gtin": gtin,
                        "name": en or fi or pid,
                        "name_fi": fi,
                        "brand": brand,
                        "vendor": _opt_str(p.get("vendorName")),
                        "category": _opt_str(p.get("category")),
                        "sales_unit": _opt_str(p.get("salesUnit")) or "ST",
                        "temperature_condition": _clean_temp(p.get("temperatureCondition")),
                        "country_of_origin": _opt_str(p.get("countryOfOrigin")),
                        "allowed_lot_size": _clean_lot_size(p.get("allowedLotSize")),
                        "_text": f"{en or ''} {fi or ''} {brand or ''}".lower(),
                    }
            _index = idx
        return _index


def _summary(rec: dict, score: float | None = None) -> ProductSummary:
    return ProductSummary(**{k: v for k, v in rec.items() if k in ProductSummary.model_fields}, score=_clean_score(score))


def search(query: str, *, claims: list[str] | None = None, category: str | None = None, limit: int = 20, **_) -> list[ProductSummary]:
    if not isinstance(query, str) or "\x00" in query:
        return []
    lim = _clean_limit(limit, 200)
    if lim <= 0:
        return []
    if category is not None and category != "":
        if not isinstance(category, str) or "\x00" in category:
            return []
    if claims is not None:
        if not isinstance(claims, (list, tuple, set, frozenset)) or any(not isinstance(c, str) or "\x00" in c for c in claims):
            return []
    toks = [t for t in re.split(r"\W+", query[:4096].lower()) if t]
    if not toks:
        return []
    hits = []
    for rec in _load().values():
        if category and rec.get("category") != category:
            continue
        words = set(re.split(r"\W+", rec["_text"]))
        n = sum(1.0 if t in words else 0.5 if len(t) > 3 and t in rec["_text"] else 0.0 for t in toks)
        if n:
            hits.append((n / len(toks), rec))
    hits.sort(key=lambda x: (-x[0], len(x[1]["name"]), x[1]["name"]))
    return [_summary(r, round(s, 3)) for s, r in hits[:lim]]


def get_product(product_id: str) -> Product | None:
    if not isinstance(product_id, str) or "\x00" in product_id:
        return None
    pid = product_id.strip()
    if not pid:
        return None
    rec = _load().get(pid)
    if rec is None:
        return None
    return Product(**{k: v for k, v in rec.items() if k in Product.model_fields})
