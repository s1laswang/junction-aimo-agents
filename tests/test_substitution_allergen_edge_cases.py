from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import random
from typing import Any

import pytest

from aimo.backend.catalog_store import normalize as normalize_raw_product
from aimo.catalog import gemini_allergens, product_from_row
from aimo.catalog.allergens import blocking, derive, effective_excluded_allergens
from aimo.catalog.data_quality import apply as apply_data_quality
from aimo.catalog.plan import fill
from aimo.catalog.substitution import Spec, _reasons, rank_substitutes, reference_spec
from aimo.catalog.textsearch import summary_from_row
from aimo.models import EU14, Need, Offer

DELIVERY = date(2026, 11, 18)
ROOT = Path(__file__).resolve().parents[1]


def _cls(
    allergen: tuple[tuple[str, str], ...] = (),
    non: tuple[str, ...] = (),
    claims: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    return [
        {"name": "allergen", "type": "ENUM", "values": [{"id": c, "unit": u} for c, u in allergen]},
        {"name": "nonAllergen", "type": "ENUM", "values": [{"id": c, "unit": "FREE_FROM"} for c in non]},
        {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": c, "unit": "FREE_FROM"} for c in claims]},
    ]


def _full_allergens(
    *,
    statuses: dict[str, str] | None = None,
    sources: dict[str, str] | None = None,
    default_free: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    base_st = "free" if default_free else "unknown"
    base_src = "structured" if default_free else "none"
    amap = {g: base_st for g in EU14}
    smap = {g: base_src for g in EU14}
    for g, st in (statuses or {}).items():
        amap[g] = st
        smap[g] = "structured" if st in ("contains", "may_contain", "free") else "none"
    for g, src in (sources or {}).items():
        smap[g] = src
    return amap, smap


def _row(
    pid: str,
    name: str,
    *,
    unit: str = "L",
    content: float = 1.0,
    brand: str = "Valio",
    fat: float | None = 1.5,
    claims: tuple[str, ...] = (),
    cat: str = "10400",
    allergens: dict[str, str] | None = None,
    allergen_source: dict[str, str] | None = None,
    ingredients_text: str | None = None,
    allergen_conflicts: dict[str, Any] | None = None,
    include_source: bool = True,
) -> dict[str, Any]:
    a_def, s_def = _full_allergens()
    d: dict[str, Any] = {
        "id": pid,
        "name": name,
        "name_en": name,
        "name_fi": None,
        "name_sv": None,
        "brand": brand,
        "fat_pct": fat,
        "content_amount": content,
        "content_unit": unit,
        "sales_unit": "ST",
        "sales_unit_size": 1,
        "base_content_amount": content,
        "claims": list(claims),
        "labels": [],
        "category": cat,
        "allergens": allergens if allergens is not None else a_def,
        "ingredients_text": ingredients_text,
        "delivery_reliability": 0.98,
        "co2_kg_per_kg": 1.0,
    }
    if include_source:
        d["allergen_source"] = allergen_source if allergen_source is not None else s_def
    if allergen_conflicts is not None:
        d["allergen_conflicts"] = allergen_conflicts
    return d


def _offer(
    pid: str,
    price: float = 1.5,
    *,
    available: bool = True,
    cutoff: bool = False,
    sales_block: bool = False,
    amount: float = 999999.0,
    unit: str = "L",
) -> Offer:
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    return Offer(
        product_id=pid,
        requested_date=DELIVERY,
        available=available and not cutoff and not sales_block,
        available_amount=amount,
        cutoff_passed=cutoff,
        sales_block=sales_block,
        closing_time=now - timedelta(hours=3) if cutoff else now + timedelta(days=1),
        logistic_model="Terminal" if cutoff else "Stock",
        unit_price=price,
        comparison_price=price,
        comparison_unit=unit,
        fetched_at=now,
    )


def test_reference_spec_school_tampere_empty_excluded_skips_forbidden_preferred_and_fallback() -> None:
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    a_nut_may, s_nut_may = _full_allergens(statuses={"milk": "free", "nuts": "may_contain"})
    a_unk, s_unk = _full_allergens(statuses={"milk": "free", "nuts": "unknown"})
    a_inf, s_inf = _full_allergens(
        statuses={"milk": "free", "nuts": "free"},
        sources={"milk": "inferred:gemini", "nuts": "structured"},
    )
    a_con, s_con = _full_allergens(statuses={"milk": "free", "nuts": "free"})
    conflict_row = apply_data_quality(
        _row(
            "almond-conflict",
            "Alpro Barista Almond 1 L",
            brand="Alpro",
            fat=1.2,
            allergens=a_con,
            allergen_source=s_con,
            ingredients_text="Water, ALMOND (2.3%), calcium, sea salt",
        )
    )
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free"})
    rows = {
        "dairy-pref": _row("dairy-pref", "Valio Whole Milk 1 L", brand="Valio", fat=3.5, allergens=a_milk, allergen_source=s_milk),
        "nut-may-pref": _row("nut-may-pref", "Hazelnut Drink 1 L", brand="NutCo", fat=2.0, allergens=a_nut_may, allergen_source=s_nut_may),
        "unk-pref": _row("unk-pref", "Unverified Oat Drink 1 L", brand="Fazer", fat=1.5, allergens=a_unk, allergen_source=s_unk),
        "inf-pref": _row("inf-pref", "Inferred Oat Drink 1 L", brand="GuessBrand", fat=1.8, allergens=a_inf, allergen_source=s_inf),
        "almond-conflict": conflict_row,
        "safe-oat": _row("safe-oat", "Oatly Oat Drink Whole 1 L", brand="Oatly", fat=2.8, allergens=a_safe, allergen_source=s_safe),
    }

    only_unsafe_need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        preferred_product_ids=["dairy-pref", "nut-may-pref", "unk-pref", "inf-pref", "almond-conflict"],
        quantity=50,
        unit="L",
        delivery_by=DELIVERY,
    )
    spec_none = reference_spec(only_unsafe_need, rows)
    assert spec_none.source == "none"
    assert spec_none.types_from is None
    assert spec_none.brand is None
    assert spec_none.name is None

    spec_unsafe_fb = reference_spec(only_unsafe_need, rows, fallback_row=rows["dairy-pref"])
    assert spec_unsafe_fb.source == "none"
    assert spec_unsafe_fb.types_from is None
    assert spec_unsafe_fb.brand is None

    spec_safe_fb = reference_spec(only_unsafe_need, rows, fallback_row=rows["safe-oat"])
    assert spec_safe_fb.source == "top_hit"
    assert spec_safe_fb.types_from == "top_hit"
    assert spec_safe_fb.brand == "Oatly"
    assert spec_safe_fb.types == {"plant-based"}

    mixed_pref_need = only_unsafe_need.model_copy(
        update={
            "preferred_product_ids": [
                "dairy-pref",
                "nut-may-pref",
                "unk-pref",
                "inf-pref",
                "almond-conflict",
                "safe-oat",
            ]
        }
    )
    spec_safe_pref = reference_spec(mixed_pref_need, rows)
    assert spec_safe_pref.source == "preferred"
    assert spec_safe_pref.types_from == "preferred"
    assert spec_safe_pref.brand == "Oatly"
    assert spec_safe_pref.fat == pytest.approx(2.8)
    assert spec_safe_pref.name == "Oatly Oat Drink Whole 1 L"
    assert spec_safe_pref.types == {"plant-based"}


def test_reference_spec_hospital_meilahti_and_hotel_aurora_and_synthetic_fallback() -> None:
    a_wheat, s_wheat = _full_allergens(statuses={"gluten": "contains"})
    a_rye_may, s_rye_may = _full_allergens(statuses={"gluten": "may_contain"})
    a_unk, s_unk = _full_allergens(statuses={"gluten": "unknown"})
    st_claim, src_claim, _ = derive(_cls(claims=("FREE_FROM_GLUTEN",)))

    bread_rows = {
        "wheat-b": _row("wheat-b", "Wheat Toast 1 KG", unit="KG", brand="Vaasan", fat=3.2, cat="11100", allergens=a_wheat, allergen_source=s_wheat),
        "rye-may": _row("rye-may", "Seed Roll 1 KG", unit="KG", brand="Fazer", fat=4.0, cat="11100", allergens=a_rye_may, allergen_source=s_rye_may),
        "unk-b": _row("unk-b", "Artisan Loaf 1 KG", unit="KG", brand="Local", fat=2.0, cat="11100", allergens=a_unk, allergen_source=s_unk),
        "claim-gf": _row(
            "claim-gf",
            "Schar Gluten Free Sliced Bread 1 KG",
            unit="KG",
            brand="Schar",
            fat=3.5,
            claims=("FREE_FROM_GLUTEN",),
            cat="11100",
            allergens=st_claim,
            allergen_source=src_claim,
        ),
    }

    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=[],
        preferred_product_ids=["wheat-b", "rye-may", "unk-b"],
        quantity=20,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_none = reference_spec(hosp_need, bread_rows, fallback_row=bread_rows["wheat-b"])
    assert hosp_none.source == "none"
    assert hosp_none.brand is None

    hosp_claim_ok = reference_spec(
        hosp_need.model_copy(update={"preferred_product_ids": ["wheat-b", "rye-may", "unk-b", "claim-gf"]}),
        bread_rows,
    )
    assert hosp_claim_ok.source == "preferred"
    assert hosp_claim_ok.brand == "Schar"
    assert hosp_claim_ok.name == "Schar Gluten Free Sliced Bread 1 KG"

    a_dairy, s_dairy = _full_allergens(statuses={"milk": "contains"})
    hotel_rows = {
        "463038": _row(
            "463038",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            brand="Valio",
            fat=1.5,
            content=20.0,
            claims=("FREE_FROM_LACTOSE",),
            allergens=a_dairy,
            allergen_source=s_dairy,
        )
    }
    hotel_need = Need(
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        excluded_allergens=[],
        preferred_product_ids=["463038"],
        quantity=100,
        unit="L",
        delivery_by=DELIVERY,
    )
    hotel_spec = reference_spec(hotel_need, hotel_rows)
    assert hotel_spec.source == "preferred"
    assert hotel_spec.brand == "Valio"
    assert hotel_spec.fat == pytest.approx(1.5)

    synth_rows = {
        "oat-min": _row("oat-min", "Oatly Oat Drink 1 L", brand="Oatly", allergens={"milk": "free"}, include_source=False),
        "milk-min": _row("milk-min", "Dairy Milk 1 L", brand="Valio", allergens={"milk": "contains"}, include_source=False),
    }
    synth_caller_excl_need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=["milk"],
        preferred_product_ids=["milk-min", "oat-min"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    synth_spec = reference_spec(synth_caller_excl_need, synth_rows)
    assert synth_spec.source == "preferred"
    assert synth_spec.brand == "Oatly"

    synth_empty_excl_need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        preferred_product_ids=["milk-min"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    assert reference_spec(synth_empty_excl_need, synth_rows).source == "none"


def test_rank_substitutes_customer_policies_with_empty_excluded_allergens_and_explicit_flags() -> None:
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    a_nut_may, s_nut_may = _full_allergens(statuses={"milk": "free", "nuts": "may_contain"})
    a_both, s_both = _full_allergens(statuses={"milk": "contains", "nuts": "may_contain"})
    a_unk, s_unk = _full_allergens(statuses={"milk": "free", "nuts": "unknown"})
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free"})

    school_rows = {
        "dairy": _row("dairy", "Whole milk drink 1 L", allergens=a_milk, allergen_source=s_milk),
        "nut-may": _row("nut-may", "Oat drink hazelnut trace 1 L", allergens=a_nut_may, allergen_source=s_nut_may),
        "both": _row("both", "Dairy nut drink 1 L", allergens=a_both, allergen_source=s_both),
        "unk": _row("unk", "Unverified oat drink 1 L", allergens=a_unk, allergen_source=s_unk),
        "safe": _row("safe", "Oatly Oat Drink 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
    }
    school_offers = {pid: _offer(pid, price=1.3 if pid != "safe" else 1.6) for pid in school_rows}
    school_need = Need(
        customer_id="school-tampere",
        product_query="drink",
        excluded_allergens=[],
        preferred_product_ids=["dairy"],
        quantity=25,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_ranked = rank_substitutes(
        school_need,
        school_offers,
        limit=20,
        rows=school_rows,
        relevance={pid: 1.0 for pid in school_rows},
    )
    assert school_need.excluded_allergens == []
    by_id = {c.product.id: c for c in school_ranked}

    assert by_id["dairy"].tier == "rejected"
    assert "contains:milk" in by_id["dairy"].flags
    assert "may_contain:nuts" not in by_id["dairy"].flags
    assert "milk: contains per supplier data" in by_id["dairy"].reasons[0]
    assert not any(r.startswith("free from ") for r in by_id["dairy"].reasons)

    assert by_id["nut-may"].tier == "rejected"
    assert "may_contain:nuts" in by_id["nut-may"].flags
    assert "contains:milk" not in by_id["nut-may"].flags
    assert "nuts: may contain per supplier data" in by_id["nut-may"].reasons[0]
    assert not any(r.startswith("free from ") for r in by_id["nut-may"].reasons)

    assert by_id["both"].tier == "rejected"
    assert "contains:milk" in by_id["both"].flags
    assert "may_contain:nuts" in by_id["both"].flags
    assert not any(r.startswith("free from ") for r in by_id["both"].reasons)

    assert by_id["unk"].tier == "rejected"
    assert "allergen_unknown" in by_id["unk"].flags
    assert "contains:nuts" not in by_id["unk"].flags
    assert "may_contain:nuts" not in by_id["unk"].flags
    assert not any(r.startswith("free from ") for r in by_id["unk"].reasons)

    assert by_id["safe"].tier != "rejected"
    assert "allergen_unknown" not in by_id["safe"].flags
    assert not any(f.startswith(("contains:", "may_contain:")) for f in by_id["safe"].flags)
    assert "free from milk, nuts per structured supplier data" in by_id["safe"].reasons

    school_plan = fill(school_need, school_ranked)
    assert [ln.product_id for ln in school_plan.lines] == ["safe"]
    assert school_plan.covered_amount == 25.0
    assert school_plan.shortfall == 0.0

    a_gluten_c, s_gluten_c = _full_allergens(statuses={"gluten": "contains"})
    a_gluten_m, s_gluten_m = _full_allergens(statuses={"gluten": "may_contain"})
    a_gluten_f, s_gluten_f = _full_allergens(statuses={"gluten": "free"})
    hosp_rows = {
        "wheat": _row("wheat", "Wheat Bread 1 KG", unit="KG", cat="11100", allergens=a_gluten_c, allergen_source=s_gluten_c),
        "oat-may": _row("oat-may", "Country Bread 1 KG", unit="KG", cat="11100", allergens=a_gluten_m, allergen_source=s_gluten_m),
        "gf": _row("gf", "Gluten-free Bread 1 KG", unit="KG", cat="11100", claims=("FREE_FROM_GLUTEN",), allergens=a_gluten_f, allergen_source=s_gluten_f),
    }
    hosp_offers = {pid: _offer(pid, unit="KG") for pid in hosp_rows}
    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=[],
        quantity=15,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_by_id = {
        c.product.id: c
        for c in rank_substitutes(hosp_need, hosp_offers, limit=20, rows=hosp_rows, relevance={pid: 1.0 for pid in hosp_rows})
    }
    assert hosp_by_id["wheat"].tier == "rejected" and "contains:gluten" in hosp_by_id["wheat"].flags
    assert hosp_by_id["oat-may"].tier == "rejected" and "may_contain:gluten" in hosp_by_id["oat-may"].flags
    assert hosp_by_id["gf"].tier != "rejected"
    assert "free from gluten per structured supplier data" in hosp_by_id["gf"].reasons

    hotel_need = Need(
        customer_id="hotel-aurora",
        product_query="drink",
        excluded_allergens=[],
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    hotel_by_id = {
        c.product.id: c
        for c in rank_substitutes(hotel_need, {"dairy": _offer("dairy")}, limit=10, rows={"dairy": school_rows["dairy"]}, relevance={"dairy": 1.0})
    }
    assert hotel_by_id["dairy"].tier != "rejected"
    assert "contains:milk" not in hotel_by_id["dairy"].flags
    assert not any(r.startswith("free from ") for r in hotel_by_id["dairy"].reasons)


def test_top_hit_level_zero_never_selects_allergen_blocked_row_as_type_reference() -> None:
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free"})
    rows = {
        "flav-dairy": _row(
            "flav-dairy",
            "Valio Chocolate Milk Drink 1 L",
            brand="Valio",
            allergens=a_milk,
            allergen_source=s_milk,
        ),
        "safe-plain": _row(
            "safe-plain",
            "Pure Rice Beverage 1 L",
            brand="BioCo",
            allergens=a_safe,
            allergen_source=s_safe,
        ),
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="school-tampere",
        product_query="special drink",
        excluded_allergens=[],
        preferred_product_ids=["safe-plain"],
        preferences={"usual_from_profile": ["safe-plain"]},
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    no_pref_need = Need(
        customer_id="school-tampere",
        product_query="drink",
        excluded_allergens=[],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    ranked = {
        c.product.id: c
        for c in rank_substitutes(no_pref_need, offers, limit=10, rows=rows, relevance={"flav-dairy": 100.0, "safe-plain": 1.0})
    }
    assert ranked["flav-dairy"].tier == "rejected"
    assert "contains:milk" in ranked["flav-dairy"].flags
    assert ranked["safe-plain"].tier != "rejected"
    assert not any("flavoured" in r for r in ranked["safe-plain"].reasons)
    assert rank_substitutes(need, offers, limit=10, rows=rows, relevance={"flav-dairy": 100.0, "safe-plain": 1.0})[0].product.id == "safe-plain"


def test_reasons_free_from_guard_checks_blocking_and_flags_not_colon_substring() -> None:
    a_milk_contains, s_milk_contains = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    a_inf_free, s_inf_free = _full_allergens(
        statuses={"milk": "free", "nuts": "free"},
        sources={"milk": "inferred:gemini", "nuts": "structured"},
    )
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free"})

    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=["milk", "nuts"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    spec = Spec(source="none", types_from=None)

    def _scratch_entry(
        d: dict[str, Any],
        *,
        reject: list[str],
        flags: list[str],
        offer: Offer | None = None,
        has_allergen_block: bool | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "pid": d["id"],
            "d": d,
            "offer": offer or _offer(d["id"]),
            "content": 1.0,
            "cunit": "L",
            "cnote": None,
            "reasons": [],
            "flags": list(flags),
            "reject": list(reject),
            "tier": "rejected" if reject else "best",
            "tier_if_ok": "best",
            "summary": summary_from_row(d, 1.0),
            "same_brand": False,
            "same_fat": True,
            "same_org": True,
            "same_cat": True,
            "coverage": 1.0,
            "spec_source": "none",
            "type_note": None,
            "ppu": 1.5,
            "screen_raw": None,
        }
        if has_allergen_block is not None:
            entry["has_allergen_block"] = has_allergen_block
        return entry

    contains_row = _row("m-con", "Milk Drink 1 L", allergens=a_milk_contains, allergen_source=s_milk_contains)
    r_contains_no_colon = _reasons(
        _scratch_entry(contains_row, reject=["blocked item without colon"], flags=[], has_allergen_block=True),
        spec,
        need,
    )
    assert not any("free from" in line for line in r_contains_no_colon)

    r_contains_fallback = _reasons(
        _scratch_entry(contains_row, reject=["blocked item without colon"], flags=[]),
        spec,
        need,
    )
    assert not any("free from" in line for line in r_contains_fallback)

    inferred_row = _row("m-inf", "Oat Drink 1 L", allergens=a_inf_free, allergen_source=s_inf_free)
    r_inferred_no_colon = _reasons(
        _scratch_entry(inferred_row, reject=["unverified source"], flags=[], has_allergen_block=True),
        spec,
        need,
    )
    assert not any("free from" in line for line in r_inferred_no_colon)

    safe_row = _row("m-safe", "Oatly Oat Drink 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe)
    r_has_blk_true = _reasons(
        _scratch_entry(safe_row, reject=[], flags=["allergen_unknown"], has_allergen_block=True),
        spec,
        need,
    )
    assert not any("free from" in line for line in r_has_blk_true)

    cutoff_offer = _offer("m-safe", cutoff=True)
    r_cutoff_colon = _reasons(
        _scratch_entry(
            safe_row,
            reject=["order cut-off passed (Sat 14 Nov 12:00) for delivery Wed 18 Nov"],
            flags=["cutoff_passed"],
            offer=cutoff_offer,
            has_allergen_block=False,
        ),
        spec,
        need,
    )
    assert "free from milk, nuts per structured supplier data" in r_cutoff_colon

    r_alias_excl = _reasons(
        _scratch_entry(
            safe_row,
            reject=["sales block: cannot be ordered currently"],
            flags=["sales_block"],
            offer=_offer("m-safe", sales_block=True),
            has_allergen_block=False,
        ),
        spec,
        need.model_copy(update={"excluded_allergens": ["dairy", "tree nuts"]}),
    )
    assert "free from milk, nuts per structured supplier data" in r_alias_excl

    unrelated_conflict_row = _row(
        "soy-conflict-oat",
        "Oat Drink With Soy Conflict 1 L",
        brand="Oatly",
        allergens=a_safe,
        allergen_source=s_safe,
        allergen_conflicts={"soy": {"evidence": "soy lecithin", "by": "lexicon"}},
    )
    excluded_conflict_row = _row(
        "nuts-conflict-oat",
        "Oat Drink With Almond Conflict 1 L",
        brand="Oatly",
        allergens=a_safe,
        allergen_source=s_safe,
        allergen_conflicts={"nuts": {"evidence": "ALMOND (2.3%)", "by": "lexicon"}},
    )
    cands_conflict = {
        c.product.id: c
        for c in rank_substitutes(
            need.model_copy(update={"excluded_allergens": []}),
            {
                "soy-conflict-oat": _offer("soy-conflict-oat"),
                "nuts-conflict-oat": _offer("nuts-conflict-oat"),
                "m-safe": cutoff_offer,
            },
            limit=10,
            rows={
                "soy-conflict-oat": unrelated_conflict_row,
                "nuts-conflict-oat": excluded_conflict_row,
                "m-safe": safe_row,
            },
            relevance={"soy-conflict-oat": 1.0, "nuts-conflict-oat": 1.0, "m-safe": 1.0},
        )
    }
    assert "allergen_data_conflict" in cands_conflict["soy-conflict-oat"].flags
    assert cands_conflict["soy-conflict-oat"].tier != "rejected"
    assert "free from milk, nuts per structured supplier data" in cands_conflict["soy-conflict-oat"].reasons

    assert cands_conflict["nuts-conflict-oat"].tier == "rejected"
    assert "allergen_data_conflict" in cands_conflict["nuts-conflict-oat"].flags
    assert "allergen_unknown" in cands_conflict["nuts-conflict-oat"].flags
    assert "ALMOND (2.3%)" in cands_conflict["nuts-conflict-oat"].reasons[0]
    assert not any(
        line.startswith("free from ") or "per structured supplier data" in line
        for line in cands_conflict["nuts-conflict-oat"].reasons
    )

    assert cands_conflict["m-safe"].tier == "rejected"
    assert "cutoff_passed" in cands_conflict["m-safe"].flags
    assert "free from milk, nuts per structured supplier data" in cands_conflict["m-safe"].reasons


def test_inferred_gemini_not_found_and_contains_never_unlock_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    a_unk_milk, s_unk_milk = _full_allergens(statuses={"nuts": "free"})
    a_unk_nuts, s_unk_nuts = _full_allergens(statuses={"milk": "free"})
    a_forged_inf, s_forged_inf = _full_allergens(
        statuses={"milk": "free", "nuts": "free"},
        sources={"milk": "inferred:gemini", "nuts": "inferred:gemini"},
    )
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free"})

    rows = {
        "nf-oat": _row("nf-oat", "Oat Drink No Milk Found 1 L", allergens=a_unk_milk, allergen_source=s_unk_milk),
        "mc-oat": _row("mc-oat", "Oat Drink May Contain Nuts 1 L", allergens=a_unk_nuts, allergen_source=s_unk_nuts),
        "cn-oat": _row("cn-oat", "Oat Drink Contains Milk Hint 1 L", allergens=a_unk_milk, allergen_source=s_unk_milk),
        "forged-inf": _row("forged-inf", "Oat Drink Inferred Free 1 L", allergens=a_forged_inf, allergen_source=s_forged_inf),
        "struct-oat": _row("struct-oat", "Oatly Oat Drink 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
    }
    hints = {
        "nf-oat": {"milk": "not_found"},
        "mc-oat": {"nuts": "may_contain"},
        "cn-oat": {"milk": "contains"},
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=30,
        unit="L",
        delivery_by=DELIVERY,
    )
    ranked = {
        c.product.id: c
        for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows}, inferred_allergens=hints)
    }

    assert ranked["nf-oat"].tier == "rejected"
    assert "allergen_unknown" in ranked["nf-oat"].flags
    assert "allergen_inferred_hint" in ranked["nf-oat"].flags
    assert "Gemini found no milk in the ingredients (inferred:gemini, needs human confirmation)" in ranked["nf-oat"].reasons[0]
    assert not any("free from" in line for line in ranked["nf-oat"].reasons)

    assert ranked["mc-oat"].tier == "rejected"
    assert "allergen_unknown" in ranked["mc-oat"].flags
    assert "allergen_inferred_hint" in ranked["mc-oat"].flags
    assert "Gemini reads the ingredients as possibly containing nuts" in ranked["mc-oat"].reasons[0]

    assert ranked["cn-oat"].tier == "rejected"
    assert "allergen_unknown" in ranked["cn-oat"].flags
    assert "allergen_inferred_hint" in ranked["cn-oat"].flags
    assert "Gemini reads the ingredients as containing milk" in ranked["cn-oat"].reasons[0]

    assert ranked["forged-inf"].tier == "rejected"
    assert "allergen_unknown" in ranked["forged-inf"].flags
    assert not any("free from" in line for line in ranked["forged-inf"].reasons)

    assert ranked["struct-oat"].tier != "rejected"
    assert "free from milk, nuts per structured supplier data" in ranked["struct-oat"].reasons

    monkeypatch.setattr(
        gemini_allergens,
        "cached_inference",
        lambda pid: {"milk": "not_found", "nuts": "not_found", "gluten": "contains"},
    )
    raw_unk_row = _row("prod-inf", "Unverified Bread 1 KG", unit="KG", cat="11100")
    prod = product_from_row(raw_unk_row)
    assert prod.allergens["milk"].value == "unknown" and prod.allergen_source["milk"] == "inferred:gemini"
    assert prod.allergens["nuts"].value == "unknown" and prod.allergen_source["nuts"] == "inferred:gemini"
    assert prod.allergens["gluten"].value == "contains" and prod.allergen_source["gluten"] == "inferred:gemini"

    hydrated_row = _row(
        "prod-inf",
        "Unverified Bread 1 KG",
        unit="KG",
        cat="11100",
        allergens={k: v.value for k, v in prod.allergens.items()},
        allergen_source=dict(prod.allergen_source),
    )
    hosp_cand = rank_substitutes(
        Need(customer_id="hospital-meilahti", product_query="bread", excluded_allergens=[], quantity=5, unit="KG", delivery_by=DELIVERY),
        {"prod-inf": _offer("prod-inf", unit="KG")},
        rows={"prod-inf": hydrated_row},
        relevance={"prod-inf": 1.0},
    )[0]
    assert hosp_cand.tier == "rejected"
    assert "contains:gluten" in hosp_cand.flags


def test_structured_claim_and_contradiction_sources_in_reference_spec_and_rank_substitutes() -> None:
    st_claims, src_claims, _ = derive(_cls(claims=("FREE_FROM_GLUTEN", "FREE_FROM_EGGS", "FREE_FROM_SOY")))
    assert st_claims["gluten"] == "free" and src_claims["gluten"] == "structured:claim"
    assert st_claims["eggs"] == "free" and src_claims["eggs"] == "structured:claim"
    assert st_claims["soy"] == "free" and src_claims["soy"] == "structured:claim"

    st_uw_override, src_uw_override, _ = derive(
        _cls(allergen=(("UW", "CONTAINS"),), claims=("FREE_FROM_GLUTEN",))
    )
    assert st_uw_override["gluten"] == "contains" and src_uw_override["gluten"] == "structured"

    claim_bread = _row(
        "claim-bread",
        "Gluten Egg Soy Free Bread 1 KG",
        unit="KG",
        brand="Schar",
        cat="11100",
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_EGGS", "FREE_FROM_SOY"),
        allergens=st_claims,
        allergen_source=src_claims,
        ingredients_text="Water, rice flour, tapioca starch, salt",
    )
    contradicted_egg_bread = apply_data_quality(
        _row(
            "egg-conflict-bread",
            "Bad Claim Bread 1 KG",
            unit="KG",
            brand="BadBakery",
            cat="11100",
            claims=("FREE_FROM_GLUTEN", "FREE_FROM_EGGS", "FREE_FROM_SOY"),
            allergens=dict(st_claims),
            allergen_source=dict(src_claims),
            ingredients_text="Water, rice flour, egg white powder, salt",
        )
    )
    assert contradicted_egg_bread["allergens"]["eggs"] == "unknown"
    assert contradicted_egg_bread["allergen_source"]["eggs"] == "contradiction"

    manual_contradiction_bread = _row(
        "manual-con-bread",
        "Manual Contradiction Bread 1 KG",
        unit="KG",
        brand="ManualCo",
        cat="11100",
        allergens={"gluten": "free", "eggs": "free", "soy": "free"},
        allergen_source={"gluten": "contradiction", "eggs": "structured:claim", "soy": "structured:claim"},
    )
    uw_contains_bread = _row(
        "uw-bread",
        "Wheat Starch Bread With UW Contains 1 KG",
        unit="KG",
        brand="WheatCo",
        cat="11100",
        allergens=st_uw_override,
        allergen_source=src_uw_override,
    )

    rows = {
        "egg-conflict-bread": contradicted_egg_bread,
        "manual-con-bread": manual_contradiction_bread,
        "uw-bread": uw_contains_bread,
        "claim-bread": claim_bread,
    }
    offers = {pid: _offer(pid, unit="KG") for pid in rows}
    need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=["eggs", "soy"],
        preferred_product_ids=["egg-conflict-bread", "manual-con-bread", "uw-bread", "claim-bread"],
        quantity=12,
        unit="KG",
        delivery_by=DELIVERY,
    )

    spec = reference_spec(need, rows)
    assert spec.source == "preferred"
    assert spec.name == "Gluten Egg Soy Free Bread 1 KG"

    cands = {c.product.id: c for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})}
    assert cands["claim-bread"].tier == "preferred"
    assert "free from eggs, soy, gluten per structured supplier data" in cands["claim-bread"].reasons

    assert cands["egg-conflict-bread"].tier == "rejected"
    assert "allergen_data_conflict" in cands["egg-conflict-bread"].flags
    assert "allergen_unknown" in cands["egg-conflict-bread"].flags
    assert "data conflict; treated as unknown" in cands["egg-conflict-bread"].reasons[0]
    assert not any(
        line.startswith("free from ") or "per structured supplier data" in line
        for line in cands["egg-conflict-bread"].reasons
    )

    assert cands["manual-con-bread"].tier == "rejected"
    assert "allergen_data_conflict" in cands["manual-con-bread"].flags
    assert "allergen_unknown" in cands["manual-con-bread"].flags
    assert not any(
        line.startswith("free from ") or "per structured supplier data" in line
        for line in cands["manual-con-bread"].reasons
    )

    assert cands["uw-bread"].tier == "rejected"
    assert "contains:gluten" in cands["uw-bread"].flags


def test_subcode_ml_lactose_free_and_other_subcodes_never_imply_group_free() -> None:
    st_eila, src_eila, _ = derive(
        _cls(
            allergen=(("AM", "CONTAINS"),),
            non=("ML", "AN"),
            claims=("FREE_FROM_LACTOSE",),
        )
    )
    assert st_eila["milk"] == "contains" and src_eila["milk"] == "structured"
    assert st_eila["nuts"] == "free" and src_eila["nuts"] == "structured"

    st_ml_only, src_ml_only, _ = derive(
        _cls(
            non=("ML", "AN"),
            claims=("FREE_FROM_LACTOSE",),
        )
    )
    assert st_ml_only["milk"] == "unknown" and src_ml_only["milk"] == "none"
    assert st_ml_only["nuts"] == "free"

    st_subcodes_only, src_subcodes_only, _ = derive(_cls(non=("GO", "SA", "ML")))
    assert st_subcodes_only["gluten"] == "unknown" and src_subcodes_only["gluten"] == "none"
    assert st_subcodes_only["nuts"] == "unknown" and src_subcodes_only["nuts"] == "none"
    assert st_subcodes_only["milk"] == "unknown" and src_subcodes_only["milk"] == "none"

    st_oat_safe, src_oat_safe, _ = derive(
        _cls(
            non=("AM", "ML", "AN"),
            claims=("FREE_FROM_LACTOSE",),
        )
    )
    assert st_oat_safe["milk"] == "free" and st_oat_safe["nuts"] == "free"

    rows = {
        "eila-dairy": _row(
            "eila-dairy",
            "Valio Eila Lactose Free Milk Drink 1 L",
            brand="Valio",
            claims=("FREE_FROM_LACTOSE",),
            allergens=st_eila,
            allergen_source=src_eila,
        ),
        "ml-only": _row(
            "ml-only",
            "Lactose Free Dairy Drink Unspecified AM 1 L",
            brand="OtherDairy",
            claims=("FREE_FROM_LACTOSE",),
            allergens=st_ml_only,
            allergen_source=src_ml_only,
        ),
        "sa-only": _row(
            "sa-only",
            "Almond-free Drink Without Tree Nut Head Code 1 L",
            brand="SubCo",
            claims=("FREE_FROM_LACTOSE",),
            allergens={**st_oat_safe, "nuts": st_subcodes_only["nuts"]},
            allergen_source={**src_oat_safe, "nuts": src_subcodes_only["nuts"]},
        ),
        "safe-oat-lf": _row(
            "safe-oat-lf",
            "Oddlygood Lactose Free Drink 1 L",
            brand="Valio",
            claims=("FREE_FROM_LACTOSE",),
            allergens=st_oat_safe,
            allergen_source=src_oat_safe,
        ),
    }
    offers = {pid: _offer(pid) for pid in rows}

    school_need = Need(
        customer_id="school-tampere",
        product_query="drink",
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        preferred_product_ids=["eila-dairy", "ml-only", "sa-only", "safe-oat-lf"],
        quantity=40,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_spec = reference_spec(school_need, rows)
    assert school_spec.source == "preferred"
    assert school_spec.name == "Oddlygood Lactose Free Drink 1 L"

    school_cands = {
        c.product.id: c
        for c in rank_substitutes(school_need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})
    }
    assert school_cands["eila-dairy"].tier == "rejected"
    assert "contains:milk" in school_cands["eila-dairy"].flags
    assert "has FREE_FROM_LACTOSE" in school_cands["eila-dairy"].reasons
    assert not any("free from" in line for line in school_cands["eila-dairy"].reasons)

    assert school_cands["ml-only"].tier == "rejected"
    assert "allergen_unknown" in school_cands["ml-only"].flags
    assert not any("free from" in line for line in school_cands["ml-only"].reasons)

    assert school_cands["sa-only"].tier == "rejected"
    assert "allergen_unknown" in school_cands["sa-only"].flags

    assert school_cands["safe-oat-lf"].tier == "preferred"
    assert "free from milk, nuts per structured supplier data" in school_cands["safe-oat-lf"].reasons

    hotel_need = Need(
        customer_id="hotel-aurora",
        product_query="drink",
        required_claims=["FREE_FROM_LACTOSE"],
        excluded_allergens=[],
        preferred_product_ids=["eila-dairy"],
        quantity=40,
        unit="L",
        delivery_by=DELIVERY,
    )
    hotel_cands = {
        c.product.id: c
        for c in rank_substitutes(hotel_need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})
    }
    assert hotel_cands["eila-dairy"].tier == "preferred"
    assert "contains:milk" not in hotel_cands["eila-dairy"].flags


def test_multi_allergen_exclusions_and_multilingual_aliases_across_customers() -> None:
    caller_aliases = ["shellfish", "dairy", "manteli", "kananmuna", "gluteeni", "soija", "seesami", "maapähkinä"]
    expected = ["crustaceans", "molluscs", "milk", "nuts", "eggs", "gluten", "soy", "peanuts", "sesame"]
    assert set(effective_excluded_allergens("school-tampere", caller_aliases)) == set(expected)

    a_multi_bad, s_multi_bad = _full_allergens(
        default_free=True,
        statuses={
            "crustaceans": "contains",
            "molluscs": "may_contain",
            "sesame": "unknown",
            "peanuts": "may_contain",
        },
    )
    a_single_may, s_single_may = _full_allergens(
        default_free=True,
        statuses={"eggs": "may_contain"},
    )
    a_all_safe, s_all_safe = _full_allergens(default_free=True)

    rows = {
        "multi-bad": _row("multi-bad", "Mixed Soup Base 1 KG", unit="KG", cat="15000", allergens=a_multi_bad, allergen_source=s_multi_bad),
        "egg-may": _row("egg-may", "Vegetable Soup Base 1 KG", unit="KG", cat="15000", allergens=a_single_may, allergen_source=s_single_may),
        "all-safe": _row("all-safe", "Allergen Safe Soup Base 1 KG", unit="KG", brand="SafeChef", cat="15000", allergens=a_all_safe, allergen_source=s_all_safe),
    }
    offers = {pid: _offer(pid, unit="KG") for pid in rows}
    need = Need(
        customer_id="school-tampere",
        product_query="soup base",
        excluded_allergens=caller_aliases,
        preferred_product_ids=["multi-bad", "egg-may", "all-safe"],
        quantity=10,
        unit="KG",
        delivery_by=DELIVERY,
    )

    spec = reference_spec(need, rows)
    assert spec.source == "preferred"
    assert spec.brand == "SafeChef"

    cands = {c.product.id: c for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})}
    assert cands["multi-bad"].tier == "rejected"
    assert "contains:crustaceans" in cands["multi-bad"].flags
    assert "may_contain:molluscs" in cands["multi-bad"].flags
    assert "may_contain:peanuts" in cands["multi-bad"].flags
    assert "allergen_unknown" in cands["multi-bad"].flags
    assert not any("free from" in line for line in cands["multi-bad"].reasons)

    assert cands["egg-may"].tier == "rejected"
    assert "may_contain:eggs" in cands["egg-may"].flags

    assert cands["all-safe"].tier == "preferred"
    assert not any(f.startswith(("contains:", "may_contain:")) for f in cands["all-safe"].flags)
    assert "allergen_unknown" not in cands["all-safe"].flags
    free_reasons = [line for line in cands["all-safe"].reasons if line.startswith("free from ")]
    assert len(free_reasons) == 1
    for grp in expected:
        assert grp in free_reasons[0]


def test_catalog_fixture_products_with_customer_policies_and_empty_excluded() -> None:
    gz_path = ROOT / "tests" / "fixtures" / "snapshot" / "replay" / "catalog" / "valio_aimo_junction26_product_data.json.gz"
    payload = json.loads(gzip.decompress(gz_path.read_bytes()))
    raw_items = payload["products"] if isinstance(payload, dict) else payload
    raw_by_id = {str(it["id"]): it for it in raw_items}

    target_ids = ["464306", "472833", "470889"]
    present = [pid for pid in target_ids if pid in raw_by_id]
    rows = {pid: apply_data_quality(normalize_raw_product(raw_by_id[pid])) for pid in present}
    assert "464306" in rows and "470889" in rows

    p_milk = product_from_row(rows["464306"])
    p_oat = product_from_row(rows["470889"])
    assert p_milk.allergens["milk"].value == "contains"
    assert "ML" in (rows["464306"].get("allergen_raw") or {}) or "FREE_FROM_LACTOSE" in (rows["464306"].get("claims") or [])
    assert p_oat.allergens["milk"].value == "free" and p_oat.allergens["nuts"].value == "free"
    assert not blocking(rows["470889"]["allergens"], ["milk", "nuts"], allergen_source=rows["470889"]["allergen_source"])

    school_need = Need(
        customer_id="school-tampere",
        product_query="drink",
        excluded_allergens=[],
        preferred_product_ids=present,
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_spec = reference_spec(school_need, rows)
    assert school_spec.source == "preferred"
    assert school_spec.name == rows["470889"]["name"]

    offers = {pid: _offer(pid) for pid in rows}
    ranked = {c.product.id: c for c in rank_substitutes(school_need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})}
    assert ranked["464306"].tier == "rejected"
    assert "contains:milk" in ranked["464306"].flags
    if "472833" in ranked:
        assert ranked["472833"].tier == "rejected"
        assert "allergen_unknown" in ranked["472833"].flags
    assert ranked["470889"].tier == "preferred"
    assert "free from milk, nuts per structured supplier data" in ranked["470889"].reasons


def test_reference_spec_shaping_preferences_do_not_inherit_forbidden_preferred_metadata() -> None:
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    a_gluten, s_gluten = _full_allergens(statuses={"gluten": "contains"})
    rows = {
        "flav-dairy": _row(
            "flav-dairy",
            "Valio Chocolate Milk 1 L",
            brand="Valio",
            fat=2.5,
            cat="10499",
            allergens=a_milk,
            allergen_source=s_milk,
        ),
        "wheat-crumbs": _row(
            "wheat-crumbs",
            "Vaasan Breadcrumbs 1 KG",
            unit="KG",
            brand="Vaasan",
            fat=1.2,
            cat="11199",
            allergens=a_gluten,
            allergen_source=s_gluten,
        ),
    }

    school_need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        preferred_product_ids=["flav-dairy"],
        preferences={"brand": "Oatly", "fat_pct": 1.5, "organic": True},
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_spec = reference_spec(school_need, rows)
    assert school_spec.source == "preferences"
    assert school_spec.types_from is None
    assert school_spec.types == set()
    assert school_spec.category is None
    assert school_spec.name is None
    assert school_spec.brand == "Oatly"
    assert school_spec.fat == pytest.approx(1.5)
    assert school_spec.organic is True

    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=[],
        preferred_product_ids=["wheat-crumbs"],
        preferences={"brand": "Schar"},
        quantity=10,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_spec = reference_spec(hosp_need, rows)
    assert hosp_spec.source == "preferences"
    assert hosp_spec.types_from is None
    assert hosp_spec.types == set()
    assert hosp_spec.category is None
    assert hosp_spec.brand == "Schar"

    hotel_need = school_need.model_copy(update={"customer_id": "hotel-aurora"})
    hotel_spec = reference_spec(hotel_need, rows)
    assert hotel_spec.source == "preferred"
    assert hotel_spec.types_from == "preferred"
    assert hotel_spec.types == {"flavoured"}
    assert hotel_spec.category == "10499"
    assert hotel_spec.brand == "Oatly"


def test_rank_substitutes_rows_none_passes_effective_policy_exclusions_to_find_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aimo.catalog.plan as plan_mod

    seen_excluded: list[tuple[str, list[str]]] = []
    a_safe_school, s_safe_school = _full_allergens(statuses={"milk": "free", "nuts": "free"})
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free"})
    synth = {
        "safe-oat": _row("safe-oat", "Oatly Oat Drink 1 L", brand="Oatly", allergens=a_safe_school, allergen_source=s_safe_school),
        "unsafe-milk": _row("unsafe-milk", "Dairy Drink 1 L", brand="Valio", allergens=a_milk, allergen_source=s_milk),
    }

    def fake_find_candidates(n: Need):
        seen_excluded.append((n.customer_id, list(n.excluded_allergens)))
        return synth, {"safe-oat": 2.0, "unsafe-milk": 3.0}

    monkeypatch.setattr(plan_mod, "find_candidates", fake_find_candidates)

    school_need = Need(
        customer_id="school-tampere",
        product_query="drink",
        excluded_allergens=[],
        quantity=15,
        unit="L",
        delivery_by=DELIVERY,
    )
    cands = {
        c.product.id: c
        for c in rank_substitutes(school_need, {"safe-oat": _offer("safe-oat"), "unsafe-milk": _offer("unsafe-milk")}, limit=10)
    }
    assert seen_excluded == [("school-tampere", ["milk", "nuts"])]
    assert cands["safe-oat"].tier != "rejected"
    assert cands["unsafe-milk"].tier == "rejected"
    assert "contains:milk" in cands["unsafe-milk"].flags


@pytest.mark.parametrize("group", list(EU14))
def test_rank_substitutes_emits_contains_may_contain_and_unknown_flags_for_all_eu14(group: str) -> None:
    a_con, s_con = _full_allergens(default_free=True, statuses={group: "contains"})
    a_may, s_may = _full_allergens(default_free=True, statuses={group: "may_contain"})
    a_unk, s_unk = _full_allergens(default_free=True, statuses={group: "unknown"})
    a_free, s_free = _full_allergens(default_free=True, statuses={group: "free"})

    rows = {
        "con": _row("con", "Test Product A 1 L", allergens=a_con, allergen_source=s_con),
        "may": _row("may", "Test Product B 1 L", allergens=a_may, allergen_source=s_may),
        "unk": _row("unk", "Test Product C 1 L", allergens=a_unk, allergen_source=s_unk),
        "ok": _row("ok", "Test Product D 1 L", allergens=a_free, allergen_source=s_free),
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="hotel-aurora",
        product_query="product",
        excluded_allergens=[group],
        quantity=5,
        unit="L",
        delivery_by=DELIVERY,
    )
    cands = {c.product.id: c for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})}

    assert cands["con"].tier == "rejected"
    assert f"contains:{group}" in cands["con"].flags
    assert f"may_contain:{group}" not in cands["con"].flags
    assert "allergen_unknown" not in cands["con"].flags
    assert not any(r.startswith("free from ") for r in cands["con"].reasons)

    assert cands["may"].tier == "rejected"
    assert f"may_contain:{group}" in cands["may"].flags
    assert f"contains:{group}" not in cands["may"].flags
    assert "allergen_unknown" not in cands["may"].flags
    assert not any(r.startswith("free from ") for r in cands["may"].reasons)

    assert cands["unk"].tier == "rejected"
    assert "allergen_unknown" in cands["unk"].flags
    assert f"contains:{group}" not in cands["unk"].flags
    assert f"may_contain:{group}" not in cands["unk"].flags
    assert not any(r.startswith("free from ") for r in cands["unk"].reasons)

    assert cands["ok"].tier != "rejected"
    assert not any(f.startswith(("contains:", "may_contain:")) for f in cands["ok"].flags)
    assert "allergen_unknown" not in cands["ok"].flags
    assert f"free from {group} per structured supplier data" in cands["ok"].reasons


def test_sales_block_colon_rejection_still_reports_structured_free_allergens() -> None:
    a_safe, s_safe = _full_allergens(statuses={"gluten": "free"})
    bread = _row("sb-gf", "Gluten Free Loaf 1 KG", unit="KG", cat="11100", claims=("FREE_FROM_GLUTEN",), allergens=a_safe, allergen_source=s_safe)
    sb_offer = _offer("sb-gf", sales_block=True, unit="KG")
    need = Need(
        customer_id="hospital-meilahti",
        product_query="loaf",
        excluded_allergens=[],
        quantity=5,
        unit="KG",
        delivery_by=DELIVERY,
    )
    cand = rank_substitutes(need, {"sb-gf": sb_offer}, limit=10, rows={"sb-gf": bread}, relevance={"sb-gf": 1.0})[0]
    assert cand.tier == "rejected"
    assert "sales_block" in cand.flags
    assert "sales block: cannot be ordered currently" in cand.reasons[0]
    assert "free from gluten per structured supplier data" in cand.reasons


@pytest.mark.parametrize("group", list(EU14))
def test_allergen_conflicts_override_free_status_across_all_eu14(group: str) -> None:
    a_free, s_free = _full_allergens(default_free=True, statuses={group: "free"})
    conflict_with_ev = _row(
        "cfl-ev",
        "Conflict Product With Evidence 1 L",
        allergens=dict(a_free),
        allergen_source=dict(s_free),
        allergen_conflicts={group: {"evidence": f"trace of {group} extract", "by": "lexicon"}},
    )
    conflict_no_ev = _row(
        "cfl-empty",
        "Conflict Product Empty Evidence 1 L",
        allergens=dict(a_free),
        allergen_source=dict(s_free),
        allergen_conflicts={group: {}},
    )
    conflict_str_ev = _row(
        "cfl-str",
        "Conflict Product String Evidence 1 L",
        allergens=dict(a_free),
        allergen_source=dict(s_free),
        allergen_conflicts={group: f"{group} ingredient"},
    )
    truly_safe = _row(
        "clean-ok",
        "Clean Safe Product 1 L",
        brand="SafeBrand",
        allergens=dict(a_free),
        allergen_source=dict(s_free),
    )

    rows = {
        "cfl-ev": conflict_with_ev,
        "cfl-empty": conflict_no_ev,
        "cfl-str": conflict_str_ev,
        "clean-ok": truly_safe,
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="hotel-aurora",
        product_query="product",
        excluded_allergens=[group],
        preferred_product_ids=["cfl-ev", "cfl-empty", "cfl-str", "clean-ok"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )

    spec = reference_spec(need, rows, fallback_row=conflict_with_ev)
    assert spec.source == "preferred"
    assert spec.brand == "SafeBrand"
    assert spec.name == "Clean Safe Product 1 L"

    only_cfl_need = need.model_copy(update={"preferred_product_ids": ["cfl-ev"]})
    spec_blocked = reference_spec(only_cfl_need, {"cfl-ev": conflict_with_ev}, fallback_row=conflict_with_ev)
    assert spec_blocked.source == "none"

    cands = {
        c.product.id: c
        for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})
    }
    for cid in ("cfl-ev", "cfl-empty", "cfl-str"):
        assert rows[cid]["allergens"][group] == "free"
        assert cands[cid].tier == "rejected"
        assert "allergen_unknown" in cands[cid].flags
        assert "allergen_data_conflict" in cands[cid].flags
        assert "data conflict; treated as unknown" in cands[cid].reasons[0]
        assert not any(
            line.startswith("free from ") or "per structured supplier data" in line
            for line in cands[cid].reasons
        )
    assert f"trace of {group} extract" in cands["cfl-ev"].reasons[0]
    assert f"{group} ingredient" in cands["cfl-str"].reasons[0]

    assert cands["clean-ok"].tier == "preferred"
    assert f"free from {group} per structured supplier data" in cands["clean-ok"].reasons

    plan = fill(need, list(cands.values()))
    assert [ln.product_id for ln in plan.lines] == ["clean-ok"]


@pytest.mark.parametrize("group", list(EU14))
def test_inferred_free_and_not_found_never_unlock_unknown_across_all_eu14(
    group: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_inf_gem, s_inf_gem = _full_allergens(
        default_free=True,
        statuses={group: "free"},
        sources={group: "inferred:gemini"},
    )
    a_inf_plain, s_inf_plain = _full_allergens(
        default_free=True,
        statuses={group: "free"},
        sources={group: "inferred"},
    )
    a_unk, s_unk = _full_allergens(default_free=True, statuses={group: "unknown"})
    a_safe, s_safe = _full_allergens(default_free=True, statuses={group: "free"})

    rows = {
        "inf-gem": _row("inf-gem", "Inferred Gemini Free Product 1 L", allergens=a_inf_gem, allergen_source=s_inf_gem),
        "inf-plain": _row("inf-plain", "Inferred Plain Free Product 1 L", allergens=a_inf_plain, allergen_source=s_inf_plain),
        "unk-nf": _row("unk-nf", "Unknown With Not Found Hint Product 1 L", allergens=dict(a_unk), allergen_source=dict(s_unk)),
        "unk-free": _row("unk-free", "Unknown With Free Hint Product 1 L", allergens=dict(a_unk), allergen_source=dict(s_unk)),
        "struct-ok": _row("struct-ok", "Structured Free Product 1 L", brand="VerifiedCo", allergens=a_safe, allergen_source=s_safe),
    }
    hints = {
        "unk-nf": {group: "not_found"},
        "unk-free": {group: "free"},
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="hotel-aurora",
        product_query="product",
        excluded_allergens=[group],
        preferred_product_ids=["inf-gem", "inf-plain", "unk-nf", "unk-free", "struct-ok"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )

    spec = reference_spec(need, rows, fallback_row=rows["inf-gem"])
    assert spec.source == "preferred"
    assert spec.brand == "VerifiedCo"

    ranked = rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows}, inferred_allergens=hints)
    cands = {c.product.id: c for c in ranked}

    for pid in ("inf-gem", "inf-plain", "unk-nf", "unk-free"):
        assert cands[pid].tier == "rejected"
        assert "allergen_unknown" in cands[pid].flags
        assert not any("free from" in line for line in cands[pid].reasons)

    assert "allergen_inferred_hint" in cands["unk-nf"].flags
    assert f"Gemini found no {group} in the ingredients" in cands["unk-nf"].reasons[0]
    assert "allergen_inferred_hint" in cands["unk-free"].flags
    assert f"Gemini found no {group} in the ingredients" in cands["unk-free"].reasons[0]

    assert cands["struct-ok"].tier == "preferred"
    assert f"free from {group} per structured supplier data" in cands["struct-ok"].reasons
    assert [ln.product_id for ln in fill(need, ranked).lines] == ["struct-ok"]

    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: {group: "not_found"})
    prod = product_from_row(_row("raw-unk", "Unverified Item 1 L", allergens=dict(a_unk), allergen_source=dict(s_unk)))
    assert prod.allergens[group].value == "unknown"
    assert prod.allergen_source[group] == "inferred:gemini"


