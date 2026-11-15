from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import clock
from aimo.backend import orders as store
from aimo.buyer import run_demo
from aimo.buyer.need import HELSINKI
from aimo.gateway import human_auth, service
from aimo.models import AllergenStatus, Product, ProductSummary

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
REPLAY_DIR = FIX / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"

ANNA = "anna@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
SARA = "sara@hospital-meilahti.example"

PINS_MAP = {
    ANNA: "anna-secret-pin-16",
    LIISA: "liisa-secret-pin-16",
    SARA: "sara-secret-pin-16",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _set_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()


class _FixtureBackend:
    base_url = "fixture://run-demo-hardening"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        for item in list(self.price_raw["products"]):
            if item["productId"] == "464306":
                self.price_raw["products"].append(dict(item, productId="472833"))
                break
        for item in list(self.avail_raw["products"]):
            if item["productId"] == "464306":
                self.avail_raw["products"].append(dict(item, productId="472833"))
                break

    def _sel(self, raw: dict, ids: list[str]) -> dict:
        now_ts = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now_ts, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now_ts}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.avail_raw, ids)


@contextlib.contextmanager
def _per_approver_replay_server(srv_dir: Path) -> Iterator[str]:
    data_dir = srv_dir / "data"
    key_dir = srv_dir / "keys"
    state_db = srv_dir / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    log_path = srv_dir / "uvicorn.log"
    log_file = open(log_path, "w+", encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AIMO_APPROVER_PIN", "AIMO_AGENT_KEY", "AIMO_AGENT_KEYS")
    }
    env.pop("AIMO_CATALOG_FIXTURE", None)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "AIMO_DATA_DIR": str(data_dir),
            "AIMO_STATE_DB": str(state_db),
            "AIMO_KEY_DIR": str(key_dir),
            "AIMO_APPROVER_PINS": json.dumps(PINS_MAP),
            "AIMO_REPLAY": str(REPLAY_DIR),
            "AIMO_OFFLINE": "0",
            "AIMO_NOW": "2026-11-14T15:00:00+02:00",
            "AIMO_NOW_FROZEN": "1",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "AIMO_API_KEY": "",
            "VALIO_API_KEY": "",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aimo.gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=log_file,
        stderr=log_file,
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        log_file.seek(0)
        proc.kill()
        tail = log_file.read()[-2500:]
        log_file.close()
        pytest.fail(f"aimo.gateway.app:app failed to start:\n{tail}")
    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


@pytest.fixture(scope="module")
def per_approver_live_base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not REPLAY_META.is_file():
        pytest.skip("S1 replay snapshot meta.json is not present")
    srv_dir = tmp_path_factory.mktemp("run_demo_hardening_srv")
    with _per_approver_replay_server(srv_dir) as base:
        yield base


def test_file_conventions_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src
    demo_src = (ROOT / "aimo" / "buyer" / "run_demo.py").read_text(encoding="utf-8")
    assert "\u2014" not in demo_src
    assert "\u2013" not in demo_src


def test_print_step_plan_lines_show_clean_and_flagged_injection_tags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    step = {
        "kind": "plan",
        "title": "Plan: 5 line(s)",
        "ts": "2026-11-14T15:00:05+02:00",
        "data": {
            "plan": {
                "lines": [
                    {
                        "tier": "best",
                        "product_id": "464306",
                        "qty_sales_units": 300,
                        "sales_unit": "ST",
                        "unit_price": 1.01,
                        "line_total": 303.0,
                        "product_name": "Clean Milk 1L",
                        "injection_screen": {"suspicious": False, "reasons": []},
                    },
                    {
                        "tier": "better",
                        "product_id": "469363",
                        "qty_sales_units": 100,
                        "sales_unit": "ST",
                        "unit_price": 1.15,
                        "line_total": 115.0,
                        "product_name": "Planted Milk 1L",
                        "injection_screen": {
                            "suspicious": True,
                            "reasons": ["imperative instruction ('ignore')", "spending/approval bypass"],
                        },
                    },
                    {
                        "tier": "good",
                        "product_id": "462775",
                        "qty_sales_units": 50,
                        "sales_unit": "ST",
                        "unit_price": 1.09,
                        "line_total": 54.5,
                        "product_name": "Reasonless Flagged Milk",
                        "injection_screen": {"suspicious": True, "reasons": []},
                    },
                    {
                        "tier": "good",
                        "product_id": "470863",
                        "qty_sales_units": 20,
                        "sales_unit": "ST",
                        "unit_price": 1.79,
                        "line_total": 35.8,
                        "product_name": "Flag Fallback Oat",
                        "flags": ["supplier_text_injection_suspected"],
                        "injection_screen": None,
                    },
                    {
                        "tier": "good",
                        "product_id": "401201",
                        "qty_sales_units": None,
                        "sales_unit": "ST",
                        "unit_price": 4.5,
                        "line_total": 0.0,
                        "product_name": "Unscreened Cream",
                    },
                ],
                "candidates": [],
            }
        },
    }
    run_demo.print_step(step)
    out_lines = capsys.readouterr().out.splitlines()
    line_by_pid = {
        pid: next(ln for ln in out_lines if pid in ln)
        for pid in ("464306", "469363", "462775", "470863", "401201")
    }
    assert "[injection_screen: clean]" in line_by_pid["464306"]
    assert (
        "[injection_screen: FLAGGED (imperative instruction ('ignore'), spending/approval bypass)]"
        in line_by_pid["469363"]
    )
    assert line_by_pid["462775"].endswith("[injection_screen: FLAGGED]")
    assert line_by_pid["470863"].endswith("[injection_screen: FLAGGED]")
    assert "[injection_screen:" not in line_by_pid["401201"]
    assert "?" in line_by_pid["401201"]


