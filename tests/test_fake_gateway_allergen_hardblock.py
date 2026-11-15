from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from aimo import cli, config, gemini
from aimo.buyer import fakes, run_demo
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import OFFERS, PRODUCTS, FakeGateway
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)


def _set_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_AUTO_APPROVE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def _forbid(*args, **kwargs):
        raise AssertionError("Gemini must not be called in keyless fake-gateway tests")

    monkeypatch.setattr("aimo.gemini.generate", _forbid)
    monkeypatch.setattr("aimo.gemini.embed", _forbid)


def _start_fake_approver(
    gw: FakeGateway,
    approver: str,
    decision: str = "approve",
) -> tuple[threading.Event, list[str], threading.Thread]:
    stop = threading.Event()
    decided: list[str] = []

    def _worker() -> None:
        while not stop.is_set():
            for order in gw.pending():
                gw.decide(order["order_id"], decision, approver, f"auto-{decision}")
                decided.append(order["order_id"])
                return
            stop.wait(0.02)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return stop, decided, thread


def test_file_conventions_and_470889_catalog_entry() -> None:
    this_src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in this_src
    assert "\u2014" not in this_src
    assert "\u2013" not in this_src

    fakes_src = Path(fakes.__file__).read_text(encoding="utf-8")
    assert "\u2014" not in fakes_src
    assert "\u2013" not in fakes_src

    assert "470889" in PRODUCTS
    p = PRODUCTS["470889"]
    assert p["name"] == "Oatly Oat Drink Whole 1 L"
    assert p["sales_unit"] == "ST"
    assert p["content_amount"] == 1.0
    assert p["content_unit"] == "L"
    assert p["claims"] == ["VEGAN"]
    assert p["brand"] == "Oatly"
    assert p["allergens"] == {"milk": "free", "nuts": "free", "gluten": "contains"}

    assert "470889" in OFFERS
    price, avail, model, _closing = OFFERS["470889"]
    assert price > 0
    assert avail is not None and avail >= 200
    assert model == "Stock"


def test_school_tampere_plan_need_omitted_excluded_milk_cream_and_butter() -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    raw_need = {
        "product_query": "milk",
        "quantity": 50,
        "unit": "L",
        "delivery_by": "2026-11-18",
    }
    plan = gw.plan_need(raw_need)

    assert "excluded_allergens" not in raw_need
    assert plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert [ln["product_id"] for ln in plan["lines"]] == ["470889"]
    line = plan["lines"][0]
    assert line["tier"] == "preferred"
    assert line["covered_amount"] == 50.0
    assert "allergen_safe_alternative" in line["flags"]
    assert any("allergen-safe alternative" in r.lower() for r in line["reasons"])

    for ln in plan["lines"]:
        amap = PRODUCTS[ln["product_id"]]["allergens"]
        assert amap.get("milk") == "free"
        assert amap.get("nuts") == "free"

    cands_by_id = {c["product"]["id"]: c for c in plan["candidates"]}
    for dairy_id in ("463038", "464306", "469363", "462775"):
        assert dairy_id in cands_by_id
        assert cands_by_id[dairy_id]["tier"] == "rejected"
        assert "allergen_contains" in cands_by_id[dairy_id]["flags"]
        assert any("milk: contains" in r for r in cands_by_id[dairy_id]["reasons"])

    for non_rej in [c for c in plan["candidates"] if c["tier"] != "rejected"]:
        pid = non_rej["product"]["id"]
        assert PRODUCTS[pid]["allergens"].get("milk") == "free"
        assert PRODUCTS[pid]["allergens"].get("nuts") == "free"
        assert "allergen_safe_alternative" in non_rej["flags"]

    assert "school-tampere" in plan["explanation"]
    assert "milk, nuts" in plan["explanation"]
    assert "470889" in plan["explanation"]

    gw_default = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    cross_plan = gw_default.plan_need({**raw_need, "customer_id": "school-tampere"})
    assert cross_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert [ln["product_id"] for ln in cross_plan["lines"]] == ["470889"]

    split_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "milk",
        "quantity": 1200,
        "unit": "L",
        "delivery_by": "2026-11-18",
    })
    assert [ln["product_id"] for ln in split_plan["lines"]] == ["470889", "470863"]
    assert split_plan["covered_amount"] == 1200.0
    assert all("allergen_safe_alternative" in ln["flags"] for ln in split_plan["lines"])
    assert all(
        PRODUCTS[ln["product_id"]]["allergens"].get("milk") == "free"
        and PRODUCTS[ln["product_id"]]["allergens"].get("nuts") == "free"
        for ln in split_plan["lines"]
    )

    cream_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "cream",
        "quantity": 20,
        "unit": "L",
        "delivery_by": "2026-11-18",
    })
    assert cream_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert [ln["product_id"] for ln in cream_plan["lines"]] == ["470889"]
    assert "allergen_safe_alternative" in cream_plan["lines"][0]["flags"]
    cream_cands = {c["product"]["id"]: c for c in cream_plan["candidates"]}
    assert cream_cands["401201"]["tier"] == "rejected"

    butter_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "butter",
        "quantity": 10,
        "unit": "KG",
        "delivery_by": "2026-11-18",
    })
    assert butter_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert butter_plan["lines"] == []
    assert butter_plan["shortfall"] == 10.0
    assert "milk, nuts" in butter_plan["explanation"]
    butter_cands = {c["product"]["id"]: c for c in butter_plan["candidates"]}
    assert butter_cands["402105"]["tier"] == "rejected"


