from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aimo import config
from aimo.catalog.allergens import (
    CLAIM_FREE,
    CODE_TO_GROUP,
    GS1_CODE_NAMES,
    GS1_GROUPS,
    blocking,
    derive,
    has_structured_data,
)
from aimo.models import EU14, AllergenStatus, Need, Product, Quote, QuoteLine
from aimo.trust import policy as policy_mod
from aimo.trust import signing
from aimo.trust.audit import AuditLog
from aimo.trust.policy import Ctx, evaluate, get_mandate, load_policy, r_allergen_block

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
DELIVERY = date(2026, 11, 16)

EXPECTED_GS1_GROUPS = {
    "gluten": (("AW",), ("AW", "UW", "GB", "GO", "NR", "GS", "GK")),
    "crustaceans": (("AC",), ("AC",)),
    "eggs": (("AE",), ("AE",)),
    "fish": (("AF",), ("AF",)),
    "peanuts": (("AP",), ("AP",)),
    "soy": (("AY",), ("AY",)),
    "milk": (("AM",), ("AM", "ML")),
    "nuts": (("AN",), ("AN", "SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ")),
    "celery": (("BC",), ("BC",)),
    "mustard": (("BM",), ("BM",)),
    "sesame": (("AS",), ("AS",)),
    "sulphites": (("AU",), ("AU",)),
    "lupin": (("NL",), ("NL",)),
    "molluscs": (("UM",), ("UM",)),
}

SUBCODES_ONLY = [
    (group, code)
    for group, (heads, members) in EXPECTED_GS1_GROUPS.items()
    for code in members
    if code not in heads
]

