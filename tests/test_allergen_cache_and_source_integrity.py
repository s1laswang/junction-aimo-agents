from __future__ import annotations

import hashlib
import io
import json
import tokenize
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from aimo import config, gemini
import aimo.catalog as cat
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.catalog import allergens as allergens_mod
from aimo.catalog import embeddings, gemini_allergens, get_product, product_from_row, textsearch
from aimo.catalog.allergens import (
    ALLERGEN_ALIASES,
    PolicyAllergenBlock,
    blocking,
    derive,
    effective_excluded_allergens,
    has_structured_data,
    normalize_allergen,
    normalize_allergens,
    policy_allergen_block,
    policy_allergens,
    policy_excluded_allergens,
    with_policy_allergens,
)
from aimo.catalog.plan import plan_need
from aimo.catalog.substitution import rank_substitutes
from aimo.gateway import human_auth, resilience, verify as verify_mod
from aimo.models import EU14, AllergenStatus, Need, Offer
from aimo.recipes.planner import DISHES, plan_recipe
from aimo.trust import policy as trust_policy
from aimo.trust import signing
from aimo.trust.audit import audit as audit_log

ROOT = Path(__file__).resolve().parents[1]
REAL_POLICY_DIR = Path(trust_policy.__file__).resolve().parent / "policies"
DELIVERY = date(2026, 11, 18)

NEW_INFLECTED_AND_PLURAL_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("maitoa", ("milk",)),
    ("milk proteins", ("milk",)),
    ("pähkinää", ("nuts",)),
    ("pähkinöitä", ("nuts",)),
    ("cashew nut", ("nuts",)),
    ("cashew nuts", ("nuts",)),
    ("pecan nut", ("nuts",)),
    ("pecan nuts", ("nuts",)),
    ("pistachio nut", ("nuts",)),
    ("pistachio nuts", ("nuts",)),
    ("macadamia nut", ("nuts",)),
    ("macadamia nuts", ("nuts",)),
    ("maapähkinää", ("peanuts",)),
    ("kananmunaa", ("eggs",)),
    ("munia", ("eggs",)),
    ("vehnää", ("gluten",)),
    ("ruista", ("gluten",)),
    ("ohraa", ("gluten",)),
    ("gluteenia", ("gluten",)),
    ("soijaa", ("soy",)),
    ("kalaa", ("fish",)),
    ("seesamia", ("sesame",)),
    ("selleriä", ("celery",)),
    ("sinappia", ("mustard",)),
    ("lupiinia", ("lupin",)),
    ("nilviäisiä", ("molluscs",)),
    ("äyriäisiä", ("crustaceans",)),
    ("sulfiitteja", ("sulphites",)),
]


class _PureAllergenEnum(Enum):
    MILK_PARTITIVE = "maitoa"
    NUT_PLURAL = "cashew nuts"


def _synthetic_row(
    pid: str,
    name: str,
    *,
    allergens: dict[str, str],
    allergen_source: dict[str, str],
    unit: str = "L",
    content: float = 1.0,
    brand: str = "Oatly",
    category: str = "10402",
    ingredients_text: str = "water, oats, rapeseed oil",
) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "name_en": name,
        "name_fi": None,
        "name_sv": None,
        "brand": brand,
        "fat_pct": 1.5,
        "content_amount": content,
        "content_unit": unit,
        "sales_unit": "ST",
        "sales_unit_size": 1,
        "base_content_amount": content,
        "allowed_lot_size": 1.0,
        "claims": [],
        "labels": [],
        "category": category,
        "allergens": dict(allergens),
        "allergen_source": dict(allergen_source),
        "ingredients_text": ingredients_text,
        "marketing_text": "",
        "delivery_reliability": 0.98,
        "co2_kg_per_kg": 0.4,
    }


def _synthetic_offer(
    pid: str,
    price: float = 1.80,
    *,
    available: bool = True,
    cutoff_passed: bool = False,
    available_date: date | None = None,
    amount: float = 500.0,
    unit: str = "L",
) -> Offer:
    return Offer(
        product_id=pid,
        requested_date=DELIVERY,
        unit_price=price,
        comparison_price=price,
        comparison_unit=unit,
        sales_unit="ST",
        available=available,
        available_amount=amount,
        available_date=available_date,
        cutoff_passed=cutoff_passed,
        sales_block=False,
        lead_time_days=1,
        logistic_model="STOCK",
        fetched_at=datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc),
    )


