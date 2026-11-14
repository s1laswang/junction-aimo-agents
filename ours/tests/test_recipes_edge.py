import json
from datetime import date
from pathlib import Path

import pytest

from aimo.models import Constraint
from aimo.recipes import DISHES, _triggered, menu_to_need, recipe_swaps

TODAY = date(2026, 11, 14)
RAW_DISHES = json.loads((Path(__file__).resolve().parent.parent / "data_extra" / "recipes.json").read_text())["dishes"]
UNIT_MAP = {"g": ("kg", 1000), "ml": ("L", 1000), "pcs": ("pcs", 1)}


def by_query(need):
    return {i.query: i for i in need.items}


def test_all_eleven_dishes_present():
    assert len(RAW_DISHES) == 11
    assert len(DISHES) == 11
    assert [d["id"] for d in DISHES] == [
        "salmon-soup",
        "karelian-stew",
        "macaroni-casserole",
        "pancakes",
        "oat-porridge",
        "lasagne",
        "salad-bar",
        "meatballs-and-mashed-potatoes",
        "creamy-chicken-pasta",
        "rice-porridge",
        "carbonara",
    ]


@pytest.mark.parametrize("dish", RAW_DISHES, ids=[d["id"] for d in RAW_DISHES])
def test_all_dishes_linear_scaling_english_and_finnish(dish):
    en_alias = dish["en"]
    fi_alias = next((a for a in reversed(dish["aliases"]) if a != dish["en"]), dish["fi"])

    en_10 = by_query(menu_to_need(f"{en_alias} for 10 guests", "hotel-aurora", today=TODAY))
    en_30 = by_query(menu_to_need(f"{en_alias} for 30 guests", "hotel-aurora", today=TODAY))
    en_70 = by_query(menu_to_need(f"{en_alias} for 70 guests", "hotel-aurora", today=TODAY))
    fi_10 = by_query(menu_to_need(f"{fi_alias} 10 hengelle", "school-hervanta", today=TODAY))
    fi_30 = by_query(menu_to_need(f"{fi_alias} 30 hengelle", "school-hervanta", today=TODAY))

    expected_queries = {ing["query"] for ing in dish["ingredients"]}
    assert set(en_10) == expected_queries
    assert set(fi_10) == expected_queries

    for ing in dish["ingredients"]:
        q = ing["query"]
        out_unit, div = UNIT_MAP[ing["unit"]]
        base = round(ing["per_cover"] * 10 / div, 4)
        assert en_10[q].unit == out_unit
        assert en_10[q].quantity == pytest.approx(base)
        assert en_30[q].quantity == pytest.approx(base * 3)
        assert en_70[q].quantity == pytest.approx(base * 7)
        assert fi_10[q].quantity == pytest.approx(base)
        assert fi_30[q].quantity == pytest.approx(base * 3)


@pytest.mark.parametrize(
    "dish_id,alias,expected",
    [(d["id"], a, {i["query"] for i in d["ingredients"]}) for d in RAW_DISHES for a in d["aliases"]],
)
def test_every_alias_resolves_single_dish(dish_id, alias, expected):
    n1 = menu_to_need(f"{alias} for 20 guests", "hotel-aurora", today=TODAY)
    n2 = menu_to_need(f"20 {alias}", "hotel-aurora", today=TODAY)
    assert {i.query for i in n1.items} == expected, dish_id
    assert {i.query for i in n2.items} == expected, dish_id
    assert {i.query: i.quantity for i in n1.items} == {i.query: i.quantity for i in n2.items}


def test_stock_deduction_reduces_and_zeros_out():
    n = menu_to_need(
        "salmon soup for 100 guests",
        "hotel-aurora",
        today=TODAY,
        stock={"salmon fillet": 2.25, "potato": 10.0, "carrot": 25.0, "milk": 4.0},
    )
    got = by_query(n)
    assert got["salmon fillet"].quantity == 2.75
    assert got["milk"].quantity == 6.0
    assert got["cooking cream"].quantity == 6.0
    assert "potato" not in got
    assert "carrot" not in got

    full_stock = {
        "salmon fillet": 5.0,
        "potato": 10.0,
        "carrot": 3.0,
        "leek": 2.0,
        "cooking cream": 6.0,
        "milk": 10.0,
        "dill": 0.2,
        "butter": 0.5,
    }
    empty = menu_to_need("salmon soup for 100 guests", "hotel-aurora", today=TODAY, stock=full_stock)
    assert empty.items == []


def test_stock_deduction_after_allergen_swap():
    n = menu_to_need(
        "salmon soup for 100 guests",
        "hospital-tays",
        today=TODAY,
        stock={"oat drink": 3.5, "oat cooking": 6.0},
        constraints=Constraint(exclude_allergens=["milk"]),
    )
    got = by_query(n)
    assert got["oat drink"].quantity == 6.5
    assert "oat cooking" not in got
    assert got["lactose free margarine"].quantity == 0.5


