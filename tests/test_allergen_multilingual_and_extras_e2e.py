from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import aimo.catalog as cat
from aimo import config, gemini
from aimo.agents import drift as drift_mod
from aimo.agents import seller as seller_mod
from aimo.backend import catalog_store, clock
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import embeddings, gemini_allergens, get_product, taxonomy, textsearch
from aimo.catalog.allergens import GS1_GROUPS, normalize_allergen
from aimo.gateway import extras, human_auth, resilience
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import EU14, Quote
from aimo.trust import policy as trust_policy
from aimo.trust.signing import verify_quote_detailed

DELIVERY = date(2026, 11, 16)
HKI = timezone(timedelta(hours=2))
APPROVER_PIN = "test-pin-wave5"

H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "school-tampere/buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "hospital-meilahti/buyer-1"}
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "hotel-aurora/buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}


class HeaderContext:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = dict(headers)


def _synkka_product(
    pid: str,
    name_en: str,
    category: str,
    unit_kind: str,
    unit_amount: float,
    *,
    name_fi: str | None = None,
    name_sv: str | None = None,
    brand: str = "Valio",
    sales_unit: str = "ST",
    claims: tuple[str, ...] = (),
    allergen_states: dict[str, str] | None = None,
    ingredients: str = "water, salt",
    fat: float = 1.5,
) -> dict[str, Any]:
    states = {g: "free" for g in EU14}
    if allergen_states:
        states.update(allergen_states)
    con_vals: list[dict[str, str]] = []
    free_vals: list[dict[str, str]] = []
    for g, st in states.items():
        head = GS1_GROUPS[g][0][0]
        if st == "contains":
            con_vals.append({"id": head, "unit": "CONTAINS"})
        elif st == "may_contain":
            con_vals.append({"id": head, "unit": "MAY_CONTAIN"})
        elif st == "free":
            free_vals.append({"id": head, "unit": "FREE_FROM"})
        elif st == "unknown":
            con_vals.append({"id": head, "unit": "UNDECLARED"})
    classifications: list[dict[str, Any]] = []
    if con_vals:
        classifications.append({"name": "allergen", "type": "ENUM", "values": con_vals})
    if free_vals:
        classifications.append({"name": "nonAllergen", "type": "ENUM", "values": free_vals})
    if claims:
        classifications.append(
            {
                "name": "nutritionalClaim",
                "type": "ENUM",
                "values": [{"id": cl, "unit": "FREE_FROM"} for cl in claims],
            }
        )
    classifications.append({"name": "nutritionFact", "values": [{"id": "FAT", "value": fat}]})
    if unit_kind == "L":
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": unit_amount}}]
    else:
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": unit_amount}}]
    names = [{"language": "en", "value": name_en}]
    if name_fi:
        names.append({"language": "fi", "value": name_fi})
    if name_sv:
        names.append({"language": "sv", "value": name_sv})
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": sales_unit,
        "baseUnit": sales_unit,
        "category": category,
        "vendorName": brand,
        "allowedLotSize": 1,
        "countryOfOrigin": "FI",
        "units": [{"unitId": sales_unit, "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": names,
            "unitConversions": conv,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "nutritionalContent": [{"id": "ENERC", "value": 180.0, "basisQuantity": 100.0}],
            "marketingTexts": [{"language": "en", "value": f"{name_en} for foodservice kitchens."}],
        },
    }


