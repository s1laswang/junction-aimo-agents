import csv
from datetime import date, timedelta
import hashlib
import json
import warnings

from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet
import pytest

from aimo import audit, catalog, offers, parse, valio
from aimo.app import app

warnings.filterwarnings("ignore", message=".*EdDSA is deprecated.*")

AGENT_KEYS = {
    "hotel-aurora": {"X-Agent-Key": "demo-hotel-aurora"},
    "hospital-tays": {"X-Agent-Key": "demo-hospital-tays"},
    "school-hervanta": {"X-Agent-Key": "demo-school-hervanta"},
}
ADMIN_HEADER = {"X-Admin-Key": "demo-admin"}
AUDIT_FIELDS = ("action", "actor", "customer", "detail", "seq", "subject", "ts", "why")


def _synkka_item(pid, fi_name, en_name, brand="Valio", unit="PRK", volume_l=1.0, weight_kg=None, claims=None, allergens=None):
    conversions = []
    if volume_l is not None:
        conversions.append({"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": volume_l, "unit": "ltr"}})
    if weight_kg is not None:
        conversions.append({"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"value": weight_kg, "unit": "KGM"}})
    classifications = []
    if allergens:
        classifications.append({"name": "allergen", "values": allergens})
    if claims:
        classifications.append({"name": "nutritionalClaim", "values": [{"id": x} for x in claims]})
    return {
        "id": pid,
        "deleted": False,
        "category": "10400",
        "vendorName": "VALIO OY",
        "countryOfOrigin": "FI",
        "temperatureCondition": 4,
        "salesUnit": unit,
        "baseUnit": unit,
        "units": [{"unitId": unit, "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": [{"language": "fi", "value": fi_name}, {"language": "en", "value": en_name}],
            "classifications": classifications,
            "unitConversions": conversions,
        },
    }


def _write_mini_catalog(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    items = [
        _synkka_item(
            "1001",
            "Valio Eila kevytmaito laktoositon 1 l",
            "Valio Eila lactose free milk 1 l",
            brand="Valio",
            volume_l=1.0,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
        ),
        _synkka_item(
            "1002",
            "Valio kevytmaito UHT laktoositon 1 l",
            "Valio low fat milk UHT lactose free 1 l",
            brand="Valio",
            volume_l=1.0,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
        ),
        _synkka_item(
            "2001",
            "Myllyn Paras lasagnelevy 1 kg",
            "Lasagne pasta sheets 1 kg",
            brand="Valio",
            unit="PKT",
            volume_l=None,
            weight_kg=1.0,
            allergens=[{"id": "AW", "unit": "CONTAINS"}],
        ),
        _synkka_item(
            "2002",
            "Valio voi suolaton 1 kg",
            "Valio unsalted butter 1 kg",
            brand="Valio",
            unit="PKT",
            volume_l=None,
            weight_kg=1.0,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
        ),
    ]
    (data_dir / "valio_aimo_junction26_product_data.json").write_text(json.dumps(items), encoding="utf-8")
    with open(data_dir / "valio_aimo_junction26_delivery_reliability.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["productId", "deliveryReliability"])
        for pid, rel in [("1001", "0.98"), ("1002", "0.96"), ("2001", "0.95"), ("2002", "0.97")]:
            w.writerow([pid, rel])
    with open(data_dir / "valio_aimo_junction26_carbon_footprint.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["productId", "AVG kg CO2-eq/kg"])
        for pid, co2 in [("1001", "1.10"), ("1002", "1.15"), ("2001", "1.30"), ("2002", "8.40")]:
            w.writerow([pid, co2])


def _canon_js(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _browser_hash(event):
    body = {k: event[k] for k in AUDIT_FIELDS}
    return hashlib.sha256((event["prev_hash"] + _canon_js(body)).encode("utf-8")).hexdigest()


@pytest.fixture
def ui_client(tmp_path, monkeypatch):
    warnings.filterwarnings("ignore", message=".*EdDSA is deprecated.*")
    data_dir = tmp_path / "mini_data"
    _write_mini_catalog(data_dir)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "ui_e2e.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "ui_keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setattr(parse, "_llm_parse", lambda *a, **kw: None)
    catalog.reset()
    offers.clear_simulation()
    net_prices = {"1001": 1.20, "1002": 1.30, "2001": 2.50, "2002": 6.80}
    stock_amounts = {"1001": 3000.0, "1002": 3000.0, "2001": 500.0, "2002": 500.0}

    def fake_price(customer, day, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": net_prices[str(pid)]},
                "priceWithTax": {"value": round(net_prices[str(pid)] * 1.14, 2)},
            }
            for pid in ids
            if str(pid) in net_prices
        }

    def fake_availability(customer, day, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "closingTime": "2026-11-12T16:00:00+0200",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": stock_amounts[str(pid)],
                        "availableDate": day.isoformat(),
                        "salesUnit": "PRK",
                    }
                ],
            }
            for pid in ids
            if str(pid) in stock_amounts
        }

    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)
    with TestClient(app) as client:
        yield client
    offers.clear_simulation()
    catalog.reset()


def test_shell_and_auth_contract(ui_client):
    for route in ["/", "/console.html", "/inbox.html", "/audit.html", "/admin.html", "/style.css", "/shell.js"]:
        res = ui_client.get(route)
        assert res.status_code == 200 and res.text

    expected_limits = {
        "hotel-aurora": ("buyer-aurora", 2500.0, 15000.0, ["Laura Virtanen", "Mikko Heikkinen"]),
        "hospital-tays": ("buyer-tays", 1500.0, 8000.0, ["Helena Korhonen", "Antti Lahtinen"]),
        "school-hervanta": ("buyer-hervanta", 600.0, 3000.0, ["Sari Nieminen"]),
    }
    for customer, headers in AGENT_KEYS.items():
        r = ui_client.get("/v1/mandate", headers=headers)
        assert r.status_code == 200
        body = r.json()
        agent_id, per_order, per_week, approvers = expected_limits[customer]
        assert body["customer"] == customer
        assert body["agent_id"] == agent_id
        assert body["per_order_eur"] == per_order
        assert body["per_week_eur"] == per_week
        assert body["approvers"] == approvers
        assert isinstance(body["exclude_allergens"], list)
        assert body["substitutions"] in ("never", "within_prefs", "ask")

    unauth = ui_client.get("/v1/mandate")
    assert unauth.status_code == 401
    assert set(unauth.json()) >= {"code", "text"}

    cross = ui_client.get("/v1/mandate?customer=hospital-tays", headers=AGENT_KEYS["hotel-aurora"])
    assert cross.status_code == 403
    assert cross.json()["code"] == "forbidden" and cross.json()["text"]

    for customer in expected_limits:
        r_admin = ui_client.get(f"/v1/mandate?customer={customer}", headers=ADMIN_HEADER)
        assert r_admin.status_code == 200
        assert r_admin.json()["customer"] == customer

    card = ui_client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    card_mandates = card.json()["policies"]["mandates"]
    assert {m["customer"] for m in card_mandates} == set(expected_limits)

    assert ui_client.get("/v1/approvals", headers=ADMIN_HEADER).status_code == 200
    assert ui_client.get("/v1/audit", headers=ADMIN_HEADER).status_code == 200


def test_console_journey_and_stockout_simulation_contract(ui_client):
    headers = AGENT_KEYS["hotel-aurora"]
    deliver_by = (date.today() + timedelta(days=4)).isoformat()

    r_parse = ui_client.post(
        "/v1/needs/parse",
        json={"text": f"I need 200 litres of lactose-free milk by {deliver_by}", "customer": "hotel-aurora"},
        headers=headers,
    )
    assert r_parse.status_code == 200
    need = r_parse.json()
    assert need["customer"] == "hotel-aurora"
    assert need["deliver_by"] == deliver_by
    assert len(need["items"]) == 1
    item0 = need["items"][0]
    assert item0["query"] == "milk"
    assert item0["quantity"] == 200.0
    assert item0["unit"] == "L"
    assert item0["constraints"]["lactose_free"] is True
    assert isinstance(need.get("summary"), str) and need["summary"]

    r_offers = ui_client.post("/v1/offers", json=need, headers=headers)
    assert r_offers.status_code == 200
    off = r_offers.json()
    assert len(off["items"]) == 1
    grp = off["items"][0]
    assert grp["shortfall"] == 0
    assert len(grp["offers"]) >= 1
    picked = grp["offers"][0]
    for key in (
        "product_id",
        "product",
        "units_needed",
        "unit_price_net",
        "total_net",
        "total_gross",
        "available_amount",
        "available_date",
        "closing_time",
        "sales_block",
        "reliability",
        "co2_per_kg",
        "match_score",
        "tier",
        "substitute_for",
        "notes",
    ):
        assert key in picked
    for pkey in ("name", "brand", "sales_unit", "unit_size"):
        assert pkey in picked["product"]
    assert picked["product_id"] == "1001"
    assert picked["units_needed"] == 200.0

    quote_lines_req = [{"product_id": picked["product_id"], "quantity": picked["units_needed"]}]
    r_quote = ui_client.post(
        "/v1/quotes",
        json={"offers": quote_lines_req, "delivery_date": need["deliver_by"]},
        headers=headers,
    )
    assert r_quote.status_code == 200
    quote = r_quote.json()
    for qkey in ("quote_id", "jws", "lines", "total_net", "total_gross", "valid_until", "delivery_date", "customer", "agent_id", "currency"):
        assert qkey in quote
    assert quote["quote_id"] and quote["jws"].count(".") == 2
    assert len(quote["lines"]) == 1
    assert quote["lines"][0]["product_id"] == "1001"
    assert quote["total_net"] == 240.0
    assert quote["total_gross"] > quote["total_net"]

    r_order = ui_client.post(
        "/v1/orders",
        json={"quote_id": quote["quote_id"], "jws": quote["jws"]},
        headers=headers,
    )
    assert r_order.status_code == 200
    order = r_order.json()
    for okey in ("order_id", "quote_id", "customer", "agent_id", "lines", "total_net", "status", "decision", "substitutions", "delivery_date", "valid_until"):
        assert okey in order
    assert order["status"] == "confirmed"
    assert order["decision"]["outcome"] == "auto"
    assert isinstance(order["decision"]["reasons"], list) and order["decision"]["reasons"]
    assert set(order["decision"]["reasons"][0]) >= {"code", "text"}
    assert order["substitutions"] == []

    r_sim = ui_client.post(
        "/v1/admin/simulate",
        json={"product_id": "1001", "available_amount": 0},
        headers=ADMIN_HEADER,
    )
    assert r_sim.status_code == 200 and r_sim.json() == {"ok": True}

    reopened = ui_client.get(f"/v1/orders/{order['order_id']}", headers=headers)
    assert reopened.status_code == 200
    ro = reopened.json()
    assert ro["status"] == "pending_approval"
    assert ro["decision"]["outcome"] == "needs_approval"
    assert ro["decision"]["reasons"][0]["code"] == "STOCKOUT"
    assert len(ro["substitutions"]) == 1
    assert ro["substitutions"][0]["original_id"] == "1001"
    assert ro["substitutions"][0]["substitute_id"] == "1002"
    assert ro["substitutions"][0]["reason"]

    r_offers_after = ui_client.post("/v1/offers", json=need, headers=headers)
    assert r_offers_after.status_code == 200
    alt_pick = r_offers_after.json()["items"][0]["offers"][0]
    assert alt_pick["product_id"] == "1002"
    q_after = ui_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": alt_pick["product_id"], "quantity": alt_pick["units_needed"]}], "delivery_date": need["deliver_by"]},
        headers=headers,
    ).json()
    o_after = ui_client.post(
        "/v1/orders",
        json={"quote_id": q_after["quote_id"], "jws": q_after["jws"]},
        headers=headers,
    ).json()
    assert o_after["status"] == "confirmed"
    assert o_after["lines"][0]["product_id"] == "1002"

    r_clear = ui_client.delete("/v1/admin/simulate", headers=ADMIN_HEADER)
    assert r_clear.status_code == 200 and r_clear.json() == {"ok": True}

    r_offers_restored = ui_client.post("/v1/offers", json=need, headers=headers)
    assert r_offers_restored.status_code == 200
    assert r_offers_restored.json()["items"][0]["offers"][0]["product_id"] == "1001"

    r_menu = ui_client.post(
        "/v1/needs/parse",
        json={"text": f"We need to cook lasagne for 40 portions by {deliver_by}.", "customer": "hotel-aurora"},
        headers=headers,
    )
    assert r_menu.status_code == 200
    assert len(r_menu.json()["items"]) >= 3


def test_inbox_journey_approval_and_rejection_contract(ui_client):
    headers = AGENT_KEYS["hotel-aurora"]
    deliver_by = (date.today() + timedelta(days=4)).isoformat()

    q_over = ui_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "1001", "quantity": 2200}], "delivery_date": deliver_by},
        headers=headers,
    ).json()
    assert q_over["total_net"] == 2640.0

    r_pending = ui_client.post(
        "/v1/orders",
        json={"quote_id": q_over["quote_id"], "jws": q_over["jws"]},
        headers=headers,
    )
    assert r_pending.status_code == 200
    pending_order = r_pending.json()
    oid = pending_order["order_id"]
    assert pending_order["status"] == "pending_approval"
    assert pending_order["decision"]["outcome"] == "needs_approval"
    assert "OVER_ORDER_LIMIT" in [r["code"] for r in pending_order["decision"]["reasons"]]

    for path in ("/v1/approvals", "/v1/approvals?status=pending", "/v1/approvals?customer=hotel-aurora"):
        r_list = ui_client.get(path, headers=ADMIN_HEADER)
        assert r_list.status_code == 200
        items = r_list.json()
        hit = [o for o in items if o["order_id"] == oid]
        assert len(hit) == 1
        card = hit[0]
        for ikey in (
            "order_id",
            "quote_id",
            "customer",
            "agent_id",
            "status",
            "lines",
            "total_net",
            "substitutions",
            "decision",
            "created_at",
            "updated_at",
            "delivery_date",
            "valid_until",
        ):
            assert ikey in card
        for lkey in ("product_id", "name", "quantity", "sales_unit", "unit_price_net", "line_total_net"):
            assert lkey in card["lines"][0]

    r_wrong_by = ui_client.post(
        f"/v1/approvals/{oid}",
        json={"approve": True, "by": "Helena Korhonen", "note": "wrong customer approver"},
        headers=ADMIN_HEADER,
    )
    assert r_wrong_by.status_code == 403
    assert r_wrong_by.json()["code"] == "NOT_APPROVER" and r_wrong_by.json()["text"]

    r_approve = ui_client.post(
        f"/v1/approvals/{oid}",
        json={"approve": True, "by": "Laura Virtanen", "note": "ok"},
        headers=ADMIN_HEADER,
    )
    assert r_approve.status_code == 200
    decided = r_approve.json()
    assert decided["order_id"] == oid
    assert decided["status"] == "confirmed"
    assert [o for o in ui_client.get("/v1/approvals", headers=ADMIN_HEADER).json() if o["order_id"] == oid] == []

    r_decided_audit = ui_client.get("/v1/audit?customer=hotel-aurora", headers=ADMIN_HEADER)
    assert r_decided_audit.status_code == 200
    app_events = [
        e for e in r_decided_audit.json()["events"]
        if e["action"] == "approval.approved" and e["subject"] == oid
    ]
    assert len(app_events) == 1
    assert app_events[0]["actor"] == "human:Laura Virtanen"

    q_rej = ui_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "1001", "quantity": 2300}], "delivery_date": deliver_by},
        headers=headers,
    ).json()
    o_rej = ui_client.post(
        "/v1/orders",
        json={"quote_id": q_rej["quote_id"], "jws": q_rej["jws"]},
        headers=headers,
    ).json()
    r_reject = ui_client.post(
        f"/v1/approvals/{o_rej['order_id']}",
        json={"approve": False, "by": "Laura Virtanen", "note": "defer to next week"},
        headers=ADMIN_HEADER,
    )
    assert r_reject.status_code == 200
    assert r_reject.json()["status"] == "rejected"


