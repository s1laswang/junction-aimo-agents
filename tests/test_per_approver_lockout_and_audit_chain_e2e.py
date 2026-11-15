from __future__ import annotations

import json
import os
import time
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import catalog as aimo_catalog
from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import human_auth, service
from aimo.gateway.app import create_app
from aimo.models import AllergenStatus, Need, Plan, PlanLine, Product, ProductSummary
from conformance import verify as conformance_verify

FIX = Path(__file__).parent / "fixtures"
DELIVERY_DAY = date(2026, 11, 16)

ANNA = "anna@hotel-aurora.example"
MIKKO = "mikko@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
SARA = "sara@hospital-meilahti.example"
OLLI = "olli@hospital-meilahti.example"
HELENA = "helena@hospital-meilahti.example"

H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}


def _load_fixture_with_oat(filename: str) -> dict[str, Any]:
    raw = json.loads((FIX / filename).read_text(encoding="utf-8"))
    if not any(p["productId"] == "470889" for p in raw["products"]):
        for item in raw["products"]:
            if item["productId"] == "464306":
                raw["products"].append(dict(item, productId="470889"))
                break
    return raw


_PRICE_RAW = _load_fixture_with_oat("price_20261116.json")
_AVAIL_RAW = _load_fixture_with_oat("availability_20261116.json")
_SHARED_APP = create_app(with_mcp=False)


class _FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def _sel(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict[str, Any]:
        return self._sel(_PRICE_RAW, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict[str, Any]:
        return self._sel(_AVAIL_RAW, ids)


def _stub_plan(need: Need) -> Plan:
    pid = "470889" if need.customer_id == "school-tampere" else "464306"
    name = (
        "Oatly Oat Drink Whole 1 L"
        if pid == "470889"
        else "Valio Eila lactose free semi-skimmed milk drink 1 l"
    )
    line = PlanLine(
        product_id=pid,
        product_name=name,
        tier="best",
        qty_sales_units=need.quantity,
        sales_unit="ST",
        covered_amount=need.quantity,
        unit_price=1.01,
        line_total=round(need.quantity * 1.01, 2),
        reasons=["deterministic fixture plan"],
        flags=[],
    )
    return Plan(
        need=need,
        lines=[line],
        candidates=[],
        covered_amount=need.quantity,
        shortfall=0.0,
        total_net=line.line_total,
        explanation="deterministic plan",
    )


@pytest.fixture(autouse=True)
def isolated_gateway_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
            allergens={"gluten": AllergenStatus.free},
            allergen_source={"gluten": "structured"},
        ),
        "470889": Product(
            id="470889",
            name="Oatly Oat Drink Whole 1 L",
            brand="Oatly",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_MILK", "VEGAN"],
            allergens={
                "milk": AllergenStatus.free,
                "nuts": AllergenStatus.free,
                "gluten": AllergenStatus.free,
            },
            allergen_source={
                "milk": "structured",
                "nuts": "structured",
                "gluten": "structured",
            },
        ),
        "469363": Product(
            id="469363",
            name="Valio butter 500 g",
            brand="Valio",
            category="10400",
            sales_unit="ST",
            content_amount=0.5,
            content_unit="KG",
            claims=[],
            allergens={},
            allergen_source={},
        ),
    }
    summaries = [
        ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
    ]
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: summaries,
        get_product=lambda pid: products.get(str(pid)),
        plan_need=_stub_plan,
        catalog_available=lambda: True,
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(aimo_catalog, "get_product", lambda pid: products.get(str(pid)))
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {})
    monkeypatch.setattr(textsearch, "fts_search", lambda *a, **kw: [])
    db_file = tmp_path / "state.sqlite"
    monkeypatch.setattr(store, "DB_PATH", db_file)
    monkeypatch.setattr(config, "STATE_DB", db_file)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    for env_key in (
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
        "AIMO_ENV",
    ):
        monkeypatch.delenv(env_key, raising=False)

    store.close_all()
    human_auth.reset_for_tests()
    bc.set_client(_FixtureClient())
    yield
    human_auth.reset_for_tests()
    bc.set_client(None)
    store.close_all()


def _make_app():
    return _SHARED_APP


def _client_for(app: Any, host: str = "198.51.100.1", port: int = 50000) -> TestClient:
    return TestClient(app, client=(host, port))


