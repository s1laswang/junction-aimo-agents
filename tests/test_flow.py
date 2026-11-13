import pytest
from joserfc import jws
from joserfc.jwk import KeySet

TEXT = "I need 1,000 litres of lactose-free milk by Monday"


def call(client, method, path, key, **kw):
    r = client.request(method, path, headers={"X-Agent-Key": key}, **kw)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


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
        order = call(client, "POST", f"/v1/approvals/{order['order_id']}", agent_key, json={"approve": True, "by": "qa", "note": "ok"})
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
