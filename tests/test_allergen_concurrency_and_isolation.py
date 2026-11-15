from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest
import yaml

from aimo import config, gemini
import aimo.catalog as cat
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport
from aimo.catalog import embeddings, gemini_allergens, get_product, plan as plan_mod, substitution as sub_mod, textsearch
from aimo.catalog.allergens import GS1_GROUPS
from aimo.catalog.plan import plan_need as catalog_plan_need
from aimo.catalog.substitution import rank_substitutes
from aimo.gateway import extras, human_auth, resilience, service, verify as verify_mod
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.gateway.verify import verify_lines
from aimo.models import EU14, Need, Offer, Quote
from aimo.trust import audit as trust_audit, policy as trust_policy
from aimo.trust.signing import verify_quote_detailed

DELIVERY = date(2026, 11, 16)
HKI = timezone(timedelta(hours=2))
FETCHED_AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
APPROVER_PIN = "test-pin-12chars-ok"
PINNED_MTIME = 1_763_120_000.0


class HeaderContext:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = dict(headers)


def _make_raw_product(
    pid: str,
    name: str,
    category: str,
    unit_kind: str,
    unit_amount: float,
    *,
    brand: str = "Valio",
    claims: tuple[str, ...] = (),
    allergen_states: dict[str, str] | None = None,
    ingredients: str = "water, salt",
) -> dict[str, Any]:
    states = {g: "free" for g in EU14}
    if allergen_states:
        states.update(allergen_states)
    allergen_vals: list[dict[str, str]] = []
    for g, st in states.items():
        head = GS1_GROUPS[g][0][0]
        if st == "contains":
            allergen_vals.append({"id": head, "unit": "CONTAINS"})
        elif st == "may_contain":
            allergen_vals.append({"id": head, "unit": "MAY_CONTAIN"})
        elif st == "free":
            allergen_vals.append({"id": head, "unit": "FREE_FROM"})
        elif st == "unknown":
            allergen_vals.append({"id": head, "unit": "UNDECLARED"})
    classifications: list[dict[str, Any]] = [
        {"name": "allergen", "values": allergen_vals},
        {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
    ]
    if claims:
        classifications.append({"name": "nutritionalClaim", "values": [{"id": c} for c in claims]})
    if unit_kind == "L":
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": unit_amount}}]
    else:
        conv = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": unit_amount}}]
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": "Synthetic Concurrency Wholesaler",
        "allowedLotSize": 1,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"language": "en", "value": name}, {"language": "fi", "value": name}],
            "brand": brand,
            "unitConversions": conv,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "nutritionalContent": [{"id": "ENERC", "value": 200.0, "basisQuantity": 100.0}],
            "marketingTexts": [{"language": "en", "value": f"{name} for professional kitchens."}],
        },
    }


