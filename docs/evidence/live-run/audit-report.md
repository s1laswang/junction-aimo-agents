# Audit trail for ORD-20261115-06eda13d

_Sun 15 Nov 2026, times in Helsinki (EET), real clock._

- **05:38:10**, Valio Aimo issued quote Q-20261115-964294b6 to buyer-1: 1,000 × Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free at €1.01 = €1,010.00 net, delivery Mon 16 Nov, price locked until 05:53, signed (hash 6def7dae163f…).  `#1 quote.created`
- **05:38:12**, Policy hotel-aurora v1.2.0 evaluated quote Q-20261115-964294b6: NEEDS APPROVAL by a procurement manager. Order total €1,010.00 is above the €500.00 auto-approve limit, so a procurement manager must approve it. Also: quantity drift.  `#2 policy.evaluated`
- **05:38:12**, Order ORD-20261115-06eda13d is waiting for a procurement manager to approve (€1,010.00 net).  `#3 order.pending_approval`
- **05:38:15**, anna@hotel-aurora.example (procurement manager) approved quote Q-20261115-964294b6 (€1,010.00), bound to hash 6def7dae163f…: "Approved from the terminal demo"  `#4 approval.verified`
- **05:38:16**, Order ORD-20261115-06eda13d was approved by anna@hotel-aurora.example (€1,010.00 net): Approved from the terminal demo  `#5 order.approved`
- **05:38:16**, Order ORD-20261115-06eda13d was placed with Valio Aimo (€1,010.00 net); supplier response AP (accepted): 464306 AP 1000  `#6 order.placed`

## Policy decision: needs approval (approver: procurement manager)

Policy `hotel-aurora` version 1.2.0.

| Rule | Outcome | Why |
|---|---|---|
| `quote_integrity` | pass | Quote arithmetic checks out (1 line(s), €1,010.00); signed by Valio Aimo and unmodified. |
| `mandate_scope` | pass | Agent hotel-aurora/buyer-1 holds a valid mandate from Hotel Aurora Helsinki to submit orders. |
| `quote_freshness` | pass | Prices are locked until 05:53 (Helsinki time). |
| `spend` | **needs approval** | Order total €1,010.00 is above the €500.00 auto-approve limit, so a procurement manager must approve it. |
| `weekly_budget` | pass | Spending in the last 7 days would be €1,010.00 of the €8,000.00 weekly budget. |
| `category_scope` | pass | All lines are within the allowed categories (dairy and chilled, fresh bakery, chilled food and produce, dry goods, frozen food). |
| `substitution:464306` | pass | 'Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free' (464306) is a substitute: same brand and spec in a different pack (tier best), verified as a customer-approved alternative in the profile. Allowed automatically by this policy. |
| `allergens` | pass | This mandate has no allergen exclusions. |
| `delivery` | pass | Delivery Mon 16 Nov meets the requested date (Mon 16 Nov) and no line is past its order cut-off. |
| `quantity_drift:milk` | **needs approval** | This is 3.3× your usual weekly volume of milk (1,000 L vs about 300 L a week). A procurement manager must confirm the quantity. |
| `split_order` | pass | This order is above the auto-approve limit on its own; the spend rule decides. |
| `price_jump` | pass | No line is more than 15% above the last price paid. |
| `injection` | pass | No prompt-injection signals in the request or supplier text. |

## Key facts

- Quote `Q-20261115-964294b6` hash (sha256): `6def7dae163f23324fb97037000ac9ab7fa7276971eead120069e09f3270fbda`, signed with key `CIbMjdup7xejhizQv2eS1sKdEZ_rIOdetD4U1D-Xavc`
- Approved by **anna@hotel-aurora.example** (procurement manager) at 05:38:15, bound to quote hash `6def7dae163f2332…`: "Approved from the terminal demo"
- Order approved by **anna@hotel-aurora.example** at 05:38:16

**Chain verified: 6 events, signatures valid (key kid CIbMjdup7xejhizQ…).** This trail shows 6 of them.
