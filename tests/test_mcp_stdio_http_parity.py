import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from aimo import allocate, audit, catalog, keys, offers, quotes
from aimo.app import app
from aimo.mcp_server import _keys, mcp

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

ROOT = Path(__file__).resolve().parent.parent
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AGENT_KEY = "demo-hotel-aurora"
OTHER_AGENT_KEY = "demo-hospital-tays"
ADMIN_KEY = "demo-admin"

EXPECTED_TOOLS = {
    "search_products",
    "get_product",
    "parse_request",
    "get_offers",
    "request_quote",
    "place_order",
    "get_order",
    "list_orders",
    "list_approvals",
    "decide_approval",
    "get_audit_trail",
    "get_mandate",
}

EXPECTED_REQUIRED = {
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

ROUTE_MAP = {
    "/v1/products/{id}": "/v1/products/{product_id}",
    "/v1/orders/{id}": "/v1/orders/{order_id}",
}


def _synthetic_catalog_items() -> list[dict]:
    return [
        {
            "id": "5001",
            "deleted": False,
            "category": "10400",
            "vendorName": "VALIO OY",
            "countryOfOrigin": "FI",
            "temperatureCondition": 4,
            "salesUnit": "LTR",
            "baseUnit": "LTR",
            "units": [{"unitId": "LTR", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [
                    {"language": "fi", "value": "Valio Kevytmaito laktoositon 1 l"},
                    {"language": "en", "value": "Valio Low fat milk lactose free 1 l"},
                ],
                "classifications": [
                    {"name": "allergen", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "packagingMarkedLabel", "values": [{"id": "coolTemperature"}]},
                    {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}]},
                ],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": 1.0, "unit": "ltr"}}
                ],
            },
        },
        {
            "id": "5002",
            "deleted": False,
            "category": "10400",
            "vendorName": "VALIO OY",
            "countryOfOrigin": "FI",
            "temperatureCondition": 4,
            "salesUnit": "LTR",
            "baseUnit": "LTR",
            "units": [{"unitId": "LTR", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [
                    {"language": "fi", "value": "Valio Luomu Kevytmaito laktoositon 1 l"},
                    {"language": "en", "value": "Valio Organic Low fat milk lactose free 1 l"},
                ],
                "classifications": [
                    {"name": "allergen", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {
                        "name": "packagingMarkedLabel",
                        "values": [{"id": "ORGANIC"}, {"id": "coolTemperature"}],
                    },
                    {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}]},
                ],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": 1.0, "unit": "ltr"}}
                ],
            },
        },
        {
            "id": "5003",
            "deleted": False,
            "category": "10400",
            "vendorName": "ARLA OY",
            "countryOfOrigin": "SE",
            "temperatureCondition": 4,
            "salesUnit": "LTR",
            "baseUnit": "LTR",
            "units": [{"unitId": "LTR", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Arla",
                "names": [
                    {"language": "fi", "value": "Arla Täysmaito 1 l"},
                    {"language": "en", "value": "Arla Whole milk 1 l"},
                ],
                "classifications": [
                    {
                        "name": "allergen",
                        "values": [
                            {"id": "AM", "unit": "CONTAINS"},
                            {"id": "ML", "unit": "CONTAINS"},
                            {"id": "AN", "unit": "MAY_CONTAIN"},
                        ],
                    },
                    {"name": "packagingMarkedLabel", "values": [{"id": "coolTemperature"}]},
                ],
                "unitConversions": [
                    {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": 1.0, "unit": "ltr"}}
                ],
            },
        },
    ]


def _write_synthetic_data(data_dir: Path, cache_dir: Path, delivery: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "valio_aimo_junction26_product_data.json").write_text(
        json.dumps(_synthetic_catalog_items()),
        encoding="utf-8",
    )
    (data_dir / "valio_aimo_junction26_delivery_reliability.csv").write_text(
        "productId,deliveryReliability\n5001,0.99\n5002,0.97\n5003,0.94\n",
        encoding="utf-8",
    )
    (data_dir / "valio_aimo_junction26_carbon_footprint.csv").write_text(
        "productId,AVG kg CO2-eq/kg\n5001,1.05\n5002,0.98\n5003,1.18\n",
        encoding="utf-8",
    )
    prices = {
        "5001": {
            "productId": "5001",
            "currency": "EUR",
            "catalogPrice": {"value": 1.20},
            "priceWithTax": {"value": 1.37},
        },
        "5002": {
            "productId": "5002",
            "currency": "EUR",
            "catalogPrice": {"value": 1.50},
            "priceWithTax": {"value": 1.71},
        },
        "5003": {
            "productId": "5003",
            "currency": "EUR",
            "catalogPrice": {"value": 1.10},
            "priceWithTax": {"value": 1.25},
        },
    }
    avail = {
        "5001": {
            "productId": "5001",
            "closingTime": "2026-11-15T14:00:00+02:00",
            "scheduleLines": [
                {
                    "availableDate": delivery,
                    "availableAmount": 5000.0,
                    "salesUnit": "LTR",
                    "salesBlock": False,
                }
            ],
        },
        "5002": {
            "productId": "5002",
            "closingTime": "2026-11-15T14:00:00+02:00",
            "scheduleLines": [
                {
                    "availableDate": delivery,
                    "availableAmount": 3000.0,
                    "salesUnit": "LTR",
                    "salesBlock": False,
                }
            ],
        },
        "5003": {
            "productId": "5003",
            "closingTime": "2026-11-15T14:00:00+02:00",
            "scheduleLines": [
                {
                    "availableDate": delivery,
                    "availableAmount": 2000.0,
                    "salesUnit": "LTR",
                    "salesBlock": False,
                }
            ],
        },
    }
    for customer in ("hotel-aurora", "hospital-tays", "school-hervanta"):
        (cache_dir / f"price__{customer}__{delivery}.json").write_text(
            json.dumps(prices),
            encoding="utf-8",
        )
        (cache_dir / f"availability__{customer}__{delivery}.json").write_text(
            json.dumps(avail),
            encoding="utf-8",
        )


def _unpack_result(res: dict):
    assert not res.get("isError"), res
    blocks = [json.loads(b["text"]) for b in res.get("content", [])]
    sc = res.get("structuredContent")
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], list):
            assert blocks == sc["result"]
            return sc["result"]
        return sc
    assert len(blocks) == 1, res
    return blocks[0]


def _unpack_error(name: str, res: dict) -> dict:
    assert res.get("isError") is True, res
    text = " ".join(b["text"] for b in res.get("content", []))
    prefix = f"Error executing tool {name}: "
    assert prefix in text, text
    return json.loads(text.split(prefix, 1)[1])


def _unpack_tool_error(name: str, exc: ToolError) -> dict:
    text = str(exc)
    prefix = f"Error executing tool {name}: "
    assert text.startswith(prefix), text
    return json.loads(text[len(prefix) :])


class HttpMcpSession:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid = None
        self.seq = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "parity-http", "version": "1.0"},
            },
        )
        resp = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, "mcp-session-id": self.sid},
        )
        assert resp.status_code in (200, 202, 204), resp.text

    def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        self.seq += 1
        req_headers = dict(MCP_HEADERS)
        if self.sid and method != "initialize":
            req_headers["mcp-session-id"] = self.sid
        if headers:
            req_headers.update(headers)
        resp = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            headers=req_headers,
        )
        assert resp.status_code == 200, resp.text
        self.sid = resp.headers.get("mcp-session-id", self.sid)
        body = resp.text
        if resp.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
        return json.loads(body)

    def call_ok(self, name: str, arguments: dict | None = None, headers: dict | None = None):
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        return _unpack_result(out["result"])

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        return _unpack_error(name, out["result"])


def _call_tool_ok(monkeypatch, name: str, arguments: dict | None = None, agent_key: str = "", admin_key: str = ""):
    monkeypatch.setenv("AIMO_AGENT_KEY", agent_key)
    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", admin_key)
    res = asyncio.run(mcp.call_tool(name, arguments or {}))
    return _unpack_result(res.model_dump(by_alias=True))


