import re
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
    if not os.path.exists(DATA):
        pytest.skip("needs Valio product data")
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


@pytest.mark.skipif(not os.path.exists(DATA), reason="needs Valio product data")
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


@pytest.mark.skipif(not os.path.exists(DATA), reason="needs Valio product data")
@pytest.mark.parametrize("query,must", [("whipping cream", "kuohukerma"), ("butter", "voi"), ("carrots", "porkkana"), ("cucumber", "kurkku")])
def test_top_match_is_the_product_asked(query, must):
    from aimo import catalog

    item = NeedItem(query=query, quantity=10, unit="kg")
    found = catalog.search(query, item.constraints, limit=50)
    top = max(found, key=lambda p: match_score(item, p))
    assert must in top.name.lower() and match_score(item, top) >= 0.5, top.name


@pytest.mark.skipif(not os.path.exists(DATA), reason="needs Valio product data")
def test_mince_top_is_jauheliha():
    from aimo import catalog

    item = NeedItem(query="mince", quantity=60, unit="kg")
    top = max(catalog.search("mince", item.constraints, limit=50), key=lambda p: match_score(item, p))
    assert "jauheliha" in top.name.lower(), top.name


@pytest.mark.skipif(not os.path.exists(DATA), reason="needs Valio product data")
def test_oat_drink_requires_oat():
    from aimo import catalog

    item = NeedItem(query="oat drink", quantity=100, unit="l", constraints=Constraint(exclude_allergens=["soy"]))
    top = max(catalog.search("oat drink", item.constraints, limit=50), key=lambda p: match_score(item, p))
    assert re.search(r"oat|kaura", top.name.lower()), top.name
    prot = next(p for p in catalog.search("protein drink", Constraint(), limit=50) if "PROTEINDRINK" in p.name.upper())
    assert match_score(item, prot) < 0.5


def _offer(
    pid,
    name,
    price,
    size_l=1.0,
    category="10400",
    allergens=None,
    labels=None,
    available=100.0,
    sales_block=None,
    reliability=0.95,
    co2=1.1,
    sales_unit="ST",
):
    p = Product(
        product_id=pid,
        name=name,
        category=category,
        sales_unit=sales_unit,
        unit_size=UnitSize(value=size_l, unit="l"),
        allergens=allergens if allergens is not None else ["milk"],
        labels=labels if labels is not None else ["free_from_lactose"],
    )
    return Offer(
        product_id=pid,
        product=p,
        units_needed=10,
        unit_price_net=price,
        total_net=price * 10,
        total_gross=price * 10,
        available_amount=available,
        sales_block=sales_block,
        reliability=reliability,
        co2_per_kg=co2,
    )


def test_offline_allergen_safe():
    lf_label = Product(product_id="lf1", name="Semi-skimmed milk", allergens=["milk"], labels=["free_from_lactose"])
    lf_name = Product(product_id="lf2", name="Laktoositon kevytmaito", allergens=["milk"])
    reg_milk = Product(product_id="m1", name="Semi-skimmed milk", allergens=["milk"])
    assert allergen_safe(lf_label, Constraint(lactose_free=True), [])
    assert allergen_safe(lf_name, Constraint(lactose_free=True), ["lactose"])
    assert not allergen_safe(reg_milk, Constraint(lactose_free=True), [])
    assert not allergen_safe(lf_label, Constraint(lactose_free=True), ["milk"])

    gf_label = Product(product_id="gf1", name="Oat bread", allergens=["oats"], labels=["free_from_gluten"])
    gf_name = Product(product_id="gf2", name="Gluten-free roll", allergens=["egg"])
    gf_wheat = Product(product_id="gf3", name="Gluten-free wheat mix", allergens=["wheat"], labels=["free_from_gluten"])
    reg_oat = Product(product_id="gf4", name="Oat bread", allergens=["oats"])
    assert allergen_safe(gf_label, Constraint(gluten_free=True), [])
    assert allergen_safe(gf_name, Constraint(gluten_free=True), [])
    assert not allergen_safe(gf_wheat, Constraint(gluten_free=True), [])
    assert not allergen_safe(reg_oat, Constraint(gluten_free=True), [])

    trace = Product(product_id="tr1", name="Seed bread", allergens=["oats", "may_contain:sesame"])
    assert not allergen_safe(trace, Constraint(), ["sesame"])
    assert not allergen_safe(trace, Constraint(exclude_allergens=["sesame"]), [])
    assert allergen_safe(trace, Constraint(), ["egg"])

    peanut_p = Product(product_id="n1", name="Satay sauce", allergens=["peanuts"])
    treenut_p = Product(product_id="n2", name="Granola", allergens=["tree nuts"])
    trace_nut = Product(product_id="n3", name="Muesli", allergens=["may_contain:tree nuts"])
    soy_p = Product(product_id="n4", name="Tofu", allergens=["soy"])
    assert not allergen_safe(peanut_p, Constraint(), ["nuts"])
    assert not allergen_safe(treenut_p, Constraint(), ["nuts"])
    assert not allergen_safe(trace_nut, Constraint(), ["nuts"])
    assert allergen_safe(soy_p, Constraint(), ["nuts"])

    no_meta = Product(product_id="nm1", name="Lactose-free gluten-free water")
    assert allergen_safe(no_meta, Constraint(), [])
    assert not allergen_safe(no_meta, Constraint(lactose_free=True), [])
    assert not allergen_safe(no_meta, Constraint(gluten_free=True), [])
    assert not allergen_safe(no_meta, Constraint(), ["egg"])

    assert not allergen_safe(lf_label, Constraint(), ["unknownium"])
    assert not allergen_safe(lf_label, Constraint(exclude_allergens=["mystery_allergen"]), [])


