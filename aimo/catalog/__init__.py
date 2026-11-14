"""Catalog: search, product detail (allergen tri-state), good-better-best substitution, need planning.

See docs/interfaces.md. Public API:
    search(query, *, claims=[], category=None, content_unit=None, limit=20) -> list[ProductSummary]
    get_product(product_id) -> Product | None
    rank_substitutes(need, offers=None, limit=12) -> list[Candidate]
    plan_need(need, offers_fn=None) -> Plan
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from aimo.models import Candidate, Need, Offer, Plan, Product, ProductSummary

__all__ = ["search", "get_product", "rank_substitutes", "plan_need", "load_catalog"]


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
