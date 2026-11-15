from __future__ import annotations

import json
import types

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import clock
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import human_auth, service
from aimo.gateway.mcp_server import _call, _dump, _identity
from aimo.models import Need
from test_gateway import APPROVER, FixtureClient, H

PAST = "2026-11-10T09:00:00+02:00"
DELIVERY = "2026-11-16"
MILK_NEED = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "required_claims": ["FREE_FROM_LACTOSE"],
    "quantity": 100,
    "unit": "L",
    "delivery_by": DELIVERY,
    "preferred_product_ids": ["463038"],
}


def _synthetic_catalog_products() -> list[dict]:
    def _item(pid: str, sales: str, name_en: str, brand: str, units: list[tuple[str, int]], conv: list[dict], marketing: str) -> dict:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": marketing}],
            },
        }

    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    return [
        _item(
            "463038",
            "KI",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            [("KI", 2), ("ST", 1)],
            [{"synkkaPackagingType": "CASE"}],
            "Valio Eila lactose free dispenser milk drink for hotels and restaurants.",
        ),
        _item(
            "464306",
            "ST",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            base_1l,
            "Valio Eila semi-skimmed milk drink is lactose free and suitable for cooking.",
        ),
        _item(
            "469363",
            "ST",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            [("ST", 1)],
            base_1l,
            "Arla lactose free semi-skimmed milk drink 1 L UHT.",
        ),
        _item(
            "462775",
            "ST",
            "Valio Eila whole milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            base_1l,
            "Valio Eila lactose free milk drink 1 L.",
        ),
    ]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    used_synth = not config.PRODUCT_JSON.exists()
    if used_synth:
        pjson = tmp_path / "valio_aimo_junction26_product_data.json"
        cdb = tmp_path / "catalog.sqlite"
        pjson.write_text(json.dumps(_synthetic_catalog_products()), encoding="utf-8")
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    human_auth.reset_for_tests()
    bc.set_client(FixtureClient())
    try:
        yield
    finally:
        human_auth.reset_for_tests()
        bc.set_client(None)
        if used_synth:
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def api():
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"})


def _mcp_ctx(headers: dict[str, str]):
    return types.SimpleNamespace(headers=headers)


def test_http_agent_ignores_now_and_faults_without_demo_faults(api, monkeypatch):
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    assert clock.request_overrides_allowed(True) is False

    r_now = api.post(
        "/v1/offers",
        headers={**H, "X-Aimo-Now": PAST},
        json={"product_ids": ["463038"], "requested_date": DELIVERY},
    )
    assert r_now.status_code == 200, r_now.text
    assert r_now.json()[0]["cutoff_passed"] is True
    assert r_now.json()[0]["available"] is False

    r_plan_now = api.post("/v1/plan", headers={**H, "X-Aimo-Now": PAST}, json=MILK_NEED)
    assert r_plan_now.status_code == 200, r_plan_now.text
    plan_now = r_plan_now.json()
    assert all(ln["product_id"] != "463038" for ln in plan_now["lines"])
    c_463 = next(c for c in plan_now["candidates"] if c["product"]["id"] == "463038")
    assert c_463["offer"]["cutoff_passed"] is True

    r_stock = api.post(
        "/v1/offers",
        headers={**H, "X-Aimo-Faults": "stockout:464306"},
        json={"product_ids": ["464306"], "requested_date": DELIVERY},
    )
    assert r_stock.status_code == 200, r_stock.text
    assert r_stock.json()[0]["available"] is True
    assert not any("injected" in i for i in r_stock.json()[0].get("issues", []))

    r_plan_faults = api.post(
        "/v1/plan",
        headers={**H, "X-Aimo-Faults": "stockout:464306"},
        json=MILK_NEED,
    )
    assert r_plan_faults.status_code == 200, r_plan_faults.text
    plan_stock = r_plan_faults.json()
    assert any(ln["product_id"] == "464306" for ln in plan_stock["lines"])

    r_plan_inj = api.post(
        "/v1/plan",
        headers={**H, "X-Aimo-Faults": "inject_text:464306"},
        json=MILK_NEED,
    )
    assert r_plan_inj.status_code == 200, r_plan_inj.text
    plan_inj = r_plan_inj.json()
    ln_464 = next(ln for ln in plan_inj["lines"] if ln["product_id"] == "464306")
    assert ln_464["injection_screen"]["suspicious"] is False
    assert "injected:inject_text" not in ln_464["flags"]
    assert "supplier_text_injection_suspected" not in ln_464["flags"]


