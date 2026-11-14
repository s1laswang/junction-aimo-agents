from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
import json
import threading
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from aimo import allocate, approvals, audit, catalog, keys, mandates, mcp_server, offers, orders, quotes, valio
from aimo.app import app
from aimo.models import (
    Mandate,
    PlaceOrderRequest,
    Product,
    QuoteRequest,
    QuoteRequestLine,
    SimulateStock,
    UnitSize,
)

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

HEL = ZoneInfo("Europe/Helsinki")
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}
HERVANTA_HEADERS = {"X-Agent-Key": "demo-school-hervanta"}
ADMIN_HEADERS = {"X-Admin-Key": "demo-admin"}

SYNTHETIC_PRODUCTS = {
    "1001": Product(
        product_id="1001",
        name="Valio Luomu Kevytmaito laktoositon",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["organic", "free_from_lactose"],
        origin="FI",
        temperature=4,
    ),
    "1002": Product(
        product_id="1002",
        name="Valio Maito laktoositon UHT",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose"],
        origin="FI",
        temperature=20,
    ),
}


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
                "clientInfo": {"name": "weekly-budget-concurrency", "version": "1.0"},
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
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], list):
                return sc["result"]
            return sc
        blocks = [json.loads(b["text"]) for b in res.get("content", [])]
        assert len(blocks) == 1, res
        return blocks[0]

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        assert "result" in out, out
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        return json.loads(text.split(prefix, 1)[-1])


@pytest.fixture(autouse=True)
def isolated_offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    for env_var in (
        "VALIO_API_BASE",
        "VALIO_API_KEY",
        "LLM_BASE_URL",
        "AIMO_ADMIN_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_STDIO_ADMIN_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()
    allocate._data.cache_clear()

    monkeypatch.setattr(catalog, "get", lambda pid: SYNTHETIC_PRODUCTS.get(str(pid)))
    monkeypatch.setattr(
        catalog,
        "search",
        lambda q="", constraints=None, limit=50: list(SYNTHETIC_PRODUCTS.values())[:limit],
    )
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.20 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})

    upstream = {
        "unit_net": 2.00,
        "unit_gross": 2.28,
        "sales_block": {},
        "available_amount": 10000.0,
    }

    def fake_price(customer, requested_date, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": upstream["unit_net"]},
                "priceWithTax": {"value": upstream["unit_gross"]},
            }
            for pid in ids
            if str(pid) in SYNTHETIC_PRODUCTS
        }

    def fake_availability(customer, requested_date, ids):
        day = requested_date if hasattr(requested_date, "isoformat") else datetime.now(HEL).date()
        closing = datetime.combine(day, time(12, 0), HEL).isoformat()
        out = {}
        for pid in ids:
            spid = str(pid)
            if spid not in SYNTHETIC_PRODUCTS:
                continue
            blocked = bool(upstream["sales_block"].get(spid, False))
            out[spid] = {
                "productId": spid,
                "salesBlock": blocked,
                "closingTime": closing,
                "scheduleLines": [
                    {
                        "salesBlock": blocked,
                        "availableAmount": upstream["available_amount"],
                        "availableDate": day.isoformat(),
                        "salesUnit": SYNTHETIC_PRODUCTS[spid].sales_unit,
                    }
                ],
            }
        return out

    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)

    yield upstream

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()
    allocate._data.cache_clear()


def _delivery_day():
    return datetime.now(HEL).date() + timedelta(days=3)


def _assert_audit_chain_intact():
    ok, bad_seq = audit.verify()
    assert (ok, bad_seq) == (True, None)
    trail = audit.events()
    assert len(trail) > 0
    assert [ev.seq for ev in trail] == list(range(1, len(trail) + 1))
    prev = audit.GENESIS
    for ev in trail:
        assert ev.prev_hash == prev
        prev = ev.hash
    cp = audit.checkpoint()
    verified_head = audit.verify_checkpoint(cp["jws"], keys.jwks())
    assert verified_head["seq"] == len(trail)
    assert verified_head["hash"] == trail[-1].hash


