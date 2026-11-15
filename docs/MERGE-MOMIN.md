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

## Execution status (Sun 15 Nov)
All four planned ports from `junction26-valio` plus every judge-review gap port from `ours/` have landed on `combined` / `fr8/valio/*` (`aad9653` / `2e6ae66`; details and `team/*` branch disposition in [COMBINE.md](COMBINE.md)):
- **13 MCP tools** advertised by `build_mcp().list_tools()` and documented in `README.md` (the 9 core `otap/0.1` tools plus recipes, drift, and `propose_offer`; 0 approval tools).
- **Four feature ports live in `aimo/`:** menu -> recipe -> order (`aimo/recipes/`, `plan_recipe`), proactive selling agent (`aimo/agents/seller.py`, `propose_offer`), standing-order drift detector (`aimo/agents/drift.py`, `detect_drift`), and the `scenarios/s0..s7` acceptance suite (**8/8 PASS** both live and keyless in replay, 88 checks) plus `Dockerfile` and `NUMBERS.md` command discipline.
- **Judge-gap ports closed:** customer policy allergen hard-blocks enforced inside `plan_need`, pre-planning quantity validation (`quantity <= 0` rejected with 4xx before planning), known-customer authentication across `/v1`, catalog search, MCP and buyer runs, human-only approvals with per-approver credentials (`approver_pins.json`, `X-Approver-Session`, and per-`(approver, client)` lockout), keyless scripted buyer fallback in `uv run aimo demo`, and `injection_screen` surfaced on `PlanLine` and `Candidate`.
