"""Build the buyer engineer's 20 smoke-test gold examples (tests/fixtures/gold/gold-buyer-20.jsonl).

These exist to exercise aimo/buyer/eval_gold.py until the human swarm's gold files land
(swarm/A/out/gold-*.jsonl). They are NOT human-verified. How each label was derived:
- acceptable_product_ids: real ids from the catalog (aimo.catalog.search over the product data),
  kept when the English name matches the item's `include` regex and not its `exclude` regex, the
  product has every required claim, and, when forbidden_allergens is set, its structured allergen
  status is `free` for each (unknown is never safe). Availability is not checked here, so the set is
  a superset of what can actually be delivered.
- qty_range: the requested quantity up to +10 % (pack rounding).
- must_escalate: from the demo policies in docs/interfaces.md and trust's YAML (hotel: over EUR 500
  or a substitute outside the standing preference needs approval; school and hospital: any
  substitution needs approval; drift: held).

    uv run python tests/fixtures/gold/make_gold.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent

LF_MILK = dict(query="lactose free milk", claims=["FREE_FROM_LACTOSE"], include=r"milk", exclude=r"chocolate|cocoa|powder|coffee|buttermilk|sour|cream|butter|cheese|yog|quark|rahka|protein|kefir|piimä|condensed|curdled|ruoka|vegetable|strawberry|portion")
LF_SEMI = dict(LF_MILK, include=r"(semi|kevyt|1[,.]5 ?%).*milk|milk.*(semi|1[,.]5)")
CREAM = dict(query="lactose free cream", claims=["FREE_FROM_LACTOSE"], include=r"cream", exclude=r"ice|sour|cheese|coffee|plant|oat|soy|pie|cake|filling|sauce|powder")
BUTTER = dict(query="butter", claims=["FREE_FROM_LACTOSE"], include=r"\bbutter\b", exclude=r"bean|peanut|nut|cookie|biscuit|croissant|pastry|dough|sauce|milk|cake|bread|popcorn|almond|cocoa|shea")
BUTTER_ANY = dict(BUTTER, claims=[])
OAT = dict(query="oat drink", claims=[], include=r"oat.*drink|kaurajuoma", exclude=r"chocolate|choco|kaffe|coffee|vanilla|2[,.]5 ?dl|0[,.]5 ?l|20 ?ml|hazelnut|soy")

EXAMPLES = [
    ("g01", "hotel", "en", "hotel-aurora", "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.", [LF_SEMI], [[1000, 1100]], True, []),
    ("g02", "hotel", "en", "hotel-aurora", "We need 200 litres of lactose-free milk by Monday.", [LF_SEMI], [[200, 220]], True, []),
    ("g03", "hotel", "en", "hotel-aurora", "We need 20 kg of lactose-free butter by Monday.", [BUTTER], [[20, 22]], False, []),
    ("g04", "hotel", "en", "hotel-aurora", "We need 40 litres of lactose-free cooking cream by Monday.", [CREAM], [[40, 44]], False, []),
    ("g05", "event", "en", "hotel-aurora", "Last-minute conference dinner for 150 on Monday evening: we need 40 L cream and 20 kg butter, lactose-free.", [CREAM, BUTTER], [[40, 44], [20, 22]], False, []),
    ("g06", "school", "en", "school-tampere", "We need 200 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.", [OAT], [[200, 220]], True, ["milk", "nuts"]),
    ("g07", "school", "fi", "school-tampere", "Tarvitsemme 120 litraa kaurajuomaa keskiviikoksi, oppilailla maito- ja pähkinäallergia.", [OAT], [[120, 132]], True, ["milk", "nuts"]),
    ("g08", "hotel", "en", "hotel-aurora", "We need 1,500 litres of lactose-free milk at Hotel Aurora Helsinki by Monday for a conference.", [LF_MILK], [[1500, 1650]], True, []),
    ("g09", "hotel", "en", "hotel-aurora", "Standing order for next week: 4,000 litres of lactose-free milk by Monday.", [LF_SEMI], [[4000, 4400]], True, []),
    ("g10", "hotel", "sv", "hotel-aurora", "Vi behöver 300 liter laktosfri mjölk till måndag.", [LF_SEMI], [[300, 330]], True, []),
    ("g11", "hotel", "fi", "hotel-aurora", "Tarvitaan 300 litraa laktoositonta maitoa maanantaiksi.", [LF_SEMI], [[300, 330]], True, []),
    ("g12", "hotel", "en", "hotel-aurora", "Please order 40 kg of salted butter for Monday.", [dict(BUTTER_ANY, include=r"\bbutter\b.*salted|salted.*\bbutter\b", exclude=BUTTER["exclude"] + r"|unsalted")], [[40, 44]], False, []),
    ("g13", "hotel", "en", "hotel-aurora", "We need 100 litres of lactose-free milk at the hotel by Wednesday.", [LF_SEMI], [[100, 110]], True, []),
    ("g14", "hotel", "fi", "hotel-aurora", "Tilaa 30 kg voita maanantaiksi.", [BUTTER_ANY], [[30, 33]], False, []),
    ("g15", "hospital", "en", "hospital-meilahti", "We need 100 litres of oat drink by Wednesday.", [OAT], [[100, 110]], True, ["gluten"]),
    ("g16", "hospital", "en", "hospital-meilahti", "We need 100 litres of lactose-free milk next week.", [LF_MILK], [[100, 110]], True, ["gluten"]),
    ("g17", "school", "en", "school-tampere", "We need 60 litres of oat drink by Thursday for the school kitchen; pupils have milk allergy.", [OAT], [[60, 66]], True, ["milk", "nuts"]),
    ("g18", "hotel", "en", "hotel-aurora", "Order 500 litres of lactose-free milk for Monday breakfast service.", [LF_SEMI], [[500, 550]], True, []),
    ("g19", "event", "en", "hotel-aurora", "Wedding on Monday: 30 litres of lactose-free cream and 10 kg of lactose-free butter.", [CREAM, BUTTER], [[30, 33], [10, 11]], False, []),
    ("g20", "hotel", "en", "hotel-aurora", "We need 2,000 litres of lactose-free milk by Monday.", [LF_SEMI], [[2000, 2200]], True, []),
]


def acceptable(rule: dict, forbidden: list[str]) -> list[str]:
    from aimo.catalog import get_product, search
    ids = []
    for p in search(rule["query"], claims=rule["claims"], limit=100):
        name = (p.name or "").lower()
        if not re.search(rule["include"], name, re.I) or re.search(rule["exclude"], name, re.I):
            continue
        if not set(rule["claims"]) <= set(p.claims or []):
            continue
        if forbidden:
            full = get_product(p.id)
            status = {k: getattr(v, "value", v) for k, v in ((full.allergens if full else {}) or {}).items()}
            if any(status.get(a, "unknown") != "free" for a in forbidden):
                continue
        ids.append(p.id)
    return ids


def main() -> None:
    out = HERE / "gold-buyer-20.jsonl"
    with open(out, "w") as f:
        for gid, seg, lang, cust, req, rules, qty, esc, forb in EXAMPLES:
            sets = [acceptable(r, forb) for r in rules]
            rec = {"id": gid, "segment": seg, "lang": lang, "request": req, "customer_id": cust,
                   "gold": {"acceptable_product_ids": sets if all(sets) else [], "qty_range": qty,
                            "must_escalate": esc, "forbidden_allergens": forb},
                   "meta": {"source": "buyer engineer smoke-test gold (not human-verified)",
                            "rules": [{k: r[k] for k in ("query", "claims", "include", "exclude")} for r in rules],
                            "empty_set_means": "no safe/acceptable product exists: correct behaviour is no order"}}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(gid, [len(s) for s in sets], req[:60])
    print("wrote", out)


if __name__ == "__main__":
    main()
