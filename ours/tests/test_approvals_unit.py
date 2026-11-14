import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aimo import approvals, audit, catalog, mandates, offers, orders, quotes
from aimo.app import app
from aimo.approvals import ApprovalError
from aimo.models import PlaceOrderRequest, Product, QuoteLine, QuoteRequest, QuoteRequestLine, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA_KEY = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS_KEY = {"X-Agent-Key": "demo-hospital-tays"}
HERVANTA_KEY = {"X-Agent-Key": "demo-school-hervanta"}
ADMIN_KEY = {"X-Admin-Key": "demo-admin"}

PRODUCTS = {
    "milk-1l": Product(
        product_id="milk-1l",
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
    ),
    "milk-alt": Product(
        product_id="milk-alt",
        name="Valio Kevytmaito 1 L",
        brand="Valio",
        vendor="Valio Oy",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["finnish"],
        origin="FI",
        temperature=6,
    ),
    "oat-other": Product(
        product_id="oat-other",
        name="Nordic Oat Drink 1 L",
        brand="OtherBrand",
        vendor="OtherVendor",
        category="Dairy",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=[],
        labels=[],
        origin="SE",
        temperature=6,
    ),
    "peanut-snack": Product(
        product_id="peanut-snack",
        name="Roasted Peanut Snack",
        brand="Valio",
        vendor="Valio Oy",
        category="Snacks",
        sales_unit="PCE",
        unit_size=UnitSize(value=1.0, unit="pcs"),
        allergens=["peanut"],
        labels=[],
        origin="FI",
        temperature=20,
    ),
}

PRICES = {
    "milk-1l": 1.0,
    "milk-alt": 1.0,
    "oat-other": 1.0,
    "peanut-snack": 10.0,
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
                "clientInfo": {"name": "approvals-unit", "version": "1.0"},
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
                assert blocks == sc["result"]
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "approvals_unit.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "empty_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)

    state = {"ttl": timedelta(hours=2)}

    def fake_get(pid):
        return PRODUCTS.get(str(pid))

    def fake_search(q="", constraints=None, limit=50):
        return list(PRODUCTS.values())[:limit]

    def fake_price_lines(customer, delivery_date, lines):
        out = []
        for line in lines:
            prod = PRODUCTS.get(line.product_id)
            if prod is None:
                raise ValueError(f"unknown product {line.product_id}")
            unit_net = PRICES[line.product_id]
            line_net = round(line.quantity * unit_net, 2)
            out.append(
                QuoteLine(
                    product_id=line.product_id,
                    name=prod.name,
                    quantity=line.quantity,
                    sales_unit=prod.sales_unit,
                    unit_price_net=unit_net,
                    line_total_net=line_net,
                    substitute_for=line.substitute_for,
                )
            )
        total_net = sum(x.line_total_net for x in out)
        valid_until = datetime.now(timezone.utc) + state["ttl"]
        return out, round(total_net * 1.14, 2), valid_until

    monkeypatch.setattr(catalog, "get", fake_get)
    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(offers, "price_lines", fake_price_lines)

    with TestClient(app) as client:
        yield client, McpSession(client), state


def _delivery_day(days=3) -> date:
    return date.today() + timedelta(days=days)


def _place_py(
    customer="hotel-aurora",
    agent_id="buyer-aurora",
    product_id="milk-1l",
    quantity=3000.0,
    substitute_for=None,
    ask_human=False,
    note=None,
    valid_until=None,
):
    day = _delivery_day()
    if valid_until is None:
        q = orders.create_quote(
            customer,
            agent_id,
            QuoteRequest(
                offers=[QuoteRequestLine(product_id=product_id, quantity=quantity, substitute_for=substitute_for)],
                delivery_date=day,
            ),
        )
    else:
        prod = PRODUCTS[product_id]
        unit_net = PRICES[product_id]
        line_net = round(quantity * unit_net, 2)
        line = QuoteLine(
            product_id=product_id,
            name=prod.name,
            quantity=quantity,
            sales_unit=prod.sales_unit,
            unit_price_net=unit_net,
            line_total_net=line_net,
            substitute_for=substitute_for,
        )
        q = quotes.issue(customer, agent_id, [line], day, valid_until, round(line_net * 1.14, 2))
    return orders.place(customer, agent_id, PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws, ask_human=ask_human, note=note))


