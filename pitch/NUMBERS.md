# Numbers we may say

Rule: a number is only said if it has a source command that was run. Unmeasured = NOT FOUND, never guessed.
Regenerate: `python3 pitch/measure_data.py` (reads data/ files only, never the Valio API). Run on 2026-11-13 against the files in `data/`.

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

## From our running system (NOT FOUND until measured after 04:00)

| Claim | Status | How to measure |
|---|---|---|
| Latency per step (parse, offers, quote, order) | NOT FOUND [MEASURE] | time each REST call against uvicorn on a warm cache, 10 runs, report median |
| Scenarios passing of 6 | NOT FOUND [MEASURE] | `pytest -q scenarios/` or the scenarios runner, copy the real pass line |
| Orders auto vs needs approval vs blocked | NOT FOUND [MEASURE] | count `decision.outcome` across the scenario run |
| Offers returned for the milk request | NOT FOUND [MEASURE] | `POST /v1/offers` on the parsed milk need |
| Total euro value of the milk order | NOT FOUND [MEASURE] | from the Quote in the demo run |
| Audit events written per order | NOT FOUND [MEASURE] | `GET /v1/audit?order_id=` count |
| Tests passing | NOT FOUND [MEASURE] | `pytest -q` on dev |
