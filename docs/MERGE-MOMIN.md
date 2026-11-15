# junction-aimo-agents vs Momin's junction26-valio: recommendation

CEO, Sat 14 Nov ~20:00. I reviewed Momin's code from a read-only clone of `freighter:~/momin/fleet/repos/junction26-valio.git` (branch `dev`, 373 commits, mostly 13 Nov). I didn't touch anything on the fleet side.

## Recommendation
**Submit junction-aimo-agents (s1laswang/junction-aimo-agents, main).** Port in, with credit, the four things Momin has that we don't (see "What to bring over" below). Point the fleet's `valio` team at our repo for scenarios, tests and docs, if Silas approves.

Why ours is the base:
1. **Gemini is essential in ours and optional in Momin's.**
   - Momin's runs with "No LLM key needed": an optional OpenAI-compatible `chat()` picks an offer id.
   - Ours uses Gemini throughout: the buyer agent (function calling over MCP, with flash escalating to gemini-4-hydrogen HIGH), structured need parsing, gemini-embedding-2 hybrid search over 17.5k products, allergen inference for 4,076 products, recipe-compatibility checks, injection screening and wind-tunnel generation.
   - That matters for the Google DeepMind prize, and it helps "solves the need with minimal correction".
2. **Our demo is richer.** The approval UI streams the live agent and checks the quote signature in the browser via JWKS/WebCrypto. It also has good-better-best cards, a live tamper demo (red, then restore, then green) and a presenter mode.
3. **Our trust layer was red-teamed and hardened today.**
   - Approvals come only from humans in the directory, and agents can never approve.
   - Tier and flags are computed server-side, and the policy re-verifies them.
   - Identity in headers wins over the request body, and quotes re-fetch prices live.
   - 123 tests pass.
4. **Ours is on GitHub with a one-command run.** Momin's lives only on freighter's bare repo.

What Momin's does as well or better:
- **The scenario suite.** `scenarios/s0..s7` covers every brief scenario as a runnable acceptance script with checks; `make demo` runs all 8 and they pass.
- **Numbers discipline.** `pitch/NUMBERS.md` only states measured numbers, each with the command that produced it.
- **Packaging.** A Dockerfile, a public-URL plan and MCP client setup docs.

## Side by side
| | junction-aimo-agents (ours) | junction26-valio (Momin) |
|---|---|---|
| Mock API | live price/availability, cache, retry, fault injection | live, with a frozen clock for the cut-off |
| Interface | REST /v1 + MCP (9 tools, HTTP + stdio) + otap.json + JWKS | REST + MCP (HTTP + stdio) + discovery |
| Policy | YAML per customer, 11 rules, plain-language hits | mandates (per-order limit, weekly budget, allergens) |
| Substitution | good-better-best, allergen tri-state, Gemini allergen inference, embeddings | ranked offers, allocate/split |
| Audit | hash-chained, Ed25519-signed per event, readable report, evidence pack | tamper-evident audit trail |
| Agents | Gemini buyer (MCP), streaming runs | rule-based buyer, **proactive seller**, **standing-order drift** |
| Scenarios | hero + stockout, school, hospital, injection, drift, event (montage) | **s0–s7, all 8 brief scenarios, incl. recipe → order** |
| UI | rich SPA at /ui | server-rendered console, inbox, audit |
| LLM | Gemini essential | optional, none by default |

## What to bring over (with credit to Momin)
1. **Menu → recipe → order:** `data_extra/recipes.json` and `aimo/recipes.py`, exposed as a `plan_recipe` MCP tool and a buyer scenario.
2. **Proactive selling agent:** `agents/seller.py`, adapted to our API. The seller proposes, and the buyer's policy decides.
3. **Standing-order drift detector:** `agents/drift.py` (price creep, volume creep), feeding our `quantity_drift` rule and a UI card.
4. **Acceptance suite:** `scenarios/s0..s7` ported as tests against our `/v1` API, plus `pitch/measure_*` and the NUMBERS.md method. Also the Dockerfile.

