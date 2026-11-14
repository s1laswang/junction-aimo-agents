import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
import sys

from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from aimo import api, approvals, audit, catalog, keys, mandates, mcp_server, offers, orders, quotes, valio
from aimo.app import app
from aimo.approvals import ApprovalError
from aimo.models import Mandate, PlaceOrderRequest, Product, QuoteLine, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

SYNTHETIC_PRODUCTS = {
    "1001": Product(
        product_id="1001",
        name="Valio Laktoositon Kevytmaito 1 L",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose", "finnish"],
        origin="FI",
        temperature=4,
    ),
    "1002": Product(
        product_id="1002",
        name="Valio Luomu Kevytmaito laktoositon 1 L",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["organic", "free_from_lactose", "finnish"],
        origin="FI",
        temperature=4,
    ),
    "1003": Product(
        product_id="1003",
        name="Pahkina Granola Patukka",
        brand="NordicSnack",
        vendor="NORDICSNACK OY",
        category="90900",
        sales_unit="PCE",
        unit_size=UnitSize(value=1.0, unit="pcs"),
        allergens=["peanut", "nuts", "gluten"],
        labels=[],
        origin="SE",
        temperature=20,
    ),
}


class McpHttpClient:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid = None
        self.seq = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mandates-test", "version": "1.0"},
            },
        )
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, "mcp-session-id": self.sid},
        )
        assert r.status_code in (200, 202, 204), r.text

    def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        self.seq += 1
        req_headers = dict(MCP_HEADERS)
        if self.sid and method != "initialize":
            req_headers["mcp-session-id"] = self.sid
        if headers:
            req_headers.update(headers)
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            headers=req_headers,
        )
        assert r.status_code == 200, r.text
        self.sid = r.headers.get("mcp-session-id", self.sid)
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
        return json.loads(body)

    def call_ok(self, name: str, arguments: dict | None = None, headers: dict | None = None):
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        res = out["result"]
        assert not res.get("isError"), res
        blocks = [json.loads(b["text"]) for b in res.get("content", [])]
        if "structuredContent" in res and res["structuredContent"] is not None:
            sc = res["structuredContent"]
            if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], list):
                return sc["result"]
            return sc
        assert len(blocks) == 1, res
        return blocks[0]

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        return json.loads(text.split(prefix, 1)[-1])


class _StdioStubContext:
    headers = None


@pytest.fixture(autouse=True)
def isolated_offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_STDIO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()

    def fake_search(q="", constraints=None, limit=50):
        items = list(SYNTHETIC_PRODUCTS.values())
        if q:
            ql = q.lower()
            items = [
                p
                for p in items
                if ql in p.name.lower() or ("milk" in ql and "maito" in p.name.lower())
            ]
        if constraints and constraints.lactose_free:
            items = [p for p in items if "free_from_lactose" in p.labels]
        return items[:limit]

    monkeypatch.setattr(catalog, "get", lambda pid: SYNTHETIC_PRODUCTS.get(str(pid)))
    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.15 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})

    def fake_price(customer, requested_date, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": 1.50},
                "priceWithTax": {"value": 1.71},
            }
            for pid in ids
            if str(pid) in SYNTHETIC_PRODUCTS
        }

    def fake_availability(customer, requested_date, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "salesBlock": False,
                "closingTime": "2026-11-15T12:00:00+02:00",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 10000.0,
                        "availableDate": requested_date.isoformat(),
                        "salesUnit": SYNTHETIC_PRODUCTS[str(pid)].sales_unit,
                    }
                ],
            }
            for pid in ids
            if str(pid) in SYNTHETIC_PRODUCTS
        }

    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)

    yield tmp_path

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()


def _delivery_day(days_ahead: int = 3) -> date:
    return datetime.now(quotes.HELSINKI).date() + timedelta(days=days_ahead)


def _issue_quote_direct(
    customer="hotel-aurora",
    agent_id="buyer-aurora",
    product_id="1001",
    quantity=100.0,
    unit_price_net=1.50,
    substitute_for=None,
):
    now = datetime.now(timezone.utc)
    prod = SYNTHETIC_PRODUCTS[product_id]
    net = round(quantity * unit_price_net, 2)
    gross = round(net * 1.14, 2)
    line = QuoteLine(
        product_id=product_id,
        name=prod.name,
        quantity=quantity,
        sales_unit=prod.sales_unit,
        unit_price_net=unit_price_net,
        line_total_net=net,
        substitute_for=substitute_for,
    )
    return quotes.issue(
        customer=customer,
        agent_id=agent_id,
        lines=[line],
        delivery_date=_delivery_day(),
        valid_until=now + timedelta(hours=3),
        total_gross=gross,
    )


def _stdio_subprocess_call(env_vars: dict[str, str], tool_name: str, arguments: dict):
    base_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VALIO_API_BASE", "VALIO_API_KEY", "AIMO_AGENT_KEY", "AIMO_STDIO_ADMIN_KEY")
    }
    base_env.update(env_vars)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aimo.mcp_server"],
            env=base_env,
        )
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    return asyncio.run(run())