def _place_rest(client: TestClient, headers: dict, product_id="milk-1l", quantity=3000.0, **extra) -> dict:
    day = _delivery_day().isoformat()
    qr = client.post(
        "/v1/quotes",
        headers=headers,
        json={"offers": [{"product_id": product_id, "quantity": quantity}], "delivery_date": day},
    )
    assert qr.status_code == 200, qr.text
    q = qr.json()
    pr = client.post("/v1/orders", headers=headers, json={"quote_id": q["quote_id"], "jws": q["jws"], **extra})
    assert pr.status_code == 200, pr.text
    return pr.json()


def test_pending_sorting_and_status_filtering_across_surfaces(env, monkeypatch):
    client, mcp, _ = env
    base = datetime.now(timezone.utc)

    o_auto = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 500.0)
    assert o_auto.status == "confirmed"

    o_blocked = _place_py("hospital-tays", "buyer-tays", "peanut-snack", 5.0)
    assert o_blocked.status == "blocked"

    o_to_reject = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2600.0)
    assert o_to_reject.status == "pending_approval"
    approver = mandates.get("hotel-aurora").approvers[0]
    assert approvals.decide(o_to_reject.order_id, False, approver, "not needed").status == "rejected"

    monkeypatch.setattr(orders, "_now", lambda: base + timedelta(minutes=30))
    o_late = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2700.0)

    monkeypatch.setattr(orders, "_now", lambda: base + timedelta(minutes=5))
    o_early = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2800.0)

    monkeypatch.setattr(orders, "_now", lambda: base + timedelta(minutes=15))
    o_mid = _place_py("hospital-tays", "buyer-tays", "milk-1l", 1800.0)

    monkeypatch.setattr(orders, "_now", lambda: base + timedelta(minutes=20))
    o_school = _place_py("school-hervanta", "buyer-hervanta", "milk-1l", 700.0)

    monkeypatch.setattr(orders, "_now", lambda: base + timedelta(minutes=35))

    expected_all = [o_early.order_id, o_mid.order_id, o_school.order_id, o_late.order_id]
    expected_aurora = [o_early.order_id, o_late.order_id]

    py_all = approvals.pending()
    assert [o.order_id for o in py_all] == expected_all
    assert all(o.status == "pending_approval" for o in py_all)
    assert [o.created_at for o in py_all] == sorted(o.created_at for o in py_all)
    assert [o.order_id for o in approvals.pending("hotel-aurora")] == expected_aurora
    assert [o.order_id for o in approvals.pending("hospital-tays")] == [o_mid.order_id]
    assert [o.order_id for o in approvals.pending("school-hervanta")] == [o_school.order_id]

    r_admin_all = client.get("/v1/approvals", headers=ADMIN_KEY)
    assert r_admin_all.status_code == 200
    assert [o["order_id"] for o in r_admin_all.json()] == expected_all

    r_admin_scoped = client.get("/v1/approvals?customer=hospital-tays", headers=ADMIN_KEY)
    assert r_admin_scoped.status_code == 200
    assert [o["order_id"] for o in r_admin_scoped.json()] == [o_mid.order_id]

    r_aurora = client.get("/v1/approvals", headers=AURORA_KEY)
    assert r_aurora.status_code == 200
    assert [o["order_id"] for o in r_aurora.json()] == expected_aurora

    r_aurora_self = client.get("/v1/approvals?customer=hotel-aurora", headers=AURORA_KEY)
    assert r_aurora_self.status_code == 200
    assert [o["order_id"] for o in r_aurora_self.json()] == expected_aurora

    r_tays = client.get("/v1/approvals", headers=TAYS_KEY)
    assert r_tays.status_code == 200
    assert [o["order_id"] for o in r_tays.json()] == [o_mid.order_id]

    r_school = client.get("/v1/approvals", headers=HERVANTA_KEY)
    assert r_school.status_code == 200
    assert [o["order_id"] for o in r_school.json()] == [o_school.order_id]

    r_cross = client.get("/v1/approvals?customer=hospital-tays", headers=AURORA_KEY)
    assert r_cross.status_code == 403
    assert r_cross.json()["code"] == "forbidden"

    r_unauth = client.get("/v1/approvals")
    assert r_unauth.status_code == 401
    assert r_unauth.json()["code"] == "unauthorized"

    mcp_admin_all = mcp.call_ok("list_approvals", {}, headers=ADMIN_KEY)
    assert [o["order_id"] for o in mcp_admin_all] == expected_all

    mcp_aurora = mcp.call_ok("list_approvals", {}, headers=AURORA_KEY)
    assert [o["order_id"] for o in mcp_aurora] == expected_aurora

    mcp_cross = mcp.call_err("list_approvals", {"customer": "hospital-tays"}, headers=AURORA_KEY)
    assert mcp_cross["code"] == "forbidden"

    mcp_unauth = mcp.call_err("list_approvals", {})
    assert mcp_unauth["code"] == "unauthorized"


