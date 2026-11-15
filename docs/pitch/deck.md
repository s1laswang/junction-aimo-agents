# Deck outline: Trusted Trade Agents (8 slides)

For Chintufp to build the visual deck and record the video. Every number has a source in brackets ([docs/NUMBERS.md](../NUMBERS.md)). Hard questions and answers: [QA.md](QA.md). Keep slides sparse: one idea, one visual, at most 15 words of text. The live demo ([docs/DEMO.md](../DEMO.md)) replaces slide 3 when presenting live; the video uses the screen recording there.

Visual style suggestion: white background, Valio-ish deep blue (#1F4E9C) for the gateway, green for "pass", amber for "needs approval", red for "reject" (the same colours as the UI). Screenshots and video: `docs/reports/frontend/s5-approval-signed-in.png` (live S5 UI with sign-in and 3 approval rules), `docs/reports/frontend/s5-requote-diff.png` (409 requote diff card), `docs/pitch/img/approval-ui.png`, narrated walkthrough video `docs/pitch/demo-walkthrough.mp4` (111 s, 1280x720 H.264+AAC), and headless Playwright backup capture [docs/demo-backup.mp4](../demo-backup.mp4).

---

## Slide 1: Hook
**On slide:** "1,000 litres of lactose-free milk by Monday." Below it, smaller: *sent by an AI agent, not a person.*
**Visual:** a chat bubble from "Hotel Aurora buyer agent" to "Valio Aimo".
**Speaker notes (15 s):** "On Friday a hotel's AI agent sends this to its wholesaler. The usual product can't make it in time. Should the agent pick a substitute? Spend €1,010? Who checks? Today nobody can answer that safely. We built the answer."

## Slide 2: Problem
**On slide:** three icons with one line each:
- *Agents need access*: lookup, prices, stock, ordering.
- *Wholesalers can't expose ERP* or confidential data.
- *Customers need control*: limits, allergens, approvals, proof.
**Speaker notes (20 s):** "Every big foodservice customer will soon have purchasing agents. Valio Aimo can't hand them SAP credentials, and a hotel can't let an agent spend without limits. A school can't let an agent guess about milk allergies. What's missing is a trusted, open interface between the two."

## Slide 3: Solution (demo)
**On slide:** "Trusted Trade gateway" in the middle; left: "Any agent: Gemini, Claude, ERP" (13 MCP tools / OpenAPI 3.1, 0 approval tools); right: "Valio Aimo backend"; bottom: "Human approver". The milk flow as 6 steps: request → live lookup → good-better-best → signed quote → policy: needs approval → approved, placed (AP), audited.
**Visual:** screenshot `docs/reports/frontend/s5-approval-signed-in.png` (or `docs/pitch/img/approval-ui.png`) or the walkthrough clip (`docs/pitch/demo-walkthrough.mp4`; headless Playwright backup capture at [docs/demo-backup.mp4](../demo-backup.mp4)).
**Numbers:** €1,010 for 1,000 × Valio Eila 1 L at €1.01 [live run, docs/ARCHITECTURE.md §2]; usual 10 L box missed its cut-off Thu 12 Nov 15:00 [mock API]; 2 rules needed a human on the final policy: spend > €500 and 3.3× usual weekly volume (the 1 L pack is the hotel's listed alternative, so the pack swap is allowed; screenshots captured before Sun 05:50 show 3 rules including outside preference) [docs/NUMBERS.md, final policy].
**Speaker notes (40 s):** walk the six steps. Stress: live mock-API data (synthetic, as the challenge provides), the agent re-planned around two real exceptions, the policy is the hotel's own file, the human approved the exact signed quote.

## Slide 4: Trust layer
**On slide:** four boxes in a row:
1. **Mandate**: who may act (scopes).
2. **Policy**: what runs alone (customer-owned YAML, strictest rule wins).
3. **Signed quote + human-only approval bound to its hash** (Ed25519 JWS, 15-min price lock; per-approver credentials in `data/keys/approver_pins.json` → signed session, agents get 403, and hosted `/v1/agent/runs` requires a signed-in human too; expired lock or partial stock while waiting → 409 `requote_needed`, never a partial order; `X-Aimo-Faults`, `X-Aimo-Now` and run-body `faults` operator-only via `AIMO_DEMO_FAULTS=1`).
4. **Hash-chained, signed audit**: readable, verifiable offline.
Bottom line: *Unknown is never safe. AI can add warnings, never remove them.*
**Numbers:** 3 demo customers with different rules (hotel €500 auto limit; school blocks milk and nuts and unknown allergens; hospital needs a dietitian for any substitution) [docs/TRUST.md §3].
**Speaker notes (30 s):** "An API key tells you who. Our mandate and policy tell you what they may do. The signature proves what was offered, the hash binding proves what the human approved, and the chain proves nothing was changed afterwards. Gemini reads allergens from Finnish ingredient text, but its reading can only block a product, never clear one."

## Slide 5: Robustness numbers
**On slide:** headline **"6 of 6 live scenarios pass"** with six small tiles: happy (approved by Anna, placed) · stock-out (split into 4 lines) · school allergen (milk rejected, oat drink auto-approved by the school's policy) · prompt injection (6 texts flagged and redacted) · standing-order drift (held for a human) · last-minute event (2 items, one order). Caption: *Gemini agent, live mock API, 20–37 s each, re-recorded on the final code* [docs/demo-montage.md, Sun 02:54].
Second half (or a build): wind tunnel table, "Trusted gateway vs naive agent with direct API access":
| Metric | Ours | Naive |
|---|---|---|
| Scenarios (Gemini-generated hotels, schools, hospitals, events) | 640 | same |
| Correct escalations (recall) | 99.7% | 17.8% |
| Policy violations | **0** | 372 |
| Allergen violations | **0** | 193 |
| Injection payloads blocked | 80 of 80 | 0 of 80 |
Source: `docs/windtunnel-results.md` § "Gemini-written scenarios (wt640)" (merged into main; run on git dfecb2a, snapshot `2f2fef829c50`). Both columns are the final-code re-score (main `a99692f` / final policy Sun 15 Nov 05:50; 80 of 80 injection payloads blocked across supplier text and buyer request / `product_query` with 1 false positive in 560 clean requests, up from 56.2% vs 0% before the S3 and final-code screening updates) from committed inputs: `docs/evidence/windtunnel/README.md`. Say with the 80 of 80 injection row: the shipped gateway runs the regex screen by default (44.4% recall on our harder 36-payload evaluation set, 0 false positives on 34,159 supplier texts, 80 of 80 wind-tunnel scenario payloads flagged); layer 2 is opt-in via `AIMO_SCREEN_GEMINI=1` (94.4% recall on the 36-payload set, 0/60 false positives on supplier texts, 42/60 on buyer requests, off by default), and a flag only sends the order to a human [same file]. Honest caveat for Q&A: escalation precision is 68.2% after S3 and 69.5% on the final policy (145 extra escalations, up from 54.0%; the naive agent's 90.8% comes from almost never escalating), so the gateway still asks a human more often than strictly needed; the 90% target is not met. Scale run on template text: 2,560 scenarios, 11 vs 1,452 policy violations, 0 vs 728 allergen violations [same file, § wt2560].
Footnote facts already measured: 0 false positives on 34,159 catalog supplier texts [docs/TRUST.md]; 1,150+ passed keyless offline across 1,257+ collected tests in `fr8/valio` (`371 passed, 7 skipped` keyed / `270 passed` keyless on standalone `origin/main` `4ccc1fb`, `362 passed, 7 skipped` / `261 passed` on `d4b0b41`; 743 passed with full catalog JSON on `2468bdd`; 8/8 scenarios, 88 checks; 44/44 conformance on two wholesalers) [[docs/NUMBERS.md](../NUMBERS.md), [docs/OPENNESS.md](../OPENNESS.md)]; an agent that tries to approve as the real manager gets 403 and the refusal is audited [docs/pitch/permissions_attack.py].
**Speaker notes (25 s):** "We didn't test one happy path. Six live scenarios, end to end with the Gemini agent against the live challenge mock API: stock-out, school allergens, prompt injection, standing-order drift, a last-minute event. All six pass. And our wind tunnel replays recorded API data across 640 Gemini-written customers, scored against an independent oracle: zero policy violations for us, 372 for a naive agent with direct API access, zero allergen violations, and 80 of 80 injection payloads blocked."

## Slide 6: Openness and production path
**On slide:** left column "Open today": MCP (13 tools: 9 core `otap/0.1` + 4 extensions, 0 approval tools) · OpenAPI 3.1 · otap.json discovery · JWS/JWKS · GS1 GTIN · Peppol AP/CA/RE · EU14 allergens (44/44 conformance across two wholesalers). Right column "Production next": SAP adapter behind one interface · OAuth 2.1 (MCP auth spec) · mandates as W3C Verifiable Credentials · KMS keys · Peppol BIS Ordering · WORM audit store.
**Also on slide (small):** "Every assumption, decision, openness report and number is written down": [docs/OPENNESS.md](../OPENNESS.md) · [docs/CONNECT.md](../CONNECT.md) · [docs/ASSUMPTIONS.md](../ASSUMPTIONS.md) · [docs/DECISIONS.md](../DECISIONS.md) · [docs/NUMBERS.md](../NUMBERS.md).
**Speaker notes (20 s):** "Nothing here is proprietary. Any agent connects with a three-line MCP config. To go live, Valio swaps the mock for SAP behind one interface we already have, puts OAuth in front, and moves the key into a KMS." [docs/ARCHITECTURE.md §4]

## Slide 7: Gemini
**On slide:** "Gemini on both sides of the gateway" with 6 small tiles: buyer agent (function calling over 13 MCP tools) · need parsing (structured output) · hybrid search over 17,546 products (gemini-embedding-2) · allergen inference for 4,076 products from Finnish ingredient text (can only block) · injection screen layer 2 · wind tunnel customers and judge.
**Numbers:** **$0.0352 per order** in Gemini calls on the Sunday final-code montage ($0.0339 after the Saturday token trim, down 57%–59% from $0.0822 before the trim; 53 calls, 38% of input from Gemini's implicit cache on Sunday and 31% on Saturday; hero run 87k → 27k input tokens after the trim, 30.5k on Sunday) [docs/demo-montage.md, `docs/evidence/`]. Escalation to gemini-4-hydrogen (thinking HIGH) on only the quote-decision turn: 5 of 34 agent steps (15%) on Sunday, 6 of 31 (19%) on Saturday [same]; automatic daily-quota fallback via `GEMINI_FALLBACK_MODELS` (`gemini-3.5-flash` → `gemini-4-hydrogen`, 29 s hero order on Sun 05:00) [docs/GEMINI.md §4]. Team usage: 4,255 Gemini calls, 14.2M input tokens [docs/GEMINI.md §5, Sat 23:10 snapshot]. Cost numbers recompute offline from `docs/evidence/`. Embedding the whole catalog took 44 s, allergen inference 49 s [catalog report].
**Speaker notes (20 s):** "Gemini reads Finnish, embeds the catalog, runs the buyer, screens for injection and plays thousands of customers. We use thinking levels as a policy knob: deep reasoning only on the one decision a human later has to defend, 15% of the steps. And a whole order costs about three and a half cents in Gemini calls."

## Slide 8: Team and ask
**On slide:** team names and roles; the ask: *"Pilot with one Valio Aimo customer segment: schools or hotels."* Repo link and QR code: https://github.com/s1laswang/junction-aimo-agents (tag `aimo-v1`).
**Speaker notes (15 s):** "We're Silas, Momin, Ivar and Chintufp, with a team of Claude Code agents. Our ask: let us pilot this with one customer segment, behind your real webshop API, with the trust layer exactly as you've seen it."

---

## Timing for a 3-minute pitch with live demo
Slide 1 (0:15) → Slide 2 (0:20) → live demo instead of slide 3 (1:30, docs/DEMO.md 0:20–2:35 condensed) → Slide 5 (0:20) → Slide 6 (0:15) → Slide 7 (0:10, or skip if over time and cover Gemini during the demo) → Slide 8 (0:10).

## Video (2–3 minutes)
Slides 1–2 as title cards (20 s), screen recording of the demo (90–110 s, follow docs/DEMO.md; narrated walkthrough video `docs/pitch/demo-walkthrough.mp4`, 111 s 1280x720 H.264+AAC via `docs/pitch/build_walkthrough.sh`, plus headless Playwright backup capture at [docs/demo-backup.mp4](../demo-backup.mp4), reproducible via `uv run python scripts/record_demo_video.py`), slides 4–7 (40 s), slide 8 (10 s). Captions for the approval reasons and "Chain verified".