def test_policy_allergen_block_single_string_excluded_and_malformed_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trust_policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)

    (tmp_path / "single-str.yaml").write_text(
        "\n".join(
            [
                "policy_id: single-str",
                'version: "1.0"',
                "customer:",
                "  id: single-str",
                "  name: Single String Policy",
                "rules:",
                "  - id: milk_rule",
                "    type: allergen_block",
                "    excluded: milk",
                "    reason: single string milk block",
                "    on_contains: reject",
                "    on_may_contain: reject",
                "    on_unknown: reject",
            ]
        ),
        encoding="utf-8",
    )
    blk = policy_allergen_block("single-str")
    assert blk == PolicyAllergenBlock(
        customer_id="single-str",
        excluded=("milk",),
        reason="single string milk block",
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="reject",
        policy_id="single-str",
    )
    assert policy_excluded_allergens("single-str") == ["milk"]
    assert effective_excluded_allergens("single-str") == ["milk"]
    assert effective_excluded_allergens("single-str", "cashew nuts") == ["nuts", "milk"]

    (tmp_path / "mixed-rules.yaml").write_text(
        "\n".join(
            [
                "policy_id: mixed-rules",
                'version: "1.0"',
                "customer:",
                "  id: mixed-rules",
                "rules:",
                "  - id: r1",
                "    type: allergen_block",
                "    excluded: maitoa",
                "    reason: true",
                "  - id: r2",
                "    type: allergen_block",
                "    excluded: [null, true, false, pähkinöitä, milk proteins]",
                "    reason: verified kitchen rule",
                "  - id: r3",
                "    type: allergen_block",
                "    excluded: true",
                "  - id: r4",
                "    type: allergen_block",
                "    excluded: 42",
                "  - id: r5",
                "    type: allergen_block",
                "    excluded:",
                "      milk: true",
            ]
        ),
        encoding="utf-8",
    )
    blk_mixed = policy_allergen_block("mixed-rules")
    assert blk_mixed.excluded == ("milk", "nuts")
    assert blk_mixed.reason == "verified kitchen rule"

    monkeypatch.setattr(
        trust_policy,
        "planning_profile",
        lambda cid: {"excluded_allergens": "gluteenia", "allergen_reason": "coeliac ward", "policy_id": cid}
        if cid == "profile-str"
        else {},
        raising=False,
    )
    assert policy_allergens("profile-str") == (["gluten"], "coeliac ward", "profile-str")


def test_normalize_allergens_none_bool_and_non_string_guards() -> None:
    assert normalize_allergen(None) == ()
    assert normalize_allergen(True) == ()
    assert normalize_allergen(False) == ()
    assert normalize_allergen(0) == ()
    assert normalize_allergen(123) == ()
    assert normalize_allergen(["milk"]) == ()
    assert normalize_allergen({"milk": "free"}) == ()
    assert normalize_allergen(_PureAllergenEnum.MILK_PARTITIVE) == ("milk",)
    assert normalize_allergen(_PureAllergenEnum.NUT_PLURAL) == ("nuts",)

    assert normalize_allergens(None) == []
    assert normalize_allergens(True) == []
    assert normalize_allergens(False) == []
    assert normalize_allergens(123) == []
    assert normalize_allergens(b"milk") == []
    assert normalize_allergens(bytearray(b"milk")) == []
    assert normalize_allergens({"milk": True}) == []
    assert normalize_allergens("maitoa") == ["milk"]
    assert normalize_allergens(
        [None, True, False, "", "   ", 99, ["nested"], "maitoa", "MILK PROTEINS", "pähkinää", "shellfish", None]
    ) == ["milk", "nuts", "crustaceans", "molluscs"]

    assert effective_excluded_allergens(None, None) == []
    assert effective_excluded_allergens(None, True) == []
    assert effective_excluded_allergens(None, False) == []
    assert effective_excluded_allergens("school-tampere", [None, True, False, "vehnää"]) == [
        "gluten",
        "milk",
        "nuts",
    ]


@pytest.mark.parametrize("alias,expected", NEW_INFLECTED_AND_PLURAL_ALIASES)
def test_new_inflected_and_plural_allergen_aliases(alias: str, expected: tuple[str, ...]) -> None:
    assert len(NEW_INFLECTED_AND_PLURAL_ALIASES) == 28
    assert ALLERGEN_ALIASES[alias] == expected
    assert normalize_allergen(alias) == expected
    assert normalize_allergen(f"  {alias.upper()}  ") == expected
    assert normalize_allergen(alias.replace(" ", "_")) == expected
    assert normalize_allergen(alias.replace(" ", "-")) == expected
    assert normalize_allergens([None, False, alias, True]) == list(expected)

    group = expected[0]
    assert blocking({group: "free"}, [alias], allergen_source={group: "structured"}) == []
    assert blocking({group: "free"}, [alias], allergen_source={group: "none"}) == [(group, "unknown")]


def test_blocking_rejects_none_inferred_and_contradiction_sources_while_preserving_empty_source() -> None:
    for bad_src in (
        "none",
        "NONE",
        "  none  ",
        "none:missing",
        "inferred",
        "inferred:gemini",
        "  Inferred:Gemini  ",
        "contradiction",
        "CONTRADICTION",
        "contradiction:ingredients",
    ):
        assert blocking(
            {"milk": "free", "nuts": AllergenStatus.free},
            ["milk", "nuts"],
            allergen_source={"milk": bad_src, "nuts": "structured"},
        ) == [("milk", "unknown")]
        assert blocking(
            {"milk": AllergenStatus.free},
            ["maitoa"],
            allergen_source={"milk": bad_src},
        ) == [("milk", "unknown")]

    assert blocking({"milk": "free"}, ["milk"], allergen_source={}) == []
    assert blocking({"milk": "free"}, ["milk"], allergen_source=None) == []
    assert blocking({"milk": "free"}, ["milk"], allergen_source={"nuts": "none"}) == []
    assert blocking({"milk": "free"}, ["milk"], allergen_source={"milk": "structured"}) == []
    assert blocking({"gluten": "free"}, ["gluten"], allergen_source={"gluten": "structured:claim"}) == []

    for bad_src in ("none", "inferred:gemini", "contradiction", "structured"):
        assert blocking(
            {"milk": "contains", "nuts": "may_contain"},
            ["milk", "nuts"],
            allergen_source={"milk": bad_src, "nuts": bad_src},
        ) == [("milk", "contains"), ("nuts", "may_contain")]

    bad_obj: Any = ["not-a-dict"]
    assert blocking(bad_obj, ["milk"], allergen_source=bad_obj) == [("milk", "unknown")]


