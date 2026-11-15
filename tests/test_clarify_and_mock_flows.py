from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types

import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.buyer import clarify, run_demo, runs
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import PRODUCTS, FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, MockModel, _tool_errors
from aimo.buyer.need import HELSINKI, parse_request
from aimo.buyer.profiles import get_profile, mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.gateway.human_auth import local_pin
from aimo.models import Need

ROOT = Path(__file__).resolve().parents[1]
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
MUTATION_TOOLS = {"plan_need", "create_quote", "place_order"}


class _OverrideTransport:
    kind = "fake"

    def __init__(self, base: FakeTransport, overrides: dict[str, Any]):
        self.base = base
        self.overrides = overrides
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self):
        return self.base.list_tools()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if name in self.overrides:
            handler = self.overrides[name]
            return handler(args) if callable(handler) else handler
        return self.base.call(name, args)

    def close(self) -> None:
        self.base.close()


def _set_api_keys(monkeypatch: pytest.MonkeyPatch, gemini_key: str, google_key: str) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


@pytest.fixture(autouse=True)
def keyless_forbid_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_api_keys(monkeypatch, "", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    gemini.client.cache_clear()

    def forbid_generate(*args, **kwargs):
        raise AssertionError("aimo.gemini.generate must never be called in keyless tests")

    def forbid_embed(*args, **kwargs):
        raise AssertionError("aimo.gemini.embed must never be called in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_generate)
    monkeypatch.setattr("aimo.gemini.embed", forbid_embed)


@pytest.fixture(scope="module")
def replay_catalog_env(tmp_path_factory: pytest.TempPathFactory):
    mp = pytest.MonkeyPatch()
    cat_tmp = tmp_path_factory.mktemp("clarify-replay-cat")
    orig_source = cat._SOURCE
    prev_con = None
    try:
        if not config.PRODUCT_JSON.exists():
            if os.environ.get("AIMO_CATALOG_FIXTURE", "").strip() == "0":
                pytest.skip("fixture catalog disabled via AIMO_CATALOG_FIXTURE=0")
            mp.setattr(config, "DATA_DIR", cat_tmp)
            mp.setattr(config, "PRODUCT_JSON", cat_tmp / "valio_aimo_junction26_product_data.json")
            mp.setattr(config, "RELIABILITY_CSV", cat_tmp / "valio_aimo_junction26_delivery_reliability.csv")
            mp.setattr(config, "CO2_CSV", cat_tmp / "valio_aimo_junction26_carbon_footprint.csv")
            mp.setattr(config, "CATALOG_DB", cat_tmp / "catalog.sqlite")
            mp.setenv("AIMO_OFFLINE", "0")
            mp.setattr(config, "OFFLINE", False)
            mp.setenv("AIMO_API_KEY", "")
            mp.setattr(config, "AIMO_API_KEY", "")
            mp.setenv("AIMO_REPLAY", "1")
            mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)
            src = cat._use_fixture_catalog()
            assert src == "fixture-subset"
            mp.setattr(cat, "_SOURCE", src)
            mp.setattr(catalog_store, "PRODUCT_JSON", config.PRODUCT_JSON)
            mp.setattr(catalog_store, "RELIABILITY_CSV", config.RELIABILITY_CSV)
            mp.setattr(catalog_store, "CO2_CSV", config.CO2_CSV)
            mp.setattr(catalog_store, "CATALOG_DB", config.CATALOG_DB)
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()
            prev_con = textsearch._con
        yield
    finally:
        if prev_con is not None:
            try:
                prev_con().close()
            except Exception:
                pass
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()
        mp.setattr(cat, "_SOURCE", orig_source)
        mp.undo()
        shutil.rmtree(cat_tmp, ignore_errors=True)


def _start_fake_approver(
    gw: FakeGateway,
    decision: str = "approve",
    approver: str = "liisa@school-tampere.example",
    preserve_approver_on_reject: bool = False,
):
    stop = threading.Event()
    decided: list[str] = []

    def worker():
        while not stop.is_set():
            for order in gw.pending():
                gw.decide(order["order_id"], decision, approver, f"test-{decision}")
                if preserve_approver_on_reject and decision == "reject":
                    order["approved_by"] = approver
                decided.append(order["order_id"])
                return
            stop.wait(0.01)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return stop, decided, t


def test_file_hygiene_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src
    for rel in ("aimo/buyer/clarify.py", "aimo/buyer/mock_model.py"):
        owned = (ROOT / rel).read_text(encoding="utf-8")
        assert "\x23" not in owned
        assert "\u2014" not in owned
        assert "\u2013" not in owned


@pytest.mark.parametrize(
    "vague_text",
    [
        "We need 50 litres of oat drink next week.",
        "Tarvitsemme 50 litraa kaurajuomaa ensi viikolla.",
        "Tarvitsemme 50 litraa kaurajuomaa ensi viikon tilaukseen.",
        "Vi behöver 50 liter havredryck nästa vecka.",
        "We need 10 litres of sunflower oil next week.",
    ],
)
def test_date_next_week_en_fi_sv_and_missing_date_questions(vague_text: str) -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    needs_nw = parse_request(vague_text, prof, SAT, use_gemini=False)
    qs_nw = clarify.find_questions(needs_nw, prof, vague_text, SAT, search_fn)
    assert len(qs_nw) == 1
    q_nw = qs_nw[0]
    assert q_nw["question_id"] == "q_date"
    assert q_nw["type"] == "date"
    assert q_nw["item_index"] is None
    assert q_nw["question"] == "Which day next week should it arrive?"
    assert [o["patch"]["delivery_by"] for o in q_nw["options"]] == [
        "2026-11-16",
        "2026-11-18",
        "2026-11-20",
    ]

    updated_nw, label_nw = clarify.apply_answer(needs_nw, q_nw, {"option_id": "d1"})
    assert label_nw == q_nw["options"][1]["label"]
    assert updated_nw[0].delivery_by == date(2026, 11, 18)
    assert updated_nw[0].preferences.get("clarified") == ["q_date"]


def test_missing_date_and_explicit_weekday_date_resolution() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    missing_date_text = "We need 50 litres of oat drink and 10 kg of gluten-free bread."
    needs_missing = parse_request(missing_date_text, prof, SAT, use_gemini=False)
    assert len(needs_missing) == 2
    assert all(n.preferences.get("delivery_date_assumed") is True for n in needs_missing)
    qs_missing = clarify.find_questions(needs_missing, prof, missing_date_text, SAT, search_fn)
    assert len(qs_missing) == 1
    q_missing = qs_missing[0]
    assert q_missing["question_id"] == "q_date"
    assert q_missing["type"] == "date"
    assert q_missing["item_index"] is None
    assert q_missing["question"] == "When do you need the delivery?"
    assert [o["patch"]["delivery_by"] for o in q_missing["options"]] == [
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
    ]

    updated_missing, label_missing = clarify.apply_answer(needs_missing, q_missing, {"option_id": "d2"})
    assert label_missing == q_missing["options"][2]["label"]
    assert len(updated_missing) == 2
    for item in updated_missing:
        assert item.delivery_by == date(2026, 11, 18)
        assert "delivery_date_assumed" not in item.preferences
        assert item.preferences.get("clarified") == ["q_date"]

    for explicit_text in (
        "We need 50 litres of oat drink next week on Wednesday.",
        "Tarvitsemme 50 litraa kaurajuomaa ensi viikon keskiviikoksi.",
        "Vi behöver 50 liter havredryck nästa vecka på onsdag.",
    ):
        needs_explicit = parse_request(explicit_text, prof, SAT, use_gemini=False)
        assert clarify.find_questions(needs_explicit, prof, explicit_text, SAT, search_fn) == []
    assert clarify.find_questions([], prof, "", SAT, search_fn) == []


def test_unit_pack_count_and_missing_quantity_en_fi_sv_questions() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    for pack_text in (
        "We need 20 packs of lactose-free milk by Monday.",
        "Tilaa 20 tölkkiä laktoositonta maitoa maanantaiksi.",
        "Vi behöver 20 förp laktosfri mjölk till måndag.",
    ):
        needs = parse_request(pack_text, prof, SAT, use_gemini=False)
        assert needs[0].unit == "PCS"
        assert needs[0].quantity == 20.0
        qs = clarify.find_questions(needs, prof, pack_text, SAT, search_fn)
        assert len(qs) == 1
        q = qs[0]
        assert q["question_id"] == "q_unit_0"
        assert q["type"] == "unit"
        assert q["item_index"] == 0
        assert len(q["options"]) >= 2
        assert all(o["product_id"] in PRODUCTS for o in q["options"])
        opt_10l = next(o for o in q["options"] if o["product_id"] == "463038")
        assert opt_10l["patch"] == {"unit": "L", "quantity": 200.0, "preferred_product_ids": ["463038"]}
        updated, label = clarify.apply_answer(needs, q, {"option_id": opt_10l["id"]})
        assert label == opt_10l["label"]
        assert updated[0].unit == "L"
        assert updated[0].quantity == 200.0
        assert updated[0].preferred_product_ids == ["463038"]
        assert updated[0].preferences.get("clarified") == ["q_unit_0"]

    for missing_qty_text, expected_unit in (
        ("We need oat drink by Wednesday.", "L"),
        ("Tarvitsemme kaurajuomaa keskiviikoksi.", "L"),
        ("Vi behöver laktosfritt smör till måndag.", "KG"),
        ("We need gluten-free bread by Monday.", "KG"),
    ):
        needs_mq = parse_request(missing_qty_text, prof, SAT, use_gemini=False)
        qs_mq = clarify.find_questions(needs_mq, prof, missing_qty_text, SAT, search_fn)
        assert len(qs_mq) == 1
        q_mq = qs_mq[0]
        assert q_mq["question_id"] == "q_unit_0"
        assert q_mq["type"] == "unit"
        assert q_mq["question"].startswith("How much ")
        assert all(o["patch"]["unit"] == expected_unit and o["patch"]["quantity"] > 0 for o in q_mq["options"])
        patched_mq, lbl_mq = clarify.apply_answer(needs_mq, q_mq, {"option_id": q_mq["options"][1]["id"]})
        assert lbl_mq == q_mq["options"][1]["label"]
        assert patched_mq[0].unit == expected_unit
        assert patched_mq[0].quantity == q_mq["options"][1]["patch"]["quantity"]

    hotel_prof = get_profile("hotel-aurora")
    hotel_mq_text = "We need lactose-free milk by Monday."
    hotel_needs_mq = parse_request(hotel_mq_text, hotel_prof, SAT, use_gemini=False)
    hotel_qs_mq = clarify.find_questions(hotel_needs_mq, hotel_prof, hotel_mq_text, SAT, search_fn)
    assert [q["question_id"] for q in hotel_qs_mq] == ["q_unit_0"]
    assert hotel_qs_mq[0]["options"][0]["patch"] == {"quantity": 300.0, "unit": "L"}
    assert "usual weekly order" in hotel_qs_mq[0]["options"][0]["label"]


def test_variant_dairy_fat_en_fi_sv_and_plant_exclusion() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    for variant_text in (
        "We need 50 litres of lactose-free milk by Monday.",
        "Tarvitsemme 50 litraa laktoositonta maitoa maanantaiksi.",
        "Vi behöver 50 liter laktosfri mjölk till måndag.",
    ):
        needs = parse_request(variant_text, prof, SAT, use_gemini=False)
        qs = clarify.find_questions(needs, prof, variant_text, SAT, search_fn)
        assert len(qs) == 1
        q = qs[0]
        assert q["question_id"] == "q_variant_0"
        assert q["type"] == "variant"
        assert q["item_index"] == 0
        assert 2 <= len(q["options"]) <= 4
        assert all(o["product_id"] in PRODUCTS for o in q["options"])
        labels = [o["label"] for o in q["options"]]
        assert any(lbl.startswith("semi-skimmed") for lbl in labels)
        assert any(lbl.startswith("fat-free") for lbl in labels)

        chosen_opt = next(o for o in q["options"] if o["label"].startswith("fat-free"))
        updated, label = clarify.apply_answer(needs, q, {"option_id": chosen_opt["id"]})
        assert label == chosen_opt["label"]
        assert chosen_opt["product_id"] in updated[0].preferred_product_ids
        assert "fat-free" in updated[0].product_query
        assert updated[0].preferences.get("clarified") == ["q_variant_0"]

    for clear_text in (
        "We need 50 litres of 1.5% lactose-free milk by Monday.",
        "Tarvitsemme 50 litraa laktoositonta rasvatonta maitoa maanantaiksi.",
        "Vi behöver 50 liter laktosfri lättmjölk till måndag.",
        "Vi behöver 50 liter laktosfri mellanmjölk till måndag.",
        "We need 50 litres of oat milk by Monday.",
        "Tarvitsemme 50 litraa kauramaitoa maanantaiksi.",
    ):
        needs_clear = parse_request(clear_text, prof, SAT, use_gemini=False)
        assert clarify.find_questions(needs_clear, prof, clear_text, SAT, search_fn) == []

    hotel_prof = get_profile("hotel-aurora")
    hero_text = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
    needs_hero = parse_request(hero_text, hotel_prof, SAT, use_gemini=False)
    assert clarify.find_questions(needs_hero, hotel_prof, hero_text, SAT, search_fn) == []


def test_conflict_milk_excluded_plant_substitute_claims_and_skip() -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    school_prof = get_profile("school-tampere")

    for conflict_text in (
        "We need 100 litres of milk by Wednesday.",
        "Tarvitsemme 100 litraa maitoa keskiviikoksi.",
        "Vi behöver 100 liter mjölk till onsdag.",
        "Tarvitsemme 40 litraa piimää keskiviikoksi.",
    ):
        c_needs = parse_request(conflict_text, school_prof, SAT, use_gemini=False)
        qs_c = clarify.find_questions(c_needs, school_prof, conflict_text, SAT, search_fn)
        assert [q["question_id"] for q in qs_c] == ["q_conflict_0"]
        assert qs_c[0]["type"] == "conflict"
        plant_opts = [o for o in qs_c[0]["options"] if o["id"] != "skip"]
        assert len(plant_opts) >= 1
        assert all(o["product_id"] in PRODUCTS for o in plant_opts)
        assert all(PRODUCTS[o["product_id"]]["allergens"].get("milk") != "contains" for o in plant_opts)

    lf_milk_text = "We need 50 litres of lactose-free milk by Wednesday."
    lf_needs = parse_request(lf_milk_text, school_prof, SAT, use_gemini=False)
    assert "FREE_FROM_LACTOSE" in lf_needs[0].required_claims
    lf_q = clarify.find_questions(lf_needs, school_prof, lf_milk_text, SAT, search_fn)[0]
    lf_sub, _ = clarify.apply_answer(lf_needs, lf_q, {"option_id": "p0"})
    assert lf_sub[0].product_query == "oat drink"
    assert "FREE_FROM_LACTOSE" not in lf_sub[0].required_claims

    single_text = "We need 100 litres of milk by Wednesday."
    single_needs = parse_request(single_text, school_prof, SAT, use_gemini=False)
    q_conf = clarify.find_questions(single_needs, school_prof, single_text, SAT, search_fn)[0]
    cancelled_single, skip_label = clarify.apply_answer(single_needs, q_conf, {"option_id": "skip"})
    assert skip_label == "Leave out the milk"
    assert len(cancelled_single) == 1
    assert cancelled_single[0].preferences.get("cancelled_by_clarification") is True
    assert cancelled_single[0].preferences.get("clarified") == ["q_conflict_0"]

    next_week_milk = "We need 100 litres of milk next week."
    nw_needs = parse_request(next_week_milk, school_prof, SAT, use_gemini=False)
    qs_nw = clarify.find_questions(nw_needs, school_prof, next_week_milk, SAT, search_fn)
    assert [q["question_id"] for q in qs_nw] == ["q_date", "q_conflict_0"]
    after_date, _ = clarify.apply_answer(nw_needs, qs_nw[0], {"option_id": "d0"})
    after_skip, _ = clarify.apply_answer(after_date, qs_nw[1], {"option_id": "skip"})
    assert len(after_skip) == 1
    assert after_skip[0].delivery_by == date(2026, 11, 16)
    assert after_skip[0].preferences.get("cancelled_by_clarification") is True
    assert after_skip[0].preferences.get("clarified") == ["q_date", "q_conflict_0"]

    two_conf_text = "We need 50 litres of milk and 10 kg of butter by Wednesday."
    two_conf_needs = parse_request(two_conf_text, school_prof, SAT, use_gemini=False)
    qs_two = clarify.find_questions(two_conf_needs, school_prof, two_conf_text, SAT, search_fn)
    assert [q["question_id"] for q in qs_two] == ["q_conflict_0", "q_conflict_1"]
    butter_opts = [o for o in qs_two[1]["options"] if o["id"] != "skip"]
    assert all(PRODUCTS[o["product_id"]]["allergens"].get("milk") != "contains" for o in butter_opts)
    step1_needs, _ = clarify.apply_answer(two_conf_needs, qs_two[0], {"option_id": "skip"})
    step2_needs, step2_lbl = clarify.apply_answer(step1_needs, qs_two[1], {"option_id": "skip"})
    assert step2_lbl == "Leave out the butter"
    assert step2_needs[0].preferences.get("cancelled_by_clarification") is True

    three_ambig = "We need 20 packs of milk and 10 kg of butter next week."
    three_needs = parse_request(three_ambig, school_prof, SAT, use_gemini=False)
    qs_capped = clarify.find_questions(three_needs, school_prof, three_ambig, SAT, search_fn)
    assert len(qs_capped) == clarify.MAX_QUESTIONS == 2


@pytest.mark.parametrize(
    ("customer_id", "request_text", "expected_q_types"),
    [
        ("cafe-x", "We need 50 litres of oat drink next week.", ["date"]),
        ("cafe-x", "We need oat drink by Wednesday.", ["unit"]),
        ("cafe-x", "We need 20 packs of lactose-free milk by Monday.", ["unit"]),
        ("cafe-x", "We need 50 litres of lactose-free milk by Monday.", ["variant"]),
        ("school-tampere", "We need 50 litres of lactose-free milk by Wednesday.", ["conflict"]),
    ],
)
def test_keyless_answer_auto_vs_answer_none_across_all_four_triggers(
    customer_id: str,
    request_text: str,
    expected_q_types: list[str],
) -> None:
    for none_mode in ("demo_none", "none_fn", "raises_error"):
        gw_none = FakeGateway(customer_id, f"{customer_id}/buyer-1", now=SAT)
        if none_mode == "demo_none":
            ans_fn = run_demo._answer_fn("none")
        elif none_mode == "none_fn":
            ans_fn = None
        else:
            def ans_fn(_q):
                raise RuntimeError("simulated callback error")

        agent_none = BuyerAgent(
            FakeTransport(gw_none),
            customer_id,
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=ans_fn,
        )
        res_none = agent_none.run(request_text)
        assert res_none["mock"] is True
        assert res_none["status"] == "needs_clarification"
        assert res_none["plan"] is None
        assert res_none["quote"] is None
        assert res_none["order"] is None
        assert len(res_none["clarifications"]) == 1
        assert res_none["clarifications"][0]["type"] == expected_q_types[0]
        assert res_none["clarifications"][0]["answer"] is None
        step_kinds = [s["kind"] for s in res_none["steps"]]
        assert "needs_clarification" in step_kinds
        assert step_kinds[-1] == "final"
        assert not any(name in MUTATION_TOOLS for name, _ in gw_none.calls)

    gw_auto = FakeGateway(customer_id, f"{customer_id}/buyer-1", now=SAT)
    stop, _, worker = _start_fake_approver(gw_auto, decision="approve", approver=f"approver@{customer_id}.example")
    try:
        agent_auto = BuyerAgent(
            FakeTransport(gw_auto),
            customer_id,
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=run_demo._answer_fn("auto"),
        )
        res_auto = agent_auto.run(request_text)
        worker.join(timeout=1.0)
    finally:
        stop.set()

    assert res_auto["mock"] is True
    assert res_auto["status"] == "placed"
    assert [c["type"] for c in res_auto["clarifications"]] == expected_q_types
    assert all(c["answer"] == {"option_id": c["options"][0]["id"]} for c in res_auto["clarifications"])
    assert res_auto["quote"] is not None
    assert res_auto["order"]["status"] == "placed"
    assert res_auto["summary"].startswith(MOCK_LABEL)


def test_apply_answer_free_text_options_dates_quantities_and_variants() -> None:
    gw_school = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    search_school = lambda q, c: gw_school.search_products(q, c)
    school_prof = get_profile("school-tampere")

    milk_text = "We need 100 litres of milk by Wednesday."
    milk_needs = parse_request(milk_text, school_prof, SAT, use_gemini=False)
    q_conf = clarify.find_questions(milk_needs, school_prof, milk_text, SAT, search_school)[0]

    sub_by_text, lbl_sub = clarify.apply_answer(milk_needs, q_conf, {"text": "oatly oat drink semi"})
    assert "Oatly Oat Drink Semi 1L" in lbl_sub
    assert sub_by_text[0].product_query == "oat drink"
    assert sub_by_text[0].preferred_product_ids == ["470863"]

    for cancel_phrase in (
        "Please Leave out the milk from this order",
        "skip",
        "jätä pois",
        "hoppa över",
    ):
        skipped, lbl_skip = clarify.apply_answer(milk_needs, q_conf, {"text": cancel_phrase})
        assert lbl_skip == "Leave out the milk"
        assert skipped[0].preferences.get("cancelled_by_clarification") is True

    assert clarify.apply_answer(milk_needs, q_conf, {"text": "   "}) == (milk_needs, "")
    assert clarify.apply_answer(milk_needs, q_conf, {"text": "deliver on Friday morning"}) == (milk_needs, "")
    assert clarify.apply_answer(milk_needs, q_conf, None) == (milk_needs, "")
    assert clarify.apply_answer(milk_needs, q_conf, {"option_id": "nonexistent"}) == (milk_needs, "")

    gw_cafe = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_cafe = lambda q, c: gw_cafe.search_products(q, c)
    cafe_prof = get_profile("cafe-x")

    pack_text = "We need 20 packs of lactose-free milk by Monday."
    pack_needs = parse_request(pack_text, cafe_prof, SAT, use_gemini=False)
    q_pack = clarify.find_questions(pack_needs, cafe_prof, pack_text, SAT, search_cafe)[0]
    u_1l, _ = clarify.apply_answer(pack_needs, q_pack, {"text": "1 L"})
    assert u_1l[0].quantity == 20.0
    assert u_1l[0].unit == "L"
    u_10l, _ = clarify.apply_answer(pack_needs, q_pack, {"text": "10 L"})
    assert u_10l[0].quantity == 200.0
    assert u_10l[0].unit == "L"
    u_custom, lbl_u_custom = clarify.apply_answer(pack_needs, q_pack, {"text": "150 litres"})
    assert lbl_u_custom == "free text: 150 litres"
    assert u_custom[0].quantity == 150.0
    assert u_custom[0].unit == "L"

    mq_text = "We need oat drink by Wednesday."
    mq_needs = parse_request(mq_text, cafe_prof, SAT, use_gemini=False)
    q_mq = clarify.find_questions(mq_needs, cafe_prof, mq_text, SAT, search_cafe)[0]
    mq_bare, lbl_mq_bare = clarify.apply_answer(mq_needs, q_mq, {"text": "75"})
    assert lbl_mq_bare == "free text: 75"
    assert mq_bare[0].quantity == 75.0
    assert mq_bare[0].unit == "L"

    var_text = "We need 50 litres of lactose-free milk by Monday."
    var_needs = parse_request(var_text, cafe_prof, SAT, use_gemini=False)
    q_var = clarify.find_questions(var_needs, cafe_prof, var_text, SAT, search_cafe)[0]
    for semi_txt in ("1.5%", "kevytmaito", "lättmjölk"):
        v_semi, v_lbl = clarify.apply_answer(var_needs, q_var, {"text": semi_txt})
        assert v_lbl.startswith("semi-skimmed")
        assert v_semi[0].preferred_product_ids
    for skim_txt in ("0%", "rasvaton", "fettfri"):
        v_skim, v_lbl = clarify.apply_answer(var_needs, q_var, {"text": skim_txt})
        assert v_lbl.startswith("fat-free")
        assert v_skim[0].preferred_product_ids == ["462775"]

    nw_text = "We need 50 litres of oat drink next week."
    nw_needs = parse_request(nw_text, cafe_prof, SAT, use_gemini=False)
    q_nw = clarify.find_questions(nw_needs, cafe_prof, nw_text, SAT, search_cafe)[0]
    d_by_id_txt, _ = clarify.apply_answer(nw_needs, q_nw, {"text": "d2"})
    assert d_by_id_txt[0].delivery_by == date(2026, 11, 20)
    for thu_txt in ("by Thursday", "torstaina", "på torsdag"):
        d_thu, lbl_thu = clarify.apply_answer(nw_needs, q_nw, {"text": thu_txt})
        assert lbl_thu == f"free text: {thu_txt}"
        assert d_thu[0].delivery_by == date(2026, 11, 19)


def test_keyless_buyer_free_text_e2e_flows() -> None:
    gw1 = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    agent1 = BuyerAgent(
        FakeTransport(gw1),
        "school-tampere",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
        answer_fn=lambda q: {"text": "Oatly Oat Drink Semi 1L"},
    )
    res1 = agent1.run("We need 50 litres of milk by Wednesday.")
    assert res1["mock"] is True
    assert agent1.model == MOCK_MODEL_NAME
    assert res1["status"] == "placed"
    assert res1["clarifications"][0]["resolved"] == "Plant-based instead: Oatly Oat Drink Semi 1L"
    assert res1["need"]["product_query"] == "oat drink"
    assert [ln["product_id"] for ln in res1["quote"]["lines"]] == ["470863"]

    gw2 = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    stop2, _, worker2 = _start_fake_approver(gw2, decision="approve", approver="liisa@school-tampere.example")
    try:
        agent2 = BuyerAgent(
            FakeTransport(gw2),
            "school-tampere",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=lambda q: {"text": "by Thursday"},
        )
        res2 = agent2.run("We need 50 litres of oat drink.")
        worker2.join(timeout=1.0)
    finally:
        stop2.set()

    assert res2["mock"] is True
    assert res2["status"] == "placed"
    assert res2["clarifications"][0]["resolved"] == "free text: by Thursday"
    assert res2["need"]["delivery_by"] == "2026-11-19"
    assert "delivery_date_assumed" not in res2["need"]["preferences"]
    assert res2["order"]["status"] == "placed"


@pytest.mark.parametrize(
    "bad_qty_text",
    ["0", "0.0", "-5", "0 L", "0 litres", "-5 litres", "0 litraa", "-10 kg", "0 kpl", "-2 förp"],
)
def test_non_positive_quantities_never_patched_into_valid_orders(bad_qty_text: str) -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    pack_text = "We need 20 packs of lactose-free milk by Monday."
    pack_needs = parse_request(pack_text, prof, SAT, use_gemini=False)
    q_pack = clarify.find_questions(pack_needs, prof, pack_text, SAT, search_fn)[0]

    patched, lbl = clarify.apply_answer(pack_needs, q_pack, {"text": bad_qty_text})
    assert "rejected non-positive quantity" in lbl
    assert all(n.quantity > 0 for n in patched)
    assert all(n.preferences.get("cancelled_by_clarification") is True for n in patched)
    assert all(n.preferences.get("non_positive_quantity") is True for n in patched)

    gw_e2e = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw_e2e),
        "cafe-x",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
        answer_fn=lambda q: {"text": bad_qty_text},
    )
    res = agent.run(pack_text)
    assert res["mock"] is True
    assert res["status"] == "no_order"
    assert res["plan"] is None
    assert res["quote"] is None
    assert res["order"] is None
    assert not any(name in MUTATION_TOOLS for name, _ in gw_e2e.calls)
    assert res["summary"].startswith(MOCK_LABEL)
    assert "non-positive quantity rejected" in res["summary"]


