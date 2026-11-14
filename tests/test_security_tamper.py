import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OKPKey

from aimo import audit, catalog, keys, offers, valio
from aimo.app import app
from aimo.models import Product, QuoteLine, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS = {"X-Agent-Key": "demo-hospital-tays"}
HERVANTA = {"X-Agent-Key": "demo-school-hervanta"}
ADMIN = {"X-Admin-Key": "demo-admin"}

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

P_OAT = Product(
    product_id="400200",
    name="Valio Oddlygood Kaurajuoma 1 L",
    brand="Valio",
    vendor="Valio Oy",
    category="Dairy",
    sales_unit="LTR",
    unit_size=UnitSize(value=1.0, unit="l"),
    allergens=["oats"],
    labels=["free_from_lactose", "finnish"],
    origin="FI",
    temperature=6,
)

PRODUCTS = {p.product_id: p for p in (P_MILK, P_OAT)}
UNIT_NET = 1.50
UNIT_GROSS = 1.71


def _b64_json(obj: dict) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_jws_parts(token: str) -> tuple[dict, dict, str, str, str]:
    h_b64, p_b64, s_b64 = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(h_b64 + "=="))
    payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=="))
    return header, payload, h_b64, p_b64, s_b64


def _flip_sig_byte(s_b64: str, index: int = 0) -> str:
    raw = bytearray(base64.urlsafe_b64decode(s_b64 + "=="))
    raw[index] ^= 0xFF
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()


class McpSession:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid = None
        self.seq = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "security-tamper", "version": "1.0"},
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
        assert "result" in out, out
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
    return out[:limit]


def _fake_price(customer, day, ids):
    return {
        str(pid): {
            "productId": str(pid),
            "currency": "EUR",
            "catalogPrice": {"value": UNIT_NET},
            "priceWithTax": {"value": UNIT_GROSS},
        }
        for pid in ids
        if str(pid) in PRODUCTS
    }


def _fake_availability(customer, day, ids):
    d_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return {
        str(pid): {
            "productId": str(pid),
            "salesBlock": False,
            "scheduleLines": [
                {
                    "salesBlock": False,
                    "availableAmount": 5000.0,
                    "availableDate": d_str,
                    "salesUnit": PRODUCTS[str(pid)].sales_unit,
                }
            ],
        }
        for pid in ids
        if str(pid) in PRODUCTS
    }