def _assert_err(resp: Any, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, (resp.status_code, resp.text)
    body = resp.json()
    assert isinstance(body, dict), body
    assert body.get("error") == code, body
    assert isinstance(body.get("message"), str) and body["message"], body
    return body


def _create_pending_aurora(c: TestClient, qty: float = 1000) -> tuple[dict[str, Any], dict[str, Any]]:
    q_resp = c.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": qty, "tier": "best"}],
        },
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    o_resp = c.post(
        "/v1/orders",
        headers=H_AURORA,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "pending_approval", order
    assert order["decision"]["approver_role"] == "procurement_manager", order
    return quote, order


def _create_pending_school(c: TestClient, qty: float = 500) -> tuple[dict[str, Any], dict[str, Any]]:
    q_resp = c.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "470889", "qty_sales_units": qty, "tier": "preferred"}],
        },
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    o_resp = c.post(
        "/v1/orders",
        headers=H_SCHOOL,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "pending_approval", order
    assert order["decision"]["approver_role"] == "kitchen_manager", order
    return quote, order


def _create_pending_hospital_dietitian(c: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    q_resp = c.post(
        "/v1/quotes",
        headers=H_HOSPITAL,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": 10}],
        },
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    o_resp = c.post(
        "/v1/orders",
        headers=H_HOSPITAL,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "pending_approval", order
    assert order["decision"]["approver_role"] == "dietitian", order
    return quote, order


def _assert_chain_ok(c: TestClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jwks = c.get("/.well-known/jwks.json").json()
    events = c.get("/v1/audit").json()
    assert len(events) >= 1
    gw_verify = c.get("/v1/audit/verify").json()
    assert gw_verify["ok"] is True, gw_verify
    assert gw_verify["broken_at"] is None, gw_verify
    assert gw_verify["checked"] == len(events), gw_verify
    indep = conformance_verify.verify_events(events, jwks, contiguous=True)
    assert indep["ok"] is True, indep
    assert indep["checked"] == len(events), indep
    assert indep["problems"] == [], indep
    return events, jwks


def test_file_has_zero_comments_and_zero_em_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src
    assert os.environ.get("AIMO_OFFLINE") == "1"
    assert os.environ.get("GEMINI_API_KEY") == ""
    assert os.environ.get("GOOGLE_API_KEY") == ""


def test_single_source_max_failures_locks_anna_with_retry_after_and_signed_refusal_while_peers_and_session_succeed() -> None:
    app = _make_app()
    c_src = _client_for(app, "198.51.100.10", 51010)
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin, liisa_pin = pins[ANNA], pins[MIKKO], pins[LIISA]

    r_pre_sess = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    assert r_pre_sess.status_code == 200, r_pre_sess.text
    anna_session = r_pre_sess.json()["session"]
    assert r_pre_sess.json()["approver"] == ANNA

    q_aurora_mikko, o_aurora_mikko = _create_pending_aurora(c_src)
    q_aurora_anna, o_aurora_anna = _create_pending_aurora(c_src)
    q_school_liisa, o_school_liisa = _create_pending_school(c_src)

    max_fail = human_auth.MAX_FAILURES
    assert max_fail >= 2
    for idx in range(max_fail - 1):
        r_wrong = c_src.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": f"wrong-anna-guess-{idx}"},
            json={"approver": ANNA},
        )
        _assert_err(r_wrong, 401, "approver_auth_required")

    r_lock = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "wrong-anna-final"},
        json={"approver": ANNA},
    )
    lock_body = _assert_err(r_lock, 429, "approver_locked")
    assert isinstance(lock_body.get("detail"), dict), lock_body
    retry_after = lock_body["detail"].get("retry_after")
    assert isinstance(retry_after, int) and 1 <= retry_after <= int(human_auth.LOCK_S) + 1, lock_body

    r_anna_valid_pin_signin = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    blocked_signin = _assert_err(r_anna_valid_pin_signin, 429, "approver_locked")
    assert int(blocked_signin["detail"]["retry_after"]) >= 1

    r_anna_valid_pin_decide = c_src.post(
        f"/v1/approvals/{o_aurora_anna['order_id']}/decision",
        headers={"X-Approver-Pin": anna_pin},
        json={"decision": "approve", "quote_hash": q_aurora_anna["quote_hash"], "approver": ANNA},
    )
    blocked_decide = _assert_err(r_anna_valid_pin_decide, 429, "approver_locked")
    assert int(blocked_decide["detail"]["retry_after"]) >= 1

    r_mikko_signin = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": mikko_pin},
        json={"approver": MIKKO},
    )
    assert r_mikko_signin.status_code == 200 and r_mikko_signin.json()["approver"] == MIKKO

    r_mikko_decide = c_src.post(
        f"/v1/approvals/{o_aurora_mikko['order_id']}/decision",
        headers={"X-Approver-Pin": mikko_pin},
        json={"decision": "approve", "quote_hash": q_aurora_mikko["quote_hash"], "approver": MIKKO},
    )
    assert r_mikko_decide.status_code == 200, r_mikko_decide.text
    assert r_mikko_decide.json()["status"] == "placed"
    assert r_mikko_decide.json()["approved_by"] == MIKKO

    r_liisa_signin = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": liisa_pin},
        json={"approver": LIISA},
    )
    assert r_liisa_signin.status_code == 200 and r_liisa_signin.json()["approver"] == LIISA

    r_liisa_decide = c_src.post(
        f"/v1/approvals/{o_school_liisa['order_id']}/decision",
        headers={"X-Approver-Pin": liisa_pin},
        json={"decision": "approve", "quote_hash": q_school_liisa["quote_hash"], "approver": LIISA},
    )
    assert r_liisa_decide.status_code == 200, r_liisa_decide.text
    assert r_liisa_decide.json()["status"] == "placed"
    assert r_liisa_decide.json()["approved_by"] == LIISA

    r_anna_sess_decide = c_src.post(
        f"/v1/approvals/{o_aurora_anna['order_id']}/decision",
        headers={"X-Approver-Session": anna_session},
        json={"decision": "approve", "quote_hash": q_aurora_anna["quote_hash"]},
    )
    assert r_anna_sess_decide.status_code == 200, r_anna_sess_decide.text
    assert r_anna_sess_decide.json()["status"] == "placed"
    assert r_anna_sess_decide.json()["approved_by"] == ANNA

    r_still_locked = c_src.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    _assert_err(r_still_locked, 429, "approver_locked")

    events, jwks = _assert_chain_ok(c_src)
    lock_refusals = [
        ev
        for ev in events
        if ev["action"] == "approval.refused"
        and ev["actor"] == f"human:{ANNA}"
        and f"{max_fail} wrong approver credentials for {ANNA}" in str(ev.get("data", {}).get("reason", ""))
    ]
    assert len(lock_refusals) == 1, events
    header, payload = conformance_verify.verify_jws(lock_refusals[0]["signature"], jwks)
    assert header["alg"] == "EdDSA"
    assert payload["hash"] == lock_refusals[0]["hash"]
    assert payload["seq"] == lock_refusals[0]["seq"]


