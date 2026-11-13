import json
import os

import pytest

from aimo.models import Constraint, NeedItem, Offer, Product, UnitSize
from aimo.allocate import _ratio
from aimo.substitute import CODE_NAMES, allergen_safe, match_score, rank_substitutes, tradeoff

NAMES = CODE_NAMES
DATA = os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json")


def to_product(x):
    s = x["synkkaData"]
    cs = s.get("classifications", [])
    en = next((n["value"] for n in s["names"] if n["language"] == "en"), "")
    return Product(
        product_id=x["id"],
        name=en,
        category=x["category"],
        allergens=[NAMES.get(v["id"], v["id"].lower()) if v.get("unit") == "CONTAINS" else "may_contain:" + NAMES.get(v["id"], v["id"].lower()) for c in cs if c["name"] == "allergen" for v in c["values"]],
        labels=[v["id"].lower() for c in cs if c["name"] in ("nutritionalClaim", "packagingMarkedLabel") for v in c["values"]],
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
    plain = next(p for p in products.values() if p.category == "10400" and "lactose" not in p.name.lower() and "free_from_lactose" not in p.labels)
    assert not allergen_safe(plain, c, [])


def test_excluded_allergen_and_missing_data(products):
    c = Constraint()
    assert not allergen_safe(products["464073"], c, ["milk"])
    assert allergen_safe(products["464073"], c, ["egg"])
    assert not allergen_safe(products["464073"], c, ["unknownium"])
    assert not allergen_safe(Product(product_id="x", name="Lactose free milk"), c, ["egg"])


FIX = os.path.join(os.path.dirname(__file__), "fixtures", "price_lactose_free_milk.json")


def csv_map(name):
    out = {}
    for line in open(os.path.join(os.path.dirname(DATA), name)).read().splitlines()[1:]:
        k, v = line.split(",")
        out[k] = float(v)
    return out


@pytest.fixture(scope="module")
def offers(products):
    rel = csv_map("valio_aimo_junction26_delivery_reliability.csv")
    co2 = csv_map("valio_aimo_junction26_carbon_footprint.csv")
    out = []
    for r in json.load(open(FIX))["products"]:
        if r.get("error") or not r.get("comparisonPrice"):
            continue
        p = products[r["productId"]].model_copy()
        price = r["catalogPrice"]["value"]
        p.unit_size = UnitSize(value=price / r["comparisonPrice"]["value"], unit="l")
        out.append(Offer(
            product_id=p.product_id, product=p, units_needed=10, unit_price_net=price, total_net=price * 10,
            total_gross=price * 10, available_amount=100, reliability=rel.get(p.product_id), co2_per_kg=co2.get(p.product_id),
        ))
    return out


def test_rank_substitutes_milk_shortfall(products, offers):
    orig = products["464073"]
    res = rank_substitutes(MILK, offers, [], original=orig)
    assert 2 <= len(res) <= 3
    assert [o.tier for o in res] == ["good", "better", "best"][: len(res)]
    assert orig.product_id not in {o.product_id for o in res}
    assert "460448" not in {o.product_id for o in res}
    for o in res:
        assert allergen_safe(o.product, MILK.constraints, [])
        assert o.match_score >= 0.5
        assert o.notes and "\n" not in o.notes[0]
    assert res[0].unit_price_net / _ratio(res[0].product, "l") <= min(x.unit_price_net / _ratio(x.product, "l") for x in res)


def test_rank_substitutes_excludes_milk_allergen(offers):
    assert rank_substitutes(MILK, offers, ["milk"]) == []


def test_tradeoff_public(products, offers):
    by = {o.product_id: o for o in offers}
    a, b = by["463335"], by["464201"]
    line = tradeoff(b, a)
    assert "\n" not in line and line
    assert "vs 1 " not in line.replace("reliability 1 vs 1,", "")
    same = tradeoff(a, a)
    assert "reliability" not in same


def test_safe_alternatives_when_milk_excluded():
    from aimo import catalog
    from aimo.substitute import safe_alternatives

    item = NeedItem(query="milk", quantity=100, unit="l", constraints=Constraint(lactose_free=True))
    assert not any(allergen_safe(p, item.constraints, ["milk"]) and match_score(item, p) >= 0.5 for p in catalog.search("milk", item.constraints, limit=50))
    alts = safe_alternatives(item, ["milk"])
    assert alts and all(why for _, why in alts)
    alt, why = alts[0]
    assert alt.query == "oat drink" and alt.quantity == 100 and alt.unit == "l"
    ok = [p for p in catalog.search(alt.query, alt.constraints, limit=50) if allergen_safe(p, alt.constraints, ["milk"])]
    assert max(match_score(alt, p) for p in ok) >= 0.5
    assert safe_alternatives(NeedItem(query="milk", quantity=1, unit="l"), ["egg"]) == []


@pytest.mark.parametrize("query,must", [("whipping cream", "kuohukerma"), ("butter", "voi"), ("carrots", "porkkana"), ("cucumber", "kurkku")])
def test_top_match_is_the_product_asked(query, must):
    from aimo import catalog

    item = NeedItem(query=query, quantity=10, unit="kg")
    found = catalog.search(query, item.constraints, limit=50)
    top = max(found, key=lambda p: match_score(item, p))
    assert must in top.name.lower() and match_score(item, top) >= 0.5, top.name