def test_derive_has_structured_data_and_with_policy_allergens_malformed_and_case_guards() -> None:
    for bad_cls in (None, True, False, 123, "allergen", b"allergen", {"name": "allergen"}):
        st, src, raw = derive(bad_cls)
        assert all(st[g] == "unknown" and src[g] == "none" for g in EU14)
        assert raw == {}
        assert has_structured_data(src) is False

    st_mix, src_mix, raw_mix = derive(
        [
            None,
            True,
            "not-a-dict",
            {"name": "allergen", "values": "not-a-list"},
            {"name": "allergen", "values": {"id": "AM", "unit": "CONTAINS"}},
            {
                "name": "allergen",
                "values": [
                    None,
                    True,
                    "bad-val",
                    {"id": True, "unit": "CONTAINS"},
                    {"id": "AM", "unit": "FREE_FROM"},
                    {"id": " ml ", "unit": " contains "},
                    {"id": " aw ", "unit": " free_from "},
                    {"id": " sh ", "unit": " may_contain "},
                    {"id": "X99", "unit": 123},
                ],
            },
            {
                "name": "nutritionalClaim",
                "values": [
                    None,
                    {"id": " free_from_eggs "},
                    {"id": 999},
                ],
            },
        ]
    )
    assert st_mix["milk"] == "contains"
    assert src_mix["milk"] == "structured"
    assert st_mix["gluten"] == "free"
    assert src_mix["gluten"] == "structured"
    assert st_mix["nuts"] == "may_contain"
    assert src_mix["nuts"] == "structured"
    assert st_mix["eggs"] == "free"
    assert src_mix["eggs"] == "structured:claim"
    assert raw_mix["AM"] == ["FREE_FROM"]
    assert raw_mix["ML"] == ["CONTAINS"]
    assert raw_mix["X99"] == ["?"]
    assert has_structured_data(src_mix) is True
    assert has_structured_data(None) is False
    assert has_structured_data(["structured"]) is False
    assert has_structured_data({"milk": None, "nuts": 123, "gluten": "none"}) is False
    assert has_structured_data({"milk": "  STRUCTURED:CLAIM  "}) is True

    assert with_policy_allergens(None) == (None, [])
    need_in = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        quantity=4.0,
        unit="L",
        delivery_by=DELIVERY,
        excluded_allergens=["vehnää"],
    )
    need_out, added = with_policy_allergens(need_in)
    assert need_out.excluded_allergens == ["gluten", "milk", "nuts"]
    assert added == ["milk", "nuts"]