def test_concurrent_orders_place_weekly_budget_cap_hotel_aurora():
    mandates.put(
        Mandate(
            customer="hotel-aurora",
            agent_id="buyer-aurora",
            per_order_eur=2500.0,
            per_week_eur=6000.0,
            exclude_allergens=[],
            allowed_categories=None,
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Laura Virtanen", "Mikko Heikkinen"],
        )
    )

    delivery = _delivery_day()
    issued_quotes = [
        orders.create_quote(
            "hotel-aurora",
            "buyer-aurora",
            QuoteRequest(
                offers=[QuoteRequestLine(product_id="1001", quantity=1000.0)],
                delivery_date=delivery,
            ),
        )
        for _ in range(6)
    ]
    assert len({q.quote_id for q in issued_quotes}) == 6
    assert all(q.total_net == 2000.0 for q in issued_quotes)
    assert sum(q.total_net for q in issued_quotes) == 12000.0

    num_threads = 6
    barrier = threading.Barrier(num_threads)

    def place_worker(q):
        req = PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws)
        barrier.wait(timeout=10)
        return orders.place("hotel-aurora", "buyer-aurora", req)

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(place_worker, q) for q in issued_quotes]
        placed_orders = [f.result(timeout=15) for f in futures]

    assert len(placed_orders) == 6
    assert len({o.order_id for o in placed_orders}) == 6
    assert {o.quote_id for o in placed_orders} == {q.quote_id for q in issued_quotes}

    confirmed = [o for o in placed_orders if o.status == "confirmed"]
    pending = [o for o in placed_orders if o.status == "pending_approval"]

    assert len(confirmed) == 3
    assert len(pending) == 3

    for o in confirmed:
        assert o.decision.outcome == "auto"
        assert [r.code for r in o.decision.reasons] == ["WITHIN_MANDATE"]
        conf = orders.confirmation(o.order_id)
        assert conf is not None and conf["mock"] is True

    confirmed_texts = [o.decision.reasons[0].text for o in confirmed]
    assert sum("2,000.00 EUR of the 6,000.00 EUR weekly budget" in t for t in confirmed_texts) == 1
    assert sum("4,000.00 EUR of the 6,000.00 EUR weekly budget" in t for t in confirmed_texts) == 1
    assert sum("6,000.00 EUR of the 6,000.00 EUR weekly budget" in t for t in confirmed_texts) == 1

    for o in pending:
        assert o.decision.outcome == "needs_approval"
        assert [r.code for r in o.decision.reasons] == ["OVER_WEEK_LIMIT"]
        assert "6,000.00 EUR already spent this week" in o.decision.reasons[0].text
        assert orders.confirmation(o.order_id) is None

    assert orders.spent_this_week("hotel-aurora") == 6000.0
    assert {o.order_id for o in approvals.pending("hotel-aurora")} == {o.order_id for o in pending}
    assert len(orders.list(customer="hotel-aurora", status="confirmed")) == 3
    assert len(orders.list(customer="hotel-aurora", status="pending_approval")) == 3

    _assert_audit_chain_intact()


