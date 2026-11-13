import json
import os

import pytest

from aimo.models import Constraint, NeedItem, Product
from aimo.substitute import allergen_safe, match_score

DATA = os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json")


def to_product(x):
    s = x["synkkaData"]
    cs = s.get("classifications", [])
    en = next((n["value"] for n in s["names"] if n["language"] == "en"), "")
    return Product(
        product_id=x["id"],
        name=en,
        category=x["category"],
        allergens=[v["id"] for c in cs if c["name"] == "allergen" for v in c["values"]],
        labels=[v["id"] for c in cs if c["name"] in ("nutritionalClaim", "packagingMarkedLabel") for v in c["values"]],
    )


@pytest.fixture(scope="module")
def products():
    return {x["id"]: to_product(x) for x in json.load(open(DATA))}


MILK = NeedItem(query="milk", quantity=1000, unit="l", constraints=Constraint(lactose_free=True))


def test_real_milk_beats_chocolate_drink(products):
    good = [products[i] for i in ("464073", "463335", "464398", "464119")]
    for p in good:
        assert match_score(MILK, p) >= 0.8, p.name
    for i in ("460448", "463814", "465042", "466215", "462626", "463203"):
        assert match_score(MILK, products[i]) <= 0.3, products[i].name


def test_lactose_free_hard_filter(products):
    c = MILK.constraints
    assert allergen_safe(products["464073"], c, [])
    plain = next(p for p in products.values() if p.category == "10400" and "lactose" not in p.name.lower() and "FREE_FROM_LACTOSE" not in p.labels)
    assert not allergen_safe(plain, c, [])


def test_excluded_allergen_and_missing_data(products):
    c = Constraint()
    assert not allergen_safe(products["464073"], c, ["milk"])
    assert allergen_safe(products["464073"], c, ["egg"])
    assert not allergen_safe(products["464073"], c, ["unknownium"])
    assert not allergen_safe(Product(product_id="x", name="Lactose free milk"), c, ["egg"])
