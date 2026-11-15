"""Terminal demo (`aimo demo`): keyless transport choice, the operator's approver, verdict lines (owner: live)."""
from __future__ import annotations

import threading

from aimo import config
from aimo.buyer import run_demo


def test_keyless_auto_transport_uses_real_gateway_on_replay(monkeypatch):
    """No AIMO_API_KEY but a replay snapshot: the demo runs on the in-process gateway (policies, signing, audit),
    not on the FAKE gateway."""
    from aimo.buyer.tools import connect
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.delenv("AIMO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_OFFLINE", raising=False)
    monkeypatch.setenv("AIMO_REPLAY", "1")
    t = connect("auto", agent_id="buyer-1", customer_id="hotel-aurora", mcp_url="http://127.0.0.1:9/mcp")
    assert t.kind == "inproc"
    monkeypatch.setenv("AIMO_REPLAY", "0")  # no key and no snapshot: still the clearly labelled FAKE gateway
    t = connect("auto", agent_id="buyer-1", customer_id="hotel-aurora", mcp_url="http://127.0.0.1:9/mcp")
    assert t.kind == "fake"


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class _Gateway:
    """Pending approvals: one order already waiting before the demo starts, one created during the run."""

    def __init__(self):
        self.gets, self.posts = 0, []

    def get(self, url, params=None, headers=None, timeout=None):
        from aimo.gateway.human_auth import local_pin
        assert headers == {"X-Approver-Pin": local_pin()}  # the human credential, never agent identity
        self.gets += 1
        old = {"order": {"order_id": "ORD-old", "customer_id": "hotel-aurora", "quote_hash": "h0"}}
        new = {"order": {"order_id": "ORD-new", "customer_id": "hotel-aurora", "quote_hash": "h1"}}
        other = {"order": {"order_id": "ORD-school", "customer_id": "school-tampere", "quote_hash": "h2"}}
        return _Resp([old, other] if self.gets == 1 else [old, other, new])

    def post(self, url, json=None, headers=None, timeout=None):
        assert "X-Agent-Id" not in headers
        self.posts.append((url, json))
        return _Resp({"status": "placed"})


def test_auto_approver_decides_only_orders_created_during_the_run():
    gw, stop, ready = _Gateway(), threading.Event(), threading.Event()
    th = threading.Thread(target=run_demo.http_auto_approver,
                          args=("", "anna@hotel-aurora.example", "hotel-aurora", stop, 0.0, "approve", gw, ready),
                          daemon=True)
    th.start()
    assert ready.wait(5)
    for _ in range(50):
        if gw.posts:
            break
        stop.wait(0.1)
    stop.set()
    th.join(5)
    assert [u for u, _ in gw.posts] == ["/v1/approvals/ORD-new/decision"]
    assert gw.posts[0][1]["quote_hash"] == "h1" and gw.posts[0][1]["approver"] == "anna@hotel-aurora.example"


def _cand(pid, tier, reason=""):
    return {"product": {"id": pid}, "tier": tier, "reasons": [reason] if reason else []}


def test_verdict_shows_split_and_cutoff_substitution():
    lines = [{"product_id": p, "qty_sales_units": q} for p, q in (("464306", 400), ("464201", 400), ("463779", 80))]
    r = {"status": "pending_approval", "mock": True, "need": {"preferred_product_ids": ["463038"]},
         "quote": {"lines": lines, "total_net": 2544.8},
         "plan": {"lines": lines, "shortfall": 0, "candidates": [
             _cand("463038", "rejected", "Rejected: order cut-off passed (Thu 12 Nov 15:00) for delivery Mon 16 Nov")]},
         "steps": [{"kind": "approval_wait", "title": "", "data": {"approver_role": "procurement_manager"}}]}
    v = run_demo.scenario_verdict("stockout", r)
    assert "3 line(s), EUR 2,544.80, shortfall 0" in v
    assert "cut-off substitution: usual 463038 -> 463779, 464201, 464306" in v
    assert "partial stock split across 464306 x400" in v
    assert "waiting for a human (procurement_manager); nothing ordered" in v
    assert "scripted (no Gemini)" in v


def test_verdict_shows_allergen_rule_and_probe():
    r = {"status": "placed", "need": {"excluded_allergens": ["milk", "nuts"]},
         "order": {"approved_by": "policy:school-tampere"},
         "quote": {"lines": [{"product_id": "470889", "qty_sales_units": 200}], "total_net": 280.0},
         "plan": {"shortfall": 0, "candidates": []},
         "steps": [{"kind": "decision", "title": "", "data": {"decision": {"hits": [
             {"rule_id": "allergens", "outcome": "allow", "message": "All lines are confirmed free of milk or nuts."}]}}}],
         "allergen_probe": {"ran": True, "status": "rejected",
                            "reason": "'Valio Eila milk' (464306) contains milk, which is excluded for this customer."}}
    v = run_demo.scenario_verdict("school-allergen", r)
    assert "excluded milk, nuts; allergen rule: [allow]" in v
    assert "allergen probe (464306, cow's milk): rejected by policy (contains milk" in v


def test_montage_approvals_line_separates_humans_from_policy():
    rows = [("happy", {"status": "placed", "order": {"approved_by": "anna@hotel-aurora.example"}}),
            ("school-allergen", {"status": "placed", "order": {"approved_by": "policy:school-tampere"}}),
            ("drift", {"status": "needs_confirmation", "order": None})]
    line = run_demo._approvals_line(rows)
    assert line.startswith("**Approvals:** 1 order(s) approved by a named human (happy: anna@hotel-aurora.example)")
    assert "X-Approver-Pin" in line and "1 auto-approved by the customer's own policy" in line
    assert "drift: needs_confirmation" in line
