"""Tests for per-approver credentials, per-credential session binding, and per-identity brute-force lockout."""
from __future__ import annotations

import json
import time
import types
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import human_auth, service
from aimo.models import AllergenStatus, Product, ProductSummary

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 11, 16)
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
ANNA = "anna@hotel-aurora.example"
MIKKO = "mikko@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
SARA = "sara@hospital-meilahti.example"


class _FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        for item in self.price_raw["products"]:
            if item["productId"] == "464306":
                oat_price = dict(item, productId="472833")
                self.price_raw["products"].append(oat_price)
                break
        for item in self.avail_raw["products"]:
            if item["productId"] == "464306":
                oat_avail = dict(item, productId="472833")
                self.avail_raw["products"].append(oat_avail)
                break

    def _sel(self, raw: dict, ids: list[str]) -> dict:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.avail_raw, ids)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        search=lambda q, **kw: [ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()],
        get_product=lambda pid: products.get(str(pid)),
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    human_auth.reset_for_tests()
    bc.set_client(_FixtureClient())
    yield
    human_auth.reset_for_tests()
    bc.set_client(None)


def _client() -> TestClient:
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False))


def _pending_aurora(c: TestClient) -> tuple[dict, dict]:
    q = c.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
    ).json()
    o = c.post("/v1/orders", headers=H_AURORA, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


def _pending_school(c: TestClient) -> tuple[dict, dict]:
    q = c.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={"delivery_date": D.isoformat(), "lines": [{"product_id": "472833", "qty_sales_units": 500, "tier": "preferred"}]},
    ).json()
    o = c.post("/v1/orders", headers=H_SCHOOL, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


def test_generated_per_approver_pins_differ_and_block_cross_approver_impersonation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    c = _client()

    pins = human_auth.approver_pins()
    pins_file = config.KEY_DIR / "approver_pins.json"
    assert pins_file.exists()
    assert (pins_file.stat().st_mode & 0o777) == 0o600
    for who in (ANNA, MIKKO, LIISA, SARA):
        assert who in pins
        assert len(pins[who]) >= 12
    assert len(set(pins.values())) == len(pins)
    assert human_auth.expected_pin_for("Anna Virtanen") == pins[ANNA]
    assert human_auth.expected_pin_for(MIKKO) == pins[MIKKO]
    assert human_auth.expected_pin_for(LIISA) == pins[LIISA]
    assert human_auth.expected_pin_for("nobody@example.com") is None

    anna_pin = pins[ANNA]
    mikko_pin = pins[MIKKO]
    liisa_pin = pins[LIISA]

    r_signin_mikko_with_anna = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": MIKKO},
    )
    assert r_signin_mikko_with_anna.status_code == 401
    assert r_signin_mikko_with_anna.json()["error"] == "approver_auth_required"

    r_signin_liisa_with_anna = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": LIISA},
    )
    assert r_signin_liisa_with_anna.status_code == 401
    assert r_signin_liisa_with_anna.json()["error"] == "approver_auth_required"

    q_aurora, o_aurora = _pending_aurora(c)
    r_decide_mikko_with_anna = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Pin": anna_pin},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": MIKKO},
    )
    assert r_decide_mikko_with_anna.status_code == 401
    assert r_decide_mikko_with_anna.json()["error"] == "approver_auth_required"

    q_school, o_school = _pending_school(c)
    r_decide_liisa_with_anna = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Pin": anna_pin},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"], "approver": LIISA},
    )
    assert r_decide_liisa_with_anna.status_code == 401
    assert r_decide_liisa_with_anna.json()["error"] == "approver_auth_required"

    r_decide_mikko_ok = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Pin": mikko_pin},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": MIKKO},
    )
    assert r_decide_mikko_ok.status_code == 200
    assert r_decide_mikko_ok.json()["status"] == "placed"
    assert r_decide_mikko_ok.json()["approved_by"] == MIKKO

    r_decide_liisa_ok = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Pin": liisa_pin},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"], "approver": LIISA},
    )
    assert r_decide_liisa_ok.status_code == 200
    assert r_decide_liisa_ok.json()["status"] == "placed"
    assert r_decide_liisa_ok.json()["approved_by"] == LIISA


