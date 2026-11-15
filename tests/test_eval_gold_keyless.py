from __future__ import annotations

import json
import sys
import time
import types
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types as genai_types

import aimo.catalog as catalog_mod
import aimo.trust as trust_pkg
from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import clock
from aimo.backend import orders as store
from aimo.buyer import eval_gold
from aimo.buyer.eval_gold import APPROVERS, Approver, InprocApprover, _now
from aimo.buyer.mock_model import MOCK_MODEL_NAME, mock_wanted
from aimo.buyer.need import HELSINKI, resolve_date
from aimo.catalog import textsearch as textsearch_mod
from aimo.gateway import human_auth, service
from aimo.models import AllergenStatus, Product, ProductSummary
from aimo.trust import approvals as trust_approvals

FIX = Path(__file__).parent / "fixtures"
DELIVERY = date(2026, 11, 16)


def _set_gemini_keys(monkeypatch: pytest.MonkeyPatch, gemini_key: str, google_key: str = "") -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key or google_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


class _FixtureUpstream:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        base_price = next(p for p in self.price_raw["products"] if p["productId"] == "464306")
        base_avail = next(p for p in self.avail_raw["products"] if p["productId"] == "464306")
        for extra_id in ("472833", "473328"):
            self.price_raw["products"].append(dict(base_price, productId=extra_id))
            self.avail_raw["products"].append(dict(base_avail, productId=extra_id))

    def _sel(self, raw: dict, ids: list[str]) -> dict:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.avail_raw, ids)