def _synthetic_catalog_spec() -> tuple[
    list[dict[str, Any]],
    dict[str, float],
    dict[str, tuple[dict[str, str], dict[str, str]]],
]:
    products: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    inferred_patches: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    def add(
        pid: str,
        name: str,
        category: str,
        unit_kind: str,
        unit_amount: float,
        price: float,
        *,
        brand: str = "Valio",
        claims: tuple[str, ...] = (),
        allergen_states: dict[str, str] | None = None,
        ingredients: str = "water, salt",
        inferred_groups: tuple[str, ...] = (),
    ) -> None:
        products.append(
            _make_raw_product(
                pid,
                name,
                category,
                unit_kind,
                unit_amount,
                brand=brand,
                claims=claims,
                allergen_states=allergen_states,
                ingredients=ingredients,
            )
        )
        prices[pid] = price
        if inferred_groups:
            base_states = {g: "free" for g in EU14}
            if allergen_states:
                base_states.update(allergen_states)
            for g in inferred_groups:
                base_states[g] = "free"
            inf_sources = {
                g: ("inferred:gemini" if g in inferred_groups else "structured")
                for g in EU14
            }
            inferred_patches[pid] = (base_states, inf_sources)

    add(
        "463038",
        "Valio Eila semi skimmed milk drink 10 L lactose free",
        "10400",
        "L",
        10.0,
        14.00,
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
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "conc-milk-whole",
        "Valio whole milk 1 L",
        "10400",
        "L",
        1.0,
        1.15,
        allergen_states={"milk": "contains"},
        ingredients="pasteurised whole milk",
    )
    add(
        "conc-milk-may",
        "Shared-line whole milk drink 1 L",
        "10400",
        "L",
        1.0,
        0.99,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, rapeseed oil",
    )
    add(
        "conc-milk-unk",
        "Unverified farm whole milk 1 L",
        "10400",
        "L",
        1.0,
        0.92,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="milk",
    )
    add(
        "conc-milk-inf",
        "Inferred dairy-style whole milk 1 L",
        "10400",
        "L",
        1.0,
        0.88,
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="water, rapeseed oil",
    )
    add(
        "conc-milk-glutentrap",
        "Cereal-blended dairy milk 1 L",
        "10400",
        "L",
        1.0,
        0.85,
        brand="Generic",
        allergen_states={"milk": "contains", "gluten": "may_contain"},
        ingredients="milk, barley extract",
    )

    add(
        "470889",
        "Oatly oat drink whole 1 L",
        "10402",
        "L",
        1.0,
        1.95,
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, gluten-free oats, rapeseed oil, salt",
    )
    add(
        "conc-oat-safe2",
        "Oddlygood oat drink 1 L",
        "10402",
        "L",
        1.0,
        2.05,
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, gluten-free oats, rapeseed oil, salt",
    )
    add(
        "conc-oat-nut",
        "Hazelnut oat drink 1 L",
        "10402",
        "L",
        1.0,
        1.05,
        brand="Nordic",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={"nuts": "contains"},
        ingredients="water, gluten-free oats, hazelnut",
    )
    add(
        "conc-oat-maymilk",
        "Shared-line oat drink 1 L",
        "10402",
        "L",
        1.0,
        1.08,
        brand="Nordic",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={"milk": "may_contain"},
        ingredients="water, gluten-free oats, rapeseed oil",
    )
    add(
        "conc-oat-maynut",
        "Almond-trace oat drink 1 L",
        "10402",
        "L",
        1.0,
        1.10,
        brand="Nordic",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={"nuts": "may_contain"},
        ingredients="water, gluten-free oats, rapeseed oil",
    )
    add(
        "conc-oat-unk",
        "Unverified oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.95,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown", "gluten": "unknown"},
        ingredients="water, oats",
    )
    add(
        "conc-oat-inf",
        "Inferred oat drink 1 L",
        "10402",
        "L",
        1.0,
        0.90,
        brand="Generic",
        claims=("FREE_FROM_GLUTEN",),
        inferred_groups=("milk", "nuts"),
        ingredients="water, gluten-free oats",
    )

    add(
        "conc-butter-reg",
        "Valio butter 500 g",
        "10500",
        "KG",
        0.5,
        3.10,
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "conc-butter-lf",
        "Valio Eila lactose-free butter 500 g",
        "10500",
        "KG",
        0.5,
        3.40,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "conc-butter-may",
        "Shared-churn butter blend 500 g",
        "10500",
        "KG",
        0.5,
        1.95,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="vegetable oil, water",
    )
    add(
        "conc-butter-unk",
        "Unverified butter block 500 g",
        "10500",
        "KG",
        0.5,
        1.80,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="fat blend",
    )
    add(
        "conc-butter-glutentrap",
        "Herb crumb butter 500 g",
        "10500",
        "KG",
        0.5,
        1.50,
        brand="Generic",
        allergen_states={"milk": "contains", "gluten": "may_contain"},
        ingredients="cream, wheat crumb, herbs",
    )

    add(
        "conc-marg-safe",
        "Sunnuntai lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        2.90,
        brand="Sunnuntai",
        claims=("FREE_FROM_LACTOSE", "VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="rapeseed oil, water, salt",
    )
    add(
        "conc-marg-may",
        "Blended lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        1.65,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "may_contain"},
        ingredients="rapeseed oil, water, salt",
    )
    add(
        "conc-marg-unk",
        "Unverified lactose free margarine 1 kg",
        "1051",
        "KG",
        1.0,
        1.55,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="rapeseed oil, water",
    )

    add(
        "conc-cream-reg",
        "Valio cooking cream 15% 1 L",
        "10401",
        "L",
        1.0,
        2.70,
        allergen_states={"milk": "contains"},
        ingredients="cream, stabilizer",
    )
    add(
        "conc-cream-lf",
        "Valio Eila lactose-free cream 15% 1 L",
        "10401",
        "L",
        1.0,
        2.95,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, lactase",
    )
    add(
        "conc-cream-may",
        "Shared-line cooking cream 1 L",
        "10401",
        "L",
        1.0,
        1.95,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, vegetable oil",
    )
    add(
        "conc-cream-unk",
        "Unverified cooking cream 1 L",
        "10401",
        "L",
        1.0,
        1.85,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="water, vegetable fat",
    )
    add(
        "conc-oatcook-safe",
        "Oddlygood oat cooking 1 L",
        "10402",
        "L",
        1.0,
        2.40,
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, gluten-free oats, rapeseed oil, salt",
    )
    add(
        "conc-oatcook-nut",
        "Cashew oat cooking 1 L",
        "10402",
        "L",
        1.0,
        1.35,
        brand="Generic",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={"nuts": "may_contain"},
        ingredients="water, gluten-free oats, rapeseed oil",
    )

    add(
        "conc-almonds-contains",
        "Whole almonds 1 kg",
        "21400",
        "KG",
        1.0,
        8.50,
        brand="Nordic",
        allergen_states={"nuts": "contains"},
        ingredients="almonds",
    )
    add(
        "conc-almonds-may",
        "Sliced almonds 1 kg",
        "21400",
        "KG",
        1.0,
        7.90,
        brand="Nordic",
        allergen_states={"nuts": "may_contain"},
        ingredients="almonds",
    )
    add(
        "conc-almonds-unk",
        "Unverified almonds 1 kg",
        "21400",
        "KG",
        1.0,
        6.50,
        brand="Generic",
        allergen_states={"nuts": "unknown", "milk": "unknown"},
        ingredients="seeds",
    )

    add(
        "473328",
        "Schar Vital dark sliced bread 350 g gluten-free",
        "11000",
        "KG",
        0.35,
        3.40,
        brand="Schar",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="water, rice flour, buckwheat",
    )
    add(
        "conc-bread-wheat",
        "Fazer wheat toast bread 500 g",
        "11000",
        "KG",
        0.5,
        1.75,
        brand="Fazer",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour, water, yeast",
    )
    add(
        "conc-bread-rye",
        "Vaasan sliced rye bread 500 g",
        "11000",
        "KG",
        0.5,
        1.65,
        brand="Vaasan",
        allergen_states={"gluten": "contains"},
        ingredients="wholegrain rye, water, salt",
    )
    add(
        "conc-bread-may",
        "Shared-bakery sliced bread 500 g",
        "11000",
        "KG",
        0.5,
        1.50,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, water",
    )
    add(
        "conc-bread-unk",
        "Unverified sliced bread 500 g",
        "11000",
        "KG",
        0.5,
        1.40,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="corn starch, water",
    )
    add(
        "conc-bread-inf",
        "Inferred gluten-free bread 500 g",
        "11000",
        "KG",
        0.5,
        1.30,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="corn starch, water",
    )
    add(
        "conc-bread-nuttrap",
        "Walnut wheat bread 500 g",
        "11000",
        "KG",
        0.5,
        1.25,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "contains"},
        ingredients="wheat flour, walnuts, water",
    )

    add(
        "conc-flour-wheat",
        "Sunnuntai wheat flour 2 kg",
        "2130",
        "KG",
        2.0,
        1.75,
        brand="Sunnuntai",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour",
    )
    add(
        "conc-flour-nuttrap",
        "Millhouse wheat flour 2 kg",
        "2130",
        "KG",
        2.0,
        1.30,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="wheat flour",
    )
    add(
        "conc-flour-may",
        "Cross-line gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.60,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, potato starch",
    )
    add(
        "conc-flour-unk",
        "Unverified gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.50,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, corn starch",
    )
    add(
        "conc-flour-inf",
        "Inferred gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        1.45,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, potato starch",
    )
    add(
        "conc-flour-safe",
        "Semper gluten-free flour mix 1 kg",
        "2131",
        "KG",
        1.0,
        3.10,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="rice flour, potato starch, corn starch",
    )

    add(
        "conc-eggs-trap",
        "Cream-blended liquid eggs 1 kg",
        "10590",
        "KG",
        1.0,
        2.50,
        brand="Generic",
        allergen_states={"eggs": "contains", "milk": "may_contain", "gluten": "may_contain"},
        ingredients="egg, cream",
    )
    add(
        "conc-eggs-safe",
        "Kultamuna pasteurised liquid eggs 1 kg",
        "10590",
        "KG",
        1.0,
        4.10,
        brand="Kultamuna",
        allergen_states={"eggs": "contains"},
        ingredients="pasteurised egg",
    )
    add(
        "conc-sugar-trap",
        "Unverified bakery sugar 2 kg",
        "22100",
        "KG",
        2.0,
        1.20,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "may_contain", "gluten": "unknown"},
        ingredients="sugar",
    )
    add(
        "conc-sugar-safe",
        "Dansukker granulated sugar 2 kg",
        "22100",
        "KG",
        2.0,
        2.40,
        brand="Dansukker",
        allergen_states={},
        ingredients="sugar",
    )
    add(
        "conc-oatflakes-reg",
        "Elovena oat flakes 1 kg",
        "21370",
        "KG",
        1.0,
        1.90,
        brand="Elovena",
        allergen_states={"gluten": "contains"},
        ingredients="rolled oats",
    )
    add(
        "conc-oatflakes-nuttrap",
        "Nut-mill oat flakes 1 kg",
        "21370",
        "KG",
        1.0,
        1.20,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="rolled oats",
    )
    add(
        "conc-gfoats-unk",
        "Unverified gluten-free oats 1 kg",
        "2137",
        "KG",
        1.0,
        1.50,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="corn grits",
    )
    add(
        "conc-gfoats-safe",
        "Provenance gluten-free oats 1 kg",
        "2137",
        "KG",
        1.0,
        2.70,
        brand="Provenance",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="gluten-free oats",
    )
    add(
        "conc-pasta-reg",
        "Myllyn Paras penne pasta 2 kg",
        "21380",
        "KG",
        2.0,
        2.30,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "conc-pasta-nuttrap",
        "Hazelnut-mill penne pasta 2 kg",
        "21380",
        "KG",
        2.0,
        1.40,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="durum wheat",
    )
    add(
        "conc-gfpasta-may",
        "Shared-mill gluten-free pasta 1 kg",
        "21380",
        "KG",
        1.0,
        1.85,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="corn flour, rice flour",
    )
    add(
        "conc-gfpasta-safe",
        "Semper gluten-free pasta 1 kg",
        "21380",
        "KG",
        1.0,
        3.40,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="corn flour, rice flour",
    )
    add(
        "conc-chicken-safe",
        "Kariniemen chicken fillet 2 kg",
        "1875",
        "KG",
        2.0,
        13.20,
        brand="Kariniemen",
        allergen_states={},
        ingredients="chicken fillet, salt",
    )
    add(
        "conc-onion-safe",
        "Peeled yellow onion 2 kg",
        "17405",
        "KG",
        2.0,
        2.30,
        brand="Tuore",
        allergen_states={},
        ingredients="onion",
    )

    add(
        "eu14-soup-allfree",
        "Pure garden kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        3.95,
        brand="NordicPure",
        allergen_states={},
        ingredients="water, carrot, potato, salt",
    )
    add(
        "eu14-soup-usual-ps",
        "Umami soy kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        3.10,
        brand="NordicPure",
        allergen_states={"soy": "contains"},
        ingredients="water, soybeans, salt",
    )
    add(
        "eu14-soup-usual-esf",
        "Herb garden kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        3.20,
        brand="NordicPure",
        allergen_states={"celery": "contains"},
        ingredients="water, celery, carrot, salt",
    )
    add(
        "eu14-soup-usual-six",
        "Roasted peanut kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        3.30,
        brand="NordicPure",
        allergen_states={"peanuts": "contains"},
        ingredients="water, peanuts, salt",
    )
    add(
        "eu14-trap-peanuts-con",
        "Satay kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.10,
        brand="Generic",
        allergen_states={"peanuts": "contains"},
        ingredients="water, peanuts, spices",
    )
    add(
        "eu14-trap-peanuts-unk",
        "Street kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.12,
        brand="Generic",
        allergen_states={"peanuts": "unknown"},
        ingredients="water, spices",
    )
    add(
        "eu14-trap-sesame-may",
        "Tahini kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.14,
        brand="Generic",
        allergen_states={"sesame": "may_contain"},
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-sesame-inf",
        "Inferred sesame kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.16,
        brand="Generic",
        inferred_groups=("sesame",),
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-eggs-con",
        "Egg-drop kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.20,
        brand="Generic",
        allergen_states={"eggs": "contains"},
        ingredients="water, egg, salt",
    )
    add(
        "eu14-trap-soy-may",
        "Tamari kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.22,
        brand="Generic",
        allergen_states={"soy": "may_contain"},
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-fish-con",
        "Bonito kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.24,
        brand="Generic",
        allergen_states={"fish": "contains"},
        ingredients="water, tuna extract, salt",
    )
    add(
        "eu14-trap-fish-unk",
        "Coastal kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.26,
        brand="Generic",
        allergen_states={"fish": "unknown"},
        ingredients="water, kelp, salt",
    )
    add(
        "eu14-trap-fish-inf",
        "Inferred sea kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.28,
        brand="Generic",
        inferred_groups=("fish", "soy"),
        ingredients="water, kelp, salt",
    )
    add(
        "eu14-trap-crustaceans-con",
        "Prawn kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.30,
        brand="Generic",
        allergen_states={"crustaceans": "contains"},
        ingredients="water, prawns, salt",
    )
    add(
        "eu14-trap-molluscs-may",
        "Mussel kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.32,
        brand="Generic",
        allergen_states={"molluscs": "may_contain"},
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-sulphites-con",
        "Wine-reduced kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.34,
        brand="Generic",
        allergen_states={"sulphites": "contains"},
        ingredients="water, sulphites, salt",
    )
    add(
        "eu14-trap-celery-may",
        "Mirepoix kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.36,
        brand="Generic",
        allergen_states={"celery": "may_contain"},
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-mustard-con",
        "Dijon kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.38,
        brand="Generic",
        allergen_states={"mustard": "contains"},
        ingredients="water, mustard, salt",
    )
    add(
        "eu14-trap-lupin-unk",
        "Lupin-protein kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.40,
        brand="Generic",
        allergen_states={"lupin": "unknown"},
        ingredients="water, pea protein, salt",
    )
    add(
        "eu14-trap-lupin-inf",
        "Inferred legume kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.42,
        brand="Generic",
        inferred_groups=("lupin", "mustard"),
        ingredients="water, pea protein, salt",
    )
    add(
        "eu14-trap-milk-con",
        "Creamy dairy kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.06,
        brand="Generic",
        allergen_states={"milk": "contains"},
        ingredients="water, cream, salt",
    )
    add(
        "eu14-trap-nuts-may",
        "Cashew-trace kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.07,
        brand="Generic",
        allergen_states={"nuts": "may_contain"},
        ingredients="water, carrot, salt",
    )
    add(
        "eu14-trap-gluten-con",
        "Roux wheat kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.08,
        brand="Generic",
        allergen_states={"gluten": "contains"},
        ingredients="water, wheat flour, salt",
    )
    add(
        "eu14-trap-contradiction",
        "Mislabeled bistro kitchen soup base 1 L",
        "21500",
        "L",
        1.0,
        1.05,
        brand="Generic",
        allergen_states={},
        ingredients="water, peanuts, egg powder, celery, salt",
    )

    return products, prices, inferred_patches


