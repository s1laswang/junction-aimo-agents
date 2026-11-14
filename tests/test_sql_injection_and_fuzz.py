import hashlib
import json
import sqlite3
import urllib.parse
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from aimo import allocate, audit, catalog, keys, mandates, offers, quotes, valio
from aimo.app import app
from aimo.models import Product, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS = {"X-Agent-Key": "demo-hospital-tays"}
HERVANTA = {"X-Agent-Key": "demo-school-hervanta"}
ADMIN = {"X-Admin-Key": "demo-admin"}

ALLOWED_STATUSES = {200, 400, 401, 403, 404, 409, 422}

SQLI_PAYLOADS = [
    "' OR '1'='1",
    "hotel-aurora'; DROP TABLE orders; --",
    "' UNION SELECT * FROM mandates --",
    "q_1' OR 1=1 --",
]

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

P_FI_SV = Product(
    product_id="400300",
    name="Valio laktoositon täysmaito 1 L - pähkinätön & gluteeniton (mjölk äöåÄÖÅ, 1,50 €)",
    brand="Valio",
    vendor="Valio Oy",
    category="Dairy",
    sales_unit="LTR",
    unit_size=UnitSize(value=1.0, unit="l"),
    allergens=[],
    labels=["free_from_lactose", "free_from_gluten", "finnish"],
    origin="FI",
    temperature=6,
)

PRODUCTS = {p.product_id: p for p in (P_MILK, P_OAT, P_FI_SV)}
UNIT_NET = 1.50
UNIT_GROSS = 1.71


