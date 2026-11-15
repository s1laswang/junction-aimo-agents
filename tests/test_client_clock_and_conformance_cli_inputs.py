from __future__ import annotations

import asyncio
import io
import json
import math
import time
import tokenize
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store, clock, replay
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Offer
from conformance import __main__ as conf_cli
from conformance import verify as conf_verify

FIX = Path(__file__).resolve().parent / "fixtures"
DELIVERY = date(2026, 11, 16)


class _FixtureBackend:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        self.stats = {"upstream_calls": 0}

    def _select(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
        ts = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": ts, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        self.stats["upstream_calls"] += 1
        return {**meta, "products": prods, "_oldest_fetch": ts}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        return self._select(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        return self._select(self.avail_raw, ids)


def _synth_catalog_items() -> list[dict[str, Any]]:
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
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": "Lactose free milk drink for professional kitchens."}],
            },
        },
        {
            "id": "469363",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Arla",
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Arla",
                "names": [{"language": "en", "value": "Arla semi skimmed milk drink 1 L UHT lactose free"}],
                "unitConversions": base_1l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk"}],
                "marketingTexts": [{"language": "en", "value": "Lactose free UHT milk drink."}],
            },
        },
    ]


@pytest.fixture(scope="module")
def synth_catalog_root(tmp_path_factory: pytest.TempPathFactory) -> Path | None:
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("client-clock-conf-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_catalog_items()), encoding="utf-8")
    return d


@pytest.mark.parametrize(
    "bad_dt",
    [
        None,
        "",
        123,
        True,
        False,
        [],
        {},
        "2026-11-14T14:00:00Z" + ("x" * 120),
        "not-an-iso-datetime",
        "2026-13-40T99:00:00+0200",
        "2026-11-14T14:00:00+2599",
    ],
)
def test_client_parse_dt_rejects_bad_inputs(bad_dt: Any) -> None:
    assert bc.parse_dt(bad_dt) is None


def test_client_parse_dt_supports_iso_and_compact_offsets() -> None:
    dt_colon = bc.parse_dt("2026-11-14T14:00:00+02:00")
    dt_compact = bc.parse_dt("2026-11-14T14:00:00+0200")
    dt_neg = bc.parse_dt("2026-11-14T08:30:00-0530")
    assert dt_colon == datetime(2026, 11, 14, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert dt_compact == dt_colon
    assert dt_neg == datetime(2026, 11, 14, 8, 30, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))


@pytest.mark.parametrize("bad_date", [None, "", 0, True, [], "bad-date", "2026-02-30", "2026-13-01", "20269999"])
def test_client_parse_date_rejects_bad_inputs(bad_date: Any) -> None:
    assert bc.parse_date(bad_date) is None


def test_client_parse_date_slug_and_chunked_helpers() -> None:
    assert bc.parse_date("2026-11-16") == DELIVERY
    assert bc.parse_date("20261116") == DELIVERY
    assert bc.parse_date("2026-11-16T15:00:00+0200") == DELIVERY
    assert bc._slug("") == "_"
    assert bc._slug("hotel/aurora:prod?x=1") == "hotel_aurora_prod_x_1"
    assert len(bc._slug("a" * 200)) == 64
    assert list(bc.chunked(["a", "b", "c", "d", "e"], 2)) == [["a", "b"], ["c", "d"], ["e"]]
    assert list(bc.chunked([], 5)) == []


@pytest.mark.parametrize("non_str_spec", [None, 0, 123, True, False, ["stockout:*"], {"spec": "cutoff:*"}])
def test_client_parse_faults_handles_non_string_specs(non_str_spec: Any) -> None:
    parsed = bc.parse_faults(non_str_spec)
    assert parsed.empty() is True
    assert parsed.spec == ""


def test_client_parse_faults_all_directives_control_stripping_and_context_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        "STOCKOUT:\x00464306\r\n;"
        "partial:469363:12.5;partial:zero_id:-0.0;"
        "price_shock:464306:1.25;"
        "price_error:p_err;cutoff:p_cut;sales_block:p_blk;inject_text:*;"
        "error_rate:0.4;rate_limit:1.8;latency_ms:320;unknown_kind:abc"
    )
    f = bc.parse_faults(raw)
    assert f.empty() is False
    assert "464306" in f.stockout
    assert f.partial == {"469363": 12.5, "zero_id": 0.0}
    assert f.price_shock == {"464306": 1.25}
    assert f.price_error == {"p_err"}
    assert f.cutoff == {"p_cut"}
    assert f.sales_block == {"p_blk"}
    assert f.inject_text == {"*"}
    assert f.error_rate == 0.4
    assert f.rate_limit == 1.0
    assert f.latency_ms == 320
    assert bc.Faults._val({"*": 9.0, "464306": 2.0}, "464306") == 2.0
    assert bc.Faults._val({"*": 9.0, "464306": 2.0}, "999999") == 9.0

    monkeypatch.setenv("AIMO_FAULTS", "stockout:*")
    assert bc.current_faults().stockout == {"*"}
    tok_clear = bc.set_request_faults("")
    try:
        assert bc.current_faults().empty() is True
    finally:
        bc.reset_request_faults(tok_clear)
    assert bc.current_faults().stockout == {"*"}