def test_mcp_call_agent_ignores_now_and_faults_without_demo_faults(monkeypatch):
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)

    ctx_now = _mcp_ctx({**H, "X-Aimo-Now": PAST})
    offers_now = _call(ctx_now, service.get_offers, ["463038"], DELIVERY, identity=_identity(ctx_now))
    assert offers_now["offers"][0]["cutoff_passed"] is True
    assert offers_now["offers"][0]["available"] is False

    plan_now = _dump(_call(ctx_now, service.plan_need, Need(**MILK_NEED), identity=_identity(ctx_now)))
    assert all(ln["product_id"] != "463038" for ln in plan_now["lines"])

    ctx_stock = _mcp_ctx({**H, "X-Aimo-Faults": "stockout:464306"})
    offers_stock = _call(ctx_stock, service.get_offers, ["464306"], DELIVERY, identity=_identity(ctx_stock))
    assert offers_stock["offers"][0]["available"] is True
    assert not any("injected" in i for i in offers_stock["offers"][0].get("issues", []))

    plan_stock = _dump(_call(ctx_stock, service.plan_need, Need(**MILK_NEED), identity=_identity(ctx_stock)))
    assert any(ln["product_id"] == "464306" for ln in plan_stock["lines"])

    ctx_inj = _mcp_ctx({**H, "X-Aimo-Faults": "inject_text:464306"})
    plan_inj = _dump(_call(ctx_inj, service.plan_need, Need(**MILK_NEED), identity=_identity(ctx_inj)))
    ln_464 = next(ln for ln in plan_inj["lines"] if ln["product_id"] == "464306")
    assert ln_464["injection_screen"]["suspicious"] is False
    assert "injected:inject_text" not in ln_464["flags"]
    assert "supplier_text_injection_suspected" not in ln_464["flags"]


def test_demo_faults_env_and_operator_requests_allow_overrides_in_dev(api, monkeypatch):
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    assert clock.request_overrides_allowed(True) is True

    r_now = api.post(
        "/v1/offers",
        headers={**H, "X-Aimo-Now": PAST},
        json={"product_ids": ["463038"], "requested_date": DELIVERY},
    )
    assert r_now.status_code == 200, r_now.text
    assert r_now.json()[0]["cutoff_passed"] is False
    assert r_now.json()[0]["available"] is True

    r_plan_now = api.post("/v1/plan", headers={**H, "X-Aimo-Now": PAST}, json=MILK_NEED)
    assert r_plan_now.status_code == 200, r_plan_now.text
    assert any(ln["product_id"] == "463038" for ln in r_plan_now.json()["lines"])

    r_stock = api.post(
        "/v1/offers",
        headers={**H, "X-Aimo-Faults": "stockout:464306"},
        json={"product_ids": ["464306"], "requested_date": DELIVERY},
    )
    assert r_stock.status_code == 200, r_stock.text
    assert r_stock.json()[0]["available"] is False
    assert "injected:stockout" in r_stock.json()[0]["issues"]

    r_plan_inj = api.post(
        "/v1/plan",
        headers={**H, "X-Aimo-Faults": "inject_text:464306"},
        json=MILK_NEED,
    )
    assert r_plan_inj.status_code == 200, r_plan_inj.text
    ln_464 = next(ln for ln in r_plan_inj.json()["lines"] if ln["product_id"] == "464306")
    assert ln_464["injection_screen"]["suspicious"] is True
    assert "injected:inject_text" in ln_464["flags"]

    ctx_now = _mcp_ctx({**H, "X-Aimo-Now": PAST})
    mcp_offers_now = _call(ctx_now, service.get_offers, ["463038"], DELIVERY, identity=_identity(ctx_now))
    assert mcp_offers_now["offers"][0]["cutoff_passed"] is False

    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    assert clock.request_overrides_allowed(False) is True

    op_headers = {"X-Customer-Id": "hotel-aurora"}
    r_op_now = api.post(
        "/v1/offers",
        headers={**op_headers, "X-Aimo-Now": PAST},
        json={"product_ids": ["463038"], "requested_date": DELIVERY},
    )
    assert r_op_now.status_code == 200, r_op_now.text
    assert r_op_now.json()[0]["cutoff_passed"] is False

    r_op_stock = api.post(
        "/v1/offers",
        headers={**op_headers, "X-Aimo-Faults": "stockout:464306"},
        json={"product_ids": ["464306"], "requested_date": DELIVERY},
    )
    assert r_op_stock.status_code == 200, r_op_stock.text
    assert r_op_stock.json()[0]["available"] is False
    assert "injected:stockout" in r_op_stock.json()[0]["issues"]

    ctx_op = _mcp_ctx({**op_headers, "X-Aimo-Now": PAST})
    mcp_op_offers = _call(ctx_op, service.get_offers, ["463038"], DELIVERY, identity=_identity(ctx_op))
    assert mcp_op_offers["offers"][0]["cutoff_passed"] is False


