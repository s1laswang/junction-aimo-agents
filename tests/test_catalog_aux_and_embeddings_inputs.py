from __future__ import annotations

from datetime import date, datetime, timezone
import io
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import tokenize
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import aimo.catalog as catalog
from aimo.catalog import data_quality, embeddings, gemini_allergens, plan as catalog_plan, taxonomy
from aimo.models import AllergenStatus, Need, Offer

ROOT = Path(__file__).resolve().parents[1]
OWNED_FILES = (
    "aimo/catalog/__init__.py",
    "aimo/catalog/embeddings.py",
    "aimo/catalog/data_quality.py",
    "aimo/catalog/taxonomy.py",
    "tests/test_catalog_aux_and_embeddings_inputs.py",
)


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emb_path = tmp_path / "embeddings.npy"
    ids_path = tmp_path / "embeddings_ids.json"
    chunk_dir = tmp_path / "cache" / "emb_chunks"
    qcache = tmp_path / "cache" / "query_embeddings.json"
    tax_json = tmp_path / "cache" / "taxonomy.json"
    job_file = tmp_path / "cache" / "taxonomy_batch_job.json"

    monkeypatch.setattr(embeddings, "EMB_PATH", emb_path)
    monkeypatch.setattr(embeddings, "IDS_PATH", ids_path)
    monkeypatch.setattr(embeddings, "CHUNK_DIR", chunk_dir)
    monkeypatch.setattr(embeddings, "QCACHE", qcache)
    monkeypatch.setattr(taxonomy, "TAX_JSON", tax_json)
    monkeypatch.setattr(taxonomy, "JOB_FILE", job_file)

    embeddings._matrix.cache_clear()
    embeddings._embed_query_cached.cache_clear()
    taxonomy.labels_for.cache_clear()
    yield
    embeddings._matrix.cache_clear()
    embeddings._embed_query_cached.cache_clear()
    taxonomy.labels_for.cache_clear()


def _write_matrix(ids: list[Any], mat: np.ndarray) -> None:
    embeddings.EMB_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(embeddings.EMB_PATH, mat)
    embeddings.IDS_PATH.write_text(json.dumps(ids), encoding="utf-8")
    embeddings._matrix.cache_clear()


def _unit_vec(idx: int = 0) -> np.ndarray:
    v = np.zeros(embeddings.DIM, dtype=np.float32)
    v[idx % embeddings.DIM] = 1.0
    return v


def _valid_need(**overrides: Any) -> Need:
    base: dict[str, Any] = {
        "raw_text": "10 L milk",
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 10.0,
        "unit": "L",
        "delivery_by": date(2026, 11, 16),
        "preferred_product_ids": [],
        "preferences": {},
    }
    base.update(overrides)
    return Need.model_construct(**base)


def test_owned_files_hygiene_zero_comments_em_dashes_and_co_authored_by() -> None:
    co_author = "Co-" + "Authored-By"
    self_src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert "\u2014" not in self_src
    assert "\u2013" not in self_src
    assert co_author not in self_src

    for rel in OWNED_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "\u2014" not in src, rel
        assert co_author not in src, rel
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert comments == [], (rel, comments)


@pytest.mark.parametrize(
    "bad_query",
    [None, True, False, 0, 123, 3.14, ["milk"], ("milk",), {"q": "milk"}, b"milk"],
)
def test_catalog_search_rejects_non_str_query(bad_query: Any) -> None:
    with pytest.raises(ValueError, match="query"):
        catalog.search(bad_query)


@pytest.mark.parametrize(
    "bad_limit",
    [True, False, 0, -1, -999, 1.5, 20.0, "20", None, [20], {"limit": 20}],
)
def test_catalog_search_rejects_bad_limit(bad_limit: Any) -> None:
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        catalog.search("milk", limit=bad_limit)


@pytest.mark.parametrize(
    "bad_claims",
    ["FREE_FROM_LACTOSE", b"VEGAN", {"VEGAN": 1}, 123, True, [123], [None], [True], ["VEGAN", 99]],
)
def test_catalog_search_rejects_bad_claims(bad_claims: Any) -> None:
    with pytest.raises(ValueError, match="claims"):
        catalog.search("milk", claims=bad_claims)


@pytest.mark.parametrize("bad_cat", [123, True, False, ["10400"], {"c": "10400"}])
def test_catalog_search_rejects_non_str_category(bad_cat: Any) -> None:
    with pytest.raises(ValueError, match="category"):
        catalog.search("milk", category=bad_cat)


@pytest.mark.parametrize("bad_unit", [123, True, False, ["L"], {"u": "L"}])
def test_catalog_search_rejects_non_str_content_unit(bad_unit: Any) -> None:
    with pytest.raises(ValueError, match="content_unit"):
        catalog.search("milk", content_unit=bad_unit)


