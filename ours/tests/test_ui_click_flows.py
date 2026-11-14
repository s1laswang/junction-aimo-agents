from datetime import date, timedelta
import hashlib
from html.parser import HTMLParser
import json
from urllib.parse import urljoin

from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import KeySet
import pytest

from aimo import allocate, catalog, keys, offers as offers_mod, parse, substitute, valio
from aimo.app import app
from aimo.models import Constraint, NeedItem, Product, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")


PAGE_DOM_CONTRACT = {
    "/": {
        "customer",
        "n-appr",
        "n-audit",
        "d-audit",
        "n-man",
        "pend",
        "act",
        "connect",
        "c-mcp",
        "c-stdio",
        "c-rest",
        "c-card",
        "c-curl",
    },
    "/console.html": {
        "customer",
        "req",
        "text",
        "scenarios",
        "run",
        "result",
        "trace",
        "live",
    },
    "/inbox.html": {
        "customer",
        "inbox",
        "listPane",
        "detailPane",
        "cards",
        "count",
        "who",
        "whoDlg",
        "toast",
        "decided",
    },
    "/audit.html": {
        "customer",
        "badge",
        "filters",
        "f-customer",
        "f-order",
        "f-actor",
        "f-action",
        "v-ledger",
        "v-text",
        "ledger",
        "plain",
        "receipt",
        "legend",
    },
    "/admin.html": {
        "customer",
        "f",
        "per_order_eur",
        "per_week_eur",
        "substitutions",
        "reset",
        "msg",
        "last",
    },
}

NAV_ROUTES = {"/console.html", "/inbox.html", "/audit.html", "/admin.html"}

AUDIT_FIELDS = ("action", "actor", "customer", "detail", "seq", "subject", "ts", "why")


class PageInspector(HTMLParser):
    def __init__(self, page_route: str):
        super().__init__()
        self.page_route = page_route
        self.ids: list[str] = []
        self.scripts: list[str] = []
        self.external_scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.links: set[str] = set()
        self.scenario_chips: set[str] = set()
        self.mandate_chip_lists: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        elem_id = attr_map.get("id", "").strip()
        if elem_id:
            self.ids.append(elem_id)
        if attr_map.get("data-s"):
            self.scenario_chips.add(attr_map["data-s"].strip())
        if attr_map.get("data-k"):
            self.mandate_chip_lists.add(attr_map["data-k"].strip())
        if tag == "script" and attr_map.get("src"):
            src = attr_map["src"].strip()
            if src.startswith(("http://", "https://", "//")):
                self.external_scripts.append(src)
            elif not src.startswith("data:"):
                self.scripts.append(urljoin(self.page_route, src))
        elif tag == "link":
            rels = set(attr_map.get("rel", "").lower().split())
            href = attr_map.get("href", "").strip()
            if "stylesheet" in rels and href and not href.startswith(("http://", "https://", "//", "data:")):
                self.stylesheets.append(urljoin(self.page_route, href))
        elif tag == "a" and attr_map.get("href"):
            self.links.add(attr_map["href"].strip())


