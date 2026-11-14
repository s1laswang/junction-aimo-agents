from __future__ import annotations

import json
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store, client as client_mod, orders as store
from aimo.catalog import allergens as allergens_mod, textsearch
from aimo.catalog.allergens import GS1_GROUPS
from aimo.catalog.units import parse_name_quantity
from aimo.gateway.app import create_app
from aimo.gateway.extras import install
from aimo.trust import policy as trust_policy
from scenarios import _common as sc_common, s5_allergen_safe

ROOT = Path(__file__).resolve().parents[1]
HKI = timezone(timedelta(hours=2))
TAX = 13.5

SYNTHETIC_ITEMS: list[dict[str, Any]] = [
    {
        "id": "472833",
        "en": "Fazer Aito oat drink 1 L UHT",
        "fi": "Fazer Aito kaurajuoma 1 L UHT",
        "brand": "Fazer Aito",
        "category": "10402",
        "price": 1.85,
        "stock": 5000,
        "contains": ["AW", "GO"],
        "claims": ["VEGAN", "FREE_FROM_LACTOSE"],
        "ingredients": "water, oats, rapeseed oil, calcium, salt",
    },
    {
        "id": "S5_OAT_ALT",
        "en": "Oddlygood oat drink 1 L",
        "fi": "Oddlygood kaurajuoma 1 L",
        "brand": "Valio",
        "category": "10402",
        "price": 1.95,
        "stock": 5000,
        "contains": ["AW", "GO"],
        "claims": ["VEGAN", "FREE_FROM_LACTOSE"],
        "ingredients": "water, gluten-free oat base, rapeseed oil, salt",
    },
    {
        "id": "S5_OAT_COOK",
        "en": "Oat cooking 1 L",
        "fi": "Kauraruoka 1 L",
        "brand": "Fazer Aito",
        "category": "10402",
        "price": 2.40,
        "stock": 2000,
        "contains": ["AW", "GO"],
        "claims": ["VEGAN"],
        "ingredients": "water, oats, rapeseed oil, salt",
    },
    {
        "id": "S5_MARG",
        "en": "Baking margarine 1 kg lactose free",
        "fi": "Leivontamargariini 1 kg laktoositon",
        "brand": "Sunnuntai",
        "category": "10510",
        "price": 3.20,
        "stock": 2000,
        "contains": [],
        "claims": ["FREE_FROM_LACTOSE", "VEGAN"],
        "ingredients": "rapeseed oil, palm oil, water, salt, emulsifier",
    },
    {
        "id": "S5_WFLOUR",
        "en": "Wheat flour 2 kg",
        "fi": "Vehnäjauho 2 kg",
        "brand": "Sunnuntai",
        "category": "21300",
        "price": 1.90,
        "stock": 2000,
        "contains": ["AW", "UW"],
        "claims": ["VEGAN"],
        "ingredients": "wheat flour, ascorbic acid",
    },
    {
        "id": "S5_EGGS",
        "en": "Free-range eggs 1 kg",
        "fi": "Kananmuna 1 kg",
        "brand": "Kultamuna",
        "category": "10590",
        "price": 3.60,
        "stock": 2000,
        "contains": ["AE"],
        "claims": [],
        "ingredients": "chicken eggs",
    },
    {
        "id": "S5_SUGAR",
        "en": "Fine sugar 1 kg",
        "fi": "Taloussokeri 1 kg",
        "brand": "Dansukker",
        "category": "22100",
        "price": 1.45,
        "stock": 2000,
        "contains": [],
        "claims": ["VEGAN"],
        "ingredients": "sugar",
    },
    {
        "id": "463038",
        "en": "Valio Eila semi-skimmed lactose-free milk drink 10 L",
        "fi": "Valio Eila kevytmaitojuoma 10 L laktoositon",
        "brand": "Valio",
        "category": "10400",
        "price": 11.50,
        "stock": 1000,
        "contains": ["AM"],
        "claims": ["FREE_FROM_LACTOSE"],
        "ingredients": "milk, vitamin D",
    },
    {
        "id": "464306",
        "en": "Valio Eila semi-skimmed lactose-free milk drink 1 L",
        "fi": "Valio Eila kevytmaitojuoma 1 L laktoositon",
        "brand": "Valio",
        "category": "10400",
        "price": 1.20,
        "stock": 3000,
        "contains": ["AM"],
        "claims": ["FREE_FROM_LACTOSE"],
        "ingredients": "milk, vitamin D",
    },
    {
        "id": "S5_WHOLE_MILK",
        "en": "Valio whole milk 1 L",
        "fi": "Valio täysmaito 1 L",
        "brand": "Valio",
        "category": "10400",
        "price": 1.15,
        "stock": 3000,
        "contains": ["AM", "ML"],
        "claims": [],
        "ingredients": "pasteurized whole milk, vitamin D",
    },
    {
        "id": "S5_SKIM_MILK",
        "en": "Valio skimmed milk 1 L",
        "fi": "Valio rasvaton maito 1 L",
        "brand": "Valio",
        "category": "10400",
        "price": 1.05,
        "stock": 3000,
        "contains": ["AM", "ML"],
        "claims": ["FREE_FROM_FAT"],
        "ingredients": "pasteurized skimmed milk, vitamin D",
    },
    {
        "id": "S5_CREAM",
        "en": "Valio cooking cream 1 L",
        "fi": "Valio ruokakerma 1 L",
        "brand": "Valio",
        "category": "10401",
        "price": 3.10,
        "stock": 1500,
        "contains": ["AM", "ML"],
        "claims": [],
        "ingredients": "cream, milk, modified starch",
    },
    {
        "id": "S5_BUTTER",
        "en": "Valio butter 500 g",
        "fi": "Valio voi 500 g",
        "brand": "Valio",
        "category": "10500",
        "price": 3.80,
        "stock": 1500,
        "contains": ["AM", "ML"],
        "claims": [],
        "ingredients": "cream, salt, lactic acid culture",
    },
    {
        "id": "S5_HAZELNUT_SPREAD",
        "en": "Hazelnut spread 1 kg",
        "fi": "Hasselpähkinälevite 1 kg",
        "brand": "Nordic Nut",
        "category": "21450",
        "price": 6.50,
        "stock": 800,
        "contains": ["SH"],
        "claims": ["VEGAN"],
        "ingredients": "sugar, rapeseed oil, roasted hazelnuts, cocoa",
    },
    {
        "id": "S5_HAZELNUT_MAY_OAT",
        "en": "Hazelnut flavour oat drink 1 L",
        "fi": "Hasselpähkinä kaurajuoma 1 L",
        "brand": "Nordic Nut",
        "category": "10402",
        "price": 2.10,
        "stock": 800,
        "contains": ["AW", "GO"],
        "may_contain": ["SH"],
        "claims": ["VEGAN"],
        "ingredients": "water, oats, natural flavouring",
    },
    {
        "id": "S5_PEANUT_PASTE",
        "en": "Roasted peanut paste 1 kg",
        "fi": "Paahdettu maapähkinätahna 1 kg",
        "brand": "Nordic Nut",
        "category": "21450",
        "price": 5.40,
        "stock": 800,
        "contains": ["AP"],
        "claims": ["VEGAN"],
        "ingredients": "roasted peanuts, salt",
    },
    {
        "id": "S5_PEANUT_NUT_MIX",
        "en": "Peanut hazelnut snack mix 500 g",
        "fi": "Maapähkinä hasselpähkinä sekoitus 500 g",
        "brand": "Nordic Nut",
        "category": "21450",
        "price": 4.90,
        "stock": 800,
        "contains": ["AP", "SH"],
        "claims": ["VEGAN"],
        "ingredients": "roasted peanuts, hazelnuts, salt",
    },
    {
        "id": "S5_SUNFLOWER_SPREAD",
        "en": "Sunflower seed spread 1 kg",
        "fi": "Auringonkukkalevite 1 kg",
        "brand": "SafeBake",
        "category": "21450",
        "price": 5.80,
        "stock": 800,
        "contains": [],
        "claims": ["VEGAN"],
        "ingredients": "sunflower seeds, rapeseed oil, salt",
    },
    {
        "id": "473328",
        "en": "Schar Vital dark sliced bread 350 g gluten-free",
        "fi": "Schär Vital tumma viipaleleipä 350 g gluteeniton",
        "brand": "Schar",
        "category": "11100",
        "price": 3.90,
        "stock": 1500,
        "contains": [],
        "claims": ["FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE", "VEGAN"],
        "ingredients": "buckwheat sourdough, rice flour, water, sunflower seeds, yeast, salt",
    },
    {
        "id": "S5_GF_WHITE",
        "en": "Gluten-free white bread 500 g",
        "fi": "Gluteeniton vaalea leipä 500 g",
        "brand": "Schar",
        "category": "11100",
        "price": 4.10,
        "stock": 1500,
        "contains": [],
        "claims": ["FREE_FROM_GLUTEN", "VEGAN"],
        "ingredients": "rice flour, potato starch, water, psyllium, yeast, salt",
    },
    {
        "id": "S5_GF_FLOUR",
        "en": "Gluten-free flour mix 1 kg",
        "fi": "Gluteeniton jauhoseos 1 kg",
        "brand": "Schar",
        "category": "21310",
        "price": 3.50,
        "stock": 1500,
        "contains": [],
        "claims": ["FREE_FROM_GLUTEN", "VEGAN"],
        "ingredients": "rice flour, potato starch, corn starch, psyllium",
    },
    {
        "id": "S5_RYE_BREAD",
        "en": "Rye bread 500 g sliced",
        "fi": "Ruisleipä 500 g viipaloitu",
        "brand": "Vaasan",
        "category": "11100",
        "price": 2.20,
        "stock": 2000,
        "contains": ["AW", "NR"],
        "claims": ["VEGAN"],
        "ingredients": "wholegrain rye flour, water, yeast, salt",
    },
    {
        "id": "S5_MAY_GLUTEN_BREAD",
        "en": "Buckwheat loaf bread 500 g",
        "fi": "Tattarileipä 500 g",
        "brand": "Vaasan",
        "category": "11100",
        "price": 3.40,
        "stock": 1000,
        "contains": [],
        "may_contain": ["UW"],
        "claims": ["VEGAN"],
        "ingredients": "buckwheat flour, water, yeast, salt",
    },
    {
        "id": "S5_UNK_OAT",
        "en": "Unverified farm oat drink 1 L",
        "fi": "Tilakaurajuoma 1 L",
        "brand": "LocalFarm",
        "category": "10402",
        "price": 1.40,
        "stock": 2000,
        "unverified": True,
        "claims": [],
        "ingredients": "water, oats, rapeseed oil, salt",
    },
    {
        "id": "S5_UNK_BREAD",
        "en": "Unverified village bread 500 g",
        "fi": "Kyläleipä 500 g",
        "brand": "LocalFarm",
        "category": "11100",
        "price": 1.50,
        "stock": 2000,
        "unverified": True,
        "claims": [],
        "ingredients": "rice flour, water, yeast, salt",
    },
    {
        "id": "S5_CONFLICT_ALMOND_OAT",
        "en": "Barista oat drink 1 L",
        "fi": "Barista kaurajuoma 1 L",
        "brand": "ConflictCo",
        "category": "10402",
        "price": 1.30,
        "stock": 2000,
        "contains": ["AW", "GO"],
        "claims": ["VEGAN"],
        "ingredients": "water, oats, roasted almonds, rapeseed oil, salt",
    },
    {
        "id": "S5_CONFLICT_MILK_OAT",
        "en": "Smooth oat drink 1 L",
        "fi": "Pehmeä kaurajuoma 1 L",
        "brand": "ConflictCo",
        "category": "10402",
        "price": 1.35,
        "stock": 2000,
        "contains": ["AW", "GO"],
        "claims": [],
        "ingredients": "water, oats, whole milk powder, salt",
    },
    {
        "id": "S5_CONFLICT_GLUTEN_BREAD",
        "en": "Seeded loaf bread 500 g",
        "fi": "Siemenleipä 500 g",
        "brand": "ConflictCo",
        "category": "11100",
        "price": 1.60,
        "stock": 2000,
        "contains": [],
        "claims": ["VEGAN"],
        "ingredients": "water, wheat flour, rye, sunflower seeds, yeast, salt",
    },
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _to_synkka_json(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in items:
        cls: list[dict[str, Any]] = []
        if not p.get("unverified"):
            contains = set(p.get("contains") or [])
            may = set(p.get("may_contain") or [])
            groups_present = {
                g for g, (_, members) in GS1_GROUPS.items()
                if (contains | may) & set(members)
            }
            allergen_vals = [{"id": c, "unit": "CONTAINS"} for c in sorted(contains)]
            allergen_vals += [{"id": c, "unit": "MAY_CONTAIN"} for c in sorted(may)]
            allergen_vals += [
                {"id": GS1_GROUPS[g][0][0], "unit": "FREE_FROM"}
                for g in GS1_GROUPS
                if g not in groups_present
            ]
            cls.append({"name": "allergen", "values": allergen_vals})
        if p.get("claims"):
            cls.append({"name": "nutritionalClaim", "values": [{"id": c} for c in p["claims"]]})
        out.append(
            {
                "id": p["id"],
                "deleted": False,
                "salesUnit": "ST",
                "baseUnit": "ST",
                "category": p["category"],
                "vendorName": "Synthetic S5 Wholesaler",
                "allowedLotSize": 1,
                "countryOfOrigin": "FI",
                "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
                "synkkaData": {
                    "names": [
                        {"language": "en", "value": p["en"]},
                        {"language": "fi", "value": p["fi"]},
                    ],
                    "brand": p["brand"],
                    "classifications": cls,
                    "keyIngredients": [{"language": "en", "value": p["ingredients"]}],
                    "marketingTexts": [{"language": "en", "value": f"{p['en']} synthetic catalog item."}],
                },
            }
        )
    return out


class _SyntheticBackend:
    base_url = "static://s5-synthetic"
    ttl_s = 300

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.products = {p["id"]: p for p in items}
        self.stats = {"upstream_calls": 0}

    def _wrap(self, rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        return {
            **meta,
            "products": [{**r, "_fetched_at": now, "_meta": meta} for r in rows],
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
        rows = []
        for pid in product_ids:
            p = self.products.get(str(pid))
            if p is None:
                rows.append({"productId": str(pid), "error": True, "errorMessage": "unknown product"})
                continue
            net = float(p["price"])
            gross = round(net * (1 + TAX / 100), 2)
            qty, mult = parse_name_quantity(p["en"])
            item: dict[str, Any] = {
                "productId": p["id"],
                "quantity": 1.0,
                "catalogPrice": {
                    "value": net,
                    "valueWithTax": gross,
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "currency": "EUR",
                },
                "priceWithTax": {
                    "value": gross,
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "currency": "EUR",
                },
                "listPrice": {
                    "value": net,
                    "valueWithTax": gross,
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "currency": "EUR",
                },
                "tax": {"taxRate": TAX, "taxValue": round(gross - net, 2)},
                "netPriceFound": False,
                "error": False,
            }
            if qty is not None and qty.amount > 0:
                amt = qty.amount * (mult or 1)
                item["comparisonPrice"] = {
                    "value": round(net / amt, 4),
                    "valueWithTax": round(gross / amt, 4),
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "currency": "EUR",
                }
                item["comparisonUnit"] = qty.unit
            rows.append(item)
        return self._wrap(
            rows,
            {
                "customerId": customer or "demo",
                "currency": "EUR",
                "pricingDate": _iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )

    def availability(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        rows = []
        for pid in product_ids:
            p = self.products.get(str(pid))
            if p is None:
                continue
            close = datetime.combine(
                requested_date - timedelta(days=1),
                datetime.min.time(),
                HKI,
            ).replace(hour=14)
            rows.append(
                {
                    "productId": p["id"],
                    "closingTime": _iso(close),
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": float(p["stock"]),
                            "availableDate": requested_date.isoformat(),
                            "salesUnit": "ST",
                        }
                    ],
                }
            )
        return self._wrap(
            rows,
            {
                "timeNow": _iso(datetime.now(HKI)),
                "requestedDate": _iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )


def _delivery_day() -> str:
    return sc_common.next_delivery_day().isoformat()


@pytest.fixture(scope="module")
def _s5_static(tmp_path_factory):
    base_dir = tmp_path_factory.mktemp("s5_static")
    pjson = base_dir / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_to_synkka_json(SYNTHETIC_ITEMS), ensure_ascii=False), encoding="utf-8")
    cat_db = base_dir / "catalog.sqlite"
    key_dir = base_dir / "keys"
    rel_csv = base_dir / "rel.csv"
    co2_csv = base_dir / "co2.csv"

    orig_pjson = catalog_store.PRODUCT_JSON
    orig_rel = catalog_store.RELIABILITY_CSV
    orig_co2 = catalog_store.CO2_CSV
    orig_cat = catalog_store.CATALOG_DB
    catalog_store.PRODUCT_JSON = pjson
    catalog_store.RELIABILITY_CSV = rel_csv
    catalog_store.CO2_CSV = co2_csv
    catalog_store.CATALOG_DB = cat_db
    try:
        catalog_store.load_catalog(cat_db, force=True)
    finally:
        catalog_store.PRODUCT_JSON = orig_pjson
        catalog_store.RELIABILITY_CSV = orig_rel
        catalog_store.CO2_CSV = orig_co2
        catalog_store.CATALOG_DB = orig_cat

    app = install(create_app(with_mcp=False))
    backend = _SyntheticBackend(SYNTHETIC_ITEMS)
    return base_dir, pjson, cat_db, key_dir, rel_csv, co2_csv, app, backend


@pytest.fixture
def s5_env(_s5_static, tmp_path, monkeypatch):
    base_dir, pjson, cat_db, key_dir, rel_csv, co2_csv, app, backend = _s5_static
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir(parents=True, exist_ok=True)
    for yf in (ROOT / "aimo" / "trust" / "policies").glob("*.yaml"):
        shutil.copy2(yf, pol_dir / yf.name)

    state_db = tmp_path / "state.sqlite"

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", base_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(config, "CO2_CSV", co2_csv)
    monkeypatch.setattr(config, "CATALOG_DB", cat_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(catalog_store, "CO2_CSV", co2_csv)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cat_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(trust_policy, "POLICY_DIR", pol_dir)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])

    prev_client = client_mod._client
    client_mod.set_client(backend)
    trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    try:
        with TestClient(app) as tc:
            yield tc, pol_dir
    finally:
        client_mod.set_client(prev_client)
        trust_policy._load_policy_cached.cache_clear()
        allergens_mod._last_policy_dir = None
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def test_s5_allergen_safe_scenario_runs_end_to_end_offline(s5_env, monkeypatch):
    tc, _ = s5_env

    class _InProcessHttpClient:
        def __init__(self, *, base_url: str = "", headers: dict[str, str] | None = None, **_: Any) -> None:
            self._headers = dict(headers or {})

        def request(self, method: str, path: str, **kw: Any):
            merged = {**self._headers, **dict(kw.pop("headers", None) or {})}
            return tc.request(method, path, headers=merged, **kw)

    monkeypatch.setattr(sc_common.httpx, "Client", _InProcessHttpClient)
    run_res = sc_common.Run("s5_allergen_safe", "http://testserver", quiet=True).guard(s5_allergen_safe.run)
    failures = [item for item in run_res.results if not item[1]]
    assert run_res.ok is True, failures
    assert failures == []
    assert len(run_res.results) >= 14

    labels = {label for label, _, _ in run_res.results}
    assert "school plan has lines" in labels
    assert "school milk plan (omitted excluded): zero dairy/nut lines, policy hard-block active" in labels
    assert "school milk plan (omitted excluded): every non-rejected candidate structured FREE of milk and nuts" in labels
    assert "school oat drink plan (omitted excluded): has lines and every line structured FREE of milk and nuts" in labels
    assert "hospital bread plan (omitted excluded): has lines and every line structured FREE of gluten" in labels

    audit_ver = tc.get("/v1/audit/verify").json()
    assert audit_ver["ok"] is True
    assert audit_ver["broken_at"] is None


@pytest.mark.parametrize(
    "query,unit,hotel_expected_ids",
    [
        ("whole milk", "L", {"S5_WHOLE_MILK"}),
        ("skimmed milk", "L", {"S5_SKIM_MILK"}),
        ("milk", "L", {"463038", "464306", "S5_WHOLE_MILK", "S5_SKIM_MILK"}),
        ("lactose-free milk", "L", {"463038", "464306"}),
        ("cream", "L", {"S5_CREAM"}),
        ("butter", "KG", {"S5_BUTTER"}),
    ],
)
def test_school_omitted_exclusions_hard_blocks_all_dairy_variants(
    s5_env, query: str, unit: str, hotel_expected_ids: set[str]
):
    tc, _ = s5_env
    day = _delivery_day()

    school_resp = tc.post(
        "/v1/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "school-tampere",
            "product_query": query,
            "quantity": 20,
            "unit": unit,
            "delivery_by": day,
        },
    )
    assert school_resp.status_code == 200, school_resp.text
    school_plan = school_resp.json()
    assert {"milk", "nuts"} <= set(school_plan["need"]["excluded_allergens"])
    assert "Customer policy (school-tampere) excludes milk, nuts" in school_plan["explanation"]

    dairy_ids = {"463038", "464306", "S5_WHOLE_MILK", "S5_SKIM_MILK", "S5_CREAM", "S5_BUTTER"}
    school_line_ids = {ln["product_id"] for ln in school_plan["lines"]}
    assert school_line_ids.isdisjoint(dairy_ids)
    for ln in school_plan["lines"]:
        prod = tc.get(f"/v1/products/{ln['product_id']}").json()
        assert prod["allergens"]["milk"] == "free"
        assert prod["allergens"]["nuts"] == "free"
        assert prod["allergen_source"]["milk"].startswith("structured")
        assert prod["allergen_source"]["nuts"].startswith("structured")

    for cand in school_plan["candidates"]:
        cid = cand["product"]["id"]
        if cid in dairy_ids:
            assert cand["tier"] == "rejected"
        if cand["tier"] != "rejected":
            prod = tc.get(f"/v1/products/{cid}").json()
            assert prod["allergens"]["milk"] == "free"
            assert prod["allergens"]["nuts"] == "free"

    hotel_resp = tc.post(
        "/v1/plan",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "hotel-aurora",
            "product_query": query,
            "quantity": 10,
            "unit": unit,
            "delivery_by": day,
        },
    )
    assert hotel_resp.status_code == 200, hotel_resp.text
    hotel_plan = hotel_resp.json()
    hotel_line_ids = {ln["product_id"] for ln in hotel_plan["lines"]}
    assert hotel_line_ids & hotel_expected_ids


