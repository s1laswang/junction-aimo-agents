import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import warnings
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import OKPKey
import pytest

from aimo import audit, catalog, keys, mandates, offers, quotes
from aimo.app import app
from aimo.models import Product, Quote, QuoteLine

HELSINKI = ZoneInfo("Europe/Helsinki")

EXPECTED_TOOLS = [
    ("search_products", "GET", "/v1/products"),
    ("get_product", "GET", "/v1/products/{id}"),
    ("parse_request", "POST", "/v1/needs/parse"),
    ("get_offers", "POST", "/v1/offers"),
    ("request_quote", "POST", "/v1/quotes"),
    ("place_order", "POST", "/v1/orders"),
    ("get_order", "GET", "/v1/orders/{id}"),
    ("list_orders", "GET", "/v1/orders"),
    ("list_approvals", "GET", "/v1/approvals"),
    ("decide_approval", "POST", "/v1/approvals/{order_id}"),
    ("get_audit_trail", "GET", "/v1/audit"),
    ("get_mandate", "GET", "/v1/mandate"),
]


@pytest.fixture(autouse=True)
def isolate_discovery_env(tmp_path, monkeypatch):
    warnings.filterwarnings("ignore", message="EdDSA is deprecated")
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    keys._cache.clear()
    yield
    keys._cache.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _assert_public_ed25519_jwk(entry: dict) -> None:
    assert isinstance(entry, dict)
    assert "d" not in entry
    assert set(entry.keys()) <= {"kty", "crv", "x", "kid", "alg", "use"}
    assert entry.get("kty") == "OKP"
    assert entry.get("crv") == "Ed25519"
    assert entry.get("alg") == "EdDSA"
    if "use" in entry:
        assert entry["use"] == "sig"
    assert isinstance(entry.get("kid"), str) and entry["kid"].strip()
    assert isinstance(entry.get("x"), str) and entry["x"].strip()
    raw_pub = _b64url_decode(entry["x"])
    assert len(raw_pub) == 32
    imported = OKPKey.import_key(entry)
    assert imported.thumbprint() == entry["kid"]


def _verify_compact_jws_via_jwks_kid(token: str, jwks_doc: dict) -> tuple[dict, dict]:
    segments = token.split(".")
    assert len(segments) == 3 and all(segments)
    protected = json.loads(_b64url_decode(segments[0]))
    kid = protected.get("kid")
    assert isinstance(kid, str) and kid.strip()
    matches = [k for k in jwks_doc["keys"] if k.get("kid") == kid]
    assert len(matches) == 1
    _assert_public_ed25519_jwk(matches[0])
    pub_key = OKPKey.import_key(matches[0])
    verified = jws.deserialize_compact(token, pub_key, algorithms=["EdDSA"])
    assert verified.headers().get("alg") == "EdDSA"
    assert verified.headers().get("kid") == kid
    return protected, json.loads(verified.payload)


def _issue_synthetic_quote(customer: str = "hotel-aurora", agent_id: str = "buyer-aurora", qty: float = 10.0) -> Quote:
    delivery = datetime.now(HELSINKI).date() + timedelta(days=2)
    valid_until = datetime.now(timezone.utc) + timedelta(hours=3)
    net = round(qty * 1.50, 2)
    gross = round(net * 1.14, 2)
    line = QuoteLine(
        product_id="synth-milk-1l",
        name="Synthetic Lactose-Free Milk 1L",
        quantity=qty,
        sales_unit="L",
        unit_price_net=1.50,
        line_total_net=net,
    )
    return quotes.issue(customer, agent_id, [line], delivery, valid_until, gross)


def test_public_discovery_endpoints_zero_auth_and_json(client):
    routes = [
        "/health",
        "/.well-known/agent-card.json",
        "/.well-known/jwks.json",
    ]
    for route in routes:
        resp = client.get(route)
        sent_headers = {k.lower() for k in resp.request.headers.keys()}
        assert "x-agent-key" not in sent_headers
        assert "x-admin-key" not in sent_headers
        assert "authorization" not in sent_headers
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert isinstance(body, dict)

    assert client.get("/health").json() == {"ok": True}


