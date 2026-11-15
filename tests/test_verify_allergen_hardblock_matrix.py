from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import substitution as sub_mod
from aimo.catalog import textsearch
from aimo.gateway import service, verify
from aimo.gateway.verify import (
    excluded_allergens_in_query,
    need_names_excluded_allergen,
    policy_excluded_allergens,
    usual_matches_allergen_query,
    usual_products,
    verify_lines,
)
from aimo.models import AllergenStatus, Candidate, EU14, Need, Offer, Product, ProductSummary

DELIVERY = date(2026, 11, 16)
FETCHED_AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

SCHOOL_USUAL_ROW: dict[str, Any] = {
    "id": "470889",
    "name": "Oatly Oat Drink Whole 1 L",
    "name_en": "Oatly Oat Drink Whole 1 L",
    "name_fi": "Oatly Kaurajuoma Täyteläinen 1 L",
    "name_sv": "Oatly Havredryck Hel 1 L",
    "brand": "Oatly",
    "category": "10400",
    "sales_unit": "ST",
    "content_amount": 1.0,
    "content_unit": "L",
    "claims": ["VEGAN"],
    "allergens": {g: ("contains" if g == "gluten" else "free") for g in EU14},
    "allergen_source": {g: "structured" for g in EU14},
}

HOSPITAL_USUAL_ROW: dict[str, Any] = {
    "id": "473328",
    "name": "Schar Vital dark sliced bread 350 g gluten free",
    "name_en": "Schar Vital dark sliced bread 350 g gluten free",
    "name_fi": "Schar Vital tumma viipaleleipä 350 g gluteeniton",
    "name_sv": "Schar Vital mörkt skivat bröd 350 g glutenfritt",
    "brand": "Schar",
    "category": "14100",
    "sales_unit": "ST",
    "content_amount": 0.35,
    "content_unit": "KG",
    "claims": ["FREE_FROM_GLUTEN"],
    "allergens": {g: "free" for g in EU14},
    "allergen_source": {g: "structured" for g in EU14},
}


def _offer(pid: str, price: float = 1.80, available_amount: float = 100.0) -> Offer:
    return Offer(
        product_id=pid,
        requested_date=DELIVERY,
        available=True,
        available_amount=available_amount,
        sales_unit="ST",
        unit_price=price,
        unit_price_gross=round(price * 1.14, 2),
        comparison_price=price,
        comparison_unit="L",
        fetched_at=FETCHED_AT,
    )


def _clean_screen(_prod: Product) -> dict[str, Any]:
    return {"suspicious": False, "reasons": []}


def _make_product(
    pid: str,
    name: str,
    *,
    unit: str = "L",
    amount: float = 1.0,
    category: str = "10400",
    claims: list[str] | None = None,
    allergens: dict[str, Any] | None = None,
    allergen_source: dict[str, str] | None = None,
    ingredients_text: str | None = None,
) -> Product:
    return Product(
        id=pid,
        name=name,
        brand="TestBrand",
        category=category,
        sales_unit="ST",
        content_amount=amount,
        content_unit=unit,
        claims=list(claims or []),
        allergens=dict(allergens or {}),
        allergen_source=dict(allergen_source or {}),
        ingredients_text=ingredients_text,
    )


def _all_free_except(
    overrides: dict[str, Any] | None = None,
    source_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    amap: dict[str, Any] = {g: AllergenStatus.free for g in EU14}
    smap: dict[str, str] = {g: "structured" for g in EU14}
    if overrides:
        amap.update(overrides)
    if source_overrides:
        smap.update(source_overrides)
    return amap, smap


class _MockBackend:
    base_url = "fixture://verify-matrix-mock"
    ttl_s = 300

    def __init__(self, ids: list[str]) -> None:
        self._ids = set(ids)
        self.stats = {"upstream_calls": 0}

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        return {
            **meta,
            "_oldest_fetch": now,
            "products": [
                {
                    "productId": pid,
                    "salesUnit": "ST",
                    "quantity": 1,
                    "netPriceFound": True,
                    "error": False,
                    "catalogPrice": {"value": 1.80, "currency": "EUR"},
                    "comparisonPrice": {"value": 1.80, "unit": "LTR"},
                    "_fetched_at": now,
                    "_meta": meta,
                }
                for pid in ids
                if pid in self._ids
            ],
        }

    def availability(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        return {
            **meta,
            "_oldest_fetch": now,
            "products": [
                {
                    "productId": pid,
                    "closingTime": "2026-11-15T17:00:00+0200",
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableDate": requested_date.isoformat(),
                            "availableAmount": 250.0,
                            "salesUnit": "ST",
                        }
                    ],
                    "_fetched_at": now,
                    "_meta": meta,
                }
                for pid in ids
                if pid in self._ids
            ],
        }


def test_real_school_and_hospital_policy_exclusions_and_usuals() -> None:
    assert policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert policy_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert policy_excluded_allergens("hotel-aurora") == []
    assert "470889" in usual_products("school-tampere")
    assert "473328" in usual_products("hospital-meilahti")
    assert "463038" in usual_products("hotel-aurora")


