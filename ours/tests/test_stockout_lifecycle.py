import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet

from aimo import allocate, audit, catalog, keys, offers, orders, substitute, valio
from aimo.app import app
from aimo.models import Constraint, Product, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS_HEADERS = {"X-Agent-Key": "demo-hospital-tays"}
HERVANTA_HEADERS = {"X-Agent-Key": "demo-school-hervanta"}
ADMIN_HEADERS = {"X-Admin-Key": "demo-admin"}

STOP_WORDS = {
    "valio",
    "lactose",
    "gluten",
    "free",
    "reserve",
    "barista",
    "nutty",
    "roast",
    "dairy",
    "wheat",
    "organic",
    "pure",
    "classic",
    "almond",
    "and",
    "the",
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
                "clientInfo": {"name": "stockout-lifecycle-test", "version": "1.0"},
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

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        assert "result" in out, out
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        return json.loads(text.split(prefix, 1)[-1])


def _make_product(
    pid: str,
    name: str,
    brand: str = "Valio",
    category: str = "10400",
    sales_unit: str = "LTR",
    size_l: float = 1.0,
    allergens: list[str] | None = None,
    labels: list[str] | None = None,
    origin: str = "FI",
) -> Product:
    return Product(
        product_id=pid,
        name=name,
        brand=brand,
        vendor=f"{brand} Oy",
        category=category,
        sales_unit=sales_unit,
        unit_size=UnitSize(value=size_l, unit="l"),
        allergens=allergens if allergens is not None else ["milk"],
        labels=labels if labels is not None else ["free_from_lactose", "finnish"],
        origin=origin,
        temperature=6,
    )


def _price_row(pid: str, net: float, gross: float | None = None) -> dict:
    return {
        "productId": pid,
        "currency": "EUR",
        "catalogPrice": {"value": net},
        "priceWithTax": {"value": gross if gross is not None else round(net * 1.14, 2)},
    }


def _avail_row(
    pid: str,
    day: date,
    amount: float = 500.0,
    closing: str = "2026-11-12T15:00:00+0200",
    sales_block: bool = False,
) -> dict:
    return {
        "productId": pid,
        "closingTime": closing,
        "salesBlock": sales_block,
        "scheduleLines": [
            {
                "salesBlock": sales_block,
                "availableAmount": amount,
                "availableDate": day.isoformat(),
                "salesUnit": "LTR",
            }
        ],
    }


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "stockout_lifecycle.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    for var in (
        "VALIO_API_BASE",
        "VALIO_API_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_ADMIN_KEY",
        "AIMO_AGENT_KEY",
        "AIMO_STDIO_ADMIN_KEY",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(valio, "_client", None)
    allocate._data.cache_clear()
    catalog.reset()
    offers._overrides.clear()
    offers.clear_simulation()
    keys._cache.clear()
    yield
    offers._overrides.clear()
    offers.clear_simulation()
    catalog.reset()
    allocate._data.cache_clear()
    keys._cache.clear()


@pytest.fixture
def future_day() -> date:
    return datetime.now(offers.HEL).date() + timedelta(days=5)


def _wire_synthetic_catalog(
    monkeypatch,
    products: list[Product],
    prices: dict[str, dict],
    avail: dict[str, dict],
    rel: dict[str, float] | None = None,
    co2: dict[str, float] | None = None,
):
    by_id = {p.product_id: p for p in products}
    rel_map = rel or {p.product_id: 0.98 for p in products}
    co2_map = co2 or {p.product_id: 1.05 for p in products}

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        c = constraints or Constraint()
        raw_tokens = [
            tok
            for tok in q.lower().replace("-", " ").split()
            if tok.isalpha() and len(tok) >= 3
        ]
        core_tokens = [tok for tok in raw_tokens if tok not in STOP_WORDS] or raw_tokens
        out: list[Product] = []
        for p in products:
            pname = p.name.lower()
            if core_tokens and not all(tok in pname for tok in core_tokens):
                continue
            if (c.exclude_allergens or c.lactose_free or c.gluten_free) and not substitute.allergen_safe(
                p, c, c.exclude_allergens
            ):
                continue
            if c.brands and not any(b.lower() in (p.brand or "").lower() for b in c.brands):
                continue
            if c.labels:
                want = {x.lower() for x in c.labels}
                if not want <= {x.lower() for x in p.labels}:
                    continue
            if c.origin and (p.origin or "").lower() != c.origin.strip().lower()[:2]:
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return out

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: rel_map.get(str(pid)))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: co2_map.get(str(pid)))
    monkeypatch.setattr(valio, "price", lambda cust, day, ids: {i: prices[i] for i in ids if i in prices})
    monkeypatch.setattr(valio, "availability", lambda cust, day, ids: {i: avail[i] for i in ids if i in avail})


def _assert_hash_chain_and_checkpoint(client: TestClient, trail: dict, full_chain: bool = False):
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    evs = trail["events"]
    assert len(evs) > 0
    if full_chain:
        seqs = [e["seq"] for e in evs]
        assert seqs == list(range(1, len(evs) + 1))
        assert evs[0]["prev_hash"] == audit.GENESIS
        for idx in range(1, len(evs)):
            assert evs[idx]["prev_hash"] == evs[idx - 1]["hash"]
    jwks_resp = client.get("/.well-known/jwks.json")
    assert jwks_resp.status_code == 200
    jwks_data = jwks_resp.json()
    cp = trail["checkpoint"]
    head = audit.verify_checkpoint(cp["jws"], jwks_data)
    assert head["seq"] == cp["seq"]
    assert head["hash"] == cp["hash"]
    token_obj = jws.deserialize_compact(cp["jws"], KeySet.import_key_set(jwks_data), algorithms=["EdDSA"])
    payload = json.loads(token_obj.payload)
    assert payload["seq"] == cp["seq"]
    assert payload["hash"] == cp["hash"]
    if full_chain:
        assert cp["seq"] == evs[-1]["seq"]
        assert cp["hash"] == evs[-1]["hash"]