def test_client_numeric_helpers_guard_booleans_overflow_and_underflow() -> None:
    assert bc._price_val(None) is None
    assert bc._price_val({"value": True}) is None
    assert bc._price_val({"value": 0.0}, allow_zero=False) is None
    assert bc._price_val({"value": -0.0}, allow_zero=True) == 0.0
    assert bc._price_val({"value": -0.01}, allow_zero=True) is None
    assert bc._price_val({"value": "3.75"}) == 3.75

    ts = bc._from_ts([None, True, False, -10, 0.0, math.nan, math.inf, "bad", 1700000200.0, 1700000000.0])
    assert ts == datetime.fromtimestamp(1700000000.0, tz=timezone.utc)
    fallback_ts = bc._from_ts([None, True, 1e25, -5.0])
    assert fallback_ts.tzinfo == timezone.utc

    assert bc._scale_price(None, 2.0) is None
    assert bc._scale_price(True, 2.0) is None
    assert bc._scale_price(2.0, True) is None
    assert bc._scale_price(2.0, 0.0) is None
    assert bc._scale_price(1e-6, 1e-6) is None
    assert bc._scale_price(1e308, 100.0) is None
    assert bc._scale_price(1.23456, 2.0) == 2.4691


def test_merge_offer_and_apply_faults_edge_cases() -> None:
    now_utc = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    valid_price = {"productId": "464306", "catalogPrice": {"value": 2.0, "valueWithTax": 2.28}}

    miss_avail = bc.merge_offer("464306", DELIVERY, valid_price, None, now=now_utc)
    assert miss_avail.available is False
    assert "availability_not_found" in miss_avail.issues

    no_sched = bc.merge_offer("464306", DELIVERY, valid_price, {"logisticModel": "Stock", "scheduleLines": []}, now=now_utc)
    assert no_sched.available is False
    assert "no_schedule" in no_sched.issues

    bad_sched = bc.merge_offer("464306", DELIVERY, valid_price, {"logisticModel": "Stock", "scheduleLines": ["corrupt"]}, now=now_utc)
    assert bad_sched.available is False
    assert bad_sched.available_amount == 0.0
    assert "availability_error" in bad_sched.issues

    term_avail = {
        "logisticModel": "Terminal",
        "closingTime": "2026-11-15T15:00:00+00:00",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": 999999, "availableDate": "2026-11-16"}],
    }
    term_offer = bc.merge_offer("464306", DELIVERY, valid_price, term_avail, now=now_utc)
    assert term_offer.available is True
    assert term_offer.available_amount is None
    assert "terminal_unknown_qty" in term_offer.issues

    blocked_late_cutoff = {
        "logisticModel": "Stock",
        "closingTime": "2026-11-14T10:00:00",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": 0, "availableDate": "2026-11-18", "salesBlock": True}],
    }
    combo = bc.merge_offer("464306", DELIVERY, valid_price, blocked_late_cutoff, now=now_utc)
    assert combo.available is False
    assert combo.cutoff_passed is True
    assert combo.sales_block is True
    for expected_issue in ("cutoff_passed", "sales_block", "late_delivery", "out_of_stock"):
        assert expected_issue in combo.issues

    bool_qty_avail = {
        "logisticModel": "Stock",
        "scheduleLines": [{"salesUnit": "ST", "availableAmount": 0.5, "availableDate": "2026-11-16"}],
    }
    bool_qty_offer = bc.merge_offer("464306", DELIVERY, valid_price, bool_qty_avail, qty=True, now=now_utc)
    assert "limited_stock" in bool_qty_offer.issues
    assert "partial_stock" not in bool_qty_offer.issues

    faulted = bc.apply_faults(
        {"464306": bool_qty_offer},
        bc.parse_faults("stockout:464306;cutoff:464306;sales_block:464306;price_error:464306"),
    )["464306"]
    assert faulted.available is False
    assert faulted.available_amount == 0.0
    assert faulted.unit_price is None
    assert "limited_stock" not in faulted.issues
    for tag in ("injected:stockout", "injected:cutoff", "injected:sales_block", "injected:price_error"):
        assert tag in faulted.issues