def test_custom_agent_keys_authenticates_rest_and_mcp_and_rejects_demo(monkeypatch):
    custom_map = {"custom-aurora-key": ["hotel-aurora", "buyer-aurora"]}
    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps(custom_map))

    resolved = mandates.agent_for_key("custom-aurora-key")
    assert resolved is not None
    assert resolved[0] == "hotel-aurora"
    assert resolved[1] == "buyer-aurora"
    assert resolved[2].customer == "hotel-aurora"
    assert mandates.agent_for_key("demo-hotel-aurora") is None
    assert mandates.agent_for_key("demo-hospital-tays") is None
    assert mandates.agent_for_key("demo-school-hervanta") is None
    assert mandates.agent_for_key("") is None

    delivery = _delivery_day().isoformat()
    custom_hdr = {"X-Agent-Key": "custom-aurora-key"}
    disabled_hdr = {"X-Agent-Key": "demo-hotel-aurora"}

    with TestClient(app) as client:
        mcp = McpHttpClient(client)

        for disabled in (disabled_hdr, {"X-Agent-Key": "demo-hospital-tays"}, {}):
            assert client.get("/v1/mandate", headers=disabled).status_code == 401
            assert client.get("/v1/orders", headers=disabled).status_code == 401
            assert client.get("/v1/approvals", headers=disabled).status_code == 401
            assert client.get("/v1/audit", headers=disabled).status_code == 401
            assert (
                client.post(
                    "/v1/offers",
                    headers=disabled,
                    json={
                        "customer": "hotel-aurora",
                        "deliver_by": delivery,
                        "items": [{"query": "milk", "quantity": 50, "unit": "l"}],
                    },
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/v1/quotes",
                    headers=disabled,
                    json={"offers": [{"product_id": "1001", "quantity": 50}], "delivery_date": delivery},
                ).status_code
                == 401
            )

        r_mandate = client.get("/v1/mandate", headers=custom_hdr)
        assert r_mandate.status_code == 200
        assert r_mandate.json()["customer"] == "hotel-aurora"
        assert r_mandate.json()["agent_id"] == "buyer-aurora"

        r_cross = client.get("/v1/mandate?customer=hospital-tays", headers=custom_hdr)
        assert r_cross.status_code == 403
        assert r_cross.json()["code"] == "forbidden"

        r_parse_disabled = client.post(
            "/v1/needs/parse",
            headers=disabled_hdr,
            json={"text": "50 litres of milk", "customer": "hotel-aurora"},
        )
        assert r_parse_disabled.status_code == 200

        r_parse_custom = client.post(
            "/v1/needs/parse",
            headers=custom_hdr,
            json={"text": "60 litres of milk", "customer": "hotel-aurora"},
        )
        assert r_parse_custom.status_code == 200
        parse_events = [e for e in audit.events(customer="hotel-aurora") if e.action == "parse.request"]
        assert parse_events[-2].actor == "system"
        assert parse_events[-1].actor == "agent:buyer-aurora"

        r_quote = client.post(
            "/v1/quotes",
            headers=custom_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 100}], "delivery_date": delivery},
        )
        assert r_quote.status_code == 200, r_quote.text
        q_body = r_quote.json()

        r_place_disabled = client.post(
            "/v1/orders",
            headers=disabled_hdr,
            json={"quote_id": q_body["quote_id"], "jws": q_body["jws"]},
        )
        assert r_place_disabled.status_code == 401
        assert r_place_disabled.json()["code"] == "unauthorized"

        r_place_ok = client.post(
            "/v1/orders",
            headers=custom_hdr,
            json={"quote_id": q_body["quote_id"], "jws": q_body["jws"]},
        )
        assert r_place_ok.status_code == 200, r_place_ok.text
        assert r_place_ok.json()["status"] == "confirmed"

        err_demo_mcp = mcp.call_err("get_mandate", {}, headers=disabled_hdr)
        assert err_demo_mcp["code"] == "unauthorized"
        err_missing_mcp = mcp.call_err("get_mandate", {})
        assert err_missing_mcp["code"] == "unauthorized"

        m_mandate = mcp.call_ok("get_mandate", {}, headers=custom_hdr)
        assert m_mandate["customer"] == "hotel-aurora"
        assert m_mandate["agent_id"] == "buyer-aurora"

        m_quote = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "1001", "quantity": 80}], "delivery_date": delivery},
            headers=custom_hdr,
        )
        assert m_quote["customer"] == "hotel-aurora"

        err_demo_place = mcp.call_err(
            "place_order",
            {"quote_id": m_quote["quote_id"], "jws": m_quote["jws"]},
            headers=disabled_hdr,
        )
        assert err_demo_place["code"] == "unauthorized"

        m_order = mcp.call_ok(
            "place_order",
            {"quote_id": m_quote["quote_id"], "jws": m_quote["jws"]},
            headers=custom_hdr,
        )
        assert m_order["status"] == "confirmed"


