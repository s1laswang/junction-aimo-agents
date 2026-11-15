"""Bad-input and fuzz tests across all 13 MCP tools (in-process SDK Client, streamable HTTP /mcp, and buyer transports).

Every bad input (quantity <= 0, NaN/Inf, wrong types, missing fields, huge strings, SQL/path traversal strings,
unknown or unsafe customer ids, missing strict identity) must be rejected cleanly with is_error=True (or HTTP 4xx
for malformed transport payloads), never ZeroDivisionError, never HTTP 500, and never poisoning the MCP session.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store
from aimo.buyer.tools import InProcessTransport, MCPTransport, ToolError as BuyerToolError
from aimo.catalog import plan as plan_mod, textsearch
from aimo.gateway import fallback_catalog, human_auth, resilience, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from conformance.profile import MCP_FORBIDDEN, MCP_TOOLS
from test_fuzz import _synth_products
from test_gateway import FixtureClient, H

EXPECTED_13_TOOLS = {
    "search_products",
    "get_product",
    "get_offers",
    "plan_need",
    "create_quote",
    "place_order",
    "get_order_status",
    "get_audit_trail",
    "list_policies",
    "list_recipes",
    "plan_recipe",
    "check_standing_order_drift",
    "propose_offer",
}

HUGE = "x" * 10_000
BAD_SCALARS: list[Any] = [
    None,
    "",
    "   ",
    HUGE,
    "ü🥛ñ\u202e\x00",
    -1,
    -1e308,
    1e308,
    0,
    False,
    [],
    {},
    "2026-13-45",
    "' OR 1=1 --",
    "../../etc/passwd",
]

NEED_SEED: dict[str, Any] = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "required_claims": ["FREE_FROM_LACTOSE"],
    "quantity": 10,
    "unit": "L",
    "delivery_by": "2026-11-16",
}

TOOL_SEEDS: dict[str, dict[str, Any]] = {
    "search_products": {"query": "milk", "claims": ["FREE_FROM_LACTOSE"], "limit": 5},
    "get_product": {"product_id": "464306"},
    "get_offers": {"product_ids": ["464306"], "requested_date": "2026-11-16"},
    "plan_need": {"need": dict(NEED_SEED)},
    "create_quote": {
        "delivery_date": "2026-11-16",
        "lines": [{"product_id": "464306", "qty_sales_units": 10}],
    },
    "place_order": {"quote_id": "Q-20261114-deadbeef", "quote_hash": "0" * 64},
    "get_order_status": {"order_id": "ORD-20261114-deadbeef"},
    "get_audit_trail": {"subject_id": "Q-20261114-deadbeef"},
    "list_policies": {},
    "list_recipes": {},
    "plan_recipe": {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": "2026-11-16"},
    "check_standing_order_drift": {"customer_id": "hotel-aurora"},
    "propose_offer": {
        "customer_id": "hotel-aurora",
        "product_id": "464306",
        "qty_sales_units": 10,
        "delivery_date": "2026-11-16",
    },
}


def _reset_breakers() -> None:
    for b in list(resilience._breakers.values()):
        b.reset()


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("mcp-bad-inputs-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, synth_catalog_dir):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    for var in (
        "AIMO_FAULTS",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    if synth_catalog_dir is not None:
        pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
        cdb = synth_catalog_dir / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(FixtureClient())
    yield
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(None)
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _result_pair(res: Any) -> tuple[bool, str]:
    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    text = " ".join(getattr(x, "text", "") for x in (getattr(res, "content", None) or []))
    if not text and sc is not None:
        text = json.dumps(sc)
    return is_err, text


def _call_batch(calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[bool, str]]:
    async def _run() -> list[tuple[bool, str]]:
        out: list[tuple[bool, str]] = []
        async with Client(build_mcp()) as client:
            for name, args in calls:
                try:
                    res = await client.call_tool(name, args)
                    out.append(_result_pair(res))
                except Exception as exc:
                    out.append((True, f"{type(exc).__name__}: {exc}"))
                _reset_breakers()
        return out

    return asyncio.run(_run())


def _call_one(name: str, args: dict[str, Any]) -> tuple[bool, str]:
    return _call_batch([(name, args)])[0]


def _assert_clean_error(is_err: bool, text: str, label: Any = "") -> None:
    assert is_err, f"expected tool error for {label!r}, got ok: {text[:200]}"
    assert text.strip(), f"empty error text for {label!r}"
    assert "Traceback (most recent call last)" not in text, text[:300]
    assert "ZeroDivision" not in text, text[:300]


class McpHttpSession:
    BASE_HEADERS = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def __init__(self, tc: TestClient):
        self.tc = tc
        self.sid: str | None = None
        self._id = 0

    def _parse(self, r) -> dict[str, Any]:
        assert r.status_code < 500, (r.status_code, r.text[:300])
        if r.headers.get("mcp-session-id"):
            self.sid = r.headers["mcp-session-id"]
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            data_lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
            assert data_lines, r.text[:300]
            return json.loads(data_lines[0])
        return r.json()

    def initialize(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._id += 1
        r = self.tc.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-bad-inputs-test", "version": "0.1.0"},
                },
            },
            headers={**self.BASE_HEADERS, **(headers or {})},
        )
        assert r.status_code == 200, r.text[:300]
        body = self._parse(r)
        nh = {**self.BASE_HEADERS, **(headers or {})}
        if self.sid:
            nh["mcp-session-id"] = self.sid
        r_init = self.tc.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=nh)
        assert r_init.status_code < 500, r_init.text[:300]
        return body

    def rpc(self, method: str, params: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._id += 1
        req_headers = {**self.BASE_HEADERS, **(headers or {})}
        if self.sid and method != "initialize":
            req_headers.setdefault("mcp-session-id", self.sid)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        r = self.tc.post("/mcp", json=payload, headers=req_headers)
        assert r.status_code == 200, (method, r.status_code, r.text[:300])
        return self._parse(r)

    def call_tool(self, name: str, arguments: Any, headers: dict[str, str] | None = None) -> tuple[bool, str]:
        msg = self.rpc("tools/call", {"name": name, "arguments": arguments}, headers=headers)
        _reset_breakers()
        if "error" in msg:
            return True, json.dumps(msg["error"])
        res = msg.get("result") or {}
        is_err = bool(res.get("isError") or res.get("is_error"))
        txt = " ".join(c.get("text", "") for c in (res.get("content") or []) if isinstance(c, dict))
        if not txt and res.get("structuredContent") is not None:
            txt = json.dumps(res["structuredContent"])
        return is_err, txt


def test_all_13_mcp_tools_registered_and_forbidden_tools_rejected():
    async def _inspect():
        async with Client(build_mcp()) as c:
            return (await c.list_tools()).tools

    tools = asyncio.run(_inspect())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_13_TOOLS
    assert len(tools) == 13
    assert not (set(by_name) & set(MCP_FORBIDDEN))

    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        assert (t.description or "").strip(), t.name
        assert schema.get("type") == "object", t.name

    for name, req_fields in MCP_TOOLS.items():
        schema = getattr(by_name[name], "input_schema", None) or getattr(by_name[name], "inputSchema", None) or {}
        props = set((schema.get("properties") or {}).keys())
        req = set(schema.get("required") or [])
        assert set(req_fields) <= props, (name, props)
        assert set(req_fields) <= req, (name, req)

    bad_tool_calls = [(bad, {}) for bad in [*MCP_FORBIDDEN, "unknown_tool", "", "../etc/passwd"]]
    for (bad, _), (is_err, text) in zip(bad_tool_calls, _call_batch(bad_tool_calls)):
        _assert_clean_error(is_err, text, bad)
        assert "Unknown tool" in text, (bad, text)


@pytest.mark.parametrize(
    "bad_qty",
    [0, 0.0, -0.0, -1, -0.0001, -500.5, False, "NaN", "-Infinity", "abc", None, [], {}],
)
def test_mcp_plan_need_non_positive_quantities_rejected_before_planning(monkeypatch, bad_qty):
    called: list[str] = []

    def _forbid_candidates(*_a, **_kw):
        called.append("find_candidates")
        raise AssertionError("find_candidates must not run when quantity <= 0")

    class _NoCallBackend(FixtureClient):
        def price(self, ids, requested_date, customer=None):
            called.append("price")
            raise AssertionError("price must not run when quantity <= 0")

        def availability(self, ids, requested_date, customer=None):
            called.append("availability")
            raise AssertionError("availability must not run when quantity <= 0")

    monkeypatch.setattr(plan_mod, "find_candidates", _forbid_candidates)
    bc.set_client(_NoCallBackend())

    is_err, text = _call_one("plan_need", {"need": {**NEED_SEED, "quantity": bad_qty}})
    _assert_clean_error(is_err, text, bad_qty)
    assert called == []


def test_mcp_plan_need_transports_and_bad_need_fields():
    transport = MCPTransport(build_mcp(), timeout=20)
    try:
        assert {t.name for t in transport.list_tools()} == EXPECTED_13_TOOLS
        for qty in (0, -3):
            with pytest.raises(BuyerToolError) as ei:
                transport.call("plan_need", {"need": {**NEED_SEED, "quantity": qty}})
            assert "ZeroDivision" not in str(ei.value)
    finally:
        transport.close()

    inproc = InProcessTransport("buyer-1", "hotel-aurora")
    for qty in (0, -3):
        with pytest.raises(service.ServiceError) as sei:
            inproc.call("plan_need", {"need": {**NEED_SEED, "quantity": qty}})
        assert sei.value.status == 422 and sei.value.code == "invalid_need"

    bad_needs: list[Any] = [
        None,
        "10 L milk",
        123,
        [],
        {},
        {k: v for k, v in NEED_SEED.items() if k != "quantity"},
        {k: v for k, v in NEED_SEED.items() if k != "unit"},
        {k: v for k, v in NEED_SEED.items() if k != "delivery_by"},
        {k: v for k, v in NEED_SEED.items() if k != "product_query"},
        {**NEED_SEED, "quantity": 1_000_001},
        {**NEED_SEED, "quantity": 1e308},
        {**NEED_SEED, "quantity": "Infinity"},
        {**NEED_SEED, "product_query": ""},
        {**NEED_SEED, "product_query": "   "},
        {**NEED_SEED, "product_query": "x" * 501},
        {**NEED_SEED, "unit": "ML"},
        {**NEED_SEED, "unit": "l"},
        {**NEED_SEED, "unit": ""},
        {**NEED_SEED, "delivery_by": ""},
        {**NEED_SEED, "delivery_by": "not-a-date"},
        {**NEED_SEED, "delivery_by": "2026-13-45"},
        {**NEED_SEED, "required_claims": "FREE_FROM_LACTOSE"},
        {**NEED_SEED, "excluded_allergens": "milk"},
        {**NEED_SEED, "preferred_product_ids": "464306"},
        {**NEED_SEED, "preferences": ["brand=Valio"]},
        {**NEED_SEED, "customer_id": "unknown-cust"},
        {**NEED_SEED, "customer_id": "../etc/passwd"},
        {**NEED_SEED, "customer_id": "x" * 200},
        {**NEED_SEED, "customer_id": "hotel\x00aurora"},
    ]
    results = _call_batch([("plan_need", {"need": n}) for n in bad_needs])
    for n, (is_err, text) in zip(bad_needs, results):
        _assert_clean_error(is_err, text, n)

    ok_err, ok_text = _call_one("plan_need", {"need": dict(NEED_SEED)})
    assert not ok_err, ok_text
    assert json.loads(ok_text)["need"]["quantity"] == 10


def test_mcp_catalog_and_offers_tools_bad_inputs():
    bad_search = [
        {},
        {"query": None},
        {"query": 123},
        {"query": []},
        {"query": "milk", "claims": "FREE_FROM_LACTOSE"},
        {"query": "milk", "claims": [123]},
        {"query": "milk", "limit": "ten"},
        {"query": "milk", "limit": None},
        {"query": "milk", "limit": []},
    ]
    for args, (is_err, text) in zip(bad_search, _call_batch([("search_products", a) for a in bad_search])):
        _assert_clean_error(is_err, text, args)

    edge_search = [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 2000},
        {"query": "' OR 1=1 --"},
        {"query": "ü🥛ñ\u202e\x00"},
        {"query": "milk", "limit": 0},
        {"query": "milk", "limit": -5},
        {"query": "milk", "limit": 999_999},
    ]
    for args, (is_err, text) in zip(edge_search, _call_batch([("search_products", a) for a in edge_search])):
        assert "Traceback (most recent call last)" not in text
        if not is_err:
            prods = json.loads(text)["products"]
            assert isinstance(prods, list) and len(prods) <= 100

    bad_get_product = [
        ({}, None),
        ({"product_id": None}, None),
        ({"product_id": 123}, None),
        ({"product_id": []}, None),
        ({"product_id": ""}, "product_not_found"),
        ({"product_id": "   "}, "product_not_found"),
        ({"product_id": "does-not-exist"}, "product_not_found"),
        ({"product_id": "../../etc/passwd"}, "product_not_found"),
        ({"product_id": "' OR 1=1 --"}, "product_not_found"),
        ({"product_id": "x" * 2000}, "product_not_found"),
    ]
    for (args, code), (is_err, text) in zip(
        bad_get_product,
        _call_batch([("get_product", a) for a, _ in bad_get_product]),
    ):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)

    bad_offers = [
        ({}, None),
        ({"product_ids": ["464306"]}, None),
        ({"requested_date": "2026-11-16"}, None),
        ({"product_ids": "464306", "requested_date": "2026-11-16"}, None),
        ({"product_ids": [123], "requested_date": "2026-11-16"}, None),
        ({"product_ids": ["464306"], "requested_date": 20261116}, None),
        ({"product_ids": [], "requested_date": "2026-11-16"}, "no_products"),
        ({"product_ids": ["", "   "], "requested_date": "2026-11-16"}, "no_products"),
        ({"product_ids": [str(i) for i in range(1001)], "requested_date": "2026-11-16"}, "too_many_products"),
        ({"product_ids": ["464306"], "requested_date": ""}, "bad_date"),
        ({"product_ids": ["464306"], "requested_date": "not-a-date"}, "bad_date"),
        ({"product_ids": ["464306"], "requested_date": "2026-13-45"}, "bad_date"),
        ({"product_ids": ["464306"], "requested_date": "2026-02-30"}, "bad_date"),
    ]
    for (args, code), (is_err, text) in zip(
        bad_offers,
        _call_batch([("get_offers", a) for a, _ in bad_offers]),
    ):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)


def test_mcp_quotes_orders_audit_and_policies_bad_inputs(monkeypatch):
    strict_missing = [
        ("create_quote", {"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}]}),
        ("place_order", {"quote_id": "Q-missing", "quote_hash": "0" * 64}),
    ]
    for (name, args), (is_err, text) in zip(strict_missing, _call_batch(strict_missing)):
        _assert_clean_error(is_err, text, name)
        assert "identity_required" in text, (name, text)

    with TestClient(create_app(with_mcp=True), raise_server_exceptions=False) as tc:
        sess = McpHttpSession(tc)
        sess.initialize(H)

        bad_quotes: list[tuple[dict[str, Any], str | None]] = [
            ({}, None),
            ({"delivery_date": "2026-11-16"}, None),
            ({"delivery_date": "2026-11-16", "lines": []}, "no_lines"),
            ({"delivery_date": "bad-date", "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, "bad_date"),
            ({"delivery_date": "2026-13-45", "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, "bad_date"),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "", "qty_sales_units": 1}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "   ", "qty_sales_units": 1}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464 306", "qty_sales_units": 1}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "x" * 65, "qty_sales_units": 1}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 0}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": -1}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": -0.5}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1_000_001}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1e308}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": "NaN"}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": "Infinity"}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1, "tier": "bogus"}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1, "flags": ["f"] * 51}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1, "product_name": "n" * 301}]}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "463038", "qty_sales_units": 10}]}, "lines_unavailable"),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "469363", "qty_sales_units": 1000}]}, "lines_unavailable"),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}], "need": {**NEED_SEED, "quantity": 0}}, None),
            ({"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}], "need": {**NEED_SEED, "quantity": -5}}, None),
        ]
        for qargs, expected_code in bad_quotes:
            is_err, text = sess.call_tool("create_quote", qargs, headers=H)
            _assert_clean_error(is_err, text, qargs)
            if expected_code:
                assert expected_code in text, (qargs, text)

        if service.integration_status()["trust.evaluate"] == "aimo.trust":
            is_err, text = sess.call_tool(
                "create_quote",
                {"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}]},
                headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "mallory"},
            )
            _assert_clean_error(is_err, text, "mallory")
            assert "scope_missing" in text, text

        ok_err, ok_txt = sess.call_tool(
            "create_quote",
            {"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
            headers=H,
        )
        assert not ok_err, ok_txt
        quote = json.loads(ok_txt)

        for oargs, hdrs, expected_code in [
            ({}, H, None),
            ({"quote_id": None, "quote_hash": quote["quote_hash"]}, H, None),
            ({"quote_id": "Q-does-not-exist", "quote_hash": quote["quote_hash"]}, H, "quote_not_found"),
            ({"quote_id": quote["quote_id"], "quote_hash": "0" * 64}, H, "quote_hash_mismatch"),
            (
                {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
                {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "planner-1"},
                "agent_mismatch",
            ),
            (
                {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
                {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
                "customer_mismatch",
            ),
            (
                {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
                {"X-Customer-Id": "../etc/passwd", "X-Agent-Id": "buyer-1"},
                "unknown_customer",
            ),
        ]:
            is_err, text = sess.call_tool("place_order", oargs, headers=hdrs)
            _assert_clean_error(is_err, text, (oargs, hdrs))
            if expected_code:
                assert expected_code in text, (oargs, hdrs, text)

        is_err, text = sess.call_tool(
            "plan_need",
            {"need": dict(NEED_SEED)},
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        )
        _assert_clean_error(is_err, text, "plan_need customer mismatch")
        assert "customer_mismatch" in text, text

        for bad_cust in ("unknown-cust", "../etc/passwd", "x" * 200, "hotel\x00aurora"):
            is_err, text = sess.call_tool(
                "list_policies",
                {},
                headers={"X-Customer-Id": bad_cust, "X-Agent-Id": "buyer-1"},
            )
            _assert_clean_error(is_err, text, bad_cust)
            assert "unknown_customer" in text, (bad_cust, text)

        monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora:top-secret-key")
        is_err, text = sess.call_tool("list_policies", {}, headers=H)
        _assert_clean_error(is_err, text, "missing agent key")
        assert "agent_key_required" in text, text

        is_err, text = sess.call_tool("list_policies", {}, headers={**H, "X-Agent-Key": "wrong-key"})
        _assert_clean_error(is_err, text, "wrong agent key")
        assert "invalid_agent_key" in text, text

        ok_pol_err, ok_pol_txt = sess.call_tool("list_policies", {}, headers={**H, "X-Agent-Key": "top-secret-key"})
        assert not ok_pol_err and "policies" in json.loads(ok_pol_txt)
        monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    bad_status = [
        ({}, None),
        ({"order_id": None}, None),
        ({"order_id": 123}, None),
        ({"order_id": "ORD-does-not-exist"}, "order_not_found"),
        ({"order_id": ""}, "order_not_found"),
        ({"order_id": "../../etc/passwd"}, "order_not_found"),
    ]
    for (args, code), (is_err, text) in zip(bad_status, _call_batch([("get_order_status", a) for a, _ in bad_status])):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)

    bad_audit = [{}, {"subject_id": None}, {"subject_id": 123}, {"subject_id": []}]
    for args, (is_err, text) in zip(bad_audit, _call_batch([("get_audit_trail", a) for a in bad_audit])):
        _assert_clean_error(is_err, text, args)

    for sid in ("Q-unknown", "", "' OR 1=1 --", "ü🥛", "x" * 2000):
        is_err, text = _call_one("get_audit_trail", {"subject_id": sid})
        assert not is_err, (sid, text)
        trail = json.loads(text)
        assert trail["events"] == [] and trail["verify"]["ok"] is True


def test_mcp_extras_tools_bad_inputs():
    ok_rec_err, ok_rec_txt = _call_one("list_recipes", {})
    assert not ok_rec_err and json.loads(ok_rec_txt)["recipes"]

    bad_plan_recipe: list[tuple[dict[str, Any], str | None]] = [
        ({}, "recipe_not_found"),
        ({"recipe_id": "no-such-dish", "portions": 10}, "recipe_not_found"),
        ({"recipe_id": "../etc/passwd", "portions": 10}, "recipe_not_found"),
        ({"recipe_id": "lasagne"}, "portions_required"),
        ({"recipe_id": "lasagne", "portions": 0}, None),
        ({"recipe_id": "lasagne", "portions": -5}, None),
        ({"recipe_id": "lasagne", "portions": 100_001}, None),
        ({"recipe_id": "lasagne", "portions": "twenty"}, None),
        ({"recipe_id": "lasagne", "portions": 10, "excluded_allergens": "milk"}, None),
    ]
    for (args, code), (is_err, text) in zip(
        bad_plan_recipe,
        _call_batch([("plan_recipe", a) for a, _ in bad_plan_recipe]),
    ):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)

    bad_drift: list[tuple[dict[str, Any], str | None]] = [
        ({"customer_id": "unknown-cust"}, "unknown_customer"),
        ({"customer_id": "../etc/passwd"}, "unknown_customer"),
        ({"customer_id": "x" * 200}, "unknown_customer"),
        ({"customer_id": "' OR 1=1 --"}, "unknown_customer"),
        ({"customer_id": 123}, None),
        ({"customer_id": []}, None),
    ]
    for (args, code), (is_err, text) in zip(
        bad_drift,
        _call_batch([("check_standing_order_drift", a) for a, _ in bad_drift]),
    ):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)

    bad_propose: list[tuple[dict[str, Any], str | None]] = [
        ({}, None),
        ({"customer_id": None}, None),
        ({"customer_id": 123}, None),
        ({"customer_id": "unknown-cust"}, "unknown_customer"),
        ({"customer_id": "../etc/passwd"}, "unknown_customer"),
        ({"customer_id": "x" * 200}, "unknown_customer"),
        ({"customer_id": "hotel-aurora", "product_id": "no-such-product", "delivery_date": "2026-11-16"}, "product_not_found"),
        ({"customer_id": "hotel-aurora", "product_id": "463038", "delivery_date": "2026-11-16"}, "not_orderable"),
        ({"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 0, "delivery_date": "2026-11-16"}, None),
        ({"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": -5, "delivery_date": "2026-11-16"}, None),
        ({"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": "abc", "delivery_date": "2026-11-16"}, None),
        ({"customer_id": "hotel-aurora", "product_id": "464306", "delivery_date": "not-a-date"}, None),
        ({"customer_id": "hotel-aurora", "product_id": "464306", "delivery_date": "2026-13-45"}, None),
    ]
    for (args, code), (is_err, text) in zip(
        bad_propose,
        _call_batch([("propose_offer", a) for a, _ in bad_propose]),
    ):
        _assert_clean_error(is_err, text, args)
        if code:
            assert code in text, (args, text)


def _mcp_fuzz_cases() -> list[tuple[str, dict[str, Any]]]:
    cases: list[tuple[str, dict[str, Any]]] = []
    for tool_name in sorted(EXPECTED_13_TOOLS):
        seed = TOOL_SEEDS[tool_name]
        cases.append((tool_name, {"__unexpected_field__": "bad"}))
        for field in list(seed):
            cases.append((tool_name, {k: v for k, v in seed.items() if k != field}))
            for bad in BAD_SCALARS:
                cases.append((tool_name, {**seed, field: bad}))
        if tool_name == "get_offers":
            for bad in BAD_SCALARS:
                cases.append((tool_name, {**seed, "product_ids": [bad]}))
        if tool_name == "plan_need":
            for nf in ("customer_id", "product_query", "quantity", "unit", "delivery_by", "required_claims", "excluded_allergens"):
                for bad in BAD_SCALARS:
                    cases.append((tool_name, {"need": {**NEED_SEED, nf: bad}}))
        if tool_name == "create_quote":
            for lf in ("product_id", "qty_sales_units", "tier", "flags"):
                for bad in BAD_SCALARS:
                    cases.append((tool_name, {**seed, "lines": [{**seed["lines"][0], lf: bad}]}))
    return cases


def test_fuzz_all_13_mcp_tools_never_crash_or_poison_session():
    cases = _mcp_fuzz_cases()
    assert len(cases) >= 400

    async def _run_inproc_fuzz() -> tuple[list[str], int, dict[str, Any]]:
        problems: list[str] = []
        async with Client(build_mcp()) as client:
            for tool_name, args in cases:
                try:
                    res = await client.call_tool(tool_name, args)
                    is_err, text = _result_pair(res)
                except Exception as exc:
                    is_err, text = True, f"{type(exc).__name__}: {exc}"
                _reset_breakers()
                if "Traceback (most recent call last)" in text or "ZeroDivision" in text:
                    problems.append(f"{tool_name}({list(args)}): {text[:200]}")
                if is_err and not text.strip():
                    problems.append(f"{tool_name}({list(args)}): empty error text")
            tools_after = (await client.list_tools()).tools
            rec_after = await client.call_tool("list_recipes", {})
            _, rec_text = _result_pair(rec_after)
            return problems, len(tools_after), json.loads(rec_text)

    problems, count_after, rec_after = asyncio.run(_run_inproc_fuzz())
    assert not problems, "\n".join(problems[:30])
    assert count_after == 13
    assert rec_after.get("recipes")

    with TestClient(create_app(with_mcp=True), raise_server_exceptions=False) as tc:
        for raw in (b"", b"{", b"null", b"[]", b'"str"', b"\xff\xfe"):
            r = tc.post("/mcp", content=raw, headers=McpHttpSession.BASE_HEADERS)
            assert r.status_code < 500, (raw, r.status_code, r.text[:200])

        sess = McpHttpSession(tc)
        sess.initialize(H)
        for bad_rpc in (
            {"jsonrpc": "2.0", "id": 900, "method": "no_such_method", "params": {}},
            {"jsonrpc": "2.0", "id": 901, "method": "tools/call", "params": {}},
            {"jsonrpc": "2.0", "id": 902, "method": "tools/call", "params": {"name": 123, "arguments": "bad"}},
            {"jsonrpc": "2.0", "id": 903, "method": "tools/call", "params": {"name": "plan_need", "arguments": "not-a-dict"}},
        ):
            r = tc.post(
                "/mcp",
                json=bad_rpc,
                headers={**McpHttpSession.BASE_HEADERS, **({"mcp-session-id": sess.sid} if sess.sid else {}), **H},
            )
            assert r.status_code < 500, (bad_rpc, r.status_code, r.text[:200])

        listed = sess.rpc("tools/list", {}, headers=H)
        assert {t["name"] for t in listed["result"]["tools"]} == EXPECTED_13_TOOLS
