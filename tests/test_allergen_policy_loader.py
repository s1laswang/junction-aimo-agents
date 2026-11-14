from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aimo.catalog.allergens import (
    ALLERGEN_ALIASES,
    CODE_TO_GROUP,
    PolicyAllergenBlock,
    blocking,
    effective_excluded_allergens,
    normalize_allergen,
    normalize_allergens,
    policy_allergen_block,
    policy_excluded_allergens,
)
from aimo.models import EU14, AllergenStatus

ROOT = Path(__file__).resolve().parents[1]


def test_school_tampere_policy_block():
    blk = policy_allergen_block("school-tampere")
    assert blk == PolicyAllergenBlock(
        customer_id="school-tampere",
        excluded=("milk", "nuts"),
        reason="pupils with milk and nut allergies",
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="reject",
        policy_id="school-tampere",
    )
    assert policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert effective_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert effective_excluded_allergens("school-tampere", ["egg", "dairy"]) == ["eggs", "milk", "nuts"]
    with pytest.raises(FrozenInstanceError):
        setattr(blk, "customer_id", "other")


def test_hospital_meilahti_policy_block():
    blk = policy_allergen_block("  hospital-meilahti  ")
    assert blk == PolicyAllergenBlock(
        customer_id="hospital-meilahti",
        excluded=("gluten",),
        reason="coeliac patients",
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="needs_approval",
        policy_id="hospital-meilahti",
    )
    assert policy_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert effective_excluded_allergens("hospital-meilahti", ["wheat", "soija"]) == ["gluten", "soy"]


def test_hotel_aurora_policy_block():
    blk = policy_allergen_block("hotel-aurora")
    assert blk == PolicyAllergenBlock(
        customer_id="hotel-aurora",
        excluded=(),
        reason=None,
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="needs_approval",
        policy_id="hotel-aurora",
    )
    assert policy_excluded_allergens("hotel-aurora") == []
    assert effective_excluded_allergens("hotel-aurora") == []
    assert effective_excluded_allergens("hotel-aurora", ["peanut", "AM"]) == ["peanuts", "milk"]


@pytest.mark.parametrize(
    "cid,expected_cid",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("x", "x"),
        ("unknown-customer", "unknown-customer"),
        ("../bad/path", "../bad/path"),
    ],
)
def test_unknown_or_empty_customer_fails_gracefully(cid, expected_cid):
    blk = policy_allergen_block(cid)
    assert blk == PolicyAllergenBlock(
        customer_id=expected_cid,
        excluded=(),
        reason=None,
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="reject",
        policy_id=None,
    )
    assert policy_excluded_allergens(cid) == []
    assert effective_excluded_allergens(cid, ["milk"]) == ["milk"]


def test_toywholesaler_policy_dir_override(monkeypatch):
    from aimo.trust import policy as trust_policy

    assert policy_allergen_block("school-tampere").excluded == ("milk", "nuts")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", ROOT / "toywholesaler" / "policies")

    hosp = policy_allergen_block("hospital-meilahti")
    assert hosp.customer_id == "hospital-meilahti"
    assert hosp.excluded == ("gluten",)
    assert hosp.on_unknown == "needs_approval"
    assert hosp.policy_id == "hospital-meilahti"

    hotel = policy_allergen_block("hotel-aurora")
    assert hotel.excluded == ()
    assert hotel.policy_id == "hotel-aurora"

    missing_school = policy_allergen_block("school-tampere")
    assert missing_school.excluded == ()
    assert missing_school.policy_id is None


def test_custom_policy_dir_multi_rule_severity_and_invalid_yaml(tmp_path, monkeypatch):
    from aimo.trust import policy as trust_policy

    monkeypatch.setattr(trust_policy, "POLICY_DIR", tmp_path)

    (tmp_path / "multi-ward.yaml").write_text(
        "\n".join(
            [
                "policy_id: multi-ward",
                'version: "1.0"',
                "customer:",
                "  id: multi-ward",
                "  name: Multi Ward",
                "rules:",
                "  - id: ward_a",
                "    type: allergen_block",
                "    excluded: [dairy, maapähkinä]",
                "    reason: ward A allergies",
                "    on_contains: needs_approval",
                "    on_may_contain: allow",
                "    on_unknown: allow",
                "  - id: ward_b",
                "    type: allergen_block",
                "    excluded: [shellfish, milk]",
                "    reason: ward B allergies",
                "    on_contains: reject",
                "    on_may_contain: needs_approval",
                "    on_unknown: needs_approval",
            ]
        ),
        encoding="utf-8",
    )
    blk = policy_allergen_block("multi-ward")
    assert blk == PolicyAllergenBlock(
        customer_id="multi-ward",
        excluded=("milk", "peanuts", "crustaceans", "molluscs"),
        reason="ward A allergies; ward B allergies",
        on_contains="reject",
        on_may_contain="needs_approval",
        on_unknown="needs_approval",
        policy_id="multi-ward",
    )

    (tmp_path / "no-allergen-rule.yaml").write_text(
        "\n".join(
            [
                "policy_id: no-allergen-rule",
                'version: "1.0"',
                "customer:",
                "  id: no-allergen-rule",
                "rules:",
                "  - id: delivery_only",
                "    type: delivery",
            ]
        ),
        encoding="utf-8",
    )
    no_rule = policy_allergen_block("no-allergen-rule")
    assert no_rule == PolicyAllergenBlock(
        customer_id="no-allergen-rule",
        excluded=(),
        reason=None,
        on_contains="reject",
        on_may_contain="reject",
        on_unknown="reject",
        policy_id="no-allergen-rule",
    )

    (tmp_path / "broken-pol.yaml").write_text("policy_id: wrong-id\nversion: 1\n", encoding="utf-8")
    broken = policy_allergen_block("broken-pol")
    assert broken == PolicyAllergenBlock(customer_id="broken-pol")