def test_valio_client_cache_offline_corrupt_files_retries_and_circuit_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bc.time, "sleep", lambda _: None)

    offline_miss = bc.ValioClient(base_url="http://127.0.0.1:9", api_key="k", cache_dir=tmp_path / "c1", offline=True)
    with pytest.raises(bc.UpstreamError) as exc_off:
        offline_miss.price(["464306"], DELIVERY, customer="hotel-aurora")
    assert exc_off.value.status_code == 503

    no_key = bc.ValioClient(base_url="http://127.0.0.1:9", api_key="", cache_dir=None, offline=False)
    with pytest.raises(bc.UpstreamError) as exc_key:
        no_key.price(["464306"], DELIVERY, customer="hotel-aurora")
    assert exc_key.value.status_code == 503

    cache_dir = tmp_path / "c2"
    calls: list[dict[str, Any]] = []
    outcomes: list[Any] = [
        httpx.Response(429, headers={"Retry-After": "0.5"}, text="rate limit"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json={"timeNow": "2026-11-14T14:00:00+0200", "products": [{"productId": "464306", "ok": True}]}),
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content.decode("utf-8"))})
        nxt = outcomes.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    vc = bc.ValioClient(base_url="http://mock.local/", api_key="secret-k", cache_dir=cache_dir, ttl_s=300, offline=False, max_retries=3)
    vc._http = httpx.Client(base_url=vc.base_url, transport=httpx.MockTransport(_handler))

    corrupt_path = vc._disk_path("price", "hotel-aurora", DELIVERY.isoformat(), "464306")
    assert corrupt_path is not None
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{not-valid-json", encoding="utf-8")

    res1 = vc.price(["464306", "", "   ", "464306"], DELIVERY, customer="hotel-aurora")
    assert len(res1["products"]) == 1
    assert vc.stats["retries"] == 2
    assert vc.stats["upstream_calls"] == 3
    assert calls[-1]["body"]["products"] == ["464306"]

    res_cached = vc.price(["464306"], DELIVERY, customer="hotel-aurora")
    assert len(res_cached["products"]) == 1
    assert vc.stats["cache_hits"] == 1
    assert vc.stats["upstream_calls"] == 3

    vc_stale_offline = bc.ValioClient(base_url="http://mock.local", api_key="", cache_dir=cache_dir, ttl_s=0.0, offline=True)
    corrupt_path.write_text(
        json.dumps({"fetched_at": 1000.0, "item": {"productId": "464306", "stale": True}, "meta": {}}),
        encoding="utf-8",
    )
    stale_res = vc_stale_offline.price(["464306"], DELIVERY, customer="hotel-aurora")
    assert stale_res["products"][0]["stale"] is True

    outcomes.clear()
    outcomes.append(httpx.Response(400, text="bad request body"))
    vc_4xx = bc.ValioClient(base_url="http://mock.local", api_key="k", cache_dir=None, offline=False, max_retries=3)
    vc_4xx._http = httpx.Client(base_url=vc_4xx.base_url, transport=httpx.MockTransport(_handler))
    with pytest.raises(bc.UpstreamError) as exc_4xx:
        vc_4xx.price(["464306"], DELIVERY)
    assert exc_4xx.value.status_code == 400
    assert vc_4xx.stats["retries"] == 0
    assert vc_4xx.stats["circuit_opened"] == 0

    outcomes.clear()
    outcomes.extend([httpx.ConnectError("refused"), httpx.ConnectError("refused"), httpx.ConnectError("refused")])
    vc_net = bc.ValioClient(base_url="http://mock.local", api_key="k", cache_dir=None, offline=False, max_retries=4)
    vc_net._http = httpx.Client(base_url=vc_net.base_url, transport=httpx.MockTransport(_handler))
    with pytest.raises(bc.UpstreamError) as exc_net:
        vc_net.price(["464306"], DELIVERY)
    assert exc_net.value.status_code == 503
    assert vc_net.stats["upstream_calls"] == 2
    assert vc_net.stats["retries"] == 1
    assert vc_net.stats["circuit_opened"] == 1

    calls_before = vc_net.stats["upstream_calls"]
    with pytest.raises(bc.UpstreamError, match="circuit open"):
        vc_net.price(["464306"], DELIVERY)
    assert vc_net.stats["upstream_calls"] == calls_before

    outcomes.clear()
    outcomes.extend([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    vc_to = bc.ValioClient(base_url="http://mock.local", api_key="k", cache_dir=None, offline=False, max_retries=1)
    vc_to._http = httpx.Client(base_url=vc_to.base_url, transport=httpx.MockTransport(_handler))
    with pytest.raises(bc.UpstreamError) as exc_to:
        vc_to.availability(["464306"], DELIVERY)
    assert exc_to.value.status_code == 504
    assert vc_to.stats["retries"] == 1


def test_get_offers_with_meta_replay_fallback_injected_errors_and_legacy_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LegacyThreeArgClient:
        base_url = "legacy://adapter"
        ttl_s = 120
        stats = {"upstream_calls": 0}

        def price(self, ids: list[str], d: date, customer: str | None = None) -> dict[str, Any]:
            return {
                "products": [{"productId": i, "catalogPrice": {"value": 1.5, "valueWithTax": 1.71}} for i in ids],
                "_oldest_fetch": time.time(),
            }

        def availability(self, ids: list[str], d: date, customer: str | None = None) -> dict[str, Any]:
            return {
                "products": [
                    {
                        "productId": i,
                        "logisticModel": "Stock",
                        "scheduleLines": [{"salesUnit": "ST", "availableAmount": 25, "availableDate": d.isoformat()}],
                    }
                    for i in ids
                ],
                "_oldest_fetch": time.time(),
            }

    bc.set_client(_LegacyThreeArgClient())
    try:
        offers, meta = bc.get_offers_with_meta(["464306"], DELIVERY, fresh=True)
        assert offers["464306"].available is True
        assert meta["fresh_requested"] is True
    finally:
        bc.set_client(None)

    tok_rl = bc.set_request_faults("rate_limit:1.0")
    try:
        with pytest.raises(bc.UpstreamError) as exc_rl:
            bc.get_offers(["464306"], DELIVERY)
        assert exc_rl.value.status_code == 429
        assert exc_rl.value.injected is True
    finally:
        bc.reset_request_faults(tok_rl)

    class _DownUpstreamClient:
        base_url = "https://down.example"
        ttl_s = 300
        stats = {"upstream_calls": 1}

        def price(self, ids: list[str], d: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
            raise bc.UpstreamError("upstream price HTTP 503", 503)

        def availability(self, ids: list[str], d: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
            raise bc.UpstreamError("upstream availability HTTP 503", 503)

    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.setenv("AIMO_REPLAY_FALLBACK", str(replay.BUNDLED_DIR))
    bc.set_client(_DownUpstreamClient())
    try:
        fb_offers, fb_meta = bc.get_offers_with_meta(["464306"], DELIVERY)
        assert "464306" in fb_offers
        assert fb_meta["served_from"] == "replay"
        assert "503" in fb_meta["replay"]["fallback_reason"]
    finally:
        bc.set_client(None)


def test_clock_modes_utc_year_boundaries_overrides_and_cutoff_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("AIMO_ENV", "AIMO_DEMO_FAULTS", "AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_REPLAY", "AIMO_REPLAY_CLOCK"):
        monkeypatch.delenv(k, raising=False)

    assert clock.demo_enabled() is True
    assert clock.request_overrides_allowed(has_agent_identity=False) is True
    assert clock.request_overrides_allowed(has_agent_identity=True) is False
    monkeypatch.setenv("AIMO_DEMO_FAULTS", " 1 ")
    assert clock.request_overrides_allowed(has_agent_identity=True) is True

    monkeypatch.setenv("AIMO_ENV", "  Production ")
    assert clock.demo_enabled() is False
    assert clock.request_overrides_allowed(has_agent_identity=False) is False
    assert clock.request_overrides_allowed(has_agent_identity=True) is False
    tok_prod = clock.set_request_now("2026-11-14T14:00:00+02:00")
    try:
        assert clock.describe()["mode"] != "header"
    finally:
        clock.reset_request_now(tok_prod)

    monkeypatch.setenv("AIMO_ENV", "dev")
    for bad in (None, 123, True, "", "   ", "2000-01-01T01:00:00+02:00", "2101-01-01T00:00:00Z"):
        assert clock.parse(bad) is None
    assert clock.parse("2000-01-01T00:00:00Z") == datetime(2000, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert clock.parse("2101-01-01T00:30:00+02:00") == datetime(2101, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert clock.parse("2026-11-14T12:00:00") == datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

    tok_hdr = clock.set_request_now("2026-11-14T23:30:00Z")
    try:
        assert clock.simulated() is True
        assert clock.describe()["mode"] == "header"
        assert clock.today_local() == date(2026, 11, 15)
    finally:
        clock.reset_request_now(tok_hdr)

    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    desc_env = clock.describe()
    assert desc_env["mode"] == "env"
    assert desc_env["frozen"] is True
    assert clock.now() == datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    assert clock.describe()["mode"] == "replay"
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "real")
    assert clock.describe()["mode"] == "real"

    before_sun_cutoff = datetime(2026, 11, 15, 16, 59, tzinfo=clock.HELSINKI_TZ)
    at_sun_cutoff = datetime(2026, 11, 15, 17, 0, tzinfo=clock.HELSINKI_TZ)
    assert clock.next_delivery_day(at=before_sun_cutoff, min_days=1) == date(2026, 11, 16)
    assert clock.next_delivery_day(at=at_sun_cutoff, min_days=1) == date(2026, 11, 17)
    assert clock.next_delivery_day(at=before_sun_cutoff, min_days=1, weekday=2) == date(2026, 11, 18)


def test_conformance_suite_helpers_peppol_default_day_and_cli_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = conf_cli.Suite("http://127.0.0.1:8999/", "hotel-aurora", "buyer-1", quiet=True)
    try:
        assert s.base == "http://127.0.0.1:8999"
        assert s.row("C1", "pass check", True, "ok") is True
        assert s.row("C2", "skip check", None, "skipped detail") is False
        assert s.row("C3", "fail check", False, "x" * 600) is False
        assert len(s.rows[-1]["detail"]) == 400

        def _boom() -> None:
            raise RuntimeError("boom inside check")

        assert s.guard("G1", "guarded check", _boom) is None
        assert s.rows[-1]["status"] == "FAIL"
        assert "RuntimeError: boom inside check" in s.rows[-1]["detail"]

        conf_cli.peppol_check(s, {}, "P0")
        conf_cli.peppol_check(s, {"status": "placed", "response_code": ""}, "P_MISS")
        assert s.rows[-1]["id"] == "P_MISS" and s.rows[-1]["status"] == "FAIL"
        conf_cli.peppol_check(s, {"status": "placed", "response_code": "AP", "response_lines": [{"code": "AP"}]}, "P_OK")
        assert s.rows[-1]["id"] == "P_OK" and s.rows[-1]["status"] == "PASS"
        conf_cli.peppol_check(s, {"status": "placed", "response_code": "XX", "response_lines": [{"code": "AP"}]}, "P_BAD")
        assert s.rows[-1]["id"] == "P_BAD" and s.rows[-1]["status"] == "FAIL"

        def _cust_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "customer_id": "hotel-aurora",
                        "override_roles": ["gm"],
                        "approvers": [{"id": "boss@hotel.example", "role": "gm"}],
                    }
                ],
            )

        s.h.close()
        s.h = httpx.Client(base_url=s.base, transport=httpx.MockTransport(_cust_handler))
        assert conf_cli.find_approver(s, "missing_role") == ("boss@hotel.example", "gm")
        s.customer = "unknown-cust"
        assert conf_cli.find_approver(s, "gm") == (None, None)

        s.ctx["otap"] = {"mcp": {"url": "http://127.0.0.1:1/mcp"}}
        conf_cli.check_mcp(s)
        assert s.rows[-1]["id"] == "M1" and s.rows[-1]["status"] == "FAIL"
    finally:
        s.h.close()

    empty_tbl = conf_cli.table(
        {
            "profile": "otap/0.1",
            "target": "http://127.0.0.1:8999",
            "name": None,
            "duration_s": 0.1,
            "summary": {"pass": 0, "fail": 0, "skip": 0, "total": 0},
            "results": [],
        }
    )
    assert "0/0 passed" in empty_tbl

    monkeypatch.setattr(
        conf_cli.httpx,
        "get",
        lambda url, **kw: httpx.Response(200, json={"clock": {"now": "2026-11-14T14:00:00+02:00"}}),
    )
    assert conf_cli.default_day("http://127.0.0.1:8999") == date(2026, 11, 16)

    def _unreachable_healthz(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(conf_cli.httpx, "get", _unreachable_healthz)
    fallback_d = conf_cli.default_day("http://127.0.0.1:8999")
    assert fallback_d.weekday() < 5

    with pytest.raises(SystemExit) as exc_missing_base:
        conf_cli.main([])
    assert exc_missing_base.value.code == 2

    with pytest.raises(SystemExit) as exc_bad_date:
        conf_cli.main(["--base", "http://127.0.0.1:8999", "--date", "not-iso-date"])
    assert exc_bad_date.value.code == 2

    captured_args: dict[str, Any] = {}

    def _fake_run(
        base: str,
        customer: str,
        agent: str,
        query: str,
        product: str | None,
        day: date | None,
        approver: str | None,
        approver_role: str | None,
        quiet: bool,
    ) -> dict[str, Any]:
        captured_args.update(
            base=base,
            customer=customer,
            agent=agent,
            query=query,
            product=product,
            day=day,
            approver=approver,
            approver_role=approver_role,
            quiet=quiet,
        )
        return {
            "profile": "otap/0.1",
            "target": base,
            "name": "fake-gw",
            "duration_s": 0.0,
            "summary": {"pass": 2, "fail": 1, "skip": 0, "total": 3},
            "results": [
                {"id": "D1", "check": "otap", "status": "PASS", "detail": ""},
                {"id": "D2", "check": "fields", "status": "PASS", "detail": ""},
                {"id": "M1", "check": "mcp", "status": "FAIL", "detail": "unreachable"},
            ],
        }

    monkeypatch.setattr(conf_cli, "run", _fake_run)
    explicit_json = tmp_path / "custom-report.json"
    rc = conf_cli.main(
        [
            "--base",
            "http://127.0.0.1:8899",
            "--customer",
            "school-tampere",
            "--agent",
            "bot-9",
            "--query",
            "oat",
            "--product",
            "464306",
            "--date",
            "2026-11-18",
            "--approver",
            "liisa@school-tampere.example",
            "--approver-role",
            "kitchen_lead",
            "--json",
            str(explicit_json),
            "-q",
        ]
    )
    assert rc == 1
    assert captured_args["day"] == date(2026, 11, 18)
    assert captured_args["customer"] == "school-tampere"
    assert captured_args["quiet"] is True
    assert json.loads(explicit_json.read_text(encoding="utf-8"))["summary"]["fail"] == 1

    monkeypatch.chdir(tmp_path)
    rc_default_path = conf_cli.main(["--base", "http://127.0.0.1:8899", "-q"])
    assert rc_default_path == 1
    assert (tmp_path / "conformance-127.0.0.1_8899.json").is_file()


def test_conformance_inproc_discovery_trade_audit_and_verify_bad_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_catalog_root: Path | None
) -> None:
    store.close_all()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-pin")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    if synth_catalog_root is not None:
        pjson = synth_catalog_root / "valio_aimo_junction26_product_data.json"
        cdb = tmp_path / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()

    bc.set_client(_FixtureBackend())
    try:
        app = create_app(with_mcp=False)
        tc = TestClient(app, base_url="http://testserver", headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"})
        s = conf_cli.Suite("http://testserver", "hotel-aurora", "buyer-1", quiet=True)
        s.h.close()
        s.h = tc
        monkeypatch.setattr(conf_cli.httpx, "get", lambda url, **kw: tc.get(url))

        conf_cli.check_discovery(s)
        conf_cli.check_trade(s, "lactose free milk", DELIVERY, "464306", None, None)
        conf_cli.check_audit(s)

        failed = [r for r in s.rows if r["status"] == "FAIL"]
        assert failed == [], failed
        row_ids = {r["id"] for r in s.rows}
        for must_have in ("D1", "D6", "D7", "D10", "Q1", "Q2", "O1", "O2", "O3", "A0", "A2", "A4", "A5", "A6", "U1", "U3", "U5"):
            assert must_have in row_ids

        mcp_tools = asyncio.run(build_mcp().list_tools())
        assert len(mcp_tools) == 13
    finally:
        bc.set_client(None)
        store.close_all()
        if synth_catalog_root is not None:
            monkeypatch.setattr(fallback_catalog, "_index", None)
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()

    for bad_b64 in (123, "===", "abc=def", "abcde", "bad!char"):
        with pytest.raises(ValueError):
            conf_verify.b64d(bad_b64)
    with pytest.raises(ValueError):
        conf_verify.b64e("not-bytes")
    for bad_json_val in (math.nan, math.inf, -math.inf, object()):
        with pytest.raises(ValueError):
            conf_verify.canonical_json({"x": bad_json_val})
    assert conf_verify.canonical_json({"b": 10.0, "a": 1}) == '{"a":1,"b":10}'

    for bad_jwk in ("not-dict", {"kty": "RSA"}, {"kty": "OKP", "crv": "Ed25519", "x": conf_verify.b64e(b"short")}):
        with pytest.raises(ValueError):
            conf_verify.thumbprint(bad_jwk)
    for bad_tok in ("", 123, "only.two", f"{conf_verify.b64e(b'{\"alg\":\"none\"}')}.{conf_verify.b64e(b'{}')}.sig"):
        with pytest.raises(ValueError):
            conf_verify.verify_jws(bad_tok, {"keys": []})

    assert conf_verify.verify_quote("not-a-dict", {"keys": []})["ok"] is False
    assert conf_verify.verify_events("not-a-list", {"keys": []})["ok"] is False
    assert conf_verify.verify_events([{"seq": True}], {"keys": []})["ok"] is False


def test_this_file_has_zero_comments_and_zero_em_or_en_dashes() -> None:
    raw = Path(__file__).resolve().read_text(encoding="utf-8")
    assert chr(35) not in raw
    assert chr(0x2014) not in raw
    assert chr(0x2013) not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
