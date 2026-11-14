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

## Gateway additions (appended Sat 14 Nov by gateway; all optional / additive)
- **Models (optional fields appended):** `Offer.provenance`, `Quote.provenance`, `Quote.availability_snapshot`, `Order.provenance`.
  Provenance = `{source, upstream, endpoints, served_from: live|cache, fetched_at, valid_for_s, upstream_time_now, faults}`.
  Quote provenance also has `price_locked_until`. These are part of the signed quote body.
- **`BackendClient.price/availability`** take an optional third arg `customer` (passed upstream as `customer`).
  `get_offers(ids, date, customer_id=None, quantities=None)`: with `quantities`, `partial_stock` is tagged when stock < qty.
  `aimo.backend.client.get_offers_with_meta()` also returns the provenance dict. `aimo.backend.set_client()` swaps the client (tests).
- **Offer.issues vocabulary:** `cutoff_passed`, `terminal_unknown_qty` (Terminal 999999 sentinel -> `available_amount=None`),
  `limited_stock` (real stock limit < 100,000), `partial_stock`, `out_of_stock`, `sales_block`, `late_delivery`,
  `price_error`, `availability_not_found`, `no_schedule`, `injected:<kind>` for fault injection.
  Stock sentinels 9999999999 / 666666666 are passed through unchanged (effectively unlimited).
- **Fault injection:** env `AIMO_FAULTS` or header `X-Aimo-Faults` (HTTP and MCP): `stockout:<id|*>`, `partial:<id|*>:<n>`,
  `price_shock:<id|*>:<factor>`, `price_error:<id|*>`, `cutoff:<id|*>`, `sales_block:<id|*>`, `error_rate:<p>` (503),
  `rate_limit:<p>` (429), `latency_ms:<n>`. Upstream failures -> HTTP 503 with `Retry-After` and `detail.retry_after`.
- **HTTP:** `POST /v1/quotes` returns **409 `lines_unavailable`** with `detail.refused[] {product_id, reason: unavailable|partial_stock, issues, available_amount?}`.
  `POST /v1/orders` is idempotent per quote (and retries placement if an earlier placement failed upstream); 409 `quote_hash_mismatch` / `quote_expired`; 403 `customer_mismatch`.
  `POST /v1/approvals/{order_id}/decision` accepts optional `approver_role` (defaults to the approver directory role, then `decision.approver_role`); 409 `quote_hash_mismatch` / `quote_expired` (order -> `expired`) / `not_pending`; 403 `approval_refused` (trust.check_approval reason).
  `GET /v1/approvals?status=pending|placed|rejected|expired|all` returns `[{order, quote, decision, expired, approvals}]`. `GET /healthz` shows which integrations are live vs fallback.
  Errors are `{"error": code, "message", "detail"?}`. Offers responses carry `X-Aimo-Source/Served-From/Fetched-At/Valid-For` headers.
- **Order response:** supplier RE on every line -> `status=rejected, response_code=RE`; otherwise `status=placed` with AP or CA. Auto-approved orders have `approved_by="auto (policy <id>)"`.
- **In-process tool layer:** `aimo.gateway.service.<mcp_tool_name>(**args, identity={agent_id, customer_id})` returns exactly the MCP JSON
  (`search_products -> {products}`, `get_offers -> {offers, provenance}`, `create_quote -> Quote`, `place_order -> Order`,
  `get_order_status -> Order`, `get_audit_trail -> {events, report, verify}`, `list_policies -> {policies}`). `plan_need -> Plan` (needs aimo.catalog.plan_need, else 501).
- **MCP identity:** headers `X-Agent-Id` / `X-Customer-Id` on the streamable-HTTP connection, else env. No approval tool on MCP by design.