def _delivery_iso(days: int = 3) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _fake_search(q="", constraints=None, limit=50):
    if limit <= 0:
        return []
    out = list(PRODUCTS.values())
    if q:
        ql = q.lower()
        out = [
            p
            for p in out
            if ql in p.name.lower()
            or ("milk" in ql and "maito" in p.name.lower())
            or ("täysmaito" in ql and "täysmaito" in p.name.lower())
        ]
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
                "clientInfo": {"name": "sqli-fuzz-suite", "version": "1.0"},
            },
        )
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, "mcp-session-id": self.sid},
        )
        assert r.status_code in (200, 202, 204), r.text

    def rpc_raw(self, payload: dict, headers: dict | None = None):
        req_headers = dict(MCP_HEADERS)
        if self.sid and payload.get("method") != "initialize":
            req_headers["mcp-session-id"] = self.sid
        if headers:
            req_headers.update(headers)
        r = self.client.post("/mcp", json=payload, headers=req_headers)
        assert r.status_code < 500, r.text
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        return r

    def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        self.seq += 1
        r = self.rpc_raw(
            {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
        return json.loads(body)

    def call_any(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        assert "result" in out or "error" in out, out
        return out

    def call_ok(self, name: str, arguments: dict | None = None, headers: dict | None = None):
        out = self.call_any(name, arguments=arguments, headers=headers)
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
        out = self.call_any(name, arguments=arguments, headers=headers)
        assert "result" in out, out
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        payload = text.split(prefix, 1)[-1]
        return json.loads(payload)


@pytest.fixture
def fuzz_env(tmp_path, monkeypatch):
    db_path = tmp_path / "fuzz.db"
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
    allocate._data.cache_clear()
    catalog.reset()

    monkeypatch.setattr(catalog, "search", _fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.05)
    monkeypatch.setattr(valio, "price", _fake_price)
    monkeypatch.setattr(valio, "availability", _fake_availability)

    with TestClient(app) as client:
        yield client, McpSession(client), db_path, tmp_path

    keys._cache.clear()
    offers._overrides.clear()
    allocate._data.cache_clear()
    catalog.reset()


def _table_counts(db_path) -> dict[str, int]:
    tables = ("orders", "quotes", "mandates", "audit")
    con = sqlite3.connect(db_path)
    try:
        existing = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for t in tables:
            assert t in existing, f"missing SQLite table {t}"
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def _table_snapshot(db_path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(db_path)
    try:
        return {
            "orders": con.execute(
                "SELECT order_id, idem, customer, status, data FROM orders ORDER BY order_id"
            ).fetchall(),
            "quotes": con.execute(
                "SELECT quote_id, customer, agent_id, payload, jws, used_at FROM quotes ORDER BY quote_id"
            ).fetchall(),
            "mandates": con.execute(
                "SELECT customer, agent_id, body FROM mandates ORDER BY customer, agent_id"
            ).fetchall(),
        }
    finally:
        con.close()


def _seed_two_tenants(client: TestClient, db_path):
    for cust in ("hotel-aurora", "hospital-tays"):
        m = client.get("/v1/mandate", params={"customer": cust}, headers=ADMIN)
        assert m.status_code == 200, m.text
        r_put = client.put("/v1/mandate", json=m.json(), headers=ADMIN)
        assert r_put.status_code == 200, r_put.text

    seeded = {}
    for slug, hdr, pid in (
        ("aurora", AURORA, "400100"),
        ("tays", TAYS, "400200"),
    ):
        customer_name = "hotel-aurora" if slug == "aurora" else "hospital-tays"
        rp = client.post(
            "/v1/needs/parse",
            json={"text": f"20 litres milk by {_delivery_iso(3)}", "customer": customer_name},
            headers=hdr,
        )
        assert rp.status_code == 200, rp.text

        ro = client.post(
            "/v1/offers",
            json={
                "customer": customer_name,
                "deliver_by": _delivery_iso(3),
                "items": [{"query": "milk", "quantity": 10, "unit": "l"}],
            },
            headers=hdr,
        )
        assert ro.status_code == 200, ro.text

        q1 = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": pid, "quantity": 10}], "delivery_date": _delivery_iso(3)},
            headers=hdr,
        ).json()
        o1 = client.post(
            "/v1/orders",
            json={"quote_id": q1["quote_id"], "jws": q1["jws"], "ask_human": False},
            headers=hdr,
        ).json()
        assert o1["status"] == "confirmed"

        q2 = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": pid, "quantity": 12}], "delivery_date": _delivery_iso(3)},
            headers=hdr,
        ).json()
        o2 = client.post(
            "/v1/orders",
            json={
                "quote_id": q2["quote_id"],
                "jws": q2["jws"],
                "ask_human": True,
                "note": f"secret-{slug}-pending-note",
            },
            headers=hdr,
        ).json()
        assert o2["status"] == "pending_approval"

        q3 = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": pid, "quantity": 5}], "delivery_date": _delivery_iso(3)},
            headers=hdr,
        ).json()

        seeded[slug] = {
            "confirmed_order": o1["order_id"],
            "pending_order": o2["order_id"],
            "quotes": [q1["quote_id"], q2["quote_id"], q3["quote_id"]],
            "unused_quote": q3,
        }

    counts = _table_counts(db_path)
    assert counts["orders"] == 4
    assert counts["quotes"] == 6
    assert counts["mandates"] == 3
    assert counts["audit"] >= 14
    assert audit.verify() == (True, None)
    return seeded, counts, _table_snapshot(db_path)


def _assert_no_tays_leak(body, seeded_tays: dict):
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    assert "hospital-tays" not in raw
    assert "buyer-tays" not in raw
    assert "secret-tays-pending-note" not in raw
    assert seeded_tays["confirmed_order"] not in raw
    assert seeded_tays["pending_order"] not in raw
    for qid in seeded_tays["quotes"]:
        assert qid not in raw


def test_sqli_rest_read_path_query_and_header_isolation_and_exact_row_counts(fuzz_env):
    client, _, db_path, _ = fuzz_env
    seeded, base_counts, base_snap = _seed_two_tenants(client, db_path)
    tays = seeded["tays"]
    aurora = seeded["aurora"]

    for payload in SQLI_PAYLOADS:
        enc = urllib.parse.quote(payload, safe="")

        r = client.get(
            "/v1/products",
            params={
                "q": payload,
                "origin": payload,
                "exclude_allergens": [payload],
                "labels": [payload],
                "brands": [payload],
            },
        )
        assert r.status_code in ALLOWED_STATUSES, r.text

        r = client.get(f"/v1/products/{enc}")
        assert r.status_code == 404, r.text

        r = client.get("/v1/orders", params={"customer": payload}, headers=AURORA)
        assert r.status_code == 403, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/orders", params={"status": payload}, headers=AURORA)
        assert r.status_code == 200, r.text
        assert r.json() == []

        r = client.get("/v1/orders", params={"customer": payload}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == []

        r = client.get("/v1/orders", params={"status": payload}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == []

        r = client.get(f"/v1/orders/{enc}", headers=AURORA)
        assert r.status_code == 404, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get(f"/v1/orders/{tays['confirmed_order']}", headers=AURORA)
        assert r.status_code == 403, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/approvals", params={"customer": payload}, headers=AURORA)
        assert r.status_code == 403, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/approvals", params={"customer": payload}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == []

        r = client.post(
            f"/v1/approvals/{enc}",
            json={"approve": True, "by": "Laura Virtanen", "note": payload},
            headers=ADMIN,
        )
        assert r.status_code == 404, r.text

        r = client.post(
            f"/v1/approvals/{aurora['pending_order']}",
            json={"approve": True, "by": payload, "note": payload},
            headers=ADMIN,
        )
        assert r.status_code == 403, r.text

        r = client.get("/v1/audit", params={"customer": payload}, headers=AURORA)
        assert r.status_code == 403, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/audit", params={"order_id": payload}, headers=AURORA)
        assert r.status_code == 200, r.text
        assert r.json()["events"] == []
        assert r.json()["verified"] == {"ok": True, "bad_seq": None}
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/audit", params={"order_id": tays["confirmed_order"]}, headers=AURORA)
        assert r.status_code == 200, r.text
        assert r.json()["events"] == []
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/audit", params={"customer": payload, "order_id": payload}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["events"] == []
        assert r.json()["verified"] == {"ok": True, "bad_seq": None}

        r = client.get("/v1/mandate", params={"customer": payload}, headers=AURORA)
        assert r.status_code == 403, r.text
        _assert_no_tays_leak(r.json(), tays)

        r = client.get("/v1/mandate", params={"customer": payload}, headers=ADMIN)
        assert r.status_code == 404, r.text

        bad_agent = {"X-Agent-Key": payload}
        for method, path, body in (
            ("GET", "/v1/orders", None),
            ("GET", "/v1/approvals", None),
            ("GET", "/v1/audit", None),
            ("GET", "/v1/mandate", None),
            (
                "POST",
                "/v1/offers",
                {
                    "customer": "hotel-aurora",
                    "deliver_by": _delivery_iso(3),
                    "items": [{"query": "milk", "quantity": 5, "unit": "l"}],
                },
            ),
            (
                "POST",
                "/v1/quotes",
                {"offers": [{"product_id": "400100", "quantity": 5}], "delivery_date": _delivery_iso(3)},
            ),
            (
                "POST",
                "/v1/orders",
                {"quote_id": aurora["unused_quote"]["quote_id"], "jws": aurora["unused_quote"]["jws"]},
            ),
        ):
            resp = client.request(method, path, json=body, headers=bad_agent)
            assert resp.status_code == 401, resp.text
            _assert_no_tays_leak(resp.json(), tays)

        bad_admin = {"X-Admin-Key": payload}
        resp = client.post(
            f"/v1/approvals/{aurora['pending_order']}",
            json={"approve": True, "by": "Laura Virtanen", "note": "ok"},
            headers=bad_admin,
        )
        assert resp.status_code == 401, resp.text

        r_quote = client.post(
            "/v1/quotes",
            json={
                "offers": [{"product_id": payload, "quantity": 5, "substitute_for": payload}],
                "delivery_date": _delivery_iso(3),
            },
            headers=AURORA,
        )
        assert r_quote.status_code == 409, r_quote.text

        r_sim = client.post(
            "/v1/admin/simulate",
            json={"product_id": payload, "available_amount": 0},
            headers=ADMIN,
        )
        assert r_sim.status_code == 200, r_sim.text
        client.delete("/v1/admin/simulate", headers=ADMIN)

    r_aurora_orders = client.get("/v1/orders", headers=AURORA)
    assert r_aurora_orders.status_code == 200
    assert {o["order_id"] for o in r_aurora_orders.json()} == {
        aurora["confirmed_order"],
        aurora["pending_order"],
    }
    _assert_no_tays_leak(r_aurora_orders.json(), tays)

    r_aurora_audit = client.get("/v1/audit", headers=AURORA)
    assert r_aurora_audit.status_code == 200
    assert all(e["customer"] == "hotel-aurora" for e in r_aurora_audit.json()["events"])
    _assert_no_tays_leak(r_aurora_audit.json(), tays)

    assert _table_counts(db_path) == base_counts
    assert _table_snapshot(db_path) == base_snap
    assert audit.verify() == (True, None)


def test_sqli_mcp_tools_and_write_audit_parameterization(fuzz_env):
    client, mcp, db_path, _ = fuzz_env
    seeded, base_counts, base_snap = _seed_two_tenants(client, db_path)
    tays = seeded["tays"]
    aurora = seeded["aurora"]

    expected_audit_delta = 0

    for payload in SQLI_PAYLOADS:
        prods = mcp.call_ok("search_products", {"q": payload, "origin": payload, "brands": [payload]})
        assert isinstance(prods, list)

        err = mcp.call_err("get_product", {"product_id": payload})
        assert err["code"] == "not_found"

        err = mcp.call_err("list_orders", {"customer": payload}, headers=AURORA)
        assert err["code"] == "forbidden"
        _assert_no_tays_leak(err, tays)

        rows = mcp.call_ok("list_orders", {"status": payload}, headers=AURORA)
        assert rows == []

        rows_admin = mcp.call_ok("list_orders", {"customer": payload, "status": payload}, headers=ADMIN)
        assert rows_admin == []

        err = mcp.call_err("get_order", {"order_id": payload}, headers=AURORA)
        assert err["code"] == "not_found"

        err = mcp.call_err("get_order", {"order_id": tays["pending_order"]}, headers=AURORA)
        assert err["code"] == "forbidden"
        _assert_no_tays_leak(err, tays)

        err = mcp.call_err("list_approvals", {"customer": payload}, headers=AURORA)
        assert err["code"] == "forbidden"

        assert mcp.call_ok("list_approvals", {"customer": payload}, headers=ADMIN) == []

        err = mcp.call_err(
            "decide_approval",
            {"order_id": payload, "approve": True, "by": "Laura Virtanen", "note": payload},
            headers=ADMIN,
        )
        assert err["code"] == "NOT_FOUND"

        err = mcp.call_err(
            "decide_approval",
            {"order_id": aurora["pending_order"], "approve": True, "by": payload, "note": payload},
            headers=ADMIN,
        )
        assert err["code"] == "NOT_APPROVER"

        trail = mcp.call_ok("get_audit_trail", {"order_id": payload}, headers=AURORA)
        assert trail["events"] == []
        assert trail["verified"] == {"ok": True, "bad_seq": None}
        _assert_no_tays_leak(trail, tays)

        err = mcp.call_err("get_mandate", {"customer": payload}, headers=AURORA)
        assert err["code"] == "forbidden"

        err = mcp.call_err("get_mandate", {"customer": payload}, headers=ADMIN)
        assert err["code"] == "not_found"

        err = mcp.call_err("list_orders", {}, headers={"X-Agent-Key": payload})
        assert err["code"] == "unauthorized"

        err = mcp.call_err(
            "get_offers",
            {
                "customer": payload,
                "deliver_by": _delivery_iso(3),
                "items": [{"query": payload, "quantity": 5, "unit": "l"}],
            },
            headers=AURORA,
        )
        assert err["code"] == "forbidden"

        err = mcp.call_err(
            "request_quote",
            {
                "offers": [{"product_id": payload, "quantity": 5}],
                "delivery_date": _delivery_iso(3),
            },
            headers=AURORA,
        )
        assert err["code"] == "INVALID_QUOTE"

        r_parse = client.post(
            "/v1/needs/parse",
            json={"text": payload, "customer": payload},
            headers={"X-Agent-Key": payload},
        )
        assert r_parse.status_code == 200, r_parse.text
        assert r_parse.json()["customer"] == payload
        assert r_parse.json()["raw_text"] == payload
        expected_audit_delta += 1

        m_parse = mcp.call_ok(
            "parse_request",
            {"text": payload, "customer": payload},
            headers={"X-Agent-Key": payload},
        )
        assert m_parse["customer"] == payload
        assert m_parse["raw_text"] == payload
        expected_audit_delta += 1

        r_place = client.post(
            "/v1/orders",
            json={
                "quote_id": payload,
                "jws": aurora["unused_quote"]["jws"],
                "ask_human": True,
                "note": payload,
            },
            headers=AURORA,
        )
        assert r_place.status_code == 409, r_place.text
        assert r_place.json()["code"] == "UNKNOWN_QUOTE"
        expected_audit_delta += 1

        m_place = mcp.call_err(
            "place_order",
            {
                "quote_id": payload,
                "jws": aurora["unused_quote"]["jws"],
                "ask_human": True,
                "note": payload,
            },
            headers=AURORA,
        )
        assert m_place["code"] == "UNKNOWN_QUOTE"
        expected_audit_delta += 1

    post_counts = _table_counts(db_path)
    assert post_counts["orders"] == base_counts["orders"] == 4
    assert post_counts["quotes"] == base_counts["quotes"] == 6
    assert post_counts["mandates"] == base_counts["mandates"] == 3
    assert post_counts["audit"] == base_counts["audit"] + expected_audit_delta
    assert _table_snapshot(db_path) == base_snap
    assert audit.verify() == (True, None)

    tays_audit = mcp.call_ok("get_audit_trail", {}, headers=TAYS)
    assert all(e["customer"] == "hospital-tays" for e in tays_audit["events"])
    assert tays_audit["verified"] == {"ok": True, "bad_seq": None}


def test_sqli_against_real_sqlite_fts5_catalog_index_with_synthetic_fixture(tmp_path, monkeypatch):
    tiny_dir = tmp_path / "tiny_catalog_data"
    tiny_dir.mkdir(parents=True, exist_ok=True)
    synthetic_products = [
        {
            "id": "900100",
            "salesUnit": "LTR",
            "baseUnit": "LTR",
            "units": [{"unitId": "LTR", "sizeInBaseUnits": 1.0}],
            "countryOfOrigin": "FI",
            "vendorName": "Valio Oy",
            "category": "Dairy",
            "temperatureCondition": 6,
            "synkkaData": {
                "brand": "Valio",
                "names": [
                    {"language": "fi", "value": "Valio laktoositon täysmaito 1 L"},
                    {"language": "sv", "value": "Valio laktosfri helmjölk äöåÄÖÅ 1 L"},
                    {"language": "en", "value": "Valio lactose free whole milk 1 L"},
                ],
                "classifications": [
                    {"name": "allergen", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}]},
                ],
                "unitConversions": [
                    {
                        "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                        "volume": {"value": 1.0, "unit": "LTR"},
                    }
                ],
            },
        }
    ]
    (tiny_dir / "valio_aimo_junction26_product_data.json").write_text(
        json.dumps(synthetic_products, ensure_ascii=False), encoding="utf-8"
    )

    fts_db = tmp_path / "fts.db"
    monkeypatch.setenv("AIMO_DB", str(fts_db))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tiny_dir))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "fts_keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "fts_cache"))
    catalog.reset()

    try:
        with TestClient(app) as client:
            mcp = McpSession(client)
            fts_payloads = SQLI_PAYLOADS + [
                'milk" OR 1=1 --',
                'NEAR("maito" "milk", 5) OR NOT *',
                "laktoositon täysmaito äöåÄÖÅ €",
                "((())) AND OR NOT ^ *",
            ]
            for q_str in fts_payloads:
                r = client.get("/v1/products", params={"q": q_str})
                assert r.status_code == 200, r.text
                m_res = mcp.call_ok("search_products", {"q": q_str})
                assert isinstance(m_res, list)

            hit = client.get("/v1/products", params={"q": "laktoositon täysmaito"}).json()
            assert len(hit) == 1
            assert hit[0]["product_id"] == "900100"
            assert "täysmaito" in hit[0]["name"]

            con = sqlite3.connect(fts_db)
            try:
                tables = {
                    r[0]
                    for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                assert "catalog_fts" in tables
                assert "catalog_meta" in tables
                assert con.execute("SELECT count(*) FROM catalog_fts").fetchone()[0] == 1
            finally:
                con.close()
    finally:
        catalog.reset()


def test_finnish_swedish_unicode_roundtrip_jws_and_sha256_audit_chain(fuzz_env):
    client, mcp, _, _ = fuzz_env

    mandate_payload = {
        "customer": "school-hervanta",
        "agent_id": "buyer-hervanta",
        "per_order_eur": 900.0,
        "per_week_eur": 4500.0,
        "exclude_allergens": ["peanut", "nuts"],
        "allowed_categories": ["Dairy"],
        "substitutions": "ask",
        "preferred_brands": ["Valio", "Äkta Mjölk & Grädde äöåÄÖÅ"],
        "approvers": ["Sari Nieminen", "Åsa Öhman"],
    }
    r_put = client.put("/v1/mandate", json=mandate_payload, headers=ADMIN)
    assert r_put.status_code == 200, r_put.text
    assert r_put.json()["approvers"] == ["Sari Nieminen", "Åsa Öhman"]
    assert r_put.json()["preferred_brands"] == ["Valio", "Äkta Mjölk & Grädde äöåÄÖÅ"]

    r_get_m = client.get("/v1/mandate", headers=HERVANTA)
    assert r_get_m.status_code == 200
    assert r_get_m.json()["approvers"] == ["Sari Nieminen", "Åsa Öhman"]
    m_get_m = mcp.call_ok("get_mandate", {}, headers=HERVANTA)
    assert m_get_m["preferred_brands"] == ["Valio", "Äkta Mjölk & Grädde äöåÄÖÅ"]

    fi_text = (
        "Tilaan 40 L laktoositon täysmaito, pähkinätön & gluteeniton, "
        "enintään 2,20 € / l"
    )
    r_parse = client.post(
        "/v1/needs/parse",
        json={"text": fi_text, "customer": "school-hervanta"},
        headers=HERVANTA,
    )
    assert r_parse.status_code == 200, r_parse.text
    parsed = r_parse.json()
    assert parsed["raw_text"].encode("utf-8") == fi_text.encode("utf-8")
    assert parsed["items"][0]["query"] == "täysmaito"
    assert parsed["items"][0]["quantity"] == 40.0
    assert parsed["items"][0]["unit"] == "L"
    assert parsed["items"][0]["constraints"]["lactose_free"] is True
    assert parsed["items"][0]["constraints"]["gluten_free"] is True
    assert "nuts" in parsed["items"][0]["constraints"]["exclude_allergens"]
    assert parsed["items"][0]["constraints"]["max_unit_price"] == 2.2

    m_parsed = mcp.call_ok(
        "parse_request",
        {"text": fi_text, "customer": "school-hervanta"},
        headers=HERVANTA,
    )
    assert m_parsed["raw_text"].encode("utf-8") == fi_text.encode("utf-8")
    assert m_parsed["items"][0]["query"] == "täysmaito"

    fi_sv_kitchen = (
        "Tarvitsemme 30 litraa laktoositon maito, pähkinätön & gluteeniton, "
        "alle 2,10 € / l - keittiö äöåÄÖÅ (yhteyshenkilö Sari Nieminen)"
    )
    r_parse2 = client.post(
        "/v1/needs/parse",
        json={"text": fi_sv_kitchen, "customer": "school-hervanta"},
        headers=HERVANTA,
    )
    assert r_parse2.status_code == 200, r_parse2.text
    parsed2 = r_parse2.json()
    assert parsed2["raw_text"].encode("utf-8") == fi_sv_kitchen.encode("utf-8")
    assert parsed2["items"][0]["query"] == "milk"
    assert parsed2["items"][0]["constraints"]["lactose_free"] is True
    assert parsed2["items"][0]["constraints"]["gluten_free"] is True
    assert "nuts" in parsed2["items"][0]["constraints"]["exclude_allergens"]

    r_offers = client.post(
        "/v1/offers",
        json={
            "customer": "school-hervanta",
            "deliver_by": _delivery_iso(3),
            "raw_text": fi_sv_kitchen,
            "items": parsed["items"],
        },
        headers=HERVANTA,
    )
    assert r_offers.status_code == 200, r_offers.text
    offers_data = r_offers.json()
    assert offers_data["items"][0]["offers"][0]["product"]["name"] == P_FI_SV.name

    m_offers = mcp.call_ok(
        "get_offers",
        {
            "customer": "school-hervanta",
            "deliver_by": _delivery_iso(3),
            "raw_text": fi_sv_kitchen,
            "items": parsed["items"],
        },
        headers=HERVANTA,
    )
    assert m_offers["items"][0]["offers"][0]["product"]["name"] == P_FI_SV.name

    r_quote = client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "400300", "quantity": 40}], "delivery_date": _delivery_iso(3)},
        headers=HERVANTA,
    )
    assert r_quote.status_code == 200, r_quote.text
    q_rest = r_quote.json()
    assert q_rest["lines"][0]["name"].encode("utf-8") == P_FI_SV.name.encode("utf-8")

    jwks = client.get("/.well-known/jwks.json").json()
    verified_quote = quotes.verify(q_rest["jws"], jwks)
    assert verified_quote.lines[0].name == P_FI_SV.name
    for token_piece in ("äöåÄÖÅ", "€", "laktoositon täysmaito", "pähkinätön & gluteeniton"):
        assert token_piece in verified_quote.lines[0].name

    order_note = "Tarkistus Sari Nieminen: laktoositon täysmaito, pähkinätön & gluteeniton, 60,00 € (äöåÄÖÅ)"
    r_order = client.post(
        "/v1/orders",
        json={
            "quote_id": q_rest["quote_id"],
            "jws": q_rest["jws"],
            "ask_human": True,
            "note": order_note,
        },
        headers=HERVANTA,
    )
    assert r_order.status_code == 200, r_order.text
    ord_rest = r_order.json()
    assert ord_rest["status"] == "pending_approval"
    assert ord_rest["lines"][0]["name"] == P_FI_SV.name
    assert ord_rest["decision"]["reasons"][0]["text"].encode("utf-8") == order_note.encode("utf-8")

    approval_note = "Hyväksytty: laktoositon täysmaito, pähkinätön & gluteeniton, hinta 60,00 € ok - äöåÄÖÅ"
    r_dec = client.post(
        f"/v1/approvals/{ord_rest['order_id']}",
        json={"approve": True, "by": "Sari Nieminen", "note": approval_note},
        headers=ADMIN,
    )
    assert r_dec.status_code == 200, r_dec.text
    assert r_dec.json()["status"] == "confirmed"

    m_quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400300", "quantity": 20}], "delivery_date": _delivery_iso(3)},
        headers=HERVANTA,
    )
    assert quotes.verify(m_quote["jws"], jwks).lines[0].name == P_FI_SV.name

    m_ord = mcp.call_ok(
        "place_order",
        {
            "quote_id": m_quote["quote_id"],
            "jws": m_quote["jws"],
            "ask_human": True,
            "note": order_note,
        },
        headers=HERVANTA,
    )
    assert m_ord["status"] == "pending_approval"
    assert m_ord["decision"]["reasons"][0]["text"] == order_note

    m_dec = mcp.call_ok(
        "decide_approval",
        {
            "order_id": m_ord["order_id"],
            "approve": True,
            "by": "Sari Nieminen",
            "note": approval_note,
        },
        headers=ADMIN,
    )
    assert m_dec["status"] == "confirmed"

    trail_rest = client.get("/v1/audit", headers=HERVANTA).json()
    trail_mcp = mcp.call_ok("get_audit_trail", {}, headers=HERVANTA)
    assert trail_rest["verified"] == {"ok": True, "bad_seq": None}
    assert trail_mcp["verified"] == {"ok": True, "bad_seq": None}
    assert audit.verify() == (True, None)

    head = audit.verify_checkpoint(trail_rest["checkpoint"]["jws"], jwks)
    assert head["seq"] == trail_rest["checkpoint"]["seq"]
    assert head["hash"] == trail_rest["checkpoint"]["hash"]

    all_trail = client.get("/v1/audit", headers=ADMIN).json()
    prev_hash = "0" * 64
    for idx, ev in enumerate(all_trail["events"], start=1):
        assert ev["seq"] == idx
        assert ev["prev_hash"] == prev_hash
        ts_str = ev["ts"].replace("+00:00", "Z")
        canon_row = {
            "seq": ev["seq"],
            "ts": ts_str,
            "customer": ev["customer"],
            "actor": ev["actor"],
            "action": ev["action"],
            "subject": ev["subject"],
            "detail": ev["detail"],
            "why": ev["why"],
        }
        body = json.dumps(canon_row, sort_keys=True, separators=(",", ":"))
        expected_digest = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()
        assert ev["hash"] == expected_digest
        prev_hash = ev["hash"]

    hervanta_events = trail_rest["events"]
    parse_events = [e for e in hervanta_events if e["action"] == "parse.request"]
    assert any(e["detail"]["text"] == fi_text for e in parse_events)

    placed_events = [e for e in hervanta_events if e["action"] == "order.placed"]
    assert any(e["detail"]["lines"][0]["name"] == P_FI_SV.name for e in placed_events)

    approval_events = [e for e in hervanta_events if e["action"] == "approval.approved"]
    assert len(approval_events) == 2
    for ev in approval_events:
        assert ev["actor"] == "human:Sari Nieminen"
        assert ev["detail"]["note"] == approval_note
        for piece in ("Sari Nieminen", "äöåÄÖÅ", "€", "laktoositon täysmaito", "pähkinätön & gluteeniton"):
            assert piece in ev["why"]