def test_no_overrides_allowed_in_production(api, monkeypatch):
    monkeypatch.setenv("AIMO_ENV", "production")
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    assert clock.request_overrides_allowed(False) is False
    assert clock.request_overrides_allowed(True) is False

    for hdrs in ({"X-Customer-Id": "hotel-aurora"}, H):
        r_now = api.post(
            "/v1/offers",
            headers={**hdrs, "X-Aimo-Now": PAST},
            json={"product_ids": ["463038"], "requested_date": DELIVERY},
        )
        assert r_now.status_code == 200, r_now.text
        assert r_now.json()[0]["cutoff_passed"] is True

        r_stock = api.post(
            "/v1/offers",
            headers={**hdrs, "X-Aimo-Faults": "stockout:464306"},
            json={"product_ids": ["464306"], "requested_date": DELIVERY},
        )
        assert r_stock.status_code == 200, r_stock.text
        assert r_stock.json()[0]["available"] is True
        assert not any("injected" in i for i in r_stock.json()[0].get("issues", []))


def test_approver_session_and_decision_agent_refusal_audit_and_blank_header(api):
    r_refused = api.post(
        "/v1/auth/approver-session",
        headers={"X-Agent-Id": "  buyer-1  "},
        json={"approver": "anna@hotel-aurora.example"},
    )
    assert r_refused.status_code == 403, r_refused.text
    assert r_refused.json()["error"] == "agent_cannot_approve"

    events = api.get("/v1/audit", params={"subject_id": "auth:approver-session"}).json()
    refusals = [
        e for e in events if e.get("action") == "approval.refused" and e.get("actor") == "agent:buyer-1"
    ]
    assert len(refusals) == 1
    assert "approver sign-in endpoint" in refusals[0].get("summary", "")
    assert api.get("/v1/audit/verify").json()["ok"] is True

    r_blank = api.post(
        "/v1/auth/approver-session",
        headers={"X-Agent-Id": "   ", "X-Approver-Pin": "test-pin"},
        json={"approver": "anna@hotel-aurora.example"},
    )
    assert r_blank.status_code == 200, r_blank.text
    assert r_blank.json()["approver"] == "anna@hotel-aurora.example"
    assert r_blank.json()["session"]

    q = api.post(
        "/v1/quotes",
        headers=H,
        json={"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
    ).json()
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"

    r_dec_agent = api.post(
        f"/v1/approvals/{o['order_id']}/decision",
        headers={"X-Agent-Id": "  buyer-1  "},
        json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER},
    )
    assert r_dec_agent.status_code == 403, r_dec_agent.text
    assert r_dec_agent.json()["error"] == "agent_cannot_approve"
    dec_events = api.get("/v1/audit", params={"subject_id": o["order_id"]}).json()
    assert any(e.get("action") == "approval.refused" and e.get("actor") == "agent:buyer-1" for e in dec_events)

    r_dec_blank = api.post(
        f"/v1/approvals/{o['order_id']}/decision",
        headers={"X-Agent-Id": "   ", "X-Approver-Pin": "test-pin"},
        json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER},
    )
    assert r_dec_blank.status_code == 200, r_dec_blank.text
    assert r_dec_blank.json()["status"] == "placed"