## Trust conventions (landed Sat 16:xx, from team/trust; see docs/TRUST.md)
- Agent ids: `<customer>/<agent>` e.g. `hotel-aurora/buyer-1` (bare `buyer-1` accepted). Unknown agent → mandate with no scopes → reject. Unknown customer → KeyError.
- Line flags read by policy: `cutoff_passed`, `unavailable`, `sales_block` → reject line; `outside_preference` and any flag containing `injection` → needs_approval; `contains:<allergen>` / `may_contain:<allergen>` used when catalog has no product. Buyer may pass screen results in `need.preferences["injection_flags"]`.
- Line-level RuleHit ids are `"<rule>:<product_id>"` so agents can drop just that line.
- Link audit events with `data.related_ids` (run ↔ quote ↔ order). Use helpers `log_request`, `log_quote`, `log_decision`, `log_order` for consistent wording; the weekly budget rule reads `order.placed`.
- Extra API: `get_mandate`, `list_customers`, `check_approval(quote, decision, approver, approver_role, quote_hash, *, decision_kind, note, order_id) -> {ok, reason}`, `explain(decision)`, `verify_quote_detailed`, `evidence_pack(subject_id)`, `tamper_for_demo/untamper_for_demo` (disabled when AIMO_ENV=production).
- Decision (CEO): out-of-scope category for hotel stays needs_approval (category grouping is inferred from codes).

## Catalog conventions (landed Sat ~15:45, from team/catalog; see docs/reports/catalog.md)
- Candidate/PlanLine flags: `cutoff_passed`, `sales_block`, `unavailable` → always tier `rejected` (match trust line flags). Others: `outside_preference`, `partial_stock`, `allergen_unknown`, `allergen_inferred_hint`, `content_from_price`, `no_live_offer`, `recipe_check_failed`.
- `Product.allergen_source` values: `structured`, `structured:claim`, `inferred:gemini`, `none`. Inferred can only block, never clear.
- Tiers without a usual product: best = all query concepts in name, better = ≥ half, good = rest that passes hard filters.
- `plan_need(need, offers_fn=None)`; `UpstreamError` from `get_offers` propagates (gateway → 503).
- CLI: `python -m aimo.catalog build|search <q>|demo`. Built artifacts in data/ (gitignored): catalog.sqlite, embeddings.npy + embeddings_ids.json, cache/gemini_allergens.json. `aimo serve` must build if missing.

## Buyer conventions (landed Sat ~16:30, from team/buyer; see aimo/buyer/)
- Router: `from aimo.buyer.runs import router` (prefix `/v1/agent`). `POST /v1/agent/runs` body
  `{text, customer_id="hotel-aurora", model?, thinking? (MINIMAL|LOW|MEDIUM|HIGH), transport? (auto|mcp|inproc|fake), escalate=true, faults?}`
  → `{run_id, events_url}`. `GET /v1/agent/runs` (recent runs), `GET /v1/agent/runs/{id}` → `{run_id, status, summary, order_status, latency_s, steps[]}`.
- SSE `GET /v1/agent/runs/{id}/events`: each event has `id: <seq>`, `event: <AgentStep.kind>`, `data: <AgentStep JSON>`; replays from the
  start (or after `Last-Event-ID`), closes after `final`. Step `data` keys the UI can rely on:
  `thought` (+`need` on the parse step), `tool_call` {name, args, model, thinking, latency_s} or {name, blocked},
  `tool_result` {name, result, latency_s}, `plan` {plan, rationale}, `quote` {quote}, `decision` {order_id, status, decision},
  `approval_wait` {order_id, approver_role, quote_id, quote_hash, hits}, `order` {order}, `error` {error}, `final`
  {summary, status, audit_report, latency_s, models_used, escalated, injections}. `final.status` is the order status or `no_order` / `needs_confirmation` / `error`.
