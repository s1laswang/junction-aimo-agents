import json
from datetime import date
from pathlib import Path

import pytest

from aimo import recipes
from aimo.models import Constraint
from aimo.recipes import DISHES, SWAPS, menu_to_need, recipe_swaps

TODAY = date(2026, 11, 14)
RECIPES_PATH = Path(__file__).resolve().parent.parent / "data_extra" / "recipes.json"
UNIT_MAP = {"g": ("kg", 1000), "ml": ("L", 1000), "pcs": ("pcs", 1)}


def items_by_query(need):
    return {item.query: item for item in need.items}


def test_recipes_json_matches_loaded_constants():
    raw = json.loads(RECIPES_PATH.read_text(encoding="utf-8"))
    assert raw["dishes"] == DISHES
    assert raw["swaps"] == SWAPS
    assert len(DISHES) >= 11
    assert len(SWAPS) >= 7
    dish_ids = [d["id"] for d in DISHES]
    swap_ids = [s["id"] for s in SWAPS]
    assert len(dish_ids) == len(set(dish_ids))
    assert len(swap_ids) == len(set(swap_ids))


@pytest.mark.parametrize("dish", DISHES, ids=[d["id"] for d in DISHES])
def test_dishes_integrity(dish):
    for key in ("id", "en", "fi"):
        assert isinstance(dish.get(key), str) and dish[key].strip()
    assert isinstance(dish.get("aliases"), list) and dish["aliases"]
    for alias in dish["aliases"]:
        assert isinstance(alias, str) and alias.strip()
    assert isinstance(dish.get("ingredients"), list) and dish["ingredients"]
    seen_queries = set()
    for ing in dish["ingredients"]:
        assert isinstance(ing.get("query"), str) and ing["query"].strip()
        assert ing["query"] not in seen_queries
        seen_queries.add(ing["query"])
        per_cover = ing.get("per_cover")
        assert isinstance(per_cover, (int, float)) and not isinstance(per_cover, bool)
        assert per_cover > 0
        assert ing.get("unit") in ("g", "ml", "pcs")


@pytest.mark.parametrize("rule", SWAPS, ids=[r["id"] for r in SWAPS])
def test_swaps_integrity(rule):
    assert isinstance(rule.get("id"), str) and rule["id"].strip()
    assert isinstance(rule.get("match"), list) and rule["match"]
    for term in rule["match"]:
        assert isinstance(term, str) and term.strip()
        assert term == term.lower().strip()
    assert rule.get("trigger") in ("lactose", "gluten")
    assert isinstance(rule.get("options"), list) and rule["options"]
    for opt in rule["options"]:
        assert isinstance(opt.get("query"), str) and opt["query"].strip()
        ratio = opt.get("ratio")
        assert isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
        assert ratio > 0
        assert isinstance(opt.get("why"), str) and opt["why"].strip()


@pytest.mark.parametrize(
    "dish,label",
    [
        (dish, label)
        for dish in DISHES
        for label in dict.fromkeys([dish["en"], dish["fi"], *dish["aliases"]])
    ],
)
def test_every_dish_resolves_by_en_fi_and_aliases(dish, label):
    need_en = menu_to_need(f"{label} for 40 guests", "hotel-aurora", today=TODAY)
    need_fi = menu_to_need(f"{label.upper()} 40 hengelle", "school-hervanta", today=TODAY)
    got_en = items_by_query(need_en)
    got_fi = items_by_query(need_fi)
    expected_queries = {ing["query"] for ing in dish["ingredients"]}
    assert set(got_en) == expected_queries
    assert set(got_fi) == expected_queries
    for ing in dish["ingredients"]:
        out_unit, div = UNIT_MAP[ing["unit"]]
        expected_qty = round(ing["per_cover"] * 40 / div, 4)
        assert got_en[ing["query"]].unit == out_unit
        assert got_en[ing["query"]].quantity == pytest.approx(expected_qty)
        assert got_fi[ing["query"]].unit == out_unit
        assert got_fi[ing["query"]].quantity == pytest.approx(expected_qty)