def test_env_approver_pins_overrides_and_rotating_one_pin_invalidates_only_that_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_APPROVER_PIN", "shared-fallback-12")
    monkeypatch.setenv(
        "AIMO_APPROVER_PINS",
        json.dumps({ANNA: "anna-passphrase-1", LIISA: "liisa-passphrase-1"}),
    )
    c = _client()

    assert c.post("/v1/auth/approver-session", headers={"X-Approver-Pin": "shared-fallback-12"}, json={"approver": ANNA}).status_code == 401
    assert c.post("/v1/auth/approver-session", headers={"X-Approver-Pin": "anna-passphrase-1"}, json={"approver": LIISA}).status_code == 401

    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "anna-passphrase-1"},
        json={"approver": ANNA},
    ).json()["session"]
    liisa_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": "liisa-passphrase-1"},
        json={"approver": LIISA},
    ).json()["session"]
    assert human_auth.session_approver(anna_sess) == ANNA
    assert human_auth.session_approver(liisa_sess) == LIISA

    monkeypatch.setenv("AIMO_APPROVER_PINS", f"{ANNA}=anna-passphrase-2,{LIISA}=liisa-passphrase-1")
    assert human_auth.session_approver(anna_sess) is None
    assert human_auth.session_approver(liisa_sess) == LIISA

    q_aurora, o_aurora = _pending_aurora(c)
    r_old_anna = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": anna_sess},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_old_anna.status_code == 401

    q_school, o_school = _pending_school(c)
    r_liisa_still_good = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Session": liisa_sess},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"]},
    )
    assert r_liisa_still_good.status_code == 200
    assert r_liisa_still_good.json()["status"] == "placed"


def test_locking_out_anna_does_not_lock_out_liisa_or_annas_active_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    c = _client()
    pins = human_auth.approver_pins()
    anna_pin, mikko_pin, liisa_pin = pins[ANNA], pins[MIKKO], pins[LIISA]

    anna_sess = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": anna_pin},
        json={"approver": ANNA},
    ).json()["session"]

    q_aurora, o_aurora = _pending_aurora(c)
    q_school, o_school = _pending_school(c)

    body_anna = {"decision": "approve", "quote_hash": q_aurora["quote_hash"], "approver": ANNA}
    codes = [
        c.post(
            f"/v1/approvals/{o_aurora['order_id']}/decision",
            headers={"X-Approver-Pin": f"wrong-{i}"},
            json=body_anna,
        ).status_code
        for i in range(6)
    ]
    assert codes == [401, 401, 401, 401, 429, 429]

    assert (
        c.post(
            f"/v1/approvals/{o_aurora['order_id']}/decision",
            headers={"X-Approver-Pin": anna_pin},
            json=body_anna,
        ).status_code
        == 429
    )

    r_liisa_signin = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": liisa_pin},
        json={"approver": LIISA},
    )
    assert r_liisa_signin.status_code == 200

    r_mikko_signin = c.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": mikko_pin},
        json={"approver": MIKKO},
    )
    assert r_mikko_signin.status_code == 200

    r_liisa_decide = c.post(
        f"/v1/approvals/{o_school['order_id']}/decision",
        headers={"X-Approver-Pin": liisa_pin},
        json={"decision": "approve", "quote_hash": q_school["quote_hash"], "approver": LIISA},
    )
    assert r_liisa_decide.status_code == 200
    assert r_liisa_decide.json()["status"] == "placed"

    r_anna_session_decide = c.post(
        f"/v1/approvals/{o_aurora['order_id']}/decision",
        headers={"X-Approver-Session": anna_sess},
        json={"decision": "approve", "quote_hash": q_aurora["quote_hash"]},
    )
    assert r_anna_session_decide.status_code == 200
    assert r_anna_session_decide.json()["status"] == "placed"
    assert r_anna_session_decide.json()["approved_by"] == ANNA

    human_auth.reset_for_tests()
    monkeypatch.setattr(human_auth, "GLOBAL_MAX", 6)
    for idx in range(6):
        assert human_auth.check_pin_for(f"distributed-guess-{idx}", ANNA, source=f"198.51.100.{idx}") is False
    assert human_auth.locked_for("203.0.113.77", ANNA) > 0
    assert human_auth.check_pin_for(anna_pin, ANNA, source="203.0.113.77") is False
    assert human_auth.locked_for("203.0.113.77", LIISA) == 0.0
    assert human_auth.check_pin_for(liisa_pin, LIISA, source="203.0.113.77") is True
    assert human_auth.check_session(anna_sess) is True
