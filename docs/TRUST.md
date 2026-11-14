# Trust protocol

An outside agent never gets a bare price. It gets a signed commitment, acts inside a customer mandate, and leaves a hash-chained trail a human can read and a third party can verify offline.

## Identity and mandate

- Each buyer agent authenticates with `X-Agent-Key` (or `AIMO_AGENT_KEY` over MCP stdio). It resolves to one customer, one `agent_id`, and one mandate (`aimo/mandates.py`). Every agent call is scoped to that customer: reading another customer's order or passing another customer id returns 403 (`forbidden`).
- Humans and admins send `X-Admin-Key` (env `AIMO_ADMIN_KEY`, or `AIMO_STDIO_ADMIN_KEY` over MCP stdio). It is required to decide approvals (`POST /v1/approvals/{order_id}`), update a mandate (`PUT /v1/mandate`), and control stock simulations (`/v1/admin/simulate`).
- Missing or invalid key: 401 (`unauthorized`). A valid agent key on an admin route: 403 (`forbidden`).
- A mandate holds `per_order_eur` and `per_week_eur` net limits, `exclude_allergens`, `allowed_categories`, a substitution rule (`never`, `within_prefs`, `ask`), `preferred_brands`, and named `approvers`. Mandate changes validate positive limits (`per_order_eur <= per_week_eur`, otherwise 409 `INVALID_MANDATE`) and are recorded in the audit log (`mandate_updated`). Seeded limits and approvers are listed in [ASSUMPTIONS](ASSUMPTIONS.md).

## Signed quotes

- `request_quote` (`POST /v1/quotes`) locks priced offers into a single-use `Quote` signed as a JWS compact token (`alg` `EdDSA` over Ed25519, protected header `kid` from `/.well-known/jwks.json`).
- `valid_until` is computed from the Valio order cutoff (`closingTime`, the day before delivery in `Europe/Helsinki`).
- `place_order` (`POST /v1/orders`) verifies the signature with the algorithm pinned to `EdDSA`, checks `quote_id`, `customer`, `agent_id`, and `valid_until`, and atomically sets `used_at` so a quote can only be redeemed once. Failed redemptions create no order and write `order.rejected` to the audit log.
- Key rotation (`python -m aimo.keys rotate`) moves the previous public key to `retired-<kid>.json` and keeps publishing it in `/.well-known/jwks.json`, so pre-rotation quotes remain verifiable and redeemable. Anyone can verify a quote offline with the public keys; see [STANDARDS](STANDARDS.md).
- Quote errors return HTTP 409 with `code` and `text`: `BAD_SIGNATURE`, `QUOTE_MISMATCH`, `WRONG_AGENT`, `QUOTE_EXPIRED`, `QUOTE_USED`, `UNKNOWN_QUOTE`, and `INVALID_QUOTE` (for example a delivery date in the past). Request lines with `quantity <= 0` are rejected at schema validation with HTTP 422.

## Policy decisions

`policy.decide` evaluates the quote against the customer's mandate and catalog metadata, returning every matching reason. The outcome is the strictest one: `blocked`, then `needs_approval`, then `auto`.

| Code | Effect |
|---|---|
| `WITHIN_MANDATE` | `auto`: placed and confirmed automatically |
| `OVER_ORDER_LIMIT`, `OVER_WEEK_LIMIT`, `SUBSTITUTION_NEEDS_APPROVAL`, `UNKNOWN_PRODUCT` | `needs_approval`: held in `pending_approval` |
| `ALLERGEN_CONFLICT`, `CATEGORY_NOT_ALLOWED`, `SUBSTITUTION_NOT_ALLOWED`, `PAST_CUTOFF`, `INVALID_QUOTE` | `blocked`: refused (`ALLERGEN_CONFLICT` covers both `CONTAINS` and `may_contain` traces) |
| `AGENT_REQUESTED` | `needs_approval`: caller set `ask_human=true` on an otherwise `auto` order |
| `STOCKOUT` | `needs_approval`: supplier stock dropped after placement (`orders.react_stockout` reallocates or records shortfall and reopens affected orders to `pending_approval`) |
| `NON_PREFERRED_BRAND` | informational note attached to `auto` decisions |

