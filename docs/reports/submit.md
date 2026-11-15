# Session S4 `submit`: report

Branch `team/submit`, worktree `~/junction-aimo-team/sessions/submit`. Owner of README, SUBMISSION, GEMINI, NUMBERS, DEMO, pitch/*, ASSUMPTIONS, DECISIONS, OPENNESS, demo-montage and the pitch report (docs/SESSIONS.md "S4 submit").

## Done (block 1, Sat 14 Nov 22:55–23:25)
| Item | Evidence |
|---|---|
| SUBMISSION.md and GEMINI.md: all 6 TODOs closed. Repo URL in; video and public-URL rows say "filled at the freeze" and name the exact cell and form field | `grep -c TODO docs/SUBMISSION.md docs/GEMINI.md` → 0, 0 |
| Sanitized montage ledgers (before and after the token trim) in `docs/evidence/`, with `montage_stats.py` | `uv run python docs/evidence/montage_stats.py` reproduces every montage number: 55 → 50 calls, $0.0822 → $0.0339 per order, hero 87,184 → 27,403 input, 31% cached, 6/36 → 6/31 escalated steps. Rows keep token counts, latency, model, run id and meta only; `host` and `pid` removed; the ledger never had prompts or keys |
| GEMINI.md ledger snapshot re-run | 4,255 calls over 7 ledger dirs (was 3,449 over 8 at Sat 18:27) |
| README truth pass | test count from a real run (267 passed, 7 skipped, 2 xfailed, fac34f0); 13 MCP tools (`build_mcp().list_tools()`); "Approving as a human" (PIN location, sign-in, 403/401/429); keyless vs keyed table; `AIMO_REPLAY`, `AIMO_NOW`; what `make accept` checks; `aimo-v1` tag |
| README curl flow fixed | the old step 6 sent `X-Agent-Id` to the decision route, which now returns 403. New steps 6–8 (agent 403, human sign-in, session approval, audit) were re-run on a replay gateway, Sat 23:10 |
| DEMO.md | sign-in moment, 30-second permissions attack, tamper demo with credential, Wi-Fi drop via replay, `AIMO_NOW` after Sun 17:00 (verified: now = Sun 18:00 gives `cutoff_passed` on 464306, now = Sun 10:00 does not) |
| `docs/pitch/permissions_attack.py` | the agent tries to approve as anna with the right hash: HTTP 403 `agent_cannot_approve`, audit line `approval.refused`, exit 0, about 3 s (replay gateway, Sat 23:14) |
| NUMBERS.md | every number with its command and status; unmeasured numbers (gold set, photo intake) listed as not claimed |
| `docs/pitch/QA.md` | 20 judge questions with file citations, including the honest demo-only gaps (shared PIN, header-only agent identity, self-held signing key) |
| OPENNESS.md | conformance re-run 44/44 on both wholesalers (the suite grew from 43); reports in docs/openness/ regenerated (no PIN inside) |
| ASSUMPTIONS, DECISIONS, deck | remaining TODOs closed with what is true now |

## Gate runs
| When | Commit | pytest | scenarios.run_all | make accept |
|---|---|---|---|---|
| Sat 14 Nov 23:22–23:28 | 8d0d10e (on fac34f0) | 267 passed, 7 skipped, 2 xfailed (41 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP: live Gemini demo by design; keyed .env; conformance 44/44, MILK FLOW OK) |
| Sat 14 Nov 23:26–23:29 | 74b5e79 (rebased on 4ae14c6, S5 ui merged) | 285 passed, 7 skipped, 2 xfailed (58 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP) → pushed to main |
| Sun 15 Nov 00:28–00:31 | rebased on 89bc3db (S2 harden, S3 need v1+v2, S5 ui round 2 merged) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (34 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP) → pushed to main |
| Sun 15 Nov 02:29–02:31 | rebased on 2a68792 (S1 replay merged) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | keyed RESULT: PASS (19 PASS, 1 SKIP); keyless fresh clone RESULT: PASS (16 PASS, 4 SKIP) → pushed to main |
| Sun 15 Nov 02:41–02:44 | d13a07b on 738c93a (one-page README, RUN.md, CONNECT.md, re-score) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | RESULT: PASS (19 PASS, 1 SKIP) → pushed to main |
| Sun 15 Nov 03:08–03:15 | rebased on b301d5a (tests + live merged) | 349 passed, 7 skipped, 1 xfailed, 1 xpassed (37 s) | 8/8 PASS (live) | keyed RESULT: PASS (19/0/1); keyless `git archive` of b301d5a RESULT: PASS (17/0/3, keyless pytest 250/0) → pushed to main |
| Sun 15 Nov 04:30–04:33 | rebased on ddbeeba (fleet ports: per-approver credentials, unknown_customer 403, request-text injection screen) | 357 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | RESULT: PASS (19/0/1) → pushed to main |
| Sun 15 Nov 05:00–05:08 | rebased on d4b0b41 (judge panel fixes A–D) | 362 passed, 7 skipped, 1 xfailed, 1 xpassed (39 s) | 8/8 PASS (live) | keyed RESULT: PASS (19/0/1); keyless `git archive` of d4b0b41 RESULT: PASS (17/0/3, keyless pytest 261/0) → S4-1 + S4-2 pushed |

## Done (hourly re-check 1, Sun 15 Nov 00:28)
- S3 need: wind-tunnel headline now 0 policy / 0 allergen violations for the gateway, recall 99.7%, precision 68.2% (from 54.0%; 90% target not met). Naive column stays Sat 18:24 and is labelled as against the earlier policy. Updated SUBMISSION, GEMINI row 8, DEMO 2:45 line, deck slide 5, QA 15–16, DECISIONS D5, ASSUMPTIONS, NUMBERS, README pillar 3.
- Hero flow re-checked on 89bc3db in replay: still exactly 3 needs-approval rules (spend, substitution, 3.3× drift); split_order and price_jump pass.
- S2 harden: fuzz (0 server errors over ~2,600 requests) and quantity 422s in SUBMISSION, QA 13, NUMBERS; audit reports now label the simulated clock (DEMO 2:30 note).
- S5 round 2 (re-quote after the price lock, presenter fit): no claim in S4 docs affected.
- Test count 344 / 7 / 1 xfailed / 1 xpassed everywhere.

## Done (hourly re-check 3, Sun 15 Nov 02:28)
- S1 replay merged: README env table, keyless vs keyed table, quickstart and `make accept` description rewritten (automatic replay, 669-product snapshot and catalog subset, 503 `catalog_missing`, bundled live fallback); DEMO step 1 and the Wi-Fi row drop `AIMO_REPLAY_FALLBACK`; NUMBERS, ASSUMPTIONS, OPENNESS, QA 13 and SUBMISSION updated.
- Own keyless check (fresh clone, empty `.env`, key variables unset): RESULT: PASS, 16 PASS / 4 SKIP. Keyless pytest has 8 failures from S2/S3 tests that need the full catalog but are not marked `needs_data`; documented in README and NUMBERS (request below).

## Done (hourly re-check, Sun 15 Nov 04:28)
- Fleet port `ccab8f5` (per-approver credentials) verified on a replay gateway: Anna's passphrase signs in as Anna (200) and not as Mikko (401); Mikko's own works (200); 6 other approvers each get a 12-character passphrase. README assumption 3, QA Q3 and DEMO step 2 updated (stage: use Anna's own `keys/approver_pin`, leave the operator `AIMO_APPROVER_PIN` empty); deck test count 357.

## Done (Sun 15 Nov 03:15): V3 live montage and V2 tests reflected
- New montage (Sun 02:54, final code; verified by `montage_stats.py`): 6/6, $0.0352 per order (−57% vs $0.0822 before trim), 38% cached, 5/34 escalated steps, school-allergen auto-approved by policy at €280, injection 6 texts, event €266.49. Updated SUBMISSION, GEMINI, DEMO, NUMBERS, deck, QA 9/20, DECISIONS D6.
- Test counts 349 keyed / 250 keyless with 0 failures; keyless `make accept` 17/0/3. The "8 keyless failures" caveat is removed everywhere.

## Keyless re-score before `aimo-v1` (Sun 15 Nov 02:37–02:55, handed over by the bake-off review session)
On two `git archive` exports of main 738c93a, no `.env`, every key variable unset. Probe scripts from `~/junction-aimo-team/review-probes/aimo/`, retargeted to local ports; `session_probe.py` was rewritten as `session_v2.py` because sessions now need `{"approver": ...}`.
| Check | Result |
|---|---|
| `make accept` keyless | RESULT: PASS, 16 pass / 0 fail / 4 skip; scenarios 8/8 (replay), conformance 44/44, MILK FLOW OK, order ORD-20261114-8ce7ab61, Peppol AP, audit verify ok (20 events) |
| Original self-approval exploit (agent headers + anna + remember) | 403 `agent_cannot_approve`; also with lowercase header and with the correct PIN |
| Wrong, missing, duplicated or query/body PIN; tamper without credential | 401 |
| Rotating X-Forwarded-For / X-Real-IP / Forwarded | stays locked: 429 (298/300, 30/30, 30/30) |
| Brute force | 498 of 500 guesses get 429 after the lock; the credential is a 12-character passphrase |
| anna's session deciding as mikko | 403 `approver_mismatch` |
| Forged session tokens (truncated, flipped, garbage v1, v1 prefix, approver swapped) | 401 ×5 |
| MCP create_quote / place_order without identity (HTTP: none, agent-only, customer-only; stdio) | `identity_required`; unknown agent → `scope_missing` |
| PIN length (judge_accept.sh, run_all.py, generated file) | 12 characters, mode 600 |
| PIN / session token in logs, audit, JS, 9 endpoints | not found |
| 2.5 ST; bad `/v1/agent/runs` body; search and plan with no data | 422; 422; 503 `catalog_missing` |
| Known, documented gap | spoofed `X-Customer-Id` alone lists that customer's inbox (header-only agent identity, QA Q4/Q11) |
| Learned swap after "remember" | the swap no longer escalates as a substitution; spend and drift still do |

## Open items
- Re-check README, SUBMISSION, DEMO and QA against main every hour while the other sessions merge:
  - S1 replay: keyless `make accept` and the catalog subset will change the "Keyless vs keyed" table (catalog row) and NUMBERS "make accept".
  - S2 harden: fuzz tests change the test count; MCP URL / port handling may change README "Connect your own agent".
  - S3 need: escalation precision, split-order window and learned approvals change SUBMISSION robustness, QA 15–16, DECISIONS D5, deck slide 5.
  - S5 ui: the sign-in panel changes DEMO 2:00–2:20 wording and checklist step 2 (plain-text PIN prompt); new screenshots for README.
- At the freeze: re-run pytest and the ledger snapshot; paste the video URL and public URL (or the "runs locally" sentence) into SUBMISSION.

## Requests to other sessions
- ~~**S2 harden / S3 need:** mark the 8 catalog-dependent tests~~ done by the tests session (0f51cf0); (`tests/test_harden.py` ×7, `tests/test_need.py::test_split_orders_escalate_once_they_pass_the_auto_limit`) `needs_data`, so keyless pytest is clean instead of 8 failures.
- ~~**S1 replay:** automatic live fallback~~ resolved: `fallback_dir()` now ends at the bundled snapshot (merged Sun ~02:00); DEMO step 1 no longer sets `AIMO_REPLAY_FALLBACK`.
- ~~**S5 ui:** plain-text PIN prompt~~ resolved by S5's sign-in panel (masked PIN field), merged Sat 23:18; DEMO and README updated.
- **Gateway owner (no session owns it):** agent identity is header-only. Known and documented (ASSUMPTIONS A8, QA Q4); no change requested before the freeze.
