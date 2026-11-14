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

## Open items
- Re-check README, SUBMISSION, DEMO and QA against main every hour while the other sessions merge:
  - S1 replay: keyless `make accept` and the catalog subset will change the "Keyless vs keyed" table (catalog row) and NUMBERS "make accept".
  - S2 harden: fuzz tests change the test count; MCP URL / port handling may change README "Connect your own agent".
  - S3 need: escalation precision, split-order window and learned approvals change SUBMISSION robustness, QA 15–16, DECISIONS D5, deck slide 5.
  - S5 ui: the sign-in panel changes DEMO 2:00–2:20 wording and checklist step 2 (plain-text PIN prompt); new screenshots for README.
- At the freeze: re-run pytest and the ledger snapshot; paste the video URL and public URL (or the "runs locally" sentence) into SUBMISSION.

## Requests to other sessions
- **S1 replay:** consider defaulting `AIMO_REPLAY_FALLBACK` to `tests/fixtures/snapshot` when `data/windtunnel/snapshot` is missing. Today a live gateway on the laptop has no automatic fallback when Wi-Fi drops unless the env var is set (`aimo/backend/replay.py` `fallback_dir`). DEMO checklist step 1 sets it explicitly for now.
- ~~**S5 ui:** plain-text PIN prompt~~ resolved by S5's sign-in panel (masked PIN field), merged Sat 23:18; DEMO and README updated.
- **Gateway owner (no session owns it):** agent identity is header-only. Known and documented (ASSUMPTIONS A8, QA Q4); no change requested before the freeze.