def test_aurora_happy_path_rest_and_mcp_stockout_recovery_and_audit(monkeypatch, future_day):
    p_primary = _make_product("410101", "Valio Lactose Free Milk 1L")
    p_backup = _make_product("410202", "Valio Lactose Free Milk Reserve 1L")
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_backup],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.45),
        },
        {
            "410101": _avail_row("410101", future_day, 600.0),
            "410202": _avail_row("410202", future_day, 600.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        parse_resp = client.post(
            "/v1/needs/parse",
            json={
                "text": f"I need 200 litres of lactose-free milk by {future_day.isoformat()}",
                "customer": "hotel-aurora",
            },
            headers=AURORA_HEADERS,
        )
        assert parse_resp.status_code == 200
        parsed = parse_resp.json()
        assert parsed["customer"] == "hotel-aurora"
        assert parsed["deliver_by"] == future_day.isoformat()
        assert len(parsed["items"]) == 1
        assert parsed["items"][0]["query"] == "milk"
        assert parsed["items"][0]["quantity"] == 200.0
        assert parsed["items"][0]["constraints"]["lactose_free"] is True
        assert "200 L milk" in parsed["summary"]

        need_payload = {
            "customer": parsed["customer"],
            "deliver_by": parsed["deliver_by"],
            "items": parsed["items"],
            "raw_text": parsed["raw_text"],
        }
        offers_resp = client.post("/v1/offers", json=need_payload, headers=AURORA_HEADERS)
        assert offers_resp.status_code == 200
        group = offers_resp.json()["items"][0]
        assert group["shortfall"] == 0.0
        assert [(o["product_id"], o["units_needed"], o["total_net"]) for o in group["offers"]] == [
            ("410101", 200.0, 240.0)
        ]
        assert [a["product_id"] for a in group["alternatives"]] == ["410202"]

        quote_resp = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "410101", "quantity": 200}], "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        )
        assert quote_resp.status_code == 200
        quote_1 = quote_resp.json()
        assert quote_1["quote_id"].startswith("q_")
        assert quote_1["total_net"] == 240.0
        assert quote_1["jws"].count(".") == 2

        order_resp = client.post(
            "/v1/orders",
            json={"quote_id": quote_1["quote_id"], "jws": quote_1["jws"]},
            headers=AURORA_HEADERS,
        )
        assert order_resp.status_code == 200
        order_rest = order_resp.json()
        oid_rest = order_rest["order_id"]
        assert order_rest["status"] == "confirmed"
        assert order_rest["decision"]["outcome"] == "auto"
        assert any(r["code"] == "WITHIN_MANDATE" for r in order_rest["decision"]["reasons"])

        conf_rest_before = orders.confirmation(oid_rest)
        assert conf_rest_before is not None
        assert conf_rest_before["confirmation_id"].startswith("MOCK-")
        assert conf_rest_before["mock"] is True
        assert conf_rest_before["delivery_date"] == future_day.isoformat()

        mcp_parsed = mcp.call_ok(
            "parse_request",
            {"text": f"I need 100 litres of lactose-free milk by {future_day.isoformat()}", "customer": "hotel-aurora"},
            headers=AURORA_HEADERS,
        )
        mcp_offers = mcp.call_ok(
            "get_offers",
            {
                "customer": mcp_parsed["customer"],
                "deliver_by": mcp_parsed["deliver_by"],
                "items": mcp_parsed["items"],
                "raw_text": mcp_parsed["raw_text"],
            },
            headers=AURORA_HEADERS,
        )
        assert mcp_offers["items"][0]["offers"][0]["product_id"] == "410101"

        quote_2 = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "410101", "quantity": 100}], "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        )
        order_mcp_approve = mcp.call_ok(
            "place_order",
            {"quote_id": quote_2["quote_id"], "jws": quote_2["jws"]},
            headers=AURORA_HEADERS,
        )
        oid_mcp_approve = order_mcp_approve["order_id"]
        assert order_mcp_approve["status"] == "confirmed"
        conf_mcp_before = orders.confirmation(oid_mcp_approve)
        assert conf_mcp_before is not None and conf_mcp_before["confirmation_id"].startswith("MOCK-")

        quote_3 = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "410101", "quantity": 80}], "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        )
        order_mcp_reject = mcp.call_ok(
            "place_order",
            {"quote_id": quote_3["quote_id"], "jws": quote_3["jws"]},
            headers=AURORA_HEADERS,
        )
        oid_mcp_reject = order_mcp_reject["order_id"]
        assert order_mcp_reject["status"] == "confirmed"

        sim_resp = client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 0},
            headers=ADMIN_HEADERS,
        )
        assert sim_resp.status_code == 200
        assert sim_resp.json() == {"ok": True}

        expected_refills = [
            (oid_rest, 200.0, 290.0),
            (oid_mcp_approve, 100.0, 145.0),
            (oid_mcp_reject, 80.0, 116.0),
        ]
        for oid, qty, total_net in expected_refills:
            got_rest = client.get(f"/v1/orders/{oid}", headers=AURORA_HEADERS)
            assert got_rest.status_code == 200
            got_mcp = mcp.call_ok("get_order", {"order_id": oid}, headers=AURORA_HEADERS)
            for payload in (got_rest.json(), got_mcp):
                assert payload["status"] == "pending_approval"
                assert payload["decision"]["outcome"] == "needs_approval"
                assert [r["code"] for r in payload["decision"]["reasons"]] == ["STOCKOUT"]
                assert "Stock dropped after the order:" in payload["decision"]["reasons"][0]["text"]
                assert [(l["product_id"], l["quantity"], l["unit_price_net"], l["line_total_net"]) for l in payload["lines"]] == [
                    ("410202", qty, 1.45, total_net)
                ]
                assert payload["total_net"] == total_net
                assert payload["substitutions"] == [
                    {
                        "original_id": "410101",
                        "substitute_id": "410202",
                        "reason": "Stock dropped, refilled from the next offer.",
                    }
                ]

        mcp_pending = mcp.call_ok("list_approvals", {}, headers=AURORA_HEADERS)
        rest_pending = client.get("/v1/approvals", headers=ADMIN_HEADERS).json()
        assert {o["order_id"] for o in mcp_pending} == {oid_rest, oid_mcp_approve, oid_mcp_reject}
        assert {o["order_id"] for o in rest_pending} == {oid_rest, oid_mcp_approve, oid_mcp_reject}
        for pending_item in mcp_pending:
            assert pending_item["status"] == "pending_approval"
            assert pending_item["decision"]["outcome"] == "needs_approval"
            assert pending_item["decision"]["reasons"][0]["code"] == "STOCKOUT"
            assert pending_item["substitutions"][0]["substitute_id"] == "410202"

        offers_during_sim = client.post("/v1/offers", json=need_payload, headers=AURORA_HEADERS).json()["items"][0]
        assert [(o["product_id"], o["units_needed"]) for o in offers_during_sim["offers"]] == [("410202", 200.0)]

        rest_approve = client.post(
            f"/v1/approvals/{oid_rest}",
            json={"approve": True, "by": "Laura Virtanen", "note": "Reserve milk approved via REST"},
            headers=ADMIN_HEADERS,
        )
        assert rest_approve.status_code == 200
        assert rest_approve.json()["status"] == "confirmed"
        conf_rest_after = orders.confirmation(oid_rest)
        assert conf_rest_after is not None
        assert conf_rest_after["confirmation_id"].startswith("MOCK-")
        assert conf_rest_after["confirmation_id"] != conf_rest_before["confirmation_id"]

        mcp_approved = mcp.call_ok(
            "decide_approval",
            {
                "order_id": oid_mcp_approve,
                "approve": True,
                "by": "Laura Virtanen",
                "note": "Reserve milk approved via MCP",
            },
            headers=ADMIN_HEADERS,
        )
        assert mcp_approved["status"] == "confirmed"
        conf_mcp_after = orders.confirmation(oid_mcp_approve)
        assert conf_mcp_after is not None
        assert conf_mcp_after["confirmation_id"].startswith("MOCK-")
        assert conf_mcp_after["confirmation_id"] != conf_mcp_before["confirmation_id"]

        mcp_rejected = mcp.call_ok(
            "decide_approval",
            {
                "order_id": oid_mcp_reject,
                "approve": False,
                "by": "Laura Virtanen",
                "note": "Extra batch cancelled via MCP",
            },
            headers=ADMIN_HEADERS,
        )
        assert mcp_rejected["status"] == "rejected"
        assert client.get(f"/v1/orders/{oid_mcp_reject}", headers=AURORA_HEADERS).json()["status"] == "rejected"

        assert mcp.call_ok("list_approvals", {}, headers=AURORA_HEADERS) == []
        assert client.get("/v1/approvals", headers=ADMIN_HEADERS).json() == []

        clear_resp = client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS)
        assert clear_resp.status_code == 200
        assert clear_resp.json() == {"ok": True}

        restored_rest = client.post("/v1/offers", json=need_payload, headers=AURORA_HEADERS).json()["items"][0]
        assert [(o["product_id"], o["units_needed"], o["total_net"]) for o in restored_rest["offers"]] == [
            ("410101", 200.0, 240.0)
        ]
        restored_mcp = mcp.call_ok("get_offers", need_payload, headers=AURORA_HEADERS)["items"][0]
        assert [(o["product_id"], o["units_needed"], o["total_net"]) for o in restored_mcp["offers"]] == [
            ("410101", 200.0, 240.0)
        ]

        order_trail_rest = client.get(f"/v1/audit?order_id={oid_rest}", headers=AURORA_HEADERS).json()
        order_trail_mcp = mcp.call_ok("get_audit_trail", {"order_id": oid_rest}, headers=AURORA_HEADERS)
        for trail in (order_trail_rest, order_trail_mcp):
            _assert_hash_chain_and_checkpoint(client, trail, full_chain=False)
            actions = [e["action"] for e in trail["events"]]
            assert "quote.issued" in actions
            assert "quote.redeemed" in actions
            assert "order.stockout" in actions
            assert "approval.approved" in actions
            assert actions.count("order.confirmed") == 2
            confirmed_ids = [e["detail"]["confirmation_id"] for e in trail["events"] if e["action"] == "order.confirmed"]
            assert confirmed_ids == [conf_rest_before["confirmation_id"], conf_rest_after["confirmation_id"]]
            assert "Supplier stock-out:" in trail["text"]
            assert "Laura Virtanen approved an order" in trail["text"]
            assert "Reserve milk approved via REST" in trail["text"]

        rejected_trail = client.get(f"/v1/audit?order_id={oid_mcp_reject}", headers=AURORA_HEADERS).json()
        _assert_hash_chain_and_checkpoint(client, rejected_trail, full_chain=False)
        assert "Supplier stock-out:" in rejected_trail["text"]
        assert "Laura Virtanen rejected an order" in rejected_trail["text"]
        assert "Extra batch cancelled via MCP" in rejected_trail["text"]

        full_audit = client.get("/v1/audit", headers=ADMIN_HEADERS).json()
        _assert_hash_chain_and_checkpoint(client, full_audit, full_chain=True)
        assert "Supplier stock-out:" in full_audit["text"]
        assert "Laura Virtanen approved an order" in full_audit["text"]
        assert "Laura Virtanen rejected an order" in full_audit["text"]


