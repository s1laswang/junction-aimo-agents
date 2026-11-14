from datetime import date, datetime, timedelta, timezone
import json
import warnings
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from aimo import approvals, audit, catalog, keys, mandates, mcp_server, offers, orders, policy, valio
from aimo.app import app
from aimo.models import Mandate, Product, Quote, QuoteLine, QuoteRequestLine, Substitution, UnitSize

HELSINKI = ZoneInfo("Europe/Helsinki")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
ADMIN_HEADERS = {"X-Admin-Key": "demo-admin"}
AURORA_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS_HEADERS = {"X-Agent-Key": "demo-hospital-tays"}
HERVANTA_HEADERS = {"X-Agent-Key": "demo-school-hervanta"}

SYNTHETIC_PRODUCTS = {
    "milk-valio-1l": Product(
        product_id="milk-valio-1l",
        name="Valio Lactose-Free Milk 1L",
        brand="Valio",
        vendor="Valio Oy",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose", "finnish"],
        origin="FI",
        temperature=6,
    ),
    "milk-valio-uht": Product(
        product_id="milk-valio-uht",
        name="Valio UHT Milk 1L",
        brand="Valio",
        vendor="Valio Oy",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose", "finnish"],
        origin="FI",
        temperature=20,
    ),
    "vendor-valio-cream": Product(
        product_id="vendor-valio-cream",
        name="Aimo Whipping Cream 1L",
        brand=None,
        vendor="Valio Oy",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose"],
        origin="FI",
        temperature=6,
    ),
    "oat-other-1l": Product(
        product_id="oat-other-1l",
        name="Nordic Oat Barista 1L",
        brand="NordicOat",
        vendor="Nordic Drinks Ab",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["oats"],
        labels=["vegan"],
        origin="SE",
        temperature=20,
    ),
    "bread-bakery-1kg": Product(
        product_id="bread-bakery-1kg",
        name="Valio Kitchen Rye Loaf 1kg",
        brand="Valio",
        vendor="Valio Oy",
        category="Bakery",
        sales_unit="KGM",
        unit_size=UnitSize(value=1.0, unit="kg"),
        allergens=["rye", "gluten"],
        labels=["finnish"],
        origin="FI",
        temperature=20,
    ),
    "snack-peanut-direct": Product(
        product_id="snack-peanut-direct",
        name="Valio Protein Peanut Bar",
        brand="Valio",
        vendor="Valio Oy",
        category="Snacks",
        sales_unit="PCS",
        unit_size=UnitSize(value=1.0, unit="pcs"),
        allergens=["peanut"],
        labels=[],
        origin="FI",
        temperature=20,
    ),
    "snack-peanut-trace": Product(
        product_id="snack-peanut-trace",
        name="Valio Berry Oat Bar",
        brand="Valio",
        vendor="Valio Oy",
        category="Snacks",
        sales_unit="PCS",
        unit_size=UnitSize(value=1.0, unit="pcs"),
        allergens=["may_contain:peanut"],
        labels=[],
        origin="FI",
        temperature=20,
    ),
    "snack-nut-short-trace": Product(
        product_id="snack-nut-short-trace",
        name="Valio Seed Cracker",
        brand="Valio",
        vendor="Valio Oy",
        category="Snacks",
        sales_unit="PCS",
        unit_size=UnitSize(value=1.0, unit="pcs"),
        allergens=["may:AN"],
        labels=[],
        origin="FI",
        temperature=20,
    ),
    "egg-pasta-direct": Product(
        product_id="egg-pasta-direct",
        name="Valio Fresh Egg Pasta",
        brand="Valio",
        vendor="Valio Oy",
        category="Pasta",
        sales_unit="KGM",
        unit_size=UnitSize(value=1.0, unit="kg"),
        allergens=["egg", "wheat"],
        labels=[],
        origin="FI",
        temperature=6,
    ),
}

PRICED_IDS = set(SYNTHETIC_PRODUCTS.keys()) | {"unlisted-mystery-item"}


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
                "clientInfo": {"name": "weekly-budget-suite", "version": "1.0"},
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


@pytest.fixture(autouse=True)
def isolate_offline_env(tmp_path, monkeypatch):
    warnings.filterwarnings("ignore", message="EdDSA is deprecated")
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    keys._cache.clear()
    offers.clear_simulation()
    catalog.reset()
    monkeypatch.setattr(valio, "_client", None)

    def forbid_catalog_load():
        raise AssertionError("Real catalog JSON load is forbidden in offline unit tests")

    def forbid_valio_http():
        raise AssertionError("Live Valio HTTP client is forbidden in offline unit tests")

    def fake_catalog_get(product_id):
        return SYNTHETIC_PRODUCTS.get(str(product_id))

    def fake_catalog_search(q="", constraints=None, limit=50):
        items = list(SYNTHETIC_PRODUCTS.values())
        if q:
            ql = q.lower()
            items = [p for p in items if ql in p.name.lower() or ql in (p.category or "").lower()]
        if constraints and constraints.brands:
            wanted = {b.lower() for b in constraints.brands}
            items = [p for p in items if (p.brand or "").lower() in wanted]
        return items[:limit]

    def fake_valio_price(customer: str, requested_date: date, ids) -> dict[str, dict]:
        out = {}
        for pid in ids:
            spid = str(pid)
            if spid in PRICED_IDS:
                out[spid] = {
                    "productId": spid,
                    "currency": "EUR",
                    "catalogPrice": {"value": 1.0},
                    "priceWithTax": {"value": 1.14},
                }
        return out

    def fake_valio_availability(customer: str, requested_date: date, ids) -> dict[str, dict]:
        out = {}
        for pid in ids:
            spid = str(pid)
            if spid in PRICED_IDS:
                prod = SYNTHETIC_PRODUCTS.get(spid)
                out[spid] = {
                    "productId": spid,
                    "salesBlock": False,
                    "closingTime": "2026-11-14T14:00:00+02:00",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": 50000.0,
                            "availableDate": requested_date.isoformat(),
                            "salesUnit": prod.sales_unit if prod else "PCS",
                        }
                    ],
                }
        return out

    monkeypatch.setattr(catalog, "_load", forbid_catalog_load)
    monkeypatch.setattr(catalog, "get", fake_catalog_get)
    monkeypatch.setattr(catalog, "search", fake_catalog_search)
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.05 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(valio, "_http", forbid_valio_http)
    monkeypatch.setattr(valio, "price", fake_valio_price)
    monkeypatch.setattr(valio, "availability", fake_valio_availability)

    yield

    keys._cache.clear()
    offers.clear_simulation()
    catalog.reset()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mcp_env():
    with TestClient(app) as c:
        yield c, McpSession(c)


