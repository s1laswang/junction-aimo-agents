# Port report: Momin's junction26-valio → junction-aimo-agents

## Swarm D (openness / conformance), Sat ~19:30: done
- **Conformance suite** in `conformance/`. Run it with `uv run python -m conformance --base <url> [--query ...] [--json out.json]`.
  - It covers 43 checks: otap.json, JWKS (Ed25519, RFC 7638 kid), OpenAPI paths, MCP tools/list names and schemas, and that there is no approval tool.
  - It verifies the quote JWS and hash binding with its own crypto code, never `aimo`'s, and a tampered quote must fail.
  - Orders: a wrong hash gets 409, an intruder agent and another customer get 403, and the policy decision must come with plain-language hits.
  - Approvals: escalation to approval, an agent can't approve (403), a wrong hash on the approval gets 409, and a human approval places the order. Peppol AP/CA/RE codes are checked.
  - Audit: /v1/audit/verify plus an independent re-hash and signature check of the full chain from genesis.
  - It prints a table, writes JSON, and exits with the number of FAILs.
  - Results: **Valio gateway 43/43, Nordic Bakery Supply 43/43** (reports in `docs/openness/`).
- **Second wholesaler** in `toywholesaler/`: "Nordic Bakery Supply", with 30 synthetic products, its own Ed25519 key, state DB, audit chain and policies.
  - It reuses the aimo gateway, trust and catalog unchanged, with a static `BackendClient` and the product data in the same JSON shape.
  - It runs offline. `tests/test_openness.py` boots it and runs conformance (4 tests).
- **Zero-code agent run:** the Gemini buyer ordered 40 kg rye bread from Nordic over MCP using only `--base-url`.
  - Nordic's own policy gave needs_approval; anna approved and the order was placed.
  - Log: `docs/openness/buyer-run-nordic-bakery.log`.
- **Offline verifier** `scripts/verify_quote.py` (about 30 lines, cryptography only). It handles a quote, an evidence pack or a list of events, and takes keys only from the JWKS you pass. A Nordic quote fails against Valio's JWKS.
- **Docs:** `docs/OPENNESS.md` is new, and `docs/SCENARIOS.md` has a new openness section. The Dockerfile now also copies conformance/ and toywholesaler/.
- **Portability gaps for the owners** (listed in OPENNESS.md; not fixed, not my files):
  - Offer provenance has `source: "valio-aimo-mock-api"` hard-coded (`backend/client.py`).
  - "signed by Valio Aimo" in `trust/policy.py:197`, and "Valio Aimo issued…" / "placed with Valio Aimo" in `trust/approvals.py`.
  - The buyer profile seeds Valio ids.
- The quote-poisoning finding below was fixed on main by gateway round 4 ("only the quoting agent may submit"). Conformance O2 and O5 now test it.


Branch `team/port`. Last updated Sat 14 Nov, around 19:00 Helsinki. Merged origin/main at d2fe641 and later. **Items 1–5 are done.** Full pytest: 155 passed, 1 skipped (the live wrapper). Scenarios: **8/8 PASS against the live mock API.**

Source: a read-only snapshot of Momin's `junction26-valio` (branch dev, 75dbbed) at `/tmp/momin-review/junction26-valio`. Nothing on his side was touched. Every ported file credits him in its header, and the recipe and seller payloads say so in `provenance`.

## Hook for the CEO (2 lines, both idempotent)
```python
# aimo/gateway/app.py, in create_app(), right after app.include_router(build_router()):
from aimo.gateway.extras import router as extras_router; app.include_router(extras_router)
# aimo/gateway/mcp_server.py, in build_mcp(), right before `return mcp`:
from aimo.gateway.extras import register_mcp_tools; register_mcp_tools(mcp)
```
- **Before the hook:** `uvicorn scenarios.app_with_extras:app` serves the gateway with the extras. The scenario runner uses this.
- **After the hook:** plain `aimo serve` has them too. The Dockerfile uses `aimo serve`, so it needs the hook for the extras.
- **interfaces.md:** please append the "Port additions" section below. I may not edit it.

