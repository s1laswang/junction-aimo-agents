# Swarm A: solve the user need (owner: Silas)

**Gates:**
- **Zero-correction rate ≥95%** on a 1,000-request gold set: the agent's final cart is acceptable with no human edit.
- **Escalation precision ≥90% at recall ≥99%**: humans are asked only when the policy truly requires it.
- Every brief scenario is covered for 10 kitchen segments.

**Baseline (wind tunnel, Sat 18:45):**
- line acceptability 90%
- escalation precision 54%
- units agree with price 93%
- bread → flour mix, juice → juice drink, gluten-free bread → crispbread

**Your 100 cards** (`cards.jsonl`):

| Cards | Count | What |
|---|---|---|
| `A-gold-*` | 40 | 1,000 requests with gold carts |
| `A-tax-*` | 30 | independent product-type labels for the whole catalog |
| `A-allergen-*` | 10 | allergen data contradictions |
| `A-policy-*` | 10 | realistic customer policies for 10 segments |
| `A-phrasing-*` | 10 | 500 messy phrasings plus a parser check |

**Claude leads:** catalog (taxonomy, hard type filter, contradictions to unknown) and buyer (clarifying questions, the eval harness `aimo/buyer/eval_gold.py`).

**Silas, human tasks:** 15 minutes with mentor Minni Ruhtinas (online sales) or Riku Tapper (IT/data governance). Validate the approval matrix, the good-better-best tiers and the "learned approvals" idea, then write their quote into `swarm/A/out/A-mentor.md`.

## Gold schema (one JSON object per line)
```json
{"id": "A-gold-001-07", "segment": "hotel", "lang": "fi",
 "request": "Tarvitaan 600 l laktoositonta kevytmaitoa ja 20 kg voita ensi maanantaiksi",
 "customer_id": "hotel-aurora", "now": "2026-11-14T14:00:00+02:00",
 "gold": {
   "lines": [
     {"concept": "lactose-free semi-skimmed milk", "acceptable_product_ids": [["464306", "465039", "464398"]],
      "qty_range": [600, 600], "unit": "L"},
     {"concept": "butter", "acceptable_product_ids": [["<id>", "<id>"]], "qty_range": [20, 20.5], "unit": "KG"}],
   "acceptable_product_ids": [["464306", "465039", "464398"], ["<id>", "<id>"]],
   "qty_range": [600, 620],
   "must_escalate": true, "escalation_reason": "spend > €500",
   "forbidden_allergens": [],
   "expected_outcome": "needs_approval"},
 "notes": "why these products are acceptable"}
```
- `customer_id` is one of hotel-aurora, school-tampere or hospital-meilahti, or `<segment>-demo` from your policy cards.
- `expected_outcome` is one of `auto_approve | needs_approval | reject | no_order | needs_clarification`.
- Use `needs_clarification` only when a careful human buyer would ask.

## Taxonomy vocabulary (use these; otherwise `other:<snake_name>`)
milk_drink, lactose_free_milk_drink, plant_drink_oat, plant_drink_soy, plant_drink_almond, plant_drink_other, cream, cooking_cream, sour_cream, yoghurt, quark, butter, margarine_spread, cheese_hard, cheese_soft, cheese_grated, egg, bread_loaf, bread_roll, crispbread, tortilla_wrap, flour, flour_mix, pasta_dry, pasta_fresh, rice, grain_other, breakfast_cereal, muesli_oats, juice_100, juice_drink, soft_drink, water, coffee, tea, cocoa_drink, coffee_milk_drink, meat_fresh, meat_minced, meat_processed, poultry, fish_fresh, fish_frozen, seafood, vegetable_fresh, vegetable_frozen, fruit_fresh, fruit_frozen, berries, potato, herb_fresh, legume, nut_seed, oil_fat, sauce_condiment, spice, sugar_sweetener, baking_ingredient, dessert, ice_cream, pastry_frozen, ready_meal, soup, snack, confectionery, baby_food, clinical_nutrition, equipment, disposable_tableware, cleaning, non_food_other

## Taxonomy row
`{"product_id": "464306", "product_type": "lactose_free_milk_drink", "is_food": true, "pack_count": 1, "content_amount": 1.0, "content_unit": "L", "spot_checked": false, "confidence": 0.95}`