def test_audit_journey_chain_and_jwks_signature_contract(ui_client):
    headers = AGENT_KEYS["hotel-aurora"]
    deliver_by = (date.today() + timedelta(days=4)).isoformat()

    quote = ui_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "1001", "quantity": 2200}], "delivery_date": deliver_by},
        headers=headers,
    ).json()
    order = ui_client.post(
        "/v1/orders",
        json={"quote_id": quote["quote_id"], "jws": quote["jws"]},
        headers=headers,
    ).json()
    oid = order["order_id"]
    assert ui_client.post(
        f"/v1/approvals/{oid}",
        json={"approve": True, "by": "Laura Virtanen", "note": "approved for breakfast"},
        headers=ADMIN_HEADER,
    ).status_code == 200

    r_full = ui_client.get("/v1/audit", headers=ADMIN_HEADER)
    assert r_full.status_code == 200
    full = r_full.json()
    assert set(full) >= {"events", "text", "verified", "checkpoint"}
    assert full["verified"] == {"ok": True, "bad_seq": None}
    assert isinstance(full["text"], str) and full["text"]

    events = full["events"]
    assert len(events) >= 5
    prev = "0" * 64
    for idx, ev in enumerate(events, 1):
        assert ev["seq"] == idx
        assert ev["prev_hash"] == prev
        assert _browser_hash(ev) == ev["hash"]
        prev = ev["hash"]

    cp = full["checkpoint"]
    assert set(cp) >= {"seq", "hash", "ts", "jws"}
    assert cp["seq"] == events[-1]["seq"]
    assert cp["hash"] == events[-1]["hash"]
    assert cp["ts"] == events[-1]["ts"]

    r_scoped = ui_client.get(f"/v1/audit?order_id={oid}", headers=ADMIN_HEADER)
    assert r_scoped.status_code == 200
    scoped = r_scoped.json()
    assert scoped["verified"] == {"ok": True, "bad_seq": None}
    assert isinstance(scoped["text"], str) and scoped["text"]
    scoped_actions = {e["action"] for e in scoped["events"]}
    assert {"quote.issued", "quote.redeemed", "order.placed", "order.pending_approval", "approval.approved", "order.confirmed"} <= scoped_actions
    for ev in scoped["events"]:
        assert _browser_hash(ev) == ev["hash"]

    r_receipt = ui_client.get(f"/v1/orders/{oid}", headers=ADMIN_HEADER)
    assert r_receipt.status_code == 200
    rc = r_receipt.json()
    assert rc["order_id"] == oid and rc["quote_id"] == quote["quote_id"] and rc["status"] == "confirmed"

    r_jwks = ui_client.get("/.well-known/jwks.json")
    assert r_jwks.status_code == 200
    jwks_doc = r_jwks.json()
    assert "keys" in jwks_doc and len(jwks_doc["keys"]) >= 1
    pub = jwks_doc["keys"][0]
    assert pub["kty"] == "OKP" and pub["crv"] == "Ed25519" and pub["kid"] and pub["x"]
    assert "d" not in pub

    keyset = KeySet.import_key_set(jwks_doc)
    cp_tok = jws.deserialize_compact(cp["jws"], keyset, algorithms=["EdDSA"])
    cp_payload = json.loads(cp_tok.payload)
    assert cp_payload == {"hash": cp["hash"], "seq": cp["seq"], "ts": cp["ts"]}
    assert audit.verify_checkpoint(cp["jws"], jwks_doc)["seq"] == cp["seq"]

    q_tok = jws.deserialize_compact(quote["jws"], keyset, algorithms=["EdDSA"])
    q_payload = json.loads(q_tok.payload)
    assert q_payload["quote_id"] == quote["quote_id"]
    assert q_payload["total_net"] == quote["total_net"]


