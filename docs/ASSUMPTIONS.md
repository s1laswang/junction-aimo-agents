# Assumptions

## Mocked or ours

- Valio has no order endpoint. Orders, approvals and the audit log are our own store (SQLite). In production they would be sent to Valio's order system.
- Prices and availability come from the shared Valio mock API (`/price`, `/availability`), cached in `var/cache`, ids batched.
- Agent keys and mandates are seeded in `aimo/mandates.py`. Demo keys are fixed (`demo-hotel-aurora` and so on) and the admin key defaults to `demo-admin`. No real auth.
- Signing keys are local files. The Ed25519 key is a JSON file in `var/keys/` (`AIMO_KEY_DIR`), created on first use.

## Data sources

- Product JSON: 17,546 items with fi/en/sv names, allergens, nutrition, units.
- Delivery reliability CSV (0 to 1).
- CO2 CSV (kg CO2-eq per kg, category average, not per product).

Nothing is invented. A missing value shows as empty, not a guess.

## Seeded customers

| Customer | Type | Note |
|---|---|---|
| `hotel-aurora` | hotel chain | 2,500 EUR per order, 15,000 EUR per week, substitutions within preferences |
| `hospital-tays` | hospital kitchen | 1,500 EUR per order, 8,000 EUR per week, excludes peanut, nuts, egg, substitutions ask a human |
| `school-hervanta` | school | 600 EUR per order, 3,000 EUR per week, excludes peanut and nuts, substitutions within preferences |

## Limits

- Quotes are valid until the Valio order cutoff (`closingTime`, Helsinki time).
- Substitution rules are rule-based, not a clinical allergen check.