@pytest.mark.parametrize(
    "endpoint,method,headers",
    [
        ("/v1/needs/parse", "POST", AURORA),
        ("/v1/offers", "POST", AURORA),
        ("/v1/quotes", "POST", AURORA),
        ("/v1/orders", "POST", AURORA),
        ("/v1/approvals/ord_nonexistent", "POST", ADMIN),
        ("/v1/mandate", "PUT", ADMIN),
        ("/v1/admin/simulate", "POST", ADMIN),
    ],
)
@pytest.mark.parametrize(
    "raw_body",
    [
        b"{not valid json",
        b"",
        b"[]",
        b'"just-a-string"',
        b"12345",
        b"null",
        b'{"broken": "\xff\xfe"}',
    ],
)
def test_malformed_raw_http_json_bodies_never_500(fuzz_env, endpoint, method, headers, raw_body):
    client, _, _, _ = fuzz_env
    req_headers = {"Content-Type": "application/json", **headers}
    r = client.request(method, endpoint, content=raw_body, headers=req_headers)
    assert r.status_code in ALLOWED_STATUSES, f"{method} {endpoint} -> {r.status_code}: {r.text}"
    assert r.status_code >= 400


def test_wrong_types_invalid_dates_and_boundary_values_rest_and_mcp(fuzz_env):
    client, mcp, _, _ = fuzz_env

    bad_offers_payloads = [
        {"customer": "hotel-aurora", "deliver_by": _delivery_iso(3), "items": "not-a-list"},
        {"customer": "hotel-aurora", "deliver_by": "not-a-date", "items": []},
        {"customer": "hotel-aurora", "deliver_by": "2026-02-30", "items": []},
        {
            "customer": "hotel-aurora",
            "deliver_by": _delivery_iso(3),
            "items": [{"query": "milk", "quantity": "abc", "unit": "l"}],
        },
        {
            "customer": "hotel-aurora",
            "deliver_by": _delivery_iso(3),
            "items": [{"query": "milk", "quantity": -1, "unit": "l"}],
        },
        {
            "customer": "hotel-aurora",
            "deliver_by": _delivery_iso(3),
            "items": [{"query": "milk", "quantity": 0, "unit": "l"}],
        },
        {"customer": "hotel-aurora", "deliver_by": _delivery_iso(3), "items": []},
    ]
    for body in bad_offers_payloads:
        r = client.post("/v1/offers", json=body, headers=AURORA)
        assert r.status_code in ALLOWED_STATUSES, r.text
        out = mcp.call_any("get_offers", body, headers=AURORA)
        assert "result" in out or "error" in out

    bad_quote_payloads = [
        {"offers": "not-a-list", "delivery_date": _delivery_iso(3)},
        {"offers": [], "delivery_date": _delivery_iso(3)},
        {"offers": [{"product_id": "400100", "quantity": "abc"}], "delivery_date": _delivery_iso(3)},
        {"offers": [{"product_id": "400100", "quantity": -1}], "delivery_date": _delivery_iso(3)},
        {"offers": [{"product_id": "400100", "quantity": 0}], "delivery_date": _delivery_iso(3)},
        {"offers": [{"product_id": "400100", "quantity": 5}], "delivery_date": "not-a-date"},
        {"offers": [{"product_id": "400100", "quantity": 5}], "delivery_date": "2026-13-40"},
        {"offers": [{"product_id": "400100", "quantity": 5}], "delivery_date": "2020-01-01"},
    ]
    for body in bad_quote_payloads:
        r = client.post("/v1/quotes", json=body, headers=AURORA)
        assert r.status_code in {409, 422}, f"{body} -> {r.status_code}: {r.text}"
        out = mcp.call_any("request_quote", body, headers=AURORA)
        if "result" in out:
            assert out["result"].get("isError") is True

    assert client.post(
        "/v1/quotes",
        json={"offers": [], "delivery_date": _delivery_iso(3)},
        headers=AURORA,
    ).status_code == 409
    assert client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "400100", "quantity": -1}], "delivery_date": _delivery_iso(3)},
        headers=AURORA,
    ).status_code == 422
    assert client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "400100", "quantity": 0}], "delivery_date": _delivery_iso(3)},
        headers=AURORA,
    ).status_code == 422

    bad_mandates = [
        {
            "customer": "hotel-aurora",
            "agent_id": "buyer-aurora",
            "per_order_eur": 0,
            "per_week_eur": 1000,
        },
        {
            "customer": "hotel-aurora",
            "agent_id": "buyer-aurora",
            "per_order_eur": -50,
            "per_week_eur": 1000,
        },
        {
            "customer": "hotel-aurora",
            "agent_id": "buyer-aurora",
            "per_order_eur": 2000,
            "per_week_eur": 500,
        },
        {
            "customer": "hotel-aurora",
            "agent_id": "buyer-aurora",
            "per_order_eur": "not-a-float",
            "per_week_eur": 1000,
        },
        {
            "customer": "hotel-aurora",
            "agent_id": "buyer-aurora",
            "per_order_eur": 500,
            "per_week_eur": 1000,
            "substitutions": "unknown-policy",
        },
    ]
    for m_body in bad_mandates:
        r = client.put("/v1/mandate", json=m_body, headers=ADMIN)
        assert r.status_code in {409, 422}, r.text

    for params in (
        {"limit": "abc"},
        {"limit": 0},
        {"limit": -10},
        {"max_unit_price": "not-a-number"},
        {"lactose_free": "not-a-bool"},
    ):
        r = client.get("/v1/products", params=params)
        assert r.status_code in ALLOWED_STATUSES, r.text

    for raw_mcp in (b"{bad json", b"", b"[]"):
        r_mcp = client.post("/mcp", content=raw_mcp, headers=MCP_HEADERS)
        assert r_mcp.status_code < 500, r_mcp.text

    r_unknown_method = mcp.rpc_raw({"jsonrpc": "2.0", "id": 999, "method": "nonexistent/method", "params": {}})
    assert r_unknown_method.status_code < 500

    out_unknown_tool = mcp.call_any("nonexistent_tool", {"x": 1})
    assert "error" in out_unknown_tool or out_unknown_tool["result"].get("isError") is True

    assert audit.verify() == (True, None)


