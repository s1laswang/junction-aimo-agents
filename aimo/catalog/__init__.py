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
            if not (dst / name).exists() or not stamp.exists() or stamp.read_text() != digest:
                tmp = dst / (name + ".tmp")
                tmp.write_bytes(gzip.decompress(raw))
                tmp.replace(dst / name)
                stamp.write_text(digest)
    except OSError:
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


def search(query: str, *, claims: list[str] | None = None, category: str | None = None,
           content_unit: str | None = None, limit: int = 20) -> list[ProductSummary]:
    from aimo.catalog.textsearch import search as _s
    return _s(query, claims=claims or [], category=category, content_unit=content_unit, limit=limit)


def get_product(product_id: str) -> Optional[Product]:
    from aimo.catalog.textsearch import get_rows
    d = get_rows([str(product_id)]).get(str(product_id))
    if d is None:
        return None
    return product_from_row(d)


def product_from_row(d: dict[str, Any]) -> Product:
    from aimo.catalog.textsearch import summary_from_row
    allergens = dict(d.get("allergens") or {})
    source = dict(d.get("allergen_source") or {})
    try:  # Gemini-inferred hints (cached only; never makes anything "free")
        from aimo.catalog.gemini_allergens import cached_inference
        inf = cached_inference(d["id"])
        for g, st in (inf or {}).items():
            if source.get(g) == "none":
                source[g] = "inferred:gemini"
                if st in ("contains", "may_contain"):
                    allergens[g] = st          # conservative: an inferred 'contains' still blocks
                # inferred 'free' stays 'unknown' until a human confirms
    except Exception:  # noqa: BLE001
        pass
    s = summary_from_row(d)
    return Product(**s.model_dump(), allergens=allergens, allergen_source=source,
                   ingredients_text=d.get("ingredients_text"), nutrition=d.get("nutrition") or {},
                   temperature_condition=d.get("temperature_condition"), country_of_origin=d.get("country_of_origin"),
                   allowed_lot_size=d.get("allowed_lot_size"), marketing_text=d.get("marketing_text"))


def rank_substitutes(need: Need, offers: dict[str, Offer] | None = None, limit: int = 12) -> list[Candidate]:
    """Ranks candidates for `need`. If `offers` is None, fetches live offers for the top ~30."""
    from aimo.catalog import plan as P
    from aimo.catalog.substitution import rank_substitutes as _r
    if offers is None:
        return P.plan_need(need, limit=limit).candidates
    need, _, _ = P.with_customer_policy(need)
    need, _src = P.with_effective_query(need)
    rows, rel = P.find_candidates(need)
    keep = {pid: rows[pid] for pid in rows if pid in offers or pid in need.preferred_product_ids}
    return _r(need, offers, limit=limit, rows=keep or rows, relevance=rel)


def plan_need(need: Need, offers_fn: Optional[Callable[..., dict[str, Offer]]] = None) -> Plan:
    from aimo.catalog.plan import plan_need as _p
    return _p(need, offers_fn=offers_fn)
