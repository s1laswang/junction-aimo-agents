from aimo import catalog
from aimo.models import Constraint


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
