from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog, service
from aimo.models import Offer, Order, PolicyDecision, Quote, QuoteLine
from aimo.trust.audit import AuditLog, _clock_now, clock_label

from test_gateway import APPROVER, H, FixtureClient

D = "2026-11-16"


def _synth_products() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    return [
        {
            "id": "464306",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": "Valio Eila semi skimmed milk drink 1 l lactose free"}],
                "unitConversions": base_1l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "FREE_FROM"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                ],
                "keyIngredients": [{"language": "en", "value": "Valio Eila semi skimmed milk drink 1 l lactose free"}],
                "marketingTexts": [{"language": "en", "value": "Valio Eila semi skimmed milk drink 1 l lactose free"}],
            },
        }
    ]


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("audit-clock-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, synth_catalog_dir):
    store.close_all()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-pin")
    for k in ("AIMO_FAULTS", "AIMO_AGENT_KEYS", "AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_REPLAY", "AIMO_REPLAY_CLOCK", "AIMO_ENV"):
        monkeypatch.delenv(k, raising=False)
    if synth_catalog_dir is not None:
        pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
        cdb = synth_catalog_dir / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)
    store.close_all()
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def api():
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"}, raise_server_exceptions=False)


def _post_quote(api: TestClient, qty: float = 10, headers: dict[str, str] | None = None):
    hdrs = {**H, **(headers or {})}
    return api.post("/v1/quotes", headers=hdrs, json={"delivery_date": D, "lines": [{"product_id": "464306", "qty_sales_units": qty}]})


def test_audit_report_uses_simulated_env_clock_and_preserves_wall_ts(api, monkeypatch):
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:16:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    r = _post_quote(api, qty=10)
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["created_at"].startswith("2026-11-14T12:16"), q["created_at"]

    rep_res = api.get(f"/v1/audit/report/{q['quote_id']}")
    assert rep_res.status_code == 200
    rep = rep_res.text
    assert "simulated clock (AIMO_NOW); real wall time is in each event's data.clock.wall_ts" in rep, rep[:500]
    assert "**14:16:00**" in rep, rep[:600]
    assert "times in Helsinki (EET)" in rep

    evs = api.get("/v1/audit", params={"subject_id": q["quote_id"]}).json()
    assert evs
    ev = evs[0]
    assert str(ev["ts"]).startswith("2026-11-14T12:16")
    assert ev["data"]["clock"]["mode"] == "env"
    assert ev["data"]["clock"]["wall_ts"]
    assert api.get("/v1/audit/verify").json()["ok"] is True

    pack = AuditLog().evidence_pack(q["quote_id"])
    assert pack["generated_at"].startswith("2026-11-14T12:16")


def test_audit_report_real_clock_label(api):
    r = _post_quote(api, qty=10)
    assert r.status_code == 200, r.text
    q = r.json()
    rep = api.get(f"/v1/audit/report/{q['quote_id']}").text
    subtitle = rep.split("\n")[2]
    assert "real clock" in subtitle, subtitle
    assert "UTC" not in subtitle, subtitle
    evs = api.get("/v1/audit", params={"subject_id": q["quote_id"]}).json()
    assert evs and "clock" not in evs[0]["data"]


def test_header_clock_and_mixed_clocks_sim_marker(api):
    r_sim = _post_quote(api, qty=1000, headers={"X-Aimo-Now": "2026-11-14T14:16:00+02:00"})
    assert r_sim.status_code == 200, r_sim.text
    q = r_sim.json()
    assert q["created_at"].startswith("2026-11-14T12:16")

    rep_only_header = api.get(f"/v1/audit/report/{q['quote_id']}").text
    assert "simulated clock (X-Aimo-Now)" in rep_only_header
    assert " (sim)" not in rep_only_header

    AuditLog().append("human:anna@hotel-aurora.example", "audit.note", q["quote_id"], "Reviewed quote on real clock.")

    rep_mixed = api.get(f"/v1/audit/report/{q['quote_id']}").text
    assert "mixed clocks: events marked (sim) use the simulated clock (X-Aimo-Now), the rest real time" in rep_mixed
    assert "**14:16:00** (sim)," in rep_mixed
    real_lines = [ln for ln in rep_mixed.splitlines() if ln.startswith("- **") and " (sim)," not in ln]
    assert real_lines and "Reviewed quote on real clock." in real_lines[0]