def test_school_hazelnut_and_peanut_hard_blocks_and_policy_union(s5_env):
    tc, pol_dir = s5_env
    day = _delivery_day()
    school_headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}

    for query in ("hazelnut spread", "peanut hazelnut snack"):
        resp = tc.post(
            "/v1/plan",
            headers=school_headers,
            json={
                "customer_id": "school-tampere",
                "product_query": query,
                "quantity": 5,
                "unit": "KG",
                "delivery_by": day,
            },
        )
        assert resp.status_code == 200, resp.text
        plan = resp.json()
        assert {"milk", "nuts"} <= set(plan["need"]["excluded_allergens"])
        assert plan["lines"] == []
        by_id = {c["product"]["id"]: c for c in plan["candidates"]}
        for nid in ("S5_HAZELNUT_SPREAD", "S5_PEANUT_NUT_MIX"):
            if nid in by_id:
                assert by_id[nid]["tier"] == "rejected"

    peanut_union = tc.post(
        "/v1/plan",
        headers=school_headers,
        json={
            "customer_id": "school-tampere",
            "product_query": "roasted peanut paste",
            "excluded_allergens": ["peanut"],
            "quantity": 5,
            "unit": "KG",
            "delivery_by": day,
        },
    )
    assert peanut_union.status_code == 200, peanut_union.text
    p_plan = peanut_union.json()
    assert set(p_plan["need"]["excluded_allergens"]) == {"peanuts", "milk", "nuts"}
    assert p_plan["lines"] == []
    p_cands = {c["product"]["id"]: c for c in p_plan["candidates"]}
    assert p_cands["S5_PEANUT_PASTE"]["tier"] == "rejected"
    assert "peanuts" in " ".join(p_cands["S5_PEANUT_PASTE"]["reasons"]).lower()

    (pol_dir / "nursery-espoo.yaml").write_text(
        "\n".join(
            [
                "policy_id: nursery-espoo",
                'version: "1.0.0"',
                "customer:",
                "  id: nursery-espoo",
                "  name: Espoo Daycare Kitchen",
                "agents:",
                "  - agent_id: nursery-espoo/buyer-1",
                "    scopes: [catalog:read, quote:create, order:submit]",
                "rules:",
                "  - id: mandate_scope",
                "    type: scope",
                "    required_scopes: [order:submit]",
                "  - id: allergens",
                "    type: allergen_block",
                "    excluded: [milk, nuts, peanut]",
                "    reason: severe dairy, tree nut and peanut allergies",
                "    on_contains: reject",
                "    on_may_contain: reject",
                "    on_unknown: reject",
            ]
        ),
        encoding="utf-8",
    )
    trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None

    nursery_resp = tc.post(
        "/v1/plan",
        headers={"X-Customer-Id": "nursery-espoo", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "nursery-espoo",
            "product_query": "spread",
            "quantity": 5,
            "unit": "KG",
            "delivery_by": day,
        },
    )
    assert nursery_resp.status_code == 200, nursery_resp.text
    n_plan = nursery_resp.json()
    assert set(n_plan["need"]["excluded_allergens"]) == {"milk", "nuts", "peanuts"}
    assert [ln["product_id"] for ln in n_plan["lines"]] == ["S5_SUNFLOWER_SPREAD"]
    n_cands = {c["product"]["id"]: c for c in n_plan["candidates"]}
    for unsafe_id in ("S5_HAZELNUT_SPREAD", "S5_PEANUT_PASTE", "S5_PEANUT_NUT_MIX"):
        if unsafe_id in n_cands:
            assert n_cands[unsafe_id]["tier"] == "rejected"