def test_gemini_allergens_cache_path_isolation_and_offline_infer_via_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_a = tmp_path / "data_a" / "cache" / "gemini_allergens.json"
    cache_b = tmp_path / "data_b" / "cache" / "gemini_allergens.json"
    cache_empty = tmp_path / "data_empty" / "cache" / "gemini_allergens.json"
    custom_cache = tmp_path / "explicit_cache.json"

    cache_a.parent.mkdir(parents=True)
    cache_b.parent.mkdir(parents=True)
    cache_empty.parent.mkdir(parents=True)

    cache_a.write_text(
        json.dumps(
            {
                "id:101": {
                    "allergens": {"milk": "contains", "nuts": "not_found"},
                    "evidence": {"milk": "maitojauhe"},
                }
            }
        ),
        encoding="utf-8",
    )
    cache_b.write_text(
        json.dumps(
            {
                "id:101": {
                    "allergens": {"milk": "not_found", "nuts": "may_contain"},
                    "evidence": {"nuts": "saattaa sisältää pähkinää"},
                }
            }
        ),
        encoding="utf-8",
    )
    custom_cache.write_text(
        json.dumps(
            {
                "id:101": {
                    "allergens": {"gluten": "contains"},
                    "evidence": {"gluten": "vehnäjauho"},
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(gemini_allergens, "CACHE", cache_a)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    assert gemini_allergens.cached_inference("101") == {"milk": "contains", "nuts": "not_found"}
    assert gemini_allergens.cached_evidence("101") == {"milk": "maitojauhe"}

    monkeypatch.setattr(gemini_allergens, "CACHE", cache_b)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    assert gemini_allergens.cached_inference("101") == {"milk": "not_found", "nuts": "may_contain"}
    assert gemini_allergens.cached_evidence("101") == {"nuts": "saattaa sisältää pähkinää"}

    monkeypatch.setattr(gemini_allergens, "CACHE", custom_cache)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    assert gemini_allergens.cached_inference("101") == {"gluten": "contains"}
    assert gemini_allergens.cached_evidence("101") == {"gluten": "vehnäjauho"}

    monkeypatch.setattr(gemini_allergens, "CACHE", cache_empty)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    assert gemini_allergens.cached_inference("101") is None
    assert gemini_allergens.cached_evidence("101") == {}

    stored = gemini_allergens._store(
        "202",
        "kaura, vesi, maitojauhe",
        [
            gemini_allergens.GroupHint(group="gluten", status="may_contain", evidence="traces of oat"),
            gemini_allergens.GroupHint(group="gluten", status="contains", evidence="kaura"),
            gemini_allergens.GroupHint(group="gluten", status="may_contain", evidence="late weak hint"),
        ],
    )
    gemini_allergens._save()
    assert cache_empty.is_file()
    assert stored["gluten"] == "contains"
    assert stored["milk"] == "not_found"
    assert "free" not in stored.values()
    assert gemini_allergens.cached_inference("202") == stored
    assert gemini_allergens.cached_evidence("202") == {"gluten": "kaura"}

    gen_calls: list[str] = []

    def _fake_infer_generate(feature: str, _model: str, _prompt: str, config: Any = None) -> Any:
        gen_calls.append(feature)
        batch = gemini_allergens.HintBatch(
            products=[
                gemini_allergens.ProductHints(
                    id="303",
                    allergens=[
                        gemini_allergens.GroupHint(group="milk", status="contains", evidence="kerma"),
                    ],
                )
            ]
        )
        return SimpleNamespace(parsed=batch, text=batch.model_dump_json())

    monkeypatch.setattr(gemini, "generate", _fake_infer_generate)
    row_303 = {"id": "303", "name": "Cream Soup", "ingredients_text": "vesi, kerma, suola"}
    inf_first = gemini_allergens.infer_allergens(row_303)
    assert inf_first["milk"] == "contains"
    assert inf_first["nuts"] == "not_found"
    assert len(gen_calls) == 1

    inf_cached = gemini_allergens.infer_allergens(row_303)
    assert inf_cached == inf_first
    assert len(gen_calls) == 1

    row_303_changed = {"id": "303", "name": "Cream Soup", "ingredients_text": "vesi, kerma, suola, pippuri"}
    inf_recomputed = gemini_allergens.infer_allergens(row_303_changed)
    assert inf_recomputed["milk"] == "contains"
    assert len(gen_calls) == 2


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        "{not valid json",
        "",
        "[1, 2, 3]",
        '"plain string"',
        "null",
        "42",
        "true",
        json.dumps(
            {
                "id:bad1": "string-entry",
                "id:bad8": 99,
            }
        ),
        json.dumps(
            {
                "id:bad1": None,
                "id:bad5": {},
                "id:bad6": {"allergens": None, "evidence": None},
                "id:bad7": {"allergens": {}, "evidence": {}},
                "id:bad8": {"allergens": {"milk": "unexpected_status", "nuts": "free"}, "evidence": {"milk": "x"}},
            }
        ),
    ],
)
def test_corrupt_gemini_allergens_cache_never_raises_in_loaders_or_plan_need(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_payload: str,
) -> None:
    cache_file = tmp_path / "gemini_allergens.json"
    cache_file.write_text(corrupt_payload, encoding="utf-8")
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_file)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")

    loaded = gemini_allergens._load()
    if corrupt_payload in ("{not valid json", ""):
        assert loaded == {}
        assert gemini_allergens.cached_inference("bad1") is None
        assert gemini_allergens.cached_evidence("bad1") == {}

    unk_allergens = {g: ("unknown" if g == "milk" else "free") for g in EU14}
    unk_source = {g: ("none" if g == "milk" else "structured") for g in EU14}
    safe_allergens = {g: "free" for g in EU14}
    safe_source = {g: "structured" for g in EU14}

    rows = {
        "bad1": _synthetic_row("bad1", "Oat Drink Unverified 1 L", allergens=unk_allergens, allergen_source=unk_source),
        "bad8": _synthetic_row("bad8", "Oat Drink Corrupt Hint 1 L", allergens=unk_allergens, allergen_source=unk_source),
        "safe1": _synthetic_row("safe1", "Oatly Oat Drink Safe 1 L", allergens=safe_allergens, allergen_source=safe_source),
    }
    offers = {pid: _synthetic_offer(pid) for pid in rows}

    for check_pid in ("bad1", "bad8"):
        prod = product_from_row(rows[check_pid])
        assert blocking(prod.allergens, ["milk", "nuts"], allergen_source=prod.allergen_source) == [("milk", "unknown")]

    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [(pid, 10.0) for pid in rows])
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {str(i): rows[str(i)] for i in ids if str(i) in rows})
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *_a, **_kw: [])
    monkeypatch.setattr("aimo.catalog.plan._con", lambda: None)

    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=lambda ids, *_a, **_kw: {i: offers[i] for i in ids if i in offers})
    assert [ln.product_id for ln in plan.lines] == ["safe1"]