## What was ported, from where, and what changed
| # | Our file | From Momin | Changes |
|---|---|---|---|
| 1 | `aimo/recipes/recipes.json` | `data_extra/recipes.json` | Verbatim: 11 dishes, en/fi aliases, illustrative per-cover quantities, swap rules. |
| 1 | `aimo/recipes/planner.py` | `aimo/recipes.py` (`menu_to_need`, `recipe_swaps`, `_dishes_in`, `_default_covers`) | See "Recipes" below. |
| 2 | `aimo/agents/drift.py` | `agents/drift.py` (`SEEDS`, `drift_rules`, `next_delivery`, thresholds 5 / 10 / 20 %) | See "Drift" below. |
| 3 | `aimo/agents/seller.py` | `agents/seller.py` (`usual_items`, `allowed`, `rank`, the "why" wording) | See "Seller" below. |
| 4 | `scenarios/_common.py`, `s0..s7`, `run_all.py` | `scenarios/*` | Rewritten for our `/v1` API, identity headers, signed quotes, `X-Aimo-Faults` and our customers. See docs/SCENARIOS.md. |
| 5 | `Dockerfile`, `.dockerignore` | `Dockerfile` | Uses uv with uv.lock, `aimo serve` on port 8000, a /app/data volume, a non-root user and a healthcheck. `.dockerignore` keeps `.env` and `data/` out of the image. |
| – | `aimo/gateway/extras.py` | (new) | HTTP router plus MCP tool registration. |
| – | `scenarios/app_with_extras.py` | (new) | The app with the extras, used until the hook lands. |
| – | `tests/test_scenarios_momin.py` | `tests/test_recipes.py`, `test_drift.py`, `test_seller.py`, `test_scenarios.py` (ideas only) | 17 offline tests plus 1 live wrapper (`AIMO_LIVE=1 … -k live`). |

**Recipes**
- Each ingredient becomes one of our `Need`s, and our `aimo.catalog.plan_need` plans it.
- All offers for a recipe come from ONE batched price + availability call. Pass 1 records the candidate shortlists, then one upstream call fetches the union, which is under 1,000 ids.
- Swap triggers read the customer's policy exclusions. For a milk allergy only an option that Momin's data marks "dairy free" is used. When no such option exists, the line is planned with milk excluded and flagged for a human.
- If the customer's usual milk is lactose-free (Hotel Aurora), the recipe plans lactose-free dairy, because the gateway requires that claim for same-kind lines.
- Swaps keep the ingredient's use ("grated cheese" becomes "lactose-free grated cheese").
- A category guard and a name guard per ingredient, both from codes observed in our catalog, stop bad matches: "milk" no longer plans a milk-system cleaner and "ground beef" no longer plans beef liver.

**Drift**
- His seeds are mapped to our customers, with ids checked in our catalog and against each policy's allergens:
  - His school milk moved to the hotel, because our school is milk-free. That gives "Milk volume crept up 50% over 8 weeks (8 → 12 cases)".
  - The school gets Oatly oat drink.
  - The hospital's oat flakes (contain gluten) became gluten-free oat muesli.
- Offers come in-process and honour `X-Aimo-Faults`.
- The price baseline is the first live price observed, stored in the state DB (`port_drift_baseline`). Faulted prices never become the baseline.
- Kinds are `price | volume | availability | allergen`. Messages are plain language, each flag has route `auto|approval`, and checks are audit-logged as `agent:valio-aimo/drift-watch-1`.

**Seller**
- Candidates come from our catalog (`find_candidates` + `rank_substitutes`) and are compared per L/KG using the API's comparison price, with one batched offers call.
- The seller can only **propose**. The proposal stores a non-binding preview of the customer's own policy on a draft quote (same `verify_lines` + `trust.evaluate`).
- Accepting is customer-side only and runs `make_quote` + `submit_order` under the buyer's identity, so the policy decides.
- `valio-aimo/*` agents get 403 `seller_cannot_order` on accept, and `scope_missing` on `/v1/quotes` because no policy gives them a mandate.
- Audit actor is `agent:valio-aimo/seller-1`. Proposals are stored in the state DB (`port_proposals`).

