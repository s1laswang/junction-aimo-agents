from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import embeddings, gemini_allergens, get_product, rank_substitutes as catalog_rank_substitutes, textsearch
from aimo.catalog.allergens import GS1_GROUPS
from aimo.catalog.plan import fill, plan_need
from aimo.catalog.substitution import rank_substitutes
from aimo.gateway import human_auth
from aimo.models import Candidate, EU14, Need, Offer, ProductSummary
from aimo.recipes import plan_recipe, recipe_needs
from aimo.recipes.planner import DISHES, policy_exclusions
from aimo.trust import policy as trust_policy

DELIVERY = date(2026, 11, 20)
HKI = timezone(timedelta(hours=2))

MATRIX_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    *[(g, g, (g,)) for g in EU14],
    ("alias_dairy", "dairy", ("milk",)),
    ("alias_tree_nuts_space", "tree nuts", ("nuts",)),
    ("alias_tree_nuts_under", "tree_nuts", ("nuts",)),
    ("alias_shellfish", "shellfish", ("crustaceans", "molluscs")),
    ("alias_wheat", "wheat", ("gluten",)),
    ("alias_vehna", "vehnä", ("gluten",)),
    ("alias_maapahkina", "maapähkinä", ("peanuts",)),
    ("alias_mjolk", "mjölk", ("milk",)),
    ("alias_soija", "soija", ("soy",)),
]

SCHOOL_DAIRY_DISHES = [
    "pancakes",
    "lasagne",
    "salmon-soup",
    "macaroni-casserole",
    "oat-porridge",
    "meatballs-and-mashed-potatoes",
    "creamy-chicken-pasta",
    "rice-porridge",
    "carbonara",
]

HOSPITAL_GLUTEN_DISHES = [
    "pancakes",
    "lasagne",
    "macaroni-casserole",
    "oat-porridge",
    "creamy-chicken-pasta",
    "carbonara",
]


def _make_raw_product(
    pid: str,
    name: str,
    category: str,
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
    classifications: list[dict[str, Any]] = [{"name": "allergen", "values": allergen_vals}]
    if claims:
        classifications.append({"name": "nutritionalClaim", "values": [{"id": c} for c in claims]})
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": "Synthetic Test Wholesaler",
        "allowedLotSize": 1,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"language": "en", "value": name}, {"language": "fi", "value": name}],
            "brand": brand,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "nutritionalContent": [{"id": "ENERC", "value": 200.0, "basisQuantity": 100.0}],
            "marketingTexts": [{"language": "en", "value": f"{name} for professional kitchens."}],
        },
    }


