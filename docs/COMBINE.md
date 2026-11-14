# Combining the two Valio builds

Sat 14 Nov 2026. Branch `combined` in `s1laswang/junction-aimo-agents` = Silas's `main` (the submission, judge score 85/100, issue 13 on agent-arena) plus our `junction26-valio` (branch `fr8/valio/main`, 525 tests passing) under `ours/` with its full history. Nothing from either side is deleted. The work now is to port our strong parts into `aimo/`, then fix the weak spots below, then PR `combined` into `main`.

## Side by side

| | Silas's build (`aimo/`) | Ours (`ours/`) |
|---|---|---|
| Size | 15.8k Python lines, 125 commits, 162 files | 8.6k Python lines, 349 commits |
| Tests | 126 test functions (judge: 169 passed) | 255 test functions (525 passed incl. scenarios) |
| Run | `uv run aimo serve` (one command, downloads data) | `make demo` (boots, runs 8 scenarios, keeps a stage server) |
| Agent interface | MCP (README says 9 tools, server has 13), OpenAPI 3.1, OTAP discovery manifest, JWKS | MCP (12 tools) + REST, agent card, JWKS |
| Buyer | Gemini buyer agent (escalates to gemini-4-hydrogen), stops without a key | Rule-based parser (en/fi, works offline) + scripted buyer, seller and drift agents |
| Trust | Ed25519 JWS quotes, signed audit, Peppol AP response, second wholesaler (toywholesaler), conformance suite (43/43) | Ed25519 JWS quotes, single-use redeem, hash-chained audit with verify, mandates per customer |
| Guardrails | Approval gate, 403 for self/stranger/wrong role, 409 on hash mismatch, injection forces approval | Per-customer agent keys (X-Agent-Key), mandates (per-order + weekly limits, allergens, substitution policy), reason codes, approval queue |
| Exceptions | Cut-off, partial stock, stock-out, drift, event scenario (s0-s7 8/8) | Split across products/dates, good-better-best substitutes with an allergen hard filter, stock-out simulator, 8 scenarios offline with a mock Valio |
| Gemini | Embeddings, Gemini allergen hints, Gemini buyer | None needed (optional LLM path) |

## Strong parts to keep

Silas: the whole product shape (OTAP, Peppol, conformance, toywholesaler, swarm), the Gemini buyer (Gemini prize), one-command `aimo serve`, the approval UI, the polished README.

Ours: everything that works with no key, strict input models, per-customer auth and mandates, allergen hard filter in substitution, offline end-to-end harness with a mock Valio server, a large test suite.

## Port plan (ours -> aimo/), mapped to the judge's lost points

| # | Port | From | Fixes judge point |
|---|---|---|---|
| 1 | Input validation: `quantity: float = Field(gt=0)` and the policy `quantity <= 0` check, clean 4xx instead of 500 | `ours/aimo/models.py:111`, `ours/aimo/policy.py:50` | Robustness: qty 0 gives 500, qty -5 gives 200 (+2) |
| 2 | Offline buyer: rule-based parser + scripted buyer as the fallback when `GEMINI_API_KEY` is absent, so `aimo demo` finishes | `ours/aimo/parse.py`, `ours/agents/buyer.py` | End-to-end: demo stops without a key (+3) |
| 3 | Allergen hard-block inside planning (school planner must never return whole milk) | `ours/aimo/substitute.py`, `ours/aimo/policy.py` | User need (+2) |
| 4 | Known customer on mutating `/v1` routes (per-customer keys) | `ours/aimo/api.py`, `ours/aimo/mandates.py` | Autonomy and controls: identity is a demo header (+1) |
| 5 | Offline e2e harness + mock Valio server so all scenarios and tests run with no key and no live API | `ours/tests`, `ours/scenarios` | Reproducibility, craft |
| 6 | Hash-chained audit verify, if stronger than the current signed audit | `ours/aimo/audit.py` | Integrity story |

## Weak spots of the combined system (fix after porting)

1. `aimo demo` must finish with no Gemini key (port 2).
2. Bad input must never 500 (port 1), plus fuzz tests on every `/v1` route.
3. School planner allergen leak (port 3).
4. Identity is a demo header; require a known customer (port 4).
5. Injection-screen result is only shown on the order decision; show it on plan lines.
6. README says 9 MCP tools, server has 13.
7. Approved alternatives always need a human, even for a 10 EUR line: make it a mandate setting.
8. Catalog search works without a customer header: decide and document.
9. Five team/* branches hold unmerged commits (buyer +3, gateway +4, catalog +3, trust +1, windtunnel +12, 48 behind): merge or close each with a reason.
10. README screenshot TODO: re-capture from main after the freeze.
11. One test command for both halves until `ours/` is fully ported, then delete only what is truly duplicated, with a note in this file.

## Rules for the port

- `aimo/` is the product. Port into it; do not run two servers.
- Keep every Silas test green and add ours alongside.
- Delete code from `ours/` only after its function lives in `aimo/` with tests, and list it here.
- PR `combined` into `main` in small steps, each with tests passing.
