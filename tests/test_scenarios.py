import json
import sys
sys.path.insert(0, "scenarios")
import run_all
from _common import allergens_of, best_picks_covering, picks_from, product_db, table, violates


def test_table():
    out, fails = table([(True, "a", ""), (False, "b", "boom")])
    assert fails == 1 and "FAIL  b" in out and "1/2 passed" in out
    assert callable(run_all.run_all)


def test_picks_from():
    single = {"items": [{"offers": [{"product_id": "p1", "units_needed": 4}]}]}
    assert picks_from(single) == [{"product_id": "p1", "quantity": 4, "substitute_for": None}]

    split = {
        "items": [
            {
                "offers": [
                    {"product_id": "p1", "units_needed": 6},
                    {"product_id": "p2", "units_needed": 4},
                    {"product_id": "p3", "units_needed": 2, "substitute_for": "p1"},
                    {"product_id": "p4", "units_needed": 0},
                ]
            }
        ]
    }
    assert picks_from(split) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 4, "substitute_for": None},
        {"product_id": "p3", "quantity": 2, "substitute_for": "p1"},
    ]
    assert picks_from(split, per_item=1) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
    ]


def test_best_picks_covering():
    single = {
        "need_item": {"quantity": 50},
        "offers": [{"product_id": "p1", "units_needed": 5, "available_amount": 20}],
    }
    assert best_picks_covering(single) == [{"product_id": "p1", "quantity": 5, "substitute_for": None}]

    split = {
        "need_item": {"quantity": 100},
        "offers": [
            {"product_id": "p1", "units_needed": 6, "available_amount": 6},
            {"product_id": "p2", "units_needed": 4, "available_amount": 10, "substitute_for": "p1"},
            {"product_id": "p3", "units_needed": 3, "available_amount": 0},
        ],
    }
    assert best_picks_covering(split) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 4, "substitute_for": "p1"},
    ]
    assert best_picks_covering(split, need_units=8) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 2, "substitute_for": "p1"},
    ]


def _write_products(folder, items):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "valio_aimo_junction26_product_data.json").write_text(json.dumps(items), encoding="utf-8")


def test_allergens_and_violates(tmp_path, monkeypatch):
    d1 = tmp_path / "d1"
    _write_products(
        d1,
        [
            {
                "id": "101",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [
                                {"id": "AP", "unit": "CONTAINS"},
                                {"id": "ML", "unit": "FREE_FROM"},
                            ],
                        }
                    ]
                },
            },
            {
                "id": "102",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [{"id": "AN", "unit": "MAY_CONTAIN"}],
                        }
                    ]
                },
            },
            {"id": "103", "deleted": False, "synkkaData": {"classifications": []}},
        ],
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(d1))
    assert allergens_of("101") == {"AP"}
    assert allergens_of("102") == {"AN"}
    assert allergens_of("103") == set()
    assert allergens_of("999") is None
    assert violates("101", {"peanuts"})
    assert violates("101", {"nuts"})
    assert not violates("101", {"lactose"})
    assert violates("102", {"nuts"})
    assert violates("102", {"tree nuts"})
    assert not violates("102", {"egg"})
    assert not violates("103", {"nuts", "peanuts", "egg"})
    assert not violates("999", {"nuts"})

    d2 = tmp_path / "d2"
    _write_products(
        d2,
        [
            {
                "id": "201",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [{"id": "AE", "unit": "MAY_CONTAIN"}],
                        }
                    ]
                },
            }
        ],
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(d2))
    assert set(product_db()) == {"201"}
    assert allergens_of("101") is None
    assert allergens_of("201") == {"AE"}
    assert violates("201", {"egg"})
