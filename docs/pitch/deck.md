# Deck outline: Trusted Trade Agents (8 slides)

For Chintufp to build the visual deck and record the video. Every number has a source in brackets ([docs/NUMBERS.md](../NUMBERS.md)). Hard questions and answers: [QA.md](QA.md). Keep slides sparse: one idea, one visual, at most 15 words of text. The live demo ([docs/DEMO.md](../DEMO.md)) replaces slide 3 when presenting live; the video uses the screen recording there.

Visual style suggestion: white background, Valio-ish deep blue (#1F4E9C) for the gateway, green for "pass", amber for "needs approval", red for "reject" (the same colours as the UI). Screenshots and video: `docs/reports/frontend/s5-approval-signed-in.png` (live S5 UI with sign-in and 3 approval rules), `docs/reports/frontend/s5-requote-diff.png` (409 requote diff card), `docs/pitch/img/approval-ui.png`, and headless Playwright backup capture [docs/demo-backup.mp4](../demo-backup.mp4).

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
**On slide:** "Trusted Trade gateway" in the middle; left: "Any agent: Gemini, Claude, ERP" (MCP / OpenAPI); right: "Valio Aimo backend"; bottom: "Human approver". The milk flow as 6 steps: request → live lookup → good-better-best → signed quote → policy: needs approval → approved, placed (AP), audited.
**Visual:** screenshot `docs/reports/frontend/s5-approval-signed-in.png` (or `docs/pitch/img/approval-ui.png`) or the 60-second clip (headless Playwright backup capture at [docs/demo-backup.mp4](../demo-backup.mp4)).
**Numbers:** €1,010 for 1,000 × Valio Eila 1 L at €1.01 [live run, docs/ARCHITECTURE.md §2]; usual 10 L box missed its cut-off Thu 12 Nov 15:00 [mock API]; 3 rules needed a human: spend > €500, outside preference, 3.3× usual weekly volume [frontend live run screenshot].
**Speaker notes (40 s):** walk the six steps. Stress: live mock-API data (synthetic, as the challenge provides), the agent re-planned around two real exceptions, the policy is the hotel's own file, the human approved the exact signed quote.

## Slide 4: Trust layer
**On slide:** four boxes in a row:
1. **Mandate**: who may act (scopes).
2. **Policy**: what runs alone (customer-owned YAML, strictest rule wins).
3. **Signed quote + human-only approval bound to its hash** (Ed25519 JWS, 15-min price lock; a human signs in, agents get 403).
4. **Hash-chained, signed audit**: readable, verifiable offline.
Bottom line: *Unknown is never safe. AI can add warnings, never remove them.*
**Numbers:** 3 demo customers with different rules (hotel €500 auto limit; school blocks milk and nuts and unknown allergens; hospital needs a dietitian for any substitution) [docs/TRUST.md §3].
**Speaker notes (30 s):** "An API key tells you who. Our mandate and policy tell you what they may do. The signature proves what was offered, the hash binding proves what the human approved, and the chain proves nothing was changed afterwards. Gemini reads allergens from Finnish ingredient text, but its reading can only block a product, never clear one."

## Slide 5: Robustness numbers
**On slide:** headline **"6 of 6 live scenarios pass"** with six small tiles: happy (approved, placed) · stock-out (split into 4 lines) · school allergen (milk rejected, oat drink approved) · prompt injection (4 texts flagged and redacted) · standing-order drift (held for a human) · last-minute event (2 items, one order). Caption: *Gemini agent, live mock API, 22–37 s each* [docs/demo-montage.md].
Second half (or a build): wind tunnel table, "Trusted gateway vs naive agent with direct API access":
| Metric | Ours | Naive |
|---|---|---|
| Scenarios (Gemini-generated hotels, schools, hospitals, events) | 640 | same |
| Correct escalations (recall) | 99.7% | 17.8% |
| Policy violations | **0** | 372 |
| Allergen violations | **0** | 193 |
| Injection block rate | 56.2% | 0% |
Source: `docs/windtunnel-results.md` § "Gemini-written scenarios (wt640)" (merged into main; run on git dfecb2a, snapshot `2f2fef829c50`). Our column is the need session's re-run after its policy changes (Sun 15 Nov 00:30, § "S3 need" of the same file); the naive column and the injection row are from Sat 18:24, against the earlier policy version. Honest caveat for Q&A: escalation precision is 68.2% (up from 54.0%; the naive agent's 90.8% comes from almost never escalating), so the gateway still asks a human more often than strictly needed; the 90% target is not met. Scale run on template text: 2,560 scenarios, 11 vs 1,452 policy violations, 0 vs 728 allergen violations [same file, § wt2560]. Injection screen: regex alone 44.4% recall, regex + Gemini 94.4% (Gemini flags 42/60 buyer requests, 0/60 supplier texts; a flag only means a human looks) [same file].
Footnote facts already measured: 0 false positives on 34,159 catalog supplier texts [docs/TRUST.md]; 707 passed keyless offline across 814 collected tests on `2e6ae66` (743 passed with full catalog JSON; 8/8 scenarios, 88 checks; 44/44 conformance on two wholesalers) [docs/NUMBERS.md, docs/OPENNESS.md]; an agent that tries to approve as the real manager gets 403 and the refusal is audited [docs/pitch/permissions_attack.py].
**Speaker notes (25 s):** "We didn't test one happy path. Six live scenarios, end to end with the Gemini agent against the live challenge mock API: stock-out, school allergens, prompt injection, standing-order drift, a last-minute event. All six pass. And our wind tunnel replays recorded API data across 640 Gemini-written customers, scored against an independent oracle: zero policy violations for us, 372 for a naive agent with direct API access, and zero allergen violations."

