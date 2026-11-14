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


def build() -> int:
    ids = set(DEMO_IDS)
    for k in FIXTURE_SPECS:
        ids |= {p.id for p in oracle.spec_products(oracle.SPECS[k])}
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