@pytest.mark.parametrize(
    "text,covers",
    [
        ("salmon soup for 80 guests", 80),
        ("for 80 salmon soup", 80),
        ("salmon soup for 120 people", 120),
        ("salmon soup, 120 people", 120),
        ("salmon soup, 50 portions", 50),
        ("50 portions of salmon soup", 50),
        ("salmon soup 100 pax", 100),
        ("salmon soup for 70 persons", 70),
        ("salmon soup for 65 covers", 65),
        ("lohikeitto 90 hengelle", 90),
        ("lohikeitto 40 henkilölle", 40),
        ("lohikeitto 40 henkilolle", 40),
        ("lohikeitto 60 annosta", 60),
        ("60 annosta lohikeitto", 60),
        ("lohikeitto 35 hengen", 35),
        ("lohikeitto 45 henkeä", 45),
        ("lohikeitto 10 annos", 10),
    ],
)
def test_cover_extraction_patterns_en_and_fi(text, covers):
    got = items_by_query(menu_to_need(text, "hotel-aurora", today=TODAY))
    assert got["salmon fillet"].quantity == pytest.approx(covers * 50 / 1000)
    assert got["milk"].quantity == pytest.approx(covers * 100 / 1000)
    assert got["potato"].quantity == pytest.approx(covers * 100 / 1000)


def test_per_dish_overrides_and_mixed_defaults():
    pair = items_by_query(menu_to_need("60 lasagne and 40 salad bar", "hotel-aurora", today=TODAY))
    assert pair["lasagne"].quantity == pytest.approx(3.6)
    assert pair["ground beef"].quantity == pytest.approx(4.8)
    assert pair["milk"].quantity == pytest.approx(6.0)
    assert pair["iceberg lettuce"].quantity == pytest.approx(1.6)
    assert pair["tomatoes"].quantity == pytest.approx(2.0)
    assert pair["rapeseed oil"].quantity == pytest.approx(0.32)

    pancakes = items_by_query(menu_to_need("30x pancakes", "hotel-aurora", today=TODAY))
    assert pancakes["milk"].quantity == pytest.approx(4.5)
    assert pancakes["wheat flour"].quantity == pytest.approx(1.5)
    assert pancakes["eggs"].quantity == pytest.approx(1.2)
    assert pancakes["butter"].quantity == pytest.approx(0.3)
    assert pancakes["sugar"].quantity == pytest.approx(0.15)

    mixed = items_by_query(
        menu_to_need("30x pancakes and salmon soup for 80 guests", "hotel-aurora", today=TODAY)
    )
    assert mixed["salmon fillet"].quantity == pytest.approx(4.0)
    assert mixed["wheat flour"].quantity == pytest.approx(1.5)
    assert mixed["milk"].quantity == pytest.approx(12.5)
    assert mixed["butter"].quantity == pytest.approx(0.7)

    override_both = items_by_query(
        menu_to_need("60 lasagne and 40 salad bar for 100 guests", "hotel-aurora", today=TODAY)
    )
    assert override_both["lasagne"].quantity == pytest.approx(3.6)
    assert override_both["iceberg lettuce"].quantity == pytest.approx(1.6)

    fi_override = items_by_query(
        menu_to_need("25 portions of lasagne ja 15 annosta karjalanpaisti", "school-hervanta", today=TODAY)
    )
    assert fi_override["lasagne"].quantity == pytest.approx(1.5)
    assert fi_override["pork shoulder"].quantity == pytest.approx(1.2)
    assert fi_override["onion"].quantity == pytest.approx(0.8)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "salmon soup",
        "salmon soup and pancakes on Friday",
        "lohikeitto ja karjalanpaisti perjantaiksi",
        "sushi and ramen for 80 guests",
        "salmon soup for 0 guests",
        "0x pancakes",
    ],
)
def test_missing_or_zero_covers_produce_empty_items_safely(text):
    need = menu_to_need(text, "hotel-aurora", today=TODAY)
    assert need.customer == "hotel-aurora"
    assert need.raw_text == text
    assert need.items == []