def test_catalog_search_sanitizes_nul_surrogates_and_huge_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimo.catalog import textsearch

    captured: dict[str, Any] = {}

    def _fake_search(q: str, **kw: Any):
        captured["q"] = q
        captured["kw"] = kw
        return []

    monkeypatch.setattr(textsearch, "search", _fake_search)
    assert catalog.search("milk\x00\ud800drink", limit=10**25) == []
    assert captured["q"] == "milkdrink"
    assert captured["kw"]["limit"] == 10_000

    assert catalog.search("milk", claims=["FREE\x00"]) == []
    assert catalog.search("milk", claims=["FREE\ud800"]) == []
    assert catalog.search("milk", category="104\x00") == []
    assert catalog.search("milk", content_unit="L\udfff") == []


@pytest.mark.parametrize(
    "bad_pid",
    [
        None,
        True,
        False,
        0,
        464306,
        -1,
        3.14,
        float("nan"),
        ["464306"],
        ("464306",),
        {"id": "464306"},
        b"464306",
        "",
        " ",
        "   \t\r\n  ",
        "x" * 129,
        " " * 129,
        "464306\x00",
        "\x00",
        "464306\ud800",
        "\udfff",
    ],
)
def test_get_product_rejects_bad_ids_without_touching_sqlite(
    bad_pid: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aimo.catalog import textsearch

    def _never_called(_ids: Any):
        raise AssertionError("get_rows must not be called for invalid product_id")

    monkeypatch.setattr(textsearch, "get_rows", _never_called)
    assert catalog.get_product(bad_pid) is None


def test_get_product_trims_valid_id_and_returns_product(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimo.catalog import textsearch

    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {"464306": {"id": "464306", "name": "Milk 1L", "sales_unit": "ST"}} if ids == ["464306"] else {},
    )
    p = catalog.get_product("  464306  ")
    assert p is not None
    assert p.id == "464306"
    assert p.name == "Milk 1L"
    assert catalog.get_product("999999") is None


@pytest.mark.parametrize("bad_row", [None, [], (), "464306", 123, True, False, 3.14])
def test_product_from_row_rejects_non_dict(bad_row: Any) -> None:
    with pytest.raises(ValueError, match="dict"):
        catalog.product_from_row(bad_row)


@pytest.mark.parametrize(
    "bad_id",
    [None, True, False, 0, 464306, "", "   ", "\t\n", "464\x00", "464\ud800", "x" * 129, ["x"], {"id": "x"}],
)
def test_product_from_row_rejects_missing_or_invalid_id(bad_id: Any) -> None:
    d: dict[str, Any] = {} if bad_id is None else {"id": bad_id}
    with pytest.raises(ValueError, match="id"):
        catalog.product_from_row(d)


def test_product_from_row_missing_id_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="id"):
        catalog.product_from_row({"name": "Milk", "sales_unit": "ST"})


@pytest.mark.parametrize("bad_inf", ["not-a-dict", ["milk"], 123, True, False, None, {123: "contains"}])
def test_product_from_row_guards_non_dict_cached_inference(
    bad_inf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda _pid: bad_inf)
    p = catalog.product_from_row({
        "id": "p-1",
        "name": "Test Product",
        "sales_unit": "ST",
        "allergens": {"milk": "unknown"},
        "allergen_source": {"milk": "none"},
    })
    assert p.id == "p-1"
    assert p.allergens["milk"] == AllergenStatus.unknown


def test_product_from_row_sanitizes_nutrition_lot_size_temp_and_dirty_fields() -> None:
    p = catalog.product_from_row({
        "id": "  p-dirty  ",
        "gtin": 12345,
        "name": ["bad"],
        "name_fi": 99,
        "brand": {"b": "Valio"},
        "vendor": True,
        "category": 10400,
        "sales_unit": None,
        "content_amount": float("nan"),
        "content_unit": "INVALID",
        "claims": ["FREE_FROM_LACTOSE", 123, None, True],
        "labels": "not-a-list",
        "delivery_reliability": 1.5,
        "co2_kg_per_kg": -2.0,
        "allergens": {"milk": "contains", "bad": "invalid_status", 123: "free"},
        "allergen_source": {"milk": "structured", "bad": 999},
        "ingredients_text": ["milk"],
        "marketing_text": 42,
        "country_of_origin": ("FI",),
        "nutrition": {
            "FAT": 1.5,
            "ZERO": -0.0,
            "INT": 4,
            "BOOL_T": True,
            "BOOL_F": False,
            "NAN": float("nan"),
            "INF": float("inf"),
            "NINF": float("-inf"),
            "NEG": -0.1,
            "STR": "3.2",
            "NONE": None,
            "": 2.0,
            "   ": 2.0,
        },
        "allowed_lot_size": 0.0,
        "temperature_condition": True,
    })
    assert p.id == "p-dirty"
    assert p.name == "p-dirty"
    assert p.sales_unit == ""
    assert p.content_amount is None
    assert p.content_unit is None
    assert p.claims == ["FREE_FROM_LACTOSE"]
    assert p.labels == []
    assert p.delivery_reliability is None
    assert p.co2_kg_per_kg is None
    assert p.allergens == {"milk": AllergenStatus.contains}
    assert p.allergen_source == {"milk": "structured"}
    assert p.nutrition == {"FAT": 1.5, "ZERO": 0.0, "INT": 4.0}
    assert p.allowed_lot_size is None
    assert p.temperature_condition is None
    assert p.ingredients_text is None
    assert p.marketing_text is None
    assert p.country_of_origin is None


