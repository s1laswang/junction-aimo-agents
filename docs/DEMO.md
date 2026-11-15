# Live demo script (3 minutes)

Presenter: Silas (talks). Driver: Chintufp (clicks). Screen: browser at `http://localhost:8000/ui`, 1280×800 or larger, zoom 110%.
Judging weights in brackets: **[E2E 30]** end-to-end demo, **[NEED 20]** solves the user need, **[CTRL 20]** autonomy and controls with auditability, **[ROB 15]** robustness to exceptions, **[OPEN 15]** openness and production path. **[GEM]** marks the Gemini prize points.

UI labels below are from `web/app.js` on main (Sat 15:00). A full live run takes about 35–40 s, of which the agent reaches the approval wait after about 25 s (measured with `uv run aimo demo`, 38 s including approval).

## Before you go on stage (T-15 min)

1. **Fresh state.** The weekly-budget rule sums every order placed in the last 7 days, so rehearsal orders change the verdict. Start from a clean state DB, keeping the signing key and the approver credential:
   ```bash
   mkdir -p ~/aimo-demo-data && cd ~/aimo-demo-data
   for f in /PATH/TO/junction-aimo-agents/data/*; do case "$f" in *state.sqlite*) ;; *) ln -sf "$f" . ;; esac; done
   cd /PATH/TO/junction-aimo-agents && AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve
   ```
   When `AIMO_API_KEY` is set, the gateway automatically falls back by default (`replay.fallback_dir()`) to the bundled replay snapshot (`tests/fixtures/snapshot/replay/`: 669 product ids and a 669-product gzipped challenge catalog subset across `2026-11-16`..`2026-11-20`, recorded `2026-11-14T23:50:28+0200` via `scripts/record_replay.py` in 10 upstream calls; `tests/fixtures/snapshot` also holds 92 ids) if the live mock API stops responding (Wi-Fi drop), marking responses `served_from: replay`. With an empty `.env` (no `AIMO_API_KEY`, no `GEMINI_API_KEY`), `uv run aimo serve` (and keyless `Dockerfile` containers), `make accept`, `uv run python -m scenarios.run_all` (8/8 PASS, 92 checks), `conformance` (44/44), and `qa_milk_flow.py` automatically run off that bundled replay snapshot and 669-product catalog subset (`served_from: replay`, UI header pill `REPLAY · served_from: replay · recorded …`), with no downloaded `data/` needed.
