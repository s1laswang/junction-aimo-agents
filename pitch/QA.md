# Judge Q&A crib sheet, 2 minutes

Companion to [PITCH.md](PITCH.md), [DEMO.md](DEMO.md), and [docs/JUDGING.md](../docs/JUDGING.md). Answer in at most 3 spoken sentences, cite only measured figures from [NUMBERS.md](NUMBERS.md), and name the exact `aimo/` code seam; if a metric was not measured (for example live latency with an external LLM key), say we did not measure it.

## 1. Working End-to-End Demo (30)

### Q: Is this hitting the real Valio API or a mock?

Product metadata, historical delivery reliability (`4,425` rows), and category-average carbon intensity (`11,033` rows) come from Valio's `17,546`-product catalog in `data/` (`0` deleted), while prices and availability come from Valio's shared challenge API (`/price` and `/availability`) via [aimo/valio.py](../aimo/valio.py). The first cold `offers` call takes `3,632 ms` to index the catalog and warm the cache, after which warm `offers` calls run in `17 ms` to `19 ms` with a `4 ms` median step latency across 21 live calls ([NUMBERS.md](NUMBERS.md)). Because the challenge API returns `9999999999` as a stock sentinel, we never state that figure as real warehouse inventory.

### Q: Why is there no Valio order endpoint?

Valio's Junction challenge API exposes `POST /price` and `POST /availability` only and omits an order placement endpoint ([docs/ASSUMPTIONS.md](../docs/ASSUMPTIONS.md)). Our gateway makes the Ed25519-signed quote in [aimo/quotes.py](../aimo/quotes.py) the binding contract, enforces customer mandates and human approvals in SQLite, and records a simulated `MOCK-...` confirmation (`mock: true`) in [aimo/orders.py](../aimo/orders.py). In production, swapping `orders._confirm` for an asynchronous Valio ERP/SAP adapter keyed by `quote_id` connects real fulfilment without changing any agent tool or REST schema.

### Q: Does it work over both MCP and REST without an SDK?

Yes: [aimo/mcp_server.py](../aimo/mcp_server.py) and [aimo/api.py](../aimo/api.py) share the exact same domain handlers and Pydantic models across all `12` MCP tools (over HTTP `/mcp` and `stdio`) and `15` OpenAPI paths (`12` under `/v1/*` plus `/health`, `/.well-known/agent-card.json`, and `/.well-known/jwks.json`). Any MCP client or HTTP agent discovers tools, auth headers (`X-Agent-Key` and `X-Admin-Key`), and public keys directly from `/.well-known/agent-card.json` and `/openapi.json` with no custom SDK ([docs/STANDARDS.md](../docs/STANDARDS.md)). On the `1,000` L lactose-free milk request, `hotel-aurora` (`2,500.00 EUR` per-order limit) and `hospital-tays` (`1,500.00 EUR` per-order limit) auto-confirm with `6` audit events each while `school-hervanta` (`600.00 EUR` per-order limit) holds in `pending_approval` with `4` audit events over both transports.

## 2. Precise Match to the Ask (20)

### Q: Why did 1,000 L lactose-free milk pick `464119` at `1,332.00 EUR` net instead of `464401` Arki Eila maitojuoma at `1,100.00 EUR`?

Product `464401` (`Valio Hyvä suomalainen Arki® Eila® rasvaton maitojuoma 1 l laktoositon`) is a milk drink (`maitojuoma`) rather than plain milk (`maito`), so [aimo/substitute.py](../aimo/substitute.py) scores it `0.75` versus `0.965` for plain UHT milk `464119` (`Valio rasvaton maito 1 l laktoositon UHT`). [aimo/offers.py](../aimo/offers.py) buckets exact matches (`match_score >= 0.9`) ahead of partial matches before sorting by net price per litre, picking `100` `10 L` `KI` cases of `464119` at `13.32 EUR` net per case (`1,332.00 EUR` net, `1,512.00 EUR` gross). Instead of hiding the cheaper milk drink, `rank_substitutes` returns `464401` at `1,100.00 EUR` net (`1,250.00 EUR` gross) as the `good` alternative alongside `464073` (`better`, `1,640.00 EUR` net) and `464201` (`best`, `1,560.00 EUR` net).