def test_concurrent_http_post_orders_weekly_budget_cap_school_hervanta():
    delivery = _delivery_day().isoformat()

    with TestClient(app) as client:
        put_resp = client.put(
            "/v1/mandate",
            headers=ADMIN_HEADERS,
            json={
                "customer": "school-hervanta",
                "agent_id": "buyer-hervanta",
                "per_order_eur": 600.0,
                "per_week_eur": 1500.0,
                "exclude_allergens": ["peanut", "nuts"],
                "allowed_categories": None,
                "substitutions": "within_prefs",
                "preferred_brands": ["Valio"],
                "approvers": ["Sari Nieminen"],
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["per_week_eur"] == 1500.0

        issued_quotes = []
        for _ in range(5):
            q_resp = client.post(
                "/v1/quotes",
                headers=HERVANTA_HEADERS,
                json={"offers": [{"product_id": "1001", "quantity": 250.0}], "delivery_date": delivery},
            )
            assert q_resp.status_code == 200, q_resp.text
            q_json = q_resp.json()
            assert q_json["total_net"] == 500.0
            issued_quotes.append(q_json)

        assert len({q["quote_id"] for q in issued_quotes}) == 5

        num_threads = 5
        barrier = threading.Barrier(num_threads)

        def http_worker(q_data):
            barrier.wait(timeout=10)
            return client.post(
                "/v1/orders",
                headers=HERVANTA_HEADERS,
                json={"quote_id": q_data["quote_id"], "jws": q_data["jws"]},
            )

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(http_worker, q) for q in issued_quotes]
            responses = [f.result(timeout=15) for f in futures]

        assert all(r.status_code == 200 for r in responses)
        bodies = [r.json() for r in responses]
        confirmed = [b for b in bodies if b["status"] == "confirmed"]
        pending = [b for b in bodies if b["status"] == "pending_approval"]

        assert len(confirmed) == 3
        assert len(pending) == 2

        for b in confirmed:
            assert b["decision"]["outcome"] == "auto"
            assert [r["code"] for r in b["decision"]["reasons"]] == ["WITHIN_MANDATE"]

        for b in pending:
            assert b["decision"]["outcome"] == "needs_approval"
            assert [r["code"] for r in b["decision"]["reasons"]] == ["OVER_WEEK_LIMIT"]

        assert orders.spent_this_week("school-hervanta") == 1500.0

        pending_resp = client.get("/v1/approvals", headers=HERVANTA_HEADERS)
        assert pending_resp.status_code == 200
        assert {o["order_id"] for o in pending_resp.json()} == {b["order_id"] for b in pending}

        audit_resp = client.get("/v1/audit", headers=ADMIN_HEADERS)
        assert audit_resp.status_code == 200
        assert audit_resp.json()["verified"] == {"ok": True, "bad_seq": None}

    _assert_audit_chain_intact()


def test_concurrent_multi_customer_weekly_budgets_isolated():
    mandates.put(
        Mandate(
            customer="hotel-aurora",
            agent_id="buyer-aurora",
            per_order_eur=2500.0,
            per_week_eur=6000.0,
            exclude_allergens=[],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Laura Virtanen"],
        )
    )
    mandates.put(
        Mandate(
            customer="school-hervanta",
            agent_id="buyer-hervanta",
            per_order_eur=600.0,
            per_week_eur=1000.0,
            exclude_allergens=["peanut", "nuts"],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Sari Nieminen"],
        )
    )

    delivery = _delivery_day()
    aurora_quotes = [
        orders.create_quote(
            "hotel-aurora",
            "buyer-aurora",
            QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=1000.0)], delivery_date=delivery),
        )
        for _ in range(4)
    ]
    hervanta_quotes = [
        orders.create_quote(
            "school-hervanta",
            "buyer-hervanta",
            QuoteRequest(offers=[QuoteRequestLine(product_id="1002", quantity=250.0)], delivery_date=delivery),
        )
        for _ in range(4)
    ]

    jobs = [
        *[("hotel-aurora", "buyer-aurora", q) for q in aurora_quotes],
        *[("school-hervanta", "buyer-hervanta", q) for q in hervanta_quotes],
    ]
    barrier = threading.Barrier(len(jobs))

    def worker(customer, agent_id, q):
        req = PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws)
        barrier.wait(timeout=10)
        return orders.place(customer, agent_id, req)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        placed = [f.result(timeout=15) for f in [pool.submit(worker, c, a, q) for c, a, q in jobs]]

    aurora_orders = [o for o in placed if o.customer == "hotel-aurora"]
    hervanta_orders = [o for o in placed if o.customer == "school-hervanta"]

    assert sum(o.status == "confirmed" for o in aurora_orders) == 3
    assert sum(o.status == "pending_approval" for o in aurora_orders) == 1
    assert orders.spent_this_week("hotel-aurora") == 6000.0

    assert sum(o.status == "confirmed" for o in hervanta_orders) == 2
    assert sum(o.status == "pending_approval" for o in hervanta_orders) == 2
    assert orders.spent_this_week("school-hervanta") == 1000.0
    assert orders.spent_this_week("hospital-tays") == 0.0

    _assert_audit_chain_intact()


