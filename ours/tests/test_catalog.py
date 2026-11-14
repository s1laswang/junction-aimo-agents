import os
from pathlib import Path

import pytest

from aimo import catalog
from aimo.models import Constraint

DATA = Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json"
pytestmark = pytest.mark.skipif(not DATA.exists(), reason="needs Valio product data")


def test_loaded():
    assert len(catalog._load()["products"]) > 17000
    assert catalog.get("460000").name


def test_lactose_free_milk():
    res = catalog.search("laktoositon maito", Constraint(lactose_free=True), limit=500)
    both = [p for p in res if "laktoositon" in " ".join(catalog.names(p.product_id).values()).lower()
            and "maito" in " ".join(catalog.names(p.product_id).values()).lower()]
    assert len(both) >= 60
    assert len({p.product_id for p in res[:60]}) == 60
    for p in res:
        assert "lactose" not in p.allergens
    top = res[:40]
    assert all("laktoositon" in catalog.names(p.product_id).get("fi", "").lower()
               or "lactose free" in catalog.names(p.product_id).get("en", "").lower() for p in top)


def test_english_synonym():
    fi = {p.product_id for p in catalog.search("laktoositon maito", Constraint(lactose_free=True), limit=500)}
    en = {p.product_id for p in catalog.search("lactose-free milk", Constraint(lactose_free=True), limit=500)}
    assert len(fi & en) >= 60


def test_exclude_and_brand():
    res = catalog.search("maito", Constraint(exclude_allergens=["milk"]), limit=100)
    assert all("milk" not in p.allergens and "may_contain:milk" not in p.allergens for p in res)
    res = catalog.search("maito", Constraint(brands=["valio"]), limit=20)
    assert res and all("valio" in (p.brand or "").lower() for p in res)


def test_unit_math():
    p = catalog.get("462775")
    assert p.unit_size.unit == "l"
    assert catalog.units_needed(p, 1000, "litres") == 1000.0 / p.unit_size.value
    assert catalog.units_needed(p, 3, "kg") is None
    assert catalog.units_needed("462626", 1000, "l") == 200.0


def test_csv_lookups():
    assert catalog.reliability("460001") == 1.0
    assert catalog.co2_per_kg("460000") == 2.315


def _fi(p):
    return catalog.names(p.product_id).get("fi", "").lower()


def test_rank_plain_milk_first():
    for q in ("maito", "laktoositon maito", "lactose-free milk"):
        res = catalog.search(q, Constraint(lactose_free=True), limit=10)
        assert len(res) == 10
        for p in res:
            n = _fi(p)
            assert "maitorahka" not in n and "maitosuklaa" not in n and "kahvimaito" not in n
            assert "maito" in n or "maitojuoma" in n


def test_rank_head_before_modifier():
    res = catalog.search("maito", limit=300)
    names = [_fi(p) for p in res]
    first_mod = next(i for i, n in enumerate(names) if "maitorahka" in n)
    plain = [i for i, n in enumerate(names) if n.split()[-1:] and any(w in ("maito", "täysmaito", "kevytmaito") for w in n.split())]
    assert plain and min(plain) < first_mod


def _top(q, c=None, n=5):
    return [p.name.lower() for p in catalog.search(q, c, limit=n)]


def test_english_plural_and_phrase():
    assert any("peruna" in n for n in _top("potatoes"))
    assert all("jauheliha" in n for n in _top("minced meat"))


def test_food_beats_nonfood():
    assert all("veitsi" not in n and "koristelu" not in n for n in _top("salmon") + _top("rice"))
    assert all("lohi" in n or "salmon" in n for n in _top("salmon"))
    assert all("riisi" in n or "rice" in n for n in _top("rice"))


def test_modifier_compounds_lose():
    assert all("patukka" not in n and "bar" not in n for n in _top("apple"))
    assert all("kana" not in n for n in _top("pasta"))
    assert all("peruna" not in n for n in _top("flour", Constraint(gluten_free=True), 3))


def test_case_pack_unit_size_is_per_sales_unit():
    assert catalog.get("465039").unit_size.value == 5.0
    assert catalog.get("469287").unit_size.value == 21.0
    assert catalog.get("464119").unit_size.value == 10.0
