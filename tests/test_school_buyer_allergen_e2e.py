from __future__ import annotations

import gzip
import json
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import catalog_store, clock, orders as store, replay
from aimo.backend import client as client_mod
from aimo.backend.peppol import NS_CBC, check_t116_rules, order_response_xml
from aimo.buyer import run_demo, runs as buyer_runs
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import PRODUCTS as FAKE_PRODUCTS, FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport, MCPTransport
from aimo.catalog import allergens as allergens_mod, textsearch
from aimo.gateway import service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Quote
from aimo.trust import policy as trust_policy, signing

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
FIXTURE_CAT_DIR = REPLAY_DIR / "catalog"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
SAT_ISO = "2026-11-14T15:00:00+02:00"
SCHOOL = "school-tampere"
SCHOOL_APPROVER = "liisa@school-tampere.example"
SCHOOL_ROLE = "kitchen_manager"
APPROVER_PIN = "test-pin-school-12ch"


def _fc(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ]
    )


def _txt(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


def _fn_responses(contents: list[Any]) -> list[str]:
    return [
        p.function_response.name
        for c in contents
        for p in (getattr(c, "parts", None) or [])
        if getattr(p, "function_response", None)
    ]


def _read_sse_events(resp: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur: dict[str, str] = {}
    for raw in resp.iter_lines():
        line = raw.strip("\r")
        if not line:
            if "data" in cur:
                payload = json.loads(cur["data"])
                events.append({"id": cur.get("id"), "event": cur.get("event"), "data": payload})
                if cur.get("event") == "final" or payload.get("kind") == "final":
                    return events
            cur = {}
            continue
        if line.startswith(":"):
            continue
        field, _, val = line.partition(":")
        cur[field] = val.lstrip(" ")
    if "data" in cur:
        events.append({"id": cur.get("id"), "event": cur.get("event"), "data": json.loads(cur["data"])})
    return events


def _assert_structured_free(product_id: str, groups: tuple[str, ...] = ("milk", "nuts")) -> None:
    prod = service.get_product(product_id)
    for g in groups:
        st = prod.allergens[g]
        val = st.value if hasattr(st, "value") else str(st)
        src = str(prod.allergen_source.get(g, ""))
        assert val == "free", f"{product_id} expected {g}=free, got {val} ({src})"
        assert src.startswith("structured"), f"{product_id} expected structured source for {g}, got {src}"


def _assert_non_rejected_candidates_structured_free(
    candidates: list[dict[str, Any]],
    groups: tuple[str, ...] = ("milk", "nuts"),
) -> None:
    for cand in candidates:
        if cand.get("tier") == "rejected":
            continue
        pid = str((cand.get("product") or {}).get("id") or "")
        assert pid
        _assert_structured_free(pid, groups)


def _verify_quote_peppol_and_audit(quote_dict: dict[str, Any], order_id: str, expected_peppol_code: str) -> None:
    q_obj = Quote.model_validate(quote_dict)
    sig_detail = signing.verify_quote_detailed(q_obj, now=clock.now())
    assert sig_detail["ok"] is True, sig_detail
    assert sig_detail["signature_valid"] is True
    assert sig_detail["hash_matches"] is True
    assert sig_detail["expired"] is False
    assert q_obj.signature and len(q_obj.signature.split(".")) == 3

    ord_obj = service.get_order(order_id)
    xml_text = order_response_xml(ord_obj, service.get_quote(ord_obj.quote_id))
    assert check_t116_rules(xml_text) == []
    root = ET.fromstring(xml_text.encode("utf-8"))
    code = root.findtext("cbc:OrderResponseCode", None, {"cbc": NS_CBC})
    assert code is not None and code.strip() == expected_peppol_code

    trail = service.get_audit_trail(order_id)
    assert trail["verify"]["ok"] is True
    assert trail["verify"].get("broken_at") is None
    assert len(trail["events"]) >= 3


def _start_inproc_approver(
    customer_id: str = SCHOOL,
    approver: str = SCHOOL_APPROVER,
    approver_role: str = SCHOOL_ROLE,
    decision: str = "approve",
) -> tuple[threading.Event, list[str], list[Exception], threading.Thread]:
    stop = threading.Event()
    decided: list[str] = []
    errors: list[Exception] = []
    decide_fn = getattr(service, "decide_approval", service.decide)

    def worker() -> None:
        while not stop.is_set():
            try:
                pending = service.list_approvals("pending", customer_id)
                for item in pending:
                    order = item.get("order", item)
                    if order.get("status") == "pending_approval":
                        decide_fn(
                            order["order_id"],
                            decision=decision,
                            approver=approver,
                            quote_hash=order["quote_hash"],
                            note=f"school e2e {decision}",
                            approver_role=approver_role,
                        )
                        decided.append(order["order_id"])
                        return
            except Exception as exc:
                errors.append(exc)
                return
            stop.wait(0.02)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return stop, decided, errors, thread


@pytest.fixture(scope="module")
def _replay_catalog_static(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path, Path, Path]:
    cat_dir = tmp_path_factory.mktemp("school_buyer_replay_catalog")
    names = (
        "valio_aimo_junction26_product_data.json",
        "valio_aimo_junction26_delivery_reliability.csv",
        "valio_aimo_junction26_carbon_footprint.csv",
    )
    for name in names:
        gz = FIXTURE_CAT_DIR / f"{name}.gz"
        assert gz.is_file(), f"missing replay fixture {gz}"
        (cat_dir / name).write_bytes(gzip.decompress(gz.read_bytes()))

    pjson = cat_dir / names[0]
    rel_csv = cat_dir / names[1]
    co2_csv = cat_dir / names[2]
    cat_db = cat_dir / "catalog.sqlite"

    orig = (
        catalog_store.PRODUCT_JSON,
        catalog_store.RELIABILITY_CSV,
        catalog_store.CO2_CSV,
        catalog_store.CATALOG_DB,
    )
    catalog_store.PRODUCT_JSON = pjson
    catalog_store.RELIABILITY_CSV = rel_csv
    catalog_store.CO2_CSV = co2_csv
    catalog_store.CATALOG_DB = cat_db
    try:
        catalog_store.load_catalog(cat_db, force=True)
    finally:
        (
            catalog_store.PRODUCT_JSON,
            catalog_store.RELIABILITY_CSV,
            catalog_store.CO2_CSV,
            catalog_store.CATALOG_DB,
        ) = orig

    return cat_dir, pjson, rel_csv, co2_csv, cat_db


@pytest.fixture
def replay_gateway_env(
    _replay_catalog_static: tuple[Path, Path, Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    _, pjson, rel_csv, co2_csv, cat_db = _replay_catalog_static
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)

    gemini_calls: list[str] = []
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_NOW", SAT_ISO)
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(config, "CO2_CSV", co2_csv)
    monkeypatch.setattr(config, "CATALOG_DB", cat_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(cat, "_SOURCE", "fixture-subset")

    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(catalog_store, "CO2_CSV", co2_csv)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cat_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])

    def forbid_gemini(*args: Any, **kwargs: Any) -> None:
        gemini_calls.append("called")
        raise AssertionError("Gemini network call must not execute in keyless school buyer e2e tests")

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    prev_client = client_mod._client
    client_mod.set_client(replay.ReplayClient(REPLAY_DIR))
    trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    try:
        yield gemini_calls
    finally:
        client_mod.set_client(prev_client)
        trust_policy._load_policy_cached.cache_clear()
        allergens_mod._last_policy_dir = None
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def test_file_conventions_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_school_buyer_omitted_allergens_inproc_auto_approve_ed25519_peppol_and_audit(
    replay_gateway_env: list[str],
) -> None:
    transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))
    try:
        agent = BuyerAgent(transport, SCHOOL, poll_s=0.03, approval_timeout_s=5.0, now=SAT)
        res = agent.run("We need 60 litres of oat drink for Wednesday's lunch.")
    finally:
        transport.close()

    assert res["mock"] is True
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
    assert res["status"] == "placed"
    assert res["summary"].startswith(MOCK_LABEL)
    assert {m["model"] for m in res["models_used"]} == {MOCK_MODEL_NAME}

    plan = res["plan"]
    assert plan and plan["lines"]
    assert "excluding milk, nuts" in plan["explanation"]
    assert [ln["product_id"] for ln in plan["lines"]] == ["470889"]
    assert plan["lines"][0]["tier"] == "preferred"
    for ln in plan["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    _assert_non_rejected_candidates_structured_free(plan["candidates"], ("milk", "nuts"))

    quote = res["quote"]
    assert quote and quote["total_net"] <= 300.0
    assert {"milk", "nuts"} <= set(quote["need"]["excluded_allergens"])
    for ln in quote["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))

    order = res["order"]
    assert order and order["status"] == "placed"
    assert order["decision"]["outcome"] == "auto_approve"
    assert "approval_wait" not in [s["kind"] for s in res["steps"]]

    _verify_quote_peppol_and_audit(quote, order["order_id"], "AP")
    assert replay_gateway_env == []


def test_school_buyer_omitted_allergens_mcp_transport_split_and_kitchen_manager_approval(
    replay_gateway_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_CUSTOMER_ID", SCHOOL)
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    transport = MCPTransport(build_mcp(), timeout=30.0)
    stop, decided, errors, worker = _start_inproc_approver()
    try:
        raw_mcp_plan = transport.call(
            "plan_need",
            {
                "need": {
                    "customer_id": SCHOOL,
                    "product_query": "milk",
                    "quantity": 40,
                    "unit": "L",
                    "delivery_by": "2026-11-18",
                }
            },
        )
        assert {"milk", "nuts"} <= set(raw_mcp_plan["need"]["excluded_allergens"])
        assert "Customer policy (school-tampere) excludes milk, nuts" in raw_mcp_plan["explanation"]
        for ln in raw_mcp_plan["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))
        _assert_non_rejected_candidates_structured_free(raw_mcp_plan["candidates"], ("milk", "nuts"))

        agent = BuyerAgent(transport, SCHOOL, poll_s=0.03, approval_timeout_s=8.0, now=SAT)
        res = agent.run("We need 300 litres of oat drink for Wednesday's school lunch.")
        worker.join(timeout=3.0)
    finally:
        stop.set()
        transport.close()

    assert errors == []
    assert res["status"] == "placed"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])

    plan = res["plan"]
    assert plan and len(plan["lines"]) >= 2
    assert round(plan["covered_amount"], 4) >= 300.0
    assert plan["shortfall"] == 0.0
    for ln in plan["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    _assert_non_rejected_candidates_structured_free(plan["candidates"], ("milk", "nuts"))

    quote = res["quote"]
    assert quote and quote["total_net"] > 300.0
    assert len(quote["lines"]) >= 2
    for ln in quote["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))

    assert "approval_wait" in [s["kind"] for s in res["steps"]]
    wait_steps = [s for s in res["steps"] if s["kind"] == "approval_wait"]
    assert wait_steps[0]["data"]["approver_role"] == SCHOOL_ROLE

    order = res["order"]
    assert order["status"] == "placed"
    assert order["approved_by"] == SCHOOL_APPROVER
    assert decided == [order["order_id"]]

    _verify_quote_peppol_and_audit(quote, order["order_id"], "AP")
    assert replay_gateway_env == []


def test_school_buyer_fake_transport_locks_excluded_allergens_even_when_omitted(
    replay_gateway_env: list[str],
) -> None:
    gw = FakeGateway(SCHOOL, "school-tampere/buyer-1", scenario="happy", now=SAT)
    stop = threading.Event()

    def approve_fake() -> None:
        while not stop.is_set():
            for pending in gw.pending():
                gw.decide(pending["order_id"], "approve", SCHOOL_APPROVER, "approved school fake run")
                return
            stop.wait(0.02)

    t = threading.Thread(target=approve_fake, daemon=True)
    t.start()
    try:
        agent = BuyerAgent(FakeTransport(gw), SCHOOL, poll_s=0.03, approval_timeout_s=5.0, now=SAT)
        res = agent.run("We need 150 litres of oat drink for Wednesday's lunch.")
        t.join(timeout=2.0)
    finally:
        stop.set()

    assert res["status"] == "placed"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
    plan_calls = [args["need"] for name, args in gw.calls if name == "plan_need"]
    assert len(plan_calls) == 1
    assert {"milk", "nuts"} <= set(plan_calls[0]["excluded_allergens"])

    line_ids = {ln["product_id"] for ln in res["plan"]["lines"]}
    assert line_ids == {"470863"}
    assert "469040" not in line_ids
    assert "471424" not in line_ids
    for pid in line_ids:
        assert FAKE_PRODUCTS[pid]["allergens"].get("milk") == "free"
        assert FAKE_PRODUCTS[pid]["allergens"].get("nuts") == "free"

    by_cand = {c["product"]["id"]: c for c in res["plan"]["candidates"]}
    for unsafe_or_unk in ("469040", "471424"):
        assert by_cand[unsafe_or_unk]["tier"] == "rejected"


def test_school_buyer_dairy_conflict_clarification_halt_swap_and_skip(
    replay_gateway_env: list[str],
) -> None:
    transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))
    try:
        unanswered = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=2.0,
            now=SAT,
            clarify=True,
            answer_fn=None,
        ).run("We need 50 litres of milk for Wednesday's lunch.")
        assert unanswered["status"] == "needs_clarification"
        assert unanswered["quote"] is None
        assert unanswered["order"] is None
        clarify_steps = [s for s in unanswered["steps"] if s["kind"] == "needs_clarification"]
        assert len(clarify_steps) == 1
        assert clarify_steps[0]["data"]["type"] == "conflict"
        assert "milk" in clarify_steps[0]["title"].lower()

        def pick_first_plant_option(q: dict[str, Any]) -> dict[str, str]:
            plant_opts = [o for o in q["options"] if o["id"] != "skip"]
            assert plant_opts
            return {"option_id": plant_opts[0]["id"]}

        stop, _, errors, worker = _start_inproc_approver()
        try:
            swapped = BuyerAgent(
                transport,
                SCHOOL,
                poll_s=0.03,
                approval_timeout_s=5.0,
                now=SAT,
                clarify=True,
                answer_fn=pick_first_plant_option,
            ).run("We need 50 litres of milk for Wednesday's lunch.")
            worker.join(timeout=2.0)
        finally:
            stop.set()
        assert errors == []
        assert swapped["status"] == "placed"
        assert swapped["plan"] and swapped["plan"]["lines"]
        for ln in swapped["plan"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))
        for ln in swapped["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))

        skipped = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=5.0,
            now=SAT,
            clarify=True,
            answer_fn=lambda _q: {"option_id": "skip"},
        ).run("We need 80 litres of oat drink and 30 litres of milk for Wednesday's lunch.")
        assert skipped["status"] == "placed"
        assert len(skipped["needs"]) == 1
        assert "oat" in skipped["needs"][0]["product_query"].lower()
        assert skipped["plan"]["lines"]
        for ln in skipped["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    finally:
        transport.close()


def test_school_buyer_headless_milk_swaps_to_safe_oat_while_hotel_orders_dairy(
    replay_gateway_env: list[str],
) -> None:
    school_transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))
    stop, decided, errors, worker = _start_inproc_approver()
    try:
        school_res = BuyerAgent(
            school_transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=8.0,
            now=SAT,
            clarify=False,
        ).run("We need 50 litres of milk for Wednesday's lunch.")
        worker.join(timeout=3.0)
    finally:
        stop.set()
        school_transport.close()

    assert errors == []
    assert school_res["status"] == "placed"
    assert {"milk", "nuts"} <= set(school_res["need"]["excluded_allergens"])
    assert [ln["product_id"] for ln in school_res["plan"]["lines"]] == ["470889"]
    for ln in school_res["quote"]["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    _assert_non_rejected_candidates_structured_free(school_res["plan"]["candidates"], ("milk", "nuts"))
    assert decided == [school_res["order"]["order_id"]]
    _verify_quote_peppol_and_audit(school_res["quote"], school_res["order"]["order_id"], "AP")

    hotel_transport = InProcessTransport("buyer-1", "hotel-aurora", mandate=mandate_for("hotel-aurora"))
    h_stop, h_decided, h_errors, h_worker = _start_inproc_approver(
        customer_id="hotel-aurora",
        approver="anna@hotel-aurora.example",
        approver_role="procurement_manager",
    )
    try:
        hotel_res = BuyerAgent(
            hotel_transport,
            "hotel-aurora",
            poll_s=0.03,
            approval_timeout_s=5.0,
            now=SAT,
            clarify=False,
        ).run("We need 50 litres of milk for Wednesday's lunch.")
        h_worker.join(timeout=2.0)
    finally:
        h_stop.set()
        hotel_transport.close()

    assert h_errors == []
    assert hotel_res["status"] == "placed"
    assert hotel_res["need"]["excluded_allergens"] == []
    assert hotel_res["quote"]["lines"]
    hotel_allergens = [
        service.get_product(ln["product_id"]).allergens["milk"] for ln in hotel_res["quote"]["lines"]
    ]
    assert any((st.value if hasattr(st, "value") else str(st)) == "contains" for st in hotel_allergens)


@pytest.mark.parametrize(
    "request_text",
    [
        "We need 50 litres of lactose-free milk for Wednesday's lunch.",
        "We need 20 litres of whipping cream for Wednesday's lunch.",
        "We need 15 kg of butter for Wednesday's lunch.",
        "We need 20 litres of hazelnut oat drink for Wednesday's lunch.",
    ],
)
def test_school_buyer_forbidden_dairy_and_nut_queries_produce_shortfall_and_zero_orders(
    replay_gateway_env: list[str],
    request_text: str,
) -> None:
    transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))
    try:
        res = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=2.0,
            now=SAT,
            clarify=False,
        ).run(request_text)
    finally:
        transport.close()

    assert res["status"] == "no_order"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
    assert res["plan"] is not None
    assert res["plan"]["lines"] == []
    assert res["plan"]["shortfall"] > 0
    assert res["quote"] is None
    assert res["order"] is None
    _assert_non_rejected_candidates_structured_free(res["plan"]["candidates"], ("milk", "nuts"))

    called_tools = [
        s["data"]["name"]
        for s in res["steps"]
        if s["kind"] == "tool_call" and not s["data"].get("blocked")
    ]
    assert "plan_need" in called_tools
    assert "create_quote" not in called_tools
    assert "place_order" not in called_tools


