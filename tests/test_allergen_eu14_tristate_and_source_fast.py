from __future__ import annotations

import io
import tokenize
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import aimo.catalog.allergens as allergens_mod
import aimo.catalog.data_quality as data_quality
import aimo.catalog.embeddings as embeddings_mod
import aimo.catalog.gemini_allergens as gemini_allergens_mod
import aimo.catalog.plan as plan_mod
import aimo.catalog.taxonomy as taxonomy_mod
import aimo.catalog.textsearch as textsearch_mod
from aimo.catalog.allergens import (
    CLAIM_FREE,
    CODE_TO_GROUP,
    GS1_GROUPS,
    blocking,
    derive,
    effective_excluded_allergens,
    has_structured_data,
)
from aimo.catalog.plan import plan_need
from aimo.models import EU14, AllergenStatus, Need, Offer
from aimo.trust import policy as trust_policy

DELIVERY = date(2026, 11, 18)
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
REAL_POLICY_DIR = Path(trust_policy.__file__).resolve().parent / "policies"

EXPECTED_EU14 = [
    "gluten",
    "crustaceans",
    "eggs",
    "fish",
    "peanuts",
    "soy",
    "milk",
    "nuts",
    "celery",
    "mustard",
    "sesame",
    "sulphites",
    "lupin",
    "molluscs",
]

CONTRADICTION_INGREDIENTS: dict[str, str] = {
    "gluten": "water, wheat flour, rapeseed oil, salt",
    "crustaceans": "water, shrimp extract, rapeseed oil, salt",
    "eggs": "water, egg yolk, rapeseed oil, salt",
    "fish": "water, salmon extract, rapeseed oil, salt",
    "peanuts": "water, roasted peanuts, rapeseed oil, salt",
    "soy": "water, soybeans, rapeseed oil, salt",
    "milk": "water, whole milk powder, rapeseed oil, salt",
    "nuts": "water, roasted almonds, rapeseed oil, salt",
    "celery": "water, celery root, rapeseed oil, salt",
    "mustard": "water, mustard seed, rapeseed oil, salt",
    "sesame": "water, sesame paste, rapeseed oil, salt",
    "sulphites": "water, wine vinegar, sulphites, salt",
    "lupin": "water, lupin protein, rapeseed oil, salt",
    "molluscs": "water, mussel extract, rapeseed oil, salt",
}

ALL_SUBCODES: list[tuple[str, str]] = [
    (group, code)
    for group, (heads, members) in GS1_GROUPS.items()
    for code in members
    if code not in heads
]

ALL_MEMBER_CODES: list[tuple[str, str]] = [
    (group, code)
    for group, (_heads, members) in GS1_GROUPS.items()
    for code in members
]


