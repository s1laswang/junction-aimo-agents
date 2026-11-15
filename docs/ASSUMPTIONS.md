# Assumptions

Everything the prototype assumes that a production system would have to confirm or replace. Each entry has its evidence (a file, a command or a measured run), its impact on what we show, and the production replacement. Related: [DECISIONS.md](DECISIONS.md) (why we chose what we chose), [ARCHITECTURE.md](ARCHITECTURE.md) (next steps), [NUMBERS.md](NUMBERS.md) (commands for every number).

Status words: **VERIFIED** = we measured it; **ASSUMED** = our choice, not given by Valio Aimo; **UNVERIFIED** = plausible, not confirmed.

## Summary

| # | Assumption | Status | Impact if wrong | Production replacement |
|---|---|---|---|---|
| A1 | No upstream order API, so we mock the order backend | VERIFIED (no order endpoint in the mock spec) | Order placement and the Peppol response are ours, not Valio's | SAP / webshop order adapter behind `BackendClient`; Peppol BIS Ordering 3 |
| A2 | Customers, policies, approvers, usual products and standing orders are invented | ASSUMED | Thresholds and roles are illustrative; the *mechanism* is what we show | Customer onboarding: each customer authors its own policy, directory and mandates |
| A3 | Category groupings are inferred from category codes | UNVERIFIED | Hotel's category-scope rule may group products wrongly | Valio's real category tree (names and hierarchy) |
| A4 | Mock API quirks: availability ignores `requestedDate`, `closingTime` is fixed per product, Terminal stock is unknown | VERIFIED (probe below) | Delivery-date planning is shallower than real life; the cut-off story is clock-dependent | Real ATP/availability per date and route from SAP |
| A5 | CO2 is a category average, not a product footprint | VERIFIED (177 distinct values over 11,033 products) | CO2 comparisons only separate categories, not brands | Product-level LCA data (supplier EPDs) when available |
| A6 | Gemini allergen inference is advisory only | ASSUMED (by design) | Fewer auto-approvals for products lacking structured data | Supplier-confirmed allergen data (GS1/Synkka), human confirmation workflow |
| A7 | Prices are not customer-specific in the mock | VERIFIED (same prices for two customer ids) | Quotes show catalog prices, not contract prices | Customer pricing conditions from SAP |
| A8 | Identity is two HTTP headers in the demo | ASSUMED | Anyone who can reach the gateway can claim an identity | OAuth 2.1 bearer tokens per the MCP authorization spec, mandates as VCs |
| A9 | The demo signing key lives on disk | ASSUMED | Key theft would allow forged quotes | KMS/HSM-held Ed25519 key, rotation via JWKS |
| A10 | Static content per sales unit is right for about 93% of products; live price corrects the rest | VERIFIED on a sample (catalog report) | A wrong unit conversion would mis-size an order | Master data with explicit net content per sales unit |

## Details

### A1. There is no upstream order API, so the order backend is ours
- **Evidence:** the mock API spec (`docs/valio-mock-openapi.json`) has exactly these paths: `/api/ping`, `/api/availability`, `/api/price` and three data downloads, with no order endpoint; `aimo/backend/orders.py` implements quotes, orders, approvals and the order response in SQLite.
- **Impact:** "placed" means placed in our mock store. The Peppol-style AP/CA/RE response is computed from a live availability re-check at placement, not returned by Valio.
- **Production:** an order adapter behind the same seam (`aimo/backend/__init__.py`, `BackendClient`) that writes to SAP or the webshop, and Peppol BIS Order / Order Response documents over the Peppol network. The gateway, trust layer and agents do not change.

### A2. Customers, policies, approvers and usual products are invented
- **Evidence:** `aimo/trust/policies/hotel-aurora.yaml`, `school-tampere.yaml`, `hospital-meilahti.yaml` (v1.2.0 in S3 `need`, updated from v1.1.0). Hotel usual product 463038 with 464306 pre-approved as best; school usual product updated to 470889 in S3 v1.2.0 (Oatly Oat Drink Whole 1 L with structured `FREE_FROM` milk and nuts, replacing v1.1.0 472833 which lacked structured allergen data; [reports/need.md](reports/need.md)) and hospital usual 473328 are demo assumptions (trust report, round 2). Standing orders (hotel milk 300 L/week) and approver names (`anna@hotel-aurora.example`) are invented.
- **Impact:** the €500 / €5,000 thresholds, the 3× drift rule, the €50 auto-approved alternative, the 15% price-jump rule, the 7-day split-order window and the approver roles are illustrative examples. They have not been validated with Valio or a customer; we present them as examples of what a customer would set during Phase 1 pilot onboarding ([ARCHITECTURE.md](ARCHITECTURE.md) §5), never as recommended values.
- **Production:** each customer authors and versions its own policy (or brings OPA/Cedar), maintains its approver directory in its identity provider, and issues mandates to its agents.

### A3. Category groupings are inferred from category codes
- **Evidence:** the product data has category codes but no names (catalog report). The hotel policy's `category_scope` groups prefixes 10 (dairy and chilled), 11 (bakery) and 17 (chilled food and produce), marked UNVERIFIED in the YAML.
- **Impact:** an out-of-scope product may be grouped wrongly. Mitigation: out-of-scope and unknown categories lead to `needs_approval`, never to automatic acceptance or a hard reject.
- **Production:** Valio's category tree from master data.