def test_non_positive_option_patch_and_constructed_need_guards() -> None:
    prof = get_profile("cafe-x")
    needs = parse_request("We need oat drink by Wednesday.", prof, SAT, use_gemini=False)
    bad_q = {
        "question_id": "q_unit_0",
        "type": "unit",
        "item_index": 0,
        "question": "How much oat drink do you need?",
        "options": [
            {"id": "zero", "label": "0 L", "patch": {"quantity": 0.0, "unit": "L"}},
            {"id": "neg", "label": "-10 L", "patch": {"quantity": -10.0, "unit": "L"}},
        ],
    }
    for oid in ("zero", "neg"):
        out, lbl = clarify.apply_answer(needs, bad_q, {"option_id": oid})
        assert lbl == "rejected non-positive quantity"
        assert out[0].quantity > 0
        assert out[0].preferences.get("cancelled_by_clarification") is True
        assert out[0].preferences.get("non_positive_quantity") is True

    raw_zero = Need.model_construct(
        raw_text="0 packs milk",
        customer_id="cafe-x",
        product_query="lactose-free milk",
        required_claims=[],
        excluded_allergens=[],
        quantity=0.0,
        unit="PCS",
        delivery_by=date(2026, 11, 16),
        preferred_product_ids=[],
        preferences={},
    )
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    assert clarify.find_questions([raw_zero], prof, "20 packs milk", SAT, lambda q, c: gw.search_products(q, c)) == []