def test_agent_card_metadata_endpoints_auth_and_tools(client):
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()

    assert isinstance(card.get("name"), str) and card["name"].strip()
    assert isinstance(card.get("description"), str) and card["description"].strip()

    endpoints = card["endpoints"]
    assert endpoints["rest"] == "/v1" or endpoints["rest"].endswith("/v1")
    assert endpoints["mcp"] == "/mcp" or endpoints["mcp"].endswith("/mcp")
    assert endpoints["openapi"] == "/openapi.json" or endpoints["openapi"].endswith("/openapi.json")
    assert endpoints["jwks"] == "/.well-known/jwks.json" or endpoints["jwks"].endswith("/.well-known/jwks.json")

    auth = card["auth"]
    assert auth["agent"]["name"] == "X-Agent-Key"
    assert auth["agent"]["in"] == "header"
    assert auth["agent"]["type"] == "apiKey"
    assert auth["admin"]["name"] == "X-Admin-Key"
    assert auth["admin"]["in"] == "header"
    assert auth["admin"]["type"] == "apiKey"

    tools = card["tools"]
    assert isinstance(tools, list) and len(tools) == 12
    assert [(t["name"], t["method"], t["route"]) for t in tools] == EXPECTED_TOOLS
    for tool in tools:
        assert isinstance(tool.get("description"), str) and tool["description"].strip()


def test_agent_card_policies_and_mandates_match_store(client):
    card = client.get("/.well-known/agent-card.json").json()
    policies = card["policies"]

    assert isinstance(policies.get("quotes"), str) and policies["quotes"].strip()

    qv = policies["quote_verification"]
    assert qv["format"] == "JWS compact"
    assert qv["alg"] == "EdDSA"
    assert qv["curve"] == "Ed25519"
    assert qv["keys"] == "/.well-known/jwks.json" or qv["keys"].endswith("/.well-known/jwks.json")
    assert isinstance(qv.get("steps"), list) and len(qv["steps"]) > 0
    assert all(isinstance(step, str) and step.strip() for step in qv["steps"])
    steps_joined = " ".join(qv["steps"])
    assert "jwks.json" in steps_joined
    assert "kid" in steps_joined
    assert "EdDSA" in steps_joined

    assert policies["decisions"] == ["auto", "needs_approval", "blocked"]
    assert isinstance(policies.get("audit"), str) and "checkpoint" in policies["audit"]

    stored = mandates.all()
    assert [m.customer for m in stored] == ["hotel-aurora", "hospital-tays", "school-hervanta"]
    expected_mandates = [
        {
            "customer": m.customer,
            "agent_id": m.agent_id,
            "per_order_eur": m.per_order_eur,
            "per_week_eur": m.per_week_eur,
            "exclude_allergens": m.exclude_allergens,
            "substitutions": m.substitutions,
        }
        for m in stored
    ]
    assert policies["mandates"] == expected_mandates