def test_product_from_row_strips_nul_and_surrogates_so_json_serialization_succeeds() -> None:
    p = catalog.product_from_row({
        "id": "p-surrogate",
        "name": "Milk\x00\ud8001L",
        "name_fi": "Maito\udfff",
        "brand": "Valio\x00",
        "ingredients_text": "milk\ud800, vitamin D",
        "marketing_text": "fresh\x00\udfffdaily",
        "country_of_origin": "FI\x00",
        "sales_unit": "ST\x00",
    })
    dumped = p.model_dump_json()
    parsed = json.loads(dumped)
    assert parsed["name"] == "Milk1L"
    assert parsed["brand"] == "Valio"
    assert parsed["ingredients_text"] == "milk, vitamin D"
    assert parsed["marketing_text"] == "freshdaily"
    assert parsed["country_of_origin"] == "FI"


@pytest.mark.parametrize(
    ("lot_in", "lot_expected"),
    [
        (0.01, 0.01),
        (2, 2.0),
        (0, None),
        (-1.0, None),
        (True, None),
        (False, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        ("1.0", None),
        (None, None),
    ],
)
def test_product_from_row_allowed_lot_size_matrix(lot_in: Any, lot_expected: Any) -> None:
    p = catalog.product_from_row({"id": "p-lot", "allowed_lot_size": lot_in})
    assert p.allowed_lot_size == lot_expected


@pytest.mark.parametrize(
    ("temp_in", "temp_expected"),
    [
        (4, 4),
        (0, 0),
        (-18, -18),
        (True, None),
        (False, None),
        (4.0, None),
        (3.5, None),
        (float("nan"), None),
        ("4", None),
        (None, None),
    ],
)
def test_product_from_row_temperature_condition_matrix(temp_in: Any, temp_expected: Any) -> None:
    p = catalog.product_from_row({"id": "p-temp", "temperature_condition": temp_in})
    assert p.temperature_condition == temp_expected


@pytest.mark.parametrize(
    "bad_qty",
    [0, 0.0, -0.0, -1, -100.5, float("nan"), float("inf"), float("-inf"), True, False, "10", None, 1_000_001.0],
)
@pytest.mark.parametrize("offers_arg", [None, {}])
def test_rank_substitutes_rejects_bad_quantity_at_very_top(
    bad_qty: Any, offers_arg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any):
        raise AssertionError("downstream planner functions must not run before validate_need")

    monkeypatch.setattr(catalog_plan, "with_customer_policy", _boom)
    monkeypatch.setattr(catalog_plan, "with_effective_query", _boom)
    monkeypatch.setattr(catalog_plan, "find_candidates", _boom)

    bad_need = _valid_need(quantity=bad_qty)
    with pytest.raises(ValueError, match="quantity"):
        catalog.rank_substitutes(bad_need, offers=offers_arg, limit=12)


@pytest.mark.parametrize(
    "bad_limit",
    [True, False, 0, -1, -50, 1.5, "12", None, [5]],
)
@pytest.mark.parametrize("offers_arg", [None, {}])
def test_rank_substitutes_rejects_bad_limit_at_very_top(
    bad_limit: Any, offers_arg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any):
        raise AssertionError("downstream planner functions must not run before limit validation")

    monkeypatch.setattr(catalog_plan, "with_customer_policy", _boom)
    monkeypatch.setattr(catalog_plan, "find_candidates", _boom)

    with pytest.raises(ValueError, match="limit must be a positive integer"):
        catalog.rank_substitutes(_valid_need(), offers=offers_arg, limit=bad_limit)


@pytest.mark.parametrize(
    "bad_offers",
    [
        "not-a-dict",
        [],
        [1, 2],
        123,
        True,
        False,
        {123: None},
        {True: None},
        {"464306": "not-an-offer"},
        {"464306": 123},
        {"464306": {"unit_price": 1.5}},
    ],
)
def test_rank_substitutes_rejects_malformed_offers_at_very_top(
    bad_offers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog_plan,
        "with_customer_policy",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    with pytest.raises(ValueError, match="offers"):
        catalog.rank_substitutes(_valid_need(), offers=bad_offers, limit=5)


@pytest.mark.parametrize(
    "field_override",
    [
        {"customer_id": ""},
        {"customer_id": "   "},
        {"customer_id": None},
        {"product_query": ""},
        {"product_query": "   "},
        {"unit": "ML"},
        {"delivery_by": datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)},
        {"required_claims": "FREE_FROM_LACTOSE"},
        {"excluded_allergens": [123]},
        {"preferred_product_ids": [None]},
        {"preferences": ["not-a-dict"]},
    ],
)
def test_rank_substitutes_rejects_other_malformed_need_fields_with_empty_offers(
    field_override: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog_plan,
        "with_customer_policy",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    with pytest.raises(ValueError):
        catalog.rank_substitutes(_valid_need(**field_override), offers={}, limit=5)


def test_rank_substitutes_rejects_none_need() -> None:
    with pytest.raises(ValueError, match="need is required"):
        catalog.rank_substitutes(None, offers={})


@pytest.mark.parametrize(
    "bad_qty",
    [0, 0.0, -1, -50.25, float("nan"), float("inf"), float("-inf"), True, False, "10", None],
)
def test_catalog_plan_need_rejects_bad_quantity_before_plan(
    bad_qty: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog_plan,
        "plan_need",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("plan_need must not be called")),
    )
    with pytest.raises(ValueError, match="quantity"):
        catalog.plan_need(_valid_need(quantity=bad_qty))


@pytest.mark.parametrize("bad_limit", [True, False, 0, -1, 2.5, "12", None])
def test_catalog_plan_need_rejects_bad_limit_and_bad_offers_fn(
    bad_limit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog_plan,
        "plan_need",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("plan_need must not be called")),
    )
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        catalog.plan_need(_valid_need(), limit=bad_limit)

    for bad_fn in ("not-callable", 123, True, {}, []):
        with pytest.raises(ValueError, match="offers_fn"):
            catalog.plan_need(_valid_need(), offers_fn=bad_fn)


