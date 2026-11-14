# Verification scenarios

The `scenarios/` suite runs 8 end-to-end verification scripts (`s0_guardrails.py` through `s7_drift.py`) over real HTTP and MCP against a booted gateway, the 17,546-product Synkka catalog (`data/`), and Valio pricing/availability (`/price`, `/availability`, cached in `var/cache`). Every stage scene in the [Demo script](../pitch/DEMO.md) maps directly to one or more of these scripts.

## Summary

| Scenario | Customer(s) | Judging criterion | What it proves | Key gateway/agent seams exercised |
|---|---|---|---|---|
| `s0_guardrails` (`scenarios/s0_guardrails.py`) | `hotel-aurora` | Autonomy and controls (20 pts) | Over-mandate orders hold in `pending_approval` until a named approver signs off; tampered JWS quotes are rejected (`409`) without creating an order | `GET /v1/mandate`, `POST /v1/needs/parse`, `POST /v1/offers`, `POST /v1/quotes`, `POST /v1/orders` (`quotes.redeem`, `policy.decide`), `GET /v1/approvals`, `POST /v1/approvals/{order_id}` (`approvals.decide`) |
| `s1_milk` (`scenarios/s1_milk.py`) | `hotel-aurora` | Working end-to-end demo (30 pts), Openness (15 pts) | 1,000 L lactose-free milk request flows from text to confirmed order over both REST and streamable HTTP `/mcp`, with offline Ed25519 JWKS verification and narrated audit events | `POST /v1/needs/parse`, `POST /v1/offers`, `POST /v1/quotes`, `GET /.well-known/jwks.json`, `POST /v1/orders`, `GET /v1/audit`, `/mcp` (`tools/list`, `agents/buyer.py`) |
| `s2_need_recipe_order` (`scenarios/s2_need_recipe_order.py`) | `hotel-aurora` | Precise match to the ask (20 pts) | Dish request (`lasagne for 40 portions`) expands into linearly scaled ingredient lines, matches catalog offers, locks a multi-line JWS quote, and places a policy-checked order | `POST /v1/needs/parse` (`aimo/parse.py`, `aimo/recipes.py`), `POST /v1/offers` (`aimo/allocate.py`), `POST /v1/quotes`, `POST /v1/orders` |
| `s3_event_substitution` (`scenarios/s3_event_substitution.py`) | `hotel-aurora` | Robustness to exceptions (15 pts) | Last-minute 400-portion event (`pasta carbonara`) substitutes short items within allergen and brand rules, records `Order.substitutions` with reasons, and cleans up simulated stock | `POST /v1/needs/parse`, `POST /v1/offers` (`aimo/substitute.py`), `POST /v1/admin/simulate`, `DELETE /v1/admin/simulate`, `POST /v1/quotes`, `POST /v1/orders`, `POST /v1/approvals/{order_id}`, `GET /v1/audit` |
| `s4_stockout` (`scenarios/s4_stockout.py`) | `hotel-aurora` | Robustness to exceptions (15 pts) | Mid-week supplier stock-out (`available_amount=0`) triggers server-side `orders.react_stockout`, reopens the existing order to `pending_approval` with `STOCKOUT`, prevents watcher duplicate orders, and confirms after human approval | `POST /v1/orders`, `POST /v1/admin/simulate` (`orders.react_stockout`, `allocate.stockout_plan`), `GET /v1/orders/{order_id}`, `GET /v1/audit`, `agents/drift.py` (`watch_orders`), `DELETE /v1/admin/simulate`, `POST /v1/approvals/{order_id}` |
| `s5_allergen_safe` (`scenarios/s5_allergen_safe.py`) | `hospital-tays` | Precise match (20 pts), Autonomy and controls (20 pts), Robustness (15 pts) | Dual-layer allergen protection: `/v1/offers` filters all `CONTAINS` and `MAY_CONTAIN` excluded allergens out of candidate offers, and `/v1/orders` hard-blocks (`ALLERGEN_CONFLICT`) direct quotes for violating products | `GET /v1/mandate`, `POST /v1/needs/parse`, `POST /v1/offers` (`substitute.allergen_safe`), `POST /v1/quotes`, `POST /v1/orders` (`policy.decide`), `GET /v1/audit` |
| `s6_proactive_seller` (`scenarios/s6_proactive_seller.py`) | `hotel-aurora`, `hospital-tays` | Openness and path to production (15 pts) | Proactive seller agent proposes better-priced, higher-reliability, or lower-CO2 offers with plain-language `why` while enforcing each customer's excluded allergens and allowed categories | `agents/seller.py` (`propose`), `GET /v1/mandate`, `GET /v1/orders`, `GET /v1/audit`, `GET /v1/products/{product_id}`, `POST /v1/offers` |
| `s7_drift` (`scenarios/s7_drift.py`) | `hotel-aurora`, `school-hervanta` | Robustness to exceptions (15 pts), Openness (15 pts) | Standing-order drift monitor flags price shifts, quantity creep, late availability, and injected stock-outs with `auto` vs `approval` routing, never auto-applying allergen changes | `agents/drift.py` (`check`), `GET /v1/mandate`, `GET /v1/products/{product_id}`, `POST /v1/offers`, `POST /v1/quotes`, `POST /v1/admin/simulate`, `DELETE /v1/admin/simulate` |

