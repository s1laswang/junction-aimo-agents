import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet

from aimo import audit, catalog, offers
from aimo.app import app
from aimo.models import Offer, OfferGroup, OffersResponse, Product, QuoteLine, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS = {"X-Agent-Key": "demo-hospital-tays"}
ADMIN = {"X-Admin-Key": "demo-admin"}

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

P_MILK = Product(
    product_id="400100",
    name="Valio Laktoositon Kevytmaito 1 L",
    brand="Valio",
    vendor="Valio Oy",
    category="Dairy",
    sales_unit="LTR",
    unit_size=UnitSize(value=1.0, unit="l"),
    allergens=["milk"],
    labels=["free_from_lactose", "finnish"],
    origin="FI",
    temperature=6,
)

P_ORGANIC_MILK = Product(
    product_id="400200",
    name="Valio Luomu Laktoositon Kevytmaito 1 L",
    brand="Valio",
    vendor="Valio Oy",
    category="Dairy",
    sales_unit="LTR",
    unit_size=UnitSize(value=1.0, unit="l"),
    allergens=["milk"],
    labels=["organic", "free_from_lactose", "finnish"],
    origin="FI",
    temperature=6,
)

PRODUCTS = {p.product_id: p for p in (P_MILK, P_ORGANIC_MILK)}
UNIT_PRICE_NET = 1.50


class McpSession:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid = None
        self.seq = 0
        self.called = set()
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-e2e", "version": "1.0"},
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
        self.called.add(name)
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        assert "result" in out, out
        res = out["result"]
        assert not res.get("isError"), res
        blocks = [json.loads(b["text"]) for b in res.get("content", [])]
        if "structuredContent" in res and res["structuredContent"] is not None:
            sc = res["structuredContent"]
            if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], list):
                assert blocks == sc["result"]
                return sc["result"]
            return sc
        assert len(blocks) == 1, res
        return blocks[0]

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        self.called.add(name)
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        assert "result" in out, out
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        payload = text.split(prefix, 1)[-1]
        return json.loads(payload)


def _fake_search(q="", constraints=None, limit=50):
    out = list(PRODUCTS.values())
    if q:
        ql = q.lower()
        out = [p for p in out if ql in p.name.lower() or ("milk" in ql and "maito" in p.name.lower())]
    if constraints:
        if constraints.lactose_free:
            out = [p for p in out if "free_from_lactose" in p.labels]
        if constraints.labels:
            req_labels = {x.lower() for x in constraints.labels}
            out = [p for p in out if req_labels <= {x.lower() for x in p.labels}]
        if constraints.brands:
            req_brands = {x.lower() for x in constraints.brands}
            out = [p for p in out if (p.brand or "").lower() in req_brands]
    return out[:limit]


def _fake_get(product_id):
    return PRODUCTS.get(str(product_id))


def _fake_get_offers(need, mandate=None, now=None):
    groups = []
    for item in need.items:
        net = round(item.quantity * UNIT_PRICE_NET, 2)
        gross = round(net * 1.14, 2)
        offer = Offer(
            product_id=P_MILK.product_id,
            product=P_MILK,
            units_needed=item.quantity,
            unit_price_net=UNIT_PRICE_NET,
            total_net=net,
            total_gross=gross,
            currency="EUR",
            available_amount=5000.0,
            available_date=need.deliver_by,
            reliability=0.99,
            co2_per_kg=1.05,
            match_score=1.0,
            tier="exact",
        )
        groups.append(OfferGroup(need_item=item, offers=[offer], shortfall=0.0))
    return OffersResponse(items=groups)


def _fake_price_lines(customer, delivery_date, lines):
    out = []
    for line in lines:
        prod = PRODUCTS.get(line.product_id)
        if prod is None:
            raise ValueError(f"unknown product {line.product_id}")
        line_net = round(line.quantity * UNIT_PRICE_NET, 2)
        out.append(
            QuoteLine(
                product_id=line.product_id,
                name=prod.name,
                quantity=line.quantity,
                sales_unit=prod.sales_unit,
                unit_price_net=UNIT_PRICE_NET,
                line_total_net=line_net,
                substitute_for=line.substitute_for,
            )
        )
    total_gross = round(sum(l.line_total_net for l in out) * 1.14, 2)
    valid_until = datetime.now(timezone.utc) + timedelta(hours=4)
    return out, total_gross, valid_until


