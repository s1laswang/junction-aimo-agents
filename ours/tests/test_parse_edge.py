from datetime import date

import pytest

from aimo.models import Constraint, Need, NeedItem
from aimo.parse import describe_need, parse_need

TODAY = date(2026, 11, 14)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)


def parse(text: str) -> Need:
    need = parse_need(text, "hotel-aurora", TODAY)
    assert need.customer == "hotel-aurora"
    assert need.raw_text == text
    return need


@pytest.mark.parametrize(
    "text,expected_qty,expected_unit,expected_query",
    [
        ("1,000 L milk", 1000.0, "L", "milk"),
        ("1 000 L milk", 1000.0, "L", "milk"),
        ("1.000 L milk", 1000.0, "L", "milk"),
        ("2,5 kg butter", 2.5, "kg", "butter"),
        ("1,250.50 kg potatoes", 1250.5, "kg", "potatoes"),
        ("1 250,50 kg perunaa", 1250.5, "kg", "potatoes"),
    ],
)
def test_number_formats(text, expected_qty, expected_unit, expected_query):
    need = parse(text)
    assert len(need.items) == 1
    item = need.items[0]
    assert (item.quantity, item.unit, item.query) == (expected_qty, expected_unit, expected_query)


@pytest.mark.parametrize(
    "text,expected_qty,expected_unit,expected_query",
    [
        ("500 ml cream", 0.5, "L", "cream"),
        ("20 dl milk", 2.0, "L", "milk"),
        ("1.5 tonnes potatoes", 1500.0, "kg", "potatoes"),
        ("50 cl juice", 0.5, "L", "juice"),
        ("1,5 tonnia perunaa", 1500.0, "kg", "potatoes"),
    ],
)
def test_unit_normalization(text, expected_qty, expected_unit, expected_query):
    need = parse(text)
    assert len(need.items) == 1
    item = need.items[0]
    assert (item.quantity, item.unit, item.query) == (expected_qty, expected_unit, expected_query)


@pytest.mark.parametrize(
    "text,expected_date",
    [
        ("10 kg butter 2026-11-20", date(2026, 11, 20)),
        ("10 kg voita 20.11.", date(2026, 11, 20)),
        ("10 kg butter tomorrow", date(2026, 11, 15)),
        ("10 kg voita huomenna", date(2026, 11, 15)),
        ("10 kg butter day after tomorrow", date(2026, 11, 16)),
        ("10 kg voita ylihuomenna", date(2026, 11, 16)),
        ("10 kg butter asap", date(2026, 11, 15)),
        ("10 kg voita heti", date(2026, 11, 15)),
        ("10 kg butter in 3 days", date(2026, 11, 17)),
        ("10 kg voita 3 päivän päästä", date(2026, 11, 17)),
        ("10 kg butter by the end of the week", date(2026, 11, 20)),
        ("10 kg voita viikon loppuun mennessä", date(2026, 11, 20)),
        ("10 kg butter next week Monday", date(2026, 11, 16)),
        ("10 kg voita ensi viikon maanantaina", date(2026, 11, 16)),
    ],
)
def test_date_parsing_edge_cases(text, expected_date):
    need = parse(text)
    assert need.deliver_by == expected_date
    assert len(need.items) == 1
    assert (need.items[0].query, need.items[0].quantity, need.items[0].unit) == ("butter", 10.0, "kg")


def test_multi_item_trailing_constraints():
    need = parse("100 L milk and 20 kg butter, lactose-free, Finnish, Valio")
    assert [(i.query, i.quantity, i.unit) for i in need.items] == [
        ("milk", 100.0, "L"),
        ("butter", 20.0, "kg"),
    ]
    for item in need.items:
        assert item.constraints.lactose_free is True
        assert item.constraints.origin == "fi"
        assert item.constraints.brands == ["Valio"]