@pytest.mark.parametrize(
    "case_id,customer_id,caller_excl,group,allergen_override,source_override,ingredients,expect_unknown,expect_flag,expect_reason_sub",
    [
        (
            "school-milk-contains",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.contains},
            {},
            "milk, vitamin D",
            False,
            "contains:milk",
            "milk: contains per supplier data",
        ),
        (
            "school-nuts-may-contain",
            "school-tampere",
            [],
            "nuts",
            {"nuts": AllergenStatus.may_contain},
            {},
            "water, oats",
            False,
            "may_contain:nuts",
            "nuts: may contain per supplier data",
        ),
        (
            "school-milk-unknown",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.unknown},
            {"milk": "none"},
            "water, oats",
            True,
            "allergen_unknown",
            "milk: no structured allergen data",
        ),
        (
            "school-nuts-missing-key",
            "school-tampere",
            [],
            "nuts",
            {},
            {},
            "water, oats",
            True,
            "allergen_unknown",
            "nuts: no structured allergen data",
        ),
        (
            "school-inferred-gemini-free",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.free},
            {"milk": "inferred:gemini"},
            "water, oats",
            True,
            "allergen_unknown",
            "inferred:gemini",
        ),
        (
            "school-inferred-ingredients-free",
            "school-tampere",
            [],
            "nuts",
            {"nuts": AllergenStatus.free},
            {"nuts": "inferred:ingredients"},
            "water, oats",
            True,
            "allergen_unknown",
            "inferred:ingredients",
        ),
        (
            "school-inferred-gemini-contains",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.contains},
            {"milk": "inferred:gemini"},
            "water, milk powder",
            False,
            "contains:milk",
            "inferred:gemini",
        ),
        (
            "school-contradiction-source-with-free",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.free},
            {"milk": "contradiction"},
            "water, oats",
            True,
            "allergen_unknown",
            "contradiction",
        ),
        (
            "school-contradiction-from-ingredients",
            "school-tampere",
            [],
            "milk",
            {"milk": AllergenStatus.free},
            {"milk": "structured"},
            "water, oats, whole milk powder, salt",
            True,
            "allergen_unknown",
            "whole milk powder",
        ),
        (
            "school-none-source-with-forged-free",
            "school-tampere",
            [],
            "nuts",
            {"nuts": AllergenStatus.free},
            {"nuts": "none"},
            "water, oats",
            True,
            "allergen_unknown",
            "nuts: no structured allergen data",
        ),
        (
            "hospital-gluten-contains",
            "hospital-meilahti",
            [],
            "gluten",
            {"gluten": AllergenStatus.contains},
            {},
            "wheat flour, water",
            False,
            "contains:gluten",
            "gluten: contains per supplier data",
        ),
        (
            "hospital-gluten-may-contain",
            "hospital-meilahti",
            [],
            "gluten",
            {"gluten": AllergenStatus.may_contain},
            {},
            "maize starch, water",
            False,
            "may_contain:gluten",
            "gluten: may contain per supplier data",
        ),
        (
            "hospital-gluten-unknown",
            "hospital-meilahti",
            [],
            "gluten",
            {"gluten": AllergenStatus.unknown},
            {"gluten": "none"},
            "rice flour, water",
            True,
            "allergen_unknown",
            "gluten: no structured allergen data",
        ),
        (
            "hospital-gluten-inferred",
            "hospital-meilahti",
            [],
            "gluten",
            {"gluten": AllergenStatus.free},
            {"gluten": "inferred:gemini"},
            "rice flour, water",
            True,
            "allergen_unknown",
            "inferred:gemini",
        ),
        (
            "hospital-gluten-contradiction-ingredients",
            "hospital-meilahti",
            [],
            "gluten",
            {"gluten": AllergenStatus.free},
            {"gluten": "structured"},
            "water, rye flour, wheat gluten, salt",
            True,
            "allergen_unknown",
            "data conflict",
        ),
        (
            "hotel-caller-soy-alias-unknown",
            "hotel-aurora",
            ["soija"],
            "soy",
            {"soy": AllergenStatus.unknown},
            {"soy": "none"},
            "water, oats",
            True,
            "allergen_unknown",
            "soy: no structured allergen data",
        ),
        (
            "school-caller-peanuts-may-contain",
            "school-tampere",
            ["maapähkinä"],
            "peanuts",
            {"peanuts": AllergenStatus.may_contain},
            {},
            "water, oats",
            False,
            "may_contain:peanuts",
            "peanuts: may contain per supplier data",
        ),
    ],
)
def test_verify_lines_fallback_rank_none_hard_blocks_every_unsafe_state(
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    customer_id: str,
    caller_excl: list[str],
    group: str,
    allergen_override: dict[str, Any],
    source_override: dict[str, str],
    ingredients: str,
    expect_unknown: bool,
    expect_flag: str,
    expect_reason_sub: str,
) -> None:
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: (_ for _ in ()).throw(FileNotFoundError("catalog unavailable")),
    )
    pid = f"cand-{case_id}"
    amap, smap = _all_free_except(allergen_override, source_override)
    if case_id == "school-nuts-missing-key":
        amap.pop("nuts", None)
        smap.pop("nuts", None)
    unit = "KG" if customer_id == "hospital-meilahti" else "L"
    query = "gluten" if customer_id == "hospital-meilahti" else "milk"
    prod = _make_product(
        pid,
        f"Test Product {case_id}",
        unit=unit,
        amount=1.0,
        category="14100" if unit == "KG" else "10400",
        allergens=amap,
        allergen_source=smap,
        ingredients_text=ingredients,
    )
    need = Need(
        customer_id=customer_id,
        product_query=query,
        excluded_allergens=caller_excl,
        quantity=10.0,
        unit=unit,
        delivery_by=DELIVERY,
    )
    for need_arg in (need, need if caller_excl else None):
        if need_arg is None and caller_excl:
            continue
        out = verify_lines(
            customer_id,
            DELIVERY,
            {pid: 5.0},
            {pid: _offer(pid)},
            need_arg,
            {pid: prod},
            _clean_screen,
        )
        res = out[pid]
        assert res["tier"] == "rejected", (case_id, need_arg, res)
        assert res["tier"] not in ("preferred", "good", "best", "better")
        assert expect_flag in res["flags"], (case_id, res["flags"])
        if expect_unknown:
            assert "allergen_unknown" in res["flags"], (case_id, res["flags"])
        assert "allergen_safe_alternative" not in res["flags"]
        assert "tier_unverified" not in res["flags"]
        assert not any("treated as a substitute (good)" in r for r in res["reasons"])
        assert any(expect_reason_sub in r for r in res["reasons"]), (case_id, res["reasons"])
        assert any(r.startswith(f"{group}:") for r in res["reasons"]), (case_id, res["reasons"])


@pytest.mark.parametrize(
    "customer_id,usual_pid,caller_excl,allergen_override,source_override,ingredients,expect_unknown",
    [
        ("school-tampere", "470889", ["gluten"], {"gluten": "contains"}, {}, "water, oats", False),
        ("school-tampere", "470889", ["wheat"], {"gluten": "may_contain"}, {}, "water, oats", False),
        ("school-tampere", "470889", ["soy"], {"soy": "unknown"}, {"soy": "none"}, "water, oats", True),
        ("school-tampere", "470889", ["soy"], {"soy": "free"}, {"soy": "inferred:gemini"}, "water, oats", True),
        ("school-tampere", "470889", [], {"milk": "free"}, {"milk": "contradiction"}, "water, milk powder", True),
        ("hospital-meilahti", "473328", ["eggs"], {"eggs": "contains"}, {}, "water, egg white", False),
        ("hospital-meilahti", "473328", ["sesame"], {"sesame": "may_contain"}, {}, "water, sesame", False),
        ("hospital-meilahti", "473328", ["milk"], {"milk": "unknown"}, {"milk": "none"}, "water, starch", True),
        ("hospital-meilahti", "473328", [], {"gluten": "free"}, {"gluten": "inferred:ingredients"}, "water", True),
        ("hospital-meilahti", "473328", [], {"gluten": "free"}, {"gluten": "contradiction"}, "wheat flour", True),
    ],
)
def test_verify_lines_fallback_never_leaves_preferred_on_unsafe_usual_product(
    monkeypatch: pytest.MonkeyPatch,
    customer_id: str,
    usual_pid: str,
    caller_excl: list[str],
    allergen_override: dict[str, str],
    source_override: dict[str, str],
    ingredients: str,
    expect_unknown: bool,
) -> None:
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: (_ for _ in ()).throw(FileNotFoundError("no catalog")),
    )
    amap, smap = _all_free_except(allergen_override, source_override)
    unit = "KG" if customer_id == "hospital-meilahti" else "L"
    prod = _make_product(
        usual_pid,
        "Usual Product Under Extra Restriction",
        unit=unit,
        amount=1.0,
        allergens=amap,
        allergen_source=smap,
        ingredients_text=ingredients,
    )
    need = Need(
        customer_id=customer_id,
        product_query="bread" if unit == "KG" else "oat drink",
        excluded_allergens=caller_excl,
        quantity=10.0,
        unit=unit,
        delivery_by=DELIVERY,
    )
    out = verify_lines(
        customer_id,
        DELIVERY,
        {usual_pid: 5.0},
        {usual_pid: _offer(usual_pid)},
        need,
        {usual_pid: prod},
        _clean_screen,
    )
    res = out[usual_pid]
    assert res["tier"] == "rejected"
    assert res["method"] == "fallback"
    assert res["reasons"]
    if expect_unknown:
        assert "allergen_unknown" in res["flags"]
    assert "allergen_safe_alternative" not in res["flags"]


