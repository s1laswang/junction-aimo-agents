# Trusted Trade Agents

A gateway that lets any buyer agent find, compare, quote and order from Valio Aimo. Prices and stock come from the Valio API. Every quote is signed, every order is checked against the buyer's mandate, risky orders wait for a human, and every step is written to a tamper-evident audit trail. One service speaks MCP and REST/OpenAPI, so no custom integration is needed.

Built for Junction 2026, Valio Aimo challenge.

## Quickstart (60 seconds)

```
ln -s ~/aimo-agents/data data && ln -s ~/aimo-agents/.env .env
make demo
```

`make demo` sets up the venv, boots the server, runs the scenarios and keeps the server up. Other targets: `setup`, `run`, `test`, `mcp`, `scenarios`. Set `PORT` to change the port (default 8080). No LLM key is needed.

Open `http://localhost:8080/` for the console, approval inbox and audit view. MCP at `/mcp`, stdio via `.venv/bin/python -m aimo.mcp_server`.

## Example

Request from the hotel agent:

> I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability.

Flow: `parse_request` turns it into a structured `Need`, `get_offers` returns ranked offers with price, stock, delivery date, reliability and CO2, `request_quote` returns a signed quote, `place_order` either places it or sends it to a human, depending on the mandate.

TODO-VERIFY: paste real curl and a trimmed real response here.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Open standards](docs/STANDARDS.md)
- [Assumptions](docs/ASSUMPTIONS.md)
- [Production path](docs/PRODUCTION.md)
- [Contract](CONTRACT.md)
