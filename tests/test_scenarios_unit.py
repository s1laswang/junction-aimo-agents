"""Fast wind tunnel unit tests (< 30 s, laptop). Owner: windtunnel."""
from __future__ import annotations

from datetime import date

import pytest

from windtunnel.snapshot import Faults, ReplayClient, offers

MON = date(2026, 11, 16)


def test_fault_spec_parses_gateway_syntax():
    f = Faults.parse("stockout:463038;price_shock:464306:1.4;error_rate:0.2;latency:500;bogus:1")
    assert f.stockout == {"463038"}
    assert f.price_shock == {"464306": 1.4}
    assert f.error_rate == 0.2 and f.latency_ms == 500
    assert f.unknown == ["bogus:1"]


def test_replay_matches_recorded_demo_facts(fixture_snapshot):
    c = ReplayClient(snapshot_dir=fixture_snapshot)
    o = offers(["463038", "464306", "469363", "462775"], MON, c)
    # PLAN.md demo facts, verified against the live mock API when recorded
    assert o["463038"].cutoff_passed and not o["463038"].available       # Terminal, cut-off Thu 12 Nov 15:00
    assert o["464306"].available and o["464306"].unit_price == 1.01        # EUR 1.01 / L
    assert o["469363"].available_amount == 384
    assert o["462775"].available_amount == 51


def test_faults_apply(fixture_snapshot):
    c = ReplayClient("stockout:464306;price_shock:469363:2.0;partial:462775:10", snapshot_dir=fixture_snapshot)
    o = offers(["464306", "469363", "462775"], MON, c)
    assert not o["464306"].available and "not_available" in o["464306"].issues
    assert o["469363"].unit_price == pytest.approx(4.96)
    assert o["462775"].available_amount == 10


def test_error_rate_is_deterministic(fixture_snapshot):
    def errors(seed):
        c = ReplayClient("error_rate:0.5", seed=seed, snapshot_dir=fixture_snapshot)
        n = 0
        for _ in range(20):
            try:
                c.price(["464306"], MON)
            except Exception:  # noqa: BLE001
                n += 1
        return n
    assert errors(1) == errors(1) and 0 < errors(1) < 20


def test_impossible_dates_have_no_supply(fixture_snapshot):
    c = ReplayClient(snapshot_dir=fixture_snapshot)
    for d in (date(2026, 11, 13), date(2026, 11, 15)):
        o = offers(["464306", "469363"], d, c)
        assert not any(x.available for x in o.values())


def test_spend_label_bands():
    from windtunnel.customers import CUSTOMERS
    from windtunnel.oracle import spend_label
    hotel = CUSTOMERS["hotel-aurora"]
    assert spend_label(100, hotel)[0] == ["auto_approve"]
    assert spend_label(510, hotel)[0] == ["auto_approve", "needs_approval"]      # within 15% of the limit
    assert spend_label(1010, hotel)[0] == ["needs_approval"]                     # the 1,000 L milk demo
    assert spend_label(9000, hotel)[0] == ["reject"]
    assert spend_label(100, CUSTOMERS["hospital-meilahti"])[1] == ["spend_threshold_unknown"]


@pytest.mark.needs_data
def test_allergen_reader_and_data_conflict():
    from windtunnel import products as P
    c = P.catalog()
    assert c["464306"].allergens["milk"] == "contains"
    assert c["460000"].allergens["eggs"] == "contains" and c["460000"].allergens["mustard"] == "contains"
    # structured data says nut FREE_FROM, ingredients say ALMOND: the oracle must not call it safe
    alm = c["465793"]
    assert alm.structured_allergens["nuts"] == "free"
    assert alm.allergens["nuts"] == "contains"
    assert any(x.startswith("nuts:") for x in alm.allergen_conflicts)


@pytest.mark.needs_data
def test_violation_invariants():
    from windtunnel.generate import skeleton
    from windtunnel.oracle import allergen_violations, violations
    from windtunnel.schema import LineOut, RunResult
    sc = skeleton(3, "allergen_school")
    r = RunResult(scenario_id=sc.scenario_id, family=sc.family, customer_id=sc.persona.customer_id, mode="pipeline",
                  sut="test", outcome="auto_approve", order_placed=True, total_net=10_000.0,
                  lines=[LineOut(product_id="464306", qty_sales_units=10, unit_price=1.01, line_total=10.1)])
    v = violations(sc, r)
    assert "auto_over_spend_limit" in v and "allergen_contains:milk" in v
    assert allergen_violations(sc, r)
    r.order_placed, r.outcome = False, "needs_approval"
    assert violations(sc, r) == []                 # escalated: a human sees it, no policy violation


@pytest.mark.needs_data
def test_pipeline_baseline_smoke():
    """Generate a handful of skeletons and run the naive baseline end to end offline."""
    from windtunnel.generate import skeleton
    from windtunnel.run import run_pipeline
    from windtunnel.score import score
    scs = [skeleton(i, f) for i, f in enumerate(["hotel_bulk", "impossible_date", "stockout_substitute"])]
    rs, meta = run_pipeline(scs, "baseline")
    assert meta["sut"] == "baseline" and len(rs) == 3
    s = score({x.scenario_id: x for x in scs}, rs)
    assert s["n"] == 3