def test_school_buyer_caller_allergen_union_soy_and_gluten_with_standing_policy(
    replay_gateway_env: list[str],
) -> None:
    transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))
    try:
        soy_res = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=5.0,
            now=SAT,
        ).run("We need 50 litres of oat drink for Wednesday's school lunch. A pupil also has a soy allergy.")
        assert soy_res["status"] == "placed"
        assert {"soy", "milk", "nuts"} <= set(soy_res["need"]["excluded_allergens"])
        assert soy_res["quote"]["lines"]
        for ln in soy_res["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts", "soy"))
        _assert_non_rejected_candidates_structured_free(soy_res["plan"]["candidates"], ("milk", "nuts", "soy"))

        gf_res = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=5.0,
            now=SAT,
        ).run("We need 10 kg of gluten-free bread for Wednesday's school lunch.")
        assert gf_res["status"] == "placed"
        assert {"gluten", "milk", "nuts"} <= set(gf_res["need"]["excluded_allergens"])
        assert "FREE_FROM_GLUTEN" in gf_res["need"]["required_claims"]
        assert gf_res["quote"]["lines"]
        for ln in gf_res["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("gluten", "milk", "nuts"))
        _assert_non_rejected_candidates_structured_free(gf_res["plan"]["candidates"], ("gluten", "milk", "nuts"))
    finally:
        transport.close()


def test_adversarial_gemini_model_tampering_blocked_by_buyer_harness_and_planner(
    replay_gateway_env: list[str],
) -> None:
    transport = InProcessTransport("buyer-1", SCHOOL, mandate=mandate_for(SCHOOL))

    def rogue_swap_model(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        if isinstance(contents, str):
            return _txt(f"Rogue {feature}")
        done = _fn_responses(contents)
        if "plan_need" not in done:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "oat drink",
                        "excluded_allergens": [],
                        "preferred_product_ids": ["464306"],
                        "quantity": 1,
                        "unit": "L",
                        "delivery_by": "2026-11-18",
                    }
                },
            )
        if "create_quote" not in done:
            return _fc(
                "create_quote",
                {
                    "delivery_date": "2026-11-18",
                    "lines": [{"product_id": "464306", "qty_sales_units": 60, "tier": "preferred"}],
                },
            )
        if "place_order" not in done:
            for item in reversed(contents):
                for part in getattr(item, "parts", None) or []:
                    fr = getattr(part, "function_response", None)
                    if fr and fr.name == "create_quote":
                        q = fr.response
                        return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Finished.")

    try:
        res = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=5.0,
            now=SAT,
            generate_fn=rogue_swap_model,
            use_gemini_parse=False,
        ).run("We need 60 litres of oat drink for Wednesday's lunch.")

        assert res["mock"] is False
        assert res["status"] == "placed"
        plan_tool_calls = [
            s["data"]["args"]["need"]
            for s in res["steps"]
            if s["kind"] == "tool_call" and s["data"].get("name") == "plan_need"
        ]
        assert len(plan_tool_calls) == 1
        assert plan_tool_calls[0]["customer_id"] == SCHOOL
        assert {"milk", "nuts"} <= set(plan_tool_calls[0]["excluded_allergens"])
        assert plan_tool_calls[0]["quantity"] == 60.0

        quoted_ids = [ln["product_id"] for ln in res["quote"]["lines"]]
        assert quoted_ids == ["470889"]
        assert "464306" not in quoted_ids
        correction_thoughts = [
            s for s in res["steps"] if s["kind"] == "thought" and "corrected to the planner's lines" in s["title"]
        ]
        assert len(correction_thoughts) == 1

        butter_attempts = {"quote": 0}

        def rogue_shortfall_model(feature: str, model: str, contents: Any, config: Any = None) -> Any:
            if isinstance(contents, str):
                return _txt(f"Rogue {feature}")
            done = _fn_responses(contents)
            if "plan_need" not in done:
                return _fc(
                    "plan_need",
                    {
                        "need": {
                            "product_query": "butter",
                            "excluded_allergens": [],
                            "quantity": 15,
                            "unit": "KG",
                            "delivery_by": "2026-11-18",
                        }
                    },
                )
            if butter_attempts["quote"] == 0:
                butter_attempts["quote"] += 1
                return _fc(
                    "create_quote",
                    {
                        "delivery_date": "2026-11-18",
                        "lines": [{"product_id": "464306", "qty_sales_units": 15}],
                    },
                )
            return _txt("No orderable items.")

        shortfall_res = BuyerAgent(
            transport,
            SCHOOL,
            poll_s=0.03,
            approval_timeout_s=2.0,
            now=SAT,
            clarify=False,
            generate_fn=rogue_shortfall_model,
            use_gemini_parse=False,
        ).run("We need 15 kg of butter for Wednesday's lunch.")

        assert shortfall_res["status"] == "no_order"
        assert shortfall_res["plan"]["lines"] == []
        assert shortfall_res["quote"] is None
        assert shortfall_res["order"] is None
        blocked_quotes = [
            s
            for s in shortfall_res["steps"]
            if s["kind"] == "tool_call" and s["data"].get("name") == "create_quote" and s["data"].get("blocked")
        ]
        assert len(blocked_quotes) == 1
        assert "full shortfall" in blocked_quotes[0]["data"]["blocked"]
    finally:
        transport.close()


