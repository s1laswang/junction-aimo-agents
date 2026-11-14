# Pitch, QA and docs report

Branch `team/pitch`. Last updated Sat 14 Nov 2026, 18:35 Helsinki. Round 2: merged origin/main at 18:20 (trust r2, gateway r3, catalog r3, buyer r2–r3; 138 tests).

## Status
| Deliverable | File | State |
|---|---|---|
| README (quickstart, five pillars, mermaid architecture, connect your own agent, curl walkthrough, repo map, team) | `README.md` | done; commands verified (see below) |
| Live demo script, click by click, criteria mapping, fallbacks, robustness montage beat (`aimo demo --montage`) | `docs/DEMO.md` | done; wind-tunnel line TODO |
| DeepMind write-up | `docs/GEMINI.md` | done; montage, cost per order, caching and escalation share from docs/demo-montage.md; usage over 8 ledgers at Sat 18:27; wind tunnel TODO |
| Junction submission text + short version (425 chars) | `docs/SUBMISSION.md` | done; live montage table in; wind tunnel numbers and links TODO |
| Architecture, milk data flow, trust summary, production path | `docs/ARCHITECTURE.md` | done |
| Deck outline, 8 slides with speaker notes (for Chintufp) | `docs/pitch/deck.md` | done; slide 5 has the 6/6 montage, wind tunnel table TODO; slide 7 has cost per order |
| Gemini ledger summariser | `docs/pitch/ledger_stats.py` | done |
| HTTP QA of the milk flow (no Gemini, about 3 s) | `docs/pitch/qa_milk_flow.py` | done, passes on merged main |
| README screenshot | `docs/pitch/img/approval-ui.png` | copied from the frontend's live run; re-capture after the freeze |

## Verified (on merged main, Sat 14 Nov 14:55–15:05, port 8020, fresh state DB in /tmp)
- `uv run aimo serve`: healthz shows every integration live; `/ui/` returns 200.
- `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8020`: MILK FLOW OK (search, offers with `served_from=live`, plan best 464306 × 1000 = €1,010, signed quote, pending_approval with spend and 3.3× drift, wrong hash → 409, approve → placed AP, 38-line audit report, verify ok).
- `uv run aimo demo --base-url http://localhost:8020 --auto-approve-as anna@hotel-aurora.example`: placed in 38 s, escalated to gemini-4-hydrogen HIGH, 3 needs-approval reasons.
- `uv run aimo mcp-check` (HTTP) and `uv run aimo mcp-check stdio`: 9 tools listed, search and offers return data.
- README curl commands in both bash and zsh (array form `"${H[@]}"`).
- `POST /v1/demo/tamper/3` → verify reports `broken_at: 3`; `untamper` → ok.
- `uv run --extra dev pytest -q`: 106 passed.
- Gemini layer-2 injection screen re-run once: paraphrased injection flagged with no regex match.

Untested: the Claude Desktop / Cursor / Gemini CLI configs in the README (formats from each client's documentation; the server side is verified with the MCP Python client). The hospital customer end to end. The UI click path itself (I described it from `web/app.js` and the frontend's screenshots, not by clicking).

## Round 3: Swarm D docs lead (Sat 19:10)
- **New:**
  - `docs/ASSUMPTIONS.md`: A1–A10, each with status, evidence, impact and production replacement.
  - `docs/DECISIONS.md`: D1–D11 ADRs plus production proposals D-P1–D-P4.
  - `docs/NUMBERS.md`: every number with its command and reproduction status.
  - A phased "Next steps" table in `docs/ARCHITECTURE.md` §5: owners, exit criteria and risks.
- **New scripts:**
  - `docs/pitch/probe_mock_quirks.py` (live, 5 calls) measured three mock-API quirks: availability ignores requestedDate (always one schedule line dated 16 Nov), closingTime is fixed per product, and prices are identical for two customer ids.
  - `docs/pitch/repro_injection_fp.py` (offline, 3 s) reproduces 0 of 34,159 marketing texts flagged.