ALL_MEMBER_CODES = [
    (group, code)
    for group, (_heads, members) in EXPECTED_GS1_GROUPS.items()
    for code in members
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(policy_mod, "_catalog_lookup", lambda ids: {i: None for i in ids})
    yield


def make_cls(allergen=(), non_allergen=(), claims=()):
    out = []
    if allergen:
        out.append({"name": "allergen", "type": "ENUM", "values": [{"id": c, "unit": u} for c, u in allergen]})
    if non_allergen:
        vals = [{"id": c, "unit": u} if isinstance(c, str) else c for c, u in (
            (x if isinstance(x, tuple) else (x, "FREE_FROM")) for x in non_allergen
        )]
        out.append({"name": "nonAllergen", "type": "ENUM", "values": vals})
    if claims:
        vals = [{"id": c, "unit": "FREE_FROM"} if isinstance(c, str) else {"id": c[0], "unit": c[1]} for c in claims]
        out.append({"name": "nutritionalClaim", "type": "ENUM", "values": vals})
    return out


def make_product(
    pid: str,
    name: str = "Test Product 1 L",
    category: str = "10400",
    allergens: dict | None = None,
    source: dict | None = None,
    claims: tuple[str, ...] = (),
    content: float = 1.0,
    unit: str = "L",
    brand: str = "Valio",
) -> Product:
    return Product(
        id=pid,
        name=name,
        sales_unit="ST",
        category=category,
        content_amount=content,
        content_unit=unit,
        claims=list(claims),
        allergens=allergens or {},
        allergen_source=source or {},
        brand=brand,
    )


def make_quote(
    customer: str,
    lines: list[QuoteLine],
    excluded: tuple[str, ...] = (),
    claims: tuple[str, ...] = (),
    unit: str = "L",
    product_query: str = "test item",
    with_need: bool = True,
) -> Quote:
    total_qty = sum(ln.qty_sales_units for ln in lines)
    total_net = round(sum(ln.line_total for ln in lines), 2)
    need = (
        Need(
            customer_id=customer,
            product_query=product_query,
            required_claims=list(claims),
            excluded_allergens=list(excluded),
            quantity=total_qty,
            unit=unit,
            delivery_by=DELIVERY,
        )
        if with_need
        else None
    )
    q = Quote(
        quote_id=f"Q-{customer}",
        customer_id=customer,
        agent_id=f"{customer}/buyer-1",
        delivery_date=DELIVERY,
        lines=lines,
        total_net=total_net,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        need=need,
    )
    return signing.sign_quote(q)


def make_line(
    pid: str,
    name: str = "Test Product 1 L",
    qty: float = 10.0,
    price: float = 2.0,
    tier: str = "preferred",
    flags: tuple[str, ...] = (),
) -> QuoteLine:
    return QuoteLine(
        product_id=pid,
        product_name=name,
        qty_sales_units=qty,
        sales_unit="ST",
        unit_price=price,
        line_total=round(qty * price, 2),
        tier=tier,
        flags=list(flags),
    )


def allergen_rule_of(policy: dict) -> dict:
    return next(r for r in policy["rules"] if r["type"] == "allergen_block")


def allergen_hits(decision):
    return [h for h in decision.hits if h.rule_id == "allergens" or h.rule_id.startswith("allergens:")]


def test_gs1_groups_and_code_to_group_invariants():
    assert len(EU14) == 14
    assert set(GS1_GROUPS.keys()) == set(EU14)
    assert GS1_GROUPS == EXPECTED_GS1_GROUPS

    seen_codes: set[str] = set()
    for group, (heads, members) in GS1_GROUPS.items():
        assert len(heads) >= 1
        assert set(heads) <= set(members)
        assert not (seen_codes & set(members)), f"duplicate member code in {group}"
        seen_codes.update(members)

    assert len(seen_codes) == 30
    assert set(CODE_TO_GROUP.keys()) == seen_codes
    for group, (_heads, members) in GS1_GROUPS.items():
        for code in members:
            assert CODE_TO_GROUP[code] == group
            assert code in GS1_CODE_NAMES and GS1_CODE_NAMES[code]

    assert CLAIM_FREE == {
        "FREE_FROM_GLUTEN": "gluten",
        "FREE_FROM_EGGS": "eggs",
        "FREE_FROM_SOY": "soy",
    }
    assert "FREE_FROM_LACTOSE" not in CLAIM_FREE


@pytest.mark.parametrize("group", EU14)
def test_eu14_head_code_free_from_sets_group_free(group):
    heads, _members = GS1_GROUPS[group]
    head = heads[0]

    st, src, raw = derive(make_cls(non_allergen=[head]))
    assert set(st.keys()) == set(EU14)
    assert set(src.keys()) == set(EU14)
    assert st[group] == "free"
    assert src[group] == "structured"
    assert raw == {head: ["FREE_FROM"]}
    assert has_structured_data(src) is True
    for other in EU14:
        if other != group:
            assert st[other] == "unknown"
            assert src[other] == "none"

    st_lower, src_lower, raw_lower = derive(make_cls(allergen=[(head, "free_from")]))
    assert st_lower[group] == "free"
    assert src_lower[group] == "structured"
    assert raw_lower == {head: ["FREE_FROM"]}


@pytest.mark.parametrize("group,subcode", SUBCODES_ONLY)
def test_member_subcode_free_from_alone_is_unknown_never_free(group, subcode):
    st, src, raw = derive(make_cls(non_allergen=[subcode]))
    assert st[group] == "unknown"
    assert src[group] == "none"
    assert raw == {subcode: ["FREE_FROM"]}
    assert has_structured_data(src) is False


def test_all_subcodes_free_from_without_head_still_unknown():
    gluten_subs = ["UW", "GB", "GO", "NR", "GS", "GK"]
    st_g, src_g, _ = derive(make_cls(non_allergen=gluten_subs))
    assert st_g["gluten"] == "unknown" and src_g["gluten"] == "none"

    nut_subs = ["SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ"]
    st_n, src_n, _ = derive(make_cls(non_allergen=nut_subs))
    assert st_n["nuts"] == "unknown" and src_n["nuts"] == "none"

    st_m, src_m, _ = derive(make_cls(non_allergen=["ML"], claims=["FREE_FROM_LACTOSE"]))
    assert st_m["milk"] == "unknown" and src_m["milk"] == "none"


@pytest.mark.parametrize("group,code", ALL_MEMBER_CODES)
def test_every_member_code_contains_maps_to_group_contains(group, code):
    st, src, raw = derive(make_cls(allergen=[(code, "CONTAINS")]))
    assert st[group] == "contains"
    assert src[group] == "structured"
    assert raw == {code: ["CONTAINS"]}
    assert has_structured_data(src) is True
    for other in EU14:
        if other != group:
            assert st[other] == "unknown" and src[other] == "none"


def test_explicit_gluten_cereal_and_nut_subcodes_contains():
    for cereal_code in ("AW", "UW", "GO", "NR", "GB", "GS", "GK"):
        st, src, _ = derive(make_cls(allergen=[(cereal_code, "CONTAINS")]))
        assert st["gluten"] == "contains" and src["gluten"] == "structured"

    for nut_code in ("AN", "SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ"):
        st, src, _ = derive(make_cls(allergen=[(nut_code, "CONTAINS")]))
        assert st["nuts"] == "contains" and src["nuts"] == "structured"


@pytest.mark.parametrize("group,code", ALL_MEMBER_CODES)
def test_every_member_code_may_contain_maps_to_group_may_contain(group, code):
    st, src, raw = derive(make_cls(allergen=[(code, "MAY_CONTAIN")]))
    assert st[group] == "may_contain"
    assert src[group] == "structured"
    assert raw == {code: ["MAY_CONTAIN"]}
    assert has_structured_data(src) is True


@pytest.mark.parametrize("group", EU14)
def test_precedence_contains_beats_may_contain_beats_free_from_for_every_eu14(group):
    heads, members = GS1_GROUPS[group]
    head = heads[0]
    sub = members[-1]

    st_may, src_may, _ = derive(make_cls(allergen=[(sub, "MAY_CONTAIN")], non_allergen=[head]))
    assert st_may[group] == "may_contain" and src_may[group] == "structured"

    st_may_rev, _, _ = derive([
        {"name": "nonAllergen", "values": [{"id": head, "unit": "FREE_FROM"}]},
        {"name": "allergen", "values": [{"id": sub, "unit": "MAY_CONTAIN"}]},
    ])
    assert st_may_rev[group] == "may_contain"

    st_con, src_con, _ = derive(
        make_cls(allergen=[(head, "MAY_CONTAIN"), (sub, "CONTAINS")], non_allergen=[head])
    )
    assert st_con[group] == "contains" and src_con[group] == "structured"

    st_con_rev, _, _ = derive([
        {"name": "allergen", "values": [{"id": sub, "unit": "CONTAINS"}]},
        {"name": "allergen", "values": [{"id": head, "unit": "MAY_CONTAIN"}]},
        {"name": "nonAllergen", "values": [{"id": head, "unit": "FREE_FROM"}]},
    ])
    assert st_con_rev[group] == "contains"


def test_precedence_across_distinct_subcodes_in_gluten_milk_and_nuts():
    st_g, _, _ = derive(make_cls(allergen=[("UW", "MAY_CONTAIN"), ("GO", "CONTAINS")], non_allergen=["AW"]))
    assert st_g["gluten"] == "contains"

    st_g2, _, _ = derive(make_cls(allergen=[("NR", "MAY_CONTAIN")], non_allergen=["AW", "UW"]))
    assert st_g2["gluten"] == "may_contain"

    st_n, _, _ = derive(make_cls(allergen=[("SH", "MAY_CONTAIN"), ("SQ", "CONTAINS")], non_allergen=["AN"]))
    assert st_n["nuts"] == "contains"

    st_n2, _, _ = derive(make_cls(allergen=[("SP", "MAY_CONTAIN")], non_allergen=["AN", "SA"]))
    assert st_n2["nuts"] == "may_contain"

    st_m, _, _ = derive(make_cls(allergen=[("ML", "CONTAINS")], non_allergen=["AM"]))
    assert st_m["milk"] == "contains"

    st_m2, _, _ = derive(make_cls(allergen=[("ML", "MAY_CONTAIN")], non_allergen=["AM"]))
    assert st_m2["milk"] == "may_contain"


@pytest.mark.parametrize("group,code", ALL_MEMBER_CODES)
def test_undeclared_yields_unknown_with_none_source_and_preserved_in_raw(group, code):
    st, src, raw = derive(make_cls(allergen=[(code, "UNDECLARED")]))
    assert st[group] == "unknown"
    assert src[group] == "none"
    assert raw == {code: ["UNDECLARED"]}
    assert has_structured_data(src) is False


@pytest.mark.parametrize("unmapped", ["NC", "NM", "NE", "BF", "X99"])
@pytest.mark.parametrize("unit", ["CONTAINS", "MAY_CONTAIN", "FREE_FROM", "UNDECLARED"])
def test_unmapped_synkka_codes_stay_unknown_and_preserved_in_raw(unmapped, unit):
    st, src, raw = derive(make_cls(allergen=[(unmapped, unit)]))
    for g in EU14:
        assert st[g] == "unknown"
        assert src[g] == "none"
    assert raw == {unmapped: [unit]}
    assert has_structured_data(src) is False


def test_multiple_unmapped_codes_and_missing_units_preserved_in_raw():
    cls = [
        {
            "name": "allergen",
            "values": [
                {"id": "NC", "unit": "CONTAINS"},
                {"id": "NC", "unit": "MAY_CONTAIN"},
                {"id": "NM", "unit": "FREE_FROM"},
                {"id": "NE", "unit": "UNDECLARED"},
                {"id": "BF", "unit": "CONTAINS"},
                {"id": "X99", "unit": ""},
                {"id": "AW", "unit": None},
                {"id": None, "unit": "CONTAINS"},
            ],
        },
        {"name": "allergen", "values": None},
        {"name": "packagingMarkedLabel", "values": [{"id": "HEART_SYMBOL", "unit": ""}]},
    ]
    st, src, raw = derive(cls)
    assert all(st[g] == "unknown" and src[g] == "none" for g in EU14)
    assert raw == {
        "NC": ["CONTAINS", "MAY_CONTAIN"],
        "NM": ["FREE_FROM"],
        "NE": ["UNDECLARED"],
        "BF": ["CONTAINS"],
        "X99": ["?"],
        "AW": ["?"],
    }

    st_empty, src_empty, raw_empty = derive(None)
    assert all(st_empty[g] == "unknown" and src_empty[g] == "none" for g in EU14)
    assert raw_empty == {}


@pytest.mark.parametrize(
    "claim_id,expected_group",
    [
        ("FREE_FROM_GLUTEN", "gluten"),
        ("FREE_FROM_EGGS", "eggs"),
        ("FREE_FROM_SOY", "soy"),
    ],
)
def test_claim_free_sets_structured_claim_and_loses_to_contains_or_may_contain(claim_id, expected_group):
    st, src, raw = derive(make_cls(claims=[claim_id]))
    assert st[expected_group] == "free"
    assert src[expected_group] == "structured:claim"
    assert raw == {}
    assert has_structured_data(src) is True
    for other in EU14:
        if other != expected_group:
            assert st[other] == "unknown" and src[other] == "none"

    heads, members = GS1_GROUPS[expected_group]
    for member in members:
        st_con, src_con, _ = derive(make_cls(allergen=[(member, "CONTAINS")], claims=[claim_id]))
        assert st_con[expected_group] == "contains"
        assert src_con[expected_group] == "structured"

        st_may, src_may, _ = derive(make_cls(allergen=[(member, "MAY_CONTAIN")], claims=[claim_id]))
        assert st_may[expected_group] == "may_contain"
        assert src_may[expected_group] == "structured"

    st_both, src_both, _ = derive(make_cls(non_allergen=[heads[0]], claims=[claim_id]))
    assert st_both[expected_group] == "free"
    assert src_both[expected_group] == "structured"


def test_blocking_and_has_structured_data_helpers():
    assert has_structured_data({}) is False
    assert has_structured_data({g: "none" for g in EU14}) is False
    assert has_structured_data({"milk": "inferred:gemini", "nuts": "none"}) is False
    assert has_structured_data({"milk": "structured", "nuts": "none"}) is True
    assert has_structured_data({"gluten": "structured:claim"}) is True

    allergens = {
        "milk": "free",
        "eggs": AllergenStatus.free,
        "nuts": "may_contain",
        "gluten": AllergenStatus.contains,
        "soy": AllergenStatus.unknown,
    }
    assert blocking(allergens, ["milk", "eggs"]) == []
    assert blocking(allergens, ["milk", "nuts", "gluten", "soy", "sesame"]) == [
        ("nuts", "may_contain"),
        ("gluten", "contains"),
        ("soy", "unknown"),
        ("sesame", "unknown"),
    ]
    assert blocking({}, []) == []


def test_r_allergen_block_school_tampere_hard_rejects_contains_may_contain_and_unknown(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    mandate = get_mandate("school-tampere", "buyer-1")
    pol = load_policy("school-tampere")
    rule = allergen_rule_of(pol)
    assert rule["excluded"] == ["milk", "nuts"]
    assert rule["on_unknown"] == "reject"

    safe_prod = make_product(
        "470889",
        name="Oatly Oat Drink Whole 1 L",
        allergens={"milk": "free", "nuts": "free"},
        source={"milk": "structured", "nuts": "structured"},
    )
    q_safe = make_quote("school-tampere", [make_line("470889", "Oatly Oat Drink Whole 1 L", qty=50, price=1.8)])
    dec_safe = evaluate(q_safe, mandate, products={"470889": safe_prod}, now=NOW, audit_log=log)
    assert dec_safe.outcome == "auto_approve"
    hits_safe = allergen_hits(dec_safe)
    assert len(hits_safe) == 1 and hits_safe[0].outcome == "allow"
    assert "milk or nuts" in hits_safe[0].message

    for bad_status, expected_phrase in [
        ("contains", "contains nuts"),
        ("may_contain", "may contain traces of nuts"),
        ("unknown", "Unknown is never treated as safe, so the line cannot be ordered."),
    ]:
        prod = make_product(
            "470889",
            name="Oatly Oat Drink Whole 1 L",
            allergens={"milk": "free", "nuts": bad_status},
            source={"milk": "structured", "nuts": "structured" if bad_status != "unknown" else "none"},
        )
        dec = evaluate(q_safe, mandate, products={"470889": prod}, now=NOW, audit_log=log)
        assert dec.outcome == "reject"
        hit = allergen_hits(dec)[0]
        assert hit.rule_id == "allergens:470889"
        assert hit.outcome == "reject"
        assert expected_phrase in hit.message

    inferred_prod = make_product(
        "470889",
        name="Oatly Oat Drink Whole 1 L",
        allergens={"milk": "free", "nuts": "free"},
        source={"milk": "structured", "nuts": "inferred:gemini"},
    )
    dec_inf = evaluate(q_safe, mandate, products={"470889": inferred_prod}, now=NOW, audit_log=log)
    assert dec_inf.outcome == "reject"
    assert "AI-inferred" in allergen_hits(dec_inf)[0].message

    dec_missing = evaluate(q_safe, mandate, products={}, now=NOW, audit_log=log)
    assert dec_missing.outcome == "reject"
    assert "no supplier allergen data" in allergen_hits(dec_missing)[0].message


def test_r_allergen_block_hospital_meilahti_unknown_needs_dietitian_while_contains_rejects(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    mandate = get_mandate("hospital-meilahti", "buyer-1")
    pol = load_policy("hospital-meilahti")
    rule = allergen_rule_of(pol)
    assert rule["excluded"] == ["gluten"]
    assert rule["on_unknown"] == "needs_approval"
    assert rule["approver_role"] == "dietitian"

    line = make_line("473328", "Schar Vital dark sliced bread 350 g gluten-free", qty=20, price=3.5)
    q = make_quote("hospital-meilahti", [line], claims=("FREE_FROM_GLUTEN",), unit="KG", product_query="bread")

    st_decl, src_decl, _ = derive(make_cls(non_allergen=["AW"], claims=["FREE_FROM_GLUTEN"]))
    safe_bread = make_product(
        "473328",
        name="Schar Vital dark sliced bread 350 g gluten-free",
        category="11000",
        allergens=st_decl,
        source=src_decl,
        claims=("FREE_FROM_GLUTEN",),
        content=0.35,
        unit="KG",
    )
    dec_safe = evaluate(q, mandate, products={"473328": safe_bread}, now=NOW, audit_log=log)
    assert dec_safe.outcome == "auto_approve"
    assert allergen_hits(dec_safe)[0].outcome == "allow"

    st_claim, src_claim, _ = derive(make_cls(claims=["FREE_FROM_GLUTEN"]))
    claim_only_bread = safe_bread.model_copy(update={"allergens": st_claim, "allergen_source": src_claim})
    dec_claim = evaluate(q, mandate, products={"473328": claim_only_bread}, now=NOW, audit_log=log)
    assert dec_claim.outcome == "needs_approval"
    assert dec_claim.approver_role == "dietitian"
    assert "nutrition claim" in allergen_hits(dec_claim)[0].message

    st_sub_free, src_sub_free, _ = derive(make_cls(non_allergen=["UW"]))
    unknown_bread = safe_bread.model_copy(update={"allergens": st_sub_free, "allergen_source": src_sub_free})
    dec_unk = evaluate(q, mandate, products={"473328": unknown_bread}, now=NOW, audit_log=log)
    assert dec_unk.outcome == "needs_approval"
    assert dec_unk.approver_role == "dietitian"
    hit_unk = allergen_hits(dec_unk)[0]
    assert hit_unk.rule_id == "allergens:473328"
    assert hit_unk.outcome == "needs_approval"
    assert "dietitian must check the label first" in hit_unk.message

    expensive_line = make_line("473328", "Schar Vital dark sliced bread 350 g gluten-free", qty=400, price=4.0)
    q_big = make_quote(
        "hospital-meilahti", [expensive_line], claims=("FREE_FROM_GLUTEN",), unit="KG", product_query="bread"
    )
    dec_big = evaluate(q_big, mandate, products={"473328": unknown_bread}, now=NOW, audit_log=log)
    assert dec_big.outcome == "needs_approval"
    assert dec_big.approver_role == "dietitian"

    for unit_kind in ("CONTAINS", "MAY_CONTAIN"):
        st_bad, src_bad, _ = derive(make_cls(allergen=[("NR", unit_kind)], claims=["FREE_FROM_GLUTEN"]))
        bad_bread = safe_bread.model_copy(update={"allergens": st_bad, "allergen_source": src_bad})
        dec_bad = evaluate(q, mandate, products={"473328": bad_bread}, now=NOW, audit_log=log)
        assert dec_bad.outcome == "reject"
        assert allergen_hits(dec_bad)[0].outcome == "reject"


def test_r_allergen_block_hotel_aurora_request_level_excluded_allergens(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    mandate = get_mandate("hotel-aurora", "buyer-1")
    pol = load_policy("hotel-aurora")
    rule = allergen_rule_of(pol)
    assert rule["excluded"] == []
    assert rule["on_unknown"] == "needs_approval"
    assert rule["approver_role"] == "procurement_manager"

    milk_prod = make_product(
        "464306",
        name="Valio Eila semi skimmed milk drink 1 l lactose free",
        category="10400",
        allergens={"milk": "contains", "nuts": "free", "sesame": "free"},
        source={"milk": "structured", "nuts": "structured", "sesame": "structured"},
        claims=("FREE_FROM_LACTOSE",),
    )
    line = make_line("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", qty=100, price=1.01, tier="best")

    q_no_excl = make_quote("hotel-aurora", [line], excluded=(), claims=("FREE_FROM_LACTOSE",), product_query="milk")
    dec_none = evaluate(q_no_excl, mandate, products={"464306": milk_prod}, now=NOW, audit_log=log)
    assert dec_none.outcome == "auto_approve"
    hit_none = allergen_hits(dec_none)[0]
    assert hit_none.outcome == "allow" and "no allergen exclusions" in hit_none.message

    q_free = make_quote(
        "hotel-aurora", [line], excluded=("nuts", "sesame"), claims=("FREE_FROM_LACTOSE",), product_query="milk"
    )
    dec_free = evaluate(q_free, mandate, products={"464306": milk_prod}, now=NOW, audit_log=log)
    assert dec_free.outcome == "auto_approve"
    hit_free = allergen_hits(dec_free)[0]
    assert hit_free.outcome == "allow" and "nuts or sesame" in hit_free.message

    q_milk_excl = make_quote(
        "hotel-aurora", [line], excluded=("milk",), claims=("FREE_FROM_LACTOSE",), product_query="milk"
    )
    dec_contains = evaluate(q_milk_excl, mandate, products={"464306": milk_prod}, now=NOW, audit_log=log)
    assert dec_contains.outcome == "reject"
    assert allergen_hits(dec_contains)[0].outcome == "reject"

    may_sesame = milk_prod.model_copy(
        update={
            "allergens": {"milk": "contains", "nuts": "free", "sesame": "may_contain"},
            "allergen_source": {"milk": "structured", "nuts": "structured", "sesame": "structured"},
        }
    )
    dec_may = evaluate(q_free, mandate, products={"464306": may_sesame}, now=NOW, audit_log=log)
    assert dec_may.outcome == "reject"
    assert "may contain traces of sesame" in allergen_hits(dec_may)[0].message

    q_soy_excl = make_quote(
        "hotel-aurora", [line], excluded=("soy",), claims=("FREE_FROM_LACTOSE",), product_query="milk"
    )
    dec_unk = evaluate(q_soy_excl, mandate, products={"464306": milk_prod}, now=NOW, audit_log=log)
    assert dec_unk.outcome == "needs_approval"
    assert dec_unk.approver_role == "procurement_manager"
    hit_unk = allergen_hits(dec_unk)[0]
    assert hit_unk.rule_id == "allergens:464306"
    assert hit_unk.outcome == "needs_approval"
    assert "procurement manager must check the label first" in hit_unk.message


def test_r_allergen_block_direct_ctx_line_flags_and_combined_exclusions():
    pol = load_policy("school-tampere")
    mandate = get_mandate("school-tampere", "buyer-1")
    rule = allergen_rule_of(pol)

    ln_ok = make_line("100", "Safe Oat Drink", qty=5, price=2.0)
    ln_flag_con = make_line("101", "Unlisted Nut Bar", qty=5, price=2.0, flags=("contains:gluten",))
    ln_flag_may = make_line("102", "Unlisted Biscuit", qty=5, price=2.0, flags=("may_contain:gluten",))
    q = make_quote("school-tampere", [ln_ok, ln_flag_con, ln_flag_may], excluded=("nuts", "gluten"))

    prod_ok = make_product(
        "100",
        allergens={"milk": "free", "nuts": "free", "gluten": "free"},
        source={"milk": "structured", "nuts": "structured", "gluten": "structured"},
    )
    ctx = Ctx(
        quote=q,
        mandate=mandate,
        policy=pol,
        products={"100": prod_ok, "101": None, "102": None},
        now=NOW,
    )
    r_allergen_block(ctx, rule)

    by_id = {h.rule_id: (h, role) for h, role in ctx.hits}
    assert "allergens:100" not in by_id
    assert by_id["allergens:101"][0].outcome == "reject"
    assert "contains gluten" in by_id["allergens:101"][0].message
    assert by_id["allergens:102"][0].outcome == "reject"
    assert "may contain traces of gluten" in by_id["allergens:102"][0].message


def test_r_allergen_block_without_need_and_intraline_severity_precedence(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    hotel_mandate = get_mandate("hotel-aurora", "buyer-1")
    hotel_line = make_line("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", qty=50, price=1.01, tier="best")
    q_hotel_no_need = make_quote("hotel-aurora", [hotel_line], with_need=False)
    milk_prod = make_product(
        "464306",
        name="Valio Eila semi skimmed milk drink 1 l lactose free",
        category="10400",
        allergens={"milk": "contains"},
        source={"milk": "structured"},
        claims=("FREE_FROM_LACTOSE",),
    )
    dec_hotel = evaluate(q_hotel_no_need, hotel_mandate, products={"464306": milk_prod}, now=NOW, audit_log=log)
    assert dec_hotel.outcome == "auto_approve"
    assert "no allergen exclusions" in allergen_hits(dec_hotel)[0].message

    school_pol = load_policy("school-tampere")
    school_mandate = get_mandate("school-tampere", "buyer-1")
    school_rule = allergen_rule_of(school_pol)
    q_school_no_need = make_quote("school-tampere", [make_line("472833", "Oat drink 1 L", qty=10, price=1.8)], with_need=False)

    mixed_con_may = make_product(
        "472833",
        allergens={"milk": "may_contain", "nuts": "contains"},
        source={"milk": "structured", "nuts": "structured"},
    )
    ctx1 = Ctx(quote=q_school_no_need, mandate=school_mandate, policy=school_pol, products={"472833": mixed_con_may}, now=NOW)
    r_allergen_block(ctx1, school_rule)
    assert ctx1.hits[0][0].outcome == "reject"
    assert "contains nuts" in ctx1.hits[0][0].message

    mixed_may_unk = make_product(
        "472833",
        allergens={"milk": "unknown", "nuts": "may_contain"},
        source={"milk": "none", "nuts": "structured"},
    )
    ctx2 = Ctx(quote=q_school_no_need, mandate=school_mandate, policy=school_pol, products={"472833": mixed_may_unk}, now=NOW)
    r_allergen_block(ctx2, school_rule)
    assert ctx2.hits[0][0].outcome == "reject"
    assert "may contain traces of nuts" in ctx2.hits[0][0].message

    hosp_pol = load_policy("hospital-meilahti")
    hosp_mandate = get_mandate("hospital-meilahti", "buyer-1")
    hosp_q = make_quote(
        "hospital-meilahti",
        [make_line("473328", "Bread 350 g", qty=10, price=3.5)],
        claims=("FREE_FROM_GLUTEN",),
        unit="KG",
    )
    hosp_inf = make_product(
        "473328",
        category="11000",
        allergens={"gluten": "free"},
        source={"gluten": "inferred:gemini"},
        claims=("FREE_FROM_GLUTEN",),
        content=0.35,
        unit="KG",
    )
    dec_hosp_inf = evaluate(hosp_q, hosp_mandate, products={"473328": hosp_inf}, now=NOW, audit_log=log)
    assert dec_hosp_inf.outcome == "needs_approval"
    assert dec_hosp_inf.approver_role == "dietitian"
    assert "AI-inferred" in allergen_hits(dec_hosp_inf)[0].message

    custom_allow_unknown = {
        "id": "allergens",
        "type": "allergen_block",
        "excluded": ["lupin"],
        "on_unknown": "allow",
    }
    ctx3 = Ctx(
        quote=q_school_no_need,
        mandate=school_mandate,
        policy=school_pol,
        products={"472833": make_product("472833")},
        now=NOW,
    )
    r_allergen_block(ctx3, custom_allow_unknown)
    assert ctx3.hits[0][0].outcome == "allow"
    assert ctx3.hits[0][1] is None