2. **Approver PIN.** Read Anna's generated passphrase with `cat ~/aimo-demo-data/keys/approver_pin` (each directory approver gets a distinct 12-char passphrase in `keys/approver_pins.json`, overridable via `AIMO_APPROVER_PINS`; leave `AIMO_APPROVER_PIN` unset on the stage gateway unless you want a single-credential operator override across all approvers). Write Anna's passphrase on paper for the driver. The sign-in panel's PIN field is masked. Do not sign in during the warm-up in tab (a): the sign-in is a beat in the script.
3. **Check health:** `curl -s localhost:8000/healthz` must show every integration as `aimo.catalog` / `aimo.trust`, not `fallback`, `"replay": {"mode": ...}` (`fallback` when live, `auto` when keyless, `forced` when `AIMO_REPLAY` is set), and `"clock": {"mode": ...}` (`real`, `env` when `AIMO_NOW` is set, or `replay` anchored at `2026-11-14T23:50:28+0200`).
4. **Warm up once** (fills the 5-minute offers cache and the query-embedding cache): `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` must end in `MILK FLOW OK`. Then restart with a fresh state dir again (step 1) so the audit view starts clean.
5. **Cut-off clock.** The story depends on real mock-API data: 463038's cut-off passed Thu 12 Nov 15:00, and 464306 can be ordered for Monday until **Sun 15 Nov 17:00** (`closing_time` from `/v1/offers`). **Presenting after Sun 16:30:** start the gateway on a simulated clock: `AIMO_NOW=2026-11-15T10:00:00+02:00 AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve` (the clock runs forward from Sun 10:00; add `AIMO_NOW_FROZEN=1` to hold it). Verified Sat 23:15: with now = Sun 10:00, 464306 has no issues for Monday; with now = Sun 18:00 it shows `cutoff_passed`. Say once: "the gateway runs on a demo clock set to Sunday morning."
6. Open three tabs: (a) a **fresh** browser tab at `http://localhost:8000/ui/?customer=hotel-aurora` (the approver session lives in that tab's sessionStorage, so a fresh tab guarantees the sign-in moment when you click Run on stage), (b) `http://localhost:8000/ui/?mock=1` (backup), (c) a terminal with large font in the repo directory, with this line typed but not yet run:
   `uv run python docs/pitch/permissions_attack.py --base http://localhost:8000`
7. Phone on silent, laptop on power, Wi-Fi checked (the live mock API and Gemini are online services; see "Wi-Fi drops" in the fallback plan). **Gemini quota:** if `gemini-3.8-flash` is out of daily quota (it was at Sun 05:00; daily quota resets 10:00 Helsinki), `aimo.gemini.generate` falls back automatically across `GEMINI_FALLBACK_MODELS` (default `gemini-3.5-flash`, then `gemini-4-hydrogen`, skipping the exhausted model for 30 minutes and logging `meta.fallback_from`; verified live Sun 05:00 with a 29 s hero order on `gemini-3.5-flash`), and the UI agent steps show the answering model. The montage numbers we quote were measured on `gemini-3.8-flash`, so say so if the live run shows another model.
8. **T-4 min:** start the robustness montage in a second terminal against the second gateway on port 8031 (see "Robustness montage" below). Run it under `caffeinate -i` so the laptop cannot sleep mid-run (the buyer report lost a run that way).

## The script

| Time | Click (driver) | Say (presenter) | Criteria |
|---|---|---|---|
| **0:00–0:15** | Tab (a) is showing the Trusted Trade console. Nothing clicked yet. | "Hotels, schools and hospitals are starting to send AI agents to buy for them. Valio Aimo can't hand an agent its SAP login. We built the front door instead: an open gateway any company's agent can use to find, quote and order, inside rules the *customer* sets, with every step signed." | [NEED] |
| **0:15–0:30** | Customer selector shows **Hotel Aurora Helsinki**. Point at the mandate line ("Mandate hotel-aurora/buyer-1 under policy hotel-aurora"). Click **Run the example request**. **The sign-in moment:** the panel asks "Sign in to instruct the buying agent. The same sign-in covers the approval later." Pick **Anna Virtanen · Procurement Manager**, type the stage PIN (masked), **Sign in**; the run starts with "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday." | "Only a signed-in person can instruct our hosted agent (`POST /v1/agent/runs` requires `X-Approver-Session` or `X-Approver-Pin`, 401 otherwise, and ignores body `faults` and `inproc`/`fake` transports unless `AIMO_DEMO_FAULTS=1`), and agents can never sign in. Anna starts it; it runs on Gemini and turns the sentence into a structured need: lactose-free, 1,000 litres, Monday 16 November." | [CTRL] [E2E] [GEM] |
| **0:30–0:55** | Timeline fills: parsed-need chips, tool calls `plan_need`, `get_offers`. Point at the exception callouts: **cut-off passed** (463038) and **partial stock** (469363). | "Every step is a call to our MCP server, the same tools Claude or Cursor would use, against Valio Aimo's price and availability API. Two real exceptions: the hotel's usual 10-litre box missed its order cut-off on Thursday, and one alternative only has 384 units. The agent doesn't guess. It re-plans." | [ROB] [OPEN] |
| **0:55–1:15** | Click the **Plan** tab. Best = Valio Eila 1 L (464306), €1.01/L, 1,000 units, €1,010; better = Arla (partial stock); good = different fat %. Expand **Rejected** to show the cut-off reason. | "Good-better-best, each with a reason. Missing allergen data counts as *unknown*, never safe, and Gemini's reading of Finnish ingredient lists can only add a warning, never clear a product." | [NEED] [GEM] |
| **1:15–1:30** | Timeline shows **Awaiting approval** ("The agent has paused. Nothing is ordered until a human approves the exact signed quote."). Click **Open approval inbox →**. Point at the header pill **0 attacks blocked** (signed out, the inbox shows the **Approvals are human-only** card; here Anna's session from starting the run is already active). | "The agent has paused: this order is outside its mandate. Nothing gets ordered until a named person approves." | [CTRL] |
| **1:30–2:00** | **Permissions attack.** Switch to terminal tab (c) and press Enter on the prepared `permissions_attack.py` line. It prints the agent's 13 MCP tools ("Approval tools on MCP: none"), then the agent approving *as anna* with the correct hash → **HTTP 403 agent_cannot_approve**, then the audit line `approval.refused`. Switch back to tab (a): the header pill now says **1 attack blocked**. | "So can the agent just approve itself? It has no approve tool. Over REST it names the real manager, Anna, with the right hash: 403, and the attempt is written into the signed audit log. You can talk an agent into anything; it still can't sign for a human." | [CTRL] |
| **2:00–2:20** | **The human decision.** Anna is already signed in from starting the run (header chip; if signed out, click **Sign in as approver** on the human-only card). Point at **Needs approval by Procurement Manager** and the two NEEDS APPROVAL rows on the final policy (`7225d06`: spend > €500, 3.3× usual weekly volume; the pack swap row says allowed because the 1 L pack is the hotel's listed alternative; pre-`7225d06` captures show a third row for the pack swap), then "Deciding as Anna Virtanen" and "Your decision is bound to quote hash …". Click **Approve €1,010.00**. | "The agent could not approve; Anna can, because she signed in as herself. The hotel's own policy says two things need her: over the €500 limit, and 3.3 times their normal weekly milk. The pack swap is one the hotel listed in advance, so it passes on its own. Her session is bound to her alone, and she approves *this exact* signed quote: if the price changed, her approval would not apply." | [CTRL] [E2E] |
| **2:20–2:30** | Order response card: **Placed**, **AP** Accepted, 1,000 confirmed. | "Order placed, with a Peppol-style order response: accepted." | [E2E] [OPEN] |
| **2:30–2:45** | Click **View audit trail →**. Point at the refused agent attempt, then Anna's approval bound to the hash. Click **Verify chain** → green. (In replay or with `AIMO_NOW`, the report header says "simulated clock" and keeps the real wall time per event: say so if asked.) | "Afterwards anyone can check what happened, including the refused attempt: every event is hash-chained and signed, and verify recomputes all of it. The evidence pack lets an auditor check it offline." | [CTRL] |
| **2:45–2:55** | Switch to the montage terminal. Scroll to **== Montage verdicts ==**: six green **PASS** lines. | "Six more, run live by the same agent while we talked: a stock-out split, school allergies, prompt injection in supplier text, a standing order 13 times too big, a last-minute event. Six of six, about three and a half cents of Gemini per order. And across 640 Gemini-written customers: zero policy violations for the gateway, 372 for a naive agent with direct API access." | [ROB] [GEM] |
| **2:55–3:00** | Click **Connect your agent** in the header, or open `/.well-known/otap.json`. | "All open: MCP, OpenAPI, JWS, Peppol semantics. Swap the mock for SAP behind one interface and this goes to production. Trusted Trade Agents." | [OPEN] |

If the agent run is slow, drop the montage beat (2:45–2:55) first. Never drop the attack or the sign-in.

**Optional, if time or asked:** instead of the Approve click, use **Decide by voice**: the gateway reads the order out, Anna says "yes, approve it", the UI shows the transcript and Gemini's intent, and one confirm click sends the normal decision. Voice never decides on its own ([docs/reports/ui.md](reports/ui.md)).

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
- **"What about prompt injection in supplier data?"** The shipped regex screen covers supplier text, the buyer request (`need.raw_text`), and `need.product_query` (surfacing `injection_screen` on `PlanLine` and `Candidate`): 0 of 34,159 real supplier texts flagged, 80 of 80 scenario payloads flagged in `wt640`, and 44.4% recall on our separate harder 36-payload evaluation set; the opt-in Gemini layer (`AIMO_SCREEN_GEMINI=1`, off by default) reaches 94.4% recall on that 36-payload set but flags 42 of 60 normal buyer requests. The buyer harness redacts flagged text before the model sees it, flagged orders need a human, and nothing an injection says can approve an order.
- **"Is the order real?"** Valio Aimo has no order API in the mock, so we built a mock order backend behind the same interface a SAP adapter would implement.
- **"Why Gemini?"** Finnish/Swedish product data, embeddings in the same API, thinking levels so reasoning is only spent on the substitution decision, structured output everywhere.

### Price-change-during-approval and partial-stock beat (`409 requote_needed`)

1. **Inside the 15-minute lock:** even if upstream price moves (`-H "X-Aimo-Faults: price_shock:464306:1.25"`), approving before `expires_at` honours the signed fixed-price quote (€1,010.00).
2. **Stock drop during approval (`reason: partial_stock`):** if stock at approval time falls below the quoted quantity (e.g. `-H "X-Aimo-Faults: partial:464306:400"`), approval returns `409 requote_needed` (reason `partial_stock`) with a fresh signed quote for the human to approve instead of placing a partial order.
3. **After lock expiry + upstream price move:** advance the demo clock +20 min past `expires_at` and inject a +25% price move on `464306` (`X-Aimo-Now` and `X-Aimo-Faults` are operator-only: they are ignored on any request carrying agent identity over REST and MCP unless the operator starts the gateway with `AIMO_DEMO_FAULTS=1`, and never honoured with `AIMO_ENV=production`; the human decision calls below carry no agent identity):

```bash
# 1. Lock expired + price moved -> 409 requote_needed with old vs new diff + replacement signed quote & pending order
curl -s -X POST http://localhost:8000/v1/approvals/<ORDER_ID>/decision \
  -H "Content-Type: application/json" -H "X-Approver-Pin: $AIMO_APPROVER_PIN" \
  -H "X-Aimo-Faults: price_shock:464306:1.25" -H "X-Aimo-Now: <CREATED_AT_PLUS_20M_ISO>" \
  -d '{"decision":"approve","approver":"anna@hotel-aurora.example","quote_hash":"<OLD_QUOTE_HASH>"}'

# 2. One-step human approval of the new signed quote_hash -> placed (AP) + order.requoted in verified audit chain
curl -s -X POST http://localhost:8000/v1/approvals/<NEW_ORDER_ID>/decision \
  -H "Content-Type: application/json" -H "X-Approver-Pin: $AIMO_APPROVER_PIN" \
  -d '{"decision":"approve","approver":"anna@hotel-aurora.example","quote_hash":"<NEW_QUOTE_HASH>"}'
```

In `/ui`, the Approvals tab renders the old-vs-new line diff card automatically and lets Anna approve the replacement `quote_hash` in one click, while `GET /v1/audit/verify` confirms `order.requoted` in the intact chain. Runnable one-command script for the same beat: `uv run python docs/pitch/requote_beat.py --base http://localhost:8000`.

## Robustness montage (`uv run aimo demo --montage`)

`uv run aimo demo --montage` runs the six live scenarios in sequence with the Gemini agent over MCP against the live mock API and approves each pending order as the policy's named approver through the real decision endpoint with the human approver credential (drift is left held for confirmation by design; an order the policy auto-approves needs no one). It prints one verdict per scenario plus Gemini calls and tokens, and writes a results table.

Measured result to quote ([demo-montage.md](demo-montage.md), Sun 15 Nov 02:54 final-code re-recording): **6/6 PASS**, 20–37 s per scenario, 174 s wall clock in total, 53 Gemini calls, **$0.0352 per placed order** (down from $0.0822 before the token trim, −57%; 38% of input served from Gemini's implicit cache; escalation to gemini-4-hydrogen HIGH only on the quote-decision turn, 5 of 34 agent steps, 15%; earlier Sat 14 Nov 18:21 after-trim ledger reproduced by `docs/evidence/montage_stats.py`: 50 calls, 22–37 s, 176 s total, $0.0339 per placed order, −59%, 31% cached, 6 of 31 steps, 19%).

| Scenario | What the audience sees in the verdict line |
|---|---|
| happy | placed, EUR 1,010.00, approved by anna@hotel-aurora.example |
| stockout | placed, split into 4 lines, EUR 2,544.80, shortfall 0 |
| school-allergen | placed, Oatly Oat Drink Whole 1L, 2 candidates rejected, approved by policy:school-tampere (EUR 280, under the school's €300 limit) |
| injection | placed, 6 supplier texts flagged (regex screen) and redacted, policy cites injection, largest line 300 units |
| drift | needs_confirmation, EUR 4,040.00 quote prepared, place_order held by the agent |
| event | placed, 2 lines (Valio whipping cream 1.75 l and Valio Eila butter 500 g), EUR 266.49 |

How to run it for the pitch (it takes about 3 minutes, so start it before going on stage):
1. Run it against a **second gateway with its own state**, so its five placed orders do not count towards the hotel's weekly budget in the hero demo:
   ```bash
   AIMO_DEMO_FAULTS=1 AIMO_DATA_DIR=~/aimo-montage-data uv run aimo serve --port 8031   # data dir prepared like step 1 of the checklist
   # AIMO_DEMO_FAULTS=1: the montage injects stock-outs and supplier-text attacks as the agent on purpose. Without it,
   # X-Aimo-Now and X-Aimo-Faults are ignored on any request carrying agent identity (REST and MCP), and never honoured with AIMO_ENV=production.
   uv run aimo demo --montage --base-url http://localhost:8031 --montage-out /tmp/montage.md
   ```
   `--montage-out` keeps the committed `docs/demo-montage.md` unchanged.
2. Start it about 4 minutes before the slot, in terminal tab (c) with a large font, and leave the verdicts on screen.
3. If it fails or the Wi-Fi is down, show [docs/demo-montage.md](demo-montage.md) (the measured run) and say it is the recorded result.

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
| Gemini slow, quota exhausted, failing, or no `GEMINI_API_KEY` | When a Gemini model hits its daily quota or is rate-limited twice, `aimo.gemini.generate` falls back automatically across `GEMINI_FALLBACK_MODELS` (`gemini-3.5-flash`, then `gemini-4-hydrogen`, 30-min skip, `meta.fallback_from`). Without `GEMINI_API_KEY`, `uv run aimo demo` finishes end-to-end via the deterministic regex need parser and scripted buyer fallback over the same MCP tools. In `/ui`, the agent falls back to the deterministic need parser; keep talking over the timeline, or if the run stalls for > 20 s, switch to tab (b) mock mode. | 5 s |
| **Wi-Fi drops** (mock API unreachable) | Live mode automatically falls back to the bundled `tests/fixtures/snapshot/replay` snapshot (669 product ids across `2026-11-16`..`2026-11-20`) when the live upstream fails after retries, marking responses `served_from: replay` (the UI header shows a `REPLAY` pill). To be fully offline from the start, restart with `AIMO_REPLAY=1 AIMO_DATA_DIR=~/aimo-demo-data uv run aimo serve` (or with an empty `.env` / unset `AIMO_API_KEY`, which activates `tests/fixtures/snapshot/replay` plus its 669-product catalog subset automatically and anchors the clock at `2026-11-14T23:50:28+0200` so Monday `2026-11-16` is orderable). Without network for Gemini, `uv run aimo demo` uses the deterministic scripted buyer fallback, or run `qa_milk_flow.py` or mock mode. Say: "we're on recorded mock-API responses now." | 20 s |
| Mock API down or slow (`/healthz` ok but offers 503) | Switch to tab (b): `http://localhost:8000/ui/?mock=1` plays the identical story with no backend. The header shows **MOCK DATA · no backend**: say so honestly. Mock mode also has **Simulate tampering** to show the chain turning red. | 5 s |
| Gateway won't start / laptop problem | Mock UI without the gateway: `cd web && python -m http.server 8765`, open `http://127.0.0.1:8765/index.html?mock=1`. | 30 s |
| Browser or UI broken | Terminal: `uv run aimo demo --auto-approve-as anna@hotel-aurora.example` (uses Gemini in about 30–40 s when keyed, or finishes end-to-end via the deterministic scripted buyer fallback when keyless), or `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` (search → offers → plan → signed quote → pending approval → wrong-hash 409 → approve → AP → audit verify, about 3 s). | 10 s |
| Delivery cut-off passed (live demo after Sun 15 Nov 17:00 Helsinki: product 464306 is no longer orderable for Monday; 463038's cut-off already passed Thu 12 Nov 15:00) | Prevent it: start the gateway with `AIMO_NOW=2026-11-15T10:00:00+02:00` (checklist step 5) or in replay mode. If you only notice on stage, run the story in mock mode: `http://localhost:8000/ui/?mock=1` (or, with no gateway, `cd web && python -m http.server 8765` → `http://127.0.0.1:8765/index.html?mock=1`). Say: "The real delivery cut-off for Monday has passed, so this is the identical flow on recorded data." That is itself the cut-off handling story. | 5 s |
| Everything down | Play [docs/pitch/demo-walkthrough.mp4](pitch/demo-walkthrough.mp4) (`https://github.com/s1laswang/junction-aimo-agents/raw/aimo-v1/docs/pitch/demo-walkthrough.mp4`, 110 s narrated walkthrough built via `bash docs/pitch/build_walkthrough.sh`) or the committed headless Playwright backup video [docs/demo-backup.mp4](demo-backup.mp4) (reproducible via `uv run python scripts/record_demo_video.py`, also linked in [SUBMISSION.md](SUBMISSION.md); raw offline-verifiable live Gemini run in [docs/evidence/live-run/README.md](evidence/live-run/README.md)). | 0 s |

## Recording the video (2–3 minutes)

Committed backup videos: [docs/pitch/demo-walkthrough.mp4](pitch/demo-walkthrough.mp4) (`https://github.com/s1laswang/junction-aimo-agents/raw/aimo-v1/docs/pitch/demo-walkthrough.mp4`, 110 s narrated walkthrough built from screens of the running system via `bash docs/pitch/build_walkthrough.sh`) and [docs/demo-backup.mp4](demo-backup.mp4) (headless Playwright walkthrough of the request, signed quote, 403 permissions attack, 409 price-change requote, human approval, Peppol AP order, and verified audit chain; reproducible via `uv run python scripts/record_demo_video.py`), plus offline-verifiable live Gemini run artifacts in [docs/evidence/live-run/README.md](evidence/live-run/README.md). For a live narrated stage capture, use the same script, including the permissions attack and the sign-in, and do the approval click by hand (don't use `&autoapprove=1` for the video: the human click is the point). Useful URL helpers: `?customer=hotel-aurora`, `&autorun=1` (starts the run on load), `&tab=plan|approvals|audit`. Record at 1280x800, add captions for the NEEDS APPROVAL reasons (two on the final policy `7225d06`, three on earlier captures) and the verify-chain result, and end on the openness slide (linked in [docs/SUBMISSION.md](SUBMISSION.md)).