### Q: How do you handle `8,110` of `17,546` products that declare neither allergen nor nonAllergen lists?

Only `9,436` of `17,546` catalog products (`53.8%`) declare either `allergen` (`7,205`) or `FREE_FROM` (`5,119`) lists, leaving `8,110` products with neither, so we never equate "no allergen listed" with "safe" ([NUMBERS.md](NUMBERS.md)). Whenever any allergen exclusion, `lactose_free` (`4,178` declared `FREE_FROM_LACTOSE` claims), or `gluten_free` (`3,049` declared claims) constraint applies, `allergen_safe` in [aimo/substitute.py](../aimo/substitute.py) drops every item that declares neither `allergens` nor `labels`. At order placement, [aimo/policy.py](../aimo/policy.py) also checks both `CONTAINS` and `may_contain` traces (`ALLERGEN_CONFLICT` -> `blocked`) and holds any missing product record in `needs_approval` (`UNKNOWN_PRODUCT`).

### Q: How do you handle Finnish + English + recipes without requiring an LLM key?

[aimo/parse.py](../aimo/parse.py) runs a deterministic bilingual Finnish and English parser first for quantities, units, relative Helsinki delivery dates, and inflected dietary constraints (`laktoositon`, `gluteeniton`, `pähkinätön`), while [aimo/catalog.py](../aimo/catalog.py) indexes `fi`, `en`, and `sv` names (`17,544` English names across `349` categories) with trigram FTS5 and synonyms. Dish requests hit [aimo/recipes.py](../aimo/recipes.py) (`11` dishes in `data_extra/recipes.json`) to scale covers into ingredient lines and apply dietary swaps, falling back to an optional LLM only when an API key is configured. With no LLM key set, this deterministic path scores `40/40` on our three core precision suites (`set1 20/20`, `holdout 10/10`, `holdout2 10/10`) and `9/15` on the unseen `holdout3` suite (`49/55` overall in [NUMBERS.md](NUMBERS.md)).

### Q: Are your delivery reliability and CO2 numbers measured per product or estimated?

Delivery reliability comes directly from Valio's historical CSV covering `4,425` catalog products (`0.0` to `1.0`, median `1.0`, mean `0.972`), and carbon intensity comes from Valio's category-average CSV covering `11,033` products (`0.13` to `30.46` kg CO2-eq/kg, median `2.11`). Both files cover subsets of the `17,546` catalog items, so [aimo/catalog.py](../aimo/catalog.py) leaves missing values as `null` rather than inventing estimates ([docs/ASSUMPTIONS.md](../docs/ASSUMPTIONS.md)). In [aimo/offers.py](../aimo/offers.py), reliability breaks ties after availability, match score, and net unit price, while category-average CO2 is shown transparently on each offer and substitute note.

## 3. Autonomy and Controls (20)

### Q: What stops a compromised or hallucinating agent from editing the price or replaying a quote?

`issue` in [aimo/quotes.py](../aimo/quotes.py) locks canonical JSON (`quote_id`, `customer`, `agent_id`, `lines`, `total_net`, `total_gross`, `valid_until`) into a compact JWS signed with `alg` pinned to `EdDSA` over Ed25519. `redeem` verifies the signature against `/.well-known/jwks.json`, checks `quote_id`, `customer`, `agent_id`, and `valid_until`, and runs an atomic `UPDATE quotes SET used_at=? WHERE quote_id=? AND used_at IS NULL` in SQLite. Editing a single price digit, downgrading `alg`, replaying a spent quote, or presenting another customer's quote returns HTTP `409` (`BAD_SIGNATURE`, `QUOTE_USED`, or `WRONG_AGENT`) and logs `order.rejected` with no order created ([docs/TRUST.md](../docs/TRUST.md)).

