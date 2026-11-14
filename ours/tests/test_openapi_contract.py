from datetime import date, datetime, timedelta
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from aimo import catalog, models, offers, valio
from aimo.app import app

ROOT = Path(__file__).resolve().parent.parent
HEL = ZoneInfo("Europe/Helsinki")

AGENT_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}
ADMIN_HEADERS = {"X-Admin-Key": "demo-admin"}

CONTRACT_ROUTES = {
    ("GET", "/health"),
    ("GET", "/.well-known/agent-card.json"),
    ("GET", "/.well-known/jwks.json"),
    ("GET", "/v1/products"),
    ("GET", "/v1/products/{product_id}"),
    ("POST", "/v1/needs/parse"),
    ("POST", "/v1/offers"),
    ("POST", "/v1/quotes"),
    ("POST", "/v1/orders"),
    ("GET", "/v1/orders"),
    ("GET", "/v1/orders/{order_id}"),
    ("GET", "/v1/approvals"),
    ("POST", "/v1/approvals/{order_id}"),
    ("GET", "/v1/audit"),
    ("GET", "/v1/mandate"),
    ("PUT", "/v1/mandate"),
    ("POST", "/v1/admin/simulate"),
    ("DELETE", "/v1/admin/simulate"),
}

OPENAPI_DIRECT_SCHEMAS = {
    "Constraint",
    "NeedItem",
    "Need",
    "Mandate",
    "ParseRequest",
    "ParseResponse",
    "QuoteRequest",
    "QuoteRequestLine",
    "PlaceOrderRequest",
    "ApprovalDecision",
    "SimulateStock",
}

EXPECTED_SCHEMAS = {
    "Constraint": (
        {
            "lactose_free",
            "gluten_free",
            "exclude_allergens",
            "labels",
            "origin",
            "max_unit_price",
            "brands",
        },
        set(),
    ),
    "NeedItem": (
        {"query", "quantity", "unit", "constraints"},
        {"query", "quantity", "unit"},
    ),
    "Need": (
        {"customer", "deliver_by", "items", "raw_text"},
        {"customer", "deliver_by", "items"},
    ),
    "Product": (
        {
            "product_id",
            "name",
            "brand",
            "vendor",
            "category",
            "sales_unit",
            "unit_size",
            "allergens",
            "labels",
            "origin",
            "temperature",
        },
        {"product_id", "name"},
    ),
    "Offer": (
        {
            "product_id",
            "product",
            "units_needed",
            "unit_price_net",
            "total_net",
            "total_gross",
            "currency",
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
        },
        {
            "product_id",
            "product",
            "units_needed",
            "unit_price_net",
            "total_net",
            "total_gross",
            "available_amount",
        },
    ),
    "QuoteLine": (
        {
            "product_id",
            "name",
            "quantity",
            "sales_unit",
            "unit_price_net",
            "line_total_net",
            "substitute_for",
        },
        {"product_id", "quantity", "unit_price_net", "line_total_net"},
    ),
    "Quote": (
        {
            "quote_id",
            "customer",
            "agent_id",
            "delivery_date",
            "lines",
            "total_net",
            "total_gross",
            "currency",
            "issued_at",
            "valid_until",
            "jws",
        },
        {
            "quote_id",
            "customer",
            "agent_id",
            "delivery_date",
            "lines",
            "total_net",
            "total_gross",
            "issued_at",
            "valid_until",
        },
    ),
    "Order": (
        {
            "order_id",
            "quote_id",
            "customer",
            "agent_id",
            "lines",
            "total_net",
            "status",
            "decision",
            "substitutions",
            "delivery_date",
            "valid_until",
            "created_at",
            "updated_at",
        },
        {
            "order_id",
            "quote_id",
            "customer",
            "agent_id",
            "lines",
            "total_net",
            "status",
            "decision",
            "created_at",
            "updated_at",
        },
    ),
    "Decision": (
        {"outcome", "reasons"},
        {"outcome"},
    ),
    "Mandate": (
        {
            "customer",
            "agent_id",
            "per_order_eur",
            "per_week_eur",
            "exclude_allergens",
            "allowed_categories",
            "substitutions",
            "preferred_brands",
            "approvers",
        },
        {"customer", "agent_id", "per_order_eur", "per_week_eur"},
    ),
    "AuditEvent": (
        {
            "seq",
            "ts",
            "customer",
            "actor",
            "action",
            "subject",
            "detail",
            "why",
            "prev_hash",
            "hash",
        },
        {
            "seq",
            "ts",
            "customer",
            "actor",
            "action",
            "subject",
            "why",
            "prev_hash",
            "hash",
        },
    ),
    "ParseRequest": (
        {"text", "customer"},
        {"text", "customer"},
    ),
    "QuoteRequest": (
        {"offers", "delivery_date"},
        {"offers", "delivery_date"},
    ),
    "QuoteRequestLine": (
        {"product_id", "quantity", "substitute_for"},
        {"product_id", "quantity"},
    ),
    "PlaceOrderRequest": (
        {"quote_id", "jws", "ask_human", "note"},
        {"quote_id", "jws"},
    ),
    "ApprovalDecision": (
        {"approve", "by", "note"},
        {"approve", "by"},
    ),
    "SimulateStock": (
        {"product_id", "available_amount", "available_date"},
        {"product_id", "available_amount"},
    ),
}

