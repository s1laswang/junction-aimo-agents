# Acceptance scenarios s0–s7

These scenarios are ported from Momin's `junction26-valio` (`scenarios/s0..s7`, `_common.py`, `run_all.py`, credited to him) and rewritten to run against **our** `/v1` HTTP API. Each scenario is a script with named checks. The runner boots a fresh gateway on a scratch data dir and prints a PASS/FAIL table.

## Run it
```bash
uv run python -m scenarios.run_all                  # boot fresh gateway (free port 88xx, scratch data dir), run s0..s7
uv run python -m scenarios.run_all --only s2 -v     # one scenario, every check and narrative line
uv run python -m scenarios.run_all --base http://127.0.0.1:8000   # against a server you already run
uv run python -m scenarios.s6_proactive_seller --base http://127.0.0.1:8000   # a single script
AIMO_LIVE=1 uv run pytest -q tests/test_scenarios_momin.py -k live   # the same suite as a pytest test
uv run pytest -q tests/test_scenarios_momin.py      # offline unit tests (fake offers, temp state DB)
```
- **Requirements:** `AIMO_API_KEY` in `.env`, because offers come from the live Valio Aimo mock API, and `data/catalog.sqlite` (run `uv run aimo serve` once to build it).
- **Scratch data dir:** the runner symlinks the data files, catalog, embeddings and cache into a temp dir. The state DB and signing key there are new for each run, so no one's state is touched. `--keep` keeps the dir, including `server.log`.
- **App the runner serves:** `scenarios.app_with_extras:app`, which is our gateway plus the ported extras. Once the 2-line hook in `aimo/gateway/extras.py` is applied to app.py and mcp_server.py, this is identical to `aimo serve`.

## Result (Sat 14 Nov, ~19:00 Helsinki, live mock API)
| | Scenario | What it shows | Checks |
|---|---|---|---|
| PASS | s0_guardrails | 1,000 L milk (€1,010) is over the €500 limit, so the order goes to `pending_approval` (procurement_manager) and appears in the inbox. A tampered quote_hash gets 409. An agent as approver gets 403. A named human approves and the order is placed (AP). Unregistered `mallory` gets 403 when quoting. The audit chain verifies. | 11 |
| PASS | s1_milk | Hero flow: the plan covers 1,000 L and every line keeps FREE_FROM_LACTOSE. The usual 463038 is rejected because its cut-off passed. Offers carry live provenance. A third party verifies the quote against `/.well-known/jwks.json`. The decision has rule hits, the audit report is readable, and MCP lists the core and ported tools. | 11 |
| PASS | s2_recipe_order | "Lasagne for 120 on Friday at Hotel Aurora" becomes 8 ingredient Needs. Quantities scale with portions and the hotel's lactose-free house dairy is applied. One batched offers call feeds an 8-line signed quote (€341). Verdict: **needs_approval** (out-of-scope categories, unverified substitutes), then the procurement manager approves and the order is placed. | 11 |
| PASS | s3_event_substitution | Carbonara for 400 on the next delivery day. The planned cream is stocked out with `X-Aimo-Faults`, so the planner substitutes (with reasons), the gateway re-verifies the tiers, and a human approves before placement. | 9 |
| PASS | s4_stockout | An order is placed, then the supplier stocks out. The re-quote gets 409 `lines_unavailable` naming the product. A re-plan avoids it and still covers 200 L, and the replacement order goes through policy. The drift watcher flags the egg stock-out in plain language. | 8 |
| PASS | s5_allergen_safe | School (milk and nuts excluded): every planned product is structured FREE of both. The pancake recipe swaps milk to **oat drink**, not lactose-free milk. A forced milk order is rejected with a plain-language reason. Hospital: bread is structured gluten-free only, and a forced gluten order is rejected ("contains gluten … coeliac patients"). | 10 |
| PASS | s6_proactive_seller | The seller proposes a cheaper equivalent (464306 for 463038, saves about €11/week) with a why and a non-binding policy preview (needs_approval). The seller gets 403 on accept (`seller_cannot_order`) and 403 on quoting. The buyer accepts, which produces a signed quote and a policy decision, and the order belongs to `buyer-1`. A second accept gets 409. Audit shows `agent:valio-aimo/seller-1` and the refused attempt. A milk product proposed to the school previews as reject. | 14 |
| PASS | s7_drift | Milk volume +50% ("8 → 12 cases"). A croissant price shock (+12%, injected) is flagged with route approval, and the faulted price never becomes the baseline. School and hospital flags are plain language, allergen flags never route to auto, checks are audit-logged, and an unknown customer gets 404. | 13 |

