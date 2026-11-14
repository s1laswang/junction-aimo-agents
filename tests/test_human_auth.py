"""Human-only approvals (bake-off review, Sat 22:30): an agent must never approve, even naming a real approver."""
from fastapi.testclient import TestClient

from test_gateway import APPROVER, D, H, _quote, api, isolated  # noqa: F401  (fixtures reused)


def _anon():
    from aimo.gateway.app import create_app
    return TestClient(create_app(with_mcp=False))  # no approver credential, no identity


def _pending(api):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]).json()
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


def test_agent_naming_a_real_approver_cannot_approve(api):
    """The reviewer's exploit: agent headers + approver=anna placed the order. Must be 403 and nothing placed."""
    q, o = _pending(api)
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", headers=H,
                 json={"decision": "approve", "quote_hash": q["quote_hash"], "remember": True, **APPROVER})
    assert r.status_code == 403 and r.json()["error"] == "agent_cannot_approve"
    assert api.get(f"/v1/orders/{o['order_id']}", headers=H).json()["status"] == "pending_approval"


def test_human_without_pin_gets_401_with_pin_places(api):
    q, o = _pending(api)
    anon = _anon()
    body = {"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER}
    r = anon.post(f"/v1/approvals/{o['order_id']}/decision", json=body)
    assert r.status_code == 401 and r.json()["error"] == "approver_auth_required"
    assert anon.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Approver-Pin": "wrong"}, json=body).status_code == 401
    assert anon.post("/v1/demo/tamper/1").status_code == 401
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", json=body)
    assert r.status_code == 200 and r.json()["status"] == "placed"


def test_quotes_and_orders_need_identity_and_inbox_is_scoped(api):
    anon = _anon()
    r = anon.post("/v1/quotes", json={"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 10}]})
    assert r.status_code == 401 and r.json()["error"] == "identity_required"
    _pending(api)
    assert anon.get("/v1/approvals", params={"status": "all"}).status_code == 401
    assert anon.get("/v1/approvals", params={"status": "all"}, headers={"X-Customer-Id": "school-tampere"}).json() == []
    assert anon.get("/v1/approvals", params={"customer_id": "hotel-aurora"},
                    headers={"X-Customer-Id": "school-tampere"}).status_code == 403