def test_approve_transition_confirmation_and_audit_python(env):
    approvers = mandates.get("hotel-aurora").approvers
    assert "Laura Virtanen" in approvers
    o = _place_py(
        "hotel-aurora",
        "buyer-aurora",
        "oat-other",
        3000.0,
        substitute_for="milk-1l",
    )
    assert o.status == "pending_approval"
    held_codes = {r.code for r in o.decision.reasons}
    assert {"OVER_ORDER_LIMIT", "SUBSTITUTION_NEEDS_APPROVAL"} <= held_codes
    held_texts = [r.text for r in o.decision.reasons]

    spent_before = orders.spent_this_week("hotel-aurora")
    assert spent_before == 0.0

    decided = approvals.decide(o.order_id, True, "Laura Virtanen", "banquet weekend")
    assert decided.status == "confirmed"
    assert orders.get(o.order_id).status == "confirmed"
    assert approvals.pending("hotel-aurora") == []
    assert orders.spent_this_week("hotel-aurora") == 3000.0

    conf = orders.confirmation(o.order_id)
    assert conf is not None
    assert conf["confirmation_id"].startswith("MOCK-")
    assert conf["mock"] is True
    assert conf["delivery_date"] == o.delivery_date.isoformat()

    evs = audit.events(order_id=o.order_id)
    actions = [e.action for e in evs]
    assert actions == [
        "quote.issued",
        "quote.redeemed",
        "order.placed",
        "order.pending_approval",
        "approval.approved",
        "order.approved",
        "order.placed",
        "order.confirmed",
    ]

    ap_ev = next(e for e in evs if e.action == "approval.approved")
    assert ap_ev.actor == "human:Laura Virtanen"
    assert ap_ev.subject == o.order_id
    assert ap_ev.detail == {"order_id": o.order_id, "note": "banquet weekend"}
    assert "Laura Virtanen approved the order of 3,000.00 EUR." in ap_ev.why
    assert "Note: banquet weekend." in ap_ev.why
    assert "Held for approval because:" in ap_ev.why
    for txt in held_texts:
        assert txt in ap_ev.why

    ord_ap = next(e for e in evs if e.action == "order.approved")
    assert ord_ap.actor == "human:Laura Virtanen"
    assert ord_ap.why == "Approved by Laura Virtanen."

    ord_placed = [e for e in evs if e.action == "order.placed"][-1]
    assert ord_placed.actor == "human:Laura Virtanen"
    assert ord_placed.why == "Placed after human approval."

    ord_conf = next(e for e in evs if e.action == "order.confirmed")
    assert ord_conf.actor == "system"
    assert ord_conf.detail["confirmation_id"] == conf["confirmation_id"]
    assert ord_conf.detail["mock"] is True

    assert audit.verify() == (True, None)
    buyer_narration = audit.narrate(evs, view="buyer")
    assert "Laura Virtanen approved an order." in buyer_narration
    assert "Note: banquet weekend." in buyer_narration
    assert "Placed after human approval." in buyer_narration
    assert "Order confirmed by Valio (mock)." in buyer_narration
    assert buyer_narration.count("approved an order") == 1
    assert "Mandate check passed:" not in buyer_narration

    valio_narration = audit.narrate(evs, view="valio")
    assert "Laura Virtanen at Hotel Aurora approved an order." in valio_narration
    assert f"[{o.order_id}," in valio_narration


