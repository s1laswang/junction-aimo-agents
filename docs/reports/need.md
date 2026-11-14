# S3 `need` report: the agent does what the buyer asked, with the least correction

Branch `team/need` (worktree `~/junction-aimo-team/sessions/need`). Owner files: `aimo/catalog/plan.py`,
`aimo/catalog/substitution.py`, `aimo/trust/policy.py`, `aimo/trust/policies/*.yaml`, `aimo/trust/learned.py` (new),
`tests/test_need.py` (new). Contract additions appended to `docs/interfaces.md` ("S3 need").

## Done

### 1. Allergen hard-block inside planning (the school-milk leak)
- **Before (main fac34f0):** `plan_need(Need(customer_id="school-tampere", product_query="milk", 100 L))` with no
  `excluded_allergens` planned **476644 Arla 20 L whole milk**; "cream" planned Valio whipping cream.
- **Now:** the customer's policy `allergen_block.excluded` groups are merged into the need in `plan_need` *and* in
  `rank_substitutes` (so the gateway's quote verification and the deterministic planner get it too). Aliases are
  normalised (dairy/maito/mjölk → milk, tree nuts/almond → nuts, wheat/rye → gluten…). Unknown stays unsafe.
- School "milk" → **470889 Oatly Oat Drink Whole 1 L** (the school's usual milk-free product), flagged
  `allergen_safe_alternative`, and the policy sends it to the kitchen manager ("the request named 'milk' … a human
  confirms"). The policy decides this from `quote.need` itself, so it also holds for gateway-derived quote flags.
  With no allergen-safe usual product the plan says so and leaves the shortfall explicit.
- "cream"/"butter"/"cheese" at the school: only products confirmed milk-free are planned and each line needs a human.
- **Credit:** ported by hand from Momin's fleet branch `origin/fr8/valio/valio-allergen-w2` (e642884 policy allergen
  loader + aliases, 6e268c6 enforce in `plan_need`; workers valio-allergen-w1/-w2). Re-applied onto current main in
  files this session owns (`substitution.py` instead of `allergens.py`), alias list trimmed.
- **Data fix:** the school's usual product was 472833 Fazer Aito oat drink, which has **no structured milk/nut data**,
  so under the school's own policy (`on_unknown: reject`) it could never be ordered (docs/TRUST.md says it was
  "confirmed free of milk and nuts"; it is not). Now 470889 (structured FREE_FROM milk and nuts; in the replay
  fixture). Policy v1.2.0.

### 2. Fewer pointless escalations
- **Catalog-verified tiers:** `evaluate` re-runs the catalog's own substitution ranking for every line that is not
  a usual product (`policy.catalog_verify`), against the policy's usual products of the same kind (never the
  agent's label). Before, every product outside the usual list was "Agent-supplied tier could not be verified"
  → needs approval, for every customer (≈ 1,000 of 1,050 false escalations in wt2560 per the windtunnel report).
- **`new_product: allow`** (all three policies): no usual product of this kind + the catalog confirms an exact
  match for the request = a first purchase, not a substitution. Claims, allergens, spend etc. still apply.
- **`auto_approve_alternatives_below_eur`** (hotel: €50): a verified-best or customer-approved alternative line under
  the amount is swapped without asking even if its tier would need approval. Test: €10 pre-approved "better" swap
  auto-places, €100 needs the procurement manager.
- **Learned swaps** (`aimo/trust/learned.py`, table `learned_swaps` in `config.STATE_DB`): what the gateway's
  `decide(remember=True)` already called. A remembered swap (scope `product` or `product_type`) is `allow` in the
  substitution rule; every other rule still applies. `forget_swap` revokes.
- **Usual products first in planning:** when the request names no usual product and no brand/spec preference, the
  plan starts from the customer's usual product that matches the request (school "oat drink" → its usual oat drink,
  no swap to approve). An explicit brand preference wins over the profile.
- **Hotel category scope** widened to dry goods (21: flour, pasta, beans) and frozen food incl. frozen bread (41).
  Coffee/spices (22), meat/fish (18) and non-food stay out. Category groups remain inferred from names.
- **First purchase** only when the request's unit is normal for the product phrase; claim-only "free" allergen
  status counts as unknown in the allergen rule.

### 3. Split orders and price jumps
- **`split_order` rule** (all policies): orders of the same thing (same product, standing-order product, category or
  request phrase) placed without a human in the last 7 days that add up to more than the auto limit → needs
  approval. Prior orders come from the audit log (`order.placed` joined to `quote.created` for lines); when the log
  has no line detail, an order counts if it was itself ≥ 60% of the auto limit (the split signature).
  3 × €399.96 milk at the hotel: auto, then **needs approval** on the second and third.
- **`quantity_drift`** now adds the volume ordered earlier in the last 7 days (`window_days`).
- **`price_jump` rule:** a unit price more than 15% above the last price paid for the same product (newest placed
  order in the audit log, 90 days) needs approval. The buyer may report `need.preferences.reference_unit_price`;
  the lower reference wins, so a reported price can only make the check stricter.