def _catalog_spec() -> tuple[list[dict[str, Any]], dict[str, float], dict[str, dict[str, str]], dict[str, tuple[dict[str, str], dict[str, str]]]]:
    products: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    gemini_hints: dict[str, dict[str, str]] = {}
    inferred_db_patches: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    def add(
        pid: str,
        name: str,
        category: str,
        price: float,
        *,
        brand: str = "Valio",
        claims: tuple[str, ...] = (),
        allergen_states: dict[str, str] | None = None,
        ingredients: str = "water, salt",
    ) -> None:
        products.append(
            _make_raw_product(
                pid,
                name,
                category,
                brand=brand,
                claims=claims,
                allergen_states=allergen_states,
                ingredients=ingredients,
            )
        )
        prices[pid] = price

    for case_id, _raw_term, groups in MATRIX_CASES:
        qtoken = f"matrixbase{case_id.replace('_', '')}"
        first_g = groups[0]
        last_g = groups[-1]

        pid_contains = f"mx-{case_id}-contains"
        pid_may = f"mx-{case_id}-may"
        pid_unknown = f"mx-{case_id}-unknown"
        pid_hint = f"mx-{case_id}-hint"
        pid_inferred = f"mx-{case_id}-inferred"
        pid_free = f"mx-{case_id}-free"

        add(pid_contains, f"Alpha {qtoken} stock 1 kg", "91000", 1.00, allergen_states={first_g: "contains"})
        add(pid_may, f"Beta {qtoken} stock 1 kg", "91000", 1.10, allergen_states={last_g: "may_contain"})
        add(pid_unknown, f"Gamma {qtoken} stock 1 kg", "91000", 1.20, allergen_states={first_g: "unknown"})
        add(pid_hint, f"Delta {qtoken} stock 1 kg", "91000", 1.25, allergen_states={last_g: "unknown"})
        add(pid_inferred, f"Epsilon {qtoken} stock 1 kg", "91000", 1.30, allergen_states={})
        add(pid_free, f"Omega {qtoken} stock 1 kg", "91000", 2.50, allergen_states={})

        gemini_hints[pid_hint] = {g: "not_found" for g in EU14}
        inf_allergens = {g: "free" for g in EU14}
        inf_sources = {g: ("inferred:gemini" if g in groups else "structured") for g in EU14}
        inferred_db_patches[pid_inferred] = (inf_allergens, inf_sources)

    add(
        "463038",
        "Valio Eila semi skimmed milk drink 10 L lactose free",
        "10400",
        14.00,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "464306",
        "Valio Eila lactose-free semi skimmed milk drink 1 L",
        "10400",
        1.55,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "rec-milk-reg",
        "Valio whole milk 1 L",
        "10400",
        1.20,
        allergen_states={"milk": "contains"},
        ingredients="milk",
    )
    add(
        "rec-milk-unk",
        "Unverified farm milk 1 L",
        "10400",
        1.00,
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="raw liquid",
    )

    add(
        "rec-oatdrink-nut",
        "Hazelnut oat drink 1 L",
        "10402",
        1.05,
        brand="Oddlygood",
        allergen_states={"nuts": "contains"},
        ingredients="water, pure oats, hazelnut",
    )
    add(
        "rec-oatdrink-maymilk",
        "Shared-line oat drink 1 L",
        "10402",
        1.10,
        brand="Oddlygood",
        allergen_states={"milk": "may_contain"},
        ingredients="water, pure oats, rapeseed oil",
    )
    add(
        "rec-oatdrink-unk",
        "Unverified oat drink 1 L",
        "10402",
        1.15,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown", "gluten": "unknown"},
        ingredients="water, oats",
    )
    add(
        "470889",
        "Oatly oat drink whole 1 L",
        "10402",
        1.95,
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )

    add(
        "rec-cream-reg",
        "Valio cooking cream 15% 1 L",
        "10401",
        2.80,
        allergen_states={"milk": "contains"},
        ingredients="cream, stabilizer",
    )
    add(
        "rec-cream-lf",
        "Valio Eila lactose-free cream 15% 1 L",
        "10401",
        3.10,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, lactase",
    )
    add(
        "rec-oatcook-nut",
        "Cashew oat cooking 1 L",
        "10402",
        1.40,
        brand="Generic",
        allergen_states={"nuts": "may_contain"},
        ingredients="water, pure oats, rapeseed oil",
    )
    add(
        "rec-oatcook-unk",
        "Unverified oat cooking 1 L",
        "10402",
        1.30,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="water, pure oats",
    )
    add(
        "rec-oatcook-safe",
        "Oddlygood oat cooking 1 L",
        "10402",
        2.40,
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )

    add(
        "rec-butter-reg",
        "Valio butter 500 g",
        "10500",
        3.20,
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "rec-butter-lf",
        "Valio Eila lactose-free butter 500 g",
        "10500",
        3.50,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "rec-marg-may",
        "Blended lactose free margarine 1 kg",
        "1051",
        1.80,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "may_contain"},
        ingredients="rapeseed oil, water, salt",
    )
    add(
        "rec-marg-unk",
        "Unverified lactose free margarine 1 kg",
        "1051",
        1.70,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="rapeseed oil, water",
    )
    add(
        "rec-marg-safe",
        "Sunnuntai lactose free margarine 1 kg",
        "1051",
        2.90,
        brand="Sunnuntai",
        claims=("FREE_FROM_LACTOSE", "VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="rapeseed oil, water, salt",
    )

    add(
        "rec-cheese-reg",
        "Valio grated cheese 1 kg",
        "10603",
        6.50,
        allergen_states={"milk": "contains"},
        ingredients="milk, salt, starter",
    )
    add(
        "rec-cheese-lf",
        "Valio Oltermanni lactose-free grated cheese 1 kg",
        "10603",
        7.10,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, salt, starter",
    )
    add(
        "rec-cheese-unk",
        "Unverified grated cheese 1 kg",
        "10603",
        5.00,
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="curd, salt",
    )

    add(
        "rec-flour-wheat",
        "Sunnuntai wheat flour 2 kg",
        "2130",
        1.80,
        brand="Sunnuntai",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour",
    )
    add(
        "rec-flour-nuttrap",
        "Millhouse wheat flour 2 kg",
        "2130",
        1.40,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="wheat flour",
    )
    add(
        "rec-gff-may",
        "Cross-line gluten-free flour mix 1 kg",
        "2131",
        1.70,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, potato starch",
    )
    add(
        "rec-gff-unk",
        "Unverified gluten-free flour mix 1 kg",
        "2131",
        1.60,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, corn starch",
    )
    add(
        "rec-gff-safe",
        "Semper gluten-free flour mix 1 kg",
        "2131",
        3.10,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="rice flour, potato starch, corn starch",
    )

    add(
        "rec-pasta-reg",
        "Myllyn Paras penne pasta 2 kg",
        "21380",
        2.40,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "rec-macaroni-reg",
        "Myllyn Paras macaroni 2 kg",
        "21380",
        2.20,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "rec-lasagne-reg",
        "Myllyn Paras lasagne 1 kg",
        "21380",
        2.60,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "rec-lasagne-nuttrap",
        "Artisan lasagne 1 kg",
        "21380",
        1.90,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="durum wheat",
    )
    add(
        "rec-gfp-may",
        "Shared-mill gluten-free pasta 1 kg",
        "21380",
        1.90,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="corn flour, rice flour",
    )
    add(
        "rec-gfp-unk",
        "Unverified gluten-free pasta 1 kg",
        "21380",
        1.80,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="corn flour, rice flour",
    )
    add(
        "rec-gfp-safe",
        "Semper gluten-free pasta 1 kg",
        "21380",
        3.40,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="corn flour, rice flour",
    )

    add(
        "rec-oatflakes-reg",
        "Elovena oat flakes 1 kg",
        "21370",
        1.90,
        brand="Elovena",
        allergen_states={"gluten": "contains"},
        ingredients="rolled oats",
    )
    add(
        "rec-gfoats-unk",
        "Unverified gluten-free oats 1 kg",
        "2137",
        1.50,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="pure oats",
    )
    add(
        "rec-gfoats-safe",
        "Provenance gluten-free oats 1 kg",
        "2137",
        2.70,
        brand="Provenance",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="pure oats",
    )

    add(
        "473328",
        "Schar Vital dark sliced bread 350 g gluten-free",
        "11000",
        3.60,
        brand="Schar",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="water, rice flour, buckwheat",
    )

    add(
        "rec-salmon-nuttrap",
        "Crust salmon fillet 1 kg",
        "1870",
        6.00,
        brand="Generic",
        allergen_states={"fish": "contains", "nuts": "contains", "milk": "contains"},
        ingredients="salmon, almond, butter",
    )
    add(
        "rec-salmon-safe",
        "Atlantic salmon fillet 1 kg",
        "1870",
        12.50,
        brand="Kalatukku",
        allergen_states={"fish": "contains"},
        ingredients="salmon, salt",
    )
    add(
        "rec-potato-unk",
        "Unverified potato 5 kg",
        "17421",
        2.00,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown", "gluten": "unknown"},
        ingredients="potato",
    )
    add("rec-potato-safe", "Peeled Finnish potato 5 kg", "17421", 4.50, brand="Tuore", ingredients="potato")
    add("rec-carrot-safe", "Peeled carrot 2 kg", "1743", 2.60, brand="Tuore", ingredients="carrot")
    add("rec-leek-safe", "Washed leek 1 kg", "17405", 3.10, brand="Tuore", ingredients="leek")
    add("rec-dill-safe", "Fresh dill 500 g", "22630", 4.20, brand="Tuore", ingredients="dill")
    add("rec-pork-safe", "Finnish pork shoulder 2 kg", "18710", 11.00, brand="Atria", ingredients="pork")
    add("rec-beefcubes-safe", "Finnish beef cubes 2 kg", "1872", 14.50, brand="Atria", ingredients="beef")
    add("rec-onion-safe", "Peeled yellow onion 2 kg", "17405", 2.30, brand="Tuore", ingredients="onion")
    add("rec-groundbeef-safe", "Snellman ground beef 2 kg", "41702", 13.80, brand="Snellman", ingredients="beef")
    add(
        "rec-eggs-safe",
        "Kultamuna pasteurised liquid eggs 1 kg",
        "10590",
        4.10,
        brand="Kultamuna",
        allergen_states={"eggs": "contains"},
        ingredients="pasteurised egg",
    )
    add("rec-sugar-safe", "Dansukker granulated sugar 2 kg", "22100", 2.40, brand="Dansukker", ingredients="sugar")
    add("rec-crushedtom-safe", "Mutti crushed tomatoes 2,5 kg", "21590", 4.90, brand="Mutti", ingredients="tomato, salt")
    add("rec-lettuce-safe", "Washed iceberg lettuce 1 kg", "17403", 3.20, brand="Tuore", ingredients="iceberg lettuce")
    add("rec-cucumber-safe", "Finnish cucumber 1 kg", "17402", 2.90, brand="Tuore", ingredients="cucumber")
    add("rec-tomatoes-safe", "Finnish round tomatoes 1 kg", "17401", 3.40, brand="Tuore", ingredients="tomato")
    add("rec-pepper-safe", "Sweet pepper red 1 kg", "17406", 3.80, brand="Tuore", ingredients="red bell pepper")
    add("rec-oil-safe", "Keiju rapeseed oil 5 L", "10530", 9.50, brand="Keiju", ingredients="rapeseed oil")
    add(
        "rec-meatballs-unsafe",
        "Classic meatballs 2 kg",
        "17300",
        5.50,
        brand="Generic",
        allergen_states={"milk": "contains", "gluten": "contains"},
        ingredients="beef, breadcrumbs, milk, salt",
    )
    add(
        "rec-meatballs-safe",
        "Snellman beef meatballs 2 kg",
        "17300",
        9.20,
        brand="Snellman",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="beef, potato starch, onion, salt",
    )
    add("rec-jam-safe", "Saarioinen lingonberry jam 2 kg", "21450", 6.40, brand="Saarioinen", ingredients="lingonberries, sugar, water")
    add("rec-chicken-safe", "Kariniemen chicken fillet 2 kg", "1875", 13.20, brand="Kariniemen", ingredients="chicken fillet, salt")
    add("rec-rice-safe", "Risella long grain rice 5 kg", "21340", 7.80, brand="Risella", ingredients="rice")
    add("rec-bacon-safe", "HK sliced bacon 1 kg", "17202", 8.90, brand="HK", ingredients="pork, salt")

    return products, prices, gemini_hints, inferred_db_patches


class StaticTestBackendClient:
    base_url = "static://allergen-matrix-recipes"
    ttl_s = 300

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.stats = {"upstream_calls": 0}

    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _wrap(self, items: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        return {
            **meta,
            "products": [{**it, "_fetched_at": now, "_meta": meta} for it in items],
            "_oldest_fetch": now,
        }

    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
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
                    "quantity": 1.0,
                    "catalogPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "priceWithTax": {"value": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "listPrice": {"value": net, "valueWithTax": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "tax": {"taxRate": 13.5, "taxValue": round(gross - net, 2)},
                    "netPriceFound": False,
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

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        close = datetime.combine(requested_date - timedelta(days=1), datetime.min.time(), HKI).replace(hour=14)
        now_dt = datetime.combine(requested_date - timedelta(days=5), datetime.min.time(), HKI).replace(hour=9)
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


def _close_cached_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture(scope="module")
def synthetic_catalog_bundle(tmp_path_factory):
    cat_dir = tmp_path_factory.mktemp("synth_allergen_catalog")
    products, prices, gemini_hints, inferred_db_patches = _catalog_spec()
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
        for pid, (a_map, s_map) in inferred_db_patches.items():
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
        "gemini_hints": gemini_hints,
    }


@pytest.fixture(autouse=True)
def isolated_env(synthetic_catalog_bundle, tmp_path, monkeypatch):
    _close_cached_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()

    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)

    monkeypatch.setattr(config, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(config, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synthetic_catalog_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", synthetic_catalog_bundle["co2_csv"])

    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")

    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *a, **k: [])
    hints = synthetic_catalog_bundle["gemini_hints"]
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: hints.get(str(pid)))

    human_auth.reset_for_tests()
    bc.set_client(StaticTestBackendClient(synthetic_catalog_bundle["prices"]))
    yield
    bc.set_client(None)
    _close_cached_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()


