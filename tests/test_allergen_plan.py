from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aimo.catalog import get_product, rank_substitutes as catalog_rank_substitutes
from aimo.catalog.plan import _static_priority, fill, find_candidates, plan_need, with_customer_policy
from aimo.catalog.substitution import missing_query_concepts, rank_substitutes
from aimo.models import Candidate, EU14, Need, Offer, ProductSummary

DELIVERY = date(2026, 11, 18)


def _have_data() -> bool:
    from aimo.config import PRODUCT_JSON
    return PRODUCT_JSON.exists()


def _offer(pid: str, price: float = 1.5, available: bool = True, amount: float = 9999999.0) -> Offer:
    now = datetime.now(timezone.utc)
    return Offer(
        product_id=pid,
        requested_date=DELIVERY,
        available=available,
        available_amount=amount,
        cutoff_passed=False,
        closing_time=now + timedelta(days=1),
        logistic_model="Stock",
        unit_price=price,
        comparison_price=price,
        comparison_unit="L",
        fetched_at=now,
    )


def _fake_offers(price: float = 1.5):
    def fn(ids, d, customer_id=None):
        now = datetime.now(timezone.utc)
        return {
            pid: Offer(
                product_id=pid,
                requested_date=d,
                available=True,
                available_amount=9999999.0,
                cutoff_passed=False,
                closing_time=now + timedelta(days=1),
                logistic_model="Stock",
                unit_price=price,
                fetched_at=now,
            )
            for pid in ids
        }

    return fn


def _full_allergens(**overrides: str) -> tuple[dict[str, str], dict[str, str]]:
    allergens = {g: "unknown" for g in EU14}
    sources = {g: "none" for g in EU14}
    for g, st in overrides.items():
        allergens[g] = st
        sources[g] = "structured" if st in ("contains", "may_contain", "free") else "none"
    return allergens, sources


def _synth_row(
    pid: str,
    name: str,
    unit: str = "L",
    content: float = 1.0,
    brand: str = "Valio",
    fat: float = 1.5,
    claims: tuple[str, ...] = (),
    cat: str = "10400",
    allergens: dict[str, str] | None = None,
    allergen_source: dict[str, str] | None = None,
) -> dict:
    a_default, s_default = _full_allergens()
    return {
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
        "allergens": allergens if allergens is not None else a_default,
        "allergen_source": allergen_source if allergen_source is not None else s_default,
        "delivery_reliability": 0.98,
        "co2_kg_per_kg": 1.0,
    }


@pytest.fixture
def offline_plan(monkeypatch):
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    from aimo.catalog import textsearch
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])


def test_with_customer_policy_merges_and_tracks_added():
    school_need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=50,
        unit="L",
        delivery_by=DELIVERY,
    )
    updated, added, pb = with_customer_policy(school_need)
    assert set(updated.excluded_allergens) == {"milk", "nuts"}
    assert added == ["milk", "nuts"]
    assert pb.policy_id == "school-tampere"
    assert pb.reason == "pupils with milk and nut allergies"

    extra_need = school_need.model_copy(update={"excluded_allergens": ["soy"]})
    updated_extra, added_extra, _ = with_customer_policy(extra_need)
    assert updated_extra.excluded_allergens == ["soy", "milk", "nuts"]
    assert added_extra == ["milk", "nuts"]

    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=[],
        quantity=20,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_updated, hosp_added, hosp_pb = with_customer_policy(hosp_need)
    assert hosp_updated.excluded_allergens == ["gluten"]
    assert hosp_added == ["gluten"]
    assert hosp_pb.reason == "coeliac patients"

    hotel_need = Need(
        customer_id="hotel-aurora",
        product_query="milk",
        excluded_allergens=[],
        quantity=100,
        unit="L",
        delivery_by=DELIVERY,
    )
    hotel_updated, hotel_added, _ = with_customer_policy(hotel_need)
    assert hotel_updated is hotel_need
    assert hotel_updated.excluded_allergens == []
    assert hotel_added == []

    unknown_need = Need(
        customer_id="unknown-demo-customer",
        product_query="milk",
        excluded_allergens=["dairy"],
        quantity=10,
        unit="L",
        delivery_by=DELIVERY,
    )
    unk_updated, unk_added, _ = with_customer_policy(unknown_need)
    assert unk_updated.excluded_allergens == ["milk"]
    assert unk_added == []


def test_missing_query_concepts_ignores_exclusion_phrases_in_product_names():
    margarine = {"name_en": "Rama Professional liquid margarine milk free 78% 2,5L", "claims": ["VEGAN"]}
    ice = {"name_en": "Amigo Strawberry Ice Milk-free 5L", "claims": []}
    dairy_milk = {"name_en": "Valio Eila semi skimmed milk drink 1 l lactose free", "claims": ["FREE_FROM_LACTOSE"]}
    gf_bread = {"name_en": "Schar Vital dark sliced bread 350g gluten-free", "claims": ["FREE_FROM_GLUTEN"]}

    assert "milk" in missing_query_concepts("milk", margarine)
    assert "milk" in missing_query_concepts("milk", ice)
    assert missing_query_concepts("milk", dairy_milk) == []
    assert missing_query_concepts("lactose-free milk", dairy_milk) == []
    assert missing_query_concepts("gluten free bread", gf_bread) == []