def test_verify_lines_missing_row_in_get_rows_and_missing_product_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {"470889": dict(SCHOOL_USUAL_ROW)} if "470889" in ids else {},
    )
    amap_safe, smap_safe = _all_free_except()
    safe_unindexed = _make_product(
        "unindexed-safe-oat",
        "Unindexed Safe Oat Drink 1 L",
        allergens=amap_safe,
        allergen_source=smap_safe,
        ingredients_text="water, coconut milk, cocoa butter",
    )
    amap_inf, smap_inf = _all_free_except({"nuts": "free"}, {"nuts": "inferred:gemini"})
    inf_unindexed = _make_product(
        "unindexed-inf-oat",
        "Unindexed Inferred Oat Drink 1 L",
        allergens=amap_inf,
        allergen_source=smap_inf,
    )
    amap_con, smap_con = _all_free_except({"milk": "contains"})
    con_unindexed = _make_product(
        "unindexed-milk",
        "Unindexed Dairy Drink 1 L",
        allergens=amap_con,
        allergen_source=smap_con,
    )
    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    pids = ["unindexed-safe-oat", "unindexed-inf-oat", "unindexed-milk", "completely-missing"]
    out = verify_lines(
        "school-tampere",
        DELIVERY,
        {pid: 4.0 for pid in pids},
        {pid: _offer(pid) for pid in pids},
        need,
        {
            "unindexed-safe-oat": safe_unindexed,
            "unindexed-inf-oat": inf_unindexed,
            "unindexed-milk": con_unindexed,
        },
        _clean_screen,
    )
    assert out["unindexed-safe-oat"]["tier"] == "good"
    assert "tier_unverified" in out["unindexed-safe-oat"]["flags"]
    assert "allergen_safe_alternative" in out["unindexed-safe-oat"]["flags"]
    assert "allergen_unknown" not in out["unindexed-safe-oat"]["flags"]
    assert "allergen_data_conflict" not in out["unindexed-safe-oat"]["flags"]

    assert out["unindexed-inf-oat"]["tier"] == "rejected"
    assert "allergen_unknown" in out["unindexed-inf-oat"]["flags"]
    assert "allergen_inferred_hint" in out["unindexed-inf-oat"]["flags"]
    assert "allergen_safe_alternative" not in out["unindexed-inf-oat"]["flags"]
    assert any("nuts:" in r and "inferred:gemini" in r for r in out["unindexed-inf-oat"]["reasons"])

    assert out["unindexed-milk"]["tier"] == "rejected"
    assert "contains:milk" in out["unindexed-milk"]["flags"]
    assert "allergen_safe_alternative" not in out["unindexed-milk"]["flags"]
    assert any("milk: contains per supplier data" in r for r in out["unindexed-milk"]["reasons"])

    assert out["completely-missing"]["tier"] == "rejected"
    assert "allergen_unknown" in out["completely-missing"]["flags"]
    assert "allergen_safe_alternative" not in out["completely-missing"]["flags"]
    assert any("missing from catalog" in r for r in out["completely-missing"]["reasons"])


def test_verify_lines_rank_exception_and_forged_candidate_still_hard_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_row = {
        **SCHOOL_USUAL_ROW,
        "id": "dairy-1",
        "name": "Whole Milk 1 L",
        "name_en": "Whole Milk 1 L",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "milk": "contains"},
        "allergen_source": {g: "structured" for g in EU14},
    }
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: {"470889": dict(SCHOOL_USUAL_ROW), "dairy-1": unsafe_row},
    )
    monkeypatch.setattr(
        sub_mod,
        "rank_substitutes",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("ranking crashed")),
    )
    amap, smap = _all_free_except({"milk": "contains"})
    dairy_prod = _make_product("dairy-1", "Whole Milk 1 L", allergens=amap, allergen_source=smap)
    out_exc = verify_lines(
        "school-tampere",
        DELIVERY,
        {"dairy-1": 5.0},
        {"dairy-1": _offer("dairy-1")},
        None,
        {"dairy-1": dairy_prod},
        _clean_screen,
    )
    assert out_exc["dairy-1"]["tier"] == "rejected"
    assert "contains:milk" in out_exc["dairy-1"]["flags"]
    assert any("milk: contains" in r for r in out_exc["dairy-1"]["reasons"])

    stale_row = {
        **SCHOOL_USUAL_ROW,
        "id": "stale-oat",
        "name": "Stale Oat Drink 1 L",
        "name_en": "Stale Oat Drink 1 L",
    }
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: {"470889": dict(SCHOOL_USUAL_ROW), "stale-oat": stale_row},
    )
    monkeypatch.setattr(
        sub_mod,
        "rank_substitutes",
        lambda *_a, **_kw: [
            Candidate(
                product=ProductSummary(
                    id="stale-oat",
                    name="Stale Oat Drink 1 L",
                    sales_unit="ST",
                    content_amount=1.0,
                    content_unit="L",
                ),
                offer=_offer("stale-oat"),
                tier="best",
                rank_score=0.99,
                reasons=["Matches spec"],
                flags=["allergen_safe_alternative"],
            )
        ],
    )
    amap_c, smap_c = _all_free_except({"nuts": "free"}, {"nuts": "contradiction"})
    contradiction_prod = _make_product(
        "stale-oat",
        "Stale Oat Drink 1 L",
        allergens=amap_c,
        allergen_source=smap_c,
        ingredients_text="water, oats, almond paste",
    )
    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    out_forged = verify_lines(
        "school-tampere",
        DELIVERY,
        {"stale-oat": 5.0},
        {"stale-oat": _offer("stale-oat")},
        need,
        {"stale-oat": contradiction_prod},
        _clean_screen,
    )
    assert out_forged["stale-oat"]["tier"] == "rejected"
    assert "allergen_unknown" in out_forged["stale-oat"]["flags"]
    assert "allergen_data_conflict" in out_forged["stale-oat"]["flags"]
    assert "allergen_safe_alternative" not in out_forged["stale-oat"]["flags"]
    assert any("nuts:" in r for r in out_forged["stale-oat"]["reasons"])


