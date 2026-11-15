"""Catalog: search, product detail (allergen tri-state), good-better-best substitution, need planning.

See docs/interfaces.md. Public API:
    search(query, *, claims=[], category=None, content_unit=None, limit=20) -> list[ProductSummary]
    get_product(product_id) -> Product | None
    rank_substitutes(need, offers=None, limit=12) -> list[Candidate]
    plan_need(need, offers_fn=None) -> Plan
"""
from __future__ import annotations

import gzip
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Callable, Optional

from aimo.models import Candidate, Need, Offer, Plan, Product, ProductSummary

__all__ = ["search", "get_product", "rank_substitutes", "plan_need", "load_catalog", "catalog_available",
           "catalog_source", "CATALOG_MISSING_MESSAGE"]

FIXTURE_CATALOG_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "snapshot" / "replay" / "catalog"
CATALOG_MISSING_MESSAGE = ("product catalog not found (data/valio_aimo_junction26_product_data.json). Set AIMO_API_KEY "
                           "in .env and run `uv run aimo fetch-data`, or unset AIMO_API_KEY to run the bundled "
                           "replay demo")
_SOURCE = "full"


def _use_fixture_catalog() -> str:
    """Point aimo.config at the committed catalog subset when the full catalog is absent and replay is on
    (AIMO_CATALOG_FIXTURE=1 forces it, =0 disables it). Returns 'full', 'fixture-subset' or 'missing'."""
    from aimo import config

    if config.PRODUCT_JSON.exists():
        return "full"
    flag = os.environ.get("AIMO_CATALOG_FIXTURE", "").strip()
    src = FIXTURE_CATALOG_DIR / (config.PRODUCT_JSON.name + ".gz")
    if flag == "0" or not src.exists():
        return "missing"
    if flag != "1":
        try:
            from aimo.backend import replay
            if replay.replay_dir() is None:
                return "missing"
        except Exception:
            return "missing"
    dst = config.DATA_DIR / "catalog-fixture"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for name in (config.PRODUCT_JSON.name, config.RELIABILITY_CSV.name, config.CO2_CSV.name):
            gz = FIXTURE_CATALOG_DIR / (name + ".gz")
            if not gz.exists():
                continue
            raw = gz.read_bytes()
            stamp = dst / (name + ".md5")
            digest = hashlib.md5(raw).hexdigest()
            if not (dst / name).exists() or not stamp.exists() or stamp.read_text(encoding="utf-8", errors="replace") != digest:
                tmp = dst / (name + ".tmp")
                tmp.write_bytes(gzip.decompress(raw))
                tmp.replace(dst / name)
                stamp.write_text(digest, encoding="utf-8")
    except (OSError, EOFError, ValueError):
        return "missing"
    config.PRODUCT_JSON = dst / config.PRODUCT_JSON.name
    config.RELIABILITY_CSV = dst / config.RELIABILITY_CSV.name
    config.CO2_CSV = dst / config.CO2_CSV.name
    config.CATALOG_DB = dst / "catalog.sqlite"
    return "fixture-subset"


_SOURCE = _use_fixture_catalog()


def catalog_source() -> str:
    """'full' (challenge files in data/), 'fixture-subset' (keyless replay) or 'missing'."""
    from aimo import config

    if not config.PRODUCT_JSON.exists():
        return "missing"
    return "fixture-subset" if _SOURCE == "fixture-subset" else "full"


def catalog_available() -> bool:
    return catalog_source() != "missing"


def load_catalog() -> None:
    from aimo.backend.catalog_store import load_catalog as _lc
    _lc()


def _has_bad_codepoints(s: str) -> bool:
    if "\x00" in s:
        return True
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _sanitize_search_query(query: str) -> str:
    return "".join(
        ch for ch in query[:4096]
        if ch != "\x00" and not (0xD800 <= ord(ch) <= 0xDFFF)
    )