def test_school_tampere_oat_drink_omitted_excluded_and_may_contain_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    oat_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "oat drink",
        "quantity": 100,
        "unit": "L",
        "delivery_by": "2026-11-18",
    })
    assert oat_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert oat_plan["lines"]
    for ln in oat_plan["lines"]:
        pid = ln["product_id"]
        assert pid in ("470863", "470889")
        assert PRODUCTS[pid]["allergens"]["milk"] == "free"
        assert PRODUCTS[pid]["allergens"]["nuts"] == "free"
        assert "allergen_safe_alternative" not in ln["flags"]

    cands_by_id = {c["product"]["id"]: c for c in oat_plan["candidates"]}
    for unk_id in ("469040", "471424"):
        assert cands_by_id[unk_id]["tier"] == "rejected"
        assert "allergen_unknown" in cands_by_id[unk_id]["flags"]

    pref_oat_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "oat drink",
        "quantity": 80,
        "unit": "L",
        "delivery_by": "2026-11-18",
        "preferred_product_ids": ["470889"],
    })
    assert [ln["product_id"] for ln in pref_oat_plan["lines"]] == ["470889"]
    assert pref_oat_plan["lines"][0]["tier"] == "preferred"

    monkeypatch.setitem(
        fakes.PRODUCTS,
        "990001",
        dict(
            name="Test Nut Trace Oat Drink 1 L",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["VEGAN"],
            brand="Oatly",
            allergens={"milk": "free", "nuts": "may_contain", "gluten": "contains"},
        ),
    )
    monkeypatch.setitem(
        fakes.OFFERS,
        "990001",
        (0.99, 500, "Stock", OFFERS["470889"][3]),
    )
    may_plan = gw.plan_need({
        "customer_id": "school-tampere",
        "product_query": "oat drink",
        "quantity": 50,
        "unit": "L",
        "delivery_by": "2026-11-18",
    })
    may_cands = {c["product"]["id"]: c for c in may_plan["candidates"]}
    assert may_cands["990001"]["tier"] == "rejected"
    assert "allergen_may_contain" in may_cands["990001"]["flags"]
    assert "990001" not in {ln["product_id"] for ln in may_plan["lines"]}

    q_may = gw.create_quote("2026-11-18", [{"product_id": "990001", "qty_sales_units": 10}])
    o_may = gw.place_order(q_may["quote_id"], q_may["quote_hash"])
    assert o_may["status"] == "rejected"
    assert o_may["decision"]["outcome"] == "reject"
    assert any(
        h["rule_id"] == "allergens:990001" and h["outcome"] == "reject" and "nuts" in h["message"].lower()
        for h in o_may["decision"]["hits"]
    )


