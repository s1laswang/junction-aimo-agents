# Demo script, click by click

Server: `make demo` (runs all scenarios to warm `var/cache`, resets `var/demo.db`, and starts `.venv/bin/uvicorn aimo.app:app --port 8080`). UI at `http://localhost:8080/` with top nav `Console` (`/console.html`), `Approvals` (`/inbox.html`), `Audit` (`/audit.html`), `Mandates` (`/admin.html`). Customer picker (`Customer` select, top right) on every page (`hotel-aurora`, `hospital-tays`, `school-hervanta`); agent header `X-Agent-Key: demo-<customer>`, admin header `X-Admin-Key: demo-admin`.

Before going on stage: `data/` linked, `.env` present, `make demo` running (or `.venv/bin/python scenarios/run_all.py --base http://localhost:8080` already green against a live server), three tabs open: `/console.html`, `/inbox.html`, `/audit.html`. Never run a scenario for the first time on stage.

Each scene maps to a scenario file in `scenarios/` that already checks it, so what you see on stage is what the test proved.

## Scenes, one per judging criterion (about 3 min total)

### Scene 1, working end to end (30 pts), 80 s, s1_milk
| Click | Say | Judge sees | Fallback |
|---|---|---|---|
| Open `/console.html`, header `Customer` select top right = `hotel-aurora` (or show `/admin.html` "Mandates") | "A hotel's buying agent." | `hotel-aurora` selected; mandate limits 2,500 EUR per order, 15,000 EUR per week | `GET /v1/mandate` with `X-Agent-Key: demo-hotel-aurora` |
| Click chip "Lactose-free milk" (fills "Request" and selects `hotel-aurora`) and press "Run" bottom right | "One line, like a person writes it." | "Parsed need": milk, 1000 L, lactose-free, delivery date, plus mandate summary line | No LLM key: rule parser, same output. Say nothing. |
| "Offers" table appears | "Real Valio products. Price, stock on Monday, why it ranked." | Offers covering 1000 L (`picked` tag, price, availability, reliability, CO2/kg, notes per offer) | Cached responses in `var/cache` |
| Watch the right column "Agent trace": `parse_request`, `get_offers`, `request_quote`, `place_order` appear one by one, expand "request / response" on one | "Price and stock locked, signed. Anyone can verify it." | "Signed quote" (`signed Ed25519`) and "Order" ("Auto-approved", status `confirmed`, `WITHIN_MANDATE` reason) | `GET /.well-known/jwks.json` |
| Click "Audit trail" on the order (or open `/audit.html`, nav link "Audit") | "Every step, in words." | Verified hash-chain badge, "Order receipt", events each with a `why` ("Ledger" / "Plain text") | `GET /v1/audit?order_id=<order_id>` |

### Scene 2, precise match (20 pts), 30 s, s2_need_recipe_order + s5_allergen_safe
Type (no chip for this one) `We need to cook lasagne for 40 portions by Monday` and press "Run". Say: "It does not ask me for a shopping list. Dish to ingredient lines, scaled to 40 portions." Judge sees: several ingredient lines (pasta, meat, ...), offers for most, one signed quote with a line per ingredient. Then click chip "Allergen-safe hospital" (switches `Customer` to `hospital-tays` and fills `We need 100 litres of lactose-free milk and 50 kg of bread by Monday.`) and press "Run": say "Allergens the hospital bans never show up in offers." (`hospital-tays` excludes `peanut`, `nuts`, `egg`). Fallback: scenario output lines from `python scenarios/s2_need_recipe_order.py` and `python scenarios/s5_allergen_safe.py`.

