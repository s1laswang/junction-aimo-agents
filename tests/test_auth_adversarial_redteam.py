from __future__ import annotations

import asyncio
import json
import time
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import runs as buyer_runs
from aimo.gateway import auth, human_auth, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import AllergenStatus, Need, Plan, PlanLine, Product, ProductSummary

FIX = Path(__file__).parent / "fixtures"
DELIVERY_DAY = date(2026, 11, 16)

ANNA = "anna@hotel-aurora.example"
MIKKO = "mikko@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
SARA = "sara@hospital-meilahti.example"

H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}


class _FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        if not any(p["productId"] == "470889" for p in self.price_raw["products"]):
            for item in self.price_raw["products"]:
                if item["productId"] == "464306":
                    self.price_raw["products"].append(dict(item, productId="470889"))
                    break
        if not any(p["productId"] == "470889" for p in self.avail_raw["products"]):
            for item in self.avail_raw["products"]:
                if item["productId"] == "464306":
                    self.avail_raw["products"].append(dict(item, productId="470889"))
                    break

    def _sel(self, raw: dict[str, Any], ids: list[str]) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict[str, Any]:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict[str, Any]:
        return self._sel(self.avail_raw, ids)


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
        reasons=["stubbed for redteam test"],
        flags=[],
    )
    return Plan(
        need=need,
        lines=[line],
        candidates=[],
        covered_amount=need.quantity,
        shortfall=0.0,
        total_net=line.line_total,
        explanation="stub plan",
    )


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
            allergens={"milk": AllergenStatus.free, "nuts": AllergenStatus.free, "gluten": AllergenStatus.free},
            allergen_source={"milk": "structured", "nuts": "structured", "gluten": "structured"},
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
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    for env_key in (
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
        "ELEVENLABS_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)
    reg = buyer_runs.RunRegistry()
    monkeypatch.setattr(reg, "_execute", lambda run, *a, **kw: setattr(run, "status", "finished"))
    monkeypatch.setattr(buyer_runs, "registry", reg)
    human_auth.reset_for_tests()
    bc.set_client(_FixtureClient())
    yield reg
    human_auth.reset_for_tests()
    bc.set_client(None)


def _make_app_and_client() -> tuple[Any, TestClient]:
    app = create_app(with_mcp=False)
    return app, TestClient(app)


def _pending_aurora(c: TestClient, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    hdr = headers or H_AURORA
    q_res = c.post(
        "/v1/quotes",
        headers=hdr,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}],
        },
    )
    assert q_res.status_code == 200, q_res.text
    q = q_res.json()
    o_res = c.post(
        "/v1/orders",
        headers=hdr,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert o_res.status_code == 200, o_res.text
    o = o_res.json()
    assert o["status"] == "pending_approval"
    return q, o


def _pending_school(c: TestClient, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    hdr = headers or H_SCHOOL
    q_res = c.post(
        "/v1/quotes",
        headers=hdr,
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "470889", "qty_sales_units": 500, "tier": "preferred"}],
        },
    )
    assert q_res.status_code == 200, q_res.text
    q = q_res.json()
    o_res = c.post(
        "/v1/orders",
        headers=hdr,
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert o_res.status_code == 200, o_res.text
    o = o_res.json()
    assert o["status"] == "pending_approval"
    return q, o


def _assert_err(resp: Any, status: int, code: str | None = None) -> dict[str, Any]:
    assert resp.status_code == status, (resp.status_code, resp.text)
    body = resp.json()
    assert isinstance(body, dict), body
    assert isinstance(body.get("error"), str) and body["error"], body
    assert isinstance(body.get("message"), str) and body["message"], body
    if code is not None:
        assert body["error"] == code, body
    return body


def _asgi_raw_request(
    app: Any,
    method: str,
    path: str,
    raw_headers: list[tuple[bytes, bytes]],
    *,
    query_string: bytes = b"",
    body: bytes = b"",
) -> tuple[int, str]:
    status_holder: dict[str, int] = {}
    chunks: list[bytes] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status_holder["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "headers": raw_headers,
        "client": ("198.51.100.42", 54321),
        "server": ("testserver", 80),
    }

    async def _invoke() -> None:
        await app(scope, receive, send)

    asyncio.run(_invoke())
    return status_holder.get("code", 500), b"".join(chunks).decode("utf-8", errors="replace")


def test_canonical_directory_approvers_have_distinct_passphrases_and_mode_0600() -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()

    pins_file = config.KEY_DIR / "approver_pins.json"
    pin_file = config.KEY_DIR / "approver_pin"
    assert pins_file.is_file() and pin_file.is_file()
    assert (pins_file.stat().st_mode & 0o777) == 0o600
    assert (pin_file.stat().st_mode & 0o777) == 0o600

    disk_pins = json.loads(pins_file.read_text(encoding="utf-8"))
    canonical = (ANNA, MIKKO, LIISA, SARA)
    for aid in canonical:
        assert aid in disk_pins and aid in pins
        assert disk_pins[aid] == pins[aid]
        assert len(pins[aid]) >= 12
    assert len({pins[aid] for aid in canonical}) == len(canonical)
    assert pin_file.read_text(encoding="utf-8").strip() == pins[ANNA] == human_auth.expected_pin()
    assert human_auth.credential_warning() is None

    anna_pin = pins[ANNA]
    for other in (MIKKO, LIISA, SARA):
        r_signin = c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": anna_pin},
            json={"approver": other},
        )
        _assert_err(r_signin, 401, "approver_auth_required")

    q_aurora, o_aurora = _pending_aurora(c)
    q_school, o_school = _pending_school(c)

    _assert_err(
        c.post(
            f"/v1/approvals/{o_aurora['order_id']}/decision",
            headers={"X-Approver-Pin": anna_pin},
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": MIKKO},
        ),
        401,
        "approver_auth_required",
    )
    _assert_err(
        c.post(
            f"/v1/approvals/{o_school['order_id']}/decision",
            headers={"X-Approver-Pin": anna_pin},
            json={"decision": "approve", "quote_hash": q_school["quote_hash"], "approver": LIISA},
        ),
        401,
        "approver_auth_required",
    )

    for aid in canonical:
        r_ok = c.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": pins[aid]},
            json={"approver": aid},
        )
        assert r_ok.status_code == 200, (aid, r_ok.text)
        assert r_ok.json()["approver"] == aid