## Port additions (please append to docs/interfaces.md)
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/v1/recipes` | | `[{id, name, name_fi, aliases, ingredients[{query, per_portion, unit}], note}]` |
| POST | `/v1/recipes/plan` | `{recipe_id? , text?, portions?, delivery_by?, customer_id?, excluded_allergens[], lactose_free, gluten_free, include_candidates}` | `{recipe, customer_id, delivery_by, excluded_allergens, swaps[], ingredients[{ingredient, query, quantity, unit, swap, need, plan}], uncovered[], total_net_estimate, summary, quote_request{customer_id, delivery_date, lines[]}, provenance}`. Errors: 404 `recipe_not_found`, 400 `portions_required`, 503 upstream. |
| GET | `/v1/drift/{customer_id}` | `rebaseline?` | `{customer_id, delivery_date, standing_order, flags[{product_id, kind: price\|volume\|availability\|allergen, change_pct, message, route: auto\|approval, before?, after?, fix, product_name}], summary, provenance}`. 404 `no_standing_order`. |
| POST | `/v1/proposals` | `{customer_id, product_id?, qty_sales_units?, replaces_product_id?, delivery_date?, note}` + `X-Agent-Id: valio-aimo/<agent>` | proposal `{proposal_id, seller_id, kind: cheaper_equivalent\|seller_pick, status, product, replaces, qty_sales_units, offer, why[], estimated_saving_eur, policy_preview{binding:false, outcome, approver_role, hits}, seller_can_order:false, quote_request}`. 404 `no_better_offer` / `unknown_customer`, 409 `not_orderable`. |
| GET | `/v1/proposals` | `customer_id?, status?` | proposals, newest first |
| GET | `/v1/proposals/{id}` | | proposal |
| POST | `/v1/proposals/{id}/accept` | buyer headers | `{proposal, quote, order}`. 403 `seller_cannot_order` / `customer_mismatch`, 409 `not_open`. |
| POST | `/v1/proposals/{id}/decline` | `{reason}` | proposal |

MCP tools: `list_recipes()`, `plan_recipe(recipe_id?, portions?, delivery_by?, text?, excluded_allergens?)`, `check_standing_order_drift(customer_id?)`, `propose_offer(customer_id, product_id?, qty_sales_units?, replaces_product_id?, delivery_date?, note?)`. The buyer accepts a proposal over MCP with `create_quote(proposal.quote_request…)` + `place_order`.

Audit actions: `drift.checked`, `proposal.created`, `proposal.accepted`, `proposal.declined`, `proposal.accept_refused`.

## Scenario table (live mock API, Sat ~19:00)
```
PASS  s0_guardrails          11 checks
PASS  s1_milk                11 checks
PASS  s2_recipe_order        11 checks   lasagne x120 -> 8-line quote EUR 341 -> needs_approval -> approved -> placed
PASS  s3_event_substitution   9 checks
PASS  s4_stockout             8 checks
PASS  s5_allergen_safe       10 checks
PASS  s6_proactive_seller    14 checks
PASS  s7_drift               13 checks
8/8 passed
```
Command: `uv run python -m scenarios.run_all`. See docs/SCENARIOS.md.

## Verified vs untested
- **Verified:** offline unit tests, the live scenario run (several times), HTTP routes through the app, and MCP tool registration and listing over streamable HTTP (s1). The seller 403s and the drift messages were checked live.
- **Untested:**
  - The Dockerfile. It was not built locally, as instructed. It installs `uv>=0.9,<1` with pip, because uv.lock is lock revision 5 and the uv 0.5 image cannot read it.
  - The MCP tools' *call* path. Only listing was tested; the calls go through the same `do_*` functions as HTTP.

## Finding for the gateway owner (not fixed, outside my files)
**Quote poisoning: another agent of the same customer can burn a buyer's quote.**
- Repro, in-process, live: the buyer creates quote Q, then `submit_order(Identity("valio-aimo/seller-1", "hotel-aurora"), Q)`. The policy rejects it (agent mismatch, no mandate) but **stores a rejected order for Q**. The buyer's own `place_order(Q)` then returns that same rejected order, because orders are idempotent per quote.
- So a seller or rogue agent who learns a quote_id + hash can deny service, though it can never place an order.
- **Fix suggestion** (`service.submit_order`): return 403 `agent_mismatch` before creating an order when `ident.agent_id != quote.agent_id`, or make idempotency per (quote, agent).

## Decisions for Silas / CEO
1. Apply the 2-line hook and append the interface rows above (CEO).
2. Optional: add the drift and proposal cards to the UI. The frontend can call `/v1/drift/{customer}` and `/v1/proposals?customer_id=`.