ROUTE_NORMALIZE = {
    "/v1/products/{id}": "/v1/products/{product_id}",
    "/v1/orders/{id}": "/v1/orders/{order_id}",
}


def _future_day(days: int = 3) -> date:
    return datetime.now(HEL).date() + timedelta(days=days)


def _fastapi_schema_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        items = (
            route.effective_route_contexts()
            if hasattr(route, "effective_route_contexts")
            else [route]
        )
        for item in items:
            if getattr(item, "include_in_schema", False) and getattr(item, "methods", None):
                for method in item.methods:
                    if method != "HEAD":
                        out.add((method.upper(), item.path))
    return out


def _openapi_routes(spec: dict) -> set[tuple[str, str]]:
    http_methods = {"get", "post", "put", "delete", "patch", "options"}
    return {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if method.lower() in http_methods
    }


def _ref_name(schema_obj: dict) -> str:
    return schema_obj["$ref"].rsplit("/", 1)[-1]


def _synthetic_milk(pid: str = "SYNTH-MILK-1") -> models.Product:
    return models.Product(
        product_id=pid,
        name="Lactose free milk 1L",
        brand="Valio",
        vendor="Valio Oy",
        category="10400",
        sales_unit="LTR",
        unit_size=models.UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose", "finland"],
        origin="fi",
        temperature=4,
    )


def _install_synthetic_backend(monkeypatch, product: models.Product, day: date) -> None:
    by_id = {product.product_id: product}
    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [product])
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(pid))
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if pid in by_id else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.15 if pid in by_id else None)
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, requested_date, ids: {
            pid: {
                "productId": pid,
                "currency": "EUR",
                "catalogPrice": {"value": 1.25},
                "priceWithTax": {"value": 1.43},
            }
            for pid in ids
            if pid in by_id
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, requested_date, ids: {
            pid: {
                "productId": pid,
                "closingTime": "2026-11-12T15:00:00+0200",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 2000.0,
                        "availableDate": requested_date.isoformat(),
                        "salesUnit": "LTR",
                    }
                ],
            }
            for pid in ids
            if pid in by_id
        },
    )


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "empty_data"))
    monkeypatch.setenv("VALIO_CACHE_DIR", str(tmp_path / "cache"))
    offers.clear_simulation()
    with TestClient(app) as c:
        yield c
    offers.clear_simulation()


def test_openapi_serves_200_and_matches_contract_and_fastapi_routes(isolated_client):
    resp = isolated_client.get("/openapi.json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")
    spec = resp.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "AIMO Trusted Trade Gateway"
    assert isinstance(spec["info"]["version"], str) and spec["info"]["version"]

    openapi_routes = _openapi_routes(spec)
    fastapi_routes = _fastapi_schema_routes()
    assert openapi_routes == CONTRACT_ROUTES
    assert fastapi_routes == CONTRACT_ROUTES

    contract_text = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    documented = set()
    for method, raw_path in re.findall(
        r"`(GET|POST|PUT|DELETE)\s+((?:/v1/[^\s`|]+)|(?:/\.well-known/[^\s`|]+)|/health)`",
        contract_text,
    ):
        clean_path = raw_path.split("?", 1)[0]
        clean_path = ROUTE_NORMALIZE.get(clean_path, clean_path)
        documented.add((method, clean_path))
    assert documented <= openapi_routes
    assert ("GET", "/health") in openapi_routes
    assert ("GET", "/.well-known/agent-card.json") in openapi_routes
    assert ("GET", "/.well-known/jwks.json") in openapi_routes


