"""Gateway tests. Offline: recorded mock-API responses in tests/fixtures (14 Nov 2026, 14:16 Helsinki).

Run: uv run pytest -q tests/test_gateway.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import service

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 11, 16)
H = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
APPROVER = {"approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager"}


class FixtureClient:
    """BackendClient serving recorded responses; stamps _fetched_at=now so cut-offs are judged at the
    recorded upstream timeNow (Sat 14 Nov 14:16 +0200)."""

    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self):
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text())
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text())
        self.stats = {"upstream_calls": 0}

    def _sel(self, raw, ids):
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        self.stats["upstream_calls"] += 1
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids, requested_date, customer=None):
        return self._sel(self.price_raw, ids)

    def availability(self, ids, requested_date, customer=None):
        return self._sel(self.avail_raw, ids)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)


@pytest.fixture
def api():
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False))


# ------------------------------------------------------------------ backend client
def test_offers_merge_cutoff_terminal_partial():
    offers = bc.get_offers(["463038", "464306", "469363"], D, quantities={"469363": 1000})
    o = offers["463038"]
    assert o.cutoff_passed and not o.available and o.logistic_model == "Terminal"
    assert o.available_amount is None and "terminal_unknown_qty" in o.issues and "cutoff_passed" in o.issues
    assert o.unit_price == 31.31 and o.comparison_price == 1.57 and o.comparison_unit == "L"
    m = offers["464306"]
    assert m.available and not m.cutoff_passed and m.unit_price == 1.01 and m.sales_unit == "ST" and m.issues == []
    p = offers["469363"]
    assert p.available and p.available_amount == 384 and {"limited_stock", "partial_stock"} <= set(p.issues)


def test_price_error_makes_offer_unavailable():
    pi = {"productId": "x", "error": True, "netPriceFound": False, "quantity": 1}
    o = bc.merge_offer("x", D, pi, None)
    assert not o.available and "price_error" in o.issues and "availability_not_found" in o.issues


def test_fault_injection_env_and_context(monkeypatch):
    monkeypatch.setenv("AIMO_FAULTS", "stockout:469363;price_shock:464306:1.5")
    offers = bc.get_offers(["464306", "469363"], D)
    assert not offers["469363"].available and "injected:stockout" in offers["469363"].issues
    assert offers["464306"].unit_price == pytest.approx(1.515) and any(i.startswith("injected:price_shock") for i in offers["464306"].issues)
    tok = bc.set_request_faults("partial:464306:10")  # context var overrides env
    try:
        o = bc.get_offers(["464306"], D, quantities={"464306": 50})["464306"]
        assert o.available_amount == 10 and "partial_stock" in o.issues and o.unit_price == 1.01
    finally:
        bc.reset_request_faults(tok)
    tok = bc.set_request_faults("error_rate:1")
    try:
        with pytest.raises(bc.UpstreamError) as e:
            bc.get_offers(["464306"], D)
        assert e.value.injected and e.value.status_code == 503
    finally:
        bc.reset_request_faults(tok)


def test_valio_client_chunks_caches_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(bc.time, "sleep", lambda s: None)
    calls = []
    state = {"n": 0}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        assert req.headers["X-API-Key"] == "test-key"
        assert len(body["products"]) <= 1000
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        calls.append(len(body["products"]))
        return httpx.Response(200, json={"timeNow": "2026-11-14T14:16:11+0200", "products": [
            {"productId": p, "salesBlock": False, "scheduleLines": []} for p in body["products"]]})

    c = bc.ValioClient(base_url="https://mock.invalid", api_key="test-key", cache_dir=tmp_path, offline=False)
    c._http = httpx.Client(base_url="https://mock.invalid", transport=httpx.MockTransport(handler),
                           headers={"X-API-Key": "test-key"})
    ids = [str(i) for i in range(2500)]
    r = c.availability(ids, D, "hotel-aurora")
    assert calls == [1000, 1000, 500] and len(r["products"]) == 2500 and c.stats["retries"] == 1
    c.availability(ids[:10], D, "hotel-aurora")  # memory cache
    assert calls == [1000, 1000, 500]
    c2 = bc.ValioClient(base_url="https://mock.invalid", api_key="test-key", cache_dir=tmp_path, offline=True)
    assert len(c2.availability(ids[:5], D, "hotel-aurora")["products"]) == 5  # disk cache, offline


# ------------------------------------------------------------------ order backend
def test_confirm_lines_ap_ca_re():
    from aimo.models import Quote, QuoteLine
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    q = Quote(quote_id="Q", customer_id="c", agent_id="a", delivery_date=D, total_net=1, created_at=now,
              expires_at=now + timedelta(minutes=5), lines=[
                  QuoteLine(product_id="464306", product_name="m", qty_sales_units=10, sales_unit="ST", unit_price=1, line_total=10),
                  QuoteLine(product_id="469363", product_name="n", qty_sales_units=500, sales_unit="ST", unit_price=1, line_total=500)])
    offers = bc.get_offers(["464306", "469363"], D)
    code, lines = store.confirm_lines(q, offers)
    assert code == "CA" and [l.code for l in lines] == ["AP", "CA"] and lines[1].qty_confirmed == 384
    q2 = q.model_copy(update={"lines": q.lines[:1]})
    assert store.confirm_lines(q2, offers)[0] == "AP"
    offers_re = bc.apply_faults(offers, bc.parse_faults("stockout:*"))
    assert store.confirm_lines(q, offers_re)[0] == "RE"


# ------------------------------------------------------------------ HTTP end to end (M1)
def _quote(api, lines, headers=H):
    return api.post("/v1/quotes", headers=headers, json={"delivery_date": D.isoformat(), "lines": lines})


def test_m1_flow_offers_quote_order_approve(api):
    r = api.post("/v1/offers", headers=H, json={"product_ids": ["463038", "464306", "469363"], "requested_date": "2026-11-16"})
    assert r.status_code == 200 and r.headers["x-aimo-source"] == "valio-aimo-mock-api"
    by = {o["product_id"]: o for o in r.json()}
    assert by["463038"]["cutoff_passed"] is True and by["464306"]["available"] is True
    assert by["464306"]["provenance"]["source"] == "valio-aimo-mock-api"

    r = _quote(api, [{"product_id": "463038", "qty_sales_units": 100}])
    assert r.status_code == 409 and r.json()["detail"]["refused"][0]["product_id"] == "463038"

    r = _quote(api, [{"product_id": "469363", "qty_sales_units": 1000}])
    assert r.status_code == 409 and r.json()["detail"]["refused"][0]["reason"] == "partial_stock"

    r = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}])
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["total_net"] == 1010.0 and q["quote_hash"] and q["lines"][0]["unit_price"] == 1.01
    assert q["availability_snapshot"][0]["product_id"] == "464306" and q["provenance"]["source"]
    assert api.get(f"/v1/quotes/{q['quote_id']}").json()["quote_hash"] == q["quote_hash"]

    r = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": "wrong"})
    assert r.status_code == 409
    r = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["status"] == "pending_approval" and o["decision"]["outcome"] == "needs_approval"
    # idempotent
    assert api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()["order_id"] == o["order_id"]

    inbox = api.get("/v1/approvals").json()
    assert [x["order"]["order_id"] for x in inbox] == [o["order_id"]] and inbox[0]["quote"]["quote_id"] == q["quote_id"]

    r = api.post(f"/v1/approvals/{o['order_id']}/decision", json={"decision": "approve", "quote_hash": "abc", **APPROVER})
    assert r.status_code == 409
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER})
    assert r.status_code == 200, r.text
    placed = r.json()
    assert placed["status"] == "placed" and placed["response_code"] == "AP"
    assert placed["response_lines"][0]["qty_confirmed"] == 1000

    evs = api.get("/v1/audit", params={"subject_id": o["order_id"]}).json()
    actions = [e["action"] for e in evs]
    assert "quote.created" in actions and "policy.evaluated" in actions and "order.placed" in actions
    assert api.get("/v1/audit/verify").json()["ok"] is True
    assert o["order_id"] in api.get(f"/v1/audit/report/{o['order_id']}").text


def test_approval_with_partial_stock_gives_ca(api):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]).json()
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", headers={"X-Aimo-Faults": "partial:464306:600"},
                 json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER})
    assert r.status_code == 200, r.text
    assert r.json()["response_code"] == "CA" and r.json()["response_lines"][0]["qty_confirmed"] == 600


def test_expired_quote_needs_requote(api, monkeypatch):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000}]).json()
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    q2 = _quote(api, [{"product_id": "464306", "qty_sales_units": 1}]).json()
    real_now = service._now
    monkeypatch.setattr(service, "_now", lambda: real_now() + timedelta(hours=1))
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER})
    assert r.status_code == 409 and r.json()["error"] == "quote_expired"
    assert api.get(f"/v1/orders/{o['order_id']}").json()["status"] == "expired"
    r = api.post("/v1/orders", headers=H, json={"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]})
    assert r.status_code == 409 and r.json()["error"] == "quote_expired"


def test_reject_decision_and_customer_mismatch(api):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000}]).json()
    r = api.post("/v1/orders", headers={**H, "X-Customer-Id": "school-tampere"},
                 json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    assert r.status_code == 403
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    r = api.post(f"/v1/approvals/{o['order_id']}/decision",
                 json={"decision": "reject", "quote_hash": q["quote_hash"], "note": "too much", **APPROVER})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert api.post(f"/v1/approvals/{o['order_id']}/decision",
                    json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER}).status_code == 409


def test_upstream_error_maps_to_503(api):
    r = api.post("/v1/offers", headers={**H, "X-Aimo-Faults": "error_rate:1"},
                 json={"product_ids": ["464306"], "requested_date": "2026-11-16"})
    assert r.status_code == 503 and r.headers.get("retry-after") and r.json()["detail"]["injected"] is True


def test_discovery_and_openapi(api):
    m = api.get("/.well-known/otap.json").json()
    assert m["mcp"]["server_name"] == "valio-aimo-trusted-trade" and "MCP" in m["supported_standards"]
    assert "keys" in api.get("/.well-known/jwks.json").json()
    spec = api.get("/openapi.json").json()
    for path in ["/v1/offers", "/v1/quotes", "/v1/orders", "/v1/approvals/{order_id}/decision", "/v1/audit/verify"]:
        assert path in spec["paths"]


# ------------------------------------------------------------------ MCP
def test_mcp_tools_listed_and_callable():
    from mcp import Client

    from aimo.gateway.mcp_server import build_mcp

    async def run():
        async with Client(build_mcp()) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool("get_offers", {"product_ids": ["463038"], "requested_date": "2026-11-16"})
            return names, res

    names, res = asyncio.run(run())
    assert {"search_products", "get_product", "get_offers", "plan_need", "create_quote", "place_order",
            "get_order_status", "get_audit_trail", "list_policies"} <= names
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    text = json.dumps(sc) if sc else " ".join(getattr(c, "text", "") for c in res.content)
    assert "cutoff_passed" in text and "463038" in text


def test_inprocess_tool_layer_matches_mcp_names():
    """aimo.buyer InProcessTransport calls service.<tool>(**args, identity=...)."""
    ident = {"agent_id": "buyer-1", "customer_id": "hotel-aurora"}
    off = service.get_offers(["463038", "464306"], "2026-11-16", identity=ident)
    assert {o["product_id"] for o in off["offers"]} == {"463038", "464306"} and off["provenance"]["source"]
    q = service.create_quote("2026-11-16", [{"product_id": "464306", "qty_sales_units": 1000}], identity=ident)
    assert q["customer_id"] == "hotel-aurora" and q["quote_hash"]
    o = service.place_order(q["quote_id"], q["quote_hash"], identity=ident)
    assert o["status"] == "pending_approval"
    assert service.get_order_status(o["order_id"])["order_id"] == o["order_id"]
    trail = service.get_audit_trail(o["order_id"])
    assert trail["events"] and trail["verify"]["ok"] and isinstance(trail["report"], str)
    assert service.list_policies()["policies"]


# ------------------------------------------------------------------ round 2: identity, approver role, inject_text, tamper
def _pending_order(api, headers=H, qty=1000):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": qty, "tier": "best"}], headers=headers).json()
    o = api.post("/v1/orders", headers=headers, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    return q, o


def test_normalize_agent_id():
    assert service.normalize_agent_id("hotel-aurora", "hotel-aurora/buyer-1") == "buyer-1"
    assert service.normalize_agent_id("hotel-aurora", "buyer-1") == "buyer-1"
    assert service.normalize_agent_id("hotel-aurora", "hotel-aurora/hotel-aurora/buyer-1") == "buyer-1"
    assert service.normalize_agent_id("hotel-aurora", "school-tampere/buyer-1") == "school-tampere/buyer-1"
    i = service.Identity(agent_id="hotel-aurora/buyer-1", customer_id="hotel-aurora")
    assert i.agent_id == "buyer-1" and i.actor == "agent:hotel-aurora/buyer-1"
    assert service.resolve_identity({"agent_id": "hotel-aurora/buyer-1", "customer_id": "hotel-aurora"}).actor == \
        "agent:hotel-aurora/buyer-1"


@pytest.mark.parametrize("agent", ["buyer-1", "hotel-aurora/buyer-1"])
def test_audit_actor_not_doubled(api, agent):
    hdr = {**H, "X-Agent-Id": agent}
    q, o = _pending_order(api, headers=hdr)
    assert q["agent_id"] == "buyer-1" and o["agent_id"] == "buyer-1"
    actors = {e["actor"] for e in api.get("/v1/audit", params={"subject_id": o["order_id"]}).json()}
    assert not any("hotel-aurora/hotel-aurora" in a for a in actors), actors
    assert {a for a in actors if a.startswith("agent:")} <= {"agent:hotel-aurora/buyer-1"}


def test_approved_by_is_plain_id_with_separate_role(api):
    q, o = _pending_order(api)
    r = api.post(f"/v1/approvals/{o['order_id']}/decision", json={"decision": "approve", "quote_hash": q["quote_hash"], **APPROVER})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "anna@hotel-aurora.example"
    assert r.json()["approver_role"] == "procurement_manager"
    # role defaults from the approver directory when omitted
    q2, o2 = _pending_order(api)
    r = api.post(f"/v1/approvals/{o2['order_id']}/decision",
                 json={"decision": "reject", "quote_hash": q2["quote_hash"], "approver": "anna@hotel-aurora.example"})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "anna@hotel-aurora.example" and r.json()["approver_role"] == "procurement_manager"


def test_inject_text_fault_plants_and_flags_supplier_text(api):
    from aimo.backend.client import INJECTION_TEXT

    fh = {**H, "X-Aimo-Faults": "inject_text:464306"}
    p = api.get("/v1/products/464306", headers=fh).json()
    assert INJECTION_TEXT in (p["marketing_text"] or "") and "injected:inject_text" in p["labels"]
    clean = api.get("/v1/products/464306", headers=H).json()
    assert INJECTION_TEXT not in (clean.get("marketing_text") or "") and "injected:inject_text" not in clean["labels"]
    res = api.get("/v1/products/search", params={"q": "lactose free milk", "limit": 5},
                  headers={**H, "X-Aimo-Faults": "inject_text:*"}).json()
    assert res and all(INJECTION_TEXT in (x.get("marketing_text") or "") for x in res)
    assert service.screen_supplier_text(service.Product(**{**p, "allergens": {}}))["suspicious"] is True

    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 100, "tier": "best"}], headers=fh).json()
    assert "supplier_text_injection_suspected" in q["lines"][0]["flags"]
    o = api.post("/v1/orders", headers=fh, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] != "placed"
    if service.integration_status()["trust.evaluate"] == "aimo.trust":
        inj = [h for h in o["decision"]["hits"] if h["rule_id"].startswith("injection")]
        assert inj and any(h["outcome"] == "needs_approval" for h in inj), o["decision"]["hits"]


def test_inject_text_via_mcp_tool_layer():
    from aimo.backend.client import INJECTION_TEXT

    tok = bc.set_request_faults("inject_text:*")
    try:
        p = service.get_product("464306")
        res = service.search_products("lactose free milk", limit=3)
    finally:
        bc.reset_request_faults(tok)
    assert INJECTION_TEXT in p.marketing_text
    assert all(INJECTION_TEXT in (x.get("marketing_text") or "") for x in res["products"])


def test_demo_tamper_routes(api, monkeypatch):
    if service.integration_status()["trust.audit"] != "aimo.trust":
        pytest.skip("trust audit not available")
    q, o = _pending_order(api)
    seq = api.get("/v1/audit", params={"subject_id": o["order_id"]}).json()[0]["seq"]
    assert api.get("/v1/audit/verify").json()["ok"] is True
    r = api.post(f"/v1/demo/tamper/{seq}")
    assert r.status_code == 200, r.text
    assert r.json()["verify"]["ok"] is False and api.get("/v1/audit/verify").json()["ok"] is False
    r = api.post(f"/v1/demo/untamper/{seq}")
    assert r.status_code == 200 and r.json()["verify"]["ok"] is True
    assert api.post("/v1/demo/tamper/999999").status_code == 404
    monkeypatch.setenv("AIMO_ENV", "production")
    assert api.post(f"/v1/demo/tamper/{seq}").status_code == 404
    assert api.post(f"/v1/demo/untamper/{seq}").status_code == 404
    assert api.get("/v1/audit/verify").json()["ok"] is True
    spec = api.get("/openapi.json").json()
    assert spec["paths"]["/v1/demo/tamper/{seq}"]["post"]["tags"] == ["demo"]