def _synthetic_catalog() -> dict[str, dict]:
    return {
        "1001": {
            "product": Product(
                product_id="1001",
                name="Valio Luomu Kevytmaito laktoositon 1 L",
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
            "net": 1.20,
            "gross": 1.37,
            "stock": 2000.0,
            "reliability": 0.99,
            "co2": 1.15,
        },
        "1002": {
            "product": Product(
                product_id="1002",
                name="Valio Kevytmaito laktoositon 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk"],
                labels=["free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.25,
            "gross": 1.43,
            "stock": 2000.0,
            "reliability": 0.97,
            "co2": 1.20,
        },
        "1003": {
            "product": Product(
                product_id="1003",
                name="Arla Kevytmaito laktoositon 1 L",
                brand="Arla",
                vendor="ARLA OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk"],
                labels=["free_from_lactose"],
                origin="SE",
                temperature=4,
            ),
            "net": 1.35,
            "gross": 1.54,
            "stock": 2000.0,
            "reliability": 0.95,
            "co2": 1.25,
        },
        "2001": {
            "product": Product(
                product_id="2001",
                name="Valio Provenia Gluteeniton pasta 1 kg",
                brand="Valio",
                vendor="VALIO OY",
                category="15100",
                sales_unit="KGM",
                unit_size=UnitSize(value=1.0, unit="kg"),
                allergens=[],
                labels=["free_from_gluten", "free_from_lactose"],
                origin="FI",
                temperature=20,
            ),
            "net": 4.20,
            "gross": 4.79,
            "stock": 600.0,
            "reliability": 0.96,
            "co2": 1.10,
        },
        "2002": {
            "product": Product(
                product_id="2002",
                name="Valio Oddlygood Kaurajuoma gluteeniton 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["oats"],
                labels=["free_from_gluten", "free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.50,
            "gross": 1.71,
            "stock": 600.0,
            "reliability": 0.94,
            "co2": 0.85,
        },
        "3001": {
            "product": Product(
                product_id="3001",
                name="Fazer Maitosuklaa maapahkina 1 kg",
                brand="Fazer",
                vendor="FAZER OY",
                category="16200",
                sales_unit="KGM",
                unit_size=UnitSize(value=1.0, unit="kg"),
                allergens=["milk", "lactose", "peanuts"],
                labels=[],
                origin="FI",
                temperature=18,
            ),
            "net": 8.50,
            "gross": 9.69,
            "stock": 300.0,
            "reliability": 0.92,
            "co2": 4.80,
        },
    }


def _matches_constraints(product: Product, c: Constraint) -> bool:
    if (c.lactose_free or c.gluten_free or c.exclude_allergens) and not substitute.allergen_safe(
        product, c, c.exclude_allergens
    ):
        return False
    if c.labels:
        plabels = {x.lower() for x in product.labels}
        if not all(any(req.lower() in pl for pl in plabels) for req in c.labels):
            return False
    if c.origin:
        want = "fi" if c.origin.strip().lower() in ("fi", "finland", "suomi") else c.origin.strip().lower()[:2]
        if (product.origin or "").lower() != want:
            return False
    if c.brands and not any(b.lower() in (product.brand or "").lower() for b in c.brands):
        return False
    return True


@pytest.fixture
def ui_client(tmp_path, monkeypatch):
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    for env_var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "AIMO_AGENT_KEYS", "AIMO_ADMIN_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(keys, "_cache", {})
    monkeypatch.setattr(offers_mod, "_overrides", {})
    monkeypatch.setattr(allocate, "_data", lambda: {})
    monkeypatch.setattr(parse, "_llm_parse", lambda *a, **kw: None)

    items = _synthetic_catalog()

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        c = constraints or Constraint()
        probe = NeedItem(query=q or "", quantity=1.0, unit="pcs", constraints=c)
        scored: list[tuple[float, str, Product]] = []
        for pid, entry in items.items():
            prod = entry["product"]
            if not _matches_constraints(prod, c):
                continue
            if not (q or "").strip():
                scored.append((-1.0, pid, prod))
                continue
            score = substitute.match_score(probe, prod)
            if score <= 0:
                q_tokens = {t for t in q.lower().split() if len(t) >= 3}
                n_tokens = set(prod.name.lower().split())
                if q_tokens & n_tokens:
                    score = 0.55
            if score > 0:
                scored.append((-score, pid, prod))
        scored.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in scored[:limit]]

    def fake_get(pid: str) -> Product | None:
        entry = items.get(str(pid))
        return entry["product"] if entry else None

    def fake_reliability(pid: str) -> float | None:
        entry = items.get(str(pid))
        return entry["reliability"] if entry else None

    def fake_co2(pid: str) -> float | None:
        entry = items.get(str(pid))
        return entry["co2"] if entry else None

    def fake_price(customer: str, day: date, ids) -> dict[str, dict]:
        out = {}
        for raw_id in ids:
            pid = str(raw_id)
            entry = items.get(pid)
            if entry:
                out[pid] = {
                    "productId": pid,
                    "currency": "EUR",
                    "catalogPrice": {"value": entry["net"]},
                    "priceWithTax": {"value": entry["gross"]},
                }
        return out

    def fake_availability(customer: str, day: date, ids) -> dict[str, dict]:
        out = {}
        for raw_id in ids:
            pid = str(raw_id)
            entry = items.get(pid)
            if entry:
                out[pid] = {
                    "productId": pid,
                    "closingTime": "2026-11-12T23:59:59+02:00",
                    "salesBlock": False,
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": entry["stock"],
                            "availableDate": day.isoformat(),
                            "salesUnit": entry["product"].sales_unit,
                        }
                    ],
                }
        return out

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", fake_get)
    monkeypatch.setattr(catalog, "reliability", fake_reliability)
    monkeypatch.setattr(catalog, "co2_per_kg", fake_co2)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)

    with TestClient(app) as client:
        yield client