def test_custom_keys_mcp_stdio_inproc_and_subprocess(monkeypatch):
    custom_map = json.dumps({"custom-aurora-key": ["hotel-aurora", "buyer-aurora"]})
    monkeypatch.setenv("AIMO_AGENT_KEYS", custom_map)
    monkeypatch.setenv("AIMO_ADMIN_KEY", "prod-secret-admin")

    ctx = _StdioStubContext()

    monkeypatch.setenv("AIMO_AGENT_KEY", "custom-aurora-key")
    mandate_out = mcp_server.get_mandate(ctx)
    assert mandate_out["customer"] == "hotel-aurora"
    assert mandate_out["agent_id"] == "buyer-aurora"

    monkeypatch.setenv("AIMO_AGENT_KEY", "demo-hotel-aurora")
    with pytest.raises(ToolError) as exc_disabled:
        mcp_server.get_mandate(ctx)
    assert json.loads(str(exc_disabled.value))["code"] == "unauthorized"

    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", "demo-admin")
    with pytest.raises(ToolError) as exc_bad_admin:
        mcp_server.get_mandate(ctx, customer="hospital-tays")
    assert json.loads(str(exc_bad_admin.value))["code"] == "unauthorized"

    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", "prod-secret-admin")
    tays_mandate = mcp_server.get_mandate(ctx, customer="hospital-tays")
    assert tays_mandate["customer"] == "hospital-tays"

    sub_ok = _stdio_subprocess_call(
        {
            "AIMO_AGENT_KEYS": custom_map,
            "AIMO_AGENT_KEY": "custom-aurora-key",
        },
        "get_mandate",
        {},
    )
    assert not sub_ok.is_error
    sub_payload = json.loads(sub_ok.content[0].text)
    assert sub_payload["customer"] == "hotel-aurora"
    assert sub_payload["agent_id"] == "buyer-aurora"

    sub_rejected = _stdio_subprocess_call(
        {
            "AIMO_AGENT_KEYS": custom_map,
            "AIMO_AGENT_KEY": "demo-hotel-aurora",
        },
        "get_mandate",
        {},
    )
    assert sub_rejected.is_error
    assert "unauthorized" in sub_rejected.content[0].text


@pytest.mark.parametrize(
    "raw_value",
    [
        "",
        "   ",
        "{invalid-json",
        "[1,2]",
        '["hotel-aurora", "buyer-aurora"]',
        "42",
        "null",
        "true",
        '"scalar-string"',
    ],
)
def test_malformed_or_non_dict_agent_keys_falls_back_to_demo_keys(monkeypatch, raw_value):
    monkeypatch.setenv("AIMO_AGENT_KEYS", raw_value)
    assert mandates._keys() == mandates._DEMO_KEYS

    resolved = mandates.agent_for_key("demo-hotel-aurora")
    assert resolved is not None
    assert resolved[0] == "hotel-aurora"
    assert resolved[1] == "buyer-aurora"

    with TestClient(app) as client:
        mcp = McpHttpClient(client)
        r = client.get("/v1/mandate", headers={"X-Agent-Key": "demo-hotel-aurora"})
        assert r.status_code == 200
        assert r.json()["customer"] == "hotel-aurora"

        m = mcp.call_ok("get_mandate", {}, headers={"X-Agent-Key": "demo-hotel-aurora"})
        assert m["customer"] == "hotel-aurora"


def test_agent_keys_type_error_short_lists_and_unknown_pairs(monkeypatch):
    orig_env_get = mandates.os.environ.get
    with monkeypatch.context() as mp:
        mp.setattr(
            mandates.os.environ,
            "get",
            lambda k, default=None: 12345 if k == "AIMO_AGENT_KEYS" else orig_env_get(k, default),
        )
        assert mandates._keys() == mandates._DEMO_KEYS
        assert mandates.agent_for_key("demo-hotel-aurora") is not None

    tricky_mapping = {
        "empty-list": [],
        "short-list": ["hotel-aurora"],
        "str-entry": "hotel-aurora",
        "int-entry": 7,
        "null-entry": None,
        "dict-entry": {"customer": "hotel-aurora", "agent_id": "buyer-aurora"},
        "unknown-customer": ["nonexistent-customer", "buyer-aurora"],
        "unknown-agent": ["hotel-aurora", "nonexistent-agent"],
        "unknown-both": ["ghost-corp", "ghost-buyer"],
        "valid-with-extra": ["school-hervanta", "buyer-hervanta", "extra-tag"],
    }
    monkeypatch.setenv("AIMO_AGENT_KEYS", json.dumps(tricky_mapping))

    bad_keys = [
        "empty-list",
        "short-list",
        "str-entry",
        "int-entry",
        "null-entry",
        "dict-entry",
        "unknown-customer",
        "unknown-agent",
        "unknown-both",
        "demo-hotel-aurora",
        "unmapped-key",
    ]
    for k in bad_keys:
        assert mandates.agent_for_key(k) is None

    good = mandates.agent_for_key("valid-with-extra")
    assert good is not None
    assert good[0] == "school-hervanta"
    assert good[1] == "buyer-hervanta"

    with TestClient(app) as client:
        mcp = McpHttpClient(client)
        for k in bad_keys:
            r = client.get("/v1/mandate", headers={"X-Agent-Key": k})
            assert r.status_code == 401
            assert r.json()["code"] == "unauthorized"
            err = mcp.call_err("get_mandate", {}, headers={"X-Agent-Key": k})
            assert err["code"] == "unauthorized"

        r_good = client.get("/v1/mandate", headers={"X-Agent-Key": "valid-with-extra"})
        assert r_good.status_code == 200
        assert r_good.json()["customer"] == "school-hervanta"


