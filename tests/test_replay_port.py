"""Unit and route tests for S1 replay port: replay modes, gzipped snapshots, simulated delivery dates,
fixture catalog subset, and 503 catalog_missing after customer auth."""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient
import pytest

import aimo.catalog as cat
from aimo import config
from aimo.backend import clock, replay
from aimo.backend.client import UpstreamError
from aimo.gateway.app import create_app


def test_replay_mode_and_replay_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    assert replay.mode() == "auto"
    assert replay.replay_dir() == replay.BUNDLED_DIR

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    assert replay.mode() == "off"
    assert replay.replay_dir() is None

    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_API_KEY", "live-test-key")
    monkeypatch.setattr(config, "AIMO_API_KEY", "live-test-key")
    assert replay.mode() == "off"
    assert replay.replay_dir() is None
    monkeypatch.delenv("AIMO_REPLAY_FALLBACK", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert replay.fallback_dir() == replay.BUNDLED_DIR

    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    assert replay.mode() == "forced"
    assert replay.replay_dir() == replay.BUNDLED_DIR

    monkeypatch.setenv("AIMO_REPLAY", "0")
    assert replay.mode() == "off"
    assert replay.replay_dir() is None

    custom = tmp_path / "custom-snap"
    custom.mkdir()
    monkeypatch.setenv("AIMO_REPLAY", str(custom))
    assert replay.mode() == "forced"
    assert replay.replay_dir() == custom


def test_gzipped_replay_client_serves_bundled_and_synthetic(tmp_path):
    rc = replay.ReplayClient(replay.BUNDLED_DIR)
    assert rc.dates() == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]

    pr = rc.price(["463038", "464306"], date(2026, 11, 16))
    assert pr["_replay"] is True
    assert {p["productId"] for p in pr["products"]} == {"463038", "464306"}

    av = rc.availability(["463038", "464306"], date(2026, 11, 16))
    assert av["_replay"] is True
    assert av.get("timeNow")
    assert rc.stats["replay_calls"] == 2

    with pytest.raises(UpstreamError) as exc:
        rc.price(["464306"], date(2026, 12, 25))
    assert exc.value.status_code == 503

    snap = tmp_path / "gzsnap"
    snap.mkdir()
    (snap / "meta.json").write_text(json.dumps({"time_now": "2026-11-14T12:00:00+02:00", "n_ids": 1}))
    p_blob = json.dumps({"products": [{"productId": "123", "salesUnitPrice": 4.5}]}).encode()
    a_blob = json.dumps({"timeNow": "2026-11-14T12:00:00+02:00",
                         "products": [{"productId": "123", "availableAmount": 10}]}).encode()
    (snap / "price_2026-11-18.json.gz").write_bytes(gzip.compress(p_blob))
    (snap / "availability_2026-11-18.json.gz").write_bytes(gzip.compress(a_blob))

    gz_client = replay.ReplayClient(snap)
    assert gz_client.dates() == ["2026-11-18"]
    out = gz_client.price(["123"], date(2026, 11, 18))
    assert out["products"][0]["salesUnitPrice"] == 4.5


def test_clock_next_delivery_day_cutoff_and_weekends():
    before_sun_cutoff = datetime.fromisoformat("2026-11-15T14:00:00+02:00")
    assert clock.next_delivery_day(before_sun_cutoff) == date(2026, 11, 16)

    after_sun_cutoff = datetime.fromisoformat("2026-11-15T17:30:00+02:00")
    assert clock.next_delivery_day(after_sun_cutoff) == date(2026, 11, 17)

    fri_evening = datetime.fromisoformat("2026-11-13T18:00:00+02:00")
    assert clock.next_delivery_day(fri_evening) == date(2026, 11, 16)

    sat_noon = datetime.fromisoformat("2026-11-14T12:00:00+02:00")
    assert clock.next_delivery_day(sat_noon, weekday=2) == date(2026, 11, 18)

    late_utc = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)
    assert clock.today_local(late_utc) == date(2026, 11, 15)


def test_use_fixture_catalog_into_tmp_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", tmp_path / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", tmp_path / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", tmp_path / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", tmp_path / "catalog.sqlite")

    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    assert cat._use_fixture_catalog() == "missing"

    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    assert cat._use_fixture_catalog() == "missing"

    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setenv("AIMO_REPLAY", "1")

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    monkeypatch.setattr(cat, "_SOURCE", src)
    assert cat.catalog_source() == "fixture-subset"
    assert cat.catalog_available() is True

    dst = tmp_path / "catalog-fixture"
    assert config.PRODUCT_JSON == dst / "valio_aimo_junction26_product_data.json"
    assert config.PRODUCT_JSON.exists()
    assert (dst / "valio_aimo_junction26_delivery_reliability.csv").exists()
    assert (dst / "valio_aimo_junction26_carbon_footprint.csv").exists()
    assert config.CATALOG_DB == dst / "catalog.sqlite"


def test_gateway_catalog_missing_503_after_customer_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", tmp_path / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    assert not cat.catalog_available()

    client = TestClient(create_app(with_mcp=False))
    need_body = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "quantity": 10,
        "unit": "L",
        "delivery_by": "2026-11-16",
    }

    r_search = client.get("/v1/products/search", params={"q": "milk"}, headers={"X-Customer-Id": "hotel-aurora"})
    assert r_search.status_code == 503
    assert r_search.json()["error"] == "catalog_missing"

    r_plan = client.post("/v1/plan", json=need_body,
                         headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"})
    assert r_plan.status_code == 503
    assert r_plan.json()["error"] == "catalog_missing"

    r_unknown_search = client.get("/v1/products/search", params={"q": "milk"},
                                  headers={"X-Customer-Id": "unknown-customer-xyz"})
    assert r_unknown_search.status_code == 403
    assert r_unknown_search.json()["error"] == "unknown_customer"

    unknown_need = dict(need_body, customer_id="unknown-customer-xyz")
    r_unknown_plan = client.post("/v1/plan", json=unknown_need,
                                 headers={"X-Customer-Id": "unknown-customer-xyz", "X-Agent-Id": "buyer-1"})
    assert r_unknown_plan.status_code == 403
    assert r_unknown_plan.json()["error"] == "unknown_customer"

    r_no_auth = client.get("/v1/products/search", params={"q": "milk"})
    assert r_no_auth.status_code == 401
    assert r_no_auth.json()["error"] == "identity_required"