def test_orderability_rejected_vs_allergen_rejected_reasons_and_normalized_allergen_hit_retention() -> None:
    a_safe, s_safe = _full_allergens(default_free=True)
    a_milk, s_milk = _full_allergens(default_free=True, statuses={"milk": "contains"})
    a_shell, s_shell = _full_allergens(default_free=True, statuses={"crustaceans": "may_contain"})

    rows = {
        "safe-ok": _row("safe-ok", "Oatly Oat Drink 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "safe-sb": _row("safe-sb", "Oat Drink Sales Block 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "safe-co": _row("safe-co", "Oat Drink Cutoff 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "safe-oos": _row("safe-oos", "Oat Drink Out Of Stock 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "safe-late": _row("safe-late", "Oat Drink Late Date 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "safe-noprice": _row("safe-noprice", "Oat Drink No Price 1 L", brand="Oatly", allergens=a_safe, allergen_source=s_safe),
        "dairy-wrong-unit": _row(
            "dairy-wrong-unit",
            "Chocolate Milk Powder 1 KG",
            unit="KG",
            allergens=a_milk,
            allergen_source=s_milk,
        ),
        "shell-wrong-unit": _row(
            "shell-wrong-unit",
            "Seafood Broth Base 1 KG",
            unit="KG",
            allergens=a_shell,
            allergen_source=s_shell,
        ),
        "safe-wrong-unit-sb": _row(
            "safe-wrong-unit-sb",
            "unrelated safe powder 1 KG",
            unit="KG",
            allergens=a_safe,
            allergen_source=s_safe,
        ),
    }
    late_offer = _offer("safe-late", available=False, amount=10.0).model_copy(
        update={"available_date": DELIVERY + timedelta(days=3)}
    )
    noprice_offer = _offer("safe-noprice").model_copy(update={"unit_price": None, "comparison_price": None})
    offers = {
        "safe-ok": _offer("safe-ok"),
        "safe-sb": _offer("safe-sb", sales_block=True),
        "safe-co": _offer("safe-co", cutoff=True),
        "safe-oos": _offer("safe-oos", available=False, amount=0.0),
        "safe-late": late_offer,
        "safe-noprice": noprice_offer,
        "dairy-wrong-unit": _offer("dairy-wrong-unit", sales_block=True, unit="KG"),
        "shell-wrong-unit": _offer("shell-wrong-unit", unit="KG"),
        "safe-wrong-unit-sb": _offer("safe-wrong-unit-sb", sales_block=True, unit="KG"),
    }
    need = Need(
        customer_id="hotel-aurora",
        product_query="oat drink",
        excluded_allergens=["dairy", "shellfish"],
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    ranked = rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})
    by_id = {c.product.id: c for c in ranked}

    expected_free = "free from milk, crustaceans, molluscs per structured supplier data"
    assert by_id["safe-ok"].tier != "rejected"
    assert expected_free in by_id["safe-ok"].reasons

    for pid in ("safe-sb", "safe-co", "safe-oos", "safe-late", "safe-noprice"):
        assert by_id[pid].tier == "rejected"
        assert expected_free in by_id[pid].reasons

    assert "dairy-wrong-unit" in by_id
    assert by_id["dairy-wrong-unit"].tier == "rejected"
    assert "contains:milk" in by_id["dairy-wrong-unit"].flags
    assert not any("free from" in line for line in by_id["dairy-wrong-unit"].reasons)

    assert "shell-wrong-unit" in by_id
    assert by_id["shell-wrong-unit"].tier == "rejected"
    assert "may_contain:crustaceans" in by_id["shell-wrong-unit"].flags
    assert not any("free from" in line for line in by_id["shell-wrong-unit"].reasons)

    assert "safe-wrong-unit-sb" not in by_id


def test_rank_substitutes_determinism_across_permutations_and_ties() -> None:
    a_safe, s_safe = _full_allergens(statuses={"milk": "free", "nuts": "free", "soy": "free"})
    a_milk, s_milk = _full_allergens(statuses={"milk": "contains", "nuts": "free", "soy": "free"})
    a_unk, s_unk = _full_allergens(statuses={"milk": "free", "nuts": "unknown", "soy": "free"})
    a_cfl, s_cfl = _full_allergens(statuses={"milk": "free", "nuts": "free", "soy": "free"})

    base_rows = {
        "oat-alpha": _row("oat-alpha", "Oat Drink Alpha 1 L", brand="Alpha", fat=1.5, allergens=a_safe, allergen_source=s_safe),
        "oat-beta": _row("oat-beta", "Oat Drink Beta 1 L", brand="Beta", fat=1.5, allergens=a_safe, allergen_source=s_safe),
        "oat-gamma": _row("oat-gamma", "Oat Drink Gamma 1 L", brand="Gamma", fat=1.5, allergens=a_safe, allergen_source=s_safe),
        "oat-better-1": _row("oat-better-1", "Oddlygood Oat One 1 L", brand="One", fat=3.0, allergens=a_safe, allergen_source=s_safe),
        "oat-better-2": _row("oat-better-2", "Oddlygood Oat Two 1 L", brand="Two", fat=3.0, allergens=a_safe, allergen_source=s_safe),
        "rej-milk": _row("rej-milk", "Dairy Milk Drink 1 L", brand="Valio", fat=1.5, allergens=a_milk, allergen_source=s_milk),
        "rej-unk": _row("rej-unk", "Oat Drink Unverified 1 L", brand="Unk", fat=1.5, allergens=a_unk, allergen_source=s_unk),
        "rej-cfl": _row(
            "rej-cfl",
            "Oat Drink Conflict 1 L",
            brand="Cfl",
            fat=1.5,
            allergens=a_cfl,
            allergen_source=s_cfl,
            allergen_conflicts={"nuts": {"evidence": "hazelnut paste", "by": "lexicon"}},
        ),
        "rej-sb": _row("rej-sb", "Oat Drink Blocked 1 L", brand="Blk", fat=1.5, allergens=a_safe, allergen_source=s_safe),
    }
    base_offers = {
        pid: _offer(pid, price=1.50, sales_block=(pid == "rej-sb"))
        for pid in base_rows
    }
    base_relevance = {pid: 1.0 for pid in base_rows}
    base_hints = {"rej-unk": {"nuts": "not_found"}, "oat-alpha": {"soy": "not_found"}}

    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=["soija"],
        preferred_product_ids=["rej-milk"],
        quantity=30,
        unit="L",
        delivery_by=DELIVERY,
    )

    def _signature(cands: list[Any]) -> list[tuple[str, str, float, tuple[str, ...], tuple[str, ...]]]:
        return [
            (c.product.id, c.tier, c.rank_score, tuple(c.flags), tuple(c.reasons))
            for c in cands
        ]

    baseline = rank_substitutes(
        need,
        dict(base_offers),
        limit=20,
        rows=dict(base_rows),
        relevance=dict(base_relevance),
        inferred_allergens=dict(base_hints),
    )
    baseline_sig = _signature(baseline)
    assert need.excluded_allergens == ["soija"]
    assert [c.product.id for c in baseline if c.tier == "best"] == ["oat-alpha", "oat-beta", "oat-gamma"]
    assert [c.product.id for c in baseline if c.tier == "better"] == ["oat-better-1", "oat-better-2"]

    keys = list(base_rows.keys())
    permutations = [list(reversed(keys))]
    for seed in range(12):
        rng = random.Random(seed)
        shuffled = list(keys)
        rng.shuffle(shuffled)
        permutations.append(shuffled)

    for order in permutations:
        rows_perm = {k: dict(base_rows[k]) for k in order}
        offers_perm = {k: base_offers[k] for k in reversed(order)}
        rel_perm = {k: base_relevance[k] for k in order}
        hints_perm = {k: dict(base_hints[k]) for k in order if k in base_hints}
        res = rank_substitutes(
            need,
            offers_perm,
            limit=20,
            rows=rows_perm,
            relevance=rel_perm,
            inferred_allergens=hints_perm,
        )
        assert _signature(res) == baseline_sig


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    self_src = Path(__file__).read_text(encoding="utf-8")
    sub_src = (ROOT / "aimo" / "catalog" / "substitution.py").read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert "\u2014" not in self_src
    assert "\u2014" not in sub_src
