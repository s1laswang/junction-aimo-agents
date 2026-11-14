"""The Trusted Trade wholesaler profile ("otap/0.1"): what any conforming wholesaler gateway must expose.

Normative summary (docs/OPENNESS.md has the prose):
  discovery   GET /.well-known/otap.json with OTAP_FIELDS; GET /.well-known/jwks.json (Ed25519 OKP, kid = RFC 7638)
  OpenAPI     OPENAPI_PATHS at otap.openapi_url
  MCP         streamable HTTP at otap.mcp.url, tools MCP_TOOLS with at least the required arguments
  identity    X-Customer-Id / X-Agent-Id headers (demo); production: OAuth 2.1 bearer + scopes
  quotes      signed: quote_hash = sha256(canonical_json(quote - {quote_hash, signature, kid})), signature =
              compact JWS EdDSA with payload {quote_id, quote_hash, ...}, expires_at = price lock
  orders      POST /v1/orders {quote_id, quote_hash}: wrong hash -> 409; only the quoting agent may submit (403);
              status placed | pending_approval | rejected, decision.hits[] in plain language
  approvals   POST /v1/approvals/{order_id}/decision {decision, approver, quote_hash}: bound to the quote hash
              (wrong -> 409); agents can never approve (403); approve -> placed
  responses   Peppol BIS Order Response semantics: response_code and response_lines[].code in AP | CA | RE
  audit       GET /v1/audit/verify -> {ok}; events hash-chained (sha256(prev_hash + canonical_json(event core)))
              and each signed (JWS payload {typ, seq, hash})
"""
PROFILE = "otap/0.1"

OTAP_FIELDS = ["name", "version", "protocol", "openapi_url", "mcp", "jwks_url", "identity", "endpoints",
               "supported_standards", "policies"]
OTAP_ENDPOINTS = ["search_products", "get_product", "get_offers", "create_quote", "get_quote", "place_order",
                  "get_order", "approvals", "decide", "audit", "audit_verify"]

OPENAPI_PATHS = [
    "/.well-known/otap.json", "/.well-known/jwks.json", "/v1/products/search", "/v1/products/{product_id}",
    "/v1/offers", "/v1/quotes", "/v1/quotes/{quote_id}", "/v1/orders", "/v1/orders/{order_id}", "/v1/approvals",
    "/v1/approvals/{order_id}/decision", "/v1/audit", "/v1/audit/verify",
]

# tool -> required arguments (they must appear in the tool's inputSchema properties; those marked required
# by the profile must be in inputSchema.required)
MCP_TOOLS = {
    "search_products": ["query"],
    "get_product": ["product_id"],
    "get_offers": ["product_ids", "requested_date"],
    "plan_need": ["need"],
    "create_quote": ["delivery_date", "lines"],
    "place_order": ["quote_id", "quote_hash"],
    "get_order_status": ["order_id"],
    "get_audit_trail": ["subject_id"],
    "list_policies": [],
}
MCP_FORBIDDEN = ["approve", "approve_order", "decide", "decide_approval"]   # agents can never approve

PEPPOL_CODES = {"AP", "CA", "RE"}
ORDER_STATUSES = {"placed", "pending_approval", "rejected", "approved", "expired", "failed"}
