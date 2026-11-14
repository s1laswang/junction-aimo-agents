import json

import pytest
from fastapi.testclient import TestClient

from aimo import api, audit, mandates
from aimo.app import app
from aimo.approvals import ApprovalError

ADMIN = {"X-Admin-Key": "demo-admin"}


@pytest.fixture(autouse=True)
def env_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))


@pytest.fixture
def client():
    return TestClient(app)


def test_custom_agent_keys_valid_mapping(client, monkeypatch):
    custom = {
        "custom-aurora-key": ["hotel-aurora", "buyer-aurora"],
        "custom-tays-key": ["hospital-tays", "buyer-tays"],
    }
    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps(custom))
    hit = mandates.agent_for_key("custom-aurora-key")
    assert hit is not None
    assert hit[0] == "hotel-aurora" and hit[1] == "buyer-aurora"
    assert mandates.agent_for_key("demo-hotel-aurora") is None

    r = client.get("/v1/mandate", headers={"X-Agent-Key": "custom-aurora-key"})
    assert r.status_code == 200
    assert r.json()["customer"] == "hotel-aurora"

    r_old = client.get("/v1/mandate", headers={"X-Agent-Key": "demo-hotel-aurora"})
    assert r_old.status_code == 401


@pytest.mark.parametrize(
    "bad_raw",
    [
        "{not-json",
        "   ",
        '["hotel-aurora", "buyer-aurora"]',
        "123",
        "null",
        "true",
        '"just-a-string"',
    ],
)
def test_malformed_agent_keys_fallback(client, monkeypatch, bad_raw):
    monkeypatch.setenv("AIMO_AGENT_KEYS", bad_raw)
    assert mandates._keys() == mandates._DEMO_KEYS
    hit = mandates.agent_for_key("demo-hotel-aurora")
    assert hit is not None and hit[0] == "hotel-aurora"

    r = client.get("/v1/mandate", headers={"X-Agent-Key": "demo-hotel-aurora"})
    assert r.status_code == 200
    assert r.json()["customer"] == "hotel-aurora"


def test_agent_keys_type_error_and_bad_entries(monkeypatch):
    orig_get = mandates.os.environ.get
    with monkeypatch.context() as m:
        m.setattr(
            mandates.os.environ,
            "get",
            lambda k, d=None: 999 if k == "AIMO_AGENT_KEYS" else orig_get(k, d),
        )
        assert mandates._keys() == mandates._DEMO_KEYS

    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps({"bad-str": "ab", "short-list": ["hotel-aurora"]}))
    assert "bad-str" in mandates._keys()
    assert mandates.agent_for_key("bad-str") is None
    assert mandates.agent_for_key("short-list") is None


@pytest.mark.parametrize(
    "per_order,per_week",
    [
        (0.0, 1000.0),
        (-25.0, 1000.0),
        (100.0, 0.0),
        (100.0, -500.0),
        (2000.0, 1500.0),
    ],
)
def test_put_mandate_validation_raises_approval_error(per_order, per_week):
    base = mandates.get("hotel-aurora")
    bad = base.model_copy(update={"per_order_eur": per_order, "per_week_eur": per_week})
    with pytest.raises(ApprovalError) as exc:
        mandates.put(bad)
    assert exc.value.code == "INVALID_MANDATE"
    assert exc.value.text

    with pytest.raises(api.Err) as mapped:
        api.domain(mandates.put, bad)
    assert mapped.value.status == 409
    assert mapped.value.body["code"] == "INVALID_MANDATE"


def test_put_mandate_http_rejects_invalid_and_persists_valid(client):
    base = mandates.get("hotel-aurora")

    for per_order, per_week in [
        (0.0, 15000.0),
        (-10.0, 15000.0),
        (500.0, 0.0),
        (500.0, -10.0),
        (20000.0, 15000.0),
    ]:
        bad = base.model_copy(update={"per_order_eur": per_order, "per_week_eur": per_week})
        r = client.put("/v1/mandate", headers=ADMIN, json=bad.model_dump())
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "INVALID_MANDATE"
        assert body["text"]

    assert mandates.get("hotel-aurora").per_order_eur == 2500.0
    assert mandates.get("hotel-aurora").per_week_eur == 15000.0
    assert [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"] == []

    updated = base.model_copy(update={"per_order_eur": 3200.0, "per_week_eur": 16000.0})
    r_ok = client.put("/v1/mandate", headers=ADMIN, json=updated.model_dump())
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["per_order_eur"] == 3200.0
    assert r_ok.json()["per_week_eur"] == 16000.0

    persisted = client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN)
    assert persisted.status_code == 200
    assert persisted.json()["per_order_eur"] == 3200.0
    assert persisted.json()["per_week_eur"] == 16000.0

    equal = updated.model_copy(update={"per_order_eur": 4000.0, "per_week_eur": 4000.0})
    r_eq = client.put("/v1/mandate", headers=ADMIN, json=equal.model_dump())
    assert r_eq.status_code == 200, r_eq.text
    assert mandates.get("hotel-aurora").per_order_eur == 4000.0

    events = [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"]
    assert len(events) == 2
    assert "per_order_eur 2500.0 -> 3200.0" in events[0].why
    assert "per_order_eur 3200.0 -> 4000.0" in events[1].why
    assert audit.verify()[0]