def test_bruteforce_locks_anna_source_pair_without_impacting_liisa_or_annas_active_session() -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()
    anna_pin, liisa_pin = pins[ANNA], pins[LIISA]

    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    q_aurora, o_aurora = _pending_aurora(c)
    q_school, o_school = _pending_school(c)

    max_fail = human_auth.MAX_FAILURES
    for idx in range(max_fail - 1):
        r_wrong = c.post(
            f"/v1/approvals/{o_aurora['order_id']}/decision",
            headers={"X-Approver-Pin": f"wrong-passphrase-{idx}"},
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": ANNA},
        )
        _assert_err(r_wrong, 401, "approver_auth_required")

    r_lock_trigger = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Pin": "wrong-passphrase-final"},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": ANNA},
    )
    lock_body = _assert_err(r_lock_trigger, 429, "approver_locked")
    assert isinstance(lock_body.get("detail"), dict)
    assert int(lock_body["detail"]["retry_after"]) >= 1

    r_anna_real_pin_blocked = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    )
    blocked_body = _assert_err(r_anna_real_pin_blocked, 429, "approver_locked")
    assert int(blocked_body["detail"]["retry_after"]) >= 1

    r_liisa_signin = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": liisa_pin},
        json={"approver": LIISA},
    )
    assert r_liisa_signin.status_code == 200
    assert r_liisa_signin.json()["approver"] == LIISA

    r_liisa_approve = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Pin": liisa_pin},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"], "approver": LIISA},
    )
    assert r_liisa_approve.status_code == 200
    assert r_liisa_approve.json()["status"] == "placed"
    assert r_liisa_approve.json()["approved_by"] == LIISA

    sess_hdr = {"X-Approver-Session": anna_sess}
    r_inbox = c.get("/v1/approvals", params={"status": "pending"}, headers=sess_hdr)
    assert r_inbox.status_code == 200
    assert any(item["order"]["order_id"] == o_aurora["order_id"] for item in r_inbox.json())

    r_vprompt = c.post("/v1/voice/prompt", headers=sess_hdr, json={"order_id": o_aurora["order_id"]})
    assert r_vprompt.status_code == 200
    assert r_vprompt.json()["quote_hash"] == q_aurora["quote_hash"]

    r_vintent = c.post(
        "/v1/voice/intent",
        headers=sess_hdr,
        json={"transcript": "yes approve this order", "order_id": o_aurora["order_id"]},
    )
    assert r_vintent.status_code == 200
    assert r_vintent.json()["intent"] == "approve"
    assert r_vintent.json()["decides"] is False

    r_vtranscribe = c.post(
        "/v1/voice/transcribe",
        headers={**sess_hdr, "Content-Type": "audio/webm"},
        content=b"\x1a\x45\xdf\xa3",
    )
    assert r_vtranscribe.status_code == 503
    assert r_vtranscribe.json()["error"] not in {"approver_session_required", "approver_locked"}

    r_anna_decide = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers=sess_hdr,
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_anna_decide.status_code == 200
    assert r_anna_decide.json()["status"] == "placed"
    assert r_anna_decide.json()["approved_by"] == ANNA