def test_print_step_plan_lines_ansi_styles_when_tty_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", True)
    step = {
        "kind": "plan",
        "title": "Plan: 2 line(s)",
        "ts": "2026-11-14T15:00:05+02:00",
        "data": {
            "plan": {
                "lines": [
                    {
                        "tier": "best",
                        "product_id": "464306",
                        "qty_sales_units": 10,
                        "sales_unit": "ST",
                        "unit_price": 1.01,
                        "line_total": 10.1,
                        "product_name": "Clean Milk",
                        "injection_screen": {"suspicious": False, "reasons": []},
                    },
                    {
                        "tier": "better",
                        "product_id": "469363",
                        "qty_sales_units": 10,
                        "sales_unit": "ST",
                        "unit_price": 1.15,
                        "line_total": 11.5,
                        "product_name": "Injected Milk",
                        "injection_screen": {"suspicious": True, "reasons": ["role override"]},
                    },
                ]
            }
        },
    }
    run_demo.print_step(step)
    out = capsys.readouterr().out
    assert "\033[2m[injection_screen: clean]\033[0m" in out
    assert "\033[31m[injection_screen: FLAGGED (role override)]\033[0m" in out


def test_fake_cli_injection_and_happy_print_plan_line_tags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _set_keyless(monkeypatch)
    monkeypatch.setattr(run_demo, "_TTY", False)

    inj_json = tmp_path / "inj.json"
    rc_inj = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "injection",
            "--text",
            "We need 5,100 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "--approve-delay",
            "0.02",
            "--approval-timeout",
            "10",
            "--json",
            str(inj_json),
        ]
    )
    assert rc_inj == 0
    out_inj = capsys.readouterr().out
    assert "[injection_screen: clean]" in out_inj
    assert "[injection_screen: FLAGGED" in out_inj

    happy_json = tmp_path / "happy.json"
    rc_happy = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--approve-delay",
            "0.02",
            "--approval-timeout",
            "10",
            "--json",
            str(happy_json),
        ]
    )
    assert rc_happy == 0
    out_happy = capsys.readouterr().out
    assert "[injection_screen: clean]" in out_happy


def test_http_auto_approver_uses_per_approver_pin_without_single_pin_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products = {
        "464306": Product(
            id="464306",
            name="Valio Eila lactose free semi-skimmed milk drink 1 l",
            brand="Valio",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_LACTOSE"],
        ),
        "472833": Product(
            id="472833",
            name="Fazer Aito oat drink 1 L UHT",
            brand="Fazer Aito",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_MILK", "VEGAN"],
            allergens={"milk": AllergenStatus.free, "nuts": AllergenStatus.free},
            allergen_source={"milk": "structured", "nuts": "structured"},
        ),
    }
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: [
            ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
        ],
        get_product=lambda pid: products.get(str(pid)),
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    human_auth.reset_for_tests()
    bc.set_client(_FixtureBackend())

    try:
        from aimo.gateway.app import create_app

        tc = TestClient(create_app(with_mcp=False))
        anna_pin = human_auth.local_pin(ANNA)
        liisa_pin = human_auth.local_pin(LIISA)
        assert len(anna_pin) >= 12
        assert len(liisa_pin) >= 12
        assert anna_pin != liisa_pin

        q_aurora = tc.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={
                "delivery_date": "2026-11-16",
                "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}],
            },
        ).json()
        o_aurora = tc.post(
            "/v1/orders",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={"quote_id": q_aurora["quote_id"], "quote_hash": q_aurora["quote_hash"]},
        ).json()
        assert o_aurora["status"] == "pending_approval"

        q_school_pre = tc.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
            json={
                "delivery_date": "2026-11-16",
                "lines": [{"product_id": "472833", "qty_sales_units": 450, "tier": "best"}],
                "need": {
                    "customer_id": "school-tampere",
                    "product_query": "oat drink",
                    "quantity": 450,
                    "unit": "L",
                    "delivery_by": "2026-11-16",
                    "excluded_allergens": ["milk", "nuts"],
                },
            },
        ).json()
        o_school_pre = tc.post(
            "/v1/orders",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
            json={"quote_id": q_school_pre["quote_id"], "quote_hash": q_school_pre["quote_hash"]},
        ).json()
        assert o_school_pre["status"] == "pending_approval"

        seen_pins: list[tuple[str, str]] = []

        def fake_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 5):
            path = "/" + url.split("://", 1)[1].split("/", 1)[1]
            seen_pins.append(("GET", (headers or {}).get("X-Approver-Pin", "")))
            return tc.get(path, params=params, headers=headers)

        def fake_post(url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 10):
            path = "/" + url.split("://", 1)[1].split("/", 1)[1]
            seen_pins.append(("POST", (headers or {}).get("X-Approver-Pin", "")))
            return tc.post(path, json=json, headers=headers)

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)

        stop = threading.Event()
        ready = threading.Event()
        t = threading.Thread(
            target=run_demo.http_auto_approver,
            args=("http://testserver", LIISA, "school-tampere", stop, 0.01, "approve", None, ready, 0.02),
            daemon=True,
        )
        t.start()
        assert ready.wait(3.0)

        q_school = tc.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
            json={
                "delivery_date": "2026-11-16",
                "lines": [{"product_id": "472833", "qty_sales_units": 500, "tier": "best"}],
                "need": {
                    "customer_id": "school-tampere",
                    "product_query": "oat drink",
                    "quantity": 500,
                    "unit": "L",
                    "delivery_by": "2026-11-16",
                    "excluded_allergens": ["milk", "nuts"],
                },
            },
        ).json()
        o_school = tc.post(
            "/v1/orders",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
            json={"quote_id": q_school["quote_id"], "quote_hash": q_school["quote_hash"]},
        ).json()
        assert o_school["status"] == "pending_approval"

        deadline = time.time() + 3.0
        while time.time() < deadline:
            cur = tc.get(f"/v1/orders/{o_school['order_id']}").json()
            if cur["status"] == "placed":
                break
            time.sleep(0.02)
        stop.set()
        t.join(timeout=2.0)

        school_after = tc.get(f"/v1/orders/{o_school['order_id']}").json()
        school_pre_after = tc.get(f"/v1/orders/{o_school_pre['order_id']}").json()
        aurora_after = tc.get(f"/v1/orders/{o_aurora['order_id']}").json()
        assert school_after["status"] == "placed"
        assert school_after["approved_by"] == LIISA
        assert school_pre_after["status"] == "pending_approval"
        assert aurora_after["status"] == "pending_approval"
        assert ("POST", liisa_pin) in seen_pins
        assert all(pin == liisa_pin for _, pin in seen_pins)
        assert human_auth.locked_for("testclient", LIISA) == 0.0
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)


