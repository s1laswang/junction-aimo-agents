"""E2E: the canonical "1,000 L lactose-free milk by Monday" through HTTP (FastAPI TestClient), offline.

The mock API is replaced by the recorded fixture snapshot (tests/fixtures/snapshot) via windtunnel.run.install_replay.
Contract: docs/interfaces.md (HTTP API). Skips cleanly until the gateway exposes a FastAPI app.
Expected per PLAN.md demo script: ~EUR 1,010 net > hotel-aurora auto limit EUR 500 -> pending_approval;
approval bound to quote_hash -> placed; audit chain verifies.
"""
from __future__ import annotations

import importlib

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.needs_data]

HDR = {"X-Agent-Id": "hotel-aurora/buyer-1", "X-Customer-Id": "hotel-aurora"}
NEED = {"raw_text": "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
        "customer_id": "hotel-aurora", "product_query": "lactose-free milk", "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [], "quantity": 1000, "unit": "L", "delivery_by": "2026-11-16",
        "preferred_product_ids": ["463038"]}   # the hotel's usual 10 L dispenser box (cut-off passed)


def _app():
    for mod, attr in (("aimo.gateway.app", "app"), ("aimo.gateway.main", "app"), ("aimo.gateway", "app"),
                      ("aimo.gateway.server", "app")):
        try:
            m = importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            continue
        app = getattr(m, attr, None) or (getattr(m, "create_app", None) and m.create_app())
        if app is not None:
            return app
    return None


@pytest.fixture(scope="module")
def client(tmp_path_factory, fixture_snapshot):
    app = _app()
    if app is None:
        pytest.skip("aimo.gateway has no FastAPI app yet")
    from fastapi.testclient import TestClient
    from windtunnel import run as R
    from windtunnel.snapshot import ReplayClient
    rc = ReplayClient(snapshot_dir=fixture_snapshot)
    R._CURRENT["client"] = rc
    import aimo.backend as B
    B.set_client(rc)                       # gateway path (get_offers_with_meta -> get_client())
    R.install_replay()                     # catalog path
    yield TestClient(app, headers={"X-Approver-Pin": "test-pin"})
    B.set_client(None)


def test_discovery(client):
    r = client.get("/.well-known/otap.json")
    assert r.status_code == 200
    assert client.get("/.well-known/jwks.json").json().get("keys")


def test_milk_flow_needs_approval_then_places(client):
    plan = client.post("/v1/plan", json=NEED, headers=HDR)
    assert plan.status_code == 200, plan.text
    p = plan.json()
    assert p["covered_amount"] >= 1000 and p["lines"]
    assert all(l["product_id"] != "463038" for l in p["lines"]), "463038 cut-off passed; must not be planned"
    lines = [{"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"], "tier": l.get("tier")}
             for l in p["lines"]]
    q = client.post("/v1/quotes", json={"customer_id": "hotel-aurora", "agent_id": HDR["X-Agent-Id"],
                                        "delivery_date": NEED["delivery_by"], "lines": lines, "need": NEED}, headers=HDR)
    assert q.status_code == 200, q.text
    quote = q.json()
    assert quote["quote_hash"] and quote["signature"]
    assert quote["total_net"] > 500
    o = client.post("/v1/orders", json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]}, headers=HDR)
    assert o.status_code in (200, 201, 202), o.text
    order = o.json()
    assert order["status"] == "pending_approval"
    assert order["decision"]["outcome"] == "needs_approval"
    # a tampered hash must not approve
    bad = client.post(f"/v1/approvals/{order['order_id']}/decision",
                      json={"decision": "approve", "approver": "anna@hotel-aurora.example", "note": "", "quote_hash": "0" * 64})
    assert bad.status_code >= 400 or bad.json().get("status") != "placed"
    ok = client.post(f"/v1/approvals/{order['order_id']}/decision",
                     json={"decision": "approve", "approver": "anna@hotel-aurora.example", "note": "ok",
                           "quote_hash": quote["quote_hash"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] in ("placed", "approved")
    v = client.get("/v1/audit/verify").json()
    assert v["ok"] is True