### Q: What stops an agent from approving its own order?

Buyer agent keys (`X-Agent-Key`, or `AIMO_AGENT_KEY` on MCP stdio) are rejected with HTTP `403` `forbidden` on `POST /v1/approvals/{order_id}`, `PUT /v1/mandate`, and `/v1/admin/simulate` by `resolve_admin` in [aimo/api.py](../aimo/api.py). Even with `X-Admin-Key`, `decide` in [aimo/approvals.py](../aimo/approvals.py) checks that `by` matches one of that customer's named human `approvers` (such as `Sari Nieminen` for `school-hervanta`), returning `403` `NOT_APPROVER` otherwise and `409` `QUOTE_EXPIRED` if the cutoff passed. Every decision is audited under `human:<name>` with the hold reason and note, and any second attempt on a decided order is rejected with `409` `NOT_PENDING`.

### Q: What stops an agent from splitting one large purchase into smaller orders under the per-order limit?

Every customer mandate enforces both `per_order_eur` and a rolling 7-day `per_week_eur` net cap (`3,000.00 EUR` for `school-hervanta`, `8,000.00 EUR` for `hospital-tays`, and `15,000.00 EUR` for `hotel-aurora`). On every `place_order`, `spent_this_week` in [aimo/orders.py](../aimo/orders.py) sums `total_net` across `placed`, `approved`, and `confirmed` orders from the past 7 days and feeds that total into [aimo/policy.py](../aimo/policy.py). The moment cumulative 7-day spend plus the new quote exceeds `per_week_eur`, the order stops in `pending_approval` with reason `OVER_WEEK_LIMIT`.

### Q: How can a third party verify the audit log wasn't edited in SQLite?

Every event in [aimo/audit.py](../aimo/audit.py) is written inside a `BEGIN IMMEDIATE` transaction and SHA-256 hash-chained from a `64`-zero genesis hash over canonical JSON of `(seq, ts, customer, actor, action, subject, detail, why)`. `GET /v1/audit` recomputes the entire chain (`verified: {ok, bad_seq}`) and signs the latest `{seq, hash, ts}` head as an `EdDSA` JWS checkpoint published against `/.well-known/jwks.json`. Anyone can verify the chain in `/audit.html` or offline via `python -m aimo.audit verify-checkpoint`, so altering or deleting a SQLite row immediately sets `verified.ok` to `false` at `bad_seq` ([docs/STANDARDS.md](../docs/STANDARDS.md)).

## 4. Robustness to Exceptions (15)

### Q: What happens when supplier stock drops after an order is placed?

When stock drops (`POST /v1/admin/simulate` in the demo, or an ERP shortfall event in production), `react_stockout` in [aimo/orders.py](../aimo/orders.py) pulls fresh offers and runs `stockout_plan` in [aimo/allocate.py](../aimo/allocate.py) across affected orders. Rather than silently short-shipping or auto-swapping behind the buyer's back, it reallocates remaining stock or compliant substitutes, calculates any shortfall or split-delivery options, and reopens the order to `pending_approval` with reason `STOCKOUT`. The human approver sees the proposed line changes in `/inbox.html`, and [aimo/audit.py](../aimo/audit.py) appends an `order.stockout` event with the plain-language reason.

### Q: What happens when the Valio API returns 429/5xx or goes down during a run?

[aimo/valio.py](../aimo/valio.py) deduplicates and chunks up to `1,000` product IDs per request, retries `429` and `5xx` up to 3 attempts using `Retry-After` (capped at `10.0` seconds) or exponential backoff, and caches per `(endpoint, customer, requestedDate)`. If the upstream stays unreachable, `_fetch` falls back to the latest cached `(endpoint, customer)` snapshot on disk so active flows keep running. Only when both the live API and local cache miss does [aimo/api.py](../aimo/api.py) return a structured HTTP `503` `UPSTREAM_UNAVAILABLE` response instead of an unhandled `500`.