def _call_tool_err(
    monkeypatch,
    name: str,
    arguments: dict | None = None,
    agent_key: str = "",
    admin_key: str = "",
) -> dict:
    monkeypatch.setenv("AIMO_AGENT_KEY", agent_key)
    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", admin_key)
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(mcp.call_tool(name, arguments or {}))
    return _unpack_tool_error(name, exc_info.value)


@dataclass
class ParityWorkspace:
    base_dir: Path
    data_dir: Path
    cache_dir: Path
    key_dir: Path
    delivery: str

    def db_path(self, label: str) -> str:
        return str(self.base_dir / f"{label}.db")

    def stdio_env(self, db_label: str, agent_key: str = "", admin_key: str = "") -> dict[str, str]:
        return {
            **os.environ,
            "AIMO_DB": self.db_path(db_label),
            "AIMO_KEY_DIR": str(self.key_dir),
            "AIMO_DATA_DIR": str(self.data_dir),
            "AIMO_CACHE_DIR": str(self.cache_dir),
            "AIMO_ADMIN_KEY": ADMIN_KEY,
            "AIMO_AGENT_KEYS": "",
            "AIMO_AGENT_KEY": agent_key,
            "AIMO_STDIO_ADMIN_KEY": admin_key,
            "VALIO_API_BASE": "",
            "VALIO_API_KEY": "",
            "LLM_BASE_URL": "",
            "LLM_API_KEY": "",
            "LLM_MODEL": "",
        }


def _run_stdio_session(env: dict[str, str], callback):
    async def runner():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aimo.mcp_server"],
            env=env,
            cwd=str(ROOT),
        )
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await callback(session)

    return asyncio.run(runner())


async def _session_ok(session: ClientSession, name: str, arguments: dict | None = None):
    res = await session.call_tool(name, arguments or {})
    return _unpack_result(res.model_dump(by_alias=True))


async def _session_err(session: ClientSession, name: str, arguments: dict | None = None) -> dict:
    res = await session.call_tool(name, arguments or {})
    return _unpack_error(name, res.model_dump(by_alias=True))


@pytest.fixture
def parity_ws(tmp_path, monkeypatch) -> ParityWorkspace:
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    key_dir = tmp_path / "keys"
    delivery = (date.today() + timedelta(days=5)).isoformat()
    _write_synthetic_data(data_dir, cache_dir, delivery)

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "bootstrap.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("AIMO_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("AIMO_AGENT_KEYS", "")
    monkeypatch.setenv("AIMO_AGENT_KEY", "")
    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", "")
    monkeypatch.setenv("VALIO_API_BASE", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")

    catalog.reset()
    allocate._data.cache_clear()
    offers.clear_simulation()
    keys.jwks()
    catalog.search("milk")

    ws = ParityWorkspace(
        base_dir=tmp_path,
        data_dir=data_dir,
        cache_dir=cache_dir,
        key_dir=key_dir,
        delivery=delivery,
    )
    yield ws
    offers.clear_simulation()
    allocate._data.cache_clear()
    catalog.reset()


def _openapi_args(spec: dict, op: dict) -> set[str]:
    args = {p["name"] for p in op.get("parameters", []) if p.get("in") in ("path", "query")}
    if "requestBody" in op:
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in schema:
            schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        args |= set(schema.get("properties", ()))
    return args


def _norm_quote(q: dict) -> dict:
    assert q["quote_id"].startswith("q_")
    assert isinstance(q["issued_at"], str) and q["issued_at"]
    assert q["jws"].count(".") == 2
    return {k: v for k, v in q.items() if k not in {"quote_id", "issued_at", "jws"}}


def _verify_quote_jws(q: dict, jwks: dict) -> None:
    verified = quotes.verify(q["jws"], jwks)
    assert verified.quote_id == q["quote_id"]
    assert verified.customer == q["customer"]
    assert verified.total_net == q["total_net"]
    assert verified.total_gross == q["total_gross"]
    obj = jws.deserialize_compact(q["jws"], KeySet.import_key_set(jwks), algorithms=["EdDSA"])
    assert obj.headers().get("alg") == "EdDSA"
    assert obj.headers().get("kid") in {k["kid"] for k in jwks["keys"]}
    payload = json.loads(obj.payload)
    assert payload["quote_id"] == q["quote_id"]
    assert payload["lines"] == q["lines"]


def _norm_order(o: dict) -> dict:
    assert o["order_id"].startswith("ord_")
    assert o["quote_id"].startswith("q_")
    assert isinstance(o["created_at"], str) and o["created_at"]
    assert isinstance(o["updated_at"], str) and o["updated_at"]
    return {
        k: v
        for k, v in o.items()
        if k not in {"order_id", "quote_id", "created_at", "updated_at"}
    }


def _verify_audit_trail(trail: dict, jwks: dict) -> list[tuple[int, str, str, str]]:
    assert set(trail.keys()) == {"events", "text", "verified", "checkpoint"}
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    assert isinstance(trail["events"], list) and len(trail["events"]) > 0
    assert isinstance(trail["text"], str) and trail["text"].strip()
    cp = trail["checkpoint"]
    assert set(cp.keys()) == {"seq", "hash", "ts", "jws"}
    head = audit.verify_checkpoint(cp["jws"], jwks)
    assert head["seq"] == cp["seq"]
    assert head["hash"] == cp["hash"]
    return [(e["seq"], e["customer"], e["actor"], e["action"]) for e in trail["events"]]


def test_keys_ctx_env_fallback_vs_http_headers(parity_ws, monkeypatch):
    class NoHeadersCtx:
        @property
        def headers(self):
            raise RuntimeError("no http request in stdio mode")

    class WithHeadersCtx:
        def __init__(self, headers: dict[str, str]):
            self.headers = headers

    monkeypatch.setenv("AIMO_AGENT_KEY", AGENT_KEY)
    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", ADMIN_KEY)
    assert _keys(None) == (AGENT_KEY, ADMIN_KEY)
    assert _keys(NoHeadersCtx()) == (AGENT_KEY, ADMIN_KEY)
    assert _keys(WithHeadersCtx({})) == (None, None)
    assert _keys(WithHeadersCtx({"x-agent-key": OTHER_AGENT_KEY})) == (OTHER_AGENT_KEY, None)
    assert _keys(WithHeadersCtx({"x-admin-key": ADMIN_KEY})) == (None, ADMIN_KEY)

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("ctx_check"))
    with TestClient(app) as client:
        mcp_http = HttpMcpSession(client)
        monkeypatch.setenv("AIMO_AGENT_KEY", AGENT_KEY)
        monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", ADMIN_KEY)
        unauth_http = mcp_http.call_err("get_mandate", {})
        assert unauth_http == {"code": "unauthorized", "text": "Missing or invalid X-Agent-Key."}

        stdio_mandate = _call_tool_ok(monkeypatch, "get_mandate", {}, agent_key=AGENT_KEY)
        http_mandate = mcp_http.call_ok("get_mandate", {}, headers={"X-Agent-Key": AGENT_KEY})
        assert stdio_mandate == http_mandate


