from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

import pytest

from aimo.backend import catalog_store as cs
from aimo.catalog import product_from_row
from aimo.catalog import units as U
from aimo.catalog.textsearch import summary_from_row
from aimo.models import EU14

ROOT = Path(__file__).resolve().parents[1]


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    self_src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert "\u2014" not in self_src
    assert "\u2013" not in self_src
    for rel in ("aimo/backend/catalog_store.py", "aimo/catalog/units.py"):
        owned = (ROOT / rel).read_text(encoding="utf-8")
        assert "\u2014" not in owned


@pytest.mark.parametrize(
    "bad_raw",
    [None, [], (), "464306", 123, 0, True, False, 3.14],
)
def test_normalize_rejects_non_dict_raw_with_value_error(bad_raw: Any) -> None:
    with pytest.raises(ValueError, match="dict"):
        cs.normalize(bad_raw)


@pytest.mark.parametrize(
    "bad_id",
    [None, "", " ", "   \t\n  ", True, False, 0, 464306, 1.5, ["464306"], {"id": "464306"}],
)
def test_normalize_rejects_missing_or_bad_id_with_value_error(bad_id: Any) -> None:
    raw = {} if bad_id is None else {"id": bad_id}
    with pytest.raises(ValueError, match="product id"):
        cs.normalize(raw)


def test_normalize_missing_id_key_raises_value_error_not_key_error() -> None:
    with pytest.raises(ValueError):
        cs.normalize({"salesUnit": "ST"})


@pytest.mark.parametrize(
    "bad_synkka",
    [None, "", "not-a-dict", [], [1, 2], 42, True, False],
)
def test_normalize_handles_non_dict_synkka_data(bad_synkka: Any) -> None:
    row = cs.normalize({"id": "  p-100  ", "synkkaData": bad_synkka})
    assert row["id"] == "p-100"
    assert row["name"] == "p-100"
    assert row["claims"] == []
    assert row["labels"] == []
    assert row["nutrition"] == {}
    assert row["substitutes"] == []
    assert set(row["allergens"]) == set(EU14)


def test_normalize_guards_malformed_nested_containers_and_items() -> None:
    raw: dict[str, Any] = {
        "id": "p-malformed",
        "salesUnitGtin": 12345,
        "vendorName": ["Valio"],
        "category": True,
        "salesUnit": 999,
        "baseUnit": {"unit": "ST"},
        "countryOfOrigin": ["FI"],
        "substitutions": [
            None,
            "bad-item",
            123,
            {"substituteProduct": None},
            {"substituteProduct": ""},
            {"substituteProduct": "   "},
            {"substituteProduct": True},
            {"substituteProduct": ["464306"]},
            {"substituteProduct": " 464306 "},
            {"substituteProduct": "464306"},
            {"substituteProduct": "465039"},
        ],
        "synkkaData": {
            "gtin": " 06408430043064 ",
            "brand": ["Valio"],
            "variableUnit": "false",
            "names": [
                None,
                "str-item",
                123,
                {"language": "en", "value": None},
                {"language": "en", "value": "   "},
                {"language": "en", "value": 999},
                {"language": "fi", "value": "Valio kevytmaito 1 l"},
            ],
            "classifications": [
                None,
                "bad-cls",
                123,
                {"name": None, "values": []},
                {"name": 123, "values": []},
                {"name": "nutritionalClaim", "values": "not-a-list"},
                {
                    "name": "nutritionalClaim",
                    "values": [
                        None,
                        "bad-val",
                        {"id": None},
                        {"id": ""},
                        {"id": True},
                        {"id": ["unhashable"]},
                        {"id": "FREE_FROM_LACTOSE", "unit": 123},
                        {"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"},
                    ],
                },
                {
                    "name": "allergen",
                    "values": [
                        {"id": ["bad"], "unit": 999},
                        {"id": "AM", "unit": "CONTAINS"},
                        {"id": "UW", "unit": None},
                    ],
                },
                {
                    "name": "packagingMarkedLabel",
                    "values": [
                        {"id": "coolTemperature"},
                        {"id": "EU_ORGANIC_FARMING"},
                        {"id": 1234},
                    ],
                },
            ],
            "nutritionalContent": "not-a-list",
            "keyIngredients": {"language": "en", "value": "milk"},
            "ingredients": [
                None,
                "water",
                123,
                {"sequence": "second", "names": [{"language": "en", "value": "salt"}]},
                {"sequence": True, "names": [{"language": "en", "value": "oats"}]},
                {"sequence": float("nan"), "names": [{"language": "en", "value": "enzymes"}]},
                {"sequence": -1, "names": [{"language": "en", "value": "water"}]},
                {"sequence": [1, 2], "names": "not-a-list"},
            ],
            "marketingTexts": 42,
        },
    }
    row = cs.normalize(raw)
    assert row["id"] == "p-malformed"
    assert row["gtin"] == "06408430043064"
    assert row["brand"] is None
    assert row["vendor"] is None
    assert row["category"] is None
    assert row["sales_unit"] == ""
    assert row["base_unit"] is None
    assert row["country_of_origin"] is None
    assert row["variable_unit"] == 0
    assert row["name"] == "Valio kevytmaito 1 l"
    assert row["claims"] == ["FREE_FROM_LACTOSE", "ORGANIC"]
    assert row["labels"] == ["EU_ORGANIC_FARMING"]
    assert row["temperature_label"] == "coolTemperature"
    assert row["allergens"]["milk"] == "contains"
    assert row["ingredients_text"] == "water, salt, oats, enzymes"
    assert row["marketing_text"] is None
    assert row["substitutes"] == ["464306", "465039"]
    assert row["fat_pct"] == 1.5


