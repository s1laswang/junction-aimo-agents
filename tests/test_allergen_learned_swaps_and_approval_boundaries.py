from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import time
import tokenize
from typing import Any, Iterator

from fastapi.testclient import TestClient
import pytest
import yaml

from aimo import config, gemini
from aimo.agents import drift, seller
from aimo.backend import catalog_store, clock
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import (
    allergens as allergens_mod,
    embeddings,
    gemini_allergens,
    get_product,
    plan as plan_mod,
    plan_need,
    textsearch,
)
from aimo.catalog.substitution import product_types
from aimo.gateway import auth, human_auth, resilience, service, verify as verify_mod
from aimo.gateway.app import create_app
from aimo.models import EU14, Need, Quote, QuoteLine
from aimo.trust import approvals as approvals_mod
from aimo.trust import learned
from aimo.trust import policy as trust_policy
from aimo.trust.audit import AuditLog, audit as global_audit
from aimo.trust.signing import verify_jws, verify_quote_detailed

DELIVERY = date(2026, 11, 17)
NOW_ISO = "2026-11-14T10:00:00+02:00"

H_HOTEL = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
H_HOSPITAL = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}

HOTEL_PM = "anna@hotel-aurora.example"
HOTEL_GM = "mikko@hotel-aurora.example"
SCHOOL_KM = "liisa@school-tampere.example"
SCHOOL_CD = "jari@school-tampere.example"
HOSP_DIETITIAN = "sara@hospital-meilahti.example"
HOSP_PM = "olli@hospital-meilahti.example"
HOSP_HEAD = "helena@hospital-meilahti.example"

APPROVER_PINS: dict[str, str] = {
    HOTEL_PM: "pin-anna-aurora-12ch",
    HOTEL_GM: "pin-mikko-aurora-12c",
    SCHOOL_KM: "pin-liisa-school-12c",
    SCHOOL_CD: "pin-jari-school-12ch",
    HOSP_DIETITIAN: "pin-sara-hosp-12char",
    HOSP_PM: "pin-olli-hosp-12char",
    HOSP_HEAD: "pin-helena-hosp-12ch",
}

REAL_POLICY_DIR = Path(trust_policy.__file__).resolve().parent / "policies"


def _pin_hdr(approver: str) -> dict[str, str]:
    return {"X-Approver-Pin": APPROVER_PINS[approver]}


def _session_hdr(api: TestClient, approver: str) -> dict[str, str]:
    resp = api.post(
        "/v1/auth/approver-session",
        headers=_pin_hdr(approver),
        json={"approver": approver},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approver"] == approver
    return {"X-Approver-Session": body["session"]}


def _synkka_item(
    pid: str,
    name_en: str,
    brand: str,
    category: str,
    unit_kind: str,
    unit_amount: float,
    allergen_cls: list[tuple[str, str]],
    *,
    claims: list[str] | None = None,
    ingredients: str = "water, salt",
    fat: float = 1.5,
) -> dict[str, Any]:
    classifications: list[dict[str, Any]] = []
    con_vals = [
        {"id": code, "unit": status}
        for code, status in allergen_cls
        if status in ("CONTAINS", "MAY_CONTAIN", "UNDECLARED")
    ]
    free_vals = [
        {"id": code, "unit": "FREE_FROM"}
        for code, status in allergen_cls
        if status == "FREE_FROM"
    ]
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
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": brand,
        "allowedLotSize": 0.01,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": [{"language": "en", "value": name_en}, {"language": "fi", "value": name_en}],
            "unitConversions": conv,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "nutritionalContent": [{"id": "ENERC", "value": 180.0, "basisQuantity": 100.0}],
            "marketingTexts": [{"language": "en", "value": f"{name_en} for foodservice kitchens."}],
        },
    }


