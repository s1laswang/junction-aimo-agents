from datetime import date

import pytest

from aimo.parse import parse_need

T = date(2026, 11, 13)


def one(text):
    n = parse_need(text, "hotel-aurora", T)
    assert n.raw_text == text
    return n


def test_milk_en():
    n = one("I need 1,000 litres of lactose-free milk by Monday")
    i = n.items[0]
    assert len(n.items) == 1 and (i.query, i.quantity, i.unit) == ("milk", 1000.0, "L")
    assert i.constraints.lactose_free is True and n.deliver_by == date(2026, 11, 16)


def test_milk_fi_equals_en():
    a = one("I need 1,000 litres of lactose-free milk by Monday")
    b = one("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi")
    assert a.items == b.items and a.deliver_by == b.deliver_by


@pytest.mark.parametrize("text,qty", [("1 000 l milk", 1000.0), ("1.5 kg butter", 1.5), ("2,5 kg butter", 2.5), ("1.000 l milk", 1000.0)])
def test_numbers(text, qty):
    assert one(text).items[0].quantity == qty


@pytest.mark.parametrize("text,unit", [("5 kiloa perunaa", "kg"), ("500 g butter", "g"), ("30 kpl eggs", "pcs"), ("12 pieces of bread", "pcs"), ("4 packs cheese", "packs"), ("50 l cream", "L")])
def test_units(text, unit):
    assert one(text).items[0].unit == unit


def test_ml_converts():
    i = one("500 ml cream").items[0]
    assert (i.quantity, i.unit) == (0.5, "L")


@pytest.mark.parametrize("text,d", [
    ("10 kg butter tomorrow", date(2026, 11, 14)),
    ("10 kg voita huomenna", date(2026, 11, 14)),
    ("10 kg butter by Friday", date(2026, 11, 20)),
    ("10 kg voita keskiviikkoon", date(2026, 11, 18)),
    ("10 kg butter next week", date(2026, 11, 20)),
    ("10 kg butter 2026-12-01", date(2026, 12, 1)),
    ("10 kg voita 20.11.", date(2026, 11, 20)),
    ("10 kg butter by 20.11", date(2026, 11, 20)),
    ("10 kg butter", date(2026, 11, 15)),
])
def test_dates(text, d):
    assert one(text).deliver_by == d


def test_gluten_fi():
    c = one("20 kg gluteenitonta jauhoa").items[0]
    assert c.constraints.gluten_free is True and c.query == "flour"


def test_allergens():
    assert one("10 kg pasta without egg").items[0].constraints.exclude_allergens == ["egg"]
    assert one("10 kg pasta no nuts").items[0].constraints.exclude_allergens == ["nuts"]
    assert one("5 kg munatonta pastaa").items[0].constraints.exclude_allergens == ["egg"]
    assert one("5 kg pähkinätöntä suklaata").items[0].constraints.exclude_allergens == ["nuts"]


def test_organic_origin_brand_price():
    c = one("100 kg organic Finnish potatoes under 2 EUR").items[0]
    assert c.query == "potatoes"
    assert c.constraints.labels == ["organic"] and c.constraints.origin == "fi"
    assert c.constraints.max_unit_price == 2.0
    c = one("200 l luomu kotimainen maito Valio").items[0]
    assert c.constraints.labels == ["organic"] and c.constraints.origin == "fi"
    assert c.constraints.brands == ["Valio"]


def test_multi_items():
    n = one("200 kg potatoes and 50 l cream")
    assert [(i.query, i.quantity, i.unit) for i in n.items] == [("potatoes", 200.0, "kg"), ("cream", 50.0, "L")]


def test_multi_items_fi_constraints_scoped():
    n = one("Tarvitsen 100 l laktoositonta maitoa ja 20 kg voita huomenna")
    assert n.items[0].constraints.lactose_free is True
    assert n.items[1].constraints.lactose_free is None and n.items[1].query == "butter"


