# Live demo script (3 minutes)

Presenter: Silas (talks). Driver: Chintufp (clicks). Screen: browser at `http://localhost:8000/ui`, 1280×800 or larger, zoom 110%.
Judging weights in brackets: **[E2E 30]** end-to-end demo, **[NEED 20]** solves the user need, **[CTRL 20]** autonomy and controls with auditability, **[ROB 15]** robustness to exceptions, **[OPEN 15]** openness and production path. **[GEM]** marks the Gemini prize points.

UI labels below are from `web/app.js` on main (Sat 15:00). A full live run takes about 35–40 s, of which the agent reaches the approval wait after about 25 s (measured with `uv run aimo demo`, 38 s including approval).

## Before you go on stage (T-15 min)

1. **Fresh state.** The weekly-budget rule sums every order placed in the last 7 days, so rehearsal orders change the verdict. Start from a clean state DB, keeping the signing key:
   ```bash
   mkdir -p ~/aimo-demo-data && cd ~/aimo-demo-data
   for f in /PATH/TO/junction-aimo-agents/data/*; do case "$f" in *state.sqlite*) ;; *) ln -sf "$f" . ;; esac; done
   cd /PATH/TO/junction-aimo-agents && AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve
   ```
2. **Check health:** `curl -s localhost:8000/healthz` must show every integration as `aimo.catalog` / `aimo.trust`, not `fallback`.
3. **Warm up once** (fills the 5-minute offers cache and the query-embedding cache): `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` must end in `MILK FLOW OK`. Then restart with a fresh state dir again (step 1) so the audit view starts clean.
4. **Cut-off clock.** The story depends on real mock-API data: 463038's cut-off passed Thu 12 Nov 15:00, and 464306 can be ordered for Monday until **Sun 15 Nov 17:00** (`closing_time` from `/v1/offers`). If you present after Sun 16:30, use the fallback (mock mode) or say "delivery Tuesday" and check the plan first.
5. Open three tabs: (a) `http://localhost:8000/ui/?customer=hotel-aurora`, (b) `http://localhost:8000/ui/?mock=1` (backup), (c) a terminal with large font in the repo directory.
6. Phone on silent, laptop on power, Wi-Fi checked (the mock API and Gemini are both online services).
7. **T-4 min:** start the robustness montage in tab (c) against the second gateway on port 8031 (see "Robustness montage" below). Run it under `caffeinate -i` so the laptop cannot sleep mid-run (the buyer report lost a run that way).

## The script