def test_overlapping_alias_substrings_never_double_count():
    rice_only = items_by_query(menu_to_need("rice porridge for 50 guests", "hotel-aurora", today=TODAY))
    assert "rice" in rice_only
    assert "oat flakes" not in rice_only
    assert rice_only["milk"].quantity == pytest.approx(12.5)
    assert rice_only["butter"].quantity == pytest.approx(0.15)

    oat_only = items_by_query(menu_to_need("oat porridge for 50 guests", "hotel-aurora", today=TODAY))
    assert oat_only["oat flakes"].quantity == pytest.approx(2.0)
    assert oat_only["milk"].quantity == pytest.approx(7.5)

    chicken_pasta = items_by_query(menu_to_need("creamy chicken pasta for 50 guests", "hotel-aurora", today=TODAY))
    assert chicken_pasta["chicken fillet"].quantity == pytest.approx(5.0)
    assert chicken_pasta["pasta"].quantity == pytest.approx(4.5)

    meatballs = items_by_query(
        menu_to_need("meatballs and mashed potatoes for 50 guests", "hotel-aurora", today=TODAY)
    )
    assert meatballs["meatballs"].quantity == pytest.approx(6.0)
    assert meatballs["potato"].quantity == pytest.approx(10.0)

    carbonara = items_by_query(
        menu_to_need("spaghetti carbonara for 50 guests", "hotel-aurora", today=TODAY)
    )
    assert carbonara["pasta"].quantity == pytest.approx(5.0)

    duplicate_aliases = items_by_query(
        menu_to_need("pancakes, pannukakut and letut for 20 guests", "hotel-aurora", today=TODAY)
    )
    assert duplicate_aliases["milk"].quantity == pytest.approx(3.0)
    assert duplicate_aliases["wheat flour"].quantity == pytest.approx(1.0)

    both_porridges = items_by_query(
        menu_to_need("rice porridge and porridge for 20 guests", "hotel-aurora", today=TODAY)
    )
    assert both_porridges["rice"].quantity == pytest.approx(0.8)
    assert both_porridges["oat flakes"].quantity == pytest.approx(0.8)
    assert both_porridges["milk"].quantity == pytest.approx(8.0)


def test_unit_conversion_g_ml_pcs_and_ceiling_rounding(monkeypatch):
    one_cover_soup = items_by_query(menu_to_need("salmon soup for 1 guest", "hotel-aurora", today=TODAY))
    assert one_cover_soup["salmon fillet"].unit == "kg"
    assert one_cover_soup["salmon fillet"].quantity == 0.05
    assert one_cover_soup["dill"].unit == "kg"
    assert one_cover_soup["dill"].quantity == 0.01
    assert one_cover_soup["butter"].unit == "kg"
    assert one_cover_soup["butter"].quantity == 0.01
    assert one_cover_soup["leek"].quantity == 0.02
    assert one_cover_soup["cooking cream"].unit == "L"
    assert one_cover_soup["cooking cream"].quantity == 0.06
    assert one_cover_soup["milk"].unit == "L"
    assert one_cover_soup["milk"].quantity == 0.1

    one_cover_salad = items_by_query(menu_to_need("salad bar for 1 guest", "hotel-aurora", today=TODAY))
    assert one_cover_salad["rapeseed oil"].unit == "L"
    assert one_cover_salad["rapeseed oil"].quantity == 0.01

    two_cover_salad = items_by_query(menu_to_need("salad bar for 2 guests", "hotel-aurora", today=TODAY))
    assert two_cover_salad["rapeseed oil"].quantity == 0.02

    float_exact = items_by_query(
        menu_to_need("salmon soup for 3 guests", "hotel-aurora", today=TODAY, stock={"milk": 0.2})
    )
    assert float_exact["milk"].quantity == 0.1

    float_ceil = items_by_query(
        menu_to_need("salmon soup for 3 guests", "hotel-aurora", today=TODAY, stock={"milk": 0.199})
    )
    assert float_ceil["milk"].quantity == 0.11

    synthetic_dishes = [
        {
            "id": "breakfast-box",
            "en": "breakfast box",
            "fi": "aamiaislaatikko",
            "aliases": ["breakfast box", "aamiaislaatikko"],
            "ingredients": [
                {"query": "boiled egg", "per_cover": 2, "unit": "pcs"},
                {"query": "apple", "per_cover": 1.253, "unit": "pcs"},
                {"query": "milk", "per_cover": 200, "unit": "ml"},
                {"query": "butter", "per_cover": 7, "unit": "g"},
            ],
        }
    ]
    monkeypatch.setattr(recipes, "DISHES", synthetic_dishes)
    box = items_by_query(
        menu_to_need(
            "breakfast box for 10 guests",
            "hotel-aurora",
            today=TODAY,
            stock={"boiled egg": 5, "butter": 0.07},
        )
    )
    assert box["boiled egg"].unit == "pcs"
    assert box["boiled egg"].quantity == 15.0
    assert box["apple"].unit == "pcs"
    assert box["apple"].quantity == 12.53
    assert box["milk"].unit == "L"
    assert box["milk"].quantity == 2.0
    assert "butter" not in box

    box_stocked = items_by_query(
        menu_to_need(
            "aamiaislaatikko 10 hengelle",
            "hotel-aurora",
            today=TODAY,
            stock={"boiled egg": 25, "apple": 12.53},
        )
    )
    assert "boiled egg" not in box_stocked
    assert "apple" not in box_stocked