def _make_cls(
    *,
    allergen: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    non_allergen: list[tuple[str, str] | str] | tuple[tuple[str, str] | str, ...] = (),
    claims: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if allergen:
        out.append(
            {
                "name": "allergen",
                "type": "ENUM",
                "values": [{"id": code, "unit": unit} for code, unit in allergen],
            }
        )
    if non_allergen:
        vals = [
            {"id": item[0], "unit": item[1]} if isinstance(item, tuple) else {"id": item, "unit": "FREE_FROM"}
            for item in non_allergen
        ]
        out.append({"name": "nonAllergen", "type": "ENUM", "values": vals})
    if claims:
        out.append(
            {
                "name": "nutritionalClaim",
                "type": "ENUM",
                "values": [{"id": cl, "unit": "FREE_FROM"} for cl in claims],
            }
        )
    return out


def _all_free_except(**overrides: tuple[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    allergens = {g: "free" for g in EU14}
    sources = {g: "structured" for g in EU14}
    for group, (st, src) in overrides.items():
        allergens[group] = st
        sources[group] = src
    return allergens, sources


def _synth_row(
    pid: str,
    name: str,
    *,
    allergens: dict[str, str],
    allergen_source: dict[str, str],
    unit: str = "L",
    content: float = 1.0,
    brand: str = "NordicKitchen",
    fat: float = 1.5,
    claims: tuple[str, ...] = (),
    category: str = "10400",
    ingredients_text: str = "water, vegetables, rapeseed oil, salt",
    allergen_conflicts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": pid,
        "name": name,
        "name_en": name,
        "name_fi": None,
        "name_sv": None,
        "brand": brand,
        "fat_pct": fat,
        "content_amount": content,
        "content_unit": unit,
        "sales_unit": "ST",
        "sales_unit_size": 1,
        "base_content_amount": content,
        "allowed_lot_size": 1.0,
        "claims": list(claims),
        "labels": [],
        "category": category,
        "allergens": dict(allergens),
        "allergen_source": dict(allergen_source),
        "ingredients_text": ingredients_text,
        "marketing_text": "",
        "delivery_reliability": 0.99,
        "co2_kg_per_kg": 0.4,
    }
    if allergen_conflicts is not None:
        row["allergen_conflicts"] = dict(allergen_conflicts)
    return row


def _offer_for(
    pid: str,
    d: date,
    *,
    price: float = 2.0,
    unit: str = "L",
    available: bool = True,
    available_amount: float = 5000.0,
    cutoff_passed: bool = False,
    available_date: date | None = None,
) -> Offer:
    return Offer(
        product_id=pid,
        requested_date=d,
        available=available,
        available_amount=available_amount,
        available_date=available_date,
        closing_time=(NOW - timedelta(hours=2)) if cutoff_passed else (NOW + timedelta(days=2)),
        cutoff_passed=cutoff_passed,
        logistic_model="Stock",
        sales_block=False,
        sales_unit="ST",
        unit_price=price,
        comparison_price=price,
        comparison_unit=unit,
        fetched_at=NOW,
    )


def _make_offers_fn(
    prices: dict[str, float] | None = None,
    *,
    default_price: float = 2.0,
    unit: str = "L",
    overrides: dict[str, Offer] | None = None,
):
    price_map = prices or {}
    override_map = overrides or {}

    def _offers(ids: list[str], d: date, customer_id: str | None = None) -> dict[str, Offer]:
        out: dict[str, Offer] = {}
        for pid in ids:
            if pid in override_map:
                out[pid] = override_map[pid]
            else:
                out[pid] = _offer_for(pid, d, price=price_map.get(pid, default_price), unit=unit)
        return out

    return _offers


def _install_in_memory_catalog(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, dict[str, Any]],
    *,
    hints: dict[str, dict[str, str]] | None = None,
) -> None:
    hint_map = hints or {}
    monkeypatch.setattr(plan_mod, "_con", lambda: None)
    monkeypatch.setattr(plan_mod, "get_rows", lambda ids: {str(i): rows[str(i)] for i in ids if str(i) in rows})
    monkeypatch.setattr(textsearch_mod, "semantic_hits", lambda *_a, **_kw: [])
    monkeypatch.setattr(embeddings_mod, "similar_products", lambda *_a, **_kw: [])
    monkeypatch.setattr(gemini_allergens_mod, "cached_inference", lambda pid: hint_map.get(str(pid)))
    monkeypatch.setattr(taxonomy_mod, "label", lambda _pid: None)

    def _fts(
        query: str,
        *,
        claims: list[str] | None = None,
        category: str | None = None,
        content_unit: str | None = None,
        limit: int = 20,
        pool: int = 400,
        free_of: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        _ = pool
        words = [w for w in query.lower().replace("-", " ").split() if len(w) >= 3]
        hits: list[tuple[str, float]] = []
        for pid, d in rows.items():
            if claims and not set(claims) <= set(d.get("claims") or []):
                continue
            if category and d.get("category") != category:
                continue
            if content_unit and d.get("content_unit") != content_unit:
                continue
            if free_of and any((d.get("allergens") or {}).get(g) != "free" for g in free_of):
                continue
            text = " ".join(str(d.get(k) or "") for k in ("name_en", "name", "name_fi", "name_sv")).lower()
            matched = sum(1 for w in words if w in text)
            if matched or not words:
                hits.append((pid, float(matched or 1)))
        hits.sort(key=lambda item: (-item[1], item[0]))
        return hits[:limit]

    monkeypatch.setattr(plan_mod, "fts_search", _fts)


def _write_policy(
    policy_dir: Path,
    customer_id: str,
    excluded: list[str],
    *,
    reason: str = "clinical allergen policy",
    preferred_ids: list[str] | None = None,
) -> None:
    pref_lines = ["preferred_products:"]
    if preferred_ids:
        for pid in preferred_ids:
            pref_lines.append(f'  - {{product_id: "{pid}", label: "Preferred {pid}"}}')
    else:
        pref_lines = ["preferred_products: []"]
    excl_yaml = ", ".join(excluded)
    text = "\n".join(
        [
            f"policy_id: {customer_id}",
            'version: "1.0"',
            "customer:",
            f"  id: {customer_id}",
            f"  name: Customer {customer_id}",
            *pref_lines,
            "rules:",
            "  - id: allergens",
            "    type: allergen_block",
            f"    excluded: [{excl_yaml}]",
            f"    reason: {reason}",
            "    on_contains: reject",
            "    on_may_contain: reject",
            "    on_unknown: reject",
        ]
    )
    (policy_dir / f"{customer_id}.yaml").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _hermetic_keyless_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AIMO_DISABLE_GEMINI", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    orig_policy_dir = trust_policy.POLICY_DIR
    yield
    trust_policy.POLICY_DIR = orig_policy_dir
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    plan_mod._last_policy_dir = None


def test_eu14_invariants_and_derive_none_defaults() -> None:
    assert EU14 == EXPECTED_EU14
    assert len(EU14) == 14
    assert set(GS1_GROUPS.keys()) == set(EU14)

    for empty_input in ([], (), None, "AM", {"name": "allergen"}, 42):
        st, src, raw = derive(empty_input)
        assert st == {g: "unknown" for g in EU14}
        assert src == {g: "none" for g in EU14}
        assert raw == {}
        assert has_structured_data(src) is False

    undeclared_cls = _make_cls(
        allergen=[
            *(
                (GS1_GROUPS[g][0][0], "UNDECLARED")
                for g in EU14
            ),
            ("NC", "CONTAINS"),
            ("NM", "FREE_FROM"),
            ("X99", "MAY_CONTAIN"),
        ]
    )
    st_u, src_u, raw_u = derive(undeclared_cls)
    assert st_u == {g: "unknown" for g in EU14}
    assert src_u == {g: "none" for g in EU14}
    assert raw_u["NC"] == ["CONTAINS"]
    assert raw_u["NM"] == ["FREE_FROM"]
    assert raw_u["X99"] == ["MAY_CONTAIN"]
    assert has_structured_data(src_u) is False


@pytest.mark.parametrize("group", EU14)
def test_derive_tristate_and_precedence_across_all_eu14(group: str) -> None:
    heads, members = GS1_GROUPS[group]
    head = heads[0]

    for code in members:
        st_c, src_c, raw_c = derive(_make_cls(allergen=[(f" {code.lower()} ", " contains ")]))
        assert st_c[group] == "contains"
        assert src_c[group] == "structured"
        assert raw_c == {code: ["CONTAINS"]}
        assert has_structured_data(src_c) is True
        for other in EU14:
            if other != group:
                assert st_c[other] == "unknown"
                assert src_c[other] == "none"

        st_m, src_m, raw_m = derive(_make_cls(allergen=[(code, "MAY_CONTAIN")]))
        assert st_m[group] == "may_contain"
        assert src_m[group] == "structured"
        assert raw_m == {code: ["MAY_CONTAIN"]}
        assert has_structured_data(src_m) is True
        for other in EU14:
            if other != group:
                assert st_m[other] == "unknown"
                assert src_m[other] == "none"

    st_f, src_f, raw_f = derive(_make_cls(non_allergen=[(head, "FREE_FROM")]))
    assert st_f[group] == "free"
    assert src_f[group] == "structured"
    assert raw_f == {head: ["FREE_FROM"]}
    assert has_structured_data(src_f) is True
    for other in EU14:
        if other != group:
            assert st_f[other] == "unknown"
            assert src_f[other] == "none"

    claim_for_group = [cl for cl, g in CLAIM_FREE.items() if g == group]
    st_prec_c, src_prec_c, _ = derive(
        _make_cls(
            allergen=[(members[-1], "CONTAINS"), (head, "MAY_CONTAIN")],
            non_allergen=[(head, "FREE_FROM")],
            claims=claim_for_group,
        )
    )
    assert st_prec_c[group] == "contains"
    assert src_prec_c[group] == "structured"

    st_prec_m, src_prec_m, _ = derive(
        _make_cls(
            allergen=[(members[-1], "MAY_CONTAIN")],
            non_allergen=[(head, "FREE_FROM")],
            claims=claim_for_group,
        )
    )
    assert st_prec_m[group] == "may_contain"
    assert src_prec_m[group] == "structured"


@pytest.mark.parametrize(
    "claim,expected_group",
    [
        ("FREE_FROM_GLUTEN", "gluten"),
        ("FREE_FROM_EGGS", "eggs"),
        ("FREE_FROM_SOY", "soy"),
    ],
)
def test_derive_claim_free_sets_structured_claim_source(claim: str, expected_group: str) -> None:
    st, src, raw = derive(_make_cls(claims=[claim]))
    assert st[expected_group] == "free"
    assert src[expected_group] == "structured:claim"
    assert raw == {}
    assert has_structured_data(src) is True
    for other in EU14:
        if other != expected_group:
            assert st[other] == "unknown"
            assert src[other] == "none"


def test_derive_gs1_subcode_free_from_traps_ml_uw_sa_and_all_subcodes() -> None:
    st_ml, src_ml, raw_ml = derive(
        _make_cls(non_allergen=[("ML", "FREE_FROM")], claims=["FREE_FROM_LACTOSE"])
    )
    assert st_ml["milk"] == "unknown"
    assert src_ml["milk"] == "none"
    assert raw_ml == {"ML": ["FREE_FROM"]}
    assert has_structured_data(src_ml) is False
    assert blocking(st_ml, ["milk"], allergen_source=src_ml) == [("milk", "unknown")]

    st_am, src_am, _ = derive(_make_cls(non_allergen=[("AM", "FREE_FROM")]))
    assert st_am["milk"] == "free"
    assert src_am["milk"] == "structured"
    assert blocking(st_am, ["milk"], allergen_source=src_am) == []

    st_uw, src_uw, raw_uw = derive(_make_cls(non_allergen=[("UW", "FREE_FROM")]))
    assert st_uw["gluten"] == "unknown"
    assert src_uw["gluten"] == "none"
    assert raw_uw == {"UW": ["FREE_FROM"]}
    assert blocking(st_uw, ["gluten"], allergen_source=src_uw) == [("gluten", "unknown")]

    all_gluten_subcodes = ["UW", "GB", "GO", "NR", "GS", "GK"]
    st_all_g, src_all_g, _ = derive(_make_cls(non_allergen=all_gluten_subcodes))
    assert st_all_g["gluten"] == "unknown"
    assert src_all_g["gluten"] == "none"
    assert blocking(st_all_g, ["gluten"], allergen_source=src_all_g) == [("gluten", "unknown")]

    st_aw, src_aw, _ = derive(_make_cls(non_allergen=[("AW", "FREE_FROM")]))
    assert st_aw["gluten"] == "free"
    assert src_aw["gluten"] == "structured"
    assert blocking(st_aw, ["gluten"], allergen_source=src_aw) == []

    st_sa, src_sa, raw_sa = derive(_make_cls(non_allergen=[("SA", "FREE_FROM")]))
    assert st_sa["nuts"] == "unknown"
    assert src_sa["nuts"] == "none"
    assert raw_sa == {"SA": ["FREE_FROM"]}
    assert blocking(st_sa, ["nuts"], allergen_source=src_sa) == [("nuts", "unknown")]

    all_nut_subcodes = ["SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ"]
    st_all_n, src_all_n, _ = derive(_make_cls(non_allergen=all_nut_subcodes))
    assert st_all_n["nuts"] == "unknown"
    assert src_all_n["nuts"] == "none"
    assert blocking(st_all_n, ["nuts"], allergen_source=src_all_n) == [("nuts", "unknown")]

    st_an, src_an, _ = derive(_make_cls(non_allergen=[("AN", "FREE_FROM")]))
    assert st_an["nuts"] == "free"
    assert src_an["nuts"] == "structured"
    assert blocking(st_an, ["nuts"], allergen_source=src_an) == []

    assert len(ALL_SUBCODES) == 16
    for group, subcode in ALL_SUBCODES:
        assert CODE_TO_GROUP[subcode] == group
        st_sub, src_sub, _ = derive(_make_cls(non_allergen=[(subcode, "FREE_FROM")]))
        assert st_sub[group] == "unknown"
        assert src_sub[group] == "none"
        assert blocking(st_sub, [group], allergen_source=src_sub) == [(group, "unknown")]


@pytest.mark.parametrize("group", EU14)
def test_blocking_tristate_and_source_downgrades_across_all_eu14(group: str) -> None:
    head_code = GS1_GROUPS[group][0][0]

    assert blocking({group: "contains"}, [group], allergen_source={group: "structured"}) == [
        (group, "contains")
    ]
    assert blocking({group: AllergenStatus.contains}, [head_code], allergen_source={group: "structured"}) == [
        (group, "contains")
    ]
    assert blocking({group: "may_contain"}, [group], allergen_source={group: "structured"}) == [
        (group, "may_contain")
    ]
    assert blocking({group: AllergenStatus.may_contain}, [group], allergen_source={group: "structured"}) == [
        (group, "may_contain")
    ]
    assert blocking({group: "unknown"}, [group], allergen_source={group: "none"}) == [
        (group, "unknown")
    ]
    assert blocking({}, [group], allergen_source={}) == [(group, "unknown")]
    assert blocking({group: ""}, [group], allergen_source={group: "structured"}) == [(group, "unknown")]

    for untrusted_source in (
        "inferred:gemini",
        "inferred",
        "  INFERRED:GEMINI  ",
        "contradiction",
        " CONTRADICTION ",
        "none",
        "NONE",
    ):
        assert blocking({group: "free"}, [group], allergen_source={group: untrusted_source}) == [
            (group, "unknown")
        ]
        assert blocking(
            {group: AllergenStatus.free},
            [group],
            allergen_source={group: untrusted_source},
        ) == [(group, "unknown")]

    assert blocking({group: "unknown"}, [group], allergen_source={group: "contradiction"}) == [
        (group, "unknown")
    ]

    assert blocking({group: "free"}, [group], allergen_source={group: "structured"}) == []
    assert blocking({group: "free"}, [group], allergen_source={group: "structured:claim"}) == []
    assert blocking({group: AllergenStatus.free}, [head_code], allergen_source={group: "structured"}) == []


def test_blocking_all_14_groups_simultaneously() -> None:
    mixed_status: dict[str, str] = {}
    mixed_source: dict[str, str] = {}
    expected: list[tuple[str, str]] = []
    modes = (
        ("contains", "structured", "contains"),
        ("may_contain", "structured", "may_contain"),
        ("unknown", "none", "unknown"),
        ("free", "inferred:gemini", "unknown"),
        ("free", "contradiction", "unknown"),
        ("free", "none", "unknown"),
        ("free", "structured", None),
    )
    for idx, group in enumerate(EU14):
        st, src, exp = modes[idx % len(modes)]
        mixed_status[group] = st
        mixed_source[group] = src
        if exp is not None:
            expected.append((group, exp))

    assert blocking(mixed_status, EU14, allergen_source=mixed_source) == expected


def test_effective_excluded_allergens_bundled_and_all_eu14_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)

    assert effective_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert effective_excluded_allergens("school-tampere", ["soy", "dairy", "SA"]) == [
        "soy",
        "milk",
        "nuts",
    ]
    assert effective_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert effective_excluded_allergens("hospital-meilahti", ["shellfish", "UW"]) == [
        "crustaceans",
        "molluscs",
        "gluten",
    ]
    assert effective_excluded_allergens("hotel-aurora") == []
    assert effective_excluded_allergens("hotel-aurora", ["sesame", "lupin"]) == ["sesame", "lupin"]
    assert effective_excluded_allergens("", ["peanuts"]) == ["peanuts"]
    assert effective_excluded_allergens(None, ["mustard"]) == ["mustard"]

    monkeypatch.setattr(trust_policy, "POLICY_DIR", tmp_path)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)

    for idx, group in enumerate(EU14):
        cid = f"cust-{group}"
        head_code = GS1_GROUPS[group][0][0]
        _write_policy(tmp_path, cid, [head_code if idx % 2 == 0 else group])
        assert effective_excluded_allergens(cid) == [group]
        neighbor = EU14[(idx + 1) % len(EU14)]
        assert effective_excluded_allergens(cid, [neighbor, group]) == [neighbor, group]

    _write_policy(tmp_path, "cust-all-eu14", list(EU14))
    assert effective_excluded_allergens("cust-all-eu14") == list(EU14)


@pytest.mark.parametrize("group", EU14)
def test_plan_need_enforces_hardblock_across_all_eu14_states_and_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
) -> None:
    cid = f"policy-{group}"
    _write_policy(tmp_path, cid, [group], reason=f"strict {group} allergy")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", tmp_path)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)
    monkeypatch.setattr(plan_mod, "_last_policy_dir", None)

    a_contains, s_contains = _all_free_except(**{group: ("contains", "structured")})
    a_may, s_may = _all_free_except(**{group: ("may_contain", "structured")})
    a_unknown, s_unknown = _all_free_except(**{group: ("unknown", "none")})
    a_gemini, s_gemini = _all_free_except(**{group: ("free", "inferred:gemini")})
    a_contra, s_contra = _all_free_except(**{group: ("free", "contradiction")})
    a_none_free, s_none_free = _all_free_except(**{group: ("free", "none")})
    a_safe, s_safe = _all_free_except()

    pid_contains = f"{group}-contains"
    pid_may = f"{group}-may"
    pid_unknown = f"{group}-unknown"
    pid_gemini = f"{group}-gemini"
    pid_contra = f"{group}-contra"
    pid_none = f"{group}-none"
    pid_safe = f"{group}-safe"

    rows = {
        pid_contains: _synth_row(
            pid_contains,
            "Vegetable broth classic 1 L",
            allergens=a_contains,
            allergen_source=s_contains,
        ),
        pid_may: _synth_row(
            pid_may,
            "Vegetable broth kitchen 1 L",
            allergens=a_may,
            allergen_source=s_may,
        ),
        pid_unknown: _synth_row(
            pid_unknown,
            "Vegetable broth mild 1 L",
            allergens=a_unknown,
            allergen_source=s_unknown,
        ),
        pid_gemini: _synth_row(
            pid_gemini,
            "Vegetable broth select 1 L",
            allergens=a_gemini,
            allergen_source=s_gemini,
        ),
        pid_contra: _synth_row(
            pid_contra,
            "Vegetable broth clear 1 L",
            allergens=a_contra,
            allergen_source=s_contra,
            allergen_conflicts={group: {"evidence": CONTRADICTION_INGREDIENTS[group], "by": "lexicon"}},
        ),
        pid_none: _synth_row(
            pid_none,
            "Vegetable broth light 1 L",
            allergens=a_none_free,
            allergen_source=s_none_free,
        ),
        pid_safe: _synth_row(
            pid_safe,
            "Vegetable broth verified 1 L",
            allergens=a_safe,
            allergen_source=s_safe,
        ),
    }
    _install_in_memory_catalog(monkeypatch, rows, hints={pid_gemini: {group: "free"}})

    prices = {
        pid_contains: 0.90,
        pid_may: 0.95,
        pid_unknown: 1.00,
        pid_gemini: 1.05,
        pid_contra: 1.10,
        pid_none: 1.15,
        pid_safe: 2.50,
    }
    offers_fn = _make_offers_fn(prices)

    need = Need(
        customer_id=cid,
        product_query="vegetable broth",
        excluded_allergens=[],
        preferred_product_ids=[pid_contains, pid_gemini, pid_contra],
        quantity=25.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=offers_fn, limit=20)

    assert plan.need.excluded_allergens == [group]
    assert f"Customer policy ({cid}) excludes {group}" in plan.explanation
    assert [ln.product_id for ln in plan.lines] == [pid_safe]
    assert plan.covered_amount == 25.0
    assert plan.shortfall == 0.0
    assert "allergen_unknown" not in plan.lines[0].flags

    cands = {c.product.id: c for c in plan.candidates}
    assert cands[pid_safe].tier != "rejected"

    assert cands[pid_contains].tier == "rejected"
    assert f"contains:{group}" in cands[pid_contains].flags
    assert any(f"{group}: contains per supplier data" in r for r in cands[pid_contains].reasons)

    assert cands[pid_may].tier == "rejected"
    assert f"may_contain:{group}" in cands[pid_may].flags
    assert any(f"{group}: may contain per supplier data" in r for r in cands[pid_may].reasons)

    assert cands[pid_unknown].tier == "rejected"
    assert "allergen_unknown" in cands[pid_unknown].flags
    assert any("unknown is never treated as safe" in r for r in cands[pid_unknown].reasons)

    assert cands[pid_gemini].tier == "rejected"
    assert "allergen_unknown" in cands[pid_gemini].flags
    assert "allergen_inferred_hint" in cands[pid_gemini].flags
    assert any("inferred:gemini, needs human confirmation" in r for r in cands[pid_gemini].reasons)

    assert cands[pid_contra].tier == "rejected"
    assert "allergen_data_conflict" in cands[pid_contra].flags
    assert "allergen_unknown" in cands[pid_contra].flags
    assert any("data conflict; treated as unknown" in r for r in cands[pid_contra].reasons)

    assert cands[pid_none].tier == "rejected"
    assert "allergen_unknown" in cands[pid_none].flags

    unsafe_only_rows = {k: v for k, v in rows.items() if k != pid_safe}
    _install_in_memory_catalog(monkeypatch, unsafe_only_rows, hints={pid_gemini: {group: "free"}})
    late_date = DELIVERY + timedelta(days=2)
    late_overrides = {
        pid_gemini: _offer_for(
            pid_gemini,
            DELIVERY,
            price=1.05,
            available=False,
            cutoff_passed=True,
            available_date=late_date,
        )
    }
    no_safe_need = need.model_copy(update={"preferences": {"allow_split_delivery": True}})
    empty_plan = plan_need(
        no_safe_need,
        offers_fn=_make_offers_fn(prices, overrides=late_overrides),
        limit=20,
    )
    assert empty_plan.lines == []
    assert empty_plan.covered_amount == 0.0
    assert empty_plan.shortfall == 25.0


