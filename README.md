# Trusted Trade Agents

A gateway that lets any buyer agent find, compare, quote, and order from Valio Aimo. Prices and stock come from the Valio API over the 17,546-product catalog. Every quote is Ed25519-signed (JWS), every order is checked against the buyer's mandate, risky orders wait for a named human approver, and every step is written to a hash-chained audit ledger. One service speaks MCP and REST/OpenAPI, so no custom integration is needed.

Built for Junction 2026, Valio Aimo challenge.

## Quickstart (60 seconds)

```sh
ln -s ~/aimo-agents/data data && ln -s ~/aimo-agents/.env .env
make demo
```

`make demo` sets up the venv, boots the server, runs the 8 scenarios against a throwaway database, then starts a fresh server for the stage and keeps it up. `make stage` starts only the fresh server. Last run: 8 of 8 scenarios passed. Other targets: `setup`, `check`, `run`, `test`, `mcp`, `scenarios`. Set `PORT` to change the port (default `8080`). No LLM key is needed.

Interfaces on `http://localhost:8080`:

- REST API under `/v1` and OpenAPI 3.1 spec at `/openapi.json`
- MCP (streamable HTTP) at `/mcp`, or stdio via `.venv/bin/python -m aimo.mcp_server`
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

Real run on the seeded hotel agent (key `demo-hotel-aurora`): one offer, Valio kevytmaito 1 l laktoositon UHT at 1.56 EUR net per carton, no shortfall. `request_quote` for 1,000 cartons returns a signed quote of 1,560.00 EUR net, 1,770.00 EUR gross, valid until the Valio cutoff. `place_order` returns status `confirmed` with decision `auto` (`WITHIN_MANDATE`: under the 2,500 EUR per-order limit and the 15,000 EUR weekly budget). Over the limit, the order waits in `pending_approval` for a human.

## Offline verification

Verify any signed quote or signed audit checkpoint (`GET /v1/audit` returns `checkpoint.jws`) against the gateway's public JWKS URL or a saved JWKS file:

```sh
python -m aimo.quotes verify "$JWS" http://localhost:8080/.well-known/jwks.json
python -m aimo.audit verify-checkpoint "$CHECKPOINT_JWS" http://localhost:8080/.well-known/jwks.json
```

See [Open standards](docs/STANDARDS.md) and [Trust protocol](docs/TRUST.md) for JWS details, single-use quote redemption, and red-team checks.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Trust protocol](docs/TRUST.md)
- [Open standards](docs/STANDARDS.md)
- [Judging checklist](docs/JUDGING.md)
- [Assumptions](docs/ASSUMPTIONS.md)
- [Production path](docs/PRODUCTION.md)
- [MCP client setup](deploy/mcp-clients.md)
- [Public URL plan](deploy/PUBLIC_URL.md)
- [Contract](CONTRACT.md)

## Pitch and demo

- [3-minute pitch](pitch/PITCH.md)
- [Demo script](pitch/DEMO.md)
- [Slide outline](pitch/DECK.md)
- [Measured numbers](pitch/NUMBERS.md)
