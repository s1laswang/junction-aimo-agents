# Demo script, click by click

Server: `.venv/bin/uvicorn aimo.app:app --port 8080`. UI at `http://localhost:8080/`. Customer picker on every page (hotel-aurora, hospital-tays, school-hervanta), keys are `demo-<customer>`. Inbox labels are confirmed by ui-inbox (branch ui-inbox/inbox, may shift slightly). Console labels confirmed by ui-console (branch ui-console/console, may shift).

Before going on stage: server up, `data/` linked, `.env` present, three tabs open: console, approval inbox, audit. Run `python scenarios/run_all.py --base http://localhost:8080` once beforehand so the cache is warm and we know it is green. Never run a scenario for the first time on stage.

Each scene maps to a scenario file in `scenarios/` that already checks it, so what you see on stage is what the test proved.

## Scenes, one per judging criterion (about 3 min total)

### Scene 1, working end to end (30 pts), 80 s, s1_milk
| Click | Say | Judge sees | Fallback |
|---|---|---|---|
| Open `/console.html`, header select top right = hotel-aurora | "A hotel's buying agent." | Mandate card with the limits | `GET /v1/mandate` with `X-Agent-Key: demo-hotel-aurora` |
| Click chip "Lactose-free milk" (fills the "Request" box with the milk request, or type it) and press "Run" bottom right | "One line, like a person writes it." | Need parsed: 1000 L, lactose-free, date | No LLM key: rule parser, same output. Say nothing. |
| Offers appear | "Real Valio products. Price, stock on Monday, why it ranked." | Offers covering 1000 L, shortfall 0, notes per offer | Cached responses in `var/cache` |
| Watch the right column "Agent trace": parse_request, get_offers, request_quote, place_order appear one by one, expand one to show request/response | "Price and stock locked, signed. Anyone can verify it." | Signed quote, order with a Decision and reasons | `GET /.well-known/jwks.json` |
| Open audit tab | "Every step, in words." | Events each with a why | `GET /v1/audit?order_id=` |

### Scene 2, precise match (20 pts), 30 s, s2_need_recipe_order
Type (no chip for this one) `We need to cook lasagne for 40 portions by Monday`. Say: "It does not ask me for a shopping list. Dish to ingredient lines, scaled to 40 portions." Judge sees: several ingredient lines (pasta, meat, ...), offers for most, one quote with a line per ingredient. Then switch the header to hospital-tays, `100 litres of lactose-free milk and 50 kg of bread`: say "Allergens the hospital bans never show up in offers." (s5_allergen_safe). Fallback: scenario output lines from `python scenarios/s2_need_recipe_order.py`.

### Scene 3, autonomy and controls (20 pts), 45 s, s0_guardrails + s5_allergen_safe
1. Place a big order as hotel-aurora (quantity far above the limit). Say: "Over the per-order limit, so it does not go through." Judge sees: Decision needs_approval, plain reason, card in the approval inbox, `/inbox.html` (nav link "Approvals"), which refreshes itself every 3 seconds. The card shows the net total and sentences like "Over per-order limit 2,000 EUR by 339.96 EUR".
2. Click the button "Approve <total> EUR" in the inbox. First click opens "Who is approving?", pick the name, press Continue. Toast says "Approved <customer> <total> EUR as <name>". (Reject needs a typed reason, field "Reason for rejecting (required)".) Say: "A human gets a sentence, not JSON." Order moves to placed.
3. As hospital-tays, order a product with a banned allergen. Say: "Not asked. Blocked." Decision blocked with a reason.
4. If time: tampered signature. Say: "Change one character in the signed quote and the order is rejected." Scenario s0 proves it. Fallback for all: audit tab shows the same story.

### Scene 4, exceptions (15 pts), 40 s, s4_stockout + s3_event_substitution
1. Place and approve a 200 L lactose-free milk order. Open the console as `/console.html?demo=1` and press "Simulate stock-out" (hits the first line of the last order, available_amount 0), "Clear" when done. Both show up as trace lines. Button is promised by lead-ui, not on dev yet. Fallback: trigger it with the admin call used by the scenario (`simulate stockout`, see `scenarios/_common.py`, header `X-Admin-Key`), run in a terminal next to the browser. The trace polls audit every 2 s so the reaction shows up. The "Allergen-safe school swap" chip shows a shortfall row in the offers table, use it as the visual.
2. Say: "Wednesday the supplier runs out." Judge sees: order reacts (split, substitute or new date), a human is asked before anything changes, the audit has the stock-out with a why.
3. Chip "Last-minute event". Say: "Last-minute event, 400 portions, two days out, items short. It substitutes by recipe logic and writes down why." (s3). Clear the simulation after.
Fallback: run `python scenarios/s4_stockout.py --base http://localhost:8080` and show the PASS lines.

### Scene 5, openness and production path (15 pts), 30 s, s1 MCP check + s6 + s7
1. Open `/.well-known/agent-card.json` and `/openapi.json`. Say: "REST and MCP. Any agent plugs in, no custom adapter."
2. Show jwks.json: "Public keys, anyone verifies our quotes."
3. Say: "The mock has no order endpoint, so orders are ours. In production we swap in the real system, same interface." Then proactive seller (s6): "The selling side proposes too, within each buyer's allergens and categories." Standing order drift (s7): "It flags a changed price or missing stock on a standing order, allergen changes always go to a human."
Fallback: `python scenarios/s6_proactive_seller.py`, `s7_drift.py`.

## If something breaks
1. App down: restart uvicorn, `make demo` if sh-deploy ships it (TODO). Backup is a screen recording, record after 07:00.
2. Slow Valio API: "shared mock, we cache it", move on.
3. LLM down: rule parser is the default path, do not mention it.
4. Anything odd on screen: go to the audit tab.

## Cut order if short on time
Scene 5 step 3, then Scene 3 step 4, then scene 2 hospital line. Never cut the approve click or the audit tab.

## Do not
Do not loop-call the Valio API. Do not leave a stock-out simulation on after a scene.
