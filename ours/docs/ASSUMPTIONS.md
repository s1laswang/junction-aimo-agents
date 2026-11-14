# Assumptions

## Mocked or ours

- Valio's challenge API provides `POST /price` and `POST /availability` only; it has no order placement endpoint. Orders, human approvals, mandates, quotes, and the hash-chained audit log live in our SQLite store (`AIMO_DB`, default `var/aimo.db`), and `aimo/orders.py` confirms placed orders with a simulated `MOCK-<hex>` confirmation (`mock: true`). See [PRODUCTION](PRODUCTION.md) for the ERP adapter path.
- Prices and availability come from the shared Valio mock API (`/price`, `/availability`), batched up to `1,000` product IDs per call and cached per `(endpoint, customer, requestedDate)` in `AIMO_CACHE_DIR` (`var/cache`), with stale-cache fallback on `429` or `5xx`. In the mock API, `availableAmount` can use large sentinel values (such as `9999999999`) and `closingTime` carries a fixed date, so `aimo/offers.py` applies `closingTime`'s `Europe/Helsinki` clock time to `(availableDate - 1 day)`.
- Agent keys and mandates are seeded in `aimo/mandates.py` (`INSERT OR IGNORE` on first connection). Demo agent keys are fixed (`demo-hotel-aurora`, `demo-hospital-tays`, `demo-school-hervanta`, overridable via `AIMO_AGENT_KEYS`) and the admin key defaults to `demo-admin` (`AIMO_ADMIN_KEY`, plus `AIMO_STDIO_ADMIN_KEY` for MCP stdio). Approver names (`ApprovalDecision.by`) are checked against `mandate.approvers` behind `X-Admin-Key`; production binds them to SSO/OIDC identities.
- Signing keys are local `0600` JSON files in `AIMO_KEY_DIR` (`var/keys/quote-ed25519.json`), created on first use. `python -m aimo.keys rotate` retires the previous public key to `retired-<kid>.json` and keeps publishing it in `/.well-known/jwks.json`; see [STANDARDS](STANDARDS.md).
- `POST /v1/admin/simulate` overrides product availability in-memory (`aimo/offers.py`, cleared with `DELETE /v1/admin/simulate`) and triggers server-side `orders.react_stockout` (`aimo/orders.py`) on affected orders.
- Recipe scaling definitions live in `data_extra/recipes.json` (`aimo/recipes.py`), and `agents/drift.py` and `agents/seller.py` use seeded baseline histories when no prior ERP order history exists.

## Data sources

Measured directly from `data/` by `pitch/measure_data.py` (see [NUMBERS](../pitch/NUMBERS.md)):

- `valio_aimo_junction26_product_data.json`: `17,546` active products (`0` deleted) across `349` categories, with `fi`/`en`/`sv` names (`17,544` have English names), packaging units (`synkkaData.unitConversions`), nutritional claims (`4,178` `FREE_FROM_LACTOSE`, `321` `LOW_LACTOSE`, `3,049` `FREE_FROM_GLUTEN`, `2,959` `VEGAN`), and allergen declarations (`9,436` declare either `allergen` (`7,205`) or `nonAllergen` `FREE_FROM` (`5,119`), leaving `8,110` with neither list).
- `valio_aimo_junction26_delivery_reliability.csv`: `4,425` catalog products with historical delivery reliability from `0.0` to `1.0` (median `1.0`, mean `0.972`).
- `valio_aimo_junction26_carbon_footprint.csv`: `11,033` catalog products with category-average carbon intensity in `kg CO2-eq/kg` (`0.13` to `30.46`, median `2.11`), not product-specific lifecycle assessments.

Nothing is invented: missing reliability or CO2 values stay `null` rather than estimated.

## Seeded customers

| Customer | Agent ID (demo key) | Type | Net limits | Excluded allergens | Substitutions (preferred brand) | Named approvers |
|---|---|---|---|---|---|---|
| `hotel-aurora` | `buyer-aurora` (`demo-hotel-aurora`) | hotel chain | 2,500 EUR / order, 15,000 EUR / week | none | `within_prefs` (`Valio`) | Laura Virtanen, Mikko Heikkinen |
| `hospital-tays` | `buyer-tays` (`demo-hospital-tays`) | hospital kitchen | 1,500 EUR / order, 8,000 EUR / week | `peanut`, `nuts`, `egg` | `ask` (`Valio`) | Helena Korhonen, Antti Lahtinen |
| `school-hervanta` | `buyer-hervanta` (`demo-school-hervanta`) | school | 600 EUR / order, 3,000 EUR / week | `peanut`, `nuts` | `within_prefs` (`Valio`) | Sari Nieminen |

## Limits

- Quotes are valid until the Valio order cutoff (`closingTime`, `Europe/Helsinki`). If the cutoff for a requested date has already passed, `aimo/offers.py` rolls forward to the next open delivery date and records why in `notes`.
- Mandate spending caps (`per_order_eur`, `per_week_eur`) compare net EUR (`total_net`), and `orders.spent_this_week` sums `placed`, `approved`, and `confirmed` orders over a rolling 7-day window.
- Substitution and allergen rules (`aimo/substitute.py`, `aimo/policy.py`) rely on supplier-declared Synkka metadata (`CONTAINS`, `MAY_CONTAIN`, and `nutritionalClaim`), not a clinical allergen audit. When dietary or allergen constraints apply during `get_offers`, `substitute.allergen_safe` drops items that declare neither `allergens` nor `labels`, and `policy.decide` holds orders in `needs_approval` (`UNKNOWN_PRODUCT`) if product data is missing.