def test_http_auto_approver_retries_5xx_and_network_error_skips_expired_and_caps_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aimo.gateway.human_auth.local_pin", lambda who=None: f"pin-for-{who}")
    post_attempts: dict[str, int] = {}

    class _Resp:
        def __init__(self, status_code: int, payload: Any = None):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> Any:
            return self._payload

    pending_items = [
        {
            "order": {"order_id": "ord-expired", "customer_id": "hotel-aurora", "quote_hash": "qh-exp"},
            "expired": True,
        },
        {
            "order": {"order_id": "ord-flaky", "customer_id": "hotel-aurora", "quote_hash": "qh-flaky"},
            "expired": False,
        },
        {
            "order": {"order_id": "ord-403", "customer_id": "hotel-aurora", "quote_hash": "qh-403"},
            "expired": False,
        },
    ]

    stop = threading.Event()
    ready = threading.Event()
    polls = 0

    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 5):
        nonlocal polls
        assert headers == {"X-Approver-Pin": f"pin-for-{ANNA}"}
        polls += 1
        if polls == 1:
            return _Resp(200, [])
        if post_attempts.get("ord-flaky", 0) >= 3 and post_attempts.get("ord-403", 0) >= 1:
            stop.set()
            return _Resp(200, [])
        return _Resp(200, pending_items)

    def fake_post(url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 10):
        assert headers == {"X-Approver-Pin": f"pin-for-{ANNA}"}
        oid = url.rsplit("/", 2)[-2]
        post_attempts[oid] = post_attempts.get(oid, 0) + 1
        n = post_attempts[oid]
        if oid == "ord-flaky":
            if n == 1:
                raise httpx.ConnectError("transient socket drop")
            if n == 2:
                return _Resp(503, {"error": "upstream_unavailable"})
            return _Resp(200, {"order_id": oid, "status": "placed"})
        if oid == "ord-403":
            return _Resp(403, {"error": "approver_role_forbidden"})
        raise AssertionError(f"unexpected post for {oid}")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    run_demo.http_auto_approver("http://gateway.local", ANNA, "hotel-aurora", stop, delay=0.0, ready=ready, poll_s=0.01)
    assert ready.is_set()
    assert "ord-expired" not in post_attempts
    assert post_attempts["ord-flaky"] == 3
    assert post_attempts["ord-403"] == 1


def test_gateway_now_syncs_env_healthz_inproc_and_handles_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:30:00+02:00")
    dt_env = run_demo._gateway_now("http://127.0.0.1:9", "mcp")
    assert dt_env == datetime(2026, 11, 14, 15, 30, tzinfo=HELSINKI)

    monkeypatch.setenv("AIMO_NOW", "2026-11-14T09:15:00")
    dt_naive = run_demo._gateway_now(None, "fake")
    assert dt_naive == datetime(2026, 11, 14, 9, 15, tzinfo=HELSINKI)

    monkeypatch.delenv("AIMO_NOW", raising=False)

    class _HealthResp:
        def __init__(self, status_code: int, payload: Any):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> Any:
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout=2: _HealthResp(
            200,
            {"ok": True, "clock": {"mode": "replay", "now": "2026-12-05T08:00:00+00:00", "frozen": True}},
        ),
    )
    dt_mcp = run_demo._gateway_now("http://gateway.local/", "mcp")
    assert dt_mcp == datetime(2026, 12, 5, 10, 0, tzinfo=HELSINKI)

    assert run_demo._gateway_now("http://gateway.local", "fake") is None

    monkeypatch.setattr(httpx, "get", lambda url, timeout=2: _HealthResp(200, {"ok": True, "fake": True}))
    assert run_demo._gateway_now("http://gateway.local", "mcp") is None

    monkeypatch.setattr(httpx, "get", lambda url, timeout=2: _HealthResp(503, {"ok": False}))
    assert run_demo._gateway_now("http://gateway.local", "mcp") is None

    def _boom(url: str, timeout: float = 2):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "get", _boom)
    assert run_demo._gateway_now("http://gateway.local", "mcp") is None

    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    assert run_demo._gateway_now(None, "inproc") is None

    tok = clock.set_request_now("2026-11-14T12:00:00+02:00")
    try:
        dt_inproc = run_demo._gateway_now(None, "inproc")
        assert dt_inproc == datetime(2026, 11, 14, 12, 0, tzinfo=HELSINKI)
    finally:
        clock.reset_request_now(tok)


