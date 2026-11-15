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
Headers: `X-Agent-Id`, `X-Customer-Id` (demo identity on `/v1` and `/mcp`, known customer required; production: OAuth 2.1 bearer with scopes); human-only routes (`POST /v1/auth/approver-session`, `/v1/approvals*`, `/v1/voice/*`, `/v1/demo/*`) require `X-Approver-Session` or `X-Approver-Pin` and refuse `X-Agent-Id`; starting, answering and listing `/v1/agent/runs` likewise require `X-Approver-Session` or `X-Approver-Pin` (`401` otherwise).
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
| GET | `/v1/orders/{id}/peppol-order-response.xml` | | `application/xml` Peppol BIS T116 UBL 2.3 Order Response |
| POST | `/v1/auth/approver-session` | `{approver}` + `X-Approver-Pin` | `{token, approver, role, expires_at}` (`X-Approver-Session`) |
| GET | `/v1/approvals` | `status=pending` | `[{order, quote, decision}]` |
| POST | `/v1/approvals/{order_id}/decision` | `{decision: approve|reject, approver, note, quote_hash}` | `Order` (or `409 requote_needed` on expired lock / cut-off / `partial_stock`) |
| GET | `/v1/audit` | `subject_id?` | `AuditEvent[]` |
| GET | `/v1/audit/verify` | | `{ok, checked, broken_at}` |
| GET | `/v1/audit/report/{subject_id}` | | `text/markdown` human-readable trail |
| POST | `/v1/agent/runs` | `{text, customer_id, model?}` + `X-Approver-Session` or `X-Approver-Pin` | `{run_id}` (`faults` and `inproc`/`fake` transports ignored unless `AIMO_DEMO_FAULTS=1`) [buyer] |
| GET | `/v1/agent/runs/{run_id}/events` | | SSE stream of `AgentStep` [buyer] |
| GET | `/v1/customers` | | demo customers + mandates + policy summaries [trust] |

## MCP tools (server name `valio-aimo-trusted-trade`, mounted at `/mcp`, also `python -m aimo.gateway.mcp_server` stdio)  [gateway]
13 tools in total (9 core `otap/0.1` profile tools plus 4 port additions: `list_recipes`, `plan_recipe`, `check_standing_order_drift`, `propose_offer`; no approval tool):
`search_products(query, claims?, limit?)`, `get_product(product_id)`, `get_offers(product_ids, requested_date)`,
`plan_need(need)`, `create_quote(delivery_date, lines, need?)`, `place_order(quote_id, quote_hash)`,
`get_order_status(order_id)`, `get_audit_trail(subject_id)`, `list_policies()`,
`list_recipes()`, `plan_recipe(recipe_id?, portions?, delivery_by?, text?, excluded_allergens?)`,
`check_standing_order_drift(customer_id?)`, `propose_offer(customer_id, product_id?, qty_sales_units?, replaces_product_id?, delivery_date?, note?)`.
The tools take the same args as HTTP and return the same JSON. Identity comes from `X-Agent-Id` / `X-Customer-Id` on streamable HTTP `/mcp` or env (`AIMO_AGENT_ID`, `AIMO_CUSTOMER_ID`) on stdio, and a known customer is enforced on both transports.

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
- **Fault injection:** env `AIMO_FAULTS` or header `X-Aimo-Faults` (HTTP and MCP; operator-only: ignored on any request carrying agent identity unless `AIMO_DEMO_FAULTS=1`, and never honoured in `AIMO_ENV=production`): `stockout:<id|*>`, `partial:<id|*>:<n>`,
  `price_shock:<id|*>:<factor>`, `price_error:<id|*>`, `cutoff:<id|*>`, `sales_block:<id|*>`, `error_rate:<p>` (503),
  `rate_limit:<p>` (429), `latency_ms:<n>`. Upstream failures -> HTTP 503 with `Retry-After` and `detail.retry_after`.