@pytest.fixture
def offers_fn(synthetic_catalog_bundle):
    prices = synthetic_catalog_bundle["prices"]

    def _fn(ids, requested_date, customer_id=None):
        now = datetime.now(timezone.utc)
        return {
            str(pid): Offer(
                product_id=str(pid),
                requested_date=requested_date,
                available=True,
                available_amount=999999.0,
                cutoff_passed=False,
                closing_time=now + timedelta(days=2),
                logistic_model="Stock",
                sales_unit="ST",
                unit_price=float(prices.get(str(pid), 2.50)),
                fetched_at=now,
            )
            for pid in ids
        }

    return _fn


@pytest.fixture(scope="module")
def api_client():
    from aimo.gateway.app import create_app

    with TestClient(create_app(with_mcp=False)) as client:
        yield client


def _write_customer_policy(policy_dir: Path, customer_id: str, excluded_terms: list[str]) -> None:
    policy_yaml = "\n".join(
        [
            f"policy_id: {customer_id}",
            'version: "1.2.0"',
            "customer:",
            f"  id: {customer_id}",
            f"  name: Customer {customer_id}",
            "agents:",
            f"  - agent_id: {customer_id}/buyer-1",
            "    scopes: [catalog:read, quote:create, order:submit]",
            "rules:",
            "  - id: allergens",
            "    type: allergen_block",
            f"    excluded: {json.dumps(excluded_terms, ensure_ascii=False)}",
            "    reason: customer policy hard block",
            "    on_contains: reject",
            "    on_may_contain: reject",
            "    on_unknown: reject",
        ]
    )
    (policy_dir / f"{customer_id}.yaml").write_text(policy_yaml, encoding="utf-8")