def test_run_scenario_passes_healthz_synced_clock_into_buyer_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keyless(monkeypatch)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    captured: dict[str, Any] = {}

    class _DummyTool:
        def __init__(self, name: str):
            self.name = name

    class _DummyMCPTransport:
        kind = "mcp"

        def list_tools(self):
            return [_DummyTool("plan_need"), _DummyTool("create_quote"), _DummyTool("place_order")]

        def close(self) -> None:
            captured["closed"] = True

    class _HealthResp:
        status_code = 200

        def json(self) -> dict:
            return {"ok": True, "clock": {"mode": "env", "now": "2026-12-05T10:00:00+02:00", "frozen": True}}

    class _SpyAgent:
        def __init__(self, transport, customer, **kwargs):
            captured["customer"] = customer
            captured["now"] = kwargs.get("now")

        def run(self, text: str) -> dict:
            captured["text"] = text
            return {"status": "placed", "steps": []}

    monkeypatch.setattr("aimo.buyer.tools.connect", lambda *a, **k: _DummyMCPTransport())
    monkeypatch.setattr("aimo.buyer.agent.BuyerAgent", _SpyAgent)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=2: _HealthResp())

    args = argparse.Namespace(
        text=None,
        customer=None,
        base_url="http://gateway.local",
        reject=False,
        transport="mcp",
        faults=None,
        approve_delay=0.05,
        no_auto_approve=True,
        model=None,
        thinking="LOW",
        no_escalate=False,
        max_steps=14,
        approval_timeout=5.0,
        image=None,
        answer="auto",
    )
    res = run_demo.run_scenario(args, "happy", None)
    assert res["status"] == "placed"
    assert res["transport"] == "mcp"
    assert captured["now"] == datetime(2026, 12, 5, 10, 0, tzinfo=HELSINKI)
    assert captured["closed"] is True


def test_via_server_sends_auth_headers_faults_answers_clarification_and_stops_approver_on_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    monkeypatch.setenv("AIMO_AGENT_KEY", "agent-secret-xyz")
    monkeypatch.setattr(
        run_demo,
        "http_auto_approver",
        lambda *a, **k: (a[7].set() if len(a) > 7 and a[7] is not None else None, a[3].wait(5.0)),
    )

    recorded_posts: list[tuple[str, dict | None, dict | None]] = []
    recorded_streams: list[tuple[str, dict | None]] = []

    class _PostResp:
        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                req = httpx.Request("POST", "http://gw/v1/agent/runs")
                raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.status_code, request=req))

        def json(self) -> dict:
            return self._body

    class _StreamCtx:
        def __init__(self, lines: list[str]):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            return iter(self._lines)

    sse_steps = [
        {
            "kind": "needs_clarification",
            "title": "Clarify milk type",
            "ts": "2026-11-14T15:00:01+02:00",
            "data": {
                "question_id": "q-1",
                "options": [{"id": "opt-lf", "label": "Lactose-free semi-skimmed"}],
            },
        },
        {
            "kind": "plan",
            "title": "Plan: 1 line(s)",
            "ts": "2026-11-14T15:00:02+02:00",
            "data": {
                "plan": {
                    "lines": [
                        {
                            "tier": "best",
                            "product_id": "464306",
                            "qty_sales_units": 300,
                            "sales_unit": "ST",
                            "unit_price": 1.01,
                            "line_total": 303.0,
                            "product_name": "Valio Eila 1L",
                            "injection_screen": {"suspicious": True, "reasons": ["imperative instruction"]},
                        }
                    ]
                }
            },
        },
        {
            "kind": "final",
            "title": "Done: placed",
            "ts": "2026-11-14T15:00:03+02:00",
            "data": {"status": "placed", "summary": "Placed order.", "latency_s": 0.4, "escalated": False},
        },
    ]
    sse_lines = [f"data: {json.dumps(s)}" for s in sse_steps]

    def fake_post(url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 10):
        recorded_posts.append((url, json, headers))
        if url.endswith("/v1/agent/runs"):
            return _PostResp(200, {"run_id": "run-abc123"})
        return _PostResp(200, {"ok": True})

    @contextlib.contextmanager
    def fake_stream(method: str, url: str, headers: dict | None = None, timeout: Any = None):
        assert method == "GET"
        recorded_streams.append((url, headers))
        yield _StreamCtx(sse_lines)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "stream", fake_stream)

    args = argparse.Namespace(
        scenario="injection",
        thinking="LOW",
        no_escalate=False,
        model=None,
        transport="auto",
        faults=None,
        no_auto_approve=False,
        auto_approve_as=ANNA,
        approve_delay=0.05,
        answer="auto",
    )
    stop_ok = threading.Event()
    rc = run_demo._via_server(args, "http://gw", "300 L milk", "hotel-aurora", stop_ok, "approve")
    assert rc == 0
    assert stop_ok.is_set()

    expected_headers = {
        "X-Customer-Id": "hotel-aurora",
        "X-Approver-Pin": human_auth.local_pin(ANNA),
        "X-Agent-Key": "agent-secret-xyz",
    }
    assert recorded_posts[0][0] == "http://gw/v1/agent/runs"
    assert recorded_posts[0][1] is not None and recorded_posts[0][1]["faults"] == "inject_text:*"
    assert recorded_posts[0][2] == expected_headers
    assert recorded_streams == [("http://gw/v1/agent/runs/run-abc123/events", expected_headers)]
    assert recorded_posts[1] == (
        "http://gw/v1/agent/runs/run-abc123/answer",
        {"option_id": "opt-lf", "customer_id": "hotel-aurora", "question_id": "q-1"},
        expected_headers,
    )
    out = capsys.readouterr().out
    assert "[injection_screen: FLAGGED (imperative instruction)]" in out

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _PostResp(403, {"error": "forbidden"}))
    stop_err = threading.Event()
    with pytest.raises(httpx.HTTPStatusError):
        run_demo._via_server(args, "http://gw", "300 L milk", "hotel-aurora", stop_err, "approve")
    assert stop_err.is_set()