def test_oversized_16kb_query_text_and_notes_roundtrip_without_500(fuzz_env):
    client, mcp, _, _ = fuzz_env

    chunk = "laktoositon täysmaito äöåÄÖÅ € pähkinätön & gluteeniton "
    big_16k = (chunk * ((16384 // len(chunk)) + 2))[:16384]
    assert len(big_16k) == 16384
    assert len(big_16k.encode("utf-8")) > 16384

    r_prod = client.get("/v1/products", params={"q": big_16k})
    assert r_prod.status_code in ALLOWED_STATUSES, r_prod.text
    m_prod = mcp.call_ok("search_products", {"q": big_16k})
    assert isinstance(m_prod, list)

    r_parse = client.post(
        "/v1/needs/parse",
        json={"text": big_16k, "customer": "hotel-aurora"},
        headers=AURORA,
    )
    assert r_parse.status_code == 200, r_parse.text
    assert r_parse.json()["raw_text"] == big_16k

    m_parse = mcp.call_ok(
        "parse_request",
        {"text": big_16k, "customer": "hotel-aurora"},
        headers=AURORA,
    )
    assert m_parse["raw_text"] == big_16k

    r_offers = client.post(
        "/v1/offers",
        json={
            "customer": "hotel-aurora",
            "deliver_by": _delivery_iso(3),
            "raw_text": big_16k,
            "items": [{"query": big_16k, "quantity": 10, "unit": "l"}],
        },
        headers=AURORA,
    )
    assert r_offers.status_code == 200, r_offers.text

    q = client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "400100", "quantity": 10}], "delivery_date": _delivery_iso(3)},
        headers=AURORA,
    ).json()

    r_order = client.post(
        "/v1/orders",
        json={
            "quote_id": q["quote_id"],
            "jws": q["jws"],
            "ask_human": True,
            "note": big_16k,
        },
        headers=AURORA,
    )
    assert r_order.status_code == 200, r_order.text
    ord_data = r_order.json()
    assert ord_data["status"] == "pending_approval"
    assert ord_data["decision"]["reasons"][0]["text"] == big_16k

    r_dec = client.post(
        f"/v1/approvals/{ord_data['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": big_16k},
        headers=ADMIN,
    )
    assert r_dec.status_code == 200, r_dec.text
    assert r_dec.json()["status"] == "confirmed"

    trail = client.get("/v1/audit", params={"order_id": ord_data["order_id"]}, headers=AURORA).json()
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    approval_ev = [e for e in trail["events"] if e["action"] == "approval.approved"]
    assert len(approval_ev) == 1
    assert approval_ev[0]["detail"]["note"] == big_16k

    m_offers = mcp.call_ok(
        "get_offers",
        {
            "customer": "hotel-aurora",
            "deliver_by": _delivery_iso(3),
            "raw_text": big_16k,
            "items": [{"query": big_16k, "quantity": 5, "unit": "l"}],
        },
        headers=AURORA,
    )
    assert len(m_offers["items"]) == 1

    m_quote = mcp.call_ok(
        "request_quote",
        {"offers": [{"product_id": "400100", "quantity": 5}], "delivery_date": _delivery_iso(3)},
        headers=AURORA,
    )
    m_ord = mcp.call_ok(
        "place_order",
        {
            "quote_id": m_quote["quote_id"],
            "jws": m_quote["jws"],
            "ask_human": True,
            "note": big_16k,
        },
        headers=AURORA,
    )
    assert m_ord["status"] == "pending_approval"
    assert m_ord["decision"]["reasons"][0]["text"] == big_16k

    m_dec = mcp.call_ok(
        "decide_approval",
        {
            "order_id": m_ord["order_id"],
            "approve": True,
            "by": "Laura Virtanen",
            "note": big_16k,
        },
        headers=ADMIN,
    )
    assert m_dec["status"] == "confirmed"
    assert audit.verify() == (True, None)
    assert mandates.get("hotel-aurora") is not None


