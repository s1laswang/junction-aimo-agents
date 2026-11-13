from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aimo import api, quotes
from aimo.app import app
from aimo.models import QuoteLine

A = {"X-Agent-Key": "demo-hotel-aurora"}
ADMIN = {"X-Admin-Key": "demo-admin"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    return TestClient(app)


def test_auth_and_mandate(client):
    assert client.get("/v1/mandate").status_code == 401
    assert client.get("/v1/mandate", headers={"X-Agent-Key": "bad"}).status_code == 401
    r = client.get("/v1/mandate", headers=A)
    assert r.status_code == 200 and r.json()["customer"] == "hotel-aurora"
    assert client.get("/v1/mandate?customer=school-hervanta", headers=A).status_code == 403
    assert client.get("/v1/mandate?customer=school-hervanta", headers=ADMIN).status_code == 200
    body = {"approve": True, "by": "x"}
    r = client.get("/v1/mandate")
    assert r.json() == {"code": "unauthorized", "text": "Missing or invalid X-Agent-Key."}
    assert client.post("/v1/approvals/x", json=body).status_code == 401
    assert client.post("/v1/approvals/x", json=body, headers={"X-Admin-Key": "bad"}).status_code == 401
    r = client.post("/v1/approvals/x", json=body, headers=A)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_domain_status_map():
    class ApprovalError(Exception):
        def __init__(self, code):
            self.code, self.text = code, "t"

    def boom(code):
        raise ApprovalError(code)

    for code, status in [("NOT_FOUND", 404), ("NOT_APPROVER", 403), ("NOT_PENDING", 409)]:
        with pytest.raises(api.Err) as e:
            api.domain(boom, code)
        assert e.value.status == status and e.value.body["code"] == code


def test_helpers():
    assert api.agent_from_key("demo-hotel-aurora")[0] == "hotel-aurora"
    assert api.agent_from_key("nope") is None and api.agent_from_key(None) is None
    assert api.is_admin("demo-admin") and not api.is_admin("x") and not api.is_admin(None)


def test_quote_place_flow(client, monkeypatch):
    import sys
    import types

    pytest.importorskip("aimo.orders")
    try:
        from aimo import offers
    except ImportError:
        offers = types.ModuleType("aimo.offers")
        monkeypatch.setitem(sys.modules, "aimo.offers", offers)
        monkeypatch.setattr("aimo.offers", offers, raising=False)

    def price_lines(customer, delivery_date, lines):
        ql = [QuoteLine(product_id=l.product_id, quantity=l.quantity, unit_price_net=1.0, line_total_net=l.quantity) for l in lines]
        return ql, round(sum(l.quantity for l in lines) * 1.14, 2), datetime.now(timezone.utc) + timedelta(hours=2)

    monkeypatch.setattr(offers, "price_lines", price_lines, raising=False)
    r = client.post("/v1/quotes", json={"offers": [{"product_id": "p1", "quantity": 10}], "delivery_date": "2026-11-16"}, headers=A)
    assert r.status_code == 200, r.text
    q = r.json()
    r = client.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"]}, headers=A)
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["outcome"] == "auto"
    r = client.post("/v1/orders", json={"quote_id": q["quote_id"], "jws": q["jws"]}, headers=A)
    assert r.status_code == 409 and r.json()["code"] == "QUOTE_USED"
    assert client.get(f"/v1/orders/{r.json().get('order_id', 'x')}", headers=A).status_code == 404
    assert client.get("/v1/orders", headers=ADMIN).status_code == 200
    assert client.get(f"/v1/audit?order_id={q['quote_id']}", headers=A).json()["events"]


def test_audit_trail_verified(client):
    r = client.get("/v1/audit", headers=A)
    assert r.status_code == 200
    d = r.json()
    assert d["verified"] == {"ok": True, "bad_seq": None} and d["checkpoint"]