def test_keyless_single_and_multi_item_skip_and_http_runs_e2e(
    replay_catalog_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    seen_questions: list[dict] = []
    agent = BuyerAgent(
        FakeTransport(gw),
        "school-tampere",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
        answer_fn=lambda q: seen_questions.append(q) or {"option_id": "skip"},
    )
    res = agent.run("We need 100 litres of milk by Wednesday.")
    assert [q["question_id"] for q in seen_questions] == ["q_conflict_0"]
    assert res["status"] == "no_order"
    assert res["plan"] is None
    assert res["quote"] is None
    assert res["order"] is None
    assert not any(name in MUTATION_TOOLS for name, _ in gw.calls)
    assert res["summary"].startswith(MOCK_LABEL)
    assert "cancelled per buyer clarification" in res["summary"]
    assert "no orderable lines" not in res["summary"]

    db_file = tmp_path / "state.sqlite"
    monkeypatch.setattr(store, "DB_PATH", db_file)
    monkeypatch.setattr(config, "STATE_DB", db_file)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_REPLAY", "1")

    rc = replay.ReplayClient(replay.BUNDLED_DIR)
    bc.set_client(rc)
    transport = InProcessTransport("buyer-1", "school-tampere", mandate=mandate_for("school-tampere"))
    try:
        res_inproc = BuyerAgent(
            transport,
            "school-tampere",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=lambda q: {"option_id": "skip"},
        ).run("We need 100 litres of milk by Wednesday.")
    finally:
        transport.close()
        bc.set_client(None)
        con = store._conns.pop(str(db_file), None)
        if con is not None:
            con.close()

    assert res_inproc["status"] == "no_order"
    assert "cancelled per buyer clarification" in res_inproc["summary"]

    app = FastAPI()
    app.include_router(runs.router)
    gw_http = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    orig_start = runs.registry.start
    runs.registry.start = lambda req, **kw: orig_start(
        req,
        transport_factory=lambda r: FakeTransport(gw_http),
        image=kw.get("image"),
        agent_kwargs={"poll_s": 0.01, "approval_timeout_s": 2.0, "now": SAT},
    )
    try:
        client = TestClient(app)
        http_headers = {"X-Customer-Id": "school-tampere", "X-Approver-Pin": local_pin()}
        run_id = client.post(
            "/v1/agent/runs",
            headers=http_headers,
            json={"text": "We need 100 litres of milk by Wednesday.", "customer_id": "school-tampere"},
        ).json()["run_id"]
        view = {}
        for _ in range(200):
            view = client.get(f"/v1/agent/runs/{run_id}", headers=http_headers).json()
            if view.get("pending_question"):
                break
            time.sleep(0.01)
        assert view.get("status") == "waiting_for_answer"
        ans_resp = client.post(
            f"/v1/agent/runs/{run_id}/answer",
            headers=http_headers,
            json={"option_id": "skip", "customer_id": "school-tampere"},
        )
        assert ans_resp.status_code == 200
        final_view = {}
        for _ in range(200):
            final_view = client.get(f"/v1/agent/runs/{run_id}", headers=http_headers).json()
            if final_view.get("status") in ("finished", "error"):
                break
            time.sleep(0.01)
        assert final_view["status"] == "finished"
        assert final_view["order_status"] == "no_order"
        assert "cancelled per buyer clarification" in final_view["summary"]
    finally:
        runs.registry.start = orig_start

    gw_multi = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    stop_m, decided_m, worker_m = _start_fake_approver(gw_multi, decision="approve", approver="liisa@school-tampere.example")
    try:
        res_multi = BuyerAgent(
            FakeTransport(gw_multi),
            "school-tampere",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
            answer_fn=lambda q: {"option_id": "skip"},
        ).run("We need 100 litres of milk and 50 litres of oat drink by Wednesday.")
        worker_m.join(timeout=1.0)
    finally:
        stop_m.set()

    assert res_multi["status"] == "placed"
    assert decided_m == [res_multi["order"]["order_id"]]
    assert sum(1 for name, _ in gw_multi.calls if name == "plan_need") == 1
    assert [ln["product_id"] for ln in res_multi["quote"]["lines"]] == ["470863"]

    gw_both = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    asked_both: list[str] = []
    res_both = BuyerAgent(
        FakeTransport(gw_both),
        "school-tampere",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
        answer_fn=lambda q: asked_both.append(q["question_id"]) or {"option_id": "skip"},
    ).run("We need 50 litres of milk and 10 kg of butter by Wednesday.")
    assert asked_both == ["q_conflict_0", "q_conflict_1"]
    assert res_both["status"] == "no_order"
    assert not any(name in MUTATION_TOOLS for name, _ in gw_both.calls)


def test_mock_model_multi_item_merged_quote_single_order_and_duplicate_pid_sum() -> None:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
    )
    res = agent.run("We need 40 litres of lactose-free whipping cream and 20 kg of lactose-free butter by Monday.")
    assert res["mock"] is True
    assert res["status"] == "placed"
    assert len(res["needs"]) == 2
    assert sum(1 for name, _ in gw.calls if name == "plan_need") == 2
    assert sum(1 for name, _ in gw.calls if name == "create_quote") == 1
    assert sum(1 for name, _ in gw.calls if name == "place_order") == 1
    quoted_pids = {ln["product_id"] for ln in res["quote"]["lines"]}
    assert quoted_pids == {"401201", "402105"}
    assert res["order"]["status"] == "placed"
    assert res["summary"].startswith(MOCK_LABEL)

    school_prof = get_profile("school-tampere")
    needs = parse_request(
        "We need 100 litres of milk and 50 litres of oat drink and 30 litres of oat drink by Wednesday.",
        school_prof,
        SAT,
        use_gemini=False,
    )
    cancelled_milk = needs[0].model_copy(
        update={"preferences": {**needs[0].preferences, "cancelled_by_clarification": True}}
    )
    oat1 = needs[1].model_copy(update={"delivery_by": date(2026, 11, 19)})
    oat2 = needs[2].model_copy(update={"delivery_by": date(2026, 11, 18)})
    stub_agent = SimpleNamespace(
        needs=[cancelled_milk, oat1, oat2],
        need=cancelled_milk,
        plans={
            0: {"lines": [{"product_id": "464306", "qty_sales_units": 100.0, "tier": "best"}], "shortfall": 0.0},
            1: {
                "lines": [
                    {"product_id": "470863", "qty_sales_units": 50.0, "tier": "better"},
                    {"product_id": "470863", "qty_sales_units": 0.0, "tier": "better"},
                ],
                "shortfall": 0.0,
            },
            2: {"lines": [{"product_id": "470863", "qty_sales_units": 30.0, "tier": "better"}], "shortfall": 0.0},
        },
        plan=None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    mm = MockModel(stub_agent)
    after_plans = [
        types.Content(role="user", parts=[types.Part(text="run")]),
        types.Content(
            role="user",
            parts=[
                types.Part(function_response=types.FunctionResponse(name="plan_need", response=stub_agent.plans[1])),
                types.Part(function_response=types.FunctionResponse(name="plan_need", response=stub_agent.plans[2])),
            ],
        ),
    ]
    quote_step = mm("buyer.agent_step", MOCK_MODEL_NAME, after_plans)
    fc_q = quote_step.candidates[0].content.parts[0].function_call
    assert fc_q.name == "create_quote"
    assert fc_q.args["delivery_date"] == "2026-11-18"
    assert fc_q.args["lines"] == [{"product_id": "470863", "qty_sales_units": 80.0, "tier": "better"}]


def test_mock_model_rejected_blocked_cancelled_timed_out_and_drift_flows() -> None:
    gw_pol_rej = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res_pol_rej = BuyerAgent(
        FakeTransport(gw_pol_rej),
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
    ).run("We need 2,000 kg of lactose-free butter by Monday.")
    assert res_pol_rej["status"] == "rejected"
    assert sum(1 for name, _ in gw_pol_rej.calls if name == "place_order") == 1
    assert res_pol_rej["summary"].startswith(MOCK_LABEL)
    assert "status rejected" in res_pol_rej["summary"]
    assert "rejected by policy" in res_pol_rej["summary"]
    assert "approved by" not in res_pol_rej["summary"]

    gw_hum_rej = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    stop_r, decided_r, worker_r = _start_fake_approver(
        gw_hum_rej,
        decision="reject",
        approver="anna@hotel-aurora.example",
        preserve_approver_on_reject=True,
    )
    try:
        agent_hum_rej = BuyerAgent(
            FakeTransport(gw_hum_rej),
            "hotel-aurora",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
        )
        res_hum_rej = agent_hum_rej.run("We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
        worker_r.join(timeout=1.0)
    finally:
        stop_r.set()

    assert res_hum_rej["status"] == "rejected"
    assert agent_hum_rej.blocked is True
    assert decided_r == [res_hum_rej["order"]["order_id"]]
    assert sum(1 for name, _ in gw_hum_rej.calls if name == "place_order") == 1
    assert "rejected by anna@hotel-aurora.example" in res_hum_rej["summary"]
    assert "approved by" not in res_hum_rej["summary"]

    gw_timeout = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res_timeout = BuyerAgent(
        FakeTransport(gw_timeout),
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=0.05,
        now=SAT,
    ).run("We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
    assert res_timeout["status"] == "pending_approval"
    assert res_timeout["order"]["timed_out"] is True
    assert "approval timed out" in res_timeout["summary"]

    gw_drift = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    res_drift = BuyerAgent(
        FakeTransport(gw_drift),
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
    ).run("Standing order for next week: 4,000 litres of lactose-free milk by Monday.")
    assert res_drift["status"] == "needs_confirmation"
    assert res_drift["order"] is None
    assert res_drift["quote"] is not None
    assert "held for human confirmation" in res_drift["summary"]
    assert "tool error" not in res_drift["summary"]

    blocked_stub = SimpleNamespace(
        needs=[],
        need=None,
        plans={},
        plan=None,
        quote=None,
        order={"order_id": "O-canc", "status": "cancelled"},
        blocked=True,
        drift=None,
        injections=[],
    )
    mm_blocked = MockModel(blocked_stub)
    resp_blocked = mm_blocked(
        "buyer.agent_step",
        MOCK_MODEL_NAME,
        [types.Content(role="user", parts=[types.Part(text="run")])],
    )
    txt_blocked = resp_blocked.candidates[0].content.parts[0].text
    assert txt_blocked.startswith(MOCK_LABEL)
    assert "status cancelled" in txt_blocked
    assert "order cancelled" in txt_blocked


def test_mock_model_zero_orderable_lines_shortfall_and_non_positive_qty_errors() -> None:
    gw_full_sf = FakeGateway("hospital-meilahti", "hospital-meilahti/buyer-1", now=SAT)
    res_full_sf = BuyerAgent(
        FakeTransport(gw_full_sf),
        "hospital-meilahti",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
    ).run("We need 100 litres of oat drink by Wednesday.")
    assert res_full_sf["status"] == "no_order"
    assert res_full_sf["quote"] is None
    assert res_full_sf["order"] is None
    assert res_full_sf["plan"]["lines"] == []
    assert res_full_sf["plan"]["shortfall"] == 100.0
    assert "create_quote" not in [name for name, _ in gw_full_sf.calls]
    assert "place_order" not in [name for name, _ in gw_full_sf.calls]
    assert "shortfall 100" in res_full_sf["summary"]
    assert "no orderable lines" in res_full_sf["summary"]

    gw_part_sf = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    stop_p, _, worker_p = _start_fake_approver(gw_part_sf, decision="approve", approver="buyer@cafe-x.example")
    try:
        res_part_sf = BuyerAgent(
            FakeTransport(gw_part_sf),
            "cafe-x",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
        ).run("We need 6,000 litres of 1.5% lactose-free milk by Monday.")
        worker_p.join(timeout=1.0)
    finally:
        stop_p.set()

    assert res_part_sf["plan"]["shortfall"] > 0
    assert "shortfall" in res_part_sf["summary"]

    gw_err = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    t_err = _OverrideTransport(
        FakeTransport(gw_err),
        {"plan_need": lambda _a: {"detail": "quantity must be greater than zero"}},
    )
    res_err = BuyerAgent(
        t_err,
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=2.0,
        now=SAT,
    ).run("We need 40 litres of lactose-free whipping cream and 20 kg of lactose-free butter by Monday.")
    assert res_err["status"] == "no_order"
    assert sum(1 for name, _ in t_err.calls if name == "plan_need") == 1
    assert "create_quote" not in [name for name, _ in t_err.calls]
    assert "tool error (plan_need: quantity must be greater than zero)" in res_err["summary"]

    assert _tool_errors(
        [
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="plan_need",
                            response={"isError": True, "text": "quantity <= 0"},
                        )
                    )
                ],
            )
        ]
    ) == ["plan_need: tool_error"]

    bad_need = Need.model_construct(
        raw_text="0 L milk",
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        required_claims=[],
        excluded_allergens=[],
        quantity=0.0,
        unit="L",
        delivery_by=date(2026, 11, 16),
        preferred_product_ids=[],
        preferences={},
    )
    stub_non_pos = SimpleNamespace(
        needs=[bad_need],
        need=bad_need,
        plans={},
        plan=None,
        quote=None,
        order=None,
        blocked=False,
        drift=None,
        injections=[],
    )
    mm_non_pos = MockModel(stub_non_pos)
    step_np = mm_non_pos(
        "buyer.agent_step",
        MOCK_MODEL_NAME,
        [types.Content(role="user", parts=[types.Part(text="run")])],
    )
    assert step_np.candidates[0].content.parts[0].function_call is None
    assert "non-positive quantity rejected" in step_np.candidates[0].content.parts[0].text