### Q: How do you prevent an allergen-unsafe substitute for a hospital kitchen?

We enforce two independent gates: during offer search and `rank_substitutes`, [aimo/substitute.py](../aimo/substitute.py) combines request constraints with the customer mandate (`hospital-tays` excludes `peanut`, `nuts`, and `egg`) and drops any product with matching `CONTAINS` or `MAY_CONTAIN` declarations or missing allergen metadata. Even if an agent bypasses `get_offers` and quotes an unsafe product ID directly, `decide` in [aimo/policy.py](../aimo/policy.py) checks both quote lines and `substitutions` at `place_order` and hard-blocks the order (`blocked`, `ALLERGEN_CONFLICT`). In addition, `hospital-tays` sets mandate `substitutions` to `"ask"`, so even an allergen-safe swap stops in `pending_approval` (`SUBSTITUTION_NEEDS_APPROVAL`) for `Helena Korhonen` or `Antti Lahtinen` to review.

### Q: How do you catch silent price creep or missing stock on standing weekly orders?

[agents/drift.py](../agents/drift.py) compares standing weekly order lines against live gateway offers and each customer's mandate before the next delivery cutoff. It flags price increases (`>= 5%` review, `> 10%` human approval), gradual quantity creep (`>= 20%`), sales blocks, late availability, and newly declared excluded allergens. Any mandate allergen conflict or out-of-policy substitute is routed to human approval rather than auto-ordered.

## 5. Openness and Path to Production (15)

### Q: How does Ed25519 key rotation work without breaking active quotes?

Running `python -m aimo.keys rotate` in [aimo/keys.py](../aimo/keys.py) writes the current public JWK to `retired-<kid>.json`, generates a new `0600` Ed25519 keypair, and atomically replaces `quote-ed25519.json`. Because `keys.jwks()` publishes the active public key alongside every `retired-*.json` key at `/.well-known/jwks.json`, `quotes.verify` ([aimo/quotes.py](../aimo/quotes.py)) and `audit.verify_checkpoint` ([aimo/audit.py](../aimo/audit.py)) match the JWS `kid` thumbprint so pre-rotation quotes still verify and redeem until `valid_until`. In production, only the [aimo/keys.py](../aimo/keys.py) seam moves from local `0600` files to cloud KMS or an HSM on an automated rotation schedule ([docs/PRODUCTION.md](../docs/PRODUCTION.md)).

### Q: What changes to go from SQLite + static keys to Valio production ERP?

Every external dependency sits behind one module seam, so all `12` MCP tools and `/v1` REST schemas remain unchanged ([docs/PRODUCTION.md](../docs/PRODUCTION.md)). [aimo/api.py](../aimo/api.py) swaps static `X-Agent-Key` and `X-Admin-Key` headers for OAuth 2.0 or mTLS agent tokens and SSO/OIDC human approver identities, [aimo/db.py](../aimo/db.py) swaps SQLite for PostgreSQL with external WORM anchoring for [aimo/audit.py](../aimo/audit.py) checkpoints, and [aimo/valio.py](../aimo/valio.py) moves the file cache to Redis. Finally, `orders._confirm` in [aimo/orders.py](../aimo/orders.py) replaces the `MOCK-...` stub with an asynchronous Valio ERP/SAP order adapter using `quote_id` as the idempotency key, rolled out from read-only offers to conservative human-approved limits before raising mandates.

### Q: Can Valio's selling side also propose orders without bypassing buyer guardrails?

Yes: [agents/seller.py](../agents/seller.py) inspects customer order history and mandates through `/v1`, filtering out excluded allergens, disallowed categories, sales blocks, past cutoffs, and offers over `per_order_eur` before proposing restocks or alternatives. A seller proposal never places an order on its own; the buyer side still locks a single-use Ed25519 quote via [aimo/quotes.py](../aimo/quotes.py) and submits it through [aimo/policy.py](../aimo/policy.py) and [aimo/audit.py](../aimo/audit.py).