## Slide 6: Openness and production path
**On slide:** left column "Open today": MCP · OpenAPI 3.1 · otap.json discovery · JWS/JWKS · GS1 GTIN · Peppol AP/CA/RE · EU14 allergens. Right column "Production next": SAP adapter behind one interface · OAuth 2.1 (MCP auth spec) · mandates as W3C Verifiable Credentials · KMS keys · Peppol BIS Ordering · WORM audit store.
**Also on slide (small):** "Every assumption, decision and number is written down": docs/ASSUMPTIONS.md · docs/DECISIONS.md · docs/NUMBERS.md.
**Speaker notes (20 s):** "Nothing here is proprietary. Any agent connects with a three-line MCP config. To go live, Valio swaps the mock for SAP behind one interface we already have, puts OAuth in front, and moves the key into a KMS." [docs/ARCHITECTURE.md §4]

## Slide 7: Gemini
**On slide:** "Gemini on both sides of the gateway" with 6 small tiles: buyer agent (function calling over MCP) · need parsing (structured output) · hybrid search over 17,546 products (gemini-embedding-2) · allergen inference for 4,076 products from Finnish ingredient text (can only block) · injection screen layer 2 · wind tunnel customers and judge.
**Numbers:** **$0.034 per order** in Gemini calls, down 59% from $0.082 after a token trim; hero run 87k → 27k input tokens; 31% of input from Gemini's implicit cache [docs/demo-montage.md]. Escalation to gemini-4-hydrogen (thinking HIGH) on only the quote-decision turn: 19% of agent steps [same]. Team usage: 4,255 Gemini calls, 14.2M input tokens [docs/GEMINI.md §5, Sat 23:10 snapshot; refresh at the freeze]. Cost numbers recompute offline from `docs/evidence/`. Embedding the whole catalog took 44 s, allergen inference 49 s [catalog report].
**Speaker notes (20 s):** "Gemini reads Finnish, embeds the catalog, runs the buyer, screens for injection and plays thousands of customers. We use thinking levels as a policy knob: deep reasoning only on the one decision a human later has to defend, 19% of the steps. And a whole order costs about three cents in Gemini calls."

## Slide 8: Team and ask
**On slide:** team names and roles; the ask: *"Pilot with one Valio Aimo customer segment: schools or hotels."* Repo link and QR code: https://github.com/s1laswang/junction-aimo-agents (tag `aimo-v1`).
**Speaker notes (15 s):** "We're Silas, Momin, Ivar and Chintufp, with a team of Claude Code agents. Our ask: let us pilot this with one customer segment, behind your real webshop API, with the trust layer exactly as you've seen it."

---

## Timing for a 3-minute pitch with live demo
Slide 1 (0:15) → Slide 2 (0:20) → live demo instead of slide 3 (1:30, docs/DEMO.md 0:20–2:35 condensed) → Slide 5 (0:20) → Slide 6 (0:15) → Slide 7 (0:10, or skip if over time and cover Gemini during the demo) → Slide 8 (0:10).

## Video (2–3 minutes)
Slides 1–2 as title cards (20 s), screen recording of the demo (90–110 s, follow docs/DEMO.md; headless Playwright backup capture at [docs/demo-backup.mp4](../demo-backup.mp4), reproducible via `uv run python scripts/record_demo_video.py`), slides 4–7 (40 s), slide 8 (10 s). Captions for the three approval reasons and "Chain verified".
