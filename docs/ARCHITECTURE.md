# Architecture

One Python process: FastAPI serves REST under `/v1`, MCP at `/mcp`, the UI at `/`, discovery under `/.well-known/`. State is in SQLite, opened only through `aimo/db.py` (path from `AIMO_DB`, default `var/aimo.db`). Each module creates its own tables on first use. Valio responses are cached in `var/cache`.

## Flow

```
buyer agent (MCP or REST, X-Agent-Key)
   |
   v
parse_request -> Need
   |
get_offers: search -> Valio /price + /availability -> rank -> split / substitute -> [Offer]
   |
request_quote -> signed Quote (JWS, valid until closingTime)
   |
place_order: verify JWS -> policy against Mandate
   |            |-> auto            -> placed -> confirmed
   |            |-> needs_approval  -> human decides -> placed / rejected
   |            '-> blocked
   v
audit: every step appends a hash-chained event with a plain-language why
```

Valio has no order endpoint, so orders live in our store (see ASSUMPTIONS).

## Auth

- Agents send `X-Agent-Key`. It maps to a customer, an agent id and a mandate. Calls are scoped to that customer.
- Humans and admins send `X-Admin-Key` (env `AIMO_ADMIN_KEY`). Needed to decide approvals, change mandates and use `/v1/admin/*`.
- Missing or bad key: 401. Wrong customer: 403. Domain errors: 409 with `code` and `text`.
- Search, parse, discovery and `/health` need no key.

## Modules

| Module | What it does | Owner |
|---|---|---|
| `aimo/app.py`, `models.py`, `db.py` | app wiring, pydantic models | lead-gateway |
| `catalog.py` | product search over the product JSON | gw-catalog |
| `valio.py`, `offers.py` | cached Valio client, offers and ranking | gw-offers |
| `orders.py` | order store and state machine | gw-orders |
| `api.py`, `mcp_server.py`, `discovery.py` | REST, MCP, well-known docs | gw-mcp |
| `quotes.py`, `keys.py` | signed quotes, Ed25519 keys, JWKS | tr-sign |
| `policy.py`, `mandates.py` | decision rules, agent keys and mandates | tr-policy |
| `approvals.py` | human approval queue | tr-approve |
| `audit.py` | hash-chained audit log | tr-audit |
| `parse.py` | text to Need, rule-based, LLM optional | br-parse |
| `substitute.py`, `allocate.py`, `recipes.py` | substitutes, split across offers, recipes | br-subst, br-split, br-recipe |
| `agents/` | buyer, seller and drift agents | lead-agents |
| `web/` | console, inbox, audit, admin UI | lead-ui |

The LLM is optional (`agents/llm.py`, OpenAI-compatible env vars). Without a key the rule-based parser runs.

TODO-VERIFY: module list and decision rules against real code on dev.