def _assert_structured_free(product_id: str, groups: tuple[str, ...]) -> None:
    prod = get_product(product_id)
    assert prod is not None, product_id
    for g in groups:
        status = prod.allergens[g].value if hasattr(prod.allergens[g], "value") else prod.allergens[g]
        source = prod.allergen_source.get(g, "")
        assert status == "free", (product_id, prod.name, g, status, source)
        assert source.startswith("structured"), (product_id, prod.name, g, status, source)


@pytest.mark.parametrize(
    "raw_terms,expected",
    [
        (["dairy", "tree_nuts"], ["milk", "nuts"]),
        (["wheat", "vehnä", "gluteeni"], ["gluten"]),
        (["shellfish", "maapähkinä"], ["crustaceans", "molluscs", "peanuts"]),
        (["mjölk", "nötter", "ägg", "soija", "fisk", "senap"], ["milk", "nuts", "eggs", "soy", "fish", "mustard"]),
    ],
)
def test_recipe_policy_exclusions_normalizes_aliases(tmp_path, monkeypatch, raw_terms, expected):
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()
    _write_customer_policy(pol_dir, "alias-cust", raw_terms)
    monkeypatch.setattr(trust_policy, "POLICY_DIR", pol_dir)
    assert policy_exclusions("alias-cust") == expected