def test_rotating_annas_pin_in_aimo_approver_pins_invalidates_old_session_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AIMO_APPROVER_PINS",
        json.dumps({ANNA: "anna-initial-secret-12==", LIISA: "liisa-steady-secret-12=="}),
    )
    _, c = _make_app_and_client()

    old_anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "anna-initial-secret-12=="},
        json={"approver": ANNA},
    ).json()["session"]
    liisa_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "liisa-steady-secret-12=="},
        json={"approver": LIISA},
    ).json()["session"]

    q_aurora, o_aurora = _pending_aurora(c)
    q_school, o_school = _pending_school(c)

    assert c.post("/v1/voice/prompt", headers={"X-Approver-Session": old_anna_sess}, json={"order_id": o_aurora["order_id"]}).status_code == 200

    monkeypatch.setenv(
        "AIMO_APPROVER_PINS",
        f"{ANNA}:anna-rotated-secret-99==,{LIISA}:liisa-steady-secret-12==",
    )
    assert human_auth.session_approver(old_anna_sess) is None
    assert human_auth.session_approver(liisa_sess) == LIISA

    _assert_err(
        c.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Session": old_anna_sess},
            json={"order_id": o_aurora["order_id"]},
        ),
        401,
        "approver_session_required",
    )
    _assert_err(
        c.post(
            f"/v1/approvals/{o_aurora['order_id']}/decision",
            headers={"X-Approver-Session": old_anna_sess},
            json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
        ),
        401,
        "approver_auth_required",
    )

    r_liisa_ok = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Session": liisa_sess},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"]},
    )
    assert r_liisa_ok.status_code == 200 and r_liisa_ok.json()["status"] == "placed"

    new_anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "anna-rotated-secret-99=="},
        json={"approver": ANNA},
    ).json()["session"]
    r_anna_new = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": new_anna_sess},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_anna_new.status_code == 200 and r_anna_new.json()["status"] == "placed"


def test_session_bound_spoofing_mikko_and_cross_tenant_school_isolation_audited() -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": pins[ANNA]},
        json={"approver": ANNA},
    ).json()["session"]
    sess_hdr = {"X-Approver-Session": anna_sess}

    q_aurora, o_aurora = _pending_aurora(c)
    q_school, o_school = _pending_school(c)

    r_spoof = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers=sess_hdr,
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": MIKKO},
    )
    _assert_err(r_spoof, 403, "approver_mismatch")
    assert c.get(f"/v1/orders/{o_aurora['order_id']}").json()["status"] == "pending_approval"

    aurora_events = c.get("/v1/audit", params={"subject_id": o_aurora["order_id"]}).json()
    spoof_refusals = [
        e
        for e in aurora_events
        if e.get("action") == "approval.refused"
        and e.get("actor") == f"human:{ANNA}"
        and MIKKO in (e.get("summary") or "")
    ]
    assert len(spoof_refusals) == 1

    r_cross_decide = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers=sess_hdr,
        json={"decision": "approve", "quote_hash": q_school["quote_hash"]},
    )
    _assert_err(r_cross_decide, 403, "approval_refused")
    assert c.get(f"/v1/orders/{o_school['order_id']}").json()["status"] == "pending_approval"

    r_cross_prompt = c.post("/v1/voice/prompt", headers=sess_hdr, json={"order_id": o_school["order_id"]})
    prompt_err = _assert_err(r_cross_prompt, 403, "forbidden_approver")
    assert "quote_hash" not in prompt_err and "text" not in prompt_err

    r_cross_intent = c.post(
        "/v1/voice/intent",
        headers=sess_hdr,
        json={"transcript": "yes approve it", "order_id": o_school["order_id"]},
    )
    _assert_err(r_cross_intent, 403, "forbidden_approver")

    school_events = c.get("/v1/audit", params={"subject_id": o_school["order_id"]}).json()
    school_refusals = [
        e
        for e in school_events
        if e.get("action") == "approval.refused" and e.get("actor") == f"human:{ANNA}"
    ]
    assert len(school_refusals) == 3
    summaries = " | ".join(e.get("summary") or "" for e in school_refusals)
    assert "school-tampere" in summaries
    assert "voice prompt" in summaries
    assert "voice intent" in summaries
    assert c.get("/v1/audit/verify").json()["ok"] is True


