"""Judge panel re-score (Sun 05:10): POST /v1/agent/runs took a `faults` body field that bypassed AIMO_DEMO_FAULTS.
With {"transport": "inproc", "faults": "price_shock:*:0.01"} the hotel got a signed EUR 10.10 quote for 1,000 L and
a school order was auto-placed at EUR 2.80 with no human. Faults and test transports are now operator-only, and only
a signed-in human may start or answer a run."""
from fastapi.testclient import TestClient

from test_gateway import api, isolated  # noqa: F401  (fixtures reused)

EXPLOIT = {"text": "1000 l lactose-free milk by Monday", "customer_id": "hotel-aurora",
           "transport": "inproc", "faults": "price_shock:*:0.01"}


def _start(api, monkeypatch, body):
    """POST /v1/agent/runs with the registry stubbed: returns (response, the RunRequest the registry received)."""
    from aimo.buyer import runs
    seen = {}

    class _Run:
        run_id = "run-test"

    def start(req, **kw):
        seen["req"] = req
        return _Run()

    monkeypatch.setattr(runs.registry, "start", start)
    return api.post("/v1/agent/runs", json=body), seen.get("req")


def test_run_api_ignores_faults_and_test_transports_without_demo_faults(api, monkeypatch):
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    r, req = _start(api, monkeypatch, EXPLOIT)
    assert r.status_code == 200, r.text
    assert req.faults is None and req.transport is None
    assert sorted(r.json()["ignored"]) == ["faults", "transport"]


def test_operator_can_still_inject_faults_through_the_run_api(api, monkeypatch):
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    r, req = _start(api, monkeypatch, {"text": "milk", "customer_id": "hotel-aurora", "faults": "partial:*:400"})
    assert r.status_code == 200 and req.faults == "partial:*:400" and "ignored" not in r.json()


def test_no_faults_through_the_run_api_in_production(api, monkeypatch):
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    monkeypatch.setenv("AIMO_ENV", "production")
    r, req = _start(api, monkeypatch, EXPLOIT)
    assert r.status_code == 200 and req.faults is None


def test_inprocess_transport_does_not_apply_faults_without_demo_faults(monkeypatch):
    from aimo.backend.client import current_faults
    from aimo.buyer.tools import InProcessTransport
    seen = {}

    class Svc:
        @staticmethod
        def get_offers(product_ids, requested_date, identity=None):
            seen["spec"] = current_faults().spec
            return {}

    t = InProcessTransport.__new__(InProcessTransport)
    t.service, t.agent_id, t.customer_id, t.mandate, t.faults = Svc, "buyer-1", "hotel-aurora", None, "price_shock:*:0.01"
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    t.call("get_offers", {"product_ids": ["464306"], "requested_date": "2026-11-16"})
    assert seen["spec"] == ""
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    t.call("get_offers", {"product_ids": ["464306"], "requested_date": "2026-11-16"})
    assert "price_shock" in seen["spec"]


def test_run_api_needs_a_signed_in_human():
    from aimo.gateway import human_auth
    from aimo.gateway.app import create_app
    human_auth.reset_for_tests()
    anon = TestClient(create_app(with_mcp=False))
    body = {"text": "10 l milk", "customer_id": "hotel-aurora"}
    r = anon.post("/v1/agent/runs", json=body)
    assert r.status_code == 401 and r.json()["error"] == "approver_auth_required"
    assert anon.post("/v1/agent/runs", json=body, headers={"X-Agent-Id": "buyer-1"}).status_code == 401
    assert anon.get("/v1/agent/runs").status_code == 401
    assert anon.post("/v1/agent/runs/whatever/answer", json={"text": "x"}).status_code == 401
    human_auth.reset_for_tests()
