# Live demo script (3 minutes)

Presenter: Silas (talks). Driver: Chintufp (clicks). Screen: browser at `http://localhost:8000/ui`, 1280×800 or larger, zoom 110%.
Judging weights in brackets: **[E2E 30]** end-to-end demo, **[NEED 20]** solves the user need, **[CTRL 20]** autonomy and controls with auditability, **[ROB 15]** robustness to exceptions, **[OPEN 15]** openness and production path. **[GEM]** marks the Gemini prize points.

UI labels below are from `web/app.js` on main (Sat 15:00). A full live run takes about 35–40 s, of which the agent reaches the approval wait after about 25 s (measured with `uv run aimo demo`, 38 s including approval).

## Before you go on stage (T-15 min)

1. **Fresh state.** The weekly-budget rule sums every order placed in the last 7 days, so rehearsal orders change the verdict. Start from a clean state DB, keeping the signing key and the approver credential:
   ```bash
   mkdir -p ~/aimo-demo-data && cd ~/aimo-demo-data
   for f in /PATH/TO/junction-aimo-agents/data/*; do case "$f" in *state.sqlite*) ;; *) ln -sf "$f" . ;; esac; done
   cd /PATH/TO/junction-aimo-agents && AIMO_REPLAY_FALLBACK=tests/fixtures/snapshot AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve
   ```
   `AIMO_REPLAY_FALLBACK` makes the gateway answer from the recorded snapshot if the live mock API stops responding (Wi-Fi drop). Without it there is no automatic fallback on this laptop, because `data/windtunnel/snapshot` does not exist.