## Ask to Silas
Approve: (a) ours as the submission; (b) porting the four items above; (c) optionally retargeting the fleet's `valio` team to open `fr8/valio/*` PRs against junction-aimo-agents.

## Fleet ports into main (Sun 15 Nov, ~03:45, merge session)
Hand-ported from the fleet's `fr8/valio/*` branches (PR #6, built on `combined`, so it does not merge onto main directly). Credit to the fleet workers named in each commit:
- `0a792f0` (valio-auth-w2), read-route part: unknown customer gets 403 `unknown_customer` on `POST /v1/plan`, `POST /v1/offers`, `GET /v1/products/search` and the MCP `search_products`, `get_offers`, `plan_need` tools. Agent keys and the buyer-run checks were not ported.
- `0d96f61` + `599a991` (valio-auth-lead): per-approver credentials. Anna keeps `data/keys/approver_pin`, the other approvers get their own passphrases in `data/keys/approver_pins.json`, and a credential only acts as its own approver. Per-identity lockout was not ported; main's per-source lock and global backstop stay.
- `70289e6` (valio-auth-w5): `injection_screen` on candidates and plan lines, with flagged lines named in the plan explanation, built on main's existing `plan.mark_supplier_text`.
- Two fixes in the same session, not from the fleet: the policy injection rule now also screens the buyer's request text (`need.raw_text`), and day formatting no longer uses `%-d` (fails on Windows), with `tzdata` declared.
Tests: 357 passed with the catalog, 256 keyless (was 349 / 250). Keyless `make accept` gives PASS=17 FAIL=0 SKIP=3. Scenarios 8/8, conformance 44/44.

