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

## The script

| Time | Click (driver) | Say (presenter) | Criteria |
|---|---|---|---|
| **0:00–0:20** | Tab (a) is showing the Trusted Trade console. Nothing clicked yet. | "Hotels, schools and hospitals are starting to send AI agents to buy for them. Valio Aimo can't hand an agent its SAP login. We built the front door instead: an open gateway any company's agent can use to find, quote and order, inside rules the *customer* sets, with every step signed." | [NEED] |
| **0:20–0:40** | Customer selector shows **Hotel Aurora Helsinki**. Point at the mandate line ("Mandate hotel-aurora/buyer-1 under policy hotel-aurora"). Click **Run the example request** (fills "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."). Model **Gemini 3.8 Flash**. Click **Run agent**. | "This is the hotel's buyer agent, running on Gemini. It turns the sentence into a structured need with Gemini structured output: lactose-free, 1,000 litres, Monday 16 November." | [E2E] [GEM] |
| **0:40–1:10** | Timeline fills: parsed-need chips, tool calls `plan_need`, `get_offers`. Expand one tool call to show JSON. Point at the exception callouts: **cut-off passed** (463038) and **partial stock** (469363). | "Every step is a call to our MCP server, the same tools Claude or Cursor would use, and each hits Valio Aimo's live price and availability API. Two real exceptions: the hotel's usual 10-litre dispenser box missed its order cut-off on Thursday, and one alternative only has 384 units. The agent doesn't guess. It re-plans." | [ROB] [OPEN] |
| **1:10–1:35** | Click the **Plan** tab. Show the good-better-best cards: best = Valio Eila 1 L (464306), €1.01/L, 1,000 units, €1,010; better = Arla (partial stock); good = different fat %. Expand **Rejected** to show the cut-off reason. | "Good-better-best, each with a reason: best is the same brand and fat level in a different pack. Allergen data that's missing counts as *unknown*, never safe, and Gemini's reading of Finnish ingredient lists can only add a warning, never clear a product." | [NEED] [GEM] |
| **1:35–2:05** | Timeline shows **Waiting for procurement_manager to approve**. Click **Open approval inbox →** (Approvals tab). Point at the banner "Needs approval by Procurement Manager" and the three NEEDS APPROVAL rows (spend > €500, outside standing preferences, 3.3× usual weekly volume). Point at the quote hash and the **Ed25519 signature** badge and the price-lock countdown. Approver is preset to Anna (procurement manager). Click **Approve €1,010.00**. | "The hotel's policy, a versioned YAML file the hotel owns, checked 12 rules and three need a human: over the €500 limit, a pack outside their usual preference, and 3.3 times their normal weekly milk. The agent has paused. Anna approves *this exact* signed quote: the signature is verified right here in the browser, and if the price changed, her approval would not apply. There is no approve tool for agents." | [CTRL] [E2E] |
| **2:05–2:15** | Order response card: **Placed**, **AP** Accepted, 1,000 confirmed. | "Order placed, with a Peppol-style order response: accepted." | [E2E] [OPEN] |
| **2:15–2:35** | Click **View audit trail →**. Show the chain rail (agent, system, human nodes). Click **Verify chain** → green. Scroll the readable report for two lines. Hover **Download evidence pack**. | "And afterwards anyone can check what happened: every event is hash-chained and signed, in plain sentences. Verify recomputes every hash and signature. The evidence pack lets an auditor check it offline with our public key." | [CTRL] |
| **2:35–2:50** | Switch to the robustness slide (deck) or terminal: show wind-tunnel numbers. **TODO: numbers from the wind tunnel run.** | "We didn't test this on one happy path. Our wind tunnel had Gemini generate **TODO N** synthetic hotels, schools and hospitals, with stock-outs, outages and prompt injections. Result: **TODO**% correct escalations, **TODO 0** policy violations, **TODO 0** allergen violations, versus a naive agent with direct API access that **TODO**." | [ROB] [GEM] |
| **2:50–3:00** | Click **Connect your agent** in the header (MCP config dialog), or open `/.well-known/otap.json`. | "It's all open: MCP, OpenAPI 3.1, a discovery manifest, JWS signatures, Peppol semantics. Swap the mock for SAP behind one interface, put OAuth in front, and this goes to production. Trusted Trade Agents." | [OPEN] |