def test_concurrent_non_divisible_weekly_cap_and_human_approval_accounting():
    mandates.put(
        Mandate(
            customer="hotel-aurora",
            agent_id="buyer-aurora",
            per_order_eur=1000.0,
            per_week_eur=2500.0,
            exclude_allergens=[],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Laura Virtanen"],
        )
    )

    delivery = _delivery_day()
    quotes_800 = [
        orders.create_quote(
            "hotel-aurora",
            "buyer-aurora",
            QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=400.0)], delivery_date=delivery),
        )
        for _ in range(5)
    ]
    assert all(q.total_net == 800.0 for q in quotes_800)

    barrier = threading.Barrier(5)

    def worker(q):
        req = PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws)
        barrier.wait(timeout=10)
        return orders.place("hotel-aurora", "buyer-aurora", req)

    with ThreadPoolExecutor(max_workers=5) as pool:
        placed = [f.result(timeout=15) for f in [pool.submit(worker, q) for q in quotes_800]]

    confirmed = [o for o in placed if o.status == "confirmed"]
    pending = [o for o in placed if o.status == "pending_approval"]

    assert len(confirmed) == 3
    assert len(pending) == 2
    assert orders.spent_this_week("hotel-aurora") == 2400.0

    rejected = approvals.decide(pending[0].order_id, False, "Laura Virtanen", "defer extra shipment")
    assert rejected.status == "rejected"
    assert orders.spent_this_week("hotel-aurora") == 2400.0

    approved = approvals.decide(pending[1].order_id, True, "Laura Virtanen", "approve weekend surge")
    assert approved.status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 3200.0

    follow_up_quote = orders.create_quote(
        "hotel-aurora",
        "buyer-aurora",
        QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=50.0)], delivery_date=delivery),
    )
    follow_up_order = orders.place(
        "hotel-aurora",
        "buyer-aurora",
        PlaceOrderRequest(quote_id=follow_up_quote.quote_id, jws=follow_up_quote.jws),
    )
    assert follow_up_order.status == "pending_approval"
    assert [r.code for r in follow_up_order.decision.reasons] == ["OVER_WEEK_LIMIT"]
    assert orders.spent_this_week("hotel-aurora") == 3200.0

    _assert_audit_chain_intact()


def test_simulated_zero_stock_quote_guard_python_rest_and_mcp():
    delivery = _delivery_day()
    delivery_str = delivery.isoformat()

    with TestClient(app) as client:
        mcp = McpSession(client)

        sim_resp = client.post(
            "/v1/admin/simulate",
            headers=ADMIN_HEADERS,
            json={"product_id": "1001", "available_amount": 0},
        )
        assert sim_resp.status_code == 200
        assert sim_resp.json() == {"ok": True}

        with pytest.raises(quotes.QuoteError) as py_exc:
            offers.price_lines("hotel-aurora", delivery, [QuoteRequestLine(product_id="1001", quantity=10)])
        assert py_exc.value.code == "OUT_OF_STOCK"

        for qty in (10, 1):
            rest_resp = client.post(
                "/v1/quotes",
                headers=AURORA_HEADERS,
                json={"offers": [{"product_id": "1001", "quantity": qty}], "delivery_date": delivery_str},
            )
            assert rest_resp.status_code == 409, rest_resp.text
            assert rest_resp.json()["code"] == "OUT_OF_STOCK"

            mcp_err = mcp.call_err(
                "request_quote",
                {"offers": [{"product_id": "1001", "quantity": qty}], "delivery_date": delivery_str},
                headers=AURORA_HEADERS,
            )
            assert mcp_err["code"] == "OUT_OF_STOCK"

        unaffected_rest = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": [{"product_id": "1002", "quantity": 10}], "delivery_date": delivery_str},
        )
        assert unaffected_rest.status_code == 200, unaffected_rest.text
        assert quotes.verify(unaffected_rest.json()["jws"]).total_net == 20.0

        unaffected_mcp = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "1002", "quantity": 10}], "delivery_date": delivery_str},
            headers=AURORA_HEADERS,
        )
        assert quotes.verify(unaffected_mcp["jws"]).total_net == 20.0