def _tamper_jws(token: str) -> str:
    tail = "AAAA" if not token.endswith("AAAA") else "BBBB"
    return token[:-4] + tail


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo_e2e.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "isolated_no_data"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(catalog, "search", _fake_search)
    monkeypatch.setattr(catalog, "get", _fake_get)
    monkeypatch.setattr(offers, "get_offers", _fake_get_offers)
    monkeypatch.setattr(offers, "price_lines", _fake_price_lines)
    with TestClient(app) as c:
        yield c, McpSession(c)


def test_e2e_all_twelve_mcp_tools(mcp_env):
    client, mcp = mcp_env
    delivery = (date.today() + timedelta(days=3)).isoformat()

    listed = mcp.rpc("tools/list")
    assert {t["name"] for t in listed["result"]["tools"]} == EXPECTED_TOOLS

    found = mcp.call_ok("search_products", {"q": "milk", "lactose_free": True})
    assert [p["product_id"] for p in found] == ["400100", "400200"]
    organic_only = mcp.call_ok("search_products", {"q": "milk", "labels": ["organic"]})
    assert [p["product_id"] for p in organic_only] == ["400200"]

    prod = mcp.call_ok("get_product", {"product_id": "400100"})
    assert prod["product_id"] == "400100"
    assert prod["brand"] == "Valio"
    assert prod["unit_size"] == {"value": 1.0, "unit": "l"}

    missing = mcp.call_err("get_product", {"product_id": "999999"})
    assert missing["code"] == "not_found"

    mandate = mcp.call_ok("get_mandate", {}, headers=AURORA)
    assert mandate["customer"] == "hotel-aurora"
    assert mandate["agent_id"] == "buyer-aurora"
    assert mandate["per_order_eur"] == 2500.0
    assert "Laura Virtanen" in mandate["approvers"]

    parsed = mcp.call_ok(
        "parse_request",
        {"text": f"I need 500 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
        headers=AURORA,
    )
    assert parsed["customer"] == "hotel-aurora"
    assert parsed["deliver_by"] == delivery
    assert len(parsed["items"]) == 1
    assert parsed["items"][0]["query"] == "milk"
    assert parsed["items"][0]["quantity"] == 500.0
    assert parsed["items"][0]["constraints"]["lactose_free"] is True
    assert "500 L milk" in parsed["summary"]

    compared = mcp.call_ok(
        "get_offers",
        {
            "customer": parsed["customer"],
            "deliver_by": parsed["deliver_by"],
            "items": parsed["items"],
            "raw_text": parsed["raw_text"],
        },
        headers=AURORA,
    )
    assert compared["items"][0]["shortfall"] == 0.0
    pick = compared["items"][0]["offers"][0]
    assert pick["product_id"] == "400100"
    assert pick["total_net"] == 750.0

    under_quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": pick["product_id"], "quantity": 500}], "delivery_date": delivery},
        headers=AURORA,
    )
    assert under_quote["quote_id"].startswith("q_")
    assert under_quote["customer"] == "hotel-aurora"
    assert under_quote["total_net"] == 750.0
    assert under_quote["jws"].count(".") == 2

    over_quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": pick["product_id"], "quantity": 2000}], "delivery_date": delivery},
        headers=AURORA,
    )
    assert over_quote["quote_id"].startswith("q_")
    assert over_quote["total_net"] == 3000.0
    assert over_quote["jws"].count(".") == 2

    auto_order = mcp.call_ok(
        "place_order",
        {"quote_id": under_quote["quote_id"], "jws": under_quote["jws"]},
        headers=AURORA,
    )
    assert auto_order["status"] == "confirmed"
    assert auto_order["decision"]["outcome"] == "auto"
    assert any(r["code"] == "WITHIN_MANDATE" for r in auto_order["decision"]["reasons"])

    replayed_err = mcp.call_err(
        "place_order",
        {"quote_id": under_quote["quote_id"], "jws": under_quote["jws"]},
        headers=AURORA,
    )
    assert replayed_err["code"] == "QUOTE_USED"

    tampered_err = mcp.call_err(
        "place_order",
        {"quote_id": over_quote["quote_id"], "jws": _tamper_jws(over_quote["jws"])},
        headers=AURORA,
    )
    assert tampered_err["code"] == "BAD_SIGNATURE"

    over_order = mcp.call_ok(
        "place_order",
        {"quote_id": over_quote["quote_id"], "jws": over_quote["jws"]},
        headers=AURORA,
    )
    assert over_order["status"] == "pending_approval"
    assert over_order["decision"]["outcome"] == "needs_approval"
    assert any(r["code"] == "OVER_ORDER_LIMIT" for r in over_order["decision"]["reasons"])

    ask_quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": pick["product_id"], "quantity": 100}], "delivery_date": delivery},
        headers=AURORA,
    )
    ask_order = mcp.call_ok(
        "place_order",
        {
            "quote_id": ask_quote["quote_id"],
            "jws": ask_quote["jws"],
            "ask_human": True,
            "note": "Check cold storage capacity",
        },
        headers=AURORA,
    )
    assert ask_order["status"] == "pending_approval"
    assert ask_order["decision"]["outcome"] == "needs_approval"
    assert ask_order["decision"]["reasons"][0]["code"] == "AGENT_REQUESTED"

    pending = mcp.call_ok("list_approvals", {}, headers=AURORA)
    assert {o["order_id"] for o in pending} == {over_order["order_id"], ask_order["order_id"]}
    assert all(o["status"] == "pending_approval" for o in pending)

    decided = mcp.call_ok(
        "decide_approval",
        {
            "order_id": over_order["order_id"],
            "approve": True,
            "by": "Laura Virtanen",
            "note": "Approved for weekend banquet",
        },
        headers=ADMIN,
    )
    assert decided["order_id"] == over_order["order_id"]
    assert decided["status"] == "confirmed"

    pending_after = mcp.call_ok("list_approvals", {}, headers=AURORA)
    assert [o["order_id"] for o in pending_after] == [ask_order["order_id"]]

    fetched = mcp.call_ok("get_order", {"order_id": over_order["order_id"]}, headers=AURORA)
    assert fetched["order_id"] == over_order["order_id"]
    assert fetched["customer"] == "hotel-aurora"
    assert fetched["status"] == "confirmed"
    assert fetched["total_net"] == 3000.0

    all_orders = mcp.call_ok("list_orders", {}, headers=AURORA)
    assert [o["order_id"] for o in all_orders] == [
        auto_order["order_id"],
        over_order["order_id"],
        ask_order["order_id"],
    ]
    confirmed_orders = mcp.call_ok("list_orders", {"status": "confirmed"}, headers=AURORA)
    assert [o["order_id"] for o in confirmed_orders] == [
        auto_order["order_id"],
        over_order["order_id"],
    ]

    trail = mcp.call_ok("get_audit_trail", {}, headers=AURORA)
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    assert trail["events"]
    assert trail["text"]
    jwks = client.get("/.well-known/jwks.json").json()
    cp = trail["checkpoint"]
    verified_head = audit.verify_checkpoint(cp["jws"], jwks)
    assert verified_head["seq"] == cp["seq"]
    assert verified_head["hash"] == cp["hash"]
    decoded = jws.deserialize_compact(cp["jws"], KeySet.import_key_set(jwks), algorithms=["EdDSA"])
    assert json.loads(decoded.payload)["hash"] == cp["hash"]

    assert mcp.called == EXPECTED_TOOLS


