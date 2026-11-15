from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import embeddings, gemini_allergens, get_product, plan_need, textsearch
from aimo.catalog.allergens import GS1_GROUPS
from aimo.gateway import human_auth
from aimo.models import Candidate, EU14, Need, Offer, Plan, PlanLine, ProductSummary
from aimo.recipes import plan_recipe, recipe_needs
from aimo.recipes.planner import _dietary, house_lactose_free
from aimo.trust import policy as trust_policy

DELIVERY = date(2026, 11, 20)
HKI = timezone(timedelta(hours=2))


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


def _catalog_spec() -> tuple[list[dict[str, Any]], dict[str, float], dict[str, tuple[dict[str, str], dict[str, str]]]]:
    products: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
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
        "rec-milk-inferred",
        "Inferred plant drink 1 L",
        "10402",
        1.05,
        brand="Generic",
        allergen_states={},
        ingredients="water, oats",
    )
    inferred_db_patches["rec-milk-inferred"] = (
        {g: "free" for g in EU14},
        {g: ("inferred:gemini" if g == "milk" else "structured") for g in EU14},
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
        "rec-flour-wheat",
        "Sunnuntai wheat flour 2 kg",
        "2130",
        1.80,
        brand="Sunnuntai",
        allergen_states={"gluten": "contains"},
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
        "rec-gff-inferred",
        "Inferred gluten-free flour mix 1 kg",
        "2131",
        1.50,
        brand="Generic",
        allergen_states={},
        ingredients="rice flour",
    )
    inferred_db_patches["rec-gff-inferred"] = (
        {g: "free" for g in EU14},
        {g: ("inferred:gemini" if g == "gluten" else "structured") for g in EU14},
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
        "rec-lasagne-reg",
        "Myllyn Paras lasagne 1 kg",
        "21380",
        2.60,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
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
    add("rec-potato-safe", "Peeled Finnish potato 5 kg", "17421", 4.50, brand="Tuore", ingredients="potato")
    add("rec-carrot-safe", "Peeled carrot 2 kg", "1743", 2.60, brand="Tuore", ingredients="carrot")
    add("rec-leek-safe", "Washed leek 1 kg", "17405", 3.10, brand="Tuore", ingredients="leek")
    add("rec-dill-safe", "Fresh dill 500 g", "22630", 4.20, brand="Tuore", ingredients="dill")
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
    add("rec-chicken-safe", "Kariniemen chicken fillet 2 kg", "1875", 13.20, brand="Kariniemen", ingredients="chicken fillet, salt")

    return products, prices, inferred_db_patches


class StaticRecipeBackendClient:
    base_url = "static://recipe-allergen-normalization"
    ttl_s = 300

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _wrap(self, items: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
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
    cat_dir = tmp_path_factory.mktemp("synth_recipe_norm_catalog")
    products, prices, inferred_db_patches = _catalog_spec()
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
        "product_json": prod_json,
        "catalog_db": cat_db,
        "rel_csv": rel_csv,
        "co2_csv": co2_csv,
        "prices": prices,
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
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: None)

    human_auth.reset_for_tests()
    bc.set_client(StaticRecipeBackendClient(synthetic_catalog_bundle["prices"]))
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


def _assert_structured_free(product_id: str, groups: tuple[str, ...] | list[str]) -> None:
    prod = get_product(product_id)
    assert prod is not None, product_id
    for g in groups:
        status = prod.allergens[g].value if hasattr(prod.allergens[g], "value") else prod.allergens[g]
        source = prod.allergen_source.get(g, "")
        assert status == "free", (product_id, prod.name, g, status, source)
        assert source.startswith("structured"), (product_id, prod.name, g, status, source)


MILK_NUT_INPUTS: list[tuple[str, list[str], list[str]]] = [
    ("alias_dairy_tree_nuts", ["dairy", "tree nuts"], ["milk", "nuts"]),
    ("alias_cows_milk", ["cow's milk"], ["milk"]),
    ("alias_cows_milk_curly", ["cow\u2019s milk"], ["milk"]),
    ("mixed_case_milk_nuts", ["MILK", "NUTS"], ["milk", "nuts"]),
    ("mixed_case_milk", ["Milk"], ["milk"]),
    ("fi_maito_pahkina", ["maito", "pähkinä"], ["milk", "nuts"]),
    ("sv_mjolk_notter", ["mjölk", "nötter"], ["milk", "nuts"]),
    ("gs1_am_an", ["AM", "AN"], ["milk", "nuts"]),
]

GLUTEN_INPUTS: list[tuple[str, list[str]]] = [
    ("alias_wheat", ["wheat"]),
    ("alias_rye", ["rye"]),
    ("mixed_case_gluten", ["Gluten"]),
    ("fi_gluteeni", ["gluteeni"]),
    ("fi_vehna", ["vehnä"]),
    ("gs1_aw", ["AW"]),
]


def test_hotel_aurora_pancakes_dairy_alias_swaps_to_oat_drink_not_lactose_free_milk(offers_fn, api_client):
    assert house_lactose_free("hotel-aurora") is True

    rn_default = recipe_needs("pancakes", 40, DELIVERY, "hotel-aurora")
    assert rn_default.excluded_allergens == []
    default_by_ing = {it.ingredient: it for it in rn_default.items}
    assert default_by_ing["milk"].query == "lactose-free milk"
    assert default_by_ing["butter"].query == "lactose-free butter"

    rn_dairy = recipe_needs("pancakes", 40, DELIVERY, "hotel-aurora", excluded_allergens=["dairy"])
    assert rn_dairy.excluded_allergens == ["milk"]
    dairy_by_ing = {it.ingredient: it for it in rn_dairy.items}
    assert dairy_by_ing["milk"].query == "oat drink"
    assert dairy_by_ing["milk"].query != "lactose-free milk"
    assert dairy_by_ing["butter"].query == "lactose free margarine"
    assert dairy_by_ing["butter"].query != "lactose-free butter"
    for it in rn_dairy.items:
        assert it.need.excluded_allergens == ["milk"]

    res = plan_recipe("pancakes", 40, DELIVERY, "hotel-aurora", excluded_allergens=["dairy"], offers_fn=offers_fn)
    assert res["excluded_allergens"] == ["milk"]
    swap_map = {s["ingredient"]: s["to"] for s in res["swaps"]}
    assert swap_map["milk"] == "oat drink"
    assert swap_map["butter"] == "lactose free margarine"
    assert res["uncovered"] == []
    assert res["quote_request"]["lines"]

    ing_map = {i["ingredient"]: i for i in res["ingredients"]}
    assert ing_map["milk"]["query"] == "oat drink"
    assert ing_map["milk"]["plan"]["lines"]
    for ln in ing_map["milk"]["plan"]["lines"]:
        assert ln["product_id"] not in {
            "463038",
            "464306",
            "rec-milk-reg",
            "rec-milk-unk",
            "rec-milk-inferred",
            "rec-oatdrink-maymilk",
            "rec-oatdrink-unk",
        }
        _assert_structured_free(ln["product_id"], ("milk",))

    for ing_entry in res["ingredients"]:
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk",))
    for qline in res["quote_request"]["lines"]:
        assert qline["product_id"] not in {"463038", "464306", "rec-milk-reg", "rec-butter-lf", "rec-butter-reg"}
        _assert_structured_free(qline["product_id"], ("milk",))

    http_resp = api_client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "hotel-aurora/buyer-1"},
        json={
            "recipe_id": "pancakes",
            "portions": 40,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": ["dairy"],
        },
    )
    assert http_resp.status_code == 200, http_resp.text
    http_body = http_resp.json()
    assert http_body["excluded_allergens"] == ["milk"]
    http_ing = {i["ingredient"]: i for i in http_body["ingredients"]}
    assert http_ing["milk"]["query"] == "oat drink"
    for qline in http_body["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], ("milk",))


@pytest.mark.parametrize("raw_excl", [
    ["dairy", "tree nuts"],
    ["cow's milk"],
    ["MILK", "NUTS"],
    ["maito", "pähkinä"],
    ["AM", "AN"],
])
def test_dietary_defense_in_depth_milk_aliases(raw_excl):
    d = _dietary(raw_excl, "", lactose_free=True, gluten_free=False)
    assert d["milk"] is True
    assert d["lactose"] is True
    assert d["gluten"] is False


@pytest.mark.parametrize("raw_excl", [
    ["wheat"],
    ["rye"],
    ["Gluten"],
    ["gluteeni"],
    ["AW"],
])
def test_dietary_defense_in_depth_gluten_aliases(raw_excl):
    d = _dietary(raw_excl, "", lactose_free=True, gluten_free=False)
    assert d["gluten"] is True
    assert d["milk"] is False


def test_recipe_needs_fallback_when_normalize_allergens_fails(monkeypatch):
    from aimo.catalog import allergens as allergens_mod

    monkeypatch.setattr(
        allergens_mod,
        "normalize_allergens",
        lambda items: (_ for _ in ()).throw(RuntimeError("normalizer unavailable")),
    )
    rn = recipe_needs("pancakes", 40, DELIVERY, "hotel-aurora", excluded_allergens=["dairy", "wheat"])
    assert rn.excluded_allergens == ["milk", "gluten"]
    by_ing = {it.ingredient: it for it in rn.items}
    assert by_ing["milk"].query == "oat drink"
    assert by_ing["wheat flour"].query == "gluten-free flour mix"


@pytest.mark.parametrize("case_id,raw_excl,expected_excl", MILK_NUT_INPUTS, ids=[c[0] for c in MILK_NUT_INPUTS])
def test_hotel_aurora_milk_and_nut_normalization_forms(offers_fn, case_id, raw_excl, expected_excl):
    for dish_id in ("pancakes", "salmon-soup"):
        rn = recipe_needs(dish_id, 50, DELIVERY, "hotel-aurora", excluded_allergens=raw_excl)
        assert rn.excluded_allergens == expected_excl, case_id
        by_ing = {it.ingredient: it for it in rn.items}
        assert by_ing["milk"].query == "oat drink"
        assert by_ing["butter"].query == "lactose free margarine"
        if "cooking cream" in by_ing:
            assert by_ing["cooking cream"].query == "oat cooking"
        for it in rn.items:
            assert it.need.excluded_allergens == expected_excl

        res = plan_recipe(dish_id, 50, DELIVERY, "hotel-aurora", excluded_allergens=raw_excl, offers_fn=offers_fn)
        assert res["excluded_allergens"] == expected_excl
        assert res["uncovered"] == []
        assert res["quote_request"]["lines"]
        for ing_entry in res["ingredients"]:
            for ln in ing_entry["plan"]["lines"]:
                _assert_structured_free(ln["product_id"], expected_excl)
        for qline in res["quote_request"]["lines"]:
            _assert_structured_free(qline["product_id"], expected_excl)


@pytest.mark.parametrize("case_id,raw_excl", GLUTEN_INPUTS, ids=[c[0] for c in GLUTEN_INPUTS])
def test_hotel_aurora_gluten_normalization_forms(offers_fn, case_id, raw_excl):
    for dish_id in ("pancakes", "lasagne", "oat-porridge"):
        rn = recipe_needs(dish_id, 50, DELIVERY, "hotel-aurora", excluded_allergens=raw_excl)
        assert rn.excluded_allergens == ["gluten"], case_id
        by_ing = {it.ingredient: it for it in rn.items}
        if "wheat flour" in by_ing:
            assert by_ing["wheat flour"].query == "gluten-free flour mix"
        if "lasagne" in by_ing:
            assert by_ing["lasagne"].query == "gluten-free pasta"
        if "oat flakes" in by_ing:
            assert by_ing["oat flakes"].query == "gluten-free oats"
        for it in rn.items:
            assert it.need.excluded_allergens == ["gluten"]

        res = plan_recipe(dish_id, 50, DELIVERY, "hotel-aurora", excluded_allergens=raw_excl, offers_fn=offers_fn)
        assert res["excluded_allergens"] == ["gluten"]
        assert res["uncovered"] == []
        assert res["quote_request"]["lines"]
        for ing_entry in res["ingredients"]:
            for ln in ing_entry["plan"]["lines"]:
                _assert_structured_free(ln["product_id"], ("gluten",))
        for qline in res["quote_request"]["lines"]:
            _assert_structured_free(qline["product_id"], ("gluten",))


@pytest.mark.parametrize(
    "case_id,raw_excl,expected_excl",
    [
        ("omitted_none", None, ["milk", "nuts"]),
        ("alias_dairy_tree_nuts", ["dairy", "tree nuts"], ["milk", "nuts"]),
        ("alias_cows_milk", ["cow's milk"], ["milk", "nuts"]),
        ("alias_wheat", ["wheat"], ["milk", "nuts", "gluten"]),
        ("alias_rye", ["rye"], ["milk", "nuts", "gluten"]),
        ("mixed_case_milk_nuts", ["MILK", "NUTS"], ["milk", "nuts"]),
        ("mixed_case_gluten", ["Gluten"], ["milk", "nuts", "gluten"]),
        ("fi_maito_pahkina", ["maito", "pähkinä"], ["milk", "nuts"]),
        ("fi_gluteeni", ["gluteeni"], ["milk", "nuts", "gluten"]),
        ("gs1_am_an", ["AM", "AN"], ["milk", "nuts"]),
        ("gs1_aw", ["AW"], ["milk", "nuts", "gluten"]),
    ],
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_school_tampere_omitted_and_all_allergen_forms(offers_fn, case_id, raw_excl, expected_excl):
    kwargs: dict[str, Any] = {} if raw_excl is None else {"excluded_allergens": raw_excl}
    rn = recipe_needs("pancakes", 60, DELIVERY, "school-tampere", **kwargs)
    assert rn.excluded_allergens == expected_excl, case_id
    by_ing = {it.ingredient: it for it in rn.items}
    assert by_ing["milk"].query == "oat drink"
    assert by_ing["butter"].query == "lactose free margarine"
    if "gluten" in expected_excl:
        assert by_ing["wheat flour"].query == "gluten-free flour mix"
    for it in rn.items:
        assert it.need.excluded_allergens == expected_excl

    res = plan_recipe("pancakes", 60, DELIVERY, "school-tampere", offers_fn=offers_fn, **kwargs)
    assert res["excluded_allergens"] == expected_excl
    assert res["uncovered"] == []
    assert res["quote_request"]["lines"]
    for ing_entry in res["ingredients"]:
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], expected_excl)
    for qline in res["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], expected_excl)


@pytest.mark.parametrize(
    "case_id,raw_excl,expected_excl",
    [
        ("omitted_none", None, ["gluten"]),
        ("alias_wheat", ["wheat"], ["gluten"]),
        ("alias_rye", ["rye"], ["gluten"]),
        ("mixed_case_gluten", ["Gluten"], ["gluten"]),
        ("fi_gluteeni", ["gluteeni"], ["gluten"]),
        ("gs1_aw", ["AW"], ["gluten"]),
        ("alias_dairy_tree_nuts", ["dairy", "tree nuts"], ["gluten", "milk", "nuts"]),
        ("alias_cows_milk", ["cow's milk"], ["gluten", "milk"]),
        ("mixed_case_milk_nuts", ["MILK", "NUTS"], ["gluten", "milk", "nuts"]),
        ("fi_maito_pahkina", ["maito", "pähkinä"], ["gluten", "milk", "nuts"]),
        ("gs1_am_an", ["AM", "AN"], ["gluten", "milk", "nuts"]),
    ],
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_hospital_meilahti_omitted_and_all_allergen_forms(offers_fn, case_id, raw_excl, expected_excl):
    kwargs: dict[str, Any] = {} if raw_excl is None else {"excluded_allergens": raw_excl}
    rn = recipe_needs("pancakes", 60, DELIVERY, "hospital-meilahti", **kwargs)
    assert rn.excluded_allergens == expected_excl, case_id
    by_ing = {it.ingredient: it for it in rn.items}
    assert by_ing["wheat flour"].query == "gluten-free flour mix"
    if "milk" in expected_excl:
        assert by_ing["milk"].query == "oat drink"
        assert by_ing["butter"].query == "lactose free margarine"
    for it in rn.items:
        assert it.need.excluded_allergens == expected_excl

    res = plan_recipe("pancakes", 60, DELIVERY, "hospital-meilahti", offers_fn=offers_fn, **kwargs)
    assert res["excluded_allergens"] == expected_excl
    assert res["uncovered"] == []
    assert res["quote_request"]["lines"]
    for ing_entry in res["ingredients"]:
        for ln in ing_entry["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], expected_excl)
    for qline in res["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], expected_excl)


@pytest.mark.parametrize(
    "rogue_pid,customer_id,extra_excl",
    [
        ("464306", "school-tampere", None),
        ("rec-oatdrink-nut", "school-tampere", None),
        ("rec-oatdrink-maymilk", "school-tampere", None),
        ("rec-oatdrink-unk", "school-tampere", None),
        ("rec-milk-inferred", "school-tampere", None),
        ("464306", "hotel-aurora", ["dairy"]),
        ("rec-milk-inferred", "hotel-aurora", ["AM"]),
    ],
)
def test_plan_recipe_defense_in_depth_filters_rogue_catalog_unsafe_lines(
    offers_fn,
    rogue_pid,
    customer_id,
    extra_excl,
):
    def rogue_plan_fn(need: Need, offers_fn=None) -> Plan:
        if need.product_query == "oat drink":
            bad_line = PlanLine(
                product_id=rogue_pid,
                product_name=f"Rogue {rogue_pid}",
                tier="preferred",
                qty_sales_units=need.quantity,
                sales_unit="ST",
                covered_amount=need.quantity,
                unit_price=1.50,
                line_total=round(need.quantity * 1.50, 2),
                reasons=["Rogue stub injected forbidden item"],
                flags=[],
            )
            return Plan(
                need=need,
                lines=[bad_line],
                candidates=[],
                covered_amount=need.quantity,
                shortfall=0.0,
                total_net=round(need.quantity * 1.50, 2),
                explanation="Rogue full coverage.",
            )
        return plan_need(need, offers_fn=offers_fn)

    res = plan_recipe(
        "pancakes",
        40,
        DELIVERY,
        customer_id,
        excluded_allergens=extra_excl,
        offers_fn=offers_fn,
        plan_fn=rogue_plan_fn,
    )

    quoted_ids = {ln["product_id"] for ln in res["quote_request"]["lines"]}
    assert rogue_pid not in quoted_ids

    milk_entry = next(i for i in res["ingredients"] if i["ingredient"] == "milk")
    assert milk_entry["plan"]["lines"] == []
    assert milk_entry["plan"]["shortfall"] == pytest.approx(6.0)

    uncovered_by_ing = {u["ingredient"]: u for u in res["uncovered"]}
    assert "milk" in uncovered_by_ing
    assert uncovered_by_ing["milk"]["shortfall"] == pytest.approx(6.0)
    assert uncovered_by_ing["milk"]["unit"] == "L"

    for qline in res["quote_request"]["lines"]:
        _assert_structured_free(qline["product_id"], res["excluded_allergens"])


def test_plan_recipe_defense_in_depth_filters_allergen_unknown_and_partial_rogue_lines(offers_fn):
    def rogue_partial_plan_fn(need: Need, offers_fn=None) -> Plan:
        if need.product_query == "gluten-free flour mix":
            safe_line = PlanLine(
                product_id="rec-gff-safe",
                product_name="Semper gluten-free flour mix 1 kg",
                tier="best",
                qty_sales_units=1.0,
                sales_unit="ST",
                covered_amount=1.0,
                unit_price=3.10,
                line_total=3.10,
                reasons=["Verified gluten-free flour"],
                flags=[],
            )
            unk_flag_line = PlanLine(
                product_id="rogue-unlisted-unknown",
                product_name="Mystery bulk flour 1 kg",
                tier="good",
                qty_sales_units=1.0,
                sales_unit="ST",
                covered_amount=1.0,
                unit_price=1.00,
                line_total=1.00,
                reasons=["Stub unknown allergen line"],
                flags=["allergen_unknown"],
            )
            contains_flag_line = PlanLine(
                product_id="rogue-unlisted-wheat",
                product_name="Rogue wheat flour 1 kg",
                tier="good",
                qty_sales_units=1.0,
                sales_unit="ST",
                covered_amount=1.0,
                unit_price=1.00,
                line_total=1.00,
                reasons=["Stub contains wheat line"],
                flags=["contains:wheat"],
            )
            cand_unk = Candidate(
                product=ProductSummary(id="rogue-cand-unknown", name="Cand unknown flour", sales_unit="ST", content_amount=1.0, content_unit="KG"),
                tier="good",
                rank_score=0.5,
                reasons=[],
                flags=["allergen_unknown"],
            )
            cand_unk_line = PlanLine(
                product_id="rogue-cand-unknown",
                product_name="Cand unknown flour",
                tier="good",
                qty_sales_units=1.0,
                sales_unit="ST",
                covered_amount=1.0,
                unit_price=1.00,
                line_total=1.00,
                reasons=["Candidate carries allergen_unknown"],
                flags=[],
            )
            return Plan(
                need=need,
                lines=[safe_line, unk_flag_line, contains_flag_line, cand_unk_line],
                candidates=[cand_unk],
                covered_amount=4.0,
                shortfall=0.0,
                total_net=6.10,
                explanation="Rogue mixed plan.",
            )
        return plan_need(need, offers_fn=offers_fn)

    res = plan_recipe(
        "pancakes",
        60,
        DELIVERY,
        "hospital-meilahti",
        excluded_allergens=["gluteeni"],
        offers_fn=offers_fn,
        plan_fn=rogue_partial_plan_fn,
    )
    assert res["excluded_allergens"] == ["gluten"]
    quoted_by_id = {ln["product_id"]: ln["qty_sales_units"] for ln in res["quote_request"]["lines"]}
    assert "rogue-unlisted-unknown" not in quoted_by_id
    assert "rogue-unlisted-wheat" not in quoted_by_id
    assert "rogue-cand-unknown" not in quoted_by_id
    assert quoted_by_id["rec-gff-safe"] == pytest.approx(1.0)

    flour_entry = next(i for i in res["ingredients"] if i["ingredient"] == "wheat flour")
    assert [ln["product_id"] for ln in flour_entry["plan"]["lines"]] == ["rec-gff-safe"]
    assert flour_entry["plan"]["covered_amount"] == pytest.approx(1.0)
    assert flour_entry["plan"]["shortfall"] == pytest.approx(2.0)
    assert flour_entry["plan"]["total_net"] == pytest.approx(3.10)

    uncovered_by_ing = {u["ingredient"]: u for u in res["uncovered"]}
    assert "wheat flour" in uncovered_by_ing
    assert uncovered_by_ing["wheat flour"]["shortfall"] == pytest.approx(2.0)
    assert uncovered_by_ing["wheat flour"]["unit"] == "KG"