def test_simulated_partial_stock_five_vs_ten_quote_guard_python_rest_and_mcp():
    delivery = _delivery_day()
    delivery_str = delivery.isoformat()

    offers.simulate(SimulateStock(product_id="1001", available_amount=5))

    with pytest.raises(quotes.QuoteError) as py_over_10:
        offers.price_lines("hotel-aurora", delivery, [QuoteRequestLine(product_id="1001", quantity=10)])
    assert py_over_10.value.code == "OUT_OF_STOCK"

    with pytest.raises(quotes.QuoteError) as py_over_6:
        orders.create_quote(
            "hotel-aurora",
            "buyer-aurora",
            QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=6)], delivery_date=delivery),
        )
    assert py_over_6.value.code == "OUT_OF_STOCK"

    py_exact_5 = orders.create_quote(
        "hotel-aurora",
        "buyer-aurora",
        QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=5)], delivery_date=delivery),
    )
    assert py_exact_5.total_net == 10.0
    assert quotes.verify(py_exact_5.jws).quote_id == py_exact_5.quote_id

    with TestClient(app) as client:
        mcp = McpSession(client)

        sim_resp = client.post(
            "/v1/admin/simulate",
            headers=ADMIN_HEADERS,
            json={"product_id": "1001", "available_amount": 5},
        )
        assert sim_resp.status_code == 200

        for over_qty in (10, 6):
            rest_err = client.post(
                "/v1/quotes",
                headers=AURORA_HEADERS,
                json={"offers": [{"product_id": "1001", "quantity": over_qty}], "delivery_date": delivery_str},
            )
            assert rest_err.status_code == 409, rest_err.text
            assert rest_err.json()["code"] == "OUT_OF_STOCK"

            mcp_err = mcp.call_err(
                "request_quote",
                {"offers": [{"product_id": "1001", "quantity": over_qty}], "delivery_date": delivery_str},
                headers=AURORA_HEADERS,
            )
            assert mcp_err["code"] == "OUT_OF_STOCK"

        for ok_qty, expected_net in ((5, 10.0), (3, 6.0)):
            rest_ok = client.post(
                "/v1/quotes",
                headers=AURORA_HEADERS,
                json={"offers": [{"product_id": "1001", "quantity": ok_qty}], "delivery_date": delivery_str},
            )
            assert rest_ok.status_code == 200, rest_ok.text
            rest_quote = rest_ok.json()
            assert rest_quote["total_net"] == expected_net
            assert quotes.verify(rest_quote["jws"]).quote_id == rest_quote["quote_id"]

            mcp_ok = mcp.call_ok(
                "request_quote",
                {"offers": [{"product_id": "1001", "quantity": ok_qty}], "delivery_date": delivery_str},
                headers=AURORA_HEADERS,
            )
            assert mcp_ok["total_net"] == expected_net
            assert quotes.verify(mcp_ok["jws"]).quote_id == mcp_ok["quote_id"]