def test_cross_customer_isolation_over_mcp(mcp_env):
    _, mcp = mcp_env
    delivery = (date.today() + timedelta(days=3)).isoformat()

    quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400100", "quantity": 100}], "delivery_date": delivery},
        headers=AURORA,
    )
    order = mcp.call_ok(
        "place_order",
        {"quote_id": quote["quote_id"], "jws": quote["jws"]},
        headers=AURORA,
    )
    assert order["customer"] == "hotel-aurora"
    assert order["status"] == "confirmed"

    err = mcp.call_err("get_order", {"order_id": order["order_id"]}, headers=TAYS)
    assert err["code"] == "forbidden"

    err_list = mcp.call_err("list_orders", {"customer": "hotel-aurora"}, headers=TAYS)
    assert err_list["code"] == "forbidden"

    err_mandate = mcp.call_err("get_mandate", {"customer": "hotel-aurora"}, headers=TAYS)
    assert err_mandate["code"] == "forbidden"

    tays_orders = mcp.call_ok("list_orders", {}, headers=TAYS)
    assert tays_orders == []


def test_catalog_and_mandate_tools(mcp_env):
    _, mcp = mcp_env
    items = mcp.call_ok("search_products", {"q": "milk", "brands": ["Valio"], "limit": 1})
    assert len(items) == 1 and items[0]["product_id"] == "400100"

    prod = mcp.call_ok("get_product", {"product_id": "400200"})
    assert prod["product_id"] == "400200" and "organic" in prod["labels"]

    not_found = mcp.call_err("get_product", {"product_id": "does-not-exist"})
    assert not_found["code"] == "not_found"

    mandate = mcp.call_ok("get_mandate", {}, headers=TAYS)
    assert mandate["customer"] == "hospital-tays"
    assert "peanut" in mandate["exclude_allergens"]