@pytest.mark.parametrize(
    "bad_container",
    [None, "bad", 123, True, {"a": 1}],
)
def test_normalize_handles_non_list_ingredients_and_classifications(bad_container: Any) -> None:
    raw = {
        "id": "p-containers",
        "substitutions": bad_container,
        "synkkaData": {
            "names": bad_container,
            "classifications": bad_container,
            "nutritionalContent": bad_container,
            "keyIngredients": bad_container,
            "ingredients": bad_container,
            "marketingTexts": bad_container,
        },
    }
    row = cs.normalize(raw)
    assert row["id"] == "p-containers"
    assert row["ingredients_text"] is None
    assert row["substitutes"] == []


@pytest.mark.parametrize(
    "bad_lot",
    [0, 0.0, -0.0, -1, -0.01, True, False, math.nan, math.inf, -math.inf, "0", "-2", "nan", "inf", "-inf", "abc", "", [], {}],
)
def test_normalize_rejects_bad_allowed_lot_size_to_none(bad_lot: Any) -> None:
    row = cs.normalize({"id": "p-lot", "allowedLotSize": bad_lot})
    assert row["allowed_lot_size"] is None


@pytest.mark.parametrize(
    "good_lot,expected",
    [(0.01, 0.01), (1, 1.0), (5.5, 5.5), ("0.25", 0.25)],
)
def test_normalize_accepts_positive_finite_allowed_lot_size(good_lot: Any, expected: float) -> None:
    row = cs.normalize({"id": "p-lot", "allowedLotSize": good_lot})
    assert row["allowed_lot_size"] == pytest.approx(expected)
    assert not isinstance(row["allowed_lot_size"], bool)


@pytest.mark.parametrize(
    "bad_tc",
    [None, True, False, math.nan, math.inf, -math.inf, 4.5, "4.5", "warm", "", "   ", 10**30, -(10**30), [], {}],
)
def test_normalize_rejects_bad_temperature_condition_to_none(bad_tc: Any) -> None:
    row = cs.normalize({"id": "p-tc", "temperatureCondition": bad_tc})
    assert row["temperature_condition"] is None


@pytest.mark.parametrize(
    "good_tc,expected",
    [(4, 4), (0, 0), (-18, -18), (8.0, 8), ("5", 5), (" -20 ", -20)],
)
def test_normalize_accepts_valid_int_temperature_condition(good_tc: Any, expected: int) -> None:
    row = cs.normalize({"id": "p-tc", "temperatureCondition": good_tc})
    assert row["temperature_condition"] == expected
    assert type(row["temperature_condition"]) is int