def _build_synthetic_catalog_spec() -> tuple[
    list[dict[str, Any]],
    dict[str, float],
    dict[str, tuple[dict[str, str], dict[str, str]]],
]:
    products: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    inferred_patches: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    def add(
        pid: str,
        name_en: str,
        category: str,
        unit_kind: str,
        unit_amount: float,
        price: float,
        *,
        name_fi: str | None = None,
        name_sv: str | None = None,
        brand: str = "Valio",
        sales_unit: str = "ST",
        claims: tuple[str, ...] = (),
        allergen_states: dict[str, str] | None = None,
        ingredients: str = "water, salt",
        fat: float = 1.5,
        inferred_groups: tuple[str, ...] = (),
    ) -> None:
        products.append(
            _synkka_product(
                pid,
                name_en,
                category,
                unit_kind,
                unit_amount,
                name_fi=name_fi,
                name_sv=name_sv,
                brand=brand,
                sales_unit=sales_unit,
                claims=claims,
                allergen_states=allergen_states,
                ingredients=ingredients,
                fat=fat,
            )
        )
        prices[pid] = price
        if inferred_groups:
            inf_allergens = {g: "free" for g in EU14}
            if allergen_states:
                inf_allergens.update(allergen_states)
            inf_sources = {g: ("inferred:gemini" if g in inferred_groups else "structured") for g in EU14}
            inferred_patches[pid] = (inf_allergens, inf_sources)

    add(
        "463038",
        "Valio Eila semi skimmed milk drink 10 L lactose free",
        "10400",
        "L",
        10.0,
        14.00,
        name_fi="Valio Eila kevytmaitojuoma laktoositon maito 10 L",
        name_sv="Valio Eila laktosfri lätt mjölkdryck mjölk 10 L",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "464306",
        "Valio Eila lactose-free semi skimmed milk drink 1 L",
        "10400",
        "L",
        1.0,
        1.45,
        name_fi="Valio Eila laktoositon kevytmaitojuoma maito 1 L",
        name_sv="Valio Eila laktosfri lätt mjölkdryck mjölk 1 L",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "460396",
        "Valio skimmed milk 10 l novobox",
        "10400",
        "L",
        10.0,
        12.50,
        name_fi="Valio rasvaton maito 10 l novobox",
        name_sv="Valio fettfri mjölk 10 l novobox",
        sales_unit="CS",
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
        fat=0.1,
    )
    add(
        "462775",
        "Arla Milkdrink 1L lactose free fat free UHT",
        "10400",
        "L",
        1.0,
        1.55,
        name_fi="Arla maitojuoma 1 L laktoositon rasvaton UHT",
        name_sv="Arla mjölkdryck 1 L laktosfri fettfri UHT",
        brand="Arla",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="skimmed milk, vitamin D",
        fat=0.1,
    )
    add(
        "ml-cows-milk",
        "Farm cow's milk whole 1 L",
        "10400",
        "L",
        1.0,
        0.85,
        name_fi="Tilamaito täysmaito maito 1 L",
        name_sv="Gårdsmjölk helmjölk mjölk 1 L",
        brand="Generic",
        allergen_states={"milk": "contains"},
        ingredients="pasteurised milk",
        fat=3.5,
    )
    add(
        "ml-milk-may",
        "Shared-line milk drink 1 L",
        "10400",
        "L",
        1.0,
        0.80,
        name_fi="Jaetun linjan maitojuoma maito 1 L",
        name_sv="Delad linje mjölkdryck mjölk 1 L",
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, oats",
    )
    add(
        "ml-milk-unk",
        "Unverified farm milk 1 L",
        "10400",
        "L",
        1.0,
        0.75,
        name_fi="Varmistamaton maito 1 L",
        name_sv="Obekräftad mjölk 1 L",
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="raw liquid",
    )
    add(
        "ml-milk-inf",
        "Inferred dairy-style milk drink 1 L",
        "10400",
        "L",
        1.0,
        0.70,
        name_fi="Päätelty maitojuoma maito 1 L",
        name_sv="Härledd mjölkdryck mjölk 1 L",
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="water, oats",
    )

    add(
        "470889",
        "Oatly Oat Drink Whole 1 L",
        "10402",
        "L",
        1.0,
        2.10,
        name_fi="Oatly kaurajuoma täyteläinen 1 L",
        name_sv="Oatly havredryck hel 1 L",
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )
    add(
        "470863",
        "Oatly Oat Drink Semi 1L",
        "10402",
        "L",
        1.0,
        2.25,
        name_fi="Oatly kaurajuoma kevyt 1 L",
        name_sv="Oatly havredryck lätt 1 L",
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )
    add(
        "479001",
        "Kaslink Aito Oat Drink 1 L UHT",
        "10402",
        "L",
        1.0,
        1.85,
        name_fi="Kaslink Aito kaurajuoma 1 L UHT",
        name_sv="Kaslink Aito havredryck 1 L UHT",
        brand="Kaslink",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )
    add(
        "ml-oat-nut",
        "Oddlygood hazelnut oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.90,
        name_fi="Oddlygood hasselpähkinä kaurajuoma 1 L",
        name_sv="Oddlygood hasselnöt havredryck 1 L",
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={"nuts": "contains"},
        ingredients="water, pure oats, hazelnut",
    )
    add(
        "ml-oat-maymilk",
        "Shared-line oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.92,
        name_fi="Jaetun linjan kaurajuoma 1 L",
        name_sv="Delad linje havredryck 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={"milk": "may_contain"},
        ingredients="water, pure oats, rapeseed oil",
    )
    add(
        "ml-oat-maynut",
        "Almond-trace oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.94,
        name_fi="Mantelijäämä kaurajuoma 1 L",
        name_sv="Mandelspår havredryck 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={"nuts": "may_contain"},
        ingredients="water, pure oats, rapeseed oil",
    )
    add(
        "ml-oat-unk",
        "Unverified oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.88,
        name_fi="Varmistamaton kaurajuoma 1 L",
        name_sv="Obekräftad havredryck 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="water, pure oats",
    )
    add(
        "ml-oat-inf",
        "Inferred oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.86,
        name_fi="Päätelty kaurajuoma 1 L",
        name_sv="Härledd havredryck 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        inferred_groups=("milk", "nuts"),
        ingredients="water, pure oats",
    )

    add(
        "ml-coconut-safe",
        "Kara coconut milk 1 L",
        "10402",
        "L",
        1.0,
        2.35,
        name_fi="Kara kookosmaito 1 L",
        name_sv="Kara kokosmjölk 1 L",
        brand="Kara",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="coconut extract, water",
        fat=17.0,
    )
    add(
        "ml-coconut-maymilk",
        "Shared-line coconut milk 1 L",
        "10402",
        "L",
        1.0,
        0.95,
        name_fi="Jaetun linjan kookosmaito 1 L",
        name_sv="Delad linje kokosmjölk 1 L",
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="coconut extract, water",
        fat=17.0,
    )
    add(
        "ml-coconut-maynut",
        "Nut-facility coconut milk 1 L",
        "10402",
        "L",
        1.0,
        0.92,
        name_fi="Pähkinätehtaan kookosmaito 1 L",
        name_sv="Nötfabrik kokosmjölk 1 L",
        brand="Generic",
        allergen_states={"nuts": "may_contain"},
        ingredients="coconut extract, water",
        fat=17.0,
    )
    add(
        "ml-coconut-unk",
        "Unverified coconut milk 1 L",
        "10402",
        "L",
        1.0,
        0.89,
        name_fi="Varmistamaton kookosmaito 1 L",
        name_sv="Obekräftad kokosmjölk 1 L",
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="coconut extract, water",
        fat=17.0,
    )
    add(
        "ml-coconut-inf",
        "Inferred coconut milk 1 L",
        "10402",
        "L",
        1.0,
        0.87,
        name_fi="Päätelty kookosmaito 1 L",
        name_sv="Härledd kokosmjölk 1 L",
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="coconut extract, water",
        fat=17.0,
    )

    add(
        "ml-cream-reg",
        "Valio cooking cream 15% 1 L",
        "10401",
        "L",
        1.0,
        1.30,
        name_fi="Valio ruokakerma kerma 15% 1 L",
        name_sv="Valio matlagningsgrädde grädde 15% 1 L",
        allergen_states={"milk": "contains"},
        ingredients="cream, stabilizer",
        fat=15.0,
    )
    add(
        "ml-cream-lf",
        "Valio Eila lactose-free cream 15% 1 L",
        "10401",
        "L",
        1.0,
        1.45,
        name_fi="Valio Eila laktoositon ruokakerma kerma 15% 1 L",
        name_sv="Valio Eila laktosfri matlagningsgrädde grädde 15% 1 L",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, lactase",
        fat=15.0,
    )
    add(
        "ml-cream-may",
        "Shared-line cooking cream 1 L",
        "10401",
        "L",
        1.0,
        0.95,
        name_fi="Jaetun linjan kerma 1 L",
        name_sv="Delad linje grädde 1 L",
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, rapeseed oil",
        fat=15.0,
    )
    add(
        "ml-cream-nut",
        "Cashew cooking cream 1 L",
        "10402",
        "L",
        1.0,
        0.92,
        name_fi="Cashew kaurakerma kerma 1 L",
        name_sv="Cashew havregrädde grädde 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={"nuts": "contains"},
        ingredients="water, pure oats, cashew",
        fat=15.0,
    )
    add(
        "ml-cream-unk",
        "Unverified cooking cream 1 L",
        "10402",
        "L",
        1.0,
        0.90,
        name_fi="Varmistamaton kerma 1 L",
        name_sv="Obekräftad grädde 1 L",
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="water, rapeseed oil",
        fat=15.0,
    )
    add(
        "ml-cream-inf",
        "Inferred plant cooking cream 1 L",
        "10402",
        "L",
        1.0,
        0.88,
        name_fi="Päätelty kasvikerma kerma 1 L",
        name_sv="Härledd växtgrädde grädde 1 L",
        brand="Generic",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        inferred_groups=("milk", "nuts"),
        ingredients="water, rapeseed oil",
        fat=15.0,
    )
    add(
        "ml-oatcream-safe",
        "Oatly oat cooking cream 1 L",
        "10402",
        "L",
        1.0,
        2.45,
        name_fi="Oatly iMat kaurakerma ruoanlaittoon kerma 1 L",
        name_sv="Oatly iMat havregrädde matlagningsgrädde grädde 1 L",
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
        fat=13.0,
    )

    add(
        "ml-butter-reg",
        "Valio butter 500 g",
        "10500",
        "KG",
        0.5,
        1.80,
        name_fi="Valio voi 500 g",
        name_sv="Valio smör 500 g",
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
        fat=80.0,
    )
    add(
        "ml-butter-lf",
        "Valio Eila lactose-free butter 500 g",
        "10500",
        "KG",
        0.5,
        1.95,
        name_fi="Valio Eila laktoositon voi 500 g",
        name_sv="Valio Eila laktosfritt smör 500 g",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
        fat=80.0,
    )
    add(
        "ml-butter-may",
        "Shared-churn butter block 500 g",
        "10500",
        "KG",
        0.5,
        1.05,
        name_fi="Jaetun kirnun voi 500 g",
        name_sv="Delat kärnat smör 500 g",
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="rapeseed oil, water",
        fat=80.0,
    )
    add(
        "ml-butter-nut",
        "Almond culinary butter 500 g",
        "10500",
        "KG",
        0.5,
        1.00,
        name_fi="Mantelivoi voi 500 g",
        name_sv="Mandelsmör smör 500 g",
        brand="Generic",
        allergen_states={"nuts": "contains"},
        ingredients="almonds, rapeseed oil",
        fat=55.0,
    )
    add(
        "ml-butter-unk",
        "Unverified butter block 500 g",
        "10500",
        "KG",
        0.5,
        0.95,
        name_fi="Varmistamaton voi 500 g",
        name_sv="Obekräftat smör 500 g",
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="vegetable fat, water",
        fat=80.0,
    )
    add(
        "ml-butter-inf",
        "Inferred plant butter block 500 g",
        "10500",
        "KG",
        0.5,
        0.90,
        name_fi="Päätelty kasvivoi voi 500 g",
        name_sv="Härlett växtsmör smör 500 g",
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="rapeseed oil, water",
        fat=80.0,
    )
    add(
        "ml-veganbutter-safe",
        "Eleplant vegan butter block 500 g",
        "1051",
        "KG",
        0.5,
        2.75,
        name_fi="Eleplant vegaaninen kasvivoi voi 500 g",
        name_sv="Eleplant växtbaserat smör 500 g",
        brand="Eleplant",
        claims=("FREE_FROM_LACTOSE", "VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="rapeseed oil, water, salt",
        fat=79.0,
    )

    add(
        "ml-marg-may",
        "Blended lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        1.60,
        name_fi="Sekoitettu laktoositon margariini 1 kg",
        name_sv="Blandat laktosfritt margarin 1 kg",
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "may_contain"},
        ingredients="rapeseed oil, water, salt",
        fat=70.0,
    )
    add(
        "ml-marg-unk",
        "Unverified lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        1.50,
        name_fi="Varmistamaton laktoositon margariini 1 kg",
        name_sv="Obekräftat laktosfritt margarin 1 kg",
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="rapeseed oil, water",
        fat=70.0,
    )
    add(
        "ml-marg-safe",
        "Sunnuntai lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        2.85,
        name_fi="Sunnuntai laktoositon margariini 1 kg",
        name_sv="Sunnuntai laktosfritt margarin 1 kg",
        brand="Sunnuntai",
        claims=("FREE_FROM_LACTOSE", "VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="rapeseed oil, water, salt",
        fat=70.0,
    )

    add(
        "473328",
        "Schar Vital dark sliced gluten-free bread 350 g",
        "11000",
        "KG",
        0.35,
        3.20,
        name_fi="Schär Vital tumma viipaloitu gluteeniton leipä 350 g",
        name_sv="Schär Vital mörkt skivat glutenfritt bröd 350 g",
        brand="Schar",
        claims=("FREE_FROM_GLUTEN", "VEGAN"),
        allergen_states={},
        ingredients="water, rice flour, buckwheat",
        fat=3.0,
    )
    add(
        "461765",
        "Moilas Gluten-free Dark Bread 600g sliced frozen",
        "11000",
        "KG",
        0.6,
        3.50,
        name_fi="Moilas tumma viipaloitu gluteeniton leipä 600 g",
        name_sv="Moilas mörkt skivat glutenfritt bröd 600 g",
        brand="Moilas",
        sales_unit="KI",
        claims=("FREE_FROM_GLUTEN", "VEGAN"),
        allergen_states={},
        ingredients="water, rice flour, buckwheat",
        fat=3.0,
    )
    add(
        "461578",
        "Vaasan Rye bread 45x85g",
        "11000",
        "KG",
        3.825,
        6.50,
        name_fi="Vaasan ruisleipä 45x85g",
        name_sv="Vaasan rågbröd 45x85g",
        brand="Vaasan",
        sales_unit="CS",
        allergen_states={"gluten": "contains"},
        ingredients="wholegrain rye, water, salt",
        fat=2.0,
    )
    add(
        "ml-rye-contains",
        "Bakery sliced rye bread 500 g",
        "11000",
        "KG",
        0.5,
        0.80,
        name_fi="Leipomon viipaloitu ruisleipä 500 g",
        name_sv="Bageriets skivat rågbröd 500 g",
        brand="Vaasan",
        allergen_states={"gluten": "contains"},
        ingredients="wholegrain rye, water, salt",
        fat=2.0,
    )
    add(
        "ml-rye-may",
        "Shared-oven rye bread 500 g",
        "11000",
        "KG",
        0.5,
        0.85,
        name_fi="Jaetun uunin ruisleipä 500 g",
        name_sv="Delad ugn rågbröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="buckwheat, water, salt",
        fat=2.0,
    )
    add(
        "ml-rye-unk",
        "Unverified rye bread 500 g",
        "11000",
        "KG",
        0.5,
        0.90,
        name_fi="Varmistamaton ruisleipä 500 g",
        name_sv="Obekräftat rågbröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="buckwheat, water",
        fat=2.0,
    )
    add(
        "ml-rye-inf",
        "Inferred rye style bread 500 g",
        "11000",
        "KG",
        0.5,
        0.95,
        name_fi="Päätelty ruisleipä 500 g",
        name_sv="Härlett rågbröd 500 g",
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="buckwheat, water",
        fat=2.0,
    )
    add(
        "ml-wheatbread-contains",
        "Bakery sliced wheat bread 500 g",
        "11000",
        "KG",
        0.5,
        0.80,
        name_fi="Leipomon viipaloitu vehnäleipä 500 g",
        name_sv="Bageriets skivat vetebröd 500 g",
        brand="Fazer",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour, water, yeast",
        fat=3.0,
    )
    add(
        "ml-wheatbread-may",
        "Shared-oven wheat bread 500 g",
        "11000",
        "KG",
        0.5,
        0.85,
        name_fi="Jaetun uunin vehnäleipä 500 g",
        name_sv="Delad ugn vetebröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, water",
        fat=3.0,
    )
    add(
        "ml-wheatbread-unk",
        "Unverified wheat bread 500 g",
        "11000",
        "KG",
        0.5,
        0.90,
        name_fi="Varmistamaton vehnäleipä 500 g",
        name_sv="Obekräftat vetebröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, water",
        fat=3.0,
    )
    add(
        "ml-wheatbread-inf",
        "Inferred wheat style bread 500 g",
        "11000",
        "KG",
        0.5,
        0.95,
        name_fi="Päätelty vehnäleipä 500 g",
        name_sv="Härlett vetebröd 500 g",
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, water",
        fat=3.0,
    )
    add(
        "ml-gfbread-may",
        "Shared-bakery dark gluten-free bread 500 g",
        "11000",
        "KG",
        0.5,
        0.90,
        name_fi="Jaetun leipomon gluteeniton leipä 500 g",
        name_sv="Delat bageri glutenfritt bröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, water",
        fat=3.0,
    )
    add(
        "ml-gfbread-unk",
        "Unverified dark gluten-free bread 500 g",
        "11000",
        "KG",
        0.5,
        0.85,
        name_fi="Varmistamaton gluteeniton leipä 500 g",
        name_sv="Obekräftat glutenfritt bröd 500 g",
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, water",
        fat=3.0,
    )
    add(
        "ml-gfbread-inf",
        "Inferred dark gluten-free bread 500 g",
        "11000",
        "KG",
        0.5,
        0.80,
        name_fi="Päätelty gluteeniton leipä 500 g",
        name_sv="Härlett glutenfritt bröd 500 g",
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, water",
        fat=3.0,
    )

    add(
        "ml-lasagne-reg",
        "Myllyn Paras lasagne pasta sheets 1 kg",
        "21380",
        "KG",
        1.0,
        2.20,
        name_fi="Myllyn Paras lasagnelevy pasta 1 kg",
        name_sv="Myllyn Paras lasagneplattor pasta 1 kg",
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "ml-gfpasta-may",
        "Shared-mill gluten-free pasta 1 kg",
        "21380",
        "KG",
        1.0,
        1.50,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="corn flour, rice flour",
    )
    add(
        "ml-gfpasta-unk",
        "Unverified gluten-free pasta 1 kg",
        "21380",
        "KG",
        1.0,
        1.40,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="corn flour, rice flour",
    )
    add(
        "ml-gfpasta-safe",
        "Semper gluten-free pasta lasagne 1 kg",
        "21380",
        "KG",
        1.0,
        3.20,
        name_fi="Semper gluteeniton pasta lasagne 1 kg",
        name_sv="Semper glutenfri pasta lasagne 1 kg",
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="corn flour, rice flour",
    )

    add(
        "ml-flour-wheat",
        "Sunnuntai wheat flour 2 kg",
        "2130",
        "KG",
        2.0,
        1.65,
        name_fi="Sunnuntai vehnäjauho 2 kg",
        name_sv="Sunnuntai vetemjöl 2 kg",
        brand="Sunnuntai",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour",
    )
    add(
        "ml-gff-may",
        "Cross-line gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.50,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, potato starch",
    )
    add(
        "ml-gff-unk",
        "Unverified gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.40,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, corn starch",
    )
    add(
        "ml-gff-inf",
        "Inferred gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.30,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, corn starch",
    )
    add(
        "ml-gff-safe",
        "Semper gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        3.00,
        name_fi="Semper gluteeniton jauhoseos 1 kg",
        name_sv="Semper glutenfri mjölmix 1 kg",
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="rice flour, potato starch, corn starch",
    )

    add(
        "ml-oats-reg",
        "Elovena oat flakes 1 kg",
        "21370",
        "KG",
        1.0,
        1.80,
        name_fi="Elovena kaurahiutale 1 kg",
        name_sv="Elovena havregryn 1 kg",
        brand="Elovena",
        allergen_states={"gluten": "contains"},
        ingredients="rolled oats",
    )
    add(
        "ml-gfoats-safe",
        "Provenance gluten-free oats 1 kg",
        "2137",
        "KG",
        1.0,
        2.70,
        name_fi="Provenance gluteeniton kaurahiutale 1 kg",
        name_sv="Provenance glutenfria havregryn 1 kg",
        brand="Provenance",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="pure oats",
    )

    add(
        "ml-beef-safe",
        "Snellman ground beef 2 kg",
        "41702",
        "KG",
        2.0,
        11.50,
        brand="Snellman",
        allergen_states={},
        ingredients="beef",
        fat=10.0,
    )
    add(
        "ml-tom-safe",
        "Mutti crushed tomatoes 2.5 kg",
        "21590",
        "KG",
        2.5,
        4.50,
        brand="Mutti",
        allergen_states={},
        ingredients="tomato, salt",
        fat=0.2,
    )
    add(
        "ml-cheese-reg",
        "Valio grated cheese 1 kg",
        "10603",
        "KG",
        1.0,
        6.20,
        allergen_states={"milk": "contains"},
        ingredients="milk, salt, starter",
        fat=26.0,
    )
    add(
        "ml-cheese-lf",
        "Valio Oltermanni lactose-free grated cheese 1 kg",
        "10603",
        "KG",
        1.0,
        6.80,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, salt, starter",
        fat=26.0,
    )
    add(
        "ml-onion-safe",
        "Peeled yellow onion 2 kg",
        "17405",
        "KG",
        2.0,
        2.20,
        brand="Tuore",
        allergen_states={},
        ingredients="onion",
        fat=0.1,
    )
    add(
        "ml-carrot-safe",
        "Peeled carrot 2 kg",
        "1743",
        "KG",
        2.0,
        2.40,
        brand="Tuore",
        allergen_states={},
        ingredients="carrot",
        fat=0.1,
    )
    add(
        "ml-leek-safe",
        "Washed leek 1 kg",
        "17405",
        "KG",
        1.0,
        2.90,
        brand="Tuore",
        allergen_states={},
        ingredients="leek",
        fat=0.1,
    )
    add(
        "ml-dill-safe",
        "Fresh dill 500 g",
        "22630",
        "KG",
        0.5,
        3.80,
        brand="Tuore",
        allergen_states={},
        ingredients="dill",
        fat=0.1,
    )
    add(
        "ml-salmon-nuttrap",
        "Almond crust salmon fillet 1 kg",
        "1870",
        "KG",
        1.0,
        6.50,
        brand="Generic",
        allergen_states={"fish": "contains", "nuts": "contains", "milk": "contains"},
        ingredients="salmon, almond, butter",
        fat=12.0,
    )
    add(
        "ml-salmon-safe",
        "Atlantic salmon fillet 1 kg",
        "1870",
        "KG",
        1.0,
        12.00,
        brand="Kalatukku",
        allergen_states={"fish": "contains"},
        ingredients="salmon, salt",
        fat=12.0,
    )
    add(
        "ml-eggs-trap",
        "Shared-kitchen liquid eggs 1 kg",
        "10590",
        "KG",
        1.0,
        2.10,
        brand="Generic",
        allergen_states={"eggs": "contains", "milk": "may_contain", "nuts": "may_contain", "gluten": "may_contain"},
        ingredients="pasteurised egg",
        fat=9.5,
    )
    add(
        "ml-eggs-safe",
        "Kultamuna pasteurised liquid eggs 1 kg",
        "10590",
        "KG",
        1.0,
        3.90,
        brand="Kultamuna",
        allergen_states={"eggs": "contains"},
        ingredients="pasteurised egg",
        fat=9.5,
    )
    add(
        "ml-sugar-trap",
        "Bulk bakery granulated sugar 2 kg",
        "22100",
        "KG",
        2.0,
        1.10,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "may_contain", "gluten": "unknown"},
        ingredients="sugar",
        fat=0.0,
    )
    add(
        "ml-sugar-safe",
        "Dansukker granulated sugar 2 kg",
        "22100",
        "KG",
        2.0,
        2.30,
        brand="Dansukker",
        allergen_states={},
        ingredients="sugar",
        fat=0.0,
    )

    add(
        "460573",
        "PP POTATO PRE-WASHED BELLAROSA 5 kg",
        "17421",
        "KG",
        5.0,
        4.20,
        name_fi="Pesty peruna Bellarosa 5 kg",
        name_sv="Tvättad potatis Bellarosa 5 kg",
        brand="Tuore",
        sales_unit="KG",
        allergen_states={},
        ingredients="potato",
        fat=0.1,
    )
    add(
        "469269",
        "Urtekram Organic Gluten-free Oat Muesli 550g",
        "2137",
        "KG",
        0.55,
        3.80,
        brand="Urtekram",
        claims=("FREE_FROM_GLUTEN", "ORGANIC"),
        allergen_states={},
        ingredients="pure oats, raisins",
        fat=5.0,
    )
    add(
        "463982",
        "Kieku Barn Eggs L180 12300g",
        "10590",
        "KG",
        12.3,
        28.00,
        brand="Kieku",
        sales_unit="KI",
        allergen_states={"eggs": "contains"},
        ingredients="egg",
        fat=10.0,
    )
    add(
        "460468",
        "Paulig Presidentti Tumma Paahto 18x300g",
        "22200",
        "KG",
        5.4,
        42.00,
        brand="Paulig",
        sales_unit="KI",
        allergen_states={},
        ingredients="roasted coffee",
        fat=0.5,
    )
    add(
        "460884",
        "Reuter & Stolt Croissant butter 18 % 80 x 90g",
        "11200",
        "KG",
        7.2,
        36.00,
        brand="Reuter",
        sales_unit="KI",
        allergen_states={"gluten": "contains", "milk": "contains", "eggs": "contains"},
        ingredients="wheat flour, butter, egg",
        fat=18.0,
    )

    add(
        "broth-safe",
        "Nordic clear culinary broth 1 L",
        "21500",
        "L",
        1.0,
        2.50,
        name_fi="Pohjoinen kirkas keittiöliemi 1 L",
        name_sv="Nordisk klar buljong 1 L",
        brand="Nordic",
        allergen_states={},
        ingredients="water, salt",
        fat=0.5,
    )
    add(
        "broth-all-contains",
        "Rich culinary broth all-allergen 1 L",
        "21500",
        "L",
        1.0,
        0.20,
        brand="Generic",
        allergen_states={g: "contains" for g in EU14},
        ingredients="water, salt",
        fat=0.5,
    )
    add(
        "broth-all-may",
        "Shared-kettle culinary broth 1 L",
        "21500",
        "L",
        1.0,
        0.25,
        brand="Generic",
        allergen_states={g: "may_contain" for g in EU14},
        ingredients="water, salt",
        fat=0.5,
    )
    add(
        "broth-all-unk",
        "Unverified culinary broth 1 L",
        "21500",
        "L",
        1.0,
        0.30,
        brand="Generic",
        allergen_states={g: "unknown" for g in EU14},
        ingredients="water, salt",
        fat=0.5,
    )
    add(
        "broth-all-inf",
        "Inferred clear culinary broth 1 L",
        "21500",
        "L",
        1.0,
        0.35,
        brand="Generic",
        inferred_groups=tuple(EU14),
        ingredients="water, salt",
        fat=0.5,
    )
    for g in EU14:
        add(
            f"broth-only-{g}",
            f"Seasoned culinary broth {g} edition 1 L",
            "21500",
            "L",
            1.0,
            3.10,
            brand="Generic",
            allergen_states={g: "contains"},
            ingredients="water, salt",
            fat=0.5,
        )

    return products, prices, inferred_patches