def test_verify_lines_excludes_unsafe_usual_from_same_kind_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aurora_usual = {
        "id": "463038",
        "name": "Valio Eila semi-skimmed lactose-free milk drink 10 L",
        "name_en": "Valio Eila semi-skimmed lactose-free milk drink 10 L",
        "brand": "Valio",
        "category": "10400",
        "sales_unit": "ST",
        "content_amount": 10.0,
        "content_unit": "L",
        "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {g: ("contains" if g == "milk" else "free") for g in EU14},
        "allergen_source": {g: "structured" for g in EU14},
    }
    oat_sub = {
        **SCHOOL_USUAL_ROW,
        "id": "470863",
        "name": "Oatly Oat Drink Semi 1L",
        "name_en": "Oatly Oat Drink Semi 1L",
    }
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: {"463038": aurora_usual, "470863": oat_sub},
    )
    amap_oat, smap_oat = _all_free_except({"gluten": "contains"})
    oat_prod = _make_product("470863", "Oatly Oat Drink Semi 1L", allergens=amap_oat, allergen_source=smap_oat)
    need = Need(
        customer_id="hotel-aurora",
        product_query="milk",
        excluded_allergens=["milk"],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    out = verify_lines(
        "hotel-aurora",
        DELIVERY,
        {"470863": 10.0},
        {"470863": _offer("470863")},
        need,
        {"470863": oat_prod},
        _clean_screen,
    )
    assert out["470863"]["reference"] is None
    assert out["470863"]["tier"] != "rejected"
    assert "allergen_safe_alternative" in out["470863"]["flags"]


def test_http_v1_quotes_and_orders_reject_forged_preferred_in_fallback_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aimo.gateway.app import create_app

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: (_ for _ in ()).throw(FileNotFoundError("no catalog")),
    )

    amap_inf, smap_inf = _all_free_except({"milk": "free"}, {"milk": "inferred:gemini"})
    amap_unk, smap_unk = _all_free_except({"gluten": "unknown"}, {"gluten": "none"})
    prods = {
        "inf-school-oat": _make_product(
            "inf-school-oat",
            "Inferred Oat Drink 1 L",
            allergens=amap_inf,
            allergen_source=smap_inf,
        ),
        "unk-hosp-bread": _make_product(
            "unk-hosp-bread",
            "Unlabelled Ward Bread 500 g",
            unit="KG",
            amount=0.5,
            category="14100",
            allergens=amap_unk,
            allergen_source=smap_unk,
        ),
    }

    class _StubCatalog:
        @staticmethod
        def get_product(pid: str) -> Product | None:
            return prods.get(str(pid))

    monkeypatch.setattr(service, "_catalog", lambda: _StubCatalog)
    bc.set_client(_MockBackend(list(prods)))
    try:
        api = TestClient(create_app(with_mcp=False))
        for cid, pid, unit, qtxt in (
            ("school-tampere", "inf-school-oat", "L", "milk"),
            ("hospital-meilahti", "unk-hosp-bread", "KG", "bread"),
        ):
            headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
            rq = api.post(
                "/v1/quotes",
                headers=headers,
                json={
                    "delivery_date": DELIVERY.isoformat(),
                    "need": {
                        "customer_id": cid,
                        "product_query": qtxt,
                        "excluded_allergens": [],
                        "quantity": 10,
                        "unit": unit,
                        "delivery_by": DELIVERY.isoformat(),
                    },
                    "lines": [{"product_id": pid, "qty_sales_units": 5, "tier": "preferred", "flags": []}],
                },
            )
            assert rq.status_code == 200, rq.text
            quote = rq.json()
            assert quote["lines"][0]["tier"] == "rejected"
            assert "allergen_unknown" in quote["lines"][0]["flags"]
            assert "allergen_safe_alternative" not in quote["lines"][0]["flags"]
            lv = quote["provenance"]["line_verification"][pid]
            assert lv["tier"] == "rejected"
            assert lv["reasons"]

            ro = api.post(
                "/v1/orders",
                headers=headers,
                json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
            )
            assert ro.status_code == 200, ro.text
            order = ro.json()
            assert order["status"] == "rejected"
            assert order["decision"]["outcome"] == "reject"
    finally:
        bc.set_client(None)