@pytest.mark.skipif(not REPLAY_META.is_file(), reason="S1 replay snapshot meta.json is not present")
def test_live_replay_http_per_approver_pins_healthz_clock_and_injection_tags(
    per_approver_live_base: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _set_keyless(monkeypatch)
    monkeypatch.setattr(run_demo, "_TTY", False)
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_APPROVER_PINS", json.dumps(PINS_MAP))
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_OFFLINE", "0")

    school_json = tmp_path / "school_placed.json"
    rc_school = run_demo.main(
        [
            "--base-url",
            per_approver_live_base,
            "--scenario",
            "school-allergen",
            "--auto-approve-as",
            LIISA,
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "20",
            "--json",
            str(school_json),
        ]
    )
    school_out = capsys.readouterr().out
    assert rc_school == 0, school_out
    school_res = json.loads(school_json.read_text(encoding="utf-8"))
    assert school_res["status"] == "placed"
    assert school_res["transport"] == "mcp"
    assert school_res["need"]["delivery_by"] == "2026-11-18"
    assert school_res["order"]["approved_by"] == "policy:school-tampere"
    assert school_res["allergen_probe"]["ran"] is True
    assert school_res["allergen_probe"]["status"] == "rejected"
    assert "[injection_screen: clean]" in school_out
    assert "Verdict [school-allergen]:" in school_out

    school_big_json = tmp_path / "school_big_placed.json"
    rc_school_big = run_demo.main(
        [
            "--base-url",
            per_approver_live_base,
            "--scenario",
            "school-allergen",
            "--text",
            "We need 400 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.",
            "--auto-approve-as",
            LIISA,
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "20",
            "--json",
            str(school_big_json),
        ]
    )
    school_big_out = capsys.readouterr().out
    assert rc_school_big == 0, school_big_out
    school_big_res = json.loads(school_big_json.read_text(encoding="utf-8"))
    assert school_big_res["status"] == "placed"
    assert school_big_res["order"]["approved_by"] == LIISA

    inj_json = tmp_path / "inj_placed.json"
    rc_inj = run_demo.main(
        [
            "--base-url",
            per_approver_live_base,
            "--scenario",
            "injection",
            "--auto-approve-as",
            ANNA,
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "20",
            "--json",
            str(inj_json),
        ]
    )
    inj_out = capsys.readouterr().out
    assert rc_inj == 0, inj_out
    inj_res = json.loads(inj_json.read_text(encoding="utf-8"))
    assert inj_res["status"] == "placed"
    assert inj_res["transport"] == "mcp"
    assert inj_res["need"]["delivery_by"] == "2026-11-16"
    assert inj_res["order"]["approved_by"] == ANNA
    assert "[injection_screen: FLAGGED" in inj_out

    rc_via = run_demo.main(
        [
            "--via-server",
            "--base-url",
            per_approver_live_base,
            "--scenario",
            "injection",
            "--text",
            "We need 300 litres of lactose-free milk by 2026-11-16. Check the product details of the alternatives before choosing.",
            "--auto-approve-as",
            ANNA,
            "--approve-delay",
            "0.05",
        ]
    )
    via_out = capsys.readouterr().out
    assert rc_via == 0, via_out
    assert "[injection_screen: FLAGGED" in via_out
    assert "status=placed" in via_out


def test_scenario_verdict_cutoff_split_allergen_probe_pending_and_mock_tags() -> None:
    def cand(pid: str, tier: str, reason: str = "") -> dict[str, Any]:
        return {"product": {"id": pid}, "tier": tier, "reasons": [reason] if reason else []}

    lines = [{"product_id": p, "qty_sales_units": q} for p, q in (("464306", 400), ("464201", 400), ("463779", 80))]
    stockout_res = {
        "status": "pending_approval",
        "mock": True,
        "need": {"preferred_product_ids": ["463038"]},
        "quote": {"lines": lines, "total_net": 2544.8},
        "plan": {
            "lines": lines,
            "shortfall": 0,
            "candidates": [
                cand("463038", "rejected", "Rejected: order cut-off passed (Thu 12 Nov 15:00) for delivery Mon 16 Nov"),
                cand("469040", "rejected", "Rejected: no structured allergen data"),
                cand("471424", "rejected", "Rejected: sales block on requested date"),
            ],
        },
        "steps": [{"kind": "approval_wait", "title": "", "data": {"approver_role": "procurement_manager"}}],
    }
    v_stockout = run_demo.scenario_verdict("stockout", stockout_res)
    assert "pending_approval" in v_stockout
    assert "3 line(s), EUR 2,544.80, shortfall 0" in v_stockout
    assert "3 candidate(s) rejected (1 cut-off, 1 allergen, 1 unavailable)" in v_stockout
    assert "cut-off substitution: usual 463038 -> 463779, 464201, 464306" in v_stockout
    assert "partial stock split across 464306 x400, 464201 x400, 463779 x80" in v_stockout
    assert "waiting for a human (procurement_manager); nothing ordered" in v_stockout
    assert "model: scripted (no Gemini)" in v_stockout

    school_res = {
        "status": "placed",
        "need": {"excluded_allergens": ["milk", "nuts"]},
        "order": {"approved_by": "policy:school-tampere"},
        "quote": {"lines": [{"product_id": "470889", "qty_sales_units": 200}], "total_net": 280.0},
        "plan": {"shortfall": 0, "candidates": []},
        "steps": [
            {
                "kind": "decision",
                "title": "",
                "data": {
                    "decision": {
                        "hits": [
                            {
                                "rule_id": "allergens",
                                "outcome": "allow",
                                "message": "All lines are confirmed free of milk or nuts.",
                            }
                        ]
                    }
                },
            }
        ],
        "allergen_probe": {
            "ran": True,
            "status": "rejected",
            "reason": "'Valio Eila milk' (464306) contains milk, which is excluded for this customer.",
        },
    }
    v_school = run_demo.scenario_verdict("school-allergen", school_res)
    assert "excluded milk, nuts; allergen rule: [allow] All lines are confirmed free of milk or nuts." in v_school
    assert "approved by policy:school-tampere" in v_school
    assert "allergen probe (464306, cow's milk): rejected by policy (contains milk" in v_school
    assert "scripted (no Gemini)" not in v_school

    inj_drift_res = {
        "status": "needs_confirmation",
        "quote": {"lines": [{"product_id": "469363", "qty_sales_units": 100}], "total_net": 115.0},
        "plan": {"shortfall": 0, "candidates": []},
        "steps": [
            {
                "kind": "decision",
                "title": "",
                "data": {
                    "decision": {
                        "hits": [
                            {"rule_id": "security.prompt_injection", "outcome": "needs_approval", "message": "flagged"},
                            {"rule_id": "spend.quantity_drift", "outcome": "needs_approval", "message": "drift"},
                        ]
                    }
                },
            }
        ],
    }
    v_drift = run_demo.scenario_verdict("drift", inj_drift_res)
    assert "injection flagged on 1 line(s), needs a human" in v_drift
    assert "standing-order drift held for human confirmation" in v_drift

    v_err = run_demo.scenario_verdict("happy", {"status": "error", "error": "upstream timeout"})
    assert v_err == "error: upstream timeout"


def test_allergen_probe_cows_milk_rejected_by_school_policy_and_handles_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aimo.buyer.profiles import mandate_for
    from aimo.buyer.tools import connect

    monkeypatch.setattr(run_demo, "_TTY", False)
    products = {
        "464306": Product(
            id="464306",
            name="Valio Eila lactose free semi-skimmed milk drink 1 l",
            brand="Valio",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_LACTOSE"],
            allergens={"milk": AllergenStatus.contains},
            allergen_source={"milk": "structured"},
        )
    }
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: [
            ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
        ],
        get_product=lambda pid: products.get(str(pid)),
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    bc.set_client(_FixtureBackend())
    try:
        transport = connect(
            "inproc",
            agent_id="school-tampere/buyer-1",
            customer_id="school-tampere",
            mandate=mandate_for("school-tampere"),
        )
        try:
            probe = run_demo.allergen_probe(transport, {"quote": {"delivery_date": "2026-11-16"}})
        finally:
            transport.close()
        out = capsys.readouterr().out
        assert run_demo.PROBE_MILK == "464306"
        assert probe["ran"] is True
        assert probe["status"] == "rejected"
        assert probe["order_id"]
        assert "milk" in (probe["reason"] or "").lower()
        assert "Allergen probe: the agent tries to order 1 x 464306 (cow's milk)" in out
        assert "[reject] allergens" in out
    finally:
        bc.set_client(None)

    class _BrokenTransport:
        def call(self, name: str, args: dict) -> Any:
            raise RuntimeError("socket closed")

    failed = run_demo.allergen_probe(_BrokenTransport(), {"need": {"delivery_by": "2026-11-16"}})
    assert failed["ran"] is False
    assert "RuntimeError: socket closed" in failed["error"]


def test_approvals_line_and_montage_baseline_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keyless(monkeypatch)
    rows = [
        (
            "happy",
            {"status": "placed", "customer_id": "hotel-aurora", "transport": "inproc", "latency_s": 0.2, "run_id": "r1",
             "order": {"approved_by": ANNA}},
            True,
            "placed; 1 line(s)",
            {},
        ),
        (
            "school-allergen",
            {"status": "placed", "customer_id": "school-tampere", "transport": "inproc", "latency_s": 0.2, "run_id": "r2",
             "order": {"approved_by": "policy:school-tampere"}},
            True,
            "placed; oat drink",
            {},
        ),
        (
            "drift",
            {"status": "needs_confirmation", "customer_id": "hotel-aurora", "transport": "inproc", "latency_s": 0.1,
             "run_id": "r3", "order": None},
            True,
            "needs_confirmation",
            {},
        ),
    ]
    line = run_demo._approvals_line(rows)
    assert line.startswith(f"**Approvals:** 1 order(s) approved by a named human (happy: {ANNA})")
    assert "POST /v1/approvals/{id}/decision" in line
    assert "X-Approver-Pin" in line
    assert "1 auto-approved by the customer's own policy within its limits (school-allergen)" in line
    assert "1 not placed (drift: needs_confirmation)" in line

    tot = {
        "calls": 0, "in": 0, "cached": 0, "out": 0, "thinking": 0, "gemini_s": 0.0,
        "cost_usd": 0.0, "escalated_runs": 0, "escalated_steps": 0, "agent_steps": 0, "runs": 3,
    }
    base_summary = {
        "calls": 18, "in": 12000, "cached": 6000, "out": 900, "thinking": 300, "gemini_s": 4.2,
        "cost_usd": 0.0352, "escalated_runs": 1, "escalated_steps": 2, "agent_steps": 18, "runs": 6,
    }
    started = datetime(2026, 11, 15, 3, 0, tzinfo=HELSINKI)

    out_custom = tmp_path / "montage_custom.md"
    a_custom = types.SimpleNamespace(
        model=None, no_escalate=False, base_url="http://localhost:8000", thinking="LOW",
        baseline_pid=42, baseline_orders=5, baseline_label="Sunday live baseline",
    )
    run_demo._write_montage(str(out_custom), started, a_custom, rows, tot, 0.5, 2, 3, base_summary)
    md_custom = out_custom.read_text(encoding="utf-8")
    assert "**Approvals:** 1 order(s) approved by a named human" in md_custom
    assert "| Sunday live baseline | 6 | 5 |" in md_custom
    assert "| This run | 3 | 2 |" in md_custom
    assert "mock-replay (no Gemini)" in md_custom

    out_default = tmp_path / "montage_default.md"
    a_default = types.SimpleNamespace(
        model=None, no_escalate=False, base_url="http://localhost:8000", thinking="LOW",
        baseline_pid=42, baseline_orders=5, baseline_label=None,
    )
    run_demo._write_montage(str(out_default), started, a_default, rows, tot, 0.5, 2, 3, base_summary)
    md_default = out_default.read_text(encoding="utf-8")
    assert "| Before token trim (round 2 montage, ledger pid 42) | 6 | 5 |" in md_default


def test_http_auto_approver_baseline_exclusion_and_409_replacement_order_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aimo.gateway.human_auth.local_pin", lambda who=None: f"pin-for-{who}")

    class _Resp:
        def __init__(self, status_code: int, payload: Any):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> Any:
            return self._payload

    class _Client:
        def __init__(self) -> None:
            self.gets = 0
            self.posts: list[tuple[str, dict]] = []
            self.replaced = False

        def get(self, url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 5):
            assert headers == {"X-Approver-Pin": f"pin-for-{ANNA}"}
            self.gets += 1
            pre = {"order": {"order_id": "ORD-pre", "customer_id": "hotel-aurora", "quote_hash": "qh-pre"}}
            other = {"order": {"order_id": "ORD-school", "customer_id": "school-tampere", "quote_hash": "qh-sch"}}
            stale = {"order": {"order_id": "ORD-stale", "customer_id": "hotel-aurora", "quote_hash": "qh-stale"}}
            fresh = {"order": {"order_id": "ORD-fresh", "customer_id": "hotel-aurora", "quote_hash": "qh-fresh"}}
            if self.gets == 1:
                return _Resp(200, [pre, other])
            if not self.replaced:
                return _Resp(200, [pre, other, stale])
            return _Resp(200, [pre, other, stale, fresh])

        def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 10):
            assert headers == {"X-Approver-Pin": f"pin-for-{ANNA}"}
            assert "X-Agent-Id" not in (headers or {})
            assert json is not None
            self.posts.append((url, json))
            if url.endswith("/ORD-stale/decision"):
                self.replaced = True
                return _Resp(
                    409,
                    {"error": "requote_needed", "detail": {"new_order": {"order_id": "ORD-fresh"}}},
                )
            if url.endswith("/ORD-fresh/decision"):
                stop.set()
                return _Resp(200, {"order_id": "ORD-fresh", "status": "placed"})
            raise AssertionError(f"unexpected decision post: {url}")

    client = _Client()
    stop = threading.Event()
    ready = threading.Event()
    worker = threading.Thread(
        target=run_demo.http_auto_approver,
        args=("", ANNA, "hotel-aurora", stop, 0.0, "approve", client, ready, 0.01),
        daemon=True,
    )
    worker.start()
    assert ready.wait(3.0)
    worker.join(timeout=3.0)
    assert stop.is_set()
    assert [u for u, _ in client.posts] == [
        "/v1/approvals/ORD-stale/decision",
        "/v1/approvals/ORD-fresh/decision",
    ]
    assert client.posts[0][1]["quote_hash"] == "qh-stale"
    assert client.posts[1][1]["quote_hash"] == "qh-fresh"
    assert client.posts[1][1]["approver"] == ANNA

    monkeypatch.undo()
    products = {
        "464306": Product(
            id="464306",
            name="Valio Eila lactose free semi-skimmed milk drink 1 l",
            brand="Valio",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_LACTOSE"],
        )
    }
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: [
            ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
        ],
        get_product=lambda pid: products.get(str(pid)),
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state_409.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state_409.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys_409")
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    human_auth.reset_for_tests()
    bc.set_client(_FixtureBackend())
    try:
        from aimo.gateway.app import create_app

        tc = TestClient(create_app(with_mcp=False))
        q_pre = tc.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 900, "tier": "best"}]},
        ).json()
        o_pre = tc.post(
            "/v1/orders",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={"quote_id": q_pre["quote_id"], "quote_hash": q_pre["quote_hash"]},
        ).json()
        assert o_pre["status"] == "pending_approval"

        decision_statuses: list[tuple[str, int]] = []

        class _ClockAdvancingClient:
            app = tc.app

            def get(self, url: str, params: dict | None = None, headers: dict | None = None):
                return tc.get(url, params=params, headers=headers)

            def post(self, url: str, json: dict | None = None, headers: dict | None = None):
                if not decision_statuses:
                    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:20:00+02:00")
                resp = tc.post(url, json=json, headers=headers)
                decision_statuses.append((url, resp.status_code))
                return resp

        stop_real = threading.Event()
        ready_real = threading.Event()
        th_real = threading.Thread(
            target=run_demo.http_auto_approver,
            args=("", ANNA, "hotel-aurora", stop_real, 0.0, "approve", _ClockAdvancingClient(), ready_real, 0.02),
            daemon=True,
        )
        th_real.start()
        assert ready_real.wait(3.0)

        q_run = tc.post(
            "/v1/quotes",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
        ).json()
        o_run = tc.post(
            "/v1/orders",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={"quote_id": q_run["quote_id"], "quote_hash": q_run["quote_hash"]},
        ).json()
        assert o_run["status"] == "pending_approval"

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if len(decision_statuses) >= 2 and decision_statuses[-1][1] == 200:
                break
            time.sleep(0.02)
        stop_real.set()
        th_real.join(timeout=2.0)

        assert len(decision_statuses) == 2
        assert decision_statuses[0] == (f"/v1/approvals/{o_run['order_id']}/decision", 409)
        assert decision_statuses[1][1] == 200
        replacement_oid = decision_statuses[1][0].rsplit("/", 2)[-2]
        assert replacement_oid != o_run["order_id"]
        assert tc.get(f"/v1/orders/{o_pre['order_id']}").json()["status"] == "pending_approval"
        placed_replacement = tc.get(f"/v1/orders/{replacement_oid}").json()
        assert placed_replacement["status"] == "placed"
        assert placed_replacement["approved_by"] == ANNA
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)