def test_unknown_allergen_and_data_conflict_products_blocked_in_school_and_hospital_plans(s5_env):
    tc, _ = s5_env
    day = _delivery_day()

    unk_oat = tc.get("/v1/products/S5_UNK_OAT").json()
    assert unk_oat["allergens"]["milk"] == "unknown"
    assert unk_oat["allergens"]["nuts"] == "unknown"
    assert unk_oat["allergen_source"]["milk"] == "none"

    conflict_almond = tc.get("/v1/products/S5_CONFLICT_ALMOND_OAT").json()
    assert conflict_almond["allergens"]["nuts"] == "unknown"
    assert conflict_almond["allergen_source"]["nuts"] == "contradiction"

    conflict_milk = tc.get("/v1/products/S5_CONFLICT_MILK_OAT").json()
    assert conflict_milk["allergens"]["milk"] == "unknown"
    assert conflict_milk["allergen_source"]["milk"] == "contradiction"

    conflict_gluten = tc.get("/v1/products/S5_CONFLICT_GLUTEN_BREAD").json()
    assert conflict_gluten["allergens"]["gluten"] == "unknown"
    assert conflict_gluten["allergen_source"]["gluten"] == "contradiction"

    school_oat = tc.post(
        "/v1/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "school-tampere",
            "product_query": "oat drink",
            "quantity": 40,
            "unit": "L",
            "delivery_by": day,
        },
    ).json()
    assert {"milk", "nuts"} <= set(school_oat["need"]["excluded_allergens"])
    oat_line_ids = {ln["product_id"] for ln in school_oat["lines"]}
    assert oat_line_ids
    assert oat_line_ids <= {"472833", "S5_OAT_ALT"}

    oat_cands = {c["product"]["id"]: c for c in school_oat["candidates"]}
    for blocked_id in ("S5_UNK_OAT", "S5_CONFLICT_ALMOND_OAT", "S5_CONFLICT_MILK_OAT", "S5_HAZELNUT_MAY_OAT"):
        assert blocked_id in oat_cands
        assert oat_cands[blocked_id]["tier"] == "rejected"
    assert "allergen_unknown" in oat_cands["S5_UNK_OAT"]["flags"]
    assert "allergen_data_conflict" in oat_cands["S5_CONFLICT_ALMOND_OAT"]["flags"]
    assert "allergen_unknown" in oat_cands["S5_CONFLICT_ALMOND_OAT"]["flags"]
    assert "allergen_data_conflict" in oat_cands["S5_CONFLICT_MILK_OAT"]["flags"]

    hosp_bread = tc.post(
        "/v1/plan",
        headers={"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "bread",
            "quantity": 10,
            "unit": "KG",
            "delivery_by": day,
        },
    ).json()
    assert hosp_bread["need"]["excluded_allergens"] == ["gluten"]
    bread_line_ids = {ln["product_id"] for ln in hosp_bread["lines"]}
    assert bread_line_ids
    assert bread_line_ids <= {"473328", "S5_GF_WHITE"}

    bread_cands = {c["product"]["id"]: c for c in hosp_bread["candidates"]}
    for blocked_id in ("S5_RYE_BREAD", "S5_MAY_GLUTEN_BREAD", "S5_UNK_BREAD", "S5_CONFLICT_GLUTEN_BREAD"):
        assert blocked_id in bread_cands
        assert bread_cands[blocked_id]["tier"] == "rejected"
    assert "allergen_unknown" in bread_cands["S5_UNK_BREAD"]["flags"]
    assert "allergen_data_conflict" in bread_cands["S5_CONFLICT_GLUTEN_BREAD"]["flags"]