- **HTTP:** `POST /v1/quotes` returns **409 `lines_unavailable`** with `detail.refused[] {product_id, reason: unavailable|partial_stock, issues, available_amount?}`.
  `POST /v1/orders` is idempotent per quote (and retries placement if an earlier placement failed upstream); 409 `quote_hash_mismatch` / `quote_expired`; 403 `customer_mismatch`.
  `POST /v1/approvals/{order_id}/decision` accepts optional `approver_role` (defaults to the approver directory role, then `decision.approver_role`); 409 `quote_hash_mismatch` / `requote_needed` (`quote_expired`, `cutoff_passed`, `line_unavailable`, `partial_stock`) / `not_pending`; 403 `approval_refused` (trust.check_approval reason).
  `GET /v1/approvals?status=pending|placed|rejected|expired|all` returns `[{order, quote, decision, expired, approvals}]`. `GET /healthz` shows which integrations are live vs fallback.
  Errors are `{"error": code, "message", "detail"?}`. Offers responses carry `X-Aimo-Source/Served-From/Fetched-At/Valid-For` headers.
- **Order response:** supplier RE on every line -> `status=rejected, response_code=RE`; otherwise `status=placed` with AP or CA (at human approval time, live stock below `qty_sales_units` returns `409 requote_needed` with reason `partial_stock` instead of silently placing less). Auto-approved orders have `approved_by="policy:<policy_id>"`.
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
  → `{run_id, events_url}` (starting, answering and listing runs require `X-Approver-Session` or `X-Approver-Pin`; body `faults` and `inproc`/`fake` transports are ignored unless `AIMO_DEMO_FAULTS=1`, never in `AIMO_ENV=production`). `GET /v1/agent/runs` (recent runs), `GET /v1/agent/runs/{id}` → `{run_id, status, summary, order_status, latency_s, steps[]}`.
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

