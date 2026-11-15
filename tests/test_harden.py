"""Hardening (S2): sane quantities, agent runs that never hang, MCP URL from the served port, one audit clock.
Offline: recorded mock-API fixtures (tests/test_gateway.FixtureClient). Owner: harden.

Run: uv run pytest -q tests/test_harden.py
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import service

from test_gateway import H, FixtureClient  # noqa: E402

D = "2026-11-16"
NEED = {"customer_id": "hotel-aurora", "product_query": "lactose-free milk", "unit": "L", "delivery_by": D}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    for k in ("AIMO_MCP_URL", "AIMO_SERVE_URL", "AIMO_NOW", "AIMO_NOW_FROZEN"):
        monkeypatch.delenv(k, raising=False)
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)


@pytest.fixture
def api():
    from aimo.gateway.app import create_app
    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"}, raise_server_exceptions=False)


def _quote(api, lines, **extra):
    return api.post("/v1/quotes", headers=H, json={"delivery_date": D, "lines": lines, **extra})


def _err(r, status, code=None):
    assert r.status_code == status, r.text
    body = r.json()
    assert body.get("error") and body.get("message"), body
    if code:
        assert body["error"] == code, body
    return body


# ------------------------------------------------------------------ 1. quantities
@pytest.mark.needs_data
def test_fractional_count_unit_is_422_with_plain_reason(api):
    b = _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2.5}]), 422, "fractional_quantity")
    assert "ST is a count unit" in b["message"] and "2 or 3" in b["message"]
    assert b["detail"]["refused"][0]["sales_unit"] == "ST"


@pytest.mark.needs_data
def test_whole_count_units_still_quote(api):
    r = _quote(api, [{"product_id": "464306", "qty_sales_units": 3}])
    assert r.status_code == 200, r.text


@pytest.mark.needs_data
def test_fraction_allowed_only_by_a_real_fractional_lot_step(api, monkeypatch):
    real = service.get_product

    def half_box(pid):
        p = real(pid)
        return p.model_copy(update={"allowed_lot_size": 0.5}) if pid == "464306" else p

    monkeypatch.setattr(service, "get_product", half_box)
    assert _quote(api, [{"product_id": "464306", "qty_sales_units": 2.5}]).status_code == 200
    _err(_quote(api, [{"product_id": "464306", "qty_sales_units": 2.25}]), 422, "fractional_quantity")


@pytest.mark.parametrize("qty", [1e9, 1e6 + 1, 1e300])
def test_absurd_quantity_is_422_rest(api, qty):
    b = _err(_quote(api, [{"product_id": "464306", "qty_sales_units": qty}]), 422)
    assert "qty_sales_units" in b["message"]


@pytest.mark.parametrize("qty", [1e9, float("inf"), float("nan"), "lots", None, True, -3])
def test_absurd_or_bad_quantity_is_422_in_process_and_mcp_path(qty):
    with pytest.raises(service.ServiceError) as ei:
        service.create_quote(D, [{"product_id": "464306", "qty_sales_units": qty}],
                             identity={"agent_id": "buyer-1", "customer_id": "hotel-aurora"})
    assert ei.value.status == 422 and ei.value.code in ("absurd_quantity", "bad_quantity")


def test_split_lines_summing_past_the_cap_are_422():
    with pytest.raises(service.ServiceError) as ei:
        service.create_quote(D, [{"product_id": "464306", "qty_sales_units": 900_000}] * 2,
                             identity={"agent_id": "buyer-1", "customer_id": "hotel-aurora"})
    assert ei.value.status == 422 and ei.value.code == "absurd_quantity"


def test_plan_absurd_quantity_is_422(api):
    b = _err(api.post("/v1/plan", headers=H, json={**NEED, "quantity": 1e9}), 422, "absurd_quantity")
    assert "not plausible" in b["message"]


@pytest.mark.needs_data
def test_large_qty_against_standing_baseline_needs_approval(api, monkeypatch):
    # 464306 at EUR 1.01: 120 units = EUR 121, far under the EUR 500 auto limit. With a standing baseline of
    # 2 a week, 120 is 60x -> flagged and never auto-approved.
    monkeypatch.setattr(service, "_standing_baseline", lambda c, pid: 2.0 if pid == "464306" else None)
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 120}]).json()
    assert "unknown_stock_large_qty" in q["lines"][0]["flags"], q["lines"][0]
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval", o
    hits = {h["rule_id"]: h for h in o["decision"]["hits"]}
    assert hits["quantity.plausibility"]["outcome"] == "needs_approval"
    assert "60x" in hits["quantity.plausibility"]["message"] or "60x" in str(q["provenance"]["line_verification"])


@pytest.mark.needs_data
def test_normal_qty_within_baseline_is_not_flagged(api, monkeypatch):
    monkeypatch.setattr(service, "_standing_baseline", lambda c, pid: 20.0 if pid == "464306" else None)
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 40}]).json()
    assert "unknown_stock_large_qty" not in q["lines"][0]["flags"]
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert "quantity.plausibility" not in {h["rule_id"] for h in o["decision"]["hits"]}, o["decision"]


def test_unknown_stock_large_qty_reason():
    terminal = SimpleNamespace(available_amount=None, issues=["terminal_unknown_qty"], logistic_model="Terminal")
    stock = SimpleNamespace(available_amount=5000.0, issues=[], logistic_model="Stock")
    assert service._large_qty_reason("nobody", "x", 5000, terminal)
    assert service._large_qty_reason("nobody", "x", 50, terminal) is None
    assert service._large_qty_reason("nobody", "x", 5000, stock) is None   # known stock: the stock check governs


def test_quantity_guard_never_auto_and_keeps_reject():
    from aimo.models import PolicyDecision, Quote, QuoteLine
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    ln = QuoteLine(product_id="1", product_name="x", qty_sales_units=5000, sales_unit="ST", unit_price=1, line_total=5000,
                   flags=["unknown_stock_large_qty"])
    q = Quote(quote_id="Q", customer_id="hotel-aurora", agent_id="buyer-1", delivery_date=D, lines=[ln], total_net=1,
              total_gross=1, created_at=now, expires_at=now)
    auto = PolicyDecision(outcome="auto_approve", policy_id="p", policy_version="1")
    d = service._quantity_guard(q, auto)
    assert d.outcome == "needs_approval" and d.approver_role
    rej = PolicyDecision(outcome="reject", policy_id="p", policy_version="1")
    assert service._quantity_guard(q, rej).outcome == "reject"


def test_hostile_customer_ids_are_refused_not_500(api):
    for cid in ("x" * 5000, "../../etc/passwd", "a/b"):
        r = api.post("/v1/quotes", headers={**H, "X-Customer-Id": cid},
                     json={"delivery_date": D, "lines": [{"product_id": "464306", "qty_sales_units": 1}]})
        assert r.status_code in (403, 422), (cid[:20], r.status_code, r.text[:200])
        assert r.json()["error"]


# ------------------------------------------------------------------ 2. agent runs
@pytest.mark.parametrize("raw,ctype", [
    (b"", "application/json"), (b"{", "application/json"), (b"[1]", "application/json"), (b"null", "application/json"),
    (b'{"text": 5}', "application/json"), (b'{"text": "milk", "thinking": "ULTRA"}', "application/json"),
    (b'{"text": "milk", "transport": "carrier-pigeon"}', "application/json"),
    (b'{"text": "milk", "customer_id": "../../x"}', "application/json"),
    (b'{"text": "milk", "image_b64": "\\u00fc not base64"}', "application/json"),
    (b'{"text": "   "}', "application/json"), (b'{}', "application/json"),
])
def test_bad_run_body_is_422_never_500(api, raw, ctype):
    from aimo.buyer import runs
    started = []
    runs_start = runs.registry.start
    runs.registry.start = lambda req, **kw: started.append(req)  # must never be reached
    try:
        r = api.post("/v1/agent/runs", content=raw, headers={"content-type": ctype})
    finally:
        runs.registry.start = runs_start
    _err(r, 422)
    assert not started


def test_mcp_url_follows_served_port(monkeypatch):
    from aimo.buyer import runs
    from aimo.cli import _export_serve_url

    req = SimpleNamespace(scope={"server": ("127.0.0.1", 8123)})
    assert runs.mcp_url_for(req) == "http://127.0.0.1:8123/mcp"          # the socket we were reached on
    assert runs.mcp_url_for(SimpleNamespace(scope={"server": ("0.0.0.0", 9000)})) == "http://127.0.0.1:9000/mcp"
    _export_serve_url("0.0.0.0", 8765)                                    # `aimo serve --port 8765`
    assert runs.mcp_url_for(req) == "http://127.0.0.1:8765/mcp"
    monkeypatch.setenv("AIMO_MCP_URL", "http://localhost:8000/mcp")       # stale explicit URL for another port
    _export_serve_url("127.0.0.1", 8765)
    assert runs.mcp_url_for(req) == "http://127.0.0.1:8765/mcp"
    monkeypatch.setenv("AIMO_MCP_URL", "https://gateway.example/mcp")    # a deliberate remote gateway is kept
    _export_serve_url("127.0.0.1", 8765)
    assert runs.mcp_url_for(req) == "https://gateway.example/mcp"


def _wait_done(run, timeout=15.0):
    t0 = time.time()
    while not run.done() and time.time() - t0 < timeout:
        time.sleep(0.05)
    return time.time() - t0


def test_unreachable_gateway_fails_fast_never_fake(monkeypatch):
    from aimo.buyer import runs, tools
    calls = []
    real_connect = tools.connect

    def connect(kind, **kw):
        calls.append(kind)
        if kind == "inproc":
            raise RuntimeError("in-process gateway unavailable (test)")
        return real_connect(kind, **kw)

    monkeypatch.setattr(tools, "connect", connect)
    run = runs.registry.start(runs.RunRequest(text="10 l milk", customer_id="hotel-aurora", transport="auto"),
                              mcp_url="http://127.0.0.1:9/mcp")          # discard port: nothing listens
    took = _wait_done(run)
    assert run.status == "error" and took < 10, (run.status, took)
    kinds = [s.kind for s in run.steps]
    assert kinds[-2:] == ["error", "final"], kinds
    assert "fake" not in calls and "no fake fallback" in run.steps[-2].data["error"]
    assert "127.0.0.1:9" in run.steps[-2].data["error"]


def test_explicit_fake_transport_still_allowed(monkeypatch):
    from aimo.buyer import runs
    t = runs.open_transport(runs.RunRequest(text="x", transport="fake"), agent_id="buyer-1", mandate=None, mcp_url=None)
    assert getattr(t, "kind", "fake") == "fake" or type(t).__name__ == "FakeTransport"


def test_hung_run_is_closed_by_watchdog(monkeypatch):
    from aimo.buyer import agent, runs

    class Hang:
        def __init__(self, *a, **kw):
            pass

        def run(self, text):
            time.sleep(5)
            return {"status": "placed"}

    monkeypatch.setattr(agent, "BuyerAgent", Hang)
    monkeypatch.setattr(runs, "RUN_TIMEOUT_S", 0.5)
    run = runs.registry.start(runs.RunRequest(text="x"), transport_factory=lambda r: SimpleNamespace(close=lambda: None))
    took = _wait_done(run, 4)
    assert run.status == "error" and took < 3, (run.status, took)
    assert [s.kind for s in run.steps][-2:] == ["error", "final"]
    assert "timed out" in run.steps[-2].title.lower()


# ------------------------------------------------------------------ 4. one clock in the audit report
@pytest.mark.needs_data
def test_audit_report_uses_simulated_clock_and_labels_it(api, monkeypatch):
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:16:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 10}]).json()
    assert q["created_at"].startswith("2026-11-14T12:16"), q["created_at"]
    rep = api.get(f"/v1/audit/report/{q['quote_id']}").text
    assert "simulated clock (AIMO_NOW)" in rep, rep[:400]
    assert "**14:16:00**" in rep, rep[:600]
    ev = [e for e in api.get("/v1/audit", params={"subject_id": q["quote_id"]}).json()][0]
    assert str(ev["ts"]).startswith("2026-11-14T12:16") and ev["data"]["clock"]["wall_ts"]


@pytest.mark.needs_data
def test_audit_report_real_clock_label(api):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 10}]).json()
    rep = api.get(f"/v1/audit/report/{q['quote_id']}").text
    assert "real clock" in rep and "UTC" not in rep.split("\n")[2], rep[:300]
