# Demo script, click by click

App: `.venv/bin/uvicorn aimo.app:app --port 8080`, UI at `http://localhost:8080/`. Exact buttons marked TODO until lead-ui screens land. Replace TODO lines then.

Before going on stage: server running, `data/` linked, `.env` present, browser on the console page, audit page open in a second tab, approval inbox open in a third. Seed customers: hotel-aurora, hospital-tays, school-hervanta.

## Run of show (80 s core, 2 min with extras)

| # | Click | Say | Judge should see | Fallback |
|---|---|---|---|---|
| 1 | Console, customer = hotel-aurora (TODO exact control) | "This is the hotel's buying agent." | Mandate card: per order, per week, allergens | Reload page; or `curl localhost:8080/v1/mandate -H "X-Agent-Key: <key>"` |
| 2 | Paste `I need 1,000 litres of lactose-free milk by Monday` and press Run (TODO) | "One line, like a person would write it." | Parsed need: milk, 1000 L, lactose-free, date | No LLM key: rule parser runs, same Need shape. Say nothing, it just works. |
| 3 | Offers list appears | "Real Valio products. Price, stock Monday, reliability, CO2. Each says why it ranked." | Ranked offers with tier and notes, split across products to reach 1,000 L | If prices fail: cached responses in `var/cache` |
| 4 | Request quote (TODO) | "Price and stock locked, signed." | Quote id, valid until, JWS | `GET /.well-known/jwks.json` to show the keys |
| 5 | Place order (TODO) | "Over the order limit, so it stops here." | Decision = needs_approval with plain reasons | Use a larger quantity to force it |
| 6 | Open approval inbox, click Approve (TODO) | "A human gets a sentence, not JSON." | Order moves to placed | `POST /v1/approvals/{order_id}` with `{"approve":true,"by":"momin"}` |
| 7 | Audit tab | "Every step, in words, chained." | Event list with why, hashes | `GET /v1/audit?order_id=...` text field |

## Extras if time (each 20 s)

- Hospital: switch to hospital-tays, same request with a milk allergen rule. Shows blocked, not asked.
- Stock-out: run the stock-out scenario in `scenarios/` (TODO name). Shows substitute or honest failure.
- MCP: show `GET /.well-known/agent-card.json`, say "any agent can read this and start".

## If something breaks

1. App down: `make demo` (or one command, TODO once sh-deploy ships it). Backup is a recorded screen capture, record it after 07:00.
2. Slow API: say "this is the shared mock, we cache it", go on.
3. LLM down: nothing to do, rule parser is the default path. Do not apologise.
4. Anything wrong on screen: go to the audit tab, it always has the story.

## Do not

Do not run a live scenario for the first time on stage. Do not click anything that calls the Valio API in a loop.