def test_approve_via_rest_and_mcp_with_real_mandate_approvers(env):
    client, mcp, _ = env

    tays_approver = mandates.get("hospital-tays").approvers[0]
    school_approver = mandates.get("school-hervanta").approvers[0]
    assert tays_approver == "Helena Korhonen"
    assert school_approver == "Sari Nieminen"

    o_tays = _place_rest(client, TAYS_KEY, "milk-1l", 2000.0)
    assert o_tays["status"] == "pending_approval"

    r_dec = client.post(
        f"/v1/approvals/{o_tays['order_id']}",
        headers=ADMIN_KEY,
        json={"approve": True, "by": tays_approver, "note": ""},
    )
    assert r_dec.status_code == 200, r_dec.text
    body = r_dec.json()
    assert body["status"] == "confirmed"
    assert orders.confirmation(o_tays["order_id"])["confirmation_id"].startswith("MOCK-")

    r_audit = client.get(f"/v1/audit?order_id={o_tays['order_id']}", headers=TAYS_KEY)
    assert r_audit.status_code == 200
    trail = r_audit.json()
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    ap_rest = next(e for e in trail["events"] if e["action"] == "approval.approved")
    assert ap_rest["actor"] == f"human:{tays_approver}"
    assert "Note: none." in ap_rest["why"]
    assert "OVER_ORDER_LIMIT" in [r["code"] for r in o_tays["decision"]["reasons"]]
    assert o_tays["decision"]["reasons"][0]["text"] in ap_rest["why"]
    assert f"{tays_approver} approved an order." in trail["text"]
    assert "Order confirmed by Valio (mock)." in trail["text"]

    o_school = _place_rest(client, HERVANTA_KEY, "milk-1l", 750.0)
    assert o_school["status"] == "pending_approval"

    mcp_dec = mcp.call_ok(
        "decide_approval",
        {"order_id": o_school["order_id"], "approve": True, "by": school_approver, "note": "approved for lunch"},
        headers=ADMIN_KEY,
    )
    assert mcp_dec["status"] == "confirmed"
    assert orders.confirmation(o_school["order_id"])["confirmation_id"].startswith("MOCK-")
    assert orders.spent_this_week("school-hervanta") == 750.0

    mcp_trail = mcp.call_ok("get_audit_trail", {"order_id": o_school["order_id"]}, headers=HERVANTA_KEY)
    assert mcp_trail["verified"]["ok"] is True
    mcp_actions = [e["action"] for e in mcp_trail["events"]]
    for act in ("approval.approved", "order.approved", "order.placed", "order.confirmed"):
        assert act in mcp_actions
    assert f"{school_approver} approved an order." in mcp_trail["text"]
    assert "Note: approved for lunch." in mcp_trail["text"]


def test_reject_transitions_audit_and_weekly_spend_across_surfaces(env):
    client, mcp, _ = env

    o_confirmed = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 400.0)
    assert o_confirmed.status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 400.0

    o_py = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3100.0)
    o_rest = _place_rest(client, AURORA_KEY, "milk-1l", 3200.0)
    o_mcp = _place_rest(client, AURORA_KEY, "milk-1l", 3300.0)
    assert orders.spent_this_week("hotel-aurora") == 400.0

    rej_py = approvals.decide(o_py.order_id, False, "Laura Virtanen", "over budget")
    assert rej_py.status == "rejected"
    assert orders.confirmation(o_py.order_id) is None

    r_rej = client.post(
        f"/v1/approvals/{o_rest['order_id']}",
        headers=ADMIN_KEY,
        json={"approve": False, "by": "Mikko Heikkinen", "note": "postpone"},
    )
    assert r_rej.status_code == 200, r_rej.text
    assert r_rej.json()["status"] == "rejected"
    assert orders.confirmation(o_rest["order_id"]) is None

    mcp_rej = mcp.call_ok(
        "decide_approval",
        {"order_id": o_mcp["order_id"], "approve": False, "by": "Laura Virtanen", "note": "cancel"},
        headers=ADMIN_KEY,
    )
    assert mcp_rej["status"] == "rejected"
    assert orders.confirmation(o_mcp["order_id"]) is None

    assert orders.spent_this_week("hotel-aurora") == 400.0
    assert approvals.pending("hotel-aurora") == []

    for oid, who, note in [
        (o_py.order_id, "Laura Virtanen", "over budget"),
        (o_rest["order_id"], "Mikko Heikkinen", "postpone"),
        (o_mcp["order_id"], "Laura Virtanen", "cancel"),
    ]:
        evs = audit.events(order_id=oid)
        actions = [e.action for e in evs]
        assert actions[-2:] == ["approval.rejected", "order.rejected"]
        assert "order.confirmed" not in actions
        ap_rej = evs[-2]
        ord_rej = evs[-1]
        assert ap_rej.actor == f"human:{who}"
        assert ord_rej.actor == f"human:{who}"
        assert f"Note: {note}." in ap_rej.why
        assert "Held for approval because:" in ap_rej.why
        text = audit.narrate(evs)
        assert f"{who} rejected an order." in text
        assert text.count("rejected an order") == 1
        assert "Order rejected:" not in text

    assert audit.verify() == (True, None)