def test_pantry_stock_partial_exact_surplus_and_post_swap():
    partial = items_by_query(
        menu_to_need(
            "salmon soup for 50 guests",
            "hotel-aurora",
            today=TODAY,
            stock={
                "salmon fillet": 1.111,
                "potato": 5.0,
                "carrot": 9.0,
                "unused pantry item": 100.0,
            },
        )
    )
    assert partial["salmon fillet"].quantity == 1.39
    assert "potato" not in partial
    assert "carrot" not in partial
    assert partial["milk"].quantity == 5.0

    swapped_stock = items_by_query(
        menu_to_need(
            "pancakes for 20 guests",
            "hospital-tays",
            today=TODAY,
            stock={"milk": 10.0, "lactose-free milk": 1.25, "gluten-free flour mix": 1.0},
            constraints=Constraint(lactose_free=True, gluten_free=True),
        )
    )
    assert swapped_stock["lactose-free milk"].quantity == 1.75
    assert "gluten-free flour mix" not in swapped_stock
    assert "milk" not in swapped_stock
    assert "wheat flour" not in swapped_stock


def test_lactose_free_and_lactose_allergen_swaps():
    lf_constraint = Constraint(lactose_free=True)
    assert recipe_swaps("milk", lf_constraint)[0]["query"] == "lactose-free milk"
    assert recipe_swaps("cream", lf_constraint)[0]["query"] == "lactose-free cream"
    assert recipe_swaps("cooking cream", lf_constraint)[0]["query"] == "lactose-free cream"
    assert recipe_swaps("whipping cream", lf_constraint)[0]["query"] == "lactose-free cream"
    assert recipe_swaps("butter", lf_constraint)[0]["query"] == "lactose-free butter"
    assert recipe_swaps("cheese", lf_constraint)[0]["query"] == "lactose-free cheese"
    assert recipe_swaps("grated cheese", lf_constraint)[0]["query"] == "lactose-free cheese"

    need_lf = items_by_query(
        menu_to_need(
            "salmon soup and lasagne for 20 guests",
            "hospital-tays",
            today=TODAY,
            constraints=lf_constraint,
        )
    )
    swapped_dairy = {
        "lactose-free milk",
        "lactose-free cream",
        "lactose-free butter",
        "lactose-free cheese",
    }
    assert swapped_dairy <= set(need_lf)
    assert {"milk", "cooking cream", "butter", "grated cheese"}.isdisjoint(need_lf)
    for q in swapped_dairy:
        assert need_lf[q].constraints.lactose_free is True
    for q in ("salmon fillet", "potato", "ground beef", "lasagne", "wheat flour"):
        assert need_lf[q].constraints.lactose_free is None

    for lactose_tag in ("lactose", "laktoosi", " LACTOSE ", "Laktoosi"):
        c_lac = Constraint(exclude_allergens=[lactose_tag])
        assert recipe_swaps("milk", c_lac)[0]["query"] == "lactose-free milk"
        assert recipe_swaps("cooking cream", c_lac)[0]["query"] == "lactose-free cream"
        assert recipe_swaps("butter", c_lac)[0]["query"] == "lactose-free butter"
        assert recipe_swaps("grated cheese", c_lac)[0]["query"] == "lactose-free cheese"
        need_lac = items_by_query(
            menu_to_need("salmon soup for 10 guests", "hospital-tays", today=TODAY, constraints=c_lac)
        )
        assert {"lactose-free milk", "lactose-free cream", "lactose-free butter"} <= set(need_lac)
        assert {"oat drink", "oat cooking"}.isdisjoint(need_lac)
        for item in need_lac.values():
            assert item.constraints.exclude_allergens == [lactose_tag]

    text_fi = items_by_query(menu_to_need("lohikeitto 20 hengelle laktoositon", "hospital-tays", today=TODAY))
    assert {"lactose-free milk", "lactose-free cream", "lactose-free butter"} <= set(text_fi)
    assert text_fi["lactose-free milk"].constraints.lactose_free is True
    assert text_fi["salmon fillet"].constraints.lactose_free is None


