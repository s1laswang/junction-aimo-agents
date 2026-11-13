# Assumptions

## Mocked or ours

- Valio has no order endpoint. Orders, approvals and the audit log are our own store (SQLite). In production they would be sent to Valio's order system.
- Prices and availability come from the shared Valio mock API (`/price`, `/availability`), cached in `var/cache`, ids batched.
- Agent keys and mandates are seeded in `aimo/mandates.py`. No real auth.
- Signing keys are local files. TODO-VERIFY: where `aimo/keys.py` stores them.

## Data sources

- Product JSON: 17,546 items with fi/en/sv names, allergens, nutrition, units.
- Delivery reliability CSV (0 to 1).
- CO2 CSV (kg CO2-eq per kg, category average, not per product).

Nothing is invented. A missing value shows as empty, not a guess.

## Seeded customers

| Customer | Type | Note |
|---|---|---|
| `hotel-aurora` | hotel chain | TODO-VERIFY mandate limits |
| `hospital-tays` | hospital kitchen | strict allergens |
| `school-hervanta` | school | TODO-VERIFY mandate limits |

## Limits

- Quotes are valid until the Valio order cutoff (`closingTime`). TODO-VERIFY.
- Substitution rules are rule-based, not a clinical allergen check.