def test_domain_errors_not_found_not_pending_not_approver_and_auth(env):
    client, mcp, _ = env

    with pytest.raises(ApprovalError) as exc:
        approvals.decide("ord_missing_999", True, "Laura Virtanen")
    assert exc.value.code == "NOT_FOUND"
    assert "ord_missing_999" in exc.value.text

    r_nf = client.post(
        "/v1/approvals/ord_missing_999",
        headers=ADMIN_KEY,
        json={"approve": True, "by": "Laura Virtanen"},
    )
    assert r_nf.status_code == 404
    assert r_nf.json()["code"] == "NOT_FOUND"
    assert r_nf.json()["text"]

    m_nf = mcp.call_err(
        "decide_approval",
        {"order_id": "ord_missing_999", "approve": True, "by": "Laura Virtanen"},
        headers=ADMIN_KEY,
    )
    assert m_nf["code"] == "NOT_FOUND"
    assert m_nf["text"]

    r_no_key = client.post("/v1/approvals/ord_missing_999", json={"approve": True, "by": "Laura Virtanen"})
    assert r_no_key.status_code == 401
    assert r_no_key.json()["code"] == "unauthorized"

    r_agent_key = client.post(
        "/v1/approvals/ord_missing_999",
        headers=AURORA_KEY,
        json={"approve": True, "by": "Laura Virtanen"},
    )
    assert r_agent_key.status_code == 403
    assert r_agent_key.json()["code"] == "forbidden"

    m_no_key = mcp.call_err("decide_approval", {"order_id": "ord_missing_999", "approve": True, "by": "Laura Virtanen"})
    assert m_no_key["code"] == "unauthorized"

    m_agent_key = mcp.call_err(
        "decide_approval",
        {"order_id": "ord_missing_999", "approve": True, "by": "Laura Virtanen"},
        headers=AURORA_KEY,
    )
    assert m_agent_key["code"] == "forbidden"

    o_confirmed = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 200.0)
    assert o_confirmed.status == "confirmed"

    o_rejected = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3000.0)
    approvals.decide(o_rejected.order_id, False, "Laura Virtanen", "no")
    assert orders.get(o_rejected.order_id).status == "rejected"

    o_blocked = _place_py("hospital-tays", "buyer-tays", "peanut-snack", 3.0)
    assert o_blocked.status == "blocked"

    for non_pending, who in [
        (o_confirmed, "Laura Virtanen"),
        (o_rejected, "Laura Virtanen"),
        (o_blocked, "Helena Korhonen"),
    ]:
        before_count = len(audit.events(order_id=non_pending.order_id))

        with pytest.raises(ApprovalError) as exc_np:
            approvals.decide(non_pending.order_id, True, who)
        assert exc_np.value.code == "NOT_PENDING"

        r_np = client.post(
            f"/v1/approvals/{non_pending.order_id}",
            headers=ADMIN_KEY,
            json={"approve": True, "by": who},
        )
        assert r_np.status_code == 409
        assert r_np.json()["code"] == "NOT_PENDING"
        assert r_np.json()["text"]

        m_np = mcp.call_err(
            "decide_approval",
            {"order_id": non_pending.order_id, "approve": True, "by": who},
            headers=ADMIN_KEY,
        )
        assert m_np["code"] == "NOT_PENDING"
        assert m_np["text"]

        assert len(audit.events(order_id=non_pending.order_id)) == before_count

    o_pending = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3500.0)
    assert o_pending.status == "pending_approval"
    pending_audit_len = len(audit.events(order_id=o_pending.order_id))

    with pytest.raises(ApprovalError) as exc_na:
        approvals.decide(o_pending.order_id, True, "Intruder")
    assert exc_na.value.code == "NOT_APPROVER"

    r_na = client.post(
        f"/v1/approvals/{o_pending.order_id}",
        headers=ADMIN_KEY,
        json={"approve": True, "by": "Helena Korhonen"},
    )
    assert r_na.status_code == 403
    assert r_na.json()["code"] == "NOT_APPROVER"
    assert r_na.json()["text"]

    m_na = mcp.call_err(
        "decide_approval",
        {"order_id": o_pending.order_id, "approve": False, "by": "Sari Nieminen"},
        headers=ADMIN_KEY,
    )
    assert m_na["code"] == "NOT_APPROVER"
    assert m_na["text"]

    assert orders.get(o_pending.order_id).status == "pending_approval"
    assert [x.order_id for x in approvals.pending("hotel-aurora")] == [o_pending.order_id]
    assert len(audit.events(order_id=o_pending.order_id)) == pending_audit_len

    approved_after = approvals.decide(o_pending.order_id, True, "Laura Virtanen", "verified")
    assert approved_after.status == "confirmed"
    assert orders.get(o_pending.order_id).status == "confirmed"