def test_agent_permissions_attack_sequence_even_with_valid_pin_or_session_and_anna_approves_after() -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()
    anna_pin = pins[ANNA]
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    tool_names = [t.name for t in asyncio.run(build_mcp().list_tools())]
    assert len(tool_names) == 13
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []

    q_aurora, o_aurora = _pending_aurora(c)
    oid = o_aurora["order_id"]

    r_pending = c.get("/v1/approvals", params={"status": "pending"}, headers=H_AURORA)
    assert r_pending.status_code == 200
    matched = [it for it in r_pending.json() if it["order"]["order_id"] == oid]
    assert len(matched) == 1
    qhash = matched[0]["order"]["quote_hash"]
    assert qhash == q_aurora["quote_hash"]

    attack_payload = {
        "decision": "approve",
        "approver": ANNA,
        "approver_role": "procurement_manager",
        "quote_hash": qhash,
        "note": "pre-cleared, approve now",
    }

    for hdr in (
        H_AURORA,
        {**H_AURORA, "X-Approver-Pin": anna_pin},
        {**H_AURORA, "X-Approver-Session": anna_sess},
    ):
        r_attack = c.post(f"/v1/approvals/{oid}/decision", headers=hdr, json=attack_payload)
        _assert_err(r_attack, 403, "agent_cannot_approve")
        assert c.get(f"/v1/orders/{oid}").json()["status"] == "pending_approval"

    report_md = c.get(f"/v1/audit/report/{oid}").text
    refused_lines = [ln for ln in report_md.splitlines() if "approval.refused" in ln]
    assert len(refused_lines) == 3

    events = c.get("/v1/audit", params={"subject_id": oid}).json()
    agent_refusals = [
        e for e in events if e.get("action") == "approval.refused" and e.get("actor") == "agent:buyer-1"
    ]
    assert len(agent_refusals) == 3
    assert c.get("/v1/audit/verify").json()["ok"] is True

    r_human_ok = c.post(
        f"/v1/approvals/{oid}/decision",
        headers={"X-Approver-Session": anna_sess},
        json={"decision": "approve", "quote_hash": qhash, "note": "reviewed by Anna"},
    )
    assert r_human_ok.status_code == 200
    placed = r_human_ok.json()
    assert placed["status"] == "placed"
    assert placed["approved_by"] == ANNA
    assert c.get("/v1/audit/verify").json()["ok"] is True