def test_simulated_stockout_multiline_quote_rejected_atomically():
    delivery_str = _delivery_day().isoformat()
    offers.simulate(SimulateStock(product_id="1001", available_amount=5))

    multiline_offers = [
        {"product_id": "1002", "quantity": 10},
        {"product_id": "1001", "quantity": 10},
    ]

    with TestClient(app) as client:
        mcp = McpSession(client)

        rest_resp = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": multiline_offers, "delivery_date": delivery_str},
        )
        assert rest_resp.status_code == 409
        assert rest_resp.json()["code"] == "OUT_OF_STOCK"

        mcp_err = mcp.call_err(
            "request_quote",
            {"offers": multiline_offers, "delivery_date": delivery_str},
            headers=AURORA_HEADERS,
        )
        assert mcp_err["code"] == "OUT_OF_STOCK"

    assert audit.events() == []


def test_upstream_sales_block_and_delete_simulation_restores_quoting(isolated_offline_env):
    delivery = _delivery_day()
    delivery_str = delivery.isoformat()
    isolated_offline_env["sales_block"]["1001"] = True

    with pytest.raises(quotes.QuoteError) as py_exc:
        offers.price_lines("hotel-aurora", delivery, [QuoteRequestLine(product_id="1001", quantity=5)])
    assert py_exc.value.code == "OUT_OF_STOCK"

    with TestClient(app) as client:
        mcp = McpSession(client)

        rest_blocked = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 5}], "delivery_date": delivery_str},
        )
        assert rest_blocked.status_code == 409, rest_blocked.text
        assert rest_blocked.json()["code"] == "OUT_OF_STOCK"

        mcp_blocked = mcp.call_err(
            "request_quote",
            {"offers": [{"product_id": "1001", "quantity": 5}], "delivery_date": delivery_str},
            headers=AURORA_HEADERS,
        )
        assert mcp_blocked["code"] == "OUT_OF_STOCK"

        sim_zero = client.post(
            "/v1/admin/simulate",
            headers=ADMIN_HEADERS,
            json={"product_id": "1001", "available_amount": 0},
        )
        assert sim_zero.status_code == 200

        del_while_blocked = client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS)
        assert del_while_blocked.status_code == 200
        assert del_while_blocked.json() == {"ok": True}

        still_blocked_rest = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 5}], "delivery_date": delivery_str},
        )
        assert still_blocked_rest.status_code == 409
        assert still_blocked_rest.json()["code"] == "OUT_OF_STOCK"

        sim_again = client.post(
            "/v1/admin/simulate",
            headers=ADMIN_HEADERS,
            json={"product_id": "1001", "available_amount": 0},
        )
        assert sim_again.status_code == 200

        isolated_offline_env["sales_block"]["1001"] = False

        sim_still_zero = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 5}], "delivery_date": delivery_str},
        )
        assert sim_still_zero.status_code == 409
        assert sim_still_zero.json()["code"] == "OUT_OF_STOCK"

        clear_resp = client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS)
        assert clear_resp.status_code == 200
        assert clear_resp.json() == {"ok": True}

        rest_restored = client.post(
            "/v1/quotes",
            headers=AURORA_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 10}], "delivery_date": delivery_str},
        )
        assert rest_restored.status_code == 200, rest_restored.text
        rest_q = rest_restored.json()
        assert rest_q["total_net"] == 20.0

        mcp_restored = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "1001", "quantity": 10}], "delivery_date": delivery_str},
            headers=AURORA_HEADERS,
        )
        assert mcp_restored["total_net"] == 20.0

        mcp_placed = mcp.call_ok(
            "place_order",
            {"quote_id": mcp_restored["quote_id"], "jws": mcp_restored["jws"]},
            headers=AURORA_HEADERS,
        )
        assert mcp_placed["status"] == "confirmed"

    _assert_audit_chain_intact()