def test_rotating_client_sources_exhausting_max_failures_and_backstop_locks_anna_globally_without_affecting_others() -> None:
    app = _make_app()
    c_init = _client_for(app, "192.0.2.1", 52001)
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin, liisa_pin = pins[ANNA], pins[MIKKO], pins[LIISA]

    anna_session = c_init.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    q_aurora_mikko, o_aurora_mikko = _create_pending_aurora(c_init)
    q_aurora_anna, o_aurora_anna = _create_pending_aurora(c_init)
    q_school_liisa, o_school_liisa = _create_pending_school(c_init)

    max_fail = human_auth.MAX_FAILURES
    global_max = human_auth.GLOBAL_MAX
    waves = max(1, (global_max + max_fail - 1) // max_fail)

    locked_sources: list[str] = []
    total_attempts = 0
    for wave_idx in range(waves):
        src_ip = f"203.0.113.{wave_idx + 1}"
        c_rot = _client_for(app, src_ip, 53000 + wave_idx)
        for attempt_idx in range(max_fail):
            total_attempts += 1
            r = c_rot.post(
                f"/v1/approvals/{o_aurora_anna['order_id']}/decision",
                headers={"X-Approver-Pin": f"rot-wrong-{wave_idx}-{attempt_idx}"},
                json={"decision": "approve", "quote_hash": q_aurora_anna["quote_hash"], "approver": ANNA},
            )
            will_lock = (attempt_idx + 1 >= max_fail) or (total_attempts >= global_max)
            if will_lock:
                body = _assert_err(r, 429, "approver_locked")
                assert isinstance(body.get("detail"), dict)
                assert int(body["detail"]["retry_after"]) >= 1
                locked_sources.append(src_ip)
                break
            _assert_err(r, 401, "approver_auth_required")
        if total_attempts >= global_max:
            break

    assert len(locked_sources) >= 2

    c_fresh = _client_for(app, "198.18.250.99", 54099)
    fresh_signin = c_fresh.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    fresh_lock_body = _assert_err(fresh_signin, 429, "approver_locked")
    assert int(fresh_lock_body["detail"]["retry_after"]) >= 1

    fresh_decide = c_fresh.post(
        f"/v1/approvals/{o_aurora_anna['order_id']}/decision",
        headers={"X-Approver-Pin": anna_pin},
        json={"decision": "approve", "quote_hash": q_aurora_anna["quote_hash"], "approver": ANNA},
    )
    _assert_err(fresh_decide, 429, "approver_locked")

    c_locked_ip = _client_for(app, locked_sources[0], 53999)
    assert (
        c_locked_ip.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": mikko_pin},
            json={"approver": MIKKO},
        ).status_code
        == 200
    )
    assert (
        c_fresh.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": liisa_pin},
            json={"approver": LIISA},
        ).status_code
        == 200
    )

    r_mikko_ok = c_locked_ip.post(
        f"/v1/approvals/{o_aurora_mikko['order_id']}/decision",
        headers={"X-Approver-Pin": mikko_pin},
        json={"decision": "approve", "quote_hash": q_aurora_mikko["quote_hash"], "approver": MIKKO},
    )
    assert r_mikko_ok.status_code == 200 and r_mikko_ok.json()["status"] == "placed"
    assert r_mikko_ok.json()["approved_by"] == MIKKO

    r_liisa_ok = c_fresh.post(
        f"/v1/approvals/{o_school_liisa['order_id']}/decision",
        headers={"X-Approver-Pin": liisa_pin},
        json={"decision": "approve", "quote_hash": q_school_liisa["quote_hash"], "approver": LIISA},
    )
    assert r_liisa_ok.status_code == 200 and r_liisa_ok.json()["status"] == "placed"
    assert r_liisa_ok.json()["approved_by"] == LIISA

    r_anna_sess_ok = c_locked_ip.post(
        f"/v1/approvals/{o_aurora_anna['order_id']}/decision",
        headers={"X-Approver-Session": anna_session},
        json={"decision": "approve", "quote_hash": q_aurora_anna["quote_hash"]},
    )
    assert r_anna_sess_ok.status_code == 200 and r_anna_sess_ok.json()["status"] == "placed"
    assert r_anna_sess_ok.json()["approved_by"] == ANNA

    events, _ = _assert_chain_ok(c_fresh)
    lock_events = [
        ev
        for ev in events
        if ev["action"] == "approval.refused"
        and ev["subject_id"] == o_aurora_anna["order_id"]
        and "locked for" in str(ev.get("data", {}).get("reason", ""))
    ]
    assert len(lock_events) == len(locked_sources)