@pytest.mark.parametrize(
    "query,excluded,expected",
    [
        ("milk", ["milk", "nuts"], ["milk"]),
        ("cow's milk", ["milk", "nuts"], ["milk"]),
        ("lactose-free milk", ["milk", "nuts"], ["milk"]),
        ("butter", ["milk", "nuts"], ["milk"]),
        ("buttermilk", ["milk", "nuts"], ["milk"]),
        ("sour milk", ["milk", "nuts"], ["milk"]),
        ("whipping cream", ["milk", "nuts"], ["milk"]),
        ("cheddar cheese", ["milk", "nuts"], ["milk"]),
        ("greek yogurt", ["milk", "nuts"], ["milk"]),
        ("quark", ["milk", "nuts"], ["milk"]),
        ("whey protein", ["milk", "nuts"], ["milk"]),
        ("maito", ["milk", "nuts"], ["milk"]),
        ("20 L kevytmaitoa", ["milk", "nuts"], ["milk"]),
        ("täysmaito", ["milk", "nuts"], ["milk"]),
        ("laktoositon maito", ["milk", "nuts"], ["milk"]),
        ("voi", ["milk", "nuts"], ["milk"]),
        ("voita leivontaan", ["milk", "nuts"], ["milk"]),
        ("kuohukerma", ["milk", "nuts"], ["milk"]),
        ("ruokakerma", ["milk", "nuts"], ["milk"]),
        ("juustoa", ["milk", "nuts"], ["milk"]),
        ("tuorejuusto", ["milk", "nuts"], ["milk"]),
        ("leipäjuusto", ["milk", "nuts"], ["milk"]),
        ("piimä", ["milk", "nuts"], ["milk"]),
        ("jogurtti", ["milk", "nuts"], ["milk"]),
        ("rahka", ["milk", "nuts"], ["milk"]),
        ("hera", ["milk", "nuts"], ["milk"]),
        ("mjölk", ["milk", "nuts"], ["milk"]),
        ("lättmjölk", ["milk", "nuts"], ["milk"]),
        ("helmjölk", ["milk", "nuts"], ["milk"]),
        ("standardmjölk", ["milk", "nuts"], ["milk"]),
        ("laktosfri mjölk", ["milk", "nuts"], ["milk"]),
        ("smör", ["milk", "nuts"], ["milk"]),
        ("vispgrädde", ["milk", "nuts"], ["milk"]),
        ("matlagningsgrädde", ["milk", "nuts"], ["milk"]),
        ("gräddfil", ["milk", "nuts"], ["milk"]),
        ("filmjölk", ["milk", "nuts"], ["milk"]),
        ("ost", ["milk", "nuts"], ["milk"]),
        ("färskost", ["milk", "nuts"], ["milk"]),
        ("kvarg", ["milk", "nuts"], ["milk"]),
        ("vassle", ["milk", "nuts"], ["milk"]),
        ("rye bread", ["gluten"], ["gluten"]),
        ("wheat flour", ["gluten"], ["gluten"]),
        ("barley groats", ["gluten"], ["gluten"]),
        ("spelt bread", ["gluten"], ["gluten"]),
        ("gluten", ["gluten"], ["gluten"]),
        ("ruisleipä", ["gluten"], ["gluten"]),
        ("vehnäjauho", ["gluten"], ["gluten"]),
        ("vehnäleipä", ["gluten"], ["gluten"]),
        ("ohrasuurimo", ["gluten"], ["gluten"]),
        ("gluteenia", ["gluten"], ["gluten"]),
        ("rågbröd", ["gluten"], ["gluten"]),
        ("vetemjöl", ["gluten"], ["gluten"]),
        ("vetebröd", ["gluten"], ["gluten"]),
        ("kornmjöl", ["gluten"], ["gluten"]),
        ("roasted hazelnuts", ["milk", "nuts"], ["nuts"]),
        ("hasselpähkinä", ["milk", "nuts"], ["nuts"]),
        ("manteleita", ["milk", "nuts"], ["nuts"]),
        ("hasselnötter", ["milk", "nuts"], ["nuts"]),
        ("mandlar", ["milk", "nuts"], ["nuts"]),
        ("peanuts", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("maapähkinöitä", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("jordnötter", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("eggs", ["eggs"], ["eggs"]),
        ("kananmunia", ["eggs"], ["eggs"]),
        ("ägg", ["eggs"], ["eggs"]),
        ("soybeans", ["soy"], ["soy"]),
        ("soijakastike", ["soy"], ["soy"]),
        ("sojabönor", ["soy"], ["soy"]),
        ("salmon fillet", ["fish"], ["fish"]),
        ("lohta", ["fish"], ["fish"]),
        ("laxfilé", ["fish"], ["fish"]),
        ("tahini", ["sesame"], ["sesame"]),
        ("seesaminsiemenet", ["sesame"], ["sesame"]),
        ("sesamfrön", ["sesame"], ["sesame"]),
        ("selleri", ["celery"], ["celery"]),
        ("sinappi", ["mustard"], ["mustard"]),
        ("senap", ["mustard"], ["mustard"]),
    ],
)
def test_excluded_allergens_in_query_en_fi_sv_positives(
    query: str,
    excluded: list[str],
    expected: list[str],
) -> None:
    assert excluded_allergens_in_query(query, excluded) == expected


@pytest.mark.parametrize(
    "query,excluded,expected",
    [
        ("coconut milk", ["milk", "nuts"], []),
        ("coconut cream", ["milk", "nuts"], []),
        ("kookosmaito", ["milk", "nuts"], []),
        ("kookosmaitoa", ["milk", "nuts"], []),
        ("kookoskerma", ["milk", "nuts"], []),
        ("kokosmjölk", ["milk", "nuts"], []),
        ("kokosgrädde", ["milk", "nuts"], []),
        ("cocoa butter", ["milk", "nuts"], []),
        ("kaakaovoi", ["milk", "nuts"], []),
        ("kakaosmör", ["milk", "nuts"], []),
        ("shea butter", ["milk", "nuts"], []),
        ("sheavoi", ["milk", "nuts"], []),
        ("sheasmör", ["milk", "nuts"], []),
        ("vegan butter", ["milk", "nuts"], []),
        ("kasvirasva", ["milk", "nuts"], []),
        ("peanut butter", ["milk", "nuts"], []),
        ("maapähkinävoi", ["milk", "nuts"], []),
        ("jordnötssmör", ["milk", "nuts"], []),
        ("peanut butter", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("maapähkinävoi", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("jordnötssmör", ["milk", "nuts", "peanuts"], ["peanuts"]),
        ("almond butter", ["milk", "nuts"], ["nuts"]),
        ("mantelivoi", ["milk", "nuts"], ["nuts"]),
        ("mandelsmör", ["milk", "nuts"], ["nuts"]),
        ("hazelnut butter", ["milk", "nuts"], ["nuts"]),
        ("hasselpähkinävoi", ["milk", "nuts"], ["nuts"]),
        ("hasselnötssmör", ["milk", "nuts"], ["nuts"]),
        ("cashew butter", ["milk", "nuts"], ["nuts"]),
        ("cashewvoi", ["milk", "nuts"], ["nuts"]),
        ("cashewsmör", ["milk", "nuts"], ["nuts"]),
        ("almond butter", ["milk"], []),
        ("mantelivoi", ["milk"], []),
        ("mandelsmör", ["milk"], []),
        ("oat milk", ["milk", "nuts"], []),
        ("kauramaito", ["milk", "nuts"], []),
        ("havremjölk", ["milk", "nuts"], []),
        ("oat cream", ["milk", "nuts"], []),
        ("kaurakerma", ["milk", "nuts"], []),
        ("havregrädde", ["milk", "nuts"], []),
        ("rice milk", ["milk", "nuts"], []),
        ("riisimaito", ["milk", "nuts"], []),
        ("rismjölk", ["milk", "nuts"], []),
        ("soy milk", ["milk", "nuts"], []),
        ("soijamaito", ["milk", "nuts"], []),
        ("sojamjölk", ["milk", "nuts"], []),
        ("soy milk", ["milk", "soy"], ["soy"]),
        ("soijamaito", ["milk", "soy"], ["soy"]),
        ("sojamjölk", ["milk", "soy"], ["soy"]),
        ("almond milk", ["milk"], []),
        ("mantelimaito", ["milk"], []),
        ("mandelmjölk", ["milk"], []),
        ("almond milk", ["milk", "nuts"], ["nuts"]),
        ("mantelimaito", ["milk", "nuts"], ["nuts"]),
        ("mandelmjölk", ["milk", "nuts"], ["nuts"]),
        ("milk-free cream", ["milk", "nuts"], []),
        ("dairy-free butter", ["milk", "nuts"], []),
        ("maidoton ruokakerma", ["milk", "nuts"], []),
        ("maidoton voi", ["milk", "nuts"], []),
        ("mjölkfri grädde", ["milk", "nuts"], []),
        ("mjölkfritt smör", ["milk", "nuts"], []),
        ("oat drink, pupils have milk and nut allergies", ["milk", "nuts"], []),
        ("gluten-free rye bread", ["gluten"], []),
        ("gluteeniton ruisleipä", ["gluten"], []),
        ("glutenfritt rågbröd", ["gluten"], []),
        ("buckwheat flour", ["gluten"], []),
        ("tattarijauho", ["gluten"], []),
        ("bovetemjöl", ["gluten"], []),
        ("nutmeg", ["milk", "nuts"], []),
        ("muskottipähkinä", ["milk", "nuts"], []),
        ("muskotnöt", ["milk", "nuts"], []),
        ("butternut squash", ["milk", "nuts"], []),
        ("pine nuts", ["milk", "nuts"], []),
        ("nötkött", ["milk", "nuts"], []),
        ("nötfärs", ["milk", "nuts"], []),
        ("eggplant", ["eggs"], []),
        ("munakoiso", ["eggs"], []),
        ("äggplanta", ["eggs"], []),
        ("oyster mushrooms", ["milk", "molluscs"], []),
        ("osterivinokas", ["milk", "molluscs"], []),
        ("ostronskivling", ["milk", "molluscs"], []),
        ("juustokumina", ["milk"], []),
        ("cream of tartar", ["milk"], []),
        ("lactic acid", ["milk"], []),
    ],
)
def test_excluded_allergens_in_query_false_friends_and_free_claims(
    query: str,
    excluded: list[str],
    expected: list[str],
) -> None:
    assert excluded_allergens_in_query(query, excluded) == expected


def test_need_names_excluded_allergen_with_school_and_hospital_policies() -> None:
    school_excl = policy_excluded_allergens("school-tampere")
    hosp_excl = policy_excluded_allergens("hospital-meilahti")

    for q in ("milk", "kevytmaitoa", "lättmjölk", "almond butter", "mantelivoi", "mandelsmör"):
        need = Need(customer_id="school-tampere", product_query=q, quantity=10, unit="L", delivery_by=DELIVERY)
        assert need_names_excluded_allergen(need, school_excl) is True, q

    for q in (
        "oat drink",
        "kaurajuoma",
        "havredryck",
        "coconut milk",
        "kookosmaito",
        "kokosmjölk",
        "cocoa butter",
        "kaakaovoi",
        "kakaosmör",
        "peanut butter",
        "maapähkinävoi",
        "jordnötssmör",
    ):
        need = Need(customer_id="school-tampere", product_query=q, quantity=10, unit="L", delivery_by=DELIVERY)
        assert need_names_excluded_allergen(need, school_excl) is False, q

    for q in ("rye bread", "ruisleipä", "rågbröd", "wheat flour", "vehnäjauho", "vetemjöl", "gluten", "gluteeni"):
        need = Need(customer_id="hospital-meilahti", product_query=q, quantity=5, unit="KG", delivery_by=DELIVERY)
        assert need_names_excluded_allergen(need, hosp_excl) is True, q

    for q in ("gluten-free rye bread", "gluteeniton ruisleipä", "glutenfritt rågbröd", "buckwheat", "tattari", "bovete"):
        need = Need(customer_id="hospital-meilahti", product_query=q, quantity=5, unit="KG", delivery_by=DELIVERY)
        assert need_names_excluded_allergen(need, hosp_excl) is False, q

    assert need_names_excluded_allergen(None, school_excl) is False


@pytest.mark.parametrize(
    "query,expect_school_oat,expect_hosp_bread",
    [
        ("milk", True, False),
        ("dairy", True, False),
        ("lactose-free milk", True, False),
        ("maito", True, False),
        ("20 L maitoa", True, False),
        ("kevytmaito", True, False),
        ("täysmaito", True, False),
        ("laktoositon maito", True, False),
        ("mjölk", True, False),
        ("lättmjölk", True, False),
        ("helmjölk", True, False),
        ("standardmjölk", True, False),
        ("laktosfri mjölk", True, False),
        ("bread", False, True),
        ("rye bread", False, True),
        ("wheat bread", False, True),
        ("gluten", False, True),
        ("leipä", False, True),
        ("leipää", False, True),
        ("ruisleipä", False, True),
        ("vehnäleipä", False, True),
        ("paahtoleipä", False, True),
        ("gluteeni", False, True),
        ("gluteenia", False, True),
        ("bröd", False, True),
        ("rågbröd", False, True),
        ("vetebröd", False, True),
        ("coconut milk", False, False),
        ("kookosmaito", False, False),
        ("kokosmjölk", False, False),
        ("cocoa butter", False, False),
        ("kaakaovoi", False, False),
        ("kakaosmör", False, False),
        ("shea butter", False, False),
        ("sheavoi", False, False),
        ("sheasmör", False, False),
        ("peanut butter", False, False),
        ("maapähkinävoi", False, False),
        ("jordnötssmör", False, False),
        ("almond butter", False, False),
        ("mantelivoi", False, False),
        ("mandelsmör", False, False),
        ("hazelnut butter", False, False),
        ("cashew butter", False, False),
        ("butter", False, False),
        ("voi", False, False),
        ("smör", False, False),
        ("buttermilk", False, False),
        ("piimä", False, False),
        ("filmjölk", False, False),
        ("cream", False, False),
        ("kerma", False, False),
        ("grädde", False, False),
        ("cheese", False, False),
        ("juusto", False, False),
        ("ost", False, False),
        ("yoghurt", False, False),
        ("jogurtti", False, False),
        ("quark", False, False),
        ("rahka", False, False),
        ("milk powder", False, False),
        ("maitojauhe", False, False),
        ("mjölkpulver", False, False),
        ("ice cream", False, False),
        ("jäätelö", False, False),
        ("chocolate milk", False, False),
        ("wheat flour", False, False),
        ("vehnäjauho", False, False),
        ("vetemjöl", False, False),
        ("rågmjöl", False, False),
        ("pasta", False, False),
        ("spaghetti", False, False),
        ("makaroni", False, False),
        ("breadcrumbs", False, False),
        ("bread crumbs", False, False),
        ("korppujauho", False, False),
        ("ströbröd", False, False),
        ("croutons", False, False),
        ("oatmeal", False, False),
        ("puuro", False, False),
        ("gröt", False, False),
        ("muesli", False, False),
        ("mysli", False, False),
        ("leipäjuusto", False, False),
    ],
)
def test_usual_matches_allergen_query_en_fi_sv_false_friends_and_policies(
    query: str,
    expect_school_oat: bool,
    expect_hosp_bread: bool,
) -> None:
    assert usual_matches_allergen_query(query, SCHOOL_USUAL_ROW) is expect_school_oat, query
    assert usual_matches_allergen_query(query, HOSPITAL_USUAL_ROW) is expect_hosp_bread, query


def test_usual_matches_allergen_query_fi_sv_only_usual_rows() -> None:
    fi_soy_row = {"name_fi": "Alpro Soijajuoma 1 L", "content_unit": "L"}
    sv_soy_row = {"name_sv": "Alpro Sojadryck 1 L", "content_unit": "L"}
    sv_bread_row = {"name_sv": "Glutenfritt Matbröd 400 g", "content_unit": "KG"}
    crumb_row = {"name_fi": "Gluteeniton korppujauho 400 g", "content_unit": "KG"}

    assert usual_matches_allergen_query("kevytmaito", fi_soy_row) is True
    assert usual_matches_allergen_query("lättmjölk", sv_soy_row) is True
    assert usual_matches_allergen_query("kokosmjölk", sv_soy_row) is False
    assert usual_matches_allergen_query("rågbröd", sv_bread_row) is True
    assert usual_matches_allergen_query("vetemjöl", sv_bread_row) is False
    assert usual_matches_allergen_query("ruisleipä", crumb_row) is False


def test_policy_excluded_allergens_falls_back_to_planning_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aimo.trust import policy as trust_policy

    monkeypatch.setattr(verify, "_policy", lambda _cid: {})
    assert policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert policy_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert policy_excluded_allergens("hotel-aurora") == []
    assert policy_excluded_allergens("") == []

    monkeypatch.setattr(
        verify,
        "_policy",
        lambda _cid: {"policy_id": "custom-ward", "rules": [{"type": "spend_threshold"}]},
    )
    monkeypatch.setattr(
        trust_policy,
        "planning_profile",
        lambda cid: {"excluded_allergens": ["dairy", "tree nuts", "vehnä"]} if cid == "custom-ward" else {},
    )
    assert policy_excluded_allergens("custom-ward") == ["milk", "nuts", "gluten"]

    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda _ids: (_ for _ in ()).throw(FileNotFoundError("no catalog")),
    )
    amap_dairy, smap_dairy = _all_free_except({"milk": "contains"})
    dairy_prod = _make_product("ward-milk", "Dairy Milk 1 L", allergens=amap_dairy, allergen_source=smap_dairy)
    out = verify_lines(
        "custom-ward",
        DELIVERY,
        {"ward-milk": 4.0},
        {"ward-milk": _offer("ward-milk")},
        None,
        {"ward-milk": dairy_prod},
        _clean_screen,
    )
    assert out["ward-milk"]["tier"] == "rejected"
    assert "contains:milk" in out["ward-milk"]["flags"]

    monkeypatch.setattr(
        trust_policy,
        "planning_profile",
        lambda _cid: (_ for _ in ()).throw(RuntimeError("profile error")),
    )
    assert policy_excluded_allergens("custom-ward") == []


@pytest.mark.parametrize("group", list(EU14))
@pytest.mark.parametrize("catalog_available", [False, True])
def test_verify_lines_all_eu14_groups_across_states_and_catalog_modes(
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    catalog_available: bool,
) -> None:
    states: list[tuple[str, dict[str, Any], dict[str, str], bool, str | None]] = [
        ("contains", {group: "contains"}, {group: "structured"}, True, f"contains:{group}"),
        ("may_contain", {group: "may_contain"}, {group: "structured"}, True, f"may_contain:{group}"),
        ("unknown", {group: "unknown"}, {group: "none"}, True, "allergen_unknown"),
        ("inferred_free", {group: "free"}, {group: "inferred:gemini"}, True, "allergen_unknown"),
        ("structured_free", {group: "free"}, {group: "structured"}, False, None),
    ]
    rows_map: dict[str, dict[str, Any]] = {}
    prods_map: dict[str, Product] = {}
    offers_map: dict[str, Offer] = {}
    qty_by: dict[str, float] = {}

    for label, a_over, s_over, _blocked, _flag in states:
        pid = f"eu14-{group}-{label}"
        amap, smap = _all_free_except(a_over, s_over)
        prods_map[pid] = _make_product(
            pid,
            f"Oat Drink {group} {label} 1 L",
            allergens=amap,
            allergen_source=smap,
        )
        rows_map[pid] = {
            **SCHOOL_USUAL_ROW,
            "id": pid,
            "name": f"Oat Drink {group} {label} 1 L",
            "name_en": f"Oat Drink {group} {label} 1 L",
            "allergens": {k: (v.value if hasattr(v, "value") else v) for k, v in amap.items()},
            "allergen_source": dict(smap),
        }
        offers_map[pid] = _offer(pid)
        qty_by[pid] = 3.0

    if catalog_available:
        monkeypatch.setattr(textsearch, "get_rows", lambda ids: {i: rows_map[i] for i in ids if i in rows_map})
    else:
        monkeypatch.setattr(
            textsearch,
            "get_rows",
            lambda _ids: (_ for _ in ()).throw(FileNotFoundError("catalog unavailable")),
        )

    need = Need(
        customer_id="hotel-aurora",
        product_query=group,
        excluded_allergens=[group],
        quantity=6.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    out = verify_lines("hotel-aurora", DELIVERY, qty_by, offers_map, need, prods_map, _clean_screen)

    for label, _a_over, _s_over, expect_blocked, expect_flag in states:
        pid = f"eu14-{group}-{label}"
        res = out[pid]
        if expect_blocked:
            assert res["tier"] == "rejected", (group, label, catalog_available, res)
            assert expect_flag is not None and expect_flag in res["flags"], (group, label, res["flags"])
            if label == "inferred_free":
                assert "allergen_inferred_hint" in res["flags"]
            assert "allergen_safe_alternative" not in res["flags"]
            assert any(r.startswith(f"{group}:") for r in res["reasons"]), res["reasons"]
        else:
            assert res["tier"] != "rejected", (group, label, catalog_available, res)
            assert "allergen_unknown" not in res["flags"]
            assert f"contains:{group}" not in res["flags"]
            assert f"may_contain:{group}" not in res["flags"]
            assert "allergen_safe_alternative" in res["flags"]


def test_verify_lines_populates_flags_from_row_blk_when_p_none_and_p_blk_when_row_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_contains = {
        **SCHOOL_USUAL_ROW,
        "id": "row-only-contains",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "milk": "contains"},
        "allergen_source": {g: "structured" for g in EU14},
    }
    row_may = {
        **SCHOOL_USUAL_ROW,
        "id": "row-only-may",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "nuts": "may_contain"},
        "allergen_source": {g: "structured" for g in EU14},
    }
    row_unk = {
        **SCHOOL_USUAL_ROW,
        "id": "row-only-unk",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "milk": "unknown"},
        "allergen_source": {**{g: "structured" for g in EU14}, "milk": "none"},
    }
    row_inf = {
        **SCHOOL_USUAL_ROW,
        "id": "row-only-inf",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "nuts": "free"},
        "allergen_source": {**{g: "structured" for g in EU14}, "nuts": "inferred:gemini"},
    }
    row_sev = {
        **SCHOOL_USUAL_ROW,
        "id": "both-severity",
        "allergens": {**SCHOOL_USUAL_ROW["allergens"], "milk": "contains"},
        "allergen_source": {g: "structured" for g in EU14},
    }
    rows_db = {
        "470889": dict(SCHOOL_USUAL_ROW),
        "row-only-contains": row_contains,
        "row-only-may": row_may,
        "row-only-unk": row_unk,
        "row-only-inf": row_inf,
        "both-severity": row_sev,
    }
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {i: rows_db[i] for i in ids if i in rows_db})

    p_only_contains = _make_product(
        "p-only-contains",
        "P Only Dairy 1 L",
        allergens=_all_free_except({"milk": "contains"})[0],
        allergen_source=_all_free_except()[1],
    )
    p_only_may = _make_product(
        "p-only-may",
        "P Only Nut Trace 1 L",
        allergens=_all_free_except({"nuts": "may_contain"})[0],
        allergen_source=_all_free_except()[1],
    )
    p_only_unk = _make_product(
        "p-only-unk",
        "P Only Unknown 1 L",
        allergens=_all_free_except({"nuts": "unknown"})[0],
        allergen_source=_all_free_except(source_overrides={"nuts": "none"})[1],
    )
    p_partial_unknown = _make_product(
        "both-severity",
        "Partial Product With Row Contains 1 L",
        allergens={"milk": "unknown", "nuts": "free"},
        allergen_source={"milk": "none", "nuts": "structured"},
    )

    all_pids = [
        "row-only-contains",
        "row-only-may",
        "row-only-unk",
        "row-only-inf",
        "p-only-contains",
        "p-only-may",
        "p-only-unk",
        "both-severity",
    ]
    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    out = verify_lines(
        "school-tampere",
        DELIVERY,
        {pid: 2.0 for pid in all_pids},
        {pid: _offer(pid) for pid in all_pids},
        need,
        {
            "p-only-contains": p_only_contains,
            "p-only-may": p_only_may,
            "p-only-unk": p_only_unk,
            "both-severity": p_partial_unknown,
        },
        _clean_screen,
    )

    assert out["row-only-contains"]["tier"] == "rejected"
    assert "contains:milk" in out["row-only-contains"]["flags"]
    assert "allergen_safe_alternative" not in out["row-only-contains"]["flags"]

    assert out["row-only-may"]["tier"] == "rejected"
    assert "may_contain:nuts" in out["row-only-may"]["flags"]
    assert "allergen_safe_alternative" not in out["row-only-may"]["flags"]

    assert out["row-only-unk"]["tier"] == "rejected"
    assert "allergen_unknown" in out["row-only-unk"]["flags"]
    assert "allergen_safe_alternative" not in out["row-only-unk"]["flags"]

    assert out["row-only-inf"]["tier"] == "rejected"
    assert "allergen_unknown" in out["row-only-inf"]["flags"]
    assert "allergen_inferred_hint" in out["row-only-inf"]["flags"]
    assert "allergen_safe_alternative" not in out["row-only-inf"]["flags"]

    assert out["p-only-contains"]["tier"] == "rejected"
    assert "contains:milk" in out["p-only-contains"]["flags"]
    assert "allergen_safe_alternative" not in out["p-only-contains"]["flags"]

    assert out["p-only-may"]["tier"] == "rejected"
    assert "may_contain:nuts" in out["p-only-may"]["flags"]
    assert "allergen_safe_alternative" not in out["p-only-may"]["flags"]

    assert out["p-only-unk"]["tier"] == "rejected"
    assert "allergen_unknown" in out["p-only-unk"]["flags"]
    assert "allergen_safe_alternative" not in out["p-only-unk"]["flags"]

    assert out["both-severity"]["tier"] == "rejected"
    assert "contains:milk" in out["both-severity"]["flags"]
    assert "allergen_safe_alternative" not in out["both-severity"]["flags"]


