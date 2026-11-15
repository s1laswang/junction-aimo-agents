"""Bad-input hardening tests for aimo.recipes.planner, POST /v1/recipes/plan, and MCP plan_recipe."""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import aimo.catalog as cat
from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.catalog.substitution import validate_need
from aimo.gateway import fallback_catalog, human_auth, resilience
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Offer
from aimo.recipes import RecipeError, get_recipe, list_recipes, match_dishes, plan_recipe, recipe_needs
from aimo.recipes import planner as rp
from aimo.recipes.planner import (
    _dietary,
    _normalize_excluded,
    _swap_rule,
    choose_swap,
    default_portions,
    house_lactose_free,
    policy_exclusions,
    resolve_delivery,
)

D = date(2026, 11, 20)
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}


def _synth_products() -> list[dict[str, Any]]:
    vol_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    wt_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]

    def _item(
        pid: str,
        name_en: str,
        cat_code: str,
        conv: list[dict[str, Any]],
        claims: list[str] | None = None,
        allergens: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        cls: list[dict[str, Any]] = []
        if allergens:
            cls.append(
                {
                    "name": "allergen",
                    "type": "ENUM",
                    "values": [{"id": a, "unit": st} for a, st in allergens],
                }
            )
        if claims:
            cls.append(
                {
                    "name": "nutritionalClaim",
                    "type": "ENUM",
                    "values": [{"id": c, "unit": "FREE_FROM"} for c in claims],
                }
            )
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": cat_code,
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": cls,
                "keyIngredients": [{"language": "en", "value": name_en.lower()}],
                "marketingTexts": [{"language": "en", "value": name_en}],
            },
        }

    return [
        _item(
            "463038",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "10400",
            vol_1l,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "464306",
            "Valio Eila milk drink 1 l lactose free",
            "10400",
            vol_1l,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "900101",
            "Myllyn Paras oat flakes 1 kg",
            "21370",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
        ),
        _item(
            "900102",
            "Valio lactose-free butter 1 kg",
            "10500",
            wt_1kg,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "900103",
            "Myllyn Paras lasagne pasta 1 kg",
            "21380",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
        ),
        _item(
            "900104",
            "Snellman ground beef 1 kg",
            "41702",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "900105",
            "Mutti crushed tomatoes 1 kg",
            "21590",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "900106",
            "Valio lactose-free grated cheese 1 kg",
            "10603",
            wt_1kg,
            claims=["FREE_FROM_LACTOSE"],
            allergens=[("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
        _item(
            "900107",
            "Myllyn Paras wheat flour 1 kg",
            "21300",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
        ),
        _item(
            "900108",
            "Fresh yellow onion 1 kg",
            "17405",
            wt_1kg,
            allergens=[("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
        ),
    ]


def _fake_offer(pid: str, req_date: date) -> Offer:
    return Offer(
        product_id=pid,
        requested_date=req_date,
        available=True,
        available_amount=100_000.0,
        available_date=req_date,
        sales_unit="ST",
        unit_price=3.50,
        comparison_price=3.50,
        comparison_unit="KG",
        fetched_at=datetime.now(timezone.utc),
        logistic_model="Stock",
        issues=[],
    )


class _FakeBackend:
    def get_offers_with_meta(
        self,
        ids: list[str],
        requested_date: date,
        customer_id: str | None = None,
    ) -> tuple[dict[str, Offer], dict[str, Any]]:
        del customer_id
        return {pid: _fake_offer(pid, requested_date) for pid in ids}, {"source": "synthetic", "served_from": "memory"}


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("recipe-bad-inputs-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, synth_catalog_dir):
    store.close_all()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_FAULTS",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_NOW",
        "AIMO_NOW_FROZEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
    cdb = synth_catalog_dir / "catalog.sqlite"
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
    monkeypatch.setattr(cat, "_SOURCE", "full")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    human_auth.reset_for_tests()
    for b in list(resilience._breakers.values()):
        b.reset()
    backend = _FakeBackend()
    monkeypatch.setattr(bc, "get_offers_with_meta", backend.get_offers_with_meta)
    yield
    store.close_all()
    human_auth.reset_for_tests()
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.mark.parametrize(
    "bad_portions",
    [
        True,
        False,
        0,
        -1,
        -250,
        100_001,
        999_999_999,
        10**30,
        2.5,
        0.5,
        -2.5,
        0.0,
        -0.0,
        100_000.5,
        math.nan,
        math.inf,
        -math.inf,
        "twenty",
        "20",
        "",
        [],
        {},
    ],
)
def test_explicit_portions_validation_rejects_bad_values(bad_portions: Any) -> None:
    with pytest.raises(RecipeError) as exc_info:
        recipe_needs("lasagne", bad_portions, D, "hotel-aurora", text="Lasagne for 120 on Friday")
    assert exc_info.value.code == "bad_portions"
    assert exc_info.value.status == 422

    with pytest.raises(RecipeError) as plan_exc:
        plan_recipe("lasagne", bad_portions, D, "hotel-aurora", text="Lasagne for 120 on Friday")
    assert plan_exc.value.code == "bad_portions"
    assert plan_exc.value.status == 422


def test_explicit_portions_accepts_bounds_and_exact_integer_float() -> None:
    rn_min = recipe_needs("oat-porridge", 1, D, "hotel-aurora")
    assert rn_min.portions == {"oat-porridge": 1}
    for it in rn_min.items:
        validate_need(it.need)

    rn_max = recipe_needs("oat-porridge", 100_000, D, "hotel-aurora")
    assert rn_max.portions == {"oat-porridge": 100_000}
    for it in rn_max.items:
        validate_need(it.need)

    rn_float_int = recipe_needs("oat-porridge", 20.0, D, "hotel-aurora")
    assert rn_float_int.portions == {"oat-porridge": 20}


@pytest.mark.parametrize(
    "bad_text",
    [
        "0 lasagne",
        "0x lasagne",
        "0 portions of lasagne",
        "0 annosta lasagne",
        "lasagne x 0",
        "lasagne x0",
        "lasagne x-5",
        "lasagne x2.5",
        "lasagne for 0 guests",
        "lasagne for 0",
        "0 annosta",
        "0 hengelle",
        "0 pax",
        "0 servings",
        "-5 lasagne",
        "order -10 lasagne",
        "lasagne x -5",
        "lasagne for -10 guests",
        "-20 annosta",
        "lasagne for -5",
        "2.5 lasagne",
        "2,5 lasagne",
        "lasagne for 2.5 guests",
        "lasagne for 2,5 annosta",
        "100001 lasagne",
        "999999999 lasagne",
        "1,000,000 lasagne",
        "lasagne for 100001 guests",
        "lasagne for 999999999",
        "999999999 annosta",
        "40 lasagne and 0 pancakes",
        "lasagne for 40 guests, 0 annosta",
    ],
)
def test_free_text_zero_negative_fractional_and_huge_portions_rejected(bad_text: str) -> None:
    with pytest.raises(RecipeError) as exc_info:
        recipe_needs(text=bad_text, delivery_by=D, customer_id="hotel-aurora")
    assert exc_info.value.code == "bad_portions"
    assert exc_info.value.status == 422

    if "lasagne" in bad_text and ("for " not in bad_text or bad_text.startswith(("0", "-", "2", "1", "9", "40"))):
        if any(
            bad_text.startswith(p)
            for p in ("0 lasagne", "0x", "0 portions", "0 annosta lasagne", "-5", "2.5", "2,5", "100001", "999999999")
        ):
            with pytest.raises(RecipeError) as md_exc:
                match_dishes(bad_text)
            assert md_exc.value.code == "bad_portions"
            assert md_exc.value.status == 422


@pytest.mark.parametrize(
    "bad_cover_text",
    [
        "lasagne for 0 guests",
        "lasagne for 0",
        "0 annosta",
        "-12 guests",
        "for -4",
        "2.5 portions",
        "2,5 annosta",
        "100001 covers",
        "999999999 pax",
        "for 500000",
    ],
)
def test_default_portions_rejects_bad_explicit_counts(bad_cover_text: str) -> None:
    with pytest.raises(RecipeError) as exc_info:
        default_portions(bad_cover_text)
    assert exc_info.value.code == "bad_portions"
    assert exc_info.value.status == 422


def test_dates_times_and_percentages_in_text_do_not_false_trigger_bad_portions() -> None:
    rn_iso = recipe_needs(text="2026-11-20 lasagne for 40", customer_id="hotel-aurora")
    assert rn_iso.portions == {"lasagne": 40}
    assert rn_iso.delivery_by == date(2026, 11, 20)

    rn_fi = recipe_needs(text="20.11.2026 lasagne for 40", customer_id="hotel-aurora")
    assert rn_fi.portions == {"lasagne": 40}
    assert rn_fi.delivery_by == date(2026, 11, 20)

    rn_time = recipe_needs(text="08:00 lasagne for 50", delivery_by=D, customer_id="hotel-aurora")
    assert rn_time.portions == {"lasagne": 50}

    rn_pct = recipe_needs(text="0% fat milk oat porridge for 30", delivery_by=D, customer_id="hotel-aurora")
    assert rn_pct.portions == {"oat-porridge": 30}

    rn_thousands = recipe_needs(text="1,000 oat porridge", delivery_by=D, customer_id="hotel-aurora")
    assert rn_thousands.portions == {"oat-porridge": 1000}

    rn_post_x = recipe_needs(text="oat porridge x40", delivery_by=D, customer_id="hotel-aurora")
    assert rn_post_x.portions == {"oat-porridge": 40}


@pytest.mark.parametrize(
    "bad_on_hand",
    [
        ["milk"],
        ("milk", 1.0),
        {"milk"},
        "milk",
        123,
        3.14,
        True,
        False,
        object(),
        {123: 1.0},
        {True: 1.0},
        {None: 1.0},
        {"": 1.0},
        {"   ": 1.0},
        {"milk\x00": 1.0},
        {"x" * 300: 1.0},
        {"milk": True},
        {"milk": False},
        {"milk": None},
        {"milk": "abc"},
        {"milk": ""},
        {"milk": "   "},
        {"milk": "1.0\x00"},
        {"milk": "nan"},
        {"milk": "inf"},
        {"milk": "-inf"},
        {"milk": math.nan},
        {"milk": math.inf},
        {"milk": -math.inf},
        {"milk": -0.001},
        {"milk": -10},
        {"milk": [1.0]},
        {"milk": {"qty": 1.0}},
        {"milk": 10**400},
        {"milk": 1e308, "MILK": 1e308},
    ],
)
def test_on_hand_validation_rejects_non_dict_and_bad_entries(bad_on_hand: Any) -> None:
    with pytest.raises(RecipeError) as exc_info:
        recipe_needs("oat-porridge", 20, D, "hotel-aurora", on_hand=bad_on_hand)
    assert exc_info.value.code == "bad_on_hand"
    assert exc_info.value.status == 422


def test_on_hand_valid_subtraction_and_full_coverage_never_create_zero_qty_need() -> None:
    rn_partial = recipe_needs(
        "oat-porridge",
        20,
        D,
        "hotel-aurora",
        on_hand={"Lactose-Free Milk": "1.0", "oat flakes": 0},
    )
    by_q = {it.query: it.quantity for it in rn_partial.items}
    assert by_q["lactose-free milk"] == 2.0
    assert by_q["oat flakes"] == 0.8
    for it in rn_partial.items:
        assert it.quantity > 0
        validate_need(it.need)

    rn_all = recipe_needs(
        "oat-porridge",
        20,
        D,
        "hotel-aurora",
        on_hand={"lactose-free milk": 100.0, "oat flakes": 100.0, "lactose-free butter": 100.0},
    )
    assert rn_all.items == []

    res_all = plan_recipe(
        "oat-porridge",
        20,
        D,
        "hotel-aurora",
        on_hand={"lactose-free milk": 100.0, "oat flakes": 100.0, "lactose-free butter": 100.0},
    )
    assert res_all["ingredients"] == []
    assert res_all["quote_request"]["lines"] == []
    assert res_all["total_net_estimate"] == 0.0


def test_absurd_quantity_guard_rejects_scaled_qty_above_one_million_or_non_finite(monkeypatch) -> None:
    huge_dish = {
        "id": "mega-rolls",
        "en": "mega rolls",
        "fi": "jattisambylat",
        "aliases": ["mega rolls"],
        "ingredients": [{"query": "milk", "per_cover": 25, "unit": "pcs"}],
    }
    inf_dish = {
        "id": "inf-soup",
        "en": "inf soup",
        "fi": "infkeitto",
        "aliases": ["inf soup"],
        "ingredients": [{"query": "milk", "per_cover": 1e308, "unit": "ml"}],
    }
    monkeypatch.setattr(rp, "DISHES", [*rp.DISHES, huge_dish, inf_dish])

    with pytest.raises(RecipeError) as exc_big:
        recipe_needs("mega-rolls", 50_000, D, "hotel-aurora")
    assert exc_big.value.code == "absurd_quantity"
    assert exc_big.value.status == 422

    with pytest.raises(RecipeError) as exc_inf:
        recipe_needs("inf-soup", 10, D, "hotel-aurora")
    assert exc_inf.value.code == "absurd_quantity"
    assert exc_inf.value.status == 422

    bad_unit_dish = {
        "id": "bad-unit-soup",
        "en": "bad unit soup",
        "fi": "keitto",
        "aliases": ["bad unit soup"],
        "ingredients": [{"query": "milk", "per_cover": 100, "unit": "oz"}],
    }
    bad_query_dish = {
        "id": "bad-query-soup",
        "en": "bad query soup",
        "fi": "keitto2",
        "aliases": ["bad query soup"],
        "ingredients": [{"query": "", "per_cover": 100, "unit": "ml"}],
    }
    monkeypatch.setattr(rp, "DISHES", [*rp.DISHES, bad_unit_dish, bad_query_dish])

    with pytest.raises(RecipeError) as exc_unit:
        recipe_needs("bad-unit-soup", 10, D, "hotel-aurora")
    assert exc_unit.value.code == "bad_recipe"
    assert exc_unit.value.status == 422

    with pytest.raises(RecipeError) as exc_query:
        recipe_needs("bad-query-soup", 10, D, "hotel-aurora")
    assert exc_query.value.code == "bad_recipe"
    assert exc_query.value.status == 422


def test_resolve_delivery_converts_datetime_to_pure_date_and_preserves_weekdays() -> None:
    dt = datetime(2026, 11, 22, 16, 45, tzinfo=timezone.utc)
    resolved = resolve_delivery(dt)
    assert resolved == date(2026, 11, 22)
    assert type(resolved) is date

    rn = recipe_needs("oat-porridge", 20, dt, "hotel-aurora")
    assert rn.delivery_by == date(2026, 11, 22)
    assert type(rn.delivery_by) is date
    for it in rn.items:
        assert type(it.need.delivery_by) is date
        validate_need(it.need)

    assert resolve_delivery(date(2026, 11, 21)) == date(2026, 11, 21)
    assert resolve_delivery("2026-11-21") == date(2026, 11, 21)
    assert resolve_delivery("2026-11-21T09:30:00Z") == date(2026, 11, 21)
    assert resolve_delivery("2026-11-21T09:30:00+02:00") == date(2026, 11, 21)

    fri = resolve_delivery("Friday")
    assert type(fri) is date and fri.weekday() == 4
    mon = resolve_delivery("next Monday")
    assert type(mon) is date and mon.weekday() == 0
    default_d = resolve_delivery(None, "")
    assert default_d == rp._today() + timedelta(days=2)
    blank_d = resolve_delivery("   ", "")
    assert blank_d == rp._today() + timedelta(days=2)


@pytest.mark.parametrize(
    "bad_delivery",
    [
        True,
        False,
        123,
        20261120,
        3.14,
        [],
        {},
        object(),
        "2026-11-20\x00",
        "Friday\x00",
        "Friday\nSaturday",
        "x" * 300,
        "2026-13-45",
        "2026-02-30",
        "2026-00-10",
        "2026/11/20",
        "2026-11-20junk",
        "2026-11-20Tbad",
        "bad-date",
        "not-a-date",
        "../../etc/passwd",
    ],
)
def test_resolve_delivery_rejects_bad_types_control_chars_and_invalid_dates(bad_delivery: Any) -> None:
    with pytest.raises(RecipeError) as exc_info:
        resolve_delivery(bad_delivery)
    assert exc_info.value.code == "bad_delivery_date"
    assert exc_info.value.status == 422

    with pytest.raises(RecipeError) as rn_exc:
        recipe_needs("oat-porridge", 10, bad_delivery, "hotel-aurora")
    assert rn_exc.value.code == "bad_delivery_date"
    assert rn_exc.value.status == 422


def test_resolve_delivery_rejects_invalid_iso_date_embedded_in_text() -> None:
    with pytest.raises(RecipeError) as exc_info:
        resolve_delivery(None, "Lasagne for 20 on 2026-13-45")
    assert exc_info.value.code == "bad_delivery_date"
    assert exc_info.value.status == 422


@pytest.mark.parametrize("bad_input", [123, True, False, ["lasagne"], {"id": "lasagne"}, "lasagne\x00", "x" * 4001])
def test_helper_type_null_byte_and_length_guards(bad_input: Any) -> None:
    with pytest.raises(RecipeError) as gr_exc:
        get_recipe(bad_input)
    assert gr_exc.value.code == "bad_recipe"
    assert gr_exc.value.status == 422

    with pytest.raises(RecipeError) as md_exc:
        match_dishes(bad_input)
    assert md_exc.value.code == "bad_text"
    assert md_exc.value.status == 422

    with pytest.raises(RecipeError) as dp_exc:
        default_portions(bad_input)
    assert dp_exc.value.code == "bad_text"
    assert dp_exc.value.status == 422

    with pytest.raises(RecipeError) as rn_rec_exc:
        recipe_needs(bad_input, 10, D, "hotel-aurora")
    assert rn_rec_exc.value.code == "bad_recipe"
    assert rn_rec_exc.value.status == 422

    with pytest.raises(RecipeError) as rn_txt_exc:
        recipe_needs("oat-porridge", 10, D, "hotel-aurora", text=bad_input)
    assert rn_txt_exc.value.code == "bad_text"
    assert rn_txt_exc.value.status == 422


def test_helper_none_and_blank_inputs_return_empty_cleanly() -> None:
    assert get_recipe(None) is None
    assert get_recipe("") is None
    assert get_recipe("   ") is None
    assert match_dishes(None) == []
    assert match_dishes("") == []
    assert match_dishes("   ") == []
    assert default_portions(None) is None
    assert default_portions("") is None
    assert default_portions("   ") is None
    assert _swap_rule(None) is None
    assert _swap_rule(123) is None
    assert _swap_rule("milk\x00") is None
    assert choose_swap("milk", None) is None
    assert policy_exclusions(None) == []
    assert policy_exclusions(123) == []
    assert policy_exclusions("../etc/passwd") == []
    assert house_lactose_free(None) is False
    assert house_lactose_free(True) is False
    assert house_lactose_free("hotel\x00aurora") is False


@pytest.mark.parametrize(
    "bad_cid",
    [
        None,
        True,
        False,
        123,
        [],
        {},
        "",
        "   ",
        "hotel\x00aurora",
        "hotel\naurora",
        "../etc/passwd",
        "hotel/aurora",
        "hotel\\aurora",
        "x" * 129,
    ],
)
def test_customer_id_guards_in_recipe_needs_and_plan_recipe(bad_cid: Any) -> None:
    with pytest.raises(RecipeError) as exc_info:
        recipe_needs("oat-porridge", 10, D, bad_cid)
    assert exc_info.value.code == "bad_customer_id"
    assert exc_info.value.status == 422

    with pytest.raises(RecipeError) as plan_exc:
        plan_recipe("oat-porridge", 10, D, bad_cid)
    assert plan_exc.value.code == "bad_customer_id"
    assert plan_exc.value.status == 422


@pytest.mark.parametrize(
    "bad_excl",
    [
        123,
        True,
        False,
        3.14,
        b"milk",
        bytearray(b"milk"),
        {"milk": True},
        [123],
        [None],
        [True],
        [["milk"]],
        [{}],
        ["milk\x00"],
        ["x" * 129],
        "milk\x00",
        "x" * 129,
        (f"alg-{i}" for i in range(200)),
    ],
)
def test_excluded_allergens_guards(bad_excl: Any) -> None:
    with pytest.raises(RecipeError) as exc_info:
        recipe_needs("oat-porridge", 10, D, "hotel-aurora", excluded_allergens=bad_excl)
    assert exc_info.value.code == "bad_allergens"
    assert exc_info.value.status == 422


def test_boolean_flag_and_callback_guards() -> None:
    for bad_bool in ("yes", 1, 0, None, []):
        with pytest.raises(RecipeError) as lf_exc:
            recipe_needs("oat-porridge", 10, D, "hotel-aurora", lactose_free=bad_bool)
        assert lf_exc.value.code == "bad_flag" and lf_exc.value.status == 422

        with pytest.raises(RecipeError) as gf_exc:
            recipe_needs("oat-porridge", 10, D, "hotel-aurora", gluten_free=bad_bool)
        assert gf_exc.value.code == "bad_flag" and gf_exc.value.status == 422

        with pytest.raises(RecipeError) as ic_exc:
            plan_recipe("oat-porridge", 10, D, "hotel-aurora", include_candidates=bad_bool)
        assert ic_exc.value.code == "bad_flag" and ic_exc.value.status == 422

        with pytest.raises(RecipeError) as diet_exc:
            _dietary([], "", lactose_free=bad_bool, gluten_free=False)
        assert diet_exc.value.code == "bad_flag" and diet_exc.value.status == 422

    assert _normalize_excluded(("dairy", "wheat")) == ["milk", "gluten"]

    with pytest.raises(RecipeError) as of_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", offers_fn="not-callable")
    assert of_exc.value.code == "bad_offers_fn" and of_exc.value.status == 422

    with pytest.raises(RecipeError) as pf_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", plan_fn=123)
    assert pf_exc.value.code == "bad_plan_fn" and pf_exc.value.status == 422

    with pytest.raises(RecipeError) as bad_ret_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", offers_fn=lambda ids, d, customer_id=None: [1, 2, 3])
    assert bad_ret_exc.value.code == "bad_offers" and bad_ret_exc.value.status == 422

    def _missing_plan_fn(need, offers_fn=None):
        del need, offers_fn
        raise FileNotFoundError("catalog missing")

    with pytest.raises(RecipeError) as missing_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", plan_fn=_missing_plan_fn)
    assert missing_exc.value.code == "catalog_missing" and missing_exc.value.status == 503

    calls = 0

    def _bad_need_plan_fn(need, offers_fn=None):
        nonlocal calls
        calls += 1
        if calls <= 3 and offers_fn is not None:
            offers_fn(["464306"], D)
            return None
        raise ValueError("synthetic invalid need")

    with pytest.raises(RecipeError) as val_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", plan_fn=_bad_need_plan_fn)
    assert val_exc.value.code == "bad_recipe_need" and val_exc.value.status == 422

    with pytest.raises(RecipeError) as none_plan_exc:
        plan_recipe("oat-porridge", 10, D, "hotel-aurora", plan_fn=lambda need, offers_fn=None: None)
    assert none_plan_exc.value.code == "bad_plan" and none_plan_exc.value.status == 422


def test_http_and_mcp_plan_recipe_zero_500_on_bad_inputs() -> None:
    app = create_app(with_mcp=False)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/v1/recipes").status_code == 200
    assert len(list_recipes()) == 11

    http_bad_payloads: list[dict[str, Any]] = [
        {},
        {"recipe_id": "unknown-dish", "portions": 10},
        {"recipe_id": "lasagne"},
        {"recipe_id": "lasagne", "portions": 0},
        {"recipe_id": "lasagne", "portions": 0, "text": "Lasagne for 120"},
        {"recipe_id": "lasagne", "portions": -10},
        {"recipe_id": "lasagne", "portions": 100_001},
        {"recipe_id": "lasagne", "portions": 2.5},
        {"recipe_id": "lasagne", "portions": False},
        {"recipe_id": "lasagne", "portions": False, "text": "Lasagne for 120"},
        {"text": "0 lasagne"},
        {"text": "lasagne for 0 guests"},
        {"text": "0 annosta"},
        {"text": "-5 lasagne"},
        {"text": "999999999 lasagne"},
        {"text": "2.5 lasagne"},
        {"recipe_id": "lasagne\x00", "portions": 10},
        {"text": "Lasagne for 10\x00"},
        {"text": "x" * 4001, "portions": 10},
        {"recipe_id": "lasagne", "portions": 10, "delivery_by": "bad-date"},
        {"recipe_id": "lasagne", "portions": 10, "delivery_by": "2026-13-45"},
        {"recipe_id": "lasagne", "portions": 10, "delivery_by": "2026-11-20\x00"},
        {"recipe_id": "lasagne", "portions": 10, "excluded_allergens": "milk"},
        {"recipe_id": "lasagne", "portions": 10, "excluded_allergens": [123]},
        {"recipe_id": "lasagne", "portions": 10, "excluded_allergens": ["milk\x00"]},
    ]
    for payload in http_bad_payloads:
        resp = client.post("/v1/recipes/plan", headers=H_AURORA, json=payload)
        assert 400 <= resp.status_code < 500, (payload, resp.status_code, resp.text[:300])
        body = resp.json()
        assert isinstance(body.get("error"), str) and body["error"], (payload, body)
        assert isinstance(body.get("message"), str) and body["message"], (payload, body)

    ok_http = client.post(
        "/v1/recipes/plan",
        headers=H_AURORA,
        json={"recipe_id": "oat-porridge", "portions": 20, "delivery_by": D.isoformat()},
    )
    assert ok_http.status_code == 200, ok_http.text
    assert ok_http.json()["recipe"]["portions"] == {"oat-porridge": 20}

    mcp_bad_calls: list[dict[str, Any]] = [
        {},
        {"recipe_id": "lasagne", "portions": 0, "text": "Lasagne for 120"},
        {"recipe_id": "lasagne", "portions": False, "text": "Lasagne for 120"},
        {"recipe_id": "lasagne", "portions": -5},
        {"recipe_id": "lasagne", "portions": 100_001},
        {"recipe_id": "lasagne", "portions": 2.5},
        {"text": "0 lasagne"},
        {"text": "lasagne for 0 guests"},
        {"text": "0 annosta"},
        {"text": "999999999 lasagne"},
        {"recipe_id": "oat-porridge", "portions": 10, "delivery_by": "2026-13-45"},
        {"recipe_id": "oat-porridge", "portions": 10, "delivery_by": "bad-date"},
        {"recipe_id": "oat-porridge", "portions": 10, "delivery_by": "2026-11-20\x00"},
        {"recipe_id": "oat-porridge\x00", "portions": 10},
        {"text": "x" * 4001},
        {"recipe_id": "oat-porridge", "portions": 10, "excluded_allergens": ["milk\x00"]},
    ]

    async def _run_mcp() -> None:
        async with Client(build_mcp()) as mcp_client:
            for args in mcp_bad_calls:
                try:
                    res = await mcp_client.call_tool("plan_recipe", args)
                    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                    txt = " ".join(getattr(x, "text", "") for x in (getattr(res, "content", None) or []))
                except Exception as exc:
                    is_err = True
                    txt = f"{type(exc).__name__}: {exc}"
                assert is_err, (args, txt)
                assert "Traceback (most recent call last)" not in txt, (args, txt)

            ok_res = await mcp_client.call_tool(
                "plan_recipe",
                {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": D.isoformat()},
            )
            ok_err = bool(getattr(ok_res, "is_error", False) or getattr(ok_res, "isError", False))
            assert not ok_err, ok_res

    asyncio.run(_run_mcp())