def test_put_mandate_immediately_reflected_in_agent_card(client):
    admin_headers = {"X-Admin-Key": "demo-admin"}
    current = client.get("/v1/mandate?customer=hotel-aurora", headers=admin_headers)
    assert current.status_code == 200
    aurora = current.json()

    unauth = client.put("/v1/mandate", json={**aurora, "per_order_eur": 999.0})
    assert unauth.status_code == 401
    forbidden = client.put(
        "/v1/mandate",
        headers={"X-Agent-Key": "demo-hotel-aurora"},
        json={**aurora, "per_order_eur": 999.0},
    )
    assert forbidden.status_code == 403

    updated_payload = {
        **aurora,
        "per_order_eur": 1875.5,
        "per_week_eur": 11500.0,
        "exclude_allergens": ["soy", "celery"],
        "substitutions": "never",
    }
    put_resp = client.put("/v1/mandate", headers=admin_headers, json=updated_payload)
    assert put_resp.status_code == 200
    assert put_resp.json()["per_order_eur"] == 1875.5
    assert put_resp.json()["exclude_allergens"] == ["soy", "celery"]

    card_after = client.get("/.well-known/agent-card.json").json()
    by_customer = {m["customer"]: m for m in card_after["policies"]["mandates"]}
    assert set(by_customer.keys()) == {"hotel-aurora", "hospital-tays", "school-hervanta"}
    assert by_customer["hotel-aurora"]["per_order_eur"] == 1875.5
    assert by_customer["hotel-aurora"]["per_week_eur"] == 11500.0
    assert by_customer["hotel-aurora"]["exclude_allergens"] == ["soy", "celery"]
    assert by_customer["hotel-aurora"]["substitutions"] == "never"
    assert by_customer["hospital-tays"]["per_order_eur"] == 1500.0
    assert by_customer["school-hervanta"]["per_order_eur"] == 600.0


def test_jwks_public_key_invariants_across_rotations(client):
    initial = client.get("/.well-known/jwks.json").json()
    assert isinstance(initial.get("keys"), list) and len(initial["keys"]) == 1
    _assert_public_ed25519_jwk(initial["keys"][0])
    k0 = initial["keys"][0]["kid"]

    k1 = keys.rotate()
    k2 = keys.rotate()
    assert len({k0, k1, k2}) == 3

    rotated = client.get("/.well-known/jwks.json").json()
    assert len(rotated["keys"]) == 3
    assert rotated["keys"][0]["kid"] == k2
    assert {entry["kid"] for entry in rotated["keys"]} == {k0, k1, k2}
    for entry in rotated["keys"]:
        _assert_public_ed25519_jwk(entry)


def test_key_rotation_multi_jwk_quote_and_audit_checkpoint_verification(client):
    card = client.get("/.well-known/agent-card.json").json()
    qv = card["policies"]["quote_verification"]
    jwks_path = "/.well-known/jwks.json" if qv["keys"].endswith("/.well-known/jwks.json") else qv["keys"]

    q1 = _issue_synthetic_quote(qty=12.0)
    old_kid = keys.kid()
    cp_before = client.get("/v1/audit", headers={"X-Admin-Key": "demo-admin"}).json()["checkpoint"]

    new_kid = keys.rotate()
    assert new_kid != old_kid
    assert keys.kid() == new_kid

    q2 = _issue_synthetic_quote(qty=24.0)
    ev1 = audit.record("hotel-aurora", "agent:buyer-aurora", "parse.request", "hotel-aurora", "Parsed synthetic milk request.")
    ev2 = audit.record("hotel-aurora", "system", "order.confirmed", "ord_synth_1", "Confirmed synthetic delivery.")
    assert ev2.seq > ev1.seq

    jwks_resp = client.get(jwks_path)
    assert jwks_resp.status_code == 200
    assert jwks_resp.headers["content-type"].startswith("application/json")
    jwks_doc = jwks_resp.json()
    assert [k["kid"] for k in jwks_doc["keys"]] == [new_kid, old_kid]
    for k in jwks_doc["keys"]:
        _assert_public_ed25519_jwk(k)

    h1, payload1 = _verify_compact_jws_via_jwks_kid(q1.jws, jwks_doc)
    assert h1["kid"] == old_kid
    assert payload1 == q1.model_dump(mode="json", exclude={"jws"})

    h2, payload2 = _verify_compact_jws_via_jwks_kid(q2.jws, jwks_doc)
    assert h2["kid"] == new_kid
    assert payload2 == q2.model_dump(mode="json", exclude={"jws"})

    h_cp_before, payload_cp_before = _verify_compact_jws_via_jwks_kid(cp_before["jws"], jwks_doc)
    assert h_cp_before["kid"] == old_kid
    assert payload_cp_before == {k: v for k, v in cp_before.items() if k != "jws"}

    audit_resp = client.get("/v1/audit", headers={"X-Admin-Key": "demo-admin"})
    assert audit_resp.status_code == 200
    trail = audit_resp.json()
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    cp_after = trail["checkpoint"]
    h_cp_after, payload_cp_after = _verify_compact_jws_via_jwks_kid(cp_after["jws"], jwks_doc)
    assert h_cp_after["kid"] == new_kid
    assert payload_cp_after == {k: v for k, v in cp_after.items() if k != "jws"}
    assert payload_cp_after["seq"] == ev2.seq
    assert payload_cp_after["hash"] == ev2.hash

    new_pub = OKPKey.import_key(jwks_doc["keys"][0])
    old_pub = OKPKey.import_key(jwks_doc["keys"][1])
    with pytest.raises(JoseError):
        jws.deserialize_compact(q1.jws, new_pub, algorithms=["EdDSA"])
    with pytest.raises(JoseError):
        jws.deserialize_compact(q2.jws, old_pub, algorithms=["EdDSA"])

    seg_h, seg_p, _ = q2.jws.split(".")
    hs_header = _b64url_encode(json.dumps({"alg": "HS256", "kid": new_kid}, separators=(",", ":")).encode())
    hs_input = f"{hs_header}.{seg_p}"
    hs_sig = _b64url_encode(hmac.new(_b64url_decode(jwks_doc["keys"][0]["x"]), hs_input.encode(), hashlib.sha256).digest())
    with pytest.raises(JoseError):
        jws.deserialize_compact(f"{hs_input}.{hs_sig}", new_pub, algorithms=["EdDSA"])


