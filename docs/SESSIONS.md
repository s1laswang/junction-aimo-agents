# Parallel Claude Code sessions: road from 77 to 100

Owner: CEO session. Started Sat 14 Nov ~23:15 Helsinki. Freeze **Sun 06:00**, then tag `aimo-v1`; submit 09:30.
Source of truth for scoring: the independent bake-off review (77/100 at 502572e), docs/PLAN.md "Scoring checklist",
and ~/junction-aimo-team/JUDGING.md.

The review scored each criterion (it was given before the self-approval fix, 433fbb1):

| Criterion | Score | Target |
|---|---|---|
| Demo | 23/30 | 30 |
| User need | 16/20 | 20 |
| Controls | 13/20 | 20 |
| Robustness | 12/15 | 15 |
| Openness | 13/15 | 15 |

## State of main at session start (Sat 23:00, 773e28e+)
- **Re-score: about 83/100** (Controls 18/20). Security is done; the remaining points are Demo and User need.
- **Approvals are human-only.** Humans sign in with `POST /v1/auth/approver-session {"approver": "<directory id or name>"}` + `X-Approver-Pin`, get a session bound to that person, and send it as `X-Approver-Session`.
  - Scripts may send `X-Approver-Pin` directly with an `approver` name.
  - Any request with `X-Agent-Id` on the decision route gets 403.
  - The lockout is per client, plus a global backstop that never blocks sessions.