@pytest.mark.parametrize("bad_d", [None, [], (), "milk", 123, True, False, 3.14])
def test_embeddings_product_text_non_dict_returns_empty(bad_d: Any) -> None:
    assert embeddings.product_text(bad_d) == ""


def test_embeddings_product_text_guards_dirty_fields_and_claims() -> None:
    txt = embeddings.product_text({
        "name_en": 12345,
        "name_fi": "Valio\x00 kevytmaito\ud800",
        "brand": ["Valio"],
        "ingredients_text": {"bad": 1},
        "claims": [None, 123, True, {"x": 1}, "FREE_FROM_LACTOSE", "ORGANIC", "UNRELATED_CLAIM"],
    })
    assert txt == "Valio kevytmaito | free from lactose organic"

    for bad_claims in (123, True, "FREE_FROM_LACTOSE", b"VEGAN", {"VEGAN": True}):
        out = embeddings.product_text({"name_en": "Milk", "brand": "Valio", "claims": bad_claims})
        assert out == "Milk | brand Valio"


def test_embeddings_embed_batch_empty_list_returns_zero_rows_without_gemini() -> None:
    res = embeddings._embed_batch([], "catalog.embed.test")
    assert res.shape == (0, embeddings.DIM)
    assert res.dtype == np.float32


def test_embeddings_matrix_missing_files_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        embeddings._matrix()


@pytest.mark.parametrize(
    "bad_ids_raw",
    [
        "{broken json",
        "null",
        "{}",
        '"464306"',
        "123",
        "true",
        "[123]",
        "[null]",
        "[true]",
        '[""]',
        '["   "]',
    ],
)
def test_embeddings_matrix_corrupt_ids_json_raises_plain_value_error(bad_ids_raw: str) -> None:
    np.save(embeddings.EMB_PATH, np.zeros((1, embeddings.DIM), dtype=np.float16))
    embeddings.IDS_PATH.write_text(bad_ids_raw, encoding="utf-8")
    embeddings._matrix.cache_clear()
    with pytest.raises(ValueError) as exc_info:
        embeddings._matrix()
    assert not isinstance(exc_info.value, json.JSONDecodeError)


def test_embeddings_matrix_corrupt_npy_bytes_and_shapes_raise_value_error() -> None:
    embeddings.IDS_PATH.write_text(json.dumps(["p1", "p2"]), encoding="utf-8")

    embeddings.EMB_PATH.write_bytes(b"not a valid npy file")
    embeddings._matrix.cache_clear()
    with pytest.raises(ValueError, match="corrupt embeddings matrix"):
        embeddings._matrix()

    for bad_mat in (
        np.zeros((embeddings.DIM,), dtype=np.float32),
        np.zeros((2, 1, embeddings.DIM), dtype=np.float32),
        np.zeros((3, embeddings.DIM), dtype=np.float32),
        np.zeros((2, embeddings.DIM - 1), dtype=np.float32),
        np.full((2, embeddings.DIM), "abc"),
    ):
        np.save(embeddings.EMB_PATH, bad_mat)
        embeddings._matrix.cache_clear()
        with pytest.raises(ValueError):
            embeddings._matrix()


def test_embeddings_matrix_sanitizes_nan_and_inf_and_is_read_only() -> None:
    mat = np.zeros((2, embeddings.DIM), dtype=np.float32)
    mat[0, 0] = float("nan")
    mat[0, 1] = float("inf")
    mat[1, 0] = float("-inf")
    mat[1, 2] = 0.75
    _write_matrix(["p1", "p2"], mat)

    M, ids, idx = embeddings._matrix()
    assert ids == ["p1", "p2"]
    assert idx == {"p1": 0, "p2": 1}
    assert np.isfinite(M).all()
    assert M[0, 0] == 0.0 and M[0, 1] == 0.0 and M[1, 0] == 0.0
    assert M[1, 2] == pytest.approx(0.75)
    assert M.flags.writeable is False


