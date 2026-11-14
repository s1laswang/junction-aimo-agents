# Numbers we may say

Rule: a number is only said if it has a source command that was run. Unmeasured = NOT FOUND, never guessed.
Regenerate: `.venv/bin/python pitch/measure_data.py` (reads `data/` files only, never the Valio API). Verified on 2026-11-14 against the files in `data/`.

## From local data files (measured)

| Claim | Value | Key in output |
|---|---|---|
| Products in catalog file | 17,546 | products |
| Marked deleted | 0 | deleted |
| Declared lactose-free (claim FREE_FROM_LACTOSE) | 4,178 | lactose_free_claim |
| Declared low lactose (LOW_LACTOSE) | 321 | low_lactose_claim |
| Declared gluten-free | 3,049 | gluten_free_claim |
| Vegan claim | 2,959 | vegan_claim |
| Products with an allergen CONTAINS or MAY_CONTAIN list | 7,205 | with_allergen_declared |
| Products with a FREE_FROM allergen list | 5,119 | with_free_from_declared |
| Products with either kind of allergen info | 9,436 (53.8% of 17,546) | with_any_allergen_info |
| Products declared to contain milk (AM, CONTAINS) | 3,160 | with_milk_allergen_contains |
| Lactose-free AND name has milk or maito (rough name match, fi/en) | 70 | lactose_free_name_has_milk_or_maito |
| Categories | 349 | categories |
| Products with an English name | 17,544 | names_en |
| Delivery reliability rows | 4,425 (all in catalog) | reliability_rows |
| Reliability: min / p10 / median / mean | 0.0 / 0.936 / 1.0 / 0.972 | reliability_* |
| Reliability exactly 1.0 | 2,490 | reliability_perfect_1 |
| Reliability below 0.9 | 291 | reliability_below_0.9 |
| CO2 rows (category average, kg CO2-eq per kg) | 11,033 (all in catalog) | co2_rows |
| CO2 min / median / max | 0.13 / 2.11 / 30.46 | co2_* |

Caveats to say out loud if asked: lactose-free is the supplier's declared claim, not our judgement. Allergen info is missing on 8,110 products (17,546 minus 9,436), so "no allergen listed" is not "safe". Reliability covers 4,425 products, CO2 covers 11,033, both are partial. The 70 is a name match and is a rough floor, the real milk-like set is decided by the catalog module.

## From challenge brief (not measured by us, attribute to Valio)

| Claim | Source |
|---|---|
| 20,000 kitchens, 25,000 products | AIMO.md challenge text |

## From our running system (measured 2026-11-14 on fr8/valio/pitch, local gateway, rule parser, warmed Valio cache)

Command: `AIMO_CACHE_DIR=/tmp/opencode/aimo-cache .venv/bin/uvicorn aimo.app:app --host 127.0.0.1 --port 8201` (fresh temporary `AIMO_DB` and `AIMO_KEY_DIR`) then `.venv/bin/python -m pitch.measure_live --url http://127.0.0.1:8201` (also runnable in-process via `.venv/bin/python -m pitch.measure_live --inprocess`).

| Customer | mandate ms | parse ms | offers ms | quote ms | order ms | approvals ms | audit ms | offers | total net | outcome | reasons | audit events |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hotel-aurora | 16 | 48 | 3632 | 10 | 8 | 2 | 4 | 1 | 1332.0 | auto | WITHIN_MANDATE | 6 |
| hospital-tays | 3 | 3 | 19 | 3 | 7 | 2 | 4 | 1 | 1332.0 | auto | WITHIN_MANDATE | 6 |
| school-hervanta | 2 | 4 | 17 | 3 | 6 | 2 | 4 | 1 | 1332.0 | needs_approval | OVER_ORDER_LIMIT | 4 |

- Milk request (`"I need 1,000 litres of lactose-free milk by Monday"`): `auto 2, needs_approval 1, blocked 0` of 3 customers. Median step latency `4 ms` over 21 calls.
- First `offers` call is `3,632 ms` because the worker loads and indexes the 17,546-product catalog file on first search (and uncached Valio API calls add a few seconds when cold). Warm subsequent `offers` calls are `19 ms` and `17 ms`. Say "a few seconds on first load, under 20 ms warm", not "4 ms".
- Picked milk product: `464119` `Valio rasvaton maito 1 l laktoositon UHT` (`sales_unit "KI"`, `10 L`), `100 x 10 L = 1,000 L` at `13.32 EUR` net per 10 L case (`1.332 EUR/L`), totaling `1,332.00 EUR` net / `1,512.00 EUR` gross (`units_needed=100.0`, `unit_price_net=13.32`, `total_net=1332.0`, `total_gross=1512.0`).
- Mandate decisions on `1,332.00 EUR` net: `hotel-aurora` (`2,500.00 EUR` per-order limit) and `hospital-tays` (`1,500.00 EUR` per-order limit) pass automatically (`WITHIN_MANDATE`, 6 audit events each including simulated confirmation); `school-hervanta` (`600.00 EUR` per-order limit) is routed to human approval (`OVER_ORDER_LIMIT`, 4 audit events, held in pending approvals).
- Ranked `good` / `better` / `best` alternatives returned alongside the primary offer (`alternatives=3`):
  - `good`: `464401` `Valio Hyvä suomalainen Arki® Eila® rasvaton maitojuoma 1 l laktoositon` (`1,100.00 EUR` net / `1,250.00 EUR` gross)
  - `better`: `464073` `Valio täysmaito 1 l laktoositon UHT` (`1,640.00 EUR` net / `1,860.00 EUR` gross)
  - `best`: `464201` `Valio kevytmaito 1 l laktoositon UHT` (`1,560.00 EUR` net / `1,770.00 EUR` gross)
- Stock number in the Valio mock response is `9999999999`. That is the mock's sentinel, never say it as real warehouse stock.
- Interface and planning counts (`.venv/bin/python -m pitch.measure_system`): `12` MCP tools (identical over HTTP `/mcp` and stdio), `15` OpenAPI paths (`12` under `/v1/*` with `15` HTTP operations, plus `/health`, `/.well-known/agent-card.json`, `/.well-known/jwks.json`), `11` recipe dishes in `data_extra/recipes.json`, `3` seeded mandates (`hotel-aurora`, `hospital-tays`, `school-hervanta`), `8` scenario scripts (`scenarios/s*.py`), `5` UI views (`web/*.html`), and `6` smoke checks (`scripts/smoke_demo.py`).
- Search and parse precision (`.venv/bin/python scripts/eval_precision.py --holdout3`; `AIMO_EVAL=1 .venv/bin/pytest -q tests/test_precision*.py`): `set1 20/20, holdout 10/10, holdout2 10/10, total 40/40` (`27 passed, 13 xpassed` across `test_precision*.py`) on the core benchmark suites, and `holdout3 9/15` on the harder unseen holdout3 suite (`49/55` overall).
- End-to-end scenarios (`AIMO_CACHE_DIR=/tmp/opencode/aimo-cache .venv/bin/python scenarios/run_all.py --base http://127.0.0.1:8202 --boot`, verified on `63649fc`): `7/8 passed` (`s0_guardrails`, `s1_milk`, `s2_need_recipe_order`, `s3_event_substitution`, `s4_stockout`, `s5_allergen_safe`, `s6_proactive_seller` pass; `s7_drift` fails on `injected stock-out is flagged for that product`).
- Automated test suite (`.venv/bin/pytest -q` on `63649fc`): `525 passed, 44 skipped` (`0 failed`).

Still NOT FOUND: latency with a live LLM key configured, `8/8` scenarios passing once `s7_drift` is fixed.