**8/8 passed, about 11 s in total.** The suite was run several times. Upstream is the live mock API, so a cut-off can move results: after Sun 17:00 Monday delivery closes for stock milk, and s0, s1 and s4 then need a later date (`next_monday()` in `_common.py`).

## Differences from Momin's suite
- **API and identity:** his checks used his API (`/v1/needs/parse`, `X-Agent-Key`, his customers `hospital-tays` / `school-hervanta`). Ours use `X-Customer-Id` / `X-Agent-Id`, `Need` → `/v1/plan`, signed `Quote` → `quote_hash` orders, and our customers.
- **s4:** his `/v1/admin/simulate` stock-out moved a placed order back to pending. We have no post-placement reaction, so s4 shows the refusal at re-quote, the re-plan and the drift watcher, using our `X-Aimo-Faults`.
- **s2:** his needed an LLM-free parser endpoint. Ours uses the recipe planner (`POST /v1/recipes/plan`) directly.

## Openness: conformance suite and a second wholesaler
Besides s0–s7, which test *our* behaviour, the conformance suite tests *the profile*. Any wholesaler gateway can be checked from the outside with the suite's own crypto checks. See [docs/OPENNESS.md](OPENNESS.md).
```bash
uv run python -m conformance --base http://127.0.0.1:8000                        # Valio Aimo gateway: 43/43 PASS
uv run uvicorn toywholesaler.app:app --port 8820                                 # Nordic Bakery Supply (synthetic, offline)
uv run python -m conformance --base http://127.0.0.1:8820 --query "rye bread"    # 43/43 PASS
uv run pytest -q tests/test_openness.py                                          # the same, offline, in pytest
python -I scripts/verify_quote.py quote.json https://<gateway>/.well-known/jwks.json   # offline verifier
```
| | Check group | Valio Aimo | Nordic Bakery Supply |
|---|---|---|---|
| D1–D10 | otap.json fields and protocol, endpoints, MCP URL, JWKS (Ed25519, RFC 7638 kid, no private material), OpenAPI paths | PASS | PASS |
| M1–M4 | MCP tools/list, profile tools, input schemas with required arguments and descriptions, no approval tool | PASS | PASS |
| T1–T3, Q1–Q7 | search, offers with provenance, signed quote verified independently against JWKS, hash binding, tamper detection, EdDSA header, price lock | PASS | PASS |
| O1–O7, P1 | wrong hash gets 409, intruder agent gets 403, other customer gets 403, policy decision with plain-language hits, intruder can't consume the quote, idempotent, Peppol AP/CA/RE | PASS | PASS |
| A0–A5, P2 | escalation to approval, inbox, an agent can't approve, the buyer agent's own id can't approve, approval bound to the hash (409), human approval leads to placed with Peppol codes | PASS | PASS |
| U1–U5 | /v1/audit/verify, signed readable trail, each event re-hashed and its signature verified, agent and human/system actors, full chain from genesis | PASS | PASS |

The Gemini buyer agent's run against Nordic Bakery Supply, with no code changes (only `--base-url` / `AIMO_MCP_URL` differ), is in [docs/openness/buyer-run-nordic-bakery.log](openness/buyer-run-nordic-bakery.log).