def test_school_tampere_forced_dairy_quote_rejected_and_safe_alt_escalated() -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)

    for dairy_id in ("464306", "401201", "402105"):
        q_bad = gw.create_quote("2026-11-18", [{"product_id": dairy_id, "qty_sales_units": 5, "tier": "preferred"}])
        o_bad = gw.place_order(q_bad["quote_id"], q_bad["quote_hash"])
        assert o_bad["status"] == "rejected"
        assert o_bad["decision"]["outcome"] == "reject"
        assert o_bad["decision"]["approver_role"] is None
        assert any(
            h["rule_id"] == f"allergens:{dairy_id}"
            and h["outcome"] == "reject"
            and "milk" in h["message"].lower()
            for h in o_bad["decision"]["hits"]
        )

    for unk_id in ("469040", "471424"):
        q_unk = gw.create_quote("2026-11-18", [{"product_id": unk_id, "qty_sales_units": 5, "tier": "preferred"}])
        o_unk = gw.place_order(q_unk["quote_id"], q_unk["quote_hash"])
        assert o_unk["status"] == "rejected"
        assert o_unk["decision"]["outcome"] == "reject"
        assert any(
            h["rule_id"] == f"allergens:{unk_id}"
            and h["outcome"] == "reject"
            and "milk" in h["message"].lower()
            and "nuts" in h["message"].lower()
            for h in o_unk["decision"]["hits"]
        )

    q_flagged = gw.create_quote(
        "2026-11-18",
        [{"product_id": "470889", "qty_sales_units": 10, "tier": "preferred", "flags": ["allergen_safe_alternative"]}],
    )
    assert "allergen_safe_alternative" in q_flagged["lines"][0]["flags"]
    o_flagged = gw.place_order(q_flagged["quote_id"], q_flagged["quote_hash"])
    assert o_flagged["status"] == "pending_approval"
    assert o_flagged["decision"]["outcome"] == "needs_approval"
    assert o_flagged["decision"]["approver_role"] == "kitchen_manager"
    assert any(h["rule_id"] == "substitution.allergen_safe_alternative" for h in o_flagged["decision"]["hits"])

    q_detected = gw.create_quote(
        "2026-11-18",
        [{"product_id": "470889", "qty_sales_units": 10, "tier": "preferred"}],
        need={"product_query": "milk", "quantity": 10, "unit": "L", "delivery_by": "2026-11-18"},
    )
    assert "allergen_safe_alternative" in q_detected["lines"][0]["flags"]
    assert q_detected["need"]["excluded_allergens"] == ["milk", "nuts"]
    o_detected = gw.place_order(q_detected["quote_id"], q_detected["quote_hash"])
    assert o_detected["status"] == "pending_approval"
    assert o_detected["decision"]["outcome"] == "needs_approval"
    assert o_detected["decision"]["approver_role"] == "kitchen_manager"

    q_usual_oat = gw.create_quote(
        "2026-11-18",
        [{"product_id": "470889", "qty_sales_units": 10, "tier": "preferred"}],
        need={"product_query": "oat drink", "quantity": 10, "unit": "L", "delivery_by": "2026-11-18"},
    )
    assert "allergen_safe_alternative" not in q_usual_oat["lines"][0]["flags"]
    o_usual_oat = gw.place_order(q_usual_oat["quote_id"], q_usual_oat["quote_hash"])
    assert o_usual_oat["status"] == "placed"
    assert o_usual_oat["decision"]["outcome"] == "auto_approve"