def test_hospital_tays_strict_allergen_safety_during_stockout(monkeypatch, future_day):
    p_oat_primary = _make_product(
        "510100",
        "Valio Gluten Free Oat Drink 1L",
        category="10450",
        allergens=["oats"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_oat_nuts = _make_product(
        "510101",
        "Valio Gluten Free Nutty Oat Drink 1L",
        category="10450",
        allergens=["oats", "tree nuts"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_oat_peanut_trace = _make_product(
        "510102",
        "Valio Gluten Free Roast Oat Drink 1L",
        category="10450",
        allergens=["oats", "may_contain:peanuts"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_oat_milk = _make_product(
        "510103",
        "Valio Gluten Free Dairy Oat Drink 1L",
        category="10450",
        allergens=["oats", "milk"],
        labels=["free_from_gluten"],
    )
    p_oat_wheat = _make_product(
        "510104",
        "Valio Classic Wheat Oat Drink 1L",
        category="10450",
        allergens=["oats", "wheat", "gluten"],
        labels=["vegan", "free_from_lactose"],
    )
    p_oat_clean = _make_product(
        "510200",
        "Valio Gluten Free Oat Drink Reserve 1L",
        category="10450",
        allergens=["oats"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_juice_primary = _make_product(
        "610100",
        "Valio Pure Apple Juice 1L",
        category="10800",
        allergens=["sulphites"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_juice_safe = _make_product(
        "610200",
        "Valio Pure Organic Apple Juice 1L",
        category="10800",
        allergens=["sulphites"],
        labels=["vegan", "organic", "free_from_lactose", "free_from_gluten"],
    )
    p_juice_nut = _make_product(
        "610999",
        "Valio Pure Almond Apple Juice 1L",
        category="10800",
        allergens=["tree nuts"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )

    _wire_synthetic_catalog(
        monkeypatch,
        [
            p_oat_primary,
            p_oat_nuts,
            p_oat_peanut_trace,
            p_oat_milk,
            p_oat_wheat,
            p_oat_clean,
            p_juice_primary,
            p_juice_safe,
            p_juice_nut,
        ],
        {
            "510100": _price_row("510100", 1.20),
            "510101": _price_row("510101", 0.85),
            "510102": _price_row("510102", 0.90),
            "510103": _price_row("510103", 0.95),
            "510104": _price_row("510104", 1.00),
            "510200": _price_row("510200", 1.55),
            "610100": _price_row("610100", 1.15),
            "610200": _price_row("610200", 1.35),
            "610999": _price_row("610999", 1.95),
        },
        {
            "510100": _avail_row("510100", future_day, 300.0),
            "510101": _avail_row("510101", future_day, 300.0),
            "510102": _avail_row("510102", future_day, 300.0),
            "510103": _avail_row("510103", future_day, 300.0),
            "510104": _avail_row("510104", future_day, 300.0),
            "510200": _avail_row("510200", future_day, 300.0),
            "610100": _avail_row("610100", future_day, 300.0),
            "610200": _avail_row("610200", future_day, 300.0),
            "610999": _avail_row("610999", future_day, 300.0),
        },
    )

    unsafe_ids = {"510101", "510102", "510103", "510104"}

    with TestClient(app) as client:
        mcp = McpSession(client)

        parsed = client.post(
            "/v1/needs/parse",
            json={
                "text": f"I need 60 litres of gluten-free dairy-free oat drink by {future_day.isoformat()}",
                "customer": "hospital-tays",
            },
            headers=TAYS_HEADERS,
        ).json()
        assert parsed["items"][0]["constraints"]["gluten_free"] is True
        assert "milk" in parsed["items"][0]["constraints"]["exclude_allergens"]

        need_payload = {
            "customer": parsed["customer"],
            "deliver_by": parsed["deliver_by"],
            "items": parsed["items"],
            "raw_text": parsed["raw_text"],
        }

        pre_group = client.post("/v1/offers", json=need_payload, headers=TAYS_HEADERS).json()["items"][0]
        assert [o["product_id"] for o in pre_group["offers"]] == ["510100"]
        assert [a["product_id"] for a in pre_group["alternatives"]] == ["510200"]

        sim_primary = client.post(
            "/v1/admin/simulate",
            json={"product_id": "510100", "available_amount": 0},
            headers=ADMIN_HEADERS,
        )
        assert sim_primary.status_code == 200

        rest_group = client.post("/v1/offers", json=need_payload, headers=TAYS_HEADERS).json()["items"][0]
        mcp_group = mcp.call_ok("get_offers", need_payload, headers=TAYS_HEADERS)["items"][0]
        for grp in (rest_group, mcp_group):
            assert grp["shortfall"] == 0.0
            assert [(o["product_id"], o["units_needed"], o["total_net"]) for o in grp["offers"]] == [
                ("510200", 60.0, 93.0)
            ]
            assert unsafe_ids.isdisjoint({o["product_id"] for o in grp["offers"]})
            assert unsafe_ids.isdisjoint({a["product_id"] for a in grp["alternatives"]})

        sub_quote = mcp.call_ok(
            "request_quote",
            {
                "offers": [{"product_id": "510200", "quantity": 60, "substitute_for": "510100"}],
                "delivery_date": future_day.isoformat(),
            },
            headers=TAYS_HEADERS,
        )
        sub_order = mcp.call_ok(
            "place_order",
            {"quote_id": sub_quote["quote_id"], "jws": sub_quote["jws"]},
            headers=TAYS_HEADERS,
        )
        assert sub_order["status"] == "pending_approval"
        assert sub_order["decision"]["outcome"] == "needs_approval"
        assert any(r["code"] == "SUBSTITUTION_NEEDS_APPROVAL" for r in sub_order["decision"]["reasons"])

        wrong_approver = client.post(
            f"/v1/approvals/{sub_order['order_id']}",
            json={"approve": True, "by": "Laura Virtanen", "note": "wrong customer approver"},
            headers=ADMIN_HEADERS,
        )
        assert wrong_approver.status_code == 403
        assert wrong_approver.json()["code"] == "NOT_APPROVER"

        helena_ok = client.post(
            f"/v1/approvals/{sub_order['order_id']}",
            json={"approve": True, "by": "Helena Korhonen", "note": "Clean oat substitute verified"},
            headers=ADMIN_HEADERS,
        )
        assert helena_ok.status_code == 200
        assert helena_ok.json()["status"] == "confirmed"

        for bad_pid in ("510101", "510102"):
            bad_quote = client.post(
                "/v1/quotes",
                json={"offers": [{"product_id": bad_pid, "quantity": 10}], "delivery_date": future_day.isoformat()},
                headers=TAYS_HEADERS,
            ).json()
            bad_order = client.post(
                "/v1/orders",
                json={"quote_id": bad_quote["quote_id"], "jws": bad_quote["jws"]},
                headers=TAYS_HEADERS,
            ).json()
            assert bad_order["status"] == "blocked"
            assert bad_order["decision"]["outcome"] == "blocked"
            assert any(r["code"] == "ALLERGEN_CONFLICT" for r in bad_order["decision"]["reasons"])

        sim_clean_zero = client.post(
            "/v1/admin/simulate",
            json={"product_id": "510200", "available_amount": 0},
            headers=ADMIN_HEADERS,
        )
        assert sim_clean_zero.status_code == 200

        none_safe_rest = client.post("/v1/offers", json=need_payload, headers=TAYS_HEADERS).json()["items"][0]
        none_safe_mcp = mcp.call_ok("get_offers", need_payload, headers=TAYS_HEADERS)["items"][0]
        for grp in (none_safe_rest, none_safe_mcp):
            assert grp["offers"] == []
            assert grp["shortfall"] == 60.0
            assert grp["alternatives"] == []

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200

        juice_quote = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "610100", "quantity": 40}], "delivery_date": future_day.isoformat()},
            headers=TAYS_HEADERS,
        ).json()
        juice_order = client.post(
            "/v1/orders",
            json={"quote_id": juice_quote["quote_id"], "jws": juice_quote["jws"]},
            headers=TAYS_HEADERS,
        ).json()
        assert juice_order["status"] == "confirmed"

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "610100", "available_amount": 0},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        reopened_juice = client.get(f"/v1/orders/{juice_order['order_id']}", headers=TAYS_HEADERS).json()
        assert reopened_juice["status"] == "pending_approval"
        assert [r["code"] for r in reopened_juice["decision"]["reasons"]] == ["STOCKOUT"]
        assert [(l["product_id"], l["quantity"]) for l in reopened_juice["lines"]] == [("610200", 40.0)]
        assert [s["substitute_id"] for s in reopened_juice["substitutions"]] == ["610200"]

        antti_ok = mcp.call_ok(
            "decide_approval",
            {
                "order_id": juice_order["order_id"],
                "approve": True,
                "by": "Antti Lahtinen",
                "note": "Safe organic apple juice approved",
            },
            headers=ADMIN_HEADERS,
        )
        assert antti_ok["status"] == "confirmed"

        audit_tays = client.get(f"/v1/audit?order_id={juice_order['order_id']}", headers=TAYS_HEADERS).json()
        _assert_hash_chain_and_checkpoint(client, audit_tays, full_chain=False)
        assert "Supplier stock-out:" in audit_tays["text"]
        assert "Antti Lahtinen approved an order" in audit_tays["text"]


def test_partial_stock_split_and_shortfall_simulation_lifecycle(monkeypatch, future_day):
    p_primary = _make_product("410101", "Valio Lactose Free Milk 1L")
    p_backup = _make_product("410202", "Valio Lactose Free Milk Reserve 1L")
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_backup],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.50),
        },
        {
            "410101": _avail_row("410101", future_day, 500.0),
            "410202": _avail_row("410202", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        q_split = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "410101", "quantity": 80}], "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        ).json()
        o_split = client.post(
            "/v1/orders",
            json={"quote_id": q_split["quote_id"], "jws": q_split["jws"]},
            headers=AURORA_HEADERS,
        ).json()
        oid = o_split["order_id"]
        assert o_split["status"] == "confirmed"
        assert o_split["total_net"] == 96.0
        conf_initial = orders.confirmation(oid)
        assert conf_initial is not None

        sim_partial = client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 30, "available_date": future_day.isoformat()},
            headers=ADMIN_HEADERS,
        )
        assert sim_partial.status_code == 200

        got = client.get(f"/v1/orders/{oid}", headers=AURORA_HEADERS).json()
        assert got["status"] == "pending_approval"
        assert got["decision"]["outcome"] == "needs_approval"
        assert [r["code"] for r in got["decision"]["reasons"]] == ["STOCKOUT"]
        assert "80 -> 30 units, stock dropped" in got["decision"]["reasons"][0]["text"]
        assert "+50 from 410202" in got["decision"]["reasons"][0]["text"]
        assert [(l["product_id"], l["quantity"], l["line_total_net"]) for l in got["lines"]] == [
            ("410101", 30.0, 36.0),
            ("410202", 50.0, 75.0),
        ]
        assert got["total_net"] == 111.0
        assert got["substitutions"] == [
            {
                "original_id": "410101",
                "substitute_id": "410202",
                "reason": "Stock dropped, refilled from the next offer.",
            }
        ]

        need_80 = {
            "customer": "hotel-aurora",
            "deliver_by": future_day.isoformat(),
            "items": [{"query": "milk", "quantity": 80, "unit": "L", "constraints": {"lactose_free": True}}],
        }
        split_offers = mcp.call_ok("get_offers", need_80, headers=AURORA_HEADERS)["items"][0]
        assert split_offers["shortfall"] == 0.0
        assert [(o["product_id"], o["units_needed"]) for o in split_offers["offers"]] == [
            ("410101", 30.0),
            ("410202", 50.0),
        ]

        decided = mcp.call_ok(
            "decide_approval",
            {"order_id": oid, "approve": True, "by": "Mikko Heikkinen", "note": "Split across primary and reserve ok"},
            headers=ADMIN_HEADERS,
        )
        assert decided["status"] == "confirmed"
        conf_after = orders.confirmation(oid)
        assert conf_after is not None
        assert conf_after["confirmation_id"] != conf_initial["confirmation_id"]

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200
        restored = client.post("/v1/offers", json=need_80, headers=AURORA_HEADERS).json()["items"][0]
        assert restored["shortfall"] == 0.0
        assert [(o["product_id"], o["units_needed"]) for o in restored["offers"]] == [("410101", 80.0)]

        q_short = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "410101", "quantity": 80}], "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        ).json()
        o_short = client.post(
            "/v1/orders",
            json={"quote_id": q_short["quote_id"], "jws": q_short["jws"]},
            headers=AURORA_HEADERS,
        ).json()
        oid_short = o_short["order_id"]
        assert o_short["status"] == "confirmed"

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410202", "available_amount": 20, "available_date": future_day.isoformat()},
            headers=ADMIN_HEADERS,
        ).status_code == 200
        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 30, "available_date": future_day.isoformat()},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        reopened_short = client.get(f"/v1/orders/{oid_short}", headers=AURORA_HEADERS).json()
        assert reopened_short["status"] == "pending_approval"
        assert [(l["product_id"], l["quantity"]) for l in reopened_short["lines"]] == [
            ("410101", 30.0),
            ("410202", 20.0),
        ]
        assert "Shortfall 30" in reopened_short["decision"]["reasons"][0]["text"]

        need_plain_80 = {
            "customer": "hotel-aurora",
            "deliver_by": future_day.isoformat(),
            "items": [{"query": "milk", "quantity": 80, "unit": "L"}],
        }
        short_offers = client.post("/v1/offers", json=need_plain_80, headers=AURORA_HEADERS).json()["items"][0]
        assert short_offers["shortfall"] == 30.0
        assert any(
            opt["kind"] == "partial" and opt["covered"] == 50.0 and opt["shortfall"] == 30.0
            for opt in short_offers["options"]
        )

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200


