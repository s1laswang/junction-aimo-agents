# Combining the two Valio builds

Sat 14 Nov 2026. Branch `combined` in `s1laswang/junction-aimo-agents` = Silas's `main` (the submission, judge score 85/100, issue 13 on agent-arena) plus our `junction26-valio` (imported under `ours/` with its full 525-test history), developed together on `fr8/valio/*`. Nothing from either side is deleted. The work now is to finish porting our strong parts into `aimo/`, close every remaining judge gap, and PR `fr8/valio/main` -> `combined` -> `main`.

## Side by side

| | Silas's build (`aimo/`) | Ours (`ours/`) |
|---|---|---|
| Size | 14.8k Python lines in `aimo/` (27.7k across 136 `.py` files, 241 tracked files; 157 commits on `main`, 834 on `fr8/valio/main`) | 6.5k app/scenario Python lines (51.1k incl. tests across 133 `.py` files, 176 tracked files; 349 pre-merge commits) |
| Tests | 752 collected in `tests/` at `2468bdd` (`pytest -q` at `cfd6c6b`: 739 passed, 7 skipped, 2 xfailed across 748 collected; standalone `main` before fleet suites: 285 passed, 7 skipped, 2 xfailed; judge run on prior `main`: 169 passed) | 924 test functions in `ours/tests/` (525 passed in standalone `ours/` harness incl. scenarios) |
| Run | `uv run aimo serve` (one command, downloads data when `AIMO_API_KEY` is set); `make accept` for keyless judge check | `make demo` (boots, runs 8 scenarios, keeps a stage server) |
| Agent interface | MCP (13 tools in both `build_mcp().list_tools()` and README), OpenAPI 3.1, OTAP discovery manifest, JWKS | MCP (12 tools) + REST, agent card, JWKS |
| Buyer | Gemini buyer agent (escalates to gemini-4-hydrogen) + scripted/mock offline fallback when `GEMINI_API_KEY` is absent (`0f86383`, `a5e3881`) | Rule-based parser (en/fi, works offline) + scripted buyer, seller and drift agents |
| Trust | Ed25519 JWS quotes, hash-chained signed audit, Peppol AP/CA/RE responses, second wholesaler (`toywholesaler`), conformance suite (44/44) | Ed25519 JWS quotes, single-use redeem, hash-chained audit with verify, mandates per customer |
| Guardrails | Approval gate, human-only approvals with per-approver credentials + per-identity lockout (`433fbb1`, `0d96f61`, `599a991`), customer allergen hard-block in `plan_need` (`a4fa4fc`), 403 for self/stranger/wrong role, 409 on hash mismatch, injection v2 screen (`a6c21c8`) | Per-customer agent keys (X-Agent-Key), mandates (per-order + weekly limits, allergens, substitution policy), reason codes, approval queue |
| Exceptions | Cut-off, partial stock, stock-out, drift, event scenario (s0-s7 8/8), chaos/replay fallbacks (`0b33695`) | Split across products/dates, good-better-best substitutes with an allergen hard filter, stock-out simulator, 8 scenarios offline with a mock Valio |
| Gemini | Embeddings, Gemini allergen hints, Gemini buyer (all with keyless fallbacks) | None needed (optional LLM path) |

## Strong parts to keep

Silas: the whole product shape (OTAP, Peppol, conformance, toywholesaler, swarm), the Gemini buyer (Gemini prize), one-command `aimo serve`, the approval UI, the polished README.

Ours: everything that works with no key, strict input models, per-customer auth and mandates, allergen hard filter in substitution, offline end-to-end harness with a mock Valio server, a large test suite.

## Port plan (ours -> aimo/), mapped to the judge's lost points