def test_multi_item_allergen_propagation():
    trailing = parse("100 L milk and 20 kg butter, no nuts, without soy")
    assert len(trailing.items) == 2
    for item in trailing.items:
        assert set(item.constraints.exclude_allergens) == {"nuts", "soy"}

    scoped_and_global = parse("50 kg gluten-free flour without egg and 20 L milk")
    assert [i.query for i in scoped_and_global.items] == ["flour", "milk"]
    assert all(i.constraints.exclude_allergens == ["egg"] for i in scoped_and_global.items)
    assert scoped_and_global.items[0].constraints.gluten_free is True
    assert scoped_and_global.items[1].constraints.gluten_free is None

    finnish = parse("30 kg munatonta pastaa ja 10 l kermaa, pähkinätön")
    assert [i.query for i in finnish.items] == ["pasta", "cream"]
    for item in finnish.items:
        assert set(item.constraints.exclude_allergens) == {"egg", "nuts"}


def test_menu_lohikeitto():
    need = parse_need("We need to cook lohikeitto for 50 guests by Tuesday", "hotel-aurora", TODAY)
    assert need.customer == "hotel-aurora"
    assert need.deliver_by == date(2026, 11, 17)
    by_query = {i.query: (i.quantity, i.unit) for i in need.items}
    assert by_query["salmon fillet"] == (2.5, "kg")
    assert by_query["potato"] == (5.0, "kg")
    assert by_query["carrot"] == (1.5, "kg")
    assert by_query["leek"] == (1.0, "kg")
    assert by_query["cooking cream"] == (3.0, "L")
    assert by_query["milk"] == (5.0, "L")
    assert by_query["dill"] == (0.1, "kg")
    assert by_query["butter"] == (0.25, "kg")


def test_menu_lasagne():
    need = parse_need("lasagne for 40 portions", "hotel-aurora", TODAY)
    assert need.customer == "hotel-aurora"
    assert need.deliver_by == date(2026, 11, 16)
    by_query = {i.query: (i.quantity, i.unit) for i in need.items}
    assert by_query["lasagne"] == (2.4, "kg")
    assert by_query["ground beef"] == (3.2, "kg")
    assert by_query["crushed tomatoes"] == (3.2, "kg")
    assert by_query["grated cheese"] == (1.6, "kg")
    assert by_query["milk"] == (4.0, "L")
    assert by_query["wheat flour"] == (0.32, "kg")
    assert by_query["butter"] == (0.32, "kg")
    assert by_query["onion"] == (0.8, "kg")


def test_describe_need_single_bilingual():
    need = parse("100 L organic Finnish lactose-free milk Valio under 1.8 EUR by Friday")
    assert (
        describe_need(need, "en")
        == "100 L milk, lactose-free, organic, Finnish, Valio, max 1.8 EUR, deliver by Fri 20 Nov 2026"
    )
    assert (
        describe_need(need, "fi")
        == "100 L maito, laktoositon, luomu, kotimainen, Valio, max 1.8 EUR, toimitus pe 20.11.2026"
    )


def test_describe_need_multi_bilingual():
    need = Need(
        customer="hotel-aurora",
        deliver_by=date(2026, 11, 17),
        items=[
            NeedItem(
                query="flour",
                quantity=25.0,
                unit="kg",
                constraints=Constraint(
                    gluten_free=True,
                    exclude_allergens=["nuts", "egg"],
                    labels=["organic"],
                    origin="fi",
                    brands=["Fazer"],
                    max_unit_price=2.5,
                ),
            ),
            NeedItem(
                query="butter",
                quantity=10.0,
                unit="kg",
                constraints=Constraint(
                    lactose_free=True,
                    exclude_allergens=["nuts", "egg"],
                    origin="fi",
                    brands=["Valio", "Arla"],
                    max_unit_price=9.0,
                ),
            ),
        ],
    )
    assert describe_need(need, "en") == (
        "25 kg flour, gluten-free, no nuts, no egg, organic, Finnish, Fazer, max 2.5 EUR; "
        "10 kg butter, lactose-free, no nuts, no egg, Finnish, Valio, Arla, max 9 EUR; "
        "deliver by Tue 17 Nov 2026"
    )
    assert describe_need(need, "fi") == (
        "25 kg jauho, gluteeniton, ilman: pähkinä, ilman: muna, luomu, kotimainen, Fazer, max 2.5 EUR; "
        "10 kg voi, laktoositon, ilman: pähkinä, ilman: muna, kotimainen, Valio, Arla, max 9 EUR; "
        "toimitus ti 17.11.2026"
    )