class ThreadSafeStaticBackendClient:
    base_url = "static://allergen-concurrency-isolation"
    ttl_s = 300

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = dict(prices)
        self._lock = threading.Lock()
        self.stats = {"upstream_calls": 0}

    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _wrap(self, items: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self.stats["upstream_calls"] += 1
        return {
            **meta,
            "products": [{**it, "_fetched_at": now, "_meta": meta} for it in items],
            "_oldest_fetch": now,
        }

    def price(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        valid_from = "2026-11-01T00:00:00+0200"
        valid_to = "9999-12-31T00:00:00+0200"
        items: list[dict[str, Any]] = []
        for pid in product_ids:
            s_pid = str(pid)
            net = float(self.prices.get(s_pid, 2.50))
            gross = round(net * 1.135, 2)
            items.append(
                {
                    "productId": s_pid,
                    "salesUnit": "ST",
                    "quantity": 1.0,
                    "catalogPrice": {
                        "value": net,
                        "valueWithTax": gross,
                        "validFrom": valid_from,
                        "validTo": valid_to,
                        "currency": "EUR",
                    },
                    "priceWithTax": {"value": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "listPrice": {
                        "value": net,
                        "valueWithTax": gross,
                        "validFrom": valid_from,
                        "validTo": valid_to,
                        "currency": "EUR",
                    },
                    "tax": {"taxRate": 13.5, "taxValue": round(gross - net, 2)},
                    "netPriceFound": True,
                    "error": False,
                }
            )
        return self._wrap(
            items,
            {
                "customerId": customer or "demo",
                "currency": "EUR",
                "pricingDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )

    def availability(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        close = datetime.combine(requested_date - timedelta(days=1), datetime.min.time(), HKI).replace(hour=14)
        now_dt = datetime.combine(requested_date - timedelta(days=2), datetime.min.time(), HKI).replace(hour=9)
        items = [
            {
                "productId": str(pid),
                "closingTime": self._iso(close),
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 999999.0,
                        "availableDate": requested_date.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            }
            for pid in product_ids
        ]
        return self._wrap(
            items,
            {
                "timeNow": self._iso(now_dt),
                "requestedDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )


def _offer_map(pids: list[str], prices: dict[str, float]) -> dict[str, Offer]:
    out: dict[str, Offer] = {}
    for pid in pids:
        net = float(prices.get(pid, 2.50))
        out[pid] = Offer(
            product_id=pid,
            requested_date=DELIVERY,
            available=True,
            available_amount=5000.0,
            available_date=DELIVERY,
            cutoff_passed=False,
            closing_time=FETCHED_AT + timedelta(days=1),
            logistic_model="Stock",
            sales_unit="ST",
            unit_price=net,
            unit_price_gross=round(net * 1.135, 2),
            comparison_price=net,
            comparison_unit="L",
            fetched_at=FETCHED_AT,
        )
    return out


class _CacheInfo:
    def __init__(self, currsize: int) -> None:
        self.currsize = currsize


class _ThreadLocalCatalogCon:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._open_cons: list[sqlite3.Connection] = []

    def __call__(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(str(self._db_path), check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
            with self._lock:
                self._open_cons.append(con)
        return con

    def cache_clear(self) -> None:
        self._local = threading.local()
        with self._lock:
            old = list(self._open_cons)
            self._open_cons.clear()
        for c in old:
            try:
                c.close()
            except Exception:
                pass

    def cache_info(self) -> _CacheInfo:
        with self._lock:
            return _CacheInfo(len(self._open_cons))


def _close_cached_catalog_con() -> None:
    if hasattr(textsearch._con, "cache_info") and textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    if hasattr(textsearch._con, "cache_clear"):
        textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _reset_breakers() -> None:
    for b in resilience._breakers.values():
        b.reset()


@pytest.fixture(scope="module")
def synthetic_catalog_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    cat_dir = tmp_path_factory.mktemp("allergen_concurrency_catalog")
    products, prices, inferred_patches = _synthetic_catalog_spec()
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
                "update products set allergens = ?, allergen_source = ? where id = ?",
                (json.dumps(a_map), json.dumps(s_map), pid),
            )
        con.commit()
    finally:
        con.close()

    return {
        "dir": cat_dir,
        "product_json": prod_json,
        "catalog_db": cat_db,
        "rel_csv": rel_csv,
        "co2_csv": co2_csv,
        "prices": prices,
    }


@pytest.fixture(autouse=True)
def isolated_env(synthetic_catalog_bundle: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    orig_policy_dir = trust_policy.POLICY_DIR
    _close_cached_catalog_con()
    store.close_all()
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
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("aimo.gemini must never be called in offline concurrency tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *a, **k: [])
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: None)

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

    tl_con = _ThreadLocalCatalogCon(synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(textsearch, "_con", tl_con)
    monkeypatch.setattr(plan_mod, "_con", tl_con)

    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")

    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(ThreadSafeStaticBackendClient(synthetic_catalog_bundle["prices"]))
    textsearch._nonfood_categories()
    try:
        yield
    finally:
        trust_policy.POLICY_DIR = orig_policy_dir
        if hasattr(trust_policy, "_load_policy_cached"):
            trust_policy._load_policy_cached.cache_clear()
        bc.set_client(None)
        store.close_all()
        if hasattr(trust_audit, "_ready"):
            trust_audit._ready.discard(str(state_db))
        tl_con.cache_clear()
        _reset_breakers()
        _close_cached_catalog_con()


def _assert_product_structured_free(pid: str, excluded: tuple[str, ...]) -> None:
    prod = get_product(pid)
    assert prod is not None, f"missing product {pid}"
    row = textsearch.get_rows([pid]).get(pid) or {}
    conflicts = row.get("allergen_conflicts") or {}
    for g in excluded:
        assert g not in conflicts, f"product {pid} has allergen data conflict on {g}: {conflicts[g]}"
        st = prod.allergens.get(g)
        val = st.value if hasattr(st, "value") else str(st or "unknown")
        src = prod.allergen_source.get(g)
        assert val == "free", f"product {pid} ({prod.name}) leaked {g}={val!r}"
        assert src == "structured", f"product {pid} ({prod.name}) leaked non-structured {g} source={src!r}"


def _assert_plan_dict_isolation(
    plan: dict[str, Any],
    customer_id: str,
    expected_excluded: tuple[str, ...],
    forbidden_bleed: tuple[str, ...] = (),
) -> None:
    need_obj = plan.get("need") or {}
    assert need_obj.get("customer_id") == customer_id
    need_excl = set(need_obj.get("excluded_allergens") or [])
    assert set(expected_excluded) <= need_excl, (customer_id, expected_excluded, need_excl)
    for b in forbidden_bleed:
        assert b not in need_excl, f"cross-customer bleed: {b!r} leaked into {customer_id} need.excluded_allergens"
    for ln in plan.get("lines") or []:
        if expected_excluded:
            assert "allergen_unknown" not in (ln.get("flags") or [])
            _assert_product_structured_free(str(ln["product_id"]), expected_excluded)
    for cand in plan.get("candidates") or []:
        if cand.get("tier") != "rejected" and expected_excluded:
            assert "allergen_unknown" not in (cand.get("flags") or [])
            prod_info = cand.get("product") or {}
            cid = str(prod_info.get("id") or cand.get("product_id"))
            _assert_product_structured_free(cid, expected_excluded)


def _assert_customer_query_outcome(customer_id: str, query: str, plan: dict[str, Any]) -> None:
    line_ids = [str(ln["product_id"]) for ln in (plan.get("lines") or [])]
    if customer_id == "hotel-aurora":
        _assert_plan_dict_isolation(plan, "hotel-aurora", (), ("milk", "nuts", "gluten"))
        if query in ("milk", "lactose-free milk"):
            assert line_ids, f"hotel-aurora expected dairy milk lines for {query!r}"
            assert set(line_ids) <= {"463038", "464306"}
            for pid in line_ids:
                p = get_product(pid)
                assert p is not None
                st = p.allergens.get("milk")
                assert (st.value if hasattr(st, "value") else st) == "contains"
        elif query == "wheat flour":
            assert line_ids, "hotel-aurora expected wheat flour lines"
            for pid in line_ids:
                p = get_product(pid)
                assert p is not None
                st = p.allergens.get("gluten")
                assert (st.value if hasattr(st, "value") else st) == "contains"
    elif customer_id == "school-tampere":
        _assert_plan_dict_isolation(plan, "school-tampere", ("milk", "nuts"), ("gluten",))
        if query in ("milk", "whole milk"):
            assert line_ids == ["470889"]
            assert "allergen_safe_alternative" in (plan["lines"][0].get("flags") or [])
        elif query == "oat drink":
            assert line_ids == ["470889"]
            rej = {
                str((c.get("product") or {}).get("id"))
                for c in (plan.get("candidates") or [])
                if c.get("tier") == "rejected"
            }
            assert {"conc-oat-nut", "conc-oat-maymilk", "conc-oat-unk", "conc-oat-inf"} & rej
        elif query == "almonds":
            assert line_ids == []
            assert all(c.get("tier") == "rejected" for c in (plan.get("candidates") or []))
        elif query == "wheat flour":
            assert line_ids == ["conc-flour-wheat"]
    elif customer_id == "hospital-meilahti":
        _assert_plan_dict_isolation(plan, "hospital-meilahti", ("gluten",), ("milk", "nuts"))
        if query == "bread":
            assert line_ids == ["473328"]
        elif query == "rye bread":
            assert set(line_ids) <= {"473328"}
            for cand in plan.get("candidates") or []:
                cid_cand = str((cand.get("product") or {}).get("id") or "")
                if cid_cand != "473328":
                    assert cand.get("tier") == "rejected"
        elif query == "flour":
            assert line_ids == ["conc-flour-safe"]
        elif query == "milk":
            assert line_ids, "hospital-meilahti should still plan gluten-free dairy milk"
            assert "conc-milk-glutentrap" not in line_ids


CROSS_CUSTOMER_WORKLOAD: list[tuple[str, str, float, str]] = [
    ("hotel-aurora", "milk", 20.0, "L"),
    ("school-tampere", "milk", 20.0, "L"),
    ("hospital-meilahti", "bread", 7.0, "KG"),
    ("hotel-aurora", "wheat flour", 10.0, "KG"),
    ("school-tampere", "oat drink", 20.0, "L"),
    ("hospital-meilahti", "rye bread", 7.0, "KG"),
    ("hotel-aurora", "lactose-free milk", 10.0, "L"),
    ("school-tampere", "whole milk", 15.0, "L"),
    ("hospital-meilahti", "flour", 8.0, "KG"),
    ("school-tampere", "almonds", 4.0, "KG"),
    ("hospital-meilahti", "milk", 10.0, "L"),
    ("school-tampere", "wheat flour", 10.0, "KG"),
]


def test_concurrent_direct_catalog_plan_need_24_threads_cross_customer_isolation() -> None:
    n_workers = 24
    barrier = threading.Barrier(n_workers)
    baselines: dict[tuple[str, str, float, str], list[str]] = {}
    for cid, q, qty, u in CROSS_CUSTOMER_WORKLOAD:
        b_plan = catalog_plan_need(
            Need(
                customer_id=cid,
                product_query=q,
                excluded_allergens=[],
                quantity=qty,
                unit=u,
                delivery_by=DELIVERY,
            )
        ).model_dump(mode="json")
        _assert_customer_query_outcome(cid, q, b_plan)
        baselines[(cid, q, qty, u)] = [ln["product_id"] for ln in b_plan["lines"]]

    tasks = [CROSS_CUSTOMER_WORKLOAD[i % len(CROSS_CUSTOMER_WORKLOAD)] for i in range(48)]

    def worker(idx: int) -> tuple[str, str, dict[str, Any]]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, q, qty, u = tasks[idx]
        need = Need(
            customer_id=cid,
            product_query=q,
            excluded_allergens=[],
            quantity=qty,
            unit=u,
            delivery_by=DELIVERY,
        )
        res = catalog_plan_need(need).model_dump(mode="json")
        return cid, q, res

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=30.0) for f in futures]

    assert len(results) == 48
    for idx, (cid, q, plan_dict) in enumerate(results):
        _assert_customer_query_outcome(cid, q, plan_dict)
        spec = tasks[idx]
        assert [ln["product_id"] for ln in plan_dict["lines"]] == baselines[spec]


def test_concurrent_gateway_plan_deterministic_24_threads_cross_customer_isolation() -> None:
    n_workers = 24
    barrier = threading.Barrier(n_workers)
    baselines: dict[tuple[str, str, float, str], list[str]] = {}
    for cid, q, qty, u in CROSS_CUSTOMER_WORKLOAD:
        b_plan = service._plan_deterministic(
            Need(
                customer_id=cid,
                product_query=q,
                excluded_allergens=[],
                quantity=qty,
                unit=u,
                delivery_by=DELIVERY,
            )
        ).model_dump(mode="json")
        assert "deterministic planner" in b_plan["explanation"]
        _assert_customer_query_outcome(cid, q, b_plan)
        baselines[(cid, q, qty, u)] = [ln["product_id"] for ln in b_plan["lines"]]

    tasks = [CROSS_CUSTOMER_WORKLOAD[i % len(CROSS_CUSTOMER_WORKLOAD)] for i in range(48)]

    def worker(idx: int) -> tuple[str, str, dict[str, Any]]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, q, qty, u = tasks[idx]
        need = Need(
            customer_id=cid,
            product_query=q,
            excluded_allergens=[],
            quantity=qty,
            unit=u,
            delivery_by=DELIVERY,
        )
        res = service._plan_deterministic(need).model_dump(mode="json")
        return cid, q, res

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=30.0) for f in futures]

    assert len(results) == 48
    for idx, (cid, q, plan_dict) in enumerate(results):
        assert "deterministic planner" in plan_dict["explanation"]
        _assert_customer_query_outcome(cid, q, plan_dict)
        spec = tasks[idx]
        assert [ln["product_id"] for ln in plan_dict["lines"]] == baselines[spec]


def test_concurrent_http_post_v1_plan_24_threads_shared_app_and_mismatch_guard() -> None:
    app = create_app(with_mcp=False)
    shared_client = TestClient(app)
    n_workers = 24
    barrier = threading.Barrier(n_workers)
    tasks = [CROSS_CUSTOMER_WORKLOAD[i % len(CROSS_CUSTOMER_WORKLOAD)] for i in range(48)]

    def worker(idx: int) -> dict[str, Any]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, q, qty, u = tasks[idx]
        headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
        if idx % 16 == 15:
            spoof_body = {
                "customer_id": "hotel-aurora",
                "product_query": "milk",
                "excluded_allergens": [],
                "preferred_product_ids": ["463038"],
                "quantity": 20.0,
                "unit": "L",
                "delivery_by": DELIVERY.isoformat(),
            }
            r_spoof = shared_client.post(
                "/v1/plan",
                headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
                json=spoof_body,
            )
            return {"kind": "spoof", "status": r_spoof.status_code, "body": r_spoof.json()}
        body = {
            "customer_id": cid,
            "product_query": q,
            "excluded_allergens": [],
            "quantity": qty,
            "unit": u,
            "delivery_by": DELIVERY.isoformat(),
        }
        resp = shared_client.post("/v1/plan", headers=headers, json=body)
        assert resp.status_code == 200, resp.text
        return {"kind": "plan", "cid": cid, "query": q, "plan": resp.json()}

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=30.0) for f in futures]

    assert len(results) == 48
    spoof_count = 0
    for item in results:
        if item["kind"] == "spoof":
            spoof_count += 1
            assert item["status"] == 403
            assert item["body"]["error"] == "customer_mismatch"
        else:
            _assert_customer_query_outcome(item["cid"], item["query"], item["plan"])
    assert spoof_count == 3


def test_concurrent_http_post_v1_recipes_plan_18_threads_cross_customer_isolation() -> None:
    app = create_app(with_mcp=False)
    shared_client = TestClient(app)
    n_workers = 18
    barrier = threading.Barrier(n_workers)
    recipe_cases: list[tuple[str, str, int]] = [
        ("hotel-aurora", "pancakes", 40),
        ("school-tampere", "pancakes", 40),
        ("hospital-meilahti", "pancakes", 40),
        ("hotel-aurora", "oat-porridge", 30),
        ("school-tampere", "oat-porridge", 30),
        ("hospital-meilahti", "oat-porridge", 30),
        ("hotel-aurora", "creamy-chicken-pasta", 25),
        ("school-tampere", "creamy-chicken-pasta", 25),
        ("hospital-meilahti", "creamy-chicken-pasta", 25),
    ]
    tasks = [recipe_cases[i % len(recipe_cases)] for i in range(36)]

    def worker(idx: int) -> tuple[str, str, dict[str, Any]]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, rid, portions = tasks[idx]
        resp = shared_client.post(
            "/v1/recipes/plan",
            headers={"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"},
            json={
                "recipe_id": rid,
                "portions": portions,
                "delivery_by": DELIVERY.isoformat(),
                "customer_id": cid,
                "excluded_allergens": [],
                "include_candidates": True,
            },
        )
        assert resp.status_code == 200, resp.text
        return cid, rid, resp.json()

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=45.0) for f in futures]

    assert len(results) == 36
    for cid, rid, payload in results:
        assert payload["customer_id"] == cid
        assert payload["quote_request"]["customer_id"] == cid
        q_lines = payload["quote_request"]["lines"]
        assert q_lines, f"expected recipe quote lines for {cid} {rid}"
        if cid == "hotel-aurora":
            assert payload["excluded_allergens"] == []
            if rid in ("pancakes", "oat-porridge"):
                milk_ings = [ing for ing in payload["ingredients"] if ing["ingredient"] == "milk"]
                assert len(milk_ings) == 1
                milk_pids = [ln["product_id"] for ln in milk_ings[0]["plan"]["lines"]]
                assert set(milk_pids) & {"463038", "464306"}
        elif cid == "school-tampere":
            assert {"milk", "nuts"} <= set(payload["excluded_allergens"])
            assert "gluten" not in set(payload["excluded_allergens"])
            for ing in payload["ingredients"]:
                _assert_plan_dict_isolation(ing["plan"], "school-tampere", ("milk", "nuts"), ("gluten",))
            for qln in q_lines:
                _assert_product_structured_free(str(qln["product_id"]), ("milk", "nuts"))
        elif cid == "hospital-meilahti":
            assert {"gluten"} <= set(payload["excluded_allergens"])
            assert not ({"milk", "nuts"} & set(payload["excluded_allergens"]))
            for ing in payload["ingredients"]:
                _assert_plan_dict_isolation(ing["plan"], "hospital-meilahti", ("gluten",), ("milk", "nuts"))
            for qln in q_lines:
                _assert_product_structured_free(str(qln["product_id"]), ("gluten",))


def test_concurrent_post_v1_quotes_verify_lines_and_orders_20_threads(
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    app = create_app(with_mcp=False)
    shared_client = TestClient(app)
    prices = synthetic_catalog_bundle["prices"]
    n_workers = 20
    barrier = threading.Barrier(n_workers)

    quote_cases: list[dict[str, Any]] = [
        {
            "cid": "hotel-aurora",
            "pid": "463038",
            "qty": 2,
            "query": "milk",
            "unit": "L",
            "expect_tier": "preferred",
            "expect_order_status": "placed",
        },
        {
            "cid": "school-tampere",
            "pid": "470889",
            "qty": 10,
            "query": "oat drink",
            "unit": "L",
            "expect_tier": "preferred",
            "expect_order_status": "placed",
        },
        {
            "cid": "school-tampere",
            "pid": "463038",
            "qty": 1,
            "query": "oat drink",
            "unit": "L",
            "expect_tier": "rejected",
            "expect_flag": "contains:milk",
            "expect_order_status": "rejected",
        },
        {
            "cid": "school-tampere",
            "pid": "conc-oat-nut",
            "qty": 2,
            "query": "oat drink",
            "unit": "L",
            "expect_tier": "rejected",
            "expect_flag": "contains:nuts",
            "expect_order_status": "rejected",
        },
        {
            "cid": "school-tampere",
            "pid": "conc-oat-unk",
            "qty": 2,
            "query": "oat drink",
            "unit": "L",
            "expect_tier": "rejected",
            "expect_flag": "allergen_unknown",
            "expect_order_status": "rejected",
        },
        {
            "cid": "school-tampere",
            "pid": "conc-oat-inf",
            "qty": 2,
            "query": "oat drink",
            "unit": "L",
            "expect_tier": "rejected",
            "expect_flag": "allergen_unknown",
            "expect_order_status": "rejected",
        },
        {
            "cid": "hospital-meilahti",
            "pid": "473328",
            "qty": 4,
            "query": "gluten-free bread",
            "unit": "KG",
            "expect_tier": "preferred",
            "expect_order_status": "placed",
        },
        {
            "cid": "hospital-meilahti",
            "pid": "conc-bread-wheat",
            "qty": 2,
            "query": "bread",
            "unit": "KG",
            "expect_tier": "rejected",
            "expect_flag": "contains:gluten",
            "expect_order_status": "rejected",
        },
        {
            "cid": "hospital-meilahti",
            "pid": "conc-bread-may",
            "qty": 2,
            "query": "bread",
            "unit": "KG",
            "expect_tier": "rejected",
            "expect_flag": "may_contain:gluten",
            "expect_order_status": "rejected",
        },
        {
            "cid": "hospital-meilahti",
            "pid": "conc-bread-unk",
            "qty": 2,
            "query": "bread",
            "unit": "KG",
            "expect_tier": "rejected",
            "expect_flag": "allergen_unknown",
            "expect_order_status": "rejected",
        },
    ]
    tasks = [quote_cases[i % len(quote_cases)] for i in range(40)]

    def worker(idx: int) -> dict[str, Any]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        spec = tasks[idx]
        cid = spec["cid"]
        pid = spec["pid"]
        qty = spec["qty"]
        need = Need(
            customer_id=cid,
            product_query=spec["query"],
            excluded_allergens=[],
            quantity=float(qty),
            unit=spec["unit"],
            delivery_by=DELIVERY,
        )
        prod = get_product(pid)
        assert prod is not None
        direct_v = verify_lines(
            cid,
            DELIVERY,
            {pid: float(qty)},
            _offer_map([pid], prices),
            need,
            {pid: prod},
            service.screen_supplier_text,
        )

        headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
        r_q = shared_client.post(
            "/v1/quotes",
            headers=headers,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": pid, "qty_sales_units": qty, "tier": "preferred", "flags": []}],
                "need": need.model_dump(mode="json"),
            },
        )
        assert r_q.status_code == 200, r_q.text
        q_json = r_q.json()
        q_model = Quote.model_validate(q_json)
        sig_check = verify_quote_detailed(q_model)
        assert sig_check["ok"], sig_check

        r_o = shared_client.post(
            "/v1/orders",
            headers=headers,
            json={"quote_id": q_json["quote_id"], "quote_hash": q_json["quote_hash"]},
        )
        assert r_o.status_code == 200, r_o.text
        return {
            "spec": spec,
            "direct_v": direct_v[pid],
            "quote": q_json,
            "order": r_o.json(),
        }

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=45.0) for f in futures]

    assert len(results) == 40
    for item in results:
        spec = item["spec"]
        dv = item["direct_v"]
        q = item["quote"]
        o = item["order"]
        assert q["customer_id"] == spec["cid"]
        assert o["customer_id"] == spec["cid"]
        assert dv["tier"] == spec["expect_tier"]
        assert q["lines"][0]["tier"] == spec["expect_tier"]
        if "expect_flag" in spec:
            assert spec["expect_flag"] in dv["flags"]
            assert spec["expect_flag"] in q["lines"][0]["flags"]
        assert o["status"] == spec["expect_order_status"]

    chain = service.audit_verify()
    assert chain["ok"] is True, chain
    assert chain["checked"] >= 80


def test_concurrent_mcp_plan_need_and_create_quote_24_threads_and_streamable_http() -> None:
    mcp_srv = extras.register_mcp_tools(build_mcp())
    tools = {t.name: t for t in mcp_srv._tool_manager.list_tools()}
    assert {"plan_need", "create_quote"} <= set(tools)

    n_workers = 24
    barrier = threading.Barrier(n_workers)
    mcp_cases: list[tuple[str, str, float, str, str, str]] = [
        ("hotel-aurora", "milk", 20.0, "L", "463038", "preferred"),
        ("school-tampere", "milk", 20.0, "L", "463038", "rejected"),
        ("school-tampere", "oat drink", 10.0, "L", "470889", "preferred"),
        ("school-tampere", "oat drink", 10.0, "L", "conc-oat-unk", "rejected"),
        ("hospital-meilahti", "bread", 7.0, "KG", "473328", "preferred"),
        ("hospital-meilahti", "rye bread", 7.0, "KG", "conc-bread-rye", "rejected"),
    ]
    tasks = [mcp_cases[i % len(mcp_cases)] for i in range(48)]

    def thread_worker(idx: int) -> tuple[tuple[str, str, float, str, str, str], dict[str, Any], dict[str, Any]]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        spec = tasks[idx]
        cid, q, qty, u, q_pid, _exp_tier = spec
        ctx = HeaderContext({"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"})
        need_arg = {
            "customer_id": cid,
            "product_query": q,
            "excluded_allergens": [],
            "quantity": qty,
            "unit": u,
            "delivery_by": DELIVERY.isoformat(),
        }
        plan_res = tools["plan_need"].fn(need=need_arg, ctx=ctx)
        quote_res = tools["create_quote"].fn(
            delivery_date=DELIVERY.isoformat(),
            lines=[{"product_id": q_pid, "qty_sales_units": 2, "tier": "preferred"}],
            need=need_arg,
            ctx=ctx,
        )
        return spec, plan_res, quote_res

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(thread_worker, i) for i in range(len(tasks))]
        thread_results = [f.result(timeout=45.0) for f in futures]

    assert len(thread_results) == 48
    for (cid, q, _qty, _u, q_pid, exp_tier), plan_res, quote_res in thread_results:
        _assert_customer_query_outcome(cid, q, plan_res)
        assert quote_res["customer_id"] == cid
        assert quote_res["lines"][0]["product_id"] == q_pid
        assert quote_res["lines"][0]["tier"] == exp_tier
        assert verify_quote_detailed(Quote.model_validate(quote_res))["ok"]

    def _extract_mcp_result(res: Any) -> dict[str, Any]:
        is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
        assert not is_err, res
        sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if isinstance(sc, dict):
            return sc["result"] if set(sc) == {"result"} and isinstance(sc["result"], dict) else sc
        txt = "\n".join(getattr(c, "text", "") or "" for c in (res.content or []))
        return json.loads(txt)

    async def _run_24_streamable_sessions() -> list[tuple[tuple[str, str, float, str, str, str], dict[str, Any], dict[str, Any]]]:
        app = create_app(with_mcp=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)

            async def one_session(idx: int) -> tuple[tuple[str, str, float, str, str, str], dict[str, Any], dict[str, Any]]:
                spec = mcp_cases[idx % len(mcp_cases)]
                cid, q, qty, u, q_pid, _exp_tier = spec
                headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
                need_arg = {
                    "customer_id": cid,
                    "product_query": q,
                    "excluded_allergens": [],
                    "quantity": qty,
                    "unit": u,
                    "delivery_by": DELIVERY.isoformat(),
                }
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers=headers,
                ) as http:
                    async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                        p_raw = await client.call_tool("plan_need", {"need": need_arg})
                        q_raw = await client.call_tool(
                            "create_quote",
                            {
                                "delivery_date": DELIVERY.isoformat(),
                                "lines": [{"product_id": q_pid, "qty_sales_units": 2, "tier": "preferred"}],
                                "need": need_arg,
                            },
                        )
                        return spec, _extract_mcp_result(p_raw), _extract_mcp_result(q_raw)

            return list(await asyncio.gather(*(one_session(i) for i in range(24))))

    http_mcp_results = asyncio.run(_run_24_streamable_sessions())
    assert len(http_mcp_results) == 24
    for (cid, q, _qty, _u, q_pid, exp_tier), plan_res, quote_res in http_mcp_results:
        _assert_customer_query_outcome(cid, q, plan_res)
        assert quote_res["customer_id"] == cid
        assert quote_res["lines"][0]["product_id"] == q_pid
        assert quote_res["lines"][0]["tier"] == exp_tier