def test_late_delivery_simulation_past_deliver_by_and_recovery(monkeypatch, future_day):
    p_primary = _make_product("410101", "Valio Lactose Free Milk 1L")
    p_backup = _make_product("410202", "Valio Lactose Free Milk Reserve 1L")
    late_day = future_day + timedelta(days=3)
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_backup],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.45),
        },
        {
            "410101": _avail_row("410101", future_day, 500.0),
            "410202": _avail_row("410202", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        q_school = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "410101", "quantity": 50}], "delivery_date": future_day.isoformat()},
            headers=HERVANTA_HEADERS,
        ).json()
        o_school = client.post(
            "/v1/orders",
            json={"quote_id": q_school["quote_id"], "jws": q_school["jws"]},
            headers=HERVANTA_HEADERS,
        ).json()
        oid = o_school["order_id"]
        assert o_school["status"] == "confirmed"
        conf_before = orders.confirmation(oid)
        assert conf_before is not None

        sim_late = client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 500, "available_date": late_day.isoformat()},
            headers=ADMIN_HEADERS,
        )
        assert sim_late.status_code == 200

        got = client.get(f"/v1/orders/{oid}", headers=HERVANTA_HEADERS).json()
        assert got["status"] == "pending_approval"
        assert got["decision"]["outcome"] == "needs_approval"
        assert [r["code"] for r in got["decision"]["reasons"]] == ["STOCKOUT"]
        assert f"available {late_day.isoformat()}, after deliver_by {future_day.isoformat()}" in got["decision"]["reasons"][0]["text"]
        assert [(l["product_id"], l["quantity"], l["line_total_net"]) for l in got["lines"]] == [
            ("410202", 50.0, 72.5)
        ]
        assert got["substitutions"] == [
            {
                "original_id": "410101",
                "substitute_id": "410202",
                "reason": "Stock dropped, refilled from the next offer.",
            }
        ]

        pending_mcp = mcp.call_ok("list_approvals", {}, headers=HERVANTA_HEADERS)
        assert [o["order_id"] for o in pending_mcp] == [oid]

        sari_ok = client.post(
            f"/v1/approvals/{oid}",
            json={"approve": True, "by": "Sari Nieminen", "note": "On-time backup milk approved for school"},
            headers=ADMIN_HEADERS,
        )
        assert sari_ok.status_code == 200
        assert sari_ok.json()["status"] == "confirmed"
        conf_after = orders.confirmation(oid)
        assert conf_after is not None and conf_after["confirmation_id"] != conf_before["confirmation_id"]

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410202", "available_amount": 0, "available_date": future_day.isoformat()},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        reopened_no_ontime = client.get(f"/v1/orders/{oid}", headers=HERVANTA_HEADERS).json()
        assert reopened_no_ontime["status"] == "pending_approval"
        assert reopened_no_ontime["lines"] == []
        assert reopened_no_ontime["total_net"] == 0.0
        assert "Shortfall 50" in reopened_no_ontime["decision"]["reasons"][0]["text"]

        need_50 = {
            "customer": "school-hervanta",
            "deliver_by": future_day.isoformat(),
            "items": [{"query": "milk", "quantity": 50, "unit": "L"}],
        }
        late_offers_rest = client.post("/v1/offers", json=need_50, headers=HERVANTA_HEADERS).json()["items"][0]
        late_offers_mcp = mcp.call_ok("get_offers", need_50, headers=HERVANTA_HEADERS)["items"][0]
        for grp in (late_offers_rest, late_offers_mcp):
            assert grp["offers"] == []
            assert grp["shortfall"] == 50.0
            assert any(
                opt["kind"] == "late" and opt["date"] == late_day.isoformat() and opt["days_late"] == 3
                for opt in grp["options"]
            )

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200
        restored = client.post("/v1/offers", json=need_50, headers=HERVANTA_HEADERS).json()["items"][0]
        assert restored["shortfall"] == 0.0
        assert restored["options"] == []
        assert [(o["product_id"], o["units_needed"]) for o in restored["offers"]] == [("410101", 50.0)]