def test_static_priority_places_safe_ahead_of_unsafe_preferred():
    a_unsafe, s_unsafe = _full_allergens(milk="contains", nuts="free")
    a_safe, s_safe = _full_allergens(milk="free", nuts="free")
    unsafe_pref = _synth_row("pref-milk", "Whole milk 20 L", allergens=a_unsafe, allergen_source=s_unsafe)
    safe_alt = _synth_row("safe-oat", "Oat drink 1 L", allergens=a_safe, allergen_source=s_safe)

    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        preferred_product_ids=["pref-milk"],
        quantity=50,
        unit="L",
        delivery_by=DELIVERY,
    )
    p_safe = _static_priority(need, safe_alt, rel=1.0)
    p_unsafe = _static_priority(need, unsafe_pref, rel=100.0)
    assert p_safe < p_unsafe


def test_direct_rank_substitutes_enforces_customer_policy_and_source_awareness():
    a_milk, s_milk = _full_allergens(milk="contains", nuts="free")
    a_nut_may, s_nut_may = _full_allergens(milk="free", nuts="may_contain")
    a_unk, s_unk = _full_allergens(milk="free")
    a_inferred, s_inferred = _full_allergens(milk="free", nuts="free")
    s_inferred["nuts"] = "inferred:gemini"
    a_safe, s_safe = _full_allergens(milk="free", nuts="free")

    rows = {
        "m1": _synth_row("m1", "Whole milk drink 1 L", allergens=a_milk, allergen_source=s_milk),
        "n1": _synth_row("n1", "Almond oat drink 1 L", allergens=a_nut_may, allergen_source=s_nut_may),
        "u1": _synth_row("u1", "Plain oat drink 1 L", allergens=a_unk, allergen_source=s_unk),
        "i1": _synth_row("i1", "Barista oat drink 1 L", allergens=a_inferred, allergen_source=s_inferred),
        "s1": _synth_row("s1", "Safe oat drink 1 L", allergens=a_safe, allergen_source=s_safe),
    }
    offers = {pid: _offer(pid) for pid in rows}
    need = Need(
        customer_id="school-tampere",
        product_query="drink",
        excluded_allergens=[],
        preferred_product_ids=["m1"],
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    cands = {c.product.id: c for c in rank_substitutes(need, offers, limit=20, rows=rows, relevance={k: 1.0 for k in rows})}
    assert cands["s1"].tier != "rejected"
    assert cands["m1"].tier == "rejected"
    assert cands["n1"].tier == "rejected"
    assert cands["u1"].tier == "rejected" and "allergen_unknown" in cands["u1"].flags
    assert cands["i1"].tier == "rejected" and "allergen_unknown" in cands["i1"].flags


def test_direct_fill_defense_in_depth_blocks_unsafe_and_unknown_candidates():
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=20,
        unit="L",
        delivery_by=DELIVERY,
    )
    forged_unknown = Candidate(
        product=ProductSummary(id="synth-unk", name="Unverified Drink 1 L", sales_unit="ST", content_amount=1.0, content_unit="L"),
        offer=_offer("synth-unk", price=1.0),
        tier="preferred",
        rank_score=0.99,
        reasons=["Usual product"],
        flags=["allergen_unknown"],
    )
    forged_contains = Candidate(
        product=ProductSummary(id="synth-milk", name="Dairy Drink 1 L", sales_unit="ST", content_amount=1.0, content_unit="L"),
        offer=_offer("synth-milk", price=1.0),
        tier="preferred",
        rank_score=0.98,
        reasons=["Usual product"],
        flags=["contains:milk"],
    )
    safe_cand = Candidate(
        product=ProductSummary(id="synth-safe", name="Verified Oat Drink 1 L", sales_unit="ST", content_amount=1.0, content_unit="L"),
        offer=_offer("synth-safe", price=1.5),
        tier="best",
        rank_score=0.80,
        reasons=["Safe match"],
        flags=[],
    )
    plan = fill(need, [forged_unknown, forged_contains, safe_cand])
    assert [ln.product_id for ln in plan.lines] == ["synth-safe"]
    assert plan.covered_amount == 20.0
    assert plan.shortfall == 0.0


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_direct_fill_blocks_real_catalog_unsafe_products_even_if_marked_preferred():
    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=40,
        unit="L",
        delivery_by=DELIVERY,
    )
    tampered = []
    for pid in ("476644", "464306", "472833"):
        p = get_product(pid)
        assert p is not None
        tampered.append(
            Candidate(
                product=ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))),
                offer=_offer(pid, price=1.2),
                tier="preferred",
                rank_score=1.0,
                reasons=["Tampered tier"],
                flags=[],
            )
        )
    plan = fill(need, tampered)
    assert plan.lines == []
    assert plan.covered_amount == 0.0
    assert plan.shortfall == 40.0


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query", ["milk", "whole milk", "lactose-free milk", "oat drink"])
def test_school_tampere_plan_need_never_returns_milk_nuts_or_unknown(offline_plan, query):
    need = Need(
        customer_id="school-tampere",
        product_query=query,
        excluded_allergens=[],
        quantity=50,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.need.product_query == query
    assert set(plan.need.excluded_allergens) == {"milk", "nuts"}
    assert "Customer policy (school-tampere) excludes milk, nuts" in plan.explanation
    assert "pupils with milk and nut allergies" in plan.explanation

    for ln in plan.lines:
        prod = get_product(ln.product_id)
        assert prod is not None
        assert prod.allergens["milk"].value == "free", (query, ln.product_id, ln.product_name, prod.allergens)
        assert prod.allergens["nuts"].value == "free", (query, ln.product_id, ln.product_name, prod.allergens)
        assert prod.allergen_source["milk"].startswith("structured")
        assert prod.allergen_source["nuts"].startswith("structured")
        assert "allergen_unknown" not in ln.flags

    for cand in plan.candidates:
        if cand.tier != "rejected":
            prod = get_product(cand.product.id)
            assert prod is not None
            assert prod.allergens["milk"].value == "free"
            assert prod.allergens["nuts"].value == "free"

    if query == "oat drink":
        assert plan.lines and plan.shortfall == 0


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("pref_id", ["476644", "464306", "472833"])
def test_school_tampere_rejects_unsafe_or_unverified_preferred_products(offline_plan, pref_id):
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink" if pref_id == "472833" else "milk",
        excluded_allergens=[],
        preferred_product_ids=[pref_id],
        quantity=60,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_fake_offers())
    assert all(ln.product_id != pref_id for ln in plan.lines)
    by_id = {c.product.id: c for c in plan.candidates}
    assert pref_id in by_id
    assert by_id[pref_id].tier == "rejected"
    for ln in plan.lines:
        prod = get_product(ln.product_id)
        assert prod is not None
        assert prod.allergens["milk"].value == "free"
        assert prod.allergens["nuts"].value == "free"


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query", ["bread", "rye bread", "pasta"])
def test_hospital_meilahti_plan_need_never_returns_gluten_or_unknown(offline_plan, query):
    need = Need(
        customer_id="hospital-meilahti",
        product_query=query,
        excluded_allergens=[],
        quantity=20,
        unit="KG",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.need.product_query == query
    assert plan.need.excluded_allergens == ["gluten"]
    assert "Customer policy (hospital-meilahti) excludes gluten (coeliac patients)." in plan.explanation

    for ln in plan.lines:
        prod = get_product(ln.product_id)
        assert prod is not None
        assert prod.allergens["gluten"].value == "free", (query, ln.product_id, ln.product_name, prod.allergens)
        assert prod.allergen_source["gluten"].startswith("structured")
        assert "allergen_unknown" not in ln.flags

    for cand in plan.candidates:
        if cand.tier != "rejected":
            prod = get_product(cand.product.id)
            assert prod is not None
            assert prod.allergens["gluten"].value == "free"

    if query in ("bread", "pasta"):
        assert plan.lines and plan.shortfall == 0


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query,claims", [("milk", []), ("lactose-free milk", ["FREE_FROM_LACTOSE"])])
def test_hotel_aurora_plan_need_unaffected_plans_normal_dairy(offline_plan, query, claims):
    need = Need(
        customer_id="hotel-aurora",
        product_query=query,
        required_claims=claims,
        excluded_allergens=[],
        quantity=100,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.need.excluded_allergens == []
    assert "Customer policy" not in plan.explanation
    assert plan.lines and plan.shortfall == 0
    first_prod = get_product(plan.lines[0].product_id)
    assert first_prod is not None
    assert first_prod.allergens["milk"].value == "contains"


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_caller_extra_exclusions_union_with_customer_policy(offline_plan):
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=["soy"],
        quantity=40,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.need.excluded_allergens == ["soy", "milk", "nuts"]
    assert "Customer policy (school-tampere) excludes milk, nuts" in plan.explanation
    assert plan.lines and plan.shortfall == 0
    for ln in plan.lines:
        prod = get_product(ln.product_id)
        assert prod is not None
        for g in ("soy", "milk", "nuts"):
            assert prod.allergens[g].value == "free"
            assert prod.allergen_source[g].startswith("structured")


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_catalog_public_rank_substitutes_and_find_candidates_enforce_policy(offline_plan):
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=40,
        unit="L",
        delivery_by=DELIVERY,
    )
    rows, _ = find_candidates(need)
    assert any(
        (d.get("allergens") or {}).get("milk") == "free" and (d.get("allergens") or {}).get("nuts") == "free"
        for d in rows.values()
    )
    offers = _fake_offers()(list(rows)[:30], DELIVERY)
    cands = catalog_rank_substitutes(need, offers=offers, limit=10)
    eligible = [c for c in cands if c.tier != "rejected"]
    assert eligible
    for c in eligible:
        prod = get_product(c.product.id)
        assert prod is not None
        assert prod.allergens["milk"].value == "free"
        assert prod.allergens["nuts"].value == "free"