def test_schema_and_tool_list_parity_across_object_http_stdio_and_openapi(parity_ws):
    obj_tools = asyncio.run(mcp.list_tools())
    obj_by_name = {t.name: t.model_dump(by_alias=True) for t in obj_tools}

    with TestClient(app) as client:
        mcp_http = HttpMcpSession(client)
        http_tools = mcp_http.rpc("tools/list")["result"]["tools"]
        card = client.get("/.well-known/agent-card.json").json()
        openapi = client.get("/openapi.json").json()

    async def list_via_stdio(session: ClientSession):
        return [t.model_dump(by_alias=True) for t in (await session.list_tools()).tools]

    stdio_tools = _run_stdio_session(parity_ws.stdio_env("schema_stdio"), list_via_stdio)

    http_by_name = {t["name"]: t for t in http_tools}
    stdio_by_name = {t["name"]: t for t in stdio_tools}

    assert [t.name for t in obj_tools] == [t["name"] for t in http_tools] == [t["name"] for t in stdio_tools]
    assert set(obj_by_name) == set(http_by_name) == set(stdio_by_name) == EXPECTED_TOOLS
    assert len(obj_tools) == 12

    for name in EXPECTED_TOOLS:
        o_tool = obj_by_name[name]
        h_tool = http_by_name[name]
        s_tool = stdio_by_name[name]

        assert isinstance(o_tool["description"], str) and len(o_tool["description"].strip()) > 40
        assert o_tool["description"] == h_tool["description"] == s_tool["description"]

        o_schema = o_tool["inputSchema"]
        h_schema = h_tool["inputSchema"]
        s_schema = s_tool["inputSchema"]

        assert o_schema == h_schema == s_schema
        assert o_schema["type"] == "object"
        props = set(o_schema["properties"])
        assert "ctx" not in props
        assert props == set(h_schema["properties"]) == set(s_schema["properties"])

        req = set(o_schema.get("required", ()))
        assert req == set(h_schema.get("required", ())) == set(s_schema.get("required", ())) == EXPECTED_REQUIRED[name]

    assert {entry["name"] for entry in card["tools"]} == EXPECTED_TOOLS
    for entry in card["tools"]:
        route = ROUTE_MAP.get(entry["route"], entry["route"])
        op = openapi["paths"][route][entry["method"].lower()]
        assert _openapi_args(openapi, op) == set(obj_by_name[entry["name"]]["inputSchema"]["properties"])


def test_catalog_and_parse_parity_across_rest_http_and_stdio(parity_ws, monkeypatch):
    delivery = parity_ws.delivery
    parse_payload = {
        "text": f"I need 100 litres of lactose-free milk without nuts by {delivery}",
        "customer": "hotel-aurora",
    }

    with TestClient(app) as client:
        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("cat_rest"))
        rest_all = client.get("/v1/products", params={"q": "milk"}).json()
        rest_lf = client.get("/v1/products", params={"q": "milk", "lactose_free": True}).json()
        rest_no_nuts = client.get("/v1/products", params={"q": "milk", "exclude_allergens": ["nuts"]}).json()
        rest_no_milk = client.get("/v1/products", params={"q": "milk", "exclude_allergens": ["milk"]}).json()
        rest_prod = client.get("/v1/products/5001").json()
        rest_missing_resp = client.get("/v1/products/999999")
        assert rest_missing_resp.status_code == 404
        rest_missing = rest_missing_resp.json()
        rest_parsed = client.post(
            "/v1/needs/parse",
            json=parse_payload,
            headers={"X-Agent-Key": AGENT_KEY},
        ).json()

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("cat_http"))
        mcp_http = HttpMcpSession(client)
        http_all = mcp_http.call_ok("search_products", {"q": "milk"})
        http_lf = mcp_http.call_ok("search_products", {"q": "milk", "lactose_free": True})
        http_no_nuts = mcp_http.call_ok("search_products", {"q": "milk", "exclude_allergens": ["nuts"]})
        http_no_milk = mcp_http.call_ok("search_products", {"q": "milk", "exclude_allergens": ["milk"]})
        http_prod = mcp_http.call_ok("get_product", {"product_id": "5001"})
        http_missing = mcp_http.call_err("get_product", {"product_id": "999999"})
        http_parsed = mcp_http.call_ok("parse_request", parse_payload, headers={"X-Agent-Key": AGENT_KEY})

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("cat_call_tool"))
    ct_all = _call_tool_ok(monkeypatch, "search_products", {"q": "milk"})
    ct_lf = _call_tool_ok(monkeypatch, "search_products", {"q": "milk", "lactose_free": True})
    ct_no_nuts = _call_tool_ok(monkeypatch, "search_products", {"q": "milk", "exclude_allergens": ["nuts"]})
    ct_no_milk = _call_tool_ok(monkeypatch, "search_products", {"q": "milk", "exclude_allergens": ["milk"]})
    ct_prod = _call_tool_ok(monkeypatch, "get_product", {"product_id": "5001"})
    ct_missing = _call_tool_err(monkeypatch, "get_product", {"product_id": "999999"})
    ct_parsed = _call_tool_ok(monkeypatch, "parse_request", parse_payload, agent_key=AGENT_KEY)

    async def run_stdio(session: ClientSession):
        return (
            await _session_ok(session, "search_products", {"q": "milk"}),
            await _session_ok(session, "search_products", {"q": "milk", "lactose_free": True}),
            await _session_ok(session, "search_products", {"q": "milk", "exclude_allergens": ["nuts"]}),
            await _session_ok(session, "search_products", {"q": "milk", "exclude_allergens": ["milk"]}),
            await _session_ok(session, "get_product", {"product_id": "5001"}),
            await _session_err(session, "get_product", {"product_id": "999999"}),
            await _session_ok(session, "parse_request", parse_payload),
        )

    st_all, st_lf, st_no_nuts, st_no_milk, st_prod, st_missing, st_parsed = _run_stdio_session(
        parity_ws.stdio_env("cat_stdio", agent_key=AGENT_KEY),
        run_stdio,
    )

    assert {p["product_id"] for p in rest_all} == {"5001", "5002", "5003"}
    assert rest_all == http_all == ct_all == st_all

    assert [p["product_id"] for p in rest_lf] == ["5001", "5002"]
    assert rest_lf == http_lf == ct_lf == st_lf

    assert [p["product_id"] for p in rest_no_nuts] == ["5001", "5002"]
    assert rest_no_nuts == http_no_nuts == ct_no_nuts == st_no_nuts

    assert rest_no_milk == http_no_milk == ct_no_milk == st_no_milk == []

    assert rest_prod["product_id"] == "5001"
    assert rest_prod["unit_size"] == {"value": 1.0, "unit": "l"}
    assert rest_prod == http_prod == ct_prod == st_prod

    assert rest_missing == {"code": "not_found", "text": "No product 999999."}
    assert rest_missing == http_missing == ct_missing == st_missing

    assert rest_parsed["customer"] == "hotel-aurora"
    assert rest_parsed["deliver_by"] == delivery
    assert rest_parsed["items"][0]["query"] == "milk"
    assert rest_parsed["items"][0]["quantity"] == 100.0
    assert rest_parsed["items"][0]["constraints"]["lactose_free"] is True
    assert "nuts" in rest_parsed["items"][0]["constraints"]["exclude_allergens"]
    assert isinstance(rest_parsed["summary"], str) and "100 L milk" in rest_parsed["summary"]
    assert rest_parsed == http_parsed == ct_parsed == st_parsed