def test_hospital_meilahti_bread_gluten_free_and_forced_gluten_quote_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeGateway("hospital-meilahti", "hospital-meilahti/buyer-1", now=SAT)

    monkeypatch.setitem(
        fakes.PRODUCTS,
        "990002",
        dict(
            name="Traditional Rye Bread Sliced 500 g",
            sales_unit="ST",
            content_amount=0.5,
            content_unit="KG",
            claims=["VEGAN"],
            brand="Fazer",
            category="bakery",
            allergens={"gluten": "contains", "milk": "free", "nuts": "free"},
        ),
    )
    monkeypatch.setitem(
        fakes.OFFERS,
        "990002",
        (1.50, 2000, "Stock", OFFERS["473328"][3]),
    )

    bread_plan = gw.plan_need({
        "product_query": "bread",
        "quantity": 10,
        "unit": "KG",
        "delivery_by": "2026-11-18",
    })
    assert bread_plan["need"]["excluded_allergens"] == ["gluten"]
    assert [ln["product_id"] for ln in bread_plan["lines"]] == ["473328"]
    assert PRODUCTS["473328"]["allergens"]["gluten"] == "free"
    assert "hospital-meilahti" in bread_plan["explanation"]
    assert "gluten" in bread_plan["explanation"]

    cands_by_id = {c["product"]["id"]: c for c in bread_plan["candidates"]}
    assert cands_by_id["990002"]["tier"] == "rejected"
    assert "allergen_contains" in cands_by_id["990002"]["flags"]

    rye_plan = gw.plan_need({
        "customer_id": "hospital-meilahti",
        "product_query": "rye bread",
        "quantity": 5,
        "unit": "KG",
        "delivery_by": "2026-11-18",
        "preferred_product_ids": ["473328"],
    })
    assert [ln["product_id"] for ln in rye_plan["lines"]] == ["473328"]
    assert "allergen_safe_alternative" in rye_plan["lines"][0]["flags"]

    for gluten_id in ("470863", "470889", "990002"):
        q_gluten = gw.create_quote("2026-11-18", [{"product_id": gluten_id, "qty_sales_units": 5, "tier": "preferred"}])
        o_gluten = gw.place_order(q_gluten["quote_id"], q_gluten["quote_hash"])
        assert o_gluten["status"] == "rejected"
        assert o_gluten["decision"]["outcome"] == "reject"
        assert any(
            h["rule_id"] == f"allergens:{gluten_id}"
            and h["outcome"] == "reject"
            and "gluten" in h["message"].lower()
            for h in o_gluten["decision"]["hits"]
        )

    q_unk_gluten = gw.create_quote("2026-11-18", [{"product_id": "469040", "qty_sales_units": 2, "tier": "preferred"}])
    o_unk_gluten = gw.place_order(q_unk_gluten["quote_id"], q_unk_gluten["quote_hash"])
    assert o_unk_gluten["status"] == "pending_approval"
    assert o_unk_gluten["decision"]["outcome"] == "needs_approval"
    assert o_unk_gluten["decision"]["approver_role"] == "dietitian"
    assert any(
        h["rule_id"] == "allergens:469040"
        and h["outcome"] == "needs_approval"
        and "gluten" in h["message"].lower()
        for h in o_unk_gluten["decision"]["hits"]
    )


def test_alias_normalization_list_policies_and_hotel_aurora_regression_parity() -> None:
    school_pol = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT).list_policies()
    assert any("milk, nuts" in r for r in school_pol["rules"])

    hosp_pol = FakeGateway("hospital-meilahti", "hospital-meilahti/buyer-1", now=SAT).list_policies()
    assert any("gluten" in r and "unknown=needs_approval" in r for r in hosp_pol["rules"])

    gw_hotel = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    hotel_pol = gw_hotel.list_policies()
    assert len(hotel_pol["rules"]) == 5
    assert not any("excluded allergens" in r for r in hotel_pol["rules"])

    alias_plan = gw_hotel.plan_need({
        "customer_id": "hotel-aurora",
        "product_query": "milk",
        "quantity": 30,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "excluded_allergens": ["dairy", "tree_nuts"],
    })
    assert alias_plan["need"]["excluded_allergens"] == ["milk", "nuts"]
    assert alias_plan["lines"]
    assert all(
        PRODUCTS[ln["product_id"]]["allergens"].get("milk") == "free"
        and PRODUCTS[ln["product_id"]]["allergens"].get("nuts") == "free"
        and "allergen_safe_alternative" in ln["flags"]
        for ln in alias_plan["lines"]
    )

    hotel_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "quantity": 1000,
        "unit": "L",
        "delivery_by": "2026-11-16",
        "preferred_product_ids": ["463038"],
    }
    hotel_plan = gw_hotel.plan_need(hotel_need)
    assert hotel_plan["need"] == hotel_need
    assert hotel_plan["explanation"] == ""
    assert [ln["product_id"] for ln in hotel_plan["lines"]] == ["464306"]
    assert hotel_plan["lines"][0]["qty_sales_units"] == 1000.0
    assert "allergen_safe_alternative" not in hotel_plan["lines"][0]["flags"]

    q_small = gw_hotel.create_quote(
        "2026-11-16",
        [{"product_id": "464306", "qty_sales_units": 20, "tier": "best"}],
        need={**hotel_need, "quantity": 20},
    )
    o_small = gw_hotel.place_order(q_small["quote_id"], q_small["quote_hash"])
    assert o_small["status"] == "placed"
    assert o_small["decision"]["outcome"] == "auto_approve"
    assert o_small["decision"]["hits"] == []

    q_hero = gw_hotel.create_quote(
        "2026-11-16",
        [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}],
        need=hotel_need,
    )
    o_hero = gw_hotel.place_order(q_hero["quote_id"], q_hero["quote_hash"])
    assert o_hero["status"] == "pending_approval"
    assert o_hero["decision"]["outcome"] == "needs_approval"
    assert o_hero["decision"]["approver_role"] == "procurement_manager"