def test_missing_mandate_raises_not_approver_and_race_raises_not_pending(env, monkeypatch):
    o = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3000.0)
    assert o.status == "pending_approval"

    with monkeypatch.context() as m:
        m.setattr(mandates, "get", lambda customer, agent_id=None: None)
        with pytest.raises(ApprovalError) as exc:
            approvals.decide(o.order_id, True, "Laura Virtanen")
        assert exc.value.code == "NOT_APPROVER"

    assert orders.get(o.order_id).status == "pending_approval"

    with monkeypatch.context() as m:
        def conflict_set_status(order_id, status, actor="system", why=None):
            raise ValueError("already decided")

        m.setattr(orders, "set_status", conflict_set_status)
        with pytest.raises(ApprovalError) as exc_race:
            approvals.decide(o.order_id, True, "Laura Virtanen")
        assert exc_race.value.code == "NOT_PENDING"


def test_quote_expired_during_decision_python_rest_and_mcp(env, monkeypatch):
    client, mcp, _ = env
    base = datetime.now(timezone.utc)

    o_py = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3000.0, valid_until=base + timedelta(minutes=20))
    o_rest = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3100.0, valid_until=base + timedelta(minutes=20))
    o_mcp = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3200.0, valid_until=base + timedelta(minutes=20))

    with monkeypatch.context() as m:
        m.setattr(orders, "_now", lambda: base + timedelta(minutes=19))
        m.setattr(approvals, "_now", lambda: base + timedelta(minutes=20))

        with pytest.raises(ApprovalError) as exc_py:
            approvals.decide(o_py.order_id, True, "Laura Virtanen")
        assert exc_py.value.code == "QUOTE_EXPIRED"

        r_exp = client.post(
            f"/v1/approvals/{o_rest.order_id}",
            headers=ADMIN_KEY,
            json={"approve": True, "by": "Laura Virtanen"},
        )
        assert r_exp.status_code == 409
        assert r_exp.json()["code"] == "QUOTE_EXPIRED"
        assert r_exp.json()["text"]

        m_exp = mcp.call_err(
            "decide_approval",
            {"order_id": o_mcp.order_id, "approve": True, "by": "Laura Virtanen"},
            headers=ADMIN_KEY,
        )
        assert m_exp["code"] == "QUOTE_EXPIRED"
        assert m_exp["text"]

    for o in (o_py, o_rest, o_mcp):
        assert orders.get(o.order_id).status == "expired"
        evs = audit.events(order_id=o.order_id)
        actions = [e.action for e in evs]
        assert "order.expired" in actions
        assert "approval.approved" not in actions
        assert "order.confirmed" not in actions
        exp_ev = next(e for e in evs if e.action == "order.expired")
        assert exp_ev.actor == "system"
        assert "Order expired:" in audit.narrate([exp_ev])

    assert approvals.pending("hotel-aurora") == []

    o_auto_py = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3300.0, valid_until=base + timedelta(minutes=30))
    o_auto_rest = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3400.0, valid_until=base + timedelta(minutes=30))
    o_auto_mcp = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 3500.0, valid_until=base + timedelta(minutes=30))

    with monkeypatch.context() as m:
        m.setattr(orders, "_now", lambda: base + timedelta(minutes=45))
        m.setattr(approvals, "_now", lambda: base + timedelta(minutes=45))

        with pytest.raises(ApprovalError) as exc_auto:
            approvals.decide(o_auto_py.order_id, True, "Laura Virtanen")
        assert exc_auto.value.code in ("QUOTE_EXPIRED", "NOT_PENDING")

        r_auto = client.post(
            f"/v1/approvals/{o_auto_rest.order_id}",
            headers=ADMIN_KEY,
            json={"approve": True, "by": "Laura Virtanen"},
        )
        assert r_auto.status_code == 409
        assert r_auto.json()["code"] in ("QUOTE_EXPIRED", "NOT_PENDING")

        m_auto = mcp.call_err(
            "decide_approval",
            {"order_id": o_auto_mcp.order_id, "approve": True, "by": "Laura Virtanen"},
            headers=ADMIN_KEY,
        )
        assert m_auto["code"] in ("QUOTE_EXPIRED", "NOT_PENDING")

        for o in (o_auto_py, o_auto_rest, o_auto_mcp):
            assert orders.get(o.order_id).status == "expired"
            assert any(e.action == "order.expired" for e in audit.events(order_id=o.order_id))

    assert audit.verify() == (True, None)