- **Test count:** now 165 passed, 1 skipped. README, SUBMISSION and the deck are updated.
- **Not reproducible yet:** the unit-parser 93% (702/752) has no committed script. That is either a D-proof card or the catalog lead's job.
- **D outputs:** no `swarm/D/*` branches on origin as of 19:10, so nothing is folded yet.

## Round 2 re-test (Sat 14 Nov 18:26, merged main with trust r2, gateway r3, catalog r3, buyer r2–r3; fresh state DB, port 8020)
- `docs/pitch/qa_milk_flow.py`: **MILK FLOW OK.** The hero verdict now has exactly three needs-approval rows: spend, `substitution:464306` (one row: best tier, outside standing preferences) and `quantity_drift:milk`. Quote creation re-prices live (0.36 s, `served_from: live`, `upstream_calls: 2`).
- `uv run --extra dev pytest -q`: 138 passed.
- Edge script `/tmp/pitchqa/qa_edges3.py` (not committed): results per issue below.

| # | Issue (round 1) | Round 2 result |
|---|---|---|
| 1 | Unregistered approver could approve by naming the role | **FIXED.** `approver: "buyer-1"` and `"hotel-aurora/buyer-1"` → 403 `approval_refused` ("is an AI agent identity; agents can never approve orders"); `mallory@evil.example` → 403 ("not in hotel-aurora's approver directory"); `anna@hotel-aurora.example` → 200 placed |
| 2 | Tier and flags trusted from the agent | **FIXED.** 469363 sent as `preferred` or with no tier → quote line `better` with `outside_preference, limited_stock` → pending_approval; 464306 with no flags → `best, outside_preference` → pending_approval; school 470863 with no tier → `good` → pending_approval |
| 3 | Body customer_id overrode the header | **FIXED.** Header school-tampere, body hotel-aurora → 403 `customer_mismatch` |
| 4 | Duplicate contradictory `substitution` rows | **FIXED.** One row per rule id |
| 5 | Quotes served prices from cache | **FIXED.** Quote provenance `served_from: live` |
| 6 | Search "maito" ranks non-milk items first | **OPEN (low, catalog).** Top 3: HEIROL BRIOCCHI MILK JUGS 30CL, HEIROL BRIOCCHI MILK JUGS 50CL, WMF Cleaner for milk systems 1L |
| 7 | Unregistered agents got signed quotes | **FIXED.** `X-Agent-Id: mallory` → 403 `scope_missing` at quote time |
| 8 | Weekly budget remembers rehearsals (demo risk) | **OPEN (process).** Mitigated in DEMO.md: fresh state dir for the hero demo, and the montage runs on a second gateway with its own state |

## Issues for engineers (round 1, kept for reference)
All reproduced over HTTP on merged main with headers `X-Customer-Id: hotel-aurora`, `X-Agent-Id: buyer-1` unless stated. Script: `/tmp/pitchqa/qa_edges2.py` (not committed; inputs are inline below).

1. **[trust] HIGH: an unregistered approver can approve by naming the role.**
   `POST /v1/approvals/{order_id}/decision` on a pending €606 order (600 × 464306) with body `{"decision":"approve","approver":"buyer-1","approver_role":"procurement_manager","note":"self","quote_hash":"<correct hash>"}`.
   Expected: 403 `approval_refused` (buyer-1 is not in hotel-aurora's approver directory).
   Actual: 200, order `placed`, audit `human:buyer-1 approval.verified`.
   Cause: `check_approval` in `aimo/trust/approvals.py` (around lines 43–46) only refuses a *known* approver acting in the wrong role; an unknown approver passes. Fix: refuse approvers not in the policy's directory when the directory is non-empty. This matters for the pitch line "an agent cannot approve its own order": it is true over MCP, but the REST endpoint accepts the agent's own headers.

2. **[trust + gateway] HIGH: the substitution guardrail trusts the tier and flags the agent sends.**
   `POST /v1/quotes` `{"delivery_date":"2026-11-16","lines":[{"product_id":"469363","qty_sales_units":10,"tier":"preferred"}]}` then `POST /v1/orders` → `placed` (auto). The same line with `"tier":"better"` → `pending_approval` (substitution needs a procurement manager). With `tier` omitted it defaults to `preferred` and is also placed.
   Likewise 464306 without `flags` → substitution "Allowed automatically"; with `"flags":["outside_preference"]` → needs approval. For school-tampere, 470863 (Oatly) with no tier is auto-placed, although the policy says any substitution needs the kitchen manager.
   Expected: the gateway or trust derives tier and `outside_preference` server-side from the customer's standing preferences (e.g. the policy's preferred products or the plan stored for the quote), and a missing tier is not treated as `preferred`. The buyer agent does pass the plan's flags today, so the hero demo shows the right verdict, but a different agent would not.