def test_unified_all_six_surfaces_interleaved_24_threads_zero_policy_bleed(
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    app = create_app(with_mcp=False)
    shared_http = TestClient(app)
    mcp_srv = extras.register_mcp_tools(build_mcp())
    mcp_tools = {t.name: t for t in mcp_srv._tool_manager.list_tools()}
    prices = synthetic_catalog_bundle["prices"]

    n_workers = 24
    barrier = threading.Barrier(n_workers)
    customers = ("hotel-aurora", "school-tampere", "hospital-meilahti")
    surfaces = (
        "catalog_plan_need",
        "service_plan_deterministic",
        "http_plan",
        "http_recipes_plan",
        "http_quotes_and_verify",
        "mcp_plan_and_quote",
    )
    combos = [(c, s) for s in surfaces for c in customers]
    tasks = [combos[i % len(combos)] for i in range(54)]

    def worker(idx: int) -> tuple[str, str, Any]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, surface = tasks[idx]
        headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
        query = "milk" if cid in ("hotel-aurora", "school-tampere") else "bread"
        unit = "L" if query == "milk" else "KG"
        qty = 20.0 if unit == "L" else 7.0
        need = Need(
            customer_id=cid,
            product_query=query,
            excluded_allergens=[],
            quantity=qty,
            unit=unit,
            delivery_by=DELIVERY,
        )
        if surface == "catalog_plan_need":
            return cid, surface, catalog_plan_need(need).model_dump(mode="json")
        if surface == "service_plan_deterministic":
            return cid, surface, service._plan_deterministic(need).model_dump(mode="json")
        if surface == "http_plan":
            r = shared_http.post("/v1/plan", headers=headers, json=need.model_dump(mode="json"))
            assert r.status_code == 200, r.text
            return cid, surface, r.json()
        if surface == "http_recipes_plan":
            r = shared_http.post(
                "/v1/recipes/plan",
                headers=headers,
                json={
                    "recipe_id": "pancakes",
                    "portions": 30,
                    "delivery_by": DELIVERY.isoformat(),
                    "customer_id": cid,
                    "excluded_allergens": [],
                    "include_candidates": True,
                },
            )
            assert r.status_code == 200, r.text
            return cid, surface, r.json()
        if surface == "http_quotes_and_verify":
            adv_pid = {"hotel-aurora": "463038", "school-tampere": "463038", "hospital-meilahti": "conc-bread-wheat"}[cid]
            prod = get_product(adv_pid)
            assert prod is not None
            dv = verify_lines(
                cid,
                DELIVERY,
                {adv_pid: 2.0},
                _offer_map([adv_pid], prices),
                need,
                {adv_pid: prod},
                service.screen_supplier_text,
            )
            rq = shared_http.post(
                "/v1/quotes",
                headers=headers,
                json={
                    "delivery_date": DELIVERY.isoformat(),
                    "lines": [{"product_id": adv_pid, "qty_sales_units": 2, "tier": "preferred"}],
                    "need": need.model_dump(mode="json"),
                },
            )
            assert rq.status_code == 200, rq.text
            return cid, surface, {"dv": dv[adv_pid], "quote": rq.json()}
        ctx = HeaderContext(headers)
        adv_pid = {"hotel-aurora": "463038", "school-tampere": "conc-oat-nut", "hospital-meilahti": "conc-bread-inf"}[cid]
        p_out = mcp_tools["plan_need"].fn(need=need.model_dump(mode="json"), ctx=ctx)
        q_out = mcp_tools["create_quote"].fn(
            delivery_date=DELIVERY.isoformat(),
            lines=[{"product_id": adv_pid, "qty_sales_units": 2, "tier": "preferred"}],
            need=need.model_dump(mode="json"),
            ctx=ctx,
        )
        return cid, surface, {"plan": p_out, "quote": q_out}

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=60.0) for f in futures]

    assert len(results) == 54
    for cid, surface, out in results:
        expected_q = "milk" if cid in ("hotel-aurora", "school-tampere") else "bread"
        if surface in ("catalog_plan_need", "service_plan_deterministic", "http_plan"):
            _assert_customer_query_outcome(cid, expected_q, out)
        elif surface == "http_recipes_plan":
            if cid == "hotel-aurora":
                assert out["excluded_allergens"] == []
            elif cid == "school-tampere":
                assert {"milk", "nuts"} <= set(out["excluded_allergens"])
                for qln in out["quote_request"]["lines"]:
                    _assert_product_structured_free(str(qln["product_id"]), ("milk", "nuts"))
            else:
                assert {"gluten"} <= set(out["excluded_allergens"])
                for qln in out["quote_request"]["lines"]:
                    _assert_product_structured_free(str(qln["product_id"]), ("gluten",))
        elif surface == "http_quotes_and_verify":
            exp_tier = "preferred" if cid == "hotel-aurora" else "rejected"
            assert out["dv"]["tier"] == exp_tier
            assert out["quote"]["lines"][0]["tier"] == exp_tier
        elif surface == "mcp_plan_and_quote":
            _assert_customer_query_outcome(cid, expected_q, out["plan"])
            exp_tier = "preferred" if cid == "hotel-aurora" else "rejected"
            assert out["quote"]["lines"][0]["tier"] == exp_tier

    assert service.audit_verify()["ok"] is True