def test_offers_and_signed_quote_parity_across_rest_http_and_stdio(parity_ws, monkeypatch):
    delivery = parity_ws.delivery
    need_normal = {
        "customer": "hotel-aurora",
        "deliver_by": delivery,
        "items": [
            {
                "query": "milk",
                "quantity": 100.0,
                "unit": "L",
                "constraints": {"lactose_free": True, "exclude_allergens": ["nuts"]},
            }
        ],
        "raw_text": f"100 L lactose-free milk without nuts by {delivery}",
    }
    need_shortfall = {
        "customer": "hotel-aurora",
        "deliver_by": delivery,
        "items": [
            {
                "query": "milk",
                "quantity": 9000.0,
                "unit": "L",
                "constraints": {"lactose_free": True},
            }
        ],
        "raw_text": f"9000 L lactose-free milk by {delivery}",
    }
    quote_req = {
        "offers": [{"product_id": "5001", "quantity": 100.0}],
        "delivery_date": delivery,
    }

    with TestClient(app) as client:
        jwks = client.get("/.well-known/jwks.json").json()

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("offers_rest"))
        r_off = client.post("/v1/offers", json=need_normal, headers={"X-Agent-Key": AGENT_KEY}).json()
        r_short = client.post("/v1/offers", json=need_shortfall, headers={"X-Agent-Key": AGENT_KEY}).json()
        r_quote = client.post("/v1/quotes", json=quote_req, headers={"X-Agent-Key": AGENT_KEY}).json()

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("offers_http"))
        mcp_http = HttpMcpSession(client)
        h_off = mcp_http.call_ok("get_offers", need_normal, headers={"X-Agent-Key": AGENT_KEY})
        h_short = mcp_http.call_ok("get_offers", need_shortfall, headers={"X-Agent-Key": AGENT_KEY})
        h_quote = mcp_http.call_ok("request_quote", quote_req, headers={"X-Agent-Key": AGENT_KEY})

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("offers_ct"))
    c_off = _call_tool_ok(monkeypatch, "get_offers", need_normal, agent_key=AGENT_KEY)
    c_short = _call_tool_ok(monkeypatch, "get_offers", need_shortfall, agent_key=AGENT_KEY)
    c_quote = _call_tool_ok(monkeypatch, "request_quote", quote_req, agent_key=AGENT_KEY)

    async def run_stdio(session: ClientSession):
        return (
            await _session_ok(session, "get_offers", need_normal),
            await _session_ok(session, "get_offers", need_shortfall),
            await _session_ok(session, "request_quote", quote_req),
        )

    s_off, s_short, s_quote = _run_stdio_session(
        parity_ws.stdio_env("offers_stdio", agent_key=AGENT_KEY),
        run_stdio,
    )

    grp = r_off["items"][0]
    assert grp["shortfall"] == 0.0
    assert grp["offers"][0]["product_id"] == "5001"
    assert grp["offers"][0]["total_net"] == 120.0
    assert len(grp["alternatives"]) >= 1
    assert grp["alternatives"][0]["product_id"] == "5002"
    assert r_off == h_off == c_off == s_off

    assert r_short["items"][0]["shortfall"] == 1000.0
    assert r_short == h_short == c_short == s_short

    for q in (r_quote, h_quote, c_quote, s_quote):
        _verify_quote_jws(q, jwks)
    assert _norm_quote(r_quote) == _norm_quote(h_quote) == _norm_quote(c_quote) == _norm_quote(s_quote)
    assert r_quote["total_net"] == 120.0
    assert r_quote["total_gross"] == 137.0


def _run_rest_journey(client: TestClient, delivery: str) -> dict:
    agent_h = {"X-Agent-Key": AGENT_KEY}
    admin_h = {"X-Admin-Key": ADMIN_KEY}

    products = client.get("/v1/products", params={"q": "milk", "lactose_free": True}).json()
    product = client.get("/v1/products/5001").json()
    parsed = client.post(
        "/v1/needs/parse",
        json={"text": f"I need 100 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
        headers=agent_h,
    ).json()
    compared = client.post("/v1/offers", json=parsed, headers=agent_h).json()
    mandate = client.get("/v1/mandate", headers=agent_h).json()

    q_auto = client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "5001", "quantity": 100}], "delivery_date": delivery},
        headers=agent_h,
    ).json()
    o_auto = client.post(
        "/v1/orders",
        json={"quote_id": q_auto["quote_id"], "jws": q_auto["jws"], "ask_human": False},
        headers=agent_h,
    ).json()

    q_ask = client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "5002", "quantity": 50}], "delivery_date": delivery},
        headers=agent_h,
    ).json()
    o_ask = client.post(
        "/v1/orders",
        json={
            "quote_id": q_ask["quote_id"],
            "jws": q_ask["jws"],
            "ask_human": True,
            "note": "Confirm cold room space",
        },
        headers=agent_h,
    ).json()

    got_auto = client.get(f"/v1/orders/{o_auto['order_id']}", headers=agent_h).json()
    got_ask = client.get(f"/v1/orders/{o_ask['order_id']}", headers=agent_h).json()
    orders_all = client.get("/v1/orders", headers=agent_h).json()
    orders_pending = client.get("/v1/orders", params={"status": "pending_approval"}, headers=agent_h).json()
    approvals_before = client.get("/v1/approvals", headers=agent_h).json()

    forbid_resp = client.post(
        f"/v1/approvals/{o_ask['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": "Agent attempt"},
        headers=agent_h,
    )
    assert forbid_resp.status_code == 403
    decide_forbidden = forbid_resp.json()

    decided = client.post(
        f"/v1/approvals/{o_ask['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": "Approved by hotel manager"},
        headers=admin_h,
    ).json()

    approvals_after = client.get("/v1/approvals", headers=agent_h).json()
    orders_confirmed = client.get("/v1/orders", params={"status": "confirmed"}, headers=agent_h).json()
    trail_all = client.get("/v1/audit", headers=agent_h).json()
    trail_order = client.get("/v1/audit", params={"order_id": o_ask["order_id"]}, headers=agent_h).json()

    return {
        "products": products,
        "product": product,
        "parsed": parsed,
        "compared": compared,
        "mandate": mandate,
        "q_auto": q_auto,
        "o_auto": o_auto,
        "q_ask": q_ask,
        "o_ask": o_ask,
        "got_auto": got_auto,
        "got_ask": got_ask,
        "orders_all": orders_all,
        "orders_pending": orders_pending,
        "approvals_before": approvals_before,
        "decide_forbidden": decide_forbidden,
        "decided": decided,
        "approvals_after": approvals_after,
        "orders_confirmed": orders_confirmed,
        "trail_all": trail_all,
        "trail_order": trail_order,
    }