3. **[gateway] MEDIUM: the body's customer_id overrides the authenticated header.**
   `POST /v1/quotes` with header `X-Customer-Id: school-tampere` and body `{"customer_id":"hotel-aurora", ...}` → 200, a signed quote for hotel-aurora. Placing it with the school header correctly returns 403 `customer_mismatch`, but issuing the quote in another customer's name should already be 403.

4. **[trust] LOW (readability on stage): contradictory duplicate rule rows.** In the `aimo demo` decision (and the UI's rule table) `substitution:464306` appears twice: `[allow] ... (tier best). Allowed automatically by this policy.` and `[needs_approval] ... is outside your standing preferences`. Expected: one row per rule id, or the allow text should not say "allowed automatically" when the flag fires.

5. **[gateway] LOW (docs accuracy): quote prices are not re-fetched when the cache is fresh.** `POST /v1/quotes` provenance shows `"served_from": "cache", "upstream_calls": 0` (the offers were fetched up to 300 s earlier). interfaces.md says "prices re-fetched live". Either re-fetch on quote or document "price and stock at most 5 minutes old, then locked for 15 minutes". I wrote the docs as "re-check (served from the 5-minute cache if fresh)".

6. **[catalog] LOW: search ranking.** `GET /v1/products/search?q=maito&limit=1` returns 466434 "HEIROL BRIOCCHI MILK JUGS 30CL" first. Expected a milk product.

7. **[gateway] LOW: unregistered agents get signed quotes.** `X-Agent-Id: mallory` → `POST /v1/quotes` 200 with a signed quote; the order is then correctly rejected by `mandate_scope`. Refusing the quote with 403 would be cleaner and keeps junk quotes out of the audit log.

8. **[demo risk] The weekly budget remembers rehearsals.** The `weekly_budget` rule sums `order.placed` events over 7 days. After three test runs the 7-day spend already read €2,700.40; after about seven approved 1,000 L runs it passes €8,000 and adds another needs-approval reason. Mitigation is in DEMO.md (fresh `AIMO_DATA_DIR` with only the state DB removed). A `aimo serve --fresh-state` flag or a demo reset route would be safer.

## Decisions for Silas
1. **Cut-off clock.** 464306's order cut-off for Monday 16 Nov delivery is **Sun 15 Nov 17:00** (`closing_time` from `/v1/offers`). If the live pitch or judging is after about 16:30 on Sunday, the hero request "by Monday" will not plan 464306. Please confirm the pitch slot. If it is late, we demo in mock mode or change the request date.
2. ~~Fix issues 1 and 2 before the freeze?~~ Done in round 2 and verified.
3. Public repo link, video link and whether there is a live URL, for `docs/SUBMISSION.md`.

## Pending
- Wind tunnel numbers (DEMO 2:35–2:50 line, deck slide 5 table, SUBMISSION "Robustness", GEMINI scenario count, judge results and thinking comparison). All marked TODO.
- Note on run times: the CEO brief said about 30–39 s per scenario; docs/demo-montage.md measures 22.13–36.85 s (drift is the 22 s one, because it stops before ordering). The docs quote the file: 22–37 s.
- Final Gemini ledger numbers: re-run `docs/pitch/ledger_stats.py` with every worktree's ledger before submitting (command in GEMINI.md §5).
- Re-capture screenshots from main after the freeze; video link.