def test_http_quotes_orders_and_audit_across_key_rotation_offline(client, monkeypatch):
    synth_product = Product(
        product_id="synth-milk-1l",
        name="Synthetic Lactose-Free Milk 1L",
        brand="Valio",
        category="10400",
        sales_unit="L",
        allergens=["milk"],
    )

    def fake_price_lines(customer, delivery_date, lines):
        out = [
            QuoteLine(
                product_id=item.product_id,
                name=synth_product.name,
                quantity=item.quantity,
                sales_unit="L",
                unit_price_net=1.50,
                line_total_net=round(item.quantity * 1.50, 2),
                substitute_for=item.substitute_for,
            )
            for item in lines
        ]
        net_sum = sum(x.line_total_net for x in out)
        return out, round(net_sum * 1.14, 2), datetime.now(timezone.utc) + timedelta(hours=4)

    monkeypatch.setattr(catalog, "get", lambda pid: synth_product if str(pid) == "synth-milk-1l" else None)
    monkeypatch.setattr(offers, "price_lines", fake_price_lines)

    agent_headers = {"X-Agent-Key": "demo-hotel-aurora"}
    delivery_day = (datetime.now(HELSINKI).date() + timedelta(days=2)).isoformat()

    q1_resp = client.post(
        "/v1/quotes",
        headers=agent_headers,
        json={"offers": [{"product_id": "synth-milk-1l", "quantity": 10}], "delivery_date": delivery_day},
    )
    assert q1_resp.status_code == 200
    q1_json = q1_resp.json()
    old_kid = keys.kid()

    new_kid = keys.rotate()
    assert new_kid != old_kid

    q2_resp = client.post(
        "/v1/quotes",
        headers=agent_headers,
        json={"offers": [{"product_id": "synth-milk-1l", "quantity": 20}], "delivery_date": delivery_day},
    )
    assert q2_resp.status_code == 200
    q2_json = q2_resp.json()

    o1_resp = client.post(
        "/v1/orders",
        headers=agent_headers,
        json={"quote_id": q1_json["quote_id"], "jws": q1_json["jws"]},
    )
    assert o1_resp.status_code == 200
    assert o1_resp.json()["status"] == "confirmed"

    o2_resp = client.post(
        "/v1/orders",
        headers=agent_headers,
        json={"quote_id": q2_json["quote_id"], "jws": q2_json["jws"]},
    )
    assert o2_resp.status_code == 200
    assert o2_resp.json()["status"] == "confirmed"

    jwks_doc = client.get("/.well-known/jwks.json").json()
    assert [k["kid"] for k in jwks_doc["keys"]] == [new_kid, old_kid]

    h1, p1 = _verify_compact_jws_via_jwks_kid(q1_json["jws"], jwks_doc)
    assert h1["kid"] == old_kid
    assert Quote.model_validate(p1) == Quote.model_validate({**q1_json, "jws": ""})

    h2, p2 = _verify_compact_jws_via_jwks_kid(q2_json["jws"], jwks_doc)
    assert h2["kid"] == new_kid
    assert Quote.model_validate(p2) == Quote.model_validate({**q2_json, "jws": ""})

    trail = client.get("/v1/audit", headers=agent_headers).json()
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    h_cp, p_cp = _verify_compact_jws_via_jwks_kid(trail["checkpoint"]["jws"], jwks_doc)
    assert h_cp["kid"] == new_kid
    assert p_cp == {k: v for k, v in trail["checkpoint"].items() if k != "jws"}
    assert p_cp["seq"] == trail["events"][-1]["seq"]
    assert p_cp["hash"] == trail["events"][-1]["hash"]


