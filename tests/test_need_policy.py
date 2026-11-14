"""S3 trust policy tests: learned swaps, first purchases, small swaps, split orders,
rolling quantity drift, price jumps, claim-only allergen handling, and planning_profile.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import shutil
import types

import pytest
import yaml

from aimo import config
from aimo.catalog import textsearch
from aimo.models import EU14, Need, Product, Quote, QuoteLine
from aimo.trust import learned
from aimo.trust import policy as P
from aimo.trust.approvals import log_quote
from aimo.trust.audit import AuditLog
from aimo.trust.policy import _unit_plausible, evaluate, get_mandate, list_customers, load_policy, planning_profile

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
MON = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    yield


@pytest.fixture
def policy_dir(tmp_path, monkeypatch):
    d = tmp_path / "policies"
    shutil.copytree(P.POLICY_DIR, d)
    monkeypatch.setattr(P, "POLICY_DIR", d)
    P._load_policy_cached.cache_clear()
    yield d
    P._load_policy_cached.cache_clear()


def _edit_policy(d, name, fn):
    p = d / f"{name}.yaml"
    pol = yaml.safe_load(p.read_text(encoding="utf-8"))
    fn(pol)
    p.write_text(yaml.safe_dump(pol, sort_keys=False), encoding="utf-8")
    P._load_policy_cached.cache_clear()


def make_product(
    pid,
    name,
    category="10400",
    claims=(),
    content=1.0,
    unit="L",
    brand="Valio",
    allergens=None,
    source=None,
    marketing=None,
):
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
        marketing_text=marketing,
    )


def make_row(
    pid,
    name,
    category="10400",
    claims=(),
    content=1.0,
    unit="L",
    brand="Valio",
    fat_pct=None,
    allergens=None,
    allergen_source=None,
):
    all_st = {g: "free" for g in EU14}
    all_src = {g: "structured" for g in EU14}
    if allergens:
        all_st.update(allergens)
    if allergen_source:
        all_src.update(allergen_source)
    return {
        "id": pid,
        "gtin": None,
        "name": name,
        "name_en": name,
        "name_fi": name,
        "name_sv": None,
        "brand": brand,
        "vendor": brand,
        "category": category,
        "sales_unit": "ST",
        "base_unit": unit,
        "sales_unit_size": 1.0,
        "allowed_lot_size": 1.0,
        "content_amount": content,
        "content_unit": unit,
        "base_content_amount": content,
        "temperature_condition": 8.0,
        "min_delivery_days": 1,
        "lead_time_days": 1,
        "fat_pct": fat_pct,
        "energy_kj": 200.0,
        "protein_g": 3.0,
        "salt_g": 0.1,
        "sugars_g": 4.0,
        "co2_per_kg": 1.0,
        "country_of_origin": "FI",
        "claims": list(claims),
        "certifications": [],
        "allergens": all_st,
        "allergen_source": all_src,
        "ingredients": name,
        "marketing_text": None,
        "discontinued": False,
        "has_substitutes": False,
    }


def make_quote(
    customer="hotel-aurora",
    pid="464306",
    name="Valio Eila semi skimmed milk drink 1 l lactose free",
    qty=10,
    price=1.0,
    tier="best",
    query="lactose-free milk",
    claims=("FREE_FROM_LACTOSE",),
    unit="L",
    prefs=None,
    qid=None,
    now=NOW,
    flags=(),
    excluded=(),
):
    need = Need(
        customer_id=customer,
        product_query=query,
        required_claims=list(claims),
        excluded_allergens=list(excluded),
        quantity=qty,
        unit=unit,
        delivery_by=MON,
        preferences=prefs or {},
    )
    total = round(qty * price, 2)
    return Quote(
        quote_id=qid or f"Q-{customer}-{pid}-{qty}-{price}",
        customer_id=customer,
        agent_id=f"{customer}/buyer-1",
        delivery_date=MON,
        lines=[
            QuoteLine(
                product_id=pid,
                product_name=name,
                qty_sales_units=qty,
                sales_unit="ST",
                unit_price=price,
                line_total=total,
                tier=tier,
                flags=list(flags),
            )
        ],
        total_net=total,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        need=need,
    )


def dec(q, customer="hotel-aurora", products=None, log=None, now=NOW):
    return evaluate(
        q,
        get_mandate(customer, "buyer-1"),
        products=products,
        now=now,
        audit_log=log or AuditLog(config.STATE_DB),
    )


def hits(d, prefix):
    return [h for h in d.hits if h.rule_id.split(":")[0] == prefix]


class MemLog:
    class E:
        def __init__(self, action, ts, data):
            self.action, self.ts, self.data = action, ts, data

    def __init__(self):
        self.events = []

    def events_by_action(self, action):
        return [e for e in self.events if e.action == action]

    def place(self, q, ts=NOW, approver=None):
        data = {
            "customer_id": q.customer_id,
            "total_net": q.total_net,
            "quote_id": q.quote_id,
        }
        if approver:
            data["approver"] = approver
        self.events.append(MemLog.E("order.placed", ts, data))


def test_planning_profile_for_known_and_unknown_customers():
    school = planning_profile("school-tampere")
    assert school == {
        "policy_id": "school-tampere",
        "excluded_allergens": ["milk", "nuts"],
        "allergen_reason": "pupils with milk and nut allergies",
        "usual_products": ["470889"],
        "approved_alternatives": {},
        "customer_name": "Tampere Hervanta School Kitchen",
    }

    hosp = planning_profile("hospital-meilahti")
    assert hosp["policy_id"] == "hospital-meilahti"
    assert hosp["excluded_allergens"] == ["gluten"]
    assert hosp["allergen_reason"] == "coeliac patients"
    assert hosp["usual_products"] == ["473328"]

    hotel = planning_profile("hotel-aurora")
    assert hotel["policy_id"] == "hotel-aurora"
    assert hotel["excluded_allergens"] == []
    assert hotel["allergen_reason"] is None
    assert hotel["usual_products"] == ["463038"]
    assert hotel["approved_alternatives"] == {"464306": "best"}

    assert planning_profile("no-such-customer") == {}
    assert planning_profile("") == {}
    assert planning_profile(None) == {}


def test_policies_v120_and_customer_summary_lists_new_rules():
    for cid in ("hotel-aurora", "school-tampere", "hospital-meilahti"):
        pol = load_policy(cid)
        assert pol["version"] == "1.2.0"
        rule_types = {r["type"] for r in pol["rules"]}
        assert {"split_order", "price_jump"} <= rule_types

    customers = {c["customer_id"]: c for c in list_customers()}
    hotel = customers["hotel-aurora"]
    assert hotel["policy_version"] == "1.2.0"
    text = " ".join(hotel["summary"])
    assert "split-order" in text
    assert "15% above the last price" in text
    assert "€50.00" in text
    assert "first purchase" in text
    assert "'always allow'" in text

    school = customers["school-tampere"]
    assert [p["product_id"] for p in school["preferred_products"]] == ["470889"]


def test_hotel_category_scope_v120_allows_dry_and_frozen_and_escalates_coffee_and_meat():
    for cat in ("10400", "11200", "17100", "21100", "41300"):
        p = make_product("463038", "Hotel usual item 1 L", category=cat, claims=("FREE_FROM_LACTOSE",))
        q = make_quote(pid="463038", name=p.name, qty=5, price=10.0, tier="preferred")
        d = dec(q, products={"463038": p})
        assert hits(d, "category_scope")[0].outcome == "allow"

    for cat in ("22100", "18100"):
        p = make_product("463038", "Out of scope item 1 L", category=cat, claims=("FREE_FROM_LACTOSE",))
        q = make_quote(pid="463038", name=p.name, qty=5, price=10.0, tier="preferred")
        d = dec(q, products={"463038": p})
        assert hits(d, "category_scope")[0].outcome == "needs_approval"


def test_small_preapproved_swap_auto_places(policy_dir):
    _edit_policy(
        policy_dir,
        "hotel-aurora",
        lambda pol: pol["approved_alternatives"].append(
            {"product_id": "469363", "tier": "better", "for": "463038", "label": "Arla lactose-free milk drink 1 L"}
        ),
    )
    arla = make_product(
        "469363",
        "Arla Semi skimmed milkdrink 1 L UHT lactose free",
        claims=("FREE_FROM_LACTOSE",),
        brand="Arla",
    )
    small = dec(make_quote(pid="469363", name=arla.name, qty=10, price=1.0, tier="better"), products={"469363": arla})
    sub = hits(small, "substitution")
    assert small.outcome == "auto_approve", [h.message for h in small.hits if h.outcome != "allow"]
    assert sub[0].outcome == "allow" and "below the €50.00" in sub[0].message

    big = dec(make_quote(pid="469363", name=arla.name, qty=100, price=1.0, tier="better"), products={"469363": arla})
    assert big.outcome == "needs_approval" and hits(big, "substitution")[0].outcome == "needs_approval"

    unlisted = make_product("999111", "Other brand lactose free milk 1 L", claims=("FREE_FROM_LACTOSE",), brand="Other")
    small_unlisted = dec(
        make_quote(pid="999111", name=unlisted.name, qty=10, price=1.0, tier="better"),
        products={"999111": unlisted},
    )
    assert small_unlisted.outcome == "needs_approval"


def test_learned_swap_product_scope_and_revocation(monkeypatch):
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {})
    with pytest.raises(ValueError):
        learned.remember_swap("hotel-aurora", "463038", "469363", "invalid_scope")
    with pytest.raises(ValueError):
        learned.remember_swap("", "463038", "469363", "product")

    arla = make_product(
        "469363",
        "Arla Semi skimmed milkdrink 1 L UHT lactose free",
        claims=("FREE_FROM_LACTOSE",),
        brand="Arla",
    )
    q = make_quote(pid="469363", name=arla.name, qty=100, price=1.2, tier="better", flags=("outside_preference",))
    assert dec(q, products={"469363": arla}).outcome == "needs_approval"

    saved = learned.remember_swap(
        "hotel-aurora",
        "463038",
        "469363",
        "product",
        approver="anna@hotel-aurora.example",
        approver_role="procurement_manager",
        quote_id="Q-1",
        order_id="O-1",
        note="approved for breakfast",
    )
    assert saved["from_product_id"] == "463038" and saved["to_product_id"] == "469363"

    d = dec(q, products={"469363": arla})
    assert d.outcome == "auto_approve", [h.message for h in d.hits if h.outcome != "allow"]
    assert "always allow" in hits(d, "substitution")[0].message

    assert learned.match("hotel-aurora", "469363", reference_id="999999") is None
    assert learned.match("hotel-aurora", "469363", reference_id="463038") is not None
    assert learned.list_learned("hotel-aurora")[0]["to_product_id"] == "469363"
    assert learned.list_learned("school-tampere") == []

    assert learned.forget_swap("hotel-aurora", "469363") == 1
    assert learned.list_learned("hotel-aurora") == []
    revoked = learned.list_learned("hotel-aurora", include_revoked=True)
    assert len(revoked) == 1 and revoked[0]["revoked_at"] is not None
    assert dec(q, products={"469363": arla}).outcome == "needs_approval"


def test_learned_swap_product_type_scope_never_unlocks_allergens(monkeypatch):
    rows = {
        "463038": make_row(
            "463038",
            "Valio Eila semi skimmed milk drink 10 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE",),
            content=10.0,
            unit="L",
            brand="Valio",
            fat_pct=1.5,
            allergens={"milk": "contains"},
        ),
        "469363": make_row(
            "469363",
            "Arla Semi skimmed milk drink 1 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE",),
            content=1.0,
            unit="L",
            brand="Arla",
            fat_pct=1.5,
            allergens={"milk": "contains"},
        ),
        "900101": make_row(
            "900101",
            "Maitomaa semi skimmed milk drink 1 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE", "ORGANIC"),
            content=1.0,
            unit="L",
            brand="Maitomaa",
            fat_pct=1.5,
            allergens={"milk": "contains"},
        ),
        "900102": make_row(
            "900102",
            "Plain milk drink 1 L",
            category="10400",
            claims=(),
            content=1.0,
            unit="L",
            brand="Maitomaa",
            fat_pct=1.5,
            allergens={"milk": "contains"},
        ),
        "900103": make_row(
            "900103",
            "Chocolate flavoured milk drink 1 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE",),
            content=1.0,
            unit="L",
            brand="Maitomaa",
            fat_pct=1.5,
            allergens={"milk": "contains"},
        ),
    }
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {i: rows[i] for i in ids if i in rows})

    learned.remember_swap("hotel-aurora", "463038", "469363", "product_type", approver="anna@hotel-aurora.example")
    p_sibling = make_product(
        "900101",
        rows["900101"]["name"],
        claims=("FREE_FROM_LACTOSE", "ORGANIC"),
        brand="Maitomaa",
        allergens={"milk": "contains"},
        source={"milk": "structured"},
    )
    q_sibling = make_quote(pid="900101", name=p_sibling.name, qty=50, price=1.2, tier="better")
    d_sibling = dec(q_sibling, products={"900101": p_sibling})
    assert d_sibling.outcome == "auto_approve", [h.message for h in d_sibling.hits if h.outcome != "allow"]
    assert "(product_type)" in hits(d_sibling, "substitution")[0].message

    p_no_claim = make_product("900102", rows["900102"]["name"], claims=(), brand="Maitomaa")
    d_no_claim = dec(
        make_quote(pid="900102", name=p_no_claim.name, qty=50, price=1.2, tier="better", claims=()),
        products={"900102": p_no_claim},
    )
    assert d_no_claim.outcome == "needs_approval"

    p_choco = make_product("900103", rows["900103"]["name"], claims=("FREE_FROM_LACTOSE",), brand="Maitomaa")
    d_choco = dec(
        make_quote(pid="900103", name=p_choco.name, qty=50, price=1.2, tier="better"),
        products={"900103": p_choco},
    )
    assert d_choco.outcome == "needs_approval"

    q_excl = make_quote(pid="900101", name=p_sibling.name, qty=50, price=1.2, tier="better", excluded=("milk",))
    d_excl = dec(q_excl, products={"900101": p_sibling})
    assert d_excl.outcome == "reject"
    assert hits(d_excl, "allergens")[0].outcome == "reject"


def test_first_purchase_matching_request_is_new_product_and_unusual_unit_escalates(monkeypatch):
    rows = {
        "463038": make_row(
            "463038",
            "Valio Eila semi skimmed milk drink 10 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE",),
            content=10.0,
            unit="L",
            brand="Valio",
            fat_pct=1.5,
        ),
        "472127": make_row(
            "472127",
            "Porlammin Clarified butter 1,8kg",
            category="10600",
            claims=(),
            content=1.8,
            unit="KG",
            brand="Porlammin",
            fat_pct=99.0,
        ),
        "472128": make_row(
            "472128",
            "Valio Clarified butter 2 kg",
            category="10600",
            claims=(),
            content=2.0,
            unit="KG",
            brand="Valio",
            fat_pct=99.0,
        ),
        "477044": make_row(
            "477044",
            "Valio lactose free milk quark 5 kg",
            category="10500",
            claims=("FREE_FROM_LACTOSE",),
            content=5.0,
            unit="KG",
            brand="Valio",
        ),
        "464306": make_row(
            "464306",
            "Valio Eila semi skimmed milk drink 1 L lactose free",
            category="10400",
            claims=("FREE_FROM_LACTOSE",),
            content=1.0,
            unit="L",
            brand="Valio",
        ),
        "800001": make_row("800001", "Jasmine rice 5 kg", category="21100", unit="KG", content=5.0, brand=" Risella"),
    }

    def fake_get_rows(ids):
        return {i: rows[i] for i in ids if i in rows}

    def fake_fts(query, **kw):
        ql = query.lower()
        if "butter" in ql:
            return [("472127", 10.0), ("472128", 9.5)]
        if "milk" in ql:
            return [("463038", 10.0), ("464306", 9.8)]
        if "rice" in ql:
            return [("800001", 10.0)]
        return []

    monkeypatch.setattr(textsearch, "get_rows", fake_get_rows)
    monkeypatch.setattr(textsearch, "fts_search", fake_fts)

    assert _unit_plausible("clarified butter", "KG") is True
    assert _unit_plausible("clarified butter", "L") is False
    assert _unit_plausible("lactose-free milk", "L") is True
    assert _unit_plausible("lactose-free milk", "KG") is False
    assert _unit_plausible("rice", "KG") is True
    assert _unit_plausible("rice", "L") is False

    butter_prod = make_product(
        "472127",
        "Porlammin Clarified butter 1,8kg",
        category="10600",
        content=1.8,
        unit="KG",
        brand="Porlammin",
    )
    q_butter = make_quote(
        pid="472127",
        name=butter_prod.name,
        qty=2,
        price=20.0,
        tier="best",
        query="clarified butter",
        claims=(),
        unit="KG",
    )
    d_butter = dec(q_butter, products={"472127": butter_prod})
    sub = hits(d_butter, "substitution")
    assert sub[0].outcome == "allow" and "new product" in sub[0].message, sub[0].message
    assert d_butter.outcome == "auto_approve", [h.message for h in d_butter.hits if h.outcome != "allow"]

    q_worse_claim = q_butter.model_copy(
        update={"lines": [q_butter.lines[0].model_copy(update={"tier": "good"})]}
    )
    assert dec(q_worse_claim, products={"472127": butter_prod}).outcome == "needs_approval"

    quark_prod = make_product(
        "477044",
        "Valio lactose free milk quark 5 kg",
        category="10500",
        claims=("FREE_FROM_LACTOSE",),
        content=5.0,
        unit="KG",
    )
    q_wrong_unit = make_quote(
        pid="477044",
        name=quark_prod.name,
        qty=5,
        price=10.0,
        tier="best",
        query="lactose-free milk",
        claims=("FREE_FROM_LACTOSE",),
        unit="KG",
    )
    d_wrong_unit = dec(q_wrong_unit, products={"477044": quark_prod})
    sub_wrong = hits(d_wrong_unit, "substitution")[0]
    assert d_wrong_unit.outcome == "needs_approval" and sub_wrong.outcome == "needs_approval"
    assert "unusual for 'lactose-free milk'" in sub_wrong.message


def test_school_substitute_for_usual_oat_drink_still_needs_kitchen_manager(monkeypatch):
    rows = {
        "470889": make_row(
            "470889",
            "Oatly Oat Drink Whole 1 L",
            category="10402",
            content=1.0,
            unit="L",
            brand="Oatly",
            fat_pct=3.0,
            allergens={"milk": "free", "nuts": "free"},
        ),
        "470863": make_row(
            "470863",
            "Oatly Oat Drink Semi 1 L",
            category="10402",
            content=1.0,
            unit="L",
            brand="Oatly",
            fat_pct=1.5,
            allergens={"milk": "free", "nuts": "free"},
        ),
    }
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {i: rows[i] for i in ids if i in rows})
    semi = make_product(
        "470863",
        "Oatly Oat Drink Semi 1 L",
        category="10402",
        brand="Oatly",
        allergens={"milk": "free", "nuts": "free"},
        source={"milk": "structured", "nuts": "structured"},
    )
    q = make_quote(
        customer="school-tampere",
        pid="470863",
        name=semi.name,
        qty=10,
        price=1.5,
        tier="preferred",
        query="oat drink",
        claims=(),
    )
    d = dec(q, customer="school-tampere", products={"470863": semi})
    assert d.outcome == "needs_approval" and d.approver_role == "kitchen_manager"
    assert "usual product 470889" in hits(d, "substitution")[0].message


def test_school_milk_request_needs_human_without_agent_flags_both_standalone_and_delegated(monkeypatch):
    import aimo.catalog.plan as plan_mod

    oatly = make_product(
        "470889",
        "Oatly Oat Drink Whole 1 L",
        category="10402",
        brand="Oatly",
        allergens={"milk": "free", "nuts": "free"},
        source={"milk": "structured", "nuts": "structured"},
    )
    q_milk = make_quote(
        customer="school-tampere",
        pid="470889",
        name=oatly.name,
        qty=20,
        price=1.5,
        tier="preferred",
        query="milk",
        claims=(),
    )
    q_cream = make_quote(
        customer="school-tampere",
        pid="470889",
        name=oatly.name,
        qty=20,
        price=1.5,
        tier="preferred",
        query="cream",
        claims=(),
    )
    q_oat = make_quote(
        customer="school-tampere",
        pid="470889",
        name=oatly.name,
        qty=20,
        price=1.5,
        tier="preferred",
        query="oat drink",
        claims=(),
    )
    q_soy_milk = make_quote(
        customer="school-tampere",
        pid="470889",
        name=oatly.name,
        qty=20,
        price=1.5,
        tier="preferred",
        query="soy milk",
        claims=(),
    )

    monkeypatch.delattr(plan_mod, "_excluded_groups_named", raising=False)
    d_milk = dec(q_milk, customer="school-tampere", products={"470889": oatly})
    assert d_milk.outcome == "needs_approval" and d_milk.approver_role == "kitchen_manager"
    assert any("allergen-free replacement" in h.message for h in hits(d_milk, "substitution"))

    d_cream = dec(q_cream, customer="school-tampere", products={"470889": oatly})
    assert d_cream.outcome == "needs_approval" and d_cream.approver_role == "kitchen_manager"

    d_oat = dec(q_oat, customer="school-tampere", products={"470889": oatly})
    assert d_oat.outcome == "auto_approve", [h.message for h in d_oat.hits if h.outcome != "allow"]

    d_soy_milk = dec(q_soy_milk, customer="school-tampere", products={"470889": oatly})
    assert d_soy_milk.outcome == "auto_approve", [h.message for h in d_soy_milk.hits if h.outcome != "allow"]

    calls = []
    monkeypatch.setattr(
        plan_mod,
        "_excluded_groups_named",
        lambda q, ex: (calls.append((q, list(ex))), (["milk"] if q == "milk" else [], []))[1],
        raising=False,
    )
    d_delegated = dec(q_milk, customer="school-tampere", products={"470889": oatly})
    assert d_delegated.outcome == "needs_approval"
    assert calls == [("milk", ["milk", "nuts"])]


def test_split_orders_escalate_on_second_order_and_ignore_human_approved_or_tiny():
    log = MemLog()
    milk_prod = {"464306": make_product("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", claims=("FREE_FROM_LACTOSE",))}
    outcomes = []
    d = None
    for i in range(3):
        q = make_quote(pid="464306", qty=396, price=1.01, qid=f"Q-split-{i}")
        d = dec(q, products=milk_prod, log=log)
        outcomes.append(d.outcome)
        if d.outcome == "auto_approve":
            log.place(q)
    assert outcomes == ["auto_approve", "needs_approval", "needs_approval"], outcomes
    last = hits(d, "split_order")[0]
    assert last.outcome == "needs_approval" and "order 2 of the same kind" in last.message

    log_human = MemLog()
    q_approved = make_quote(pid="464306", qty=396, price=1.01, qid="Q-human-1")
    log_human.place(q_approved, approver="anna@hotel-aurora.example")
    d_after_human = dec(make_quote(pid="464306", qty=396, price=1.01, qid="Q-human-2"), products=milk_prod, log=log_human)
    assert hits(d_after_human, "split_order")[0].outcome == "allow"

    q_big = make_quote(pid="464306", qty=600, price=1.01, qid="Q-over-auto")
    d_big = dec(q_big, products=milk_prod, log=log)
    assert hits(d_big, "split_order")[0].outcome == "allow"
    assert hits(d_big, "spend")[0].outcome == "needs_approval"


def test_split_aggregation_uses_line_detail_from_audit_log():
    log = AuditLog(config.STATE_DB)
    milk_prod = make_product("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", claims=("FREE_FROM_LACTOSE",))
    butter_prod = make_product("472127", "Porlammin Clarified butter 1,8kg", category="10600", content=1.8, unit="KG")
    cheese_prod = make_product("468000", "Valio cream cheese 1,5 kg", category="10300", content=1.5, unit="KG")

    butter_q = make_quote(
        pid="472127",
        name=butter_prod.name,
        qty=20,
        price=19.99,
        query="butter",
        claims=(),
        unit="KG",
        qid="Q-butter",
    )
    for qid in ("Q-milk-a", "Q-milk-b"):
        log_quote(make_quote(pid="464306", qty=396, price=1.01, qid=qid), audit_log=log)
        log.append(
            "system:gateway",
            "order.placed",
            f"O-{qid}",
            "placed",
            {"customer_id": "hotel-aurora", "quote_id": qid, "total_net": 399.96},
        )
    log_quote(butter_q, audit_log=log)
    log.append(
        "system:gateway",
        "order.placed",
        "O-butter",
        "placed",
        {"customer_id": "hotel-aurora", "quote_id": "Q-butter", "total_net": butter_q.total_net},
    )

    milk_d = dec(
        make_quote(pid="464306", qty=396, price=1.01, qid="Q-milk-c"),
        products={"464306": milk_prod, "472127": butter_prod, "468000": cheese_prod},
        log=log,
    )
    assert hits(milk_d, "split_order")[0].outcome == "needs_approval"

    other_d = dec(
        make_quote(
            pid="468000",
            name=cheese_prod.name,
            qty=20,
            price=5.0,
            query="cream cheese",
            claims=(),
            unit="KG",
            qid="Q-other",
        ),
        products={"464306": milk_prod, "472127": butter_prod, "468000": cheese_prod},
        log=log,
    )
    assert hits(other_d, "split_order")[0].outcome == "allow"


def test_quantity_drift_counts_rolling_7_day_window():
    log = AuditLog(config.STATE_DB)
    milk_prod = {"464306": make_product("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", claims=("FREE_FROM_LACTOSE",))}
    prior = make_quote(pid="464306", qty=600, price=0.5, qid="Q-prior")
    log_quote(prior, audit_log=log)
    log.append(
        "system:gateway",
        "order.placed",
        "O-prior",
        "placed",
        {"customer_id": "hotel-aurora", "quote_id": "Q-prior", "total_net": prior.total_net},
    )
    now_eval = datetime.now(timezone.utc) + timedelta(hours=1)
    q_now = make_quote(pid="464306", qty=396, price=0.5, qid="Q-now", now=now_eval)
    d = dec(q_now, products=milk_prod, log=log, now=now_eval)
    drift = [h for h in d.hits if h.rule_id == "quantity_drift:milk"][0]
    assert drift.outcome == "needs_approval" and "600 L ordered earlier" in drift.message

    later_eval = datetime.now(timezone.utc) + timedelta(days=10)
    q_later = make_quote(pid="464306", qty=396, price=0.5, qid="Q-later", now=later_eval)
    q_later = q_later.model_copy(update={"delivery_date": later_eval.date() + timedelta(days=2),
                                         "need": q_later.need.model_copy(update={"delivery_by": later_eval.date() + timedelta(days=2)})})
    d_later = dec(q_later, products=milk_prod, log=log, now=later_eval)
    drift_later = [h for h in d_later.hits if h.rule_id == "quantity_drift:milk"][0]
    assert drift_later.outcome == "allow"


def test_price_jump_over_15_percent_and_audit_log_last_paid_price():
    milk_prod = {"464306": make_product("464306", "Valio Eila semi skimmed milk drink 1 l lactose free", claims=("FREE_FROM_LACTOSE",))}
    up = dec(
        make_quote(pid="464306", qty=12, price=3.03, prefs={"reference_unit_price": {"464306": 1.01}}),
        products=milk_prod,
    )
    pj = hits(up, "price_jump")
    assert up.outcome == "needs_approval" and pj[0].outcome == "needs_approval" and "200% above" in pj[0].message

    ok = dec(
        make_quote(pid="464306", qty=12, price=1.10, prefs={"reference_unit_price": {"464306": 1.01}}),
        products=milk_prod,
    )
    assert hits(ok, "price_jump")[0].outcome == "allow"

    log = AuditLog(config.STATE_DB)
    now_eval = datetime.now(timezone.utc) + timedelta(minutes=5)
    prior = make_quote(pid="464306", qty=12, price=1.00, qid="Q-paid", now=now_eval)
    log_quote(prior, audit_log=log)
    log.append(
        "system:gateway",
        "order.placed",
        "O-paid",
        "placed",
        {"customer_id": "hotel-aurora", "quote_id": "Q-paid", "total_net": prior.total_net},
    )
    q_up = make_quote(
        pid="464306",
        qty=12,
        price=1.30,
        prefs={"reference_unit_price": {"464306": 1.30}},
        qid="Q-up",
        now=now_eval,
    )
    d_audit = dec(q_up, products=milk_prod, log=log, now=now_eval)
    assert hits(d_audit, "price_jump")[0].outcome == "needs_approval"
    assert "paid on" in hits(d_audit, "price_jump")[0].message


def test_claim_only_allergen_free_is_treated_as_unknown_for_hospital_and_school():
    juice = make_product(
        "461893",
        "Marli Juissi Mixed juice drink 1 L",
        category="21400",
        claims=("FREE_FROM_GLUTEN",),
        allergens={"gluten": "free"},
        source={"gluten": "structured:claim"},
        brand="Marli",
    )
    q_hosp = make_quote(
        customer="hospital-meilahti",
        pid="461893",
        name=juice.name,
        qty=10,
        price=1.0,
        query="juice",
        claims=(),
        excluded=("gluten",),
    )
    d_hosp = dec(q_hosp, customer="hospital-meilahti", products={"461893": juice})
    al_hosp = hits(d_hosp, "allergens")
    assert d_hosp.outcome == "needs_approval" and d_hosp.approver_role == "dietitian"
    assert al_hosp[0].outcome == "needs_approval" and "nutrition claim" in al_hosp[0].message

    claim_oat = make_product(
        "470889",
        "Oatly Oat Drink Whole 1 L",
        category="10402",
        allergens={"milk": "free", "nuts": "free"},
        source={"milk": "structured:claim", "nuts": "structured"},
        brand="Oatly",
    )
    q_school = make_quote(
        customer="school-tampere",
        pid="470889",
        name=claim_oat.name,
        qty=10,
        price=1.5,
        tier="preferred",
        query="oat drink",
        claims=(),
    )
    d_school = dec(q_school, customer="school-tampere", products={"470889": claim_oat})
    al_school = hits(d_school, "allergens")
    assert d_school.outcome == "reject"
    assert al_school[0].outcome == "reject" and "nutrition claim" in al_school[0].message
