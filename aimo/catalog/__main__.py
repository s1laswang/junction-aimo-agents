"""Catalog CLI.

    uv run python -m aimo.catalog build        # SQLite + FTS (2.5 s), Gemini embeddings (~45 s), allergen hints (~50 s)
    uv run python -m aimo.catalog search lactose-free milk
    uv run python -m aimo.catalog demo         # the 1,000 L lactose-free milk plan (live mock API)

The Gemini steps are skipped if their cache files already exist (they are network-bound,
<= 8 concurrent requests, and logged to the Gemini usage ledger).
"""
from __future__ import annotations

import sys
from datetime import date


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "help"
    if cmd == "build":
        from aimo.backend.catalog_store import load_catalog
        load_catalog()
        print("catalog.sqlite ready")
        from aimo.catalog import embeddings
        if not embeddings.available():
            embeddings.build()
        else:
            print("embeddings present")
        from aimo.catalog import gemini_allergens
        if not gemini_allergens.CACHE.exists() or "--force" in argv:
            gemini_allergens.precompute()
        else:
            print("allergen hints present")
    elif cmd == "search":
        from aimo.catalog import search
        for p in search(" ".join(argv[1:]) or "lactose-free milk", limit=15):
            print(f"{p.id}  {p.score:>7}  {p.content_amount} {p.content_unit}/{p.sales_unit}  {p.name}")
    elif cmd == "demo":
        from aimo.catalog import plan_need
        from aimo.models import Need
        p = plan_need(Need(customer_id="hotel-aurora", product_query="lactose-free milk",
                           required_claims=["FREE_FROM_LACTOSE"], quantity=1000, unit="L",
                           delivery_by=date(2026, 11, 16), preferred_product_ids=["463038"]))
        print(p.explanation)
        for c in p.candidates:
            print(f"  {c.tier:9} {c.product.id} {c.rank_score:.3f} {','.join(c.flags):40} {c.reasons[0][:90]}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
