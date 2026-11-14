import csv
import json

import pytest

from aimo import catalog
from aimo.models import Constraint, UnitSize


def _item(
    pid,
    *,
    names,
    category="10400",
    brand="Valio",
    vendor="VALIO OY",
    origin="FI",
    sales_unit="PRK",
    base_unit="PRK",
    units=None,
    conversions=None,
    allergens=None,
    labels=None,
    claims=None,
    temperature=4,
    deleted=False,
):
    classifications = []
    if allergens:
        classifications.append({"name": "allergen", "values": allergens})
    if labels:
        classifications.append({"name": "packagingMarkedLabel", "values": [{"id": x} for x in labels]})
    if claims:
        classifications.append({"name": "nutritionalClaim", "values": [{"id": x} for x in claims]})
    return {
        "id": pid,
        "deleted": deleted,
        "category": category,
        "vendorName": vendor,
        "countryOfOrigin": origin,
        "temperatureCondition": temperature,
        "salesUnit": sales_unit,
        "baseUnit": base_unit,
        "units": units if units is not None else [{"unitId": base_unit, "sizeInBaseUnits": 1}],
        "synkkaData": {
            "brand": brand,
            "names": [{"language": k, "value": v} for k, v in names.items()],
            "classifications": classifications,
            "unitConversions": conversions or [],
        },
    }