## Running the scenarios

Prerequisites: `data/` linked (`valio_aimo_junction26_product_data.json`) and `.env` present (or `var/cache` warmed).

### Run all 8 in isolated boot mode (deterministic, zero LLM)

```sh
make demo
```

`make demo` runs `setup`, executes `make scen` (`.venv/bin/python scenarios/run_all.py --base http://127.0.0.1:8081 --boot`), resets `var/demo.db`, and leaves a clean stage server running on port `8080`. To run only the isolated verification pass without starting the stage server:

```sh
.venv/bin/python scenarios/run_all.py --base http://127.0.0.1:8081 --boot
```

With `--boot`, `scenarios/run_all.py`:

- Boots a fresh `uvicorn aimo.app:app` subprocess backed by a throwaway SQLite `AIMO_DB` before each scenario (`s0` through `s7`) and terminates it after the script exits, so state from one scenario never leaks into the next.
- Strips every `LLM_*` environment variable (`{k: v for k, v in os.environ.items() if not k.startswith("LLM_")}`), proving all 8 scenarios pass deterministically on the rule-based parser and policy engine without an LLM API key.
- Prints a per-scenario `PASS`/`FAIL` summary (`8/8 passed`) and exits `0` when all pass (separating upstream Valio mock `429` or `503` `UPSTREAM_UNAVAILABLE` rate limits from assertion failures). Set `VERBOSE=1` to stream per-assertion `[PASS]`/`[FAIL]` lines, or pass `--only s4` to filter by prefix.

### Run against a live server

When a server is already up on `http://localhost:8080` (`make run` or `make stage`), run the full suite or a single scenario against it:

```sh
make scenarios
.venv/bin/python scenarios/run_all.py --base http://localhost:8080
.venv/bin/python scenarios/s4_stockout.py --base http://localhost:8080
```

Each script accepts `--base` (defaulting to `AIMO_URL` or `http://127.0.0.1:8080`), authenticates buyer calls via `X-Agent-Key` (`demo-hotel-aurora`, `demo-hospital-tays`, `demo-school-hervanta`) and admin/approval/simulation calls via `X-Admin-Key` (`demo-admin`), and exits `0` on `PASS` or `1` on the first failed check (`scenarios/_common.py`).

## Gateway status, error, policy, and audit codes

Across the scenario scripts and their companion contract tests (`tests/test_quotes.py`, `tests/test_approvals.py`, `tests/test_policy.py`, `tests/test_guardrails_live.py`), the gateway enforces deterministic HTTP and domain codes:

| Seam | Codes verified | HTTP / state behavior |
|---|---|---|
| Quote JWS redemption (`POST /v1/orders` -> `quotes.redeem`) | `BAD_SIGNATURE`, `QUOTE_USED`, `WRONG_AGENT`, `QUOTE_MISMATCH`, `QUOTE_EXPIRED`, `UNKNOWN_QUOTE`, `INVALID_QUOTE` | Returns HTTP `409` with `{"code", "text"}`, creates no `Order` (`"order_id"` absent), and appends `order.rejected` to the audit chain (`s0_guardrails.py` asserts HTTP `409` on tampered JWS; `tests/test_quotes.py` locks single-use `QUOTE_USED`, cross-customer `WRONG_AGENT`, `alg: none`/`HS256`, and key rotation). |
| Policy evaluation (`POST /v1/orders` -> `policy.decide`) | `WITHIN_MANDATE` (`auto`), `OVER_ORDER_LIMIT`, `OVER_WEEK_LIMIT`, `SUBSTITUTION_NEEDS_APPROVAL`, `UNKNOWN_PRODUCT`, `AGENT_REQUESTED`, `STOCKOUT` (`needs_approval`), `ALLERGEN_CONFLICT`, `CATEGORY_NOT_ALLOWED`, `SUBSTITUTION_NOT_ALLOWED`, `PAST_CUTOFF` (`blocked`) | Returns HTTP `200` with `Order.decision` (`outcome` + `reasons[{code, text}]`) and transitions order status to `confirmed` (`auto`), `pending_approval` (`needs_approval`), or `blocked` (`blocked`). |
| Human approval (`POST /v1/approvals/{order_id}` -> `approvals.decide`) | `NOT_APPROVER` (`403`), `NOT_FOUND` (`404`), `NOT_PENDING` (`409`), `QUOTE_EXPIRED` (`409`) | Requires `X-Admin-Key` (`401` missing/bad key, `403` agent key) and checks `by in mandate.approvers` (`403` `NOT_APPROVER` otherwise). Approving logs `approval.approved` and moves `pending_approval -> approved -> placed -> confirmed`. |
| Audit ledger (`GET /v1/audit` -> `audit.verify`, `audit.checkpoint`) | `quote.issued`, `quote.redeemed`, `order.placed`, `order.pending_approval`, `order.approved`, `order.confirmed`, `order.blocked`, `order.rejected`, `order.stockout`, `approval.approved`, `approval.rejected` | Every event carries a non-empty plain-language `why`, is SHA-256 hash-chained (`verified: {"ok": true, "bad_seq": null}`), and ships with an EdDSA-signed `checkpoint.jws`. |

## Per-scenario breakdown

### `s0_guardrails` (`scenarios/s0_guardrails.py`)

Customer: `hotel-aurora` (`per_order_eur = 2500.0`, approvers `Laura Virtanen`, `Mikko Heikkinen`).

1. **Mandate inspection and baseline quote**: reads `GET /v1/mandate` (HTTP `200`), parses `"I need 1000 litres of lactose-free milk by <Monday>"`, fetches `/v1/offers`, and locks a baseline quote `q` via `POST /v1/quotes`.
2. **Over-limit quote (`over-limit quote exceeds mandate`)**: scales line quantities so `qb["total_net"] > m["per_order_eur"]`.
3. **Mandate hold (`over per_order_eur -> needs_approval`)**: `POST /v1/orders` on `qb` returns HTTP `200` with `decision.outcome == "needs_approval"` (`OVER_ORDER_LIMIT` reason) and `status == "pending_approval"`.
4. **Approval inbox visibility (`order listed in approvals`)**: `GET /v1/approvals` with `X-Admin-Key` lists `o["order_id"]`.
5. **Named approver sign-off (`approval -> placed`)**: `POST /v1/approvals/{order_id}` with `approve=True` and `by=m["approvers"][0]` (`Laura Virtanen`) moves status into `("placed", "approved", "confirmed")` (non-listed names fail at this check with HTTP `403` `NOT_APPROVER`).
6. **JWS tamper rejection (`tampered jws rejected`)**: flips two base64url characters in the payload segment of unused quote `q` and posts to `POST /v1/orders`; asserts `resp.status_code == 409` (`BAD_SIGNATURE` in `TAMPER_STATUS = (409,)`) and `"order_id" not in resp.json()`. Replaying a redeemed quote or redeeming under another customer's key hits the same `quotes.redeem` guard with HTTP `409` `QUOTE_USED` and `WRONG_AGENT`.

### `s1_milk` (`scenarios/s1_milk.py`)

Customer: `hotel-aurora`.

1. **Natural-language parse (`parsed 1000 L lactose-free`)**: `POST /v1/needs/parse` extracts `quantity == 1000` and `constraints.lactose_free is True`.
2. **Catalog + Valio offers (`offers returned`, `offers cover 1000 L`, `offers carry price and availability`)**: `POST /v1/offers` returns ranked lactose-free milk offers with `shortfall == 0` and non-null `unit_price_net` and `available_amount` on every offer (on the seeded catalog, 100 cases of 10 L `464119` at `1,332.00 EUR` net / `1,512.00 EUR` gross).
3. **Ed25519 signature and JWKS check (`quote signed (EdDSA)`, `jws verifies against jwks.json`)**: `POST /v1/quotes` returns a compact JWS with header `alg == "EdDSA"`, and `joserfc.jws.deserialize_compact` verifies the token against `GET /.well-known/jwks.json` with `algorithms=["EdDSA"]`.
4. **Policy decision (`order has Decision`, `decision has reasons`)**: `POST /v1/orders` returns `decision.outcome` in `("auto", "needs_approval")` (`auto` with `WITHIN_MANDATE` under the `2,500.00 EUR` hotel limit) and `len(decision.reasons) > 0`.
5. **Audit ledger (`audit trail has steps`, `every audit event has why`)**: `GET /v1/audit?order_id=...` returns `len(events) >= 3` (`quote.issued`, `quote.redeemed`, `order.placed`, `order.approved`, `order.confirmed`), every event has a non-empty `why`, and the endpoint recomputes the SHA-256 hash chain (`verified.ok`) and EdDSA `checkpoint.jws`.
6. **Streamable HTTP MCP parity (`MCP lists all tools`, `MCP buyer agent gets an order with Decision`)**: initializes a `ClientSession` against `base + "/mcp"` with `X-Agent-Key`, verifies `{"get_offers", "request_quote", "place_order", "get_audit_trail"} <= tools`, then runs `agents.buyer.run(...)` over `/mcp` and asserts the resulting order carries a populated `decision.outcome`.