def _agent_headers(slug: str) -> dict[str, str]:
    return {"X-Agent-Key": f"demo-{slug}"}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": "demo-admin"}


def _browser_event_hash(event: dict) -> str:
    canonical = json.dumps({k: event[k] for k in AUDIT_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((event["prev_hash"] + canonical).encode("utf-8")).hexdigest()


def _verify_jws_compact(token: str, jwks_doc: dict) -> tuple[dict, dict]:
    key_set = KeySet.import_key_set(jwks_doc)
    decoded = jws.deserialize_compact(token, key_set, algorithms=["EdDSA"])
    headers = decoded.headers()
    payload = json.loads(decoded.payload)
    return headers, payload


def test_all_five_pages_and_referenced_local_assets_serve_200(ui_client: TestClient):
    discovered_assets: set[str] = set()
    for route in PAGE_DOM_CONTRACT:
        resp = ui_client.get(route)
        assert resp.status_code == 200, route
        assert resp.text.strip(), route
        inspector = PageInspector(route)
        inspector.feed(resp.text)
        assert not inspector.external_scripts, (route, inspector.external_scripts)
        assert "/style.css" in inspector.stylesheets, route
        assert "/shell.js" in inspector.scripts, route
        discovered_assets.update(inspector.scripts)
        discovered_assets.update(inspector.stylesheets)

    expected_assets = {
        "/style.css",
        "/shell.js",
        "/console.css",
        "/console.js",
        "/inbox.css",
        "/inbox.js",
        "/audit.css",
        "/audit.js",
        "/admin.css",
        "/admin.js",
    }
    assert expected_assets.issubset(discovered_assets)

    for asset in sorted(discovered_assets):
        asset_resp = ui_client.get(asset)
        assert asset_resp.status_code == 200, asset
        assert len(asset_resp.content) > 0, asset


def test_all_five_pages_expose_required_interactive_dom_ids(ui_client: TestClient):
    for route, required_ids in PAGE_DOM_CONTRACT.items():
        resp = ui_client.get(route)
        assert resp.status_code == 200, route
        inspector = PageInspector(route)
        inspector.feed(resp.text)
        assert len(inspector.ids) == len(set(inspector.ids)), f"Duplicate IDs on {route}: {inspector.ids}"
        missing = required_ids - set(inspector.ids)
        assert not missing, f"Missing IDs on {route}: {sorted(missing)}"
        assert NAV_ROUTES.issubset(inspector.links), f"Missing nav links on {route}"
        if route == "/console.html":
            assert {"milk", "lasagne", "school", "hospital", "event"}.issubset(inspector.scenario_chips)
        elif route == "/admin.html":
            assert {"exclude_allergens", "allowed_categories", "preferred_brands", "approvers"}.issubset(
                inspector.mandate_chip_lists
            )


def test_overview_dashboard_live_endpoints_and_discovery_contracts(ui_client: TestClient):
    for route in ("/health", "/.well-known/agent-card.json", "/.well-known/jwks.json", "/openapi.json"):
        r = ui_client.get(route)
        assert r.status_code == 200, route
        assert r.json(), route

    jwks_doc = ui_client.get("/.well-known/jwks.json").json()
    assert jwks_doc["keys"]
    pub = jwks_doc["keys"][0]
    assert pub["kty"] == "OKP" and pub["crv"] == "Ed25519" and pub["alg"] == "EdDSA"
    assert "d" not in pub

    appr = ui_client.get("/v1/approvals", headers=_admin_headers())
    assert appr.status_code == 200 and appr.json() == []

    audit_resp = ui_client.get("/v1/audit", headers=_admin_headers())
    assert audit_resp.status_code == 200
    audit_body = audit_resp.json()
    assert audit_body["events"] == []
    assert audit_body["verified"] == {"ok": True, "bad_seq": None}
    assert audit_body["checkpoint"]["seq"] == 0

    for cust, expected_limit in (("hotel-aurora", 2500.0), ("hospital-tays", 1500.0), ("school-hervanta", 600.0)):
        mand = ui_client.get(f"/v1/mandate?customer={cust}", headers=_admin_headers())
        assert mand.status_code == 200
        assert mand.json()["per_order_eur"] == expected_limit


def test_journey_1_console_auto_placed_order_jws_and_audit_contract(ui_client: TestClient):
    headers = _agent_headers("hotel-aurora")

    parse_resp = ui_client.post(
        "/v1/needs/parse",
        headers=headers,
        json={
            "text": "I need 1,000 litres of lactose-free milk in 3 days. Find an option based on price and availability.",
            "customer": "hotel-aurora",
        },
    )
    assert parse_resp.status_code == 200
    need = parse_resp.json()
    assert need["customer"] == "hotel-aurora"
    assert need["deliver_by"] == (date.today() + timedelta(days=3)).isoformat()
    assert len(need["items"]) == 1
    assert need["items"][0]["quantity"] == 1000.0
    assert need["items"][0]["constraints"]["lactose_free"] is True

    mandate_resp = ui_client.get("/v1/mandate", headers=headers)
    assert mandate_resp.status_code == 200
    mandate = mandate_resp.json()
    assert mandate["customer"] == "hotel-aurora"
    assert mandate["per_order_eur"] == 2500.0
    assert mandate["per_week_eur"] == 15000.0
    assert mandate["substitutions"] == "within_prefs"

    offers_resp = ui_client.post("/v1/offers", headers=headers, json=need)
    assert offers_resp.status_code == 200
    offers_body = offers_resp.json()
    assert len(offers_body["items"]) == 1
    group = offers_body["items"][0]
    assert group["shortfall"] == 0.0
    assert len(group["offers"]) >= 1
    picked = group["offers"][0]
    assert picked["product_id"] == "1001"
    assert picked["units_needed"] == 1000.0
    assert picked["unit_price_net"] == 1.20
    assert picked["total_net"] == 1200.0
    assert picked["available_amount"] == 2000.0
    assert picked["available_date"] == need["deliver_by"]
    assert picked["reliability"] == 0.99
    assert picked["co2_per_kg"] == 1.15
    assert picked["product"]["name"]
    assert picked["product"]["brand"] == "Valio"
    assert picked["product"]["sales_unit"] == "LTR"
    assert picked["product"]["unit_size"] == {"value": 1.0, "unit": "l"}

    quote_resp = ui_client.post(
        "/v1/quotes",
        headers=headers,
        json={
            "offers": [{"product_id": picked["product_id"], "quantity": picked["units_needed"]}],
            "delivery_date": need["deliver_by"],
        },
    )
    assert quote_resp.status_code == 200
    quote = quote_resp.json()
    assert quote["quote_id"].startswith("q_")
    assert quote["customer"] == "hotel-aurora"
    assert quote["agent_id"] == "buyer-aurora"
    assert quote["total_net"] == 1200.0
    assert quote["total_gross"] == 1370.0
    assert quote["currency"] == "EUR"
    assert quote["valid_until"]
    assert len(quote["lines"]) == 1

    jwks_doc = ui_client.get("/.well-known/jwks.json").json()
    jws_headers, jws_payload = _verify_jws_compact(quote["jws"], jwks_doc)
    assert jws_headers["alg"] == "EdDSA"
    assert jws_headers["kid"] in {k["kid"] for k in jwks_doc["keys"]}
    assert jws_payload["quote_id"] == quote["quote_id"]
    assert jws_payload["customer"] == "hotel-aurora"
    assert jws_payload["total_net"] == 1200.0
    assert jws_payload["total_gross"] == 1370.0

    order_resp = ui_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "jws": quote["jws"]},
    )
    assert order_resp.status_code == 200
    order = order_resp.json()
    oid = order["order_id"]
    assert oid.startswith("ord_")
    assert order["quote_id"] == quote["quote_id"]
    assert order["customer"] == "hotel-aurora"
    assert order["agent_id"] == "buyer-aurora"
    assert order["total_net"] == 1200.0
    assert order["decision"]["outcome"] == "auto"
    assert order["status"] == "confirmed"
    assert any(r["code"] == "WITHIN_MANDATE" for r in order["decision"]["reasons"])
    assert order["substitutions"] == []

    replay_resp = ui_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "jws": quote["jws"]},
    )
    assert replay_resp.status_code == 409
    assert replay_resp.json()["code"] == "QUOTE_USED"

    receipt_order = ui_client.get(f"/v1/orders/{oid}", headers=_admin_headers())
    assert receipt_order.status_code == 200
    assert receipt_order.json()["status"] == "confirmed"

    scoped_audit_resp = ui_client.get(f"/v1/audit?order_id={oid}", headers=_admin_headers())
    assert scoped_audit_resp.status_code == 200
    scoped_audit = scoped_audit_resp.json()
    assert scoped_audit["verified"] == {"ok": True, "bad_seq": None}
    assert isinstance(scoped_audit["text"], str) and scoped_audit["text"].strip()

    scoped_events = scoped_audit["events"]
    actions = [e["action"] for e in scoped_events]
    for expected_action in ("quote.issued", "quote.redeemed", "order.placed", "order.approved", "order.confirmed"):
        assert expected_action in actions
    for ev in scoped_events:
        for req_key in (*AUDIT_FIELDS, "prev_hash", "hash"):
            assert req_key in ev
        assert ev["why"].strip()

    full_audit = ui_client.get("/v1/audit", headers=_admin_headers()).json()
    full_events = full_audit["events"]
    assert full_events[0]["seq"] == 1
    assert full_events[0]["prev_hash"] == "0" * 64
    for idx, ev in enumerate(full_events):
        assert _browser_event_hash(ev) == ev["hash"]
        if idx > 0:
            assert ev["prev_hash"] == full_events[idx - 1]["hash"]

    cp = scoped_audit["checkpoint"]
    cp_headers, cp_payload = _verify_jws_compact(cp["jws"], jwks_doc)
    assert cp_headers["alg"] == "EdDSA"
    assert cp_payload["seq"] == full_events[-1]["seq"]
    assert cp_payload["hash"] == full_events[-1]["hash"]