def test_custom_admin_key_enforced_across_all_admin_routes_and_mcp(monkeypatch):
    monkeypatch.setenv("AIMO_ADMIN_KEY", "prod-secret-admin")

    q1 = _issue_quote_direct(quantity=2000.0, unit_price_net=1.50)
    ord1 = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q1.quote_id, jws=q1.jws))
    assert ord1.status == "pending_approval"

    q2 = _issue_quote_direct(quantity=2100.0, unit_price_net=1.50)
    ord2 = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q2.quote_id, jws=q2.jws))
    assert ord2.status == "pending_approval"

    aurora_mandate = mandates.get("hotel-aurora", "buyer-aurora")
    updated_payload = aurora_mandate.model_copy(update={"per_order_eur": 2600.0}).model_dump()
    sim_payload = {"product_id": "1001", "available_amount": 25.0}
    decide_payload = {"approve": True, "by": "Laura Virtanen", "note": "prod key check"}

    prod_admin = {"X-Admin-Key": "prod-secret-admin"}
    demo_admin = {"X-Admin-Key": "demo-admin"}
    agent_hdr = {"X-Agent-Key": "demo-hotel-aurora"}

    with TestClient(app) as client:
        mcp = McpHttpClient(client)

        for unauth_hdr in (demo_admin, {"X-Admin-Key": "wrong-key"}, {}):
            r_put = client.put("/v1/mandate", headers=unauth_hdr, json=updated_payload)
            assert r_put.status_code == 401
            assert r_put.json()["code"] == "unauthorized"

            r_app = client.post(f"/v1/approvals/{ord1.order_id}", headers=unauth_hdr, json=decide_payload)
            assert r_app.status_code == 401
            assert r_app.json()["code"] == "unauthorized"

            r_sim = client.post("/v1/admin/simulate", headers=unauth_hdr, json=sim_payload)
            assert r_sim.status_code == 401
            assert r_sim.json()["code"] == "unauthorized"

            r_clr = client.delete("/v1/admin/simulate", headers=unauth_hdr)
            assert r_clr.status_code == 401
            assert r_clr.json()["code"] == "unauthorized"

        assert client.put("/v1/mandate", headers=agent_hdr, json=updated_payload).status_code == 403
        assert client.post(f"/v1/approvals/{ord1.order_id}", headers=agent_hdr, json=decide_payload).status_code == 403
        assert client.post("/v1/admin/simulate", headers=agent_hdr, json=sim_payload).status_code == 403
        assert client.delete("/v1/admin/simulate", headers=agent_hdr).status_code == 403

        err_demo_mcp = mcp.call_err(
            "decide_approval",
            {"order_id": ord2.order_id, "approve": True, "by": "Laura Virtanen"},
            headers=demo_admin,
        )
        assert err_demo_mcp["code"] == "unauthorized"

        err_agent_mcp = mcp.call_err(
            "decide_approval",
            {"order_id": ord2.order_id, "approve": True, "by": "Laura Virtanen"},
            headers=agent_hdr,
        )
        assert err_agent_mcp["code"] == "forbidden"

        assert mandates.get("hotel-aurora", "buyer-aurora").per_order_eur == 2500.0
        assert orders.get(ord1.order_id).status == "pending_approval"
        assert offers._overrides == {}

        r_put_ok = client.put("/v1/mandate", headers=prod_admin, json=updated_payload)
        assert r_put_ok.status_code == 200, r_put_ok.text
        assert r_put_ok.json()["per_order_eur"] == 2600.0
        assert mandates.get("hotel-aurora", "buyer-aurora").per_order_eur == 2600.0

        r_sim_ok = client.post("/v1/admin/simulate", headers=prod_admin, json=sim_payload)
        assert r_sim_ok.status_code == 200
        assert r_sim_ok.json() == {"ok": True}
        assert "1001" in offers._overrides

        r_clr_ok = client.delete("/v1/admin/simulate", headers=prod_admin)
        assert r_clr_ok.status_code == 200
        assert r_clr_ok.json() == {"ok": True}
        assert offers._overrides == {}

        r_app_ok = client.post(f"/v1/approvals/{ord1.order_id}", headers=prod_admin, json=decide_payload)
        assert r_app_ok.status_code == 200, r_app_ok.text
        assert r_app_ok.json()["status"] == "confirmed"

        m_app_ok = mcp.call_ok(
            "decide_approval",
            {"order_id": ord2.order_id, "approve": False, "by": "Mikko Heikkinen", "note": "rejected via mcp"},
            headers=prod_admin,
        )
        assert m_app_ok["status"] == "rejected"