def _run_http_mcp_journey(mcp_http: HttpMcpSession, delivery: str) -> dict:
    agent_h = {"X-Agent-Key": AGENT_KEY}
    admin_h = {"X-Admin-Key": ADMIN_KEY}

    products = mcp_http.call_ok("search_products", {"q": "milk", "lactose_free": True})
    product = mcp_http.call_ok("get_product", {"product_id": "5001"})
    parsed = mcp_http.call_ok(
        "parse_request",
        {"text": f"I need 100 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
        headers=agent_h,
    )
    compared = mcp_http.call_ok(
        "get_offers",
        {
            "customer": parsed["customer"],
            "deliver_by": parsed["deliver_by"],
            "items": parsed["items"],
            "raw_text": parsed["raw_text"],
        },
        headers=agent_h,
    )
    mandate = mcp_http.call_ok("get_mandate", {}, headers=agent_h)

    q_auto = mcp_http.call_ok(
        "request_quote",
        {"offers": [{"product_id": "5001", "quantity": 100}], "delivery_date": delivery},
        headers=agent_h,
    )
    o_auto = mcp_http.call_ok(
        "place_order",
        {"quote_id": q_auto["quote_id"], "jws": q_auto["jws"], "ask_human": False},
        headers=agent_h,
    )

    q_ask = mcp_http.call_ok(
        "request_quote",
        {"offers": [{"product_id": "5002", "quantity": 50}], "delivery_date": delivery},
        headers=agent_h,
    )
    o_ask = mcp_http.call_ok(
        "place_order",
        {
            "quote_id": q_ask["quote_id"],
            "jws": q_ask["jws"],
            "ask_human": True,
            "note": "Confirm cold room space",
        },
        headers=agent_h,
    )

    got_auto = mcp_http.call_ok("get_order", {"order_id": o_auto["order_id"]}, headers=agent_h)
    got_ask = mcp_http.call_ok("get_order", {"order_id": o_ask["order_id"]}, headers=agent_h)
    orders_all = mcp_http.call_ok("list_orders", {}, headers=agent_h)
    orders_pending = mcp_http.call_ok("list_orders", {"status": "pending_approval"}, headers=agent_h)
    approvals_before = mcp_http.call_ok("list_approvals", {}, headers=agent_h)

    decide_forbidden = mcp_http.call_err(
        "decide_approval",
        {"order_id": o_ask["order_id"], "approve": True, "by": "Laura Virtanen", "note": "Agent attempt"},
        headers=agent_h,
    )
    decided = mcp_http.call_ok(
        "decide_approval",
        {"order_id": o_ask["order_id"], "approve": True, "by": "Laura Virtanen", "note": "Approved by hotel manager"},
        headers=admin_h,
    )

    approvals_after = mcp_http.call_ok("list_approvals", {}, headers=agent_h)
    orders_confirmed = mcp_http.call_ok("list_orders", {"status": "confirmed"}, headers=agent_h)
    trail_all = mcp_http.call_ok("get_audit_trail", {}, headers=agent_h)
    trail_order = mcp_http.call_ok("get_audit_trail", {"order_id": o_ask["order_id"]}, headers=agent_h)

    return {
        "products": products,
        "product": product,
        "parsed": parsed,
        "compared": compared,
        "mandate": mandate,
        "q_auto": q_auto,
        "o_auto": o_auto,
        "q_ask": q_ask,
        "o_ask": o_ask,
        "got_auto": got_auto,
        "got_ask": got_ask,
        "orders_all": orders_all,
        "orders_pending": orders_pending,
        "approvals_before": approvals_before,
        "decide_forbidden": decide_forbidden,
        "decided": decided,
        "approvals_after": approvals_after,
        "orders_confirmed": orders_confirmed,
        "trail_all": trail_all,
        "trail_order": trail_order,
    }


def _run_call_tool_journey(monkeypatch, delivery: str) -> dict:
    products = _call_tool_ok(monkeypatch, "search_products", {"q": "milk", "lactose_free": True})
    product = _call_tool_ok(monkeypatch, "get_product", {"product_id": "5001"})
    parsed = _call_tool_ok(
        monkeypatch,
        "parse_request",
        {"text": f"I need 100 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
        agent_key=AGENT_KEY,
    )
    compared = _call_tool_ok(
        monkeypatch,
        "get_offers",
        {
            "customer": parsed["customer"],
            "deliver_by": parsed["deliver_by"],
            "items": parsed["items"],
            "raw_text": parsed["raw_text"],
        },
        agent_key=AGENT_KEY,
    )
    mandate = _call_tool_ok(monkeypatch, "get_mandate", {}, agent_key=AGENT_KEY)

    q_auto = _call_tool_ok(
        monkeypatch,
        "request_quote",
        {"offers": [{"product_id": "5001", "quantity": 100}], "delivery_date": delivery},
        agent_key=AGENT_KEY,
    )
    o_auto = _call_tool_ok(
        monkeypatch,
        "place_order",
        {"quote_id": q_auto["quote_id"], "jws": q_auto["jws"], "ask_human": False},
        agent_key=AGENT_KEY,
    )

    q_ask = _call_tool_ok(
        monkeypatch,
        "request_quote",
        {"offers": [{"product_id": "5002", "quantity": 50}], "delivery_date": delivery},
        agent_key=AGENT_KEY,
    )
    o_ask = _call_tool_ok(
        monkeypatch,
        "place_order",
        {
            "quote_id": q_ask["quote_id"],
            "jws": q_ask["jws"],
            "ask_human": True,
            "note": "Confirm cold room space",
        },
        agent_key=AGENT_KEY,
    )

    got_auto = _call_tool_ok(monkeypatch, "get_order", {"order_id": o_auto["order_id"]}, agent_key=AGENT_KEY)
    got_ask = _call_tool_ok(monkeypatch, "get_order", {"order_id": o_ask["order_id"]}, agent_key=AGENT_KEY)
    orders_all = _call_tool_ok(monkeypatch, "list_orders", {}, agent_key=AGENT_KEY)
    orders_pending = _call_tool_ok(
        monkeypatch,
        "list_orders",
        {"status": "pending_approval"},
        agent_key=AGENT_KEY,
    )
    approvals_before = _call_tool_ok(monkeypatch, "list_approvals", {}, agent_key=AGENT_KEY)

    decide_forbidden = _call_tool_err(
        monkeypatch,
        "decide_approval",
        {"order_id": o_ask["order_id"], "approve": True, "by": "Laura Virtanen", "note": "Agent attempt"},
        agent_key=AGENT_KEY,
    )
    decided = _call_tool_ok(
        monkeypatch,
        "decide_approval",
        {"order_id": o_ask["order_id"], "approve": True, "by": "Laura Virtanen", "note": "Approved by hotel manager"},
        admin_key=ADMIN_KEY,
    )

    approvals_after = _call_tool_ok(monkeypatch, "list_approvals", {}, agent_key=AGENT_KEY)
    orders_confirmed = _call_tool_ok(monkeypatch, "list_orders", {"status": "confirmed"}, agent_key=AGENT_KEY)
    trail_all = _call_tool_ok(monkeypatch, "get_audit_trail", {}, agent_key=AGENT_KEY)
    trail_order = _call_tool_ok(
        monkeypatch,
        "get_audit_trail",
        {"order_id": o_ask["order_id"]},
        agent_key=AGENT_KEY,
    )

    return {
        "products": products,
        "product": product,
        "parsed": parsed,
        "compared": compared,
        "mandate": mandate,
        "q_auto": q_auto,
        "o_auto": o_auto,
        "q_ask": q_ask,
        "o_ask": o_ask,
        "got_auto": got_auto,
        "got_ask": got_ask,
        "orders_all": orders_all,
        "orders_pending": orders_pending,
        "approvals_before": approvals_before,
        "decide_forbidden": decide_forbidden,
        "decided": decided,
        "approvals_after": approvals_after,
        "orders_confirmed": orders_confirmed,
        "trail_all": trail_all,
        "trail_order": trail_order,
    }


def _run_stdio_subprocess_journey(parity_ws: ParityWorkspace, db_label: str) -> dict:
    delivery = parity_ws.delivery

    async def buyer_phase(session: ClientSession) -> dict:
        products = await _session_ok(session, "search_products", {"q": "milk", "lactose_free": True})
        product = await _session_ok(session, "get_product", {"product_id": "5001"})
        parsed = await _session_ok(
            session,
            "parse_request",
            {"text": f"I need 100 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
        )
        compared = await _session_ok(
            session,
            "get_offers",
            {
                "customer": parsed["customer"],
                "deliver_by": parsed["deliver_by"],
                "items": parsed["items"],
                "raw_text": parsed["raw_text"],
            },
        )
        mandate = await _session_ok(session, "get_mandate", {})

        q_auto = await _session_ok(
            session,
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 100}], "delivery_date": delivery},
        )
        o_auto = await _session_ok(
            session,
            "place_order",
            {"quote_id": q_auto["quote_id"], "jws": q_auto["jws"], "ask_human": False},
        )

        q_ask = await _session_ok(
            session,
            "request_quote",
            {"offers": [{"product_id": "5002", "quantity": 50}], "delivery_date": delivery},
        )
        o_ask = await _session_ok(
            session,
            "place_order",
            {
                "quote_id": q_ask["quote_id"],
                "jws": q_ask["jws"],
                "ask_human": True,
                "note": "Confirm cold room space",
            },
        )

        got_auto = await _session_ok(session, "get_order", {"order_id": o_auto["order_id"]})
        got_ask = await _session_ok(session, "get_order", {"order_id": o_ask["order_id"]})
        orders_all = await _session_ok(session, "list_orders", {})
        orders_pending = await _session_ok(session, "list_orders", {"status": "pending_approval"})
        approvals_before = await _session_ok(session, "list_approvals", {})
        decide_forbidden = await _session_err(
            session,
            "decide_approval",
            {"order_id": o_ask["order_id"], "approve": True, "by": "Laura Virtanen", "note": "Agent attempt"},
        )
        return {
            "products": products,
            "product": product,
            "parsed": parsed,
            "compared": compared,
            "mandate": mandate,
            "q_auto": q_auto,
            "o_auto": o_auto,
            "q_ask": q_ask,
            "o_ask": o_ask,
            "got_auto": got_auto,
            "got_ask": got_ask,
            "orders_all": orders_all,
            "orders_pending": orders_pending,
            "approvals_before": approvals_before,
            "decide_forbidden": decide_forbidden,
        }

    out = _run_stdio_session(parity_ws.stdio_env(db_label, agent_key=AGENT_KEY), buyer_phase)
    ask_order_id = out["o_ask"]["order_id"]

    async def admin_phase(session: ClientSession) -> dict:
        decided = await _session_ok(
            session,
            "decide_approval",
            {"order_id": ask_order_id, "approve": True, "by": "Laura Virtanen", "note": "Approved by hotel manager"},
        )
        approvals_after = await _session_ok(session, "list_approvals", {"customer": "hotel-aurora"})
        orders_confirmed = await _session_ok(
            session,
            "list_orders",
            {"customer": "hotel-aurora", "status": "confirmed"},
        )
        trail_all = await _session_ok(session, "get_audit_trail", {"customer": "hotel-aurora"})
        trail_order = await _session_ok(
            session,
            "get_audit_trail",
            {"customer": "hotel-aurora", "order_id": ask_order_id},
        )
        return {
            "decided": decided,
            "approvals_after": approvals_after,
            "orders_confirmed": orders_confirmed,
            "trail_all": trail_all,
            "trail_order": trail_order,
        }

    out.update(_run_stdio_session(parity_ws.stdio_env(db_label, admin_key=ADMIN_KEY), admin_phase))
    return out


