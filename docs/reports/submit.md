# Session S4 `submit`: report

Branch `team/submit`, worktree `~/junction-aimo-team/sessions/submit` (synced with `fr8/valio/docs`). Owner of README, SUBMISSION, GEMINI, NUMBERS, DEMO, RUN, CONNECT, pitch/*, ASSUMPTIONS, DECISIONS, OPENNESS, demo-montage and the pitch report ([docs/SESSIONS.md](../SESSIONS.md) "S4 submit").

## Done (block 1, Sat 14 Nov 22:55-23:25)
| Item | Evidence |
|---|---|
| SUBMISSION.md and GEMINI.md: all 6 open placeholders closed. Repo URL in; video and public-URL rows documented with exact form fields (`docs/demo-backup.mp4` and local keyless `make accept` / `uv run aimo serve`) | zero open placeholders in `docs/SUBMISSION.md` and `docs/GEMINI.md` |
| Sanitized montage ledgers (before and after the token trim) in `docs/evidence/`, with `montage_stats.py` | `uv run python docs/evidence/montage_stats.py` reproduces every montage number: 55 -> 50 calls, $0.0822 -> $0.0339 per order, hero 87,184 -> 27,403 input, 31% cached, 6/36 -> 6/31 escalated steps. Rows keep token counts, latency, model, run id and meta only; `host` and `pid` removed; the ledger never had prompts or keys |
| GEMINI.md ledger snapshot re-run | 4,255 calls over 7 ledger dirs (was 3,449 over 8 at Sat 18:27) |
| README truth pass | test count from a real run (267 passed, 7 skipped, 2 xfailed, fac34f0); 13 MCP tools (`build_mcp().list_tools()`); "Approving as a human" (PIN location, sign-in, 403/401/429); keyless vs keyed table; `AIMO_REPLAY`, `AIMO_NOW`; what `make accept` checks; `aimo-v1` tag |
| README curl flow fixed | the old step 6 sent `X-Agent-Id` to the decision route, which now returns 403. New steps 6-8 (agent 403, human sign-in, session approval, audit) were re-run on a replay gateway, Sat 23:10 |
| DEMO.md | sign-in moment, 30-second permissions attack, tamper demo with credential, Wi-Fi drop via replay, `AIMO_NOW` after Sun 17:00 (verified: now = Sun 18:00 gives `cutoff_passed` on 464306, now = Sun 10:00 does not) |
| `docs/pitch/permissions_attack.py` | the agent tries to approve as anna with the right hash: HTTP 403 `agent_cannot_approve`, audit line `approval.refused`, exit 0, about 3 s (replay gateway, Sat 23:14) |
| NUMBERS.md | every number with its command and status; unmeasured numbers (gold set, photo intake) listed as not claimed |
| `docs/pitch/QA.md` | judge questions with file citations, including the honest demo-only gaps (demo passphrase distribution, header-only agent identity, self-held signing key) |
| OPENNESS.md | conformance re-run 44/44 on both wholesalers (the suite grew from 43); reports in docs/openness/ regenerated (no PIN inside) |
| ASSUMPTIONS, DECISIONS, deck | remaining placeholders closed with verified numbers |

## Gate runs
| When | Commit | pytest | scenarios.run_all | make accept |
|---|---|---|---|---|
| Sat 14 Nov 23:22-23:28 | 8d0d10e (on fac34f0) | 267 passed, 7 skipped, 2 xfailed (41 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP: live Gemini demo by design; keyed .env; conformance 44/44, MILK FLOW OK) |
| Sat 14 Nov 23:26-23:29 | 74b5e79 (rebased on 4ae14c6, S5 ui merged) | 285 passed, 7 skipped, 2 xfailed (58 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP) -> pushed to main |
| Sun 15 Nov 00:18 | 2468bdd (`fr8/valio/main`, per-approver PINs + fleet suites) | 743 passed, 7 skipped, 2 xfailed (94.7 s, 752 collected) | 8/8 PASS (live, 88 checks) | conformance 44/44 (2.3 s), `qa_milk_flow.py` MILK FLOW OK (73 audit events verified), keyless `aimo demo` placed in 5.95 s, keyless `--montage` 6/6 PASS in 2.6 s |
| Sun 15 Nov 00:28-00:31 | 81e730d (rebased on 89bc3db: S2 harden, S3 need v1+v2, S5 ui round 2 merged) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (34 s) | 8/8 PASS (live) | RESULT: PASS (15 PASS, 1 SKIP) -> pushed to main |
| Sun 15 Nov 01:56 | 2e6ae66 (`fr8/valio/docs`, S1 replay + fleet suites merged) | 707 passed, 105 skipped, 1 xfailed, 1 xpassed (79.06 s, keyless offline); 14 passed in 1.24 s (`test_docs_submission_hygiene.py` + `test_replay_port.py`) | 8/8 PASS (replay keyless, 88 checks) | keyless offline suite clean (0 failed); s0-s7 8/8 passed off bundled replay snapshot |
| Sun 15 Nov 02:29-02:31 | 738c93a on 2a68792 (`origin/main`, S1 replay merged) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | keyed RESULT: PASS (19 PASS, 1 SKIP); keyless fresh clone RESULT: PASS (16 PASS, 4 SKIP) -> pushed to main |
| Sun 15 Nov 02:41-02:44 | d13a07b on 738c93a (`origin/main`, one-page README, RUN.md, CONNECT.md, re-score) | 344 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | RESULT: PASS (19 PASS, 1 SKIP) -> pushed to main |
| Sun 15 Nov 02:48 | 6973c2d (`fr8/valio/docs` on `627c692`, S2 `/v1` fuzz + input hardening + doc reproducibility suites) | 1,150 passed, 105 skipped, 1 xfailed, 1 xpassed (117.61 s, 1,257 collected keyless offline, 0 failures) | 8/8 PASS (replay keyless, 88 checks) | keyless offline suite clean (0 failed); s0-s7 8/8 passed off bundled replay snapshot |
| Sun 15 Nov 03:08-03:15 | aef6b7d on b301d5a (`origin/main`, `tests` `0f51cf0` + `live` `b301d5a` merged) | 349 passed, 7 skipped, 1 xfailed, 1 xpassed (37 s) | 8/8 PASS (live) | keyed RESULT: PASS (19/0/1); keyless `git archive` of b301d5a RESULT: PASS (17/0/3, keyless pytest 250/0) -> pushed to main |
| Sun 15 Nov 04:30-04:33 | efbc173 on ddbeeba (`origin/main`, fleet ports: per-approver credentials, `unknown_customer` 403, request-text injection screen) | 357 passed, 7 skipped, 1 xfailed, 1 xpassed (32 s) | 8/8 PASS (live) | RESULT: PASS (19/0/1) -> pushed to main |
| Sun 15 Nov 05:00-05:08 | 9742758 / e97a367 on d4b0b41 (`origin/main`, judge panel fixes A-D + S4-1..S4-4) | 362 passed, 7 skipped, 1 xfailed, 1 xpassed (39 s) | 8/8 PASS (live) | keyed RESULT: PASS (19/0/1); keyless `git archive` of d4b0b41 RESULT: PASS (17/0/3, keyless pytest 261/0) -> S4-1 + S4-2 (`9742758`) and S4-3 + S4-4 (`e97a367`) pushed |
| Sun 15 Nov 05:11-05:19 | cfd5ed9 / 4ccc1fb on 7c290cf (`origin/main`, Gemini model fallback + S4-6..S4-8 + agent-run guard) | 366 passed, 7 skipped, 1 xfailed, 1 xpassed (keyless 265 passed / 0 failed; 371 passed on 4ccc1fb) | 8/8 PASS (live) | RESULT: PASS (19/0/1) -> quota docs, S4-6, S4-7, S4-8 video (`cfd5ed9`) and agent-run guard (`4ccc1fb`) pushed |
| Sun 15 Nov 03:18 | c98e72e (`fr8/valio/docs`, `RUN.md` + `CONNECT.md` + `OPENNESS.md` production path + fleet suites) | 1,168 passed, 105 skipped, 1 xfailed, 1 xpassed (121.71 s, 1,275 collected keyless offline, 0 failures); 10 passed in 0.97 s (`test_docs_submission_hygiene.py`) | 8/8 PASS (replay keyless, 88 checks) | keyless offline suite clean (0 failed); s0-s7 8/8 passed off bundled replay snapshot |

## Done (hourly re-check 1, Sun 15 Nov 00:28)
- S3 need: wind-tunnel headline now 0 policy / 0 allergen violations for the gateway, recall 99.7%, precision 68.2% (from 54.0%; 90% target not met). Naive column stays Sat 18:24 and is labelled as against the earlier policy. Updated SUBMISSION, GEMINI row 8, DEMO 2:45 line, deck slide 5, QA 15-16, DECISIONS D5, ASSUMPTIONS, NUMBERS, README pillar 3.
- Hero flow re-checked on 89bc3db in replay: still exactly 3 needs-approval rules (spend, substitution, 3.3x drift); split_order and price_jump pass.
- S2 harden: fuzz (0 server errors over ~2,600 requests) and quantity 422s in SUBMISSION, QA 13, NUMBERS; audit reports now label the simulated clock (DEMO 2:30 note).
- S5 round 2 (re-quote after the price lock, presenter fit): no claim in S4 docs affected.
- Test count 344 / 7 / 1 xfailed / 1 xpassed everywhere on standalone `main` at that checkpoint.

## Done (hourly re-check 3, Sun 15 Nov 02:28)
- S1 replay merged (`2a68792`, synced at `738c93a`): README env table, keyless vs keyed table, quickstart and `make accept` description rewritten (automatic replay, 669-product snapshot and catalog subset, 503 `catalog_missing`, bundled live fallback); DEMO step 1 and the Wi-Fi row drop `AIMO_REPLAY_FALLBACK`; NUMBERS, ASSUMPTIONS, OPENNESS, QA 13 and SUBMISSION updated.
- Own keyless check on `origin/main` `738c93a` (fresh clone, empty `.env`, key variables unset): RESULT: PASS, 16 PASS / 4 SKIP. The 8 catalog-dependent keyless failures on `738c93a` were then marked `@pytest.mark.needs_data` in `0f51cf0` ([tests.md](tests.md)), matching `fr8/valio/docs` where keyless offline `AIMO_OFFLINE=1 pytest -q` passes with 0 failures (`1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` across `1,275` collected tests on `c98e72e`).

## Done (Sun 15 Nov 03:15): V3 live montage and V2 tests reflected
- V3 live montage (`b301d5a`, [live.md](live.md), Sun 02:54 on current code): 6/6 PASS, $0.0352 per order (-57% vs $0.0822 before token trim), 53 calls, 38% cached, 5/34 escalated steps, school-allergen auto-approved by policy at €280 (`470889`), injection 6 supplier texts flagged, event €266.49; sanitized Sat ledgers (`montage-ledger-before-trim.jsonl`, `montage-ledger-after-trim.jsonl`) remain reproducible via `docs/evidence/montage_stats.py`.
- Keyless pytest (`0f51cf0`, [tests.md](tests.md)): test counts rose to 349 keyed / 250 keyless with 0 failures on `aef6b7d`, and keyless `make accept` reached `17 PASS, 0 FAIL, 3 SKIP`.

## Done (hourly re-check, Sun 15 Nov 04:28)
- Fleet port `ccab8f5` (per-approver credentials, ported from `fr8/valio/*`, synced in `ddbeeba` / `f5a333a`) verified on a replay gateway: Anna's passphrase signs in as Anna (200) and not as Mikko (401); Mikko's own works (200); 6 other approvers each get a 12-character passphrase (`approver_pins.json` / `AIMO_APPROVER_PINS`). README assumption 3, QA Q3 and DEMO step 2 updated; standalone `main` test count reached 357 (`256` keyless).

## Done for the CEO's S4 list (Sun 15 Nov 05:00-05:20, `d4b0b41` -> `9742758` -> `e97a367` -> `cfd5ed9`)
- **S4-1 numbers and behaviour sync (`9742758` on Judge panel fixes A-D `a99692f` + `d4b0b41`):** synced standalone `main` test counts (`362` keyed / `261` keyless), final-code wind-tunnel row (`wt640` re-scored on `a99692f` with committed inputs in `docs/evidence/windtunnel/`: 0 policy / 0 allergen violations, JSON matching headline), operator-only demo override headers (`X-Aimo-Now` / `X-Aimo-Faults` ignored on agent requests unless `AIMO_DEMO_FAULTS=1`), partial-stock requote at approval (`409 requote_needed` with `reason: partial_stock` instead of silent partial `CA`), and `need.product_query` prompt-injection screening alongside `need.raw_text` and `PlanLine.injection_screen`.
- **S4-2 SUBMISSION video and public-URL rows (`9742758`):** closed both rows in `docs/SUBMISSION.md` with local keyless 3-command evaluation (`make accept`, `uv run aimo serve` -> `http://localhost:8000/ui`) and committed backup video (`docs/demo-backup.mp4`).
- **S4-3 exception matrix (`e97a367`):** 9-row rubric exception matrix added to `docs/RUN.md` (every cited command run and passing on `d4b0b41`; the two `aimo demo` rows quoted from a truly keyless `git archive` export).
- **S4-4 QA 21-22 and PLAN refresh (`e97a367`):** added QA Q21 (precision: 154 unnecessary escalations, not the 144 in the request; 37 deliberate stops) and QA Q22 (simulated clock / fake stock provenance), and refreshed `docs/PLAN.md` questions.
- **Gemini quota fallback and S4-6..S4-8 (`7c290cf`, `33203b5`, `cfd5ed9`, `4ccc1fb`):** reported shared key `429 RESOURCE_EXHAUSTED` on `gemini-3.8-flash` at 05:05; `7c290cf` added automatic `GEMINI_FALLBACK_MODELS` failover (`gemini-3.5-flash`, `gemini-4-hydrogen`), complementing the keyless scripted buyer (`aimo/buyer/mock_model.py`); `33203b5` and `cfd5ed9` documented quota failover (`docs/RUN.md`, QA Q23), wind-tunnel data prerequisites (S4-6), honest injection recall (S4-7), and narrated walkthrough video (`docs/pitch/demo-walkthrough.mp4` + `docs/pitch/build_walkthrough.sh`, S4-8, `366` keyed / `265` keyless); `4ccc1fb` closed the `/v1/agent/runs` fault/transport guard and required human sign-in on agent runs (`371` passed).

## Keyless re-score before `aimo-v1` (Sun 15 Nov 02:37-02:55, handed over by the bake-off review session)
On two `git archive` exports of `main` `738c93a`, no `.env`, every key variable unset (probe scripts from `~/junction-aimo-team/review-probes/aimo/`, retargeted to local ports; `session_v2.py` sends `{"approver": ...}`):
| Check | Result |
|---|---|
| `make accept` keyless | RESULT: PASS, 16 pass / 0 fail / 4 skip (`17 pass / 0 fail / 3 skip` after `0f51cf0`); scenarios 8/8 (replay), conformance 44/44, MILK FLOW OK, Peppol AP, audit verify ok (20 events) |
| Original self-approval exploit (agent headers + anna + remember) | 403 `agent_cannot_approve`; also with lowercase header and with the correct PIN |
| Wrong, missing, duplicated or query/body PIN; tamper without credential | 401 |
| Rotating X-Forwarded-For / X-Real-IP / Forwarded | stays locked: 429 (298/300, 30/30, 30/30) |
| Brute force | 498 of 500 guesses get 429 after the lock; the credential is a 12-character passphrase |
| anna's session deciding as mikko | 403 `approver_mismatch` (and per-approver passphrases in `ccab8f5` / `fr8/valio/*` reject cross-approver sign-in with 401) |
| Forged session tokens (truncated, flipped, garbage v1, v1 prefix, approver swapped) | 401 x5 |
| MCP create_quote / place_order without identity (HTTP: none, agent-only, customer-only; stdio) | `identity_required`; unknown agent -> `scope_missing`; unknown customer -> `403 unknown_customer` (`5e599fd` / `fr8/valio/*`) |
| PIN length (`judge_accept.sh`, `run_all.py`, generated file) | 12 characters, mode 600 |
| PIN / session token in logs, audit, JS, 9 endpoints | not found |
| 2.5 ST; bad `/v1/agent/runs` body; search and plan with no data | 422; 422; 503 `catalog_missing` |
| Known, documented demo scope | known `X-Customer-Id` without `AIMO_AGENT_KEYS` uses header-only demo agent identity (documented in `docs/ASSUMPTIONS.md` A8 and `docs/pitch/QA.md` Q4/Q11; unknown customer ids return 403 `unknown_customer`) |
| Learned swap after "remember" | the swap no longer escalates as a substitution; spend and drift still do |

## Closed items and requests across sessions
- **S1 `replay` (`2a68792` / `627c692`):** `fallback_dir()` defaults automatically to `tests/fixtures/snapshot/replay` (`replay.BUNDLED_DIR`); keyless `make accept` and `scenarios.run_all` pass 8/8.
- **S2 `harden` + S3 `need` + `tests` (`0f51cf0` / `627c692`):** all 8 catalog-dependent tests (`tests/test_harden.py` x7, `tests/test_need.py::test_split_orders_escalate_once_they_pass_the_auto_limit`) marked `@pytest.mark.needs_data`, so keyless pytest exits 0 on both `origin/main` (`261 passed, 0 failed` at `9742758`) and `fr8/valio/docs` (`1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` at `c98e72e`).
- **S5 `ui` (`dfe58ef` / `627c692`):** masked approver sign-in panel replaced plain-text prompts; voice confirm, `409 requote_needed` diff card, clarifying-question card, and refreshed screenshots in `docs/reports/frontend/` synced with `DEMO.md` and `README.md`.
- **Judge panel fixes A-D (`a99692f`, `d4b0b41`, `9742758`, `e97a367`):** operator-only `X-Aimo-Now`/`X-Aimo-Faults`, `partial_stock` 409 requote, final-code wind-tunnel score and committed evidence, `product_query` injection screening, `docs/RUN.md` + `docs/CONNECT.md`, and SUBMISSION video/public-URL rows all closed.
- **Gateway demo agent identity:** header-only when `AIMO_AGENT_KEYS` is unset (known customers only; OAuth 2.1 client credentials documented as Phase 3 production path in `docs/OPENNESS.md` and `docs/ASSUMPTIONS.md` A8).

## Verification on `fr8/valio` (`c98e72e`, Sun 15 Nov Helsinki)
- `AIMO_REPLAY=1 AIMO_OFFLINE=1 PYTHONPATH=. /tmp/fleet-root/shared/junction-aimo-agents-venv/bin/python -m scenarios.run_all`: booted fresh keyless gateway on bundled replay snapshot, **8/8 passed** across 88 checks (`s0_guardrails` 12, `s1_milk` 11, `s2_recipe_order` 11, `s3_event_substitution` 9, `s4_stockout` 8, `s5_allergen_safe` 10, `s6_proactive_seller` 14, `s7_drift` 13).
- `AIMO_OFFLINE=1 PYTHONPATH=. /tmp/fleet-root/shared/junction-aimo-agents-venv/bin/python -m pytest -q tests/test_docs_submission_hygiene.py`: **10 passed in 0.97s**.
- `AIMO_OFFLINE=1 PYTHONPATH=. /tmp/fleet-root/shared/junction-aimo-agents-venv/bin/python -m pytest -q`: **1,168 passed, 105 skipped, 1 xfailed, 1 xpassed in 121.71s** across `1,275` collected tests (0 failures keyless offline: all catalog-dependent tests skip cleanly without `data/valio_aimo_junction26_product_data.json`; full 17,546-product catalog JSON on `2468bdd` had `743 passed, 7 skipped, 2 xfailed`).