| Time | Click (driver) | Say (presenter) | Criteria |
|---|---|---|---|
| **0:00–0:20** | Tab (a) is showing the Trusted Trade console. Nothing clicked yet. | "Hotels, schools and hospitals are starting to send AI agents to buy for them. Valio Aimo can't hand an agent its SAP login. We built the front door instead: an open gateway any company's agent can use to find, quote and order, inside rules the *customer* sets, with every step signed." | [NEED] |
| **0:20–0:40** | Customer selector shows **Hotel Aurora Helsinki**. Point at the mandate line ("Mandate hotel-aurora/buyer-1 under policy hotel-aurora"). Check the model selector shows **Gemini 3.8 Flash**. Click **Run the example request** (it starts the run at once with "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."; typing the text and clicking **Run agent** does the same). | "This is the hotel's buyer agent, running on Gemini. It turns the sentence into a structured need with Gemini structured output: lactose-free, 1,000 litres, Monday 16 November." | [E2E] [GEM] |
| **0:40–1:10** | Timeline fills: parsed-need chips, tool calls `plan_need`, `get_offers`. Expand one tool call to show JSON. Point at the exception callouts: **cut-off passed** (463038) and **partial stock** (469363). | "Every step is a call to our MCP server, the same tools Claude or Cursor would use, and each hits Valio Aimo's live price and availability API. Two real exceptions: the hotel's usual 10-litre dispenser box missed its order cut-off on Thursday, and one alternative only has 384 units. The agent doesn't guess. It re-plans." | [ROB] [OPEN] |
| **1:10–1:35** | Click the **Plan** tab. Show the good-better-best cards: best = Valio Eila 1 L (464306), €1.01/L, 1,000 units, €1,010; better = Arla (partial stock); good = different fat %. Expand **Rejected** to show the cut-off reason. | "Good-better-best, each with a reason: best is the same brand and fat level in a different pack. Allergen data that's missing counts as *unknown*, never safe, and Gemini's reading of Finnish ingredient lists can only add a warning, never clear a product." | [NEED] [GEM] |
| **1:35–2:05** | Timeline shows **Waiting for procurement_manager to approve**. Click **Open approval inbox →** (Approvals tab). Point at the banner "Needs approval by Procurement Manager" and the three NEEDS APPROVAL rows (spend > €500, outside standing preferences, 3.3× usual weekly volume). Point at the quote hash and the **Ed25519 signature** badge and the price-lock countdown. Approver is preset to Anna (procurement manager). Click **Approve €1,010.00**. | "The hotel's policy, a versioned YAML file the hotel owns, checked 12 rules and three need a human: over the €500 limit, a pack outside their usual preference, and 3.3 times their normal weekly milk. The agent has paused. Anna approves *this exact* signed quote: the signature is verified right here in the browser, and if the price changed, her approval would not apply. There is no approve tool for agents." | [CTRL] [E2E] |
| **2:05–2:15** | Order response card: **Placed**, **AP** Accepted, 1,000 confirmed. | "Order placed, with a Peppol-style order response: accepted." | [E2E] [OPEN] |
| **2:15–2:35** | Click **View audit trail →**. Show the chain rail (agent, system, human nodes). Click **Verify chain** → green. Scroll the readable report for two lines. Hover **Download evidence pack**. | "And afterwards anyone can check what happened: every event is hash-chained and signed, in plain sentences. Verify recomputes every hash and signature. The evidence pack lets an auditor check it offline with our public key." | [CTRL] |
| **2:35–2:50** | **Robustness montage.** Switch to terminal tab (c), where `uv run aimo demo --montage` was started about 4 minutes before going on stage (see "Robustness montage" below). Scroll to **== Montage verdicts ==**: six green **PASS** lines. Optional build on the deck: wind tunnel table. | "That was one request. Here are six more, run live by the same Gemini agent while we were talking: a stock-out split into four lines, a school with milk and nut allergies, a prompt injection planted in supplier text, a standing order thirteen times too big, a last-minute event. Six of six pass, 22 to 37 seconds each, about three cents of Gemini per order. **TODO:** wind tunnel line when the numbers land." | [ROB] [GEM] |
| **2:50–3:00** | Click **Connect your agent** in the header (MCP config dialog), or open `/.well-known/otap.json`. | "It's all open: MCP, OpenAPI 3.1, a discovery manifest, JWS signatures, Peppol semantics. Swap the mock for SAP behind one interface, put OAuth in front, and this goes to production. Trusted Trade Agents." | [OPEN] |

### Lines to have ready for questions
- **"What if the agent lies about the tier?"** It can't help itself: the gateway recomputes tier and substitution flags server-side and the policy re-verifies them. A substitute sent as "preferred" still comes back as "better, outside preference" and needs approval (verified Sat 18:26).
- **"Can the agent approve its own order through REST?"** No. Approvals are accepted only from a named person in the customer's approver directory; agent identities get 403 `approval_refused` (verified Sat 18:26).
- **"Why not just give the agent an API key?"** An API key says *who*, not *what they may do*. The mandate plus policy says what, and the signed quote plus approval binding proves what was agreed.
- **"What about prompt injection in supplier data?"** Two layers: regex (0 false positives on 34,159 real supplier texts) and a Gemini classifier, and the buyer harness redacts flagged text before the model sees it. Flagged orders need a human.
- **"Is the order real?"** Valio Aimo has no order API in the mock, so we built a mock order backend behind the same interface a SAP adapter would implement.
- **"Why Gemini?"** Finnish/Swedish product data, embeddings in the same API, thinking levels so reasoning is only spent on the substitution decision, structured output everywhere.

## Robustness montage (`uv run aimo demo --montage`)