def _write_custom_policy(
    policy_dir: Path,
    policy_id: str,
    *,
    excluded: list[str],
    preferred_ids: list[str],
    reason: str,
    on_unknown: str = "reject",
    mtime: float = PINNED_MTIME,
) -> Path:
    policy_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "policy_id": policy_id,
        "version": "2.0.0",
        "effective_from": "2026-11-01",
        "customer": {
            "id": policy_id,
            "name": f"Custom Mandate {policy_id}",
            "segment": "institutional",
            "description": f"Dynamic mandate excluding {', '.join(excluded) or 'none'}",
        },
        "approver_roles": ["kitchen_manager"],
        "override_roles": ["catering_director"],
        "approvers": [
            {"id": f"mgr@{policy_id}.example", "name": "Mandate Manager", "role": "kitchen_manager"},
        ],
        "agents": [
            {
                "agent_id": f"{policy_id}/buyer-1",
                "label": "Purchasing agent",
                "scopes": ["catalog:read", "quote:create", "order:submit"],
            }
        ],
        "preferred_products": [{"product_id": pid, "label": f"Usual {pid}"} for pid in preferred_ids],
        "approved_alternatives": [],
        "standing_orders": [],
        "rules": [
            {"id": "mandate_scope", "type": "scope", "required_scopes": ["order:submit"]},
            {"id": "quote_freshness", "type": "quote_freshness"},
            {
                "id": "spend",
                "type": "spend_threshold",
                "auto_max": 500,
                "approval_max": 5000,
                "approver_role": "kitchen_manager",
            },
            {
                "id": "substitution",
                "type": "substitution",
                "require_claims": True,
                "tiers": {
                    "preferred": "allow",
                    "best": "needs_approval",
                    "better": "needs_approval",
                    "good": "needs_approval",
                    "rejected": "reject",
                },
                "outside_preference": "needs_approval",
                "unverified": "needs_approval",
                "new_product": "allow",
                "approver_role": "kitchen_manager",
            },
            {
                "id": "allergens",
                "type": "allergen_block",
                "excluded": list(excluded),
                "reason": reason,
                "on_contains": "reject",
                "on_may_contain": "reject",
                "on_unknown": on_unknown,
                "approver_role": "kitchen_manager",
            },
            {"id": "delivery", "type": "delivery"},
            {"id": "injection", "type": "injection", "approver_role": "kitchen_manager"},
        ],
    }
    p = policy_dir / f"{policy_id}.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