@pytest.mark.parametrize(
    "customer,pid,expected_allergen_hit_outcome,expected_word",
    [
        ("school-tampere", "S5_WHOLE_MILK", "reject", "milk"),
        ("school-tampere", "S5_SKIM_MILK", "reject", "milk"),
        ("school-tampere", "464306", "reject", "milk"),
        ("school-tampere", "S5_CREAM", "reject", "milk"),
        ("school-tampere", "S5_BUTTER", "reject", "milk"),
        ("school-tampere", "S5_HAZELNUT_SPREAD", "reject", "nuts"),
        ("school-tampere", "S5_HAZELNUT_MAY_OAT", "reject", "nuts"),
        ("school-tampere", "S5_UNK_OAT", "reject", "unknown"),
        ("school-tampere", "S5_CONFLICT_ALMOND_OAT", "reject", "unknown"),
        ("hospital-meilahti", "S5_RYE_BREAD", "reject", "gluten"),
        ("hospital-meilahti", "S5_MAY_GLUTEN_BREAD", "reject", "gluten"),
        ("hospital-meilahti", "S5_WFLOUR", "reject", "gluten"),
        ("hospital-meilahti", "S5_UNK_BREAD", "needs_approval", "unknown"),
        ("hospital-meilahti", "S5_CONFLICT_GLUTEN_BREAD", "needs_approval", "unknown"),
    ],
)
def test_forced_unsafe_orders_rejected_by_policy(
    s5_env,
    customer: str,
    pid: str,
    expected_allergen_hit_outcome: str,
    expected_word: str,
):
    tc, _ = s5_env
    day = _delivery_day()
    headers = {"X-Customer-Id": customer, "X-Agent-Id": "buyer-1"}

    q_resp = tc.post(
        "/v1/quotes",
        headers=headers,
        json={"delivery_date": day, "lines": [{"product_id": pid, "qty_sales_units": 1}]},
    )
    assert q_resp.status_code == 200, q_resp.text
    quote = q_resp.json()
    assert quote["lines"][0]["tier"] == "rejected"

    o_resp = tc.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert o_resp.status_code == 200, o_resp.text
    order = o_resp.json()
    assert order["status"] == "rejected"
    assert order["decision"]["outcome"] == "reject"

    allergen_hits = [
        h for h in order["decision"]["hits"]
        if h["rule_id"].startswith("allergens") and h["outcome"] == expected_allergen_hit_outcome
    ]
    assert allergen_hits
    assert expected_word in allergen_hits[0]["message"].lower()