def _synthetic_items():
    return [
        _item(
            "1001",
            names={
                "fi": "Valio Luomu Kevytmaito laktoositon",
                "en": "Valio Organic Low fat milk lactose free",
                "sv": "Valio Ekologisk Lättmjölk laktosfri",
            },
            category="10400",
            brand="Valio",
            vendor="VALIO OY",
            origin="FI",
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": 1.0, "unit": "ltr"}}],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
            labels=["ORGANIC", "coolTemperature"],
            claims=["FREE_FROM_LACTOSE"],
        ),
        _item(
            "1002",
            names={
                "fi": "Valio Maito laktoositon UHT",
            },
            category="10400",
            brand="Valio",
            vendor="VALIO OY",
            origin="FI",
            sales_unit="ME",
            base_unit="PRK",
            units=[
                {"unitId": "PRK", "sizeInBaseUnits": 1},
                {"unitId": "ME", "sizeInBaseUnits": 10},
            ],
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": 1000, "unit": "ml"}}],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
            claims=["FREE_FROM_LACTOSE"],
            temperature=20,
        ),
        _item(
            "1003",
            names={
                "fi": "Arla Täysmaito 1,5 l",
                "en": "Arla Whole milk 1.5 l",
                "sv": "Arla Helmjölk 1,5 l",
            },
            category="10400",
            brand="Arla",
            vendor="ARLA OY",
            origin="SE",
            allergens=[{"id": "AM", "unit": "CONTAINS"}, {"id": "ML", "unit": "CONTAINS"}],
        ),
        _item(
            "1004",
            names={
                "fi": "Valio Maitorahka laktoositon",
                "en": "Valio Quark lactose free",
                "sv": "Valio Kvarg laktosfri",
            },
            category="10420",
            brand="Valio",
            vendor="VALIO OY",
            origin="FI",
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"value": 250, "unit": "GRM"}}],
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
            claims=["FREE_FROM_LACTOSE"],
        ),
        _item(
            "1005",
            names={
                "fi": "Fazer Maitosuklaa maapähkinä 200 g",
                "en": "Fazer Milk chocolate peanut 200 g",
                "sv": "Fazer Mjölkchoklad jordnöt 200 g",
            },
            category="16200",
            brand="Fazer",
            vendor="FAZER MAKEISET OY",
            origin="FI",
            sales_unit="KPL",
            base_unit="KPL",
            allergens=[
                {"id": "AM", "unit": "CONTAINS"},
                {"id": "ML", "unit": "CONTAINS"},
                {"id": "AP", "unit": "CONTAINS"},
            ],
            temperature=18,
        ),
        _item(
            "1006",
            names={
                "fi": "Valio Kahvimaito laktoositon 2 dl",
                "en": "Valio Coffee milk lactose free 2 dl",
                "sv": "Valio Kaffemjölk laktosfri 2 dl",
            },
            category="10400",
            brand="Valio",
            vendor="VALIO OY",
            origin="FI",
            allergens=[{"id": "AM", "unit": "CONTAINS"}],
            claims=["FREE_FROM_LACTOSE"],
        ),
        _item(
            "1007",
            names={
                "fi": "Maito kaadin teräs",
                "en": "Milk pitcher stainless steel",
                "sv": "Mjölk kanna rostfritt stål",
            },
            category="64110",
            brand="Metos",
            vendor="METOS OY",
            origin="FI",
            sales_unit="KPL",
            base_unit="KPL",
            temperature=None,
        ),
        _item(
            "1008",
            names={
                "fi": "Snellman Naudan jauheliha",
                "en": "Snellman Minced beef",
                "sv": "Snellman Malet nötkött",
            },
            category="11100",
            brand="Snellman",
            vendor="SNELLMAN PRO OY",
            origin="FI",
            sales_unit="PKT",
            base_unit="PKT",
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"value": 2.0, "unit": "KGM"}}],
            claims=["FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN"],
        ),
        _item(
            "1009",
            names={
                "fi": "Suomalainen pesty peruna",
                "sv": "Finsk tvättad potatis",
            },
            category="14100",
            brand="Tuoreverkko",
            vendor="TUOREVERKKO OY",
            origin="FI",
            sales_unit="SAK",
            base_unit="KG",
            units=[
                {"unitId": "KG", "sizeInBaseUnits": 1},
                {"unitId": "SAK", "sizeInBaseUnits": 5},
            ],
            labels=["ORGANIC"],
        ),
        _item(
            "1010",
            names={
                "fi": "Moilas Gluteeniton vuokaleipä 400 g",
                "en": "Moilas Gluten free loaf bread 400 g",
                "sv": "Moilas Glutenfritt formbröd 400 g",
            },
            category="15100",
            brand="Moilas",
            vendor="MOILAS OY",
            origin="FI",
            sales_unit="PSS",
            base_unit="PSS",
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"value": 400, "unit": "GRM"}}],
            allergens=[{"id": "AN", "unit": "MAY_CONTAIN"}],
            claims=["FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"],
            temperature=-18,
        ),
        _item(
            "1011",
            names={
                "fi": "Vaasan Kauraleipä 500 g",
                "en": "Vaasan Oat bread 500 g",
                "sv": "Vaasan Havrebröd 500 g",
            },
            category="15100",
            brand="Vaasan",
            vendor="VAASAN OY",
            origin="FI",
            sales_unit="PSS",
            base_unit="PSS",
            conversions=[{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"value": 500, "unit": "GRM"}}],
            allergens=[
                {"id": "AW", "unit": "CONTAINS"},
                {"id": "UW", "unit": "CONTAINS"},
                {"id": "GO", "unit": "CONTAINS"},
                {"id": "AE", "unit": "MAY_CONTAIN"},
            ],
            temperature=20,
        ),
        _item(
            "1012",
            names={
                "fi": "Provenia Gluteeniton kaurajauho 1 kg",
                "en": "Provenia Gluten free oat flour 1 kg",
                "sv": "Provenia Glutenfritt havremjöl 1 kg",
            },
            category="15200",
            brand="Provenia",
            vendor="RAISIO OY",
            origin="FI",
            sales_unit="PKT",
            base_unit="PKT",
            claims=["FREE_FROM_GLUTEN"],
            temperature=20,
        ),
        _item(
            "1099",
            names={
                "fi": "Valio Poistettu laktoositon maito 1 l",
                "en": "Valio Discontinued lactose free milk 1 l",
            },
            category="10400",
            brand="Valio",
            vendor="VALIO OY",
            origin="FI",
            claims=["FREE_FROM_LACTOSE"],
            deleted=True,
        ),
    ]


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture(autouse=True)
def synthetic_data_dir(tmp_path, monkeypatch):
    catalog.reset()
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "catalog.db"))
    (tmp_path / "valio_aimo_junction26_product_data.json").write_text(
        json.dumps(_synthetic_items()), encoding="utf-8"
    )
    _write_csv(
        tmp_path / "valio_aimo_junction26_delivery_reliability.csv",
        ["productId", "deliveryReliability"],
        [
            ["1001", "0.99"],
            ["1002", "0.97"],
            ["1003", "0.95"],
            ["1004", "0.98"],
            ["1005", "0.92"],
            ["1006", "0.96"],
            ["1008", "0.94"],
            ["1009", "0.99"],
            ["1010", "0.91"],
            ["1011", "0.96"],
            ["1012", "0.93"],
        ],
    )
    _write_csv(
        tmp_path / "valio_aimo_junction26_carbon_footprint.csv",
        ["productId", "AVG kg CO2-eq/kg"],
        [
            ["1001", "1.25"],
            ["1002", "1.30"],
            ["1003", "1.40"],
            ["1004", "2.10"],
            ["1005", "4.80"],
            ["1006", "1.35"],
            ["1008", "22.50"],
            ["1009", "0.18"],
            ["1010", "1.05"],
            ["1011", "0.95"],
            ["1012", "0.85"],
        ],
    )
    try:
        yield tmp_path
    finally:
        catalog.reset()