EU14_SOUP_CANDIDATES: tuple[str, ...] = (
    "eu14-soup-allfree",
    "eu14-soup-usual-ps",
    "eu14-soup-usual-esf",
    "eu14-soup-usual-six",
    "eu14-trap-peanuts-con",
    "eu14-trap-peanuts-unk",
    "eu14-trap-sesame-may",
    "eu14-trap-sesame-inf",
    "eu14-trap-eggs-con",
    "eu14-trap-soy-may",
    "eu14-trap-fish-con",
    "eu14-trap-fish-unk",
    "eu14-trap-fish-inf",
    "eu14-trap-crustaceans-con",
    "eu14-trap-molluscs-may",
    "eu14-trap-sulphites-con",
    "eu14-trap-celery-may",
    "eu14-trap-mustard-con",
    "eu14-trap-lupin-unk",
    "eu14-trap-lupin-inf",
    "eu14-trap-contradiction",
)

DYNAMIC_EU14_PROFILES: list[dict[str, Any]] = [
    {
        "tag": "peanuts_sesame",
        "excluded": ("peanuts", "sesame"),
        "usual": "eu14-soup-usual-ps",
        "must_reject": (
            "eu14-soup-usual-six",
            "eu14-trap-peanuts-con",
            "eu14-trap-peanuts-unk",
            "eu14-trap-sesame-may",
            "eu14-trap-sesame-inf",
            "eu14-trap-contradiction",
        ),
        "must_allow": (
            "eu14-soup-allfree",
            "eu14-soup-usual-ps",
            "eu14-soup-usual-esf",
            "eu14-trap-eggs-con",
            "eu14-trap-fish-con",
            "eu14-trap-crustaceans-con",
            "eu14-trap-mustard-con",
        ),
    },
    {
        "tag": "eggs_soy_fish",
        "excluded": ("eggs", "soy", "fish"),
        "usual": "eu14-soup-usual-esf",
        "must_reject": (
            "eu14-soup-usual-ps",
            "eu14-trap-eggs-con",
            "eu14-trap-soy-may",
            "eu14-trap-fish-con",
            "eu14-trap-fish-unk",
            "eu14-trap-fish-inf",
            "eu14-trap-contradiction",
        ),
        "must_allow": (
            "eu14-soup-allfree",
            "eu14-soup-usual-esf",
            "eu14-soup-usual-six",
            "eu14-trap-peanuts-con",
            "eu14-trap-sesame-may",
            "eu14-trap-crustaceans-con",
            "eu14-trap-mustard-con",
        ),
    },
    {
        "tag": "six_group_seafood_spice_lupin",
        "excluded": ("crustaceans", "molluscs", "sulphites", "celery", "mustard", "lupin"),
        "usual": "eu14-soup-usual-six",
        "must_reject": (
            "eu14-soup-usual-esf",
            "eu14-trap-crustaceans-con",
            "eu14-trap-molluscs-may",
            "eu14-trap-sulphites-con",
            "eu14-trap-celery-may",
            "eu14-trap-mustard-con",
            "eu14-trap-lupin-unk",
            "eu14-trap-lupin-inf",
            "eu14-trap-contradiction",
        ),
        "must_allow": (
            "eu14-soup-allfree",
            "eu14-soup-usual-ps",
            "eu14-soup-usual-six",
            "eu14-trap-peanuts-con",
            "eu14-trap-eggs-con",
            "eu14-trap-fish-con",
        ),
    },
]