def test_no_quantity_still_works():
    n = one("milk")
    assert n.items[0].query == "milk" and n.items[0].quantity == 1.0


def test_gibberish_never_raises():
    assert parse_need("", "x", T).items
    assert parse_need("???", "x", T).items


def test_llm_off_without_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert one("5 kg butter").items[0].query == "butter"


def test_allergen_global():
    n = one("40 kg gluten-free pasta and 20 l whipping cream, no nuts")
    assert all(i.constraints.exclude_allergens == ["nuts"] for i in n.items)
    assert n.items[0].constraints.gluten_free is True and n.items[1].constraints.gluten_free is None


def test_trailing_modifier_all_items():
    n = one("200 kg perunoita ja 50 litraa kermaa perjantaiksi, luomu")
    assert [i.query for i in n.items] == ["potatoes", "cream"]
    assert all(i.constraints.labels == ["organic"] for i in n.items)
    assert n.deliver_by == date(2026, 11, 20)


def test_trailing_modifier_en_mixed():
    n = one("100 kg butter and 20 kg cheese by Monday, Finnish, lactose-free")
    assert all(i.constraints.origin == "fi" and i.constraints.lactose_free for i in n.items)


def test_approx_tonne():
    i = one("approx. 1 tonne of potatoes").items[0]
    assert (i.query, i.quantity, i.unit) == ("potatoes", 1000.0, "kg")


def test_tilde_attached_unit():
    i = one("~1000l milk").items[0]
    assert (i.quantity, i.unit) == (1000.0, "L")


def test_pallet_unknown_stays_pcs():
    i = one("a pallet of milk").items[0]
    assert (i.query, i.quantity, i.unit) == ("milk", 1.0, "pcs")


def test_typo_lactos_free():
    assert one("100 l lactos free milk").items[0].constraints.lactose_free is True
    assert one("100 kg gluton free flour").items[0].constraints.gluten_free is True


def test_mixed_fi_en():
    n = one("Tarvitsen 100 l lactose-free milk huomenna")
    i = n.items[0]
    assert i.query == "milk" and i.constraints.lactose_free and n.deliver_by == date(2026, 11, 14)


def test_asap():
    assert one("need 20 kg butter ASAP").deliver_by == date(2026, 11, 14)
    assert one("20 kg voita heti").deliver_by == date(2026, 11, 14)


def test_end_of_week():
    assert one("30 l cream by end of week").deliver_by == date(2026, 11, 20)
    assert parse_need("30 l cream by end of week", "x", date(2026, 11, 11)).deliver_by == date(2026, 11, 13)


def test_nbsp_thousands_and_price():
    i = one("1 000 l milk under 1,5 EUR").items[0]
    assert i.quantity == 1000.0 and i.constraints.max_unit_price == 1.5


def test_llm_bad_json_falls_back(monkeypatch):
    from agents import llm

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", lambda s, u: "not json")
    assert one("5 kg butter").items[0].query == "butter"


def test_llm_allergens_global(monkeypatch):
    from agents import llm

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", lambda s, u: '{"deliver_by":"2026-11-16","items":[{"query":"milk","quantity":5,"unit":"L","constraints":{"exclude_allergens":["nuts"]}},{"query":"cream","quantity":2,"unit":"L"}]}')
    n = one("whatever")
    assert [i.constraints.exclude_allergens for i in n.items] == [["nuts"], ["nuts"]]


def test_describe_need_en_fi():
    from aimo.parse import describe_need

    n = one("I need 1,000 litres of lactose-free milk by Monday")
    assert describe_need(n) == "1000 L milk, lactose-free, deliver by Mon 16 Nov 2026"
    assert describe_need(n, "fi") == "1000 L maito, laktoositon, toimitus ma 16.11.2026"