## Port additions (landed Sat ~19:00, from team/port; ported from Momin's junction26-valio, see docs/reports/port.md and docs/SCENARIOS.md)
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/v1/recipes` | | `[{id, name, name_fi, aliases, ingredients[{query, per_portion, unit}], note}]` |
| POST | `/v1/recipes/plan` | `{recipe_id? , text?, portions?, delivery_by?, customer_id?, excluded_allergens[], lactose_free, gluten_free, include_candidates}` | `{recipe, customer_id, delivery_by, excluded_allergens, swaps[], ingredients[{ingredient, query, quantity, unit, swap, need, plan}], uncovered[], total_net_estimate, summary, quote_request{customer_id, delivery_date, lines[]}, provenance}`. Errors: 404 `recipe_not_found`, 400 `portions_required`, 503 upstream. |
| GET | `/v1/drift/{customer_id}` | `rebaseline?` | `{customer_id, delivery_date, standing_order, flags[{product_id, kind: price\|volume\|availability\|allergen, change_pct, message, route: auto\|approval, before?, after?, fix, product_name}], summary, provenance}`. 404 `no_standing_order`. |
| POST | `/v1/proposals` | `{customer_id, product_id?, qty_sales_units?, replaces_product_id?, delivery_date?, note}` + `X-Agent-Id: valio-aimo/<agent>` | proposal `{proposal_id, seller_id, kind: cheaper_equivalent\|seller_pick, status, product, replaces, qty_sales_units, offer, why[], estimated_saving_eur, policy_preview{binding:false, outcome, approver_role, hits}, seller_can_order:false, quote_request}`. 404 `no_better_offer` / `unknown_customer`, 409 `not_orderable`. |
| GET | `/v1/proposals` | `customer_id?, status?` | proposals, newest first |
| GET | `/v1/proposals/{id}` | | proposal |
| POST | `/v1/proposals/{id}/accept` | buyer headers | `{proposal, quote, order}`. 403 `seller_cannot_order` / `customer_mismatch`, 409 `not_open`. |
| POST | `/v1/proposals/{id}/decline` | `{reason}` | proposal |

MCP tools: `list_recipes()`, `plan_recipe(recipe_id?, portions?, delivery_by?, text?, excluded_allergens?)`, `check_standing_order_drift(customer_id?)`, `propose_offer(customer_id, product_id?, qty_sales_units?, replaces_product_id?, delivery_date?, note?)`. The buyer accepts a proposal over MCP with `create_quote(proposal.quote_request…)` + `place_order`.

Audit actions: `drift.checked`, `proposal.created`, `proposal.accepted`, `proposal.declined`, `proposal.accept_refused`.


## Gateway round 4 (Sat night): quote ownership
- **`POST /v1/orders` / `place_order`: only the agent that obtained the quote may submit it** (normalised agent id + customer).
  Anyone else, including another agent of the same customer or `valio-aimo/seller-*`, gets **403 `agent_mismatch`** (wrong customer: 403 `customer_mismatch`).
  It is audited as `order.refused` on the quote, and no order row is created, so it can never take the per-quote idempotency slot.
- **`order:submit` scope is checked by the gateway before any order exists:** a mandate without it gets 403 `scope_missing` and an `order.refused` audit.
  Previously trust's `mandate_scope` rule produced a stored rejected order.

## Gateway round 5 (Swarms B/D): clock, replay, learned approvals, re-quote
- **Clock** (`aimo.backend.now()`, `aimo/backend/clock.py`): `AIMO_NOW=<ISO>` anchors a clock that runs forward from server start (`AIMO_NOW_FROZEN=1` freezes it); header `X-Aimo-Now` fixes "now" for one request (HTTP and MCP; ignored on any request carrying agent identity unless `AIMO_DEMO_FAULTS=1`, and never honoured in `AIMO_ENV=production`); replay mode anchors at the snapshot's `time_now` unless `AIMO_NOW` is set (`AIMO_REPLAY_CLOCK=real` opts out). Used for cut-offs, quote created/expires, approval expiry, and passed to `trust.evaluate(now=)` / `trust.check_approval(now=)`. Offer provenance carries `clock: {mode, now, frozen}`. **Buyer:** please resolve "by Monday" with `aimo.backend.now()` (aimo/buyer/need.py `now_helsinki`).
- **Replay** (`aimo/backend/replay.py`, wind tunnel snapshot layout): `AIMO_REPLAY=<dir>` serves price/availability from `<dir>/{price,availability}_<date>.json` with no network; unrecorded dates -> 503 (never invented). Live mode falls back automatically to `AIMO_REPLAY_FALLBACK` or `data/windtunnel/snapshot` when the upstream fails (429/5xx/timeout/unreachable, after retries; circuit opens for `AIMO_UPSTREAM_CIRCUIT_S`=30 s). Provenance `served_from: "replay"` + `replay: {recorded_at, fallback_reason}`. `/healthz` shows `clock`, `replay`, `upstream.circuit_open`.
- **Learned approvals:** `POST /v1/approvals/{order_id}/decision` accepts `remember: bool` and `remember_scope: "product"|"product_type"`. On approve the gateway calls, per substitute line,
  `aimo.trust.learned.remember_swap(customer_id=, from_product_id=<usual product used as reference or None>, to_product_id=, scope=, approver=, approver_role=, quote_id=, order_id=, note=)` (lazy import; falls back to positional `(customer_id, from_product_id, to_product_id, scope)`; never blocks the order). Result in `Order.provenance.learned`, audit `approval.remembered`.
  `GET /v1/customers/{id}/learned` -> `{customer_id, available, swaps}` from `learned.list_learned(customer_id)` (or `learned_for`/`get_learned`); 403 for another customer's header. MCP `list_policies` returns `learned_swaps` for the caller's customer. `Quote.provenance.line_verification[pid].reference` = the usual product id the tier was judged against.
- **Re-quote:** approving an order whose quote expired, whose line passed its cut-off / became undeliverable, or whose live stock fell below the quoted quantity while waiting returns **409 `requote_needed`** with `detail {reason: quote_expired|cutoff_passed|line_unavailable|partial_stock, old_order_id, new_quote, new_order (pending_approval, provenance.supersedes), changes {old/new totals and lines}, approve_with {method, path, body}}` so the buyer never silently gets less than approved. The proposal re-prices the same lines (or re-plans from `quote.need` if they cannot be delivered); if neither works, `detail` has no `new_order`. The old order becomes `expired` (`provenance.requoted_as`); audit `order.requoted`. Approving the old order again returns the same proposal. The agent path (`POST /v1/orders` on an expired quote) still returns 409 `quote_expired`.
- **Idempotency:** concurrent duplicate `POST /v1/orders` for one quote are serialised (one order). A repeated identical approval (same decision, approver, quote_hash) returns the same order with 200 instead of 409.

## Voice approval (S5, Sat night): `aimo/voice/`, mounted by a guarded hook in `aimo/gateway/app.py`
Voice never decides. It reads the order out, transcribes the answer and suggests an intent; the human confirms with one
click on `POST /v1/approvals/{order_id}/decision` (same `X-Approver-Session`, the quote hash).
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/v1/voice/status` | | `{elevenlabs, tts: elevenlabs\|browser, stt: elevenlabs\|browser, intent: gemini\|rules, decides: false}` |
| POST | `/v1/voice/prompt` | `{order_id, audio?: bool}` | `{order_id, status, quote_hash, text, approver, tts, audio_b64?, mime?, tts_error?}` |
| POST | `/v1/voice/transcribe` | multipart `audio` (+`language?`) | `{transcript, language, engine, model, approver}`; 503 `voice_unavailable` without `ELEVENLABS_API_KEY` |
| POST | `/v1/voice/intent` | `{transcript, order_id?}` | `{intent: approve\|reject\|unclear, confidence, source: gemini\|rules, note?, transcript, decides: false, confirm_with}` |
All except status need `X-Approver-Session` (401 `approver_session_required`); any `X-Agent-Id` gets 403 `agent_cannot_approve`.
Env: `ELEVENLABS_API_KEY`, optional `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_MODEL` (eleven_flash_v2_5), `ELEVENLABS_STT_MODEL` (scribe_v1).
Gemini ledger feature: `voice.intent`. A negated or mixed answer is never returned as `approve`.