def _write_gold_fixture(path: Path) -> Path:
    rows = [
        {
            "id": "k01",
            "segment": "hotel",
            "lang": "en",
            "request": "We need 20 kg of lactose-free butter by Monday.",
            "customer_id": "hotel-aurora",
            "gold": {
                "acceptable_product_ids": [["402105"]],
                "qty_range": [[20, 22]],
                "must_escalate": False,
                "forbidden_allergens": [],
            },
        },
        {
            "id": "k02",
            "segment": "hotel",
            "lang": "en",
            "request": "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "customer_id": "hotel-aurora",
            "gold": {
                "acceptable_product_ids": [["464306"]],
                "qty_range": [[1000, 1100]],
                "must_escalate": True,
                "forbidden_allergens": [],
            },
        },
        {
            "id": "k03",
            "segment": "school",
            "lang": "en",
            "request": "We need 200 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.",
            "customer_id": "school-tampere",
            "gold": {
                "acceptable_product_ids": [["470863"]],
                "qty_range": [[200, 220]],
                "must_escalate": True,
                "forbidden_allergens": ["milk", "nuts"],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_approver_sends_distinct_per_approver_pins_and_retries_transient_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    human_auth.reset_for_tests()

    pins = {cust: human_auth.local_pin(approver) for cust, approver in APPROVERS.items()}
    assert set(pins) == {"hotel-aurora", "school-tampere", "hospital-meilahti"}
    assert len(set(pins.values())) == 3

    pending_payload = [
        {"order": {"order_id": "O-aurora", "customer_id": "hotel-aurora", "quote_hash": "qh-aurora"}},
        {"order": {"order_id": "O-school", "customer_id": "school-tampere", "quote_hash": "qh-school"}},
        {"order": {"order_id": "O-hospital", "customer_id": "hospital-meilahti", "quote_hash": "qh-hospital"}},
    ]

    get_calls: list[dict] = []
    post_calls: list[dict] = []
    attempts: dict[str, int] = {}
    ap = Approver("http://gw.local", poll_s=0.01)

    def fake_get(url: str, *, params=None, headers=None, timeout=None):
        get_calls.append({"url": url, "params": params, "headers": dict(headers or {})})
        if len(get_calls) >= 3:
            ap.stop.set()
        return httpx.Response(200, json=pending_payload)

    def fake_post(url: str, *, json=None, headers=None, timeout=None):
        oid = url.rsplit("/", 2)[-2]
        attempts[oid] = attempts.get(oid, 0) + 1
        post_calls.append({"oid": oid, "url": url, "json": dict(json or {}), "headers": dict(headers or {})})
        if oid == "O-school" and attempts[oid] == 1:
            return httpx.Response(503, json={"error": "upstream_unavailable"})
        if oid == "O-hospital" and attempts[oid] == 1:
            raise httpx.ConnectError("transient socket drop")
        return httpx.Response(200, json={"order_id": oid, "status": "placed"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    ap.start()
    ap.join(timeout=2.0)
    assert not ap.is_alive()

    assert len(get_calls) >= 2
    for gc in get_calls:
        assert gc["url"] == "http://gw.local/v1/approvals"
        assert gc["headers"]["X-Approver-Pin"] == human_auth.local_pin()

    by_oid: dict[str, list[dict]] = {}
    for pc in post_calls:
        by_oid.setdefault(pc["oid"], []).append(pc)

    assert len(by_oid["O-aurora"]) == 1
    assert len(by_oid["O-school"]) == 2
    assert len(by_oid["O-hospital"]) == 2

    assert by_oid["O-aurora"][-1]["json"]["approver"] == "anna@hotel-aurora.example"
    assert by_oid["O-aurora"][-1]["headers"]["X-Approver-Pin"] == pins["hotel-aurora"]

    assert by_oid["O-school"][-1]["json"]["approver"] == "liisa@school-tampere.example"
    assert by_oid["O-school"][-1]["headers"]["X-Approver-Pin"] == pins["school-tampere"]

    assert by_oid["O-hospital"][-1]["json"]["approver"] == "sara@hospital-meilahti.example"
    assert by_oid["O-hospital"][-1]["headers"]["X-Approver-Pin"] == pins["hospital-meilahti"]


def _isolate_live_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            content_unit="L",
            content_amount=1.0,
            claims=["FREE_FROM_MILK", "VEGAN"],
            allergens={"milk": AllergenStatus.free, "nuts": AllergenStatus.free},
            allergen_source={"milk": "structured", "nuts": "structured"},
        ),
        "473328": Product(
            id="473328",
            name="Schar Vital dark sliced bread 350 g gluten free",
            brand="Schar",
            category="11100",
            sales_unit="ST",
            content_amount=0.35,
            content_unit="KG",
            claims=["FREE_FROM_GLUTEN"],
            allergens={"gluten": AllergenStatus.free},
            allergen_source={"gluten": "structured"},
        ),
    }
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: [
            ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
        ],
        get_product=lambda pid: products.get(str(pid)),
    )
    isolated_audit = trust_pkg.AuditLog(tmp_path / "audit.sqlite")
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(catalog_mod, "get_product", stub_cat.get_product)
    monkeypatch.setattr(textsearch_mod, "get_rows", lambda ids: {})
    monkeypatch.setattr(sys.modules["aimo.trust.audit"], "audit", isolated_audit)
    monkeypatch.setattr(trust_pkg, "audit", isolated_audit)
    monkeypatch.setattr(trust_approvals, "audit", isolated_audit)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T12:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    human_auth.reset_for_tests()
    bc.set_client(_FixtureUpstream())


def test_approver_authenticates_against_live_gateway_for_all_three_customers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_live_gateway(tmp_path, monkeypatch)
    try:
        from aimo.gateway.app import create_app

        client = TestClient(create_app(with_mcp=False))
        specs = [
            ("hotel-aurora", "464306", 1000, "best"),
            ("school-tampere", "472833", 500, "preferred"),
            ("hospital-meilahti", "464306", 100, "best"),
        ]
        order_ids: dict[str, str] = {}
        for cust, pid, qty, tier in specs:
            hdr = {"X-Customer-Id": cust, "X-Agent-Id": "buyer-1"}
            q = client.post(
                "/v1/quotes",
                headers=hdr,
                json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": pid, "qty_sales_units": qty, "tier": tier}]},
            ).json()
            o = client.post("/v1/orders", headers=hdr, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
            assert o["status"] == "pending_approval"
            order_ids[cust] = o["order_id"]

        ap = Approver("http://testserver", poll_s=0.01)

        def bridge_get(url: str, *, params=None, headers=None, timeout=None):
            path = urlparse(url).path
            return client.get(path, params=params, headers=headers)

        def bridge_post(url: str, *, json=None, headers=None, timeout=None):
            path = urlparse(url).path
            resp = client.post(path, json=json, headers=headers)
            if all(client.get(f"/v1/orders/{oid}").json()["status"] == "placed" for oid in order_ids.values()):
                ap.stop.set()
            return resp

        monkeypatch.setattr(httpx, "get", bridge_get)
        monkeypatch.setattr(httpx, "post", bridge_post)

        ap.start()
        try:
            ap.join(timeout=3.0)
            assert not ap.is_alive()
        finally:
            ap.stop.set()

        for cust, oid in order_ids.items():
            final = client.get(f"/v1/orders/{oid}").json()
            assert final["status"] == "placed"
            assert final["approved_by"] == APPROVERS[cust]
            assert human_auth.locked_for("testclient", APPROVERS[cust]) == 0.0
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)


def test_approver_skips_expired_and_approves_replacement_after_409_requote_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_live_gateway(tmp_path, monkeypatch)
    try:
        from aimo.gateway.app import create_app

        client = TestClient(create_app(with_mcp=False))
        monkeypatch.setenv("AIMO_NOW", "2026-11-14T11:30:00+02:00")
        hdr_school = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
        q_stale = client.post(
            "/v1/quotes",
            headers=hdr_school,
            json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "472833", "qty_sales_units": 500, "tier": "preferred"}]},
        ).json()
        o_stale = client.post(
            "/v1/orders",
            headers=hdr_school,
            json={"quote_id": q_stale["quote_id"], "quote_hash": q_stale["quote_hash"]},
        ).json()
        assert o_stale["status"] == "pending_approval"
        stale_oid = o_stale["order_id"]

        monkeypatch.setenv("AIMO_NOW", "2026-11-14T12:00:00+02:00")
        hdr_aurora = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
        q_orig = client.post(
            "/v1/quotes",
            headers=hdr_aurora,
            json={"delivery_date": DELIVERY.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
        ).json()
        o_orig = client.post(
            "/v1/orders",
            headers=hdr_aurora,
            json={"quote_id": q_orig["quote_id"], "quote_hash": q_orig["quote_hash"]},
        ).json()
        assert o_orig["status"] == "pending_approval"
        orig_oid = o_orig["order_id"]

        ap = Approver("http://testserver", poll_s=0.01)
        decision_posts: list[tuple[str, int, dict]] = []

        def bridge_get(url: str, *, params=None, headers=None, timeout=None):
            path = urlparse(url).path
            return client.get(path, params=params, headers=headers)

        def bridge_post(url: str, *, json=None, headers=None, timeout=None):
            path = urlparse(url).path
            oid = path.rsplit("/", 2)[-2]
            if oid == orig_oid and not any(prev_oid == orig_oid for prev_oid, _, _ in decision_posts):
                monkeypatch.setenv("AIMO_NOW", "2026-11-14T12:20:00+02:00")
            resp = client.post(path, json=json, headers=headers)
            body = resp.json()
            decision_posts.append((oid, resp.status_code, body))
            if resp.status_code == 200 and body.get("status") == "placed":
                ap.stop.set()
            return resp

        monkeypatch.setattr(httpx, "get", bridge_get)
        monkeypatch.setattr(httpx, "post", bridge_post)

        ap.start()
        try:
            ap.join(timeout=3.0)
            assert not ap.is_alive()
        finally:
            ap.stop.set()

        assert all(oid != stale_oid for oid, _, _ in decision_posts)
        assert len(decision_posts) == 2

        first_oid, first_status, first_body = decision_posts[0]
        assert first_oid == orig_oid
        assert first_status == 409
        assert first_body["error"] == "requote_needed"
        replacement_oid = first_body["detail"]["new_order"]["order_id"]

        second_oid, second_status, second_body = decision_posts[1]
        assert second_oid == replacement_oid
        assert second_status == 200
        assert second_body["status"] == "placed"
        assert second_body["approved_by"] == APPROVERS["hotel-aurora"]

        orig_after = client.get(f"/v1/orders/{orig_oid}").json()
        assert orig_after["status"] == "expired"
        assert orig_after["provenance"]["requoted_as"] == replacement_oid
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)