def test_lactose_free_vs_milk_exclusion_swaps():
    lf = Constraint(lactose_free=True)
    no_milk = Constraint(exclude_allergens=["milk"])

    assert recipe_swaps("milk", lf)[0]["query"] == "lactose-free milk"
    assert recipe_swaps("cooking cream", lf)[0]["query"] == "lactose-free cream"
    assert recipe_swaps("butter", lf)[0]["query"] == "lactose-free butter"

    milk_swaps = recipe_swaps("milk", no_milk)
    cream_swaps = recipe_swaps("cooking cream", no_milk)
    butter_swaps = recipe_swaps("butter", no_milk)
    assert milk_swaps[0]["query"] == "oat drink"
    assert cream_swaps[0]["query"] == "oat cooking"
    assert butter_swaps[0]["query"] == "lactose free margarine"
    assert "lactose-free milk" not in {o["query"] for o in milk_swaps}
    assert "lactose-free cream" not in {o["query"] for o in cream_swaps}

    need_lf = by_query(menu_to_need("salmon soup for 20 guests", "hospital-tays", today=TODAY, constraints=lf))
    assert need_lf["lactose-free milk"].quantity == 2.0
    assert need_lf["lactose-free cream"].quantity == 1.2
    assert need_lf["lactose-free butter"].quantity == 0.1
    assert {"milk", "cooking cream", "oat drink", "oat cooking"}.isdisjoint(need_lf)

    need_df = by_query(menu_to_need("salmon soup for 20 guests", "hospital-tays", today=TODAY, constraints=no_milk))
    assert need_df["oat drink"].quantity == 2.0
    assert need_df["oat cooking"].quantity == 1.2
    assert need_df["lactose free margarine"].quantity == 0.1
    assert {"milk", "cooking cream", "lactose-free milk", "lactose-free cream"}.isdisjoint(need_df)
    assert all(i.constraints.exclude_allergens == ["milk"] for i in need_df.values())


@pytest.mark.parametrize("alias", ["milk", "dairy", "maito", " MILK ", " Dairy ", "MAITO"])
def test_dairy_exclusion_aliases_prefer_dairy_free(alias):
    c = Constraint(exclude_allergens=[alias])
    assert _triggered("lactose", c)
    assert recipe_swaps("milk", c)[0]["query"] == "oat drink"
    assert recipe_swaps("cooking cream", c)[0]["query"] == "oat cooking"
    assert recipe_swaps("butter", c)[0]["query"] == "lactose free margarine"


@pytest.mark.parametrize("alias", ["lactose", "laktoosi", " Laktoosi "])
def test_lactose_only_exclusion_aliases_use_lactose_free(alias):
    c = Constraint(exclude_allergens=[alias])
    assert _triggered("lactose", c)
    assert recipe_swaps("milk", c)[0]["query"] == "lactose-free milk"
    assert recipe_swaps("cooking cream", c)[0]["query"] == "lactose-free cream"


def test_menu_free_text_milk_vs_lactose():
    df_en = by_query(menu_to_need("salmon soup for 20 guests without milk", "hospital-tays", today=TODAY))
    df_fi = by_query(menu_to_need("lohikeitto 20 hengelle maidoton", "hospital-tays", today=TODAY))
    lf_en = by_query(menu_to_need("salmon soup for 20 guests lactose-free", "hospital-tays", today=TODAY))

    assert "oat drink" in df_en and "oat cooking" in df_en
    assert "oat drink" in df_fi and "oat cooking" in df_fi
    assert "lactose-free milk" in lf_en and "lactose-free cream" in lf_en


def test_wheat_exclusion_triggers_gluten_swaps():
    no_wheat = Constraint(exclude_allergens=["wheat"])
    assert recipe_swaps("pasta", no_wheat)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("macaroni", no_wheat)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("lasagne", no_wheat)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("wheat flour", no_wheat)[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("flour", no_wheat)[0]["query"] == "gluten-free flour mix"

    need = by_query(
        menu_to_need("lasagne and pancakes for 50 guests", "school-hervanta", today=TODAY, constraints=no_wheat)
    )
    assert need["gluten-free pasta"].quantity == 3.0
    assert need["gluten-free flour mix"].quantity == 2.9
    assert "lasagne" not in need
    assert "wheat flour" not in need
    assert all(i.constraints.exclude_allergens == ["wheat"] for i in need.values())

    pasta_need = by_query(
        menu_to_need(
            "chicken pasta and macaroni casserole for 20 guests",
            "school-hervanta",
            today=TODAY,
            constraints=no_wheat,
        )
    )
    assert pasta_need["gluten-free pasta"].quantity == 3.2
    assert "pasta" not in pasta_need
    assert "macaroni" not in pasta_need

    text_need = by_query(menu_to_need("chicken pasta and pancakes for 20 guests without wheat", "school-hervanta", today=TODAY))
    assert "gluten-free pasta" in text_need
    assert "gluten-free flour mix" in text_need


@pytest.mark.parametrize("alias", ["gluten", "wheat", "barley", "rye", "oats", "gluteeni", "vehnä", " VEHNÄ "])
def test_all_gluten_trigger_aliases(alias):
    c = Constraint(exclude_allergens=[alias])
    assert _triggered("gluten", c)
    assert recipe_swaps("pasta", c)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("wheat flour", c)[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("oat flakes", c)[0]["query"] == "gluten-free oats"