@pytest.mark.parametrize("catalog_available", [False, True])
def test_http_v1_quotes_and_orders_matrix_across_customers_forged_claims_and_eu14(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_available: bool,
) -> None:
    from aimo.gateway.app import create_app

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")

    prods: dict[str, Product] = {
        "470889": _make_product(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            claims=["VEGAN"],
            allergens=_all_free_except({"gluten": "contains"})[0],
            allergen_source=_all_free_except()[1],
        ),
        "473328": _make_product(
            "473328",
            "Schar Vital dark sliced bread 350 g gluten free",
            unit="KG",
            amount=0.35,
            category="14100",
            claims=["FREE_FROM_GLUTEN"],
            allergens=_all_free_except()[0],
            allergen_source=_all_free_except()[1],
        ),
        "464306": _make_product(
            "464306",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            claims=["FREE_FROM_LACTOSE"],
            allergens=_all_free_except({"milk": "contains"})[0],
            allergen_source=_all_free_except()[1],
        ),
    }
    rows_db: dict[str, dict[str, Any]] = {
        "470889": dict(SCHOOL_USUAL_ROW),
        "473328": dict(HOSPITAL_USUAL_ROW),
        "464306": {
            **SCHOOL_USUAL_ROW,
            "id": "464306",
            "name": "Valio Eila semi skimmed milk drink 1 l lactose free",
            "name_en": "Valio Eila semi skimmed milk drink 1 l lactose free",
            "brand": "Valio",
            "claims": ["FREE_FROM_LACTOSE"],
            "allergens": {g: ("contains" if g == "milk" else "free") for g in EU14},
            "allergen_source": {g: "structured" for g in EU14},
        },
    }

    blocked_cases: list[tuple[str, list[str], str, str, dict[str, Any], dict[str, str], str, str]] = [
        ("school-tampere", [], "L", "milk", {"milk": "contains"}, {"milk": "structured"}, "preferred", "contains:milk"),
        ("school-tampere", [], "L", "milk", {"nuts": "may_contain"}, {"nuts": "structured"}, "best", "may_contain:nuts"),
        ("school-tampere", [], "L", "milk", {"nuts": "unknown"}, {"nuts": "none"}, "preferred", "allergen_unknown"),
        ("school-tampere", [], "L", "milk", {"milk": "free"}, {"milk": "inferred:gemini"}, "best", "allergen_unknown"),
        ("hospital-meilahti", [], "KG", "bread", {"gluten": "contains"}, {"gluten": "structured"}, "preferred", "contains:gluten"),
        ("hospital-meilahti", [], "KG", "bread", {"gluten": "may_contain"}, {"gluten": "structured"}, "best", "may_contain:gluten"),
        ("hospital-meilahti", [], "KG", "bread", {"gluten": "unknown"}, {"gluten": "none"}, "preferred", "allergen_unknown"),
        ("hospital-meilahti", [], "KG", "bread", {"gluten": "free"}, {"gluten": "inferred:ingredients"}, "best", "allergen_unknown"),
    ]
    for idx, g in enumerate(EU14):
        state_cycle = (
            ({g: "contains"}, {g: "structured"}, f"contains:{g}"),
            ({g: "may_contain"}, {g: "structured"}, f"may_contain:{g}"),
            ({g: "unknown"}, {g: "none"}, "allergen_unknown"),
            ({g: "free"}, {g: "inferred:gemini"}, "allergen_unknown"),
        )[idx % 4]
        forged_tier = "preferred" if idx % 2 == 0 else "best"
        blocked_cases.append(
            ("hotel-aurora", [g], "L", g, state_cycle[0], state_cycle[1], forged_tier, state_cycle[2])
        )

    case_specs: list[tuple[str, str, list[str], str, str, str, str]] = []
    for idx, (cid, c_excl, unit, qtxt, a_over, s_over, f_tier, exp_flag) in enumerate(blocked_cases):
        pid = f"http-blk-{idx}"
        amap, smap = _all_free_except(a_over, s_over)
        cat_code = "14100" if unit == "KG" else "10400"
        prods[pid] = _make_product(
            pid,
            f"Candidate {idx}",
            unit=unit,
            amount=0.5 if unit == "KG" else 1.0,
            category=cat_code,
            allergens=amap,
            allergen_source=smap,
        )
        rows_db[pid] = {
            **(HOSPITAL_USUAL_ROW if unit == "KG" else SCHOOL_USUAL_ROW),
            "id": pid,
            "name": f"Candidate {idx}",
            "name_en": f"Candidate {idx}",
            "allergens": {k: (v.value if hasattr(v, "value") else v) for k, v in amap.items()},
            "allergen_source": dict(smap),
        }
        case_specs.append((pid, cid, c_excl, unit, qtxt, f_tier, exp_flag))

    if catalog_available:
        monkeypatch.setattr(textsearch, "get_rows", lambda ids: {i: rows_db[i] for i in ids if i in rows_db})
    else:
        monkeypatch.setattr(
            textsearch,
            "get_rows",
            lambda _ids: (_ for _ in ()).throw(FileNotFoundError("no catalog")),
        )

    class _StubCatalog:
        @staticmethod
        def get_product(pid: str) -> Product | None:
            return prods.get(str(pid))

    monkeypatch.setattr(service, "_catalog", lambda: _StubCatalog)
    monkeypatch.setattr(
        "aimo.trust.policy._catalog_lookup",
        lambda ids: {i: prods.get(str(i)) for i in ids},
    )
    bc.set_client(_MockBackend(list(prods)))
    try:
        api = TestClient(create_app(with_mcp=False))
        for pid, cid, c_excl, unit, qtxt, forged_tier, expect_flag in case_specs:
            headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
            rq = api.post(
                "/v1/quotes",
                headers=headers,
                json={
                    "delivery_date": DELIVERY.isoformat(),
                    "need": {
                        "customer_id": cid,
                        "product_query": qtxt,
                        "excluded_allergens": c_excl,
                        "quantity": 10,
                        "unit": unit,
                        "delivery_by": DELIVERY.isoformat(),
                    },
                    "lines": [
                        {
                            "product_id": pid,
                            "qty_sales_units": 4,
                            "tier": forged_tier,
                            "flags": ["allergen_safe_alternative"],
                        }
                    ],
                },
            )
            assert rq.status_code == 200, rq.text
            quote = rq.json()
            qline = quote["lines"][0]
            assert qline["tier"] == "rejected", (pid, cid, catalog_available, qline)
            assert qline["tier"] not in ("preferred", "best", "better", "good")
            assert expect_flag in qline["flags"], (pid, cid, expect_flag, qline["flags"])
            assert "allergen_safe_alternative" not in qline["flags"], (pid, qline["flags"])
            assert quote["provenance"]["client_claims"] == [
                {"product_id": pid, "tier": forged_tier, "flags": ["allergen_safe_alternative"]}
            ]
            assert quote["provenance"]["line_verification"][pid]["tier"] == "rejected"

            ro = api.post(
                "/v1/orders",
                headers=headers,
                json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
            )
            assert ro.status_code == 200, ro.text
            order = ro.json()
            assert order["status"] == "rejected", (pid, cid, catalog_available, order)
            assert order["decision"]["outcome"] == "reject", (pid, cid, catalog_available, order)

        for cid, safe_pid, unit, qtxt, exp_tier in (
            ("school-tampere", "470889", "L", "milk", "preferred"),
            ("hospital-meilahti", "473328", "KG", "bread", "preferred"),
            ("hotel-aurora", "464306", "L", "milk", None),
        ):
            headers = {"X-Customer-Id": cid, "X-Agent-Id": "buyer-1"}
            rq_ok = api.post(
                "/v1/quotes",
                headers=headers,
                json={
                    "delivery_date": DELIVERY.isoformat(),
                    "need": {
                        "customer_id": cid,
                        "product_query": qtxt,
                        "excluded_allergens": [],
                        "quantity": 6,
                        "unit": unit,
                        "delivery_by": DELIVERY.isoformat(),
                    },
                    "lines": [{"product_id": safe_pid, "qty_sales_units": 4}],
                },
            )
            assert rq_ok.status_code == 200, rq_ok.text
            q_ok = rq_ok.json()
            assert q_ok["lines"][0]["tier"] != "rejected"
            if exp_tier is not None:
                assert q_ok["lines"][0]["tier"] == exp_tier
    finally:
        bc.set_client(None)


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    for path in (Path(__file__), Path(verify.__file__)):
        src = path.read_text(encoding="utf-8")
        assert "\x23" not in src, path
        assert "\u2014" not in src, path