def test_place_order_guardrails_and_approval_flow(mcp_env):
    client, mcp = mcp_env
    delivery = (date.today() + timedelta(days=4)).isoformat()

    q_auto = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400100", "quantity": 200}], "delivery_date": delivery},
        headers=AURORA,
    )
    o_auto = mcp.call_ok("place_order", {"quote_id": q_auto["quote_id"], "jws": q_auto["jws"]}, headers=AURORA)
    assert o_auto["decision"]["outcome"] == "auto" and o_auto["status"] == "confirmed"

    replay = mcp.call_err("place_order", {"quote_id": q_auto["quote_id"], "jws": q_auto["jws"]}, headers=AURORA)
    assert replay["code"] == "QUOTE_USED"

    q_over = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400100", "quantity": 2000}], "delivery_date": delivery},
        headers=AURORA,
    )
    bad_sig = mcp.call_err(
        "place_order",
        {"quote_id": q_over["quote_id"], "jws": _tamper_jws(q_over["jws"])},
        headers=AURORA,
    )
    assert bad_sig["code"] == "BAD_SIGNATURE"

    o_over = mcp.call_ok("place_order", {"quote_id": q_over["quote_id"], "jws": q_over["jws"]}, headers=AURORA)
    assert o_over["status"] == "pending_approval"
    assert any(r["code"] == "OVER_ORDER_LIMIT" for r in o_over["decision"]["reasons"])

    q_ask = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400100", "quantity": 50}], "delivery_date": delivery},
        headers=AURORA,
    )
    o_ask = mcp.call_ok(
        "place_order",
        {"quote_id": q_ask["quote_id"], "jws": q_ask["jws"], "ask_human": True, "note": "Manual review"},
        headers=AURORA,
    )
    assert o_ask["status"] == "pending_approval"
    assert o_ask["decision"]["reasons"][0]["code"] == "AGENT_REQUESTED"

    approvals_list = mcp.call_ok("list_approvals", {}, headers=AURORA)
    assert {x["order_id"] for x in approvals_list} == {o_over["order_id"], o_ask["order_id"]}

    approved = mcp.call_ok(
        "decide_approval",
        {"order_id": o_over["order_id"], "approve": True, "by": "Laura Virtanen", "note": "OK"},
        headers=ADMIN,
    )
    assert approved["status"] == "confirmed"

    order_detail = mcp.call_ok("get_order", {"order_id": o_over["order_id"]}, headers=AURORA)
    assert order_detail["status"] == "confirmed"

    order_audit = mcp.call_ok("get_audit_trail", {"order_id": o_over["order_id"]}, headers=AURORA)
    assert order_audit["verified"]["ok"] is True
    assert order_audit["verified"]["bad_seq"] is None
    jwks = client.get("/.well-known/jwks.json").json()
    cp = order_audit["checkpoint"]
    head = audit.verify_checkpoint(cp["jws"], jwks)
    assert head["seq"] == cp["seq"] and head["hash"] == cp["hash"]