@pytest.mark.parametrize("non_dict_json", ["[]", "null", '"str"', "123", "true", "{invalid"])
def test_embeddings_qcache_non_dict_root_returns_empty_dict(non_dict_json: str) -> None:
    embeddings.QCACHE.parent.mkdir(parents=True, exist_ok=True)
    embeddings.QCACHE.write_text(non_dict_json, encoding="utf-8")
    assert embeddings._qcache() == {}


@pytest.mark.parametrize(
    "bad_q",
    [
        None,
        True,
        False,
        0,
        123,
        3.14,
        ["unhashable"],
        {"unhashable": 1},
        b"milk",
        "",
        "   ",
        "\t\r\n",
        "\x00\x00",
        "\ud800\udfff",
        " \x00 \ud800 ",
    ],
)
def test_embed_query_rejects_non_str_or_empty_with_value_error(bad_q: Any) -> None:
    with pytest.raises(ValueError, match="query"):
        embeddings.embed_query(bad_q)


@pytest.mark.parametrize(
    "corrupt_vec",
    [
        None,
        "not-a-list",
        {"0": 0.1},
        [],
        [0.1] * (embeddings.DIM - 1),
        [0.1] * (embeddings.DIM + 1),
        [True] * embeddings.DIM,
        ["0.1"] * embeddings.DIM,
        [None] + [0.0] * (embeddings.DIM - 1),
        [float("nan")] + [0.0] * (embeddings.DIM - 1),
        [float("inf")] + [0.0] * (embeddings.DIM - 1),
    ],
)
def test_embed_query_ignores_corrupt_qcache_entry_and_rewrites_atomically(
    corrupt_vec: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aimo.catalog import netcheck

    embeddings.QCACHE.parent.mkdir(parents=True, exist_ok=True)
    embeddings.QCACHE.write_text(json.dumps({"milk": "placeholder"}), encoding="utf-8")
    raw_cache = json.loads(embeddings.QCACHE.read_text(encoding="utf-8"))
    raw_cache["milk"] = corrupt_vec
    embeddings.QCACHE.write_text(json.dumps(raw_cache, allow_nan=True), encoding="utf-8")
    embeddings.embed_query.cache_clear()

    seen_texts: list[list[str]] = []

    def _fake_batch(texts: list[str], _feature: str, retries: int = 1) -> np.ndarray:
        seen_texts.append(texts)
        out = np.zeros((1, embeddings.DIM), dtype=np.float32)
        out[0, 3] = 1.0
        return out

    monkeypatch.setattr(netcheck, "gemini_online", lambda: True)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_batch)

    v = embeddings.embed_query("  Milk\x00\ud800  ")
    assert seen_texts == [["Milk"]]
    assert v.shape == (embeddings.DIM,)
    assert v[3] == pytest.approx(1.0)
    assert list(embeddings.QCACHE.parent.glob("*.tmp*")) == []
    saved = json.loads(embeddings.QCACHE.read_text(encoding="utf-8"))
    assert len(saved["milk"]) == embeddings.DIM


def test_embed_query_caps_long_query_at_2000_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimo.catalog import netcheck

    seen: list[str] = []

    def _fake_batch(texts: list[str], _feature: str, retries: int = 1) -> np.ndarray:
        seen.extend(texts)
        return np.ones((1, embeddings.DIM), dtype=np.float32)

    monkeypatch.setattr(netcheck, "gemini_online", lambda: True)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_batch)

    long_q = "a" * 5000
    v = embeddings.embed_query(long_q)
    assert v.shape == (embeddings.DIM,)
    assert len(seen) == 1 and len(seen[0]) == 2000


@pytest.mark.parametrize("bad_k", [True, False, 0, -1, -20, 1.5, "10", None, [5]])
def test_semantic_search_and_similar_products_reject_bad_k(bad_k: Any) -> None:
    with pytest.raises(ValueError, match="k must be a positive integer"):
        embeddings.semantic_search("milk", k=bad_k)
    with pytest.raises(ValueError, match="k must be a positive integer"):
        embeddings.similar_products("464306", k=bad_k)


def test_semantic_search_handles_zero_single_and_clamped_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "embed_query", lambda _q: _unit_vec(0))

    _write_matrix([], np.zeros((0, embeddings.DIM), dtype=np.float32))
    assert embeddings.semantic_search("milk", k=30) == []

    m1 = np.zeros((1, embeddings.DIM), dtype=np.float32)
    m1[0, 0] = 0.9
    _write_matrix(["only-one"], m1)
    res1 = embeddings.semantic_search("milk", k=30)
    assert len(res1) == 1 and res1[0][0] == "only-one" and res1[0][1] == pytest.approx(0.9)

    m3 = np.zeros((3, embeddings.DIM), dtype=np.float32)
    m3[0, 0] = 0.2
    m3[1, 0] = 0.95
    m3[2, 0] = 0.6
    _write_matrix(["p0", "p1", "p2"], m3)
    res_top2 = embeddings.semantic_search("milk", k=2)
    assert [pid for pid, _ in res_top2] == ["p1", "p2"]