def test_clock_label_variants_and_malformed_event_data():
    assert clock_label([]) == "real clock"
    assert clock_label([SimpleNamespace(data="not-a-dict"), SimpleNamespace(data={"clock": "bad"})]) == "real clock"
    replay_ev = SimpleNamespace(data={"clock": {"mode": "replay", "wall_ts": "2026-11-14T12:00:00+00:00"}})
    assert clock_label([replay_ev]) == "simulated clock (replay snapshot); real wall time is in each event's data.clock.wall_ts"
    real_ev = SimpleNamespace(data={})
    assert clock_label([replay_ev, real_ev]) == "mixed clocks: events marked (sim) use the simulated clock (replay snapshot), the rest real time"
    ts, clk = _clock_now()
    assert ts.tzinfo is not None and clk is None


@pytest.mark.parametrize("bad_seq", [0, -1, -999, True, False, "1", 1.5, None, [], 10**30])
def test_tamper_and_untamper_reject_bad_seq(tmp_path, bad_seq):
    log = AuditLog(tmp_path / "audit.sqlite")
    log.append("system:test", "test.event", "S-1", "Recorded event")
    with pytest.raises(KeyError, match="no audit event"):
        log.tamper_for_demo(bad_seq)
    with pytest.raises(KeyError, match="no audit event"):
        log.untamper_for_demo(bad_seq)
    assert log.verify()["ok"] is True


def test_tamper_and_untamper_http_and_production_guards(api, tmp_path, monkeypatch):
    q = _post_quote(api, qty=10).json()
    assert q["quote_id"]
    for bad in (0, -1):
        r_t = api.post(f"/v1/demo/tamper/{bad}", headers={"X-Approver-Pin": "test-pin"})
        assert r_t.status_code == 404 and r_t.json()["error"] == "audit_event_not_found"
        r_u = api.post(f"/v1/demo/untamper/{bad}", headers={"X-Approver-Pin": "test-pin"})
        assert r_u.status_code == 404 and r_u.json()["error"] == "audit_event_not_found"
    r_missing = api.post("/v1/demo/tamper/999999", headers={"X-Approver-Pin": "test-pin"})
    assert r_missing.status_code == 404 and r_missing.json()["error"] == "audit_event_not_found"
    r_un_missing = api.post("/v1/demo/untamper/999999", headers={"X-Approver-Pin": "test-pin"})
    assert r_un_missing.status_code == 200 and r_un_missing.json()["restored"] is False

    log = AuditLog(tmp_path / "prod_audit.sqlite")
    ev = log.append("system:test", "test.event", "S-prod", "Recorded event")
    monkeypatch.setenv("AIMO_ENV", "production")
    with pytest.raises(PermissionError):
        log.tamper_for_demo(ev.seq)
    with pytest.raises(PermissionError):
        log.untamper_for_demo(ev.seq)


def test_related_subjects_and_empty_subject_guards(tmp_path):
    log = AuditLog(tmp_path / "rel.sqlite")
    log.append("system:test", "a", "Q-1", "Quote created")
    log.append("system:test", "b", "ORD-1", "Order created", related_ids=["Q-1"])

    assert log.related_subjects("") == set()
    assert log.related_subjects("   ") == set()
    assert log.related_subjects(None) == set()
    assert log.events("") == []
    assert "_No events recorded for this subject._" in log.render("")

    assert log.related_subjects("Q-1", max_hops=0) == {"Q-1"}
    assert log.related_subjects("Q-1", max_hops=-5) == {"Q-1"}
    assert log.related_subjects("Q-1", max_hops=True) == {"Q-1"}
    assert log.related_subjects("Q-1", max_hops=2) == {"Q-1", "ORD-1"}