@pytest.mark.parametrize("dairy_tag", ["milk", "dairy", "maito", " MILK ", "Dairy", " maito "])
def test_strict_dairy_exclusion_prefers_dairy_free_options(dairy_tag):
    c_dairy = Constraint(exclude_allergens=[dairy_tag])
    milk_opts = recipe_swaps("milk", c_dairy)
    cream_opts = recipe_swaps("cooking cream", c_dairy)
    whip_opts = recipe_swaps("whipping cream", c_dairy)
    butter_opts = recipe_swaps("butter", c_dairy)
    cheese_opts = recipe_swaps("grated cheese", c_dairy)

    assert milk_opts[0]["query"] == "oat drink"
    assert all("dairy free" in o["why"] for o in milk_opts)
    assert cream_opts[0]["query"] == "oat cooking"
    assert all("dairy free" in o["why"] for o in cream_opts)
    assert whip_opts[0]["query"] == "oat cooking"
    assert butter_opts[0]["query"] == "lactose free margarine"
    assert all("dairy free" in o["why"] for o in butter_opts)
    assert cheese_opts[0]["query"] == "lactose-free cheese"

    c_both = Constraint(lactose_free=True, exclude_allergens=[dairy_tag])
    assert recipe_swaps("milk", c_both)[0]["query"] == "oat drink"
    assert recipe_swaps("cooking cream", c_both)[0]["query"] == "oat cooking"
    assert recipe_swaps("butter", c_both)[0]["query"] == "lactose free margarine"

    need = items_by_query(
        menu_to_need(
            "salmon soup and lasagne for 20 guests",
            "hospital-tays",
            today=TODAY,
            constraints=c_both,
        )
    )
    assert need["oat drink"].quantity == pytest.approx(4.0)
    assert need["oat cooking"].quantity == pytest.approx(1.2)
    assert need["lactose free margarine"].quantity == pytest.approx(0.26)
    assert need["lactose-free cheese"].quantity == pytest.approx(0.8)
    assert {"milk", "cooking cream", "butter", "lactose-free milk", "lactose-free cream", "lactose-free butter"}.isdisjoint(need)
    for item in need.values():
        assert item.constraints.exclude_allergens == [dairy_tag]