def test_one_guess_per_rotating_source_triggers_per_approver_backstop_with_signed_refusal() -> None:
    app = _make_app()
    c_base = _client_for(app, "198.18.0.1", 55001)
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin, liisa_pin = pins[ANNA], pins[MIKKO], pins[LIISA]

    anna_session = c_base.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]
    q_aurora, o_aurora = _create_pending_aurora(c_base)

    global_max = human_auth.GLOBAL_MAX
    for idx in range(global_max - 1):
        c_ip = _client_for(app, f"198.19.{idx // 200}.{(idx % 200) + 1}", 56000 + idx)
        r = c_ip.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": f"spray-pin-{idx}"},
            json={"approver": ANNA},
        )
        _assert_err(r, 401, "approver_auth_required")

    c_trigger = _client_for(app, "198.19.99.99", 56999)
    r_trigger = c_trigger.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "spray-pin-trigger"},
        json={"approver": ANNA},
    )
    trigger_body = _assert_err(r_trigger, 429, "approver_locked")
    assert int(trigger_body["detail"]["retry_after"]) >= 1

    c_unseen = _client_for(app, "198.19.200.200", 57200)
    _assert_err(
        c_unseen.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": anna_pin},
            json={"approver": ANNA},
        ),
        429,
        "approver_locked",
    )
    assert (
        c_unseen.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": mikko_pin},
            json={"approver": MIKKO},
        ).status_code
        == 200
    )
    assert (
        c_unseen.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": liisa_pin},
            json={"approver": LIISA},
        ).status_code
        == 200
    )

    r_sess_approve = c_unseen.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": anna_session},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_sess_approve.status_code == 200 and r_sess_approve.json()["status"] == "placed"

    events, _ = _assert_chain_ok(c_unseen)
    assert any(
        ev["action"] == "approval.refused"
        and ev["subject_id"] == "auth:approver-session"
        and "locked for" in str(ev.get("data", {}).get("reason", ""))
        for ev in events
    )


