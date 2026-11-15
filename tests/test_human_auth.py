"""Human-only approvals (bake-off review, Sat 22:30): an agent must never approve, even naming a real approver."""
import pytest
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


def test_pin_brute_force_locks_out(api):
    from aimo.gateway import human_auth
    human_auth.reset_for_tests()
    q, o = _pending(api)
    anon = _anon()
    body = {"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER}
    codes = [anon.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Approver-Pin": f"guess{i}"},
                       json=body).status_code for i in range(7)]
    assert codes[:4] == [401] * 4 and codes[4] == 429 and codes[5] == 429, codes
    # the guessing client (same source) is refused even with the right PIN while locked ...
    assert api.post(f"/v1/approvals/{o['order_id']}/decision", json=body).status_code == 429
    human_auth.reset_for_tests()
    assert api.post(f"/v1/approvals/{o['order_id']}/decision", json=body).status_code == 200


def test_signed_in_approver_is_not_locked_out_by_guessers(api):
    """No denial of service: a signed-in approver session keeps working while a guesser triggers the lock."""
    from aimo.gateway import human_auth
    human_auth.reset_for_tests()
    sess = api.post("/v1/auth/approver-session", json={"approver": "anna@hotel-aurora.example"}).json()["session"]
    assert api.post("/v1/auth/approver-session", headers={"X-Agent-Id": "buyer-1"},
                    json={"approver": "anna@hotel-aurora.example"}).status_code == 403
    assert api.post("/v1/auth/approver-session", json={"approver": "nobody@example.com"}).status_code == 404
    q, o = _pending(api)
    anon = _anon()
    body = {"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER}
    for i in range(6):
        anon.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Approver-Pin": f"guess{i}"}, json=body)
    r = anon.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Approver-Session": sess}, json=body)
    assert r.status_code == 200 and r.json()["status"] == "placed"
    assert anon.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Approver-Session": sess + "x"},
                     json=body).status_code in (401, 429)
    human_auth.reset_for_tests()


def test_session_is_bound_to_one_approver(api):
    """Anna's session cannot decide as someone else; the decision is recorded under the session's approver."""
    from aimo.gateway import human_auth
    human_auth.reset_for_tests()
    q, o = _pending(api)
    anon = _anon()
    sess = api.post("/v1/auth/approver-session", json={"approver": "Anna Virtanen"}).json()
    assert sess["approver"] == "anna@hotel-aurora.example"
    h = {"X-Approver-Session": sess["session"]}
    r = anon.post(f"/v1/approvals/{o['order_id']}/decision", headers=h,
                  json={"decision": "approve", "quote_hash": q["quote_hash"], "approver": "mikko@hotel-aurora.example"})
    assert r.status_code == 403 and r.json()["error"] == "approver_mismatch"
    r = anon.post(f"/v1/approvals/{o['order_id']}/decision", headers=h,
                  json={"decision": "approve", "quote_hash": q["quote_hash"]})
    assert r.status_code == 200 and r.json()["approved_by"] == "anna@hotel-aurora.example"


def test_approver_ids_hidden_from_agents(api):
    from aimo.gateway import human_auth
    human_auth.reset_for_tests()
    for c in _anon().get("/v1/customers").json():
        for a in c.get("approvers") or []:
            assert "id" not in a and "@" not in str(a), a
    full = api.get("/v1/customers").json()
    assert any("@" in str(a.get("id", "")) for c in full for a in c.get("approvers") or [])