### 4. Injection result on plan lines
- `plan_need` screens each candidate's supplier text with trust's regex screen (`screen_text(use_gemini=False)`,
  cached). A hit adds flag `supplier_text_injection_suspected` (the gateway's quote-flag name) and the reason
  "Supplier text contains instructions aimed at AI agents (…)" to the candidate and the plan line, so the UI and
  the agent see it before any order decision.

### 5. Wind tunnel: escalation precision before and after (wt640, pipeline, offline)
Re-run Sun 15 Nov ~00:30 on the laptop with a memory watchdog (stops only its own run PID below 15% free; it never
triggered). `AIMO_SEMANTIC=0` keeps it offline. Runtime 171–185 s under load ~28.

| System | Escalation precision | Escalation recall | Outcome accuracy | Policy violations | Allergen violations | Plan coverage |
|---|---|---|---|---|---|---|
| before (main fac34f0) | 54.0% | 98.8% | 55.0% | 1 | 0 | 92.4% |
| after v1 (main e337c7b, first S3 merge) | 67.5% | 94.3% | 72.0% | **21** | **3** | 91.4% |
| after v2 (this merge) | **68.2%** | **99.7%** | **73.4%** | **0** | **0** | 91.4% |

**Target ≥ 90% precision at ≥ 99% recall: NOT met.** Recall target met (99.7%); precision 68.2% (331 true, 154
false escalations, against 54.0% before).

v1 was a regression on main: removing the blanket "unverified substitution" escalation exposed four holes it had
been masking. v2 fixes them, each by escalating more, never less:
- 3 allergen violations: hospital "juice" auto-ordered 461893, "gluten free" only from a nutrition claim
  (`allergen_source: structured:claim`). The allergen rule now treats claim-only "free" as unknown.
- 9 mandate busts: the hotel scope widening admitted coffee (22). Dropped 22 and 18; kept dry goods (21) and frozen
  food (41).
- 6 split orders: the oracle counts evasion once auto-placed orders pass the auto limit (the 2nd order). `split_order`
  `max_factor` 2 → 1: the 2nd same-kind order over the limit goes to a human.
- 3 wrong units: "lactose-free milk" in KG matched a 5 kg quark, "rice" in L a rice drink. A first purchase is only
  auto when the unit is normal for the request (≥ 30% of the catalog's top text matches); otherwise unverified.

Remaining false escalations (v2, scorer's definition: auto was the only acceptable outcome):
| Cause | n | Why it stays |
|---|---|---|
| substitution only | 72 | 39 are menu_recipe quotes with several needs: the quote carries one `need`, so lines for the other ingredients cannot be verified. Rest: school/hospital "any substitution needs a human" vs oracle labels without a usual product |
| allergens (+ substitution) | 27 | claim-only gluten-free products now go to the dietitian (deliberately stricter than the oracle) |
| injection | 21 | request-screen hits (aimo/trust/screening.py, not this session's file) |
| price_jump (+ substitution) | 16 | spec rule (>15%); the oracle only expects a human at ≥2x |
| empty plan / backend error | 14 | catalog coverage, simulated API errors |

Ways to reach 90% (decisions for Silas/CEO, not taken here): carry every need on the quote (multi-need quotes:
gateway + models, ~39 FP); relax `price_jump` to 2x (~16 FP, contradicts the S3 spec); accept claim-only
gluten-free for the hospital (~27 FP, weaker allergen safety); tune the request screen (~21 FP).

Reproduce:
```bash
AIMO_SEMANTIC=0 uv run python -m windtunnel.run pipeline --scenarios data/windtunnel/scenarios/wt640_gem.jsonl --sut team
uv run python -m windtunnel.score --scenarios data/windtunnel/scenarios/wt640_gem.jsonl \
  --runs data/windtunnel/runs/wt640_gem__pipeline__team__0of1.jsonl --labels after --name need_after --no-md
```
Before-run causes (for comparison, all non-must cases): substitution 283, category_scope 79, injection 30, empty
plans 16, spend 7.

## Gate
Rebased on main dfe58ef, Sun 00:00 Helsinki:
- `uv run --extra dev pytest -q`: 342 passed, 7 skipped, 1 xfailed, 1 xpassed (19 new in tests/test_need.py).
- `uv run python -m scenarios.run_all`: 8/8.
- `make accept`: PASS=15 FAIL=0 SKIP=1 (live Gemini demo skipped by design).

## Open items
- Request-text screen false positives (injection rule) and empty plans are now the largest remaining false
  escalations; the screen lives in `aimo/trust/screening.py` (not this session's file).
- Gateway `_plan_deterministic` (fallback planner) gets the allergen hard block through `rank_substitutes` but not
  the usual-product-first and allergen-safe-alternative steps.

## Requests
- **docs/TRUST.md (owner trust/pitch):** the school row says "usual product (472833) confirmed free of milk and nuts".
  It is not (no structured milk/nut data). Now 470889; please update the row. Also mention `new_product`,
  `auto_approve_alternatives_below_eur`, `split_order`, `price_jump`.
- **web/ (S5):** show `allergen_safe_alternative` and `supplier_text_injection_suspected` on plan lines (reason text is
  in `PlanLine.reasons`).
- **windtunnel/run.py (owner windtunnel):** the pipeline's MemoryAudit stores `order.placed` with totals only; adding
  `"lines"` (product_id, product_name, qty_sales_units, unit_price, line_total) would let `split_order`/`price_jump`
  use product detail there as they do with the real audit log.