def test_inproc_approver_skips_expired_and_approves_replacement_after_409_requote_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_live_gateway(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("AIMO_NOW", "2026-11-14T11:30:00+02:00")
        ident_school = service.Identity(agent_id="buyer-1", customer_id="school-tampere")
        q_stale = service.make_quote(ident_school, DELIVERY, [{"product_id": "472833", "qty_sales_units": 500, "tier": "preferred"}])
        o_stale = service.submit_order(ident_school, q_stale.quote_id, q_stale.quote_hash or "")
        assert o_stale.status == "pending_approval"

        monkeypatch.setenv("AIMO_NOW", "2026-11-14T12:00:00+02:00")
        ident_aurora = service.Identity(agent_id="buyer-1", customer_id="hotel-aurora")
        q_orig = service.make_quote(ident_aurora, DELIVERY, [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}])
        o_orig = service.submit_order(ident_aurora, q_orig.quote_id, q_orig.quote_hash or "")
        assert o_orig.status == "pending_approval"

        real_decide = service.decide
        ap = InprocApprover(poll_s=0.01)
        decide_calls: list[tuple[str, str]] = []
        replacement_holder: list[str] = []

        def spy_decide(order_id: str, **kwargs):
            if order_id == o_orig.order_id and not any(oid == o_orig.order_id for oid, _ in decide_calls):
                monkeypatch.setenv("AIMO_NOW", "2026-11-14T12:20:00+02:00")
            try:
                res = real_decide(order_id, **kwargs)
                decide_calls.append((order_id, res.status))
                if res.status == "placed":
                    ap.stop.set()
                return res
            except service.ServiceError as exc:
                decide_calls.append((order_id, exc.code))
                if exc.code == "requote_needed" and isinstance(exc.detail, dict):
                    replacement_holder.append(exc.detail["new_order"]["order_id"])
                raise

        monkeypatch.setattr(service, "decide", spy_decide)
        ap.start()
        try:
            ap.join(timeout=3.0)
            assert not ap.is_alive()
        finally:
            ap.stop.set()

        assert all(oid != o_stale.order_id for oid, _ in decide_calls)
        assert len(replacement_holder) == 1
        assert decide_calls == [(o_orig.order_id, "requote_needed"), (replacement_holder[0], "placed")]
        placed = service.get_order(replacement_holder[0])
        assert placed.status == "placed"
        assert placed.approved_by == APPROVERS["hotel-aurora"]
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)


