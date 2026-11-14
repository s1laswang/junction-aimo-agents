# Catalog report (team/catalog)

Last updated: Sat 14 Nov, M1 slice.

## Status
**M1 is done. Verified live against the mock API on 14 Nov at about 14:40.**

```bash
uv run python -c "
from datetime import date; from aimo.catalog import plan_need; from aimo.models import Need
p = plan_need(Need(customer_id='hotel-aurora', product_query='lactose-free milk', required_claims=['FREE_FROM_LACTOSE'],
                   quantity=1000, unit='L', delivery_by=date(2026,11,16), preferred_product_ids=['463038']))
print(p.explanation)"
```
Result:
- 463038 is rejected because its order cut-off passed (Thu 12 Nov 15:00). It carries the flag `cutoff_passed`.
- 464306 is tier **best**. The plan has one line: 1000 ST × 1 L = 1000 L, total **€1,010.00**, shortfall 0.
- 469363 is tier **better** with the flag `partial_stock` (384 ST in stock).
- 464401, 463335 and 462775 are tier **good** (different fat level).

The run takes about 1 s: one batched price call and one batched availability call, for at most 30 ids.

## What works (verified)
- **`aimo/backend/catalog_store.py`.** `load_catalog()` builds `data/catalog.sqlite`: 17,546 rows in 2.5 s the first time. It is idempotent: it rebuilds only when the source-file signature changes.
  - Tables: `products`, `claims`, `subs` (the supplier `substitutions` field) and `fts`.
  - `fts` is an FTS5 **trigram** index over the en/fi/sv names, brand, key ingredients and claim words.
  - The reliability and CO2 CSVs are joined in.
- **`aimo.catalog.search()`.**
  - It parses the query into concepts and expands fi/en/sv synonyms (maito/milk/mjölk, laktoositon/lactose-free and so on).
  - Trigram matching handles Finnish compound words such as kevytmaitojuoma.
  - Filters: claim, category and content_unit.
  - Re-scoring: how many concepts appear in the product name, a boost for implied claims, and a penalty for product-type words the query did not ask for.
  - The demo milks are found for both "lactose-free milk" and "laktoositon maito" (covered by a test).
- **`get_product()`** returns the full `Product`, with the EU14 tri-state for all 14 keys plus `allergen_source`.
- **`rank_substitutes()` and `plan_need()`.**
  - Hard filters, tiers and scores, with readable reasons and flags.
  - Greedy fill in whole sales units that respects allowedLotSize and available_amount.
  - The plan returns covered_amount, shortfall, total_net and an explanation.
- **Live offers.**
  - When `aimo.backend.get_offers` exists, the catalog uses it. The gateway branch already has it, with a compatible signature, and the catalog passes the `customer_id` kwarg.
  - Otherwise a catalog fallback client (`aimo/catalog/offers.py`) is used. It sends up to 1,000 ids per call, caches responses for 5 minutes in `data/cache/catalog_offers/`, and computes the cut-off from the server's `timeNow`.
- **`tests/test_catalog.py`.** 20 tests run in about 0.5 s. They cover unit parsing of the demo milks, the allergen mapping, the tiers, the plan fill and search.

## Content per sales unit (the main bug risk)
The parser tries these rules in order:
1. If salesUnit is KG, one sales unit is 1 kg.
2. A multipack in the name ("12 x 1 L", "100x2 cl", "4 x 6 x 0,33 l") describes the whole sales unit.
3. unitConversions BASE_UNIT_OR_EACH volume or netWeight, multiplied by units[salesUnit].sizeInBaseUnits. A drained-weight pair in the name ("340g/285g") takes precedence here.
4. The size in the name, multiplied by sizeInBaseUnits.
5. The CASE-level conversion.
6. Otherwise the content is counted in pieces (PCS).

**Validation:** I compared the parser with live unit_price / comparison_price on a random sample of 1,000 products. Of the 752 products that had a price, 702 agree within 3 % (93 %).

At plan time the live price is always cross-checked. If the static and price-derived contents disagree, the price-derived value wins and is snapped to a clean number. The candidate gets the flag `content_from_price` and a reason.

Examples:
- 463038: KI = 2 × 10 L = 20 L. The price check agrees: 31.31 / 1.57 = 19.9.
- 465039: KI = 5 L.
- 469287: KI = 12 × 1.75 L = 21 L.

## Allergens
The schema has **no enum descriptions** for allergen codes (verified). The mapping therefore uses the GS1 AllergenTypeCode list:

| EU14 group | GS1 codes |
|---|---|
| gluten | AW, UW, GB, GO, NR, GS, GK |
| milk | AM, ML |
| nuts | AN, SA, SH, ST, SW, SC, SP, SM, SR, SQ |
| crustaceans | AC |
| eggs | AE |
| fish | AF |
| peanuts | AP |
| soy | AY |
| celery | BC |
| mustard | BM |
| sesame | AS |
| sulphites | AU |
| lupin | NL |
| molluscs | UM |

Rules:
- CONTAINS beats MAY_CONTAIN, which beats FREE_FROM.
- Only the group's head code (AM, AW, AN and so on) or a FREE_FROM_GLUTEN / FREE_FROM_EGGS / FREE_FROM_SOY claim can prove `free`.
- `UNDECLARED` and missing data both mean `unknown`. Unknown is never treated as safe.
- "Lactose free" does NOT make a product milk-free.

Coverage: 9,978 products have some structured allergen data. 7,568 have none, so every group is unknown for them.

## Data quirks
- `volume.unit` is sometimes GRM, for example 472492 "5 kg sour milk". We treat it as a weight.
- Many CASE-level unitConversions have no values, and about half the products have no BASE-level conversion.
- Availability values:
  - Stock items report 9,999,999,999 when stock is unlimited.
  - Terminal items report 999,999, which means unknown. We map that to None.
- `closingTime` can be in the past while `availableDate` still equals the requested date. Following the API docs, a past closingTime counts as cut-off passed.
- The static content is wrong for about 7 % of products, for example some fresh goods and products counted in pieces. The live price check corrects it.
- The `ORGANIC` claim is derived from packagingMarkedLabel (EU_ORGANIC_FARMING, KRAV, Soil Association, the Sun label). It is not a Synkka claim.
- Some brand strings have trailing spaces ("Arla "). We strip them.
- Some GS1 codes in the data belong to no EU14 group: NC, NM, NE, NK, NW, BF, BP, PO, SO, CM, UN, PN and X99. We keep them raw and do not map them. X99 appears with CONTAINS 121 times, which is ambiguous.
- Category names are not in the data. We infer from the products that 10400 is milk drinks. That name is UNVERIFIED.

## Interface notes and additions
- No contract changes.
- The search submodule is named `aimo/catalog/textsearch.py`, not `search.py`, so `from aimo.catalog import search` still returns the function.
- `plan_need(need, offers_fn=None)` takes an optional injected offers function `(ids, date, customer_id=...) -> dict[str, Offer]`.
- An `UpstreamError` from `aimo.backend.get_offers` propagates out of `plan_need`, so the gateway can return a 503. The catalog does not swallow it.

## Next
Gemini work:
- (a) gemini-embedding-2 vectors for semantic recall and similar products.
- (b) `infer_allergens`, using structured output. Results are marked "inferred:gemini" and never trusted.
- (c) `recipe_compatible`.

## Needs Silas / CEO
Nothing blocking.