def test_all_twelve_tools_behavioral_parity_across_rest_http_and_stdio(parity_ws, monkeypatch):
    delivery = parity_ws.delivery

    with TestClient(app) as client:
        jwks = client.get("/.well-known/jwks.json").json()

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("journey_rest"))
        rest = _run_rest_journey(client, delivery)

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("journey_http"))
        mcp_http = HttpMcpSession(client)
        http = _run_http_mcp_journey(mcp_http, delivery)

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("journey_call_tool"))
    ct = _run_call_tool_journey(monkeypatch, delivery)

    stdio = _run_stdio_subprocess_journey(parity_ws, "journey_stdio")

    runs = (rest, http, ct, stdio)

    assert rest["products"] == http["products"] == ct["products"] == stdio["products"]
    assert rest["product"] == http["product"] == ct["product"] == stdio["product"]
    assert rest["parsed"] == http["parsed"] == ct["parsed"] == stdio["parsed"]
    assert rest["compared"] == http["compared"] == ct["compared"] == stdio["compared"]
    assert rest["mandate"] == http["mandate"] == ct["mandate"] == stdio["mandate"]
    assert rest["mandate"]["customer"] == "hotel-aurora"
    assert rest["mandate"]["per_order_eur"] == 2500.0

    for run in runs:
        _verify_quote_jws(run["q_auto"], jwks)
        _verify_quote_jws(run["q_ask"], jwks)

    assert _norm_quote(rest["q_auto"]) == _norm_quote(http["q_auto"]) == _norm_quote(ct["q_auto"]) == _norm_quote(stdio["q_auto"])
    assert _norm_quote(rest["q_ask"]) == _norm_quote(http["q_ask"]) == _norm_quote(ct["q_ask"]) == _norm_quote(stdio["q_ask"])

    assert rest["o_auto"]["status"] == "confirmed"
    assert rest["o_auto"]["decision"]["outcome"] == "auto"
    assert any(x["code"] == "WITHIN_MANDATE" for x in rest["o_auto"]["decision"]["reasons"])
    assert _norm_order(rest["o_auto"]) == _norm_order(http["o_auto"]) == _norm_order(ct["o_auto"]) == _norm_order(stdio["o_auto"])

    assert rest["o_ask"]["status"] == "pending_approval"
    assert rest["o_ask"]["decision"]["outcome"] == "needs_approval"
    assert rest["o_ask"]["decision"]["reasons"] == [{"code": "AGENT_REQUESTED", "text": "Confirm cold room space"}]
    assert _norm_order(rest["o_ask"]) == _norm_order(http["o_ask"]) == _norm_order(ct["o_ask"]) == _norm_order(stdio["o_ask"])

    for run in runs:
        assert run["got_auto"] == run["o_auto"]
        assert run["got_ask"] == run["o_ask"]
    assert _norm_order(rest["got_auto"]) == _norm_order(http["got_auto"]) == _norm_order(ct["got_auto"]) == _norm_order(stdio["got_auto"])
    assert _norm_order(rest["got_ask"]) == _norm_order(http["got_ask"]) == _norm_order(ct["got_ask"]) == _norm_order(stdio["got_ask"])

    norm_list = lambda items: [_norm_order(x) for x in items]
    assert len(rest["orders_all"]) == 2
    assert norm_list(rest["orders_all"]) == norm_list(http["orders_all"]) == norm_list(ct["orders_all"]) == norm_list(stdio["orders_all"])
    assert len(rest["orders_pending"]) == 1
    assert norm_list(rest["orders_pending"]) == norm_list(http["orders_pending"]) == norm_list(ct["orders_pending"]) == norm_list(stdio["orders_pending"])
    assert len(rest["approvals_before"]) == 1
    assert norm_list(rest["approvals_before"]) == norm_list(http["approvals_before"]) == norm_list(ct["approvals_before"]) == norm_list(stdio["approvals_before"])

    expected_forbidden = {"code": "forbidden", "text": "This route needs an admin key."}
    assert rest["decide_forbidden"] == http["decide_forbidden"] == ct["decide_forbidden"] == stdio["decide_forbidden"] == expected_forbidden

    assert rest["decided"]["status"] == "confirmed"
    assert _norm_order(rest["decided"]) == _norm_order(http["decided"]) == _norm_order(ct["decided"]) == _norm_order(stdio["decided"])

    assert rest["approvals_after"] == http["approvals_after"] == ct["approvals_after"] == stdio["approvals_after"] == []
    assert len(rest["orders_confirmed"]) == 2
    assert norm_list(rest["orders_confirmed"]) == norm_list(http["orders_confirmed"]) == norm_list(ct["orders_confirmed"]) == norm_list(stdio["orders_confirmed"])

    sig_rest = _verify_audit_trail(rest["trail_all"], jwks)
    sig_http = _verify_audit_trail(http["trail_all"], jwks)
    sig_ct = _verify_audit_trail(ct["trail_all"], jwks)
    sig_stdio = _verify_audit_trail(stdio["trail_all"], jwks)
    assert sig_rest == sig_http == sig_ct == sig_stdio

    ord_sig_rest = _verify_audit_trail(rest["trail_order"], jwks)
    ord_sig_http = _verify_audit_trail(http["trail_order"], jwks)
    ord_sig_ct = _verify_audit_trail(ct["trail_order"], jwks)
    ord_sig_stdio = _verify_audit_trail(stdio["trail_order"], jwks)
    assert ord_sig_rest == ord_sig_http == ord_sig_ct == ord_sig_stdio
    assert len(ord_sig_rest) < len(sig_rest)