## S3 `need` (Sat night): allergen block in planning, fewer escalations, split orders, price jump
- **Planning applies the customer's policy allergens.** `plan_need`, `rank_substitutes` (every caller: gateway verification, deterministic planner) merge the `allergen_block.excluded` groups of the customer's policy into `need.excluded_allergens` (aliases normalised: dairy/maito → milk, tree nuts/almond → nuts, wheat → gluten …). `Plan.need.excluded_allergens` shows the merged list. Helpers: `aimo.catalog.substitution.normalize_allergens`, `with_policy_allergens`; `aimo.trust.policy.planning_profile(customer_id)` → `{policy_id, excluded_allergens, allergen_reason, usual_products, approved_alternatives, customer_name}`.
- **Usual products first:** when the request names no usual product and no brand/fat/organic/pack preference, the planner uses the policy's `preferred_products` that match the request (unit, claims, allergens, product phrase). Internally marked `need.preferences.usual_from_profile` (not returned in `Plan.need`).
- **New PlanLine/Candidate flags:** `allergen_safe_alternative` (the request names an excluded allergen, e.g. school "milk"; the line is an allergen-free replacement a human confirms) and `supplier_text_injection_suspected` (trust's regex screen hit the product's supplier text; reason text starts "Supplier text contains instructions aimed at AI agents"). Both make the policy return `needs_approval`; the policy also detects the excluded-allergen request from `quote.need` by itself, so gateway-derived quote flags are enough.
- **Policy keys (append-only):** `substitution.new_product` (allow|needs_approval; first purchase with no usual product of that kind and an exact catalog match), `substitution.auto_approve_alternatives_below_eur` (hotel: 50). New rule types `split_order {window_days, max_factor, near_limit_share}` and `price_jump {max_increase_pct, lookback_days}`. `quantity_drift.window_days` (default 7) adds earlier placed volume. Substitution tiers are now re-checked with `aimo.catalog.substitution.rank_substitutes` inside `evaluate` (`policy.catalog_verify`).
- **`aimo.trust.learned`:** `remember_swap(...)` (signature as in Gateway round 5), `forget_swap(customer_id, to_product_id, from_product_id=None, scope=None)`, `list_learned(customer_id)`, `match(customer_id, product_id, reference_id, facts)`. Table `learned_swaps` in `config.STATE_DB`. A matching swap makes the `substitution` line `allow`; all other rules still apply.
- **Last paid price** for `price_jump`: newest `order.placed` audit event joined to its `quote.created` event; the buyer may report `need.preferences.reference_unit_price {product_id: price}` (the lower reference wins).

