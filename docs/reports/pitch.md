# Pitch, QA and docs report

> **From Sat 14 Nov 23:25 the submit session (S4, [submit.md](submit.md)) and `fr8/valio/docs` coordinate these docs.** Gate runs, V3 live montage ([live.md](live.md)), keyless pytest ([tests.md](tests.md)), and Judge panel fixes A-D (`9742758`): [submit.md](submit.md).

Branch `team/pitch` / `fr8/valio/docs`. Last updated Sun 15 Nov 2026, 03:20 Helsinki. Round 5 truth pass: reconciled all docs against `origin/main` (`9742758`, `e97a367`, `7c290cf`) and `fr8/valio/docs` (`c98e72e`):
- Completed S1-S5 + `live` (`b301d5a`, [live.md](live.md)) + `tests` (`0f51cf0`, [tests.md](tests.md)) + Judge panel fixes A-D (`a99692f`, `d4b0b41`, `9742758`, `e97a367`) + Gemini quota fallback (`7c290cf`): operator-only `X-Aimo-Now` / `X-Aimo-Faults` gated behind `AIMO_DEMO_FAULTS=1`, `409 requote_needed` (`reason: partial_stock`) when stock drops below quoted quantity at approval instead of silent partial `CA`, wind tunnel `wt640` re-scored on final code with committed evidence in `docs/evidence/windtunnel/`, `need.product_query` + `need.raw_text` prompt-injection screening, `docs/RUN.md` (with 9-row rubric exception matrix), `docs/CONNECT.md`, and `docs/OPENNESS.md` production path.
- Closed all 6 `fr8/valio/*` judge-review gaps: 13 MCP tools (`build_mcp().list_tools()`, zero approval tools), `PlanLine.injection_screen` and `Candidate.injection_screen` surfaced on plan lines, `quantity <= 0` pre-planning guard (`Field(gt=0)` -> 422 before catalog/upstream calls), customer policy allergen hard-block inside `plan_need`, known-customer `/v1` and MCP auth (`403 unknown_customer`) plus per-approver 12-char passphrases (`approver_pins.json` / `AIMO_APPROVER_PINS` + `X-Approver-Session` lockout), and keyless scripted buyer (`aimo/buyer/mock_model.py`) so `aimo demo` finishes without `GEMINI_API_KEY`.
- Verified counts: `1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` across `1,275` collected tests keyless offline on `fr8/valio/docs` `c98e72e` (`743 passed, 7 skipped, 2 xfailed` with full 17,546-product catalog JSON on `2468bdd`; `362 passed` keyed / `261 passed, 0 failed` keyless on `origin/main` `9742758`, `366 passed` after `7c290cf`), `8/8` scenarios across 88 checks live and keyless replay, and 44/44 `otap/0.1` conformance on two wholesalers.

## Status
| Deliverable | File | State |
|---|---|---|
| README (one-page entry, must-haves checklist, 13 MCP tools, keyless quickstart, architecture, team) | `README.md` | done; 13 MCP tools, human approval header fixed, reconciled pytest + conformance counts |
| Keyless judge runbook and 9-row rubric exception matrix | `docs/RUN.md` | done; replay, per-approver PINs, `409 requote_needed`, every command verified |
| External agent integration guide (MCP stdio/HTTP + REST `/v1` + `otap/0.1`) | `docs/CONNECT.md` | done; Claude Desktop / Cursor / Gemini CLI JSON configs, 13 tools, known-customer auth |
| Conformance reports + phased production path and pilot KPIs | `docs/OPENNESS.md` | done; 44/44 on Valio Aimo + Nordic Bakery Supply, OAuth 2.1 + `BackendClient` path |
| Live demo script, click by click, criteria mapping, fallbacks, permissions attack, `409 requote_needed` beat, montage | `docs/DEMO.md` | done; sign-in chip, voice confirm, wind-tunnel line, `AIMO_NOW` / `AIMO_REPLAY` |
| DeepMind write-up | `docs/GEMINI.md` | done; montage, cost per order, caching, escalation share, windtunnel `wt640`/`wt2560`, thinking benchmark, judge rubric |
| Junction submission text + short version (425 chars) | `docs/SUBMISSION.md` | done; public repo URL, local replay run + `docs/demo-backup.mp4`, reconciled counts |
| Architecture, milk data flow, trust summary, production path | `docs/ARCHITECTURE.md` | done; human-only approvals, windtunnel + openness components |
| Deck outline, 8 slides with speaker notes (for Chintufp) | `docs/pitch/deck.md` | done; slide 5 `wt640`/`wt2560` table, slide 7 cost per order, slide 8 repo URL |
| Judge Q&A (Q1-Q22 citing `aimo/` files and `NUMBERS.md`) | `docs/pitch/QA.md` | done |
| Gemini ledger summariser | `docs/pitch/ledger_stats.py` | done |
| HTTP QA of the milk flow (no Gemini, about 3 s) | `docs/pitch/qa_milk_flow.py` | done, passes on merged main and keyless replay |
| README screenshot | `docs/pitch/img/approval-ui.png` (`docs/reports/frontend/s5-*.png` captured in S5) | done |