def test_concurrent_mixed_http_and_python_orders_share_weekly_cap():
    delivery = _delivery_day()
    mandates.put(
        Mandate(
            customer="hotel-aurora",
            agent_id="buyer-aurora",
            per_order_eur=2500.0,
            per_week_eur=6000.0,
            exclude_allergens=[],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Laura Virtanen", "Mikko Heikkinen"],
        )
    )

    issued = [
        orders.create_quote(
            "hotel-aurora",
            "buyer-aurora",
            QuoteRequest(offers=[QuoteRequestLine(product_id="1001", quantity=1000.0)], delivery_date=delivery),
        )
        for _ in range(6)
    ]

    barrier = threading.Barrier(6)

    with TestClient(app) as client:
        def mixed_worker(idx, q):
            barrier.wait(timeout=10)
            if idx % 2 == 0:
                resp = client.post(
                    "/v1/orders",
                    headers=AURORA_HEADERS,
                    json={"quote_id": q.quote_id, "jws": q.jws},
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()
                return data["status"], data["decision"]["outcome"], [r["code"] for r in data["decision"]["reasons"]]
            placed = orders.place("hotel-aurora", "buyer-aurora", PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws))
            return placed.status, placed.decision.outcome, [r.code for r in placed.decision.reasons]

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = [f.result(timeout=15) for f in [pool.submit(mixed_worker, i, q) for i, q in enumerate(issued)]]

    assert outcomes.count(("confirmed", "auto", ["WITHIN_MANDATE"])) == 3
    assert outcomes.count(("pending_approval", "needs_approval", ["OVER_WEEK_LIMIT"])) == 3
    assert orders.spent_this_week("hotel-aurora") == 6000.0

    _assert_audit_chain_intact()


def test_mcp_stdio_tool_stockout_and_weekly_budget_parity(monkeypatch, isolated_offline_env):
    monkeypatch.setenv("AIMO_AGENT_KEY", "demo-school-hervanta")
    delivery = _delivery_day()

    mandates.put(
        Mandate(
            customer="school-hervanta",
            agent_id="buyer-hervanta",
            per_order_eur=600.0,
            per_week_eur=1000.0,
            exclude_allergens=["peanut", "nuts"],
            substitutions="within_prefs",
            preferred_brands=["Valio"],
            approvers=["Sari Nieminen"],
        )
    )

    offers.simulate(SimulateStock(product_id="1001", available_amount=5))
    with pytest.raises(ToolError) as over_sim_err:
        mcp_server.request_quote(None, [QuoteRequestLine(product_id="1001", quantity=10)], delivery)
    assert json.loads(str(over_sim_err.value))["code"] == "OUT_OF_STOCK"

    ok_five = mcp_server.request_quote(None, [QuoteRequestLine(product_id="1001", quantity=5)], delivery)
    assert ok_five["total_net"] == 10.0

    isolated_offline_env["sales_block"]["1001"] = True
    offers.clear_simulation()
    with pytest.raises(ToolError) as blocked_err:
        mcp_server.request_quote(None, [QuoteRequestLine(product_id="1001", quantity=5)], delivery)
    assert json.loads(str(blocked_err.value))["code"] == "OUT_OF_STOCK"

    isolated_offline_env["sales_block"]["1001"] = False
    quotes_500 = [
        mcp_server.request_quote(None, [QuoteRequestLine(product_id="1001", quantity=250)], delivery)
        for _ in range(3)
    ]
    first = mcp_server.place_order(None, quotes_500[0]["quote_id"], quotes_500[0]["jws"])
    second = mcp_server.place_order(None, quotes_500[1]["quote_id"], quotes_500[1]["jws"])
    third = mcp_server.place_order(None, quotes_500[2]["quote_id"], quotes_500[2]["jws"])

    assert first["status"] == "confirmed"
    assert second["status"] == "confirmed"
    assert third["status"] == "pending_approval"
    assert [r["code"] for r in third["decision"]["reasons"]] == ["OVER_WEEK_LIMIT"]
    assert orders.spent_this_week("school-hervanta") == 1000.0

    _assert_audit_chain_intact()