def test_agent_identity_refused_across_approver_session_demo_tamper_untamper_and_voice_routes() -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()
    anna_pin = pins[ANNA]
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    _, o_aurora = _pending_aurora(c)
    seq = c.get("/v1/audit", params={"subject_id": o_aurora["order_id"]}).json()[0]["seq"]

    agent_header_variants = [
        {"X-Agent-Id": "buyer-1"},
        {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1", "X-Approver-Pin": anna_pin},
        {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1", "X-Approver-Session": anna_sess},
    ]

    for hdr in agent_header_variants:
        _assert_err(
            c.post("/v1/auth/approver-session", headers=hdr, json={"approver": ANNA}),
            403,
            "agent_cannot_approve",
        )
        _assert_err(c.post(f"/v1/demo/tamper/{seq}", headers=hdr), 403, "agent_cannot_approve")
        _assert_err(c.post(f"/v1/demo/untamper/{seq}", headers=hdr), 403, "agent_cannot_approve")
        _assert_err(
            c.post("/v1/voice/prompt", headers=hdr, json={"order_id": o_aurora["order_id"]}),
            403,
            "agent_cannot_approve",
        )
        _assert_err(
            c.post(
                "/v1/voice/intent",
                headers=hdr,
                json={"transcript": "approve", "order_id": o_aurora["order_id"]},
            ),
            403,
            "agent_cannot_approve",
        )
        _assert_err(
            c.post(
                "/v1/voice/transcribe",
                headers=hdr,
                files={"audio": ("clip.webm", b"\x00\x01", "audio/webm")},
            ),
            403,
            "agent_cannot_approve",
        )

    assert c.get("/v1/audit/verify").json()["ok"] is True
    tamper_refusals = [
        e
        for e in c.get("/v1/audit", params={"subject_id": f"audit:{seq}"}).json()
        if e.get("action") == "approval.refused" and e.get("actor") == "agent:buyer-1"
    ]
    assert len(tamper_refusals) == 6


def test_cross_tenant_customer_mismatch_matrix_header_vs_body_query_path_and_resource(
    isolated_env: buyer_runs.RunRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aimo.agents.seller as seller_mod
    import aimo.recipes as recipes_mod

    monkeypatch.setattr(
        recipes_mod,
        "plan_recipe",
        lambda recipe_id, portions, delivery_by, customer_id, **kw: {
            "customer_id": customer_id,
            "recipe": {"id": recipe_id or "lasagne"},
            "ingredients": [],
            "quote_request": {"delivery_date": DELIVERY_DAY.isoformat(), "lines": []},
        },
    )

    def _fake_propose(customer_id: str, **kw: Any) -> dict[str, Any]:
        prop = {
            "proposal_id": "P-redteam-1",
            "customer_id": customer_id,
            "seller_id": "valio-aimo/seller-1",
            "status": "proposed",
            "created_at": "2026-11-14T12:00:00+00:00",
            "quote_request": {
                "delivery_date": DELIVERY_DAY.isoformat(),
                "lines": [{"product_id": "464306", "qty_sales_units": 5}],
            },
        }
        seller_mod._save(prop)
        return prop

    monkeypatch.setattr(seller_mod, "propose_offer", _fake_propose)

    _, c = _make_app_and_client()
    d_iso = DELIVERY_DAY.isoformat()

    _assert_err(
        c.get("/v1/products/search", params={"q": "milk", "customer_id": "hotel-aurora"}, headers=H_SCHOOL),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.post(
            "/v1/offers",
            headers=H_SCHOOL,
            json={"product_ids": ["464306"], "requested_date": d_iso, "customer_id": "hotel-aurora"},
        ),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.post(
            "/v1/plan",
            headers=H_SCHOOL,
            json={
                "product_query": "milk",
                "quantity": 10,
                "unit": "L",
                "delivery_by": d_iso,
                "customer_id": "hotel-aurora",
            },
        ),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.post(
            "/v1/quotes",
            headers=H_SCHOOL,
            json={
                "delivery_date": d_iso,
                "customer_id": "hotel-aurora",
                "lines": [{"product_id": "464306", "qty_sales_units": 5}],
            },
        ),
        403,
        "customer_mismatch",
    )

    q_aurora, o_aurora = _pending_aurora(c)
    _assert_err(
        c.post(
            "/v1/orders",
            headers=H_SCHOOL,
            json={"quote_id": q_aurora["quote_id"], "quote_hash": q_aurora["quote_hash"]},
        ),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.get("/v1/approvals", params={"customer_id": "hotel-aurora"}, headers=H_SCHOOL),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.get("/v1/customers/hotel-aurora/learned", headers=H_SCHOOL),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.post(
            "/v1/recipes/plan",
            headers=H_SCHOOL,
            json={"recipe_id": "lasagne", "portions": 20, "delivery_by": d_iso, "customer_id": "hotel-aurora"},
        ),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}, headers=H_SCHOOL),
        403,
        "customer_mismatch",
    )

    prop = c.post("/v1/proposals", headers=H_SELLER, json={"customer_id": "hotel-aurora"}).json()
    pid = prop["proposal_id"]
    _assert_err(c.post(f"/v1/proposals/{pid}/accept", headers=H_SCHOOL), 403, "customer_mismatch")
    _assert_err(c.post(f"/v1/proposals/{pid}/decline", headers=H_SCHOOL, json={"reason": "no"}), 403, "not_your_proposal")

    _assert_err(
        c.post(
            "/v1/agent/runs",
            headers={"X-Customer-Id": "school-tampere"},
            json={"text": "10 L milk", "customer_id": "hotel-aurora"},
        ),
        403,
        "customer_mismatch",
    )
    run_res = c.post(
        "/v1/agent/runs",
        headers={"X-Customer-Id": "hotel-aurora"},
        json={"text": "10 L milk"},
    )
    assert run_res.status_code == 200
    run_id = run_res.json()["run_id"]
    _assert_err(
        c.post(
            f"/v1/agent/runs/{run_id}/answer",
            headers={"X-Customer-Id": "school-tampere"},
            json={"option_id": "d0"},
        ),
        403,
        "customer_mismatch",
    )

    anna_pin = human_auth.approver_pins()[ANNA]
    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]
    _assert_err(
        c.post(
            "/v1/voice/prompt",
            headers={"X-Approver-Session": anna_sess, "X-Customer-Id": "school-tampere"},
            json={"order_id": o_aurora["order_id"]},
        ),
        403,
        "customer_mismatch",
    )
    _assert_err(
        c.post(
            "/v1/voice/intent",
            headers={"X-Approver-Session": anna_sess, "X-Customer-Id": "school-tampere"},
            json={"transcript": "approve", "order_id": o_aurora["order_id"]},
        ),
        403,
        "customer_mismatch",
    )