### `s2_need_recipe_order` (`scenarios/s2_need_recipe_order.py`)

Customer: `hotel-aurora`.

1. **Recipe expansion (`dish became several ingredient lines`, `every line has quantity and unit`, `lasagne basics present`)**: `POST /v1/needs/parse` on `"We need to cook lasagne for 40 portions by <Monday>"` expands via `data_extra/recipes.json` into `>= 3` ingredient lines, each with `quantity > 0` and `unit`, including pasta/lasagne and beef/meat/mince queries.
2. **Linear portion scaling (`quantities scale with portions`)**: compares 40-portion vs 20-portion parses and asserts every ingredient quantity scales by `2x` within `1e-6` relative tolerance.
3. **Multi-line procurement (`offers found for most lines`, `quote has a line per picked ingredient`, `order has Decision`)**: fetches `/v1/offers` (`>= len(items) // 2` lines matched), locks one multi-line signed quote via `/v1/quotes` (`len(q["lines"]) >= len(got)`), and places the order via `/v1/orders` with `decision.outcome` in `("auto", "needs_approval")`.

### `s3_event_substitution` (`scenarios/s3_event_substitution.py`)

Customer: `hotel-aurora` (`substitutions = "within_prefs"`, preferred brand `Valio`).

1. **Urgent event parse (`recipe expanded to ingredient lines`)**: parses `"Last-minute event on the next delivery day, we need pasta carbonara for 400 portions by <next_delivery_day>"` into `>= 3` ingredient lines.
2. **Shortfall and substitute generation (`offers or substitutes found for lines`, `at least one substitute offer`, `substitute offers say what they replace`)**: if live inventory covers all lines directly, simulates `available_amount=1` on a candidate product via `POST /v1/admin/simulate` (always cleared with `DELETE /v1/admin/simulate` in `finally`), re-queries `/v1/offers`, and asserts `len(subs) >= 1` with `substitute_for != product_id` on every substitute offer.
3. **Order placement and approval (`order accepted`)**: quotes and orders all picked lines; if `status == "pending_approval"` (such as `OVER_ORDER_LIMIT` on 400 portions or `SUBSTITUTION_NEEDS_APPROVAL`), approves via `POST /v1/approvals/{order_id}` using the customer's first mandate approver and asserts status in `("placed", "approved", "confirmed")`.
4. **Substitution provenance and allergen safety (`every picked substitute is in Order.substitutions`, `substitution recorded with reason`, `substitutes respect mandate allergens`, `audit explains substitutions`)**: verifies every picked substitute appears in `o["substitutions"]` with `original_id`, `substitute_id`, and `reason`, checks Synkka `CONTAINS` and `MAY_CONTAIN` allergen classifications against `mandate.exclude_allergens`, and confirms every `/v1/audit` event has a `why`.

### `s4_stockout` (`scenarios/s4_stockout.py`)

Customer: `hotel-aurora`.