def test_recipe_policy_exclusions_fallback_path(monkeypatch):
    from aimo.catalog import allergens as allergens_mod
    from aimo.gateway import verify as verify_mod

    monkeypatch.setattr(allergens_mod, "effective_excluded_allergens", lambda cid: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(verify_mod, "policy_excluded_allergens", lambda cid: ["dairy", "wheat"])
    assert policy_exclusions("any-cust") == ["dairy", "wheat"]


@pytest.mark.parametrize("case_id,raw_term,expected_groups", MATRIX_CASES, ids=[c[0] for c in MATRIX_CASES])
def test_eu14_and_alias_policy_hardblock_matrix(tmp_path, monkeypatch, offers_fn, case_id, raw_term, expected_groups):
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()
    cid = f"cust-{case_id.replace('_', '-')}"
    _write_customer_policy(pol_dir, cid, [raw_term])
    monkeypatch.setattr(trust_policy, "POLICY_DIR", pol_dir)

    qtoken = f"matrixbase{case_id.replace('_', '')}"
    pid_contains = f"mx-{case_id}-contains"
    pid_may = f"mx-{case_id}-may"
    pid_unknown = f"mx-{case_id}-unknown"
    pid_hint = f"mx-{case_id}-hint"
    pid_inferred = f"mx-{case_id}-inferred"
    pid_free = f"mx-{case_id}-free"
    unsafe_ids = {pid_contains, pid_may, pid_unknown, pid_hint, pid_inferred}
    all_ids = [*unsafe_ids, pid_free]

    need = Need(
        customer_id=cid,
        product_query=qtoken,
        excluded_allergens=[],
        preferred_product_ids=[pid_contains],
        quantity=10.0,
        unit="KG",
        delivery_by=DELIVERY,
    )

    plan = plan_need(need, offers_fn=offers_fn, limit=20)
    assert plan.need.excluded_allergens == list(expected_groups)
    assert [ln.product_id for ln in plan.lines] == [pid_free]
    assert plan.covered_amount == 10.0
    assert plan.shortfall == 0.0
    _assert_structured_free(pid_free, expected_groups)

    by_id = {c.product.id: c for c in plan.candidates}
    for bad_id in unsafe_ids:
        assert bad_id in by_id, (case_id, bad_id)
        assert by_id[bad_id].tier == "rejected", (case_id, bad_id, by_id[bad_id].reasons)
    assert "allergen_unknown" in by_id[pid_unknown].flags
    assert "allergen_unknown" in by_id[pid_hint].flags
    assert "allergen_inferred_hint" in by_id[pid_hint].flags
    assert "allergen_unknown" in by_id[pid_inferred].flags
    assert by_id[pid_free].tier != "rejected"

    offers = offers_fn(all_ids, DELIVERY, customer_id=cid)
    rows = textsearch.get_rows(all_ids)
    rel = {pid: 10.0 for pid in all_ids}
    hints_map = {pid_hint: {g: "not_found" for g in EU14}}

    ranked_direct = rank_substitutes(need, offers, limit=20, rows=rows, relevance=rel, inferred_allergens=hints_map)
    ranked_by_id = {c.product.id: c for c in ranked_direct}
    assert [c.product.id for c in ranked_direct if c.tier != "rejected"] == [pid_free]
    for bad_id in unsafe_ids:
        assert ranked_by_id[bad_id].tier == "rejected"

    ranked_public = catalog_rank_substitutes(need, offers=offers, limit=20)
    assert [c.product.id for c in ranked_public if c.tier != "rejected"] == [pid_free]

    tampered_candidates: list[Candidate] = []
    for idx, pid in enumerate([pid_contains, pid_may, pid_unknown, pid_hint, pid_inferred]):
        prod = get_product(pid)
        assert prod is not None
        tampered_candidates.append(
            Candidate(
                product=ProductSummary(**prod.model_dump(include=set(ProductSummary.model_fields))),
                offer=offers[pid],
                tier="preferred",
                rank_score=0.99 - idx * 0.01,
                reasons=["Tampered candidate"],
                flags=[],
            )
        )
    now_utc = datetime.now(timezone.utc)
    for tag, flag_val in (("contains", f"contains:{raw_term}"), ("may", f"may_contain:{raw_term}"), ("unk", "allergen_unknown")):
        fid = f"forged-{tag}-{case_id}"
        tampered_candidates.append(
            Candidate(
                product=ProductSummary(id=fid, name=f"Forged {qtoken} 1 kg", sales_unit="ST", content_amount=1.0, content_unit="KG"),
                offer=Offer(
                    product_id=fid,
                    requested_date=DELIVERY,
                    available=True,
                    available_amount=999999.0,
                    unit_price=0.50,
                    fetched_at=now_utc,
                ),
                tier="preferred",
                rank_score=0.999,
                reasons=["Forged flag candidate"],
                flags=[flag_val],
            )
        )
    free_prod = get_product(pid_free)
    assert free_prod is not None
    tampered_candidates.append(
        Candidate(
            product=ProductSummary(**free_prod.model_dump(include=set(ProductSummary.model_fields))),
            offer=offers[pid_free],
            tier="good",
            rank_score=0.40,
            reasons=["Verified safe candidate"],
            flags=[],
        )
    )

    filled = fill(need, tampered_candidates)
    assert [ln.product_id for ln in filled.lines] == [pid_free]
    assert filled.covered_amount == 10.0
    assert filled.shortfall == 0.0


@pytest.mark.parametrize("dish_id", SCHOOL_DAIRY_DISHES)
def test_school_tampere_recipe_needs_and_plan_recipe_hardblock(offers_fn, dish_id):
    rn = recipe_needs(dish_id, 60, DELIVERY, "school-tampere", excluded_allergens=[])
    assert {"milk", "nuts"} <= set(rn.excluded_allergens)
    by_ing = {it.ingredient: it for it in rn.items}
    if "milk" in by_ing:
        assert by_ing["milk"].query == "oat drink"
        assert by_ing["milk"].query != "lactose-free milk"
    if "cooking cream" in by_ing:
        assert by_ing["cooking cream"].query == "oat cooking"
        assert "lactose-free" not in by_ing["cooking cream"].query
    if "butter" in by_ing:
        assert by_ing["butter"].query == "lactose free margarine"
        assert by_ing["butter"].query != "lactose-free butter"
    for it in rn.items:
        assert {"milk", "nuts"} <= set(it.need.excluded_allergens)

    res = plan_recipe(dish_id, 60, DELIVERY, "school-tampere", excluded_allergens=[], offers_fn=offers_fn)
    assert {"milk", "nuts"} <= set(res["excluded_allergens"])
    swap_map = {s["ingredient"]: s["to"] for s in res["swaps"]}
    if "milk" in swap_map:
        assert swap_map["milk"] == "oat drink"

    assert res["quote_request"]["lines"], dish_id
    for ing_entry in res["ingredients"]:
        assert ing_entry["query"] != "lactose-free milk"
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))
            assert "allergen_unknown" not in ln.get("flags", [])

    for qline in res["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("milk", "nuts"))

    if dish_id not in ("lasagne", "carbonara"):
        assert res["uncovered"] == []
    else:
        assert [u["ingredient"] for u in res["uncovered"]] == ["grated cheese"]


@pytest.mark.parametrize("dish_id", ["pancakes", "lasagne", "salmon-soup"])
def test_school_tampere_http_recipes_plan_hardblock(api_client, dish_id):
    resp = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "school-tampere/buyer-1"},
        json={
            "recipe_id": dish_id,
            "portions": 50,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer_id"] == "school-tampere"
    assert {"milk", "nuts"} <= set(body["excluded_allergens"])

    by_ing = {i["ingredient"]: i for i in body["ingredients"]}
    assert by_ing["milk"]["query"] == "oat drink"
    assert by_ing["milk"]["plan"]["lines"]

    assert body["quote_request"]["lines"]
    for ing_entry in body["ingredients"]:
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    for qline in body["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("milk", "nuts"))


@pytest.mark.parametrize("dish_id", HOSPITAL_GLUTEN_DISHES)
def test_hospital_meilahti_recipe_needs_and_plan_recipe_hardblock(offers_fn, dish_id):
    rn = recipe_needs(dish_id, 80, DELIVERY, "hospital-meilahti", excluded_allergens=[])
    assert "gluten" in rn.excluded_allergens
    by_ing = {it.ingredient: it for it in rn.items}
    if "wheat flour" in by_ing:
        assert by_ing["wheat flour"].query == "gluten-free flour mix"
    for pasta_ing in ("lasagne", "macaroni", "pasta"):
        if pasta_ing in by_ing:
            assert by_ing[pasta_ing].query == "gluten-free pasta"
    if "oat flakes" in by_ing:
        assert by_ing["oat flakes"].query == "gluten-free oats"
    for it in rn.items:
        assert "gluten" in it.need.excluded_allergens

    res = plan_recipe(dish_id, 80, DELIVERY, "hospital-meilahti", excluded_allergens=[], offers_fn=offers_fn)
    assert "gluten" in res["excluded_allergens"]
    assert res["swaps"], dish_id
    assert res["uncovered"] == [], (dish_id, res["uncovered"])
    assert res["quote_request"]["lines"], dish_id

    for ing_entry in res["ingredients"]:
        assert ing_entry["plan"]["lines"], (dish_id, ing_entry["ingredient"])
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("gluten",))
            assert "allergen_unknown" not in ln.get("flags", [])

    for qline in res["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("gluten",))


@pytest.mark.parametrize("dish_id", ["pancakes", "lasagne", "macaroni-casserole", "oat-porridge"])
def test_hospital_meilahti_http_recipes_plan_hardblock(api_client, dish_id):
    resp = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "hospital-meilahti/buyer-1"},
        json={
            "recipe_id": dish_id,
            "portions": 40,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer_id"] == "hospital-meilahti"
    assert "gluten" in body["excluded_allergens"]
    assert body["swaps"]
    assert body["uncovered"] == []
    assert body["quote_request"]["lines"]

    for ing_entry in body["ingredients"]:
        assert ing_entry["plan"]["lines"]
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("gluten",))
    for qline in body["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("gluten",))


def test_hotel_aurora_lasagne_120_baseline_plans_cleanly(api_client, offers_fn):
    rn = recipe_needs("lasagne", 120, DELIVERY, "hotel-aurora", excluded_allergens=[])
    assert rn.excluded_allergens == []
    assert rn.portions == {"lasagne": 120}
    assert len(rn.items) == 8

    res = plan_recipe("lasagne", 120, DELIVERY, "hotel-aurora", excluded_allergens=[], offers_fn=offers_fn)
    assert res["excluded_allergens"] == []
    assert len(res["ingredients"]) == 8
    assert res["uncovered"] == []
    assert len(res["quote_request"]["lines"]) == 8
    assert res["total_net_estimate"] > 0

    milk_entry = next(i for i in res["ingredients"] if i["ingredient"] == "milk")
    milk_pid = milk_entry["plan"]["lines"][0]["product_id"]
    milk_prod = get_product(milk_pid)
    assert milk_prod is not None
    assert milk_prod.allergens["milk"].value == "contains"

    http_resp = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "hotel-aurora/buyer-1"},
        json={
            "recipe_id": "lasagne",
            "portions": 120,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
        },
    )
    assert http_resp.status_code == 200, http_resp.text
    http_body = http_resp.json()
    assert http_body["excluded_allergens"] == []
    assert http_body["uncovered"] == []
    assert len(http_body["quote_request"]["lines"]) == 8