def test_quotes_and_orders_need_identity_and_inbox_is_scoped(api):
    anon = _anon()
    r = anon.post("/v1/quotes", json={"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 10}]})
    assert r.status_code == 401 and r.json()["error"] == "identity_required"
    _pending(api)
    assert anon.get("/v1/approvals", params={"status": "all"}).status_code == 401
    assert anon.get("/v1/approvals", params={"status": "all"}, headers={"X-Customer-Id": "school-tampere"}).json() == []
    assert anon.get("/v1/approvals", params={"customer_id": "hotel-aurora"},
                    headers={"X-Customer-Id": "school-tampere"}).status_code == 403


# ------------------------------------------------------------------ per-approver credentials (fleet 0d96f61 + 599a991)
@pytest.fixture
def per_approver(tmp_path, monkeypatch):
    """No operator env credential: the gateway uses data/keys/approver_pin (Anna) + approver_pins.json (others)."""
    from aimo.gateway import human_auth
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.setattr(human_auth, "PIN_FILE", tmp_path / "approver_pin")
    monkeypatch.setattr(human_auth, "PINS_FILE", tmp_path / "approver_pins.json")
    monkeypatch.setattr(human_auth, "SECRET_FILE", tmp_path / "approver_session_secret")
    (tmp_path / "approver_pin").write_text("anna-existing-pin\n")
    human_auth.reset_for_tests()
    yield human_auth
    human_auth.reset_for_tests()


def test_each_approver_has_own_credential(per_approver):
    pins = per_approver.approver_pins()
    assert pins["anna@hotel-aurora.example"] == "anna-existing-pin"   # the README credential is unchanged
    others = {k: v for k, v in pins.items() if k != "anna@hotel-aurora.example"}
    assert "mikko@hotel-aurora.example" in others and len(others) >= 2
    assert len(set(pins.values())) == len(pins)                      # all distinct
    assert all(len(v) >= 12 for v in others.values())
    assert per_approver.approver_pins() == pins                      # stable across calls (persisted 0600)
    assert oct(per_approver.PINS_FILE.stat().st_mode & 0o777) == "0o600"


def test_shared_pin_no_longer_approves_as_anyone(per_approver):
    from fastapi.testclient import TestClient

    from aimo.gateway.app import create_app
    api = TestClient(create_app(with_mcp=False))
    pins = per_approver.approver_pins()
    anna = pins["anna@hotel-aurora.example"]
    mikko = pins["mikko@hotel-aurora.example"]
    # Anna's existing credential still signs her in (README flow)
    r = api.post("/v1/auth/approver-session", json={"approver": "anna@hotel-aurora.example"},
                 headers={"X-Approver-Pin": anna})
    assert r.status_code == 200 and r.json()["approver"] == "anna@hotel-aurora.example"
    # ... but it cannot sign in as Mikko; Mikko's own credential can
    r = api.post("/v1/auth/approver-session", json={"approver": "mikko@hotel-aurora.example"},
                 headers={"X-Approver-Pin": anna})
    assert r.status_code == 401
    r = api.post("/v1/auth/approver-session", json={"approver": "Mikko Laine"}, headers={"X-Approver-Pin": mikko})
    assert r.status_code == 200 and r.json()["approver"] == "mikko@hotel-aurora.example"
    # a decision with Anna's PIN naming another approver is refused before any order lookup
    r = api.post("/v1/approvals/ORD-x/decision", headers={"X-Approver-Pin": anna},
                 json={"decision": "approve", "approver": "mikko@hotel-aurora.example", "quote_hash": "h"})
    assert r.status_code == 401
    # any valid approver credential still opens the inbox
    assert api.get("/v1/approvals", headers={"X-Approver-Pin": mikko}).status_code == 200


def test_decision_needs_the_named_approvers_own_credential(per_approver, api):
    pins = per_approver.approver_pins()
    q, o = _pending(api)
    anon = _anon()
    url = f"/v1/approvals/{o['order_id']}/decision"
    body = {"decision": "approve", "approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager",
            "quote_hash": q["quote_hash"]}
    r = anon.post(url, headers={"X-Approver-Pin": pins["mikko@hotel-aurora.example"]}, json=body)
    assert r.status_code == 401                                   # Mikko's credential cannot decide as Anna
    r = anon.post(url, headers={"X-Approver-Pin": pins["anna@hotel-aurora.example"]}, json=body)
    assert r.status_code == 200 and r.json()["status"] == "placed"