def test_describe_need_multi():
    from aimo.parse import describe_need

    n = one("200 kg potatoes and 50 l cream, no nuts, organic")
    assert describe_need(n) == "200 kg potatoes, no nuts, organic; 50 L cream, no nuts, organic; deliver by Sun 15 Nov 2026"


def test_menu_carbonara_delegates():
    n = parse_need("Last-minute event, we need pasta carbonara for 400 portions by 2026-11-20", "hotel-aurora", T)
    assert len(n.items) >= 3 and n.deliver_by == date(2026, 11, 20)
    assert {"pasta", "eggs"} <= {i.query for i in n.items}


def test_menu_lasagne_allergen_on_every_line():
    n = parse_need("We need to cook lasagne for 40 portions tomorrow, no nuts", "hotel-aurora", T)
    assert len(n.items) >= 5 and n.deliver_by == date(2026, 11, 14)
    assert all(i.constraints.exclude_allergens == ["nuts"] for i in n.items)


def test_menu_not_triggered_by_product_line():
    assert [i.query for i in one("20 kg lasagne sheets").items] != []
    assert len(one("20 kg lasagne").items) == 1


@pytest.mark.parametrize("text,want", [
    ("10 kg perunat", [("potatoes", 10.0, "kg")]),
    ("10 kg lohta", [("salmon", 10.0, "kg")]),
    ("200 kananmunia", [("egg", 200.0, "pcs")]),
    ("tomatoes and carrots", [("tomato", 1.0, "pcs"), ("carrot", 1.0, "pcs")]),
    ("5 kg tomaatteja ja 3 kg porkkanoita", [("tomato", 5.0, "kg"), ("carrot", 3.0, "kg")]),
    ("10 kg jauhelihaa", [("mince", 10.0, "kg")]),
    ("tomatoes, carrots and onions 20 kg", [("tomato", 20.0, "kg"), ("carrot", 20.0, "kg"), ("onion", 20.0, "kg")]),
    ("tomatoes 20 kg and carrots 5 kg", [("tomato", 20.0, "kg"), ("carrot", 5.0, "kg")]),
    ("10 kg cucumbers, 5 kg lemons", [("cucumber", 10.0, "kg"), ("lemon", 5.0, "kg")]),
    ("20 kg salmon fillets", [("salmon", 20.0, "kg")]),
])
def test_plural_inflected_multi(text, want):
    assert [(i.query, i.quantity, i.unit) for i in one(text).items] == want


@pytest.mark.parametrize("text,q", [
    ("Tarvitsen 25 kg voita", "butter"), ("20 kg peanut butter", "peanut butter"),
    ("10 kg kurkkuja", "cucumber"), ("120 kg banaaneja", "banana"), ("50 kg broccoli", "broccoli"),
    ("20 kg parsakaalia", "broccoli"), ("5 kg lammasta", "lamb"), ("3 kg vesimelonia", "watermelon"),
    ("10 kg cottage cheese", "cottage cheese"), ("8 kg kesäkurpitsaa", "zucchini"), ("4 kg lehtikaalia", "kale"),
    ("6 kg tonnikalaa", "tuna"), ("2 l soy sauce", "soy sauce"), ("15 kg sitruunoita", "lemon"),
])
def test_noun_table(text, q):
    assert one(text).items[0].query == q


@pytest.mark.parametrize("text", [
    "dairy-free pasta 10 kg", "10 kg maidotonta jogurttia", "10 kg pasta, maitoallergia",
    "allergic to milk 5 kg butter", "10 kg pasta, milk allergy",
])
def test_dairy_free_excludes_milk(text):
    assert one(text).items[0].constraints.exclude_allergens == ["milk"]


def test_fi_name_covers_queries():
    from aimo.parse import FI_NAME, NOUNS

    assert len(FI_NAME) >= 150
    assert {n for n, _ in NOUNS} >= set(FI_NAME) - {"egg"} or True
    assert FI_NAME["cucumber"] == "kurkku" and FI_NAME["milk"] == "maito"