def test_normalize_sanitizes_nutrition_values_and_json_dumps_no_nan() -> None:
    raw = {
        "id": "p-nutr",
        "synkkaData": {
            "classifications": [
                {
                    "name": "nutritionFact",
                    "values": [
                        {"id": "FAT", "value": "nan"},
                        {"id": "ENER-", "value": float("inf")},
                        {"id": "CHOAVL", "value": -3.0},
                        {"id": "SUGAR-", "value": True},
                        {"id": "FIBTG", "value": "unparseable"},
                        {"id": "PRO-", "value": "3.4"},
                        {"id": "SALTEQ", "value": -0.0},
                    ],
                }
            ],
            "nutritionalContent": [
                {"id": "FAT", "value": 1.5, "basisQuantity": 100},
                {"id": "LACS", "value": False, "basisQuantity": 100},
                {"id": "CA", "value": float("-inf"), "basisQuantity": None},
                {"id": "FE", "value": 12.0, "basisQuantity": True},
                {"id": "ZN", "value": 5.0, "basisQuantity": 250},
                {"id": "PRO-", "value": 9.9, "basisQuantity": 100.0},
            ],
        },
    }
    row = cs.normalize(raw)
    assert row["nutrition"] == {"PRO-": 3.4, "SALTEQ": 0.0, "FAT": 1.5}
    assert row["fat_pct"] == 1.5
    dumped = json.dumps(row["nutrition"], allow_nan=False)
    assert json.loads(dumped) == {"PRO-": 3.4, "SALTEQ": 0.0, "FAT": 1.5}


@pytest.mark.parametrize(
    "bad_rel",
    [None, True, False, -0.01, -1.0, 1.01, 2.5, 100.0, math.nan, math.inf, -math.inf, "nan", "inf", "bad", []],
)
def test_normalize_rejects_bad_delivery_reliability(bad_rel: Any) -> None:
    row = cs.normalize({"id": "p1"}, reliability={"p1": bad_rel})
    assert row["delivery_reliability"] is None


@pytest.mark.parametrize("good_rel", [0.0, 0.5, 0.985, 1.0])
def test_normalize_accepts_valid_delivery_reliability(good_rel: float) -> None:
    row = cs.normalize({"id": "p1"}, reliability={"p1": good_rel})
    assert row["delivery_reliability"] == pytest.approx(good_rel)


@pytest.mark.parametrize(
    "bad_co2",
    [None, True, False, -0.001, -10.0, math.nan, math.inf, -math.inf, "nan", "inf", "bad", {}],
)
def test_normalize_rejects_bad_co2(bad_co2: Any) -> None:
    row = cs.normalize({"id": "p1"}, co2={"p1": bad_co2})
    assert row["co2_kg_per_kg"] is None


@pytest.mark.parametrize("good_co2", [0.0, 0.77, 14.67])
def test_normalize_accepts_valid_co2(good_co2: float) -> None:
    row = cs.normalize({"id": "p1"}, co2={"p1": good_co2})
    assert row["co2_kg_per_kg"] == pytest.approx(good_co2)


def test_normalize_ignores_non_dict_reliability_and_co2_containers() -> None:
    bad_rel: Any = "bad"
    bad_co2: Any = ["bad"]
    row = cs.normalize({"id": "p1"}, reliability=bad_rel, co2=bad_co2)
    assert row["delivery_reliability"] is None
    assert row["co2_kg_per_kg"] is None


def test_read_csv_filters_bad_values_and_handles_missing_or_directory(tmp_path: Path) -> None:
    assert cs._read_csv(tmp_path / "does_not_exist.csv") == {}
    assert cs._read_csv(tmp_path) == {}
    bad_path: Any = None
    assert cs._read_csv(bad_path) == {}

    rel_csv = tmp_path / "rel.csv"
    rel_csv.write_text(
        "productId,deliveryReliability\n"
        "p_zero,0.0\n"
        "p_ok,0.975\n"
        "p_one,1.0\n"
        "p_over,1.25\n"
        "p_neg,-0.1\n"
        "p_nan,NaN\n"
        "p_inf,Infinity\n"
        "p_ninf,-Infinity\n"
        "p_bool,true\n"
        "p_bad,abc\n"
        " ,0.8\n"
        "short_row\n",
        encoding="utf-8",
    )
    rel = cs._read_csv(rel_csv, max_val=1.0)
    assert rel == {"p_zero": 0.0, "p_ok": 0.975, "p_one": 1.0}

    co2_unbounded = cs._read_csv(rel_csv)
    assert co2_unbounded == {"p_zero": 0.0, "p_ok": 0.975, "p_one": 1.0, "p_over": 1.25}