- Server-side runs reach the gateway over MCP by default: env `AIMO_BUYER_TRANSPORT` (auto) and `AIMO_MCP_URL` (http://localhost:8000/mcp). Max 8 concurrent runs (`AIMO_BUYER_MAX_RUNS`).
- Identity: the buyer sends `X-Agent-Id: buyer-1` (bare agent name) and `X-Customer-Id`; optional `X-Aimo-Faults` per scenario.
- Screen results go to `need.preferences["injection_flags"]` on `create_quote` (trust convention).
- CLI: `aimo demo [...]` → `aimo.buyer.run_demo:main()` (argparse over sys.argv). Flags: `--scenario happy|stockout|school-allergen|injection|drift|event`,
  `--auto-approve-as EMAIL`, `--reject`, `--transport`, `--base-url`, `--via-server`, `--model`, `--thinking`, `--no-escalate`, `--faults`, `--json PATH`.

- Decisions (CEO): trust sends > 3× usual week to approval; agent itself holds at ≥ 10× (likely unit error) as needs_confirmation. Hydrogen-HIGH escalation stays on in the hero demo (visible Gemini thinking control).

## Gateway round 2 (Sat evening)
- **Agent identity normalised in one place** (`aimo.gateway.service.Identity` / `normalize_agent_id`): `X-Agent-Id` may be `buyer-1` or `hotel-aurora/buyer-1`; `Quote.agent_id`/`Order.agent_id` store the bare name, audit actors are always `agent:<customer>/<agent>`. An id prefixed with another customer is kept whole (mandate lookup rejects it).
- **`Order.approver_role`** (optional, appended to models.py). `Order.approved_by` is now the plain approver id (e.g. `anna@hotel-aurora.example`); auto-approved orders have `approved_by="policy:<policy_id>"`, `approver_role=null`.
- **Fault `inject_text:<id|*>`**: gateway product reads (`get_product`, `search_products`, HTTP and MCP) append a planted prompt injection to `marketing_text` and add label `injected:inject_text`. Search results for injected products are full `Product` objects (with `marketing_text`).
- **Quote line flag `supplier_text_injection_suspected`**: added when trust's `screen_text` flags a line's supplier text (contains "injection", so the policy `injection` rule fires). The gateway also passes its product reads to `trust.evaluate(..., products=...)`.
- **Demo-only:** `POST /v1/demo/tamper/{seq}[?summary=...]` and `POST /v1/demo/untamper/{seq}` (tag `demo`) -> `{..., verify: {...}}`; 404 when `AIMO_ENV=production` or the event does not exist.
- **`aimo serve`**: fetch data -> `load_catalog()` -> if embeddings / allergen hints are missing and `GEMINI_API_KEY` is set, builds them in a background thread (FTS until done). `--build-sync` blocks, `--no-build` skips and prints `uv run python -m aimo.catalog build`.

## Gateway round 3 (Sat night): server-side line verification
- **`POST /v1/quotes` / `create_quote` ignore the caller's `tier` and `flags`.** The gateway derives them (`aimo/gateway/verify.py`):
  tier `preferred` only for the customer's usual products held server-side (trust policy `preferred_products: [ids]` or `standing_orders[].product_id(s)`
  when present; until then the gateway registry `hotel-aurora: [463038]`); otherwise `aimo.catalog.substitution.rank_substitutes` against the usual product
  of the same kind (same category / product type) using the quote's `need` (claims, allergens; caller `preferences` and `preferred_product_ids` are NOT used)
  or a minimal need built from the product's dietary claims. Flags: `cutoff_passed`, `unavailable`, `sales_block`, `partial_stock` (stock < line qty),
  `allergen_unknown` (need + policy excluded allergens), `outside_preference`, `supplier_text_injection_suspected`, `limited_stock`, `terminal_unknown_qty`,
  `injected:*`, `tier_unverified` (catalog unavailable: tier `good`).
- **Provenance:** `Quote.provenance.client_claims` = what the agent sent `[{product_id, tier, flags}]`; `provenance.line_verification` = `{pid: {tier, method, reasons}}`.
  When they differ the audit gets `quote.claims_corrected` ("agent said 469363 is preferred, gateway verified better …").
- **Identity:** headers (MCP session / in-process `identity`) win. A body `customer_id` / `agent_id` that disagrees -> **403 `customer_mismatch` / `agent_mismatch`** (quotes and offers). Without the header the body value is used (demo convenience).
- **Scope at quote time:** agents whose mandate lacks `quote:create` (e.g. unregistered `mallory`) get **403 `scope_missing`**; the refusal is audited as `request.refused`.
- **Live re-fetch:** quotes and order placement call the mock API with `fresh=True` (bypass the 5-minute cache, refresh it); `provenance.served_from="live"`, `fresh_requested=true`. Search/offers browsing stays cached. `BackendClient.price/availability` accept optional `fresh=`.