def test_offline_match_score():
    plain_en = Product(product_id="p1", name="Valio lactose free semi-skimmed milk 1 l", category="10400")
    plain_fi = Product(product_id="p2", name="Valio kevytmaito laktoositon 1 l", category="10400")
    assert match_score(MILK, plain_en) >= 0.8
    assert match_score(MILK, plain_fi) >= 0.8

    choc = Product(product_id="c1", name="Valio chocolate milk lactose free 1 l", category="10400")
    prot = Product(product_id="c2", name="Valio vanilla protein milk 250 ml", category="10400")
    prot_plain = Product(product_id="c3", name="Valio protein milk 1 l", category="10405")
    coffee = Product(product_id="c4", name="Valio coffee milk latte 250 ml", category="10400")
    for bad in (choc, prot, prot_plain, coffee):
        assert match_score(MILK, bad) <= 0.3, bad.name

    portion_pack = Product(product_id="pp1", name="Valio lactose free milk 100 x 2 cl", category="10400")
    portion_word = Product(product_id="pp2", name="Valio portion milk lactose free", category="10400")
    assert match_score(MILK, portion_pack) < 0.5
    assert match_score(MILK, portion_pack) < match_score(MILK, plain_en)
    assert match_score(MILK, portion_word) < match_score(MILK, plain_en)


def test_offline_rank_substitutes():
    orig = _offer("orig", "Valio lactose free milk 1 l", price=1.50, size_l=1.0, reliability=0.96)
    good = _offer("good1", "Valio lactose free semi-skimmed milk bag 10 l", price=11.00, size_l=10.0, reliability=0.85)
    better = _offer("bet1", "Valio lactose free milk carton 1 l", price=1.30, size_l=1.0, reliability=0.99)
    best = _offer("best1", "Valio milk lactose free", price=1.60, size_l=1.0, reliability=1.0)
    blocked = _offer("blk", "Valio lactose free milk 1 l", price=0.90, sales_block="STOP")
    zero = _offer("zero", "Valio lactose free milk 1 l", price=0.80, available=0)
    neg = _offer("neg", "Valio lactose free milk 1 l", price=0.70, available=-3)
    unsafe_lf = _offer("ulf", "Valio regular milk 1 l", price=0.60, labels=[])
    unsafe_soy = _offer("usoy", "Valio lactose free milk 1 l", price=0.50, allergens=["milk", "soy"])

    pool = [orig, blocked, zero, neg, unsafe_lf, unsafe_soy, good, better, best]
    res = rank_substitutes(MILK, pool, ["soy"], original=orig.product)
    assert [o.tier for o in res] == ["good", "better", "best"]
    assert [o.product_id for o in res] == ["good1", "bet1", "best1"]
    assert {"orig", "blk", "zero", "neg", "ulf", "usoy"}.isdisjoint({o.product_id for o in res})
    assert all(o.notes and "\n" not in o.notes[0] for o in res)
    assert rank_substitutes(MILK, pool, ["milk"], original=orig.product) == []


def test_offline_tradeoff():
    base = _offer("b1", "Valio lactose free milk 1 l", price=2.00, size_l=1.0, reliability=0.95, co2=1.2, sales_unit="PRK")
    alt = _offer("b2", "Valio lactose free milk 10 l", price=15.00, size_l=10.0, reliability=0.99, co2=1.05, sales_unit="ST")
    line = tradeoff(alt, base)
    assert "\n" not in line
    assert "25% cheaper per l" in line
    assert "10 l packs instead of 1 l packs" in line
    assert "reliability 0.99 vs 0.95" in line
    assert "CO2 1.05 vs 1.2 kg/kg" in line

    rev = tradeoff(base, alt)
    assert "\n" not in rev
    assert "33% pricier per l" in rev
    assert "1 l packs instead of 10 l packs" in rev

    same = tradeoff(base, base)
    assert "\n" not in same
    assert same == "same price per l"
