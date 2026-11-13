from fastapi import APIRouter

from aimo import keys, mandates

router = APIRouter()

TOOLS = [
    ("search_products", "GET", "/v1/products", "Search the catalog with constraints such as lactose free, allergens, origin and brand."),
    ("get_product", "GET", "/v1/products/{id}", "Read one product by id."),
    ("parse_request", "POST", "/v1/needs/parse", "Turn a free-text purchase request into a structured Need."),
    ("get_offers", "POST", "/v1/offers", "Ranked offers with price, availability, reliability and CO2 for each item of a Need."),
    ("request_quote", "POST", "/v1/quotes", "Get a signed quote for chosen offers and a delivery date."),
    ("place_order", "POST", "/v1/orders", "Redeem a signed quote. Policy decides auto, needs_approval or blocked."),
    ("get_order", "GET", "/v1/orders/{id}", "Read one order with its decision and status."),
    ("list_orders", "GET", "/v1/orders", "List orders by customer and status."),
    ("list_approvals", "GET", "/v1/approvals", "Orders waiting for human approval."),
    ("decide_approval", "POST", "/v1/approvals/{order_id}", "Approve or reject a pending order as a named approver."),
    ("get_audit_trail", "GET", "/v1/audit", "Hash-chained audit events with a plain-language summary."),
    ("get_mandate", "GET", "/v1/mandate", "The calling agent's spending and allergen mandate."),
]


@router.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "AIMO Trusted Trade Gateway",
        "description": "Gateway for external purchasing agents to find products, compare offers and place orders with the right human approval, without exposing Valio Aimo systems.",
        "endpoints": {"rest": "/v1", "mcp": "/mcp", "openapi": "/openapi.json", "jwks": "/.well-known/jwks.json"},
        "auth": {
            "agent": {"type": "apiKey", "in": "header", "name": "X-Agent-Key"},
            "admin": {"type": "apiKey", "in": "header", "name": "X-Admin-Key"},
        },
        "tools": [
            {"name": n, "method": m, "route": r, "description": d} for n, m, r, d in TOOLS
        ],
        "policies": {
            "quotes": "Signed with EdDSA (JWS), single use, valid until the product order cutoff (closingTime).",
            "quote_verification": {
                "format": "JWS compact",
                "alg": "EdDSA",
                "curve": "Ed25519",
                "keys": "/.well-known/jwks.json",
                "steps": ["GET /.well-known/jwks.json", "read the kid in the JWS protected header", "verify the signature with the matching key, algorithms pinned to EdDSA", "compare the payload with the quote you received"],
                "note": "Select the key by the kid header and pin the algorithm to EdDSA, for example joserfc with algorithms=[\"EdDSA\"].",
            },
            "decisions": ["auto", "needs_approval", "blocked"],
            "audit": "Every step is recorded in an append-only hash-chained log with a plain-language reason. GET /v1/audit returns verified {ok, bad_seq}, recomputed over the whole chain, and a checkpoint {seq, hash, ts, jws} whose jws is signed with the same EdDSA key as the quotes (check it against /.well-known/jwks.json).",
            "mandates": [
                {
                    "customer": m.customer,
                    "agent_id": m.agent_id,
                    "per_order_eur": m.per_order_eur,
                    "per_week_eur": m.per_week_eur,
                    "exclude_allergens": m.exclude_allergens,
                    "substitutions": m.substitutions,
                }
                for m in mandates.all()
            ],
        },
    }


@router.get("/.well-known/jwks.json")
def jwks():
    return keys.jwks()