## Verified (on merged main, Sat 14 Nov 14:55–15:05, port 8020, fresh state DB in /tmp)
- `uv run aimo serve`: healthz shows every integration live; `/ui/` returns 200.
- `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8020`: MILK FLOW OK (search, offers with `served_from=live`, plan best 464306 × 1000 = €1,010, signed quote, pending_approval with spend and 3.3× drift, wrong hash → 409, approve → placed AP, 38-line audit report, verify ok).
- `uv run aimo demo --base-url http://localhost:8020 --auto-approve-as anna@hotel-aurora.example`: placed in 38 s, escalated to gemini-4-hydrogen HIGH, 3 needs-approval reasons.
- `uv run aimo mcp-check` (HTTP) and `uv run aimo mcp-check stdio`: 9 trade tools listed at M1 (now 13 tools on merged main after adding recipes, drift and `propose_offer`), search and offers return data.
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
1. **Cut-off clock.** 464306's order cut-off for Monday 16 Nov delivery is **Sun 15 Nov 17:00** (`closing_time` from `/v1/offers`). If the live pitch or judging is after about 16:30 on Sunday, the hero request "by Monday" will not plan 464306 on the real wall clock. Mitigated in S1 `replay` (`14d6939` / `2a68792`, ported on `2e6ae66`): replay mode anchors the clock at the snapshot's recorded `timeNow` (`2026-11-14T23:50:28+0200`) and `aimo.backend.clock.next_delivery_day()` computes the next weekday whose Stock cut-off is still ahead on `AIMO_NOW` / replay clock.
2. ~~Fix issues 1 and 2 before the freeze?~~ Done in round 2 and verified.
3. ~~Public repo link, video link and whether there is a live URL, for `docs/SUBMISSION.md`.~~ Closed in S4-2 (`9742758`) and `fr8/valio/docs` (`b3c97cd` / `c98e72e`): public repo URL, 3-command local keyless evaluation (`make accept`, `uv run aimo serve` -> `http://localhost:8000/ui`), and committed backup video (`docs/demo-backup.mp4`) documented in `docs/SUBMISSION.md`.

## Closed in Round 4-5, S1-S5, `live` / `tests`, and Judge panel fixes A-D (`9742758`, `c98e72e`)
- Wind tunnel numbers folded into `DEMO.md`, `deck.md`, `SUBMISSION.md`, `GEMINI.md`, and `NUMBERS.md` from `docs/windtunnel-results.md`, including S3 `need` v2 (`89bc3db`) and Judge panel fix C (`d4b0b41` / `9742758`): `wt640` gateway **0 policy / 0 allergen violations**, recall **99.7%**, precision **68.2%** (from 54.0%), outcome accuracy **73.4%**, re-scored on final code with committed inputs under `docs/evidence/windtunnel/`.
- S2 `harden` (`cf6534e` / `627c692`): ~2,600 fuzzed `/v1` requests with 0 server errors, pre-planning `quantity <= 0` guard (`Field(gt=0)` -> 422), fractional count-unit / implausible quantity 422s, and simulated clock labels on audit reports folded into docs.
- S1 `replay` (`2a68792` / `627c692`), `tests` (`0f51cf0`, [tests.md](tests.md)), and `live` (`b301d5a`, [live.md](live.md)): bundled gzipped snapshot (`tests/fixtures/snapshot/replay/`, 669 ids x 16-20 Nov + catalog subset), V3 live montage (`6/6 PASS`, `$0.0352` per order, 53 calls, 38% cached; Sat sanitized ledgers at `$0.0339` per order reproducible via `docs/evidence/montage_stats.py`), keyless scripted buyer (`aimo/buyer/mock_model.py`, `aimo demo --scenario ...` on real replay gateway with `Verdict [...]` lines), keyless `make accept` `RESULT: PASS` (`17 PASS, 0 FAIL, 3 SKIP` on `9742758`), keyless `scenarios.run_all` **8/8 passed** (88 checks), and keyless offline pytest **1,168 passed, 105 skipped, 1 xfailed, 1 xpassed** across `1,275` collected tests on `c98e72e` (`261 passed, 0 failed` keyless / `362 passed` keyed on `9742758`, `366 passed` after `7c290cf` Gemini model fallback).
- Judge panel fixes A, B, D (`a99692f` / `9742758`) and `fr8/valio/*` auth + planning closures: operator-only `X-Aimo-Now` / `X-Aimo-Faults` (`AIMO_DEMO_FAULTS=1`), `409 requote_needed` on `partial_stock` at approval (never silent partial `CA`), `PlanLine.injection_screen` plus `need.raw_text` and `need.product_query` screening, customer policy allergen hard-block in `plan_need`, known-customer `403 unknown_customer` across `/v1` and MCP, per-approver 12-char credentials, and 13 MCP tools with no approval tool.
- Note on run times: `docs/demo-montage.md` measures 22.13-36.85 s on the Sat run (20-37 s on V3 Sun run); docs quote 22-37 s / 20-37 s.
- Reconciled test and conformance counts across all docs (`pytest -q`, `scenarios.run_all` 8/8, `conformance` 44/44 on two wholesalers, `docs/RUN.md`, `docs/CONNECT.md`, `docs/OPENNESS.md`).
