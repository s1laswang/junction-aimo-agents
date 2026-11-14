# Catalog report (team/catalog)

Last updated: Sat 14 Nov, about 16:30. M1 is done and the Gemini items are done.

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
- Gluten-free oat products are coded with GO (oats) as CONTAINS, so a gluten exclusion rejects them. This is conservative and correct for a hospital, but means fewer options.
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

## Round 2 (Sat evening): allergen text and query robustness
- **Buyer bug fixed: the school allergen scenario returned an empty plan.** The cause was the planner's product-type guess, not raw_text itself.
  - The catalog never reads raw_text for types. But when a query carried allergy words ("milk-free oat drink", "oat drink, pupils have milk and nut allergies"), or the buyer's regex fallback produced a junk phrase ("by Wednesday"), the "milk" mention became the reference product type. Every oat drink was then rejected as "different product type".
  - Fix 1: `textsearch.clean_query()` strips allergy clauses and exclusion mentions ("X allergy", "allergic to", "no X", "without X", "X-free", "maidoton", coeliac) before search and before any type inference. Gluten-free and lactose-free stay in the query because they are positive claims that appear in product names. Safety is enforced only through `excluded_allergens`.
  - Fix 2: `plan.effective_query()` prefers the cleaned `product_query`. It falls back to the cleaned `raw_text` (reduced to concepts) only when the product phrase finds nothing. The explanation says which one was used, and `Plan.need` is returned unchanged.
  - Fix 3: when there is no usual product, the guessed reference is now the most relevant candidate that itself passes the claims, allergen and unit checks. A product-type mismatch against a guessed reference demotes the candidate to "good" with a note. It is rejected only when the reference is the customer's real usual product.
- **Hospital gluten-free.** When allergens are excluded, the candidate pool now also includes allergen-safe matches (FTS filtered on structured `free`), and safe products are ranked first for live offers.
  - "bread", "pasta", "gluten free bread" and "gluten-free pasta" with gluten excluded each give an orderable, structurally gluten-free line, for example Fria white bread 467032 and SamMills penne 462324.
  - Before this fix, "bread" returned nothing and "pasta" picked "Pasta Filata Mozzarella".
- **Shortfall explanation.** When the need is not covered, the explanation adds counts per rejection cause (contains the allergen, unknown allergen data, cut-off missed, unavailable, missing claim). It also lists actions a human can take: re-plan for a later date, have a qualified person confirm allergen status from the label, accept the partial plan, or broaden the phrase.
- New tests: `test_clean_query_strips_allergen_mentions`, `test_school_oat_drink_with_allergy_text` (4 variants of the product phrase, all with the CEO's raw_text), `test_hospital_gluten_free_bread_and_pasta` (3 variants) and `test_shortfall_explanation_is_actionable`. They run offline with a fake offers function and no semantic recall. The catalog has 31 tests, and the full suite passes 99 tests.

## Gemini (verified live, all calls logged through aimo.gemini to data/gemini/ledger)
- **(a) Embeddings** (`aimo/catalog/embeddings.py`): `gemini-embedding-2`, 768 dimensions, over the en and fi name, brand, the first 200 characters of ingredients and the free-from claims.
  - All 17,546 products took 44 s: 176 calls of 100 texts, 8 concurrent.
  - Output: `data/embeddings.npy` (float16) plus `data/embeddings_ids.json`. Query vectors are cached in `data/cache/query_embeddings.json`.
  - Quirk: one `embed_content` call with a list of plain strings returns ONE vector. Pass one `types.Content` per text.
  - `search()` is now hybrid: FTS and semantic results are merged by weighted reciprocal rank fusion. Example: "dairy-free drink for coffee" returns Oatly iKaffe and Alpro Barista, which FTS alone cannot find.
  - `plan_need` also adds the query's semantic neighbours and the usual product's `similar_products()` to the candidate pool.
  - Interactive embedding calls fail fast (2 tries). Without embeddings or a key, search silently runs FTS only.
- **(b) `infer_allergens(product)`** (`aimo/catalog/gemini_allergens.py`): GEMINI_FAST_MODEL with a structured-output Pydantic schema, batching 25 products per call. The ingredient text is passed as untrusted data.
  - Precomputed for all 4,079 products that lack structured allergen data but have ingredient text (4,076 succeeded, 49 s). Cache: `data/cache/gemini_allergens.json`.
  - Policy: the source is `inferred:gemini`. An inferred `contains` or `may_contain` can only block. A group Gemini did not find stays **unknown**, never `free` (covered by a test).
  - Hints appear in `get_product()` and in the reject reasons ("hint: Gemini reads the ingredients as containing milk (inferred:gemini, needs human confirmation)"), with the flag `allergen_inferred_hint`.
  - Spot check: Finnish VEHNÄJAUHO maps to gluten, KANANMUNA to eggs, SELLERI to celery, and "Lactose, Milk powder" to milk.
- **(c) `recipe_compatible(original, substitute, use)`**: a short structured judgement, cached in `data/cache/gemini_recipe.json`.
  - `plan_need` runs it for at most 3 substitute lines against the usual product, in parallel, with an 8 s cap and no blocking wait.
  - The result becomes a reason line ("Gemini recipe check vs usual product: compatible (high): ...") and, if the judgement is negative, the flag `recipe_check_failed`. It never overrides the hard filters. Turn it off with `AIMO_RECIPE_CHECK=0`.

## Integration check with the gateway client
I ran `plan_need(need, offers_fn=<team/gateway aimo/backend/client.get_offers>)` live and got the same plan: €1,010, 464306 best. Partial-stock and cut-off flags agree. Their Offer issues (`limited_stock`, `terminal_unknown_qty`) are compatible.

## Setup on another checkout
`uv run python -m aimo.catalog build` builds the SQLite DB (2.5 s). It also builds the embeddings (~45 s) and the allergen hints (~50 s) if they are missing; both steps are network-bound.
`uv run python -m aimo.catalog demo` runs the M1 plan. The files live in `data/`, which is gitignored, so each checkout must build them or copy them from my worktree's data/.

## Interface notes (could not append to docs/interfaces.md: outside my edit scope, please paste)
- Candidate and PlanLine flags:
  - `cutoff_passed`, `sales_block` and `unavailable` always mean tier `rejected`. These match trust's line flags.
  - The other flags: `outside_preference`, `partial_stock`, `allergen_unknown`, `allergen_inferred_hint`, `content_from_price`, `no_live_offer`, `recipe_check_failed`.
- `Product.allergen_source` values: `structured`, `structured:claim` (a FREE_FROM_GLUTEN / FREE_FROM_EGGS / FREE_FROM_SOY claim), `inferred:gemini` and `none`.
- Tiers when there is no usual product (no preferred_product_ids and no preferences):
  - best: every query concept appears in the name.
  - better: at least half of the concepts appear.
  - good: anything else that passes.
  - The reason line says "no usual product on file".
- CLI: `python -m aimo.catalog build|search <q>|demo`.

## Needs Silas / CEO
Nothing blocking.