@pytest.fixture
def sec_env(tmp_path, monkeypatch):
    db_path = tmp_path / "security.db"
    monkeypatch.setenv("AIMO_DB", str(db_path))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    for var in (
        "AIMO_AGENT_KEYS",
        "AIMO_ADMIN_KEY",
        "AIMO_AGENT_KEY",
        "AIMO_STDIO_ADMIN_KEY",
        "VALIO_API_BASE",
        "VALIO_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    keys._cache.clear()
    offers._overrides.clear()

    quote_cfg = {"valid_delta": timedelta(hours=4)}

    def fake_price_lines(customer, delivery_date, lines):
        out = []
        for line in lines:
            prod = PRODUCTS.get(str(line.product_id))
            if prod is None:
                raise quotes_err_unknown(line.product_id)
            line_net = round(line.quantity * UNIT_NET, 2)
            out.append(
                QuoteLine(
                    product_id=line.product_id,
                    name=prod.name,
                    quantity=line.quantity,
                    sales_unit=prod.sales_unit,
                    unit_price_net=UNIT_NET,
                    line_total_net=line_net,
                    substitute_for=line.substitute_for,
                )
            )
        total_gross = round(sum(l.line_total_net for l in out) * 1.14, 2)
        valid_until = datetime.now(timezone.utc) + quote_cfg["valid_delta"]
        return out, total_gross, valid_until

    def quotes_err_unknown(pid):
        from aimo.quotes import QuoteError
        return QuoteError("INVALID_QUOTE", f"No price available for product {pid}.")

    monkeypatch.setattr(catalog, "search", _fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.05)
    monkeypatch.setattr(valio, "price", _fake_price)
    monkeypatch.setattr(valio, "availability", _fake_availability)
    monkeypatch.setattr(offers, "price_lines", fake_price_lines)

    with TestClient(app) as client:
        yield client, McpSession(client), db_path, quote_cfg

    keys._cache.clear()
    offers._overrides.clear()


def _delivery_iso(days: int = 3) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _sqlite_order_count(db_path) -> int:
    if not db_path.exists():
        return 0
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        if not row:
            return 0
        return con.execute("SELECT count(*) FROM orders").fetchone()[0]
    finally:
        con.close()


def _sqlite_quote_used_at(db_path, quote_id: str):
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT used_at FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _issue_quote(transport: str, client: TestClient, mcp: McpSession, headers: dict, quantity: float = 100.0) -> dict:
    body = {"offers": [{"product_id": "400100", "quantity": quantity}], "delivery_date": _delivery_iso()}
    if transport == "rest":
        r = client.post("/v1/quotes", headers=headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()
    return mcp.call_ok("request_quote", body, headers=headers)


def _place_ok(transport: str, client: TestClient, mcp: McpSession, headers: dict, quote_id: str, token: str, ask_human: bool = False) -> dict:
    body = {"quote_id": quote_id, "jws": token, "ask_human": ask_human}
    if transport == "rest":
        r = client.post("/v1/orders", headers=headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()
    return mcp.call_ok("place_order", body, headers=headers)


def _assert_rejected_409(
    transport: str,
    client: TestClient,
    mcp: McpSession,
    headers: dict,
    quote_id: str,
    token: str,
    expected_code: str,
    expected_customer: str,
    expected_agent: str,
    db_path,
) -> dict:
    orders_before = _sqlite_order_count(db_path)
    audit_before = client.get("/v1/audit", headers=ADMIN).json()["events"]

    if transport == "rest":
        r = client.post("/v1/orders", headers=headers, json={"quote_id": quote_id, "jws": token})
        assert r.status_code == 409, r.text
        err = r.json()
    else:
        err = mcp.call_err("place_order", {"quote_id": quote_id, "jws": token}, headers=headers)

    assert set(err.keys()) == {"code", "text"}
    assert err["code"] == expected_code
    assert isinstance(err["text"], str) and len(err["text"]) > 0

    assert _sqlite_order_count(db_path) == orders_before
    assert len(client.get("/v1/orders", headers=ADMIN).json()) == orders_before

    audit_resp = client.get("/v1/audit", headers=ADMIN).json()
    assert audit_resp["verified"] == {"ok": True, "bad_seq": None}
    audit_after = audit_resp["events"]
    assert len(audit_after) == len(audit_before) + 1
    ev = audit_after[-1]
    assert ev["action"] == "order.rejected"
    assert ev["customer"] == expected_customer
    assert ev["actor"] == f"agent:{expected_agent}"
    assert ev["subject"] == quote_id
    assert ev["why"] == err["text"]
    assert ev["detail"] == {"quote_id": quote_id, "code": expected_code}
    return err


def test_missing_and_invalid_keys_rest(sec_env):
    client, _, _, _ = sec_env
    delivery = _delivery_iso()
    need_body = {
        "customer": "hotel-aurora",
        "deliver_by": delivery,
        "items": [{"query": "milk", "quantity": 10, "unit": "l"}],
    }
    quote_body = {"offers": [{"product_id": "400100", "quantity": 10}], "delivery_date": delivery}
    order_body = {"quote_id": "q_none", "jws": "a.b.c"}

    agent_calls = [
        ("POST", "/v1/offers", need_body),
        ("POST", "/v1/quotes", quote_body),
        ("POST", "/v1/orders", order_body),
        ("GET", "/v1/orders", None),
        ("GET", "/v1/orders/ord_missing", None),
        ("GET", "/v1/approvals", None),
        ("GET", "/v1/audit", None),
        ("GET", "/v1/mandate", None),
    ]

    for bad_headers in ({}, {"X-Agent-Key": ""}, {"X-Agent-Key": "not-a-valid-agent-key"}):
        for method, path, payload in agent_calls:
            r = client.request(method, path, headers=bad_headers, json=payload)
            assert r.status_code == 401, (method, path, bad_headers, r.text)
            body = r.json()
            assert body["code"] == "unauthorized"
            assert body["text"] == "Missing or invalid X-Agent-Key."

    for method, path, payload in agent_calls[:3]:
        r = client.request(method, path, headers=ADMIN, json=payload)
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    mandate_body = {
        "customer": "hotel-aurora",
        "agent_id": "buyer-aurora",
        "per_order_eur": 2500.0,
        "per_week_eur": 15000.0,
        "exclude_allergens": [],
        "substitutions": "within_prefs",
        "preferred_brands": ["Valio"],
        "approvers": ["Laura Virtanen"],
    }
    admin_calls = [
        ("PUT", "/v1/mandate", mandate_body),
        ("POST", "/v1/approvals/ord_x", {"approve": True, "by": "Laura Virtanen"}),
        ("POST", "/v1/admin/simulate", {"product_id": "400100", "available_amount": 0.0}),
        ("DELETE", "/v1/admin/simulate", None),
    ]
    for bad_admin in ({}, {"X-Admin-Key": ""}, {"X-Admin-Key": "wrong-admin"}):
        for method, path, payload in admin_calls:
            r = client.request(method, path, headers=bad_admin, json=payload)
            assert r.status_code == 401, (method, path, bad_admin, r.text)
            assert r.json()["code"] == "unauthorized"


def test_missing_and_invalid_keys_mcp(sec_env):
    _, mcp, _, _ = sec_env
    delivery = _delivery_iso()

    protected_tools = [
        ("get_offers", {"customer": "hotel-aurora", "deliver_by": delivery, "items": [{"query": "milk", "quantity": 10, "unit": "l"}]}),
        ("request_quote", {"offers": [{"product_id": "400100", "quantity": 10}], "delivery_date": delivery}),
        ("place_order", {"quote_id": "q_none", "jws": "a.b.c"}),
        ("get_order", {"order_id": "ord_missing"}),
        ("list_orders", {}),
        ("list_approvals", {}),
        ("get_audit_trail", {}),
        ("get_mandate", {}),
        ("decide_approval", {"order_id": "ord_missing", "approve": True, "by": "Laura Virtanen"}),
    ]

    for bad_headers in (None, {"X-Agent-Key": ""}, {"X-Agent-Key": "invalid-mcp-key"}):
        for tool_name, args in protected_tools:
            err = mcp.call_err(tool_name, args, headers=bad_headers)
            assert err["code"] == "unauthorized", (tool_name, bad_headers, err)
            assert isinstance(err["text"], str) and err["text"]


def test_cross_customer_isolation_and_admin_visibility_rest_and_mcp(sec_env):
    client, mcp, _, _ = sec_env
    delivery = _delivery_iso()

    tays_q = _issue_quote("rest", client, mcp, TAYS, quantity=200.0)
    tays_order = _place_ok("rest", client, mcp, TAYS, tays_q["quote_id"], tays_q["jws"], ask_human=True)
    hospital_order_id = tays_order["order_id"]
    assert tays_order["customer"] == "hospital-tays"
    assert tays_order["status"] == "pending_approval"

    aurora_q = _issue_quote("mcp", client, mcp, AURORA, quantity=50.0)
    aurora_order = _place_ok("mcp", client, mcp, AURORA, aurora_q["quote_id"], aurora_q["jws"])
    aurora_order_id = aurora_order["order_id"]
    assert aurora_order["customer"] == "hotel-aurora"
    assert aurora_order["status"] == "confirmed"

    forbidden_rest_gets = [
        "/v1/mandate?customer=hospital-tays",
        "/v1/orders?customer=hospital-tays",
        "/v1/approvals?customer=hospital-tays",
        "/v1/audit?customer=hospital-tays",
        f"/v1/orders/{hospital_order_id}",
    ]
    for route in forbidden_rest_gets:
        r = client.get(route, headers=AURORA)
        assert r.status_code == 403, (route, r.text)
        body = r.json()
        assert body["code"] == "forbidden"
        assert "hotel-aurora" in body["text"]

    r_cross_offers = client.post(
        "/v1/offers",
        headers=AURORA,
        json={"customer": "hospital-tays", "deliver_by": delivery, "items": [{"query": "milk", "quantity": 10, "unit": "l"}]},
    )
    assert r_cross_offers.status_code == 403
    assert r_cross_offers.json()["code"] == "forbidden"

    forbidden_mcp_calls = [
        ("get_mandate", {"customer": "hospital-tays"}),
        ("list_orders", {"customer": "hospital-tays"}),
        ("list_approvals", {"customer": "hospital-tays"}),
        ("get_audit_trail", {"customer": "hospital-tays"}),
        ("get_order", {"order_id": hospital_order_id}),
        ("get_offers", {"customer": "hospital-tays", "deliver_by": delivery, "items": [{"query": "milk", "quantity": 10, "unit": "l"}]}),
    ]
    for tool_name, args in forbidden_mcp_calls:
        err = mcp.call_err(tool_name, args, headers=AURORA)
        assert err["code"] == "forbidden", (tool_name, err)
        assert "hotel-aurora" in err["text"]

    assert [o["order_id"] for o in client.get("/v1/orders", headers=AURORA).json()] == [aurora_order_id]
    assert client.get("/v1/approvals", headers=AURORA).json() == []
    assert all(e["customer"] == "hotel-aurora" for e in client.get("/v1/audit", headers=AURORA).json()["events"])

    assert [o["order_id"] for o in mcp.call_ok("list_orders", {}, headers=AURORA)] == [aurora_order_id]
    assert mcp.call_ok("list_approvals", {}, headers=AURORA) == []
    assert all(e["customer"] == "hotel-aurora" for e in mcp.call_ok("get_audit_trail", {}, headers=AURORA)["events"])

    assert client.get("/v1/mandate?customer=hospital-tays", headers=ADMIN).json()["customer"] == "hospital-tays"
    assert client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN).json()["customer"] == "hotel-aurora"
    assert client.get("/v1/mandate?customer=school-hervanta", headers=ADMIN).json()["customer"] == "school-hervanta"

    admin_orders = client.get("/v1/orders", headers=ADMIN).json()
    assert {o["order_id"] for o in admin_orders} == {hospital_order_id, aurora_order_id}
    assert [o["order_id"] for o in client.get("/v1/orders?customer=hospital-tays", headers=ADMIN).json()] == [hospital_order_id]
    assert client.get(f"/v1/orders/{hospital_order_id}", headers=ADMIN).json()["order_id"] == hospital_order_id
    assert [o["order_id"] for o in client.get("/v1/approvals?customer=hospital-tays", headers=ADMIN).json()] == [hospital_order_id]
    assert [o["order_id"] for o in client.get("/v1/approvals", headers=ADMIN).json()] == [hospital_order_id]

    admin_audit_all = client.get("/v1/audit", headers=ADMIN).json()["events"]
    assert {e["customer"] for e in admin_audit_all} == {"hospital-tays", "hotel-aurora"}
    admin_audit_tays = client.get("/v1/audit?customer=hospital-tays", headers=ADMIN).json()["events"]
    assert admin_audit_tays and all(e["customer"] == "hospital-tays" for e in admin_audit_tays)

    assert mcp.call_ok("get_mandate", {"customer": "hospital-tays"}, headers=ADMIN)["customer"] == "hospital-tays"
    assert {o["order_id"] for o in mcp.call_ok("list_orders", {}, headers=ADMIN)} == {hospital_order_id, aurora_order_id}
    assert [o["order_id"] for o in mcp.call_ok("list_orders", {"customer": "hospital-tays"}, headers=ADMIN)] == [hospital_order_id]
    assert mcp.call_ok("get_order", {"order_id": hospital_order_id}, headers=ADMIN)["order_id"] == hospital_order_id
    assert [o["order_id"] for o in mcp.call_ok("list_approvals", {"customer": "hospital-tays"}, headers=ADMIN)] == [hospital_order_id]
    mcp_admin_audit = mcp.call_ok("get_audit_trail", {"customer": "hospital-tays"}, headers=ADMIN)["events"]
    assert mcp_admin_audit and all(e["customer"] == "hospital-tays" for e in mcp_admin_audit)


def test_agent_keys_forbidden_on_admin_actions_rest_and_mcp(sec_env):
    client, mcp, _, _ = sec_env

    tays_q = _issue_quote("rest", client, mcp, TAYS, quantity=100.0)
    tays_order = _place_ok("rest", client, mcp, TAYS, tays_q["quote_id"], tays_q["jws"], ask_human=True)
    order_id = tays_order["order_id"]
    assert tays_order["status"] == "pending_approval"

    mandate_before = client.get("/v1/mandate", headers=AURORA).json()
    forged_mandate = {**mandate_before, "per_order_eur": 999999.0, "per_week_eur": 999999.0}

    for agent_hdr in (AURORA, TAYS, HERVANTA):
        r_put = client.put("/v1/mandate", headers=agent_hdr, json=forged_mandate)
        assert r_put.status_code == 403
        assert r_put.json() == {"code": "forbidden", "text": "This route needs an admin key."}

        r_decide = client.post(
            f"/v1/approvals/{order_id}",
            headers=agent_hdr,
            json={"approve": True, "by": "Helena Korhonen", "note": "self-approve"},
        )
        assert r_decide.status_code == 403
        assert r_decide.json() == {"code": "forbidden", "text": "This route needs an admin key."}

        r_sim = client.post(
            "/v1/admin/simulate",
            headers=agent_hdr,
            json={"product_id": "400100", "available_amount": 0.0},
        )
        assert r_sim.status_code == 403
        assert r_sim.json() == {"code": "forbidden", "text": "This route needs an admin key."}

        r_clear = client.delete("/v1/admin/simulate", headers=agent_hdr)
        assert r_clear.status_code == 403
        assert r_clear.json() == {"code": "forbidden", "text": "This route needs an admin key."}

        mcp_err = mcp.call_err(
            "decide_approval",
            {"order_id": order_id, "approve": True, "by": "Helena Korhonen", "note": "mcp-self-approve"},
            headers=agent_hdr,
        )
        assert mcp_err == {"code": "forbidden", "text": "This route needs an admin key."}

    assert client.get("/v1/mandate", headers=AURORA).json()["per_order_eur"] == 2500.0
    assert offers._overrides == {}
    assert client.get(f"/v1/orders/{order_id}", headers=TAYS).json()["status"] == "pending_approval"

    decided = mcp.call_ok(
        "decide_approval",
        {"order_id": order_id, "approve": True, "by": "Helena Korhonen", "note": "Authorized human"},
        headers=ADMIN,
    )
    assert decided["status"] == "confirmed"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_cross_customer_quote_redemption_wrong_agent_keeps_quote_redeemable(sec_env, transport):
    client, mcp, db_path, _ = sec_env

    q = _issue_quote(transport, client, mcp, AURORA, quantity=120.0)
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is None

    _assert_rejected_409(
        transport,
        client,
        mcp,
        HERVANTA,
        q["quote_id"],
        q["jws"],
        "WRONG_AGENT",
        "school-hervanta",
        "buyer-hervanta",
        db_path,
    )
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is None

    placed = _place_ok(transport, client, mcp, AURORA, q["quote_id"], q["jws"])
    assert placed["customer"] == "hotel-aurora"
    assert placed["quote_id"] == q["quote_id"]
    assert placed["status"] == "confirmed"
    assert _sqlite_order_count(db_path) == 1
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is not None


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_quote_replay_rejected_with_quote_used(sec_env, transport):
    client, mcp, db_path, _ = sec_env

    q = _issue_quote(transport, client, mcp, AURORA, quantity=80.0)
    first = _place_ok(transport, client, mcp, AURORA, q["quote_id"], q["jws"])
    assert first["status"] == "confirmed"
    assert _sqlite_order_count(db_path) == 1

    _assert_rejected_409(
        transport,
        client,
        mcp,
        AURORA,
        q["quote_id"],
        q["jws"],
        "QUOTE_USED",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )

    other_transport = "mcp" if transport == "rest" else "rest"
    _assert_rejected_409(
        other_transport,
        client,
        mcp,
        AURORA,
        q["quote_id"],
        q["jws"],
        "QUOTE_USED",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )
    assert _sqlite_order_count(db_path) == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_expired_quote_rejected_with_quote_expired(sec_env, transport):
    client, mcp, db_path, quote_cfg = sec_env

    quote_cfg["valid_delta"] = timedelta(minutes=-15)
    q = _issue_quote(transport, client, mcp, AURORA, quantity=60.0)

    _assert_rejected_409(
        transport,
        client,
        mcp,
        AURORA,
        q["quote_id"],
        q["jws"],
        "QUOTE_EXPIRED",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )
    assert _sqlite_order_count(db_path) == 0
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is None


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_unknown_quote_and_mismatched_quote_jws(sec_env, transport):
    client, mcp, db_path, _ = sec_env

    q1 = _issue_quote(transport, client, mcp, AURORA, quantity=40.0)
    q2 = _issue_quote(transport, client, mcp, AURORA, quantity=90.0)

    _assert_rejected_409(
        transport,
        client,
        mcp,
        AURORA,
        "q_unknown000000",
        q1["jws"],
        "UNKNOWN_QUOTE",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )

    _assert_rejected_409(
        transport,
        client,
        mcp,
        AURORA,
        q1["quote_id"],
        q2["jws"],
        "QUOTE_MISMATCH",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )

    assert _sqlite_order_count(db_path) == 0
    assert _sqlite_quote_used_at(db_path, q1["quote_id"]) is None
    assert _sqlite_quote_used_at(db_path, q2["quote_id"]) is None

    placed = _place_ok(transport, client, mcp, AURORA, q1["quote_id"], q1["jws"])
    assert placed["status"] == "confirmed"
    assert _sqlite_order_count(db_path) == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_jws_header_tampering_alg_none_hs256_and_wrong_kid(sec_env, transport):
    client, mcp, db_path, _ = sec_env

    q = _issue_quote(transport, client, mcp, AURORA, quantity=100.0)
    header, payload, _, p_b64, s_b64 = _decode_jws_parts(q["jws"])
    pub_jwk = client.get("/.well-known/jwks.json").json()["keys"][0]
    pub_x_bytes = base64.urlsafe_b64decode(pub_jwk["x"] + "==")

    alg_none_empty = f"{_b64_json({'alg': 'none', 'kid': header['kid']})}.{p_b64}."
    alg_none_sig = f"{_b64_json({'alg': 'none'})}.{p_b64}.AAAA"

    hs_signing_input = f"{_b64_json({'alg': 'HS256', 'kid': header['kid']})}.{p_b64}"
    hs_sig_pubx = base64.urlsafe_b64encode(
        hmac.new(pub_x_bytes, hs_signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    alg_hs256_pubx = f"{hs_signing_input}.{hs_sig_pubx}"

    hs_sig_jwk = base64.urlsafe_b64encode(
        hmac.new(json.dumps(pub_jwk).encode(), hs_signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    alg_hs256_jwk = f"{hs_signing_input}.{hs_sig_jwk}"

    wrong_kid_same_sig = f"{_b64_json({'alg': 'EdDSA', 'kid': 'wrong-key-id'})}.{p_b64}.{s_b64}"

    rogue_key = OKPKey.generate_key("Ed25519")
    rogue_spoofed_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": header["kid"]},
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        rogue_key,
        algorithms=["EdDSA"],
    )
    rogue_own_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": rogue_key.thumbprint()},
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        rogue_key,
        algorithms=["EdDSA"],
    )

    for forged in (
        alg_none_empty,
        alg_none_sig,
        alg_hs256_pubx,
        alg_hs256_jwk,
        wrong_kid_same_sig,
        rogue_spoofed_kid,
        rogue_own_kid,
    ):
        _assert_rejected_409(
            transport,
            client,
            mcp,
            AURORA,
            q["quote_id"],
            forged,
            "BAD_SIGNATURE",
            "hotel-aurora",
            "buyer-aurora",
            db_path,
        )

    assert _sqlite_order_count(db_path) == 0
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is None

    placed = _place_ok(transport, client, mcp, AURORA, q["quote_id"], q["jws"])
    assert placed["status"] == "confirmed"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_jws_payload_and_signature_byte_flip_tampering(sec_env, transport):
    client, mcp, db_path, _ = sec_env

    q = _issue_quote(transport, client, mcp, AURORA, quantity=100.0)
    _, payload, h_b64, p_b64, s_b64 = _decode_jws_parts(q["jws"])

    p_gross = json.loads(json.dumps(payload))
    p_gross["total_gross"] = 0.01
    tok_gross = f"{h_b64}.{_b64_json(p_gross)}.{s_b64}"

    p_qty = json.loads(json.dumps(payload))
    p_qty["lines"][0]["quantity"] = 5000.0
    tok_qty = f"{h_b64}.{_b64_json(p_qty)}.{s_b64}"

    tok_flip_first = f"{h_b64}.{p_b64}.{_flip_sig_byte(s_b64, 0)}"
    tok_flip_mid = f"{h_b64}.{p_b64}.{_flip_sig_byte(s_b64, 31)}"
    tok_flip_last = f"{h_b64}.{p_b64}.{_flip_sig_byte(s_b64, 63)}"

    for forged in (tok_gross, tok_qty, tok_flip_first, tok_flip_mid, tok_flip_last):
        _assert_rejected_409(
            transport,
            client,
            mcp,
            AURORA,
            q["quote_id"],
            forged,
            "BAD_SIGNATURE",
            "hotel-aurora",
            "buyer-aurora",
            db_path,
        )

    assert _sqlite_order_count(db_path) == 0
    assert _sqlite_quote_used_at(db_path, q["quote_id"]) is None

    placed = _place_ok(transport, client, mcp, AURORA, q["quote_id"], q["jws"])
    assert placed["status"] == "confirmed"


def _seed_multi_event_chain(client: TestClient, mcp: McpSession) -> dict:
    delivery = _delivery_iso()
    parsed = client.post(
        "/v1/needs/parse",
        headers=AURORA,
        json={"text": f"I need 100 litres of lactose-free milk by {delivery}", "customer": "hotel-aurora"},
    )
    assert parsed.status_code == 200, parsed.text
    need = parsed.json()

    r_offers = client.post(
        "/v1/offers",
        headers=AURORA,
        json={"customer": need["customer"], "deliver_by": need["deliver_by"], "items": need["items"]},
    )
    assert r_offers.status_code == 200, r_offers.text

    q_aurora = _issue_quote("rest", client, mcp, AURORA, quantity=100.0)
    _place_ok("rest", client, mcp, AURORA, q_aurora["quote_id"], q_aurora["jws"])

    q_tays = _issue_quote("mcp", client, mcp, TAYS, quantity=50.0)
    _place_ok("mcp", client, mcp, TAYS, q_tays["quote_id"], q_tays["jws"], ask_human=True)

    trail = client.get("/v1/audit", headers=ADMIN).json()
    assert len(trail["events"]) >= 8
    return trail


def test_audit_chain_and_checkpoint_jws_verify_against_jwks(sec_env):
    client, mcp, _, _ = sec_env

    trail = _seed_multi_event_chain(client, mcp)
    assert trail["verified"] == {"ok": True, "bad_seq": None}

    events = trail["events"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["prev_hash"] == "0" * 64
    for idx in range(1, len(events)):
        assert events[idx]["prev_hash"] == events[idx - 1]["hash"]

    jwks_resp = client.get("/.well-known/jwks.json")
    assert jwks_resp.status_code == 200
    jwks = jwks_resp.json()
    assert len(jwks["keys"]) >= 1
    assert all("d" not in k for k in jwks["keys"])
    assert jwks["keys"][0]["kty"] == "OKP"
    assert jwks["keys"][0]["crv"] == "Ed25519"
    assert jwks["keys"][0]["alg"] == "EdDSA"

    cp = trail["checkpoint"]
    assert cp["seq"] == events[-1]["seq"]
    assert cp["hash"] == events[-1]["hash"]

    key_set = KeySet.import_key_set(jwks)
    decoded = jws.deserialize_compact(cp["jws"], key_set, algorithms=["EdDSA"])
    assert decoded.headers()["alg"] == "EdDSA"
    assert decoded.headers()["kid"] == jwks["keys"][0]["kid"]
    cp_payload = json.loads(decoded.payload)
    assert cp_payload == {"hash": cp["hash"], "seq": cp["seq"], "ts": cp["ts"]}
    assert audit.verify_checkpoint(cp["jws"], jwks) == cp_payload

    cp_h, cp_p, cp_s = cp["jws"].split(".")
    tampered_cp = f"{cp_h}.{cp_p}.{_flip_sig_byte(cp_s, 0)}"
    with pytest.raises(JoseError):
        jws.deserialize_compact(tampered_cp, key_set, algorithms=["EdDSA"])
    with pytest.raises(ValueError, match="BAD_SIGNATURE"):
        audit.verify_checkpoint(tampered_cp, jwks)


@pytest.mark.parametrize(
    "column,mutated_value,mutated_seq",
    [
        ("why", "Retroactively altered justification", 2),
        ("detail", '{"forged":true,"total_net":0}', 4),
        ("prev_hash", "a" * 64, 6),
    ],
)
def test_sqlite_audit_mutation_detected_on_rest_and_mcp(sec_env, column, mutated_value, mutated_seq):
    client, mcp, db_path, _ = sec_env

    trail_before = _seed_multi_event_chain(client, mcp)
    assert trail_before["verified"] == {"ok": True, "bad_seq": None}
    assert len(trail_before["events"]) >= mutated_seq + 1

    jwks = client.get("/.well-known/jwks.json").json()
    cp_before = trail_before["checkpoint"]
    decoded_before = jws.deserialize_compact(cp_before["jws"], KeySet.import_key_set(jwks), algorithms=["EdDSA"])
    assert json.loads(decoded_before.payload)["hash"] == cp_before["hash"]

    con = sqlite3.connect(db_path)
    try:
        con.execute(f"UPDATE audit SET {column}=? WHERE seq=?", (mutated_value, mutated_seq))
        con.commit()
    finally:
        con.close()

    admin_trail = client.get("/v1/audit", headers=ADMIN).json()
    assert admin_trail["verified"]["ok"] is False
    assert admin_trail["verified"]["bad_seq"] == mutated_seq

    aurora_trail = client.get("/v1/audit", headers=AURORA).json()
    assert aurora_trail["verified"] == {"ok": False, "bad_seq": mutated_seq}

    mcp_trail = mcp.call_ok("get_audit_trail", {}, headers=AURORA)
    assert mcp_trail["verified"] == {"ok": False, "bad_seq": mutated_seq}


def test_sqlite_audit_rehashed_single_row_and_deleted_row_detected(sec_env):
    client, mcp, db_path, _ = sec_env

    trail_before = _seed_multi_event_chain(client, mcp)
    assert trail_before["verified"] == {"ok": True, "bad_seq": None}
    saved_checkpoint_jws = trail_before["checkpoint"]["jws"]
    jwks = client.get("/.well-known/jwks.json").json()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        r3 = con.execute("SELECT * FROM audit WHERE seq=3").fetchone()
        forged_why = "Rehashed single row attack"
        row_obj = {
            "seq": r3["seq"],
            "ts": r3["ts"],
            "customer": r3["customer"],
            "actor": r3["actor"],
            "action": r3["action"],
            "subject": r3["subject"],
            "detail": audit._norm(json.loads(r3["detail"])),
            "why": forged_why,
        }
        forged_hash = audit._digest(r3["prev_hash"], row_obj)
        con.execute("UPDATE audit SET why=?, hash=? WHERE seq=3", (forged_why, forged_hash))
        con.commit()
    finally:
        con.close()

    after_rehash = client.get("/v1/audit", headers=ADMIN).json()
    assert after_rehash["verified"] == {"ok": False, "bad_seq": 4}

    pinned_head = audit.verify_checkpoint(saved_checkpoint_jws, jwks)
    assert pinned_head["hash"] == trail_before["events"][-1]["hash"]

    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM audit WHERE seq=2")
        con.commit()
    finally:
        con.close()

    after_delete = client.get("/v1/audit", headers=ADMIN).json()
    assert after_delete["verified"] == {"ok": False, "bad_seq": 3}


def test_key_rotation_preserves_quote_and_checkpoint_over_http_and_mcp(sec_env):
    client, mcp, db_path, _ = sec_env

    q_pre_rest = _issue_quote("rest", client, mcp, AURORA, quantity=50.0)
    q_pre_mcp = _issue_quote("mcp", client, mcp, TAYS, quantity=40.0)
    old_kid = client.get("/.well-known/jwks.json").json()["keys"][0]["kid"]
    cp_pre = client.get("/v1/audit", headers=ADMIN).json()["checkpoint"]

    new_kid = keys.rotate()
    assert new_kid != old_kid

    jwks = client.get("/.well-known/jwks.json").json()
    assert [k["kid"] for k in jwks["keys"]] == [new_kid, old_kid]
    assert all("d" not in k for k in jwks["keys"])

    key_set = KeySet.import_key_set(jwks)
    decoded_pre = jws.deserialize_compact(cp_pre["jws"], key_set, algorithms=["EdDSA"])
    assert decoded_pre.headers()["kid"] == old_kid
    assert json.loads(decoded_pre.payload)["hash"] == cp_pre["hash"]

    order_rest = _place_ok("rest", client, mcp, AURORA, q_pre_rest["quote_id"], q_pre_rest["jws"])
    assert order_rest["status"] == "confirmed"
    order_mcp = _place_ok("mcp", client, mcp, TAYS, q_pre_mcp["quote_id"], q_pre_mcp["jws"])
    assert order_mcp["status"] == "confirmed"
    assert _sqlite_order_count(db_path) == 2

    q_post = _issue_quote("rest", client, mcp, AURORA, quantity=30.0)
    post_hdr, _, h_b64, p_b64, s_b64 = _decode_jws_parts(q_post["jws"])
    assert post_hdr["kid"] == new_kid

    swapped_to_retired_kid = f"{_b64_json({'alg': 'EdDSA', 'kid': old_kid})}.{p_b64}.{s_b64}"
    _assert_rejected_409(
        "rest",
        client,
        mcp,
        AURORA,
        q_post["quote_id"],
        swapped_to_retired_kid,
        "BAD_SIGNATURE",
        "hotel-aurora",
        "buyer-aurora",
        db_path,
    )

    cp_post = mcp.call_ok("get_audit_trail", {}, headers=ADMIN)["checkpoint"]
    decoded_post = jws.deserialize_compact(cp_post["jws"], key_set, algorithms=["EdDSA"])
    assert decoded_post.headers()["kid"] == new_kid
    assert json.loads(decoded_post.payload)["hash"] == cp_post["hash"]

