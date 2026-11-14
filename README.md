# Trusted Trade Agents

A gateway that lets any buyer agent find, compare, quote, and order from Valio Aimo. Prices and stock come from the Valio API over the 17,546-product catalog. Every quote is Ed25519-signed (JWS), every order is checked against the buyer's mandate, risky orders wait for a named human approver, and every step is written to a hash-chained audit ledger. One service speaks MCP and REST/OpenAPI, so no custom integration is needed.

Built for Junction 2026, Valio Aimo challenge.

## Quickstart (60 seconds)

```sh
ln -s ~/aimo-agents/data data && ln -s ~/aimo-agents/.env .env
make demo
```

`make demo` runs `setup`, executes the 8 scenarios against a throwaway database (`scen`), then resets `var/demo.db` and keeps a fresh server up (`stage`). `make stage` starts only the fresh server; `make run` starts without resetting `var/demo.db`. All `Makefile` targets: `demo`, `stage`, `run`, `test`, `unit`, `mcp`, `scenarios`, `scen`, `check`, `setup`. Set `PORT` to change the port (default `8080`). No LLM key is needed.

Interfaces on `http://localhost:8080`:

- REST API under `/v1`, OpenAPI 3.1 spec at `/openapi.json`, and health check at `/health`
- MCP (streamable HTTP, 12 tools) at `/mcp`, or stdio via `.venv/bin/python -m aimo.mcp_server`
- Discovery at `/.well-known/agent-card.json` and Ed25519 public keys at `/.well-known/jwks.json`

## UI screens

Served at `http://localhost:8080`:

- `/` (overview): pending approvals, recent audit events, agent connection snippets, and discovery links
- `/console.html` (live agent console): run buyer requests end to end with a live tool trace and stock-out simulation
- `/inbox.html` (human approval inbox): plain-language mandate reasons with named approver sign-off or rejection
- `/audit.html` (hash-chained audit ledger): narrated timeline, in-browser SHA-256 chain verification, signed checkpoint, and JSON export
- `/admin.html` (mandate editor): per-order and weekly EUR limits, excluded allergens, allowed categories, substitution policy, and approvers

## Example

Request from the hotel agent:

> I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability.

Flow: `parse_request` turns it into a structured `Need`, `get_offers` returns ranked offers with price, stock, delivery date, reliability, and CO2, `request_quote` returns a signed quote, and `place_order` verifies the quote once and evaluates the mandate (`auto`, `needs_approval`, or `blocked`).

Live run (2026-11-14) on the seeded hotel agent (`hotel-aurora`, key `demo-hotel-aurora`): `get_offers` for `2026-11-16` picks product `464119` (`Valio rasvaton maito 1 l laktoositon UHT`, sales unit `KI`, 10 L case) at `13.32 EUR` net per `KI` for quantity `100` (`0.0` shortfall, reliability `1.0`, `1.1` kg CO2-eq/kg, plus ranked alternatives). `request_quote` for `100` `KI` returns an Ed25519-signed quote of `1,332.00 EUR` net (`1,512.00 EUR` gross), valid until the Valio cutoff (`2026-11-15T15:00:00+02:00`). `place_order` returns status `confirmed` with decision `auto` (`WITHIN_MANDATE`: `"Order total 1,332.00 EUR is under the 2,500.00 EUR per-order limit and 1,332.00 EUR of the 15,000.00 EUR weekly budget"`). On `school-hervanta` (`600 EUR` per-order limit), the same `1,332.00 EUR` order waits in `pending_approval` (`OVER_ORDER_LIMIT`) for a named approver; see [MCP client and Docker setup](deploy/mcp-clients.md) for the full step-by-step trace.

## Verification

Run the unit suite, pre-stage readiness verifier, catalog precision benchmark, and dataset audit:

```sh
make unit
.venv/bin/python scripts/smoke_demo.py
AIMO_EVAL=1 .venv/bin/python scripts/eval_precision.py
.venv/bin/python pitch/measure_data.py
```

Verified outputs on `data/`:

- `scripts/smoke_demo.py`: `PASS (5/5 checks)` in-process (`/health`, discovery `agent-card.json` with 12 tools + `jwks.json` + `openapi.json`, all 5 UI views, streamable HTTP `/mcp` `initialize` + `tools/list` + `get_mandate`, and `parse.request` + hash-chain `verified.ok=True` + signed checkpoint JWS). Pass `--url http://localhost:8080` to verify a live server.
- `scripts/eval_precision.py`: `40/40` top-1 match (`set1` `20/20`, `holdout` `10/10`, `holdout2` `10/10`).
- `pitch/measure_data.py`: `17,546` products (`0` deleted, `349` categories, `4,178` declared lactose-free, `321` low-lactose, `3,049` gluten-free, `2,959` vegan, `9,436` with allergen info, `4,425` delivery-reliability rows, `11,033` CO2 rows).

Verify any signed quote or signed audit checkpoint (`GET /v1/audit` returns `checkpoint.jws`) offline against the gateway's public JWKS URL or a saved JWKS file:

```sh
.venv/bin/python -m aimo.quotes verify "$JWS" http://localhost:8080/.well-known/jwks.json
.venv/bin/python -m aimo.audit verify-checkpoint "$CHECKPOINT_JWS" http://localhost:8080/.well-known/jwks.json
```

See [Open standards](docs/STANDARDS.md) and [Trust protocol](docs/TRUST.md) for JWS details, single-use quote redemption, and red-team checks.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Trust protocol](docs/TRUST.md)
- [Open standards](docs/STANDARDS.md)
- [Judging checklist](docs/JUDGING.md)
- [Assumptions](docs/ASSUMPTIONS.md)
- [Production path](docs/PRODUCTION.md)
- [MCP client and Docker setup](deploy/mcp-clients.md)
- [Public URL plan](deploy/PUBLIC_URL.md)
- [Contract](CONTRACT.md)

## Pitch and demo

- [3-minute pitch](pitch/PITCH.md)
- [Demo script](pitch/DEMO.md)
- [Slide outline](pitch/DECK.md)
- [Measured numbers](pitch/NUMBERS.md)