def test_school_http_agent_runs_sse_clarify_answer_and_forced_unsafe_order_rejection(
    replay_gateway_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_reg = buyer_runs.RunRegistry()
    monkeypatch.setattr(buyer_runs, "registry", fresh_reg)
    orig_start = fresh_reg.start

    def inproc_start(req: buyer_runs.RunRequest, **kw: Any) -> buyer_runs.Run:
        return orig_start(
            req,
            transport_factory=lambda r: InProcessTransport("buyer-1", r.customer_id, mandate=mandate_for(r.customer_id)),
            agent_kwargs={"poll_s": 0.03, "approval_timeout_s": 8.0, "now": SAT},
            image=kw.get("image"),
            mcp_url=kw.get("mcp_url"),
        )

    monkeypatch.setattr(fresh_reg, "start", inproc_start)

    app = create_app(with_mcp=False)
    school_headers = {"X-Customer-Id": SCHOOL, "X-Agent-Id": "buyer-1"}
    run_headers = {**school_headers, "X-Approver-Pin": APPROVER_PIN}
    with TestClient(app) as tc:
        created = tc.post(
            "/v1/agent/runs",
            headers=run_headers,
            json={
                "text": "We need 60 litres of oat drink for Wednesday's lunch.",
                "customer_id": SCHOOL,
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        with tc.stream("GET", body["events_url"], headers=run_headers) as stream_resp:
            assert stream_resp.status_code == 200
            events = _read_sse_events(stream_resp)

        kinds = [e["event"] for e in events]
        for expected in ("thought", "tool_call", "plan", "quote", "decision", "order", "final"):
            assert expected in kinds, (expected, kinds)
        assert events[-1]["data"]["data"]["status"] == "placed"

        view = tc.get(f"/v1/agent/runs/{body['run_id']}", headers=run_headers).json()
        assert view["status"] == "finished"
        assert view["order_status"] == "placed"
        quote_steps = [s for s in view["steps"] if s["kind"] == "quote"]
        assert quote_steps
        for ln in quote_steps[0]["data"]["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))

        stop, _, errors, worker = _start_inproc_approver()
        try:
            conflict_created = tc.post(
                "/v1/agent/runs",
                headers=run_headers,
                json={
                    "text": "We need 40 litres of milk for Wednesday's lunch.",
                    "customer_id": SCHOOL,
                    "clarify": True,
                },
            )
            assert conflict_created.status_code == 200, conflict_created.text
            c_body = conflict_created.json()
            c_run_id = c_body["run_id"]

            pending_q = None
            for _ in range(100):
                cur_view = tc.get(f"/v1/agent/runs/{c_run_id}", headers=run_headers).json()
                if cur_view.get("pending_question"):
                    pending_q = cur_view["pending_question"]
                    break
                time.sleep(0.02)
            assert pending_q is not None
            assert pending_q["type"] == "conflict"
            plant_opt = next(o for o in pending_q["options"] if o["id"] != "skip")

            ans_resp = tc.post(
                c_body["answer_url"],
                headers=run_headers,
                json={
                    "customer_id": SCHOOL,
                    "question_id": pending_q["question_id"],
                    "option_id": plant_opt["id"],
                },
            )
            assert ans_resp.status_code == 200, ans_resp.text

            with tc.stream("GET", c_body["events_url"], headers=run_headers) as c_stream:
                assert c_stream.status_code == 200
                c_events = _read_sse_events(c_stream)
            worker.join(timeout=3.0)
        finally:
            stop.set()

        assert errors == []
        assert "needs_clarification" in [e["event"] for e in c_events]
        assert c_events[-1]["data"]["data"]["status"] == "placed"
        c_view = tc.get(f"/v1/agent/runs/{c_run_id}", headers=run_headers).json()
        c_quote_steps = [s for s in c_view["steps"] if s["kind"] == "quote"]
        assert c_quote_steps
        for ln in c_quote_steps[0]["data"]["quote"]["lines"]:
            _assert_structured_free(ln["product_id"], ("milk", "nuts"))

        forced_q = tc.post(
            "/v1/quotes",
            headers=school_headers,
            json={"delivery_date": "2026-11-18", "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
        )
        assert forced_q.status_code == 200, forced_q.text
        fq = forced_q.json()
        assert fq["lines"][0]["tier"] == "rejected"

        forced_o = tc.post(
            "/v1/orders",
            headers=school_headers,
            json={"quote_id": fq["quote_id"], "quote_hash": fq["quote_hash"]},
        )
        assert forced_o.status_code == 200, forced_o.text
        fo = forced_o.json()
        assert fo["status"] == "rejected"
        assert fo["decision"]["outcome"] == "reject"
        allergen_hits = [
            h
            for h in fo["decision"]["hits"]
            if h["rule_id"].startswith("allergens") and h["outcome"] == "reject"
        ]
        assert allergen_hits
        assert "milk" in allergen_hits[0]["message"].lower()

        _verify_quote_peppol_and_audit(fq, fo["order_id"], "RE")
        audit_ver = tc.get("/v1/audit/verify").json()
        assert audit_ver["ok"] is True
        assert audit_ver["broken_at"] is None


def test_run_demo_school_allergen_cli_inproc_replay_verdict_and_safety(
    replay_gateway_env: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aimo.buyer.agent.now_helsinki", lambda: SAT)
    out_json = tmp_path / "school_demo_inproc.json"
    rc = run_demo.main(
        [
            "--transport",
            "inproc",
            "--scenario",
            "school-allergen",
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "8",
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))
    ok, why = run_demo.verdict("school-allergen", res)
    assert ok is True, why
    assert res["transport"] == "inproc"
    assert res["customer_id"] == SCHOOL
    assert res["status"] == "placed"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
    for ln in res["quote"]["lines"]:
        _assert_structured_free(ln["product_id"], ("milk", "nuts"))
    _assert_non_rejected_candidates_structured_free(res["plan"]["candidates"], ("milk", "nuts"))
    _verify_quote_peppol_and_audit(res["quote"], res["order"]["order_id"], "AP")
    assert replay_gateway_env == []

