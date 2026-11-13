# Judging checklist

Run: `make demo` (boots uvicorn, runs `scenarios/run_all.py`), `.venv/bin/pytest -q`. Status column is checked by sh-qa after each dev merge; "pending" means the module is not on dev yet.

| Criterion (weight) | Proof | Where | Status |
|---|---|---|---|
| Working end-to-end demo (30) | Milk request through parse, offers, signed quote, order, approval, audit | `tests/test_flow.py::test_milk_flow`, `scenarios/s1_milk.py`, `agents/buyer.py` (`tests/test_buyer.py::test_milk_end_to_end`) | flow test skips until api.py lands |
| | Same flow over REST and MCP, same tools | `tests/test_mcp_parity.py::test_tool_parity`, `/.well-known/agent-card.json`, `/mcp` | pending mcp_server |
| | All six listed scenarios | `scenarios/run_all.py` (s1 milk, s2 need to recipe to order, s3 event substitution, s4 stock-out, s5 allergen-safe, s6 proactive seller, s7 drift) | pending api.py |
| | Live console with trace | UI `/` console, `web/console.*` | pending |
| Precise match to the ask (20) | Quantity, unit, date, lactose and allergen constraints parsed, English and Finnish | `tests/test_parse.py` (`test_milk_en`, `test_milk_fi_equals_en`, `test_allergens`, `test_gluten_fi`) | on dev |
| | Litres converted to sales units, case packs rounded up, shortfall reported | `tests/test_allocate.py` (`test_units_for_litre_packs`, `test_case_pack_rounds_up`, `test_split_across_products_reports_shortfall`) | on dev |
| | Offers all lactose-free, ranked on price and reliability | `tests/test_flow.py`, `tests/test_buyer.py::test_cheapest_wins`, `test_reliability_breaks_tie` | partly pending |
| | Recipe to ingredients | `tests/test_recipes.py`, `scenarios/s2_need_recipe_order.py` | recipes on dev |
| Autonomy and controls (20) | Mandate limits decide auto, needs_approval, blocked | `tests/test_policy.py` (`test_auto`, `test_over_limits`, `test_allergen_blocked_beats_limit`), `scenarios/s0_guardrails.py` | policy on dev |
| | Human approve, reject, wrong approver, expiry | `tests/test_approvals.py` | on dev |
| | Signed quotes: tamper, expiry, wrong agent, single use, public-JWKS verify | `tests/test_quotes.py`, `/.well-known/jwks.json`, s0 tampered jws check | on dev |
| | Hash-chained audit with plain-language why | `tests/test_audit.py::test_chain`, `tests/test_flow.py` (why non-empty, chain), `GET /v1/audit` | audit on dev, endpoint pending |
| | Approval inbox and audit screens | UI `web/inbox.*`, `web/audit.*` | pending |
| Robustness to exceptions (15) | Supplier stock-out, partial stock, split, substitute, new date | `scenarios/s4_stockout.py`, `scenarios/s3_event_substitution.py`, `tests/test_allocate.py` (closed cutoff, sales block) | allocate on dev |
| | Allergen-safe substitution (hospital, school) | `scenarios/s5_allergen_safe.py`, `tests/test_drift.py::test_mandate_allergen_blocks_and_skips_unsafe_alt`, `tests/test_policy.py::test_substitution_rules` | on dev |
| | Standing order drift: price, availability, quantity, late delivery | `tests/test_drift.py`, `scenarios/s7_drift.py` | on dev |
| Openness and path to production (15) | Open interfaces: REST/OpenAPI, MCP, agent card, JWKS | `/openapi.json`, `/mcp`, `/.well-known/*`, `tests/test_mcp_parity.py` | pending |
| | Documented path, assumptions, what is mocked | `docs/PRODUCTION.md`, `docs/ASSUMPTIONS.md`, `docs/ARCHITECTURE.md` | on dev |
| | Runs without an LLM key | rule-based parser, `tests/test_parse.py`; `run_all` strips `LLM_*` env | on dev |
| | Claims measured from our system | `pitch/NUMBERS.md`, `tests/test_pitch_numbers.py`, `pitch/measure_live.py` | on dev |