@pytest.mark.parametrize(
    "per_order_eur,per_week_eur",
    [
        (0.0, 15000.0),
        (-0.01, 15000.0),
        (-500.0, 15000.0),
        (500.0, 0.0),
        (500.0, -1.0),
        (0.0, 0.0),
        (-200.0, -100.0),
        (15000.01, 15000.0),
        (5000.0, 4999.99),
    ],
)
def test_put_mandate_rejects_invalid_limits_and_preserves_state(per_order_eur, per_week_eur):
    before = mandates.get("hotel-aurora", "buyer-aurora")
    invalid = before.model_copy(update={"per_order_eur": per_order_eur, "per_week_eur": per_week_eur})

    with pytest.raises(ApprovalError) as py_exc:
        mandates.put(invalid)
    assert py_exc.value.code == "INVALID_MANDATE"
    assert py_exc.value.text

    with pytest.raises(api.Err) as domain_exc:
        api.domain(mandates.put, invalid)
    assert domain_exc.value.status == 409
    assert domain_exc.value.body["code"] == "INVALID_MANDATE"

    with TestClient(app) as client:
        r = client.put("/v1/mandate", headers={"X-Admin-Key": "demo-admin"}, json=invalid.model_dump())
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "INVALID_MANDATE"
        assert body["text"]

        after_http = client.get("/v1/mandate?customer=hotel-aurora", headers={"X-Admin-Key": "demo-admin"})
        assert after_http.status_code == 200
        assert after_http.json() == before.model_dump()

    assert mandates.get("hotel-aurora", "buyer-aurora") == before
    assert [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"] == []


def test_put_mandate_non_finite_python_and_equal_boundary_rest():
    before = mandates.get("school-hervanta", "buyer-hervanta")
    for bad_order, bad_week in [
        (math.nan, 3000.0),
        (math.inf, 3000.0),
        (-math.inf, 3000.0),
        (500.0, math.nan),
        (500.0, math.inf),
    ]:
        bad = before.model_copy(update={"per_order_eur": bad_order, "per_week_eur": bad_week})
        with pytest.raises(ApprovalError) as exc:
            mandates.put(bad)
        assert exc.value.code == "INVALID_MANDATE"

    assert mandates.get("school-hervanta", "buyer-hervanta") == before

    with TestClient(app) as client:
        equal_limits = before.model_copy(update={"per_order_eur": 1200.0, "per_week_eur": 1200.0})
        r = client.put("/v1/mandate", headers={"X-Admin-Key": "demo-admin"}, json=equal_limits.model_dump())
        assert r.status_code == 200, r.text
        assert r.json()["per_order_eur"] == 1200.0
        assert r.json()["per_week_eur"] == 1200.0


def test_mandate_updated_audit_diff_no_changes_and_narration():
    admin_hdr = {"X-Admin-Key": "demo-admin"}
    base = mandates.get("hotel-aurora", "buyer-aurora")

    changed = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=1800.0,
        per_week_eur=12000.0,
        exclude_allergens=["peanut", "sesame"],
        allowed_categories=["10400"],
        substitutions="never",
        preferred_brands=["Valio", "Oddlygood"],
        approvers=["Laura Virtanen", "Tiina Laine"],
    )

    with TestClient(app) as client:
        r1 = client.put("/v1/mandate", headers=admin_hdr, json=changed.model_dump())
        assert r1.status_code == 200, r1.text

        r2 = client.put("/v1/mandate", headers=admin_hdr, json=changed.model_dump())
        assert r2.status_code == 200, r2.text

        minor = changed.model_copy(update={"per_order_eur": 1950.5})
        mandates.put(minor, actor="human:cfo")

        trail_resp = client.get("/v1/audit?customer=hotel-aurora", headers=admin_hdr)
        assert trail_resp.status_code == 200
        trail = trail_resp.json()
        assert trail["verified"] == {"ok": True, "bad_seq": None}
        jwks_doc = client.get("/.well-known/jwks.json").json()
        cp_head = audit.verify_checkpoint(trail["checkpoint"]["jws"], jwks_doc)
        assert cp_head["seq"] == trail["checkpoint"]["seq"]

    evs = [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"]
    assert len(evs) == 3

    e1, e2, e3 = evs
    assert e1.actor == "human:admin"
    assert e1.subject == "hotel-aurora"
    assert e1.detail["agent_id"] == "buyer-aurora"
    assert e1.detail["new"]["per_order_eur"] == 1800
    assert e1.why.startswith("Mandate for hotel-aurora/buyer-aurora updated: ")
    expected_fragments = [
        f"per_order_eur {base.per_order_eur} -> 1800.0",
        f"per_week_eur {base.per_week_eur} -> 12000.0",
        "exclude_allergens [] -> ['peanut', 'sesame']",
        "allowed_categories None -> ['10400']",
        "substitutions within_prefs -> never",
        "preferred_brands ['Valio'] -> ['Valio', 'Oddlygood']",
        "approvers ['Laura Virtanen', 'Mikko Heikkinen'] -> ['Laura Virtanen', 'Tiina Laine']",
    ]
    for frag in expected_fragments:
        assert frag in e1.why
    assert "customer " not in e1.why
    assert "agent_id " not in e1.why

    assert e2.why == "Mandate for hotel-aurora/buyer-aurora updated: no changes"
    assert e3.actor == "human:cfo"
    assert e3.why == "Mandate for hotel-aurora/buyer-aurora updated: per_order_eur 1800.0 -> 1950.5"

    buyer_narration = audit.narrate(evs, view="buyer")
    assert "Admin had its buying mandate updated." in buyer_narration
    assert "Cfo had its buying mandate updated." in buyer_narration
    assert "per_order_eur 2500.0 -> 1800.0" in buyer_narration
    assert "updated: no changes." in buyer_narration

    valio_narration = audit.narrate(evs, view="valio")
    assert "Admin at Hotel Aurora had its buying mandate updated." in valio_narration


def test_create_brand_new_mandates_and_operate_immediately_once_mapped(monkeypatch):
    admin_hdr = {"X-Admin-Key": "demo-admin"}
    delivery = _delivery_day().isoformat()

    new_customer_mandate = Mandate(
        customer="bistro-tampere",
        agent_id="buyer-bistro",
        per_order_eur=400.0,
        per_week_eur=2000.0,
        exclude_allergens=["peanut"],
        allowed_categories=["10400"],
        substitutions="ask",
        preferred_brands=["Valio"],
        approvers=["Eeva Maki"],
    )
    second_aurora_agent = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora-night",
        per_order_eur=200.0,
        per_week_eur=1000.0,
        exclude_allergens=[],
        allowed_categories=["10400"],
        substitutions="within_prefs",
        preferred_brands=["Valio"],
        approvers=["Laura Virtanen"],
    )

    with TestClient(app) as client:
        assert client.get("/v1/mandate?customer=bistro-tampere", headers=admin_hdr).status_code == 404

        r_create = client.put("/v1/mandate", headers=admin_hdr, json=new_customer_mandate.model_dump())
        assert r_create.status_code == 200, r_create.text
        assert r_create.json() == new_customer_mandate.model_dump()

        r_create_second = client.put("/v1/mandate", headers=admin_hdr, json=second_aurora_agent.model_dump())
        assert r_create_second.status_code == 200, r_create_second.text

        created_ev = [e for e in audit.events(customer="bistro-tampere") if e.action == "mandate_updated"]
        assert len(created_ev) == 1
        assert created_ev[0].why == "Mandate for bistro-tampere/buyer-bistro created"
        assert created_ev[0].detail["agent_id"] == "buyer-bistro"

        aurora_evs = [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"]
        assert len(aurora_evs) == 1
        assert aurora_evs[0].why == "Mandate for hotel-aurora/buyer-aurora-night created"
        assert mandates.get("hotel-aurora", "buyer-aurora").per_order_eur == 2500.0
        assert mandates.get("hotel-aurora", "buyer-aurora-night").per_order_eur == 200.0

        card = client.get("/.well-known/agent-card.json").json()
        pairs = {(m["customer"], m["agent_id"]) for m in card["policies"]["mandates"]}
        assert ("bistro-tampere", "buyer-bistro") in pairs
        assert ("hotel-aurora", "buyer-aurora-night") in pairs

        monkeypatch.setenv(
            "AIMO_AGENT_KEYS",
            json.dumps(
                {
                    "bistro-key": ["bistro-tampere", "buyer-bistro"],
                    "aurora-day-key": ["hotel-aurora", "buyer-aurora"],
                    "aurora-night-key": ["hotel-aurora", "buyer-aurora-night"],
                }
            ),
        )
        bistro_hdr = {"X-Agent-Key": "bistro-key"}
        night_hdr = {"X-Agent-Key": "aurora-night-key"}
        mcp = McpHttpClient(client)

        r_m = client.get("/v1/mandate", headers=bistro_hdr)
        assert r_m.status_code == 200
        assert r_m.json()["customer"] == "bistro-tampere"
        assert r_m.json()["per_order_eur"] == 400.0

        m_night = mcp.call_ok("get_mandate", {}, headers=night_hdr)
        assert m_night["agent_id"] == "buyer-aurora-night"
        assert m_night["per_order_eur"] == 200.0

        q_auto = client.post(
            "/v1/quotes",
            headers=bistro_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 100}], "delivery_date": delivery},
        ).json()
        o_auto = client.post(
            "/v1/orders",
            headers=bistro_hdr,
            json={"quote_id": q_auto["quote_id"], "jws": q_auto["jws"]},
        )
        assert o_auto.status_code == 200
        assert o_auto.json()["decision"]["outcome"] == "auto"
        assert o_auto.json()["status"] == "confirmed"

        q_over = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "1001", "quantity": 300}], "delivery_date": delivery},
            headers=bistro_hdr,
        )
        o_over = mcp.call_ok(
            "place_order",
            {"quote_id": q_over["quote_id"], "jws": q_over["jws"]},
            headers=bistro_hdr,
        )
        assert o_over["decision"]["outcome"] == "needs_approval"
        assert o_over["status"] == "pending_approval"
        assert any(r["code"] == "OVER_ORDER_LIMIT" for r in o_over["decision"]["reasons"])

        r_approve = client.post(
            f"/v1/approvals/{o_over['order_id']}",
            headers=admin_hdr,
            json={"approve": True, "by": "Eeva Maki", "note": "Welcome order"},
        )
        assert r_approve.status_code == 200, r_approve.text
        assert r_approve.json()["status"] == "confirmed"