@pytest.mark.parametrize(
    "corrupt_recipe_payload",
    [
        "{broken json",
        "",
        "{}",
        json.dumps({"other_key": {"compatible": False, "confidence": "low", "reason": "other"}}),
    ],
)
def test_corrupt_gemini_recipe_cache_and_path_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_recipe_payload: str,
) -> None:
    orig_obj = {"name": "Valio Oat Drink 1 L"}
    sub_obj = {"name": "Oatly Oat Drink 1 L"}
    use_str = "porridge cooking"
    key = hashlib.sha1(f"Valio Oat Drink 1 L|Oatly Oat Drink 1 L|{use_str}".encode()).hexdigest()[:16]

    rfile_1 = tmp_path / "rdata_1" / "cache" / "gemini_recipe.json"
    rfile_2 = tmp_path / "rdata_2" / "cache" / "gemini_recipe.json"
    rfile_1.parent.mkdir(parents=True)
    rfile_2.parent.mkdir(parents=True)
    rfile_1.write_text(corrupt_recipe_payload, encoding="utf-8")

    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", rfile_1)

    calls: list[str] = []

    def _fake_generate(feature: str, _model: str, _prompt: str, config: Any = None) -> Any:
        calls.append(feature)
        parsed = gemini_allergens.RecipeJudgement(
            compatible=True,
            confidence="high",
            reason="Suitable oat substitute for cooking.",
        )
        return SimpleNamespace(parsed=parsed, text=parsed.model_dump_json())

    monkeypatch.setattr(gemini, "generate", _fake_generate)

    out = gemini_allergens.recipe_compatible(orig_obj, sub_obj, use=use_str)
    assert out["compatible"] is True
    assert out["confidence"] == "high"
    assert out["source"] == "inferred:gemini"
    assert len(calls) == 1

    persisted = json.loads(rfile_1.read_text(encoding="utf-8"))
    assert isinstance(persisted, dict) and persisted[key]["compatible"] is True

    out_cached = gemini_allergens.recipe_compatible(orig_obj, sub_obj, use=use_str)
    assert out_cached["compatible"] is True
    assert len(calls) == 1

    rfile_2.write_text(
        json.dumps({key: {"compatible": False, "confidence": "medium", "reason": "Too thin for sauce."}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", rfile_2)
    out_dir2 = gemini_allergens.recipe_compatible(orig_obj, sub_obj, use=use_str)
    assert out_dir2["compatible"] is False
    assert out_dir2["reason"] == "Too thin for sauce."
    assert len(calls) == 1


@pytest.mark.parametrize(
    "recipe_cache_payload,expect_flag",
    [
        ("{corrupt", True),
        ("", True),
        ('["corrupt", "list"]', False),
        ("null", False),
        ("123", False),
    ],
)
def test_plan_need_with_recipe_checks_enabled_survives_corrupt_recipe_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipe_cache_payload: str,
    expect_flag: bool,
) -> None:
    rcache = tmp_path / "cache" / "gemini_recipe.json"
    acache = tmp_path / "cache" / "gemini_allergens.json"
    rcache.parent.mkdir(parents=True, exist_ok=True)
    rcache.write_text(recipe_cache_payload, encoding="utf-8")
    acache.write_text("{corrupt", encoding="utf-8")

    monkeypatch.setattr(gemini_allergens, "CACHE", acache)
    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", rcache)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "1")
    monkeypatch.setattr("aimo.catalog.netcheck.gemini_online", lambda: True)
    monkeypatch.setattr(gemini, "client", lambda: object())

    def _fake_generate(_feature: str, _model: str, _prompt: str, config: Any = None) -> Any:
        parsed = gemini_allergens.RecipeJudgement(
            compatible=False,
            confidence="medium",
            reason="Different texture than usual oat drink.",
        )
        return SimpleNamespace(parsed=parsed, text=parsed.model_dump_json())

    monkeypatch.setattr(gemini, "generate", _fake_generate)

    safe_alg = {g: "free" for g in EU14}
    safe_src = {g: "structured" for g in EU14}
    rows = {
        "pref-oos": _synthetic_row("pref-oos", "Valio Oat Drink 1 L", brand="Valio", allergens=safe_alg, allergen_source=safe_src),
        "sub-ok": _synthetic_row("sub-ok", "Oatly Oat Drink 1 L", brand="Oatly", allergens=safe_alg, allergen_source=safe_src),
    }
    offers = {
        "pref-oos": _synthetic_offer("pref-oos", available=False, amount=0.0),
        "sub-ok": _synthetic_offer("sub-ok", available=True, amount=100.0),
    }
    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [("pref-oos", 20.0), ("sub-ok", 18.0)])
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {str(i): rows[str(i)] for i in ids if str(i) in rows})
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *_a, **_kw: [])
    monkeypatch.setattr("aimo.catalog.plan._con", lambda: None)

    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        preferred_product_ids=["pref-oos"],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=lambda ids, *_a, **_kw: {i: offers[i] for i in ids if i in offers})
    assert [ln.product_id for ln in plan.lines] == ["sub-ok"]
    if expect_flag:
        assert "recipe_check_failed" in plan.lines[0].flags
        assert any("Gemini recipe check vs usual product: NOT compatible" in r for r in plan.lines[0].reasons)


