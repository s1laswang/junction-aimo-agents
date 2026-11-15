"""Tests for prompt-injection screening surfaced in recipe plans and FakeGateway."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import fakes
from aimo.buyer.fakes import FakeGateway
from aimo.catalog import textsearch
from aimo.gateway.app import create_app
from aimo.recipes import plan_recipe

FIX = Path(__file__).resolve().parent / "fixtures"
DELIVERY = date(2026, 11, 16)
HEADERS = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}


class _RecipeFixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        self.price_by_id = {p["productId"]: p for p in self.price_raw["products"]}
        self.avail_by_id = {p["productId"]: p for p in self.avail_raw["products"]}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in self.price_raw.items() if k != "products"}
        prods = []
        for pid in ids:
            if pid in self.price_by_id:
                base = self.price_by_id[pid]
            else:
                base = {
                    "productId": pid,
                    "quantity": 1.0,
                    "catalogPrice": {
                        "value": 3.50,
                        "valueWithTax": 3.99,
                        "validFrom": "2026-09-01T00:00:00+0300",
                        "validTo": "2026-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "priceWithTax": {
                        "value": 3.99,
                        "validFrom": "2026-09-01T00:00:00+0300",
                        "validTo": "2026-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "comparisonPrice": {
                        "value": 3.50,
                        "valueWithTax": 3.99,
                        "validFrom": "2026-09-01T00:00:00+0300",
                        "validTo": "2026-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "comparisonUnit": "KG",
                    "netPriceFound": False,
                    "error": False,
                }
            prods.append({**base, "_fetched_at": now, "_meta": meta})
        return {**meta, "products": prods, "_oldest_fetch": now}

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        meta = {k: v for k, v in self.avail_raw.items() if k != "products"}
        req_iso = requested_date.isoformat()
        prods = []
        for pid in ids:
            if pid in self.avail_by_id:
                base = self.avail_by_id[pid]
            else:
                base = {
                    "productId": pid,
                    "closingTime": "2026-11-15T17:00:00+0200",
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": 5000.0,
                            "availableDate": req_iso,
                            "salesUnit": "ST",
                        }
                    ],
                }
            prods.append({**base, "_fetched_at": now, "_meta": meta})
        return {**meta, "products": prods, "_oldest_fetch": now}


def _synthetic_catalog_products() -> list[dict[str, Any]]:
    def _item(
        pid: str,
        sales: str,
        category: str,
        name_en: str,
        brand: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
        claims: list[str],
        allergens: list[str],
        marketing: str,
        ingredients: str,
    ) -> dict[str, Any]:
        cls: list[dict[str, Any]] = []
        if allergens:
            cls.append({"name": "allergen", "type": "ENUM", "values": [{"id": a, "unit": "CONTAINS"} for a in allergens]})
        if claims:
            cls.append({"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": c, "unit": "FREE_FROM"} for c in claims]})
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": category,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": cls,
                "keyIngredients": [{"language": "en", "value": ingredients}],
                "marketingTexts": [{"language": "en", "value": marketing}],
            },
        }

    vol_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    wt_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]
    wt_500g = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 0.5}}]
    return [
        _item(
            "463038",
            "KI",
            "10400",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            [("KI", 2), ("ST", 1)],
            [{"synkkaPackagingType": "CASE"}],
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            "Valio Eila lactose free dispenser milk drink for hotels.",
            "milk, vitamin D",
        ),
        _item(
            "464306",
            "ST",
            "10400",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            [("ST", 1)],
            vol_1l,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            "Valio Eila lactose free milk drink 1 L.",
            "milk, vitamin D",
        ),
        _item(
            "469363",
            "ST",
            "10400",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            [("ST", 1)],
            vol_1l,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            "Arla lactose free semi-skimmed milk drink 1 L UHT.",
            "milk, vitamin D",
        ),
        _item(
            "900101",
            "ST",
            "21370",
            "Elovena oat flakes 1 kg",
            "Elovena",
            [("ST", 1)],
            wt_1kg,
            [],
            ["AW"],
            "Wholegrain Finnish oat flakes for porridge.",
            "wholegrain oats",
        ),
        _item(
            "402105",
            "ST",
            "10500",
            "Valio Eila lactose-free butter 500 g",
            "Valio",
            [("ST", 1)],
            wt_500g,
            ["FREE_FROM_LACTOSE"],
            ["AM"],
            "Lactose free Finnish butter for cooking and baking.",
            "cream, salt",
        ),
    ]


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    pjson = tmp_path / "valio_aimo_junction26_product_data.json"
    cdb = tmp_path / "catalog.sqlite"
    pjson.write_text(json.dumps(_synthetic_catalog_products()), encoding="utf-8")
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    bc.set_client(_RecipeFixtureClient())
    try:
        yield
    finally:
        bc.set_client(None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def test_plan_recipe_surfaces_injection_screen_clean_single_and_wildcard(isolated_env: None) -> None:
    r_clean = plan_recipe("oat-porridge", 40, DELIVERY, "hotel-aurora", include_candidates=False)
    assert r_clean["injection_flagged_lines"] == []
    assert "prompt-injection screen" not in r_clean["summary"].lower()
    assert len(r_clean["ingredients"]) == 3
    for ing in r_clean["ingredients"]:
        assert ing["plan"]["lines"]
        for ln in ing["plan"]["lines"]:
            assert isinstance(ln["injection_screen"], dict)
            assert ln["injection_screen"]["suspicious"] is False
            assert "supplier_text_injection_suspected" not in ln["flags"]
        assert ing["plan"]["candidates"]
        for cand in ing["plan"]["candidates"]:
            assert isinstance(cand["injection_screen"], dict)
            assert cand["injection_screen"]["suspicious"] is False
            assert "supplier_text_injection_suspected" not in cand["flags"]
            assert not any("prompt-injection screen" in r.lower() for r in cand["reasons"])

    tok_one = bc.set_request_faults("inject_text:464306")
    try:
        r_one = plan_recipe("oat-porridge", 40, DELIVERY, "hotel-aurora", include_candidates=False)
    finally:
        bc.reset_request_faults(tok_one)

    assert len(r_one["injection_flagged_lines"]) == 1
    flagged_one = r_one["injection_flagged_lines"][0]
    assert flagged_one["ingredient"] == "milk"
    assert flagged_one["product_id"] == "464306"
    assert flagged_one["injection_screen"]["suspicious"] is True
    assert "supplier_text_injection_suspected" in flagged_one["flags"]
    assert r_one["summary"].endswith(
        "; prompt-injection screen flagged supplier text on 464306 (human approval required)."
    )

    tok_all = bc.set_request_faults("inject_text:*")
    try:
        r_all = plan_recipe("oat-porridge", 40, DELIVERY, "hotel-aurora", include_candidates=False)
    finally:
        bc.reset_request_faults(tok_all)

    assert len(r_all["injection_flagged_lines"]) >= 3
    for item in r_all["injection_flagged_lines"]:
        assert item["injection_screen"]["suspicious"] is True
        assert "supplier_text_injection_suspected" in item["flags"]

    for ing in r_all["ingredients"]:
        for ln in ing["plan"]["lines"]:
            assert ln["injection_screen"]["suspicious"] is True
            assert ln["injection_screen"].get("injected") is True
            assert "supplier_text_injection_suspected" in ln["flags"]
            assert "injected:inject_text" in ln["flags"]
            assert any("prompt-injection screen" in r.lower() for r in ln["reasons"])
        for cand in ing["plan"]["candidates"]:
            assert isinstance(cand["injection_screen"], dict)
            assert cand["injection_screen"]["suspicious"] is True
            assert "supplier_text_injection_suspected" in cand["flags"]
            assert "injected:inject_text" in cand["flags"]
            assert any("prompt-injection screen" in r.lower() for r in cand["reasons"])
            assert len(cand["reasons"]) == len(set(cand["reasons"]))

    expected_pids = list(dict.fromkeys(x["product_id"] for x in r_all["injection_flagged_lines"]))
    assert r_all["summary"].endswith(
        f"; prompt-injection screen flagged supplier text on {', '.join(expected_pids)} (human approval required)."
    )


def test_post_v1_recipes_plan_clean_and_injected(isolated_env: None) -> None:
    api = TestClient(create_app(with_mcp=False))
    payload = {"recipe_id": "oat-porridge", "portions": 40, "delivery_by": "2026-11-16", "include_candidates": False}

    resp_clean = api.post("/v1/recipes/plan", headers=HEADERS, json=payload)
    assert resp_clean.status_code == 200, resp_clean.text
    body_clean = resp_clean.json()
    assert body_clean["injection_flagged_lines"] == []
    assert "prompt-injection screen" not in body_clean["summary"].lower()
    for ing in body_clean["ingredients"]:
        for ln in ing["plan"]["lines"]:
            assert ln["injection_screen"]["suspicious"] is False
            assert "supplier_text_injection_suspected" not in ln["flags"]
        for cand in ing["plan"]["candidates"]:
            assert cand["injection_screen"]["suspicious"] is False
            assert "supplier_text_injection_suspected" not in cand["flags"]

    hdr_inj = {**HEADERS, "X-Aimo-Faults": "inject_text:*"}
    resp_inj = api.post("/v1/recipes/plan", headers=hdr_inj, json=payload)
    assert resp_inj.status_code == 200, resp_inj.text
    body_inj = resp_inj.json()
    assert body_inj["injection_flagged_lines"]
    assert "; prompt-injection screen flagged supplier text on " in body_inj["summary"]
    assert body_inj["summary"].endswith("(human approval required).")
    for ing in body_inj["ingredients"]:
        for ln in ing["plan"]["lines"]:
            assert ln["injection_screen"]["suspicious"] is True
            assert "supplier_text_injection_suspected" in ln["flags"]
        for cand in ing["plan"]["candidates"]:
            assert cand["injection_screen"]["suspicious"] is True
            assert "supplier_text_injection_suspected" in cand["flags"]
            assert any("prompt-injection screen" in r.lower() for r in cand["reasons"])


def test_fake_gateway_injection_scenario_plan_quote_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    gw_clean = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy")
    clean_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 100,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "preferred_product_ids": ["463038"],
    }
    clean_plan = gw_clean.plan_need(clean_need)
    assert clean_plan["explanation"] == ""
    for cand in clean_plan["candidates"]:
        assert cand["injection_screen"]["suspicious"] is False
        assert "supplier_text_injection_suspected" not in cand["flags"]
    for ln in clean_plan["lines"]:
        assert ln["injection_screen"]["suspicious"] is False
        assert "supplier_text_injection_suspected" not in ln["flags"]

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="injection")
    split_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 5200,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "preferred_product_ids": ["463038"],
    }
    split_plan = gw.plan_need(split_need)
    cands_by_id = {c["product"]["id"]: c for c in split_plan["candidates"]}
    lines_by_id = {l["product_id"]: l for l in split_plan["lines"]}

    assert "464306" in cands_by_id and "469363" in cands_by_id
    assert cands_by_id["464306"]["injection_screen"]["suspicious"] is False
    assert "supplier_text_injection_suspected" not in cands_by_id["464306"]["flags"]

    c_inj = cands_by_id["469363"]
    assert c_inj["injection_screen"]["suspicious"] is True
    assert c_inj["injection_screen"]["reasons"]
    assert "supplier_text_injection_suspected" in c_inj["flags"]
    assert any("prompt-injection screen" in r.lower() for r in c_inj["reasons"])

    assert "464306" in lines_by_id and "469363" in lines_by_id
    assert lines_by_id["464306"]["injection_screen"]["suspicious"] is False
    ln_inj = lines_by_id["469363"]
    assert ln_inj["injection_screen"]["suspicious"] is True
    assert ln_inj["injection_screen"]["reasons"]
    assert "supplier_text_injection_suspected" in ln_inj["flags"]
    assert any("prompt-injection screen" in r.lower() for r in ln_inj["reasons"])
    assert split_plan["explanation"]
    assert "prompt-injection screen" in split_plan["explanation"].lower()
    assert "469363" in split_plan["explanation"]
    assert "human approval" in split_plan["explanation"].lower()

    price, _amt, model, closing = fakes.OFFERS["464306"]
    monkeypatch.setitem(fakes.OFFERS, "464306", (price, 300, model, closing))
    low_stock_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 400,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "preferred_product_ids": ["463038"],
    }
    low_stock_plan = gw.plan_need(low_stock_need)
    assert {"464306", "469363"} <= {l["product_id"] for l in low_stock_plan["lines"]}

    stripped_lines = [
        {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"], "tier": l["tier"]}
        for l in low_stock_plan["lines"]
    ]
    quote = gw.create_quote("2026-11-16", stripped_lines, need=low_stock_need)
    qlines_by_id = {l["product_id"]: l for l in quote["lines"]}
    assert "supplier_text_injection_suspected" not in qlines_by_id["464306"]["flags"]
    assert "supplier_text_injection_suspected" in qlines_by_id["469363"]["flags"]

    order = gw.place_order(quote["quote_id"], quote["quote_hash"])
    assert order["status"] == "pending_approval"
    assert order["decision"]["outcome"] == "needs_approval"
    hit_ids = [h["rule_id"] for h in order["decision"]["hits"]]
    assert "security.prompt_injection" in hit_ids
    assert hit_ids.count("security.prompt_injection") == 1

    pref_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 20,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "preferred_product_ids": ["469363"],
    }
    pref_plan = gw.plan_need(pref_need)
    assert pref_plan["lines"][0]["product_id"] == "469363"
    assert pref_plan["lines"][0]["tier"] == "preferred"
    assert pref_plan["lines"][0]["injection_screen"]["suspicious"] is True
    q_pref = gw.create_quote(
        "2026-11-16",
        [{"product_id": "469363", "qty_sales_units": 20, "tier": "preferred"}],
        need=pref_need,
    )
    o_pref = gw.place_order(q_pref["quote_id"], q_pref["quote_hash"])
    assert o_pref["status"] == "pending_approval"
    assert [h["rule_id"] for h in o_pref["decision"]["hits"]] == ["security.prompt_injection"]

    gw_clean_flag = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy")
    q_explicit = gw_clean_flag.create_quote(
        "2026-11-16",
        [{"product_id": "464306", "qty_sales_units": 50, "tier": "preferred", "flags": ["supplier_text_injection_suspected"]}],
        need={"customer_id": "hotel-aurora", "preferences": {"injection_flags": [{"tool": "get_product"}]}},
    )
    assert "supplier_text_injection_suspected" in q_explicit["lines"][0]["flags"]
    o_explicit = gw_clean_flag.place_order(q_explicit["quote_id"], q_explicit["quote_hash"])
    assert o_explicit["status"] == "pending_approval"
    explicit_hits = [h["rule_id"] for h in o_explicit["decision"]["hits"]]
    assert explicit_hits.count("security.prompt_injection") == 1
