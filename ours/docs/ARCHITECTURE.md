# Architecture

One Python process (`uvicorn aimo.app:app`): FastAPI serves REST under `/v1`, OpenAPI 3.1 at `/openapi.json`, MCP streamable HTTP at `/mcp` (plus stdio via `python -m aimo.mcp_server`), discovery under `/.well-known/`, `/health`, and the browser UI at `/`. State lives in SQLite, opened only through `aimo/db.py` (path from `AIMO_DB`, default `var/aimo.db`), where each module creates its own tables on first use. Ed25519 signing keys live in `AIMO_KEY_DIR` (`var/keys`), and Valio API responses are batched (`<= 1,000` product IDs) and cached in `AIMO_CACHE_DIR` (`var/cache`).

## Flow

```text
buyer agent (MCP HTTP / MCP stdio / REST, X-Agent-Key)
   |
   v
parse_request -> Need (+ summary)
   |
get_offers: search -> allergen_safe -> Valio /price + /availability -> rank -> allocate / substitute -> OffersResponse
   |
request_quote -> signed Quote (EdDSA JWS, single use, valid until closingTime)
   |
place_order: verify and redeem JWS -> policy.decide against Mandate
   |            |-> auto            -> approved -> placed -> confirmed
   |            |-> needs_approval  -> pending_approval -> human decides -> approved -> placed -> confirmed | rejected | expired
   |            '-> blocked         -> blocked
   v
audit: every step appends a SHA-256 hash-chained event with a plain-language why (+ EdDSA checkpoint)
```

If supplier stock drops after an order is placed (`POST /v1/admin/simulate` in the demo), `orders.react_stockout` reallocates from remaining stock or substitutes, records any shortfall, and reopens affected orders to `pending_approval` with reason `STOCKOUT` (`order.stockout`). Valio has no order placement endpoint in the challenge API, so orders live in our SQLite store; see [ASSUMPTIONS](ASSUMPTIONS.md).

## Auth

- Buyer agents send `X-Agent-Key` (or `AIMO_AGENT_KEY` over MCP stdio). It resolves via `aimo/mandates.py` to `(customer, agent_id, Mandate)` and scopes every call to that customer.
- Humans and admins send `X-Admin-Key` (env `AIMO_ADMIN_KEY`, or `AIMO_STDIO_ADMIN_KEY` over MCP stdio). Required to decide approvals (`POST /v1/approvals/{order_id}`), update mandates (`PUT /v1/mandate`), and control stock simulations (`/v1/admin/simulate`). On read routes (`list_orders`, `get_order`, `list_approvals`, `get_audit_trail`, `get_mandate`), `X-Admin-Key` can inspect all customers.
- Missing or invalid key: `401`. Wrong customer, agent key on an admin route, or non-listed approver (`NOT_APPROVER`): `403`. Missing resource: `404`. Quote, approval, or mandate domain errors: `409` with `{"code", "text"}`. Request schema errors: `422`. Unreachable Valio API with an empty cache: `503` (`UPSTREAM_UNAVAILABLE`).
- `GET /v1/products`, `GET /v1/products/{product_id}`, `POST /v1/needs/parse`, `/.well-known/*`, `/openapi.json`, `/health`, and UI static assets need no key.

See [TRUST](TRUST.md) for the trust protocol and red-team checks, and [STANDARDS](STANDARDS.md) for MCP, OpenAPI, and JWS/JWKS verification.

## Modules

| Module | Responsibility | Key interfaces |
|---|---|---|
| `aimo/app.py`, `models.py`, `db.py` | FastAPI wiring, shared Pydantic v2 schemas, SQLite connection seam | `app`, `db.connect()` |
| `aimo/catalog.py` | Trigram FTS5 + `fi`/`en`/`sv` synonym search over 17,546 products, allergen/claim normalization, unit sizing, reliability, CO2 | `search()`, `get()`, `reliability()`, `co2_per_kg()` |
| `aimo/valio.py`, `offers.py` | Cached and retried Valio `/price` and `/availability` client, Helsinki `closingTime` cutoffs, ranking, simulation overrides | `get_offers()`, `price_lines()`, `simulate()` |
| `aimo/parse.py`, `recipes.py` | Rule-based Finnish/English request parser (optional LLM fallback) and recipe/menu ingredient scaling | `parse_need()`, `menu_to_need()`, `recipe_swaps()` |
| `aimo/allocate.py`, `substitute.py` | Pack/volume/weight conversion, split allocation, shortfall options, stock-out plans, allergen-safe substitute tiers | `allocate()`, `allergen_safe()`, `rank_substitutes()` |
| `aimo/quotes.py`, `keys.py` | Ed25519 key store and rotation (`retired-<kid>.json`), JWKS, canonical-JSON `EdDSA` JWS quotes, single-use redemption | `issue()`, `verify()`, `redeem()`, `jwks()`, `rotate()` |
| `aimo/mandates.py`, `policy.py` | Agent key bindings, customer mandates, deterministic policy engine with plain-language `Reason` codes | `agent_for_key()`, `get()`, `put()`, `decide()` |
| `aimo/orders.py`, `approvals.py` | Order state machine, rolling 7-day spend, stock-out reaction, named-approver human decision queue and expiry | `place()`, `react_stockout()`, `pending()`, `decide()` |
| `aimo/audit.py` | Append-only SHA-256 hash-chained log, buyer/Valio plain-language narrator, full-chain verifier, EdDSA checkpoints | `record()`, `verify()`, `narrate()`, `checkpoint()` |
| `aimo/api.py`, `mcp_server.py`, `discovery.py` | `/v1` REST routes, streamable HTTP (`/mcp`) and stdio MCP tools, `agent-card.json` and `jwks.json` | 12 shared MCP/REST tools + 3 admin REST routes |
| `agents/` (`buyer.py`, `seller.py`, `drift.py`, `llm.py`) | Reference buyer agent, proactive seller proposals, standing-order drift and stock-out watcher, optional OpenAI-compatible client | CLI and scenario runners |
| `web/` (`index.html`, `console.*`, `inbox.*`, `audit.*`, `admin.*`) | Plain HTML/JS/CSS overview, live agent console, approval inbox, in-browser SHA-256 audit verifier, mandate editor | Served at `/` (supports offline `?mock=1`) |

The LLM client (`agents/llm.py`) is optional. Without an LLM API key, `aimo/parse.py` and all scenarios run deterministically.

## Decision rules

`policy.decide` evaluates every applicable rule and returns the strictest outcome with plain-language reasons:

- `blocked`: allergen conflict (both `CONTAINS` and `may_contain` traces), category outside `allowed_categories`, substitution when `substitutions="never"`, quote past cutoff (`PAST_CUTOFF`), or non-positive quote total/quantity (`INVALID_QUOTE`).
- `needs_approval`: over `per_order_eur` (`OVER_ORDER_LIMIT`), rolling 7-day spend plus order over `per_week_eur` (`OVER_WEEK_LIMIT`), substitution when `substitutions="ask"` or outside `preferred_brands` (`SUBSTITUTION_NEEDS_APPROVAL`), missing product metadata when allergens are excluded (`UNKNOWN_PRODUCT`), explicit `ask_human=true` (`AGENT_REQUESTED`), or post-order supplier stock-out (`STOCKOUT`).
- `auto`: otherwise (`WITHIN_MANDATE`). A non-preferred brand on a direct line adds an informational `NON_PREFERRED_BRAND` note without blocking placement.