def test_cached_inference_hints_never_unlock_unknown_or_source_none_in_plan_need_or_rank_substitutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "cache" / "gemini_allergens.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_path)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    cache_path.write_text(
        json.dumps(
            {
                "id:u-not-found": {
                    "allergens": {g: "not_found" for g in EU14},
                    "evidence": {},
                    "source": "inferred:gemini",
                },
                "id:u-contains": {
                    "allergens": {**{g: "not_found" for g in EU14}, "milk": "contains"},
                    "evidence": {"milk": "hera"},
                    "source": "inferred:gemini",
                },
                "id:u-may": {
                    "allergens": {**{g: "not_found" for g in EU14}, "nuts": "may_contain"},
                    "evidence": {"nuts": "manteli"},
                    "source": "inferred:gemini",
                },
                "id:u-forged-free": {
                    "allergens": {g: "free" for g in EU14},
                    "evidence": {},
                    "source": "inferred:gemini",
                },
            }
        ),
        encoding="utf-8",
    )

    unk_milk_alg = {g: ("unknown" if g == "milk" else "free") for g in EU14}
    unk_milk_src = {g: ("none" if g == "milk" else "structured") for g in EU14}
    unk_nuts_alg = {g: ("unknown" if g == "nuts" else "free") for g in EU14}
    unk_nuts_src = {g: ("none" if g == "nuts" else "structured") for g in EU14}
    forged_src_none_alg = {g: "free" for g in EU14}
    forged_src_none_src = {g: ("none" if g == "milk" else "structured") for g in EU14}
    forged_src_inf_src = {g: ("inferred:gemini" if g == "nuts" else "structured") for g in EU14}
    forged_src_con_src = {g: ("contradiction" if g == "milk" else "structured") for g in EU14}
    safe_alg = {g: "free" for g in EU14}
    safe_src = {g: "structured" for g in EU14}

    unsafe_rows = {
        "u-not-found": _synthetic_row(
            "u-not-found", "Oat Drink Hint Not Found 1 L", allergens=unk_milk_alg, allergen_source=unk_milk_src
        ),
        "u-contains": _synthetic_row(
            "u-contains", "Oat Drink Hint Contains 1 L", allergens=unk_milk_alg, allergen_source=unk_milk_src
        ),
        "u-may": _synthetic_row(
            "u-may", "Oat Drink Hint May Contain 1 L", allergens=unk_nuts_alg, allergen_source=unk_nuts_src
        ),
        "u-forged-free": _synthetic_row(
            "u-forged-free", "Oat Drink Forged Free Hint 1 L", allergens=unk_milk_alg, allergen_source=unk_milk_src
        ),
        "u-src-none": _synthetic_row(
            "u-src-none", "Oat Drink Source None Free 1 L", allergens=forged_src_none_alg, allergen_source=forged_src_none_src
        ),
        "u-src-inf": _synthetic_row(
            "u-src-inf", "Oat Drink Source Inferred Free 1 L", allergens=forged_src_none_alg, allergen_source=forged_src_inf_src
        ),
        "u-src-con": _synthetic_row(
            "u-src-con", "Oat Drink Source Contradiction Free 1 L", allergens=forged_src_none_alg, allergen_source=forged_src_con_src
        ),
    }

    for pid, row in unsafe_rows.items():
        prod = product_from_row(row)
        blk = blocking(prod.allergens, ["milk", "nuts"], allergen_source=prod.allergen_source)
        assert blk, f"product_from_row unexpectedly unlocked {pid}: {prod.allergens} / {prod.allergen_source}"

    offers_only_unsafe = {
        pid: _synthetic_offer(
            pid,
            price=1.20,
            available=(pid != "u-may"),
            cutoff_passed=(pid == "u-may"),
            available_date=date(2026, 11, 20) if pid == "u-may" else None,
        )
        for pid in unsafe_rows
    }

    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [(pid, 20.0) for pid in unsafe_rows])
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {str(i): unsafe_rows[str(i)] for i in ids if str(i) in unsafe_rows},
    )
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *_a, **_kw: [])
    monkeypatch.setattr("aimo.catalog.plan._con", lambda: None)

    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        quantity=12.0,
        unit="L",
        delivery_by=DELIVERY,
        preferences={"allow_split_delivery": True},
    )

    hints_map = {pid: h for pid in unsafe_rows if (h := gemini_allergens.cached_inference(pid))}
    assert set(hints_map.keys()) == {"u-not-found", "u-contains", "u-may", "u-forged-free"}

    ranked = rank_substitutes(
        need,
        offers_only_unsafe,
        limit=20,
        rows=unsafe_rows,
        relevance={pid: 20.0 for pid in unsafe_rows},
        inferred_allergens=hints_map,
    )
    assert ranked
    for cand in ranked:
        assert cand.tier == "rejected", (cand.product.id, cand.tier, cand.flags, cand.reasons)
        assert "allergen_unknown" in cand.flags, (cand.product.id, cand.flags)
        if cand.product.id in hints_map:
            assert "allergen_inferred_hint" in cand.flags

    plan_shortfall = plan_need(
        need,
        offers_fn=lambda ids, *_a, **_kw: {i: offers_only_unsafe[i] for i in ids if i in offers_only_unsafe},
    )
    assert plan_shortfall.lines == []
    assert plan_shortfall.covered_amount == 0.0
    assert plan_shortfall.shortfall == 12.0

    all_rows = {
        **unsafe_rows,
        "safe-struct": _synthetic_row(
            "safe-struct",
            "Oatly Oat Drink Structured Safe 1 L",
            allergens=safe_alg,
            allergen_source=safe_src,
        ),
    }
    all_offers = {**offers_only_unsafe, "safe-struct": _synthetic_offer("safe-struct", price=1.95)}
    monkeypatch.setattr(textsearch, "fts_search", lambda *_a, **_kw: [(pid, 20.0) for pid in all_rows])
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {str(i): all_rows[str(i)] for i in ids if str(i) in all_rows},
    )

    plan_safe = plan_need(
        need,
        offers_fn=lambda ids, *_a, **_kw: {i: all_offers[i] for i in ids if i in all_offers},
    )
    assert [ln.product_id for ln in plan_safe.lines] == ["safe-struct"]
    assert plan_safe.covered_amount == 12.0
    assert plan_safe.shortfall == 0.0


