# Interfaces (contract between components)

Models live in `aimo/models.py`. Append here; never silently change someone else's contract. Owner in brackets.

## Python seams
```python
# aimo/backend/__init__.py  [gateway]
class BackendClient(Protocol):
    def price(self, product_ids: list[str], requested_date: date) -> dict: ...         # raw /api/price JSON
    def availability(self, product_ids: list[str], requested_date: date) -> dict: ...  # raw /api/availability JSON
def get_client() -> BackendClient            # cached, retrying, optional fault injection
def get_offers(product_ids: list[str], requested_date: date) -> dict[str, Offer]       # merged price+availability

# aimo/backend/catalog_store.py  [catalog]
def load_catalog() -> None                    # builds data/catalog.sqlite from data/*.json|csv if missing
# aimo/catalog/__init__.py  [catalog]
def search(query: str, *, claims: list[str] = [], category: str | None = None,
           content_unit: str | None = None, limit: int = 20) -> list[ProductSummary]
def get_product(product_id: str) -> Product | None
def rank_substitutes(need: Need, offers: dict[str, Offer] | None = None, limit: int = 12) -> list[Candidate]
def plan_need(need: Need) -> Plan             # calls search + get_offers + rank_substitutes; splits lines

# aimo/trust/__init__.py  [trust]
def load_policy(policy_id: str) -> dict
def evaluate(quote: Quote, mandate: Mandate) -> PolicyDecision
def sign_quote(quote: Quote) -> Quote         # fills quote_hash, signature, kid
def verify_quote(quote: Quote) -> bool
def screen_text(text: str) -> dict            # {"suspicious": bool, "reasons": [...]} prompt-injection screen
audit: AuditLog                               # .append(actor, action, subject_id, summary, data) -> AuditEvent
                                              # .events(subject_id=None) -> list[AuditEvent]
                                              # .verify() -> {"ok": bool, "checked": int, "broken_at": int|None}
                                              # .render(subject_id) -> str  (markdown, human-readable)
def jwks() -> dict                            # public key(s) for /.well-known/jwks.json
```

## HTTP API (OpenAPI 3.1 at /openapi.json, docs at /docs)  [gateway]
Headers: `X-Agent-Id`, `X-Customer-Id` (demo identity; production: OAuth 2.1 bearer with scopes).
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/.well-known/otap.json` | | manifest: name, version, endpoints, mcp url, jwks url, policies, standards |
| GET | `/.well-known/jwks.json` | | JWKS (Ed25519 OKP) |
| GET | `/v1/products/search` | `q, claims (csv), category, limit` | `ProductSummary[]` |
| GET | `/v1/products/{id}` | | `Product` |
| POST | `/v1/offers` | `{product_ids[], requested_date, customer_id}` | `Offer[]` (live mock API) |
| POST | `/v1/plan` | `Need` | `Plan` (good-better-best + split) |
| POST | `/v1/quotes` | `{customer_id, agent_id, delivery_date, lines:[{product_id, qty_sales_units, tier?}], need?}` | signed `Quote` (prices re-fetched live, fixed until expires_at) |
| GET | `/v1/quotes/{id}` | | `Quote` |
| POST | `/v1/orders` | `{quote_id, quote_hash}` (+ mandate from headers) | `Order` (placed or pending_approval or rejected) |
| GET | `/v1/orders/{id}` | | `Order` |
| GET | `/v1/approvals` | `status=pending` | `[{order, quote, decision}]` |
| POST | `/v1/approvals/{order_id}/decision` | `{decision: approve|reject, approver, note, quote_hash}` | `Order` |
| GET | `/v1/audit` | `subject_id?` | `AuditEvent[]` |
| GET | `/v1/audit/verify` | | `{ok, checked, broken_at}` |
| GET | `/v1/audit/report/{subject_id}` | | `text/markdown` human-readable trail |
| POST | `/v1/agent/runs` | `{text, customer_id, model?}` | `{run_id}` [buyer] |
| GET | `/v1/agent/runs/{run_id}/events` | | SSE stream of `AgentStep` [buyer] |
| GET | `/v1/customers` | | demo customers + mandates + policy summaries [trust] |

## MCP tools (server name `valio-aimo-trusted-trade`, mounted at `/mcp`, also `python -m aimo.gateway.mcp_server` stdio)  [gateway]
`search_products(query, claims?, limit?)`, `get_product(product_id)`, `get_offers(product_ids, requested_date)`,
`plan_need(need)`, `create_quote(delivery_date, lines, need?)`, `place_order(quote_id, quote_hash)`,
`get_order_status(order_id)`, `get_audit_trail(subject_id)`, `list_policies()`.
The tools take the same args as HTTP and return the same JSON. Identity comes from the MCP session/env (`AIMO_AGENT_ID`, `AIMO_CUSTOMER_ID`).

## Demo customers  [trust]
- `hotel-aurora`: auto-approve ≤ €500/order; €500–5,000 needs `procurement_manager`; > €5,000 reject. Substitutions: same claims required; tier best → allow; better/good → needs approval. Category scope: dairy, bakery, produce.
- `school-tampere`: excluded allergens milk, nuts; auto ≤ €300; any substitution → `kitchen_manager`; allergen unknown → reject the line.
- `hospital-meilahti`: excluded gluten; `dietitian` approves any substitution; unknown allergen → needs approval by dietitian.