def test_live_policy_updates_flip_auto_order_to_needs_approval_and_blocked():
    admin_hdr = {"X-Admin-Key": "demo-admin"}
    agent_hdr = {"X-Agent-Key": "demo-hotel-aurora"}
    delivery = _delivery_day().isoformat()

    with TestClient(app) as client:
        base = Mandate.model_validate(client.get("/v1/mandate", headers=agent_hdr).json())
        assert base.per_order_eur == 2500.0
        assert base.per_week_eur == 15000.0

        q1 = client.post(
            "/v1/quotes",
            headers=agent_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 600}], "delivery_date": delivery},
        ).json()
        assert q1["total_net"] == 900.0
        o1 = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q1["quote_id"], "jws": q1["jws"]},
        ).json()
        assert o1["decision"]["outcome"] == "auto"
        assert o1["status"] == "confirmed"

        tighter_order = base.model_copy(update={"per_order_eur": 500.0})
        r_put1 = client.put("/v1/mandate", headers=admin_hdr, json=tighter_order.model_dump())
        assert r_put1.status_code == 200

        q2 = client.post(
            "/v1/quotes",
            headers=agent_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 600}], "delivery_date": delivery},
        ).json()
        o2 = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q2["quote_id"], "jws": q2["jws"]},
        ).json()
        assert o2["decision"]["outcome"] == "needs_approval"
        assert o2["status"] == "pending_approval"
        assert [r["code"] for r in o2["decision"]["reasons"]] == ["OVER_ORDER_LIMIT"]

        tighter_week = base.model_copy(update={"per_order_eur": 1200.0, "per_week_eur": 1500.0})
        r_put2 = client.put("/v1/mandate", headers=admin_hdr, json=tighter_week.model_dump())
        assert r_put2.status_code == 200

        q3 = client.post(
            "/v1/quotes",
            headers=agent_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 500}], "delivery_date": delivery},
        ).json()
        assert q3["total_net"] == 750.0
        o3 = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q3["quote_id"], "jws": q3["jws"]},
        ).json()
        assert o3["decision"]["outcome"] == "needs_approval"
        assert o3["status"] == "pending_approval"
        assert [r["code"] for r in o3["decision"]["reasons"]] == ["OVER_WEEK_LIMIT"]

        allergen_block = base.model_copy(update={"exclude_allergens": ["milk"]})
        r_put3 = client.put("/v1/mandate", headers=admin_hdr, json=allergen_block.model_dump())
        assert r_put3.status_code == 200

        q4 = client.post(
            "/v1/quotes",
            headers=agent_hdr,
            json={"offers": [{"product_id": "1001", "quantity": 50}], "delivery_date": delivery},
        ).json()
        o4 = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q4["quote_id"], "jws": q4["jws"]},
        ).json()
        assert o4["decision"]["outcome"] == "blocked"
        assert o4["status"] == "blocked"
        assert any(r["code"] == "ALLERGEN_CONFLICT" for r in o4["decision"]["reasons"])

    assert audit.verify() == (True, None)