@pytest.mark.parametrize(
    "gluten_tag",
    ["gluten", "wheat", "barley", "rye", "oats", "gluteeni", "vehnä", " GLUTEN ", "Vehnä"],
)
def test_gluten_allergen_triggers_and_gluten_free_flag(gluten_tag):
    c_gluten = Constraint(exclude_allergens=[gluten_tag])
    assert recipe_swaps("wheat flour", c_gluten)[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("flour", c_gluten)[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("macaroni", c_gluten)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("pasta", c_gluten)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("lasagne", c_gluten)[0]["query"] == "gluten-free pasta"
    assert recipe_swaps("oat flakes", c_gluten)[0]["query"] == "gluten-free oats"
    assert recipe_swaps("oats", c_gluten)[0]["query"] == "gluten-free oats"

    need = items_by_query(
        menu_to_need(
            "lasagne, macaroni casserole, chicken pasta, pancakes and oat porridge for 20 guests",
            "school-hervanta",
            today=TODAY,
            constraints=c_gluten,
        )
    )
    assert need["gluten-free pasta"].quantity == pytest.approx(4.4)
    assert need["gluten-free flour mix"].quantity == pytest.approx(1.16)
    assert need["gluten-free oats"].quantity == pytest.approx(0.8)
    assert {"lasagne", "macaroni", "pasta", "wheat flour", "oat flakes"}.isdisjoint(need)
    for item in need.values():
        assert item.constraints.exclude_allergens == [gluten_tag]


def test_gluten_free_flag_preserves_constraint_on_swapped_items_only():
    gf = Constraint(gluten_free=True)
    need = items_by_query(
        menu_to_need("pancakes and oat porridge for 30 guests", "school-hervanta", today=TODAY, constraints=gf)
    )
    assert need["gluten-free flour mix"].quantity == pytest.approx(1.5)
    assert need["gluten-free oats"].quantity == pytest.approx(1.2)
    assert need["gluten-free flour mix"].constraints.gluten_free is True
    assert need["gluten-free oats"].constraints.gluten_free is True
    assert need["milk"].constraints.gluten_free is None
    assert need["eggs"].constraints.gluten_free is None
    assert need["sugar"].constraints.gluten_free is None

    fi_gf = items_by_query(
        menu_to_need("pannukakut ja kaurapuuro 30 hengelle gluteeniton", "school-hervanta", today=TODAY)
    )
    assert {"gluten-free flour mix", "gluten-free oats"} <= set(fi_gf)
    assert fi_gf["gluten-free flour mix"].constraints.gluten_free is True
    assert fi_gf["milk"].constraints.gluten_free is None


def test_additional_excluded_allergens_propagate_to_swapped_and_unswapped():
    extra_allergens = ["nuts", "peanuts", "egg", "fish", "soy"]

    only_extra = Constraint(exclude_allergens=list(extra_allergens))
    for ing in ("milk", "cooking cream", "wheat flour", "pasta", "oat flakes", "salmon fillet", "eggs"):
        assert recipe_swaps(ing, only_extra) == []

    need_unswapped = items_by_query(
        menu_to_need(
            "salmon soup and pancakes for 20 guests",
            "school-hervanta",
            today=TODAY,
            constraints=only_extra,
        )
    )
    assert {"milk", "cooking cream", "wheat flour", "salmon fillet", "eggs"} <= set(need_unswapped)
    for item in need_unswapped.values():
        assert item.constraints.exclude_allergens == extra_allergens
        assert item.constraints.lactose_free is None
        assert item.constraints.gluten_free is None

    combined = Constraint(
        lactose_free=True,
        gluten_free=True,
        exclude_allergens=list(extra_allergens),
    )
    need_combined = items_by_query(
        menu_to_need(
            "salmon soup and pancakes for 25 guests",
            "hospital-tays",
            today=TODAY,
            constraints=combined,
        )
    )
    swapped_keys = {
        "lactose-free milk",
        "lactose-free cream",
        "lactose-free butter",
        "gluten-free flour mix",
    }
    unswapped_keys = {"salmon fillet", "potato", "carrot", "leek", "dill", "eggs", "sugar"}
    assert swapped_keys | unswapped_keys == set(need_combined)
    for key in swapped_keys:
        assert need_combined[key].constraints.lactose_free is True
        assert need_combined[key].constraints.gluten_free is True
        assert need_combined[key].constraints.exclude_allergens == extra_allergens
    for key in unswapped_keys:
        assert need_combined[key].constraints.lactose_free is None
        assert need_combined[key].constraints.gluten_free is None
        assert need_combined[key].constraints.exclude_allergens == extra_allergens

    need_combined["salmon fillet"].constraints.exclude_allergens.append("sesame")
    assert need_combined["lactose-free milk"].constraints.exclude_allergens == extra_allergens
    assert combined.exclude_allergens == extra_allergens

    strict_all = ["milk", "wheat", *extra_allergens]
    need_strict = items_by_query(
        menu_to_need(
            "salmon soup and pancakes for 20 guests",
            "hospital-tays",
            today=TODAY,
            constraints=Constraint(exclude_allergens=strict_all),
        )
    )
    assert {"oat drink", "oat cooking", "lactose free margarine", "gluten-free flour mix"} <= set(need_strict)
    for item in need_strict.values():
        assert item.constraints.exclude_allergens == strict_all

    free_en = items_by_query(
        menu_to_need(
            "salmon soup and pancakes for 20 guests lactose-free, gluten-free, without nuts, no peanuts, no egg, without fish, soy-free",
            "hospital-tays",
            today=TODAY,
        )
    )
    assert {"lactose-free milk", "gluten-free flour mix", "salmon fillet"} <= set(free_en)
    for item in free_en.values():
        assert {"nuts", "egg", "fish", "soy"} <= set(item.constraints.exclude_allergens)

    free_fi = items_by_query(
        menu_to_need(
            "lohikeitto ja pannukakut 20 hengelle laktoositon gluteeniton pähkinätön munaton kalaton soijaton",
            "hospital-tays",
            today=TODAY,
        )
    )
    assert {"lactose-free milk", "gluten-free flour mix", "salmon fillet"} <= set(free_fi)
    for item in free_fi.values():
        assert {"nuts", "egg", "fish", "soy"} <= set(item.constraints.exclude_allergens)


def test_recipe_swaps_word_boundaries_defensive_copy_and_ratio(monkeypatch):
    lf = Constraint(lactose_free=True)
    gf = Constraint(gluten_free=True)

    assert recipe_swaps("milk", None) == []
    assert recipe_swaps("organic milk", lf)[0]["query"] == "lactose-free milk"
    assert recipe_swaps("unsalted butter", lf)[0]["query"] == "lactose-free butter"
    assert recipe_swaps("  Heavy Cream ", lf)[0]["query"] == "lactose-free cream"
    assert recipe_swaps("spelt flour", gf)[0]["query"] == "gluten-free flour mix"
    assert recipe_swaps("rolled oats", gf)[0]["query"] == "gluten-free oats"
    assert recipe_swaps("buttermilk", lf) == []
    assert recipe_swaps("oatmeal", gf) == []

    opts = recipe_swaps("milk", lf)
    opts[0]["query"] = "corrupted"
    assert recipe_swaps("milk", lf)[0]["query"] == "lactose-free milk"

    custom_swaps = [
        {
            "id": "flour",
            "match": ["wheat flour"],
            "trigger": "gluten",
            "options": [{"query": "buckwheat flour", "ratio": 1.25, "why": "gluten free ratio test"}],
        }
    ]
    monkeypatch.setattr(recipes, "SWAPS", custom_swaps)
    scaled = items_by_query(
        menu_to_need("pancakes for 20 guests", "school-hervanta", today=TODAY, constraints=gf)
    )
    assert scaled["buckwheat flour"].quantity == pytest.approx(1.25)

    explicit = menu_to_need(
        "salmon soup for 10 guests on Friday",
        "hotel-aurora",
        deliver_by=date(2026, 11, 25),
        today=TODAY,
    )
    assert explicit.deliver_by == date(2026, 11, 25)