def test_openapi_operation_parameters_and_request_body_refs(isolated_client):
    spec = isolated_client.get("/openapi.json").json()
    paths = spec["paths"]

    prod_params = {p["name"]: p for p in paths["/v1/products"]["get"]["parameters"]}
    assert set(prod_params) == {
        "q",
        "lactose_free",
        "gluten_free",
        "exclude_allergens",
        "labels",
        "origin",
        "max_unit_price",
        "brands",
        "limit",
    }
    assert all(not p["required"] and p["in"] == "query" for p in prod_params.values())

    one_prod_params = paths["/v1/products/{product_id}"]["get"]["parameters"]
    assert len(one_prod_params) == 1
    assert one_prod_params[0]["name"] == "product_id"
    assert one_prod_params[0]["in"] == "path" and one_prod_params[0]["required"] is True

    expected_body_refs = {
        ("/v1/needs/parse", "post"): "ParseRequest",
        ("/v1/offers", "post"): "Need",
        ("/v1/quotes", "post"): "QuoteRequest",
        ("/v1/orders", "post"): "PlaceOrderRequest",
        ("/v1/approvals/{order_id}", "post"): "ApprovalDecision",
        ("/v1/mandate", "put"): "Mandate",
        ("/v1/admin/simulate", "post"): "SimulateStock",
    }
    for (path, method), expected_ref in expected_body_refs.items():
        op = paths[path][method]
        assert op["requestBody"]["required"] is True
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert _ref_name(schema) == expected_ref

    parse_resp_schema = paths["/v1/needs/parse"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert _ref_name(parse_resp_schema) == "ParseResponse"

    assert {p["name"] for p in paths["/v1/orders"]["get"]["parameters"]} == {
        "customer",
        "status",
    }
    assert {p["name"] for p in paths["/v1/audit"]["get"]["parameters"]} == {
        "customer",
        "order_id",
    }
    assert {p["name"] for p in paths["/v1/approvals"]["get"]["parameters"]} == {"customer"}
    assert {p["name"] for p in paths["/v1/mandate"]["get"]["parameters"]} == {"customer"}


def test_openapi_component_schemas_present(isolated_client):
    spec = isolated_client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert OPENAPI_DIRECT_SCHEMAS <= set(schemas)
    assert {"HTTPValidationError", "ValidationError"} <= set(schemas)


@pytest.mark.parametrize("schema_name", sorted(EXPECTED_SCHEMAS))
def test_component_schemas_and_required_fields_match_contract(isolated_client, schema_name):
    expected_props, expected_required = EXPECTED_SCHEMAS[schema_name]
    model_cls = getattr(models, schema_name)
    model_schema = model_cls.model_json_schema()
    assert set(model_schema.get("properties", ())) == expected_props
    assert set(model_schema.get("required", ())) == expected_required

    spec = isolated_client.get("/openapi.json").json()
    openapi_schemas = spec["components"]["schemas"]
    if schema_name in OPENAPI_DIRECT_SCHEMAS or schema_name in openapi_schemas:
        assert schema_name in openapi_schemas
        comp = openapi_schemas[schema_name]
        assert set(comp.get("properties", ())) == expected_props
        assert set(comp.get("required", ())) == expected_required


def test_contract_markdown_model_fields_subset_of_schemas():
    contract_text = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    block_match = re.search(
        r"Models \(aimo/models\.py, pydantic\)\s*```([\s\S]*?)```",
        contract_text,
    )
    assert block_match is not None
    block = block_match.group(1)

    parsed_models: dict[str, set[str]] = {}
    current_name: str | None = None
    current_body: list[str] = []
    for line in block.strip().splitlines():
        head = re.match(r"^([A-Z][A-Za-z0-9_]*)\s+\{(.*)$", line)
        if head:
            if current_name is not None:
                parsed_models[current_name] = _extract_top_level_fields(" ".join(current_body))
            current_name = head.group(1)
            current_body = [head.group(2)]
        elif line.startswith(" ") and current_name is not None:
            current_body.append(line.strip())
        else:
            if current_name is not None:
                parsed_models[current_name] = _extract_top_level_fields(" ".join(current_body))
            current_name = None
            current_body = []
    if current_name is not None:
        parsed_models[current_name] = _extract_top_level_fields(" ".join(current_body))

    expected_in_block = {
        "Constraint",
        "NeedItem",
        "Need",
        "Product",
        "Offer",
        "QuoteLine",
        "Quote",
        "Order",
        "Decision",
        "Mandate",
        "AuditEvent",
    }
    assert set(parsed_models) == expected_in_block
    for name, doc_fields in parsed_models.items():
        schema_props, _ = EXPECTED_SCHEMAS[name]
        assert doc_fields <= schema_props, (name, doc_fields - schema_props)


def _extract_top_level_fields(raw: str) -> set[str]:
    trimmed = raw.strip()
    if trimmed.endswith("}"):
        trimmed = trimmed[:-1]
    fields: set[str] = set()
    depth = 0
    token_chars: list[str] = []
    for ch in trimmed + ",":
        if ch in "{[":
            depth += 1
            token_chars.append(ch)
        elif ch in "}]":
            depth = max(0, depth - 1)
            token_chars.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(token_chars).strip()
            token_chars = []
            if part:
                fname = part.split(":", 1)[0].strip()
                if fname:
                    fields.add(fname)
        else:
            token_chars.append(ch)
    return fields


def test_schema_field_constraints_enums_and_defaults(isolated_client):
    spec = isolated_client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    qty_prop = schemas["QuoteRequestLine"]["properties"]["quantity"]
    assert qty_prop["type"] == "number"
    assert qty_prop["exclusiveMinimum"] == 0

    sub_prop = schemas["Mandate"]["properties"]["substitutions"]
    assert set(sub_prop["enum"]) == {"never", "within_prefs", "ask"}
    assert sub_prop["default"] == "ask"

    ask_prop = schemas["PlaceOrderRequest"]["properties"]["ask_human"]
    assert ask_prop["type"] == "boolean"
    assert ask_prop["default"] is False

    parse_resp = schemas["ParseResponse"]
    need_props, need_req = EXPECTED_SCHEMAS["Need"]
    assert set(parse_resp["properties"]) == (need_props | {"summary"})
    assert set(parse_resp["required"]) == need_req

    decision_schema = models.Decision.model_json_schema()
    assert set(decision_schema["properties"]["outcome"]["enum"]) == {
        "auto",
        "needs_approval",
        "blocked",
    }

    order_schema = models.Order.model_json_schema()
    assert set(order_schema["properties"]["status"]["enum"]) == {
        "draft",
        "pending_approval",
        "approved",
        "placed",
        "confirmed",
        "rejected",
        "blocked",
        "expired",
    }

    offer_schema = models.Offer.model_json_schema()
    tier_any = offer_schema["properties"]["tier"]["anyOf"]
    tier_enums = [entry["enum"] for entry in tier_any if "enum" in entry]
    assert tier_enums and set(tier_enums[0]) == {"exact", "good", "better", "best"}


def test_nested_contract_subschemas_match_contract():
    subschemas = {
        "UnitSize": ({"value", "unit"}, {"value", "unit"}),
        "Reason": ({"code", "text"}, {"code", "text"}),
        "Substitution": (
            {"original_id", "substitute_id", "reason"},
            {"original_id", "substitute_id", "reason"},
        ),
        "OfferGroup": (
            {"need_item", "offers", "shortfall", "alternatives", "options"},
            {"need_item", "offers"},
        ),
        "OffersResponse": ({"items"}, {"items"}),
        "AuditTrail": ({"events", "text"}, {"events", "text"}),
    }
    for name, (props, req) in subschemas.items():
        schema = getattr(models, name).model_json_schema()
        assert set(schema.get("properties", ())) == props
        assert set(schema.get("required", ())) == req


@pytest.mark.parametrize(
    "method,path,headers,payload",
    [
        ("POST", "/v1/needs/parse", {}, {"text": "10L milk"}),
        ("POST", "/v1/offers", AGENT_HEADERS, {"customer": "hotel-aurora"}),
        ("POST", "/v1/quotes", AGENT_HEADERS, {"offers": []}),
        ("POST", "/v1/orders", AGENT_HEADERS, {"quote_id": "q_1"}),
        ("POST", "/v1/approvals/ord_1", ADMIN_HEADERS, {"approve": True}),
        ("PUT", "/v1/mandate", ADMIN_HEADERS, {"customer": "hotel-aurora"}),
        ("POST", "/v1/admin/simulate", ADMIN_HEADERS, {"product_id": "P1"}),
    ],
)
def test_missing_required_request_fields_return_422(
    isolated_client, method, path, headers, payload
):
    resp = isolated_client.request(method, path, json=payload, headers=headers)
    assert resp.status_code == 422
    assert isinstance(resp.json().get("detail"), list)


def test_security_schemes_and_route_security_declarations(isolated_client):
    spec = isolated_client.get("/openapi.json").json()
    sec_schemes = spec["components"]["securitySchemes"]
    assert set(sec_schemes) == {"AgentKey", "AdminKey"}
    assert sec_schemes["AgentKey"]["type"] == "apiKey"
    assert sec_schemes["AgentKey"]["in"] == "header"
    assert sec_schemes["AgentKey"]["name"] == "X-Agent-Key"
    assert sec_schemes["AdminKey"]["type"] == "apiKey"
    assert sec_schemes["AdminKey"]["in"] == "header"
    assert sec_schemes["AdminKey"]["name"] == "X-Admin-Key"

    card = isolated_client.get("/.well-known/agent-card.json").json()
    assert card["auth"]["agent"] == {"type": "apiKey", "in": "header", "name": "X-Agent-Key"}
    assert card["auth"]["admin"] == {"type": "apiKey", "in": "header", "name": "X-Admin-Key"}

    paths = spec["paths"]
    for path, method in [
        ("/health", "get"),
        ("/.well-known/agent-card.json", "get"),
        ("/.well-known/jwks.json", "get"),
        ("/v1/products", "get"),
        ("/v1/products/{product_id}", "get"),
    ]:
        assert "security" not in paths[path][method], (method, path)

    for path, method in [
        ("/v1/needs/parse", "post"),
        ("/v1/offers", "post"),
        ("/v1/quotes", "post"),
        ("/v1/orders", "post"),
    ]:
        assert paths[path][method]["security"] == [{"AgentKey": []}], (method, path)

    for path, method in [
        ("/v1/orders", "get"),
        ("/v1/orders/{order_id}", "get"),
        ("/v1/approvals", "get"),
        ("/v1/audit", "get"),
        ("/v1/mandate", "get"),
    ]:
        sec = paths[path][method]["security"]
        assert {"AgentKey": []} in sec and {"AdminKey": []} in sec, (method, path)

    for path, method in [
        ("/v1/mandate", "put"),
        ("/v1/approvals/{order_id}", "post"),
        ("/v1/admin/simulate", "post"),
        ("/v1/admin/simulate", "delete"),
    ]:
        assert paths[path][method]["security"] == [{"AdminKey": []}], (method, path)


def test_public_routes_work_without_auth_headers(isolated_client, monkeypatch):
    milk = _synthetic_milk()
    _install_synthetic_backend(monkeypatch, milk, _future_day())

    r_health = isolated_client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"ok": True}

    r_card = isolated_client.get("/.well-known/agent-card.json")
    assert r_card.status_code == 200
    assert r_card.json()["endpoints"]["openapi"] == "/openapi.json"

    r_jwks = isolated_client.get("/.well-known/jwks.json")
    assert r_jwks.status_code == 200
    jwks_keys = r_jwks.json()["keys"]
    assert jwks_keys and jwks_keys[0]["kty"] == "OKP" and "d" not in jwks_keys[0]

    r_parse = isolated_client.post(
        "/v1/needs/parse",
        json={"text": "20 litres of lactose-free milk by Friday", "customer": "hotel-aurora"},
    )
    assert r_parse.status_code == 200
    parsed_body = r_parse.json()
    need = models.Need.model_validate(parsed_body)
    assert need.customer == "hotel-aurora" and len(need.items) >= 1
    assert isinstance(parsed_body.get("summary"), str)

    r_prods = isolated_client.get("/v1/products?q=milk&lactose_free=true")
    assert r_prods.status_code == 200
    prods = [models.Product.model_validate(p) for p in r_prods.json()]
    assert len(prods) == 1 and prods[0].product_id == milk.product_id

    r_prod = isolated_client.get(f"/v1/products/{milk.product_id}")
    assert r_prod.status_code == 200
    assert models.Product.model_validate(r_prod.json()).product_id == milk.product_id

    r_missing = isolated_client.get("/v1/products/NO-SUCH-ID")
    assert r_missing.status_code == 404
    assert r_missing.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        (
            "POST",
            "/v1/offers",
            {
                "customer": "hotel-aurora",
                "deliver_by": "2026-11-20",
                "items": [{"query": "milk", "quantity": 10, "unit": "l"}],
            },
        ),
        (
            "POST",
            "/v1/quotes",
            {
                "offers": [{"product_id": "SYNTH-MILK-1", "quantity": 10}],
                "delivery_date": "2026-11-20",
            },
        ),
        (
            "POST",
            "/v1/orders",
            {"quote_id": "q_1", "jws": "bad.jws.sig"},
        ),
        ("GET", "/v1/orders", None),
        ("GET", "/v1/orders/ord_1", None),
        ("GET", "/v1/approvals", None),
        ("GET", "/v1/audit", None),
        ("GET", "/v1/mandate", None),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Agent-Key": ""},
        {"X-Agent-Key": "invalid-agent-key"},
    ],
)
def test_missing_or_bad_agent_key_returns_401(isolated_client, method, path, payload, headers):
    resp = isolated_client.request(method, path, json=payload, headers=headers)
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "unauthorized"
    assert isinstance(body["text"], str) and body["text"]