def test_similar_products_handles_zero_single_two_rows_and_bad_pids() -> None:
    _write_matrix([], np.zeros((0, embeddings.DIM), dtype=np.float32))
    assert embeddings.similar_products("p0", k=15) == []

    _write_matrix(["p0"], np.ones((1, embeddings.DIM), dtype=np.float32))
    assert embeddings.similar_products("p0", k=15) == []

    m3 = np.zeros((3, embeddings.DIM), dtype=np.float32)
    m3[0, 0] = 1.0
    m3[1, 0] = 0.8
    m3[2, 0] = 0.4
    _write_matrix(["p0", "p1", "p2"], m3)
    sim_all = embeddings.similar_products("p0", k=15)
    assert [pid for pid, _ in sim_all] == ["p1", "p2"]
    sim_one = embeddings.similar_products(" p0 ", k=1)
    assert [pid for pid, _ in sim_one] == ["p1"]

    for bad_pid in (None, True, False, 123, ["p0"], {"id": "p0"}, "", "   ", "p0\x00", "p0\ud800", "x" * 200):
        assert embeddings.similar_products(bad_pid, k=5) == []


def test_similarity_guards_bad_inputs_missing_files_and_corrupt_matrix() -> None:
    assert embeddings.similarity("p0", "p1") is None

    embeddings.IDS_PATH.write_text("{corrupt", encoding="utf-8")
    embeddings.EMB_PATH.write_bytes(b"corrupt")
    embeddings._matrix.cache_clear()
    assert embeddings.similarity("p0", "p1") is None

    m2 = np.zeros((2, embeddings.DIM), dtype=np.float32)
    m2[0, 0] = 1.0
    m2[1, 0] = 0.5
    _write_matrix(["p0", "p1"], m2)

    assert embeddings.similarity("p0", "p1") == pytest.approx(0.5)
    assert embeddings.similarity(" p0 ", " p1 ") == pytest.approx(0.5)
    assert embeddings.similarity("p0", "missing") is None

    for bad in (None, True, False, 0, 123, ["p0"], {"a": 1}, "", "   ", "p0\x00", "p0\ud800"):
        assert embeddings.similarity(bad, "p1") is None
        assert embeddings.similarity("p0", bad) is None


@pytest.mark.parametrize(
    "bad_group",
    [None, True, False, 0, 123, ["milk"], {"g": "milk"}, "", "   ", "unknown_allergen", "MILK", "dairy"],
)
def test_keyword_evidence_guards_bad_or_unknown_group_before_lru_rx(bad_group: Any) -> None:
    data_quality._rx.cache_clear()
    assert data_quality.keyword_evidence(bad_group, "whole milk powder, almond") is None
    assert data_quality._rx.cache_info().currsize == 0


@pytest.mark.parametrize(
    "bad_text",
    [None, True, False, 0, 12345, 3.14, ["milk"], {"t": "milk"}, "", "   ", "\x00\x00"],
)
def test_keyword_evidence_guards_non_str_or_empty_text(bad_text: Any) -> None:
    assert data_quality.keyword_evidence("milk", bad_text) is None


def test_keyword_evidence_bounds_hostile_multi_mb_scan_length() -> None:
    prefix = "water, salt, " * 200_000
    assert len(prefix) > 2_000_000
    t0 = time.perf_counter()
    res_tail = data_quality.keyword_evidence("nuts", prefix + "ALMOND")
    elapsed = time.perf_counter() - t0
    assert res_tail is None
    assert elapsed < 0.5

    head_hit = data_quality.keyword_evidence("nuts", "Water, ALMOND, " + prefix)
    assert head_hit is not None and "ALMOND" in head_hit


@pytest.mark.parametrize("bad_d", [None, [], (), "row", 123, True, False, 3.14])
def test_data_quality_conflicts_and_apply_guard_non_dict_row(bad_d: Any) -> None:
    assert data_quality.conflicts(bad_d) == {}
    with pytest.raises(ValueError, match="dict"):
        data_quality.apply(bad_d)


def test_data_quality_conflicts_guards_malformed_nested_fields() -> None:
    assert data_quality.conflicts({"allergens": ["milk"], "ingredients_text": "milk"}) == {}
    assert data_quality.conflicts({"allergens": 123, "ingredients_text": "milk"}) == {}

    d = {
        "allergens": {"nuts": "free", "gluten": "free", 123: "free", "": "free"},
        "claims": [{"unhashable": 1}, ["nested"], None, 42, "FREE_FROM_GLUTEN"],
        "ingredients_text": 99999,
        "ingredients_fi": "Vesi, MANTELI (2,3 %)",
    }
    for bad_gem in (None, "not-a-dict", ["nuts"], 123, True, {"allergens_in_ingredients": 123},
                    {"allergens_in_ingredients": [{"bad": 1}, None]}):
        c = data_quality.conflicts(d, gemini_label=bad_gem)
        assert set(c) == {"nuts"}
        assert c["nuts"]["by"] == "lexicon"

    c_both = data_quality.conflicts(d, gemini_label={"allergens_in_ingredients": ["nuts", 123, {"x": 1}]})
    assert c_both["nuts"]["by"] == "lexicon+gemini"