def test_display_name_and_case_folded_aliases_share_per_approver_lockout_bucket() -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.19", 51019)
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin, liisa_pin = pins[ANNA], pins[MIKKO], pins[LIISA]

    anna_session = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": "Anna Virtanen"},
    ).json()["session"]

    aliases = [
        "Anna Virtanen",
        "  anna virtanen  ",
        "ANNA@HOTEL-AURORA.EXAMPLE",
        "Anna@Hotel-Aurora.Example",
        ANNA,
    ]
    max_fail = human_auth.MAX_FAILURES
    for idx in range(max_fail - 1):
        alias = aliases[idx % len(aliases)]
        r_wrong = c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": f"alias-wrong-{idx}"},
            json={"approver": alias},
        )
        _assert_err(r_wrong, 401, "approver_auth_required")

    r_locked = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "alias-wrong-final"},
        json={"approver": "Anna Virtanen"},
    )
    lock_err = _assert_err(r_locked, 429, "approver_locked")
    assert int(lock_err["detail"]["retry_after"]) >= 1

    _assert_err(
        c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": anna_pin},
            json={"approver": ANNA},
        ),
        429,
        "approver_locked",
    )
    assert (
        c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": mikko_pin},
            json={"approver": "Mikko Laine"},
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": liisa_pin},
            json={"approver": "Liisa Korhonen"},
        ).status_code
        == 200
    )

    q_aurora, o_aurora = _create_pending_aurora(c)
    r_sess = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": anna_session},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_sess.status_code == 200 and r_sess.json()["status"] == "placed"
    _assert_chain_ok(c)


def test_session_bound_spoofing_returns_403_approver_mismatch_and_records_refusal_event() -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.21", 51021)
    pins = human_auth.approver_pins()

    anna_session = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[ANNA]},
        json={"approver": ANNA},
    ).json()["session"]
    sess_hdr = {"X-Approver-Session": anna_session}

    q_aurora, o_aurora = _create_pending_aurora(c)
    order_id = o_aurora["order_id"]

    for spoofed in (MIKKO, "Mikko Laine", LIISA, "mallory@evil.example"):
        r_spoof = c.post(
            f"/v1/approvals/{order_id}/decision",
            headers=sess_hdr,
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": spoofed},
        )
        err = _assert_err(r_spoof, 403, "approver_mismatch")
        assert ANNA in err["message"]

    assert c.get(f"/v1/orders/{order_id}").json()["status"] == "pending_approval"

    trail = c.get("/v1/audit", params={"subject_id": order_id}).json()
    spoof_refusals = [
        ev
        for ev in trail
        if ev["action"] == "approval.refused"
        and ev["actor"] == f"human:{ANNA}"
        and "tried to decide as" in str(ev.get("data", {}).get("reason", ""))
    ]
    assert len(spoof_refusals) == 4
    assert any(MIKKO in ev["summary"] for ev in spoof_refusals)

    r_self_alias = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers=sess_hdr,
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": "Anna Virtanen"},
    )
    assert r_self_alias.status_code == 200, r_self_alias.text
    assert r_self_alias.json()["status"] == "placed"
    assert r_self_alias.json()["approved_by"] == ANNA

    _assert_chain_ok(c)


def test_cross_tenant_liisa_session_on_hotel_aurora_order_returns_403_and_refusal_event() -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.22", 51022)
    pins = human_auth.approver_pins()

    liisa_session = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[LIISA]},
        json={"approver": LIISA},
    ).json()["session"]

    q_aurora, o_aurora = _create_pending_aurora(c)
    order_id = o_aurora["order_id"]

    r_liisa_approve = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": liisa_session},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    err_approve = _assert_err(r_liisa_approve, 403, "approval_refused")
    assert "not in hotel-aurora's approver directory" in err_approve["message"]

    r_liisa_reject = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": liisa_session},
        json={"decision": "reject", "quote_hash": q_aurora["quote_hash"]},
    )
    err_reject = _assert_err(r_liisa_reject, 403, "approval_refused")
    assert "not in hotel-aurora's approver directory" in err_reject["message"]

    r_liisa_pin = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Pin": pins[LIISA]},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": LIISA},
    )
    _assert_err(r_liisa_pin, 403, "approval_refused")

    assert c.get(f"/v1/orders/{order_id}").json()["status"] == "pending_approval"

    trail = c.get("/v1/audit", params={"subject_id": order_id}).json()
    liisa_refusals = [
        ev
        for ev in trail
        if ev["action"] == "approval.refused"
        and ev["actor"] == f"human:{LIISA}"
        and "not in hotel-aurora's approver directory" in str(ev.get("data", {}).get("reason", ""))
    ]
    assert len(liisa_refusals) == 3

    _assert_chain_ok(c)