def test_row_to_dict_handles_sqlite_row_dict_and_corrupt_json() -> None:
    none_row: Any = None
    int_row: Any = 123
    assert cs.row_to_dict(none_row) == {}
    assert cs.row_to_dict(int_row) == {}

    corrupt_dict: dict[str, Any] = {
        "id": "p-corrupt",
        "claims": "{not valid json",
        "labels": "123",
        "substitutes": '{"wrong": "type"}',
        "allergens": "[1, 2, 3]",
        "allergen_source": "not-json",
        "allergen_raw": '{"AM": "not-a-list", "UW": ["CONTAINS", 123]}',
        "nutrition": '{"FAT": NaN, "PRO-": 3.2, "BAD": -1, "BOOL": true}',
    }
    out = cs.row_to_dict(corrupt_dict)
    assert out["claims"] == []
    assert out["labels"] == []
    assert out["substitutes"] == []
    assert out["allergens"] == {}
    assert out["allergen_source"] == {}
    assert out["allergen_raw"] == {"UW": ["CONTAINS"]}
    assert out["nutrition"] == {"PRO-": 3.2}

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("create table t(id text, claims text, allergens text, nutrition text)")
    con.execute("insert into t values (?, ?, ?, ?)", ("p-row", "[broken", "{broken", "null"))
    sq_row = con.execute("select * from t").fetchone()
    con.close()
    parsed = cs.row_to_dict(sq_row)
    assert parsed == {"id": "p-row", "claims": [], "allergens": {}, "nutrition": {}}