def test_live_adding_and_removing_approvers_controls_approval_decisions():
    admin_hdr = {"X-Admin-Key": "demo-admin"}

    q1 = _issue_quote_direct(quantity=2000.0, unit_price_net=1.50)
    ord1 = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q1.quote_id, jws=q1.jws))
    assert ord1.status == "pending_approval"

    q2 = _issue_quote_direct(quantity=2000.0, unit_price_net=1.50)
    ord2 = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q2.quote_id, jws=q2.jws))
    assert ord2.status == "pending_approval"

    base = mandates.get("hotel-aurora", "buyer-aurora")
    assert "Olli Salonen" not in base.approvers

    with TestClient(app) as client:
        mcp = McpHttpClient(client)

        r_before = client.post(
            f"/v1/approvals/{ord1.order_id}",
            headers=admin_hdr,
            json={"approve": True, "by": "Olli Salonen", "note": "not yet added"},
        )
        assert r_before.status_code == 403
        assert r_before.json()["code"] == "NOT_APPROVER"
        assert orders.get(ord1.order_id).status == "pending_approval"

        with_olli = base.model_copy(update={"approvers": [*base.approvers, "Olli Salonen"]})
        r_add = client.put("/v1/mandate", headers=admin_hdr, json=with_olli.model_dump())
        assert r_add.status_code == 200

        r_olli_ok = client.post(
            f"/v1/approvals/{ord1.order_id}",
            headers=admin_hdr,
            json={"approve": True, "by": "Olli Salonen", "note": "now authorized"},
        )
        assert r_olli_ok.status_code == 200, r_olli_ok.text
        assert r_olli_ok.json()["status"] == "confirmed"

        approved_ev = [e for e in audit.events(order_id=ord1.order_id) if e.action == "approval.approved"]
        assert len(approved_ev) == 1
        assert approved_ev[0].actor == "human:Olli Salonen"

        only_laura = with_olli.model_copy(update={"approvers": ["Laura Virtanen"]})
        r_remove = client.put("/v1/mandate", headers=admin_hdr, json=only_laura.model_dump())
        assert r_remove.status_code == 200

        r_olli_removed = client.post(
            f"/v1/approvals/{ord2.order_id}",
            headers=admin_hdr,
            json={"approve": True, "by": "Olli Salonen", "note": "revoked"},
        )
        assert r_olli_removed.status_code == 403
        assert r_olli_removed.json()["code"] == "NOT_APPROVER"

        r_mikko_removed = client.post(
            f"/v1/approvals/{ord2.order_id}",
            headers=admin_hdr,
            json={"approve": True, "by": "Mikko Heikkinen", "note": "also revoked"},
        )
        assert r_mikko_removed.status_code == 403
        assert r_mikko_removed.json()["code"] == "NOT_APPROVER"

        with pytest.raises(ApprovalError) as py_na:
            approvals.decide(ord2.order_id, True, "Olli Salonen", "python check")
        assert py_na.value.code == "NOT_APPROVER"

        m_na = mcp.call_err(
            "decide_approval",
            {"order_id": ord2.order_id, "approve": True, "by": "Olli Salonen"},
            headers=admin_hdr,
        )
        assert m_na["code"] == "NOT_APPROVER"
        assert orders.get(ord2.order_id).status == "pending_approval"

        r_laura_ok = client.post(
            f"/v1/approvals/{ord2.order_id}",
            headers=admin_hdr,
            json={"approve": True, "by": "Laura Virtanen", "note": "still authorized"},
        )
        assert r_laura_ok.status_code == 200, r_laura_ok.text
        assert r_laura_ok.json()["status"] == "confirmed"

    mandate_evs = [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"]
    assert len(mandate_evs) == 2
    assert "Olli Salonen" in mandate_evs[0].why
    assert "-> ['Laura Virtanen']" in mandate_evs[1].why
    assert audit.verify() == (True, None)


def test_live_substitutions_categories_and_stdio_new_mandate_persistence(monkeypatch):
    admin_hdr = {"X-Admin-Key": "demo-admin"}
    agent_hdr = {"X-Agent-Key": "demo-hotel-aurora"}

    with TestClient(app) as client:
        base = mandates.get("hotel-aurora", "buyer-aurora")
        assert base.substitutions == "within_prefs"

        q_sub_ok = _issue_quote_direct(product_id="1002", quantity=50.0, substitute_for="1001")
        o_sub_ok = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q_sub_ok.quote_id, "jws": q_sub_ok.jws},
        ).json()
        assert o_sub_ok["decision"]["outcome"] == "auto"
        assert o_sub_ok["status"] == "confirmed"

        ask_subs = base.model_copy(update={"substitutions": "ask"})
        assert client.put("/v1/mandate", headers=admin_hdr, json=ask_subs.model_dump()).status_code == 200

        q_sub_ask = _issue_quote_direct(product_id="1002", quantity=50.0, substitute_for="1001")
        o_sub_ask = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q_sub_ask.quote_id, "jws": q_sub_ask.jws},
        ).json()
        assert o_sub_ask["decision"]["outcome"] == "needs_approval"
        assert o_sub_ask["status"] == "pending_approval"
        assert any(r["code"] == "SUBSTITUTION_NEEDS_APPROVAL" for r in o_sub_ask["decision"]["reasons"])

        never_subs = base.model_copy(update={"substitutions": "never"})
        assert client.put("/v1/mandate", headers=admin_hdr, json=never_subs.model_dump()).status_code == 200

        q_sub_never = _issue_quote_direct(product_id="1002", quantity=50.0, substitute_for="1001")
        o_sub_never = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q_sub_never.quote_id, "jws": q_sub_never.jws},
        ).json()
        assert o_sub_never["decision"]["outcome"] == "blocked"
        assert o_sub_never["status"] == "blocked"
        assert any(r["code"] == "SUBSTITUTION_NOT_ALLOWED" for r in o_sub_never["decision"]["reasons"])

        cat_block = base.model_copy(update={"allowed_categories": ["90900"]})
        assert client.put("/v1/mandate", headers=admin_hdr, json=cat_block.model_dump()).status_code == 200

        q_cat = _issue_quote_direct(product_id="1001", quantity=20.0)
        o_cat = client.post(
            "/v1/orders",
            headers=agent_hdr,
            json={"quote_id": q_cat.quote_id, "jws": q_cat.jws},
        ).json()
        assert o_cat["decision"]["outcome"] == "blocked"
        assert o_cat["status"] == "blocked"
        assert any(r["code"] == "CATEGORY_NOT_ALLOWED" for r in o_cat["decision"]["reasons"])

        fresh = Mandate(
            customer="canteen-espoo",
            agent_id="buyer-espoo",
            per_order_eur=750.0,
            per_week_eur=3500.0,
            exclude_allergens=["nuts"],
            allowed_categories=["10400"],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Kari Laine"],
        )
        assert client.put("/v1/mandate", headers=admin_hdr, json=fresh.model_dump()).status_code == 200

    stdio_res = _stdio_subprocess_call(
        {
            "AIMO_AGENT_KEYS": json.dumps({"espoo-key": ["canteen-espoo", "buyer-espoo"]}),
            "AIMO_AGENT_KEY": "espoo-key",
        },
        "get_mandate",
        {},
    )
    assert not stdio_res.is_error
    stdio_doc = json.loads(stdio_res.content[0].text)
    assert stdio_doc["customer"] == "canteen-espoo"
    assert stdio_doc["agent_id"] == "buyer-espoo"
    assert stdio_doc["per_order_eur"] == 750.0
    assert stdio_doc["approvers"] == ["Kari Laine"]
    assert audit.verify() == (True, None)

