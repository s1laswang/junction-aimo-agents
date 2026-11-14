# Pitch, 3 minutes

Spoken by Momin. 437 spoken words (146 wpm). Every number below comes from `pitch/measure_data.py` and the live `aimo.app:app` milk flow, see [NUMBERS.md](NUMBERS.md).

## Time map to judging weights

| Time | Part | Weight it feeds |
|---|---|---|
| 0:00-0:20 | Hook | frames the demo |
| 0:20-1:40 | Live demo, milk request end to end | working demo 30 |
| (inside demo) | Why these products, not just any milk | precise match 20 |
| 1:40-2:15 | Who is allowed to buy what | autonomy and controls 20 |
| 2:15-2:40 | Stock runs out, still works | exceptions 15 |
| 2:40-3:00 | Open, plug in, path to production | openness 15 |

## Script

**Hook (0:00)**

A hotel's buying agent sends one line. "I need 1,000 litres of lactose-free milk by Monday."

Sounds easy. It isn't. Valio has 17,546 products in the catalog. 4,178 declare lactose-free. The agent has to pick the right product, check stock and price, and never spend the hotel's money outside its rules.

We built the thing in the middle: a trusted trade gateway any agent can talk to.

**Demo (0:20)** (live in `/console.html`, see [DEMO.md](DEMO.md))

Here is the buyer agent for `hotel-aurora`. I paste the line. It parses a structured need: milk, 1,000 litres, lactose-free, Monday.

Now it asks for offers. Real Valio products, real price and stock, filtered on declared claims. Each offer shows why it ranked: price, Monday availability, delivery reliability, CO2.

It matches product 464119, Valio rasvaton maito 1 l laktoositon UHT: 100 cases of 10 litres at 13.32 EUR net per case, 1.33 EUR per litre, plus 3 ranked good, better, and best alternatives with explicit trade-offs on price and pack size.

We issue a single-use Ed25519-signed quote: 1,332 EUR net, 1,512 EUR with VAT, locked until the day-before-delivery cutoff. Nobody can alter a number after that.

Now it places the order. Hotel Aurora has a 2,500 EUR per-order limit, so 1,332 EUR is auto-confirmed (`WITHIN_MANDATE`) with the reason recorded. The exact same ask from `school-hervanta` exceeds its 600 EUR per-order limit and stops in `pending_approval` in `/inbox.html`.

**Controls (1:40)** (live in `/inbox.html` and `/audit.html`)

This is the part we care about most. Every agent has a mandate: spend per order, spend per week, banned allergens, allowed categories, and whether it may swap products.

Inside the mandate, it buys on its own. Outside, a human gets one plain sentence: what, how much, and why it stopped. I click approve. The order is confirmed.

Every step lands in a hash-chained audit ledger, written in normal words, with a signed checkpoint so anyone can verify nothing was edited. Hospital Tays blocks banned allergens outright instead of asking. Same engine, different mandate.

**Exceptions (2:15)** (stock-out simulation in `/console.html?demo=1`)

Now the bad day. Supplier stock drops mid-week. The agent does not fail. It proposes a compliant substitute or split and moves the order to `pending_approval` with the reason. For Hospital Tays, a substitute that adds a banned allergen is never offered.

**Open (2:40)** (`/.well-known/agent-card.json`, `/.well-known/jwks.json`, `/openapi.json`, `/mcp`)

It is not a closed demo. REST with OpenAPI parity across 12 MCP tools, so any agent plugs in. Agent discovery card and Ed25519 public keys let anyone verify quotes and audit checkpoints. The Valio mock has no order endpoint, so orders are ours and the signed quote is the contract. In production you swap the mock for the real order system and keep the same interface.

That is it. Agents can buy. Humans stay in charge.

## Cut list if we run over

1. Drop the Hospital Tays sentence in Controls.
2. Drop the CO2 mention in the Demo.
3. Keep the hook, the approve click, and the audit ledger. Never cut those.

## Say nothing about

Anything we did not measure. No "saves X percent", no "X seconds", unless [NUMBERS.md](NUMBERS.md) has the command that measured it.