def _close_cached_catalog_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture(scope="module")
def replay_recipe_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    product_gz = cat.FIXTURE_CATALOG_DIR / "valio_aimo_junction26_product_data.json.gz"
    if not (replay.BUNDLED_DIR / "meta.json").is_file() or not product_gz.is_file():
        pytest.skip("recorded replay snapshot or fixture catalog is not present")

    cfg_attrs = (
        "DATA_DIR",
        "PRODUCT_JSON",
        "RELIABILITY_CSV",
        "CO2_CSV",
        "CATALOG_DB",
        "STATE_DB",
        "KEY_DIR",
        "OFFLINE",
        "AIMO_API_KEY",
        "GEMINI_API_KEY",
    )
    cstore_attrs = ("PRODUCT_JSON", "RELIABILITY_CSV", "CO2_CSV", "CATALOG_DB")

    orig_config = {k: getattr(config, k) for k in cfg_attrs}
    orig_cstore = {k: getattr(catalog_store, k) for k in cstore_attrs}
    orig_cat_source = cat._SOURCE
    orig_store_db = store.DB_PATH
    orig_bc_client = bc._client
    orig_policy_dir = trust_policy.POLICY_DIR
    orig_allergen_pdir = allergens_mod._last_policy_dir
    orig_verify_pdir = verify_mod._last_trust_policy_dir
    orig_get_rows = textsearch.get_rows

    base_dir = tmp_path_factory.mktemp("allergen_recipe_all11")
    data_dir = base_dir / "data"
    key_dir = base_dir / "keys"
    state_db = base_dir / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    _close_cached_catalog_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    verify_mod._last_trust_policy_dir = None

    try:
        mp = pytest.MonkeyPatch()
        mp.setenv("AIMO_REPLAY", "1")
        mp.setenv("AIMO_OFFLINE", "1")
        mp.setenv("AIMO_API_KEY", "")
        mp.setenv("VALIO_API_KEY", "")
        mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)
        mp.setattr(config, "DATA_DIR", data_dir)
        mp.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
        mp.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
        mp.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
        mp.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
        mp.setattr(config, "OFFLINE", True)
        mp.setattr(config, "AIMO_API_KEY", "")
        mp.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
        try:
            src = cat._use_fixture_catalog()
            assert src == "fixture-subset"
            pjson = config.PRODUCT_JSON
            rel_csv = config.RELIABILITY_CSV
            co2_csv = config.CO2_CSV
            cat_db = config.CATALOG_DB
            mp.setattr(catalog_store, "PRODUCT_JSON", pjson)
            mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
            mp.setattr(catalog_store, "CO2_CSV", co2_csv)
            mp.setattr(catalog_store, "CATALOG_DB", cat_db)
            catalog_store.load_catalog(cat_db, force=True)
            con = textsearch._con()
            all_ids = [r["id"] for r in con.execute("select id from products").fetchall()]
            rows_by_id = orig_get_rows(all_ids)
        finally:
            _close_cached_catalog_con()
            mp.undo()

        replay_client = replay.ReplayClient(replay.BUNDLED_DIR)
        yield {
            "data_dir": data_dir,
            "key_dir": key_dir,
            "state_db": state_db,
            "product_json": pjson,
            "rel_csv": rel_csv,
            "co2_csv": co2_csv,
            "catalog_db": cat_db,
            "rows_by_id": rows_by_id,
            "replay_client": replay_client,
        }
    finally:
        _close_cached_catalog_con()
        for k, v in orig_config.items():
            setattr(config, k, v)
        for k, v in orig_cstore.items():
            setattr(catalog_store, k, v)
        cat._SOURCE = orig_cat_source
        store.DB_PATH = orig_store_db
        with store._lock:
            cached_state_con = store._conns.pop(str(state_db), None)
        if cached_state_con is not None:
            try:
                cached_state_con.close()
            except Exception:
                pass
        audit_log._ready.discard(str(state_db))
        signing._keys.pop(str(key_dir / "ed25519-signing.pem"), None)
        bc.set_client(orig_bc_client)
        trust_policy.POLICY_DIR = orig_policy_dir
        if hasattr(trust_policy, "_load_policy_cached"):
            trust_policy._load_policy_cached.cache_clear()
        allergens_mod._last_policy_dir = orig_allergen_pdir
        verify_mod._last_trust_policy_dir = orig_verify_pdir
        human_auth.reset_for_tests()
        for br in resilience._breakers.values():
            br.reset()