def test_aimo_agent_keys_json_csv_equals_in_secret_and_wildcard_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, c = _make_app_and_client()
    aurora_secret = "aurora+b64/sec=ret=="
    wild_secret = "wild+b64/sec=1=="

    formats = [
        json.dumps({"hotel-aurora": aurora_secret, "*": wild_secret}),
        f"hotel-aurora={aurora_secret},*={wild_secret}",
        f"hotel-aurora:{aurora_secret},*:{wild_secret}",
    ]

    for raw_cfg in formats:
        monkeypatch.setenv("AIMO_AGENT_KEYS", raw_cfg)
        parsed = auth._configured_agent_keys()
        assert parsed == {"hotel-aurora": aurora_secret, "*": wild_secret}

        _assert_err(c.get("/v1/products/search", params={"q": "milk"}, headers=H_AURORA), 401, "agent_key_required")
        _assert_err(
            c.get("/v1/products/search", params={"q": "milk"}, headers={**H_AURORA, "X-Agent-Key": wild_secret}),
            403,
            "invalid_agent_key",
        )
        assert (
            c.get(
                "/v1/products/search",
                params={"q": "milk"},
                headers={**H_AURORA, "X-Agent-Key": aurora_secret},
            ).status_code
            == 200
        )

        _assert_err(c.get("/v1/products/search", params={"q": "milk"}, headers=H_SCHOOL), 401, "agent_key_required")
        _assert_err(
            c.get("/v1/products/search", params={"q": "milk"}, headers={**H_SCHOOL, "X-Agent-Key": "wrong=="}),
            403,
            "invalid_agent_key",
        )
        assert (
            c.get(
                "/v1/products/search",
                params={"q": "milk"},
                headers={**H_SCHOOL, "X-Agent-Key": wild_secret},
            ).status_code
            == 200
        )
        assert (
            c.get(
                "/v1/products/search",
                params={"q": "milk"},
                headers={**H_HOSPITAL, "X-Agent-Key": wild_secret},
            ).status_code
            == 200
        )

    q_res = c.post(
        "/v1/quotes",
        headers={**H_AURORA, "X-Agent-Key": aurora_secret},
        json={
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": 5}],
        },
    )
    assert q_res.status_code == 200
    q = q_res.json()
    _assert_err(
        c.post("/v1/orders", headers=H_AURORA, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}),
        401,
        "agent_key_required",
    )
    o_res = c.post(
        "/v1/orders",
        headers={**H_AURORA, "X-Agent-Key": aurora_secret},
        json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
    )
    assert o_res.status_code == 200