def test_build_skips_non_dict_deleted_invalid_and_duplicate_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p_json = tmp_path / "products.json"
    r_csv = tmp_path / "rel.csv"
    c_csv = tmp_path / "co2.csv"
    db_path = tmp_path / "nested" / "catalog.sqlite"

    products_payload: list[Any] = [
        None,
        "str-entry",
        42,
        ["list-entry"],
        {"deleted": True, "id": "deleted-1"},
        {"salesUnit": "ST"},
        {"id": "   "},
        {"id": True},
        {
            "id": "464306",
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "allowedLotSize": -5,
            "temperatureCondition": True,
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": "Valio Eila milk drink 1 l lactose free 250%"}],
                "classifications": [
                    {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": "nan"}]},
                    {"name": "allergen", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                ],
                "ingredients": [
                    {"sequence": "bad", "names": [{"language": "en", "value": "milk"}]},
                    {"sequence": 1, "names": [{"language": "en", "value": "vitamin D"}]},
                ],
            },
        },
        {
            "id": "464306",
            "salesUnit": "ST",
            "synkkaData": {"names": [{"language": "en", "value": "Duplicate id ignored"}]},
        },
    ]
    p_json.write_text(json.dumps(products_payload), encoding="utf-8")
    r_csv.write_text("productId,deliveryReliability\n464306,0.99\n", encoding="utf-8")
    c_csv.write_text("productId,AVG kg CO2-eq/kg\n464306,1.14\n", encoding="utf-8")

    monkeypatch.setattr(cs, "PRODUCT_JSON", p_json)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", r_csv)
    monkeypatch.setattr(cs, "CO2_CSV", c_csv)
    monkeypatch.setattr(cs, "CATALOG_DB", db_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"corrupt sqlite header")

    con = cs.connect(db_path)
    try:
        meta = {r["k"]: r["v"] for r in con.execute("select * from meta")}
        assert meta["n"] == "1"
        assert meta["skipped_deleted"] == "9"
        rows = [cs.row_to_dict(r) for r in con.execute("select * from products")]
    finally:
        con.close()

    assert len(rows) == 1
    d = rows[0]
    assert d["id"] == "464306"
    assert d["allowed_lot_size"] is None
    assert d["temperature_condition"] is None
    assert d["nutrition"] == {}
    assert d["fat_pct"] is None
    assert d["delivery_reliability"] == pytest.approx(0.99)
    assert d["co2_kg_per_kg"] == pytest.approx(1.14)
    summary = summary_from_row(d)
    prod = product_from_row(d)
    assert summary.id == "464306"
    assert prod.allergens["milk"].value == "contains"


def test_build_rejects_non_list_json_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p_json = tmp_path / "bad_root.json"
    p_json.write_text('{"products": []}', encoding="utf-8")
    monkeypatch.setattr(cs, "PRODUCT_JSON", p_json)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", tmp_path / "none1.csv")
    monkeypatch.setattr(cs, "CO2_CSV", tmp_path / "none2.csv")
    with pytest.raises(ValueError, match="list"):
        cs.load_catalog(tmp_path / "cat.sqlite", force=True)


def test_fixture_catalog_builds_and_roundtrips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fix_dir = ROOT / "tests" / "fixtures" / "snapshot" / "replay" / "catalog"
    gz_json = fix_dir / "valio_aimo_junction26_product_data.json.gz"
    if not gz_json.is_file():
        pytest.skip("bundled fixture catalog not present")
    for gz in fix_dir.glob("*.gz"):
        (tmp_path / gz.name[:-3]).write_bytes(gzip.decompress(gz.read_bytes()))
    monkeypatch.setattr(cs, "PRODUCT_JSON", tmp_path / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(cs, "RELIABILITY_CSV", tmp_path / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(cs, "CO2_CSV", tmp_path / "valio_aimo_junction26_carbon_footprint.csv")
    db_path = tmp_path / "fixture.sqlite"
    con = cs.connect(db_path)
    try:
        rows = [cs.row_to_dict(r) for r in con.execute("select * from products")]
    finally:
        con.close()
    assert len(rows) > 100
    by_id = {r["id"]: r for r in rows}
    assert by_id["464306"]["content_amount"] == pytest.approx(1.0)
    assert by_id["464306"]["content_unit"] == "L"
    for r in rows:
        if r["allowed_lot_size"] is not None:
            assert math.isfinite(r["allowed_lot_size"]) and r["allowed_lot_size"] > 0.0
        if r["fat_pct"] is not None:
            assert 0.0 <= r["fat_pct"] <= 100.0
        if r["delivery_reliability"] is not None:
            assert 0.0 <= r["delivery_reliability"] <= 1.0
        if r["co2_kg_per_kg"] is not None:
            assert math.isfinite(r["co2_kg_per_kg"]) and r["co2_kg_per_kg"] >= 0.0


@pytest.mark.parametrize(
    "bad_val",
    [None, True, False, "", "   ", ".", ",", "abc", "nan", "inf", "-inf", "NaN", "Infinity", math.nan, math.inf, -math.inf, "9" * 400, "1" * 2000, [], {}],
)
def test_units_to_float_guards_bad_and_overflow_inputs(bad_val: Any) -> None:
    assert U._to_float(bad_val) is None


@pytest.mark.parametrize(
    "raw_val,expected",
    [("1,75", 1.75), ("0.25", 0.25), (",64", 0.64), (" 10 ", 10.0), (2.5, 2.5), (0, 0.0)],
)
def test_units_to_float_parses_valid_numbers(raw_val: Any, expected: float) -> None:
    assert U._to_float(raw_val) == pytest.approx(expected)


def test_parse_name_quantity_caps_giant_and_repeated_multipliers() -> None:
    q, mult = U.parse_name_quantity("1000x " * 80 + "1 l")
    assert mult == 100_000
    assert q is None or (math.isfinite(q.amount) and q.amount > 0)

    q2, mult2 = U.parse_name_quantity("1000x 1000x 2 dl")
    assert mult2 == 100_000
    assert q2 is not None and q2.amount == pytest.approx(0.2) and q2.unit == "L"

    q3, mult3 = U.parse_name_quantity("999999999999999999999999x 1 l")
    assert mult3 == 100_000
    assert q3 is not None and q3.amount == pytest.approx(1.0) and q3.unit == "L"

    q4, mult4 = U.parse_name_quantity("500-pack 500-pack Sugar 4 g")
    assert mult4 == 100_000
    assert q4 is not None and q4.amount == pytest.approx(0.004) and q4.unit == "KG"

    q5, mult5 = U.parse_name_quantity("0x 1 l")
    assert mult5 == 1 and q5 is not None and q5.amount == pytest.approx(1.0)

    q6, mult6 = U.parse_name_quantity("00000000x 1 l")
    assert mult6 == 1 and q6 is not None and q6.amount == pytest.approx(1.0)

    q7, mult7 = U.parse_name_quantity("00000012x 2 dl")
    assert mult7 == 12 and q7 is not None and q7.amount == pytest.approx(0.2)


def test_parse_name_quantity_and_drained_quantity_bound_hostile_long_strings() -> None:
    hostile_inputs = [
        "1000x " * 10_000 + "1 l",
        "(" * 50_000 + "1 l" + ")" * 50_000,
        "340g/285g " * 20_000,
        "9" * 50_000 + " kg",
        "1/2 " * 30_000 + "kg",
        "e" * 50_000 + "100 ml",
    ]
    t0 = time.monotonic()
    for s in hostile_inputs:
        q, mult = U.parse_name_quantity(s)
        assert 1 <= mult <= 100_000
        if q is not None:
            assert math.isfinite(q.amount) and q.amount > 0.0
        dq = U.drained_quantity(s)
        if dq is not None:
            assert math.isfinite(dq.amount) and dq.amount > 0.0
    assert time.monotonic() - t0 < 0.5


@pytest.mark.parametrize("non_str", [None, "", 123, True, False, ["1 l"], {"name": "1 l"}])
def test_parse_name_and_drained_handle_non_strings(non_str: Any) -> None:
    assert U.parse_name_quantity(non_str) == (None, 1)
    assert U.drained_quantity(non_str) is None


def test_drained_quantity_valid_and_overflow_pairs() -> None:
    dq1 = U.drained_quantity("Biona Sweetcorn 340g/285g")
    assert dq1 is not None and dq1.unit == "KG" and dq1.amount == pytest.approx(0.285)
    dq2 = U.drained_quantity("Olives 1KG/,64KG")
    assert dq2 is not None and dq2.unit == "KG" and dq2.amount == pytest.approx(0.64)
    dq3 = U.drained_quantity("Ice cream 1000ml/500g")
    assert dq3 is not None and dq3.unit == "L" and dq3.amount == pytest.approx(1.0)
    dq4 = U.drained_quantity("Sauce 280g/425ml")
    assert dq4 is not None and dq4.unit == "L" and dq4.amount == pytest.approx(0.425)
    assert U.drained_quantity("Zero drained 340g/0g") is None
    assert U.drained_quantity("9" * 400 + "g/" + "9" * 400 + "g") is None


@pytest.mark.parametrize(
    "nutr,name,expected",
    [
        ({"FAT": 1.5}, "Milk", 1.5),
        ({"FAT": 0.0}, "Skimmed milk", 0.0),
        ({"FAT": 100.0}, "Rapeseed oil", 100.0),
        ({"FAT": 100.01}, "Mystery product", None),
        ({"FAT": 200.0}, "Mystery product", None),
        ({"FAT": -1.0}, "Mystery product", None),
        ({"FAT": math.nan}, "Mystery product", None),
        ({"FAT": math.inf}, "Mystery product", None),
        ({"FAT": True}, "Mystery product", None),
        ({"FAT": 250.0}, "Valio kevyt maito 1 l", 1.5),
        ({}, "Product 200% extra size", None),
        ({}, "Cream 150,5 % promo", None),
        ({}, "999999999999% milk", None),
        ({}, "200% bonus pack 1,5% milk drink", 1.5),
        ({}, "Butter 80%", 80.0),
        ({}, "Oil 100 %", 100.0),
        ({}, "Valio täysmaito 1 l", 3.5),
        ({}, "Valio rasvaton maito 1 l", 0.0),
        (None, None, None),
    ],
)
def test_fat_pct_bounds_zero_to_hundred_and_fallbacks(
    nutr: Any, name: Any, expected: float | None
) -> None:
    res = U.fat_pct(nutr, name)
    if expected is None:
        assert res is None
    else:
        assert res == pytest.approx(expected)
        assert not isinstance(res, bool)


def test_reconcile_with_price_and_describe_pack_malformed_inputs() -> None:
    bad_unit: Any = 123
    assert U.reconcile_with_price(10.0, bad_unit, 31.31, 1.57, "L")[0] == pytest.approx(20.0)
    assert U.reconcile_with_price(10.0, bad_unit, None, None, None) == (10.0, None, None)
    assert U.describe_pack(10.0, "   ", 1.0, 10.0, "ST") == "ST"
    assert U.describe_pack(10.0, "L", 2.0, 5.0, "KI") == "2 x 5 L (10 L per KI)"