def test_role_mismatch_vs_override_role_general_manager_and_non_override_role_isolation() -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.23", 51023)
    pins = human_auth.approver_pins()

    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[ANNA]},
        json={"approver": ANNA},
    ).json()["session"]
    mikko_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[MIKKO]},
        json={"approver": MIKKO},
    ).json()["session"]
    olli_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[OLLI]},
        json={"approver": OLLI},
    ).json()["session"]
    helena_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[HELENA]},
        json={"approver": HELENA},
    ).json()["session"]

    q_aurora_1, o_aurora_1 = _create_pending_aurora(c)
    q_aurora_2, o_aurora_2 = _create_pending_aurora(c)

    r_anna_claims_gm = c.post(
        f"/v1/approvals/{o_aurora_1['order_id']}/decision",
        headers={"X-Approver-Session": anna_sess},
        json={
            "decision": "approve",
            "quote_hash": q_aurora_1["quote_hash"],
            "approver_role": "general_manager",
        },
    )
    err_anna_role = _assert_err(r_anna_claims_gm, 403, "approval_refused")
    assert "registered as procurement manager, not general manager" in err_anna_role["message"]

    r_mikko_claims_pm = c.post(
        f"/v1/approvals/{o_aurora_1['order_id']}/decision",
        headers={"X-Approver-Session": mikko_sess},
        json={
            "decision": "approve",
            "quote_hash": q_aurora_1["quote_hash"],
            "approver_role": "procurement_manager",
        },
    )
    err_mikko_role = _assert_err(r_mikko_claims_pm, 403, "approval_refused")
    assert "registered as general manager, not procurement manager" in err_mikko_role["message"]

    assert c.get(f"/v1/orders/{o_aurora_1['order_id']}").json()["status"] == "pending_approval"

    r_mikko_override_implicit = c.post(
        f"/v1/approvals/{o_aurora_1['order_id']}/decision",
        headers={"X-Approver-Session": mikko_sess},
        json={"decision": "approve", "quote_hash": q_aurora_1["quote_hash"]},
    )
    assert r_mikko_override_implicit.status_code == 200, r_mikko_override_implicit.text
    placed_1 = r_mikko_override_implicit.json()
    assert placed_1["status"] == "placed"
    assert placed_1["approved_by"] == MIKKO
    assert placed_1["approver_role"] == "general_manager"

    r_mikko_override_explicit = c.post(
        f"/v1/approvals/{o_aurora_2['order_id']}/decision",
        headers={"X-Approver-Session": mikko_sess},
        json={
            "decision": "approve",
            "quote_hash": q_aurora_2["quote_hash"],
            "approver_role": "general_manager",
        },
    )
    assert r_mikko_override_explicit.status_code == 200, r_mikko_override_explicit.text
    placed_2 = r_mikko_override_explicit.json()
    assert placed_2["status"] == "placed"
    assert placed_2["approved_by"] == MIKKO
    assert placed_2["approver_role"] == "general_manager"

    q_hard_reject = c.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": 6000, "tier": "best"}],
        },
    ).json()
    o_hard_reject = c.post(
        "/v1/orders",
        headers=H_AURORA,
        json={"quote_id": q_hard_reject["quote_id"], "quote_hash": q_hard_reject["quote_hash"]},
    ).json()
    assert o_hard_reject["status"] == "rejected"
    _assert_err(
        c.post(
            f"/v1/approvals/{o_hard_reject['order_id']}/decision",
            headers={"X-Approver-Session": mikko_sess},
            json={"decision": "approve", "quote_hash": q_hard_reject["quote_hash"]},
        ),
        409,
        "not_pending",
    )

    q_hosp, o_hosp = _create_pending_hospital_dietitian(c)
    hosp_id = o_hosp["order_id"]

    r_olli_no_claim = c.post(
        f"/v1/approvals/{hosp_id}/decision",
        headers={"X-Approver-Session": olli_sess},
        json={"decision": "approve", "quote_hash": q_hosp["quote_hash"]},
    )
    err_olli = _assert_err(r_olli_no_claim, 403, "approval_refused")
    assert "procurement manager, who cannot decide this order" in err_olli["message"]

    r_olli_spoof_dietitian = c.post(
        f"/v1/approvals/{hosp_id}/decision",
        headers={"X-Approver-Session": olli_sess},
        json={
            "decision": "approve",
            "quote_hash": q_hosp["quote_hash"],
            "approver_role": "dietitian",
        },
    )
    err_olli_spoof = _assert_err(r_olli_spoof_dietitian, 403, "approval_refused")
    assert "registered as procurement manager, not dietitian" in err_olli_spoof["message"]
    assert c.get(f"/v1/orders/{hosp_id}").json()["status"] == "pending_approval"

    r_helena_override = c.post(
        f"/v1/approvals/{hosp_id}/decision",
        headers={"X-Approver-Session": helena_sess},
        json={"decision": "approve", "quote_hash": q_hosp["quote_hash"]},
    )
    assert r_helena_override.status_code == 200, r_helena_override.text
    assert r_helena_override.json()["status"] == "placed"
    assert r_helena_override.json()["approved_by"] == HELENA
    assert r_helena_override.json()["approver_role"] == "head_of_nutrition"

    aurora_trail = c.get("/v1/audit", params={"subject_id": o_aurora_1["order_id"]}).json()
    assert sum(1 for ev in aurora_trail if ev["action"] == "approval.refused") == 2
    assert any(
        ev["action"] == "approval.verified" and ev["actor"] == f"human:{MIKKO}"
        for ev in aurora_trail
    )

    hosp_trail = c.get("/v1/audit", params={"subject_id": hosp_id}).json()
    assert sum(
        1
        for ev in hosp_trail
        if ev["action"] == "approval.refused" and ev["actor"] == f"human:{OLLI}"
    ) == 2

    _assert_chain_ok(c)