def test_adversarial_unicode_non_ascii_control_and_null_headers_return_clean_401_403_zero_500s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, c = _make_app_and_client()
    q_aurora, o_aurora = _pending_aurora(c)
    oid = o_aurora["order_id"]
    qhash = q_aurora["quote_hash"]

    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"hotel-aurora": "aurora-key-12345", "*": "wild-key-12345"}))

    for bad_tok in (
        f"v2.9999999999.{ANNA}.ääää",
        f"v2.9999999999.{ANNA}.\U0001f512",
        f"v2.9999999999.{ANNA}.\ud800",
        f"v2.9999999999.{ANNA}.\x00deadbeef",
        f"v2.\u0662\u0669\u0669\u0669\u0669\u0669\u0669\u0669\u0669\u0669.{ANNA}.deadbeef",
        "v2." + ("9" * 2000) + f".{ANNA}.deadbeef",
    ):
        assert human_auth.session_approver(bad_tok) is None
        assert human_auth.check_session(bad_tok) is False

    human_auth.reset_for_tests()
    for bad_pin in ("väärä-äöå", "\U0001f512", "\ud800", "pin\x00null", "\xa0", "p" * 5000):
        assert human_auth.check_pin_for(bad_pin, ANNA, source="unit-src") is False
        human_auth.reset_for_tests()
        assert human_auth.check_pin(bad_pin, source="unit-src") is False
        human_auth.reset_for_tests()

    for bad_key in ("väärä-avain-äöå", "\U0001f512", "\ud800", "key\x00null", "\xa0", "k" * 6000):
        with pytest.raises(service.ServiceError) as exc_key:
            auth.check_agent_key("hotel-aurora", bad_key)
        assert exc_key.value.status == 403 and exc_key.value.code == "invalid_agent_key"

    for bad_cid in ("hotel-äöå", "hotel-\ud800", "hotel\x00aurora", "hotel\x85aurora", "../hotel-aurora", "c" * 500):
        with pytest.raises(service.ServiceError) as exc_cid:
            auth.require_known_customer(bad_cid)
        assert exc_cid.value.status == 403 and exc_cid.value.code == "unknown_customer"
        encoded = json.dumps(exc_cid.value.to_dict()).encode("utf-8")
        assert encoded

    session_wire_payloads = [
        f"v2.9999999999.{ANNA}.ääää".encode("utf-8"),
        f"v2.9999999999.{ANNA}.\U0001f512".encode("utf-8"),
        f"v2.9999999999.{ANNA}.".encode("ascii") + b"\xff\xfe\x80\x9f",
        f"v2.9999999999.{ANNA}.\x00bad".encode("latin-1"),
        b"v2.9999999999." + (b"x" * 4096) + b".sig",
    ]
    decision_body = json.dumps({"decision": "approve", "approver": ANNA, "quote_hash": qhash}).encode("utf-8")
    voice_prompt_body = json.dumps({"order_id": oid}).encode("utf-8")
    voice_intent_body = json.dumps({"transcript": "approve", "order_id": oid}).encode("utf-8")

    for raw_sess in session_wire_payloads:
        human_auth.reset_for_tests()
        for method, path, body in (
            ("GET", "/v1/approvals", b""),
            ("POST", f"/v1/approvals/{oid}/decision", decision_body),
            ("POST", "/v1/demo/tamper/1", b""),
            ("POST", "/v1/demo/untamper/1", b""),
            ("POST", "/v1/voice/prompt", voice_prompt_body),
            ("POST", "/v1/voice/intent", voice_intent_body),
            ("POST", "/v1/voice/transcribe", b"\x00\x01"),
        ):
            hdrs = [(b"x-approver-session", raw_sess)]
            if body.startswith(b"{"):
                hdrs.append((b"content-type", b"application/json"))
            st, txt = _asgi_raw_request(app, method, path, hdrs, body=body)
            assert st == 401, (method, path, raw_sess[:40], st, txt)
            parsed = json.loads(txt)
            assert parsed.get("error") in {
                "identity_required",
                "approver_auth_required",
                "approver_session_required",
            }

        st_cust, txt_cust = _asgi_raw_request(app, "GET", "/v1/customers", [(b"x-approver-session", raw_sess)])
        assert st_cust == 200
        for cust_obj in json.loads(txt_cust):
            for ap_obj in cust_obj.get("approvers") or []:
                assert "id" not in ap_obj

    pin_wire_payloads = [
        "väärä-salasana-äöå".encode("utf-8"),
        "\U0001f512-pin".encode("utf-8"),
        b"\xff\xfe\x00\x1f",
        b"\xa0",
        b"p" * 4096,
    ]
    signin_body = json.dumps({"approver": ANNA}).encode("utf-8")
    for raw_pin in pin_wire_payloads:
        human_auth.reset_for_tests()
        for path, body in (
            ("/v1/auth/approver-session", signin_body),
            (f"/v1/approvals/{oid}/decision", decision_body),
            ("/v1/demo/tamper/1", b""),
        ):
            hdrs = [(b"x-approver-pin", raw_pin)]
            if body:
                hdrs.append((b"content-type", b"application/json"))
            st, txt = _asgi_raw_request(app, "POST", path, hdrs, body=body)
            assert st == 401, (path, raw_pin[:30], st, txt)
            assert json.loads(txt)["error"] == "approver_auth_required"

    key_wire_payloads = [
        "väärä-avain-äöå".encode("utf-8"),
        "\U0001f512-key".encode("utf-8"),
        b"\xff\xfe\x00\x9f",
        b"\xa0",
        b"k" * 5000,
    ]
    offers_body = json.dumps({"product_ids": ["464306"], "requested_date": DELIVERY_DAY.isoformat()}).encode("utf-8")
    runs_body = json.dumps({"text": "10 L milk"}).encode("utf-8")
    for raw_key in key_wire_payloads:
        for method, path, qs, body in (
            ("GET", "/v1/products/search", b"q=milk", b""),
            ("GET", "/v1/approvals", b"", b""),
            ("POST", "/v1/offers", b"", offers_body),
            ("POST", "/v1/agent/runs", b"", runs_body),
        ):
            hdrs = [
                (b"x-customer-id", b"hotel-aurora"),
                (b"x-agent-id", b"buyer-1"),
                (b"x-agent-key", raw_key),
            ]
            if body:
                hdrs.append((b"content-type", b"application/json"))
            st, txt = _asgi_raw_request(app, method, path, hdrs, query_string=qs, body=body)
            assert st == 403, (method, path, raw_key[:30], st, txt)
            assert json.loads(txt)["error"] == "invalid_agent_key"

    cid_wire_payloads = [
        "hotel-äöå".encode("utf-8"),
        "hotel-\U0001f512".encode("utf-8"),
        b"hotel-aurora\x00",
        b"\xff\xfe\x85\x9f",
        b"../hotel-aurora",
        b"c" * 2048,
    ]
    quote_body = json.dumps(
        {
            "delivery_date": DELIVERY_DAY.isoformat(),
            "lines": [{"product_id": "464306", "qty_sales_units": 1}],
        }
    ).encode("utf-8")
    for raw_cid in cid_wire_payloads:
        for method, path, qs, body in (
            ("GET", "/v1/products/search", b"q=milk", b""),
            ("GET", "/v1/products/464306", b"", b""),
            ("GET", "/v1/approvals", b"", b""),
            ("POST", "/v1/offers", b"", offers_body),
            ("POST", "/v1/quotes", b"", quote_body),
            ("POST", "/v1/voice/prompt", b"", voice_prompt_body),
            ("POST", "/v1/agent/runs", b"", runs_body),
        ):
            hdrs = [(b"x-customer-id", raw_cid), (b"x-agent-id", b"buyer-1")]
            if body:
                hdrs.append((b"content-type", b"application/json"))
            st, txt = _asgi_raw_request(app, method, path, hdrs, query_string=qs, body=body)
            assert st == 403, (method, path, raw_cid[:30], st, txt)
            assert json.loads(txt)["error"] == "unknown_customer"

    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    for bad_aid in ("../buyer-1", "buyer\\1", "buyer\x00one", "buyer-\ud800", "buyer\x85one", "a" * 300):
        with pytest.raises(service.ServiceError) as exc_ha:
            auth.resolve_customer_identity(x_customer_id="hotel-aurora", x_agent_id=bad_aid)
        assert exc_ha.value.status == 403 and exc_ha.value.code == "invalid_agent_id"
        assert json.dumps(exc_ha.value.to_dict()).encode("utf-8")
        with pytest.raises(service.ServiceError) as exc_ba:
            auth.resolve_customer_identity(x_customer_id="hotel-aurora", body_agent_id=bad_aid)
        assert exc_ba.value.status == 403 and exc_ba.value.code == "invalid_agent_id"

    for raw_aid in (b"../buyer-1", b"buyer\\1", b"buyer\x00one", b"\xff\xfe\x85\x9f", b"a" * 512):
        for path, body in (("/v1/offers", offers_body), ("/v1/quotes", quote_body)):
            hdrs = [
                (b"x-customer-id", b"hotel-aurora"),
                (b"x-agent-id", raw_aid),
                (b"content-type", b"application/json"),
            ]
            st, txt = _asgi_raw_request(app, "POST", path, hdrs, body=body)
            assert st == 403, (path, raw_aid[:30], st, txt)
            assert json.loads(txt)["error"] == "invalid_agent_id"