def test_keyless_no_auto_approve_defaults_to_15s_wait_and_preserves_explicit_or_gemini_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_keyless(monkeypatch)
    monkeypatch.setattr(run_demo, "_TTY", False)
    monkeypatch.delenv("AIMO_AUTO_APPROVE", raising=False)

    timeouts: list[float] = []

    class _SpyAgent:
        def __init__(self, transport, customer, **kwargs):
            timeouts.append(kwargs.get("approval_timeout_s"))

        def run(self, text: str) -> dict:
            return {
                "status": "pending_approval",
                "mock": True,
                "steps": [{"kind": "approval_wait", "title": "", "data": {"approver_role": "procurement_manager"}}],
            }

    monkeypatch.setattr("aimo.buyer.agent.BuyerAgent", _SpyAgent)

    rc = run_demo.main(["--transport", "fake", "--scenario", "happy", "--no-auto-approve"])
    out = capsys.readouterr().out
    assert rc == 0
    assert timeouts[-1] == 15.0
    assert "waiting 15 s" in out
    assert "Verdict [happy]:" in out
    assert "waiting for a human (procurement_manager); nothing ordered" in out

    sn_none = types.SimpleNamespace(
        text=None, customer=None, base_url="http://127.0.0.1:9", reject=False,
        transport="fake", faults=None, approve_delay=0.01, no_auto_approve=True,
        model=None, thinking="LOW", no_escalate=False, max_steps=14,
        approval_timeout=None, image=None, answer="auto",
    )
    run_demo.run_scenario(sn_none, "happy", None)
    assert timeouts[-1] == 15.0

    sn_1800 = types.SimpleNamespace(**{**sn_none.__dict__, "approval_timeout": 1800})
    run_demo.run_scenario(sn_1800, "happy", None)
    assert timeouts[-1] == 1800

    sn_auto = types.SimpleNamespace(**{**sn_none.__dict__, "no_auto_approve": False, "approval_timeout": None})
    run_demo.run_scenario(sn_auto, "happy", None)
    assert timeouts[-1] == 1800.0

    monkeypatch.setattr(config, "GEMINI_API_KEY", "live-key-present")
    monkeypatch.setenv("GEMINI_API_KEY", "live-key-present")
    run_demo.run_scenario(sn_none, "happy", None)
    assert timeouts[-1] == 1800.0