@pytest.mark.parametrize("group", EU14)
def test_data_quality_contradiction_sweep_into_plan_need_across_all_eu14(
    monkeypatch: pytest.MonkeyPatch,
    group: str,
) -> None:
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)
    monkeypatch.setattr(plan_mod, "_last_policy_dir", None)

    all_heads_free = [(GS1_GROUPS[g][0][0], "FREE_FROM") for g in EU14]
    st_init, src_init, _ = derive(_make_cls(non_allergen=all_heads_free))
    assert st_init[group] == "free"
    assert src_init[group] == "structured"

    conflict_row = _synth_row(
        f"{group}-dq-conflict",
        "Vegetable broth pantry 1 L",
        allergens=st_init,
        allergen_source=src_init,
        ingredients_text=CONTRADICTION_INGREDIENTS[group],
    )
    safe_row = _synth_row(
        f"{group}-dq-safe",
        "Vegetable broth pure 1 L",
        allergens=st_init,
        allergen_source=src_init,
        ingredients_text="water, carrot, parsnip, rapeseed oil, salt",
    )

    rows = {conflict_row["id"]: conflict_row, safe_row["id"]: safe_row}
    _install_in_memory_catalog(monkeypatch, rows)
    data_quality.apply(conflict_row)
    data_quality.apply(safe_row)

    assert conflict_row["allergens"][group] == "unknown"
    assert conflict_row["allergen_source"][group] == "contradiction"
    assert group in conflict_row["allergen_conflicts"]
    assert safe_row["allergens"][group] == "free"
    assert safe_row["allergen_source"][group] == "structured"
    assert safe_row["allergen_conflicts"] == {}

    need = Need(
        customer_id="hotel-aurora",
        product_query="vegetable broth",
        excluded_allergens=[group],
        preferred_product_ids=[conflict_row["id"]],
        quantity=12.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(
        need,
        offers_fn=_make_offers_fn({conflict_row["id"]: 1.00, safe_row["id"]: 2.20}),
        limit=20,
    )
    assert [ln.product_id for ln in plan.lines] == [safe_row["id"]]
    assert plan.covered_amount == 12.0
    assert plan.shortfall == 0.0
    by_id = {c.product.id: c for c in plan.candidates}
    assert by_id[conflict_row["id"]].tier == "rejected"
    assert "allergen_data_conflict" in by_id[conflict_row["id"]].flags
    assert "allergen_unknown" in by_id[conflict_row["id"]].flags


def test_plan_need_gs1_subcode_free_from_traps_ml_uw_sa_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)
    monkeypatch.setattr(plan_mod, "_last_policy_dir", None)

    st_ml_trap, src_ml_trap, _ = derive(
        _make_cls(
            non_allergen=[("ML", "FREE_FROM"), ("AN", "FREE_FROM")],
            claims=["FREE_FROM_LACTOSE"],
        )
    )
    st_sa_trap, src_sa_trap, _ = derive(
        _make_cls(
            non_allergen=[
                ("AM", "FREE_FROM"),
                *[
                    (code, "FREE_FROM")
                    for code in ("SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ")
                ],
            ]
        )
    )
    st_school_safe, src_school_safe, _ = derive(
        _make_cls(non_allergen=[("AM", "FREE_FROM"), ("AN", "FREE_FROM")])
    )

    school_rows = {
        "trap-ml": _synth_row(
            "trap-ml",
            "Lactose free oat drink 1 L",
            allergens=st_ml_trap,
            allergen_source=src_ml_trap,
            claims=("FREE_FROM_LACTOSE",),
        ),
        "trap-sa": _synth_row(
            "trap-sa",
            "Almond free oat drink 1 L",
            allergens=st_sa_trap,
            allergen_source=src_sa_trap,
        ),
        "470889": _synth_row(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            allergens=st_school_safe,
            allergen_source=src_school_safe,
            brand="Oatly",
        ),
    }
    _install_in_memory_catalog(monkeypatch, school_rows)

    school_need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        preferred_product_ids=["trap-ml", "trap-sa"],
        quantity=30.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_plan = plan_need(
        school_need,
        offers_fn=_make_offers_fn({"trap-ml": 1.0, "trap-sa": 1.1, "470889": 2.0}),
        limit=20,
    )
    assert set(school_plan.need.excluded_allergens) == {"milk", "nuts"}
    assert [ln.product_id for ln in school_plan.lines] == ["470889"]
    assert school_plan.covered_amount == 30.0
    assert school_plan.shortfall == 0.0
    school_cands = {c.product.id: c for c in school_plan.candidates}
    assert school_cands["trap-ml"].tier == "rejected"
    assert "allergen_unknown" in school_cands["trap-ml"].flags
    assert school_cands["trap-sa"].tier == "rejected"
    assert "allergen_unknown" in school_cands["trap-sa"].flags

    school_literal_milk_need = Need(
        customer_id="school-tampere",
        product_query="lactose-free milk",
        excluded_allergens=[],
        preferred_product_ids=["trap-ml"],
        quantity=20.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_swap_plan = plan_need(
        school_literal_milk_need,
        offers_fn=_make_offers_fn({"trap-ml": 1.0, "trap-sa": 1.1, "470889": 2.0}),
        limit=20,
    )
    assert [ln.product_id for ln in school_swap_plan.lines] == ["470889"]
    assert "allergen_safe_alternative" in school_swap_plan.lines[0].flags

    st_uw_trap, src_uw_trap, _ = derive(
        _make_cls(
            non_allergen=[
                (code, "FREE_FROM")
                for code in ("UW", "GB", "GO", "NR", "GS", "GK")
            ]
        )
    )
    st_aw_safe, src_aw_safe, _ = derive(_make_cls(non_allergen=[("AW", "FREE_FROM")]))
    st_claim_safe, src_claim_safe, _ = derive(_make_cls(claims=["FREE_FROM_GLUTEN"]))

    hosp_rows = {
        "trap-uw": _synth_row(
            "trap-uw",
            "Wheat free dark bread 1 KG",
            unit="KG",
            allergens=st_uw_trap,
            allergen_source=src_uw_trap,
        ),
        "safe-aw": _synth_row(
            "safe-aw",
            "Verified dark bread 1 KG",
            unit="KG",
            allergens=st_aw_safe,
            allergen_source=src_aw_safe,
        ),
        "safe-claim": _synth_row(
            "safe-claim",
            "Gluten free dark bread 1 KG",
            unit="KG",
            allergens=st_claim_safe,
            allergen_source=src_claim_safe,
            claims=("FREE_FROM_GLUTEN",),
        ),
    }
    _install_in_memory_catalog(monkeypatch, hosp_rows)

    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="dark bread",
        excluded_allergens=[],
        preferred_product_ids=["trap-uw"],
        quantity=15.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_plan = plan_need(
        hosp_need,
        offers_fn=_make_offers_fn(
            {"trap-uw": 1.2, "safe-aw": 2.4, "safe-claim": 2.8},
            unit="KG",
        ),
        limit=20,
    )
    assert hosp_plan.need.excluded_allergens == ["gluten"]
    assert all(ln.product_id in ("safe-aw", "safe-claim") for ln in hosp_plan.lines)
    assert all(ln.product_id != "trap-uw" for ln in hosp_plan.lines)
    assert hosp_plan.covered_amount == 15.0
    assert hosp_plan.shortfall == 0.0
    hosp_cands = {c.product.id: c for c in hosp_plan.candidates}
    assert hosp_cands["trap-uw"].tier == "rejected"
    assert "allergen_unknown" in hosp_cands["trap-uw"].flags


def test_plan_need_simultaneous_all_14_eu14_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_half = EU14[:7]
    caller_half = EU14[7:]
    _write_policy(tmp_path, "school-eu14", policy_half, reason="multi-allergen kitchen")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", tmp_path)
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    monkeypatch.setattr(allergens_mod, "_last_policy_dir", None)
    monkeypatch.setattr(plan_mod, "_last_policy_dir", None)

    rows: dict[str, dict[str, Any]] = {}
    prices: dict[str, float] = {}
    failure_modes = (
        ("contains", "structured"),
        ("may_contain", "structured"),
        ("unknown", "none"),
        ("free", "inferred:gemini"),
        ("free", "contradiction"),
        ("free", "none"),
    )
    for idx, group in enumerate(EU14):
        st_bad, src_bad = failure_modes[idx % len(failure_modes)]
        a_bad, s_bad = _all_free_except(**{group: (st_bad, src_bad)})
        pid = f"fail-{idx:02d}-{group}"
        rows[pid] = _synth_row(
            pid,
            f"Vegetable broth batch {idx} 1 L",
            allergens=a_bad,
            allergen_source=s_bad,
        )
        prices[pid] = 1.0 + 0.02 * idx

    st_all_free, src_all_free, _ = derive(
        _make_cls(non_allergen=[(GS1_GROUPS[g][0][0], "FREE_FROM") for g in EU14])
    )
    rows["all-14-safe"] = _synth_row(
        "all-14-safe",
        "Vegetable broth pure verified 1 L",
        allergens=st_all_free,
        allergen_source=src_all_free,
    )
    prices["all-14-safe"] = 3.50

    _install_in_memory_catalog(monkeypatch, rows)

    need = Need(
        customer_id="school-eu14",
        product_query="vegetable broth",
        excluded_allergens=caller_half,
        quantity=40.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    plan = plan_need(need, offers_fn=_make_offers_fn(prices), limit=30)

    assert set(plan.need.excluded_allergens) == set(EU14)
    assert [ln.product_id for ln in plan.lines] == ["all-14-safe"]
    assert plan.covered_amount == 40.0
    assert plan.shortfall == 0.0
    for cand in plan.candidates:
        if cand.product.id == "all-14-safe":
            assert cand.tier != "rejected"
        else:
            assert cand.tier == "rejected"


def test_file_hygiene_zero_comments_and_zero_em_or_en_dashes() -> None:
    self_path = Path(__file__)
    src = self_path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    comment_tokens = [tok for tok in tokens if tok.type == tokenize.COMMENT]
    assert comment_tokens == []
    assert chr(0x23) not in src
    assert chr(0x2014) not in src
    assert chr(0x2013) not in src