Live run on a fresh database: `school-hervanta` (600.00 EUR per-order limit, 3,000.00 EUR weekly budget) ordering 1,000 L of lactose-free milk (`Valio rasvaton maito 1 l laktoositon UHT`, 100 cases at 13.32 EUR net = 1,332.00 EUR net, 1,512.00 EUR gross) returns status `pending_approval` with `OVER_ORDER_LIMIT` (`Order total 1,332.00 EUR is over the 600.00 EUR per-order limit`). Smaller orders under 600.00 EUR confirm automatically until rolling 7-day net spend exceeds 3,000.00 EUR, which adds `OVER_WEEK_LIMIT`.

## Human approval

A `pending_approval` order waits in `GET /v1/approvals` (and transitions to `expired` if `valid_until` passes first). Only `X-Admin-Key` can call `POST /v1/approvals/{order_id}`, and `by` must match one of that customer's named `approvers`, otherwise 403 `NOT_APPROVER` (unknown order id returns 404 `NOT_FOUND`). Approving moves the order through `approved` and `placed` to `confirmed`; rejecting moves it to `rejected`. If the quote expired while waiting, the order is marked `expired` and returns 409 `QUOTE_EXPIRED`. A decided order cannot be decided again (409 `NOT_PENDING`). The approver, note, and hold reasons are recorded in the audit log (`approval.approved` or `approval.rejected`).

## Audit

Every step writes an append-only SQLite event (`BEGIN IMMEDIATE`) with a plain-language `why`, SHA-256 hash-chained from a 64-zero genesis hash over canonical JSON of `(seq, ts, customer, actor, action, subject, detail, why)`. `GET /v1/audit` (and MCP `get_audit_trail`) returns `events`, a narrated Helsinki-time `text` timeline, `verified {ok, bad_seq}` recomputed over the full chain, and a `checkpoint {seq, hash, ts, jws}` signed with the active Ed25519 key (`python -m aimo.audit verify-checkpoint`).

## Checked on a booted server

| Attack or guardrail probe | Result |
|---|---|
| Tampered quote payload (edited price, quantity, total, customer, agent, or cutoff) | 409 `BAD_SIGNATURE` (no order created, `order.rejected` audited) |
| `alg: none`, `HS256` with public key, swapped `kid`, or foreign Ed25519 key | 409 `BAD_SIGNATURE` (`tests/test_quotes.py`) |
| Replay of an already used quote | 409 `QUOTE_USED` |
| Another customer's agent redeems a quote | 409 `WRONG_AGENT` (quote stays redeemable by its owner) |
| Quote ID mismatch or unknown quote ID | 409 `QUOTE_MISMATCH` or `UNKNOWN_QUOTE` |
| Hotel key reads a school order or filters another customer | 403 `forbidden` |
| Agent key or missing key calls `decide_approval`, `PUT /v1/mandate`, or `/v1/admin/simulate` | 403 `forbidden` (agent key) or 401 `unauthorized` (missing or bad key) |
| Approval by another customer's approver | 403 `NOT_APPROVER` |
| Second decision on an already decided order | 409 `NOT_PENDING` |
| Approval after quote `valid_until` passed | 409 `QUOTE_EXPIRED` (order marked `expired`) |
| Quote with a delivery date in the past | 409 `INVALID_QUOTE` (`Delivery date is in the past.`) |
| Quote line with quantity `0` or `-5` | 422 `Input should be greater than 0` |
| Mandate with non-positive limits or `per_order_eur > per_week_eur` | 409 `INVALID_MANDATE` |
| Edited SQLite audit row or tampered checkpoint JWS | `verified.ok = false` with `bad_seq`, checkpoint CLI exits 1 (`BAD_SIGNATURE`) |