def test_lookups_csv_and_deleted_skipped():
    all_products = catalog.search("", limit=50)
    assert len(all_products) == 12
    assert "1099" not in {p.product_id for p in all_products}

    assert catalog.get("1099") is None
    assert catalog.raw("1099") is None
    assert catalog.names("1099") == {}
    assert catalog.get("9999") is None
    assert catalog.raw("9999") is None
    assert catalog.names("9999") == {}

    p = catalog.get("1001")
    assert p is not None
    assert p.name == "Valio Luomu Kevytmaito laktoositon"
    assert p.brand == "Valio"
    assert p.vendor == "VALIO OY"
    assert p.category == "10400"
    assert p.origin == "FI"
    assert p.temperature == 4
    assert p.allergens == ["milk"]
    assert p.labels == ["organic", "free_from_lactose"]
    assert catalog.get("1005").allergens == ["milk", "lactose", "peanuts"]
    assert catalog.get("1010").allergens == ["may_contain:tree nuts"]
    assert catalog.get("1011").allergens == ["gluten", "wheat", "oats", "may_contain:egg"]

    r = catalog.raw("1001")
    assert r is not None and r["synkkaData"]["brand"] == "Valio"
    assert catalog.names("1001") == {
        "fi": "Valio Luomu Kevytmaito laktoositon",
        "en": "Valio Organic Low fat milk lactose free",
        "sv": "Valio Ekologisk Lättmjölk laktosfri",
    }

    assert catalog.reliability("1001") == 0.99
    assert catalog.reliability("1008") == 0.94
    assert catalog.reliability("1007") is None
    assert catalog.co2_per_kg("1001") == 1.25
    assert catalog.co2_per_kg("1008") == 22.50
    assert catalog.co2_per_kg("1007") is None


def test_reset_clears_state_and_reloads():
    assert catalog.get("1001") is not None
    assert catalog._state
    catalog.reset()
    assert catalog._state == {}
    assert catalog.get("1002") is not None
    assert len(catalog._state["products"]) == 12


def test_ranking_plain_milk_beats_modifiers_and_nonfood():
    for query in ("maito", "milk", "mjölk"):
        ids = [p.product_id for p in catalog.search(query, limit=20)]
        assert set(ids[:3]) == {"1001", "1002", "1003"}
        assert ids[3] == "1006"
        assert set(ids[4:6]) == {"1004", "1005"}
        assert ids[6] == "1007"
        assert "1099" not in ids


def test_trilingual_synonyms():
    en_milk = [p.product_id for p in catalog.search("lactose-free milk")]
    fi_milk = [p.product_id for p in catalog.search("laktoositon maito")]
    sv_milk = [p.product_id for p in catalog.search("laktosfri mjölk")]
    assert en_milk == fi_milk == sv_milk
    assert set(en_milk[:2]) == {"1001", "1002"}

    en_meat = [p.product_id for p in catalog.search("minced meat")]
    fi_meat = [p.product_id for p in catalog.search("jauheliha")]
    assert en_meat == fi_meat == ["1008"]

    en_pot = [p.product_id for p in catalog.search("potatoes")]
    fi_pot = [p.product_id for p in catalog.search("peruna")]
    sv_pot = [p.product_id for p in catalog.search("potatis")]
    assert en_pot == fi_pot == sv_pot == ["1009"]