def test_cross_customer_agent_key_returns_403(isolated_client):
    for path in [
        "/v1/mandate?customer=school-hervanta",
        "/v1/orders?customer=school-hervanta",
        "/v1/approvals?customer=school-hervanta",
        "/v1/audit?customer=school-hervanta",
    ]:
        resp = isolated_client.get(path, headers=AGENT_HEADERS)
        assert resp.status_code == 403, path
        assert resp.json()["code"] == "forbidden"

    offer_resp = isolated_client.post(
        "/v1/offers",
        json={
            "customer": "school-hervanta",
            "deliver_by": _future_day().isoformat(),
            "items": [{"query": "milk", "quantity": 5, "unit": "l"}],
        },
        headers=AGENT_HEADERS,
    )
    assert offer_resp.status_code == 403
    assert offer_resp.json()["code"] == "forbidden"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        (
            "PUT",
            "/v1/mandate",
            {
                "customer": "hotel-aurora",
                "agent_id": "buyer-aurora",
                "per_order_eur": 2500.0,
                "per_week_eur": 15000.0,
                "exclude_allergens": [],
                "substitutions": "within_prefs",
                "preferred_brands": ["Valio"],
                "approvers": ["Laura Virtanen"],
            },
        ),
        (
            "POST",
            "/v1/approvals/ord_nonexistent",
            {"approve": True, "by": "Laura Virtanen", "note": "ok"},
        ),
        (
            "POST",
            "/v1/admin/simulate",
            {"product_id": "SYNTH-MILK-1", "available_amount": 0.0},
        ),
        (
            "DELETE",
            "/v1/admin/simulate",
            None,
        ),
    ],
)
def test_admin_routes_missing_or_bad_admin_key_return_401_or_403(
    isolated_client, method, path, payload
):
    r_missing = isolated_client.request(method, path, json=payload)
    assert r_missing.status_code == 401
    assert r_missing.json()["code"] == "unauthorized"

    r_bad = isolated_client.request(
        method, path, json=payload, headers={"X-Admin-Key": "wrong-admin-key"}
    )
    assert r_bad.status_code == 401
    assert r_bad.json()["code"] == "unauthorized"

    r_agent_only = isolated_client.request(method, path, json=payload, headers=AGENT_HEADERS)
    assert r_agent_only.status_code == 403
    assert r_agent_only.json()["code"] == "forbidden"

    r_bad_with_agent = isolated_client.request(
        method,
        path,
        json=payload,
        headers={"X-Admin-Key": "wrong-admin-key", **AGENT_HEADERS},
    )
    assert r_bad_with_agent.status_code == 401
    assert r_bad_with_agent.json()["code"] == "unauthorized"