| # | Port | From | Fixes judge point | Status on `fr8/valio/*` |
|---|---|---|---|---|
| 1 | Input validation: `quantity: float = Field(gt=0)` and the policy `quantity <= 0` check, clean 4xx instead of 500 | `ours/aimo/models.py:111`, `ours/aimo/policy.py:50` | Robustness: qty 0 gives 500, qty -5 gives 200 (+2) | **DONE (`8f61ea4`, `502572e`)**: Need and lot inputs validated before planning and ranking (`quantity <= 0` rejected cleanly with 4xx, `tests/test_plan_input_validation.py`, `tests/test_quantity_validation.py`) |
| 2 | Offline buyer: rule-based parser + scripted buyer as the fallback when `GEMINI_API_KEY` is absent, so `aimo demo` finishes | `ours/aimo/parse.py`, `ours/agents/buyer.py` | End-to-end: demo stops without a key (+3) | **DONE (`0f86383`, `a5e3881`)**: scripted keyless buyer, config aliases, `judge_accept.sh` keyless `aimo demo`, profile `usual_match` (initial mock buyer in `6449bf5` / `502572e`) |
| 3 | Allergen hard-block inside planning (school planner must never return whole milk) | `ours/aimo/substitute.py`, `ours/aimo/policy.py` | User need (+2) | **DONE (`a4fa4fc`, `3d88d0e`)**: customer policy allergen hard-block enforced inside `plan_need`, with GS1 EU14 alias mapping and Nordic Bakery Supply / school tests |
| 4 | Known customer on mutating `/v1` routes (per-customer keys / known customer check) | `ours/aimo/api.py`, `ours/aimo/mandates.py` | Autonomy and controls: identity is a demo header (+1) | **DONE (`0a792f0`)**: known customer enforced across `/v1`, catalog search, MCP and `/v1/agent/runs` (`aimo/gateway/auth.py`, `tests/test_auth_known_customer.py`) |
| 5 | Offline e2e harness + mock Valio server so all scenarios and tests run with no key and no live API | `ours/tests`, `ours/scenarios` | Reproducibility, craft | **DONE (`502572e`, `0f86383`)**: `scripts/judge_accept.sh`, `FakeGateway`, and `toywholesaler` MCP e2e run full keyless acceptance |
| 6 | Hash-chained audit verify, if stronger than the current signed audit | `ours/aimo/audit.py` | Integrity story | **DONE**: `aimo/trust` runs append-only hash-chained + Ed25519-signed audit verified by `GET /v1/audit/verify` |

## Weak spots of the combined system (fix after porting)

1. **[DONE: `0f86383`, `a5e3881`]** `aimo demo` must finish with no Gemini key (port 2): scripted/mock buyer and `judge_accept.sh` finish end-to-end without `GEMINI_API_KEY`.
2. **[DONE: `8f61ea4`, `502572e`]** Bad input must never 500 (port 1), plus fuzz tests on every `/v1` route: `Field(gt=0)` plus pre-planning Need and lot input validation (`tests/test_plan_input_validation.py`, `tests/test_quantity_validation.py`, `tests/test_edge_cases.py`); squad `inputs` syncing `team/harden` (`cf6534e`) fuzz suite.
3. **[DONE: `a4fa4fc`, `3d88d0e`]** School planner allergen leak (port 3): `plan_need` now hard-blocks products matching customer policy allergens before returning candidates or substitutes.
4. **[DONE: `0a792f0`]** Identity is a demo header; require a known customer (port 4): `aimo/gateway/auth.py` requires a known customer across `/v1`, catalog search, MCP and `/v1/agent/runs` (plus optional `X-Agent-Key`).
5. **[DONE: `70289e6`]** Injection-screen result is only shown on the order decision; show it on plan lines: `injection_screen` is surfaced directly on `PlanLine`, `Candidate`, and `verify_lines` (`aimo/catalog/screening_bridge.py`, `tests/test_plan_injection_screen.py`).
6. **[DONE: `502572e`, squad `docs`]** README says 9 MCP tools, server has 13: README and architecture diagram state 13 MCP tools, matching `build_mcp().list_tools()`.
7. **[DONE: `0b33695` / `502572e`]** Approved alternatives always need a human, even for a 10 EUR line: gateway learned approvals (`4e6d82d`: `remember` / `remember_scope`, `/v1/customers/{id}/learned`) let customers pre-authorize recurring substitutions within policy.
8. **[DONE: `0a792f0`]** Catalog search works without a customer header: catalog search, `/v1` routes, MCP and buyer runs now enforce a known customer while discovery (`/.well-known/*`, `/healthz`) stays open.
9. **[DONE on `main`, active sync on `fr8/valio/*`]** `team/*` branches hold unmerged commits: 12 of 13 `origin/team/*` branches (plus `origin/justSilas-aimo-integration`) have `0` unmerged commits relative to `origin/main` (`89bc3db`), with only `origin/team/replay` (`f59603a`, 2 commits) ahead of `origin/main`; 9 `team/*` branches + `justSilas-aimo-integration` are already 100% in `combined` (`cfd6c6b`), `team/submit` (`92a7752`) is merged here on `fr8/valio/docs` (`2468bdd`), and `team/harden` (`cf6534e`), `team/ui` (`dfe58ef`), `team/need` (`89bc3db`), and `team/replay` (`f59603a`) are syncing into `fr8/valio/*` (see disposition table below).
10. **[DONE: `4ae14c6`, `74b5e79`]** README screenshot: updated to live S5 capture `docs/reports/frontend/s5-approval-signed-in.png` (refresh again at freeze if UI changes).
11. **[IN PROGRESS]** One test command for both halves until `ours/` is fully ported, then delete only what is truly duplicated, with a note in this file: root `pytest --collect-only -q` collects 752 tests at `2468bdd` (`cfd6c6b` full run: 739 passed, 7 skipped, 2 xfailed across 748 collected); `ours/` remains intact with nothing deleted yet.