def test_admin_journey_mandate_update_and_validation_contract(ui_client):
    r_get = ui_client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN_HEADER)
    assert r_get.status_code == 200
    mandate = r_get.json()
    for mkey in (
        "customer",
        "agent_id",
        "per_order_eur",
        "per_week_eur",
        "exclude_allergens",
        "allowed_categories",
        "substitutions",
        "preferred_brands",
        "approvers",
    ):
        assert mkey in mandate

    updated_body = {
        **mandate,
        "per_order_eur": 3200.0,
        "approvers": ["Laura Virtanen", "Mikko Heikkinen", "Pekka Laine"],
    }
    r_put = ui_client.put("/v1/mandate", json=updated_body, headers=ADMIN_HEADER)
    assert r_put.status_code == 200
    saved = r_put.json()
    assert saved["per_order_eur"] == 3200.0
    assert saved["approvers"] == ["Laura Virtanen", "Mikko Heikkinen", "Pekka Laine"]

    r_admin_check = ui_client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN_HEADER)
    assert r_admin_check.status_code == 200
    assert r_admin_check.json()["per_order_eur"] == 3200.0
    assert "Pekka Laine" in r_admin_check.json()["approvers"]

    r_agent_check = ui_client.get("/v1/mandate", headers=AGENT_KEYS["hotel-aurora"])
    assert r_agent_check.status_code == 200
    assert r_agent_check.json()["per_order_eur"] == 3200.0

    r_audit = ui_client.get("/v1/audit?customer=hotel-aurora", headers=ADMIN_HEADER)
    assert r_audit.status_code == 200
    mand_events = [e for e in r_audit.json()["events"] if e["action"] == "mandate_updated"]
    assert len(mand_events) == 1
    assert mand_events[0]["seq"] >= 1 and mand_events[0]["ts"] and mand_events[0]["actor"]

    bad_over_week = {**updated_body, "per_order_eur": 20000.0, "per_week_eur": 15000.0}
    r_bad = ui_client.put("/v1/mandate", json=bad_over_week, headers=ADMIN_HEADER)
    assert r_bad.status_code in (409, 422)
    err_body = r_bad.json()
    assert err_body["code"] == "INVALID_MANDATE"
    assert isinstance(err_body["text"], str) and err_body["text"]

    bad_non_pos = {**updated_body, "per_order_eur": 0.0}
    r_zero = ui_client.put("/v1/mandate", json=bad_non_pos, headers=ADMIN_HEADER)
    assert r_zero.status_code in (409, 422)
    assert set(r_zero.json()) >= {"code", "text"}

    r_forbidden = ui_client.put("/v1/mandate", json=updated_body, headers=AGENT_KEYS["hotel-aurora"])
    assert r_forbidden.status_code == 403
    assert set(r_forbidden.json()) >= {"code", "text"}