def test_demo_tamper_and_untamper_reject_agents_and_anonymous_callers() -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.31", 51031)
    pins = human_auth.approver_pins()
    anna_pin = pins[ANNA]
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    _create_pending_aurora(c)
    events_init, _ = _assert_chain_ok(c)
    target_seq = events_init[0]["seq"]

    agent_header_variants = [
        {"X-Agent-Id": "buyer-1"},
        {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
        {"X-Agent-Id": "hotel-aurora/buyer-1", "X-Approver-Pin": anna_pin},
        {"X-Agent-Id": "buyer-1", "X-Approver-Session": anna_sess},
    ]
    for hdr in agent_header_variants:
        _assert_err(c.post(f"/v1/demo/tamper/{target_seq}", headers=hdr), 403, "agent_cannot_approve")
        _assert_err(c.post(f"/v1/demo/untamper/{target_seq}", headers=hdr), 403, "agent_cannot_approve")

    anon_and_forged_variants = [
        {},
        {"X-Approver-Pin": "wrong-tamper-passphrase"},
        {"X-Approver-Session": anna_sess + "corrupted"},
    ]
    for hdr in anon_and_forged_variants:
        _assert_err(c.post(f"/v1/demo/tamper/{target_seq}", headers=hdr), 401, "approver_auth_required")
        _assert_err(c.post(f"/v1/demo/untamper/{target_seq}", headers=hdr), 401, "approver_auth_required")

    events_after, _ = _assert_chain_ok(c)
    audit_subject_refusals = [
        ev
        for ev in events_after
        if ev["action"] == "approval.refused" and ev["subject_id"] == f"audit:{target_seq}"
    ]
    assert len(audit_subject_refusals) == 2 * (len(agent_header_variants) + len(anon_and_forged_variants))


def test_demo_tamper_and_untamper_return_404_when_aimo_env_production_and_flip_chain_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.32", 51032)
    pins = human_auth.approver_pins()
    anna_pin = pins[ANNA]
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]
    sess_hdr = {"X-Approver-Session": anna_sess}

    q_aurora, o_aurora = _create_pending_aurora(c)
    order_id = o_aurora["order_id"]

    _assert_err(
        c.post(
            f"/v1/approvals/{order_id}/decision",
            headers=H_AURORA,
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": ANNA},
        ),
        403,
        "agent_cannot_approve",
    )

    r_approve = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers=sess_hdr,
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "note": "verified stock"},
    )
    assert r_approve.status_code == 200 and r_approve.json()["status"] == "placed"

    events_before, jwks = _assert_chain_ok(c)
    q_ver = conformance_verify.verify_quote(q_aurora, jwks)
    assert q_ver["ok"] is True and q_ver["hash_matches"] is True and q_ver["signature_valid"] is True
    verified_ev = next(ev for ev in events_before if ev["action"] == "approval.verified")
    target_seq = int(verified_ev["seq"])

    _assert_err(c.post("/v1/demo/tamper/999999", headers=sess_hdr), 404, "audit_event_not_found")
    _assert_err(c.post("/v1/demo/untamper/0", headers=sess_hdr), 404, "audit_event_not_found")

    monkeypatch.setenv("AIMO_ENV", "production")
    _assert_err(c.post(f"/v1/demo/tamper/{target_seq}", headers=sess_hdr), 404, "not_found")
    _assert_err(c.post(f"/v1/demo/untamper/{target_seq}", headers=sess_hdr), 404, "not_found")
    _assert_err(
        c.post(f"/v1/demo/tamper/{target_seq}", headers={"X-Approver-Pin": anna_pin}),
        404,
        "not_found",
    )
    _assert_err(
        c.post(f"/v1/demo/untamper/{target_seq}", headers={"X-Approver-Pin": anna_pin}),
        404,
        "not_found",
    )
    _assert_chain_ok(c)

    monkeypatch.delenv("AIMO_ENV", raising=False)
    r_tamper = c.post(
        f"/v1/demo/tamper/{target_seq}",
        headers=sess_hdr,
        params={"summary": "FORGED: mallory altered the signed approval record"},
    )
    assert r_tamper.status_code == 200, r_tamper.text
    tamper_json = r_tamper.json()
    assert tamper_json["tampered"] is True
    assert tamper_json["seq"] == target_seq
    assert tamper_json["verify"]["ok"] is False
    assert tamper_json["verify"]["broken_at"] == target_seq

    gw_broken = c.get("/v1/audit/verify").json()
    assert gw_broken["ok"] is False, gw_broken
    assert gw_broken["broken_at"] == target_seq, gw_broken
    assert "hash mismatch" in str(gw_broken.get("reason", ""))

    events_tampered = c.get("/v1/audit").json()
    indep_broken = conformance_verify.verify_events(events_tampered, jwks, contiguous=True)
    assert indep_broken["ok"] is False, indep_broken
    assert indep_broken["checked"] == len(events_before)
    assert any(f"seq {target_seq}: hash does not recompute" in p for p in indep_broken["problems"]), indep_broken

    report_broken = c.get(f"/v1/audit/report/{order_id}").text
    assert "CHAIN BROKEN" in report_broken

    monkeypatch.setenv("AIMO_ENV", "production")
    _assert_err(c.post(f"/v1/demo/untamper/{target_seq}", headers=sess_hdr), 404, "not_found")
    assert c.get("/v1/audit/verify").json()["ok"] is False

    monkeypatch.delenv("AIMO_ENV", raising=False)
    r_untamper = c.post(f"/v1/demo/untamper/{target_seq}", headers=sess_hdr)
    assert r_untamper.status_code == 200, r_untamper.text
    untamper_json = r_untamper.json()
    assert untamper_json["seq"] == target_seq
    assert untamper_json["restored"] is True
    assert untamper_json["verify"]["ok"] is True
    assert untamper_json["verify"]["broken_at"] is None

    events_restored, _ = _assert_chain_ok(c)
    assert events_restored == events_before

    report_restored = c.get(f"/v1/audit/report/{order_id}").text
    assert "Chain verified:" in report_restored

    _create_pending_aurora(c, qty=750)
    events_extended, _ = _assert_chain_ok(c)
    assert len(events_extended) > len(events_restored)
    assert events_extended[len(events_restored)]["prev_hash"] == events_restored[-1]["hash"]


