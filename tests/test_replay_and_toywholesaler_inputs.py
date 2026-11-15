"""Input hardening tests for aimo.backend.replay, toywholesaler, and conformance.verify."""
from __future__ import annotations

import ast
import gzip
import importlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

import aimo.catalog as cat
from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store, replay
from aimo.backend.client import UpstreamError, parse_dt
from aimo.catalog import plan as plan_mod, textsearch, units
from aimo.catalog.units import Qty
from aimo.gateway import fallback_catalog, human_auth, mcp_server as mcp_mod, resilience, service
from aimo.gateway.app import create_app
from aimo.trust import policy, signing
from conformance import verify as V
from conformance.profile import MCP_FORBIDDEN, MCP_TOOLS
from toywholesaler import backend as toy_backend
from toywholesaler.backend import CATALOG, StaticBackendClient, _content_kg, product_json

ROOT = Path(__file__).resolve().parents[1]
TOY_POLICIES = ROOT / "toywholesaler" / "policies"
TOY_SERVER_NAME = "nordic-bakery-supply-trusted-trade"
TOY_TITLE = "Nordic Bakery Supply Trusted Trade Gateway"
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}


def _reset_breakers() -> None:
    for b in list(resilience._breakers.values()):
        b.reset()


def _assert_no_500_or_zero_div(resp, context: Any = "") -> None:
    assert resp.status_code < 500, (context, resp.status_code, resp.text[:400])
    assert "ZeroDivision" not in resp.text, (context, resp.text[:400])
    assert "Traceback (most recent call last)" not in resp.text, (context, resp.text[:400])


def _all_numbers_finite(obj: Any) -> bool:
    if isinstance(obj, bool):
        return True
    if isinstance(obj, (int, float)):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_all_numbers_finite(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_all_numbers_finite(v) for v in obj)
    return True