2. **Approver PIN.** Set a stage-only `AIMO_APPROVER_PIN` (12+ characters) in `.env` before starting the gateway, or read the generated one with `cat ~/aimo-demo-data/keys/approver_pin`. Write it on paper for the driver. The sign-in panel's PIN field is masked. Do not sign in during the warm-up in tab (a): the sign-in is a beat in the script.
3. **Check health:** `curl -s localhost:8000/healthz` must show every integration as `aimo.catalog` / `aimo.trust`, not `fallback`, and `"clock": {"mode": ...}` must be what you expect (`real`, `env` when `AIMO_NOW` is set, or `replay`).
4. **Warm up once** (fills the 5-minute offers cache and the query-embedding cache): `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` must end in `MILK FLOW OK`. Then restart with a fresh state dir again (step 1) so the audit view starts clean.
5. **Cut-off clock.** The story depends on real mock-API data: 463038's cut-off passed Thu 12 Nov 15:00, and 464306 can be ordered for Monday until **Sun 15 Nov 17:00** (`closing_time` from `/v1/offers`). **Presenting after Sun 16:30:** start the gateway on a simulated clock: `AIMO_NOW=2026-11-15T10:00:00+02:00 AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve` (the clock runs forward from Sun 10:00; add `AIMO_NOW_FROZEN=1` to hold it). Verified Sat 23:15: with now = Sun 10:00, 464306 has no issues for Monday; with now = Sun 18:00 it shows `cutoff_passed`. Say once: "the gateway runs on a demo clock set to Sunday morning."
6. Open three tabs: (a) a **fresh** browser tab at `http://localhost:8000/ui/?customer=hotel-aurora` (the approver session lives in that tab's sessionStorage, so a fresh tab guarantees the sign-in moment on stage), (b) `http://localhost:8000/ui/?mock=1` (backup), (c) a terminal with large font in the repo directory, with this line typed but not yet run:
   `uv run python docs/pitch/permissions_attack.py --base http://localhost:8000`
7. Phone on silent, laptop on power, Wi-Fi checked (the live mock API and Gemini are online services; see "Wi-Fi drops" in the fallback plan).
8. **T-4 min:** start the robustness montage in a second terminal against the second gateway on port 8031 (see "Robustness montage" below). Run it under `caffeinate -i` so the laptop cannot sleep mid-run (the buyer report lost a run that way).

## The script

| Time | Click (driver) | Say (presenter) | Criteria |
|---|---|---|---|
| **0:00–0:15** | Tab (a) is showing the Trusted Trade console. Nothing clicked yet. | "Hotels, schools and hospitals are starting to send AI agents to buy for them. Valio Aimo can't hand an agent its SAP login. We built the front door instead: an open gateway any company's agent can use to find, quote and order, inside rules the *customer* sets, with every step signed." | [NEED] |
| **0:15–0:30** | Customer selector shows **Hotel Aurora Helsinki**. Point at the mandate line ("Mandate hotel-aurora/buyer-1 under policy hotel-aurora"). Check the model selector shows **Gemini 3.8 Flash**. Click **Run the example request** (it starts the run at once with "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."). | "This is the hotel's buyer agent, running on Gemini. It turns the sentence into a structured need: lactose-free, 1,000 litres, Monday 16 November." | [E2E] [GEM] |
| **0:30–0:55** | Timeline fills: parsed-need chips, tool calls `plan_need`, `get_offers`. Point at the exception callouts: **cut-off passed** (463038) and **partial stock** (469363). | "Every step is a call to our MCP server, the same tools Claude or Cursor would use, against Valio Aimo's price and availability API. Two real exceptions: the hotel's usual 10-litre box missed its order cut-off on Thursday, and one alternative only has 384 units. The agent doesn't guess. It re-plans." | [ROB] [OPEN] |
| **0:55–1:15** | Click the **Plan** tab. Best = Valio Eila 1 L (464306), €1.01/L, 1,000 units, €1,010; better = Arla (partial stock); good = different fat %. Expand **Rejected** to show the cut-off reason. | "Good-better-best, each with a reason. Missing allergen data counts as *unknown*, never safe, and Gemini's reading of Finnish ingredient lists can only add a warning, never clear a product." | [NEED] [GEM] |
| **1:15–1:30** | Timeline shows **Awaiting approval** ("The agent has paused. Nothing is ordered until a human approves the exact signed quote."). Click **Open approval inbox →**. Signed out, the inbox shows the **Approvals are human-only** card. Point at the header pill **0 attacks blocked**. | "The agent has paused: this order is outside its mandate. Nothing gets ordered until a named person approves." | [CTRL] |
| **1:30–2:00** | **Permissions attack.** Switch to terminal tab (c) and press Enter on the prepared `permissions_attack.py` line. It prints the agent's 13 MCP tools ("Approval tools on MCP: none"), then the agent approving *as anna* with the correct hash → **HTTP 403 agent_cannot_approve**, then the audit line `approval.refused`. Switch back to tab (a): the header pill now says **1 attack blocked**. | "So can the agent just approve itself? It has no approve tool. Over REST it names the real manager, Anna, with the right hash: 403, and the attempt is written into the signed audit log. You can talk an agent into anything; it still can't sign for a human." | [CTRL] |
| **2:00–2:20** | **The sign-in moment.** Click **Sign in as approver** on the human-only card. The panel preselects **Anna Virtanen · Procurement Manager**; type the stage PIN (masked), **Sign in**. The header shows Anna signed in. Point at **Needs approval by Procurement Manager** and the three NEEDS APPROVAL rows (spend > €500, substitution outside standing preferences, 3.3× usual weekly volume), then "Deciding as Anna Virtanen" and "Your decision is bound to quote hash …". Click **Approve €1,010.00**. | "A human signs in; agents never can. The hotel's own policy says three things need her: over the €500 limit, a pack outside their usual preference, 3.3 times their normal weekly milk. Her session is bound to her alone, and she approves *this exact* signed quote: if the price changed, her approval would not apply." | [CTRL] [E2E] |
| **2:20–2:30** | Order response card: **Placed**, **AP** Accepted, 1,000 confirmed. | "Order placed, with a Peppol-style order response: accepted." | [E2E] [OPEN] |
| **2:30–2:45** | Click **View audit trail →**. Point at the refused agent attempt, then Anna's approval bound to the hash. Click **Verify chain** → green. (In replay or with `AIMO_NOW`, the report header says "simulated clock" and keeps the real wall time per event: say so if asked.) | "Afterwards anyone can check what happened, including the refused attempt: every event is hash-chained and signed, and verify recomputes all of it. The evidence pack lets an auditor check it offline." | [CTRL] |
| **2:45–2:55** | Switch to the montage terminal. Scroll to **== Montage verdicts ==**: six green **PASS** lines. | "Six more, run live by the same agent while we talked: a stock-out split, school allergies, prompt injection in supplier text, a standing order 13 times too big, a last-minute event. Six of six, about three cents of Gemini per order. And across 640 Gemini-written customers: zero policy violations for the gateway, 372 for a naive agent with direct API access." | [ROB] [GEM] |
| **2:55–3:00** | Click **Connect your agent** in the header, or open `/.well-known/otap.json`. | "All open: MCP, OpenAPI, JWS, Peppol semantics. Swap the mock for SAP behind one interface and this goes to production. Trusted Trade Agents." | [OPEN] |

If the agent run is slow, drop the montage beat (2:45–2:55) first. Never drop the attack or the sign-in.

**Optional, if time or asked:** instead of the Approve click, use **Decide by voice**: the gateway reads the order out, Anna says "yes, approve it", the UI shows the transcript and Gemini's intent, and one confirm click sends the normal decision. Voice never decides on its own (docs/reports/ui.md).

## Permissions attack (30 s, `docs/pitch/permissions_attack.py`)

Run it while an order is **pending** (between "Awaiting approval" and Anna's click). Acting only as the agent (`X-Customer-Id`, `X-Agent-Id`, no approver credential), it:
1. lists the agent's MCP tools (13; none can approve);
2. fetches the customer's newest pending order and its exact quote hash (an agent may see its own customer's orders);
3. POSTs `{"decision": "approve", "approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager", "quote_hash": <correct>}`;
4. prints the answer: **HTTP 403 `agent_cannot_approve`**;
5. prints the order's audit line `agent:buyer-1 tried to decide order ORD-…; refused: agent identity on the approval endpoint. #N approval.refused` and the chain verify result.

It exits 0 only if the attempt was refused and audited. Verified Sat 14 Nov 23:14 against a replay gateway (about 3 s). The order stays pending, so Anna's approval in the next beat still works. A signed-in session that tries to decide as someone else gets 403 `approver_mismatch`.

### Lines to have ready for questions
- **"What if the agent lies about the tier?"** It can't help itself: the gateway recomputes tier and substitution flags server-side and the policy re-verifies them. A substitute sent as "preferred" still comes back as "better, outside preference" and needs approval (verified Sat 18:26).
- **"Can the agent approve its own order through REST?"** No. Decisions need a signed-in human (an approver session or the PIN, which no route or tool returns); any request carrying agent identity gets 403 `agent_cannot_approve` and is audited as `approval.refused`. That is the permissions-attack beat. More answers: [pitch/QA.md](pitch/QA.md).
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

## Tamper demo (15 s, optional or for Q&A)

The tamper routes are demo-only (disabled with `AIMO_ENV=production`) and need a signed-in approver, so an agent cannot use them either:
```bash
P=$(cat ~/aimo-demo-data/keys/approver_pin)          # or your AIMO_APPROVER_PIN
curl -s -X POST localhost:8000/v1/demo/tamper/3 -H "X-Approver-Pin: $P"      # rewrites event #3's summary
# click Verify chain in the UI: red, "event #3 was modified after it was recorded (hash mismatch)"
curl -s -X POST localhost:8000/v1/demo/untamper/3 -H "X-Approver-Pin: $P"    # restores it; verify is green again
```
Without a credential both routes return 401. Verified Sat 14 Nov 23:13 with an approver session (`X-Approver-Session`; the PIN header is accepted the same way). Mock mode (`?mock=1`) also has a **Simulate tampering** button.

## Fallback plan

| Problem | Do this | Time cost |
|---|---|---|
| Gemini slow or failing | The agent falls back to the deterministic need parser; keep talking over the timeline. If the run stalls for > 20 s, switch to tab (b) mock mode. | 5 s |
| **Wi-Fi drops** (mock API unreachable) | If the gateway was started with `AIMO_REPLAY_FALLBACK=tests/fixtures/snapshot` (checklist step 1), prices and stock keep working: when the live upstream fails after retries, it serves the recorded snapshot and marks responses `served_from: replay`. To be fully offline from the start, restart with `AIMO_REPLAY=tests/fixtures/snapshot AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve` (the clock is anchored at the recording, Sat 14 Nov 14:20, so Monday is still orderable). The Gemini agent still needs the network: without it, run `qa_milk_flow.py` (below) or mock mode. Say: "we're on recorded mock-API responses now." | 20 s |
| Mock API down or slow (`/healthz` ok but offers 503) | Switch to tab (b): `http://localhost:8000/ui/?mock=1` plays the identical story with no backend. The header shows **MOCK DATA · no backend**: say so honestly. Mock mode also has **Simulate tampering** to show the chain turning red. | 5 s |
| Gateway won't start / laptop problem | Mock UI without the gateway: `cd web && python -m http.server 8765`, open `http://127.0.0.1:8765/index.html?mock=1`. | 30 s |
| Browser or UI broken | Terminal: `uv run aimo demo --auto-approve-as anna@hotel-aurora.example` (Gemini agent, about 30–40 s), or without Gemini `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` (search → offers → plan → signed quote → pending approval → wrong-hash 409 → approve → AP → audit verify, about 3 s). | 10 s |
| Delivery cut-off passed (live demo after Sun 15 Nov 17:00 Helsinki: product 464306 is no longer orderable for Monday; 463038's cut-off already passed Thu 12 Nov 15:00) | Prevent it: start the gateway with `AIMO_NOW=2026-11-15T10:00:00+02:00` (checklist step 5) or in replay mode. If you only notice on stage, run the story in mock mode: `http://localhost:8000/ui/?mock=1` (or, with no gateway, `cd web && python -m http.server 8765` → `http://127.0.0.1:8765/index.html?mock=1`). Say: "The real delivery cut-off for Monday has passed, so this is the identical flow on recorded data." That is itself the cut-off handling story. | 5 s |
| Everything down | Play the pre-recorded video: the unlisted URL in the "Demo video" row of [SUBMISSION.md](SUBMISSION.md) (filled at the freeze), plus a local copy on the presenter laptop's desktop as `aimo-demo.mp4`. | 0 s |

## Recording the video (2–3 minutes)

Use the same script, including the permissions attack and the sign-in, and do the approval click by hand (don't use `&autoapprove=1` for the video: the human click is the point). Useful URL helpers: `?customer=hotel-aurora`, `&autorun=1` (starts the run on load), `&tab=plan|approvals|audit`. Record at 1280×800, add captions for the three NEEDS APPROVAL reasons and the verify-chain result, and end on the openness slide. Upload as unlisted and paste the link into `docs/SUBMISSION.md`.