def test_eu14_and_gs1_codes_normalize():
    for g in EU14:
        assert ALLERGEN_ALIASES[g] == (g,)
        assert normalize_allergen(g) == (g,)
        assert normalize_allergen(f"  {g.upper()}  ") == (g,)
    for code, group in CODE_TO_GROUP.items():
        assert normalize_allergen(code) == (group,)
        assert normalize_allergen(code.lower()) == (group,)
        assert normalize_allergen(f" {code} ") == (group,)


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("dairy", ("milk",)),
        ("cow's milk", ("milk",)),
        ("cow\u2019s milk", ("milk",)),
        ("cows milk", ("milk",)),
        ("maito", ("milk",)),
        ("mjölk", ("milk",)),
        ("nut", ("nuts",)),
        ("nuts", ("nuts",)),
        ("tree nut", ("nuts",)),
        ("tree nuts", ("nuts",)),
        ("tree_nuts", ("nuts",)),
        ("tree-nuts", ("nuts",)),
        ("pähkinä", ("nuts",)),
        ("pähkinät", ("nuts",)),
        ("nöt", ("nuts",)),
        ("nötter", ("nuts",)),
        ("peanut", ("peanuts",)),
        ("peanuts", ("peanuts",)),
        ("maapähkinä", ("peanuts",)),
        ("maapähkinät", ("peanuts",)),
        ("jordnöt", ("peanuts",)),
        ("jordnötter", ("peanuts",)),
        ("egg", ("eggs",)),
        ("eggs", ("eggs",)),
        ("kananmuna", ("eggs",)),
        ("muna", ("eggs",)),
        ("ägg", ("eggs",)),
        ("wheat", ("gluten",)),
        ("vehnä", ("gluten",)),
        ("rye", ("gluten",)),
        ("ruis", ("gluten",)),
        ("barley", ("gluten",)),
        ("ohra", ("gluten",)),
        ("spelt", ("gluten",)),
        ("kamut", ("gluten",)),
        ("gluteeni", ("gluten",)),
        ("cereals containing gluten", ("gluten",)),
        ("soya", ("soy",)),
        ("soija", ("soy",)),
        ("soybean", ("soy",)),
        ("soybeans", ("soy",)),
        ("kala", ("fish",)),
        ("fisk", ("fish",)),
        ("crustacean", ("crustaceans",)),
        ("äyriäiset", ("crustaceans",)),
        ("mollusc", ("molluscs",)),
        ("mollusk", ("molluscs",)),
        ("mollusks", ("molluscs",)),
        ("nilviäiset", ("molluscs",)),
        ("shellfish", ("crustaceans", "molluscs")),
        ("sulfite", ("sulphites",)),
        ("sulfites", ("sulphites",)),
        ("sulphite", ("sulphites",)),
        ("sulphur dioxide", ("sulphites",)),
        ("sulfiitit", ("sulphites",)),
        ("sesame seeds", ("sesame",)),
        ("seesami", ("sesame",)),
        ("selleri", ("celery",)),
        ("sinappi", ("mustard",)),
        ("lupiini", ("lupin",)),
    ],
)
def test_multilingual_aliases(alias, expected):
    assert normalize_allergen(alias) == expected
    assert normalize_allergen(f"  {alias.upper()}  ") == expected


def test_lactose_is_not_mapped_to_milk_and_unknown_tokens_preserved():
    for tok in ("lactose", "laktoosi", "laktos"):
        assert normalize_allergen(tok) == (tok,)
        assert "milk" not in normalize_allergen(tok)
    assert normalize_allergen("  Unknownium ") == ("unknownium",)
    assert normalize_allergen("") == ()
    assert normalize_allergen("   ") == ()
    assert normalize_allergens(["dairy", "AM", "milk", "", "shellfish", "crustaceans", "kiwi"]) == [
        "milk",
        "crustaceans",
        "molluscs",
        "kiwi",
    ]


def test_blocking_aliases_enums_unknown_and_inferred_source():
    allergens = {
        "milk": AllergenStatus.free,
        "nuts": AllergenStatus.free,
        "crustaceans": "free",
        "molluscs": "may_contain",
        "gluten": AllergenStatus.contains,
    }
    assert blocking(allergens, ["dairy", "tree nuts"]) == []
    assert blocking(allergens, ["shellfish", "wheat", "peanuts", "unknownium"]) == [
        ("molluscs", "may_contain"),
        ("gluten", "contains"),
        ("peanuts", "unknown"),
        ("unknownium", "unknown"),
    ]

    assert (
        blocking(
            {"milk": "free", "nuts": AllergenStatus.free},
            ["milk", "nuts"],
            allergen_source={"milk": "inferred:gemini", "nuts": "structured"},
        )
        == [("milk", "unknown")]
    )
    assert (
        blocking(
            {"milk": "free"},
            ["AM"],
            allergen_source={"milk": "inferred"},
        )
        == [("milk", "unknown")]
    )
    assert (
        blocking(
            {"milk": "contains"},
            ["milk"],
            allergen_source={"milk": "inferred:gemini"},
        )
        == [("milk", "contains")]
    )
    assert (
        blocking(
            {"gluten": "free"},
            ["gluten"],
            allergen_source={"gluten": "structured:claim"},
        )
        == []
    )