def test_sqlite_schema_exfiltration_mandate_sqli_and_jws_sqli_resilience(fuzz_env):
    client, mcp, db_path, _ = fuzz_env
    seeded, base_counts, _ = _seed_two_tenants(client, db_path)
    aurora = seeded["aurora"]
    tays = seeded["tays"]

    exfil_payloads = [
        "' UNION SELECT sql,sql,sql FROM sqlite_master --",
        "'; ATTACH DATABASE ':memory:' AS pwn; --",
        "'; PRAGMA writable_schema=ON; --",
        "hotel-aurora\x00' OR '1'='1",
    ]

    for payload in exfil_payloads:
        r_ord = client.get("/v1/orders", params={"customer": payload, "status": payload}, headers=ADMIN)
        assert r_ord.status_code == 200
        assert r_ord.json() == []
        assert "CREATE TABLE" not in r_ord.text.upper()

        m_ord = mcp.call_ok("list_orders", {"customer": payload, "status": payload}, headers=ADMIN)
        assert m_ord == []

        r_bad_jws = client.post(
            "/v1/orders",
            json={
                "quote_id": aurora["unused_quote"]["quote_id"],
                "jws": payload,
                "ask_human": False,
                "note": payload,
            },
            headers=AURORA,
        )
        assert r_bad_jws.status_code == 409
        assert r_bad_jws.json()["code"] == "BAD_SIGNATURE"
        _assert_no_tays_leak(r_bad_jws.json(), tays)

        m_bad_jws = mcp.call_err(
            "place_order",
            {
                "quote_id": aurora["unused_quote"]["quote_id"],
                "jws": payload,
                "ask_human": False,
                "note": payload,
            },
            headers=AURORA,
        )
        assert m_bad_jws["code"] == "BAD_SIGNATURE"

    con = sqlite3.connect(db_path)
    try:
        used_val = con.execute(
            "SELECT used_at FROM quotes WHERE quote_id=?",
            (aurora["unused_quote"]["quote_id"],),
        ).fetchone()[0]
        assert used_val is None
    finally:
        con.close()

    sqli_mandate = {
        "customer": "hotel-aurora'; DROP TABLE mandates; --",
        "agent_id": "buyer'; DROP TABLE orders; --",
        "per_order_eur": 500.0,
        "per_week_eur": 2000.0,
        "exclude_allergens": ["' OR '1'='1"],
        "allowed_categories": ["Dairy'; DROP TABLE quotes; --"],
        "substitutions": "ask",
        "preferred_brands": ["' UNION SELECT * FROM mandates --"],
        "approvers": ["Sari Nieminen'; DROP TABLE audit; --"],
    }
    r_put = client.put("/v1/mandate", json=sqli_mandate, headers=ADMIN)
    assert r_put.status_code == 200, r_put.text
    assert r_put.json()["customer"] == sqli_mandate["customer"]

    counts = _table_counts(db_path)
    assert counts["orders"] == base_counts["orders"] == 4
    assert counts["quotes"] == base_counts["quotes"] == 6
    assert counts["mandates"] == base_counts["mandates"] + 1 == 4
    assert counts["audit"] == base_counts["audit"] + (len(exfil_payloads) * 2) + 1
    assert mandates.get("hotel-aurora").per_order_eur == 2500.0
    assert mandates.get("hospital-tays").per_order_eur == 1500.0
    assert audit.verify() == (True, None)