def test_data_quality_apply_handles_missing_or_bad_id_and_non_dict_source() -> None:
    row_no_id: dict[str, Any] = {
        "allergens": {"nuts": "free", "milk": "free"},
        "allergen_source": "corrupt-source",
        "claims": 12345,
        "ingredients_text": "Water, ALMOND (2%)",
    }
    out = data_quality.apply(row_no_id)
    assert out is row_no_id
    assert out["allergens"]["nuts"] == "unknown"
    assert out["allergens"]["milk"] == "free"
    assert out["allergen_source"] == {"nuts": "contradiction"}
    assert "nuts" in out["allergen_conflicts"]

    for bad_id in (None, True, False, 123, "", "   ", ["id"]):
        r2: dict[str, Any] = {"id": bad_id, "allergens": "not-a-dict"}
        assert data_quality.apply(r2)["allergen_conflicts"] == {}


def test_data_quality_write_report_accepts_str_path_and_rejects_bad_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        data_quality,
        "sweep",
        lambda: [{
            "id": "p-conf",
            "name": "Almond drink",
            "brand": "Test",
            "conflicts": {"nuts": {"evidence": "ALMOND", "by": "lexicon"}},
            "source": {"nuts": "structured"},
        }],
    )
    out_str_path = str(tmp_path / "reports" / "dq.md")
    written = data_quality.write_report(out_str_path)
    assert written == out_str_path
    assert "p-conf" in Path(out_str_path).read_text(encoding="utf-8")

    for bad_path in (True, False, 123, ["x"], {"p": "x"}, "", "   "):
        with pytest.raises(ValueError, match="path"):
            data_quality.write_report(bad_path)


