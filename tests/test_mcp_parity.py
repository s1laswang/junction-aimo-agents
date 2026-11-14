import asyncio
import pytest

TOOLS = {"search_products", "parse_request", "get_offers", "request_quote", "place_order", "get_product", "list_orders",
         "get_order", "list_approvals", "decide_approval", "get_audit_trail", "get_mandate"}

ROUTE_MAP = {
    "/v1/products/{id}": "/v1/products/{product_id}",
    "/v1/orders/{id}": "/v1/orders/{order_id}",
}

TOOL_ARGS = {
    "search_products": {
        "q", "lactose_free", "gluten_free", "exclude_allergens", "labels",
        "origin", "max_unit_price", "brands", "limit",
    },
    "get_product": {"product_id"},
    "parse_request": {"text", "customer"},
    "get_offers": {"customer", "deliver_by", "items", "raw_text"},
    "request_quote": {"offers", "delivery_date"},
    "place_order": {"quote_id", "jws", "ask_human", "note"},
    "get_order": {"order_id"},
    "list_orders": {"customer", "status"},
    "list_approvals": {"customer"},
    "decide_approval": {"order_id", "approve", "by", "note"},
    "get_audit_trail": {"customer", "order_id"},
    "get_mandate": {"customer"},
}

TOOL_REQUIRED = {
    "search_products": set(),
    "get_product": {"product_id"},
    "parse_request": {"text", "customer"},
    "get_offers": {"customer", "deliver_by", "items"},
    "request_quote": {"offers", "delivery_date"},
    "place_order": {"quote_id", "jws"},
    "get_order": {"order_id"},
    "list_orders": set(),
    "list_approvals": set(),
    "decide_approval": {"order_id", "approve", "by"},
    "get_audit_trail": set(),
    "get_mandate": set(),
}


def _mcp():
    srv = pytest.importorskip("aimo.mcp_server")
    mcp = getattr(srv, "mcp", None) or getattr(srv, "server", None)
    if mcp is None:
        pytest.skip("no mcp object")
    return mcp


def _openapi_args(spec, op):
    args = {p["name"] for p in op.get("parameters", []) if p.get("in") in ("path", "query")}
    if "requestBody" in op:
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in schema:
            schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        args |= set(schema.get("properties", ()))
    return args


def test_tool_parity(client):
    mcp = _mcp()
    listed = asyncio.run(mcp.list_tools())
    names = {t.name for t in listed}
    card = client.get("/.well-known/agent-card.json").json()
    card_names = {t["name"] if isinstance(t, dict) else t for t in card["tools"]}
    assert names == card_names == TOOLS


def test_agent_card_openapi_routes(client):
    card_resp = client.get("/.well-known/agent-card.json")
    spec_resp = client.get("/openapi.json")
    assert card_resp.status_code == 200 and spec_resp.status_code == 200
    card = card_resp.json()
    paths = spec_resp.json()["paths"]
    assert len(card["tools"]) == len(TOOLS)
    for tool in card["tools"]:
        assert isinstance(tool, dict)
        assert {"name", "method", "route", "description"} <= set(tool)
        for field in ("name", "method", "route", "description"):
            assert isinstance(tool[field], str) and tool[field].strip()
        openapi_route = ROUTE_MAP.get(tool["route"], tool["route"])
        assert openapi_route in paths, (tool["name"], openapi_route)
        assert tool["method"].lower() in paths[openapi_route], (tool["name"], tool["method"], openapi_route)


def test_mcp_input_schema_contract_parity(client):
    mcp = _mcp()
    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(listed) == set(TOOL_ARGS) == TOOLS
    for name, tool in listed.items():
        schema = getattr(tool, "inputSchema", None) or tool.input_schema
        assert tool.model_dump(by_alias=True)["inputSchema"] == schema
        assert isinstance(schema, dict) and schema.get("type") == "object"
        props = set(schema["properties"])
        assert "ctx" not in props
        assert props == TOOL_ARGS[name], (name, props)
        assert set(schema.get("required", ())) == TOOL_REQUIRED[name], name
    card = client.get("/.well-known/agent-card.json").json()
    spec = client.get("/openapi.json").json()
    for entry in card["tools"]:
        openapi_route = ROUTE_MAP.get(entry["route"], entry["route"])
        op = spec["paths"][openapi_route][entry["method"].lower()]
        assert _openapi_args(spec, op) == TOOL_ARGS[entry["name"]]


def test_jwks_ed25519_public_key(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    resp = client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert isinstance(keys, list) and len(keys) >= 1
    assert all("d" not in k for k in keys)
    ed_keys = [
        k for k in keys
        if k.get("kty") == "OKP" and k.get("crv") == "Ed25519" and k.get("alg") == "EdDSA"
    ]
    assert len(ed_keys) >= 1
    for k in ed_keys:
        assert isinstance(k.get("kid"), str) and k["kid"].strip()
        assert isinstance(k.get("x"), str) and k["x"].strip()
        assert "d" not in k