def test_now_reads_healthz_clock_when_aimo_now_unset_and_prefers_aimo_now_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIMO_NOW", raising=False)
    assert _now() is None
    assert _now(None) is None

    seen_urls: list[tuple[str, float | int | None]] = []

    def fake_healthz(url: str, *, timeout=None):
        seen_urls.append((url, timeout))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "clock": {"mode": "replay", "now": "2026-11-14T12:00:00+00:00", "frozen": True},
            },
        )

    monkeypatch.setattr(httpx, "get", fake_healthz)
    resolved = _now("http://replay.local:8000/")
    assert seen_urls == [("http://replay.local:8000/healthz", 2)]
    assert resolved == datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    assert resolve_date("by Monday", resolved)[0] == date(2026, 11, 16)
    assert resolve_date("for Wednesday's school lunch", resolved)[0] == date(2026, 11, 18)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("/healthz should not be queried when AIMO_NOW is set")

    monkeypatch.setattr(httpx, "get", fail_if_called)
    monkeypatch.setenv("AIMO_NOW", "2026-11-21T09:30:00+02:00")
    override = _now("http://replay.local:8000")
    assert override == datetime(2026, 11, 21, 9, 30, tzinfo=HELSINKI)
    assert resolve_date("by Monday", override)[0] == date(2026, 11, 23)