### Scene 3, autonomy and controls (20 pts), 45 s, s0_guardrails + s5_allergen_safe
1. Click chip "Over limit, school" and press "Run" (`school-hervanta` per-order limit is `600 EUR`, triggers `OVER_ORDER_LIMIT` immediately), or as `hotel-aurora` place an order above `2,500 EUR` (for example `2,000 litres` of lactose-free milk). Say: "Over the per-order limit, so it does not go through." Judge sees: "Needs approval" (`pending_approval`), plain reason, link "Open in Approvals" to `/inbox.html` (nav link "Approvals"), which polls `/v1/approvals` every 3 seconds. Under "Waiting for you", the card shows the net total and the reason `"Order total <total> EUR is over the 600.00 EUR per-order limit"` (or `"... over the 2,500.00 EUR per-order limit"` for `hotel-aurora`).
2. Click the button `"Approve <total> EUR"` in `/inbox.html`. First click opens `"Who is approving?"`, pick an approver from the mandate (`Sari Nieminen` for `school-hervanta`; `Laura Virtanen` or `Mikko Heikkinen` for `hotel-aurora`), press "Continue". Toast says `"Approved <customer> <total> EUR as <name>"`. (Clicking "Reject" opens `"Reason for rejecting (required)"` and changes the button to "Reject order".) Say: "A human gets a sentence, not JSON." Order moves through `placed` to `confirmed` and appears under "Decided today".
3. As `hospital-tays`, quote and order a product containing a banned allergen via `/v1/orders` (`python scenarios/s5_allergen_safe.py --base http://localhost:8080`, since `/v1/offers` already filters unsafe products out). Say: "Not asked. Blocked." Decision `blocked` with `ALLERGEN_CONFLICT` reason.
4. If time: tampered signature. Say: "Change one character in the signed quote and the order is rejected." `POST /v1/orders` returns `409`; scenario `s0_guardrails.py` proves it. Fallback for all: `/audit.html` shows the same story.

### Scene 4, exceptions (15 pts), 40 s, s4_stockout + s3_event_substitution
1. Open `/console.html?demo=1` as `hotel-aurora`, run a 200 L lactose-free milk order (auto-approved), then press "Simulate stock-out" in the order panel (calls `POST /v1/admin/simulate` with `X-Admin-Key: demo-admin`, `available_amount: 0` on the first order line, and re-runs the request) and press "Clear" (`DELETE /v1/admin/simulate`) when done. Trace lines in "Agent trace": `"Simulated stock-out on <product_name>"`, `"Cleared simulation"`, plus `order.stockout` from the 2 s `/v1/audit` poll. (All four chips on `/console.html` are `"Lactose-free milk"`, `"Over limit, school"`, `"Allergen-safe hospital"`, `"Last-minute event"`.) Fallback: trigger via `scenarios/_common.py` (`simulate`, header `X-Admin-Key: demo-admin`) in a terminal next to the browser.
2. Say: "Wednesday the supplier runs out." Judge sees: existing order moves to `pending_approval` with a `STOCKOUT` reason (split, substitute or shortfall), a human is asked in `/inbox.html` before anything changes, and `/audit.html` records `order.stockout` with a `why`.
3. Click chip "Last-minute event" (fills the 120-guest Friday salmon, cream and butter request for `hotel-aurora`), or type `Last-minute event, we need pasta carbonara for 400 portions by Wednesday` for recipe substitution (`s3_event_substitution`). Say: "Last-minute event, short notice. Where stock is short, it substitutes within the rules and writes down why." Always press "Clear" after so no stock-out override stays active.
Fallback: run `python scenarios/s4_stockout.py --base http://localhost:8080` and `python scenarios/s3_event_substitution.py --base http://localhost:8080` and show the PASS lines.

### Scene 5, openness and production path (15 pts), 30 s, s1 MCP check + s6 + s7
1. Open `/.well-known/agent-card.json` and `/openapi.json` (also linked on `/` under "Connect an agent" and "Discovery", with `/mcp` for MCP). Say: "REST and MCP. Any agent plugs in, no custom adapter."
2. Open `/.well-known/jwks.json`: "Public keys, anyone verifies our quotes."
3. Say: "The mock has no order endpoint, so orders are ours. In production we swap in the real system, same interface." Then proactive seller (`s6_proactive_seller`): "The selling side proposes too, within each buyer's allergens and categories." Standing order drift (`s7_drift`): "It flags a changed price or missing stock on a standing order, allergen changes always go to a human."
Fallback: `python scenarios/s6_proactive_seller.py --base http://localhost:8080`, `python scenarios/s7_drift.py --base http://localhost:8080`.

## If something breaks
1. App down: run `make demo` for a clean reset (or `make run` / `.venv/bin/uvicorn aimo.app:app --port 8080` to keep `var/demo.db`; open `/console.html`, `/inbox.html`, and `/audit.html`). Backup is a screen recording, record after 07:00.
2. Slow Valio API: "shared mock, we cache it", move on.
3. LLM down: rule parser is the default path, do not mention it.
4. Anything odd on screen: go to `/audit.html` (nav link "Audit").

## Cut order if short on time
Scene 5 step 3, then Scene 3 step 4, then Scene 2 hospital line. Never cut the approve click or `/audit.html`.

## Do not
Do not loop-call the Valio API. Do not leave a stock-out simulation on after a scene.