def test_domain_and_auth_error_parity_across_rest_http_and_stdio(parity_ws, monkeypatch):
    delivery = parity_ws.delivery

    def collect_rest_errors(client: TestClient) -> dict[str, dict]:
        agent_h = {"X-Agent-Key": AGENT_KEY}
        admin_h = {"X-Admin-Key": ADMIN_KEY}

        r_unauth_agent = client.get("/v1/mandate")
        assert r_unauth_agent.status_code == 401
        r_unauth_admin = client.post(
            "/v1/approvals/ord_missing",
            json={"approve": True, "by": "Laura Virtanen"},
        )
        assert r_unauth_admin.status_code == 401

        r_forbid_admin = client.post(
            "/v1/approvals/ord_missing",
            json={"approve": True, "by": "Laura Virtanen"},
            headers=agent_h,
        )
        assert r_forbid_admin.status_code == 403
        r_forbid_scope = client.get("/v1/mandate", params={"customer": "hospital-tays"}, headers=agent_h)
        assert r_forbid_scope.status_code == 403

        r_not_found_decide = client.post(
            "/v1/approvals/ord_missing",
            json={"approve": True, "by": "Laura Virtanen"},
            headers=admin_h,
        )
        assert r_not_found_decide.status_code == 404
        r_not_found_prod = client.get("/v1/products/999999")
        assert r_not_found_prod.status_code == 404

        q_used = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "5001", "quantity": 20}], "delivery_date": delivery},
            headers=agent_h,
        ).json()
        first = client.post(
            "/v1/orders",
            json={"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            headers=agent_h,
        )
        assert first.status_code == 200
        r_quote_used = client.post(
            "/v1/orders",
            json={"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            headers=agent_h,
        )
        assert r_quote_used.status_code == 409

        q_pend = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "5001", "quantity": 25}], "delivery_date": delivery},
            headers=agent_h,
        ).json()
        o_pend = client.post(
            "/v1/orders",
            json={"quote_id": q_pend["quote_id"], "jws": q_pend["jws"], "ask_human": True, "note": "Check"},
            headers=agent_h,
        ).json()
        r_not_approver = client.post(
            f"/v1/approvals/{o_pend['order_id']}",
            json={"approve": True, "by": "Not In Mandate"},
            headers=admin_h,
        )
        assert r_not_approver.status_code == 403

        return {
            "unauthorized_agent": r_unauth_agent.json(),
            "unauthorized_admin": r_unauth_admin.json(),
            "forbidden_admin_route": r_forbid_admin.json(),
            "forbidden_scope": r_forbid_scope.json(),
            "NOT_FOUND": r_not_found_decide.json(),
            "not_found_product": r_not_found_prod.json(),
            "QUOTE_USED": r_quote_used.json(),
            "NOT_APPROVER": r_not_approver.json(),
        }

    def collect_http_mcp_errors(mcp_http: HttpMcpSession) -> dict[str, dict]:
        agent_h = {"X-Agent-Key": AGENT_KEY}
        admin_h = {"X-Admin-Key": ADMIN_KEY}

        unauth_agent = mcp_http.call_err("get_mandate", {})
        unauth_admin = mcp_http.call_err(
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
        )
        forbid_admin = mcp_http.call_err(
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
            headers=agent_h,
        )
        forbid_scope = mcp_http.call_err("get_mandate", {"customer": "hospital-tays"}, headers=agent_h)
        not_found_decide = mcp_http.call_err(
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
            headers=admin_h,
        )
        not_found_prod = mcp_http.call_err("get_product", {"product_id": "999999"})

        q_used = mcp_http.call_ok(
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 20}], "delivery_date": delivery},
            headers=agent_h,
        )
        mcp_http.call_ok("place_order", {"quote_id": q_used["quote_id"], "jws": q_used["jws"]}, headers=agent_h)
        quote_used = mcp_http.call_err(
            "place_order",
            {"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            headers=agent_h,
        )

        q_pend = mcp_http.call_ok(
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 25}], "delivery_date": delivery},
            headers=agent_h,
        )
        o_pend = mcp_http.call_ok(
            "place_order",
            {"quote_id": q_pend["quote_id"], "jws": q_pend["jws"], "ask_human": True, "note": "Check"},
            headers=agent_h,
        )
        not_approver = mcp_http.call_err(
            "decide_approval",
            {"order_id": o_pend["order_id"], "approve": True, "by": "Not In Mandate"},
            headers=admin_h,
        )

        return {
            "unauthorized_agent": unauth_agent,
            "unauthorized_admin": unauth_admin,
            "forbidden_admin_route": forbid_admin,
            "forbidden_scope": forbid_scope,
            "NOT_FOUND": not_found_decide,
            "not_found_product": not_found_prod,
            "QUOTE_USED": quote_used,
            "NOT_APPROVER": not_approver,
        }

    def collect_call_tool_errors() -> dict[str, dict]:
        unauth_agent = _call_tool_err(monkeypatch, "get_mandate", {})
        unauth_admin = _call_tool_err(
            monkeypatch,
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
        )
        forbid_admin = _call_tool_err(
            monkeypatch,
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
            agent_key=AGENT_KEY,
        )
        forbid_scope = _call_tool_err(
            monkeypatch,
            "get_mandate",
            {"customer": "hospital-tays"},
            agent_key=AGENT_KEY,
        )
        not_found_decide = _call_tool_err(
            monkeypatch,
            "decide_approval",
            {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
            admin_key=ADMIN_KEY,
        )
        not_found_prod = _call_tool_err(monkeypatch, "get_product", {"product_id": "999999"})

        q_used = _call_tool_ok(
            monkeypatch,
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 20}], "delivery_date": delivery},
            agent_key=AGENT_KEY,
        )
        _call_tool_ok(
            monkeypatch,
            "place_order",
            {"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            agent_key=AGENT_KEY,
        )
        quote_used = _call_tool_err(
            monkeypatch,
            "place_order",
            {"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            agent_key=AGENT_KEY,
        )

        q_pend = _call_tool_ok(
            monkeypatch,
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 25}], "delivery_date": delivery},
            agent_key=AGENT_KEY,
        )
        o_pend = _call_tool_ok(
            monkeypatch,
            "place_order",
            {"quote_id": q_pend["quote_id"], "jws": q_pend["jws"], "ask_human": True, "note": "Check"},
            agent_key=AGENT_KEY,
        )
        not_approver = _call_tool_err(
            monkeypatch,
            "decide_approval",
            {"order_id": o_pend["order_id"], "approve": True, "by": "Not In Mandate"},
            admin_key=ADMIN_KEY,
        )

        return {
            "unauthorized_agent": unauth_agent,
            "unauthorized_admin": unauth_admin,
            "forbidden_admin_route": forbid_admin,
            "forbidden_scope": forbid_scope,
            "NOT_FOUND": not_found_decide,
            "not_found_product": not_found_prod,
            "QUOTE_USED": quote_used,
            "NOT_APPROVER": not_approver,
        }

    def collect_stdio_errors(db_label: str) -> dict[str, dict]:
        async def unauth_phase(session: ClientSession) -> dict[str, dict]:
            return {
                "unauthorized_agent": await _session_err(session, "get_mandate", {}),
                "unauthorized_admin": await _session_err(
                    session,
                    "decide_approval",
                    {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
                ),
                "not_found_product": await _session_err(session, "get_product", {"product_id": "999999"}),
            }

        async def agent_phase(session: ClientSession) -> tuple[dict[str, dict], str]:
            forbid_admin = await _session_err(
                session,
                "decide_approval",
                {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
            )
            forbid_scope = await _session_err(session, "get_mandate", {"customer": "hospital-tays"})
            q_used = await _session_ok(
                session,
                "request_quote",
                {"offers": [{"product_id": "5001", "quantity": 20}], "delivery_date": delivery},
            )
            await _session_ok(session, "place_order", {"quote_id": q_used["quote_id"], "jws": q_used["jws"]})
            quote_used = await _session_err(
                session,
                "place_order",
                {"quote_id": q_used["quote_id"], "jws": q_used["jws"]},
            )
            q_pend = await _session_ok(
                session,
                "request_quote",
                {"offers": [{"product_id": "5001", "quantity": 25}], "delivery_date": delivery},
            )
            o_pend = await _session_ok(
                session,
                "place_order",
                {"quote_id": q_pend["quote_id"], "jws": q_pend["jws"], "ask_human": True, "note": "Check"},
            )
            return (
                {
                    "forbidden_admin_route": forbid_admin,
                    "forbidden_scope": forbid_scope,
                    "QUOTE_USED": quote_used,
                },
                o_pend["order_id"],
            )

        res = _run_stdio_session(parity_ws.stdio_env(db_label), unauth_phase)
        agent_errs, pend_order_id = _run_stdio_session(
            parity_ws.stdio_env(db_label, agent_key=AGENT_KEY),
            agent_phase,
        )
        res.update(agent_errs)

        async def admin_phase(session: ClientSession) -> dict[str, dict]:
            return {
                "NOT_FOUND": await _session_err(
                    session,
                    "decide_approval",
                    {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"},
                ),
                "NOT_APPROVER": await _session_err(
                    session,
                    "decide_approval",
                    {"order_id": pend_order_id, "approve": True, "by": "Not In Mandate"},
                ),
            }

        res.update(_run_stdio_session(parity_ws.stdio_env(db_label, admin_key=ADMIN_KEY), admin_phase))
        return res

    with TestClient(app) as client:
        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("err_rest"))
        rest_errs = collect_rest_errors(client)

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("err_http"))
        mcp_http = HttpMcpSession(client)
        http_errs = collect_http_mcp_errors(mcp_http)

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("err_ct"))
    ct_errs = collect_call_tool_errors()

    stdio_errs = collect_stdio_errors("err_stdio")

    assert rest_errs == http_errs == ct_errs == stdio_errs
    assert rest_errs["QUOTE_USED"]["code"] == "QUOTE_USED"
    assert rest_errs["NOT_APPROVER"]["code"] == "NOT_APPROVER"
    assert rest_errs["NOT_FOUND"]["code"] == "NOT_FOUND"
    assert rest_errs["not_found_product"]["code"] == "not_found"
    assert rest_errs["unauthorized_agent"]["code"] == "unauthorized"
    assert rest_errs["unauthorized_admin"]["code"] == "unauthorized"
    assert rest_errs["forbidden_admin_route"]["code"] == "forbidden"
    assert rest_errs["forbidden_scope"]["code"] == "forbidden"
    for err in rest_errs.values():
        assert set(err.keys()) == {"code", "text"}
        assert isinstance(err["text"], str) and err["text"].strip()