class McpHttpHelper:
    BASE_HEADERS = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def __init__(self, client: TestClient):
        self.client = client
        self.sid: str | None = None
        self._id = 0

    def _parse(self, r) -> dict[str, Any]:
        _assert_no_500_or_zero_div(r, "mcp-parse")
        if r.headers.get("mcp-session-id"):
            self.sid = r.headers["mcp-session-id"]
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
            assert lines, r.text[:300]
            return json.loads(lines[0])
        return r.json()

    def initialize(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._id += 1
        req_headers = {**self.BASE_HEADERS, **(headers or {})}
        r = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "replay-and-toywholesaler-inputs", "version": "0.1.0"},
                },
            },
            headers=req_headers,
        )
        assert r.status_code == 200, r.text[:300]
        parsed = self._parse(r)
        nh = dict(req_headers)
        if self.sid:
            nh["mcp-session-id"] = self.sid
        r_notif = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=nh)
        _assert_no_500_or_zero_div(r_notif, "notifications/initialized")
        return parsed

    def rpc(self, method: str, params: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._id += 1
        req_headers = {**self.BASE_HEADERS, **(headers or {})}
        if self.sid and method != "initialize":
            req_headers.setdefault("mcp-session-id", self.sid)
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        r = self.client.post("/mcp", json=body, headers=req_headers)
        assert r.status_code == 200, (method, r.status_code, r.text[:300])
        return self._parse(r)

    def call_tool(self, name: str, arguments: Any, headers: dict[str, str] | None = None) -> tuple[bool, str]:
        msg = self.rpc("tools/call", {"name": name, "arguments": arguments}, headers=headers)
        _reset_breakers()
        if "error" in msg:
            txt = json.dumps(msg["error"])
            assert "ZeroDivision" not in txt and "Traceback (most recent call last)" not in txt
            return True, txt
        res = msg.get("result") or {}
        is_err = bool(res.get("isError") or res.get("is_error"))
        txt = " ".join(c.get("text", "") for c in (res.get("content") or []) if isinstance(c, dict))
        if not txt and res.get("structuredContent") is not None:
            txt = json.dumps(res["structuredContent"])
        assert "ZeroDivision" not in txt and "Traceback (most recent call last)" not in txt
        return is_err, txt


@pytest.fixture
def toy_inproc(tmp_path, monkeypatch):
    orig_policy_dir = policy.POLICY_DIR
    orig_client = bc._client
    pjson = tmp_path / "toy_products.json"
    cdb = tmp_path / "toy_catalog.sqlite"
    sdb = tmp_path / "toy_state.sqlite"
    kdir = tmp_path / "toy_keys"
    pjson.write_text(json.dumps(product_json(), ensure_ascii=False), encoding="utf-8")

    for env_name in (
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(config, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(config, "STATE_DB", sdb)
    monkeypatch.setattr(config, "KEY_DIR", kdir)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(store, "DB_PATH", sdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(cat, "_SOURCE", "local")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    monkeypatch.setattr(policy, "POLICY_DIR", TOY_POLICIES)
    monkeypatch.setattr(mcp_mod, "SERVER_NAME", TOY_SERVER_NAME)

    orig_manifest = service.manifest

    def _toy_manifest(base_url: str) -> dict[str, Any]:
        m = orig_manifest(base_url)
        m["name"] = TOY_TITLE
        m["operator"] = CATALOG["vendor"]
        m["mcp"] = {**m["mcp"], "server_name": TOY_SERVER_NAME, "stdio": None}
        return m

    monkeypatch.setattr(service, "manifest", _toy_manifest)

    policy._load_policy_cached.cache_clear()
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    catalog_store.load_catalog(cdb, force=True)
    backend = StaticBackendClient()
    bc.set_client(backend)
    human_auth.reset_for_tests()
    _reset_breakers()

    try:
        with TestClient(create_app(with_mcp=True), raise_server_exceptions=False) as tc:
            yield tc, backend
    finally:
        signing._keys.pop(str(kdir / "ed25519-signing.pem"), None)
        human_auth.reset_for_tests()
        _reset_breakers()
        bc.set_client(orig_client)
        policy.POLICY_DIR = orig_policy_dir
        policy._load_policy_cached.cache_clear()
        fallback_catalog._index = None
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def test_replay_dates_and_dir_helpers_handle_malformed_paths_and_filenames(tmp_path, monkeypatch):
    snap = tmp_path / "dates_snap"
    snap.mkdir()
    for bad_name in (
        "availability_.json",
        "availability_.json.gz",
        "availability_foo.json",
        "availability_foo_bar.json",
        "availability_2026-13-99.json",
        "availability_2026-02-29.json",
        "availability_2026-1-2.json",
        "availability_20261116.json",
        "availability_2026-W01-1.json",
        "availability_2026-11-16.json.bak",
        "availability_2026-11-16.extra.json",
    ):
        (snap / bad_name).write_text("{}", encoding="utf-8")
    (snap / "availability_2026-11-25.json").mkdir()
    (snap / "availability_2026-11-16.json").write_text('{"products":[]}', encoding="utf-8")
    (snap / "availability_2026-11-18.json.gz").write_bytes(gzip.compress(b'{"products":[]}'))

    rc = replay.ReplayClient(snap)
    assert rc.dates() == ["2026-11-16", "2026-11-18"]
    assert replay.ReplayClient(tmp_path / "missing_dir").dates() == []

    for bad_dir in (None, True, False, 123, "", "   ", "bad\x00path"):
        with pytest.raises(ValueError):
            replay.ReplayClient(bad_dir)
        assert replay.meta(bad_dir) == {}
        if bad_dir is not None:
            assert replay.info(bad_dir) == {"available": False}

    meta_as_dir = tmp_path / "meta_as_dir"
    meta_as_dir.mkdir()
    (meta_as_dir / "meta.json").mkdir()
    assert replay.meta(meta_as_dir) == {}

    monkeypatch.setenv("AIMO_REPLAY", "x" * 5000)
    assert replay.replay_dir() is None
    monkeypatch.setenv("AIMO_REPLAY", "~nonexistent_user_999999/snap")
    assert replay.replay_dir() is None
    monkeypatch.setenv("AIMO_REPLAY", "0")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("AIMO_REPLAY_FALLBACK", "x" * 5000)
    assert replay.fallback_dir() == replay.BUNDLED_DIR


def test_replay_serve_and_load_validate_requested_date_endpoint_and_product_ids():
    rc = replay.ReplayClient(replay.BUNDLED_DIR)
    for bad_date in (
        datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 16, 0, 0),
        "2026-11-16",
        "not-a-date",
        None,
        123,
        True,
        False,
    ):
        with pytest.raises(ValueError, match="requested_date must be a date"):
            rc.price(["464306"], bad_date)
        with pytest.raises(ValueError, match="requested_date must be a date"):
            rc.availability(["464306"], bad_date)

    for bad_ids in (None, 123, 3.14, True, False, "464306", b"464306", bytearray(b"464306"), {"464306": 1}):
        with pytest.raises(ValueError):
            rc.price(bad_ids, date(2026, 11, 16))
        with pytest.raises(ValueError):
            rc.availability(bad_ids, date(2026, 11, 16))

    for bad_ep in ("orders", "../price", ""):
        with pytest.raises(ValueError):
            rc._load(bad_ep, "2026-11-16")

    for bad_iso in ("../etc/passwd", "2026-13-01", "20261116", "", None, 20261116):
        with pytest.raises(ValueError):
            rc._load("price", bad_iso)

    gen_out = rc.price((pid for pid in ("463038", "464306")), date(2026, 11, 16))
    assert {p["productId"] for p in gen_out["products"]} == {"463038", "464306"}


def test_replay_load_corrupt_and_missing_snapshots_and_gateway_503(tmp_path, monkeypatch):
    replay._meta.cache_clear()
    replay._fallback.clear()

    for idx, payload in enumerate((
        b"[1, 2, 3]",
        b"123",
        b"null",
        b'"just-a-string"',
        b"{broken-json",
        b"\xff\xfe\xfd",
        b'{"time_now": NaN, "n_ids": 5}',
        json.dumps({"time_now": 99999, "n_ids": "bad"}).encode("utf-8"),
        json.dumps({"time_now": ["not-a-str"], "n_ids": True}).encode("utf-8"),
    )):
        snap = tmp_path / f"meta_case_{idx}"
        snap.mkdir()
        (snap / "meta.json").write_bytes(payload)
        assert isinstance(replay.meta(snap), dict)
        assert replay.snapshot_time_now(snap) is None
        inf = replay.info(snap)
        assert inf["available"] is True
        assert inf["time_now"] is None
        assert inf["n_ids"] is None

    corrupt_dir = tmp_path / "corrupt_snap"
    corrupt_dir.mkdir()
    (corrupt_dir / "meta.json").write_bytes(b"[1, 2, 3]")
    (corrupt_dir / "price_2026-11-16.json.gz").write_bytes(b"not-a-gzip-file")
    (corrupt_dir / "availability_2026-11-16.json.gz").write_bytes(gzip.compress(b'{"products":[]}')[:10])
    (corrupt_dir / "price_2026-11-17.json").write_text("{broken json", encoding="utf-8")
    (corrupt_dir / "availability_2026-11-17.json").write_text("[]", encoding="utf-8")
    (corrupt_dir / "price_2026-11-18.json").write_bytes(b"\xff\xfe\xfd")
    (corrupt_dir / "availability_2026-11-18.json.gz").write_bytes(gzip.compress(b"\xff\xfe\xfd"))
    (corrupt_dir / "price_2026-11-19.json").write_text("null", encoding="utf-8")
    (corrupt_dir / "availability_2026-11-19.json.gz").write_bytes(
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\xff\xff\xff"
    )
    (corrupt_dir / "price_2026-11-21.json").mkdir()
    (corrupt_dir / "availability_2026-11-21.json").write_text(
        '{"products":[{"productId":"NB1001","v":NaN}]}', encoding="utf-8"
    )

    rc = replay.ReplayClient(corrupt_dir)
    for d in (
        date(2026, 11, 16),
        date(2026, 11, 17),
        date(2026, 11, 18),
        date(2026, 11, 19),
        date(2026, 11, 21),
    ):
        with pytest.raises(UpstreamError) as exc_p:
            rc.price(["464306"], d)
        assert exc_p.value.status_code == 503
        assert "is unreadable or corrupt" in str(exc_p.value)
        with pytest.raises(UpstreamError) as exc_a:
            rc.availability(["464306"], d)
        assert exc_a.value.status_code == 503
        assert "is unreadable or corrupt" in str(exc_a.value)

    with pytest.raises(UpstreamError) as exc_missing:
        rc.price(["464306"], date(2026, 12, 25))
    assert exc_missing.value.status_code == 503
    assert "replay snapshot has no" in str(exc_missing.value)

    mixed_dir = tmp_path / "mixed_products_snap"
    mixed_dir.mkdir()
    (mixed_dir / "meta.json").write_text(json.dumps({"time_now": "2026-11-15T10:00:00+02:00", "n_ids": 2}), encoding="utf-8")
    (mixed_dir / "price_2026-11-20.json").write_text(
        json.dumps({
            "products": [
                None,
                123,
                "not-a-dict",
                [],
                {},
                {"productId": ""},
                {"productId": "   "},
                {"productId": None},
                {"productId": True},
                {"productId": False},
                {"productId": ["bad"]},
                {
                    "productId": "NB1001",
                    "catalogPrice": {"value": 2.4, "valueWithTax": 2.72, "currency": "EUR"},
                    "error": False,
                },
                {
                    "productId": 464306,
                    "catalogPrice": {"value": 1.5, "valueWithTax": 1.7, "currency": "EUR"},
                    "error": False,
                },
            ]
        }),
        encoding="utf-8",
    )
    (mixed_dir / "availability_2026-11-20.json").write_text(
        json.dumps({
            "products": [
                "garbage",
                42,
                {"noId": True},
                {"productId": "   "},
                {
                    "productId": "NB1001",
                    "closingTime": "2026-11-19T14:00:00+0200",
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {"salesBlock": False, "availableAmount": 50.0, "availableDate": "2026-11-20", "salesUnit": "ST"}
                    ],
                },
            ]
        }),
        encoding="utf-8",
    )
    mixed_rc = replay.ReplayClient(mixed_dir)
    pr = mixed_rc.price(["NB1001", 464306, None, "", "   ", True, "MISSING"], date(2026, 11, 20))
    av = mixed_rc.availability(["NB1001", "MISSING"], date(2026, 11, 20))
    assert [str(p["productId"]) for p in pr["products"]] == ["NB1001", "464306"]
    assert [p["productId"] for p in av["products"]] == ["NB1001"]

    (mixed_dir / "price_2026-11-22.json").write_text(json.dumps({"products": "not-a-list"}), encoding="utf-8")
    (mixed_dir / "availability_2026-11-22.json").write_text(json.dumps({"products": 999}), encoding="utf-8")
    assert mixed_rc.price(["NB1001"], date(2026, 11, 22))["products"] == []
    assert mixed_rc.availability(["NB1001"], date(2026, 11, 22))["products"] == []

    orig_client = bc._client
    pjson = tmp_path / "gw_products.json"
    cdb = tmp_path / "gw_catalog.sqlite"
    pjson.write_text(json.dumps(product_json(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AIMO_REPLAY", str(corrupt_dir))
    monkeypatch.setenv("AIMO_REPLAY_FALLBACK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "gw_state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "gw_keys")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "gw_state.sqlite")
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(cat, "_SOURCE", "local")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    catalog_store.load_catalog(cdb, force=True)
    bc.set_client(rc)
    human_auth.reset_for_tests()
    _reset_breakers()

    try:
        with TestClient(create_app(with_mcp=False), raise_server_exceptions=False) as tc:
            hz = tc.get("/healthz")
            assert hz.status_code == 200 and hz.json()["ok"] is True

            for bad_day, expected_err in (
                ("2026-11-16", "upstream_unavailable"),
                ("2026-11-17", "upstream_unavailable"),
                ("2026-11-18", "upstream_unavailable"),
                ("2026-11-21", "upstream_unavailable"),
                ("2026-12-25", "replay_no_data"),
            ):
                r_off = tc.post(
                    "/v1/offers",
                    headers=H_AURORA,
                    json={"product_ids": ["NB1001"], "requested_date": bad_day},
                )
                assert r_off.status_code == 503, (bad_day, r_off.status_code, r_off.text)
                assert r_off.json()["error"] == expected_err

                r_plan = tc.post(
                    "/v1/plan",
                    headers=H_AURORA,
                    json={
                        "customer_id": "hotel-aurora",
                        "product_query": "rye bread",
                        "quantity": 2,
                        "unit": "KG",
                        "delivery_by": bad_day,
                    },
                )
                assert r_plan.status_code == 503, (bad_day, r_plan.status_code, r_plan.text)
                assert r_plan.json()["error"] == expected_err

                r_quote = tc.post(
                    "/v1/quotes",
                    headers=H_AURORA,
                    json={"delivery_date": bad_day, "lines": [{"product_id": "NB1001", "qty_sales_units": 1}]},
                )
                assert r_quote.status_code == 503, (bad_day, r_quote.status_code, r_quote.text)
                assert r_quote.json()["error"] == expected_err

            bc.set_client(mixed_rc)
            monkeypatch.setenv("AIMO_NOW", "2026-11-15T10:00:00+02:00")
            r_ok = tc.post(
                "/v1/offers",
                headers=H_AURORA,
                json={"product_ids": ["NB1001"], "requested_date": "2026-11-20"},
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()[0]["product_id"] == "NB1001"
            assert r_ok.json()[0]["available"] is True
    finally:
        signing._keys.pop(str(tmp_path / "gw_keys" / "ed25519-signing.pem"), None)
        bc.set_client(orig_client)
        replay._meta.cache_clear()
        replay._fallback.clear()
        fallback_catalog._index = None
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        human_auth.reset_for_tests()
        _reset_breakers()


def test_toywholesaler_content_kg_and_static_backend_prices_and_stock(monkeypatch):
    assert _content_kg("Rye bread 500 g sliced") == pytest.approx(0.5)
    assert _content_kg("Karelian pasties 10 x 70 g") == pytest.approx(0.7)
    assert _content_kg("Milk 1 l") is None
    assert _content_kg("") is None
    assert _content_kg("   ") is None
    assert _content_kg(None) is None
    assert _content_kg("Zero bread 0 g") is None
    assert _content_kg("Zero pack 0 x 500 g") is None

    hostile_parsed = [
        (Qty(0.0, "KG"), None),
        (Qty(-0.0, "KG"), 1),
        (Qty(-2.5, "KG"), 1),
        (Qty(math.nan, "KG"), 1),
        (Qty(math.inf, "KG"), 1),
        (Qty(-math.inf, "KG"), 1),
        (Qty(True, "KG"), 1),
        (Qty(10**400, "KG"), 1),
        (Qty(1.0, "KG"), 0),
        (Qty(1.0, "KG"), -3),
        (Qty(1.0, "KG"), math.nan),
        (Qty(1.0, "KG"), math.inf),
        (Qty(1.0, "KG"), -math.inf),
        (Qty(1.0, "KG"), True),
        (Qty(1.0, "KG"), "bad"),
        (Qty(1.0, "KG"), 10**400),
        (Qty(1e200, "KG"), 1e200),
    ]
    for fake_q, fake_mult in hostile_parsed:
        monkeypatch.setattr(units, "parse_name_quantity", lambda _name, q=fake_q, m=fake_mult: (q, m))
        assert toy_backend._content_kg("synthetic item") is None

    monkeypatch.undo()
    client = StaticBackendClient()
    client.products["NB_ZERO_KG"] = {"id": "NB_ZERO_KG", "en": "Bread 0 g", "price": 3.5, "stock": 10}
    client.products["NB_ZERO_PRICE"] = {"id": "NB_ZERO_PRICE", "en": "Bread 500 g", "price": 0.0, "stock": 10}
    client.products["NB_NEG_PRICE"] = {"id": "NB_NEG_PRICE", "en": "Bread 500 g", "price": -1.5, "stock": -20}
    client.products["NB_TINY_KG"] = {"id": "NB_TINY_KG", "en": "Bread 1 g", "price": 1e308, "stock": 10}
    client.products["NB_GROSS_OVF"] = {"id": "NB_GROSS_OVF", "en": "Bread 500 g", "price": 1.7e308, "stock": 10}
    client.products["NB_HUGE_INT"] = {"id": "NB_HUGE_INT", "en": "Bread 500 g", "price": 10**400, "stock": 10**400}
    client.products["NB_NAN_PRICE"] = {"id": "NB_NAN_PRICE", "en": "Bread 500 g", "price": math.nan, "stock": True}
    client.products["NB_INF_PRICE"] = {"id": "NB_INF_PRICE", "en": "Bread 500 g", "price": math.inf, "stock": math.nan}
    client.products["NB_BOOL_PRICE"] = {"id": "NB_BOOL_PRICE", "en": "Bread 500 g", "price": True, "stock": "bad"}

    ids = [
        "NB1001",
        "NB_ZERO_KG",
        "NB_ZERO_PRICE",
        "NB_NEG_PRICE",
        "NB_TINY_KG",
        "NB_GROSS_OVF",
        "NB_HUGE_INT",
        "NB_NAN_PRICE",
        "NB_INF_PRICE",
        "NB_BOOL_PRICE",
    ]
    pr = client.price(ids, date(2026, 12, 2))
    assert _all_numbers_finite(pr)
    by_id = {item["productId"]: item for item in pr["products"]}
    assert by_id["NB1001"]["comparisonPrice"]["value"] == pytest.approx(4.8)
    assert "comparisonPrice" not in by_id["NB_ZERO_KG"]
    assert by_id["NB_TINY_KG"]["error"] is False
    assert "comparisonPrice" not in by_id["NB_TINY_KG"]
    for bad_pid in ("NB_ZERO_PRICE", "NB_NEG_PRICE", "NB_GROSS_OVF", "NB_HUGE_INT", "NB_NAN_PRICE", "NB_INF_PRICE", "NB_BOOL_PRICE"):
        assert by_id[bad_pid]["error"] is True

    av = client.availability(["NB_NEG_PRICE", "NB_HUGE_INT", "NB_NAN_PRICE", "NB_INF_PRICE", "NB_BOOL_PRICE"], date(2026, 12, 2))
    assert _all_numbers_finite(av)
    for item in av["products"]:
        assert item["scheduleLines"][0]["availableAmount"] == 0.0


def test_static_backend_validates_product_ids_and_requested_date():
    client = StaticBackendClient()

    for bad_date in (
        datetime(2026, 12, 2, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 12, 2, 0, 0),
        "2026-12-02",
        "not-a-date",
        None,
        123,
        True,
        False,
    ):
        with pytest.raises(ValueError):
            client.price(["NB1001", "NB1009"], bad_date)
        with pytest.raises(ValueError):
            client.availability(["NB1001", "NB1009"], bad_date)

    for bad_ids in (
        None,
        123,
        3.14,
        True,
        False,
        "",
        "NB1001",
        b"NB1001",
        bytearray(b"NB1001"),
        {"NB1001": 1},
        [],
        (),
        set(),
        [""],
        ["   "],
        [True],
        [False],
        [None],
        [123],
        ["NB 1001"],
        ["NB1001\x00"],
        ["x" * 65],
    ):
        with pytest.raises(ValueError):
            client.price(bad_ids, date(2026, 12, 2))
        with pytest.raises(ValueError):
            client.availability(bad_ids, date(2026, 12, 2))

    with pytest.raises(ValueError):
        client.price((x for x in ()), date(2026, 12, 2))
    with pytest.raises(ValueError):
        client.availability((x for x in ()), date(2026, 12, 2))

    for valid_boundary in (date.min, date(1, 1, 2), date(1, 1, 3), date(1, 1, 4), date(2026, 12, 2), date.max):
        pr = client.price(("  NB1001  ", "NB1009", "NB9999"), valid_boundary, customer="hotel-aurora")
        av = client.availability(["NB1001", "NB1009", "NB9999"], valid_boundary, customer="hotel-aurora")
        assert _all_numbers_finite(pr)
        assert _all_numbers_finite(av)
        assert parse_dt(pr["pricingDate"]) is not None
        assert parse_dt(av["requestedDate"]) is not None
        for item in av["products"]:
            assert parse_dt(item["closingTime"]) is not None


def test_toywholesaler_gateway_http_and_mcp_reject_bad_inputs_without_500(toy_inproc, monkeypatch):
    tc, backend = toy_inproc
    base_need = {
        "customer_id": "hotel-aurora",
        "product_query": "rye bread",
        "quantity": 5,
        "unit": "KG",
        "delivery_by": "2026-12-02",
    }

    called: list[str] = []
    orig_find = plan_mod.find_candidates

    def _forbid_find(*_a, **_kw):
        called.append("find_candidates")
        raise AssertionError("find_candidates must not run for invalid quantity")

    monkeypatch.setattr(plan_mod, "find_candidates", _forbid_find)
    before_calls = backend.stats["upstream_calls"]
    for bad_qty in (0, 0.0, -0.0, -1, -0.0001, -999, 1_000_000_000, 1e308, "NaN", "Infinity", "-Infinity", True, False, None, [], {}):
        r = tc.post("/v1/plan", headers=H_AURORA, json={**base_need, "quantity": bad_qty})
        _assert_no_500_or_zero_div(r, ("plan-qty", bad_qty))
        assert 400 <= r.status_code < 500, (bad_qty, r.status_code, r.text)
    for raw_num in ("0", "-0.0", "-3", "NaN", "Infinity", "-Infinity", "1e9", "1e309"):
        raw_body = (
            '{"customer_id":"hotel-aurora","product_query":"rye bread","quantity":'
            + raw_num
            + ',"unit":"KG","delivery_by":"2026-12-02"}'
        ).encode("utf-8")
        r = tc.post("/v1/plan", headers={**H_AURORA, "Content-Type": "application/json"}, content=raw_body)
        _assert_no_500_or_zero_div(r, ("plan-raw", raw_num))
        assert 400 <= r.status_code < 500, (raw_num, r.status_code, r.text)
    assert called == []
    assert backend.stats["upstream_calls"] == before_calls
    monkeypatch.setattr(plan_mod, "find_candidates", orig_find)

    for bad_q_qty in (0, 0.0, -0.0, -5, 1_000_000_000, 1e308, 1.5, "NaN", "Infinity", "-Infinity", False, None, "two"):
        rq = tc.post(
            "/v1/quotes",
            headers=H_AURORA,
            json={"delivery_date": "2026-12-02", "lines": [{"product_id": "NB1001", "qty_sales_units": bad_q_qty}]},
        )
        _assert_no_500_or_zero_div(rq, ("quote-qty", bad_q_qty))
        assert 400 <= rq.status_code < 500, (bad_q_qty, rq.status_code, rq.text)

    for raw_q_num in ("0", "-1", "NaN", "Infinity", "-Infinity", "1e309"):
        raw_q_body = (
            '{"delivery_date":"2026-12-02","lines":[{"product_id":"NB1001","qty_sales_units":' + raw_q_num + "}]}"
        ).encode("utf-8")
        rq_raw = tc.post("/v1/quotes", headers={**H_AURORA, "Content-Type": "application/json"}, content=raw_q_body)
        _assert_no_500_or_zero_div(rq_raw, ("quote-raw", raw_q_num))
        assert 400 <= rq_raw.status_code < 500, (raw_q_num, rq_raw.status_code, rq_raw.text)

    for bad_day in ("2026-13-40", "2026-02-29", "not-a-date", "", None, 123, True):
        ro = tc.post("/v1/offers", headers=H_AURORA, json={"product_ids": ["NB1001"], "requested_date": bad_day})
        _assert_no_500_or_zero_div(ro, ("offers-date", bad_day))
        assert 400 <= ro.status_code < 500

        rp = tc.post("/v1/plan", headers=H_AURORA, json={**base_need, "delivery_by": bad_day})
        _assert_no_500_or_zero_div(rp, ("plan-date", bad_day))
        assert 400 <= rp.status_code < 500

        rq = tc.post(
            "/v1/quotes",
            headers=H_AURORA,
            json={"delivery_date": bad_day, "lines": [{"product_id": "NB1001", "qty_sales_units": 1}]},
        )
        _assert_no_500_or_zero_div(rq, ("quote-date", bad_day))
        assert 400 <= rq.status_code < 500

    ro_min = tc.post("/v1/offers", headers=H_AURORA, json={"product_ids": ["NB1001", "NB1009"], "requested_date": "0001-01-01"})
    _assert_no_500_or_zero_div(ro_min, "offers-min-date")
    assert ro_min.status_code == 200 and all(item["available"] is False for item in ro_min.json())
    rq_min = tc.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": "0001-01-01", "lines": [{"product_id": "NB1001", "qty_sales_units": 1}]},
    )
    _assert_no_500_or_zero_div(rq_min, "quote-min-date")
    assert rq_min.status_code == 409

    for bad_pids in ([], [""], ["   "], ["NB 1001"], [123], [True], [None], "NB1001", None):
        ro_ids = tc.post("/v1/offers", headers=H_AURORA, json={"product_ids": bad_pids, "requested_date": "2026-12-02"})
        _assert_no_500_or_zero_div(ro_ids, ("offers-ids", bad_pids))
        assert 400 <= ro_ids.status_code < 500

    for bad_pid in ("", "   ", "NB 1001", 123, True, None, ["NB1001"]):
        rq_pid = tc.post(
            "/v1/quotes",
            headers=H_AURORA,
            json={"delivery_date": "2026-12-02", "lines": [{"product_id": bad_pid, "qty_sales_units": 1}]},
        )
        _assert_no_500_or_zero_div(rq_pid, ("quote-pid", bad_pid))
        assert 400 <= rq_pid.status_code < 500

    for bad_cust in ("../etc/passwd", "x" * 5000, "unknown-bakery-customer", "school-tampere"):
        hdr = {"X-Customer-Id": bad_cust, "X-Agent-Id": "buyer-1"}
        rs = tc.get("/v1/products/search", params={"q": "rye"}, headers=hdr)
        _assert_no_500_or_zero_div(rs, ("search-cust", bad_cust[:32]))
        assert rs.status_code in (401, 403, 422)

    ok_plan = tc.post("/v1/plan", headers=H_AURORA, json=base_need)
    assert ok_plan.status_code == 200, ok_plan.text
    assert ok_plan.json()["lines"] and ok_plan.json()["covered_amount"] >= 5

    ok_quote = tc.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": "2026-12-02", "lines": [{"product_id": "NB1001", "qty_sales_units": 4}]},
    )
    assert ok_quote.status_code == 200, ok_quote.text
    q_json = ok_quote.json()
    jwks = tc.get("/.well-known/jwks.json").json()
    assert V.verify_quote(q_json, jwks)["ok"] is True

    ok_order = tc.post(
        "/v1/orders",
        headers=H_AURORA,
        json={"quote_id": q_json["quote_id"], "quote_hash": q_json["quote_hash"]},
    )
    assert ok_order.status_code == 200 and ok_order.json()["status"] == "placed"
    audit_events = tc.get("/v1/audit").json()
    assert V.verify_events(audit_events, jwks, contiguous=True)["ok"] is True

    gf_block = tc.post(
        "/v1/plan",
        headers=H_HOSPITAL,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "bread",
            "quantity": 4,
            "unit": "KG",
            "delivery_by": "2026-12-02",
        },
    )
    assert gf_block.status_code == 200, gf_block.text
    for ln in gf_block.json()["lines"]:
        assert ln["product_id"] in ("NB1011", "NB1012", "NB1013")

    sess = McpHttpHelper(tc)
    init_res = sess.initialize(H_AURORA)
    assert init_res["result"]["serverInfo"]["name"] == TOY_SERVER_NAME
    tools_msg = sess.rpc("tools/list", {}, headers=H_AURORA)
    tools = {t["name"]: t for t in tools_msg["result"]["tools"]}
    assert len(tools) == 13
    assert set(MCP_TOOLS) <= set(tools)
    assert not (set(MCP_FORBIDDEN) & set(tools))

    for bad_qty in (0, 0.0, -0.0, -2.5, 1_000_000_000, "NaN", "Infinity", "-Infinity", True, False, None):
        is_err, txt = sess.call_tool("plan_need", {"need": {**base_need, "quantity": bad_qty}}, headers=H_AURORA)
        assert is_err, (bad_qty, txt)

    for bad_q in (0, 0.0, -0.0, -1, 1.5, 1_000_000_000, False, "NaN", "Infinity", "-Infinity", None):
        is_err_q, txt_q = sess.call_tool(
            "create_quote",
            {"delivery_date": "2026-12-02", "lines": [{"product_id": "NB1001", "qty_sales_units": bad_q}]},
            headers=H_AURORA,
        )
        assert is_err_q, (bad_q, txt_q)

    for bad_pids_mcp in ([], [""], [True], [None]):
        is_err_o, txt_o = sess.call_tool(
            "get_offers",
            {"product_ids": bad_pids_mcp, "requested_date": "2026-12-02"},
            headers=H_AURORA,
        )
        assert is_err_o, (bad_pids_mcp, txt_o)


def test_toywholesaler_app_prepare_data_and_exception_handlers(tmp_path, monkeypatch):
    orig_policy_dir = policy.POLICY_DIR
    orig_client = bc._client
    orig_manifest = service.manifest
    orig_server_name = mcp_mod.SERVER_NAME
    orig_instructions = mcp_mod.INSTRUCTIONS

    pjson = tmp_path / "corrupt_toy_products.json"
    cdb = tmp_path / "corrupt_toy_catalog.sqlite"
    sdb = tmp_path / "corrupt_toy_state.sqlite"
    kdir = tmp_path / "corrupt_toy_keys"
    pjson.write_bytes(b"\xff\xfe\xfd")

    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(config, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(config, "STATE_DB", sdb)
    monkeypatch.setattr(config, "KEY_DIR", kdir)
    monkeypatch.setattr(store, "DB_PATH", sdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "no_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "no_co2.csv")
    monkeypatch.setattr(cat, "_SOURCE", "local")
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    try:
        toy_app_mod = importlib.import_module("toywholesaler.app")
        toy_app_mod._prepare_data()
        assert json.loads(pjson.read_text(encoding="utf-8"))
        val_resp = asyncio.run_or_sync_handler(toy_app_mod._toy_value_error, ValueError("bad toy value"))
        assert val_resp.status_code == 422
        assert json.loads(val_resp.body) == {"error": "invalid_request", "message": "bad toy value"}
        ovf_resp = asyncio.run_or_sync_handler(toy_app_mod._toy_overflow_error, OverflowError("too large"))
        assert ovf_resp.status_code == 422
        assert json.loads(ovf_resp.body) == {"error": "invalid_request", "message": "too large"}
    finally:
        signing._keys.pop(str(kdir / "ed25519-signing.pem"), None)
        bc.set_client(orig_client)
        policy.POLICY_DIR = orig_policy_dir
        policy._load_policy_cached.cache_clear()
        service.manifest = orig_manifest
        mcp_mod.SERVER_NAME = orig_server_name
        mcp_mod.INSTRUCTIONS = orig_instructions
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


class asyncio:
    @staticmethod
    def run_or_sync_handler(handler, exc):
        import asyncio as _aio

        return _aio.run(handler(None, exc))


def _make_ed25519_material() -> tuple[Ed25519PrivateKey, dict[str, Any], str]:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk: dict[str, Any] = {"kty": "OKP", "crv": "Ed25519", "x": V.b64e(pub_raw)}
    kid = V.thumbprint(jwk)
    jwk["kid"] = kid
    return priv, {"keys": [jwk]}, kid


def _sign_jws_with(priv: Ed25519PrivateKey, kid: str, payload: dict[str, Any], alg: str = "EdDSA") -> str:
    hdr = {"alg": alg, "kid": kid, "typ": "JOSE"}
    inp = f"{V.b64e(V.canonical_json(hdr).encode())}.{V.b64e(V.canonical_json(payload).encode())}"
    sig = priv.sign(inp.encode("ascii"))
    return f"{inp}.{V.b64e(sig)}"


def test_conformance_verify_canonical_json_thumbprint_and_key_for():
    src = (ROOT / "conformance" / "verify.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m == "aimo" or m.startswith("aimo.")], imported

    for bad_float in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="non-finite float in canonical JSON"):
            V.canonical_json(bad_float)
        with pytest.raises(ValueError, match="non-finite float in canonical JSON"):
            V.canonical_json({"nested": [1, ({"v": bad_float},)]})

    assert V.canonical_json({"b": -0.0, "a": (1.0, 2.5), "big": float(2**60)}) == f'{{"a":[1,2.5],"b":0,"big":{float(2**60)!r}}}'

    _, valid_jwks, valid_kid = _make_ed25519_material()
    valid_jwk = valid_jwks["keys"][0]
    assert V.thumbprint(valid_jwk) == signing.thumbprint(valid_jwk)
    assert V.key_for(valid_jwks, valid_kid) is not None

    bad_jwks_items: list[Any] = [
        None,
        [],
        "bad",
        123,
        {},
        {"kty": "OKP", "crv": "Ed25519"},
        {"kty": "OKP", "crv": "Ed25519", "x": ""},
        {"kty": "OKP", "crv": "Ed25519", "x": 123},
        {"kty": "RSA", "crv": "Ed25519", "x": valid_jwk["x"]},
        {"kty": "OKP", "crv": "P-256", "x": valid_jwk["x"]},
        {"kty": "OKP", "crv": "Ed25519", "x": "!!!not-base64!!!"},
        {"kty": "OKP", "crv": "Ed25519", "x": "a"},
        {"kty": "OKP", "crv": "Ed25519", "x": "ab=cd"},
        {"kty": "OKP", "crv": "Ed25519", "x": valid_jwk["x"] + "\n"},
        {"kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x01" * 16)},
        {"kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x01" * 31)},
        {"kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x01" * 33)},
        {"kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x01" * 64)},
    ]
    for bad_jwk in bad_jwks_items:
        with pytest.raises(ValueError):
            V.thumbprint(bad_jwk)

    for bad_jwks_doc in (
        None,
        [],
        "bad",
        123,
        {},
        {"keys": None},
        {"keys": "bad"},
        {"keys": [None, 123, "bad", {}]},
        {"keys": [{**(bad_jwk if isinstance(bad_jwk, dict) else {}), "kid": valid_kid} for bad_jwk in bad_jwks_items]},
    ):
        assert V.key_for(bad_jwks_doc, valid_kid) is None

    for bad_kid in (None, "", 123, "missing-kid"):
        assert V.key_for(valid_jwks, bad_kid) is None

    fallback_jwks = {
        "keys": [
            {"kid": valid_kid, "kty": "OKP", "crv": "Ed25519", "x": "!!!corrupt!!!"},
            {"kid": valid_kid, "kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x02" * 16)},
            valid_jwk,
        ]
    }
    assert V.key_for(fallback_jwks, valid_kid) is not None