def _sample_order(idx: int, now: datetime) -> Order:
    dec = PolicyDecision(outcome="auto_approve", policy_id="hotel-aurora", policy_version="1.0.0")
    return Order(
        order_id=f"ORD-TEST-{idx:04d}",
        quote_id=f"Q-TEST-{idx:04d}",
        quote_hash="0" * 64,
        customer_id="hotel-aurora",
        agent_id="buyer-1",
        status="placed",
        decision=dec,
        created_at=now + timedelta(seconds=idx),
        updated_at=now + timedelta(seconds=idx),
    )


def test_orders_clock_close_all_and_list_orders_bad_limits(monkeypatch):
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:16:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")

    rec = store.record_approval("ORD-1", "approve", "anna@hotel-aurora.example", "ok", "a" * 64)
    assert rec["ts"].startswith("2026-11-14T12:16")
    fb = store.fallback_audit_append({"actor": "system:test", "action": "test", "subject_id": "ORD-1", "summary": "ok", "data": {}})
    assert fb["ts"].startswith("2026-11-14T12:16")

    base_ts = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        store.save_order(_sample_order(i, base_ts))

    assert len(store.list_orders(limit=0)) == 1
    assert len(store.list_orders(limit=-10)) == 1
    assert len(store.list_orders(limit=float("-inf"))) == 1
    assert len(store.list_orders(limit=2)) == 2

    for bad_lim in (True, False, "abc", None, 2.5, float("nan"), float("inf"), 5000, 10**30):
        res = store.list_orders(limit=bad_lim)
        assert len(res) == 5

    assert store.list_orders(status=["placed"]) == []
    assert store.list_orders(customer_id={"bad": 1}) == []

    assert store._conns
    store.close_all()
    assert store._conns == {}
    assert len(store.list_orders(limit=3)) == 3


def test_confirm_lines_nan_negative_inf_zero_and_bool_available_amount():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    q = Quote(
        quote_id="Q-1",
        customer_id="hotel-aurora",
        agent_id="buyer-1",
        delivery_date=date.fromisoformat(D),
        total_net=10.0,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        lines=[QuoteLine(product_id="464306", product_name="Milk", qty_sales_units=10.0, sales_unit="ST", unit_price=1.0, line_total=10.0)],
    )

    def _offer_with_avail(val: Any) -> dict[str, Any]:
        o = Offer.model_construct(
            product_id="464306",
            requested_date=date.fromisoformat(D),
            available=True,
            available_amount=val,
            sales_unit="ST",
            unit_price=1.0,
            fetched_at=now,
            issues=[],
        )
        return {"464306": o}

    for err_avail in (float("nan"), -5.0, -0.01, float("-inf"), True, False, "bad"):
        hdr, lines = store.confirm_lines(q, _offer_with_avail(err_avail))
        assert hdr == "RE" and len(lines) == 1
        assert lines[0].code == "RE" and lines[0].qty_confirmed == 0.0
        assert math.isfinite(lines[0].qty_confirmed)
        assert "availability_error" in lines[0].note

    hdr_zero, lines_zero = store.confirm_lines(q, _offer_with_avail(0.0))
    assert hdr_zero == "RE" and lines_zero[0].code == "RE" and lines_zero[0].qty_confirmed == 0.0
    assert "no stock" in lines_zero[0].note

    hdr_inf, lines_inf = store.confirm_lines(q, _offer_with_avail(float("inf")))
    assert hdr_inf == "AP" and lines_inf[0].code == "AP" and lines_inf[0].qty_confirmed == 10.0
    assert math.isfinite(lines_inf[0].qty_confirmed)

    hdr_part, lines_part = store.confirm_lines(q, _offer_with_avail(4.0))
    assert hdr_part == "CA" and lines_part[0].code == "CA" and lines_part[0].qty_confirmed == 4.0