- `DecisionRequest.approver` is now optional (it defaults to the session's approver).
- Quotes and orders (REST and MCP) need explicit identity.
- `/v1/customers` hides approver ids unless the caller is signed in.
- The UI signs in with two `window.prompt` calls (name, then PIN). S5 replaces them with a panel.

## Common rules (every session)
1. **Work directly. Do NOT spawn subagents, Task agents or workflows**: they burn too many tokens.
2. **Your own worktree and branch.** From the main checkout:
   ```
   cd ~/junction-aimo-team/junction-aimo-agents && git fetch -q origin
   git worktree add ~/junction-aimo-team/sessions/<NAME> -b team/<NAME> origin/main
   cd ~/junction-aimo-team/sessions/<NAME>
   ln -sf ~/junction-aimo-team/junction-aimo-agents/.env .env && mkdir -p data
   for f in ~/junction-aimo-team/junction-aimo-agents/data/valio_aimo_* ~/junction-aimo-team/junction-aimo-agents/data/catalog.sqlite ~/junction-aimo-team/junction-aimo-agents/data/embeddings* ~/junction-aimo-team/junction-aimo-agents/data/cache; do ln -sf "$f" data/; done
   uv sync -q --extra dev
   ```
   (If the worktree already exists, just `cd` into it and `git pull --rebase origin main`.)
3. **Only edit the files your section owns.** If you need a change elsewhere, write it under "Requests" in `docs/reports/<NAME>.md` and continue.
4. **The gate before anything reaches main.** All three must pass:
   - `uv run --extra dev pytest -q` (no new failures);
   - `uv run python -m scenarios.run_all` (8/8);
   - `make accept` (PASS).
5. **Self-merge when the gate is green.**
   ```
   git fetch -q origin && git rebase origin/main
   # re-run the gate
   git push origin HEAD:main
   ```
   If the push is rejected, rebase and repeat. Also push your branch (`git push -u origin team/<NAME>`). Merge small and often (every 45–60 min).
6. **Safety.**
   - Never print or commit secrets (.env, keys, the approver PIN).
   - No heavy compute on the laptop: no load tests or long sweeps.
   - The Valio mock API has undisclosed rate limits: record once, then replay.
   - Gemini calls go only through `aimo.gemini` (it logs usage).
7. **Contracts.** `aimo/models.py` and `docs/interfaces.md` are append-only; note any addition in your report. Approval decisions are human-only (`X-Approver-Pin`, see docs/TRUST.md "Human-only approvals"), and no session may weaken that.
8. **Reporting.** Keep `docs/reports/<NAME>.md` current: done (with evidence and commands), open items, requests. End every work block by telling Silas in one line what landed on main.
9. **No merging Momin's `combined` or `fr8/valio/*` into main.** Port single functions only, with credit in a code comment.
10. **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## S1 `replay`: keyless means fully working (Demo +4, Robustness +1)
**Owns:** `aimo/backend/client.py` (replay and offline paths only), `windtunnel/snapshot.py`, `tests/fixtures/snapshot/`, `scripts/judge_accept.sh`, `scripts/record_replay.py` (new), `Makefile`, the `/v1/products/search` error path in `aimo/gateway/app.py`, and `aimo/catalog/__init__.py` (no-data handling only).
1. **Full replay snapshot.** Record price and availability ONCE from the live mock API, politely (batched ≤1,000 ids, a few calls total), for every product id and date that s0–s7, the hero milk flow, the montage, conformance, toywholesaler checks and qa_milk_flow touch. Write `scripts/record_replay.py`, which collects ids by running the scenarios with a recording client. Commit it under `tests/fixtures/snapshot/`. Target: with no `AIMO_API_KEY`, all of `scenarios.run_all` passes **8/8 in replay**.
2. **Replay on automatically** when there is no AIMO_API_KEY (the gateway and `make accept`), with provenance `served_from: replay` shown in the UI header and in responses.
3. **No product data:** `/v1/products/search` and `/v1/plan` return **503 `catalog_missing`** with "run `uv run aimo fetch-data`" instead of a 500. Commit a small catalog subset fixture (only the products the replay ids need, from the public challenge files) so a keyless clone can run the demo end to end without the 87 MB file.
4. **No hard-coded `2026-11-16`.** In scenarios, demo and docs scripts, delivery dates derive from the (simulated) clock: the next feasible weekday ≥ cut-off. Use `AIMO_NOW` when set. The demo must work after Sun 17:00.
5. **`make accept` shows a real order:** it runs `scenarios.run_all` in replay plus qa_milk_flow, and prints the order id, the Peppol code and audit verify.
   **Reviewer finding (re-score 82, Sat 23:40):** conformance and qa_milk_flow in `scripts/judge_accept.sh` (around line 145) are still gated on `has_env AIMO_API_KEY`, so a truly keyless run gives 11 PASS / 5 SKIP. Gate them on "AIMO_API_KEY set OR AIMO_REPLAY set" instead. The CEO's earlier "15/15 keyless" claim was wrong (a key was in .env). Verify with a truly empty `.env` in a fresh clone.
**Done when:** a fresh clone with an empty `.env` runs `make accept` → PASS with 8/8 scenarios, and `uv run aimo serve` + `/ui` completes the hero flow in replay.

## S2 `harden`: no 500s, sane quantities, agent runs that never hang (Robustness +2, Demo +1)
**Owns:** `aimo/gateway/schemas.py`, the quote/plan validation in `aimo/gateway/service.py` (functions `make_quote` and plan input checks only), `aimo/buyer/runs.py`, the `aimo/cli.py` serve flags, and `tests/test_fuzz.py` plus `tests/test_harden.py` (new).
1. **Quantities.** Reject fractional quantities for count units (ST, KI, RAS, BOT...) unless allowedLotSize permits them (422 with a plain reason). Quantities beyond plausible stock (Terminal unknown stock or > 50× the standing baseline) are flagged `unknown_stock_large_qty` and need approval, never auto. Absurd values (> 1e6) get 422.
2. **Agent runs:** `POST /v1/agent/runs` with missing or invalid fields returns 422, never 500. The buyer's MCP URL derives from the port actually being served (set by `aimo serve --port` into the env; no localhost:8000 default). A run that can't reach the gateway fails fast with a clear error event, never a silent FakeGateway fallback and never a run that waits forever.
3. **Fuzz every `/v1` route** (hypothesis or table-driven): wrong types, missing fields, huge strings, unicode, negative numbers, unknown ids. Assert no 500 anywhere and a JSON error with `error` + `message`.
4. **Audit report clock:** the audit report and timestamps use one clock (the simulated clock when `AIMO_NOW` is set, otherwise real), labelled. Coordinate with S1 only through the clock module (`aimo/backend/clock.py`).
**Done when:** fuzz tests pass with 0 server errors and the reviewer's cases (2.5 ST, 1e9 units, bad run body, non-8000 port) all behave.

## S3 `need`: the agent does what the buyer asked, with the least correction (User need +4, Controls +3)
**Owns:** `aimo/catalog/plan.py`, `aimo/catalog/substitution.py`, `aimo/trust/policy.py`, `aimo/trust/policies/*.yaml`, `aimo/trust/learned.py`, and `tests/test_need*.py`.
1. **Allergen hard-block inside planning:** the customer's policy excluded allergens apply in `plan_need` even when the request doesn't repeat them. School "milk" must never plan a milk product: it gives an oat drink or an explicit shortfall. Port the logic from Momin's fleet branch `origin/fr8/valio/valio-allergen-w2` (`git show origin/fr8/valio/valio-allergen-w2:aimo/catalog/plan.py`). It was built on an older main, so re-apply it by hand onto current code and credit it.
2. **Fewer pointless escalations:** add the mandate setting `auto_approve_alternatives_below_eur` (e.g. hotel €50). A verified-best or customer-approved alternative under it auto-approves. Also auto-approve learned swaps ("always allow"). Measure escalation precision before and after on `windtunnel` wt640 (commands in docs/reports/windtunnel.md, pipeline mode, laptop OK: about 40 s). Target ≥90% at recall ≥99%.
3. **Split-order aggregation:** a rolling 7-day window per customer × product (and product type) counts toward spend and drift, so 3 × €399.96 milk escalates on the third order. Add a **price-jump** rule: >15% above the last paid price needs approval.
4. **Injection result on plan lines:** a supplier-text screen hit shows as a flag and reason on the plan line and candidate, not only in the order decision.
5. Re-run the wind tunnel pipeline score and put the before/after table in `docs/reports/need.md` and `docs/windtunnel-results.md`.
**Done when:** the school-milk leak is gone, a €10 pre-approved swap auto-places, split orders escalate, and escalation precision is measured and improved.

## S4 `submit`: every claim true, every number reproducible, judge-ready docs (Openness +2, Demo +1)
**Owns:** `README.md`, `docs/SUBMISSION.md`, `docs/GEMINI.md`, `docs/NUMBERS.md`, `docs/DEMO.md`, `docs/RUN.md`, `docs/CONNECT.md`, `docs/pitch/*`, `docs/ASSUMPTIONS.md`, `docs/DECISIONS.md`, `docs/OPENNESS.md`, `docs/demo-montage.md`, and `docs/reports/pitch.md`.
1. **Close every open placeholder** in SUBMISSION.md and GEMINI.md (6). Use the repo URL https://github.com/s1laswang/junction-aimo-agents (public), and document the video and public-URL fields with exact submission entries (`docs/demo-backup.mp4` and local keyless `make accept` / `uv run aimo serve`). Commit a **sanitized montage ledger** (counts, timings, models, no prompts or keys) under `docs/evidence/` so the Gemini numbers are reproducible.
2. **README truth pass:** test counts from an actual run, the 13 MCP tools, the approver PIN step (where to find it, `AIMO_APPROVER_PIN`), keyless vs keyed paths, `make accept`, and the `aimo-v1` tag note. Delete stale claims.
3. **DEMO.md:** the 3-minute script updated for the PIN sign-in moment ("a human signs in, agents never can"), the tamper demo, replay mode when Wi-Fi drops, and `AIMO_NOW` for after Sun 17:00. Add a 30-second "permissions attack" beat: the agent tries to approve as anna, gets 403, and the audit shows the refusal.
4. **NUMBERS.md:** every number with its command, re-run where cheap. Mark numbers that need the live key.
5. **Judge Q&A** (`docs/pitch/QA.md`): 20 hard questions (permissions, data confidentiality, production path, why MCP, failure modes, Gemini) with short answers citing files.
**Done when:** a judge reading README → SUBMISSION → DEMO finds no false or stale claim, and every number has a command.

## S5 `ui`: finish the stopped UI work and the voice approval (Demo +2, User need +1)
**Owns:** `web/`, `aimo/voice/` (new), and `tests/test_voice*.py`.
**Start from the stopped work:**
- frontend: `~/junction-aimo-team/junction-aimo-agents/.claude/worktrees/agent-a382899e20328ded2` (uncommitted: clarifying-question card, "always allow this swap", re-quote diff, photo intake, sustainability badges, ops tab, recipes tab, blocked-attacks feed, mostly mock);
- voice: `.../worktrees/agent-aa314fa9909b572cb` (early).

Copy the useful parts into your worktree, then wire them to the live endpoints that now exist on main:
- `needs_clarification` with `POST /v1/agent/runs/{id}/answer`;
- `remember` on decisions plus `/v1/customers/{id}/learned`;
- the 409 `requote_needed`;
- image in `/v1/agent/runs`;
- `/v1/drift`, `/v1/proposals`, `/v1/recipes`;
- audit `approval.refused` for the blocked-attacks feed.
1. **The approver sign-in** (`web/api.js` asks for the PIN) should look intentional: a small "Sign in as approver" panel instead of `window.prompt`.
2. **Voice approval (ElevenLabs).**
   - Build `aimo/voice/` (TTS approval prompt, STT answer, intent via `aimo.gemini`). If `ELEVENLABS_API_KEY` is absent, fall back to browser Web Speech.
   - Voice must still go through the human decision endpoint with `X-Approver-Pin` and the quote hash, show the transcript, and need one confirm click.
   - Mount it with a 2-line, guarded try/except hook in `aimo/gateway/app.py`; that is the only line you may add there.
3. **Screenshots:** fresh ones into `docs/reports/frontend/` for README and deck.
**Done when:** every new backend feature is visible and usable in `/ui` (presenter mode too), and voice approval works, with ElevenLabs if the key is present or with the browser fallback.

---

## Completion status (Sun 15 Nov)

All parallel sessions (`S1`-`S5`, `tests`, `live`), Judge panel fixes A-D (`a99692f`, `d4b0b41`, `9742758`, `e97a367`, `7c290cf`), and `fr8/valio/*` gap closures have landed and been synced onto `fr8/valio/docs` (`c98e72e`), where keyless offline `AIMO_OFFLINE=1 pytest -q` passes `1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` across `1,275` collected tests with 0 failures (`743 passed, 7 skipped, 2 xfailed` with full 17,546-product catalog JSON on `2468bdd`; `362 passed` keyed / `261 passed, 0 failed` keyless on `origin/main` `9742758`, `366 passed` after `7c290cf`) and keyless replay `scenarios.run_all` passes **8/8** (88 checks):
- **S1 `replay` (`2a68792`, ported on `627c692`; [docs/reports/replay.md](reports/replay.md)):** recorded replay snapshot plus a 669-product bundled catalog subset (`tests/fixtures/snapshot/replay/`), automatic keyless replay, simulated clock (`AIMO_NOW`), `503 catalog_missing` after 422 input validation, fresh-clone keyless `make accept` `RESULT: PASS` (`17 PASS, 0 FAIL, 3 SKIP` after `0f51cf0`), and keyless `scenarios.run_all` passing **8/8** (88 checks).
- **S2 `harden` (`cf6534e`, ported on `627c692`; [docs/reports/harden.md](reports/harden.md)):** `/v1` fuzz suite (`tests/test_fuzz.py`, 0 server errors across ~2,600 requests), pre-planning `quantity <= 0` guard (`Field(gt=0)` -> 422 before catalog/upstream calls, `tests/test_plan_input_validation.py`), count-unit fractional and absurd/implausible quantity rules (`tests/test_harden.py`), fail-fast agent runs on the served port, and a single audit clock.
- **S3 `need` (`89bc3db`, `6edf3fc`, ported on `627c692`; [docs/reports/need.md](reports/need.md)):** customer policy allergen hard-block inside `plan_need`, supplier-text `injection_screen` surfaced on `PlanLine` and `Candidate` (`PlanLine.injection_screen`), catalog-verified tiers, learned swaps, `auto_approve_alternatives_below_eur`, rolling 7-day `split_order` and `price_jump` rules, and wind-tunnel `wt640` updated to 0 policy / 0 allergen violations (68.2% precision at 99.7% recall).
- **S4 `submit` (`81e730d` -> `d13a07b` -> `9742758` -> `e97a367`, synced on `c98e72e`; [docs/reports/submit.md](reports/submit.md), [docs/reports/pitch.md](reports/pitch.md)):** one-page `README.md` with 13 MCP tools (zero approval tools), `docs/RUN.md` (keyless runbook + 9-row rubric exception matrix), `docs/CONNECT.md` (external agent configs), `docs/OPENNESS.md` production path, sanitized montage ledgers in `docs/evidence/`, command-reproducible numbers in `docs/NUMBERS.md`, closed video/public-URL rows in `docs/SUBMISSION.md`, and judge Q&A Q1-Q22 in `docs/pitch/QA.md`.
- **S5 `ui` (`dfe58ef`, ported on `627c692`; [docs/reports/ui.md](reports/ui.md)):** approver sign-in panel, ElevenLabs + Web Speech voice approval intent (`aimo/voice/`), `409 requote_needed` diff card, clarifying-question card, learned swaps, ops/recipes/blocked-attacks feed, and refreshed screenshots in `docs/reports/frontend/`.
- **Session `tests` (`0f51cf0`; [docs/reports/tests.md](reports/tests.md)):** marked all 8 catalog-dependent tests `@pytest.mark.needs_data` so keyless pytest exits 0 (`261 passed, 0 failed` on `origin/main` `9742758`; `1,168 passed, 0 failed` on `fr8/valio/docs` `c98e72e`) and `judge_accept.sh` enforces `PASS pytest (keyless)`.
- **Session `live` (`b301d5a`; [docs/reports/live.md](reports/live.md)):** re-recorded V3 live montage on current `main` (6/6 PASS, 53 Gemini calls, $0.0352 per placed order, 38% cached, human-only approvals) and wired keyless scripted buyer (`aimo/buyer/mock_model.py`, `aimo demo --scenario ...`) onto the real in-process replay gateway with `Verdict [<scenario>]:` lines and 15 s bounded wait.
- **Fleet ports, Judge panel fixes A-D (`5e599fd`, `ccab8f5`, `fc63fc8`, `a99692f`, `d4b0b41`, `9742758`, `7c290cf`) and `fr8/valio/*` gap closures:** known-customer `403 unknown_customer` across `/v1` and MCP, per-approver 12-char credentials (`approver_pins.json` / `AIMO_APPROVER_PINS`), Fix A (`X-Aimo-Now` / `X-Aimo-Faults` ignored on agent requests unless `AIMO_DEMO_FAULTS=1`), Fix B (`409 requote_needed` with `reason: partial_stock` when stock drops below quoted quantity at approval, never silent partial `CA`), Fix C (wind tunnel re-scored on final code with committed inputs under `docs/evidence/windtunnel/`, honest opt-in XSD test, stray conformance JSON removed + gitignored), Fix D (`need.raw_text` and `need.product_query` prompt-injection screening), and automatic `GEMINI_FALLBACK_MODELS` failover when a Gemini model exhausts daily quota.