def test_multi_customer_scoping_and_admin_guardrails_on_stockout(monkeypatch, future_day):
    p_primary = _make_product(
        "610100",
        "Valio Pure Apple Juice 1L",
        category="10800",
        allergens=["sulphites"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    p_backup = _make_product(
        "610200",
        "Valio Pure Organic Apple Juice 1L",
        category="10800",
        allergens=["sulphites"],
        labels=["vegan", "organic", "free_from_lactose", "free_from_gluten"],
    )
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_backup],
        {
            "610100": _price_row("610100", 1.10),
            "610200": _price_row("610200", 1.30),
        },
        {
            "610100": _avail_row("610100", future_day, 500.0),
            "610200": _avail_row("610200", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        forbid_sim = client.post(
            "/v1/admin/simulate",
            json={"product_id": "610100", "available_amount": 0},
            headers=AURORA_HEADERS,
        )
        assert forbid_sim.status_code == 403
        assert forbid_sim.json()["code"] == "forbidden"

        unauth_sim = client.post(
            "/v1/admin/simulate",
            json={"product_id": "610100", "available_amount": 0},
        )
        assert unauth_sim.status_code == 401

        forbid_clear = client.delete("/v1/admin/simulate", headers=TAYS_HEADERS)
        assert forbid_clear.status_code == 403

        placed_by_customer = {}
        for cust, hdrs, qty in (
            ("hotel-aurora", AURORA_HEADERS, 40),
            ("hospital-tays", TAYS_HEADERS, 30),
            ("school-hervanta", HERVANTA_HEADERS, 20),
        ):
            q = client.post(
                "/v1/quotes",
                json={"offers": [{"product_id": "610100", "quantity": qty}], "delivery_date": future_day.isoformat()},
                headers=hdrs,
            ).json()
            o = client.post(
                "/v1/orders",
                json={"quote_id": q["quote_id"], "jws": q["jws"]},
                headers=hdrs,
            ).json()
            assert o["status"] == "confirmed"
            placed_by_customer[cust] = o["order_id"]

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "610100", "available_amount": 0},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        for cust, hdrs in (
            ("hotel-aurora", AURORA_HEADERS),
            ("hospital-tays", TAYS_HEADERS),
            ("school-hervanta", HERVANTA_HEADERS),
        ):
            scoped_rest = client.get("/v1/approvals", headers=hdrs).json()
            scoped_mcp = mcp.call_ok("list_approvals", {}, headers=hdrs)
            assert [o["order_id"] for o in scoped_rest] == [placed_by_customer[cust]]
            assert [o["order_id"] for o in scoped_mcp] == [placed_by_customer[cust]]

        all_admin_pending = mcp.call_ok("list_approvals", {}, headers=ADMIN_HEADERS)
        assert {o["order_id"] for o in all_admin_pending} == set(placed_by_customer.values())

        cross_err = mcp.call_err(
            "get_order",
            {"order_id": placed_by_customer["hotel-aurora"]},
            headers=TAYS_HEADERS,
        )
        assert cross_err["code"] == "forbidden"

        mcp_forbid_decide = mcp.call_err(
            "decide_approval",
            {
                "order_id": placed_by_customer["hotel-aurora"],
                "approve": True,
                "by": "Laura Virtanen",
            },
            headers=AURORA_HEADERS,
        )
        assert mcp_forbid_decide["code"] == "forbidden"

        for cust, approver, approve in (
            ("hotel-aurora", "Laura Virtanen", True),
            ("hospital-tays", "Helena Korhonen", True),
            ("school-hervanta", "Sari Nieminen", False),
        ):
            res = mcp.call_ok(
                "decide_approval",
                {
                    "order_id": placed_by_customer[cust],
                    "approve": approve,
                    "by": approver,
                    "note": f"Decision for {cust}",
                },
                headers=ADMIN_HEADERS,
            )
            assert res["status"] == ("confirmed" if approve else "rejected")

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200

        full_trail = client.get("/v1/audit", headers=ADMIN_HEADERS).json()
        _assert_hash_chain_and_checkpoint(client, full_trail, full_chain=True)
        stockout_subjects = {e["subject"] for e in full_trail["events"] if e["action"] == "order.stockout"}
        assert stockout_subjects == set(placed_by_customer.values())


def test_split_delivery_option_on_partial_ontime_and_late_restock(monkeypatch, future_day):
    p_primary = _make_product("410101", "Valio Lactose Free Milk 1L")
    p_backup = _make_product("410202", "Valio Lactose Free Milk Reserve 1L")
    late_day = future_day + timedelta(days=2)
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_backup],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.45),
        },
        {
            "410101": _avail_row("410101", future_day, 500.0),
            "410202": _avail_row("410202", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 25, "available_date": future_day.isoformat()},
            headers=ADMIN_HEADERS,
        ).status_code == 200
        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410202", "available_amount": 100, "available_date": late_day.isoformat()},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        need_60 = {
            "customer": "hotel-aurora",
            "deliver_by": future_day.isoformat(),
            "items": [{"query": "milk", "quantity": 60, "unit": "L"}],
        }
        rest_grp = client.post("/v1/offers", json=need_60, headers=AURORA_HEADERS).json()["items"][0]
        mcp_grp = mcp.call_ok("get_offers", need_60, headers=AURORA_HEADERS)["items"][0]
        for grp in (rest_grp, mcp_grp):
            assert [(o["product_id"], o["units_needed"]) for o in grp["offers"]] == [("410101", 25.0)]
            assert grp["shortfall"] == 35.0
            kinds = {opt["kind"]: opt for opt in grp["options"]}
            assert {"partial", "late", "split"} <= set(kinds.keys())
            assert kinds["partial"]["covered"] == 25.0
            assert kinds["partial"]["shortfall"] == 35.0
            assert kinds["late"]["date"] == late_day.isoformat()
            assert kinds["late"]["days_late"] == 2
            assert kinds["split"]["on_time"] == 25.0
            assert kinds["split"]["later"] == 35.0
            assert kinds["split"]["days_late"] == 2

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200
        restored = client.post("/v1/offers", json=need_60, headers=AURORA_HEADERS).json()["items"][0]
        assert restored["shortfall"] == 0.0
        assert restored["options"] == []


