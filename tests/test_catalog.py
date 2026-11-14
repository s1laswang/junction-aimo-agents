"""Fast catalog tests. Unit/allergen/tier tests use inline fixtures copied from the real product
JSON (fields trimmed), so they run without data/. Data-dependent tests skip if the catalog is absent."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aimo.catalog import units as U
from aimo.catalog.allergens import blocking, derive
from aimo.models import Need, Offer


def raw(pid, sales, base, names_en, units, conv=None, cls=None, brand="Valio", cat="10400"):
    return {"id": pid, "salesUnit": sales, "baseUnit": base, "category": cat, "allowedLotSize": 0.01,
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {"brand": brand, "names": [{"language": "en", "value": names_en}],
                           "unitConversions": conv or [], "classifications": cls or []}}


BASE_1L = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0},
            "netWeight": {"unit": "GRM", "value": 1025.0}}]

MILKS = {
    # id: (raw, expected litres per sales unit)
    "464306": (raw("464306", "ST", "ST", "Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free",
                   [("KRS", 160), ("PAL", 160), ("KI", 20), ("ST", 1)], BASE_1L), 1.0),
    "463038": (raw("463038", "KI", "ST", "Valio Eila® semi skimmed milk drink 10 l novobox lactose free",
                   [("KI", 2), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]), 20.0),
    "465039": (raw("465039", "KI", "ST", "Valio Eila® semi skimmed milk drink 1 l lactose free",
                   [("PAL", 160), ("KI", 5), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]), 5.0),
    "469287": (raw("469287", "KI", "ST", "Valio Eila® semi skimmed milk drink 1,75 l lactose free",
                   [("KRS", 96), ("PAL", 96), ("KI", 12), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]), 21.0),
    "464179": (raw("464179", "ST", "ST", "Arla 10 L Novobox Suomi semi skimmed lactose free milk drink ESL",
                   [("ST", 1)], [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 10.0}}],
                   brand="Arla"), 10.0),
    "463203": (raw("463203", "KI", "KI", "Arla Portion milk 100x2 cl lactose free 1,5% UHT", [("PAL", 114), ("KI", 1)],
                   [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 2.0}}], brand="Arla"), 2.0),
    "464399": (raw("464399", "KI", "ST", "14 x Arla Low fat milkdrink 2 dl Suomesta UHT lactose free",
                   [("PAL", 4004), ("KI", 14), ("ST", 1)], [{"synkkaPackagingType": "CASE"}], brand="Arla"), 2.8),
    "463779": (raw("463779", "KI", "ST", "Valio Eila® semi skimmed milk drink 2,5 dl UHT lactose free",
                   [("KI", 15), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]), 3.75),
}


@pytest.mark.parametrize("pid", list(MILKS))
def test_content_per_sales_unit_milks(pid):
    r, expected = MILKS[pid]
    c = U.compute_content(r)
    assert c.unit == "L"
    assert c.amount == pytest.approx(expected), (pid, c)


def test_parse_name_quantity():
    q, n = U.parse_name_quantity("Valio Eila 1,75 l lactose free")
    assert (q.amount, q.unit, n) == (1.75, "L", 1)
    q, n = U.parse_name_quantity("Arla Portion milk 100x2 cl")
    assert q.amount == pytest.approx(0.02) and n == 100
    q, n = U.parse_name_quantity("4 x 6 x Hartwall Vichy 0,33 l")
    assert q.amount == pytest.approx(0.33) and n == 24
    q, n = U.parse_name_quantity("Forssan Potato Salad 400g")
    assert (q.amount, q.unit) == (pytest.approx(0.4), "KG")
    q, n = U.parse_name_quantity("Valio Oltermanni e1 kg")
    assert (q.amount, q.unit) == (1.0, "KG")
    assert U.parse_name_quantity("Valio Eila semi skimmed lactose free")[0] is None


def test_sold_by_kg_is_one_kg():
    r = raw("x", "KG", "KG", "HK Beef Sirloin 3,5-5 kg", [("KG", 1)],
            [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 4.5}}])
    c = U.compute_content(r)
    assert (c.amount, c.unit) == (1.0, "KG")


def test_drained_weight():
    r = raw("x", "PRK", "PRK", "Biona Organic Sweetcorn in water 340g/285g", [("PRK", 1)],
            [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "GRM", "value": 340}}])
    assert U.compute_content(r).amount == pytest.approx(0.285)


def test_reconcile_with_price_prefers_price_when_disagreeing():
    # 463038: KI at 31.31 with comparison 1.57/L -> ~20 L; static 20 L agrees (within rounding)
    assert U.reconcile_with_price(20.0, "L", 31.31, 1.57, "L") == (20.0, "L", None)
    # if static had said 10 L (the classic bug), price wins and snaps to 20
    amt, unit, note = U.reconcile_with_price(10.0, "L", 31.31, 1.57, "L")
    assert (amt, unit) == (20.0, "L") and note
    # no comparison price -> keep static
    assert U.reconcile_with_price(5.0, "L", 6.86, None, None) == (5.0, "L", None)


def _cls(allergen=(), non=(), claims=()):
    return [{"name": "allergen", "type": "ENUM", "values": [{"id": c, "unit": u} for c, u in allergen]},
            {"name": "nonAllergen", "type": "ENUM", "values": [{"id": c, "unit": "FREE_FROM"} for c in non]},
            {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": c, "unit": "FREE_FROM"} for c in claims]}]


def test_allergen_mapping_tristate():
    st, src, raw_ = derive(_cls(allergen=[("AM", "CONTAINS"), ("UW", "MAY_CONTAIN"), ("SH", "MAY_CONTAIN")],
                                non=["AE", "AP", "ML"]))
    assert st["milk"] == "contains" and src["milk"] == "structured"
    assert st["gluten"] == "may_contain"          # wheat code UW maps to gluten group
    assert st["nuts"] == "may_contain"            # hazelnut SH maps to tree nuts
    assert st["eggs"] == "free" and st["peanuts"] == "free"
    assert st["fish"] == "unknown" and src["fish"] == "none"   # missing is unknown, never free
    assert set(st) == {"gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts", "celery",
                       "mustard", "sesame", "sulphites", "lupin", "molluscs"}


def test_lactose_free_does_not_mean_milk_free():
    st, _, _ = derive(_cls(non=["ML"], claims=["FREE_FROM_LACTOSE"]))
    assert st["milk"] == "unknown"


def test_gluten_free_claim_and_undeclared():
    st, src, _ = derive(_cls(allergen=[("AC", "UNDECLARED")], claims=["FREE_FROM_GLUTEN"]))
    assert st["gluten"] == "free" and src["gluten"] == "structured:claim"
    assert st["crustaceans"] == "unknown"


def test_blocking_unknown_is_not_safe():
    assert blocking({"milk": "free"}, ["milk"]) == []
    assert blocking({"milk": "unknown"}, ["milk"]) == [("milk", "unknown")]
    assert blocking({}, ["nuts"]) == [("nuts", "unknown")]


# ------------------------------------------------------------------ tiers (no network, no DB)
def _row(pid, name, brand, fat, content, sales="ST", claims=("FREE_FROM_LACTOSE",), cat="10400", allergens=None):
    return {"id": pid, "name": name, "name_en": name, "name_fi": None, "brand": brand, "fat_pct": fat,
            "content_amount": content, "content_unit": "L", "sales_unit": sales, "sales_unit_size": 1,
            "base_content_amount": content, "claims": list(claims), "labels": [], "category": cat,
            "allergens": allergens or {"milk": "contains"}, "delivery_reliability": 0.98, "co2_kg_per_kg": 1.1}


def _offer(pid, price, cmp_price, cutoff=False, amount=9999999999.0):
    now = datetime.now(timezone.utc)
    return Offer(product_id=pid, requested_date=date(2026, 11, 16), available=not cutoff, available_amount=amount,
                 cutoff_passed=cutoff, closing_time=now - timedelta(days=2) if cutoff else now + timedelta(days=1),
                 logistic_model="Terminal" if cutoff else "Stock", unit_price=price, comparison_price=cmp_price,
                 comparison_unit="L", fetched_at=now)


def test_tiers_good_better_best():
    from aimo.catalog.plan import fill
    from aimo.catalog.substitution import rank_substitutes
    rows = {
        "463038": _row("463038", "Valio Eila semi skimmed milk drink 10 l novobox lactose free", "Valio", 1.5, 20.0, "KI"),
        "464306": _row("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", "Valio", 1.5, 1.0),
        "469363": _row("469363", "Arla Semi skimmed milkdrink 1 L UHT lactose free", "Arla", 1.5, 1.0),
        "463335": _row("463335", "Valio Eila 3 % milk drink 1 l lactose free", "Valio", 3.0, 1.0),
        "469541": _row("469541", "12 x Arla Semi skimmed milk 1 L low lactose", "Arla", 1.5, 12.0, "KI", claims=("LOW_LACTOSE",)),
        "463380": _row("463380", "Arla Cafè milk 1 L UHT lactose free", "Arla", 1.5, 1.0),
    }
    offers = {"463038": _offer("463038", 31.31, 1.57, cutoff=True), "464306": _offer("464306", 1.01, 1.01),
              "469363": _offer("469363", 2.48, 2.48, amount=384), "463335": _offer("463335", 1.40, 1.40),
              "469541": _offer("469541", 14.0, 1.17), "463380": _offer("463380", 1.5, 1.5)}
    need = Need(customer_id="hotel-aurora", product_query="lactose-free milk", required_claims=["FREE_FROM_LACTOSE"],
                quantity=1000, unit="L", delivery_by=date(2026, 11, 16), preferred_product_ids=["463038"])
    cands = rank_substitutes(need, offers, rows=rows, relevance={k: 1.0 for k in rows})
    tier = {c.product.id: c for c in cands}
    assert tier["463038"].tier == "rejected" and "cutoff_passed" in tier["463038"].flags
    assert tier["464306"].tier == "best"
    assert tier["469363"].tier == "better" and "partial_stock" in tier["469363"].flags
    assert tier["463335"].tier == "good"
    assert tier["469541"].tier == "rejected" and "LOW_LACTOSE" in tier["469541"].reasons[0]
    plan = fill(need, cands)
    assert [(l.product_id, l.qty_sales_units) for l in plan.lines] == [("464306", 1000.0)]
    assert plan.total_net == pytest.approx(1010.0) and plan.shortfall == 0


def test_allergen_exclusion_rejects_unknown():
    from aimo.catalog.substitution import rank_substitutes
    rows = {"a": _row("a", "Oat drink 1 l", "Oatly", 1.5, 1.0, claims=(), allergens={"milk": "free", "gluten": "unknown"}),
            "b": _row("b", "Rice drink 1 l", "X", 1.0, 1.0, claims=(), allergens={"milk": "unknown"})}
    offers = {"a": _offer("a", 1.5, 1.5), "b": _offer("b", 1.2, 1.2)}
    need = Need(customer_id="school-tampere", product_query="drink", excluded_allergens=["milk"], quantity=10,
                unit="L", delivery_by=date(2026, 11, 16))
    c = {x.product.id: x for x in rank_substitutes(need, offers, rows=rows, relevance={"a": 1, "b": 1})}
    assert c["a"].tier != "rejected"
    assert c["b"].tier == "rejected" and "allergen_unknown" in c["b"].flags


# ------------------------------------------------------------------ data-dependent (skips without data/)
def _have_data():
    from aimo.config import PRODUCT_JSON
    return PRODUCT_JSON.exists()


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_search_finds_demo_milks_en_and_fi():
    from aimo.catalog import search
    for q in ("lactose-free milk", "laktoositon maito"):
        ids = {p.id for p in search(q, claims=["FREE_FROM_LACTOSE"], content_unit="L", limit=40)}
        assert {"464306", "463038", "465039", "464398", "463335", "469363", "462775"} <= ids, (q, ids)


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_get_product_allergens():
    from aimo.catalog import get_product
    p = get_product("464306")
    assert p is not None and p.content_amount == 1.0 and p.content_unit == "L"
    assert p.allergens["milk"].value == "contains"
    assert len(p.allergens) == 14


def test_clean_query_strips_allergen_mentions():
    from aimo.catalog.textsearch import clean_query, parse_query
    assert clean_query("oat drink, pupils have milk and nut allergies") == "oat drink"
    assert clean_query("milk-free oat drink") == "oat drink"
    assert clean_query("oat drink without milk or nuts") == "oat drink"
    assert clean_query("bread for coeliac patients") == "bread"
    assert clean_query("lactose-free milk") == "lactose-free milk"      # positive claims are kept
    assert clean_query("gluten-free bread") == "gluten-free bread"
    concepts = [c for c, _ in parse_query("Oat drink for a school, pupils have milk and nut allergies, 200 L by Wednesday")]
    assert concepts == ["oat", "drink"] and "milk" not in concepts


def _fake_offers(cutoff_ids=()):
    """Offline offers_fn: every product orderable at €1.50 per sales unit unless listed in cutoff_ids."""
    def fn(ids, d, customer_id=None):
        now = datetime.now(timezone.utc)
        return {pid: Offer(product_id=pid, requested_date=d, available=pid not in cutoff_ids,
                           cutoff_passed=pid in cutoff_ids, closing_time=now - timedelta(days=1) if pid in cutoff_ids
                           else now + timedelta(days=1), logistic_model="Stock", available_amount=9999999999.0,
                           unit_price=1.5, fetched_at=now) for pid in ids}
    return fn


@pytest.fixture
def offline_plan(monkeypatch):
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    from aimo.catalog import textsearch
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query", ["oat drink", "by Wednesday", "milk-free oat drink",
                                   "oat drink, pupils have milk and nut allergies"])
def test_school_oat_drink_with_allergy_text(offline_plan, query):
    """Buyer bug: allergy mentions in raw_text/product_query must not turn the request into 'milk'."""
    from aimo.catalog import get_product
    from aimo.catalog.plan import plan_need
    raw = "Oat drink for a school, pupils have milk and nut allergies, 200 L by Wednesday"
    need = Need(customer_id="school-tampere", raw_text=raw, product_query=query, excluded_allergens=["milk", "nuts"],
                quantity=200, unit="L", delivery_by=date(2026, 11, 18))
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.lines and plan.shortfall == 0, plan.explanation
    assert plan.need.product_query == query                       # the caller's need is returned unchanged
    for line in plan.lines:
        p = get_product(line.product_id)
        assert "oat" in p.name.lower()
        assert p.allergens["milk"].value == "free" and p.allergens["nuts"].value == "free"


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query", ["gluten free bread", "bread", "pasta"])
def test_hospital_gluten_free_bread_and_pasta(offline_plan, query):
    from aimo.catalog import get_product
    from aimo.catalog.plan import plan_need
    need = Need(customer_id="hospital-meilahti", raw_text=f"Gluten-free {query} for coeliac patients, 20 kg by Tuesday",
                product_query=query, excluded_allergens=["gluten"], quantity=20, unit="KG",
                delivery_by=date(2026, 11, 17))
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.lines and plan.shortfall == 0, plan.explanation
    for line in plan.lines:
        assert get_product(line.product_id).allergens["gluten"].value == "free"


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_shortfall_explanation_is_actionable(offline_plan):
    from aimo.catalog.plan import find_candidates, plan_need
    need = Need(customer_id="hospital-meilahti", product_query="gluten free bread", excluded_allergens=["gluten"],
                quantity=20, unit="KG", delivery_by=date(2026, 11, 17))
    all_ids = tuple(find_candidates(need)[0])
    plan = plan_need(need, offers_fn=_fake_offers(cutoff_ids=all_ids))   # everything missed the cut-off
    assert not plan.lines and plan.shortfall == 20
    assert "missed the order cut-off" in plan.explanation
    assert "re-plan for a later delivery date" in plan.explanation


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("prefs", [{}, {"parsed_by": "gemini"}, {"parsed_by": "regex", "delivery_date_assumed": True,
                                                                 "injection_flags": []}])
def test_oat_drink_non_spec_preferences_do_not_drop_type_check(offline_plan, prefs):
    """Buyer round-3 repro: preferences={'parsed_by': 'gemini'} made the plan pick 18 x Pepsi Max."""
    from aimo.catalog import get_product
    from aimo.catalog.plan import plan_need
    need = Need(customer_id="school-tampere", product_query="oat drink", excluded_allergens=["milk", "nuts"],
                quantity=120, unit="L", delivery_by=date(2026, 11, 18), preferences=prefs)
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.lines and plan.shortfall == 0, plan.explanation
    for line in plan.lines:
        assert "oat" in get_product(line.product_id).name.lower(), line.product_name
    for c in plan.candidates:
        if c.tier != "rejected":
            n = c.product.name.lower()
            assert "oat" in n or "kaura" in n or "havre" in n, c.product.name   # no soft drinks for 'oat drink'


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_cream_40l_lactose_free_with_parser_preferences(offline_plan):
    from aimo.catalog.plan import plan_need
    need = Need(customer_id="hotel-aurora", product_query="cream", required_claims=["FREE_FROM_LACTOSE"], quantity=40,
                unit="L", delivery_by=date(2026, 11, 18), preferences={"parsed_by": "gemini"})
    plan = plan_need(need, offers_fn=_fake_offers())
    assert plan.lines and plan.shortfall == 0, plan.explanation
    assert all("cream" in l.product_name.lower() for l in plan.lines)


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_brand_preference_without_usual_product(offline_plan):
    from aimo.catalog.plan import plan_need
    need = Need(customer_id="hotel-aurora", product_query="lactose-free milk", required_claims=["FREE_FROM_LACTOSE"],
                quantity=100, unit="L", delivery_by=date(2026, 11, 16),
                preferences={"brand": "Arla", "parsed_by": "gemini"})
    plan = plan_need(need, offers_fn=_fake_offers())
    best = [c for c in plan.candidates if c.tier == "best"]
    assert best and all((c.product.brand or "").strip() == "Arla" for c in best)
    assert all("milk" in c.product.name.lower() for c in best)
    assert plan.lines[0].tier == "best" and plan.shortfall == 0


@pytest.mark.skipif(not _have_data(), reason="product data not present")
@pytest.mark.parametrize("query", ["maito", "milk", "laktoositon maito"])
def test_milk_queries_rank_milk_drinks_above_jugs_and_cleaners(query):
    """Pitch QA: 'maito' ranked milk jugs (466434) and a milk-system cleaner above actual milk."""
    from aimo.catalog import search
    top = search(query, limit=5)
    assert len(top) == 5
    for p in top:
        assert p.category == "10400", (query, p.id, p.name)          # milk drinks
        assert "milk" in p.name.lower(), (query, p.name)
        assert not any(w in p.name.lower() for w in ("jug", "cleaner", "kannu"))


@pytest.mark.skipif(not _have_data(), reason="product data not present")
def test_equipment_queries_still_find_equipment():
    from aimo.catalog import search
    assert any("jug" in p.name.lower() for p in search("milk jug", limit=5))


def test_spec_preferences_ignore_non_spec_keys():
    from aimo.catalog.substitution import spec_preferences
    need = Need(customer_id="x", product_query="oat drink", quantity=1, unit="L", delivery_by=date(2026, 11, 18),
                preferences={"parsed_by": "gemini", "standing_order_qty_per_week": 300, "brand": "Oatly", "use": ""})
    assert spec_preferences(need) == {"brand": "Oatly"}


def test_gemini_inferred_allergens_never_make_free(monkeypatch):
    from aimo.catalog import gemini_allergens, product_from_row
    monkeypatch.setattr(gemini_allergens, "cached_inference",
                        lambda pid: {"milk": "not_found", "gluten": "contains", "eggs": "not_found"})
    d = {"id": "x", "name": "Test", "sales_unit": "ST", "claims": [], "labels": [],
         "allergens": {"milk": "unknown", "gluten": "unknown", "eggs": "free"},
         "allergen_source": {"milk": "none", "gluten": "none", "eggs": "structured"}}
    p = product_from_row(d)
    assert p.allergens["milk"].value == "unknown" and p.allergen_source["milk"] == "inferred:gemini"
    assert p.allergens["gluten"].value == "contains" and p.allergen_source["gluten"] == "inferred:gemini"
    assert p.allergens["eggs"].value == "free" and p.allergen_source["eggs"] == "structured"  # structured wins


def test_no_usual_product_tiers_by_query_match():
    from aimo.catalog.substitution import rank_substitutes
    rows = {"a": _row("a", "Oatly Oat Drink Semi 1L", "Oatly", 1.5, 1.0, claims=(), allergens={"milk": "free"}),
            "b": _row("b", "Oatly Barista 1L", "Oatly", 3.0, 1.0, claims=(), allergens={"milk": "free"})}
    offers = {"a": _offer("a", 1.4, 1.4), "b": _offer("b", 1.6, 1.6)}
    need = Need(customer_id="school-tampere", product_query="oat drink", excluded_allergens=["milk"], quantity=50,
                unit="L", delivery_by=date(2026, 11, 16))
    c = {x.product.id: x for x in rank_substitutes(need, offers, rows=rows, relevance={"a": 2, "b": 1})}
    assert c["a"].tier == "best" and "no usual product" in c["a"].reasons[0]
    assert "b" not in c or c["b"].tier != "best"   # 'Barista' lacks 'drink': not an exact match