def test_distributed_global_lockout_session_tampering_and_malformed_json_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, c = _make_app_and_client()
    pins = human_auth.approver_pins()
    anna_pin, liisa_pin = pins[ANNA], pins[LIISA]

    issued = human_auth.issue_session(ANNA)
    anna_sess = issued["session"]
    parts = anna_sess.split(".")
    sig = parts[-1]
    exp = int(parts[1])

    expired_tok = f"v2.{int(time.time()) - 10}.{ANNA}.{sig}"
    swapped_who = f"v2.{exp}.{MIKKO}.{sig}"
    wrong_ver = f"v1.{exp}.{ANNA}.{sig}"
    empty_sig = f"v2.{exp}.{ANNA}."
    for forged in (expired_tok, swapped_who, wrong_ver, empty_sig):
        assert human_auth.session_approver(forged) is None
        assert human_auth.check_session(forged) is False

    q_aurora, o_aurora = _pending_aurora(c)
    for forged in (expired_tok, swapped_who, wrong_ver, empty_sig):
        _assert_err(
            c.post(
                f"/v1/approvals/{o_aurora['order_id']}/decision",
                headers={"X-Approver-Session": forged},
                json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
            ),
            401,
            "approver_auth_required",
        )

    human_auth.reset_for_tests()
    monkeypatch.setattr(human_auth, "GLOBAL_MAX", 6)
    for idx in range(6):
        assert human_auth.check_pin_for(f"bad-dist-{idx}", ANNA, source=f"198.51.100.{idx + 1}") is False
    assert human_auth.locked_for("203.0.113.250", ANNA) > 0
    assert human_auth.check_pin_for(anna_pin, ANNA, source="203.0.113.250") is False
    assert human_auth.locked_for("203.0.113.250", LIISA) == 0.0
    assert human_auth.check_pin_for(liisa_pin, LIISA, source="203.0.113.250") is True
    assert human_auth.session_approver(anna_sess) == ANNA

    r_anna_sess_ok = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": anna_sess},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_anna_sess_ok.status_code == 200 and r_anna_sess_ok.json()["status"] == "placed"

    monkeypatch.setenv(
        "AIMO_AGENT_KEYS",
        json.dumps({"hotel-aurora": True, "school-tampere": None, "hospital-meilahti": ["bad"], "*": False}),
    )
    assert auth._configured_agent_keys() == {}
    monkeypatch.setenv("AIMO_AGENT_KEYS", "{broken-json")
    assert auth._configured_agent_keys() == {}

    monkeypatch.setenv(
        "AIMO_APPROVER_PINS",
        json.dumps({ANNA: True, MIKKO: False, LIISA: None, SARA: {"nested": "x"}}),
    )
    assert human_auth._configured_approver_pins() == {}
    assert human_auth.check_pin_for("True", ANNA, source="cfg-src") is False
    assert human_auth.check_pin_for("None", LIISA, source="cfg-src") is False