def _catalog_items() -> list[dict[str, Any]]:
    return [
        _synkka_item(
            "463038",
            "Valio Eila semi skimmed milk drink 10 L lactose free",
            "Valio",
            "10400",
            "L",
            10.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "464306",
            "Valio Eila semi skimmed milk drink 1 L lactose free",
            "Valio",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "469363",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "469364",
            "Maitomaa Semi skimmed milkdrink 1 L lactose free",
            "Maitomaa",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="milk, vitamin D",
            fat=1.5,
        ),
        _synkka_item(
            "460396",
            "Valio skimmed milk 10 l novobox",
            "Valio",
            "10400",
            "L",
            10.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            ingredients="skimmed milk, vitamin D",
            fat=0.1,
        ),
        _synkka_item(
            "463982",
            "Kieku Barn Eggs L180 12300g",
            "Kieku",
            "10590",
            "KG",
            12.3,
            [("AE", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            ingredients="eggs",
            fat=10.0,
        ),
        _synkka_item(
            "460468",
            "Paulig Presidentti Tumma Paahto 18x300g",
            "Paulig",
            "21500",
            "KG",
            5.4,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            ingredients="roasted coffee",
            fat=0.5,
        ),
        _synkka_item(
            "460884",
            "Reuter & Stolt Croissant butter 18 % 80 x 90g",
            "Reuter",
            "11100",
            "KG",
            7.2,
            [("AM", "CONTAINS"), ("AW", "CONTAINS"), ("AN", "FREE_FROM")],
            ingredients="wheat flour, butter, water, yeast",
            fat=18.0,
        ),
        _synkka_item(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats 10%, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "470863",
            "Oatly Oat Drink Semi 1L",
            "Oatly",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats 10%, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "479001",
            "Kaslink Safe Oat Drink 1 L UHT",
            "Kaslink",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, Finnish oats, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "479002",
            "Elovena Safe Oat Drink 1 L UHT",
            "Elovena",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, Finnish oats, rapeseed oil",
            fat=1.5,
        ),
        _synkka_item(
            "461578",
            "Vaasan Rye bread 45x85g",
            "Vaasan",
            "14100",
            "KG",
            3.825,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            ingredients="wholegrain rye, water, salt, yeast",
            fat=1.8,
        ),
        _synkka_item(
            "460573",
            "PP POTATO PRE-WASHED BELLAROSA",
            "PP",
            "17100",
            "KG",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            ingredients="potato",
            fat=0.1,
        ),
        _synkka_item(
            "471410",
            "Dairy Blend Oat Drink 1 L UHT",
            "BlendCo",
            "10400",
            "L",
            1.0,
            [("AM", "CONTAINS"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats, milk powder",
            fat=1.5,
        ),
        _synkka_item(
            "471425",
            "Shared Line Dairy Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "MAY_CONTAIN"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472833",
            "Unlabelled Milk Status Oat Drink 1 L UHT",
            "Fazer",
            "10400",
            "L",
            1.0,
            [("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472899",
            "Inferred Milk Free Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472950",
            "Contradiction Whey Oat Drink 1 L UHT",
            "ConflictBrand",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats, whey powder, milk protein",
            fat=1.5,
        ),
        _synkka_item(
            "471424",
            "Oddlygood Hazelnut Oat Drink 1 L UHT",
            "Oddlygood",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "CONTAINS"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats, hazelnut paste",
            fat=1.5,
        ),
        _synkka_item(
            "471426",
            "Trace Nut Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "MAY_CONTAIN"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472834",
            "Unlabelled Nut Status Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472898",
            "Inferred Nut Free Oat Drink 1 L UHT",
            "Generic",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats",
            fat=1.5,
        ),
        _synkka_item(
            "472951",
            "Contradiction Almond Oat Drink 1 L UHT",
            "ConflictBrand",
            "10400",
            "L",
            1.0,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")],
            claims=["VEGAN"],
            ingredients="water, oats, roasted hazelnut paste, almonds",
            fat=1.5,
        ),
        _synkka_item(
            "473328",
            "Schar Vital dark sliced bread 350 g gluten free",
            "Schar",
            "14100",
            "KG",
            0.35,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, sourdough, maize starch, flaxseed",
            fat=3.0,
        ),
        _synkka_item(
            "473399",
            "Schar Vital dark sliced bread 500 g gluten free",
            "Schar",
            "14100",
            "KG",
            0.50,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, sourdough, maize starch, flaxseed",
            fat=3.0,
        ),
        _synkka_item(
            "461765",
            "Moilas Gluten-free Dark Bread 600g sliced frozen",
            "Moilas",
            "14100",
            "KG",
            0.60,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, rice flour, buckwheat, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "461766",
            "Semper Gluten-free Dark Bread 500g sliced",
            "Semper",
            "14100",
            "KG",
            0.50,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, rice flour, tapioca starch, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "462775",
            "Arla Milkdrink 1L lactose free fat free UHT",
            "Arla",
            "10400",
            "L",
            1.0,
            [("AW", "FREE_FROM"), ("AM", "CONTAINS"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
            ingredients="skimmed milk, vitamin D",
            fat=0.1,
        ),
        _synkka_item(
            "469269",
            "Urtekram Organic Gluten-free Oat Muesli 550g",
            "Urtekram",
            "21300",
            "KG",
            0.55,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN", "ORGANIC"],
            ingredients="gluten-free oat flakes, sunflower seeds",
            fat=5.0,
        ),
        _synkka_item(
            "480200",
            "ClaimOnly Gluten-free Dark Bread 500 g",
            "ClaimBake",
            "14100",
            "KG",
            0.50,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, rice starch, maize flour",
            fat=3.0,
        ),
        _synkka_item(
            "460395",
            "Fresh Dill Bundled 1 kg",
            "Tuore",
            "17100",
            "KG",
            1.0,
            [],
            ingredients="fresh dill",
            fat=0.2,
        ),
        _synkka_item(
            "501100",
            "Vaasan Ruispalat Dark Bread 500 g",
            "Vaasan",
            "14100",
            "KG",
            0.50,
            [("AW", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            ingredients="wholegrain rye, water, wheat gluten, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501103",
            "Mislabelled Dark Sliced Bread 500 g",
            "BadBake",
            "14100",
            "KG",
            0.50,
            [("AW", "CONTAINS"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="water, wheat flour, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501102",
            "Trace Gluten Dark Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.50,
            [("AW", "MAY_CONTAIN"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="maize starch, water, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501101",
            "Artisan Unlabelled Dark Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.50,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            ingredients="rice flour, water, salt, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501199",
            "Inferred Dark Sliced Bread 500 g",
            "Bakery",
            "14100",
            "KG",
            0.50,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_GLUTEN"],
            ingredients="rice flour, water, salt, yeast",
            fat=3.0,
        ),
        _synkka_item(
            "501150",
            "Contradiction Barley Dark Bread 500 g",
            "ConflictBake",
            "14100",
            "KG",
            0.50,
            [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")],
            ingredients="water, wheat flour, barley malt, salt, yeast",
            fat=3.0,
        ),
    ]


class _ConfigurableBackendClient:
    base_url = "fixture://learned-swaps-and-approvals-mock"
    ttl_s = 300

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._units: dict[str, tuple[str, float]] = {}
        for it in items:
            pid = it["id"]
            conv = it["synkkaData"]["unitConversions"][0]
            if "volume" in conv:
                self._units[pid] = ("LTR", float(conv["volume"]["value"]))
            else:
                self._units[pid] = ("KGM", float(conv["netWeight"]["value"]))
        self.prices: dict[str, float] = {pid: 1.80 for pid in self._units}
        self.prices["463038"] = 14.00
        self.prices["460396"] = 11.50
        self.prices["464306"] = 1.50
        self.prices["469363"] = 1.40
        self.prices["469364"] = 1.38
        self.stock: dict[str, float] = {pid: 5000.0 for pid in self._units}
        self.closing_times: dict[str, str] = {pid: "2026-11-16T17:00:00+0200" for pid in self._units}
        self.sales_blocks: set[str] = set()
        self.stats = {"upstream_calls": 0}

    def _time_now_iso(self) -> str:
        return clock.now().astimezone(timezone(timedelta(hours=2))).strftime("%Y-%m-%dT%H:%M:%S%z")

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._time_now_iso()}
        products = []
        for pid in ids:
            if pid not in self._units:
                continue
            cunit, camt = self._units[pid]
            up = self.prices[pid]
            cmp_val = round(up / camt, 4) if camt else up
            products.append(
                {
                    "productId": pid,
                    "salesUnit": "ST",
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "comparisonUnit": cunit,
                    "catalogPrice": {"value": up, "currency": "EUR"},
                    "comparisonPrice": {"value": cmp_val, "unit": cunit},
                    "_fetched_at": now,
                    "_meta": meta,
                }
            )
        return {**meta, "products": products, "_oldest_fetch": now}

    def availability(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": self._time_now_iso()}
        products = []
        for pid in ids:
            if pid not in self._units:
                continue
            sb = pid in self.sales_blocks
            products.append(
                {
                    "productId": pid,
                    "closingTime": self.closing_times[pid],
                    "salesBlock": sb,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": sb,
                            "availableDate": requested_date.isoformat(),
                            "availableAmount": self.stock[pid],
                            "salesUnit": "ST",
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


def _clear_policy_caches() -> None:
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    plan_mod._last_policy_dir = None
    verify_mod._last_trust_policy_dir = None


def _reset_breakers() -> None:
    for br in resilience._breakers.values():
        br.reset()


def _seed_inferred_rows(cdb: Path) -> None:
    _close_cached_catalog_con()
    catalog_store.load_catalog(cdb, force=True)
    con = sqlite3.connect(str(cdb))
    try:
        milk_inf_allergens = {
            g: ("free" if g in ("milk", "nuts") else ("contains" if g == "gluten" else "unknown"))
            for g in EU14
        }
        milk_inf_sources = {
            g: ("inferred:gemini" if g == "milk" else ("structured" if g in ("nuts", "gluten") else "none"))
            for g in EU14
        }
        con.execute(
            "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
            (json.dumps(milk_inf_allergens), json.dumps(milk_inf_sources), "472899"),
        )

        nut_inf_allergens = {
            g: ("free" if g in ("milk", "nuts") else ("contains" if g == "gluten" else "unknown"))
            for g in EU14
        }
        nut_inf_sources = {
            g: ("inferred:gemini" if g == "nuts" else ("structured" if g in ("milk", "gluten") else "none"))
            for g in EU14
        }
        con.execute(
            "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
            (json.dumps(nut_inf_allergens), json.dumps(nut_inf_sources), "472898"),
        )

        gluten_inf_allergens = {
            g: ("free" if g in ("gluten", "milk", "nuts") else "unknown")
            for g in EU14
        }
        gluten_inf_sources = {
            g: ("inferred:gemini" if g == "gluten" else ("structured" if g in ("milk", "nuts") else "none"))
            for g in EU14
        }
        con.execute(
            "UPDATE products SET allergens = ?, allergen_source = ? WHERE id = ?",
            (json.dumps(gluten_inf_allergens), json.dumps(gluten_inf_sources), "501199"),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture(scope="module")
def built_catalog(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, Path, Path, list[dict[str, Any]], TestClient]]:
    cat_dir = tmp_path_factory.mktemp("learned_approval_boundaries_cat")
    items = _catalog_items()
    pjson = cat_dir / "valio_aimo_junction26_product_data.json"
    cdb = cat_dir / "catalog.sqlite"
    key_dir = cat_dir / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    pjson.write_text(json.dumps(items), encoding="utf-8")

    orig_pjson = catalog_store.PRODUCT_JSON
    orig_cdb = catalog_store.CATALOG_DB
    orig_rel = catalog_store.RELIABILITY_CSV
    orig_co2 = catalog_store.CO2_CSV
    catalog_store.PRODUCT_JSON = pjson
    catalog_store.CATALOG_DB = cdb
    catalog_store.RELIABILITY_CSV = cat_dir / "missing_rel.csv"
    catalog_store.CO2_CSV = cat_dir / "missing_co2.csv"
    try:
        _seed_inferred_rows(cdb)
    finally:
        catalog_store.PRODUCT_JSON = orig_pjson
        catalog_store.CATALOG_DB = orig_cdb
        catalog_store.RELIABILITY_CSV = orig_rel
        catalog_store.CO2_CSV = orig_co2
        _close_cached_catalog_con()

    with TestClient(create_app(with_mcp=False)) as client_app:
        yield pjson, cdb, key_dir, items, client_app
    _close_cached_catalog_con()


@pytest.fixture
def isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    built_catalog: tuple[Path, Path, Path, list[dict[str, Any]], TestClient],
) -> Iterator[tuple[TestClient, _ConfigurableBackendClient, Path]]:
    pjson, cdb, key_dir, items, api = built_catalog
    state_db = tmp_path / "state.sqlite"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    monkeypatch.setattr(gemini_allergens, "CACHE", data_dir / "cache" / "gemini_allergens.json")
    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", data_dir / "cache" / "gemini_recipe.json")
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setattr(embeddings, "EMB_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(embeddings, "IDS_PATH", data_dir / "embeddings_ids.json")
    monkeypatch.setattr(embeddings, "CHUNK_DIR", data_dir / "cache" / "emb_chunks")
    monkeypatch.setattr(embeddings, "QCACHE", data_dir / "cache" / "query_embeddings.json")
    monkeypatch.setattr(gemini, "LEDGER_DIR", data_dir / "gemini_calls")
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])

    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_API_KEY", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", NOW_ISO)
    monkeypatch.setenv("AIMO_APPROVER_PINS", json.dumps(APPROVER_PINS))

    _close_cached_catalog_con()
    _clear_policy_caches()
    embeddings._matrix.cache_clear()
    embeddings.embed_query.cache_clear()
    human_auth.reset_for_tests()
    _reset_breakers()

    backend = _ConfigurableBackendClient(items)
    bc.set_client(backend)
    try:
        yield api, backend, tmp_path
    finally:
        bc.set_client(None)
        human_auth.reset_for_tests()
        _reset_breakers()
        _close_cached_catalog_con()
        _clear_policy_caches()
        with store._lock:
            cached_con = store._conns.pop(str(state_db), None)
        if cached_con is not None:
            try:
                cached_con.close()
            except Exception:
                pass


def _quote_and_order(
    api: TestClient,
    headers: dict[str, str],
    lines: list[dict[str, Any]],
    *,
    need: dict[str, Any] | None = None,
    delivery_date: date = DELIVERY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload: dict[str, Any] = {
        "delivery_date": delivery_date.isoformat(),
        "lines": lines,
    }
    if need is not None:
        payload["need"] = need
    rq = api.post("/v1/quotes", headers=headers, json=payload)
    assert rq.status_code == 200, rq.text
    quote = rq.json()
    ro = api.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert ro.status_code == 200, ro.text
    return quote, ro.json()


def _facts_for_learned(pid: str) -> dict[str, Any]:
    row = textsearch.get_rows([pid])[pid]
    return {
        "category": row.get("category"),
        "types": sorted(product_types(row)),
        "claims": sorted(row.get("claims") or []),
        "content_unit": row.get("content_unit"),
    }


def _assert_audit_chain_valid_ed25519(api: TestClient) -> dict[str, Any]:
    rv = api.get("/v1/audit/verify")
    assert rv.status_code == 200, rv.text
    verdict = rv.json()
    assert verdict["ok"] is True, verdict
    assert verdict["broken_at"] is None, verdict
    assert verdict.get("reason") is None, verdict
    assert verdict["checked"] > 0
    assert verdict.get("kids"), verdict

    jwks_resp = api.get("/.well-known/jwks.json")
    assert jwks_resp.status_code == 200
    jwks_doc = jwks_resp.json()
    kids = {k["kid"] for k in jwks_doc.get("keys") or []}
    assert kids
    assert set(verdict["kids"]) <= kids

    events_resp = api.get("/v1/audit")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) == verdict["checked"]
    prev_hash = "0" * 64
    for ev in events:
        assert ev["prev_hash"] == prev_hash
        assert ev["kid"] in kids
        assert ev["signature"]
        hdr, jws_payload = verify_jws(ev["signature"], jwks_doc)
        assert hdr["kid"] == ev["kid"]
        assert hdr["alg"] == "EdDSA"
        assert jws_payload["seq"] == ev["seq"]
        assert jws_payload["hash"] == ev["hash"]
        prev_hash = ev["hash"]
    return verdict


def test_catalog_fixture_allergen_statuses_and_sources(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
) -> None:
    _api, _backend, _tmp = isolated_env
    expected_school = {
        "471410": ("milk", "contains", "structured"),
        "471425": ("milk", "may_contain", "structured"),
        "472833": ("milk", "unknown", "none"),
        "472899": ("milk", "free", "inferred:gemini"),
        "472950": ("milk", "unknown", "contradiction"),
        "471424": ("nuts", "contains", "structured"),
        "471426": ("nuts", "may_contain", "structured"),
        "472834": ("nuts", "unknown", "none"),
        "472898": ("nuts", "free", "inferred:gemini"),
        "472951": ("nuts", "unknown", "contradiction"),
    }
    for pid, (group, exp_st, exp_src) in expected_school.items():
        prod = get_product(pid)
        assert prod is not None
        st = prod.allergens[group]
        st_val = st.value if hasattr(st, "value") else st
        assert st_val == exp_st, (pid, group, st_val)
        assert prod.allergen_source[group] == exp_src, (pid, group, prod.allergen_source[group])

    expected_hospital = {
        "501100": ("contains", "structured"),
        "501103": ("contains", "structured"),
        "501102": ("may_contain", "structured"),
        "501101": ("unknown", "none"),
        "501199": ("free", "inferred:gemini"),
        "501150": ("unknown", "contradiction"),
        "469269": ("free", "structured:claim"),
        "480200": ("free", "structured:claim"),
        "460395": ("unknown", "none"),
    }
    for pid, (exp_st, exp_src) in expected_hospital.items():
        prod = get_product(pid)
        assert prod is not None
        st = prod.allergens["gluten"]
        st_val = st.value if hasattr(st, "value") else st
        assert st_val == exp_st, (pid, st_val)
        assert prod.allergen_source["gluten"] == exp_src, (pid, prod.allergen_source["gluten"])


def test_school_learned_swaps_product_and_product_type_never_weaken_allergen_hardblock(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
) -> None:
    api, backend, _tmp = isolated_env

    q_prod, o_prod = _quote_and_order(api, H_SCHOOL, [{"product_id": "470863", "qty_sales_units": 10}])
    assert o_prod["status"] == "pending_approval"
    assert o_prod["decision"]["approver_role"] == "kitchen_manager"
    r_dec_prod = api.post(
        f"/v1/approvals/{o_prod['order_id']}/decision",
        headers=_pin_hdr(SCHOOL_KM),
        json={
            "decision": "approve",
            "approver": SCHOOL_KM,
            "quote_hash": q_prod["quote_hash"],
            "remember": True,
            "remember_scope": "product",
            "note": "remember Oatly Semi oat drink",
        },
    )
    assert r_dec_prod.status_code == 200, r_dec_prod.text
    assert r_dec_prod.json()["status"] == "placed"
    assert r_dec_prod.json()["provenance"]["learned"]["remembered"] is True

    q_type, o_type = _quote_and_order(api, H_SCHOOL, [{"product_id": "479001", "qty_sales_units": 10}])
    assert o_type["status"] == "pending_approval"
    r_dec_type = api.post(
        f"/v1/approvals/{o_type['order_id']}/decision",
        headers=_session_hdr(api, SCHOOL_KM),
        json={
            "decision": "approve",
            "approver": SCHOOL_KM,
            "quote_hash": q_type["quote_hash"],
            "remember": True,
            "remember_scope": "product_type",
            "note": "remember safe 1 L oat drink type",
        },
    )
    assert r_dec_type.status_code == 200, r_dec_type.text
    assert r_dec_type.json()["status"] == "placed"
    assert r_dec_type.json()["provenance"]["learned"]["remembered"] is True

    _q_auto_1, o_auto_1 = _quote_and_order(api, H_SCHOOL, [{"product_id": "470863", "qty_sales_units": 10}])
    assert o_auto_1["status"] == "placed"
    assert o_auto_1["decision"]["outcome"] == "auto_approve"

    _q_auto_2, o_auto_2 = _quote_and_order(api, H_SCHOOL, [{"product_id": "479002", "qty_sales_units": 10}])
    assert o_auto_2["status"] == "placed"
    assert o_auto_2["decision"]["outcome"] == "auto_approve"

    unsafe_school_matrix = (
        ("471410", "milk", "contains"),
        ("471425", "milk", "may_contain"),
        ("472833", "milk", "unknown"),
        ("472899", "milk", "inferred:gemini"),
        ("472950", "milk", "contradiction"),
        ("471424", "nuts", "contains"),
        ("471426", "nuts", "may_contain"),
        ("472834", "nuts", "unknown"),
        ("472898", "nuts", "inferred:gemini"),
        ("472951", "nuts", "contradiction"),
    )

    for pid, _grp, _variant in unsafe_school_matrix:
        matched_by_type = learned.match("school-tampere", pid, "470889", _facts_for_learned(pid))
        assert matched_by_type is not None and matched_by_type["scope"] == "product_type", pid

    for pid, _grp, _variant in (*unsafe_school_matrix, ("464306", "milk", "contains")):
        backend.prices[pid] = 0.20
        for scope in ("product", "product_type"):
            learned.remember_swap(
                "school-tampere",
                "470889",
                pid,
                scope=scope,
                approver=SCHOOL_KM,
                approver_role="kitchen_manager",
            )
            learned.remember_swap(
                "school-tampere",
                None,
                pid,
                scope=scope,
                approver=SCHOOL_CD,
                approver_role="catering_director",
            )

    for safe_pid in ("470889", "470863", "479001", "479002"):
        backend.stock[safe_pid] = 0.0

    need_obj = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=25.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    r_plan = api.post("/v1/plan", headers=H_SCHOOL, json=need_obj.model_dump(mode="json"))
    assert r_plan.status_code == 200, r_plan.text
    http_plan = r_plan.json()
    assert http_plan["lines"] == []
    assert http_plan["shortfall"] == pytest.approx(25.0)
    assert {"milk", "nuts"} <= set(http_plan["need"]["excluded_allergens"])
    unsafe_ids = {pid for pid, _, _ in unsafe_school_matrix} | {"464306"}
    for cand in http_plan["candidates"]:
        if cand["product"]["id"] in unsafe_ids:
            assert cand["tier"] == "rejected", cand

    cat_plan = plan_need(need_obj)
    assert cat_plan.lines == []
    det_plan = service._plan_deterministic(need_obj)
    assert det_plan.lines == []

    mandate = trust_policy.get_mandate("school-tampere", "buyer-1")
    for pid, expected_group, variant in (*unsafe_school_matrix, ("464306", "milk", "contains")):
        quote, order = _quote_and_order(api, H_SCHOOL, [{"product_id": pid, "qty_sales_units": 5}])
        assert quote["lines"][0]["tier"] == "rejected", (pid, variant)
        assert order["status"] == "rejected", (pid, variant, order)
        assert order["decision"]["outcome"] == "reject", (pid, variant, order["decision"])
        allergen_hits = [
            h
            for h in order["decision"]["hits"]
            if h["rule_id"] == f"allergens:{pid}" and h["outcome"] == "reject"
        ]
        assert len(allergen_hits) == 1, (pid, variant, order["decision"]["hits"])
        msg_low = allergen_hits[0]["message"].lower()
        assert expected_group in msg_low, (pid, variant, msg_low)
        if variant == "inferred:gemini":
            assert "ai-inferred" in msg_low or "unknown" in msg_low

        stored_q = service.get_quote(quote["quote_id"])
        direct_dec = trust_policy.evaluate(stored_q, mandate)
        assert direct_dec.outcome == "reject", (pid, variant, direct_dec)

    _assert_audit_chain_valid_ed25519(api)


def test_hospital_learned_swaps_product_and_product_type_never_weaken_gluten_policy(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
) -> None:
    api, backend, _tmp = isolated_env

    q_prod, o_prod = _quote_and_order(api, H_HOSPITAL, [{"product_id": "473399", "qty_sales_units": 6}])
    assert o_prod["status"] == "pending_approval"
    assert o_prod["decision"]["approver_role"] == "dietitian"
    r_dec_prod = api.post(
        f"/v1/approvals/{o_prod['order_id']}/decision",
        headers=_pin_hdr(HOSP_DIETITIAN),
        json={
            "decision": "approve",
            "approver": HOSP_DIETITIAN,
            "quote_hash": q_prod["quote_hash"],
            "remember": True,
            "remember_scope": "product",
        },
    )
    assert r_dec_prod.status_code == 200, r_dec_prod.text
    assert r_dec_prod.json()["status"] == "placed"
    assert r_dec_prod.json()["provenance"]["learned"]["remembered"] is True

    q_type, o_type = _quote_and_order(api, H_HOSPITAL, [{"product_id": "461765", "qty_sales_units": 6}])
    assert o_type["status"] == "pending_approval"
    r_dec_type = api.post(
        f"/v1/approvals/{o_type['order_id']}/decision",
        headers=_session_hdr(api, HOSP_DIETITIAN),
        json={
            "decision": "approve",
            "approver": HOSP_DIETITIAN,
            "quote_hash": q_type["quote_hash"],
            "remember": True,
            "remember_scope": "product_type",
        },
    )
    assert r_dec_type.status_code == 200, r_dec_type.text
    assert r_dec_type.json()["status"] == "placed"
    assert r_dec_type.json()["provenance"]["learned"]["remembered"] is True

    _q_auto_1, o_auto_1 = _quote_and_order(api, H_HOSPITAL, [{"product_id": "473399", "qty_sales_units": 6}])
    assert o_auto_1["status"] == "placed"
    assert o_auto_1["decision"]["outcome"] == "auto_approve"

    _q_auto_2, o_auto_2 = _quote_and_order(api, H_HOSPITAL, [{"product_id": "461766", "qty_sales_units": 6}])
    assert o_auto_2["status"] == "placed"
    assert o_auto_2["decision"]["outcome"] == "auto_approve"

    for pid in ("501103", "501102", "501199", "480200"):
        matched = learned.match("hospital-meilahti", pid, "473328", _facts_for_learned(pid))
        assert matched is not None and matched["scope"] == "product_type", pid

    for unmatching_pid in ("501101", "501150"):
        assert learned.match("hospital-meilahti", unmatching_pid, "473328", _facts_for_learned(unmatching_pid)) is None
        _q_pre, o_pre = _quote_and_order(api, H_HOSPITAL, [{"product_id": unmatching_pid, "qty_sales_units": 4}])
        assert o_pre["status"] == "rejected"
        assert o_pre["decision"]["outcome"] == "reject"

    all_hospital_matrix = ("501100", "501103", "501102", "501101", "501199", "501150", "480200", "460395")
    for pid in all_hospital_matrix:
        backend.prices[pid] = 0.20
        for scope in ("product", "product_type"):
            learned.remember_swap(
                "hospital-meilahti",
                "473328",
                pid,
                scope=scope,
                approver=HOSP_DIETITIAN,
                approver_role="dietitian",
            )
            learned.remember_swap(
                "hospital-meilahti",
                None,
                pid,
                scope=scope,
                approver=HOSP_DIETITIAN,
                approver_role="dietitian",
            )

    for safe_pid in ("473328", "473399", "461765", "461766"):
        backend.stock[safe_pid] = 0.0

    need_bread = Need(
        customer_id="hospital-meilahti",
        product_query="dark bread",
        excluded_allergens=[],
        quantity=4.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    r_plan = api.post("/v1/plan", headers=H_HOSPITAL, json=need_bread.model_dump(mode="json"))
    assert r_plan.status_code == 200, r_plan.text
    plan_json = r_plan.json()
    plan_hard_blocked = {"501100", "501103", "501102", "501101", "501199", "501150"}
    assert not ({ln["product_id"] for ln in plan_json["lines"]} & plan_hard_blocked)
    for cand in plan_json["candidates"]:
        if cand["product"]["id"] in plan_hard_blocked:
            assert cand["tier"] == "rejected", cand

    cat_plan = plan_need(need_bread)
    assert not ({ln.product_id for ln in cat_plan.lines} & plan_hard_blocked)
    det_plan = service._plan_deterministic(need_bread)
    assert not ({ln.product_id for ln in det_plan.lines} & plan_hard_blocked)

    mandate = trust_policy.get_mandate("hospital-meilahti", "buyer-1")
    for pid in ("501100", "501103", "501102"):
        quote, order = _quote_and_order(api, H_HOSPITAL, [{"product_id": pid, "qty_sales_units": 4}])
        assert quote["lines"][0]["tier"] == "rejected"
        assert order["status"] == "rejected", (pid, order)
        assert order["decision"]["outcome"] == "reject", (pid, order["decision"])
        allergen_hits = [
            h
            for h in order["decision"]["hits"]
            if h["rule_id"] == f"allergens:{pid}" and h["outcome"] == "reject"
        ]
        assert len(allergen_hits) == 1
        assert "gluten" in allergen_hits[0]["message"].lower()
        assert trust_policy.evaluate(service.get_quote(quote["quote_id"]), mandate).outcome == "reject"

    for pid in ("501101", "501199", "501150", "480200", "460395"):
        quote, order = _quote_and_order(api, H_HOSPITAL, [{"product_id": pid, "qty_sales_units": 4}])
        assert order["status"] == "pending_approval", (pid, order)
        assert order["decision"]["outcome"] == "needs_approval", (pid, order["decision"])
        assert order["decision"]["approver_role"] == "dietitian", (pid, order["decision"])
        allergen_hits = [
            h
            for h in order["decision"]["hits"]
            if h["rule_id"] == f"allergens:{pid}" and h["outcome"] == "needs_approval"
        ]
        assert len(allergen_hits) == 1, (pid, order["decision"]["hits"])
        assert "gluten" in allergen_hits[0]["message"].lower()

    _assert_audit_chain_valid_ed25519(api)


@pytest.mark.parametrize(
    ("query_text", "expected_allergen_word"),
    [
        ("milk", "milk"),
        ("maito", "milk"),
        ("hazelnut drink", "nuts"),
    ],
)
def test_school_request_naming_excluded_allergen_still_needs_kitchen_manager_with_learned_swap(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
    query_text: str,
    expected_allergen_word: str,
) -> None:
    api, _backend, _tmp = isolated_env

    q_sub, o_sub = _quote_and_order(api, H_SCHOOL, [{"product_id": "470863", "qty_sales_units": 10}])
    assert o_sub["status"] == "pending_approval"
    r_approve_sub = api.post(
        f"/v1/approvals/{o_sub['order_id']}/decision",
        headers=_pin_hdr(SCHOOL_KM),
        json={
            "decision": "approve",
            "approver": SCHOOL_KM,
            "quote_hash": q_sub["quote_hash"],
            "remember": True,
            "remember_scope": "product_type",
        },
    )
    assert r_approve_sub.status_code == 200
    assert r_approve_sub.json()["status"] == "placed"

    learned.remember_swap(
        "school-tampere",
        "470889",
        "470863",
        scope="product",
        approver=SCHOOL_KM,
        approver_role="kitchen_manager",
    )
    learned.remember_swap(
        "school-tampere",
        None,
        "479001",
        scope="product",
        approver=SCHOOL_KM,
        approver_role="kitchen_manager",
    )

    _q_ctrl, o_ctrl = _quote_and_order(
        api,
        H_SCHOOL,
        [{"product_id": "470863", "qty_sales_units": 10}],
        need={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "quantity": 10.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert o_ctrl["status"] == "placed"
    assert o_ctrl["decision"]["outcome"] == "auto_approve"

    need_payload = {
        "customer_id": "school-tampere",
        "product_query": query_text,
        "quantity": 15.0,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }
    r_plan = api.post("/v1/plan", headers=H_SCHOOL, json=need_payload)
    assert r_plan.status_code == 200, r_plan.text
    plan_data = r_plan.json()
    assert {"milk", "nuts"} <= set(plan_data["need"]["excluded_allergens"])
    for ln in plan_data["lines"]:
        prod = get_product(ln["product_id"])
        assert prod is not None
        assert prod.allergens["milk"].value == "free"
        assert prod.allergens["nuts"].value == "free"
        assert "allergen_safe_alternative" in ln["flags"]

    for safe_oat_pid in ("470889", "470863", "479001"):
        quote, order = _quote_and_order(
            api,
            H_SCHOOL,
            [{"product_id": safe_oat_pid, "qty_sales_units": 15}],
            need=need_payload,
        )
        assert "allergen_safe_alternative" in quote["lines"][0]["flags"]
        assert order["status"] == "pending_approval", (query_text, safe_oat_pid, order)
        assert order["decision"]["outcome"] == "needs_approval"
        assert order["decision"]["approver_role"] == "kitchen_manager"
        sub_hits = [
            h
            for h in order["decision"]["hits"]
            if h["rule_id"] == f"substitution:{safe_oat_pid}" and h["outcome"] == "needs_approval"
        ]
        assert len(sub_hits) == 1
        assert expected_allergen_word in sub_hits[0]["message"].lower()
        assert "allergen-free replacement" in sub_hits[0]["message"].lower()
        assert "kitchen manager" in sub_hits[0]["message"].lower()

        r_confirm = api.post(
            f"/v1/approvals/{order['order_id']}/decision",
            headers=_pin_hdr(SCHOOL_KM),
            json={
                "decision": "approve",
                "approver": SCHOOL_KM,
                "quote_hash": quote["quote_hash"],
                "note": "confirmed dairy/nut-free oat drink replacement for school menu",
            },
        )
        assert r_confirm.status_code == 200, r_confirm.text
        assert r_confirm.json()["status"] == "placed"


@pytest.mark.parametrize(
    ("pid", "allergen_group", "variant"),
    [
        ("464306", "milk", "contains"),
        ("471425", "milk", "may_contain"),
        ("472833", "milk", "unknown"),
        ("472899", "milk", "inferred:gemini"),
        ("472950", "milk", "contradiction"),
        ("471424", "nuts", "contains"),
        ("471426", "nuts", "may_contain"),
        ("472834", "nuts", "unknown"),
        ("472898", "nuts", "inferred:gemini"),
        ("472951", "nuts", "contradiction"),
    ],
)
def test_school_allergen_reject_cannot_be_overridden_by_kitchen_manager_or_catering_director(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
    pid: str,
    allergen_group: str,
    variant: str,
) -> None:
    api, _backend, _tmp = isolated_env

    quote, order = _quote_and_order(api, H_SCHOOL, [{"product_id": pid, "qty_sales_units": 4}])
    assert order["status"] == "rejected", (pid, variant, order)
    assert order["decision"]["outcome"] == "reject"
    assert any(
        h["rule_id"] == f"allergens:{pid}" and h["outcome"] == "reject" and allergen_group in h["message"].lower()
        for h in order["decision"]["hits"]
    )

    for approver, role, hdr in (
        (SCHOOL_KM, "kitchen_manager", _pin_hdr(SCHOOL_KM)),
        (SCHOOL_KM, "kitchen_manager", _session_hdr(api, SCHOOL_KM)),
        (SCHOOL_CD, "catering_director", _pin_hdr(SCHOOL_CD)),
        (SCHOOL_CD, "catering_director", _session_hdr(api, SCHOOL_CD)),
    ):
        resp = api.post(
            f"/v1/approvals/{order['order_id']}/decision",
            headers=hdr,
            json={
                "decision": "approve",
                "approver": approver,
                "approver_role": role,
                "quote_hash": quote["quote_hash"],
                "remember": True,
                "remember_scope": "product_type",
                "note": "attempting override of school allergen hard block",
            },
        )
        assert resp.status_code == 409, (approver, resp.text)
        assert resp.json()["error"] == "not_pending"

        stored_quote = service.get_quote(quote["quote_id"])
        stored_order = service.get_order(order["order_id"])
        assert stored_order.status == "rejected"
        chk = approvals_mod.check_approval(
            stored_quote,
            stored_order.decision,
            approver,
            role,
            quote["quote_hash"],
            decision_kind="approve",
            note="direct check_approval attempt",
            order_id=order["order_id"],
            now=clock.now(),
        )
        assert chk["ok"] is False
        assert "hard rule cannot be overridden" in chk["reason"]

    r_after = api.get(f"/v1/orders/{order['order_id']}")
    assert r_after.status_code == 200
    assert r_after.json()["status"] == "rejected"
    assert learned.list_learned("school-tampere") == []
    _assert_audit_chain_valid_ed25519(api)


def test_hospital_gluten_hard_reject_and_dietitian_signoff_boundaries_with_ed25519_audit(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _backend, tmp_path = isolated_env

    for hard_pid in ("501100", "501103", "501102"):
        q_rej, o_rej = _quote_and_order(api, H_HOSPITAL, [{"product_id": hard_pid, "qty_sales_units": 3}])
        assert o_rej["status"] == "rejected"
        assert o_rej["decision"]["outcome"] == "reject"
        for approver, role in (
            (HOSP_DIETITIAN, "dietitian"),
            (HOSP_PM, "procurement_manager"),
            (HOSP_HEAD, "head_of_nutrition"),
        ):
            r_try = api.post(
                f"/v1/approvals/{o_rej['order_id']}/decision",
                headers=_pin_hdr(approver),
                json={
                    "decision": "approve",
                    "approver": approver,
                    "approver_role": role,
                    "quote_hash": q_rej["quote_hash"],
                    "remember": True,
                    "note": "override attempt on hard gluten reject",
                },
            )
            assert r_try.status_code == 409, (hard_pid, approver, r_try.text)
            assert r_try.json()["error"] == "not_pending"

            chk = approvals_mod.check_approval(
                service.get_quote(q_rej["quote_id"]),
                service.get_order(o_rej["order_id"]).decision,
                approver,
                role,
                q_rej["quote_hash"],
                decision_kind="approve",
                note="direct check_approval",
                order_id=o_rej["order_id"],
                now=clock.now(),
            )
            assert chk["ok"] is False
            assert "hard rule cannot be overridden" in chk["reason"]
        assert api.get(f"/v1/orders/{o_rej['order_id']}").json()["status"] == "rejected"
    assert learned.list_learned("hospital-meilahti") == []

    q_claim, o_claim = _quote_and_order(api, H_HOSPITAL, [{"product_id": "469269", "qty_sales_units": 4}])
    assert o_claim["status"] == "pending_approval"
    assert o_claim["decision"]["outcome"] == "needs_approval"
    assert o_claim["decision"]["approver_role"] == "dietitian"
    claim_hits = [
        h
        for h in o_claim["decision"]["hits"]
        if h["rule_id"] == "allergens:469269" and h["outcome"] == "needs_approval"
    ]
    assert len(claim_hits) == 1
    assert "nutrition claim" in claim_hits[0]["message"].lower()

    dill_prod = get_product("460395")
    assert dill_prod is not None
    now_dt = clock.now().replace(microsecond=0)
    raw_dill_quote = Quote(
        quote_id="Q-DILL-UNKNOWN-01",
        customer_id="hospital-meilahti",
        agent_id="buyer-1",
        delivery_date=DELIVERY,
        lines=[
            QuoteLine(
                product_id="460395",
                product_name=dill_prod.name,
                qty_sales_units=2.0,
                sales_unit="ST",
                unit_price=1.80,
                line_total=3.60,
                tier="best",
                flags=["allergen_unknown"],
            )
        ],
        total_net=3.60,
        total_gross=4.10,
        created_at=now_dt,
        expires_at=now_dt + timedelta(minutes=15),
        need=Need(
            customer_id="hospital-meilahti",
            product_query="fresh dill",
            quantity=2.0,
            unit="KG",
            delivery_by=DELIVERY,
        ),
    )
    signed_dill_quote = service.sign_quote(raw_dill_quote)
    assert verify_quote_detailed(signed_dill_quote, now=now_dt)["ok"] is True
    store.save_quote(signed_dill_quote)
    r_dill_order = api.post(
        "/v1/orders",
        headers=H_HOSPITAL,
        json={"quote_id": signed_dill_quote.quote_id, "quote_hash": signed_dill_quote.quote_hash},
    )
    assert r_dill_order.status_code == 200, r_dill_order.text
    o_dill = r_dill_order.json()
    assert o_dill["status"] == "pending_approval"
    assert o_dill["decision"]["outcome"] == "needs_approval"
    assert o_dill["decision"]["approver_role"] == "dietitian"
    dill_hits = [
        h
        for h in o_dill["decision"]["hits"]
        if h["rule_id"] == "allergens:460395" and h["outcome"] == "needs_approval"
    ]
    assert len(dill_hits) == 1
    assert "unknown" in dill_hits[0]["message"].lower()
    assert "dietitian" in dill_hits[0]["message"].lower()

    for q_hash, pending_order in (
        (signed_dill_quote.quote_hash, o_dill),
        (q_claim["quote_hash"], o_claim),
    ):
        oid = pending_order["order_id"]

        r_unauth = api.post(
            f"/v1/approvals/{oid}/decision",
            json={"decision": "approve", "approver": HOSP_DIETITIAN, "quote_hash": q_hash},
        )
        assert r_unauth.status_code == 401
        assert r_unauth.json()["error"] == "approver_auth_required"

        r_bad_pin = api.post(
            f"/v1/approvals/{oid}/decision",
            headers={"X-Approver-Pin": "wrong-passphrase-999"},
            json={"decision": "approve", "approver": HOSP_DIETITIAN, "quote_hash": q_hash},
        )
        assert r_bad_pin.status_code == 401
        assert r_bad_pin.json()["error"] == "approver_auth_required"
        human_auth.reset_for_tests()

        r_agent_hdr = api.post(
            f"/v1/approvals/{oid}/decision",
            headers={**_pin_hdr(HOSP_DIETITIAN), "X-Agent-Id": "buyer-1"},
            json={"decision": "approve", "approver": HOSP_DIETITIAN, "quote_hash": q_hash},
        )
        assert r_agent_hdr.status_code == 403
        assert r_agent_hdr.json()["error"] == "agent_cannot_approve"

        for wrong_cust_human in (HOTEL_PM, SCHOOL_KM, SCHOOL_CD):
            r_wrong_cust = api.post(
                f"/v1/approvals/{oid}/decision",
                headers=_pin_hdr(wrong_cust_human),
                json={"decision": "approve", "approver": wrong_cust_human, "quote_hash": q_hash},
            )
            assert r_wrong_cust.status_code == 403, (wrong_cust_human, r_wrong_cust.text)
            assert r_wrong_cust.json()["error"] == "approval_refused"

        r_sess_mismatch = api.post(
            f"/v1/approvals/{oid}/decision",
            headers=_session_hdr(api, HOSP_DIETITIAN),
            json={"decision": "approve", "approver": HOSP_HEAD, "quote_hash": q_hash},
        )
        assert r_sess_mismatch.status_code == 403
        assert r_sess_mismatch.json()["error"] == "approver_mismatch"

        r_wrong_role = api.post(
            f"/v1/approvals/{oid}/decision",
            headers=_pin_hdr(HOSP_PM),
            json={"decision": "approve", "approver": HOSP_PM, "quote_hash": q_hash, "note": "budget ok"},
        )
        assert r_wrong_role.status_code == 403, r_wrong_role.text
        assert r_wrong_role.json()["error"] == "approval_refused"
        assert "procurement manager" in r_wrong_role.json()["message"].lower()

        r_role_lie = api.post(
            f"/v1/approvals/{oid}/decision",
            headers=_pin_hdr(HOSP_PM),
            json={
                "decision": "approve",
                "approver": HOSP_PM,
                "approver_role": "dietitian",
                "quote_hash": q_hash,
            },
        )
        assert r_role_lie.status_code == 403
        assert r_role_lie.json()["error"] == "approval_refused"

        r_hash_mis = api.post(
            f"/v1/approvals/{oid}/decision",
            headers=_pin_hdr(HOSP_DIETITIAN),
            json={"decision": "approve", "approver": HOSP_DIETITIAN, "quote_hash": "0" * 64},
        )
        assert r_hash_mis.status_code == 409
        assert r_hash_mis.json()["error"] == "quote_hash_mismatch"

        assert api.get(f"/v1/orders/{oid}").json()["status"] == "pending_approval"

    monkeypatch.setenv("AIMO_APPROVER_PIN", "shared-fallback-pin-12ch")
    r_agent_body = api.post(
        f"/v1/approvals/{o_dill['order_id']}/decision",
        headers={"X-Approver-Pin": "shared-fallback-pin-12ch"},
        json={"decision": "approve", "approver": "hospital-meilahti/buyer-1", "quote_hash": signed_dill_quote.quote_hash},
    )
    assert r_agent_body.status_code == 403
    assert r_agent_body.json()["error"] == "approval_refused"
    assert "ai agent identity" in r_agent_body.json()["message"].lower()
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)

    r_sara_ok = api.post(
        f"/v1/approvals/{o_dill['order_id']}/decision",
        headers=_session_hdr(api, HOSP_DIETITIAN),
        json={
            "decision": "approve",
            "approver": HOSP_DIETITIAN,
            "quote_hash": signed_dill_quote.quote_hash,
            "note": "single-ingredient fresh dill checked and cleared for coeliac ward",
        },
    )
    assert r_sara_ok.status_code == 200, r_sara_ok.text
    placed_dill = r_sara_ok.json()
    assert placed_dill["status"] == "placed"
    assert placed_dill["response_code"] == "AP"
    assert placed_dill["approved_by"] == HOSP_DIETITIAN
    assert placed_dill["approver_role"] == "dietitian"

    r_helena_ok = api.post(
        f"/v1/approvals/{o_claim['order_id']}/decision",
        headers=_pin_hdr(HOSP_HEAD),
        json={
            "decision": "approve",
            "approver": HOSP_HEAD,
            "quote_hash": q_claim["quote_hash"],
            "note": "override sign-off: verified certified gluten-free oat muesli pack label",
        },
    )
    assert r_helena_ok.status_code == 200, r_helena_ok.text
    placed_claim = r_helena_ok.json()
    assert placed_claim["status"] == "placed"
    assert placed_claim["response_code"] == "AP"
    assert placed_claim["approved_by"] == HOSP_HEAD
    assert placed_claim["approver_role"] == "head_of_nutrition"
    assert "verified certified gluten-free" in (placed_claim["approval_note"] or "")

    custom_pol_dir = tmp_path / "custom_policies"
    custom_pol_dir.mkdir(parents=True, exist_ok=True)
    for pol_path in REAL_POLICY_DIR.glob("*.yaml"):
        doc = yaml.safe_load(pol_path.read_text(encoding="utf-8"))
        if doc.get("policy_id") == "hospital-meilahti":
            doc["override_roles"] = ["head_of_nutrition", "procurement_manager"]
        (custom_pol_dir / pol_path.name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(trust_policy, "POLICY_DIR", custom_pol_dir)
    _clear_policy_caches()

    q_claim2, o_claim2 = _quote_and_order(api, H_HOSPITAL, [{"product_id": "480200", "qty_sales_units": 3}])
    assert o_claim2["status"] == "pending_approval"
    assert o_claim2["decision"]["approver_role"] == "dietitian"
    r_olli_override = api.post(
        f"/v1/approvals/{o_claim2['order_id']}/decision",
        headers=_pin_hdr(HOSP_PM),
        json={
            "decision": "approve",
            "approver": HOSP_PM,
            "quote_hash": q_claim2["quote_hash"],
            "note": "duty override: dietitian sheet confirms gluten-free batch",
        },
    )
    assert r_olli_override.status_code == 200, r_olli_override.text
    assert r_olli_override.json()["status"] == "placed"
    assert r_olli_override.json()["approved_by"] == HOSP_PM

    q_contains_custom, o_contains_custom = _quote_and_order(
        api, H_HOSPITAL, [{"product_id": "501100", "qty_sales_units": 2}]
    )
    assert o_contains_custom["status"] == "rejected"
    r_olli_hard = api.post(
        f"/v1/approvals/{o_contains_custom['order_id']}/decision",
        headers=_pin_hdr(HOSP_PM),
        json={
            "decision": "approve",
            "approver": HOSP_PM,
            "quote_hash": q_contains_custom["quote_hash"],
            "note": "cannot override contains gluten",
        },
    )
    assert r_olli_hard.status_code == 409

    trail = api.get("/v1/audit", params={"subject_id": o_dill["order_id"]}).json()
    actions = [e["action"] for e in trail]
    assert "approval.refused" in actions
    assert "approval.verified" in actions
    assert "order.placed" in actions
    _assert_audit_chain_valid_ed25519(api)


def test_seller_proposals_and_standing_order_drift_replans_never_bypass_allergens_with_learned_swaps(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
) -> None:
    api, backend, _tmp = isolated_env

    unsafe_school_ids = (
        "464306",
        "471410",
        "471425",
        "472833",
        "472899",
        "472950",
        "471424",
        "471426",
        "472834",
        "472898",
        "472951",
    )
    for pid in unsafe_school_ids:
        backend.prices[pid] = 0.15
        learned.remember_swap("school-tampere", "470889", pid, scope="product_type", approver=SCHOOL_KM)
        learned.remember_swap("school-tampere", "470863", pid, scope="product", approver=SCHOOL_KM)

    backend.prices["470889"] = 2.40
    backend.prices["470863"] = 2.30
    backend.prices["479001"] = 1.65

    r_prop_auto = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "school-tampere", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_prop_auto.status_code == 200, r_prop_auto.text
    auto_prop = r_prop_auto.json()
    assert auto_prop["product"]["product_id"] not in set(unsafe_school_ids)
    chosen_prod = get_product(auto_prop["product"]["product_id"])
    assert chosen_prod is not None
    assert chosen_prod.allergens["milk"].value == "free"
    assert chosen_prod.allergens["nuts"].value == "free"
    assert chosen_prod.allergen_source["milk"] == "structured"
    assert chosen_prod.allergen_source["nuts"] == "structured"

    for pick_pid in ("464306", "471424", "472950"):
        r_pick = api.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={
                "customer_id": "school-tampere",
                "product_id": pick_pid,
                "qty_sales_units": 5,
                "replaces_product_id": "470889",
                "delivery_date": DELIVERY.isoformat(),
                "note": "special discount",
            },
        )
        assert r_pick.status_code == 200, r_pick.text
        pick_prop = r_pick.json()
        assert pick_prop["seller_can_order"] is False
        assert pick_prop["policy_preview"]["outcome"] == "reject"

        r_seller_no_cust = api.post(f"/v1/proposals/{pick_prop['proposal_id']}/accept", headers=H_SELLER)
        assert r_seller_no_cust.status_code == 401

        r_seller_accept = api.post(
            f"/v1/proposals/{pick_prop['proposal_id']}/accept",
            headers={**H_SELLER, "X-Customer-Id": "school-tampere"},
        )
        assert r_seller_accept.status_code == 403
        assert r_seller_accept.json()["error"] == "seller_cannot_order"

        r_buyer_accept = api.post(f"/v1/proposals/{pick_prop['proposal_id']}/accept", headers=H_SCHOOL)
        assert r_buyer_accept.status_code == 200, r_buyer_accept.text
        accepted = r_buyer_accept.json()
        assert accepted["order"]["status"] == "rejected"
        assert accepted["order"]["decision"]["outcome"] == "reject"

        r_override = api.post(
            f"/v1/approvals/{accepted['order']['order_id']}/decision",
            headers=_pin_hdr(SCHOOL_CD),
            json={
                "decision": "approve",
                "approver": SCHOOL_CD,
                "quote_hash": accepted["quote"]["quote_hash"],
                "note": "director override attempt on seller proposal",
            },
        )
        assert r_override.status_code == 409

    unsafe_hosp_ids = ("501100", "501103", "501102", "501101", "501199", "501150")
    for pid in unsafe_hosp_ids:
        backend.prices[pid] = 0.15
        learned.remember_swap("hospital-meilahti", "473328", pid, scope="product_type", approver=HOSP_DIETITIAN)
        learned.remember_swap("hospital-meilahti", "461765", pid, scope="product", approver=HOSP_DIETITIAN)

    backend.stock["470863"] = 0.0
    r_drift_school = api.get("/v1/drift/school-tampere")
    assert r_drift_school.status_code == 200, r_drift_school.text
    school_flags = r_drift_school.json()["flags"]
    avail_school = [f for f in school_flags if f["product_id"] == "470863" and f["kind"] == "availability"]
    assert len(avail_school) == 1
    assert avail_school[0]["route"] == "approval"
    assert avail_school[0]["fix"] == {"action": "replan"}

    r_replan_school = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "quantity": 40.0,
            "unit": "L",
            "delivery_by": r_drift_school.json()["delivery_date"],
        },
    )
    assert r_replan_school.status_code == 200, r_replan_school.text
    replan_school = r_replan_school.json()
    assert replan_school["lines"]
    for ln in replan_school["lines"]:
        assert ln["product_id"] not in set(unsafe_school_ids)
        p_ln = get_product(ln["product_id"])
        assert p_ln is not None
        assert p_ln.allergens["milk"].value == "free" and p_ln.allergen_source["milk"] == "structured"
        assert p_ln.allergens["nuts"].value == "free" and p_ln.allergen_source["nuts"] == "structured"

    backend.stock["461765"] = 0.0
    r_drift_hosp = api.get("/v1/drift/hospital-meilahti")
    assert r_drift_hosp.status_code == 200, r_drift_hosp.text
    hosp_flags = r_drift_hosp.json()["flags"]
    avail_hosp = [f for f in hosp_flags if f["product_id"] == "461765" and f["kind"] == "availability"]
    assert len(avail_hosp) == 1
    assert avail_hosp[0]["route"] == "approval"
    assert avail_hosp[0]["fix"] == {"action": "replan"}

    r_replan_hosp = api.post(
        "/v1/plan",
        headers=H_HOSPITAL,
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "gluten free dark bread",
            "quantity": 6.0,
            "unit": "KG",
            "delivery_by": r_drift_hosp.json()["delivery_date"],
        },
    )
    assert r_replan_hosp.status_code == 200, r_replan_hosp.text
    replan_hosp = r_replan_hosp.json()
    assert replan_hosp["lines"]
    for ln in replan_hosp["lines"]:
        assert ln["product_id"] not in set(unsafe_hosp_ids)
        p_ln = get_product(ln["product_id"])
        assert p_ln is not None
        assert p_ln.allergens["gluten"].value == "free"
        assert p_ln.allergen_source["gluten"] == "structured"

    assert drift._allergen_conflicts("464306", ["milk", "nuts"]) == ["milk (contains)"]
    assert drift._allergen_conflicts("501102", ["gluten"]) == ["gluten (may contain)"]

    backend.stock["479001"] = 5000.0
    backend.stock["479002"] = 5000.0
    q_wait, o_wait = _quote_and_order(
        api,
        H_SCHOOL,
        [{"product_id": "479001", "qty_sales_units": 200}],
        need={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "quantity": 200.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert o_wait["status"] == "pending_approval"
    backend.stock["479001"] = 0.0
    r_requote = api.post(
        f"/v1/approvals/{o_wait['order_id']}/decision",
        headers=_pin_hdr(SCHOOL_KM),
        json={"decision": "approve", "approver": SCHOOL_KM, "quote_hash": q_wait["quote_hash"]},
    )
    assert r_requote.status_code == 409
    rq_body = r_requote.json()
    assert rq_body["error"] == "requote_needed"
    new_lines = rq_body["detail"]["new_quote"]["lines"]
    assert new_lines
    for ln in new_lines:
        assert ln["product_id"] not in set(unsafe_school_ids)
        p_ln = get_product(ln["product_id"])
        assert p_ln is not None
        assert p_ln.allergens["milk"].value == "free" and p_ln.allergen_source["milk"] == "structured"
        assert p_ln.allergens["nuts"].value == "free" and p_ln.allergen_source["nuts"] == "structured"

    _assert_audit_chain_valid_ed25519(api)


def test_cross_customer_learned_swap_and_audit_isolation(
    isolated_env: tuple[TestClient, _ConfigurableBackendClient, Path],
) -> None:
    api, _backend, _tmp = isolated_env

    q_hotel_milk, o_hotel_milk = _quote_and_order(
        api,
        H_HOTEL,
        [{"product_id": "464306", "qty_sales_units": 400}],
        need={
            "customer_id": "hotel-aurora",
            "product_query": "lactose free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": 400.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert o_hotel_milk["status"] == "pending_approval"
    assert o_hotel_milk["decision"]["approver_role"] == "procurement_manager"

    r_hotel_approve = api.post(
        f"/v1/approvals/{o_hotel_milk['order_id']}/decision",
        headers=_pin_hdr(HOTEL_PM),
        json={
            "decision": "approve",
            "approver": HOTEL_PM,
            "quote_hash": q_hotel_milk["quote_hash"],
            "remember": True,
            "remember_scope": "product_type",
            "note": "always allow lactose-free 1 L milk swaps for hotel breakfast",
        },
    )
    assert r_hotel_approve.status_code == 200, r_hotel_approve.text
    assert r_hotel_approve.json()["status"] == "placed"
    assert r_hotel_approve.json()["provenance"]["learned"]["remembered"] is True

    learned.remember_swap(
        "hotel-aurora",
        None,
        "501100",
        scope="product_type",
        approver=HOTEL_PM,
        approver_role="procurement_manager",
    )

    r_hotel_learned = api.get("/v1/customers/hotel-aurora/learned", headers=H_HOTEL)
    assert r_hotel_learned.status_code == 200, r_hotel_learned.text
    hotel_swaps = r_hotel_learned.json()["swaps"]
    milk_swaps = [s for s in hotel_swaps if s["to_product_id"] == "464306"]
    assert len(milk_swaps) == 1
    assert milk_swaps[0]["scope"] == "product_type"
    assert milk_swaps[0]["kind"]["category"] == "10400"
    assert milk_swaps[0]["kind"]["content_unit"] == "L"
    assert "FREE_FROM_LACTOSE" in milk_swaps[0]["kind"]["claims"]

    _q_hotel_sib, o_hotel_sib = _quote_and_order(
        api,
        H_HOTEL,
        [{"product_id": "469364", "qty_sales_units": 20}],
        need={
            "customer_id": "hotel-aurora",
            "product_query": "lactose free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": 20.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert o_hotel_sib["status"] == "placed"
    assert o_hotel_sib["decision"]["outcome"] == "auto_approve"

    for other_hdr, other_cid in ((H_SCHOOL, "school-tampere"), (H_HOSPITAL, "hospital-meilahti")):
        r_cross_read = api.get("/v1/customers/hotel-aurora/learned", headers=other_hdr)
        assert r_cross_read.status_code == 403
        assert r_cross_read.json()["error"] == "customer_mismatch"

        r_own_learned = api.get(f"/v1/customers/{other_cid}/learned", headers=other_hdr)
        assert r_own_learned.status_code == 200
        assert r_own_learned.json()["swaps"] == []

        assert learned.list_learned(other_cid) == []
        assert learned.match(other_cid, "464306", "463038", _facts_for_learned("464306")) is None
        assert learned.match(other_cid, "469364", None, _facts_for_learned("469364")) is None
        assert learned.match(other_cid, "501100", None, _facts_for_learned("501100")) is None

        pol_view = service.list_policies(identity={"customer_id": other_cid, "agent_id": "buyer-1"})
        assert pol_view["learned_swaps"]["customer_id"] == other_cid
        assert pol_view["learned_swaps"]["swaps"] == []

        r_cross_inbox = api.get("/v1/approvals", headers=other_hdr, params={"customer_id": "hotel-aurora", "status": "all"})
        assert r_cross_inbox.status_code == 403
        assert r_cross_inbox.json()["error"] == "customer_mismatch"

    r_school_plan = api.post(
        "/v1/plan",
        headers=H_SCHOOL,
        json={
            "customer_id": "school-tampere",
            "product_query": "lactose free milk",
            "quantity": 20.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_school_plan.status_code == 200
    school_plan = r_school_plan.json()
    assert all(ln["product_id"] not in ("464306", "469363", "469364", "463038") for ln in school_plan["lines"])
    for cand in school_plan["candidates"]:
        if cand["product"]["id"] in ("464306", "469363", "469364", "463038"):
            assert cand["tier"] == "rejected"

    _q_school_milk, o_school_milk = _quote_and_order(api, H_SCHOOL, [{"product_id": "464306", "qty_sales_units": 5}])
    assert o_school_milk["status"] == "rejected"
    assert o_school_milk["decision"]["outcome"] == "reject"

    _q_hosp_milk, o_hosp_milk = _quote_and_order(api, H_HOSPITAL, [{"product_id": "464306", "qty_sales_units": 5}])
    assert o_hosp_milk["status"] == "pending_approval"
    assert o_hosp_milk["decision"]["outcome"] == "needs_approval"
    assert o_hosp_milk["decision"]["approver_role"] == "dietitian"

    _q_hosp_rye, o_hosp_rye = _quote_and_order(api, H_HOSPITAL, [{"product_id": "501100", "qty_sales_units": 5}])
    assert o_hosp_rye["status"] == "rejected"
    assert o_hosp_rye["decision"]["outcome"] == "reject"

    school_inbox = api.get("/v1/approvals", headers=H_SCHOOL, params={"status": "all"}).json()
    assert all(entry["order"]["customer_id"] == "school-tampere" for entry in school_inbox)

    hosp_inbox = api.get("/v1/approvals", headers=H_HOSPITAL, params={"status": "all"}).json()
    assert all(entry["order"]["customer_id"] == "hospital-meilahti" for entry in hosp_inbox)

    verdict = _assert_audit_chain_valid_ed25519(api)
    assert global_audit.verify()["ok"] is True
    assert AuditLog(config.STATE_DB).verify()["checked"] == verdict["checked"]


def test_file_hygiene_zero_comments_zero_em_dashes_and_keyless() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in src
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    comment_tokens = [tok for tok in tokens if tok.type == tokenize.COMMENT]
    assert comment_tokens == []