def test_expire_stale_selective_boundary_idempotent_and_naive_now(env, monkeypatch):
    base = datetime.now(timezone.utc)

    o_confirmed = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 300.0, valid_until=base + timedelta(minutes=10))
    assert o_confirmed.status == "confirmed"

    o_past = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2800.0, valid_until=base + timedelta(minutes=15))
    o_exact = _place_py("hospital-tays", "buyer-tays", "milk-1l", 1800.0, valid_until=base + timedelta(minutes=30))
    o_valid_1 = _place_py("school-hervanta", "buyer-hervanta", "milk-1l", 700.0, valid_until=base + timedelta(minutes=45))
    o_valid_2 = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2900.0, valid_until=base + timedelta(minutes=90))

    assert len(approvals.pending()) == 4

    assert approvals.expire_stale(now=base + timedelta(minutes=5)) == 0
    assert len(approvals.pending()) == 4

    expired_count = approvals.expire_stale(now=base + timedelta(minutes=30))
    assert expired_count == 2

    assert orders.get(o_past.order_id).status == "expired"
    assert orders.get(o_exact.order_id).status == "expired"
    assert orders.get(o_valid_1.order_id).status == "pending_approval"
    assert orders.get(o_valid_2.order_id).status == "pending_approval"
    assert orders.get(o_confirmed.order_id).status == "confirmed"

    assert [o.order_id for o in approvals.pending()] == [o_valid_1.order_id, o_valid_2.order_id]

    assert approvals.expire_stale(now=base + timedelta(minutes=30)) == 0

    naive_cutoff = (base + timedelta(minutes=50)).replace(tzinfo=None)
    assert approvals.expire_stale(now=naive_cutoff) == 1
    assert orders.get(o_valid_1.order_id).status == "expired"
    assert [o.order_id for o in approvals.pending()] == [o_valid_2.order_id]

    with monkeypatch.context() as m:
        m.setattr(approvals, "_now", lambda: base + timedelta(minutes=120))
        assert approvals.expire_stale() == 1

    assert orders.get(o_valid_2.order_id).status == "expired"
    assert approvals.pending() == []

    for o in (o_past, o_exact, o_valid_1, o_valid_2):
        evs = audit.events(order_id=o.order_id)
        exp_evs = [e for e in evs if e.action == "order.expired"]
        assert len(exp_evs) == 1
        assert exp_evs[0].actor == "system"
        assert "Quote validity ended while waiting for approval" in exp_evs[0].why
        assert "Order expired:" in audit.narrate(exp_evs)

    assert audit.verify() == (True, None)