def test_mandate_substitution_modes_and_double_decision_guards(monkeypatch, future_day):
    p_primary = _make_product("410101", "Valio Lactose Free Milk 1L", brand="Valio")
    p_valio_sub = _make_product("410202", "Valio Lactose Free Milk Reserve 1L", brand="Valio")
    p_other_sub = _make_product("410303", "Nordic Lactose Free Milk 1L", brand="NordicDairy")
    _wire_synthetic_catalog(
        monkeypatch,
        [p_primary, p_valio_sub, p_other_sub],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.40),
            "410303": _price_row("410303", 1.30),
        },
        {
            "410101": _avail_row("410101", future_day, 500.0),
            "410202": _avail_row("410202", future_day, 500.0),
            "410303": _avail_row("410303", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 0},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        q_pref = client.post(
            "/v1/quotes",
            json={
                "offers": [{"product_id": "410202", "quantity": 30, "substitute_for": "410101"}],
                "delivery_date": future_day.isoformat(),
            },
            headers=AURORA_HEADERS,
        ).json()
        o_pref = client.post(
            "/v1/orders",
            json={"quote_id": q_pref["quote_id"], "jws": q_pref["jws"]},
            headers=AURORA_HEADERS,
        ).json()
        assert o_pref["status"] == "confirmed"
        assert o_pref["decision"]["outcome"] == "auto"

        q_nonpref = client.post(
            "/v1/quotes",
            json={
                "offers": [{"product_id": "410303", "quantity": 30, "substitute_for": "410101"}],
                "delivery_date": future_day.isoformat(),
            },
            headers=AURORA_HEADERS,
        ).json()
        o_nonpref = client.post(
            "/v1/orders",
            json={"quote_id": q_nonpref["quote_id"], "jws": q_nonpref["jws"]},
            headers=AURORA_HEADERS,
        ).json()
        assert o_nonpref["status"] == "pending_approval"
        assert any(r["code"] == "SUBSTITUTION_NEEDS_APPROVAL" for r in o_nonpref["decision"]["reasons"])

        first_decide = client.post(
            f"/v1/approvals/{o_nonpref['order_id']}",
            json={"approve": True, "by": "Laura Virtanen", "note": "Non-preferred brand ok once"},
            headers=ADMIN_HEADERS,
        )
        assert first_decide.status_code == 200
        assert first_decide.json()["status"] == "confirmed"

        second_decide_rest = client.post(
            f"/v1/approvals/{o_nonpref['order_id']}",
            json={"approve": False, "by": "Laura Virtanen", "note": "too late"},
            headers=ADMIN_HEADERS,
        )
        assert second_decide_rest.status_code == 409
        assert second_decide_rest.json()["code"] == "NOT_PENDING"

        second_decide_mcp = mcp.call_err(
            "decide_approval",
            {"order_id": o_nonpref["order_id"], "approve": True, "by": "Laura Virtanen"},
            headers=ADMIN_HEADERS,
        )
        assert second_decide_mcp["code"] == "NOT_PENDING"

        current_mandate = mcp.call_ok("get_mandate", {}, headers=AURORA_HEADERS)
        strict_mandate = {**current_mandate, "substitutions": "never"}
        put_resp = client.put("/v1/mandate", json=strict_mandate, headers=ADMIN_HEADERS)
        assert put_resp.status_code == 200
        assert put_resp.json()["substitutions"] == "never"

        q_never = client.post(
            "/v1/quotes",
            json={
                "offers": [{"product_id": "410202", "quantity": 20, "substitute_for": "410101"}],
                "delivery_date": future_day.isoformat(),
            },
            headers=AURORA_HEADERS,
        ).json()
        o_never = client.post(
            "/v1/orders",
            json={"quote_id": q_never["quote_id"], "jws": q_never["jws"]},
            headers=AURORA_HEADERS,
        ).json()
        assert o_never["status"] == "blocked"
        assert o_never["decision"]["outcome"] == "blocked"
        assert any(r["code"] == "SUBSTITUTION_NOT_ALLOWED" for r in o_never["decision"]["reasons"])

        assert client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS).status_code == 200