def test_journey_2_inbox_over_limit_and_substitution_approve_and_reject_flows(ui_client: TestClient):
    school_headers = _agent_headers("school-hervanta")
    admin_headers = _admin_headers()

    need = ui_client.post(
        "/v1/needs/parse",
        headers=school_headers,
        json={
            "text": "Weekly school lunch: 200 kg of gluten-free pasta and 80 litres of oat drink, no nuts, no egg, in 3 days.",
            "customer": "school-hervanta",
        },
    ).json()
    offers_body = ui_client.post("/v1/offers", headers=school_headers, json=need).json()
    quote_lines = [
        {"product_id": grp["offers"][0]["product_id"], "quantity": grp["offers"][0]["units_needed"]}
        for grp in offers_body["items"]
        if grp["offers"]
    ]
    assert len(quote_lines) == 2

    quote1 = ui_client.post(
        "/v1/quotes",
        headers=school_headers,
        json={"offers": quote_lines, "delivery_date": need["deliver_by"]},
    ).json()
    assert quote1["total_net"] == 960.0

    order1_resp = ui_client.post(
        "/v1/orders",
        headers=school_headers,
        json={"quote_id": quote1["quote_id"], "jws": quote1["jws"]},
    )
    assert order1_resp.status_code == 200
    order1 = order1_resp.json()
    oid1 = order1["order_id"]
    assert order1["decision"]["outcome"] == "needs_approval"
    assert order1["status"] == "pending_approval"
    assert any(r["code"] == "OVER_ORDER_LIMIT" for r in order1["decision"]["reasons"])

    inbox_list = ui_client.get("/v1/approvals", headers=admin_headers).json()
    matching = [o for o in inbox_list if o["order_id"] == oid1]
    assert len(matching) == 1
    card_item = matching[0]
    for field in (
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
        assert field in card_item

    mandate = ui_client.get("/v1/mandate?customer=school-hervanta", headers=admin_headers).json()
    assert mandate["approvers"] == ["Sari Nieminen"]

    wrong_approver = ui_client.post(
        f"/v1/approvals/{oid1}",
        headers=admin_headers,
        json={"approve": True, "by": "Laura Virtanen", "note": "wrong org"},
    )
    assert wrong_approver.status_code == 403
    assert wrong_approver.json()["code"] == "NOT_APPROVER"
    assert any(o["order_id"] == oid1 for o in ui_client.get("/v1/approvals", headers=admin_headers).json())

    approved_resp = ui_client.post(
        f"/v1/approvals/{oid1}",
        headers=admin_headers,
        json={"approve": True, "by": "Sari Nieminen", "note": "Approved for school lunch"},
    )
    assert approved_resp.status_code == 200
    approved_order = approved_resp.json()
    assert approved_order["status"] == "confirmed"
    assert all(o["order_id"] != oid1 for o in ui_client.get("/v1/approvals", headers=admin_headers).json())

    audit1 = ui_client.get(f"/v1/audit?order_id={oid1}", headers=admin_headers).json()
    assert audit1["verified"]["ok"] is True
    assert any(
        e["action"] == "approval.approved" and e["actor"] == "human:Sari Nieminen"
        for e in audit1["events"]
    )

    quote2 = ui_client.post(
        "/v1/quotes",
        headers=school_headers,
        json={
            "offers": [{"product_id": "1003", "quantity": 200.0, "substitute_for": "1001"}],
            "delivery_date": need["deliver_by"],
        },
    ).json()
    assert quote2["total_net"] == 270.0

    order2_resp = ui_client.post(
        "/v1/orders",
        headers=school_headers,
        json={"quote_id": quote2["quote_id"], "jws": quote2["jws"]},
    )
    assert order2_resp.status_code == 200
    order2 = order2_resp.json()
    oid2 = order2["order_id"]
    assert order2["decision"]["outcome"] == "needs_approval"
    assert order2["status"] == "pending_approval"
    assert any(r["code"] == "SUBSTITUTION_NEEDS_APPROVAL" for r in order2["decision"]["reasons"])
    assert order2["substitutions"][0]["original_id"] == "1001"
    assert order2["substitutions"][0]["substitute_id"] == "1003"
    assert any(o["order_id"] == oid2 for o in ui_client.get("/v1/approvals", headers=admin_headers).json())

    reject_note = "Use Valio contract lactose-free milk instead"
    rejected_resp = ui_client.post(
        f"/v1/approvals/{oid2}",
        headers=admin_headers,
        json={"approve": False, "by": "Sari Nieminen", "note": reject_note},
    )
    assert rejected_resp.status_code == 200
    rejected_order = rejected_resp.json()
    assert rejected_order["status"] == "rejected"
    assert all(o["order_id"] != oid2 for o in ui_client.get("/v1/approvals", headers=admin_headers).json())

    audit2 = ui_client.get(f"/v1/audit?order_id={oid2}", headers=admin_headers).json()
    assert audit2["verified"]["ok"] is True
    rej_events = [
        e for e in audit2["events"] if e["action"] == "approval.rejected" and e["actor"] == "human:Sari Nieminen"
    ]
    assert len(rej_events) == 1
    assert rej_events[0]["detail"]["note"] == reject_note
    assert reject_note in rej_events[0]["why"]


def test_journey_3_blocked_allergen_and_disallowed_category_flows(ui_client: TestClient):
    hosp_headers = _agent_headers("hospital-tays")
    admin_headers = _admin_headers()
    delivery = (date.today() + timedelta(days=3)).isoformat()

    mand = ui_client.get("/v1/mandate", headers=hosp_headers).json()
    assert "peanut" in mand["exclude_allergens"]

    peanut_quote = ui_client.post(
        "/v1/quotes",
        headers=hosp_headers,
        json={"offers": [{"product_id": "3001", "quantity": 10.0}], "delivery_date": delivery},
    ).json()
    allergen_order_resp = ui_client.post(
        "/v1/orders",
        headers=hosp_headers,
        json={"quote_id": peanut_quote["quote_id"], "jws": peanut_quote["jws"]},
    )
    assert allergen_order_resp.status_code == 200
    allergen_order = allergen_order_resp.json()
    assert allergen_order["decision"]["outcome"] == "blocked"
    assert allergen_order["status"] == "blocked"
    assert any(r["code"] == "ALLERGEN_CONFLICT" for r in allergen_order["decision"]["reasons"])
    assert all(
        o["order_id"] != allergen_order["order_id"]
        for o in ui_client.get("/v1/approvals", headers=admin_headers).json()
    )
    allergen_audit = ui_client.get(
        f"/v1/audit?order_id={allergen_order['order_id']}", headers=admin_headers
    ).json()
    assert any(e["action"] == "order.blocked" for e in allergen_audit["events"])

    restricted_mandate = {**mand, "allowed_categories": ["10400"]}
    put_resp = ui_client.put("/v1/mandate", headers=admin_headers, json=restricted_mandate)
    assert put_resp.status_code == 200
    assert put_resp.json()["allowed_categories"] == ["10400"]

    pasta_quote = ui_client.post(
        "/v1/quotes",
        headers=hosp_headers,
        json={"offers": [{"product_id": "2001", "quantity": 10.0}], "delivery_date": delivery},
    ).json()
    cat_order_resp = ui_client.post(
        "/v1/orders",
        headers=hosp_headers,
        json={"quote_id": pasta_quote["quote_id"], "jws": pasta_quote["jws"]},
    )
    assert cat_order_resp.status_code == 200
    cat_order = cat_order_resp.json()
    assert cat_order["decision"]["outcome"] == "blocked"
    assert cat_order["status"] == "blocked"
    assert any(r["code"] == "CATEGORY_NOT_ALLOWED" for r in cat_order["decision"]["reasons"])
    assert all(
        o["order_id"] != cat_order["order_id"]
        for o in ui_client.get("/v1/approvals", headers=admin_headers).json()
    )


def test_journey_4_admin_mandate_put_and_stockout_simulate_and_clear_flow(ui_client: TestClient):
    hotel_headers = _agent_headers("hotel-aurora")
    admin_headers = _admin_headers()
    delivery = (date.today() + timedelta(days=3)).isoformat()

    initial_quote = ui_client.post(
        "/v1/quotes",
        headers=hotel_headers,
        json={"offers": [{"product_id": "1001", "quantity": 500.0}], "delivery_date": delivery},
    ).json()
    initial_order = ui_client.post(
        "/v1/orders",
        headers=hotel_headers,
        json={"quote_id": initial_quote["quote_id"], "jws": initial_quote["jws"]},
    ).json()
    confirmed_oid = initial_order["order_id"]
    assert initial_order["decision"]["outcome"] == "auto"
    assert initial_order["status"] == "confirmed"

    get_mand = ui_client.get("/v1/mandate?customer=hotel-aurora", headers=admin_headers)
    assert get_mand.status_code == 200
    mand = get_mand.json()
    assert mand["per_order_eur"] == 2500.0

    invalid_put = ui_client.put(
        "/v1/mandate",
        headers=admin_headers,
        json={**mand, "per_order_eur": 0.0},
    )
    assert invalid_put.status_code == 409
    assert invalid_put.json()["code"] == "INVALID_MANDATE"

    updated_mandate = {**mand, "per_order_eur": 500.0}
    put_resp = ui_client.put("/v1/mandate", headers=admin_headers, json=updated_mandate)
    assert put_resp.status_code == 200
    assert put_resp.json()["per_order_eur"] == 500.0

    reread = ui_client.get("/v1/mandate?customer=hotel-aurora", headers=admin_headers).json()
    assert reread["per_order_eur"] == 500.0

    cust_audit = ui_client.get("/v1/audit?customer=hotel-aurora", headers=admin_headers).json()
    mand_events = [e for e in cust_audit["events"] if e["action"] == "mandate_updated"]
    assert len(mand_events) == 1
    assert "per_order_eur" in mand_events[0]["why"]
    assert cust_audit["verified"]["ok"] is True

    sim_resp = ui_client.post(
        "/v1/admin/simulate",
        headers=admin_headers,
        json={"product_id": "1001", "available_amount": 0},
    )
    assert sim_resp.status_code == 200
    assert sim_resp.json() == {"ok": True}

    reopened = ui_client.get(f"/v1/orders/{confirmed_oid}", headers=admin_headers).json()
    assert reopened["status"] == "pending_approval"
    assert reopened["decision"]["outcome"] == "needs_approval"
    assert any(r["code"] == "STOCKOUT" for r in reopened["decision"]["reasons"])
    assert any(
        s["original_id"] == "1001" and s["substitute_id"] == "1002"
        for s in reopened["substitutions"]
    )
    assert any(o["order_id"] == confirmed_oid for o in ui_client.get("/v1/approvals", headers=admin_headers).json())

    stockout_audit = ui_client.get(f"/v1/audit?order_id={confirmed_oid}", headers=admin_headers).json()
    assert any(e["action"] == "order.stockout" for e in stockout_audit["events"])

    need = ui_client.post(
        "/v1/needs/parse",
        headers=hotel_headers,
        json={"text": "100 litres of lactose-free milk in 3 days", "customer": "hotel-aurora"},
    ).json()
    during_sim_offers = ui_client.post("/v1/offers", headers=hotel_headers, json=need).json()
    assert during_sim_offers["items"][0]["offers"][0]["product_id"] == "1002"

    clear_resp = ui_client.delete("/v1/admin/simulate", headers=admin_headers)
    assert clear_resp.status_code == 200
    assert clear_resp.json() == {"ok": True}

    after_clear_offers = ui_client.post("/v1/offers", headers=hotel_headers, json=need).json()
    top_after_clear = after_clear_offers["items"][0]["offers"][0]
    assert top_after_clear["product_id"] == "1001"
    assert top_after_clear["available_amount"] == 2000.0


def test_ui_auth_and_customer_scoping_contracts(ui_client: TestClient):
    hotel_headers = _agent_headers("hotel-aurora")

    unauth = ui_client.get("/v1/mandate")
    assert unauth.status_code == 401
    assert unauth.json()["code"] == "unauthorized"

    cross_mandate = ui_client.get("/v1/mandate?customer=hospital-tays", headers=hotel_headers)
    assert cross_mandate.status_code == 403
    assert cross_mandate.json()["code"] == "forbidden"

    agent_sim = ui_client.post(
        "/v1/admin/simulate",
        headers=hotel_headers,
        json={"product_id": "1001", "available_amount": 0},
    )
    assert agent_sim.status_code == 403
    assert agent_sim.json()["code"] == "forbidden"