## S1/S2/Fleet hardening & Judge Panel Fixes A-D (Sun 15 Nov)
- **Automatic replay & structured `catalog_missing` (`aimo/backend/replay.py`, `aimo/gateway/app.py`, `aimo/gateway/mcp_server.py`, `b57af02`):** unset `AIMO_API_KEY` or `AIMO_REPLAY=1` serves `tests/fixtures/snapshot/replay/` (669 ids + gzipped 669-product catalog subset across `2026-11-16`..`2026-11-20`, `served_from: replay`); live mode falls back automatically on upstream failure (`AIMO_REPLAY_FALLBACK=0` disables). When `AIMO_REPLAY=0` and no catalog exists, BOTH HTTP (`GET /v1/products/search`, `POST /v1/plan` -> HTTP `503` `{"error": "catalog_missing", ...}`) and MCP (`search_products`, `plan_need` -> structured tool error `{"error": "catalog_missing", ...}`, `b57af02`) return `catalog_missing` (never a generic 500 or `UnexpectedToolError`), evaluated after known-customer auth and `422` input validation (`quantity <= 0` -> `bad_quantity`, etc.).
- **Known-customer `/v1` + MCP auth (`aimo/gateway/auth.py`):** known customer (`X-Customer-Id` on REST and HTTP `/mcp`, or `AIMO_CUSTOMER_ID` on stdio MCP; must match `GET /v1/customers`: `hotel-aurora`, `school-tampere`, `hospital-meilahti`) is enforced across mutating `/v1` routes, `/v1/plan`, `/v1/offers`, `/v1/products/search`, all 13 MCP tools and `/v1/agent/runs` (`401 identity_required` when missing, `403 unknown_customer` when unrecognised, `403 customer_mismatch` when header and body disagree); optional `X-Agent-Key` verified when `AIMO_AGENT_KEYS` is set.
- **Per-approver session / PIN auth (`aimo/gateway/human_auth.py`):** human-only routes (`POST /v1/auth/approver-session`, `GET /v1/approvals`, `POST /v1/approvals/{order_id}/decision`, `/v1/voice/*`, `/v1/demo/*`) refuse any `X-Agent-Id` (`403 agent_cannot_approve`) and require `X-Approver-Session` (8-hour HMAC token bound to a named approver from `POST /v1/auth/approver-session`) or `X-Approver-Pin` (`401 approver_auth_required`). Primary approver uses `data/keys/approver_pin`, every directory approver gets a distinct 12-char passphrase in `data/keys/approver_pins.json` (`AIMO_APPROVER_PINS` override; `AIMO_APPROVER_PIN` operator harness fallback), a PIN only acts as its own approver, and repeated wrong guesses return `429 approver_locked`.
- **Operator-only demo overrides & human-gated agent runs (Judge Panel Fix A + `4ccc1fb`, `aimo/backend/clock.py`, `aimo/buyer/runs.py`):** `X-Aimo-Now` and `X-Aimo-Faults` are ignored on any request carrying agent identity (`X-Agent-Id`) over REST and MCP unless the operator sets `AIMO_DEMO_FAULTS=1` (scenarios, montage, test harnesses); on `/v1/agent/runs`, starting (`POST /v1/agent/runs`), answering (`POST /v1/agent/runs/{id}/answer`) and listing (`GET /v1/agent/runs`) require a signed-in human (`X-Approver-Session` or `X-Approver-Pin`, `401` otherwise), and body `faults` plus caller-chosen `inproc`/`fake` transports are ignored unless `AIMO_DEMO_FAULTS=1` (the response lists ignored fields). None of these overrides are ever honoured in `AIMO_ENV=production`.
- **Partial-stock approval re-quote (Judge Panel Fix B, `aimo/gateway/service.py`):** if available stock at human approval time is below `qty_sales_units`, `POST /v1/approvals/{order_id}/decision` returns **409 `requote_needed`** with `detail.reason = "partial_stock"` and a replacement signed quote/order instead of placing a silent partial (`CA`) order.
- **Plan-line & buyer-request/query injection screening + opt-in layer 2 (Judge Panel Fix D + `33203b5`, `aimo/models.py`, `aimo/catalog/plan.py`, `aimo/trust/policy.py`, `aimo/trust/screening.py`):** `PlanLine.injection_screen` and `Candidate.injection_screen` (`{suspicious, reasons, screened?}`) surface supplier-text screening directly on `/v1/plan` and `plan_need` responses (and note flagged lines in `Plan.explanation`); buyer-side screening covers both `need.raw_text` and `need.product_query` in `Plan.explanation` warnings and in the policy `injection` rule (`needs_approval`). The shipped gateway runs the regex screen by default (`0` of `34,159` FP on real supplier texts; `44.4%` recall on the 36-payload eval set vs `94.4%` with Gemini, which flags `42/60` buyer requests); Gemini layer-2 screening (`AIMO_SCREEN_GEMINI=1`) is opt-in and off by default.
- **Gemini daily-quota fallback, opt-in raw trace & final policy refinements (`7c290cf`, `7225d06`, `02575d8`, `62e8384`, `f3c5996`, `aimo/gemini.py`, `aimo/trust/policy.py`):** when a Gemini model exhausts its daily quota or hits 429 twice, `aimo.gemini.generate` falls back immediately across `GEMINI_FALLBACK_MODELS` (default `gemini-3.5-flash,gemini-4-hydrogen`), skips the exhausted model for 30 minutes and logs `meta.fallback_from` in the usage ledger. Setting `AIMO_GEMINI_TRACE=<path>` in `aimo/gemini.py` (`02575d8`, opt-in, off by default) writes a raw JSONL trace line per attempt (`ts`, `feature`, `run_id`, `requested_model`, answering `model`, `attempt`, `ok`, `latency_s`, `meta`, `usage`, `request` and `response` / `error`) and never logs API keys (used to capture the committed live hero evidence pack in [evidence/live-run/README.md](evidence/live-run/README.md), verifiable offline with `scripts/verify_quote.py`, alongside the 110 s walkthrough [docs/pitch/demo-walkthrough.mp4](pitch/demo-walkthrough.mp4)). In `aimo/trust/policy.py`, a pack swap listed in the customer profile (or verified `best` where policy allows `best`) passes on its own under the spend limit without an `outside_preference` escalation, and `quantity_drift`'s 7-day rolling window counts only earlier orders placed without a human (split orders still add up).
- **Pre-planning input validation & Peppol UBL 2.3 export:** pre-planning `Need` validation (`quantity <= 0` -> 4xx before planning, fractional count units -> `422 fractional_quantity`, `> 1e6` -> 422); `GET /v1/orders/{id}/peppol-order-response.xml` exports Peppol BIS T116 UBL 2.3 XML (default tests check PEPPOL-T116 rules; OASIS UBL 2.3 XSD validation is opt-in via `AIMO_UBL_XSD`).
- **Static judge walkthrough, red-team warehouse arena & fleet test baseline (`56cb49a`, `7990fa3`, `d89db12`):** [docs/demo/index.html](demo/index.html) ("One-Page Judge Walkthrough") provides a keyless, zero-network 7-step browser walkthrough over the committed `run-b9690772fe` artifacts (`catalog_missing` / hero `plan_need`, WebCrypto Ed25519 JWS verify on `Q-20261115-964294b6` against `jwks.json`, 403 `agent_cannot_approve`, human `X-Approver-Session` + 409 `requote_needed`, Peppol `AP` on `ORD-20261115-06eda13d`, and 6-event audit tamper check, verified by Playwright in `tests/test_judge_html_demo.py`); [docs/arena/](arena/) ("Try to Scam the Agent", `docs/arena/index.html`) pairs red-team guardrail scenarios with 24 `CC0-1.0` Kenney sprites (`4,382` PNG bytes + `12,151`-byte manifest = `16,533` bytes) audited in [docs/demo/ASSETS.md](demo/ASSETS.md); fleet merge `7990fa3` (`fr8/valio/main`) and docs sync `d89db12` reach `4,137` collected pytest cases in `tests/`.