def test_dynamic_policy_dir_swap_across_eu14_mandates_and_clean_restore(
    tmp_path: Path,
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    orig_dir = trust_policy.POLICY_DIR
    prices = synthetic_catalog_bundle["prices"]
    soup_rows = textsearch.get_rows(list(EU14_SOUP_CANDIDATES))
    soup_offers = _offer_map(list(EU14_SOUP_CANDIDATES), prices)
    soup_products = {pid: p for pid in EU14_SOUP_CANDIDATES if (p := get_product(pid)) is not None}
    soup_rel = {pid: 10.0 for pid in EU14_SOUP_CANDIDATES}

    baseline_ids = trust_policy.policy_ids()
    assert baseline_ids == ["hospital-meilahti", "hotel-aurora", "school-tampere"]
    baseline_school = catalog_plan_need(
        Need(
            customer_id="school-tampere",
            product_query="milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    ).model_dump(mode="json")
    _assert_customer_query_outcome("school-tampere", "milk", baseline_school)

    dirs: list[tuple[Path, dict[str, Any]]] = []
    for prof in DYNAMIC_EU14_PROFILES:
        d = tmp_path / f"policies_{prof['tag']}"
        _write_custom_policy(
            d,
            "school-tampere",
            excluded=list(prof["excluded"]),
            preferred_ids=[prof["usual"]],
            reason=f"strict {prof['tag']} exclusion",
            mtime=PINNED_MTIME,
        )
        _write_custom_policy(
            d,
            "custom-kitchen",
            excluded=list(prof["excluded"]),
            preferred_ids=[prof["usual"]],
            reason=f"custom mandate {prof['tag']}",
            mtime=PINNED_MTIME,
        )
        dirs.append((d, prof))

    app = create_app(with_mcp=False)
    http_client = TestClient(app)
    mcp_srv = extras.register_mcp_tools(build_mcp())
    mcp_tools = {t.name: t for t in mcp_srv._tool_manager.list_tools()}

    traversal = [dirs[0], dirs[1], dirs[2], dirs[1], dirs[0], dirs[2]]
    try:
        for active_dir, prof in traversal:
            trust_policy.POLICY_DIR = active_dir
            excl = tuple(prof["excluded"])
            usual_pid = prof["usual"]

            for cid in ("school-tampere", "custom-kitchen"):
                need = Need(
                    customer_id=cid,
                    product_query="kitchen soup base",
                    excluded_allergens=[],
                    quantity=6.0,
                    unit="L",
                    delivery_by=DELIVERY,
                )

                plan_cat = catalog_plan_need(need).model_dump(mode="json")
                assert tuple(plan_cat["need"]["excluded_allergens"]) == excl
                _assert_plan_dict_isolation(plan_cat, cid, excl)
                assert [ln["product_id"] for ln in plan_cat["lines"]] == [usual_pid]

                plan_det = service._plan_deterministic(need).model_dump(mode="json")
                assert tuple(plan_det["need"]["excluded_allergens"]) == excl
                _assert_plan_dict_isolation(plan_det, cid, excl)
                assert [ln["product_id"] for ln in plan_det["lines"]] == [usual_pid]

                ranked = rank_substitutes(need, soup_offers, limit=50, rows=soup_rows, relevance=soup_rel)
                by_id = {c.product.id: c for c in ranked}
                for r_pid in prof["must_reject"]:
                    assert by_id[r_pid].tier == "rejected", (prof["tag"], cid, r_pid, by_id[r_pid].tier)
                for a_pid in prof["must_allow"]:
                    assert by_id[a_pid].tier != "rejected", (prof["tag"], cid, a_pid, by_id[a_pid].reasons)

                verified = verify_lines(
                    cid,
                    DELIVERY,
                    {pid: 1.0 for pid in EU14_SOUP_CANDIDATES},
                    soup_offers,
                    need,
                    soup_products,
                    service.screen_supplier_text,
                )
                assert verified[usual_pid]["tier"] == "preferred"
                for r_pid in prof["must_reject"]:
                    assert verified[r_pid]["tier"] == "rejected", (prof["tag"], cid, r_pid, verified[r_pid])
                for a_pid in prof["must_allow"]:
                    assert verified[a_pid]["tier"] != "rejected", (prof["tag"], cid, a_pid, verified[a_pid])
                assert "allergen_data_conflict" in verified["eu14-trap-contradiction"]["flags"]

                headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
                r_http = http_client.post("/v1/plan", headers=headers, json=need.model_dump(mode="json"))
                assert r_http.status_code == 200, r_http.text
                _assert_plan_dict_isolation(r_http.json(), cid, excl)

                ctx = HeaderContext(headers)
                mcp_p = mcp_tools["plan_need"].fn(need=need.model_dump(mode="json"), ctx=ctx)
                _assert_plan_dict_isolation(mcp_p, cid, excl)
                bad_pid = prof["must_reject"][0]
                mcp_q = mcp_tools["create_quote"].fn(
                    delivery_date=DELIVERY.isoformat(),
                    lines=[
                        {"product_id": usual_pid, "qty_sales_units": 1, "tier": "preferred"},
                        {"product_id": bad_pid, "qty_sales_units": 1, "tier": "preferred"},
                    ],
                    need=need.model_dump(mode="json"),
                    ctx=ctx,
                )
                tiers = {ln["product_id"]: ln["tier"] for ln in mcp_q["lines"]}
                assert tiers[usual_pid] == "preferred"
                assert tiers[bad_pid] == "rejected"
    finally:
        trust_policy.POLICY_DIR = orig_dir

    restored_school = catalog_plan_need(
        Need(
            customer_id="school-tampere",
            product_query="milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    ).model_dump(mode="json")
    _assert_customer_query_outcome("school-tampere", "milk", restored_school)
    assert [ln["product_id"] for ln in restored_school["lines"]] == [ln["product_id"] for ln in baseline_school["lines"]]

    restored_det = service._plan_deterministic(
        Need(
            customer_id="hospital-meilahti",
            product_query="bread",
            excluded_allergens=[],
            quantity=7.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    ).model_dump(mode="json")
    _assert_customer_query_outcome("hospital-meilahti", "bread", restored_det)

    restored_aurora = catalog_plan_need(
        Need(
            customer_id="hotel-aurora",
            product_query="milk",
            excluded_allergens=[],
            quantity=20.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    ).model_dump(mode="json")
    _assert_customer_query_outcome("hotel-aurora", "milk", restored_aurora)
    assert trust_policy.policy_ids() == ["hospital-meilahti", "hotel-aurora", "school-tampere"]


@pytest.mark.parametrize(
    "entrypoint",
    ["plan_need", "rank_substitutes", "verify_lines", "plan_deterministic"],
)
def test_cold_first_caller_invalidates_stale_policy_cache_even_with_identical_mtime(
    entrypoint: str,
    tmp_path: Path,
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    orig_dir = trust_policy.POLICY_DIR
    prices = synthetic_catalog_bundle["prices"]
    soup_rows = textsearch.get_rows(list(EU14_SOUP_CANDIDATES))
    soup_offers = _offer_map(list(EU14_SOUP_CANDIDATES), prices)
    soup_products = {pid: p for pid in EU14_SOUP_CANDIDATES if (p := get_product(pid)) is not None}
    soup_rel = {pid: 10.0 for pid in EU14_SOUP_CANDIDATES}

    policy_dirs: list[tuple[Path, dict[str, Any]]] = []
    for prof in DYNAMIC_EU14_PROFILES:
        d = tmp_path / f"cold_{entrypoint}_{prof['tag']}"
        _write_custom_policy(
            d,
            "mandate-alpha",
            excluded=list(prof["excluded"]),
            preferred_ids=[prof["usual"]],
            reason=f"cold swap {prof['tag']}",
            mtime=PINNED_MTIME,
        )
        policy_dirs.append((d, prof))

    def call_single_entrypoint(prof: dict[str, Any]) -> None:
        excl = tuple(prof["excluded"])
        need = Need(
            customer_id="mandate-alpha",
            product_query="kitchen soup base",
            excluded_allergens=[],
            quantity=5.0,
            unit="L",
            delivery_by=DELIVERY,
        )
        if entrypoint == "plan_need":
            res = catalog_plan_need(need).model_dump(mode="json")
            assert tuple(res["need"]["excluded_allergens"]) == excl
            assert [ln["product_id"] for ln in res["lines"]] == [prof["usual"]]
            _assert_plan_dict_isolation(res, "mandate-alpha", excl)
        elif entrypoint == "rank_substitutes":
            cands = rank_substitutes(need, soup_offers, limit=50, rows=soup_rows, relevance=soup_rel)
            cmap = {c.product.id: c for c in cands}
            for r_pid in prof["must_reject"]:
                assert cmap[r_pid].tier == "rejected", (entrypoint, prof["tag"], r_pid)
            for a_pid in prof["must_allow"]:
                assert cmap[a_pid].tier != "rejected", (entrypoint, prof["tag"], a_pid)
        elif entrypoint == "verify_lines":
            ver = verify_lines(
                "mandate-alpha",
                DELIVERY,
                {pid: 1.0 for pid in EU14_SOUP_CANDIDATES},
                soup_offers,
                need,
                soup_products,
                service.screen_supplier_text,
            )
            assert ver[prof["usual"]]["tier"] == "preferred"
            for r_pid in prof["must_reject"]:
                assert ver[r_pid]["tier"] == "rejected", (entrypoint, prof["tag"], r_pid)
            for a_pid in prof["must_allow"]:
                assert ver[a_pid]["tier"] != "rejected", (entrypoint, prof["tag"], a_pid)
        elif entrypoint == "plan_deterministic":
            res = service._plan_deterministic(need).model_dump(mode="json")
            assert tuple(res["need"]["excluded_allergens"]) == excl
            assert [ln["product_id"] for ln in res["lines"]] == [prof["usual"]]
            _assert_plan_dict_isolation(res, "mandate-alpha", excl)

    try:
        for d, prof in [*policy_dirs, *reversed(policy_dirs)]:
            trust_policy.POLICY_DIR = d
            call_single_entrypoint(prof)
    finally:
        trust_policy.POLICY_DIR = orig_dir

    school_ver = verify_lines(
        "school-tampere",
        DELIVERY,
        {"470889": 2.0, "463038": 1.0, "conc-oat-nut": 1.0},
        _offer_map(["470889", "463038", "conc-oat-nut"], prices),
        None,
        {pid: p for pid in ("470889", "463038", "conc-oat-nut") if (p := get_product(pid)) is not None},
        service.screen_supplier_text,
    )
    assert school_ver["470889"]["tier"] == "preferred"
    assert school_ver["463038"]["tier"] == "rejected"
    assert school_ver["conc-oat-nut"]["tier"] == "rejected"


def test_dynamic_policy_dir_swap_under_18_concurrent_threads_and_restore(
    tmp_path: Path,
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    orig_dir = trust_policy.POLICY_DIR
    prices = synthetic_catalog_bundle["prices"]
    soup_rows = textsearch.get_rows(list(EU14_SOUP_CANDIDATES))
    soup_offers = _offer_map(list(EU14_SOUP_CANDIDATES), prices)
    soup_products = {pid: p for pid in EU14_SOUP_CANDIDATES if (p := get_product(pid)) is not None}
    soup_rel = {pid: 10.0 for pid in EU14_SOUP_CANDIDATES}

    multi_dir = tmp_path / "policies_multi_eu14"
    for idx, prof in enumerate(DYNAMIC_EU14_PROFILES):
        _write_custom_policy(
            multi_dir,
            f"tenant-{idx}",
            excluded=list(prof["excluded"]),
            preferred_ids=[prof["usual"]],
            reason=f"tenant {idx} {prof['tag']}",
            mtime=PINNED_MTIME,
        )

    n_workers = 18
    try:
        trust_policy.POLICY_DIR = multi_dir
        barrier = threading.Barrier(n_workers)

        def burst_worker(i: int) -> tuple[int, str]:
            if i < n_workers:
                barrier.wait(timeout=10.0)
            t_idx = i % len(DYNAMIC_EU14_PROFILES)
            prof = DYNAMIC_EU14_PROFILES[t_idx]
            cid = f"tenant-{t_idx}"
            excl = tuple(prof["excluded"])
            need = Need(
                customer_id=cid,
                product_query="kitchen soup base",
                excluded_allergens=[],
                quantity=4.0,
                unit="L",
                delivery_by=DELIVERY,
            )
            mode = ("plan_need", "rank_substitutes", "verify_lines", "plan_deterministic")[i % 4]
            if mode == "plan_need":
                p = catalog_plan_need(need).model_dump(mode="json")
                assert tuple(p["need"]["excluded_allergens"]) == excl
                assert [ln["product_id"] for ln in p["lines"]] == [prof["usual"]]
                _assert_plan_dict_isolation(p, cid, excl)
            elif mode == "rank_substitutes":
                cands = rank_substitutes(need, soup_offers, limit=50, rows=soup_rows, relevance=soup_rel)
                cmap = {c.product.id: c for c in cands}
                for r_pid in prof["must_reject"]:
                    assert cmap[r_pid].tier == "rejected"
                for a_pid in prof["must_allow"]:
                    assert cmap[a_pid].tier != "rejected"
            elif mode == "verify_lines":
                ver = verify_lines(
                    cid,
                    DELIVERY,
                    {pid: 1.0 for pid in EU14_SOUP_CANDIDATES},
                    soup_offers,
                    need,
                    soup_products,
                    service.screen_supplier_text,
                )
                assert ver[prof["usual"]]["tier"] == "preferred"
                for r_pid in prof["must_reject"]:
                    assert ver[r_pid]["tier"] == "rejected"
                for a_pid in prof["must_allow"]:
                    assert ver[a_pid]["tier"] != "rejected"
            else:
                p = service._plan_deterministic(need).model_dump(mode="json")
                assert tuple(p["need"]["excluded_allergens"]) == excl
                assert [ln["product_id"] for ln in p["lines"]] == [prof["usual"]]
                _assert_plan_dict_isolation(p, cid, excl)
            return t_idx, mode

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(burst_worker, i) for i in range(72)]
            done = [f.result(timeout=30.0) for f in futs]
        assert len(done) == 72
    finally:
        trust_policy.POLICY_DIR = orig_dir

    barrier_restore = threading.Barrier(n_workers)

    def restore_worker(i: int) -> str:
        if i < n_workers:
            barrier_restore.wait(timeout=10.0)
        cid, q, qty, u = CROSS_CUSTOMER_WORKLOAD[i % len(CROSS_CUSTOMER_WORKLOAD)]
        p = catalog_plan_need(
            Need(
                customer_id=cid,
                product_query=q,
                excluded_allergens=[],
                quantity=qty,
                unit=u,
                delivery_by=DELIVERY,
            )
        ).model_dump(mode="json")
        _assert_customer_query_outcome(cid, q, p)
        return cid

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(restore_worker, i) for i in range(36)]
        restored = [f.result(timeout=30.0) for f in futs]
    assert set(restored) == {"hotel-aurora", "school-tampere", "hospital-meilahti"}


@pytest.mark.parametrize(
    ("query", "qty", "unit"),
    [
        ("milk", 20.0, "L"),
        ("whole milk", 15.0, "L"),
        ("oat drink", 12.0, "L"),
        ("butter", 5.0, "KG"),
        ("cream", 8.0, "L"),
        ("almonds", 4.0, "KG"),
        ("bread", 7.0, "KG"),
        ("flour", 8.0, "KG"),
        ("wheat flour", 10.0, "KG"),
    ],
)
def test_parametrized_16_thread_cross_customer_query_isolation(
    query: str,
    qty: float,
    unit: float | str,
) -> None:
    n_workers = 16
    barrier = threading.Barrier(n_workers)
    customers = ("hotel-aurora", "school-tampere", "hospital-meilahti")

    def worker(i: int) -> tuple[str, dict[str, Any]]:
        barrier.wait(timeout=10.0)
        cid = customers[i % 3]
        need = Need(
            customer_id=cid,
            product_query=query,
            excluded_allergens=[],
            quantity=qty,
            unit=str(unit),
            delivery_by=DELIVERY,
        )
        fn = catalog_plan_need if i % 2 == 0 else service._plan_deterministic
        return cid, fn(need).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(worker, i) for i in range(n_workers)]
        out = [f.result(timeout=30.0) for f in futs]

    for cid, plan_dict in out:
        if cid == "hotel-aurora":
            _assert_plan_dict_isolation(plan_dict, "hotel-aurora", (), ("milk", "nuts", "gluten"))
        elif cid == "school-tampere":
            _assert_plan_dict_isolation(plan_dict, "school-tampere", ("milk", "nuts"), ("gluten",))
        else:
            _assert_plan_dict_isolation(plan_dict, "hospital-meilahti", ("gluten",), ("milk", "nuts"))
        _assert_customer_query_outcome(cid, query, plan_dict)


SINGLE_EU14_TRAP_BY_GROUP: dict[str, str] = {
    "gluten": "eu14-trap-gluten-con",
    "crustaceans": "eu14-trap-crustaceans-con",
    "eggs": "eu14-trap-eggs-con",
    "fish": "eu14-trap-fish-con",
    "peanuts": "eu14-trap-peanuts-con",
    "soy": "eu14-trap-soy-may",
    "milk": "eu14-trap-milk-con",
    "nuts": "eu14-trap-nuts-may",
    "celery": "eu14-trap-celery-may",
    "mustard": "eu14-trap-mustard-con",
    "sesame": "eu14-trap-sesame-may",
    "sulphites": "eu14-trap-sulphites-con",
    "lupin": "eu14-trap-lupin-unk",
    "molluscs": "eu14-trap-molluscs-may",
}


@pytest.mark.parametrize("allergen_group", list(EU14))
def test_all_14_eu14_individual_custom_policy_dir_mandate_isolation(
    allergen_group: str,
    tmp_path: Path,
    synthetic_catalog_bundle: dict[str, Any],
) -> None:
    orig_dir = trust_policy.POLICY_DIR
    prices = synthetic_catalog_bundle["prices"]
    trap_pid = SINGLE_EU14_TRAP_BY_GROUP[allergen_group]
    pair_ids = ["eu14-soup-allfree", trap_pid]
    rows = textsearch.get_rows(pair_ids)
    offers = _offer_map(pair_ids, prices)
    prods = {pid: p for pid in pair_ids if (p := get_product(pid)) is not None}

    custom_dir = tmp_path / f"eu14_{allergen_group}"
    _write_custom_policy(
        custom_dir,
        "mandate-eu14",
        excluded=[allergen_group],
        preferred_ids=["eu14-soup-allfree"],
        reason=f"exclude {allergen_group}",
        mtime=PINNED_MTIME,
    )

    need = Need(
        customer_id="mandate-eu14",
        product_query="kitchen soup base",
        excluded_allergens=[],
        quantity=3.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    try:
        trust_policy.POLICY_DIR = custom_dir
        prof = trust_policy.planning_profile("mandate-eu14")
        assert prof["excluded_allergens"] == [allergen_group]

        p_cat = catalog_plan_need(need).model_dump(mode="json")
        assert p_cat["need"]["excluded_allergens"] == [allergen_group]
        assert [ln["product_id"] for ln in p_cat["lines"]] == ["eu14-soup-allfree"]
        _assert_plan_dict_isolation(p_cat, "mandate-eu14", (allergen_group,))

        p_det = service._plan_deterministic(need).model_dump(mode="json")
        assert p_det["need"]["excluded_allergens"] == [allergen_group]
        assert [ln["product_id"] for ln in p_det["lines"]] == ["eu14-soup-allfree"]
        _assert_plan_dict_isolation(p_det, "mandate-eu14", (allergen_group,))

        ranked = rank_substitutes(need, offers, limit=10, rows=rows, relevance={"eu14-soup-allfree": 10.0, trap_pid: 20.0})
        rmap = {c.product.id: c.tier for c in ranked}
        assert rmap["eu14-soup-allfree"] != "rejected"
        assert rmap[trap_pid] == "rejected"

        ver = verify_lines(
            "mandate-eu14",
            DELIVERY,
            {"eu14-soup-allfree": 1.0, trap_pid: 1.0},
            offers,
            need,
            prods,
            service.screen_supplier_text,
        )
        assert ver["eu14-soup-allfree"]["tier"] == "preferred"
        assert ver[trap_pid]["tier"] == "rejected"
    finally:
        trust_policy.POLICY_DIR = orig_dir

    assert trust_policy.planning_profile("mandate-eu14") == {}
    assert trust_policy.planning_profile("school-tampere")["excluded_allergens"] == ["milk", "nuts"]


def test_concurrent_caller_excluded_allergens_union_never_bleeds_16_threads() -> None:
    n_workers = 16
    barrier = threading.Barrier(n_workers)
    cases: list[tuple[str, list[str], tuple[str, ...], tuple[str, ...]]] = [
        ("hotel-aurora", [], (), ("peanuts", "sesame", "fish", "milk", "nuts", "gluten")),
        ("hotel-aurora", ["peanuts"], ("peanuts",), ("sesame", "fish", "milk", "nuts", "gluten")),
        ("school-tampere", [], ("milk", "nuts"), ("peanuts", "sesame", "fish", "gluten")),
        ("school-tampere", ["sesame"], ("sesame", "milk", "nuts"), ("peanuts", "fish", "gluten")),
        ("hospital-meilahti", [], ("gluten",), ("peanuts", "sesame", "fish", "milk", "nuts")),
        ("hospital-meilahti", ["fish"], ("fish", "gluten"), ("peanuts", "sesame", "milk", "nuts")),
    ]

    def worker(i: int) -> tuple[tuple[str, list[str], tuple[str, ...], tuple[str, ...]], dict[str, Any]]:
        if i < n_workers:
            barrier.wait(timeout=10.0)
        spec = cases[i % len(cases)]
        cid, extra_excl, _exp, _forbid = spec
        need = Need(
            customer_id=cid,
            product_query="kitchen soup base",
            excluded_allergens=list(extra_excl),
            quantity=4.0,
            unit="L",
            delivery_by=DELIVERY,
        )
        fn = catalog_plan_need if i % 2 == 0 else service._plan_deterministic
        return spec, fn(need).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(worker, i) for i in range(32)]
        results = [f.result(timeout=30.0) for f in futs]

    for (cid, _extra, exp_excl, forbid_bleed), plan_dict in results:
        _assert_plan_dict_isolation(plan_dict, cid, exp_excl, forbid_bleed)


def test_dynamic_policy_dir_on_unknown_reject_vs_needs_approval_isolation_16_threads(
    tmp_path: Path,
) -> None:
    orig_dir = trust_policy.POLICY_DIR
    custom_dir = tmp_path / "policies_on_unknown_modes"
    _write_custom_policy(
        custom_dir,
        "strict-school",
        excluded=["lupin"],
        preferred_ids=["eu14-soup-allfree"],
        reason="strict lupin allergy",
        on_unknown="reject",
        mtime=PINNED_MTIME,
    )
    _write_custom_policy(
        custom_dir,
        "review-ward",
        excluded=["lupin"],
        preferred_ids=["eu14-soup-allfree"],
        reason="clinical lupin check",
        on_unknown="needs_approval",
        mtime=PINNED_MTIME,
    )

    n_workers = 16
    barrier = threading.Barrier(n_workers)
    try:
        trust_policy.POLICY_DIR = custom_dir
        app = create_app(with_mcp=False)
        client = TestClient(app)

        def worker(i: int) -> tuple[str, str, dict[str, Any]]:
            barrier.wait(timeout=10.0)
            cid = "strict-school" if i % 2 == 0 else "review-ward"
            pid = "eu14-trap-lupin-unk" if i % 4 < 2 else "eu14-soup-allfree"
            headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
            rq = client.post(
                "/v1/quotes",
                headers=headers,
                json={
                    "delivery_date": DELIVERY.isoformat(),
                    "lines": [{"product_id": pid, "qty_sales_units": 1}],
                },
            )
            assert rq.status_code == 200, rq.text
            q = rq.json()
            ro = client.post(
                "/v1/orders",
                headers=headers,
                json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
            )
            assert ro.status_code == 200, ro.text
            return cid, pid, ro.json()

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(worker, i) for i in range(n_workers)]
            outcomes = [f.result(timeout=30.0) for f in futs]

        for cid, pid, order_obj in outcomes:
            hits_by_rule = {h["rule_id"]: h["outcome"] for h in order_obj["decision"]["hits"]}
            if pid == "eu14-soup-allfree":
                assert order_obj["status"] == "placed"
                assert hits_by_rule["allergens"] == "allow"
            else:
                assert order_obj["status"] == "rejected"
                expected_allergen_hit = "reject" if cid == "strict-school" else "needs_approval"
                assert hits_by_rule[f"allergens:{pid}"] == expected_allergen_hit
    finally:
        trust_policy.POLICY_DIR = orig_dir

    assert trust_policy.policy_ids() == ["hospital-meilahti", "hotel-aurora", "school-tampere"]


def test_concurrent_scripted_buyer_agent_inprocess_and_fake_gateway_18_threads() -> None:
    now_sat = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
    n_workers = 18
    barrier = threading.Barrier(n_workers)
    buyer_cases: list[tuple[str, str, str]] = [
        ("hotel-aurora", "inproc", "We need 20 litres of lactose-free milk by Monday."),
        ("school-tampere", "inproc", "We need 20 litres of oat drink by Monday."),
        ("school-tampere", "inproc", "We need 20 litres of milk by Monday."),
        ("hospital-meilahti", "inproc", "We need 7 kg of gluten-free bread by Monday."),
        ("hotel-aurora", "fake", "We need 20 litres of lactose-free milk by Monday."),
        ("school-tampere", "fake", "We need 20 litres of oat drink by Monday."),
        ("school-tampere", "fake", "We need 20 litres of milk by Monday."),
        ("hospital-meilahti", "fake", "We need 7 kg of gluten-free bread by Monday."),
    ]
    tasks = [buyer_cases[i % len(buyer_cases)] for i in range(24)]

    def worker(idx: int) -> tuple[str, str, str, dict[str, Any]]:
        if idx < n_workers:
            barrier.wait(timeout=10.0)
        cid, mode, prompt = tasks[idx]
        if mode == "inproc":
            transport = InProcessTransport("buyer-1", cid, mandate=mandate_for(cid))
        else:
            transport = FakeTransport(FakeGateway(customer_id=cid, agent_id=f"{cid}/buyer-1", now=now_sat))
        try:
            agent = BuyerAgent(
                transport,
                cid,
                poll_s=0.01,
                approval_timeout_s=0.05,
                clarify=False,
                now=now_sat,
            )
            res = agent.run(prompt)
        finally:
            transport.close()
        return cid, mode, prompt, res

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(len(tasks))]
        results = [f.result(timeout=45.0) for f in futures]

    assert len(results) == 24
    for cid, mode, prompt, res in results:
        assert res["mock"] is True
        plan = res["plan"]
        assert plan and plan["lines"], (cid, mode, prompt, res["status"])
        line_ids = [str(ln["product_id"]) for ln in plan["lines"]]
        need_excl = set(res["need"]["excluded_allergens"])
        if cid == "hotel-aurora":
            assert need_excl == set()
            assert set(line_ids) <= {"463038", "464306"}
            assert res["status"] in ("placed", "pending_approval")
        elif cid == "school-tampere":
            assert {"milk", "nuts"} <= need_excl
            assert "gluten" not in need_excl
            if mode == "inproc":
                _assert_plan_dict_isolation(plan, "school-tampere", ("milk", "nuts"), ("gluten",))
                assert line_ids == ["470889"]
            else:
                assert not (set(line_ids) & {"463038", "464306", "469363", "462775"})
            assert res["status"] in ("placed", "pending_approval")
            allergen_hits = [
                h for h in res["order"]["decision"]["hits"] if str(h["rule_id"]).startswith("allergens")
            ]
            if mode == "inproc":
                assert allergen_hits
            assert all(h["outcome"] == "allow" for h in allergen_hits)
            if "milk" in prompt and mode == "inproc":
                assert res["status"] == "pending_approval"
        elif cid == "hospital-meilahti":
            assert {"gluten"} <= need_excl
            assert not ({"milk", "nuts"} & need_excl)
            assert line_ids == ["473328"]
            assert res["status"] in ("placed", "pending_approval")
            allergen_hits = [
                h for h in res["order"]["decision"]["hits"] if str(h["rule_id"]).startswith("allergens")
            ]
            if mode == "inproc":
                assert allergen_hits
            assert all(h["outcome"] == "allow" for h in allergen_hits)

    assert service.audit_verify()["ok"] is True


def test_file_conventions_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