def test_multi_line_order_single_item_stockout_and_key_rotation(monkeypatch, future_day):
    p_milk_primary = _make_product("410101", "Valio Lactose Free Milk 1L", category="10400")
    p_milk_backup = _make_product("410202", "Valio Lactose Free Milk Reserve 1L", category="10400")
    p_juice = _make_product(
        "610100",
        "Valio Pure Apple Juice 1L",
        category="10800",
        allergens=["sulphites"],
        labels=["vegan", "free_from_lactose", "free_from_gluten"],
    )
    _wire_synthetic_catalog(
        monkeypatch,
        [p_milk_primary, p_milk_backup, p_juice],
        {
            "410101": _price_row("410101", 1.20),
            "410202": _price_row("410202", 1.50),
            "610100": _price_row("610100", 2.00),
        },
        {
            "410101": _avail_row("410101", future_day, 500.0),
            "410202": _avail_row("410202", future_day, 500.0),
            "610100": _avail_row("610100", future_day, 500.0),
        },
    )

    with TestClient(app) as client:
        mcp = McpSession(client)

        parsed = mcp.call_ok(
            "parse_request",
            {
                "text": f"I need 50 litres of lactose-free milk and 20 litres of juice by {future_day.isoformat()}",
                "customer": "hotel-aurora",
            },
            headers=AURORA_HEADERS,
        )
        assert len(parsed["items"]) == 2

        compared = mcp.call_ok(
            "get_offers",
            {
                "customer": parsed["customer"],
                "deliver_by": parsed["deliver_by"],
                "items": parsed["items"],
                "raw_text": parsed["raw_text"],
            },
            headers=AURORA_HEADERS,
        )
        offer_lines = [
            {"product_id": grp["offers"][0]["product_id"], "quantity": grp["offers"][0]["units_needed"]}
            for grp in compared["items"]
        ]
        assert offer_lines == [
            {"product_id": "410101", "quantity": 50.0},
            {"product_id": "610100", "quantity": 20.0},
        ]

        quote = mcp.call_ok(
            "request_quote",
            {"offers": offer_lines, "delivery_date": future_day.isoformat()},
            headers=AURORA_HEADERS,
        )
        old_kid = keys.kid()
        new_kid = keys.rotate()
        assert new_kid != old_kid

        order = mcp.call_ok(
            "place_order",
            {"quote_id": quote["quote_id"], "jws": quote["jws"]},
            headers=AURORA_HEADERS,
        )
        oid = order["order_id"]
        assert order["status"] == "confirmed"
        assert order["total_net"] == 100.0
        conf_1 = orders.confirmation(oid)
        assert conf_1 is not None

        assert client.post(
            "/v1/admin/simulate",
            json={"product_id": "410101", "available_amount": 0},
            headers=ADMIN_HEADERS,
        ).status_code == 200

        reopened = mcp.call_ok("get_order", {"order_id": oid}, headers=AURORA_HEADERS)
        assert reopened["status"] == "pending_approval"
        assert [(l["product_id"], l["quantity"], l["line_total_net"]) for l in reopened["lines"]] == [
            ("410202", 50.0, 75.0),
            ("610100", 20.0, 40.0),
        ]
        assert reopened["total_net"] == 115.0
        assert [(s["original_id"], s["substitute_id"]) for s in reopened["substitutions"]] == [
            ("410101", "410202")
        ]

        approved = client.post(
            f"/v1/approvals/{oid}",
            json={"approve": True, "by": "Laura Virtanen", "note": "Milk backup approved, juice unchanged"},
            headers=ADMIN_HEADERS,
        ).json()
        assert approved["status"] == "confirmed"
        conf_2 = orders.confirmation(oid)
        assert conf_2 is not None and conf_2["confirmation_id"] != conf_1["confirmation_id"]

        jwks_kids = {k["kid"] for k in client.get("/.well-known/jwks.json").json()["keys"]}
        assert {old_kid, new_kid} <= jwks_kids

        full_trail = client.get("/v1/audit", headers=ADMIN_HEADERS).json()
        _assert_hash_chain_and_checkpoint(client, full_trail, full_chain=True)
        assert "Supplier stock-out:" in full_trail["text"]
        assert "Milk backup approved, juice unchanged" in full_trail["text"]

