import json
import os
from datetime import date
from pathlib import Path

from aimo.models import Constraint
from aimo.recipes import DISHES, menu_to_need, recipe_swaps

TODAY = date(2026, 11, 14)


def qty(need, q):
    return next(i.quantity for i in need.items if i.query == q)


def test_english_menu():
    n = menu_to_need("salmon soup and pancakes for 120 guests on Friday", "hotel-aurora", today=TODAY)
    assert n.deliver_by == date(2026, 11, 20)
    assert qty(n, "salmon fillet") == 6.0
    assert qty(n, "milk") == 30.0
    assert qty(n, "potato") == 12.0


def test_finnish_menu():
    n = menu_to_need("lohikeitto 120 hengelle perjantaiksi", "school-hervanta", today=TODAY)
    assert qty(n, "salmon fillet") == 6.0 and n.deliver_by == date(2026, 11, 20)


def test_per_dish_counts_and_stock():
    n = menu_to_need("60 lasagne and 40 salad bar", "hotel-aurora", today=TODAY, stock={"onion": 1.0})
    assert qty(n, "ground beef") == 4.8
    assert qty(n, "onion") == 0.2
    assert all(i.query != "pepper red" or i.quantity == 0.8 for i in n.items)


def test_swaps():
    s = recipe_swaps("cream", Constraint(lactose_free=True))
    assert s[0]["query"] == "lactose-free cream" and s[0]["ratio"] == 1.0
    assert recipe_swaps("cream", Constraint()) == []
    assert recipe_swaps("wheat flour", Constraint(exclude_allergens=["gluten"]))[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("salmon fillet", Constraint(lactose_free=True)) == []


def test_menu_applies_swaps():
    n = menu_to_need("salmon soup for 10 guests lactose-free", "hospital-tays", today=TODAY)
    assert {"lactose-free cream", "lactose-free milk", "lactose-free butter"} <= {i.query for i in n.items}
    assert all(i.constraints.lactose_free for i in n.items if i.query.startswith("lactose-free"))


def test_ingredients_in_catalog():
    f = Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json"
    if not f.exists():
        return
    names = " ".join(
        v["value"].lower() for p in json.loads(f.read_text()) for v in p["synkkaData"]["names"] if v["language"] == "en"
    )
    for d in DISHES:
        for i in d["ingredients"]:
            assert i["query"].split()[-1].rstrip("s") in names, i["query"]
