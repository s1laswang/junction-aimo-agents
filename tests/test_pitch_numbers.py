import json
import re
from pathlib import Path

import pytest

pytest.importorskip("pitch.measure_data")
from pitch.measure_data import _resolve_data_dir, measure

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"

EXPECTED_MEASURED = {
    "products": 17546,
    "deleted": 0,
    "lactose_free_claim": 4178,
    "low_lactose_claim": 321,
    "gluten_free_claim": 3049,
    "vegan_claim": 2959,
    "with_allergen_declared": 7205,
    "with_free_from_declared": 5119,
    "with_any_allergen_info": 9436,
    "with_milk_allergen_contains": 3160,
    "lactose_free_name_has_milk_or_maito": 70,
    "categories": 349,
    "names_en": 17544,
    "reliability_rows": 4425,
    "reliability_in_catalog": 4425,
    "reliability_min": 0.0,
    "reliability_p10": 0.936,
    "reliability_median": 1.0,
    "reliability_mean": 0.972,
    "reliability_perfect_1": 2490,
    "reliability_below_0.9": 291,
    "co2_rows": 11033,
    "co2_in_catalog": 11033,
    "co2_min": 0.13,
    "co2_median": 2.11,
    "co2_max": 30.46,
}


def _data_json():
    return _resolve_data_dir() / "valio_aimo_junction26_product_data.json"


@pytest.fixture(scope="module")
def measured():
    if not _data_json().exists():
        pytest.skip("no data")
    return measure()


def parse_table_numbers(markdown_text, valid_keys):
    rows = {}
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cols = [c.strip() for c in line[1:-1].split("|")]
        if len(cols) < 3:
            continue
        key = cols[2].strip("`").strip()
        if key not in valid_keys:
            continue
        cleaned = re.sub(r"\([^)]*\)", "", cols[1]).replace("`", "").replace(",", "").strip()
        rows[key] = float(cleaned) if "." in cleaned else int(cleaned)
    return rows


def _repo_doc_paths():
    fixed = [ROOT / "README.md", ROOT / "Makefile", ROOT / "Dockerfile"]
    globs = [
        sorted((ROOT / "pitch").glob("*.md")),
        sorted((ROOT / "docs").glob("*.md")),
        sorted((ROOT / "deploy").glob("*.md")),
    ]
    for path in fixed:
        assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    for group in globs:
        assert group, "expected markdown files in doc directory"
    return fixed + [path for group in globs for path in group]


def test_measure_exact_catalog_counts(measured):
    assert measured == EXPECTED_MEASURED
    assert 0 <= measured["reliability_min"] <= measured["reliability_p10"] <= measured["reliability_median"] <= 1.0
    assert 0 <= measured["co2_min"] <= measured["co2_median"] <= measured["co2_max"]


def test_numbers_md_matches_measure(measured):
    text = (ROOT / "pitch" / "NUMBERS.md").read_text(encoding="utf-8")
    parsed = parse_table_numbers(text, set(measured))
    assert len(parsed) >= 15
    for key, doc_val in parsed.items():
        assert doc_val == measured[key], f"{key}: NUMBERS.md has {doc_val}, measure() has {measured[key]}"


def test_parse_table_numbers_stripping():
    sample = "\n".join([
        "| Claim | Value | Key in output |",
        "|---|---|---|",
        "| Products in catalog file | 17,546 | products |",
        "| Either allergen info | 9,436 (53.8% of 17,546) | with_any_allergen_info |",
        "| Reliability rows | `4,425` (all in catalog) | `reliability_rows` |",
        "| Reliability summary | 0.0 / 0.936 / 1.0 / 0.972 | reliability_* |",
        "| Mean reliability | 0.972 | reliability_mean |",
    ])
    parsed = parse_table_numbers(
        sample,
        {"products", "with_any_allergen_info", "reliability_rows", "reliability_mean"},
    )
    assert parsed == {
        "products": 17546,
        "with_any_allergen_info": 9436,
        "reliability_rows": 4425,
        "reliability_mean": 0.972,
    }


def test_measure_offline_fixture(tmp_path, monkeypatch):
    products = [
        {
            "id": "p1",
            "deleted": False,
            "category": "dairy",
            "synkkaData": {
                "names": [
                    {"language": "en", "value": "Lactose-free milk 1L"},
                    {"language": "fi", "value": "Laktoositon maito 1L"},
                ],
                "classifications": [
                    {
                        "name": "nutritionalClaim",
                        "values": [{"id": "FREE_FROM_LACTOSE"}, {"id": "FREE_FROM_GLUTEN"}],
                    },
                    {
                        "name": "allergen",
                        "values": [{"id": "AM", "unit": "CONTAINS"}],
                    },
                ],
            },
        },
        {
            "id": "p2",
            "deleted": True,
            "category": "plant",
            "synkkaData": {
                "names": [{"language": "fi", "value": "Kaurajuoma"}],
                "classifications": [
                    {"name": "nutritionalClaim", "values": [{"id": "VEGAN"}, {"id": "LOW_LACTOSE"}]},
                    {"name": "nonAllergen", "values": [{"id": "AM", "unit": "FREE_FROM"}]},
                ],
            },
        },
    ]
    (tmp_path / "valio_aimo_junction26_product_data.json").write_text(json.dumps(products), encoding="utf-8")
    (tmp_path / "valio_aimo_junction26_delivery_reliability.csv").write_text(
        "productId,deliveryReliability\np1,1.0\np2,0.85\np_extra,0.95\n",
        encoding="utf-8",
    )
    (tmp_path / "valio_aimo_junction26_carbon_footprint.csv").write_text(
        "productId,AVG kg CO2-eq/kg\np1,1.20\np2,0.40\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path))
    m = measure()
    assert m["products"] == 2
    assert m["deleted"] == 1
    assert m["lactose_free_claim"] == 1
    assert m["low_lactose_claim"] == 1
    assert m["gluten_free_claim"] == 1
    assert m["vegan_claim"] == 1
    assert m["with_allergen_declared"] == 1
    assert m["with_free_from_declared"] == 1
    assert m["with_any_allergen_info"] == 2
    assert m["with_milk_allergen_contains"] == 1
    assert m["lactose_free_name_has_milk_or_maito"] == 1
    assert m["categories"] == 2
    assert m["names_en"] == 1
    assert m["reliability_rows"] == 3
    assert m["reliability_in_catalog"] == 2
    assert m["reliability_perfect_1"] == 1
    assert m["reliability_below_0.9"] == 1
    assert m["co2_rows"] == 2
    assert m["co2_in_catalog"] == 2
    assert m["co2_min"] == 0.4
    assert m["co2_max"] == 1.2


def test_repo_docs_have_no_em_dashes():
    offenders = []
    for path in _repo_doc_paths():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if EM_DASH in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"em dash found in {', '.join(offenders)}"


def test_owned_python_hygiene():
    for rel in ("pitch/measure_data.py", "tests/test_pitch_numbers.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert EM_DASH not in text, rel
        assert "\u0023" not in text, rel