def _run_cli_demo(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["demo", *args])
    return int(exc_info.value.code or 0)


def test_aimo_demo_transport_fake_school_hospital_and_hotel_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_keyless(monkeypatch)

    school_milk_json = tmp_path / "school_milk.json"
    rc_milk = _run_cli_demo([
        "--transport",
        "fake",
        "--customer",
        "school-tampere",
        "--text",
        "We need 50 litres of milk for Wednesday's school lunch.",
        "--approve-delay",
        "0.05",
        "--approval-timeout",
        "10",
        "--json",
        str(school_milk_json),
    ])
    assert rc_milk == 0
    res_milk = json.loads(school_milk_json.read_text(encoding="utf-8"))
    assert res_milk["status"] == "placed"
    assert res_milk["transport"] == "fake"
    assert {"milk", "nuts"} <= set(res_milk["quote"]["need"]["excluded_allergens"])
    for ln in res_milk["quote"]["lines"]:
        amap = PRODUCTS[ln["product_id"]]["allergens"]
        assert amap.get("milk") == "free"
        assert amap.get("nuts") == "free"

    gw_direct_milk = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    stop_m, decided_m, worker_m = _start_fake_approver(gw_direct_milk, "liisa@school-tampere.example")
    try:
        agent_m = BuyerAgent(
            FakeTransport(gw_direct_milk),
            "school-tampere",
            poll_s=0.03,
            now=SAT,
            clarify=False,
        )
        res_direct_milk = agent_m.run("We need 50 litres of milk for Wednesday's school lunch.")
        worker_m.join(timeout=2.0)
    finally:
        stop_m.set()
    assert res_direct_milk["status"] == "placed"
    assert [ln["product_id"] for ln in res_direct_milk["quote"]["lines"]] == ["470889"]
    assert "allergen_safe_alternative" in res_direct_milk["quote"]["lines"][0]["flags"]
    assert decided_m == [res_direct_milk["order"]["order_id"]]

    school_oat_json = tmp_path / "school_oat.json"
    rc_oat = run_demo.main([
        "--transport",
        "fake",
        "--customer",
        "school-tampere",
        "--text",
        "We need 80 litres of oat drink for Wednesday's school lunch.",
        "--approve-delay",
        "0.05",
        "--approval-timeout",
        "10",
        "--json",
        str(school_oat_json),
    ])
    assert rc_oat == 0
    res_oat = json.loads(school_oat_json.read_text(encoding="utf-8"))
    assert res_oat["status"] == "placed"
    assert {"milk", "nuts"} <= set(res_oat["quote"]["need"]["excluded_allergens"])
    assert all(
        PRODUCTS[ln["product_id"]]["allergens"].get("milk") == "free"
        and PRODUCTS[ln["product_id"]]["allergens"].get("nuts") == "free"
        for ln in res_oat["quote"]["lines"]
    )

    hosp_bread_json = tmp_path / "hosp_bread.json"
    rc_hosp = _run_cli_demo([
        "--transport",
        "fake",
        "--customer",
        "hospital-meilahti",
        "--text",
        "We need 10 kg of bread by Wednesday.",
        "--approve-delay",
        "0.05",
        "--approval-timeout",
        "10",
        "--json",
        str(hosp_bread_json),
    ])
    assert rc_hosp == 0
    res_hosp = json.loads(hosp_bread_json.read_text(encoding="utf-8"))
    assert res_hosp["status"] == "placed"
    assert "gluten" in res_hosp["quote"]["need"]["excluded_allergens"]
    assert [ln["product_id"] for ln in res_hosp["quote"]["lines"]] == ["473328"]
    assert res_hosp["order"]["approved_by"] == "sara@hospital-meilahti.example"

    hotel_json = tmp_path / "hotel_happy.json"
    rc_hotel = _run_cli_demo([
        "--transport",
        "fake",
        "--scenario",
        "happy",
        "--approve-delay",
        "0.05",
        "--approval-timeout",
        "10",
        "--json",
        str(hotel_json),
    ])
    assert rc_hotel == 0
    res_hotel = json.loads(hotel_json.read_text(encoding="utf-8"))
    assert res_hotel["status"] == "placed"
    assert [ln["product_id"] for ln in res_hotel["quote"]["lines"]] == ["464306"]
    assert res_hotel["order"]["approved_by"] == "anna@hotel-aurora.example"