@pytest.mark.parametrize("bad_qty", [0, 0.0, -0.01, -1, -500])
def test_quote_request_line_non_positive_quantity_returns_422(isolated_client, bad_qty):
    resp = isolated_client.post(
        "/v1/quotes",
        json={
            "offers": [{"product_id": "SYNTH-MILK-1", "quantity": bad_qty}],
            "delivery_date": _future_day().isoformat(),
        },
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 422
    err = resp.json()
    assert isinstance(err.get("detail"), list) and err["detail"]
    assert any("quantity" in [str(x) for x in item.get("loc", ())] for item in err["detail"])


def test_live_routes_return_contract_models_without_loading_catalog_file(
    isolated_client, monkeypatch
):
    day = _future_day(3)
    milk = _synthetic_milk()
    _install_synthetic_backend(monkeypatch, milk, day)

    r_offers = isolated_client.post(
        "/v1/offers",
        json={
            "customer": "hotel-aurora",
            "deliver_by": day.isoformat(),
            "items": [
                {
                    "query": "milk",
                    "quantity": 20,
                    "unit": "l",
                    "constraints": {"lactose_free": True},
                }
            ],
        },
        headers=AGENT_HEADERS,
    )
    assert r_offers.status_code == 200
    offers_model = models.OffersResponse.model_validate(r_offers.json())
    assert len(offers_model.items) == 1 and len(offers_model.items[0].offers) >= 1
    chosen = offers_model.items[0].offers[0]
    assert chosen.product_id == milk.product_id

    r_quote = isolated_client.post(
        "/v1/quotes",
        json={
            "offers": [{"product_id": chosen.product_id, "quantity": chosen.units_needed}],
            "delivery_date": day.isoformat(),
        },
        headers=AGENT_HEADERS,
    )
    assert r_quote.status_code == 200
    quote = models.Quote.model_validate(r_quote.json())
    assert quote.customer == "hotel-aurora" and quote.jws and len(quote.lines) == 1

    r_order = isolated_client.post(
        "/v1/orders",
        json={
            "quote_id": quote.quote_id,
            "jws": quote.jws,
            "ask_human": True,
            "note": "Please double-check milk quantity",
        },
        headers=AGENT_HEADERS,
    )
    assert r_order.status_code == 200
    order = models.Order.model_validate(r_order.json())
    assert order.status == "pending_approval"
    assert order.decision.outcome == "needs_approval"

    r_pending = isolated_client.get("/v1/approvals", headers=AGENT_HEADERS)
    assert r_pending.status_code == 200
    pending_orders = [models.Order.model_validate(o) for o in r_pending.json()]
    assert [o.order_id for o in pending_orders] == [order.order_id]

    r_decide = isolated_client.post(
        f"/v1/approvals/{order.order_id}",
        json={"approve": True, "by": "Laura Virtanen", "note": "Approved"},
        headers=ADMIN_HEADERS,
    )
    assert r_decide.status_code == 200
    decided = models.Order.model_validate(r_decide.json())
    assert decided.status in ("placed", "confirmed")

    r_get_order = isolated_client.get(f"/v1/orders/{order.order_id}", headers=AGENT_HEADERS)
    assert r_get_order.status_code == 200
    assert models.Order.model_validate(r_get_order.json()).order_id == order.order_id

    r_list_orders = isolated_client.get("/v1/orders", headers=ADMIN_HEADERS)
    assert r_list_orders.status_code == 200
    listed_orders = [models.Order.model_validate(o) for o in r_list_orders.json()]
    assert any(o.order_id == order.order_id for o in listed_orders)

    r_audit = isolated_client.get("/v1/audit?customer=hotel-aurora", headers=ADMIN_HEADERS)
    assert r_audit.status_code == 200
    audit_payload = r_audit.json()
    trail = models.AuditTrail.model_validate(audit_payload)
    assert len(trail.events) >= 3 and trail.text
    assert audit_payload["verified"] == {"ok": True, "bad_seq": None}
    assert audit_payload["checkpoint"]["jws"]

    r_mandate = isolated_client.get("/v1/mandate", headers=AGENT_HEADERS)
    assert r_mandate.status_code == 200
    mandate = models.Mandate.model_validate(r_mandate.json())
    assert mandate.customer == "hotel-aurora"

    updated_mandate = mandate.model_copy(update={"per_order_eur": 2600.0})
    r_put_mandate = isolated_client.put(
        "/v1/mandate",
        json=updated_mandate.model_dump(),
        headers=ADMIN_HEADERS,
    )
    assert r_put_mandate.status_code == 200
    assert models.Mandate.model_validate(r_put_mandate.json()).per_order_eur == 2600.0

    r_sim = isolated_client.post(
        "/v1/admin/simulate",
        json={"product_id": "UNORDERED-PROD", "available_amount": 0.0},
        headers=ADMIN_HEADERS,
    )
    assert r_sim.status_code == 200 and r_sim.json() == {"ok": True}

    r_clear = isolated_client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS)
    assert r_clear.status_code == 200 and r_clear.json() == {"ok": True}

    assert not catalog._state
