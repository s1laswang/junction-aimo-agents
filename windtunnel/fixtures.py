"""Build the small committed test fixture (tests/fixtures/snapshot): a subset of the recorded snapshot
(public mock API responses only; no secrets) for the demo products and a few product intents.

  uv run python -m windtunnel.fixtures
"""
from __future__ import annotations

import json

from windtunnel import oracle
from windtunnel.config import FIXTURES_DIR
from windtunnel.snapshot import SNAPSHOT_DIR, DEMO_IDS

FIXTURE_SNAPSHOT = FIXTURES_DIR / "snapshot"
FIXTURE_DATES = ("2026-11-13", "2026-11-15", "2026-11-16", "2026-11-17")
FIXTURE_SPECS = ("lf_milk", "plant_drink", "oat_drink", "spread", "gf_pasta")


def catalog_requested_ids() -> set[str]:
    """Ids the team's catalog asks offers for in the e2e milk flow (with and without the usual product)."""
    import os
    from datetime import date
    os.environ.setdefault("AIMO_RECIPE_CHECK", "0")
    from aimo.models import Need
    from windtunnel import run as R
    from windtunnel.snapshot import ReplayClient
    seen: set[str] = set()
    R._CURRENT["client"] = ReplayClient()

    def rec(ids, d, *a, **k):
        seen.update(ids)
        return R._replay_get_offers(ids, d)
    try:
        from aimo.catalog import plan_need
    except Exception:  # noqa: BLE001
        return seen
    for pref in ([], ["463038"]):
        n = Need(customer_id="hotel-aurora", product_query="lactose-free milk", quantity=1000, unit="L",
                 delivery_by=date(2026, 11, 16), required_claims=["FREE_FROM_LACTOSE"], preferred_product_ids=pref)
        try:
            plan_need(n, offers_fn=rec)
        except Exception:  # noqa: BLE001
            pass
    return seen


def build() -> int:
    ids = set(DEMO_IDS)
    for k in FIXTURE_SPECS:
        ids |= {p.id for p in oracle.spec_products(oracle.SPECS[k])}
    ids |= catalog_requested_ids()
    FIXTURE_SNAPSHOT.mkdir(parents=True, exist_ok=True)
    meta = json.loads((SNAPSHOT_DIR / "meta.json").read_text())
    keep = [d for d in meta["dates"] if d in FIXTURE_DATES]
    for d in keep:
        for ep in ("price", "availability"):
            js = json.loads((SNAPSHOT_DIR / f"{ep}_{d}.json").read_text())
            js["products"] = [p for p in js["products"] if p["productId"] in ids]
            (FIXTURE_SNAPSHOT / f"{ep}_{d}.json").write_text(json.dumps(js, indent=0))
    (FIXTURE_SNAPSHOT / "meta.json").write_text(json.dumps(
        {"dates": keep, "time_now": meta["time_now"], "n_ids": len(ids),
         "source": "subset of data/windtunnel/snapshot (public Valio mock API responses)"}, indent=1))
    (FIXTURE_SNAPSHOT / "pool.json").write_text(json.dumps({"ids": sorted(ids), "groups": {"fixture": sorted(ids)}}))
    return len(ids)


if __name__ == "__main__":
    print(build(), "ids ->", FIXTURE_SNAPSHOT)