### Lines to have ready for questions
- **"What if the agent lies about the tier?"** Today the quote line's tier and flags come from the agent (see "Issues for engineers" in `docs/reports/pitch.md`). TODO: answer once the gateway derives them server-side.
- **"Why not just give the agent an API key?"** An API key says *who*, not *what they may do*. The mandate plus policy says what, and the signed quote plus approval binding proves what was agreed.
- **"What about prompt injection in supplier data?"** Two layers: regex (0 false positives on 34,159 real supplier texts) and a Gemini classifier, and the buyer harness redacts flagged text before the model sees it. Flagged orders need a human.
- **"Is the order real?"** Valio Aimo has no order API in the mock, so we built a mock order backend behind the same interface a SAP adapter would implement.
- **"Why Gemini?"** Finnish/Swedish product data, embeddings in the same API, thinking levels so reasoning is only spent on the substitution decision, structured output everywhere.

## Robustness montage (optional 30 s, if time allows or for the video)

Pick one, rehearsed in advance:
1. **School allergen:** select **school-tampere**, request "We need 120 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies." Milk-containing and unknown-allergen products are rejected with the reason; any substitution needs the kitchen manager. Terminal equivalent: `uv run aimo demo --scenario school-allergen`.
2. **Prompt injection:** `uv run aimo demo --scenario injection`. Supplier text with hidden instructions is flagged and the order needs approval.
3. **Outage:** `uv run aimo demo --scenario stockout` (fault `partial:*:400`) or send header `X-Aimo-Faults: error_rate:1` to `/v1/offers` to show the 503 with `Retry-After`.

Run each scenario once during rehearsal and note how long it takes (happy path measured at 38 s; the buyer report has stockout at 36 s). **TODO: timings for the others.** Live tamper demo without the mock: `curl -X POST localhost:8000/v1/demo/tamper/3`, click **Verify chain** (red, "event #3 was modified after it was recorded"), then `curl -X POST localhost:8000/v1/demo/untamper/3` (verified Sat 15:00).

## Fallback plan

| Problem | Do this | Time cost |
|---|---|---|
| Gemini slow or failing | The agent falls back to the deterministic need parser; keep talking over the timeline. If the run stalls for > 20 s, switch to tab (b) mock mode. | 5 s |
| Mock API down or slow (`/healthz` ok but offers 503) | Switch to tab (b): `http://localhost:8000/ui/?mock=1` plays the identical story with no backend. The header shows **MOCK DATA · no backend**: say so honestly. Mock mode also has **Simulate tampering** to show the chain turning red. | 5 s |
| Gateway won't start / laptop problem | Mock UI without the gateway: `cd web && python -m http.server 8765`, open `http://127.0.0.1:8765/index.html?mock=1`. | 30 s |
| Browser or UI broken | Terminal: `uv run aimo demo --auto-approve-as anna@hotel-aurora.example` (Gemini agent, about 40 s), or without Gemini `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` (search → offers → plan → signed quote → pending approval → wrong-hash 409 → approve → AP → audit verify, about 3 s). | 10 s |
| Everything down | Play the pre-recorded video (Chintufp records it at the Sun 06:00 freeze; file path **TODO**). | 0 s |

## Recording the video (2–3 minutes)

Use the same script and do the approval click by hand (don't use `&autoapprove=1` for the video: the human click is the point). Useful URL helpers: `?customer=hotel-aurora`, `&autorun=1` (starts the run on load), `&tab=plan|approvals|audit`. Record at 1280×800, add captions for the three NEEDS APPROVAL reasons and the verify-chain result, and end on the openness slide. Upload as unlisted and paste the link into `docs/SUBMISSION.md`.