## Disposition of stale team/* branches

Audited with `git rev-list --count origin/main..origin/<branch>` and `git rev-list --count origin/combined..origin/<branch>`. Twelve of 13 `origin/team/*` branches and `origin/justSilas-aimo-integration` have **0 unmerged commits** relative to `origin/main` (`89bc3db`), and `origin/team/replay` (`f59603a`) is 2 commits ahead of `origin/main`. Nine round-5 `team/*` branches (plus `justSilas-aimo-integration`) also have **0 unmerged commits** relative to `origin/combined` (`cfd6c6b`), `team/submit` (`92a7752`) is merged into `fr8/valio/docs` / `fr8/valio/main` (`2468bdd`), and the late-night session tips `team/harden` (`cf6534e`), `team/ui` (`dfe58ef`), `team/need` (`89bc3db`), and `team/replay` (`f59603a`) are syncing into `fr8/valio/main`.

| Remote branch | Tip SHA | Unmerged vs `origin/main` (`89bc3db`) | Unmerged vs `combined` (`cfd6c6b`) | Landed on `main` | Disposition |
|---|---|---:|---:|---|---|
| `origin/team/buyer` | `bf961e7` | `0` | `0` | `8b99450` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/catalog` | `67a3a90` | `0` | `0` | `e09055c` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/frontend` | `da3d6fe` | `0` | `0` | `67b810e` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/gateway` | `87d2d77` | `0` | `0` | `0b33695` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/pitch` | `59a44b6` | `0` | `0` | `50ea268` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/port` | `b333e1e` | `0` | `0` | `7313679` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/trust` | `2b21faf` | `0` | `0` | `a6c21c8` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/windtunnel` | `4dfb6ad` | `0` | `0` | `0f39945` / `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/justSilas-aimo-integration` | `6449bf5` | `0` | `0` | `502572e` | 100% merged on `main` and `combined`; safe to close/delete |
| `origin/team/submit` | `92a7752` | `0` | `4` (`0` vs `fr8/valio/docs`) | `92a7752` | 100% merged on `main`, `fr8/valio/docs`, and `fr8/valio/main`; safe to close/delete once PR #5 lands on `combined` |
| `origin/team/harden` | `cf6534e` | `0` | `5` (`1` vs `fr8/valio/docs`) | `cf6534e` | 100% merged on `main`; syncing into `fr8/valio/main` |
| `origin/team/ui` | `dfe58ef` | `0` | `6` (`2` vs `fr8/valio/docs`) | `dfe58ef` | 100% merged on `main`; syncing into `fr8/valio/main` |
| `origin/team/need` | `89bc3db` | `0` | `10` (`6` vs `fr8/valio/docs`) | `89bc3db` | 100% merged on `main` (S3 need v2); syncing into `fr8/valio/main` |
| `origin/team/replay` | `f59603a` | `2` | `11` (`7` vs `fr8/valio/docs`) | pending S1 push | Active S1 replay tip (`4ceeeca` + `f59603a`); syncing into `fr8/valio/main` |

All 9 round-5 `team/*` branches (`buyer`, `catalog`, `frontend`, `gateway`, `pitch`, `port`, `trust`, `windtunnel`, plus `justSilas-aimo-integration`) are 100% merged into both `main` and `combined`; `team/submit` (`92a7752`) is merged via `fr8/valio/docs`, and `team/harden`, `team/ui`, `team/need` (`89bc3db`), and `team/replay` (`f59603a`) are syncing into `fr8/valio/main`.

## Rules for the port

- `aimo/` is the product. Port into it; do not run two servers.
- Keep every Silas test green and add ours alongside.
- Delete code from `ours/` only after its function lives in `aimo/` with tests, and list it here.
- PR `combined` into `main` in small steps, each with tests passing.