def _delivery_iso(days_ahead: int = 3) -> str:
    return (datetime.now(HELSINKI).date() + timedelta(days=days_ahead)).isoformat()


def _reason_codes(decision_payload) -> list[str]:
    if isinstance(decision_payload, dict):
        return [r["code"] for r in decision_payload["reasons"]]
    return [r.code for r in decision_payload.reasons]


def _rest_quote_and_place(
    client: TestClient,
    headers: dict,
    product_id: str,
    quantity: float,
    substitute_for: str | None = None,
    ask_human: bool = False,
    note: str | None = None,
) -> dict:
    offer_line = {"product_id": product_id, "quantity": quantity}
    if substitute_for is not None:
        offer_line["substitute_for"] = substitute_for
    q_resp = client.post(
        "/v1/quotes",
        headers=headers,
        json={"offers": [offer_line], "delivery_date": _delivery_iso()},
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    place_body = {"quote_id": quote["quote_id"], "jws": quote["jws"], "ask_human": ask_human}
    if note is not None:
        place_body["note"] = note
    o_resp = client.post("/v1/orders", headers=headers, json=place_body)
    assert o_resp.status_code == 200, o_resp.text
    return o_resp.json()


def _mcp_quote_and_place(
    mcp: McpSession,
    headers: dict,
    product_id: str,
    quantity: float,
    substitute_for: str | None = None,
    ask_human: bool = False,
    note: str | None = None,
) -> dict:
    offer_line = {"product_id": product_id, "quantity": quantity}
    if substitute_for is not None:
        offer_line["substitute_for"] = substitute_for
    quote = mcp.call_ok(
        "request_quote",
        {"offers": [offer_line], "delivery_date": _delivery_iso()},
        headers=headers,
    )
    args = {"quote_id": quote["quote_id"], "jws": quote["jws"], "ask_human": ask_human}
    if note is not None:
        args["note"] = note
    return mcp.call_ok("place_order", args, headers=headers)


def _update_mandate(client: TestClient, customer: str, **updates) -> dict:
    cur = client.get(f"/v1/mandate?customer={customer}", headers=ADMIN_HEADERS)
    assert cur.status_code == 200, cur.text
    payload = {**cur.json(), **updates}
    put = client.put("/v1/mandate", headers=ADMIN_HEADERS, json=payload)
    assert put.status_code == 200, put.text
    return put.json()


def test_weekly_budget_cumulative_approve_lifecycle_rest(client):
    updated = _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=6000.0)
    assert updated["per_order_eur"] == 2500.0
    assert updated["per_week_eur"] == 6000.0
    assert orders.spent_this_week("hotel-aurora") == 0.0

    o1 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o1["total_net"] == 2000.0
    assert o1["decision"]["outcome"] == "auto"
    assert _reason_codes(o1["decision"]) == ["WITHIN_MANDATE"]
    assert "2,000.00 EUR" in o1["decision"]["reasons"][0]["text"]
    assert "6,000.00 EUR" in o1["decision"]["reasons"][0]["text"]
    assert o1["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 2000.0

    o2 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o2["total_net"] == 2000.0
    assert o2["decision"]["outcome"] == "auto"
    assert _reason_codes(o2["decision"]) == ["WITHIN_MANDATE"]
    assert "4,000.00 EUR" in o2["decision"]["reasons"][0]["text"]
    assert o2["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o3 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2100.0)
    assert o3["total_net"] == 2100.0
    assert o3["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o3["decision"]) == ["OVER_WEEK_LIMIT"]
    reason_text = o3["decision"]["reasons"][0]["text"]
    assert "2,100.00 EUR" in reason_text
    assert "4,000.00 EUR" in reason_text
    assert "6,100.00 EUR" in reason_text
    assert "6,000.00 EUR" in reason_text
    assert o3["status"] == "pending_approval"

    assert orders.spent_this_week("hotel-aurora") == 4000.0
    pending_resp = client.get("/v1/approvals", headers=AURORA_HEADERS)
    assert pending_resp.status_code == 200
    assert [x["order_id"] for x in pending_resp.json()] == [o3["order_id"]]

    dec_resp = client.post(
        f"/v1/approvals/{o3['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": True, "by": "Laura Virtanen", "note": "Weekend conference catering approved"},
    )
    assert dec_resp.status_code == 200, dec_resp.text
    decided = dec_resp.json()
    assert decided["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 6100.0
    assert client.get("/v1/approvals", headers=AURORA_HEADERS).json() == []

    o4 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 50.0)
    assert o4["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o4["decision"]) == ["OVER_WEEK_LIMIT"]
    assert "6,100.00 EUR" in o4["decision"]["reasons"][0]["text"]
    assert o4["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 6100.0

    trail = client.get("/v1/audit", headers=AURORA_HEADERS).json()
    assert trail["verified"] == {"ok": True, "bad_seq": None}


def test_weekly_budget_reject_keeps_spent_at_4000_and_exact_boundary_auto(client):
    _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=6000.0)

    o1 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    o2 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o1["status"] == "confirmed" and o2["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o3 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2100.0)
    assert o3["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o3["decision"]) == ["OVER_WEEK_LIMIT"]
    assert "2,100.00 EUR" in o3["decision"]["reasons"][0]["text"]
    assert "4,000.00 EUR" in o3["decision"]["reasons"][0]["text"]
    assert o3["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    rej_resp = client.post(
        f"/v1/approvals/{o3['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": False, "by": "Mikko Heikkinen", "note": "Defer extra shipment to next week"},
    )
    assert rej_resp.status_code == 200, rej_resp.text
    rejected = rej_resp.json()
    assert rejected["status"] == "rejected"
    assert client.get(f"/v1/orders/{o3['order_id']}", headers=AURORA_HEADERS).json()["status"] == "rejected"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o4_exact = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o4_exact["decision"]["outcome"] == "auto"
    assert _reason_codes(o4_exact["decision"]) == ["WITHIN_MANDATE"]
    assert o4_exact["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 6000.0


def test_weekly_budget_cumulative_lifecycle_over_mcp_and_python(mcp_env):
    client, mcp = mcp_env
    _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=6000.0)

    mcp_mandate = mcp.call_ok("get_mandate", {}, headers=AURORA_HEADERS)
    assert mcp_mandate["per_order_eur"] == 2500.0
    assert mcp_mandate["per_week_eur"] == 6000.0

    o1 = _mcp_quote_and_place(mcp, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o1["decision"]["outcome"] == "auto"
    assert _reason_codes(o1["decision"]) == ["WITHIN_MANDATE"]
    assert o1["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 2000.0

    o2 = _mcp_quote_and_place(mcp, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o2["decision"]["outcome"] == "auto"
    assert _reason_codes(o2["decision"]) == ["WITHIN_MANDATE"]
    assert o2["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o3_reject = _mcp_quote_and_place(mcp, AURORA_HEADERS, "milk-valio-1l", 2100.0)
    assert o3_reject["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o3_reject["decision"]) == ["OVER_WEEK_LIMIT"]
    assert "2,100.00 EUR" in o3_reject["decision"]["reasons"][0]["text"]
    assert "4,000.00 EUR" in o3_reject["decision"]["reasons"][0]["text"]
    assert o3_reject["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    pending = mcp.call_ok("list_approvals", {}, headers=AURORA_HEADERS)
    assert [x["order_id"] for x in pending] == [o3_reject["order_id"]]

    rej = mcp.call_ok(
        "decide_approval",
        {"order_id": o3_reject["order_id"], "approve": False, "by": "Laura Virtanen", "note": "Too large"},
        headers=ADMIN_HEADERS,
    )
    assert rej["status"] == "rejected"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o3_approve = _mcp_quote_and_place(mcp, AURORA_HEADERS, "milk-valio-1l", 2100.0)
    assert o3_approve["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o3_approve["decision"]) == ["OVER_WEEK_LIMIT"]
    assert "2,100.00 EUR" in o3_approve["decision"]["reasons"][0]["text"]
    assert "4,000.00 EUR" in o3_approve["decision"]["reasons"][0]["text"]
    assert o3_approve["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    py_approved = approvals.decide(o3_approve["order_id"], True, "Laura Virtanen", "Approved via Python")
    assert py_approved.status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 6100.0

    fetched = mcp.call_ok("get_order", {"order_id": o3_approve["order_id"]}, headers=AURORA_HEADERS)
    assert fetched["status"] == "confirmed"
    confirmed_list = mcp.call_ok("list_orders", {"status": "confirmed"}, headers=AURORA_HEADERS)
    assert [x["order_id"] for x in confirmed_list] == [o1["order_id"], o2["order_id"], o3_approve["order_id"]]

    trail = mcp.call_ok("get_audit_trail", {}, headers=AURORA_HEADERS)
    assert trail["verified"] == {"ok": True, "bad_seq": None}


def test_spent_this_week_exclusions_cross_customer_statuses_and_7_day_window(client, monkeypatch):
    _update_mandate(
        client,
        "hotel-aurora",
        per_order_eur=2500.0,
        per_week_eur=6000.0,
        exclude_allergens=["peanut"],
    )

    school_order = _rest_quote_and_place(client, HERVANTA_HEADERS, "milk-valio-1l", 500.0)
    assert school_order["status"] == "confirmed"
    assert orders.spent_this_week("school-hervanta") == 500.0
    assert orders.spent_this_week("hotel-aurora") == 0.0

    tays_order = _rest_quote_and_place(client, TAYS_HEADERS, "milk-valio-1l", 1200.0)
    assert tays_order["status"] == "confirmed"
    assert orders.spent_this_week("hospital-tays") == 1200.0
    assert orders.spent_this_week("hotel-aurora") == 0.0

    blocked_order = _rest_quote_and_place(client, AURORA_HEADERS, "snack-peanut-direct", 900.0)
    assert blocked_order["status"] == "blocked"
    assert blocked_order["decision"]["outcome"] == "blocked"
    assert orders.spent_this_week("hotel-aurora") == 0.0

    expiring_order = _rest_quote_and_place(
        client,
        AURORA_HEADERS,
        "milk-valio-1l",
        850.0,
        ask_human=True,
        note="Hold until cutoff passes",
    )
    assert expiring_order["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 0.0

    far_future = datetime.fromisoformat(expiring_order["valid_until"]) + timedelta(hours=2)
    with monkeypatch.context() as mp:
        mp.setattr(orders, "_now", lambda: far_future)
        mp.setattr(approvals, "_now", lambda: far_future)
        expired_check = client.get(f"/v1/orders/{expiring_order['order_id']}", headers=AURORA_HEADERS)
        assert expired_check.status_code == 200
        assert expired_check.json()["status"] == "expired"
    assert orders.get(expiring_order["order_id"]).status == "expired"
    assert orders.spent_this_week("hotel-aurora") == 0.0

    rejected_order = _rest_quote_and_place(
        client,
        AURORA_HEADERS,
        "milk-valio-1l",
        750.0,
        ask_human=True,
        note="Will be rejected",
    )
    assert rejected_order["status"] == "pending_approval"
    client.post(
        f"/v1/approvals/{rejected_order['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": False, "by": "Laura Virtanen", "note": "No"},
    )
    assert orders.get(rejected_order["order_id"]).status == "rejected"
    assert orders.spent_this_week("hotel-aurora") == 0.0

    real_now = datetime.now(timezone.utc)
    eight_days_ago = real_now - timedelta(days=8)
    with monkeypatch.context() as mp:
        mp.setattr(orders, "_now", lambda: eight_days_ago)
        stale_confirmed = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2400.0)
        assert stale_confirmed["status"] == "confirmed"

    assert orders.get(stale_confirmed["order_id"]).status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 0.0

    six_days_ago = real_now - timedelta(days=6, hours=12)
    with monkeypatch.context() as mp:
        mp.setattr(orders, "_now", lambda: six_days_ago)
        recent_confirmed = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 1800.0)
        assert recent_confirmed["status"] == "confirmed"

    assert orders.spent_this_week("hotel-aurora") == 1800.0

    fresh_order = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2500.0)
    assert fresh_order["decision"]["outcome"] == "auto"
    assert _reason_codes(fresh_order["decision"]) == ["WITHIN_MANDATE"]
    assert fresh_order["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 4300.0

    hold_for_approved_state = _rest_quote_and_place(
        client,
        AURORA_HEADERS,
        "milk-valio-1l",
        300.0,
        ask_human=True,
        note="Stepwise status check",
    )
    assert orders.spent_this_week("hotel-aurora") == 4300.0
    orders.set_status(hold_for_approved_state["order_id"], "approved", "human:Laura Virtanen", "Step 1")
    assert orders.get(hold_for_approved_state["order_id"]).status == "approved"
    assert orders.spent_this_week("hotel-aurora") == 4600.0


def test_mandate_allowed_categories_matrix_rest_mcp_and_python(mcp_env):
    client, mcp = mcp_env
    mandate_dict = _update_mandate(client, "hotel-aurora", allowed_categories=["Dairy"])
    mandate_obj = Mandate.model_validate(mandate_dict)
    now = datetime.now(timezone.utc)

    rest_blocked = _rest_quote_and_place(client, AURORA_HEADERS, "bread-bakery-1kg", 120.0)
    mcp_blocked = _mcp_quote_and_place(mcp, AURORA_HEADERS, "bread-bakery-1kg", 120.0)
    py_quote_bad = Quote(
        quote_id="q_cat_bad",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[QuoteLine(product_id="bread-bakery-1kg", quantity=120.0, unit_price_net=1.0, line_total_net=120.0)],
        total_net=120.0,
        total_gross=136.8,
        issued_at=now,
        valid_until=now + timedelta(hours=4),
    )
    py_blocked = policy.decide(py_quote_bad, mandate_obj, SYNTHETIC_PRODUCTS, now=now)

    assert rest_blocked["status"] == "blocked"
    assert mcp_blocked["status"] == "blocked"
    assert rest_blocked["decision"]["outcome"] == mcp_blocked["decision"]["outcome"] == py_blocked.outcome == "blocked"
    assert (
        _reason_codes(rest_blocked["decision"])
        == _reason_codes(mcp_blocked["decision"])
        == _reason_codes(py_blocked)
        == ["CATEGORY_NOT_ALLOWED"]
    )
    assert "Bakery" in rest_blocked["decision"]["reasons"][0]["text"]
    assert "Dairy" in rest_blocked["decision"]["reasons"][0]["text"]
    assert orders.spent_this_week("hotel-aurora") == 0.0

    rest_ok = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 150.0)
    mcp_ok = _mcp_quote_and_place(mcp, AURORA_HEADERS, "milk-valio-1l", 150.0)
    py_quote_ok = py_quote_bad.model_copy(
        update={
            "lines": [QuoteLine(product_id="milk-valio-1l", quantity=150.0, unit_price_net=1.0, line_total_net=150.0)],
            "total_net": 150.0,
        }
    )
    py_ok = policy.decide(py_quote_ok, mandate_obj, SYNTHETIC_PRODUCTS, now=now)

    assert rest_ok["status"] == "confirmed"
    assert mcp_ok["status"] == "confirmed"
    assert rest_ok["decision"]["outcome"] == mcp_ok["decision"]["outcome"] == py_ok.outcome == "auto"
    assert (
        _reason_codes(rest_ok["decision"])
        == _reason_codes(mcp_ok["decision"])
        == _reason_codes(py_ok)
        == ["WITHIN_MANDATE"]
    )
    assert orders.spent_this_week("hotel-aurora") == 300.0


def test_mandate_substitutions_modes_rest_mcp_and_python(mcp_env):
    client, mcp = mcp_env
    now = datetime.now(timezone.utc)

    tays_never_dict = _update_mandate(client, "hospital-tays", substitutions="never")
    tays_never = Mandate.model_validate(tays_never_dict)
    rest_never = _rest_quote_and_place(
        client, TAYS_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    mcp_never = _mcp_quote_and_place(
        mcp, TAYS_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    sub_valio = [Substitution(original_id="milk-valio-1l", substitute_id="milk-valio-uht", reason="swap")]
    q_sub_valio = Quote(
        quote_id="q_sub_valio",
        customer="hospital-tays",
        agent_id="buyer-tays",
        delivery_date=now.date(),
        lines=[
            QuoteLine(
                product_id="milk-valio-uht",
                quantity=100.0,
                unit_price_net=1.0,
                line_total_net=100.0,
                substitute_for="milk-valio-1l",
            )
        ],
        total_net=100.0,
        total_gross=114.0,
        issued_at=now,
        valid_until=now + timedelta(hours=4),
    )
    py_never = policy.decide(q_sub_valio, tays_never, SYNTHETIC_PRODUCTS, substitutions=sub_valio, now=now)

    assert rest_never["status"] == mcp_never["status"] == "blocked"
    assert rest_never["decision"]["outcome"] == mcp_never["decision"]["outcome"] == py_never.outcome == "blocked"
    assert (
        _reason_codes(rest_never["decision"])
        == _reason_codes(mcp_never["decision"])
        == _reason_codes(py_never)
        == ["SUBSTITUTION_NOT_ALLOWED"]
    )
    assert "hospital-tays" in rest_never["decision"]["reasons"][0]["text"]
    assert "Valio UHT Milk 1L" in rest_never["decision"]["reasons"][0]["text"]
    assert "Valio Lactose-Free Milk 1L" in rest_never["decision"]["reasons"][0]["text"]

    tays_ask_dict = _update_mandate(client, "hospital-tays", substitutions="ask")
    tays_ask = Mandate.model_validate(tays_ask_dict)
    rest_ask = _rest_quote_and_place(
        client, TAYS_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    mcp_ask = _mcp_quote_and_place(
        mcp, TAYS_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    py_ask = policy.decide(q_sub_valio, tays_ask, SYNTHETIC_PRODUCTS, substitutions=sub_valio, now=now)

    assert rest_ask["status"] == mcp_ask["status"] == "pending_approval"
    assert rest_ask["decision"]["outcome"] == mcp_ask["decision"]["outcome"] == py_ask.outcome == "needs_approval"
    assert (
        _reason_codes(rest_ask["decision"])
        == _reason_codes(mcp_ask["decision"])
        == _reason_codes(py_ask)
        == ["SUBSTITUTION_NEEDS_APPROVAL"]
    )
    assert "requires a human to approve every substitution" in rest_ask["decision"]["reasons"][0]["text"]

    aurora_prefs_dict = _update_mandate(
        client, "hotel-aurora", substitutions="within_prefs", preferred_brands=["Valio"]
    )
    aurora_prefs = Mandate.model_validate(aurora_prefs_dict)

    rest_pref_ok = _rest_quote_and_place(
        client, AURORA_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    mcp_pref_ok = _mcp_quote_and_place(
        mcp, AURORA_HEADERS, "milk-valio-uht", 100.0, substitute_for="milk-valio-1l"
    )
    rest_vendor_ok = _rest_quote_and_place(
        client, AURORA_HEADERS, "vendor-valio-cream", 50.0, substitute_for="milk-valio-1l"
    )
    py_pref_ok = policy.decide(
        q_sub_valio.model_copy(update={"customer": "hotel-aurora", "agent_id": "buyer-aurora"}),
        aurora_prefs,
        SYNTHETIC_PRODUCTS,
        substitutions=sub_valio,
        now=now,
    )

    assert rest_pref_ok["status"] == mcp_pref_ok["status"] == rest_vendor_ok["status"] == "confirmed"
    assert (
        rest_pref_ok["decision"]["outcome"]
        == mcp_pref_ok["decision"]["outcome"]
        == rest_vendor_ok["decision"]["outcome"]
        == py_pref_ok.outcome
        == "auto"
    )
    assert (
        _reason_codes(rest_pref_ok["decision"])
        == _reason_codes(mcp_pref_ok["decision"])
        == _reason_codes(py_pref_ok)
        == ["WITHIN_MANDATE"]
    )
    assert rest_pref_ok["substitutions"][0]["original_id"] == "milk-valio-1l"
    assert rest_pref_ok["substitutions"][0]["substitute_id"] == "milk-valio-uht"

    rest_nonpref = _rest_quote_and_place(
        client, AURORA_HEADERS, "oat-other-1l", 100.0, substitute_for="milk-valio-1l"
    )
    mcp_nonpref = _mcp_quote_and_place(
        mcp, AURORA_HEADERS, "oat-other-1l", 100.0, substitute_for="milk-valio-1l"
    )
    sub_nonpref = [Substitution(original_id="milk-valio-1l", substitute_id="oat-other-1l", reason="swap")]
    q_sub_nonpref = Quote(
        quote_id="q_sub_nonpref",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[
            QuoteLine(
                product_id="oat-other-1l",
                quantity=100.0,
                unit_price_net=1.0,
                line_total_net=100.0,
                substitute_for="milk-valio-1l",
            )
        ],
        total_net=100.0,
        total_gross=114.0,
        issued_at=now,
        valid_until=now + timedelta(hours=4),
    )
    py_nonpref = policy.decide(
        q_sub_nonpref, aurora_prefs, SYNTHETIC_PRODUCTS, substitutions=sub_nonpref, now=now
    )

    assert rest_nonpref["status"] == mcp_nonpref["status"] == "pending_approval"
    assert (
        rest_nonpref["decision"]["outcome"]
        == mcp_nonpref["decision"]["outcome"]
        == py_nonpref.outcome
        == "needs_approval"
    )
    assert (
        _reason_codes(rest_nonpref["decision"])
        == _reason_codes(mcp_nonpref["decision"])
        == _reason_codes(py_nonpref)
        == ["SUBSTITUTION_NEEDS_APPROVAL"]
    )
    assert "NordicOat" in rest_nonpref["decision"]["reasons"][0]["text"]
    assert "Valio" in rest_nonpref["decision"]["reasons"][0]["text"]


def test_non_substitution_non_preferred_brand_auto_with_info_reason(mcp_env):
    client, mcp = mcp_env
    now = datetime.now(timezone.utc)
    aurora_dict = _update_mandate(client, "hotel-aurora", preferred_brands=["Valio"])
    aurora = Mandate.model_validate(aurora_dict)

    rest_order = _rest_quote_and_place(client, AURORA_HEADERS, "oat-other-1l", 80.0)
    mcp_order = _mcp_quote_and_place(mcp, AURORA_HEADERS, "oat-other-1l", 80.0)
    py_quote = Quote(
        quote_id="q_direct_nonpref",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[QuoteLine(product_id="oat-other-1l", quantity=80.0, unit_price_net=1.0, line_total_net=80.0)],
        total_net=80.0,
        total_gross=91.2,
        issued_at=now,
        valid_until=now + timedelta(hours=4),
    )
    py_dec = policy.decide(py_quote, aurora, SYNTHETIC_PRODUCTS, now=now)

    assert rest_order["status"] == mcp_order["status"] == "confirmed"
    assert rest_order["decision"]["outcome"] == mcp_order["decision"]["outcome"] == py_dec.outcome == "auto"
    assert (
        _reason_codes(rest_order["decision"])
        == _reason_codes(mcp_order["decision"])
        == _reason_codes(py_dec)
        == ["WITHIN_MANDATE", "NON_PREFERRED_BRAND"]
    )
    info_text = rest_order["decision"]["reasons"][1]["text"]
    assert info_text.startswith("Note:")
    assert "NordicOat" in info_text
    assert "Valio" in info_text

    _update_mandate(client, "hotel-aurora", preferred_brands=[])
    no_pref_order = _rest_quote_and_place(client, AURORA_HEADERS, "oat-other-1l", 40.0)
    assert no_pref_order["decision"]["outcome"] == "auto"
    assert _reason_codes(no_pref_order["decision"]) == ["WITHIN_MANDATE"]


def test_missing_product_unknown_product_and_allergen_trace_vs_direct_matrix(mcp_env):
    client, mcp = mcp_env
    now = datetime.now(timezone.utc)

    aurora_no_excl = Mandate.model_validate(_update_mandate(client, "hotel-aurora", exclude_allergens=[]))
    rest_unknown_ok = _rest_quote_and_place(client, AURORA_HEADERS, "unlisted-mystery-item", 60.0)
    mcp_unknown_ok = _mcp_quote_and_place(mcp, AURORA_HEADERS, "unlisted-mystery-item", 60.0)
    q_unknown = Quote(
        quote_id="q_unknown",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[QuoteLine(product_id="unlisted-mystery-item", quantity=60.0, unit_price_net=1.0, line_total_net=60.0)],
        total_net=60.0,
        total_gross=68.4,
        issued_at=now,
        valid_until=now + timedelta(hours=4),
    )
    py_unknown_ok = policy.decide(q_unknown, aurora_no_excl, SYNTHETIC_PRODUCTS, now=now)

    assert rest_unknown_ok["status"] == mcp_unknown_ok["status"] == "confirmed"
    assert (
        rest_unknown_ok["decision"]["outcome"]
        == mcp_unknown_ok["decision"]["outcome"]
        == py_unknown_ok.outcome
        == "auto"
    )
    assert _reason_codes(rest_unknown_ok["decision"]) == ["WITHIN_MANDATE"]

    aurora_excl = Mandate.model_validate(
        _update_mandate(client, "hotel-aurora", exclude_allergens=["peanut", "nuts", "egg"])
    )
    rest_unknown_ask = _rest_quote_and_place(client, AURORA_HEADERS, "unlisted-mystery-item", 60.0)
    mcp_unknown_ask = _mcp_quote_and_place(mcp, AURORA_HEADERS, "unlisted-mystery-item", 60.0)
    py_unknown_ask = policy.decide(q_unknown, aurora_excl, SYNTHETIC_PRODUCTS, now=now)

    assert rest_unknown_ask["status"] == mcp_unknown_ask["status"] == "pending_approval"
    assert (
        rest_unknown_ask["decision"]["outcome"]
        == mcp_unknown_ask["decision"]["outcome"]
        == py_unknown_ask.outcome
        == "needs_approval"
    )
    assert (
        _reason_codes(rest_unknown_ask["decision"])
        == _reason_codes(mcp_unknown_ask["decision"])
        == _reason_codes(py_unknown_ask)
        == ["UNKNOWN_PRODUCT"]
    )
    assert "unlisted-mystery-item" in rest_unknown_ask["decision"]["reasons"][0]["text"]
    assert "product data missing" in rest_unknown_ask["decision"]["reasons"][0]["text"]

    rest_direct = _rest_quote_and_place(client, AURORA_HEADERS, "snack-peanut-direct", 25.0)
    mcp_direct = _mcp_quote_and_place(mcp, AURORA_HEADERS, "snack-peanut-direct", 25.0)
    q_direct = q_unknown.model_copy(
        update={
            "lines": [
                QuoteLine(product_id="snack-peanut-direct", quantity=25.0, unit_price_net=1.0, line_total_net=25.0)
            ],
            "total_net": 25.0,
        }
    )
    py_direct = policy.decide(q_direct, aurora_excl, SYNTHETIC_PRODUCTS, now=now)

    assert rest_direct["status"] == mcp_direct["status"] == "blocked"
    assert rest_direct["decision"]["outcome"] == mcp_direct["decision"]["outcome"] == py_direct.outcome == "blocked"
    assert (
        _reason_codes(rest_direct["decision"])
        == _reason_codes(mcp_direct["decision"])
        == _reason_codes(py_direct)
        == ["ALLERGEN_CONFLICT"]
    )
    direct_text = rest_direct["decision"]["reasons"][0]["text"]
    assert "contains peanut" in direct_text
    assert "traces" not in direct_text

    rest_trace = _rest_quote_and_place(client, AURORA_HEADERS, "snack-peanut-trace", 25.0)
    mcp_trace = _mcp_quote_and_place(mcp, AURORA_HEADERS, "snack-peanut-trace", 25.0)
    q_trace = q_unknown.model_copy(
        update={
            "lines": [
                QuoteLine(product_id="snack-peanut-trace", quantity=25.0, unit_price_net=1.0, line_total_net=25.0)
            ],
            "total_net": 25.0,
        }
    )
    py_trace = policy.decide(q_trace, aurora_excl, SYNTHETIC_PRODUCTS, now=now)

    assert rest_trace["status"] == mcp_trace["status"] == "blocked"
    assert rest_trace["decision"]["outcome"] == mcp_trace["decision"]["outcome"] == py_trace.outcome == "blocked"
    assert (
        _reason_codes(rest_trace["decision"])
        == _reason_codes(mcp_trace["decision"])
        == _reason_codes(py_trace)
        == ["ALLERGEN_CONFLICT"]
    )
    trace_text = rest_trace["decision"]["reasons"][0]["text"]
    assert "may contain traces of peanut" in trace_text

    rest_short_trace = _rest_quote_and_place(client, TAYS_HEADERS, "snack-nut-short-trace", 20.0)
    assert rest_short_trace["status"] == "blocked"
    assert _reason_codes(rest_short_trace["decision"]) == ["ALLERGEN_CONFLICT"]
    assert "may contain traces of nuts" in rest_short_trace["decision"]["reasons"][0]["text"]

    rest_egg = _rest_quote_and_place(client, TAYS_HEADERS, "egg-pasta-direct", 20.0)
    assert rest_egg["status"] == "blocked"
    assert _reason_codes(rest_egg["decision"]) == ["ALLERGEN_CONFLICT"]
    assert "contains egg" in rest_egg["decision"]["reasons"][0]["text"]


def test_python_policy_decide_combined_reasons_and_precedence_matrix():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    mandate = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=2500.0,
        per_week_eur=6000.0,
        exclude_allergens=["peanut", "egg"],
        allowed_categories=["Dairy", "Snacks"],
        substitutions="within_prefs",
        preferred_brands=["Valio"],
        approvers=["Laura Virtanen"],
    )

    combo_product = Product(
        product_id="combo-bar",
        name="Dual Risk Bar",
        brand="Valio",
        category="Snacks",
        allergens=["peanut", "may_contain:peanut", "may_contain:egg"],
    )
    products = {**SYNTHETIC_PRODUCTS, "combo-bar": combo_product}

    q_combo = Quote(
        quote_id="q_combo",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[QuoteLine(product_id="combo-bar", quantity=10.0, unit_price_net=1.0, line_total_net=10.0)],
        total_net=10.0,
        total_gross=11.4,
        issued_at=now,
        valid_until=now + timedelta(hours=3),
    )
    d_combo = policy.decide(q_combo, mandate, products, spent_this_week=0.0, now=now)
    assert d_combo.outcome == "blocked"
    assert [r.code for r in d_combo.reasons] == ["ALLERGEN_CONFLICT", "ALLERGEN_CONFLICT"]
    assert "contains peanut" in d_combo.reasons[0].text
    assert "may contain traces of egg" in d_combo.reasons[1].text
    assert "traces of peanut" not in d_combo.reasons[1].text

    q_multi_ask = Quote(
        quote_id="q_multi_ask",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[
            QuoteLine(product_id="oat-other-1l", quantity=2600.0, unit_price_net=1.0, line_total_net=2600.0),
            QuoteLine(product_id="unlisted-mystery-item", quantity=100.0, unit_price_net=1.0, line_total_net=100.0),
        ],
        total_net=2700.0,
        total_gross=3078.0,
        issued_at=now,
        valid_until=now + timedelta(hours=3),
    )
    subs = [Substitution(original_id="milk-valio-1l", substitute_id="oat-other-1l", reason="swap")]
    d_multi_ask = policy.decide(
        q_multi_ask,
        mandate,
        products,
        spent_this_week=4000.0,
        substitutions=subs,
        now=now,
    )
    assert d_multi_ask.outcome == "needs_approval"
    assert set(_reason_codes(d_multi_ask)) == {
        "UNKNOWN_PRODUCT",
        "SUBSTITUTION_NEEDS_APPROVAL",
        "OVER_ORDER_LIMIT",
        "OVER_WEEK_LIMIT",
    }
    assert "NON_PREFERRED_BRAND" not in _reason_codes(d_multi_ask)

    q_blocked_beats_ask = Quote(
        quote_id="q_blocked_beats_ask",
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        delivery_date=now.date(),
        lines=[QuoteLine(product_id="bread-bakery-1kg", quantity=3000.0, unit_price_net=1.0, line_total_net=3000.0)],
        total_net=3000.0,
        total_gross=3420.0,
        issued_at=now,
        valid_until=now - timedelta(minutes=5),
    )
    d_prec = policy.decide(q_blocked_beats_ask, mandate, products, spent_this_week=4000.0, now=now)
    assert d_prec.outcome == "blocked"
    assert set(_reason_codes(d_prec)) == {
        "CATEGORY_NOT_ALLOWED",
        "PAST_CUTOFF",
        "OVER_ORDER_LIMIT",
        "OVER_WEEK_LIMIT",
    }


def test_dynamic_mandate_updates_and_approval_guards_preserve_weekly_spend(client):
    _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=6000.0)

    _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    o3 = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2100.0)
    assert o3["status"] == "pending_approval"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    unauth_approver = client.post(
        f"/v1/approvals/{o3['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": True, "by": "Helena Korhonen", "note": "Wrong customer approver"},
    )
    assert unauth_approver.status_code == 403
    assert unauth_approver.json()["code"] == "NOT_APPROVER"
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    ok_approve = client.post(
        f"/v1/approvals/{o3['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": True, "by": "Laura Virtanen", "note": "Approved once"},
    )
    assert ok_approve.status_code == 200
    assert orders.spent_this_week("hotel-aurora") == 6100.0

    dup_approve = client.post(
        f"/v1/approvals/{o3['order_id']}",
        headers=ADMIN_HEADERS,
        json={"approve": True, "by": "Laura Virtanen", "note": "Duplicate"},
    )
    assert dup_approve.status_code == 409
    assert dup_approve.json()["code"] == "NOT_PENDING"
    assert orders.spent_this_week("hotel-aurora") == 6100.0

    _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=8500.0)
    o4_now_fits = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 2000.0)
    assert o4_now_fits["decision"]["outcome"] == "auto"
    assert _reason_codes(o4_now_fits["decision"]) == ["WITHIN_MANDATE"]
    assert o4_now_fits["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 8100.0

    _update_mandate(client, "hotel-aurora", per_order_eur=2500.0, per_week_eur=7500.0)
    o5_tightened = _rest_quote_and_place(client, AURORA_HEADERS, "milk-valio-1l", 10.0)
    assert o5_tightened["decision"]["outcome"] == "needs_approval"
    assert _reason_codes(o5_tightened["decision"]) == ["OVER_WEEK_LIMIT"]
    assert "8,100.00 EUR" in o5_tightened["decision"]["reasons"][0]["text"]
    assert "7,500.00 EUR" in o5_tightened["decision"]["reasons"][0]["text"]


def test_mcp_stdio_env_keys_weekly_budget_and_approval_parity(monkeypatch):
    mandates.put(
        mandates.get("hotel-aurora").model_copy(update={"per_order_eur": 2500.0, "per_week_eur": 6000.0})
    )
    monkeypatch.setenv("AIMO_AGENT_KEY", "demo-hotel-aurora")
    monkeypatch.delenv("AIMO_STDIO_ADMIN_KEY", raising=False)
    delivery = datetime.now(HELSINKI).date() + timedelta(days=3)

    m = mcp_server.get_mandate(None)
    assert m["customer"] == "hotel-aurora"
    assert m["per_order_eur"] == 2500.0 and m["per_week_eur"] == 6000.0

    q1 = mcp_server.request_quote(None, [QuoteRequestLine(product_id="milk-valio-1l", quantity=2000.0)], delivery)
    o1 = mcp_server.place_order(None, q1["quote_id"], q1["jws"])
    assert o1["status"] == "confirmed" and orders.spent_this_week("hotel-aurora") == 2000.0

    q2 = mcp_server.request_quote(None, [QuoteRequestLine(product_id="milk-valio-1l", quantity=2000.0)], delivery)
    o2 = mcp_server.place_order(None, q2["quote_id"], q2["jws"])
    assert o2["status"] == "confirmed" and orders.spent_this_week("hotel-aurora") == 4000.0

    q3 = mcp_server.request_quote(None, [QuoteRequestLine(product_id="milk-valio-1l", quantity=2100.0)], delivery)
    o3 = mcp_server.place_order(None, q3["quote_id"], q3["jws"])
    assert o3["status"] == "pending_approval"
    assert _reason_codes(o3["decision"]) == ["OVER_WEEK_LIMIT"]
    assert orders.spent_this_week("hotel-aurora") == 4000.0

    monkeypatch.setenv("AIMO_STDIO_ADMIN_KEY", "demo-admin")
    admin_m = mcp_server.get_mandate(None, customer="hotel-aurora")
    assert admin_m["per_week_eur"] == 6000.0
    dec = mcp_server.decide_approval(None, o3["order_id"], True, "Laura Virtanen", "Stdio approval")
    assert dec["status"] == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 6100.0


@pytest.mark.parametrize(
    "mandate_kw,pid,qty,total,spent,sub_from,valid_delta_min,expected_outcome,expected_codes",
    [
        ({}, "milk-valio-1l", 2000.0, 2000.0, 0.0, None, 120, "auto", ["WITHIN_MANDATE"]),
        ({}, "milk-valio-1l", 2000.0, 2000.0, 4000.0, None, 120, "auto", ["WITHIN_MANDATE"]),
        ({}, "milk-valio-1l", 2100.0, 2100.0, 4000.0, None, 120, "needs_approval", ["OVER_WEEK_LIMIT"]),
        ({}, "milk-valio-1l", 2600.0, 2600.0, 0.0, None, 120, "needs_approval", ["OVER_ORDER_LIMIT"]),
        (
            {},
            "milk-valio-1l",
            2600.0,
            2600.0,
            4000.0,
            None,
            120,
            "needs_approval",
            ["OVER_ORDER_LIMIT", "OVER_WEEK_LIMIT"],
        ),
        (
            {"allowed_categories": ["Dairy"]},
            "bread-bakery-1kg",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "blocked",
            ["CATEGORY_NOT_ALLOWED"],
        ),
        (
            {"allowed_categories": ["Dairy"]},
            "milk-valio-1l",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "auto",
            ["WITHIN_MANDATE"],
        ),
        (
            {"substitutions": "never"},
            "milk-valio-uht",
            50.0,
            50.0,
            0.0,
            "milk-valio-1l",
            120,
            "blocked",
            ["SUBSTITUTION_NOT_ALLOWED"],
        ),
        (
            {"substitutions": "ask"},
            "milk-valio-uht",
            50.0,
            50.0,
            0.0,
            "milk-valio-1l",
            120,
            "needs_approval",
            ["SUBSTITUTION_NEEDS_APPROVAL"],
        ),
        (
            {"substitutions": "within_prefs", "preferred_brands": ["Valio"]},
            "milk-valio-uht",
            50.0,
            50.0,
            0.0,
            "milk-valio-1l",
            120,
            "auto",
            ["WITHIN_MANDATE"],
        ),
        (
            {"substitutions": "within_prefs", "preferred_brands": ["Valio"]},
            "oat-other-1l",
            50.0,
            50.0,
            0.0,
            "milk-valio-1l",
            120,
            "needs_approval",
            ["SUBSTITUTION_NEEDS_APPROVAL"],
        ),
        (
            {"preferred_brands": ["Valio"]},
            "oat-other-1l",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "auto",
            ["WITHIN_MANDATE", "NON_PREFERRED_BRAND"],
        ),
        (
            {"exclude_allergens": ["peanut"]},
            "unlisted-mystery-item",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "needs_approval",
            ["UNKNOWN_PRODUCT"],
        ),
        (
            {"exclude_allergens": ["peanut"]},
            "snack-peanut-trace",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "blocked",
            ["ALLERGEN_CONFLICT"],
        ),
        (
            {"exclude_allergens": ["peanut"]},
            "snack-peanut-direct",
            50.0,
            50.0,
            0.0,
            None,
            120,
            "blocked",
            ["ALLERGEN_CONFLICT"],
        ),
        ({}, "milk-valio-1l", 50.0, 50.0, 0.0, None, -1, "blocked", ["PAST_CUTOFF"]),
        ({}, "milk-valio-1l", 0.0, 0.0, 0.0, None, 120, "blocked", ["INVALID_QUOTE"]),
    ],
)
def test_parametrized_policy_decide_full_matrix(
    mandate_kw,
    pid,
    qty,
    total,
    spent,
    sub_from,
    valid_delta_min,
    expected_outcome,
    expected_codes,
):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    base_mandate = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=2500.0,
        per_week_eur=6000.0,
        exclude_allergens=[],
        allowed_categories=None,
        substitutions="within_prefs",
        preferred_brands=["Valio"],
        approvers=["Laura Virtanen"],
    )
    m = base_mandate.model_copy(update=mandate_kw)
    q = Quote(
        quote_id="q_matrix",
        customer=m.customer,
        agent_id=m.agent_id,
        delivery_date=now.date(),
        lines=[
            QuoteLine(
                product_id=pid,
                quantity=qty,
                unit_price_net=1.0,
                line_total_net=total,
                substitute_for=sub_from,
            )
        ],
        total_net=total,
        total_gross=round(total * 1.14, 2),
        issued_at=now,
        valid_until=now + timedelta(minutes=valid_delta_min),
    )
    subs = [Substitution(original_id=sub_from, substitute_id=pid, reason="swap")] if sub_from else ()
    dec = policy.decide(q, m, SYNTHETIC_PRODUCTS, spent_this_week=spent, substitutions=subs, now=now)
    assert dec.outcome == expected_outcome
    if expected_codes == ["INVALID_QUOTE"]:
        assert all(c == "INVALID_QUOTE" for c in _reason_codes(dec)) and len(dec.reasons) >= 1
    else:
        assert _reason_codes(dec) == expected_codes

