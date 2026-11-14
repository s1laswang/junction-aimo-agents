# Deck outline: Trusted Trade Agents (8 slides)

For Chintufp to build the visual deck and record the video. Every number has a source in brackets; **TODO** marks numbers that arrive with the wind tunnel run. Keep slides sparse: one idea, one visual, at most 15 words of text. The live demo (docs/DEMO.md) replaces slide 3 when presenting live; the video uses the screen recording there.

Visual style suggestion: white background, Valio-ish deep blue (#1F4E9C) for the gateway, green for "pass", amber for "needs approval", red for "reject" (the same colours as the UI). Screenshots: `docs/pitch/img/approval-ui.png`, plus fresh captures from `/ui` after the freeze.

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
**Visual:** screenshot `docs/pitch/img/approval-ui.png` or the 60-second clip.
**Numbers:** €1,010 for 1,000 × Valio Eila 1 L at €1.01 [live run, docs/ARCHITECTURE.md §2]; usual 10 L box missed its cut-off Thu 12 Nov 15:00 [mock API]; 3 rules needed a human: spend > €500, outside preference, 3.3× usual weekly volume [frontend live run screenshot].
**Speaker notes (40 s):** walk the six steps. Stress: real mock-API data, the agent re-planned around two real exceptions, the policy is the hotel's own file, the human approved the exact signed quote.

## Slide 4: Trust layer
**On slide:** four boxes in a row:
1. **Mandate**: who may act (scopes).
2. **Policy**: what runs alone (customer-owned YAML, strictest rule wins).
3. **Signed quote + approval bound to its hash** (Ed25519 JWS, 15-min price lock).
4. **Hash-chained, signed audit**: readable, verifiable offline.
Bottom line: *Unknown is never safe. AI can add warnings, never remove them.*
**Numbers:** 3 demo customers with different rules (hotel €500 auto limit; school blocks milk and nuts and unknown allergens; hospital needs a dietitian for any substitution) [docs/TRUST.md §3].
**Speaker notes (30 s):** "An API key tells you who. Our mandate and policy tell you what they may do. The signature proves what was offered, the hash binding proves what the human approved, and the chain proves nothing was changed afterwards. Gemini reads allergens from Finnish ingredient text, but its reading can only block a product, never clear one."

## Slide 5: Robustness numbers
**On slide:** a big headline number plus a small comparison table, "Trusted gateway vs naive agent with direct API access":
| Metric | Ours | Naive |
|---|---|---|
| Scenarios (Gemini-generated hotels, schools, hospitals, events) | **TODO** | same |
| Correct escalations (recall) | **TODO** | **TODO** |
| Policy violations | **TODO (target 0)** | **TODO** |
| Allergen violations | **TODO (target 0)** | **TODO** |
| Injection block rate | **TODO** | **TODO** |
Footnote facts already measured: 0 false positives on 34,159 real supplier texts [docs/TRUST.md]; 63 offline tests pass [pytest, Sat 14 Nov].
**Speaker notes (25 s):** "We didn't test one happy path. Our wind tunnel replays recorded API data with stock-outs, outages and injections across TODO synthetic customers, scored against an independent oracle." Read the headline number.

## Slide 6: Openness and production path
**On slide:** left column "Open today": MCP · OpenAPI 3.1 · otap.json discovery · JWS/JWKS · GS1 GTIN · Peppol AP/CA/RE · EU14 allergens. Right column "Production next": SAP adapter behind one interface · OAuth 2.1 (MCP auth spec) · mandates as W3C Verifiable Credentials · KMS keys · Peppol BIS Ordering · WORM audit store.
**Speaker notes (20 s):** "Nothing here is proprietary. Any agent connects with a three-line MCP config. To go live, Valio swaps the mock for SAP behind one interface we already have, puts OAuth in front, and moves the key into a KMS." [docs/ARCHITECTURE.md §4]

## Slide 7: Gemini
**On slide:** "Gemini on both sides of the gateway" with 6 small tiles: buyer agent (function calling over MCP) · need parsing (structured output) · hybrid search over 17,546 products (gemini-embedding-2) · allergen inference for 4,076 products from Finnish ingredient text (can only block) · injection screen layer 2 · wind tunnel customers and judge.
**Numbers:** 638 Gemini calls, 1.2M input tokens, 17,574 texts embedded as of Sat 14:46 [docs/GEMINI.md §5; **TODO** final numbers]; embedding the whole catalog took 44 s, allergen inference 49 s [catalog report]; flash LOW for routine steps, 4-hydrogen HIGH thinking for substitution decisions.
**Speaker notes (20 s):** "Gemini reads Finnish, embeds the catalog, runs the buyer, screens for injection and plays thousands of customers. And we use thinking levels as a policy knob: reasoning is spent on the one decision a human later has to defend."

## Slide 8: Team and ask
**On slide:** team names and roles; the ask: *"Pilot with one Valio Aimo customer segment: schools or hotels."* Repo link and QR code (**TODO** link).
**Speaker notes (15 s):** "We're Silas, Momin, Ivar and Chintufp, with a team of Claude Code agents. Our ask: let us pilot this with one customer segment, behind your real webshop API, with the trust layer exactly as you've seen it."

---

## Timing for a 3-minute pitch with live demo
Slide 1 (0:15) → Slide 2 (0:20) → live demo instead of slide 3 (1:30, docs/DEMO.md 0:20–2:35 condensed) → Slide 5 (0:20) → Slide 6 (0:15) → Slide 7 (0:10, or skip if over time and cover Gemini during the demo) → Slide 8 (0:10).

## Video (2–3 minutes)
Slides 1–2 as title cards (20 s), screen recording of the demo (90–110 s, follow docs/DEMO.md), slides 4–7 (40 s), slide 8 (10 s). Captions for the three approval reasons and "Chain verified".
