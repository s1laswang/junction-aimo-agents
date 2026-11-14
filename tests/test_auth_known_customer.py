"""Known-customer and agent-key enforcement tests across HTTP /v1, catalog search, extras, buyer runs and MCP."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import runs as buyer_runs
from aimo.gateway import auth, human_auth, service
from aimo.models import Need, Plan, PlanLine, Product, ProductSummary

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
D = date(2026, 11, 16)
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_UNKNOWN = {"X-Customer-Id": "rogue-corp", "X-Agent-Id": "buyer-1"}


class FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self):
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))

    def _sel(self, raw, ids):
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids, requested_date, customer=None):
        return self._sel(self.price_raw, ids)

    def availability(self, ids, requested_date, customer=None):
        return self._sel(self.avail_raw, ids)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_ID", raising=False)
    human_auth.reset_for_tests()
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)


@pytest.fixture
def anon_api():
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False))


@pytest.fixture
def human_api():
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"})


def _stub_plan(need: Need) -> Plan:
    line = PlanLine(
        product_id="464306",
        product_name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        tier="best",
        qty_sales_units=need.quantity,
        sales_unit="ST",
        covered_amount=need.quantity,
        unit_price=1.01,
        line_total=round(need.quantity * 1.01, 2),
        reasons=["stubbed for auth test"],
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


def _stub_catalog():
    import types

    prod = Product(
        id="464306",
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )
    summary = ProductSummary(
        id="464306",
        name="Valio Eila lactose free semi-skimmed milk drink 1 l",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
    )
    return types.SimpleNamespace(
        search=lambda q, **kw: [summary],
        get_product=lambda pid: prod if str(pid) == "464306" else None,
        plan_need=_stub_plan,
    )


def test_auth_helpers_and_policy_dir_override(monkeypatch):
    from aimo.trust import policy as trust_policy

    known = auth.known_customer_ids()
    assert {"hotel-aurora", "school-tampere", "hospital-meilahti"} <= known
    assert auth.is_known_customer("hotel-aurora") is True
    assert auth.is_known_customer("  school-tampere  ") is True
    assert auth.is_known_customer(None) is False
    assert auth.is_known_customer("") is False
    assert auth.is_known_customer("   ") is False
    assert auth.is_known_customer("rogue-corp") is False
    assert auth.require_known_customer("  hotel-aurora ") == "hotel-aurora"

    with pytest.raises(service.ServiceError) as missing_exc:
        auth.require_known_customer("   ")
    assert missing_exc.value.status == 401 and missing_exc.value.code == "identity_required"

    with pytest.raises(service.ServiceError) as unk_exc:
        auth.require_known_customer("rogue-corp")
    assert unk_exc.value.status == 403 and unk_exc.value.code == "unknown_customer"
    assert "hotel-aurora" in unk_exc.value.detail["known_customers"]

    monkeypatch.setattr(trust_policy, "POLICY_DIR", ROOT / "toywholesaler" / "policies")
    trust_policy._load_policy_cached.cache_clear()
    try:
        toy_known = auth.known_customer_ids()
        assert toy_known == {"hotel-aurora", "hospital-meilahti"}
        assert auth.is_known_customer("school-tampere") is False
    finally:
        trust_policy._load_policy_cached.cache_clear()


def test_fallback_mandate_for_rejects_unknown_customer(monkeypatch):
    monkeypatch.setattr(service, "_trust", lambda: None)
    assert auth.known_customer_ids() == {"hotel-aurora", "school-tampere", "hospital-meilahti"}
    m = service.mandate_for(service.Identity(agent_id="buyer-1", customer_id="hotel-aurora"))
    assert m.customer_id == "hotel-aurora"
    with pytest.raises(service.ServiceError) as exc:
        service.mandate_for(service.Identity(agent_id="buyer-1", customer_id="rogue-corp"))
    assert exc.value.status == 403 and exc.value.code == "unknown_customer"


def test_resolve_customer_identity_matrix():
    with pytest.raises(service.ServiceError) as e1:
        auth.resolve_customer_identity(require_headers=True, x_customer_id="hotel-aurora")
    assert e1.value.status == 401 and e1.value.code == "identity_required"

    with pytest.raises(service.ServiceError) as e2:
        auth.resolve_customer_identity()
    assert e2.value.status == 401 and e2.value.code == "identity_required"

    with pytest.raises(service.ServiceError) as e3:
        auth.resolve_customer_identity(x_customer_id="rogue-corp", body_customer_id="hotel-aurora")
    assert e3.value.status == 403 and e3.value.code == "unknown_customer"

    with pytest.raises(service.ServiceError) as e4:
        auth.resolve_customer_identity(x_customer_id="school-tampere", body_customer_id="hotel-aurora")
    assert e4.value.status == 403 and e4.value.code == "customer_mismatch"

    deferred = auth.resolve_customer_identity(
        x_customer_id="valio-aimo",
        x_agent_id="valio-aimo/seller-1",
        require_headers=True,
        defer_unknown_customer=True,
    )
    assert deferred.customer_id == "valio-aimo"

    ok = auth.resolve_customer_identity(
        x_customer_id="hotel-aurora",
        body_customer_id="hotel-aurora",
        x_agent_id="buyer-1",
        body_agent_id="hotel-aurora/buyer-1",
        require_headers=True,
    )
    assert ok.customer_id == "hotel-aurora" and ok.agent_id == "buyer-1"
    assert getattr(ok, "authenticated") == {"customer": True, "agent": True}


def test_catalog_search_and_product_detail_auth(anon_api, monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)

    r_anon = anon_api.get("/v1/products/search", params={"q": "milk"})
    assert r_anon.status_code == 401 and r_anon.json()["error"] == "identity_required"

    r_unk_h = anon_api.get("/v1/products/search", params={"q": "milk"}, headers=H_UNKNOWN)
    assert r_unk_h.status_code == 403 and r_unk_h.json()["error"] == "unknown_customer"

    r_unk_q = anon_api.get("/v1/products/search", params={"q": "milk", "customer_id": "rogue-corp"})
    assert r_unk_q.status_code == 403 and r_unk_q.json()["error"] == "unknown_customer"

    r_mis = anon_api.get(
        "/v1/products/search",
        params={"q": "milk", "customer_id": "hotel-aurora"},
        headers=H_SCHOOL,
    )
    assert r_mis.status_code == 403 and r_mis.json()["error"] == "customer_mismatch"

    r_ok_h = anon_api.get("/v1/products/search", params={"q": "milk"}, headers=H_AURORA)
    assert r_ok_h.status_code == 200 and len(r_ok_h.json()) == 1

    r_ok_q = anon_api.get("/v1/products/search", params={"q": "milk", "customer_id": "hotel-aurora"})
    assert r_ok_q.status_code == 200 and len(r_ok_q.json()) == 1

    assert anon_api.get("/v1/products/464306").status_code == 200
    assert anon_api.get("/v1/products/464306", headers=H_AURORA).status_code == 200
    r_prod_unk = anon_api.get("/v1/products/464306", headers=H_UNKNOWN)
    assert r_prod_unk.status_code == 403 and r_prod_unk.json()["error"] == "unknown_customer"


def test_offers_and_plan_known_customer_enforcement(anon_api, monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)

    offers_body = {"product_ids": ["464306"], "requested_date": D.isoformat()}
    assert anon_api.post("/v1/offers", json=offers_body).status_code == 401
    r_off_unk = anon_api.post("/v1/offers", headers=H_UNKNOWN, json=offers_body)
    assert r_off_unk.status_code == 403 and r_off_unk.json()["error"] == "unknown_customer"
    r_off_body_unk = anon_api.post("/v1/offers", json={**offers_body, "customer_id": "rogue-corp"})
    assert r_off_body_unk.status_code == 403 and r_off_body_unk.json()["error"] == "unknown_customer"
    r_off_mis = anon_api.post("/v1/offers", headers=H_SCHOOL, json={**offers_body, "customer_id": "hotel-aurora"})
    assert r_off_mis.status_code == 403 and r_off_mis.json()["error"] == "customer_mismatch"
    assert anon_api.post("/v1/offers", headers=H_AURORA, json=offers_body).status_code == 200
    assert anon_api.post("/v1/offers", json={**offers_body, "customer_id": "hotel-aurora"}).status_code == 200

    with pytest.raises(service.ServiceError) as off_svc:
        service.fetch_offers(["464306"], D, customer_id="rogue-corp")
    assert off_svc.value.status == 403 and off_svc.value.code == "unknown_customer"

    base_need = {"product_query": "lactose-free milk", "quantity": 10, "unit": "L", "delivery_by": D.isoformat()}
    r_plan_blank = anon_api.post("/v1/plan", json={**base_need, "customer_id": ""})
    assert r_plan_blank.status_code in (401, 422)
    if r_plan_blank.status_code == 401:
        assert r_plan_blank.json()["error"] == "identity_required"
    r_plan_unk = anon_api.post("/v1/plan", json={**base_need, "customer_id": "rogue-corp"})
    assert r_plan_unk.status_code == 403 and r_plan_unk.json()["error"] == "unknown_customer"
    r_plan_mis = anon_api.post("/v1/plan", headers=H_SCHOOL, json={**base_need, "customer_id": "hotel-aurora"})
    assert r_plan_mis.status_code == 403 and r_plan_mis.json()["error"] == "customer_mismatch"
    r_plan_ok = anon_api.post("/v1/plan", headers=H_AURORA, json={**base_need, "customer_id": "hotel-aurora"})
    assert r_plan_ok.status_code == 200 and r_plan_ok.json()["need"]["customer_id"] == "hotel-aurora"

    with pytest.raises(service.ServiceError) as plan_svc:
        service.plan_need({**base_need, "customer_id": "rogue-corp"})
    assert plan_svc.value.status == 403 and plan_svc.value.code == "unknown_customer"


def test_quotes_orders_approvals_and_learned_enforcement(anon_api, human_api, monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)

    q_body = {"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 10}]}
    assert anon_api.post("/v1/quotes", json=q_body).status_code == 401
    assert anon_api.post("/v1/quotes", headers={"X-Customer-Id": "hotel-aurora"}, json=q_body).status_code == 401

    r_q_unk = anon_api.post("/v1/quotes", headers=H_UNKNOWN, json=q_body)
    assert r_q_unk.status_code == 403 and r_q_unk.json()["error"] == "unknown_customer"

    r_q_unk_mis = anon_api.post("/v1/quotes", headers=H_UNKNOWN, json={**q_body, "customer_id": "hotel-aurora"})
    assert r_q_unk_mis.status_code == 403 and r_q_unk_mis.json()["error"] == "unknown_customer"

    r_q_mis = anon_api.post("/v1/quotes", headers=H_SCHOOL, json={**q_body, "customer_id": "hotel-aurora"})
    assert r_q_mis.status_code == 403 and r_q_mis.json()["error"] == "customer_mismatch"

    r_q_agent_mis = anon_api.post("/v1/quotes", headers=H_AURORA, json={**q_body, "agent_id": "planner-9"})
    assert r_q_agent_mis.status_code == 403 and r_q_agent_mis.json()["error"] == "agent_mismatch"

    q = anon_api.post("/v1/quotes", headers=H_AURORA, json=q_body).json()
    o_body = {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}

    assert anon_api.post("/v1/orders", json=o_body).status_code == 401

    r_seller_mis = anon_api.post(
        "/v1/orders",
        headers={"X-Customer-Id": "valio-aimo", "X-Agent-Id": "valio-aimo/seller-1"},
        json=o_body,
    )
    assert r_seller_mis.status_code == 403 and r_seller_mis.json()["error"] == "customer_mismatch"

    r_unk_missing_q = anon_api.post(
        "/v1/orders",
        headers=H_UNKNOWN,
        json={"quote_id": "Q-nonexistent", "quote_hash": "0" * 64},
    )
    assert r_unk_missing_q.status_code == 403 and r_unk_missing_q.json()["error"] == "unknown_customer"

    r_known_missing_q = anon_api.post(
        "/v1/orders",
        headers=H_AURORA,
        json={"quote_id": "Q-nonexistent", "quote_hash": "0" * 64},
    )
    assert r_known_missing_q.status_code == 404 and r_known_missing_q.json()["error"] == "quote_not_found"

    r_order_ok = anon_api.post("/v1/orders", headers=H_AURORA, json=o_body)
    assert r_order_ok.status_code == 200

    assert anon_api.get("/v1/approvals").status_code == 401
    r_app_unk = anon_api.get("/v1/approvals", headers=H_UNKNOWN)
    assert r_app_unk.status_code == 403 and r_app_unk.json()["error"] == "unknown_customer"
    r_app_mis = anon_api.get("/v1/approvals", params={"customer_id": "hotel-aurora"}, headers=H_SCHOOL)
    assert r_app_mis.status_code == 403 and r_app_mis.json()["error"] == "customer_mismatch"
    assert anon_api.get("/v1/approvals", headers=H_AURORA).status_code == 200
    assert human_api.get("/v1/approvals").status_code == 200

    r_lrn_unk = anon_api.get("/v1/customers/rogue-corp/learned")
    assert r_lrn_unk.status_code == 403 and r_lrn_unk.json()["error"] == "unknown_customer"
    r_lrn_hdr_unk = anon_api.get("/v1/customers/hotel-aurora/learned", headers=H_UNKNOWN)
    assert r_lrn_hdr_unk.status_code == 403 and r_lrn_hdr_unk.json()["error"] == "unknown_customer"
    r_lrn_mis = anon_api.get("/v1/customers/hotel-aurora/learned", headers=H_SCHOOL)
    assert r_lrn_mis.status_code == 403 and r_lrn_mis.json()["error"] == "customer_mismatch"
    assert anon_api.get("/v1/customers/hotel-aurora/learned", headers=H_AURORA).status_code == 200


def test_extras_recipes_proposals_and_drift_rebaseline(anon_api, monkeypatch):
    import aimo.agents.drift as drift_mod
    import aimo.agents.seller as seller_mod
    import aimo.recipes as recipes_mod

    monkeypatch.setattr(
        recipes_mod,
        "plan_recipe",
        lambda recipe_id, portions, delivery_by, customer_id, **kw: {
            "customer_id": customer_id,
            "recipe": {"id": recipe_id or "lasagne"},
            "ingredients": [],
            "quote_request": {"delivery_date": D.isoformat(), "lines": []},
        },
    )
    monkeypatch.setattr(
        drift_mod,
        "check_drift",
        lambda customer_id, rebaseline=False, **kw: {"customer_id": customer_id, "rebaseline": rebaseline, "flags": []},
    )

    rec_body = {"recipe_id": "lasagne", "portions": 20, "delivery_by": D.isoformat()}
    assert anon_api.post("/v1/recipes/plan", json=rec_body).status_code == 401
    r_rec_unk = anon_api.post("/v1/recipes/plan", headers=H_UNKNOWN, json=rec_body)
    assert r_rec_unk.status_code == 403 and r_rec_unk.json()["error"] == "unknown_customer"
    r_rec_mis = anon_api.post("/v1/recipes/plan", headers=H_SCHOOL, json={**rec_body, "customer_id": "hotel-aurora"})
    assert r_rec_mis.status_code == 403 and r_rec_mis.json()["error"] == "customer_mismatch"
    r_rec_ok = anon_api.post("/v1/recipes/plan", headers=H_AURORA, json=rec_body)
    assert r_rec_ok.status_code == 200 and r_rec_ok.json()["customer_id"] == "hotel-aurora"

    assert anon_api.get("/v1/drift/nobody").status_code == 404
    assert anon_api.get("/v1/drift/hotel-aurora").status_code == 200
    assert anon_api.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}).status_code == 401
    r_dr_unk = anon_api.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}, headers=H_UNKNOWN)
    assert r_dr_unk.status_code == 403 and r_dr_unk.json()["error"] == "unknown_customer"
    r_dr_path_unk = anon_api.get(
        "/v1/drift/rogue-corp",
        params={"rebaseline": "true"},
        headers={"X-Customer-Id": "hotel-aurora"},
    )
    assert r_dr_path_unk.status_code == 403 and r_dr_path_unk.json()["error"] == "unknown_customer"
    r_dr_mis = anon_api.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}, headers=H_SCHOOL)
    assert r_dr_mis.status_code == 403 and r_dr_mis.json()["error"] == "customer_mismatch"
    r_dr_ok = anon_api.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}, headers=H_AURORA)
    assert r_dr_ok.status_code == 200 and r_dr_ok.json()["rebaseline"] is True

    assert anon_api.post("/v1/proposals", json={"customer_id": "hotel-aurora"}).status_code == 401
    r_prop_buyer = anon_api.post("/v1/proposals", headers=H_AURORA, json={"customer_id": "hotel-aurora"})
    assert r_prop_buyer.status_code == 403 and r_prop_buyer.json()["error"] == "seller_required"
    h_seller = {"X-Agent-Id": "valio-aimo/seller-1"}
    r_prop_unk = anon_api.post("/v1/proposals", headers=h_seller, json={"customer_id": "rogue-corp"})
    assert r_prop_unk.status_code == 403 and r_prop_unk.json()["error"] == "unknown_customer"

    seq = {"n": 0}

    def _fake_propose(customer_id, **kw):
        seq["n"] += 1
        prop = {
            "proposal_id": f"P-test-{seq['n']}",
            "customer_id": customer_id,
            "seller_id": "valio-aimo/seller-1",
            "status": "proposed",
            "created_at": "2026-11-14T12:00:00+00:00",
            "quote_request": {"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
        }
        seller_mod._save(prop)
        return prop

    monkeypatch.setattr(seller_mod, "propose_offer", _fake_propose)
    monkeypatch.setattr(service, "_catalog", _stub_catalog)

    r_prop_ok = anon_api.post("/v1/proposals", headers=h_seller, json={"customer_id": "hotel-aurora"})
    assert r_prop_ok.status_code == 200
    pid = r_prop_ok.json()["proposal_id"]

    assert anon_api.post(f"/v1/proposals/{pid}/accept").status_code == 401
    assert anon_api.post(f"/v1/proposals/{pid}/accept", headers={"X-Customer-Id": "hotel-aurora"}).status_code == 401
    r_acc_seller = anon_api.post(
        f"/v1/proposals/{pid}/accept",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "valio-aimo/seller-1"},
    )
    assert r_acc_seller.status_code == 403 and r_acc_seller.json()["error"] == "seller_cannot_order"
    r_acc_unk = anon_api.post(f"/v1/proposals/{pid}/accept", headers=H_UNKNOWN)
    assert r_acc_unk.status_code == 403 and r_acc_unk.json()["error"] == "unknown_customer"
    r_acc_mis = anon_api.post(f"/v1/proposals/{pid}/accept", headers=H_SCHOOL)
    assert r_acc_mis.status_code == 403 and r_acc_mis.json()["error"] == "customer_mismatch"

    assert anon_api.post(f"/v1/proposals/{pid}/decline", json={}).status_code == 401
    r_dec_seller = anon_api.post(
        f"/v1/proposals/{pid}/decline",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "valio-aimo/seller-1"},
        json={},
    )
    assert r_dec_seller.status_code == 403 and r_dec_seller.json()["error"] == "seller_cannot_order"
    r_dec_unk = anon_api.post(f"/v1/proposals/{pid}/decline", headers=H_UNKNOWN, json={})
    assert r_dec_unk.status_code == 403 and r_dec_unk.json()["error"] == "unknown_customer"
    r_dec_ok = anon_api.post(f"/v1/proposals/{pid}/decline", headers=H_AURORA, json={"reason": "not needed"})
    assert r_dec_ok.status_code == 200 and r_dec_ok.json()["status"] == "declined"

    pid2 = anon_api.post("/v1/proposals", headers=h_seller, json={"customer_id": "hotel-aurora"}).json()["proposal_id"]
    r_acc_ok = anon_api.post(f"/v1/proposals/{pid2}/accept", headers=H_AURORA)
    assert r_acc_ok.status_code == 200 and r_acc_ok.json()["proposal"]["status"] == "accepted"


def test_buyer_runs_bare_app_and_monkeypatched_start(monkeypatch):
    reg = buyer_runs.RunRegistry()
    monkeypatch.setattr(reg, "_execute", lambda run, *a, **kw: setattr(run, "status", "finished"))
    monkeypatch.setattr(buyer_runs, "registry", reg)

    bare = FastAPI()
    bare.include_router(buyer_runs.router)
    client = TestClient(bare)

    r_no_id = client.post("/v1/agent/runs", json={"text": "20 L milk"})
    assert r_no_id.status_code == 401 and r_no_id.json()["error"] == "identity_required"

    r_form_no_id = client.post("/v1/agent/runs", data={"text": "20 L milk"}, files={"x": ("note.txt", b"hi", "text/plain")})
    assert r_form_no_id.status_code == 401 and r_form_no_id.json()["error"] == "identity_required"

    r_mis = client.post(
        "/v1/agent/runs",
        headers={"X-Customer-Id": "school-tampere"},
        json={"text": "20 L milk", "customer_id": "hotel-aurora"},
    )
    assert r_mis.status_code == 403 and r_mis.json()["error"] == "customer_mismatch"

    r_unk = client.post("/v1/agent/runs", json={"text": "20 L milk", "customer_id": "rogue-corp"})
    assert r_unk.status_code == 403 and r_unk.json()["error"] == "unknown_customer"

    r_fake_transport = client.post(
        "/v1/agent/runs",
        json={"text": "20 L milk", "customer_id": "cafe-x", "transport": "fake"},
    )
    assert r_fake_transport.status_code == 200

    r_hdr_only = client.post(
        "/v1/agent/runs",
        headers={"X-Customer-Id": "hotel-aurora"},
        json={"text": "20 L milk"},
    )
    assert r_hdr_only.status_code == 200 and r_hdr_only.json()["run_id"].startswith("run-")

    orig_start = reg.start
    monkeypatch.setattr(reg, "start", lambda req, **kw: orig_start(req, **kw))
    r_patched = client.post("/v1/agent/runs", json={"text": "20 L milk", "customer_id": "cafe-x"})
    assert r_patched.status_code == 200


def test_mcp_tools_reject_unknown_customer(monkeypatch):
    from mcp import Client

    from aimo.gateway.mcp_server import build_mcp

    monkeypatch.setattr(service, "_catalog", _stub_catalog)

    async def call_tool(mcp_srv, name: str, args: dict):
        async with Client(mcp_srv) as c:
            try:
                res = await c.call_tool(name, args)
            except Exception as exc:
                return True, str(exc)
            is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
            sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
            txt = json.dumps(sc) if sc else " ".join(getattr(x, "text", "") for x in res.content)
            return is_err, txt

    srv = build_mcp()
    err0, txt0 = asyncio.run(call_tool(srv, "search_products", {"query": "milk"}))
    assert not err0 and "products" in txt0

    err_strict, txt_strict = asyncio.run(
        call_tool(
            srv,
            "create_quote",
            {"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 1}]},
        )
    )
    assert err_strict and "identity_required" in txt_strict

    err_need_unk, txt_need_unk = asyncio.run(
        call_tool(
            srv,
            "plan_need",
            {
                "need": {
                    "customer_id": "rogue-corp",
                    "product_query": "milk",
                    "quantity": 10,
                    "unit": "L",
                    "delivery_by": D.isoformat(),
                }
            },
        )
    )
    assert err_need_unk and "unknown_customer" in txt_need_unk

    err_prop_unk, txt_prop_unk = asyncio.run(call_tool(srv, "propose_offer", {"customer_id": "rogue-corp"}))
    assert err_prop_unk and "unknown_customer" in txt_prop_unk

    err_drift_arg_unk, txt_drift_arg_unk = asyncio.run(
        call_tool(srv, "check_standing_order_drift", {"customer_id": "rogue-corp"})
    )
    assert err_drift_arg_unk and "unknown_customer" in txt_drift_arg_unk

    monkeypatch.setenv("AIMO_CUSTOMER_ID", "rogue-corp")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    for tool_name, tool_args in [
        ("search_products", {"query": "milk"}),
        ("get_offers", {"product_ids": ["464306"], "requested_date": D.isoformat()}),
        ("create_quote", {"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 1}]}),
        ("place_order", {"quote_id": "Q-1", "quote_hash": "0" * 64}),
        ("list_policies", {}),
        ("plan_recipe", {"recipe_id": "lasagne", "portions": 10, "delivery_by": D.isoformat()}),
        ("check_standing_order_drift", {}),
    ]:
        is_err, text = asyncio.run(call_tool(srv, tool_name, tool_args))
        assert is_err and "unknown_customer" in text, (tool_name, is_err, text)


def test_agent_keys_json_and_csv_formats(anon_api, monkeypatch):
    monkeypatch.setattr(service, "_catalog", _stub_catalog)
    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"hotel-aurora": "aurora-secret-1"}))

    r_missing = anon_api.get("/v1/products/search", params={"q": "milk"}, headers=H_AURORA)
    assert r_missing.status_code == 401 and r_missing.json()["error"] == "agent_key_required"

    r_wrong = anon_api.get(
        "/v1/products/search",
        params={"q": "milk"},
        headers={**H_AURORA, "X-Agent-Key": "wrong-key"},
    )
    assert r_wrong.status_code == 403 and r_wrong.json()["error"] == "invalid_agent_key"

    r_right = anon_api.get(
        "/v1/products/search",
        params={"q": "milk"},
        headers={**H_AURORA, "X-Agent-Key": "aurora-secret-1"},
    )
    assert r_right.status_code == 200

    r_other = anon_api.get("/v1/products/search", params={"q": "milk"}, headers=H_SCHOOL)
    assert r_other.status_code == 200

    monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora:k-aurora,*:k-wildcard")
    assert anon_api.get("/v1/products/search", params={"q": "milk"}, headers=H_SCHOOL).status_code == 401
    assert (
        anon_api.get(
            "/v1/products/search",
            params={"q": "milk"},
            headers={**H_SCHOOL, "X-Agent-Key": "k-wildcard"},
        ).status_code
        == 200
    )
    assert (
        anon_api.get(
            "/v1/products/search",
            params={"q": "milk"},
            headers={**H_AURORA, "X-Agent-Key": "k-wildcard"},
        ).status_code
        == 403
    )
    assert (
        anon_api.get(
            "/v1/products/search",
            params={"q": "milk"},
            headers={**H_AURORA, "X-Agent-Key": "k-aurora"},
        ).status_code
        == 200
    )

    r_run_missing = anon_api.post("/v1/agent/runs", json={"text": "10 L milk", "customer_id": "hotel-aurora"})
    assert r_run_missing.status_code == 401 and r_run_missing.json()["error"] == "agent_key_required"
    r_run_wrong = anon_api.post(
        "/v1/agent/runs",
        headers={"X-Agent-Key": "wrong"},
        json={"text": "10 L milk", "customer_id": "hotel-aurora"},
    )
    assert r_run_wrong.status_code == 403 and r_run_wrong.json()["error"] == "invalid_agent_key"
