from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from aimo import catalog, offers
from aimo.app import app
from aimo.models import Product, QuoteLine

HEL = ZoneInfo("Europe/Helsinki")
DAY = (datetime.now(HEL).date() + timedelta(days=2)).isoformat()
ADMIN = {"X-Admin-Key": "demo-admin"}

PRODUCTS = {
    "milk-valio": Product(
        product_id="milk-valio",
        name="Valio lactose-free milk 1 l",
        brand="Valio",
        category="10400",
        sales_unit="L",
        allergens=["milk"],
    ),
    "milk-orig": Product(
        product_id="milk-orig",
        name="Valio whole milk 1 l",
        brand="Valio",
        category="10400",
        sales_unit="L",
        allergens=["milk"],
    ),
    "oat-other": Product(
        product_id="oat-other",
        name="Nordic oat drink 1 l",
        brand="Other",
        category="10400",
        sales_unit="L",
        allergens=[],
    ),
    "peanut-bar": Product(
        product_id="peanut-bar",
        name="Peanut protein bar",
        brand="Valio",
        category="10400",
        sales_unit="ST",
        allergens=["peanuts"],
    ),
    "trace-granola": Product(
        product_id="trace-granola",
        name="Berry granola",
        brand="Valio",
        category="10400",
        sales_unit="ST",
        allergens=["may_contain:tree nuts"],
    ),
}

PRICES = {
    "milk-valio": 1.60,
    "milk-orig": 1.60,
    "oat-other": 1.80,
    "peanut-bar": 2.50,
    "trace-granola": 4.00,
}


def fake_price_lines(customer, delivery_date, lines):
    out = []
    for l in lines:
        p = PRODUCTS.get(l.product_id)
        net = PRICES[l.product_id]
        out.append(
            QuoteLine(
                product_id=l.product_id,
                name=p.name if p else None,
                quantity=l.quantity,
                sales_unit=p.sales_unit if p else None,
                unit_price_net=net,
                line_total_net=round(l.quantity * net, 2),
                substitute_for=l.substitute_for,
            )
        )
    total_net = sum(x.line_total_net for x in out)
    valid_until = datetime.now(timezone.utc) + timedelta(hours=4)
    return out, round(total_net * 1.14, 2), valid_until


@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(str(pid)))
    monkeypatch.setattr(offers, "price_lines", fake_price_lines)
    yield TestClient(app)


def order(c, key, pid, qty, sub_for=None):
    h = {"X-Agent-Key": key}
    line = {"product_id": pid, "quantity": qty}
    if sub_for:
        line["substitute_for"] = sub_for
    q = c.post("/v1/quotes", headers=h, json={"offers": [line], "delivery_date": DAY})
    assert q.status_code == 200, q.text
    q = q.json()
    o = c.post("/v1/orders", headers=h, json={"quote_id": q["quote_id"], "jws": q["jws"]})
    assert o.status_code == 200, o.text
    return o.json()


def test_hospital_blocked_contains_and_traces(c):
    key = "demo-hospital-tays"
    o1 = order(c, key, "peanut-bar", 1)
    assert o1["status"] == "blocked" and o1["decision"]["outcome"] == "blocked"
    assert "ALLERGEN_CONFLICT" in [r["code"] for r in o1["decision"]["reasons"]]

    o2 = order(c, key, "trace-granola", 1)
    assert o2["status"] == "blocked" and o2["decision"]["outcome"] == "blocked"
    assert any(r["code"] == "ALLERGEN_CONFLICT" and "traces" in r["text"] for r in o2["decision"]["reasons"])

    audit_resp = c.get("/v1/audit", headers={"X-Agent-Key": key})
    assert audit_resp.status_code == 200, audit_resp.text
    trail = audit_resp.json()
    assert trail["verified"]["ok"] is True
    blocked_ids = {e["subject"] for e in trail["events"] if e["action"] == "order.blocked"}
    assert {o1["order_id"], o2["order_id"]} <= blocked_ids