## Execution status (Sun 15 Nov)
All four planned ports from `junction26-valio` plus every judge-review gap port from `ours/` have landed across `origin/main` (`9742758` -> `7c290cf` -> `33203b5` -> `cfd5ed9` -> `4ccc1fb` -> `2fe8975` -> `7225d06` -> `f3c5996` -> `02575d8` -> `62e8384`; `207` commits / `38` ahead of `80c978a` at `62e8384` [`origin/main` and `origin/team/submit` tip], `206` / `37` ahead at `02575d8`, `205` / `36` ahead at `f3c5996`, `204` / `35` ahead at `7225d06`, `203` / `34` ahead at `2fe8975`: fleet hand-ports `5e599fd`, `ccab8f5`, `6edf3fc`, `ddbeeba` plus Judge panel fixes A-D in `a99692f`, `d4b0b41`, `9742758`, `e97a367`, Gemini quota fallback + honest injection recall in `7c290cf` + `33203b5`, narrated walkthrough video in `cfd5ed9`, `/v1/agent/runs` fault/transport guard + human sign-in in `4ccc1fb` + `2fe8975`, final policy listed pack swaps + auto-only `quantity_drift` window + wind-tunnel `69.5%` precision / `145` unnecessary escalations / `74.8%` outcome accuracy + `110 s` / `109.72 s` `docs/pitch/demo-walkthrough.mp4` frame 3 two-reason narration in `7225d06` + `f3c5996`, raw live Gemini hero evidence pack `docs/evidence/live-run/` via `AIMO_GEMINI_TRACE` + offline `scripts/verify_quote.py` + per-run `judge_accept.sh` temp folder in `02575d8`, and S4-9 `README.md` + `docs/SUBMISSION.md` live-run links in `62e8384`; `375 passed` keyed / `274 passed` keyless on `main` at `02575d8` and `62e8384` [`371 / 270` at `f3c5996`, `7225d06`, and `2fe8975`, `366 / 265` at `cfd5ed9`, `362 / 261` at `9742758`, `357 / 256` at `ddbeeba`]) and `combined` / `fr8/valio/*` (`80c978a` [`925` commits] / `fr8/valio/main` `c379f53` [`1,145` commits, `77,335` Python lines across `205` `.py` files and `355` non-`ours/` tracked files (`531` total), up from `039d2fa` at `1,123` commits / `2,369` collected tests and `0f0ece1` at `1,088` commits / `1,931` collected] / `fr8/valio/docs` `1d0b238` [`1,161` commits, `2,448` collected tests via `pytest --collect-only -q` across `550` tracked files (`374` non-`ours/`, `78,434` Python lines across `208` `.py` files), merging `c379f53` at `1497e82` plus `w3` `bfd4698`, `w4` `7dedf7a`, and `w5` `41b5b79` on top of `450743d` (`2,393` collected across `546` tracked files: `w2` `8cf02ca`, `w4` `82d6b2c`, `w5` `c4ef8fd`, `w6` `5e071da`), with the 3 prior `4436f9b` cross-squad merge regressions closed (`42 passed, 2 skipped`), following `1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` across `1,275` collected on `c98e72e`]; `8/8` replay `scenarios.run_all` across `92` checks, `44/44` conformance on both wholesalers, and `Dockerfile` bundling `tests/fixtures/snapshot` for keyless container replay; 16-branch `team/*` + 5-branch `wip/rescue-1115/*` disposition in [COMBINE.md](COMBINE.md)):
- **13 MCP tools** advertised by `build_mcp().list_tools()` and documented in `README.md` (the 9 core `otap/0.1` tools plus recipes, drift, and `propose_offer`; 0 approval tools).
- **Four feature ports live in `aimo/`:** menu -> recipe -> order (`aimo/recipes/`, `plan_recipe`), proactive selling agent (`aimo/agents/seller.py`, `propose_offer`), standing-order drift detector (`aimo/agents/drift.py`, `detect_drift`), and the `scenarios/s0..s7` acceptance suite (**8/8 PASS** both live and keyless in replay, 92 checks) plus `Dockerfile` (bundling `tests/fixtures/snapshot` for keyless container replay), `110 s` (`109.72 s`) narrated walkthrough video (`docs/pitch/demo-walkthrough.mp4`, `cfd5ed9` / `2fe8975` / `f3c5996` / `41b5b79` / `82d6b2c`), offline-verifiable live Gemini evidence pack (`docs/evidence/live-run/`, `02575d8` / `62e8384` / `8cf02ca`, `tests/test_live_run_evidence.py`), and `NUMBERS.md` command discipline.
- **Judge-gap and panel fixes closed:** customer policy allergen hard-blocks enforced inside `plan_need`, pre-planning quantity validation (`quantity <= 0` rejected with 4xx before planning), known-customer authentication across `/v1`, catalog search, MCP and buyer runs (`0a792f0`, `5e599fd`), human-only approvals with per-approver credentials (`approver_pins.json`, `X-Approver-Session`, `0d96f61` + `599a991`, `ccab8f5`), human sign-in required before starting, answering, or listing `/v1/agent/runs` plus operator-only body `faults`/`inproc`/`fake` transports (`4ccc1fb`, `2fe8975`, `tests/test_run_api_guard.py`), keyless scripted buyer fallback in `uv run aimo demo` (`b301d5a`) plus `GEMINI_FALLBACK_MODELS` quota failover (`7c290cf`, `33203b5`), `injection_screen` surfaced on `PlanLine` and `Candidate` plus `need.raw_text` and `need.product_query` with honest recall synced (`70289e6`, `6edf3fc`, `fc63fc8`, `a99692f`, `33203b5`), operator-only `X-Aimo-Now` / `X-Aimo-Faults` (`AIMO_DEMO_FAULTS=1`) and partial-stock `409 requote_needed` (`a99692f`), final-code policy + wind-tunnel re-score (`d4b0b41`, `9742758`, `7225d06`, `f3c5996`, `5e071da`), and live-run evidence + report/PR hygiene (`02575d8`, `62e8384`, `8cf02ca`, `bfd4698`, `7dedf7a`, `41b5b79`, `c4ef8fd`, `5e071da`).
