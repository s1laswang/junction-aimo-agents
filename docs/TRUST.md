# Trust protocol

An outside agent never gets a bare price. It gets a signed commitment, acts inside a mandate, and leaves a trail a human can read and a third party can check.

## Identity and mandate

- Each buyer agent has a key in `X-Agent-Key`. It maps to one customer, one agent id and one mandate. Every agent call is scoped to that customer: another customer's order returns 403.
- The human side uses `X-Admin-Key`. It is needed to decide approvals, change a mandate (`PUT /v1/mandate`) and use `/v1/admin/*`.
- Missing key: 401. An agent key on an admin route: 403.
- A mandate holds per-order and per-week limits, excluded allergens, allowed categories, a substitution rule (`never`, `within_prefs`, `ask`), preferred brands and named approvers. Seeded limits are in [ASSUMPTIONS](ASSUMPTIONS.md).

## Signed quotes

- `request_quote` returns a Quote as a JWS compact token: `alg` EdDSA (Ed25519), header `kid` from the JWKS.
- `valid_until` is the Valio order cutoff (`closingTime`). A quote can be redeemed once.
- `place_order` verifies the signature with the algorithm pinned to EdDSA, checks quote id, customer and agent, checks expiry, then marks the quote used.
- Anyone can verify a quote with the public keys. See [STANDARDS](STANDARDS.md).
- Errors are HTTP 409 with `code` and `text`: `BAD_SIGNATURE`, `QUOTE_MISMATCH`, `WRONG_AGENT`, `QUOTE_EXPIRED`, `QUOTE_USED`, `UNKNOWN_QUOTE`, `INVALID_QUOTE` (for example a delivery date in the past).

## Policy decisions

`policy.decide` returns every reason. The outcome is the strictest one: blocked, then needs_approval, then auto.

| Code | Effect |
|---|---|
| `WITHIN_MANDATE` | placed automatically |
| `OVER_ORDER_LIMIT`, `OVER_WEEK_LIMIT`, `SUBSTITUTION_NEEDS_APPROVAL` | needs approval |
| `ALLERGEN_CONFLICT`, `CATEGORY_NOT_ALLOWED`, `SUBSTITUTION_NOT_ALLOWED`, `PAST_CUTOFF` | blocked |
| `AGENT_REQUESTED` | the agent asked for a human |
| `NON_PREFERRED_BRAND` | info only |

Live run: a school-hervanta milk order over its 600 EUR limit came back `pending_approval` with `OVER_ORDER_LIMIT` and `OVER_WEEK_LIMIT`.

## Human approval

A `pending_approval` order waits in the queue. Only the admin key can decide, and `by` must be one of that customer's named approvers, otherwise 403 `NOT_APPROVER`. A decided order cannot be decided again (409 `NOT_PENDING`). The note is audited.

## Audit

Every step writes an event with a plain-language reason. The log is append-only and hash-chained from a zero hash. `GET /v1/audit` returns the events, a narrated `text`, `verified {ok, bad_seq}` recomputed over the chain, and a `checkpoint {seq, hash, ts, jws}` signed with the same key as the quotes.

## Checked on a booted server

| Attack | Result |
|---|---|
| Tampered quote token | `BAD_SIGNATURE` |
| Replay of a used quote | 409 `QUOTE_USED` |
| Another customer's agent redeems a quote | 409 `WRONG_AGENT` |
| Hotel key reads a school order | 403 |
| Agent key calls decide_approval | 403 |
| Approval by another customer's approver | 403 `NOT_APPROVER` |
| Second decision on a decided order | 409 `NOT_PENDING` |
| `PUT /v1/mandate` with an agent key or no key | 403 or 401 |
| Quote with a delivery date in the past | 409 `{"code":"INVALID_QUOTE","text":"Delivery date is in the past."}` (found in red-team, fixed) |
| Quote with quantity 0 or -5 | 422 `Input should be greater than 0` from the request model (found: it had been signed and auto-confirmed, fixed) |

Also reported by tr-sign from its own red-team runs, not re-run here: `alg: none`, HS256 with the public key, swapped `kid`, forged EdDSA token and edited price, quantity or total all give `BAD_SIGNATURE`.