def test_run_one_queries_healthz_only_for_mcp_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    _set_gemini_keys(monkeypatch, "", "")

    healthz_calls: list[str] = []

    def fake_healthz(url: str, *, timeout=None):
        healthz_calls.append(url)
        return httpx.Response(
            200,
            json={"ok": True, "clock": {"mode": "replay", "now": "2026-11-21T10:00:00+02:00", "frozen": True}},
        )

    monkeypatch.setattr(httpx, "get", fake_healthz)

    from aimo.buyer.fakes import FakeGateway
    from aimo.buyer.tools import FakeTransport

    monkeypatch.setattr(
        "aimo.buyer.tools.connect",
        lambda kind, *, agent_id, customer_id, **kw: FakeTransport(FakeGateway(customer_id, agent_id)),
    )
    captured_now: list[datetime | None] = []
    orig_agent = eval_gold.BuyerAgent if hasattr(eval_gold, "BuyerAgent") else None
    from aimo.buyer import agent as agent_mod

    real_cls = agent_mod.BuyerAgent

    class SpyAgent(real_cls):
        def __init__(self, *args, **kwargs):
            captured_now.append(kwargs.get("now"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "BuyerAgent", SpyAgent)
    assert orig_agent is None

    ex = {
        "id": "m01",
        "segment": "hotel",
        "lang": "en",
        "request": "We need 20 kg of lactose-free butter by Monday.",
        "customer_id": "hotel-aurora",
        "gold": {"acceptable_product_ids": [["402105"]], "qty_range": [[20, 22]], "must_escalate": False, "forbidden_allergens": []},
    }
    ns_mcp = types.SimpleNamespace(
        transport="mcp", base_url="http://replay.local:8000", model=None, thinking="LOW", no_escalate=False, approval_timeout=5
    )
    row_mcp = eval_gold.run_one(ex, ns_mcp, lambda pid: {})
    assert row_mcp["status"] == "placed"
    assert healthz_calls == ["http://replay.local:8000/healthz"]
    assert captured_now == [datetime(2026, 11, 21, 10, 0, tzinfo=HELSINKI)]

    ns_fake = types.SimpleNamespace(
        transport="fake", base_url="http://replay.local:8000", model=None, thinking="LOW", no_escalate=False, approval_timeout=5
    )
    row_fake = eval_gold.run_one(ex, ns_fake, lambda pid: {})
    assert row_fake["status"] == "placed"
    assert healthz_calls == ["http://replay.local:8000/healthz"]
    assert captured_now[-1] is None


def test_run_one_with_inproc_transport_uses_simulated_clock_when_aimo_now_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_OFFLINE", raising=False)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    _set_gemini_keys(monkeypatch, "", "")

    def fail_healthz(*args, **kwargs):
        raise AssertionError("/healthz must not be queried for inproc transport")

    monkeypatch.setattr(httpx, "get", fail_healthz)

    assert clock.simulated() is True
    expected_now = clock.now().astimezone(HELSINKI)
    assert expected_now == datetime(2026, 11, 14, 23, 50, 28, tzinfo=HELSINKI)
    assert _now(None, "inproc") == expected_now
    assert _now(None, "inproc").tzinfo == HELSINKI

    from aimo.buyer import agent as agent_mod
    from aimo.buyer.fakes import FakeGateway
    from aimo.buyer.tools import FakeTransport

    monkeypatch.setattr(
        "aimo.buyer.tools.connect",
        lambda kind, *, agent_id, customer_id, **kw: FakeTransport(FakeGateway(customer_id, agent_id)),
    )
    captured_now: list[datetime | None] = []
    real_cls = agent_mod.BuyerAgent

    class SpyAgent(real_cls):
        def __init__(self, *args, **kwargs):
            captured_now.append(kwargs.get("now"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "BuyerAgent", SpyAgent)

    ex = {
        "id": "ip01",
        "segment": "hotel",
        "lang": "en",
        "request": "We need 20 kg of lactose-free butter by Monday.",
        "customer_id": "hotel-aurora",
        "gold": {"acceptable_product_ids": [["402105"]], "qty_range": [[20, 22]], "must_escalate": False, "forbidden_allergens": []},
    }
    ns_inproc = types.SimpleNamespace(
        gold=["gold.jsonl"],
        transport="inproc",
        base_url="http://replay.local:8000",
        model=None,
        thinking="LOW",
        no_escalate=False,
        approval_timeout=5,
    )
    row_sim = eval_gold.run_one(ex, ns_inproc, lambda pid: {})
    assert row_sim["status"] == "placed"
    assert captured_now == [expected_now]
    assert resolve_date("by Monday", captured_now[0])[0] == date(2026, 11, 16)

    out_report = tmp_path / "inproc_report"
    eval_gold.write_report(out_report, [row_sim], eval_gold.summarize([row_sim]), ns_inproc, "2026-11-15 03:00", None)
    report_txt = (out_report / "report.md").read_text(encoding="utf-8")
    assert "AIMO_NOW=replay (" in report_txt

    monkeypatch.setenv("AIMO_REPLAY", "0")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    assert clock.simulated() is False
    assert _now(None, "inproc") is None

    row_real = eval_gold.run_one(ex, ns_inproc, lambda pid: {})
    assert row_real["status"] == "placed"
    assert captured_now[-1] is None


def test_eval_gold_main_keyless_fake_transport_and_report_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_jsonl = _write_gold_fixture(tmp_path / "gold_small.jsonl")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    _set_gemini_keys(monkeypatch, "", "")
    gemini.client.cache_clear()

    def forbid_gemini(*args, **kwargs):
        raise AssertionError("Gemini must not be called in keyless eval_gold")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    assert mock_wanted() is True

    out_keyless = tmp_path / "out_keyless"
    rc = eval_gold.main([str(fixture_jsonl), "--transport", "fake", "--concurrency", "2", "--out", str(out_keyless)])
    assert rc == 0

    report_keyless = (out_keyless / "report.md").read_text(encoding="utf-8")
    results_keyless = json.loads((out_keyless / "results.json").read_text(encoding="utf-8"))

    assert f"model {MOCK_MODEL_NAME} thinking LOW" in report_keyless
    assert "gemini-3.8-flash" not in report_keyless
    assert results_keyless["model"] == MOCK_MODEL_NAME
    assert results_keyless["cost"]["calls"] == 0
    assert results_keyless["summary"]["errors"] == 0
    assert results_keyless["summary"]["overall"]["n"] == 3
    assert results_keyless["summary"]["overall"]["zero_correction"] == 1.0
    assert results_keyless["summary"]["overall"]["escalation_match"] == 1.0
    assert results_keyless["summary"]["overall"]["allergen_safe"] == 1.0

    gemini_calls: list[tuple[str, str]] = []

    def stub_gemini_generate(feature: str, model: str, contents, config=None, run_id: str = "", retries: int = 5, meta=None):
        gemini_calls.append((feature, model))
        if feature == "buyer.parse_need":
            payload = {
                "items": [
                    {
                        "product_query": "lactose-free butter",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "excluded_allergens": [],
                        "quantity": 20,
                        "unit": "KG",
                        "delivery_by": "2026-11-16",
                        "delivery_expression": "by Monday",
                        "site": "",
                        "notes": "",
                    }
                ]
            }
            return genai_types.GenerateContentResponse(
                candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(text=json.dumps(payload))]))]
            )
        if feature in ("buyer.explain", "buyer.summary"):
            return genai_types.GenerateContentResponse(
                candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(text="Stub Gemini summary.")]))]
            )
        done = [
            p.function_response.name
            for c in contents
            for p in (getattr(c, "parts", None) or [])
            if getattr(p, "function_response", None)
        ]
        if "plan_need" not in done:
            fc = genai_types.FunctionCall(
                name="plan_need",
                args={
                    "need": {
                        "product_query": "lactose-free butter",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "quantity": 20,
                        "unit": "KG",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
            return genai_types.GenerateContentResponse(
                candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(function_call=fc)]))]
            )
        if "create_quote" not in done:
            fc = genai_types.FunctionCall(
                name="create_quote",
                args={"delivery_date": "2026-11-16", "lines": [{"product_id": "402105", "qty_sales_units": 40, "tier": "best"}]},
            )
            return genai_types.GenerateContentResponse(
                candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(function_call=fc)]))]
            )
        if "place_order" not in done:
            quote_resp = next(
                p.function_response.response
                for c in reversed(contents)
                for p in (getattr(c, "parts", None) or [])
                if getattr(p, "function_response", None) and p.function_response.name == "create_quote"
            )
            fc = genai_types.FunctionCall(
                name="place_order",
                args={"quote_id": quote_resp["quote_id"], "quote_hash": quote_resp["quote_hash"]},
            )
            return genai_types.GenerateContentResponse(
                candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(function_call=fc)]))]
            )
        return genai_types.GenerateContentResponse(
            candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[genai_types.Part(text="Done.")]))]
        )

    _set_gemini_keys(monkeypatch, "fake-live-gemini-key")
    monkeypatch.setattr("aimo.gemini.generate", stub_gemini_generate)
    assert mock_wanted() is False

    out_keyed = tmp_path / "out_keyed"
    rc_keyed = eval_gold.main([str(fixture_jsonl), "--transport", "fake", "--limit", "1", "--out", str(out_keyed)])
    assert rc_keyed == 0
    assert len(gemini_calls) >= 4

    report_keyed = (out_keyed / "report.md").read_text(encoding="utf-8")
    results_keyed = json.loads((out_keyed / "results.json").read_text(encoding="utf-8"))

    assert "model gemini-3.8-flash thinking LOW" in report_keyed
    assert MOCK_MODEL_NAME not in report_keyed
    assert results_keyed["model"] == "gemini-3.8-flash"
    assert results_keyed["summary"]["errors"] == 0
    assert results_keyed["summary"]["overall"]["zero_correction"] == 1.0
