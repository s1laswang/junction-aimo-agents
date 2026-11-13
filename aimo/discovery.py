from fastapi import APIRouter, Request

router = APIRouter()

TOOLS = [
    {"name": "search_products", "rest": "GET /v1/products", "auth": "none", "description": "Search the catalog with constraints."},
    {"name": "parse_request", "rest": "POST /v1/needs/parse", "auth": "none", "description": "Turn free text into a structured Need."},
    {"name": "get_offers", "rest": "POST /v1/offers", "auth": "agent", "description": "Ranked offers with price, stock, delivery and substitutes."},
    {"name": "request_quote", "rest": "POST /v1/quotes", "auth": "agent", "description": "Signed quote for chosen offers."},
    {"name": "place_order", "rest": "POST /v1/orders", "auth": "agent", "description": "Place an order; policy decides auto, needs_approval or blocked."},
    {"name": "get_order", "rest": "GET /v1/orders/{id}", "auth": "agent", "description": "Order status and decision."},
    {"name": "list_orders", "rest": "GET /v1/orders", "auth": "agent", "description": "Orders of this customer."},
    {"name": "list_approvals", "rest": "GET /v1/approvals", "auth": "agent", "description": "Orders waiting for human approval."},
    {"name": "decide_approval", "rest": "POST /v1/approvals/{order_id}", "auth": "admin", "description": "Human approves or rejects a pending order."},
    {"name": "get_audit_trail", "rest": "GET /v1/audit", "auth": "agent", "description": "Hash-chained audit events with a plain-language summary."},
    {"name": "get_mandate", "rest": "GET /v1/mandate", "auth": "agent", "description": "The spending and allergen mandate of this agent."},
]


@router.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    from aimo import mandates
    base = str(request.base_url).rstrip("/")
    return {
        "name": "AIMO Trusted Trade Gateway",
        "description": "Gateway for external purchasing agents: products, signed quotes, mandate-checked orders, human approval, audit.",
        "version": "0.1.0",
        "endpoints": {"rest": f"{base}/v1", "mcp": f"{base}/mcp", "openapi": f"{base}/openapi.json", "jwks": f"{base}/.well-known/jwks.json"},
        "transports": ["rest", "mcp-streamable-http", "mcp-stdio"],
        "auth": {
            "agent": {"header": "X-Agent-Key", "scope": "calls are limited to the customer of the key"},
            "admin": {"header": "X-Admin-Key", "scope": "human side: approvals, mandates, simulation"},
        },
        "tools": TOOLS,
        "policies": {
            "quotes": "Ed25519 signed JWS, verified at order time, valid until supplier cutoff.",
            "orders": "Each mandate sets per-order and per-week EUR limits, allergen exclusions and substitution rules; outcome is auto, needs_approval or blocked.",
            "approvals": "Orders outside the mandate wait for a named human approver.",
            "audit": "Every step is a hash-chained event with a plain-language reason.",
            "mandates": [{"customer": m.customer, "per_order_eur": m.per_order_eur, "per_week_eur": m.per_week_eur,
                          "exclude_allergens": m.exclude_allergens, "substitutions": m.substitutions} for m in mandates.all()],
        },
    }


@router.get("/.well-known/jwks.json")
def jwks():
    from aimo import keys
    return keys.jwks()