@pytest.mark.parametrize(
    "customer_id,expected_excl",
    [
        ("school-tampere", ("milk", "nuts")),
        ("hospital-meilahti", ("gluten",)),
    ],
)
@pytest.mark.parametrize("dish_id", [d["id"] for d in DISHES])
def test_all_11_recipes_enforce_structured_allergen_hard_blocks_on_replay_catalog(
    replay_recipe_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    customer_id: str,
    expected_excl: tuple[str, ...],
    dish_id: str,
) -> None:
    assert len(DISHES) == 11

    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")

    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", replay_recipe_env["data_dir"])
    monkeypatch.setattr(config, "PRODUCT_JSON", replay_recipe_env["product_json"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", replay_recipe_env["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", replay_recipe_env["co2_csv"])
    monkeypatch.setattr(config, "CATALOG_DB", replay_recipe_env["catalog_db"])
    monkeypatch.setattr(config, "KEY_DIR", replay_recipe_env["key_dir"])
    monkeypatch.setattr(config, "STATE_DB", replay_recipe_env["state_db"])
    monkeypatch.setattr(store, "DB_PATH", replay_recipe_env["state_db"])

    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", replay_recipe_env["product_json"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", replay_recipe_env["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", replay_recipe_env["co2_csv"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", replay_recipe_env["catalog_db"])
    monkeypatch.setattr(cat, "_SOURCE", "fixture-subset")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    monkeypatch.setattr(gemini_allergens, "CACHE", replay_recipe_env["data_dir"] / "cache" / "gemini_allergens.json")
    monkeypatch.setattr(gemini_allergens, "_mem", None)

    rows_by_id = replay_recipe_env["rows_by_id"]
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {str(i): dict(rows_by_id[str(i)]) for i in ids if str(i) in rows_by_id},
    )
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *_a, **_kw: [])
    if textsearch._con.cache_info().currsize == 0:
        textsearch._con()

    prev_client = bc._client
    bc.set_client(replay_recipe_env["replay_client"])
    try:
        res = plan_recipe(
            recipe=dish_id,
            portions=50,
            delivery_by=DELIVERY,
            customer_id=customer_id,
            include_candidates=True,
        )
    finally:
        bc.set_client(prev_client)

    assert res["customer_id"] == customer_id
    assert set(expected_excl) <= set(res["excluded_allergens"])
    assert res["ingredients"], f"no ingredients returned for {dish_id}"

    for ing in res["ingredients"]:
        assert set(expected_excl) <= set(ing["need"]["excluded_allergens"])
        for ln in ing["plan"]["lines"]:
            pid = str(ln["product_id"])
            flags = ln.get("flags") or []
            assert "allergen_unknown" not in flags, (dish_id, customer_id, pid, flags)
            for g in expected_excl:
                assert f"contains:{g}" not in flags
                assert f"may_contain:{g}" not in flags
            row = rows_by_id.get(pid)
            assert row is not None, f"planned product {pid} missing from replay catalog"
            assert blocking(row.get("allergens") or {}, expected_excl, allergen_source=row.get("allergen_source")) == []
            prod = get_product(pid)
            assert prod is not None
            for g in expected_excl:
                st = prod.allergens.get(g)
                st_val = st.value if hasattr(st, "value") else str(st)
                src_val = prod.allergen_source.get(g, "")
                assert st_val == "free", (dish_id, customer_id, pid, prod.name, g, st_val, src_val)
                assert src_val.startswith("structured"), (dish_id, customer_id, pid, prod.name, g, st_val, src_val)

        for cand in ing["plan"]["candidates"]:
            if cand["tier"] == "rejected":
                continue
            cpid = str(cand["product"]["id"])
            crow = rows_by_id.get(cpid)
            assert crow is not None
            assert blocking(crow.get("allergens") or {}, expected_excl, allergen_source=crow.get("allergen_source")) == []

    for qline in res["quote_request"]["lines"]:
        qpid = str(qline["product_id"])
        qrow = rows_by_id.get(qpid)
        assert qrow is not None
        assert blocking(qrow.get("allergens") or {}, expected_excl, allergen_source=qrow.get("allergen_source")) == []


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    for strict_path in (
        Path(__file__),
        ROOT / "aimo" / "catalog" / "allergens.py",
    ):
        src = strict_path.read_text(encoding="utf-8")
        assert "\x23" not in src, strict_path
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        assert not [tok for tok in tokens if tok.type == tokenize.COMMENT], strict_path
        assert "\u2014" not in src, strict_path
    assert "\u2014" not in (ROOT / "aimo" / "catalog" / "gemini_allergens.py").read_text(encoding="utf-8")