1. **Healthy initial order (`order placed before fault`)**: parses, quotes, and places a 200 L lactose-free milk order, confirming status in `("placed", "approved", "confirmed")`.
2. **Server-side stock-out reaction (`same order moved to pending_approval`, `decision has STOCKOUT reason with text`)**: injects `POST /v1/admin/simulate` with `available_amount=0` on `o["lines"][0]["product_id"]`, triggering `orders.react_stockout`; `GET /v1/orders/{order_id}` shows the existing order moved to `pending_approval` with `code == "STOCKOUT"` and explanatory `text` in `decision.reasons`.
3. **Safe reallocation (`substitution or split present`, `substitutes and lines allergen-safe`, `substitutions carry a reason`)**: asserts `after["substitutions"]` is populated or `after["lines"]` was split/reallocated, every substitute and line passes Synkka allergen checks against `mandate.exclude_allergens`, and every substitution carries a `reason`.
4. **Stock-out audit event (`audit has a stockout event with a why`)**: `GET /v1/audit?order_id=...` includes `order.stockout` with a non-empty `why`.
5. **Idempotent drift watcher (`watcher reports stockout_handled`, `watcher places no duplicate order`)**: runs `agents.drift.watch_orders(...)`, asserting it reports `kind == "stockout_handled"` with `new_order_id is None` and does not create a second order (`after_n == before_n`).
6. **Cleanup and human resolution (`human approval -> placed`)**: clears the simulation (`DELETE /v1/admin/simulate` in `finally`), approves via `POST /v1/approvals/{order_id}`, and asserts final status in `("placed", "approved", "confirmed")`.

### `s5_allergen_safe` (`scenarios/s5_allergen_safe.py`)

Customer: `hospital-tays` (`exclude_allergens = ["peanut", "nuts", "egg"]`).

1. **Mandate check (`mandate excludes allergens`)**: `GET /v1/mandate` confirms `len(exclude_allergens) > 0`.
2. **Offer-layer filtering (`offers returned`, `no offer contains an excluded allergen`)**: parses `"We need 100 litres of lactose-free milk and 50 kg of bread by <Monday>"`, calls `POST /v1/offers`, and verifies zero returned offers carry Synkka `CONTAINS` or `MAY_CONTAIN` declarations matching `exclude_allergens` (`AP`, `AN`, `AE`).
3. **Order-layer hard block (`found a violating product in data`, `violating order blocked`, `block reason is ALLERGEN_CONFLICT with text`)**: selects an active violating product from `valio_aimo_junction26_product_data.json`, obtains a signed quote directly via `POST /v1/quotes` (HTTP `200`), submits it to `POST /v1/orders`, and asserts `decision.outcome == "blocked"`, `status == "blocked"`, and `code == "ALLERGEN_CONFLICT"` with non-empty `text` in `decision.reasons`.
4. **Blocked audit trail (`audit has order.blocked with why`)**: `GET /v1/audit?order_id=...` contains an `order.blocked` event with a non-empty `why`.

### `s6_proactive_seller` (`scenarios/s6_proactive_seller.py`)

Customers: `hotel-aurora`, `hospital-tays`.

1. **Mandate-bounded seller proposals (`<cust> proposals have a why`, `<cust> proposals respect excluded allergens`, `<cust> proposals respect allowed categories`)**: runs `agents.seller.propose(cust, KEYS[cust], base)` for both customers and asserts every proposal includes a plain-language `why` (availability, net EUR savings, reliability, or CO2 intensity), zero proposed products violate `mandate.exclude_allergens` (`CONTAINS` or `MAY_CONTAIN`), and all proposed categories fall within `mandate.allowed_categories` when configured.
2. **Non-empty hotel suggestions (`hotel got at least one proposal`)**: asserts `len(props) > 0` for `hotel-aurora`.

### `s7_drift` (`scenarios/s7_drift.py`)

Customers: `hotel-aurora`, `school-hervanta`.

1. **Standing-order evaluation (`<cust> standing order returned`, `<cust> flags have a why and a route`)**: runs `agents.drift.check(cust, KEYS[cust], base, state_path=...)` for both customers and asserts `len(standing_order["lines"]) > 0` and every emitted flag carries a non-empty `why` and `route` in `("auto", "approval")`.
2. **Allergen governance (`<cust> substitutes respect mandate allergens`, `<cust> allergen flags never auto`)**: verifies every `fix.action == "substitute"` candidate passes Synkka allergen checks against `mandate.exclude_allergens`, and asserts every `kind == "mandate"` allergen flag sets `route == "approval"` (never `"auto"`).
3. **Injected stock-out detection (`injected stock-out is flagged for that product`)**: baselines `hotel-aurora`, injects `POST /v1/admin/simulate` (`available_amount=0`) on the first standing-order product (`463982`), re-runs `drift.check`, clears simulation (`DELETE /v1/admin/simulate`), and asserts `any(f["product_id"] == pid for f in flags)`.

## Related docs

- [Judging checklist](JUDGING.md)
- [Demo script](../pitch/DEMO.md)
- [Trust protocol](TRUST.md)
- [Open standards](STANDARDS.md)
- [Assumptions](ASSUMPTIONS.md)