def test_school_weekly_budget_crossing(c):
    key = "demo-school-hervanta"
    mandate = c.get("/v1/mandate", headers={"X-Agent-Key": key}).json()
    assert mandate["per_order_eur"] == 600.0 and mandate["per_week_eur"] == 3000.0

    outs = [order(c, key, "milk-valio", 350) for _ in range(6)]
    for o in outs[:5]:
        assert o["total_net"] == 560.0 < mandate["per_order_eur"]
        assert o["status"] == "confirmed"
        assert [r["code"] for r in o["decision"]["reasons"]] == ["WITHIN_MANDATE"]

    last = outs[5]
    assert last["total_net"] == 560.0 < mandate["per_order_eur"]
    assert sum(o["total_net"] for o in outs[:5]) + last["total_net"] > mandate["per_week_eur"]
    assert last["status"] == "pending_approval"
    assert last["decision"]["outcome"] == "needs_approval"
    assert [r["code"] for r in last["decision"]["reasons"]] == ["OVER_WEEK_LIMIT"]


def test_admin_mandate_update_flips_decision(c):
    key = "demo-hotel-aurora"
    o1 = order(c, key, "milk-valio", 1000)
    assert o1["total_net"] == 1600.0
    assert o1["decision"]["outcome"] == "auto" and o1["status"] == "confirmed"

    current = c.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN).json()
    updated = {**current, "per_order_eur": 1000.0}
    r = c.put("/v1/mandate", headers=ADMIN, json=updated)
    assert r.status_code == 200, r.text
    assert r.json()["per_order_eur"] == 1000.0

    o2 = order(c, key, "milk-valio", 1000)
    assert o2["total_net"] == 1600.0
    assert o2["status"] == "pending_approval"
    assert o2["decision"]["outcome"] == "needs_approval"
    assert "OVER_ORDER_LIMIT" in [x["code"] for x in o2["decision"]["reasons"]]

    audit_resp = c.get("/v1/audit", headers={"X-Agent-Key": key})
    assert audit_resp.status_code == 200, audit_resp.text
    events = audit_resp.json()["events"]
    assert any(
        e["action"] == "mandate_updated"
        and "2500.0 -> 1000.0" in e["why"]
        and e["actor"] == "human:admin"
        for e in events
    )


def test_substitution_policy_modes(c):
    o_ask = order(c, "demo-hospital-tays", "milk-valio", 10, sub_for="milk-orig")
    assert o_ask["status"] == "pending_approval" and o_ask["substitutions"]
    r_ask = next(x for x in o_ask["decision"]["reasons"] if x["code"] == "SUBSTITUTION_NEEDS_APPROVAL")
    assert "Valio lactose-free milk 1 l" in r_ask["text"] and "in place of" in r_ask["text"] and "milk-orig" in r_ask["text"]
    assert "Valio whole milk 1 l" in o_ask["substitutions"][0]["reason"]
    assert "Valio lactose-free milk 1 l" in o_ask["substitutions"][0]["reason"]

    o_pref = order(c, "demo-hotel-aurora", "milk-valio", 10, sub_for="milk-orig")
    assert o_pref["decision"]["outcome"] == "auto" and o_pref["status"] == "confirmed"
    assert o_pref["substitutions"]

    o_other = order(c, "demo-hotel-aurora", "oat-other", 10, sub_for="milk-orig")
    assert o_other["status"] == "pending_approval"
    assert o_other["decision"]["outcome"] == "needs_approval"
    assert "SUBSTITUTION_NEEDS_APPROVAL" in [x["code"] for x in o_other["decision"]["reasons"]]

    current = c.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN).json()
    r = c.put("/v1/mandate", headers=ADMIN, json={**current, "substitutions": "never"})
    assert r.status_code == 200, r.text

    o_never = order(c, "demo-hotel-aurora", "milk-valio", 10, sub_for="milk-orig")
    assert o_never["status"] == "blocked"
    assert o_never["decision"]["outcome"] == "blocked"
    assert "SUBSTITUTION_NOT_ALLOWED" in [x["code"] for x in o_never["decision"]["reasons"]]