def test_cache_reload_cli_and_forged_tokens_against_jwks(client, tmp_path):
    q1 = _issue_synthetic_quote(qty=5.0)
    old_kid = keys.kid()
    new_kid = keys.rotate()
    q2 = _issue_synthetic_quote(qty=8.0)
    audit.record("hotel-aurora", "system", "order.placed", "ord_synth_2", "Recorded synthetic order.")

    keys._cache.clear()
    jwks_doc = client.get("/.well-known/jwks.json").json()
    assert [k["kid"] for k in jwks_doc["keys"]] == [new_kid, old_kid]

    cp = client.get("/v1/audit", headers={"X-Admin-Key": "demo-admin"}).json()["checkpoint"]
    jwks_file = tmp_path / "fetched_jwks.json"
    jwks_file.write_text(json.dumps(jwks_doc), encoding="utf-8")

    assert quotes.main(["quotes.py", "verify", q1.jws, str(jwks_file)]) == 0
    assert quotes.main(["quotes.py", "verify", q2.jws, str(jwks_file)]) == 0
    assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], str(jwks_file)]) == 0

    seg_h, seg_p, seg_s = q1.jws.split(".")
    none_hdr = _b64url_encode(json.dumps({"alg": "none", "kid": old_kid}, separators=(",", ":")).encode())
    old_pub = OKPKey.import_key(jwks_doc["keys"][1])
    with pytest.raises(JoseError):
        jws.deserialize_compact(f"{none_hdr}.{seg_p}.", old_pub, algorithms=["EdDSA"])

    tampered_payload = json.loads(_b64url_decode(seg_p))
    tampered_payload["total_gross"] = 0.01
    tampered_seg_p = _b64url_encode(json.dumps(tampered_payload, separators=(",", ":")).encode())
    with pytest.raises(JoseError):
        jws.deserialize_compact(f"{seg_h}.{tampered_seg_p}.{seg_s}", old_pub, algorithms=["EdDSA"])

    attacker = OKPKey.generate_key("Ed25519")
    spoofed = jws.serialize_compact(
        {"alg": "EdDSA", "kid": new_kid},
        _b64url_decode(seg_p),
        attacker,
        algorithms=["EdDSA"],
    )
    new_pub = OKPKey.import_key(jwks_doc["keys"][0])
    with pytest.raises(JoseError):
        jws.deserialize_compact(spoofed, new_pub, algorithms=["EdDSA"])
    assert quotes.main(["quotes.py", "verify", spoofed, str(jwks_file)]) == 1
    assert audit.main(["audit.py", "verify-checkpoint", spoofed, str(jwks_file)]) == 1