`uv run aimo demo --montage` runs the six live scenarios in sequence with the Gemini agent over MCP against the live mock API and approves as each policy's named approver (drift is left held for confirmation by design). It prints one verdict per scenario plus Gemini calls and tokens, and writes a results table.

Measured result to quote ([demo-montage.md](demo-montage.md), Sat 14 Nov 18:21): **6/6 PASS**, 22–37 s per scenario, 176 s wall clock in total, 50 Gemini calls, **$0.0339 per placed order** (was $0.0822 before the token trim, −59%), escalation to gemini-4-hydrogen HIGH on 6/6 runs but only on the quote-decision turn (19% of agent steps).

| Scenario | What the audience sees in the verdict line |
|---|---|
| happy | placed, EUR 1,010.00, approved by anna@hotel-aurora.example |
| stockout | placed, split into 4 lines, EUR 2,544.80, shortfall 0 |
| school-allergen | placed, Oatly Oat Drink Semi 1L, 2 candidates rejected, approved by liisa@school-tampere.example |
| injection | placed, 4 supplier texts flagged and redacted, policy cites injection, largest line 300 units |
| drift | needs_confirmation, EUR 4,040.00 quote prepared, place_order held by the agent |
| event | placed, 2 lines (lactose-free cream and butter), EUR 251.20 |

How to run it for the pitch (it takes about 3 minutes, so start it before going on stage):
1. Run it against a **second gateway with its own state**, so its five placed orders do not count towards the hotel's weekly budget in the hero demo:
   ```bash
   AIMO_DATA_DIR=~/aimo-montage-data uv run aimo serve --port 8031      # data dir prepared like step 1 of the checklist
   uv run aimo demo --montage --base-url http://localhost:8031 --montage-out /tmp/montage.md
   ```
   `--montage-out` keeps the committed `docs/demo-montage.md` unchanged.
2. Start it about 4 minutes before the slot, in terminal tab (c) with a large font, and leave the verdicts on screen.
3. If it fails or the Wi-Fi is down, show `docs/demo-montage.md` (the measured run) and say it is the recorded result.

Single scenarios for rehearsal or Q&A: `uv run aimo demo --scenario stockout|school-allergen|injection|drift|event --auto-approve-as <approver>`.

Live tamper demo without the mock: `curl -X POST localhost:8000/v1/demo/tamper/3`, click **Verify chain** (red, "event #3 was modified after it was recorded"), then `curl -X POST localhost:8000/v1/demo/untamper/3` (verified Sat 15:00).

## Fallback plan

| Problem | Do this | Time cost |
|---|---|---|
| Gemini slow or failing | The agent falls back to the deterministic need parser; keep talking over the timeline. If the run stalls for > 20 s, switch to tab (b) mock mode. | 5 s |
| Mock API down or slow (`/healthz` ok but offers 503) | Switch to tab (b): `http://localhost:8000/ui/?mock=1` plays the identical story with no backend. The header shows **MOCK DATA · no backend**: say so honestly. Mock mode also has **Simulate tampering** to show the chain turning red. | 5 s |
| Gateway won't start / laptop problem | Mock UI without the gateway: `cd web && python -m http.server 8765`, open `http://127.0.0.1:8765/index.html?mock=1`. | 30 s |
| Browser or UI broken | Terminal: `uv run aimo demo --auto-approve-as anna@hotel-aurora.example` (Gemini agent, about 30–40 s), or without Gemini `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` (search → offers → plan → signed quote → pending approval → wrong-hash 409 → approve → AP → audit verify, about 3 s). | 10 s |
| Everything down | Play the pre-recorded video (Chintufp records it at the Sun 06:00 freeze; file path **TODO**). | 0 s |

## Recording the video (2–3 minutes)

Use the same script and do the approval click by hand (don't use `&autoapprove=1` for the video: the human click is the point). Useful URL helpers: `?customer=hotel-aurora`, `&autorun=1` (starts the run on load), `&tab=plan|approvals|audit`. Record at 1280×800, add captions for the three NEEDS APPROVAL reasons and the verify-chain result, and end on the openness slide. Upload as unlisted and paste the link into `docs/SUBMISSION.md`.
