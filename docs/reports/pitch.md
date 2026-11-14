# Pitch, QA and docs report

Branch `team/pitch`. Last updated Sat 14 Nov 2026, about 15:05 Helsinki (machine clock). Merged with origin/main at 14:55 (buyer, frontend and gateway round 2 included).

## Status
| Deliverable | File | State |
|---|---|---|
| README (quickstart, five pillars, mermaid architecture, connect your own agent, curl walkthrough, repo map, team) | `README.md` | done; commands verified (see below) |
| Live demo script, click by click, criteria mapping, fallbacks | `docs/DEMO.md` | done; wind-tunnel numbers TODO |
| DeepMind write-up | `docs/GEMINI.md` | done; usage snapshot Sat 14:46, final numbers TODO |
| Junction submission text + short version (425 chars) | `docs/SUBMISSION.md` | done; robustness numbers and links TODO |
| Architecture, milk data flow, trust summary, production path | `docs/ARCHITECTURE.md` | done |
| Deck outline, 8 slides with speaker notes (for Chintufp) | `docs/pitch/deck.md` | done; slide 5 numbers TODO |
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

## Issues for engineers
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
2. **Fix issues 1 and 2 before the freeze?** They are small, but they are exactly what a "controls" judge might probe. I recommend yes for 1 (a one-line rule) and at least defaulting a missing tier to "unknown → needs approval" for 2.
3. Public repo link, video link and whether there is a live URL, for `docs/SUBMISSION.md`.

## Pending
- Wind tunnel numbers (DEMO 2:35–2:50, deck slide 5, SUBMISSION "Robustness", GEMINI usage). All marked TODO.
- Final Gemini ledger numbers: re-run `docs/pitch/ledger_stats.py` with every worktree's ledger before submitting (command in GEMINI.md §5).
- Re-capture screenshots from main after the freeze; video link.
- Timings for the montage scenarios (school-allergen, injection) after the catalog and gateway fixes from the buyer report landed.