def test_conformance_verify_quote_and_events_hostile_inputs():
    priv, jwks, kid = _make_ed25519_material()

    for bad_obj in (None, [], "bad", 123, {"v": math.nan}, {"v": math.inf}):
        with pytest.raises(ValueError):
            V.quote_hash(bad_obj)
        with pytest.raises(ValueError):
            V.event_hash(bad_obj)
    for bad_ev in ({}, {"prev_hash": None}, {"prev_hash": ""}, {"prev_hash": 123}):
        with pytest.raises(ValueError):
            V.event_hash(bad_ev)

    valid_quote: dict[str, Any] = {
        "quote_id": "Q-test-001",
        "customer_id": "hotel-aurora",
        "total_net": 12.0,
        "lines": [{"product_id": "NB1001", "qty": 5}],
        "kid": kid,
    }
    valid_quote["quote_hash"] = V.quote_hash(valid_quote)
    valid_quote["signature"] = _sign_jws_with(
        priv,
        kid,
        {"quote_id": valid_quote["quote_id"], "quote_hash": valid_quote["quote_hash"]},
    )
    ok_q = V.verify_quote(valid_quote, jwks)
    assert ok_q["ok"] is True and ok_q["hash_matches"] is True and ok_q["signature_valid"] is True

    alg_none_tok = f"{V.b64e(b'{\"alg\":\"none\"}')}.{V.b64e(V.canonical_json({'quote_id': 'Q-test-001', 'quote_hash': valid_quote['quote_hash']}).encode())}."
    nan_pay_tok = f"{V.b64e(V.canonical_json({'alg': 'EdDSA', 'kid': kid}).encode())}.{V.b64e(b'{\"quote_id\":\"Q-test-001\",\"x\":NaN}')}.{V.b64e(b'0' * 64)}"
    list_hdr_tok = f"{V.b64e(b'[]')}.{V.b64e(b'{}')}.e30"
    list_pay_tok = f"{V.b64e(b'{\"alg\":\"EdDSA\",\"kid\":\"k\"}')}.{V.b64e(b'[]')}.e30"
    short_sig_tok = valid_quote["signature"].rsplit(".", 1)[0] + "." + V.b64e(b"\x00" * 16)
    no_qid_quote = {"total_net": 5.0, "kid": kid}
    no_qid_quote["quote_hash"] = V.quote_hash(no_qid_quote)
    no_qid_quote["signature"] = _sign_jws_with(priv, kid, {"quote_hash": no_qid_quote["quote_hash"]})

    hostile_quotes: list[tuple[Any, Any]] = [
        (None, jwks),
        ([], jwks),
        ("not-a-quote", jwks),
        (123, jwks),
        ({}, jwks),
        ({"quote_id": "Q-1"}, jwks),
        ({**valid_quote, "quote_hash": None}, jwks),
        ({**valid_quote, "quote_hash": ""}, jwks),
        ({**valid_quote, "quote_hash": 12345}, jwks),
        ({**valid_quote, "signature": None}, jwks),
        ({**valid_quote, "signature": ""}, jwks),
        ({**valid_quote, "signature": 12345}, jwks),
        ({**valid_quote, "signature": ["not", "str"]}, jwks),
        ({**valid_quote, "signature": alg_none_tok}, jwks),
        ({**valid_quote, "signature": nan_pay_tok}, jwks),
        ({**valid_quote, "signature": list_hdr_tok}, jwks),
        ({**valid_quote, "signature": list_pay_tok}, jwks),
        ({**valid_quote, "signature": short_sig_tok}, jwks),
        ({**valid_quote, "kid": 999}, jwks),
        ({**valid_quote, "kid": "wrong-kid"}, jwks),
        ({**valid_quote, "total_net": math.nan}, jwks),
        ({**valid_quote, "total_net": math.inf}, jwks),
        ({**valid_quote, "total_net": -math.inf}, jwks),
        (no_qid_quote, jwks),
        (valid_quote, None),
        (valid_quote, []),
        (valid_quote, "bad-jwks"),
        (valid_quote, {"keys": None}),
        (valid_quote, {"keys": "not-a-list"}),
        (valid_quote, {"keys": [None, 123, "bad"]}),
        (valid_quote, {"keys": [{"kid": kid, "kty": "OKP", "crv": "Ed25519", "x": "!!!bad!!!"}]}),
        (valid_quote, {"keys": [{"kid": kid, "kty": "OKP", "crv": "Ed25519", "x": V.b64e(b"\x01" * 16)}]}),
        (valid_quote, {"keys": [{"kid": kid, "kty": "OKP", "crv": "Ed25519", "x": None}]}),
    ]
    for q_in, jwks_in in hostile_quotes:
        res = V.verify_quote(q_in, jwks_in)
        assert res["ok"] is False, (q_in, jwks_in, res)
        assert res["reasons"], (q_in, jwks_in, res)

    chain: list[dict[str, Any]] = []
    prev_hash = V.GENESIS
    for seq in (1, 2, 3):
        ev: dict[str, Any] = {
            "seq": seq,
            "ts": f"2026-11-15T10:00:0{seq}+00:00",
            "actor": "agent:hotel-aurora/buyer-1",
            "action": "quote.created",
            "subject_id": "Q-test-001",
            "summary": f"event {seq}",
            "data": {"n": seq},
            "prev_hash": prev_hash,
            "kid": kid,
        }
        ev["hash"] = V.event_hash(ev)
        ev["signature"] = _sign_jws_with(priv, kid, {"typ": "audit-event", "seq": seq, "hash": ev["hash"]})
        chain.append(ev)
        prev_hash = ev["hash"]

    assert V.verify_events(chain, jwks, contiguous=True) == {"ok": True, "checked": 3, "problems": []}
    assert V.verify_events([chain[0], chain[2]], jwks, contiguous=False) == {"ok": True, "checked": 2, "problems": []}
    assert V.verify_events([], jwks, contiguous=True) == {"ok": True, "checked": 0, "problems": []}

    hostile_event_chains: list[tuple[Any, Any, bool]] = [
        (None, jwks, False),
        ({}, jwks, False),
        ("not-a-list", jwks, True),
        (chain, None, True),
        (chain, [], True),
        (chain, {"keys": "bad"}, True),
        ([None, 123, "bad"], jwks, True),
        ([{}], jwks, True),
        ([{**chain[0], "seq": True}], jwks, False),
        ([{**chain[0], "seq": "1"}, chain[1]], jwks, False),
        ([{k: v for k, v in chain[0].items() if k != "prev_hash"}], jwks, True),
        ([{**chain[0], "prev_hash": ""}], jwks, False),
        ([{**chain[0], "prev_hash": 123}], jwks, True),
        ([{k: v for k, v in chain[0].items() if k != "hash"}, chain[1]], jwks, True),
        ([{**chain[0], "hash": 123}], jwks, False),
        ([{k: v for k, v in chain[0].items() if k != "signature"}], jwks, False),
        ([{**chain[0], "signature": 999}], jwks, False),
        ([{**chain[0], "signature": alg_none_tok}], jwks, False),
        ([{**chain[0], "data": {"bad": math.nan}}], jwks, False),
        ([{**chain[0], "data": {"bad": math.inf}}], jwks, False),
        ([{**chain[0], "kid": "other-kid"}], jwks, False),
        ([chain[1], chain[2]], jwks, True),
        ([chain[0], chain[2]], jwks, True),
        ([chain[0], chain[0]], jwks, True),
    ]
    for ev_in, jwks_in, contig in hostile_event_chains:
        ev_res = V.verify_events(ev_in, jwks_in, contiguous=contig)
        assert ev_res["ok"] is False, (ev_in, jwks_in, contig, ev_res)
        assert ev_res["problems"], (ev_in, jwks_in, contig, ev_res)