def _opt_str(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    if not _has_bad_codepoints(val):
        return val
    return "".join(
        ch for ch in val
        if ch != "\x00" and not (0xD800 <= ord(ch) <= 0xDFFF)
    )


def _pos_finite_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fv = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    return fv if (math.isfinite(fv) and fv > 0.0) else None


def _nonneg_finite_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fv = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(fv) or fv < 0.0:
        return None
    return 0.0 if fv == 0.0 else fv


def search(query: str, *, claims: list[str] | None = None, category: str | None = None,
           content_unit: str | None = None, limit: int = 20) -> list[ProductSummary]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if claims is not None:
        if (isinstance(claims, (str, bytes, bytearray, dict))
                or not isinstance(claims, (list, tuple, set, frozenset))
                or any(not isinstance(c, str) for c in claims)):
            raise ValueError("claims must be a sequence of strings")
    if category is not None and not isinstance(category, str):
        raise ValueError("category must be a string")
    if content_unit is not None and not isinstance(content_unit, str):
        raise ValueError("content_unit must be a string")
    clean_claims = list(claims) if claims else []
    if any(_has_bad_codepoints(c) for c in clean_claims):
        return []
    if category is not None and _has_bad_codepoints(category):
        return []
    if content_unit is not None and _has_bad_codepoints(content_unit):
        return []
    from aimo.catalog.textsearch import search as _s
    return _s(_sanitize_search_query(query), claims=clean_claims, category=category,
              content_unit=content_unit, limit=min(limit, 10_000))


def get_product(product_id: str) -> Optional[Product]:
    if isinstance(product_id, bool) or not isinstance(product_id, str):
        return None
    if len(product_id) > 128 or _has_bad_codepoints(product_id):
        return None
    pid = product_id.strip()
    if not pid or len(pid) > 128:
        return None
    from aimo.catalog.textsearch import get_rows
    d = get_rows([pid]).get(pid)
    if d is None:
        return None
    return product_from_row(d)


def product_from_row(d: dict[str, Any]) -> Product:
    if not isinstance(d, dict):
        raise ValueError("product row must be a dict")
    raw_id = d.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, str) or _has_bad_codepoints(raw_id):
        raise ValueError("product row must have a valid string id")
    pid = raw_id.strip()
    if not pid or len(pid) > 128:
        raise ValueError("product row must have a non-empty string id")
    from aimo.catalog.textsearch import summary_from_row
    allergens: dict[str, Any] = {}
    raw_allergens = d.get("allergens")
    if isinstance(raw_allergens, dict):
        for k, v in raw_allergens.items():
            if isinstance(k, str) and k:
                sv = getattr(v, "value", v)
                if sv in ("contains", "may_contain", "free", "unknown"):
                    allergens[k] = sv
    source: dict[str, str] = {}
    raw_source = d.get("allergen_source")
    if isinstance(raw_source, dict):
        for k, v in raw_source.items():
            if isinstance(k, str) and k and isinstance(v, str):
                source[k] = v
    try:
        from aimo.catalog.gemini_allergens import cached_inference
        inf = cached_inference(pid)
        if isinstance(inf, dict):
            for g, st in inf.items():
                if isinstance(g, str) and source.get(g) == "none":
                    source[g] = "inferred:gemini"
                    if st in ("contains", "may_contain"):
                        allergens[g] = st
    except Exception:
        pass
    raw_claims = d.get("claims")
    clean_claims = [c for c in raw_claims if isinstance(c, str)] if isinstance(raw_claims, (list, tuple)) else []
    raw_labels = d.get("labels")
    clean_labels = [lb for lb in raw_labels if isinstance(lb, str)] if isinstance(raw_labels, (list, tuple)) else []
    rel = _nonneg_finite_float(d.get("delivery_reliability"))
    if rel is not None and rel > 1.0:
        rel = None
    clean_d: dict[str, Any] = {
        "id": pid,
        "gtin": _opt_str(d.get("gtin")),
        "name": (_opt_str(d.get("name")) or "").strip() or pid,
        "name_fi": _opt_str(d.get("name_fi")),
        "brand": _opt_str(d.get("brand")),
        "vendor": _opt_str(d.get("vendor")),
        "category": _opt_str(d.get("category")),
        "sales_unit": _opt_str(d.get("sales_unit")) or "",
        "content_amount": _pos_finite_float(d.get("content_amount")),
        "content_unit": d.get("content_unit") if d.get("content_unit") in ("L", "KG", "PCS") else None,
        "claims": clean_claims,
        "labels": clean_labels,
        "delivery_reliability": rel,
        "co2_kg_per_kg": _nonneg_finite_float(d.get("co2_kg_per_kg")),
    }
    s = summary_from_row(clean_d)
    nutrition: dict[str, float] = {}
    raw_nutr = d.get("nutrition")
    if isinstance(raw_nutr, dict):
        for k, v in raw_nutr.items():
            if not isinstance(k, str) or not k.strip():
                continue
            fv = _nonneg_finite_float(v)
            if fv is not None:
                nutrition[k] = fv
    raw_temp = d.get("temperature_condition")
    temp_cond = raw_temp if (isinstance(raw_temp, int) and not isinstance(raw_temp, bool)) else None
    lot_size = _pos_finite_float(d.get("allowed_lot_size"))
    return Product(**s.model_dump(), allergens=allergens, allergen_source=source,
                   ingredients_text=_opt_str(d.get("ingredients_text")), nutrition=nutrition,
                   temperature_condition=temp_cond, country_of_origin=_opt_str(d.get("country_of_origin")),
                   allowed_lot_size=lot_size, marketing_text=_opt_str(d.get("marketing_text")))


def rank_substitutes(need: Need, offers: dict[str, Offer] | None = None, limit: int = 12) -> list[Candidate]:
    """Ranks candidates for `need`. If `offers` is None, fetches live offers for the top ~30."""
    from aimo.catalog import plan as P
    from aimo.catalog.substitution import rank_substitutes as _r, validate_need
    validate_need(need)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if offers is not None:
        if not isinstance(offers, dict):
            raise ValueError("offers must be a dict or None")
        for k, v in offers.items():
            if isinstance(k, bool) or not isinstance(k, str):
                raise ValueError("offers keys must be product_id strings")
            if v is not None and not isinstance(v, Offer):
                raise ValueError("offers values must be Offer instances or None")
    if offers is None:
        return P.plan_need(need, limit=limit).candidates
    need, _, _ = P.with_customer_policy(need)
    need, _src = P.with_effective_query(need)
    rows, rel = P.find_candidates(need)
    keep = {pid: rows[pid] for pid in rows if pid in offers or pid in need.preferred_product_ids}
    return _r(need, offers, limit=limit, rows=keep or rows, relevance=rel)


def plan_need(need: Need, offers_fn: Optional[Callable[..., dict[str, Offer]]] = None, limit: int = 12) -> Plan:
    from aimo.catalog.plan import plan_need as _p
    from aimo.catalog.substitution import validate_need
    validate_need(need)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if offers_fn is not None and not callable(offers_fn):
        raise ValueError("offers_fn must be callable or None")
    return _p(need, offers_fn=offers_fn, limit=limit)
