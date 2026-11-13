# Pitch, 3 minutes

Spoken by Momin. About 430 words, 150 wpm. Numbers in [brackets] are not measured yet, see NUMBERS.md. Do not say a bracket number until it is filled.

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

Sounds easy. It isn't. Valio has 17,546 products in the data. 4,178 say lactose-free. The agent has to pick the right ones, check stock, check the price, and not spend the hotel's money on its own without anyone knowing.

We built the thing in the middle. A gateway any agent can talk to.

**Demo (0:20)** [see DEMO.md, say this over the clicks]

Here is the agent. I paste the line. It turns it into a real need: milk, 1,000 litres, lactose-free, Monday.

Now it asks us for offers. These are real products from the Valio data, with real price and real stock. Not all milk is lactose-free, so we filter on what the product data actually declares. Each offer shows why it ranked where it did. Price, availability on Monday, delivery reliability, CO2.

It found one product it can buy for Monday, 1.40 euro a litre. Four other lactose-free milks it skipped, and it tells you why: their order cutoff already passed. [splitting across products only if the offers screen shows it]

We sign the quote. Price and stock locked, valid until the order cutoff. Nobody can change a number after that.

Now it places the order. 1,400 euros. That is inside the hotel's limit, so it goes through on its own, with the reason written down. Same request from the school is over the school's limit. That one stops and lands in the approval inbox.

**Controls (1:40)**

This is the part we care about most. Every agent has a mandate. Spend per order, spend per week, banned allergens, allowed categories, whether it may swap products.

Inside the mandate, it buys by itself. Outside, a human gets a plain sentence: what, how much, and why it stopped. I click approve. It is placed.

And every step is in an audit trail, written in normal words, hash-chained so you can see if anyone edited it. A hospital kitchen with a strict allergen rule gets blocked instead of asked. Same engine, different mandate.

**Exceptions (2:15)**

Now the bad day. Supplier stock drops mid-week. The agent does not fail. It finds a substitute that still fits the rules, or it tells the human it cannot, with the reason. For the hospital, a substitute that adds an allergen is never offered. [scenario pass count: only say it after the final run, today it is 1 of 8]

**Open (2:40)**

It is not a closed demo. REST with OpenAPI, and MCP, so any agent can plug in. Discovery file, public keys to verify quotes. The Valio mock has no order endpoint, so orders are ours and the signed quote is the contract. In production you swap the mock for the real order system and keep the same interface.

That is it. Agents can buy. Humans stay in charge.

## Cut list if we run over

1. Drop the hospital sentence in Controls.
2. Drop the CO2 mention in the demo.
3. Keep the hook, the approve click and the audit trail. Never cut those.

## Say nothing about

Anything we did not measure. No "saves X percent", no "X seconds", until NUMBERS.md says so.
