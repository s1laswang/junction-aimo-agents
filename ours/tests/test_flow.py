import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet

from aimo import catalog, offers as offers_mod, valio
from aimo.app import app
from aimo.models import Product, UnitSize

TEXT = "I need 1,000 litres of lactose-free milk by Monday"


def call(client, method, path, key=None, **kw):
    kw.setdefault("headers", {"X-Agent-Key": key})
    r = client.request(method, path, **kw)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


@pytest.mark.skipif(
    not (Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json").exists()
    or (
        not os.environ.get("VALIO_API_BASE")
        and not any(Path(os.environ.get("AIMO_CACHE_DIR", "var/cache")).glob("price__hotel-aurora__*.json"))
    ),
    reason="needs product data and VALIO_API_BASE or cached prices",
)
def test_milk_flow(client, agent_key):
    need = call(client, "POST", "/v1/needs/parse", agent_key, json={"text": TEXT, "customer": "hotel-aurora"})
    assert need["customer"] == "hotel-aurora"
    item = need["items"][0]
    assert item["quantity"] == 1000
    assert item["constraints"]["lactose_free"] is True

    res = call(client, "POST", "/v1/offers", agent_key, json=need)
    offers = res["items"][0]["offers"]
    assert offers
    for o in offers:
        txt = (o["product"]["name"] + " ".join(o["product"]["labels"])).lower()
        assert "lactose" in txt or "laktoositon" in txt or "laktos" in txt
    pick = offers[0]

    quote = call(client, "POST", "/v1/quotes", agent_key, json={
        "offers": [{"product_id": pick["product_id"], "quantity": pick["units_needed"]}],
        "delivery_date": need["deliver_by"],
    })
    assert quote["lines"] and quote["total_net"] > 0
    jwks = client.get("/.well-known/jwks.json").json()
    assert jws.deserialize_compact(quote["jws"], KeySet.import_key_set(jwks), algorithms=["EdDSA"])

    order = call(client, "POST", "/v1/orders", agent_key, json={"quote_id": quote["quote_id"], "jws": quote["jws"]})
    assert order["decision"]["outcome"] in ("auto", "needs_approval", "blocked")
    assert order["decision"]["reasons"]
    if order["status"] == "pending_approval":
        order = call(
            client,
            "POST",
            f"/v1/approvals/{order['order_id']}",
            headers={"X-Admin-Key": "demo-admin"},
            json={"approve": True, "by": "Laura Virtanen", "note": "ok"},
        )
        assert order["status"] in ("approved", "placed", "confirmed")
    got = call(client, "GET", f"/v1/orders/{order['order_id']}", agent_key)
    assert got["order_id"] == order["order_id"]

    audit = call(client, "GET", f"/v1/audit?order_id={order['order_id']}", agent_key)
    events = audit if isinstance(audit, list) else audit["events"]
    assert events
    assert all(e["why"] for e in events)
    full = call(client, "GET", "/v1/audit", agent_key)
    chain = full if isinstance(full, list) else full["events"]
    for a, b in zip(chain, chain[1:]):
        assert b["prev_hash"] == a["hash"]


def test_milk_flow_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.setattr(offers_mod, "_overrides", {})

    prod = Product(
        product_id="464073",
        name="Valio laktoositon maito 1 L",
        brand="Valio",
        vendor="Valio Oy",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose"],
        origin="FI",
    )
    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [prod])
    monkeypatch.setattr(catalog, "get", lambda pid: prod if str(pid) == prod.product_id else None)
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if str(pid) == prod.product_id else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.1 if str(pid) == prod.product_id else None)
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, day, ids: {
            str(i): {
                "productId": str(i),
                "currency": "EUR",
                "catalogPrice": {"value": 3.0},
                "priceWithTax": {"value": 3.42},
            }
            for i in ids
            if str(i) == prod.product_id
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, day, ids: {
            str(i): {
                "productId": str(i),
                "closingTime": "2026-11-12T23:59:59+02:00",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 5000.0,
                        "availableDate": day.isoformat(),
                        "salesUnit": "LTR",
                    }
                ],
            }
            for i in ids
            if str(i) == prod.product_id
        },
    )

    agent_key = "demo-hotel-aurora"
    c = TestClient(app)

    need = call(c, "POST", "/v1/needs/parse", agent_key, json={"text": TEXT, "customer": "hotel-aurora"})
    assert need["customer"] == "hotel-aurora"
    item = need["items"][0]
    assert item["quantity"] == 1000
    assert item["constraints"]["lactose_free"] is True

    res = call(c, "POST", "/v1/offers", agent_key, json=need)
    offers = res["items"][0]["offers"]
    assert offers and res["items"][0]["shortfall"] == 0
    for o in offers:
        txt = (o["product"]["name"] + " ".join(o["product"]["labels"])).lower()
        assert "lactose" in txt or "laktoositon" in txt or "laktos" in txt
    pick = offers[0]
    assert pick["product_id"] == prod.product_id and pick["units_needed"] == 1000
    assert pick["reliability"] == 0.99 and pick["co2_per_kg"] == 1.1

    quote = call(
        c,
        "POST",
        "/v1/quotes",
        agent_key,
        json={
            "offers": [{"product_id": pick["product_id"], "quantity": pick["units_needed"]}],
            "delivery_date": need["deliver_by"],
        },
    )
    assert quote["lines"] and quote["total_net"] == 3000.0 and quote["total_gross"] == 3420.0
    jwks = c.get("/.well-known/jwks.json").json()
    ks = KeySet.import_key_set(jwks)
    verified_jws = jws.deserialize_compact(quote["jws"], ks, algorithms=["EdDSA"])
    assert json.loads(verified_jws.payload)["quote_id"] == quote["quote_id"]

    order = call(c, "POST", "/v1/orders", agent_key, json={"quote_id": quote["quote_id"], "jws": quote["jws"]})
    assert order["status"] == "pending_approval"
    assert order["decision"]["outcome"] == "needs_approval"
    assert any(r["code"] == "OVER_ORDER_LIMIT" for r in order["decision"]["reasons"])

    order = call(
        c,
        "POST",
        f"/v1/approvals/{order['order_id']}",
        headers={"X-Admin-Key": "demo-admin"},
        json={"approve": True, "by": "Laura Virtanen", "note": "ok"},
    )
    assert order["status"] in ("approved", "placed", "confirmed")

    got = call(c, "GET", f"/v1/orders/{order['order_id']}", agent_key)
    assert got["order_id"] == order["order_id"]
    assert got["status"] == "confirmed"

    audit = call(c, "GET", f"/v1/audit?order_id={order['order_id']}", agent_key)
    events = audit["events"]
    assert events and all(e["why"] for e in events)

    full = call(c, "GET", "/v1/audit", agent_key)
    assert full["verified"]["ok"] is True and full["verified"]["bad_seq"] is None
    chain = full["events"]
    assert chain and chain[0]["prev_hash"] == "0" * 64
    for a, b in zip(chain, chain[1:]):
        assert b["prev_hash"] == a["hash"]
    cp = full["checkpoint"]
    assert cp["seq"] == chain[-1]["seq"] and cp["hash"] == chain[-1]["hash"]
    cp_jws = jws.deserialize_compact(cp["jws"], ks, algorithms=["EdDSA"])
    assert json.loads(cp_jws.payload)["hash"] == chain[-1]["hash"]