def test_agent_requested_approval_empty_reasons_and_notification_output(env, monkeypatch, capsys):
    err_instance = ApprovalError("NOT_FOUND", "Missing order.")
    assert str(err_instance) == "NOT_FOUND: Missing order."
    assert err_instance.code == "NOT_FOUND"
    assert err_instance.text == "Missing order."

    o_ask = _place_py(
        "hotel-aurora",
        "buyer-aurora",
        "milk-1l",
        100.0,
        ask_human=True,
        note="Please double-check VIP event quantity",
    )
    assert o_ask.status == "pending_approval"
    assert [r.code for r in o_ask.decision.reasons] == ["AGENT_REQUESTED"]

    capsys.readouterr()
    out_ok = approvals.decide(o_ask.order_id, True, "Mikko Heikkinen", "VIP confirmed")
    assert out_ok.status == "confirmed"
    printed_ok = capsys.readouterr().out
    assert f"[approvals] approved order={o_ask.order_id} customer=hotel-aurora total=100.00" in printed_ok

    ap_ev = next(e for e in audit.events(order_id=o_ask.order_id) if e.action == "approval.approved")
    assert "Please double-check VIP event quantity" in ap_ev.why
    assert "Note: VIP confirmed." in ap_ev.why

    o_empty = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 2600.0)
    con = orders._con()
    cleared = o_empty.model_copy(update={"decision": o_empty.decision.model_copy(update={"reasons": []})})
    orders._save(con, cleared)
    con.commit()

    capsys.readouterr()
    out_rej = approvals.decide(o_empty.order_id, False, "Laura Virtanen")
    assert out_rej.status == "rejected"
    printed_rej = capsys.readouterr().out
    assert f"[approvals] rejected order={o_empty.order_id} customer=hotel-aurora total=2,600.00" in printed_rej

    rej_ev = next(e for e in audit.events(order_id=o_empty.order_id) if e.action == "approval.rejected")
    assert "Held for approval because: no reasons recorded." in rej_ev.why
    assert "Note: none." in rej_ev.why

    with monkeypatch.context() as m:
        m.setattr(quotes, "get", lambda qid: None)
        assert approvals._expired(o_empty, datetime.now(timezone.utc) + timedelta(days=10)) is False


def test_stockout_reopened_order_approval_and_rejection(env, monkeypatch):
    from aimo import allocate

    o1 = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 400.0)
    o2 = _place_py("hotel-aurora", "buyer-aurora", "milk-1l", 600.0)
    assert o1.status == "confirmed" and o2.status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 1000.0

    sub_line_1 = QuoteLine(
        product_id="milk-alt",
        name=PRODUCTS["milk-alt"].name,
        quantity=400.0,
        sales_unit="LTR",
        unit_price_net=1.0,
        line_total_net=400.0,
    )
    sub_line_2 = QuoteLine(
        product_id="milk-alt",
        name=PRODUCTS["milk-alt"].name,
        quantity=600.0,
        sales_unit="LTR",
        unit_price_net=1.0,
        line_total_net=600.0,
    )

    def fake_stockout_plan(lines, fresh, day):
        qty = lines[0].quantity
        line = sub_line_1 if qty == 400.0 else sub_line_2
        return {
            "changed": True,
            "lines": [line],
            "changes": ["milk-1l replaced by milk-alt"],
            "shortfall": 0.0,
        }

    monkeypatch.setattr(offers, "fresh_offers", lambda customer, day, pids: [])
    monkeypatch.setattr(allocate, "stockout_plan", fake_stockout_plan)

    reopened = orders.react_stockout("milk-1l")
    assert {o.order_id for o in reopened} == {o1.order_id, o2.order_id}
    assert {o.order_id for o in approvals.pending("hotel-aurora")} == {o1.order_id, o2.order_id}
    assert orders.spent_this_week("hotel-aurora") == 0.0

    reconfirmed = approvals.decide(o1.order_id, True, "Laura Virtanen", "accept alt milk")
    assert reconfirmed.status == "confirmed"
    assert orders.spent_this_week("hotel-aurora") == 400.0

    rejected = approvals.decide(o2.order_id, False, "Mikko Heikkinen", "skip alt milk")
    assert rejected.status == "rejected"
    assert orders.spent_this_week("hotel-aurora") == 400.0
    assert approvals.pending("hotel-aurora") == []

    ap_ev = next(e for e in audit.events(order_id=o1.order_id) if e.action == "approval.approved")
    assert "Stock dropped after the order: milk-1l replaced by milk-alt" in ap_ev.why
    assert audit.verify() == (True, None)