def test_constraint_filtering():
    lf = {p.product_id for p in catalog.search("", Constraint(lactose_free=True))}
    assert lf == {"1001", "1002", "1004", "1006", "1008", "1010"}

    gf = {p.product_id for p in catalog.search("", Constraint(gluten_free=True))}
    assert gf == {"1008", "1010", "1012"}

    no_milk = {p.product_id for p in catalog.search("", Constraint(exclude_allergens=["milk"]))}
    assert no_milk.isdisjoint({"1001", "1002", "1003", "1004", "1005", "1006"})
    assert {"1007", "1008", "1009", "1010", "1011", "1012"} == no_milk

    no_egg = {p.product_id for p in catalog.search("", Constraint(exclude_allergens=["egg"]))}
    assert "1011" not in no_egg
    assert "1010" in no_egg

    no_peanuts = {p.product_id for p in catalog.search("", Constraint(exclude_allergens=["peanuts"]))}
    assert "1005" not in no_peanuts and "1010" in no_peanuts

    no_tree_nuts = {p.product_id for p in catalog.search("", Constraint(exclude_allergens=["tree nuts"]))}
    assert "1010" not in no_tree_nuts and "1005" in no_tree_nuts

    no_nuts = {p.product_id for p in catalog.search("", Constraint(exclude_allergens=["nuts"]))}
    assert "1005" not in no_nuts and "1010" not in no_nuts

    organic = {p.product_id for p in catalog.search("", Constraint(labels=["organic"]))}
    assert organic == {"1001", "1009"}

    finnish = {p.product_id for p in catalog.search("", Constraint(origin="Finland"))}
    assert "1003" not in finnish
    assert len(finnish) == 11

    valio = {p.product_id for p in catalog.search("", Constraint(brands=["Valio"]))}
    assert valio == {"1001", "1002", "1004", "1006"}

    combo = [
        p.product_id
        for p in catalog.search(
            "milk",
            Constraint(lactose_free=True, labels=["organic"], origin="Finland", brands=["Valio"]),
        )
    ]
    assert combo == ["1001"]


def test_unit_size_and_units_needed():
    p1 = catalog.get("1001")
    assert p1.unit_size == UnitSize(value=1.0, unit="l")
    assert catalog.units_needed(p1, 2500, "ml") == 3.0
    assert catalog.units_needed("1001", 2500, "ml") == 3.0
    assert catalog.units_needed("1001", 3, "kg") is None

    assert catalog.get("1002").unit_size == UnitSize(value=10.0, unit="l")
    assert catalog.units_needed("1002", 25, "litres") == 3.0
    assert catalog.units_needed("1002", 20, "l") == 2.0
    assert catalog.units_needed("1002", 2.2, "ME") == 3.0

    assert catalog.get("1003").unit_size == UnitSize(value=1.5, unit="l")
    assert catalog.units_needed("1003", 3.0, "l") == 2.0
    assert catalog.units_needed("1003", 3.1, "l") == 3.0
    assert catalog.get("1006").unit_size == UnitSize(value=0.2, unit="l")

    assert catalog.get("1004").unit_size == UnitSize(value=0.25, unit="kg")
    assert catalog.units_needed("1004", 600, "g") == 3.0
    assert catalog.get("1008").unit_size == UnitSize(value=2.0, unit="kg")
    assert catalog.units_needed("1008", 5, "kg") == 3.0

    assert catalog.get("1005").unit_size == UnitSize(value=0.2, unit="kg")
    assert catalog.units_needed("1005", 0.5, "kg") == 3.0
    assert catalog.get("1012").unit_size == UnitSize(value=1.0, unit="kg")

    assert catalog.get("1009").unit_size == UnitSize(value=5.0, unit="kg")
    assert catalog.units_needed("1009", 12, "kg") == 3.0

    assert catalog.get("1007").unit_size == UnitSize(value=1.0, unit="pcs")
    assert catalog.units_needed("1007", 4, "pcs") == 4.0
    assert catalog.units_needed("1007", 4, "kpl") == 4.0

    assert catalog.units_needed("9999", 1, "l") is None
    assert catalog.units_needed("1001", 1, "crates") is None

    multipack_vol = _item(
        "2001",
        names={"fi": "Mehu 3x200 ml"},
        sales_unit="LT",
        base_unit="PKT",
        units=[{"unitId": "PKT", "sizeInBaseUnits": 1}, {"unitId": "LT", "sizeInBaseUnits": 4}],
    )
    assert catalog._unit_size(multipack_vol, {"fi": "Mehu 3x200 ml"}) == UnitSize(value=2.4, unit="l")

    multipack_wgt = _item(
        "2002",
        names={"en": "Butter 4x250 g"},
        sales_unit="CS",
        base_unit="PKT",
        units=[{"unitId": "PKT", "sizeInBaseUnits": 1}, {"unitId": "CS", "sizeInBaseUnits": 2}],
    )
    assert catalog._unit_size(multipack_wgt, {"en": "Butter 4x250 g"}) == UnitSize(value=2.0, unit="kg")

    unmapped = _item("2003", names={"fi": "Test 1 l"}, sales_unit="BOX", base_unit="PRK", units=[])
    assert catalog._unit_size(unmapped, {"fi": "Test 1 l"}) is None