@pytest.mark.parametrize("non_dict_json", ["[]", "[1, 2]", "null", '"hello"', "123", "true", "{bad json"])
def test_taxonomy_non_dict_json_always_returns_empty_dict(
    non_dict_json: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(taxonomy, "ensure_stored", lambda: None)
    taxonomy.TAX_JSON.parent.mkdir(parents=True, exist_ok=True)
    taxonomy.TAX_JSON.write_text(non_dict_json, encoding="utf-8")
    taxonomy.labels_for.cache_clear()
    assert taxonomy.load_labels() == {}
    assert taxonomy.label("464306") is None
    assert taxonomy.product_type("464306") is None


@pytest.mark.parametrize(
    "bad_pid",
    [None, True, False, 0, 123, ["464306"], {"id": "464306"}, "", "   ", "464\x00", "464\ud800", "x" * 129],
)
def test_taxonomy_guards_bad_pids(bad_pid: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(taxonomy, "ensure_stored", lambda: None)
    taxonomy.TAX_JSON.parent.mkdir(parents=True, exist_ok=True)
    taxonomy.TAX_JSON.write_text(json.dumps({"464306": {"product_type": "milk"}}), encoding="utf-8")
    taxonomy.labels_for.cache_clear()

    assert taxonomy.label(bad_pid) is None
    assert taxonomy.product_type(bad_pid) is None


def test_taxonomy_guards_non_dict_hits_and_non_str_product_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(taxonomy, "ensure_stored", lambda: None)
    taxonomy.TAX_JSON.parent.mkdir(parents=True, exist_ok=True)
    taxonomy.TAX_JSON.write_text(
        json.dumps({
            "p-str": "not-a-dict",
            "p-list": [1, 2, 3],
            "p-null": None,
            "   ": {"product_type": "milk"},
            "p\x00bad": {"product_type": "milk"},
            "p-num-pt": {"product_type": 123},
            "p-bool-pt": {"product_type": True},
            "p-blank-pt": {"product_type": "   "},
            "p-good": {"product_type": "  oat_drink  "},
        }),
        encoding="utf-8",
    )
    taxonomy.labels_for.cache_clear()

    loaded = taxonomy.load_labels()
    assert set(loaded) == {"p-num-pt", "p-bool-pt", "p-blank-pt", "p-good"}
    assert taxonomy.label("p-str") is None
    assert taxonomy.label("p-list") is None
    assert taxonomy.label("p-null") is None
    assert taxonomy.product_type("p-num-pt") is None
    assert taxonomy.product_type("p-bool-pt") is None
    assert taxonomy.product_type("p-blank-pt") is None
    assert taxonomy.product_type("  p-good  ") == "oat_drink"


def test_taxonomy_parse_store_poll_and_limit_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_type = next(iter(taxonomy.TYPES))
    payload = json.dumps({
        "products": [
            {
                "id": "p-valid",
                "product_type": valid_type,
                "is_food": True,
                "pack_count": 6,
                "unit_content_amount": 0.0,
                "content_amount": -5.0,
                "content_unit": "L",
                "allergens_in_ingredients": ["milk"],
                "confidence": "high",
            },
            {
                "id": "p-bad-pack-zero",
                "product_type": valid_type,
                "is_food": True,
                "pack_count": 0,
                "unit_content_amount": 1.0,
                "content_amount": 1.0,
                "content_unit": "L",
                "allergens_in_ingredients": [],
                "confidence": "high",
            },
            {
                "id": "p-bad-pack-bool",
                "product_type": valid_type,
                "is_food": True,
                "pack_count": True,
                "unit_content_amount": 1.0,
                "content_amount": 1.0,
                "content_unit": "L",
                "allergens_in_ingredients": [],
                "confidence": "high",
            },
        ]
    })
    parsed = taxonomy._parse(payload, {"p-valid", "p-bad-pack-zero", "p-bad-pack-bool"})
    assert set(parsed) == {"p-valid"}
    assert parsed["p-valid"]["unit_content_amount"] is None
    assert parsed["p-valid"]["content_amount"] is None

    taxonomy._merge_labels({
        "p-valid": {
            "product_type": valid_type,
            "is_food": True,
            "pack_count": -2,
            "unit_content_amount": float("nan"),
            "content_amount": float("inf"),
            "content_unit": "L",
            "allergens_in_ingredients": ["milk", 123],
            "confidence": "high",
        }
    })
    db_file = tmp_path / "custom_tax.sqlite"
    assert taxonomy.store(db_path=db_file) == 1
    with sqlite3.connect(str(db_file)) as con:
        row = con.execute(
            "select pack_count, unit_content_amount, content_amount, allergens_in_ingredients from taxonomy where id='p-valid'"
        ).fetchone()
    assert row == (None, None, None, '["milk"]')

    for bad_db in (True, False, 123, ["db"], "", "   "):
        with pytest.raises(ValueError, match="db_path"):
            taxonomy.store(db_path=bad_db)

    for bad_lim in (True, False, 0, -1, -10, 1.5, "10", [5]):
        with pytest.raises(ValueError, match="limit"):
            taxonomy.submit(limit=bad_lim)
        with pytest.raises(ValueError, match="limit"):
            taxonomy.interactive(limit=bad_lim)

    taxonomy.JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
    for bad_job_json in ("[]", '{"job": ""}', '{"job": "   "}', '{"job": 123}'):
        taxonomy.JOB_FILE.write_text(bad_job_json, encoding="utf-8")
        with pytest.raises(ValueError, match="invalid taxonomy batch job file"):
            taxonomy.poll(store_when_done=False)

    from aimo import gemini

    fake_resp = SimpleNamespace(
        response=SimpleNamespace(
            text=json.dumps({
                "products": [{
                    "id": "p-batch",
                    "product_type": valid_type,
                    "is_food": True,
                    "pack_count": 1,
                    "unit_content_amount": 1.0,
                    "content_amount": 1.0,
                    "content_unit": "L",
                    "allergens_in_ingredients": [],
                    "confidence": "high",
                }]
            }),
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        )
    )
    fake_job = SimpleNamespace(
        state="JOB_STATE_SUCCEEDED",
        dest=SimpleNamespace(inlined_responses=[fake_resp]),
    )
    fake_client = SimpleNamespace(batches=SimpleNamespace(get=lambda name: fake_job))
    monkeypatch.setattr(gemini, "client", lambda: fake_client)
    monkeypatch.setattr(gemini, "record", lambda *_a, **_k: None)

    taxonomy.JOB_FILE.write_text(
        json.dumps({"job": "batches/123", "chunks": [[{"unhashable": 1}, "p-batch"]]}),
        encoding="utf-8",
    )
    st = taxonomy.poll(store_when_done=False)
    assert "SUCCEEDED" in st
    assert "p-batch" in taxonomy.load_labels()


def test_concurrent_cache_reads_and_atomic_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimo.catalog import netcheck

    monkeypatch.setattr(netcheck, "gemini_online", lambda: True)
    monkeypatch.setattr(
        embeddings,
        "_embed_batch",
        lambda texts, _f, retries=1: np.ones((len(texts), embeddings.DIM), dtype=np.float32),
    )
    monkeypatch.setattr(taxonomy, "ensure_stored", lambda: None)

    errors: list[BaseException] = []

    def _worker(tid: int) -> None:
        try:
            for step in range(6):
                embeddings.embed_query(f"query-{tid}-{step}")
                taxonomy._merge_labels({f"p-{tid}-{step}": {"product_type": "milk"}})
                assert isinstance(embeddings._qcache(), dict)
                assert isinstance(taxonomy.load_labels(), dict)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert list(embeddings.QCACHE.parent.glob("*.tmp*")) == []
    assert list(taxonomy.TAX_JSON.parent.glob("*.tmp*")) == []
    assert len(embeddings._qcache()) == 48
    assert len(taxonomy.load_labels()) == 48