def test_pin_rotation_invalidates_only_rotated_approver_session_and_wildcard_lock_stays_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app()
    c = _client_for(app, "198.51.100.45", 51045)
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin = pins[ANNA], pins[MIKKO]

    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]
    mikko_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": mikko_pin},
        json={"approver": MIKKO},
    ).json()["session"]

    q_aurora, o_aurora = _create_pending_aurora(c)
    order_id = o_aurora["order_id"]

    max_fail = human_auth.MAX_FAILURES
    for idx in range(max_fail - 1):
        _assert_err(
            c.post("/v1/demo/tamper/1", headers={"X-Approver-Pin": f"wildcard-wrong-{idx}"}),
            401,
            "approver_auth_required",
        )
    wild_lock = _assert_err(
        c.post("/v1/demo/tamper/1", headers={"X-Approver-Pin": "wildcard-wrong-final"}),
        429,
        "approver_locked",
    )
    assert int(wild_lock["detail"]["retry_after"]) >= 1

    r_named_still_ok = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    assert r_named_still_ok.status_code == 200, r_named_still_ok.text

    rotated_anna_pin = "RotatedAnnaPassphrase2026!"
    monkeypatch.setenv("AIMO_APPROVER_PINS", json.dumps({ANNA: rotated_anna_pin}))

    _assert_err(
        c.post(
            f"/v1/approvals/{order_id}/decision",
            headers={"X-Approver-Session": anna_sess},
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": ANNA},
        ),
        401,
        "approver_auth_required",
    )
    c_clean = _client_for(app, "198.51.100.46", 51046)
    _assert_err(
        c_clean.post(
            f"/v1/approvals/{order_id}/decision",
            headers={"X-Approver-Session": anna_sess},
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
        ),
        401,
        "approver_auth_required",
    )

    r_mikko_still_valid = c.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": mikko_sess},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_mikko_still_valid.status_code == 200, r_mikko_still_valid.text
    assert r_mikko_still_valid.json()["status"] == "placed"
    assert r_mikko_still_valid.json()["approved_by"] == MIKKO

    r_anna_reauth = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": rotated_anna_pin},
        json={"approver": ANNA},
    )
    assert r_anna_reauth.status_code == 200
    assert human_auth.session_approver(r_anna_reauth.json()["session"]) == ANNA

    _assert_chain_ok(c)