### A4. Mock API quirks
Measured with `uv run python docs/pitch/probe_mock_quirks.py` (Sat 14 Nov 18:59 Helsinki; 5 upstream calls):
- **Availability ignores `requestedDate`.** Requests for 16 Nov, 20 Nov and 1 Dec return the same single schedule line dated 2026-11-16 for 463038, 464306 and 469363 (the upstream echoes the requested date but does not change the schedule).
- **`closingTime` is fixed per product.** 463038: 2026-11-12 15:00; 464306 and 469363: 2026-11-15 17:00, for every requested date.
- **Terminal stock is unknown.** Terminal product 463038 reports 999,999, which we map to "unknown quantity" (`terminal_unknown_qty`); Stock items report 9,999,999,999 for unlimited (catalog report).
- **Impact:** we cannot show multi-date delivery splits from real data, and the hero story depends on the wall clock: after Sun 15 Nov 17:00, Monday delivery is closed for stock milk ([SCENARIOS.md](SCENARIOS.md)). Mitigation: fault injection (`X-Aimo-Faults: cutoff:…`, `partial:…`) makes each exception reproducible; `AIMO_NOW` (simulated clock, also per request with `X-Aimo-Now`), `clock.next_delivery_day()` (which resolves the next weekday with an open Stock cut-off on the active clock), and replay mode (automatic without a key, or `AIMO_REPLAY=1`: recorded mock-API responses and 669-product catalog subset in `tests/fixtures/snapshot/replay/` across `2026-11-16`..`2026-11-20`, recorded Sat 23:50 Helsinki; alongside the 92-id `tests/fixtures/snapshot`) keep the story reproducible after the real cut-off (README, DEMO checklist step 5; verified Sat 23:15 and Sun 02:15).
- **Production:** SAP available-to-promise per date, route and delivery slot.

### A5. CO2 is a category average
- **Evidence:** `data/valio_aimo_junction26_carbon_footprint.csv` has the header `AVG kg CO2-eq/kg`; 11,033 products share only 177 distinct values (`sqlite3 data/catalog.sqlite "select count(*), count(distinct co2_kg_per_kg) from products where co2_kg_per_kg is not null"`). The catalog labels it "(category average)" in reasons (`aimo/catalog/substitution.py`).
- **Impact:** CO2 is shown, not used to rank: two brands of the same product usually have the same value.
- **Production:** product-level footprints (supplier EPDs/LCA) once available; then CO2 can become a ranking criterion or a policy rule.

### A6. Allergen inference is advisory only
- **Evidence:** 9,978 of 17,546 products have structured allergen data; 7,568 have none. Gemini read ingredient text for 4,076 of them (`data/cache/gemini_allergens.json`). Inferred `contains`/`may_contain` can block; a group Gemini did not find stays `unknown`, never `free` (`tests/test_catalog.py::test_gemini_inferred_allergens_never_make_free`; trust treats `inferred:*` as unknown, docs/TRUST.md).
- **Impact:** for a school or hospital, products without structured data never auto-approve. That is deliberate: a false "safe" is the one error we refuse to make.
- **Production:** supplier-confirmed allergen data from GS1/Synkka, plus a dietitian confirmation queue for the inferred hints.

### A7. Prices are not customer-specific in the mock
- **Evidence:** the probe above returns the same `catalogPrice` for `hotel-aurora` and `school-tampere` (463038 €31.31, 464306 €1.01, 469363 €2.48). The gateway passes `customer` upstream anyway.
- **Impact:** quotes are at catalog price; contract discounts are not modelled.
- **Production:** customer price conditions from SAP; the signed quote format already carries whatever price the backend returns.

### A8–A10
- **A8 identity:** headers `X-Customer-Id` / `X-Agent-Id` (docs/interfaces.md); the gateway enforces a known customer across `/v1`, catalog search, MCP and `/v1/agent/runs`, the header wins over any body field, and unregistered agents are refused (`tests/test_auth_known_customer.py`). The demo headers are not cryptographically authenticated, so a caller that knows a registered customer/agent pair can claim it; this is the main demo-only shortcut ([pitch/QA.md](pitch/QA.md) Q4). Human approvers are separate and do authenticate with distinct per-approver 12-character passphrases (`X-Approver-Pin` -> signed per-person `X-Approver-Session` with per-`(approver, client)` 429 lockout; [TRUST.md](TRUST.md)). Production: OAuth 2.1 client credentials per the MCP authorization spec for agents and customer SSO (OIDC) for humans.
- **A9 keys:** `data/keys/` (gitignored, generated on first use). Production: KMS/HSM.
- **A10 units:** the content-per-sales-unit parser agrees with live comparison prices for 702 of 752 sampled products (93%); at plan time the live price wins and the line is flagged `content_from_price` (catalog report).

## Not measured
- Gold set: zero-correction rate on fi/sv/en requests. Only a 20-request set exists (`tests/fixtures/gold_smoke.jsonl` / `tests/fixtures/gold/gold-buyer-20.jsonl`), with no committed live run, so we claim no accuracy number.
- Wind tunnel scale: the largest run is 2,560 template scenarios (`wt2560`) plus 640 Gemini-written ones (`wt640`) and 48 live-gateway agent runs ([windtunnel-results.md](windtunnel-results.md)), not ≥10k attacks. The 2,560 run (`wt2560`) and the naive baseline were not re-run after the Sun 00:30 policy changes; only the gateway on `wt640` was.
- Photo intake (`aimo/buyer/vision.py`): built (with deterministic keyless fallback), 4 development calls, no accuracy number.