@pytest.mark.parametrize(
    "detail_req",
    [
        "We need 200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday; please check product details.",
        "We need 200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday; inspect alternatives.",
        "We need 200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday; check ingredient details.",
    ],
)
def test_mock_model_get_product_inspection_on_detail_alternative_ingredient_requests(detail_req: str) -> None:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="injection", now=SAT)
    stop, _, worker = _start_fake_approver(gw, decision="approve", approver="anna@hotel-aurora.example")
    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            poll_s=0.01,
            approval_timeout_s=2.0,
            now=SAT,
        )
        res = agent.run(detail_req)
        worker.join(timeout=1.0)
    finally:
        stop.set()

    assert res["mock"] is True
    assert res["status"] == "placed"
    tool_seq = [name for name, _ in gw.calls]
    assert "plan_need" in tool_seq
    get_prod_idxs = [idx for idx, name in enumerate(tool_seq) if name == "get_product"]
    quote_idx = tool_seq.index("create_quote")
    assert 1 <= len(get_prod_idxs) <= 2
    assert all(idx < quote_idx for idx in get_prod_idxs)
    inspected_ids = [args["product_id"] for name, args in gw.calls if name == "get_product"]
    assert len(inspected_ids) == len(set(inspected_ids))
    assert "469363" in inspected_ids
    assert "suspicious supplier text was detected and ignored" in res["summary"]
    assert res["summary"].startswith(MOCK_LABEL)
