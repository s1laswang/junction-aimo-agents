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

## From our running system (measured 2026-11-14, dev at merge of sh-ui/gateway, local, no LLM key, rule parser)

Command: `.venv/bin/uvicorn aimo.app:app --port 8124` then `python -m pitch.measure_live --url http://localhost:8124`.

| Customer | mandate ms | parse ms | offers ms | quote ms | order ms | approvals ms | audit ms | offers | total net | outcome | reasons | audit events |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hotel-aurora | 15 | 65 | 5525 | 20 | 30 | 7 | 5 | 1 | 1400.0 | auto | WITHIN_MANDATE | 5 |
| hospital-tays | 5 | 3 | 278 | 8 | 25 | 5 | 5 | 1 | 1400.0 | auto | WITHIN_MANDATE | 5 |
| school-hervanta | 5 | 3 | 218 | 9 | 18 | 4 | 5 | 1 | 1400.0 | needs_approval | OVER_ORDER_LIMIT | 3 |

- Milk request: auto 2, needs_approval 1, blocked 0 of 3 customers. Median step latency 8 ms over 21 calls.
- First offers call is 5,525 ms because it is cold (fetches Valio). Later calls 218 and 278 ms from cache. Say "about 5 s cold, a fraction of a second warm", not "8 ms".
- 1,000 L milk = 1,400.0 EUR net (1.40 EUR per L, Valio Eila 3 % milk 1 l lactose-free). Hotel mandate per order is 2,500 EUR, so hotel goes through on its own.
- Offers returned: 1, with 4 more lactose-free milks skipped and the reason written in the notes (order cutoff 2026-11-12T15:00 already passed).
- Stock number in the offer is 9999999999. That is the mock's sentinel, never say it as stock.
- Hotel needs_approval does not happen for the plain milk request. Use school-hervanta or a larger order for the approval scene.
- Scenarios, `python scenarios/run_all.py --base http://localhost:8124`: 1 of 8 passing (only s7_drift). Failing: s0 (409 on approval), s1 (jws verify), s2 and s3 (recipe gives 1 line), s4, s5, s6. This is dev as it stands, not final. Re-run before the pitch.
- `pytest -q` on dev: 185 passed, 1 failed (tests/test_mcp_parity.py), 3 skipped, 10 xfailed.

Still NOT FOUND: scenarios passing after fixes, latency with an LLM key, milk order value once more than one offer comes back.