class ConfigurableOfferBackendClient:
    base_url = "fixture://multilingual-extras-e2e"
    ttl_s = 300

    def __init__(self, items: list[dict[str, Any]], base_prices: dict[str, float]) -> None:
        self._units: dict[str, tuple[str, float, str]] = {}
        for it in items:
            pid = it["id"]
            sunit = it.get("salesUnit") or "ST"
            conv = it["synkkaData"]["unitConversions"][0]
            if "volume" in conv:
                self._units[pid] = ("LTR", float(conv["volume"]["value"]), sunit)
            else:
                self._units[pid] = ("KGM", float(conv["netWeight"]["value"]), sunit)
        self.prices: dict[str, float] = {pid: float(base_prices.get(pid, 2.50)) for pid in self._units}
        self.stock: dict[str, float] = {pid: 50000.0 for pid in self._units}
        self.sales_blocks: set[str] = set()
        self.stats = {"upstream_calls": 0}

    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _now_iso(self) -> str:
        return clock.now().astimezone(HKI).strftime("%Y-%m-%dT%H:%M:%S%z")

    def price(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {
            "customerId": customer or "demo",
            "currency": "EUR",
            "timeNow": self._now_iso(),
            "pricingDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
        }
        products: list[dict[str, Any]] = []
        for pid in product_ids:
            s_pid = str(pid)
            if s_pid not in self._units:
                continue
            cunit, camt, sunit = self._units[s_pid]
            net = float(self.prices[s_pid])
            gross = round(net * 1.14, 2)
            cmp_val = round(net / camt, 4) if camt else net
            products.append(
                {
                    "productId": s_pid,
                    "salesUnit": sunit,
                    "quantity": 1.0,
                    "netPriceFound": True,
                    "error": False,
                    "comparisonUnit": cunit,
                    "catalogPrice": {"value": net, "valueWithTax": gross, "currency": "EUR"},
                    "comparisonPrice": {"value": cmp_val, "unit": cunit},
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}

    def availability(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {
            "timeNow": self._now_iso(),
            "requestedDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
        }
        close_dt = datetime.combine(requested_date, datetime.min.time(), HKI).replace(hour=6)
        products: list[dict[str, Any]] = []
        for pid in product_ids:
            s_pid = str(pid)
            if s_pid not in self._units:
                continue
            _cunit, _camt, sunit = self._units[s_pid]
            sb = s_pid in self.sales_blocks
            products.append(
                {
                    "productId": s_pid,
                    "closingTime": self._iso(close_dt),
                    "salesBlock": sb,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": sb,
                            "availableDate": requested_date.isoformat(),
                            "availableAmount": self.stock[s_pid],
                            "salesUnit": sunit,
                        }
                    ],
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}


def _close_cached_catalog_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _reset_breakers() -> None:
    for b in resilience._breakers.values():
        b.reset()


@pytest.fixture(scope="module")
def synthetic_catalog_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    cat_dir = tmp_path_factory.mktemp("wave5_multilingual_extras_catalog")
    products, prices, inferred_patches = _build_synthetic_catalog_spec()
    prod_json = cat_dir / "valio_aimo_junction26_product_data.json"
    cat_db = cat_dir / "catalog.sqlite"
    rel_csv = cat_dir / "reliability.csv"
    co2_csv = cat_dir / "co2.csv"
    prod_json.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")

    mp = pytest.MonkeyPatch()
    mp.setattr(catalog_store, "PRODUCT_JSON", prod_json)
    mp.setattr(catalog_store, "CATALOG_DB", cat_db)
    mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    mp.setattr(catalog_store, "CO2_CSV", co2_csv)
    try:
        catalog_store.load_catalog(cat_db, force=True)
    finally:
        mp.undo()

    con = sqlite3.connect(str(cat_db))
    try:
        for pid, (a_map, s_map) in inferred_patches.items():
            con.execute(
                "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
                (json.dumps(a_map), json.dumps(s_map), pid),
            )
        con.commit()
    finally:
        con.close()

    return {
        "dir": cat_dir,
        "items": products,
        "prices": prices,
        "product_json": prod_json,
        "catalog_db": cat_db,
        "rel_csv": rel_csv,
        "co2_csv": co2_csv,
    }


@pytest.fixture(scope="module")
def http_app_client() -> Any:
    app = extras.install(create_app(with_mcp=False))
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def isolated_env(
    synthetic_catalog_bundle: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    _close_cached_catalog_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)

    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini network call must never run in offline E2E suite")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *a, **k: [])
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: None)
    monkeypatch.setattr(taxonomy, "label", lambda pid: None)

    monkeypatch.setattr(config, "DATA_DIR", synthetic_catalog_bundle["dir"])
    monkeypatch.setattr(config, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(config, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", synthetic_catalog_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", synthetic_catalog_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synthetic_catalog_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", synthetic_catalog_bundle["co2_csv"])
    monkeypatch.setattr(cat, "_SOURCE", "full")

    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")

    human_auth.reset_for_tests()
    _reset_breakers()
    backend = ConfigurableOfferBackendClient(synthetic_catalog_bundle["items"], synthetic_catalog_bundle["prices"])
    bc.set_client(backend)
    try:
        yield backend
    finally:
        bc.set_client(None)
        human_auth.reset_for_tests()
        _reset_breakers()
        _close_cached_catalog_con()
        if hasattr(trust_policy, "_load_policy_cached"):
            trust_policy._load_policy_cached.cache_clear()


def _mcp_streamable_call(
    headers: dict[str, str],
    tool_name: str,
    args: dict[str, Any],
) -> tuple[bool, Any]:
    async def _run() -> tuple[bool, Any]:
        app = extras.install(create_app(with_mcp=True))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers=headers,
            ) as http:
                async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                    res = await client.call_tool(tool_name, args)
                    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                    if sc is not None and not is_err:
                        if isinstance(sc, dict) and set(sc) == {"result"}:
                            return False, sc["result"]
                        return False, sc
                    txt = "\n".join(getattr(c, "text", "") or "" for c in (res.content or []))
                    try:
                        return is_err, json.loads(txt)
                    except Exception:
                        return is_err, txt

    return asyncio.run(_run())


def _mcp_handler_call(
    headers: dict[str, str],
    tool_name: str,
    **kwargs: Any,
) -> Any:
    mcp_srv = extras.register_mcp_tools(build_mcp())
    tools = {t.name: t for t in mcp_srv._tool_manager.list_tools()}
    assert tool_name in tools, f"missing MCP tool {tool_name}"
    return tools[tool_name].fn(**kwargs, ctx=HeaderContext(headers))


def _assert_structured_free(pid: str, groups: tuple[str, ...] | list[str]) -> None:
    prod = get_product(pid)
    assert prod is not None, f"missing catalog product {pid}"
    for g in groups:
        st = prod.allergens.get(g)
        val = st.value if hasattr(st, "value") else str(st or "unknown")
        src = prod.allergen_source.get(g) or ""
        assert val not in ("contains", "may_contain", "unknown"), (pid, prod.name, g, val, src)
        assert val == "free", (pid, prod.name, g, val, src)
        assert not src.startswith("inferred"), (pid, prod.name, g, val, src)
        assert src.startswith("structured"), (pid, prod.name, g, val, src)


def _assert_plan_hardblock(plan: dict[str, Any], groups: tuple[str, ...] | list[str]) -> None:
    need_excl = set((plan.get("need") or {}).get("excluded_allergens") or [])
    assert set(groups) <= need_excl, (groups, need_excl)
    for ln in plan.get("lines") or []:
        assert "allergen_unknown" not in (ln.get("flags") or []), ln
        _assert_structured_free(str(ln["product_id"]), groups)
    for cand in plan.get("candidates") or []:
        if cand.get("tier") != "rejected":
            pid = str((cand.get("product") or {}).get("id") or cand.get("product_id"))
            assert "allergen_unknown" not in (cand.get("flags") or []), cand
            _assert_structured_free(pid, groups)


SCHOOL_MULTILINGUAL_CASES: list[tuple[str, str, str, float, str]] = [
    ("fi", "maito", "L", 20.0, "literal"),
    ("fi", "kerma", "L", 10.0, "implied"),
    ("fi", "voi", "KG", 5.0, "implied"),
    ("fi", "kaurajuoma", "L", 20.0, "plant"),
    ("fi", "maidoton kaurajuoma", "L", 20.0, "plant"),
    ("sv", "mjölk", "L", 20.0, "literal"),
    ("sv", "grädde", "L", 10.0, "implied"),
    ("sv", "smör", "KG", 5.0, "implied"),
    ("sv", "havredryck", "L", 20.0, "plant"),
    ("sv", "mjölkfri havredryck", "L", 20.0, "plant"),
    ("en", "milk", "L", 20.0, "literal"),
    ("en", "cow's milk", "L", 20.0, "literal"),
    ("en", "cream", "L", 10.0, "implied"),
    ("en", "oat drink", "L", 20.0, "plant"),
    ("en", "coconut milk", "L", 10.0, "plant_coconut"),
]


@pytest.mark.parametrize(
    ("lang", "query", "unit", "qty", "expected_mode"),
    SCHOOL_MULTILINGUAL_CASES,
    ids=[f"{c[0]}_{c[1].replace(' ', '_').replace(chr(39), '')}" for c in SCHOOL_MULTILINGUAL_CASES],
)
def test_school_tampere_multilingual_plan_hardblock_http_and_mcp(
    http_app_client: TestClient,
    lang: str,
    query: str,
    unit: str,
    qty: float,
    expected_mode: str,
) -> None:
    _ = lang
    need_payload = {
        "customer_id": "school-tampere",
        "product_query": query,
        "excluded_allergens": [],
        "quantity": qty,
        "unit": unit,
        "delivery_by": DELIVERY.isoformat(),
    }

    r_http = http_app_client.post("/v1/plan", headers=H_SCHOOL, json=need_payload)
    assert r_http.status_code == 200, r_http.text
    plan_http = r_http.json()

    is_err, plan_mcp = _mcp_streamable_call(H_SCHOOL, "plan_need", {"need": need_payload})
    assert not is_err, plan_mcp
    assert isinstance(plan_mcp, dict)

    plan_ctx = _mcp_handler_call(H_SCHOOL, "plan_need", need=need_payload)
    assert isinstance(plan_ctx, dict)

    assert [ln["product_id"] for ln in plan_http["lines"]] == [ln["product_id"] for ln in plan_mcp["lines"]]
    assert [ln["product_id"] for ln in plan_http["lines"]] == [ln["product_id"] for ln in plan_ctx["lines"]]

    for plan in (plan_http, plan_mcp, plan_ctx):
        _assert_plan_hardblock(plan, ("milk", "nuts"))
        assert plan["lines"], f"expected safe line for {query!r}"

        if expected_mode == "literal":
            assert [ln["product_id"] for ln in plan["lines"]] == ["470889"]
            for ln in plan["lines"]:
                assert "allergen_safe_alternative" in ln["flags"]
        elif expected_mode == "implied":
            for ln in plan["lines"]:
                assert "allergen_safe_alternative" in ln["flags"]
        elif expected_mode == "plant":
            assert [ln["product_id"] for ln in plan["lines"]] == ["470889"]
            for ln in plan["lines"]:
                assert "allergen_safe_alternative" not in ln["flags"]
        elif expected_mode == "plant_coconut":
            assert [ln["product_id"] for ln in plan["lines"]] == ["ml-coconut-safe"]
            for ln in plan["lines"]:
                assert "allergen_safe_alternative" not in ln["flags"]


@pytest.mark.parametrize("literal_query", ["maito", "mjölk", "milk", "cow's milk"])
def test_school_tampere_literal_milk_unavailable_usual_returns_zero_lines_and_shortfall(
    http_app_client: TestClient,
    isolated_env: ConfigurableOfferBackendClient,
    literal_query: str,
) -> None:
    need_payload = {
        "customer_id": "school-tampere",
        "product_query": literal_query,
        "excluded_allergens": [],
        "quantity": 25.0,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }

    fault_headers = {**H_SCHOOL, "X-Aimo-Faults": "stockout:470889"}
    r_fault = http_app_client.post("/v1/plan", headers=fault_headers, json=need_payload)
    assert r_fault.status_code == 200, r_fault.text
    plan_fault = r_fault.json()
    _assert_plan_hardblock(plan_fault, ("milk", "nuts"))
    assert plan_fault["lines"] == []
    assert plan_fault["covered_amount"] == pytest.approx(0.0)
    assert plan_fault["shortfall"] == pytest.approx(25.0)

    is_err_fault, mcp_fault = _mcp_streamable_call(fault_headers, "plan_need", {"need": need_payload})
    assert not is_err_fault, mcp_fault
    _assert_plan_hardblock(mcp_fault, ("milk", "nuts"))
    assert mcp_fault["lines"] == []
    assert mcp_fault["shortfall"] == pytest.approx(25.0)

    isolated_env.stock["470889"] = 0.0
    r_zero = http_app_client.post("/v1/plan", headers=H_SCHOOL, json=need_payload)
    assert r_zero.status_code == 200, r_zero.text
    plan_zero = r_zero.json()
    _assert_plan_hardblock(plan_zero, ("milk", "nuts"))
    assert plan_zero["lines"] == []
    assert plan_zero["shortfall"] == pytest.approx(25.0)


HOTEL_AURORA_ALIAS_CASES: list[tuple[str, str]] = [
    ("maito", "milk"),
    ("mjölk", "milk"),
    ("pähkinä", "nuts"),
    ("pähkinät", "nuts"),
    ("nötter", "nuts"),
    ("maapähkinä", "peanuts"),
    ("jordnötter", "peanuts"),
    ("kananmuna", "eggs"),
    ("ägg", "eggs"),
    ("vehnä", "gluten"),
    ("ruis", "gluten"),
    ("gluteeni", "gluten"),
    ("soija", "soy"),
    ("kala", "fish"),
    ("fisk", "fish"),
    ("seesami", "sesame"),
    ("selleri", "celery"),
    ("sinappi", "mustard"),
    ("senap", "mustard"),
    ("äyriäiset", "crustaceans"),
    ("nilviäiset", "molluscs"),
    ("sulfiitit", "sulphites"),
    ("lupiini", "lupin"),
]


@pytest.mark.parametrize(
    ("alias", "expected_eu14"),
    HOTEL_AURORA_ALIAS_CASES,
    ids=[f"{a}_{g}" for a, g in HOTEL_AURORA_ALIAS_CASES],
)
def test_hotel_aurora_multilingual_caller_allergen_aliases_http_and_mcp_parity(
    http_app_client: TestClient,
    isolated_env: ConfigurableOfferBackendClient,
    alias: str,
    expected_eu14: str,
) -> None:
    assert normalize_allergen(alias) == (expected_eu14,)
    single_trap_id = f"broth-only-{expected_eu14}"
    isolated_env.prices[single_trap_id] = 0.05

    need_payload = {
        "customer_id": "hotel-aurora",
        "product_query": "culinary broth",
        "excluded_allergens": [alias],
        "quantity": 12.0,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }

    r_http = http_app_client.post("/v1/plan", headers=H_AURORA, json=need_payload)
    assert r_http.status_code == 200, r_http.text
    plan_http = r_http.json()

    is_err, plan_mcp = _mcp_streamable_call(H_AURORA, "plan_need", {"need": need_payload})
    assert not is_err, plan_mcp
    assert isinstance(plan_mcp, dict)

    assert plan_http["need"]["excluded_allergens"] == [expected_eu14]
    assert plan_mcp["need"]["excluded_allergens"] == [expected_eu14]

    for plan in (plan_http, plan_mcp):
        _assert_plan_hardblock(plan, (expected_eu14,))
        line_ids = {ln["product_id"] for ln in plan["lines"]}
        assert line_ids
        assert single_trap_id not in line_ids
        assert line_ids.isdisjoint({"broth-all-contains", "broth-all-may", "broth-all-unk", "broth-all-inf"})

    assert [(ln["product_id"], ln["qty_sales_units"]) for ln in plan_http["lines"]] == [
        (ln["product_id"], ln["qty_sales_units"]) for ln in plan_mcp["lines"]
    ]


def test_hotel_aurora_milk_aliases_reject_house_lactose_free_milk_on_http_and_mcp(
    http_app_client: TestClient,
) -> None:
    for alias in ("maito", "mjölk"):
        need_payload = {
            "customer_id": "hotel-aurora",
            "product_query": "milk",
            "excluded_allergens": [alias],
            "preferred_product_ids": ["463038"],
            "quantity": 20.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        }
        r_http = http_app_client.post("/v1/plan", headers=H_AURORA, json=need_payload)
        assert r_http.status_code == 200, r_http.text
        plan_http = r_http.json()

        is_err, plan_mcp = _mcp_streamable_call(H_AURORA, "plan_need", {"need": need_payload})
        assert not is_err, plan_mcp

        for plan in (plan_http, plan_mcp):
            assert plan["need"]["excluded_allergens"] == ["milk"]
            _assert_plan_hardblock(plan, ("milk",))
            assert all(ln["product_id"] not in {"463038", "464306", "460396", "462775"} for ln in plan["lines"])


HOSPITAL_MULTILINGUAL_CASES: list[tuple[str, str, bool]] = [
    ("fi", "ruisleipä", False),
    ("fi", "vehnäleipä", False),
    ("sv", "rågbröd", False),
    ("sv", "vetebröd", False),
    ("fi", "gluteeniton leipä", True),
    ("sv", "glutenfritt bröd", True),
    ("en", "rye bread", False),
    ("en", "wheat bread", False),
    ("en", "gluten-free bread", True),
]


@pytest.mark.parametrize(
    ("lang", "query", "expects_safe_bread"),
    HOSPITAL_MULTILINGUAL_CASES,
    ids=[f"{c[0]}_{c[1].replace(' ', '_')}" for c in HOSPITAL_MULTILINGUAL_CASES],
)
def test_hospital_meilahti_multilingual_gluten_hardblock_http_and_mcp(
    http_app_client: TestClient,
    lang: str,
    query: str,
    expects_safe_bread: bool,
) -> None:
    _ = lang
    need_payload = {
        "customer_id": "hospital-meilahti",
        "product_query": query,
        "excluded_allergens": [],
        "quantity": 3.5,
        "unit": "KG",
        "delivery_by": DELIVERY.isoformat(),
    }

    r_http = http_app_client.post("/v1/plan", headers=H_HOSPITAL, json=need_payload)
    assert r_http.status_code == 200, r_http.text
    plan_http = r_http.json()

    is_err, plan_mcp = _mcp_streamable_call(H_HOSPITAL, "plan_need", {"need": need_payload})
    assert not is_err, plan_mcp
    assert isinstance(plan_mcp, dict)

    plan_ctx = _mcp_handler_call(H_HOSPITAL, "plan_need", need=need_payload)
    assert isinstance(plan_ctx, dict)

    assert [ln["product_id"] for ln in plan_http["lines"]] == [ln["product_id"] for ln in plan_mcp["lines"]]
    assert [ln["product_id"] for ln in plan_http["lines"]] == [ln["product_id"] for ln in plan_ctx["lines"]]

    for plan in (plan_http, plan_mcp, plan_ctx):
        _assert_plan_hardblock(plan, ("gluten",))
        if expects_safe_bread:
            assert plan["lines"], f"expected gluten-free bread for {query!r}"
            assert {ln["product_id"] for ln in plan["lines"]} <= {"473328", "461765"}
            for ln in plan["lines"]:
                assert "allergen_safe_alternative" not in ln["flags"]
        else:
            assert plan["lines"] == []
            assert plan["shortfall"] == pytest.approx(3.5)


def test_extras_list_recipes_http_and_mcp_and_app_with_extras(http_app_client: TestClient) -> None:
    import scenarios.app_with_extras as app_with_extras_mod

    r_http = http_app_client.get("/v1/recipes")
    assert r_http.status_code == 200, r_http.text
    http_dishes = r_http.json()
    assert isinstance(http_dishes, list)
    assert len(http_dishes) >= 10
    dish_ids = {d["id"] for d in http_dishes}
    assert {"lasagne", "pancakes", "salmon-soup", "oat-porridge"} <= dish_ids

    is_err, mcp_res = _mcp_streamable_call(H_SCHOOL, "list_recipes", {})
    assert not is_err, mcp_res
    assert isinstance(mcp_res, dict)
    assert [d["id"] for d in mcp_res["recipes"]] == [d["id"] for d in http_dishes]

    ctx_res = _mcp_handler_call(H_SCHOOL, "list_recipes")
    assert [d["id"] for d in ctx_res["recipes"]] == [d["id"] for d in http_dishes]

    with TestClient(app_with_extras_mod.app) as sc_client:
        r_sc = sc_client.get("/v1/recipes")
        assert r_sc.status_code == 200
        assert [d["id"] for d in r_sc.json()] == [d["id"] for d in http_dishes]


RECIPE_E2E_CASES: list[tuple[str, dict[str, str], dict[str, Any], tuple[str, ...]]] = [
    (
        "school-tampere",
        H_SCHOOL,
        {"text": "Lasagne 120 hengelle perjantaina", "excluded_allergens": []},
        ("milk", "nuts"),
    ),
    (
        "school-tampere",
        H_SCHOOL,
        {"text": "Pancakes for 80 on Wednesday", "excluded_allergens": ["gluteeni"]},
        ("milk", "nuts", "gluten"),
    ),
    (
        "school-tampere",
        H_SCHOOL,
        {
            "recipe_id": "salmon-soup",
            "portions": 60,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": ["maito", "pähkinä"],
        },
        ("milk", "nuts"),
    ),
    (
        "hospital-meilahti",
        H_HOSPITAL,
        {"text": "Lasagne 120 hengelle perjantaina", "excluded_allergens": []},
        ("gluten",),
    ),
    (
        "hospital-meilahti",
        H_HOSPITAL,
        {"text": "Pancakes for 80 on Wednesday", "excluded_allergens": ["maito", "pähkinä"]},
        ("gluten", "milk", "nuts"),
    ),
    (
        "hotel-aurora",
        H_AURORA,
        {"text": "Lasagne 120 hengelle perjantaina", "excluded_allergens": ["maito", "pähkinä", "gluteeni"]},
        ("milk", "nuts", "gluten"),
    ),
    (
        "hotel-aurora",
        H_AURORA,
        {"text": "Pancakes for 80 on Wednesday", "excluded_allergens": ["mjölk", "nötter"]},
        ("milk", "nuts"),
    ),
    (
        "hotel-aurora",
        H_AURORA,
        {
            "recipe_id": "oat-porridge",
            "portions": 70,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": ["vehnä", "ruis"],
        },
        ("gluten",),
    ),
]


@pytest.mark.parametrize(
    ("customer_id", "headers", "payload", "expected_excluded"),
    RECIPE_E2E_CASES,
    ids=[f"{c[0]}_{i}" for i, c in enumerate(RECIPE_E2E_CASES)],
)
def test_extras_plan_recipe_http_and_mcp_quote_and_order_allergen_invariants(
    http_app_client: TestClient,
    customer_id: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    expected_excluded: tuple[str, ...],
) -> None:
    r_http = http_app_client.post("/v1/recipes/plan", headers=headers, json=payload)
    assert r_http.status_code == 200, r_http.text
    res_http = r_http.json()

    mcp_args = {
        k: v
        for k, v in payload.items()
        if k in ("recipe_id", "portions", "delivery_by", "text", "excluded_allergens")
    }
    is_err, res_mcp = _mcp_streamable_call(headers, "plan_recipe", mcp_args)
    assert not is_err, res_mcp
    assert isinstance(res_mcp, dict)

    for res in (res_http, res_mcp):
        assert res["customer_id"] == customer_id
        assert set(expected_excluded) <= set(res["excluded_allergens"])
        assert res["ingredients"]
        assert res["quote_request"]["lines"]

        for ing in res["ingredients"]:
            assert set(expected_excluded) <= set(ing["need"]["excluded_allergens"])
            for ln in ing["plan"]["lines"]:
                assert "allergen_unknown" not in (ln.get("flags") or [])
                _assert_structured_free(str(ln["product_id"]), expected_excluded)
            for cand in ing["plan"].get("candidates") or []:
                if cand.get("tier") != "rejected":
                    pid = str(cand.get("product_id") or (cand.get("product") or {}).get("id"))
                    _assert_structured_free(pid, expected_excluded)

        for qline in res["quote_request"]["lines"]:
            _assert_structured_free(str(qline["product_id"]), expected_excluded)

    assert res_http["quote_request"]["lines"] == res_mcp["quote_request"]["lines"]

    qr = res_http["quote_request"]
    r_quote = http_app_client.post("/v1/quotes", headers=headers, json=qr)
    assert r_quote.status_code == 200, r_quote.text
    quote = r_quote.json()
    assert verify_quote_detailed(Quote.model_validate(quote))["ok"] is True
    for ql in quote["lines"]:
        assert ql["tier"] != "rejected", ql
        _assert_structured_free(str(ql["product_id"]), expected_excluded)

    r_order = http_app_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    order = r_order.json()
    assert order["status"] in ("placed", "pending_approval"), order
    assert order["decision"]["outcome"] in ("auto_approve", "needs_approval"), order["decision"]
    allergen_rejects = [
        h
        for h in order["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]
    assert allergen_rejects == [], order["decision"]["hits"]

    is_q_err, mcp_quote = _mcp_streamable_call(
        headers,
        "create_quote",
        {"delivery_date": qr["delivery_date"], "lines": qr["lines"]},
    )
    assert not is_q_err, mcp_quote
    is_o_err, mcp_order = _mcp_streamable_call(
        headers,
        "place_order",
        {"quote_id": mcp_quote["quote_id"], "quote_hash": mcp_quote["quote_hash"]},
    )
    assert not is_o_err, mcp_order
    assert mcp_order["status"] in ("placed", "pending_approval")
    assert not [
        h
        for h in mcp_order["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
    ]


UNSAFE_PROPOSAL_CASES: list[tuple[str, dict[str, str], str, str]] = [
    ("school-tampere", H_SCHOOL, "464306", "milk"),
    ("school-tampere", H_SCHOOL, "ml-oat-nut", "nuts"),
    ("school-tampere", H_SCHOOL, "ml-oat-maymilk", "milk"),
    ("school-tampere", H_SCHOOL, "ml-oat-unk", "unknown"),
    ("school-tampere", H_SCHOOL, "ml-oat-inf", "unknown"),
    ("hospital-meilahti", H_HOSPITAL, "ml-rye-contains", "gluten"),
    ("hospital-meilahti", H_HOSPITAL, "ml-wheatbread-contains", "gluten"),
    ("hospital-meilahti", H_HOSPITAL, "ml-rye-may", "gluten"),
]


@pytest.mark.parametrize(
    ("customer_id", "buyer_headers", "product_id", "expected_keyword"),
    UNSAFE_PROPOSAL_CASES,
    ids=[f"{c[0]}_{c[2]}" for c in UNSAFE_PROPOSAL_CASES],
)
def test_extras_proposals_unsafe_preview_reject_and_buyer_accept_rejected_and_seller_cannot_self_accept(
    http_app_client: TestClient,
    customer_id: str,
    buyer_headers: dict[str, str],
    product_id: str,
    expected_keyword: str,
) -> None:
    prop_body = {
        "customer_id": customer_id,
        "product_id": product_id,
        "qty_sales_units": 4,
        "delivery_date": DELIVERY.isoformat(),
        "note": "promotional offer",
    }
    r_prop = http_app_client.post("/v1/proposals", headers=H_SELLER, json=prop_body)
    assert r_prop.status_code == 200, r_prop.text
    prop_http = r_prop.json()

    is_err, prop_mcp = _mcp_streamable_call(H_SELLER, "propose_offer", prop_body)
    assert not is_err, prop_mcp
    assert isinstance(prop_mcp, dict)

    for prop in (prop_http, prop_mcp):
        assert prop["kind"] == "seller_pick"
        assert prop["seller_can_order"] is False
        assert prop["policy_preview"]["outcome"] == "reject"
        rej_hits = [
            h
            for h in prop["policy_preview"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
        ]
        assert rej_hits, prop["policy_preview"]
        assert expected_keyword in rej_hits[0]["message"].lower()

    r_self_accept = http_app_client.post(
        f"/v1/proposals/{prop_http['proposal_id']}/accept",
        headers={"X-Customer-Id": customer_id, **H_SELLER},
    )
    assert r_self_accept.status_code == 403
    assert r_self_accept.json()["error"] == "seller_cannot_order"

    r_self_decline = http_app_client.post(
        f"/v1/proposals/{prop_http['proposal_id']}/decline",
        headers={"X-Customer-Id": customer_id, **H_SELLER},
        json={"reason": "seller trying to decide"},
    )
    assert r_self_decline.status_code == 403
    assert r_self_decline.json()["error"] == "seller_cannot_order"

    for prop in (prop_http, prop_mcp):
        r_accept = http_app_client.post(
            f"/v1/proposals/{prop['proposal_id']}/accept",
            headers=buyer_headers,
        )
        assert r_accept.status_code == 200, r_accept.text
        accepted = r_accept.json()
        assert accepted["quote"]["lines"][0]["tier"] == "rejected"
        assert accepted["order"]["status"] == "rejected"
        assert accepted["order"]["status"] != "placed"
        assert accepted["order"]["decision"]["outcome"] == "reject"
        assert accepted["proposal"]["order_status"] == "rejected"
        order_hits = [
            h
            for h in accepted["order"]["decision"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
        ]
        assert order_hits
        assert expected_keyword in order_hits[0]["message"].lower()


def test_extras_proposals_safe_http_and_mcp_flow_cleanly_through_signed_quote_and_policy(
    http_app_client: TestClient,
    isolated_env: ConfigurableOfferBackendClient,
) -> None:
    r_buyer_prop = http_app_client.post(
        "/v1/proposals",
        headers=H_SCHOOL,
        json={"customer_id": "school-tampere", "product_id": "470889", "qty_sales_units": 5},
    )
    assert r_buyer_prop.status_code == 403
    assert r_buyer_prop.json()["error"] == "seller_required"

    safe_pick_body = {
        "customer_id": "school-tampere",
        "product_id": "470889",
        "qty_sales_units": 10,
        "delivery_date": DELIVERY.isoformat(),
        "note": "usual safe oat drink restock",
    }
    r_safe = http_app_client.post("/v1/proposals", headers=H_SELLER, json=safe_pick_body)
    assert r_safe.status_code == 200, r_safe.text
    safe_prop = r_safe.json()
    assert safe_prop["policy_preview"]["outcome"] == "auto_approve"
    assert safe_prop["policy_preview"]["tiers"]["470889"] == "preferred"

    r_self = http_app_client.post(
        f"/v1/proposals/{safe_prop['proposal_id']}/accept",
        headers={"X-Customer-Id": "school-tampere", **H_SELLER},
    )
    assert r_self.status_code == 403
    assert r_self.json()["error"] == "seller_cannot_order"

    r_accept = http_app_client.post(
        f"/v1/proposals/{safe_prop['proposal_id']}/accept",
        headers=H_SCHOOL,
    )
    assert r_accept.status_code == 200, r_accept.text
    accepted = r_accept.json()
    _assert_structured_free(accepted["quote"]["lines"][0]["product_id"], ("milk", "nuts"))
    assert verify_quote_detailed(Quote.model_validate(accepted["quote"]))["ok"] is True
    assert accepted["order"]["status"] == "placed"
    assert accepted["order"]["decision"]["outcome"] == "auto_approve"

    for unsafe_id in ("464306", "ml-oat-nut", "ml-oat-maymilk", "ml-oat-unk", "ml-oat-inf"):
        isolated_env.prices[unsafe_id] = 0.20
    isolated_env.prices["470889"] = 3.20
    isolated_env.prices["470863"] = 3.10
    isolated_env.prices["479001"] = 1.60

    is_err, cheap_prop = _mcp_streamable_call(
        H_SELLER,
        "propose_offer",
        {"customer_id": "school-tampere", "delivery_date": DELIVERY.isoformat()},
    )
    assert not is_err, cheap_prop
    assert cheap_prop["kind"] == "cheaper_equivalent"
    assert cheap_prop["product"]["product_id"] == "479001"
    _assert_structured_free(cheap_prop["product"]["product_id"], ("milk", "nuts"))
    assert cheap_prop["policy_preview"]["outcome"] in ("auto_approve", "needs_approval")

    r_accept_cheap = http_app_client.post(
        f"/v1/proposals/{cheap_prop['proposal_id']}/accept",
        headers=H_SCHOOL,
    )
    assert r_accept_cheap.status_code == 200, r_accept_cheap.text
    acc_cheap = r_accept_cheap.json()
    assert verify_quote_detailed(Quote.model_validate(acc_cheap["quote"]))["ok"] is True
    assert acc_cheap["order"]["status"] in ("placed", "pending_approval")
    assert acc_cheap["order"]["decision"]["outcome"] in ("auto_approve", "needs_approval")
    if acc_cheap["order"]["status"] == "pending_approval":
        r_dec = http_app_client.post(
            f"/v1/approvals/{acc_cheap['order']['order_id']}/decision",
            headers={"X-Approver-Pin": APPROVER_PIN},
            json={
                "decision": "approve",
                "approver": "liisa@school-tampere.example",
                "quote_hash": acc_cheap["quote"]["quote_hash"],
            },
        )
        assert r_dec.status_code == 200, r_dec.text
        assert r_dec.json()["status"] == "placed"


@pytest.mark.parametrize(
    ("customer_id", "headers"),
    [
        ("school-tampere", H_SCHOOL),
        ("hospital-meilahti", H_HOSPITAL),
        ("hotel-aurora", H_AURORA),
    ],
)
def test_extras_drift_http_and_mcp_valid_structure_without_500s(
    http_app_client: TestClient,
    customer_id: str,
    headers: dict[str, str],
) -> None:
    r_http = http_app_client.get(f"/v1/drift/{customer_id}", headers=headers)
    assert r_http.status_code == 200, r_http.text
    drift_http = r_http.json()

    is_err, drift_mcp = _mcp_streamable_call(
        headers,
        "check_standing_order_drift",
        {"customer_id": customer_id},
    )
    assert not is_err, drift_mcp
    assert isinstance(drift_mcp, dict)

    drift_ctx = _mcp_handler_call(headers, "check_standing_order_drift", customer_id=customer_id)
    assert isinstance(drift_ctx, dict)

    expected_seed_ids = [ln["product_id"] for ln in drift_mod.SEEDS[customer_id]["lines"]]
    for res in (drift_http, drift_mcp, drift_ctx):
        assert res["customer_id"] == customer_id
        assert isinstance(res["delivery_date"], str) and len(res["delivery_date"]) == 10
        assert [ln["product_id"] for ln in res["standing_order"]["lines"]] == expected_seed_ids
        assert isinstance(res["flags"], list)
        assert isinstance(res["summary"], str) and res["summary"]
        assert isinstance(res["provenance"], dict)
        assert not [f for f in res["flags"] if f["kind"] == "allergen"]
        for f in res["flags"]:
            assert f["kind"] in ("price", "volume", "availability", "allergen")
            assert f["route"] in ("auto", "approval")
            assert isinstance(f["message"], str) and f["message"]

    first_pid = expected_seed_ids[0]
    shock_headers = {**headers, "X-Aimo-Faults": f"price_shock:{first_pid}:1.25"}
    r_shock = http_app_client.get(f"/v1/drift/{customer_id}", headers=shock_headers)
    assert r_shock.status_code == 200, r_shock.text
    shock_flags = [f for f in r_shock.json()["flags"] if f["product_id"] == first_pid and f["kind"] == "price"]
    assert shock_flags
    assert shock_flags[0]["route"] == "approval"

    r_rebaseline = http_app_client.get(
        f"/v1/drift/{customer_id}",
        params={"rebaseline": "true"},
        headers=headers,
    )
    assert r_rebaseline.status_code == 200, r_rebaseline.text


def test_replay_fixture_subset_multilingual_plan_and_extras_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_data = tmp_path / "replay_data"
    replay_data.mkdir(parents=True, exist_ok=True)
    _close_cached_catalog_con()

    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", replay_data)
    monkeypatch.setattr(config, "PRODUCT_JSON", replay_data / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", replay_data / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", replay_data / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", replay_data / "catalog.sqlite")

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    monkeypatch.setattr(cat, "_SOURCE", src)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(catalog_store, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", config.CATALOG_DB)
    bc.set_client(None)
    _close_cached_catalog_con()

    with TestClient(extras.install(create_app(with_mcp=False))) as api:
        for q in ("maito", "mjölk", "milk"):
            r = api.post(
                "/v1/plan",
                headers=H_SCHOOL,
                json={
                    "customer_id": "school-tampere",
                    "product_query": q,
                    "excluded_allergens": [],
                    "quantity": 20.0,
                    "unit": "L",
                    "delivery_by": DELIVERY.isoformat(),
                },
            )
            assert r.status_code == 200, r.text
            p = r.json()
            _assert_plan_hardblock(p, ("milk", "nuts"))
            assert [ln["product_id"] for ln in p["lines"]] == ["470889"]
            assert "allergen_safe_alternative" in p["lines"][0]["flags"]

        r_rec = api.get("/v1/recipes")
        assert r_rec.status_code == 200
        assert len(r_rec.json()) >= 10

        svc_prop = seller_mod.propose_offer(
            "school-tampere",
            product_id="464306",
            qty_sales_units=3,
            delivery_date=DELIVERY.isoformat(),
            agent_id="valio-aimo/seller-1",
        )
        assert svc_prop["policy_preview"]["outcome"] == "reject"
        r_acc = api.post(f"/v1/proposals/{svc_prop['proposal_id']}/accept", headers=H_SCHOOL)
        assert r_acc.status_code == 200, r_acc.text
        assert r_acc.json()["order"]["status"] == "rejected"