def test_reject_approval_and_cross_customer_scope_parity(parity_ws, monkeypatch):
    delivery = parity_ws.delivery
    agent_h = {"X-Agent-Key": AGENT_KEY}
    other_h = {"X-Agent-Key": OTHER_AGENT_KEY}
    admin_h = {"X-Admin-Key": ADMIN_KEY}

    with TestClient(app) as client:
        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("scope_rest"))
        r_admin_mandate = client.get("/v1/mandate", params={"customer": "hospital-tays"}, headers=admin_h).json()
        r_missing_mandate_resp = client.get("/v1/mandate", params={"customer": "ghost-customer"}, headers=admin_h)
        assert r_missing_mandate_resp.status_code == 404
        r_missing_mandate = r_missing_mandate_resp.json()

        rq = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "5001", "quantity": 40}], "delivery_date": delivery},
            headers=agent_h,
        ).json()
        ro = client.post(
            "/v1/orders",
            json={"quote_id": rq["quote_id"], "jws": rq["jws"], "ask_human": True, "note": "Budget review"},
            headers=agent_h,
        ).json()
        r_cross_order_resp = client.get(f"/v1/orders/{ro['order_id']}", headers=other_h)
        assert r_cross_order_resp.status_code == 403
        r_cross_order = r_cross_order_resp.json()
        r_rejected = client.post(
            f"/v1/approvals/{ro['order_id']}",
            json={"approve": False, "by": "Mikko Heikkinen", "note": "Deferred"},
            headers=admin_h,
        ).json()

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("scope_http"))
        mcp_http = HttpMcpSession(client)
        h_admin_mandate = mcp_http.call_ok("get_mandate", {"customer": "hospital-tays"}, headers=admin_h)
        h_missing_mandate = mcp_http.call_err("get_mandate", {"customer": "ghost-customer"}, headers=admin_h)

        hq = mcp_http.call_ok(
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 40}], "delivery_date": delivery},
            headers=agent_h,
        )
        ho = mcp_http.call_ok(
            "place_order",
            {"quote_id": hq["quote_id"], "jws": hq["jws"], "ask_human": True, "note": "Budget review"},
            headers=agent_h,
        )
        h_cross_order = mcp_http.call_err("get_order", {"order_id": ho["order_id"]}, headers=other_h)
        h_rejected = mcp_http.call_ok(
            "decide_approval",
            {"order_id": ho["order_id"], "approve": False, "by": "Mikko Heikkinen", "note": "Deferred"},
            headers=admin_h,
        )

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("scope_ct"))
    c_admin_mandate = _call_tool_ok(monkeypatch, "get_mandate", {"customer": "hospital-tays"}, admin_key=ADMIN_KEY)
    c_missing_mandate = _call_tool_err(
        monkeypatch,
        "get_mandate",
        {"customer": "ghost-customer"},
        admin_key=ADMIN_KEY,
    )
    cq = _call_tool_ok(
        monkeypatch,
        "request_quote",
        {"offers": [{"product_id": "5001", "quantity": 40}], "delivery_date": delivery},
        agent_key=AGENT_KEY,
    )
    co = _call_tool_ok(
        monkeypatch,
        "place_order",
        {"quote_id": cq["quote_id"], "jws": cq["jws"], "ask_human": True, "note": "Budget review"},
        agent_key=AGENT_KEY,
    )
    c_cross_order = _call_tool_err(
        monkeypatch,
        "get_order",
        {"order_id": co["order_id"]},
        agent_key=OTHER_AGENT_KEY,
    )
    c_rejected = _call_tool_ok(
        monkeypatch,
        "decide_approval",
        {"order_id": co["order_id"], "approve": False, "by": "Mikko Heikkinen", "note": "Deferred"},
        admin_key=ADMIN_KEY,
    )

    assert r_admin_mandate == h_admin_mandate == c_admin_mandate
    assert r_admin_mandate["customer"] == "hospital-tays"
    assert r_missing_mandate == h_missing_mandate == c_missing_mandate
    assert r_missing_mandate["code"] == "not_found"
    assert r_cross_order == h_cross_order == c_cross_order
    assert r_cross_order["code"] == "forbidden"
    assert r_rejected["status"] == "rejected"
    assert _norm_order(r_rejected) == _norm_order(h_rejected) == _norm_order(c_rejected)


def test_quote_signature_and_unknown_quote_error_parity(parity_ws, monkeypatch):
    delivery = parity_ws.delivery
    agent_h = {"X-Agent-Key": AGENT_KEY}

    def tamper(token: str) -> str:
        suffix = "AAAA" if not token.endswith("AAAA") else "BBBB"
        return token[:-4] + suffix

    with TestClient(app) as client:
        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("sig_rest"))
        rq = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "5001", "quantity": 10}], "delivery_date": delivery},
            headers=agent_h,
        ).json()
        r_bad_resp = client.post(
            "/v1/orders",
            json={"quote_id": rq["quote_id"], "jws": tamper(rq["jws"])},
            headers=agent_h,
        )
        assert r_bad_resp.status_code == 409
        r_unk_resp = client.post(
            "/v1/orders",
            json={"quote_id": "q_nonexistent", "jws": rq["jws"]},
            headers=agent_h,
        )
        assert r_unk_resp.status_code == 409

        monkeypatch.setenv("AIMO_DB", parity_ws.db_path("sig_http"))
        mcp_http = HttpMcpSession(client)
        hq = mcp_http.call_ok(
            "request_quote",
            {"offers": [{"product_id": "5001", "quantity": 10}], "delivery_date": delivery},
            headers=agent_h,
        )
        h_bad = mcp_http.call_err(
            "place_order",
            {"quote_id": hq["quote_id"], "jws": tamper(hq["jws"])},
            headers=agent_h,
        )
        h_unk = mcp_http.call_err(
            "place_order",
            {"quote_id": "q_nonexistent", "jws": hq["jws"]},
            headers=agent_h,
        )

    monkeypatch.setenv("AIMO_DB", parity_ws.db_path("sig_ct"))
    cq = _call_tool_ok(
        monkeypatch,
        "request_quote",
        {"offers": [{"product_id": "5001", "quantity": 10}], "delivery_date": delivery},
        agent_key=AGENT_KEY,
    )
    c_bad = _call_tool_err(
        monkeypatch,
        "place_order",
        {"quote_id": cq["quote_id"], "jws": tamper(cq["jws"])},
        agent_key=AGENT_KEY,
    )
    c_unk = _call_tool_err(
        monkeypatch,
        "place_order",
        {"quote_id": "q_nonexistent", "jws": cq["jws"]},
        agent_key=AGENT_KEY,
    )

    assert r_bad_resp.json() == h_bad == c_bad
    assert h_bad["code"] == "BAD_SIGNATURE"
    assert r_unk_resp.json() == h_unk == c_unk
    assert h_unk["code"] == "UNKNOWN_QUOTE"