def test_all_dishes_respect_school_and_hospital_policies(offers_fn):
    for dish in DISHES:
        did = dish["id"]
        school_res = plan_recipe(did, 30, DELIVERY, "school-tampere", excluded_allergens=[], offers_fn=offers_fn)
        assert {"milk", "nuts"} <= set(school_res["excluded_allergens"])
        for ing_entry in school_res["ingredients"]:
            for ln in ing_entry["plan"]["lines"]:
                _assert_structured_free(ln["product_id"], ("milk", "nuts"))
        for qline in school_res["quote_request"]["lines"]:
            _assert_structured_free(qline["product_id"], ("milk", "nuts"))

        hosp_res = plan_recipe(did, 30, DELIVERY, "hospital-meilahti", excluded_allergens=[], offers_fn=offers_fn)
        assert "gluten" in hosp_res["excluded_allergens"]
        assert hosp_res["uncovered"] == [], (did, hosp_res["uncovered"])
        for ing_entry in hosp_res["ingredients"]:
            for ln in ing_entry["plan"]["lines"]:
                _assert_structured_free(ln["product_id"], ("gluten",))
        for qline in hosp_res["quote_request"]["lines"]:
            _assert_structured_free(qline["product_id"], ("gluten",))


def test_custom_alias_customer_policies_drive_recipe_swaps_and_http_plan(tmp_path, monkeypatch, api_client):
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()
    _write_customer_policy(pol_dir, "daycare-espoo", ["dairy", "tree_nuts"])
    _write_customer_policy(pol_dir, "clinic-turku", ["vehnä"])
    monkeypatch.setattr(trust_policy, "POLICY_DIR", pol_dir)

    r_daycare = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "daycare-espoo", "X-Agent-Id": "daycare-espoo/buyer-1"},
        json={"recipe_id": "pancakes", "portions": 40, "delivery_by": DELIVERY.isoformat(), "excluded_allergens": []},
    )
    assert r_daycare.status_code == 200, r_daycare.text
    b_daycare = r_daycare.json()
    assert b_daycare["excluded_allergens"] == ["milk", "nuts"]
    ing_daycare = {i["ingredient"]: i for i in b_daycare["ingredients"]}
    assert ing_daycare["milk"]["query"] == "oat drink"
    for qline in b_daycare["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("milk", "nuts"))

    r_clinic = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "clinic-turku", "X-Agent-Id": "clinic-turku/buyer-1"},
        json={"recipe_id": "pancakes", "portions": 40, "delivery_by": DELIVERY.isoformat(), "excluded_allergens": []},
    )
    assert r_clinic.status_code == 200, r_clinic.text
    b_clinic = r_clinic.json()
    assert b_clinic["excluded_allergens"] == ["gluten"]
    ing_clinic = {i["ingredient"]: i for i in b_clinic["ingredients"]}
    assert ing_clinic["wheat flour"]["query"] == "gluten-free flour mix"
    for qline in b_clinic["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("gluten",))
