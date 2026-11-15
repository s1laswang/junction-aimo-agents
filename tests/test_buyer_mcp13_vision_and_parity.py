from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from google.genai import types

import aimo.backend.catalog_store as cs
import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import costs
from aimo.buyer.agent import BuyerAgent, short_description
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import get_profile, mandate_for
from aimo.buyer.tool_schemas import TOOL_NAMES, to_gemini_schema
from aimo.buyer.tools import FakeTransport, InProcessTransport, MCPTransport, _ensure_replay_ready
from aimo.buyer.vision import ImageOrder, extract_order_from_image
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.gateway.extras import EXTRA_TOOLS
from aimo.gateway.mcp_server import build_mcp
from aimo.trust.signing import verify_quote_detailed

ROOT = Path(__file__).resolve().parents[1]
ORDER_IMAGE_PATH = ROOT / "tests" / "fixtures" / "images" / "order_fi_milk_butter.png"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
TYPED_MILK_BUTTER = "We need 200 litres of lactose-free milk and 10 kg butter by Monday."

FORBIDDEN_SCHEMA_KEYS = {
    "$ref",
    "$defs",
    "definitions",
    "additionalProperties",
    "prefixItems",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "title",
    "default",
    "$schema",
    "$id",
    "$comment",
}


def _collect_all_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            found |= _collect_all_keys(v)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_all_keys(item)
    return found


def _collect_schema_keyword_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k == "properties" and isinstance(v, dict):
                for pv in v.values():
                    found |= _collect_schema_keyword_keys(pv)
            else:
                found |= _collect_schema_keyword_keys(v)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_schema_keyword_keys(item)
    return found


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


def _fn_responses(contents: list[Any], name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in contents:
        for p in getattr(c, "parts", None) or []:
            fr = getattr(p, "function_response", None)
            if fr and fr.name == name and isinstance(fr.response, dict):
                out.append(fr.response)
    return out


def _set_api_keys(monkeypatch: pytest.MonkeyPatch, gemini_key: str, google_key: str) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


@pytest.fixture
def keyless_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)
    _set_api_keys(monkeypatch, "", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini API must never be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    return ledger_dir


@pytest.fixture
def isolated_replay_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    ledger_dir = tmp_path / "ledger"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    _set_api_keys(monkeypatch, "", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.setenv("AIMO_CUSTOMER_ID", "hotel-aurora")
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)

    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(bc, "_client", None)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini API must never be called in keyless replay mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    _ensure_replay_ready()

    try:
        yield tmp_path
    finally:
        bc.set_client(None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            con.close()


def test_file_hygiene_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_mcp_transport_13_tools_convert_to_gemini_schemas_and_declarations() -> None:
    transport = MCPTransport(build_mcp(), timeout=30.0)
    try:
        specs = transport.list_tools()
        assert len(specs) == 13

        by_name = {s.name: s for s in specs}
        assert set(by_name) == set(TOOL_NAMES) | set(EXTRA_TOOLS)
        assert not [n for n in by_name if "approv" in n.lower() or "decid" in n.lower()]

        raw_plan_keys = _collect_all_keys(by_name["plan_need"].input_schema)
        raw_quote_keys = _collect_all_keys(by_name["create_quote"].input_schema)
        assert {"$defs", "$ref", "exclusiveMinimum"} <= raw_plan_keys
        assert {"$defs", "$ref", "exclusiveMinimum"} <= raw_quote_keys

        decls: list[types.FunctionDeclaration] = []
        converted_by_name: dict[str, dict[str, Any]] = {}
        for spec in specs:
            cleaned = to_gemini_schema(spec.input_schema)
            converted_by_name[spec.name] = cleaned
            assert cleaned.get("type") == "object"
            assert "properties" in cleaned

            leaked = _collect_all_keys(cleaned) & FORBIDDEN_SCHEMA_KEYS
            assert not leaked, f"{spec.name} leaked unsupported keys: {leaked}"

            json_schema = types.JSONSchema.model_validate(cleaned)
            sdk_schema = types.Schema.from_json_schema(json_schema=json_schema)
            assert sdk_schema is not None

            desc = short_description(spec.description)
            assert desc
            assert len(desc) <= 321
            decls.append(
                types.FunctionDeclaration(
                    name=spec.name,
                    description=desc,
                    parameters_json_schema=cleaned,
                )
            )

        plan_qty = converted_by_name["plan_need"]["properties"]["need"]["properties"]["quantity"]
        assert plan_qty["minimum"] == 0
        assert "exclusiveMinimum" not in plan_qty

        quote_qty = (
            converted_by_name["create_quote"]["properties"]["lines"]["items"]["properties"]["qty_sales_units"]
        )
        assert quote_qty["minimum"] == 0
        assert quote_qty["maximum"] == 1000000.0
        assert "exclusiveMinimum" not in quote_qty

        tool = types.Tool(function_declarations=decls)
        assert tool.function_declarations is not None
        assert len(tool.function_declarations) == 13
        cfg = types.GenerateContentConfig(tools=[tool])
        assert cfg.tools is not None and len(cfg.tools[0].function_declarations) == 13
    finally:
        transport.close()


def test_to_gemini_schema_sanitizes_exclusive_bounds_and_preserves_property_names() -> None:
    raw = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.test/schema.json",
        "$comment": "generated",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "qty": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 500.5, "multipleOf": 0.5},
            "portions": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 100},
            "exclusiveMinimum": {"type": "string", "title": "LiteralFieldName"},
        },
        "required": ["qty"],
    }
    cleaned = to_gemini_schema(raw)
    assert _collect_schema_keyword_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)
    assert cleaned["properties"]["qty"] == {"type": "number", "minimum": 0, "maximum": 500.5}
    assert cleaned["properties"]["portions"] == {"type": "integer", "minimum": 1, "maximum": 99}
    assert cleaned["properties"]["exclusiveMinimum"] == {"type": "string"}

    validated = types.JSONSchema.model_validate(cleaned)
    assert types.Schema.from_json_schema(json_schema=validated) is not None
    decl = types.FunctionDeclaration(name="bounds_test", description="Bounds test", parameters_json_schema=cleaned)
    assert decl.name == "bounds_test"


def test_keyless_buyer_agent_e2e_over_mcp_transport_in_replay_mode(isolated_replay_env: Path) -> None:
    stop = threading.Event()
    approved_orders: list[str] = []
    approver_errors: list[Exception] = []
    decide_fn = getattr(service, "decide_approval", service.decide)

    def approve_worker() -> None:
        while not stop.is_set():
            try:
                pending = service.list_approvals("pending", "hotel-aurora")
                for item in pending:
                    order = item["order"]
                    if order["status"] == "pending_approval":
                        decide_fn(
                            order["order_id"],
                            decision="approve",
                            approver="anna@hotel-aurora.example",
                            quote_hash=order["quote_hash"],
                            note="Approved in replay MCP test",
                            approver_role="procurement_manager",
                        )
                        approved_orders.append(order["order_id"])
                        return
            except Exception as exc:
                approver_errors.append(exc)
                return
            stop.wait(0.03)

    worker = threading.Thread(target=approve_worker, daemon=True)
    worker.start()

    transport = MCPTransport(build_mcp(), timeout=30.0)
    try:
        assert len(transport.list_tools()) == 13
        recipes_out = transport.call("list_recipes", {})
        assert isinstance(recipes_out, dict) and len(recipes_out.get("recipes") or []) >= 1
        drift_out = transport.call("check_standing_order_drift", {"customer_id": "hotel-aurora"})
        assert isinstance(drift_out, dict) and drift_out.get("customer_id") == "hotel-aurora"

        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.04, approval_timeout_s=10.0, now=SAT)
        assert agent.mock is True
        assert agent.model == MOCK_MODEL_NAME
        res = agent.run(HERO)
        worker.join(timeout=3.0)
        assert not approver_errors, approver_errors

        assert res["mock"] is True
        assert res["summary"].startswith(MOCK_LABEL)
        assert res["need"]["preferences"]["parsed_by"] == "regex"
        assert res["plan"] and len(res["plan"]["lines"]) >= 1
        line_pids = [ln["product_id"] for ln in res["plan"]["lines"]]
        assert "463038" not in line_pids
        assert "464306" in line_pids

        assert res["quote"] and res["quote"]["quote_id"] and res["quote"]["quote_hash"]
        stored_quote = service.get_quote(res["quote"]["quote_id"])
        sig_check = verify_quote_detailed(stored_quote)
        assert sig_check["ok"] is True
        assert sig_check["signature_valid"] is True

        replay_client = bc.get_client()
        assert replay_client.stats["replay_calls"] > 0
        assert replay_client.stats["upstream_calls"] == 0

        kinds = [s["kind"] for s in res["steps"]]
        for expected_kind in ("plan", "quote", "decision", "approval_wait", "order", "final"):
            assert expected_kind in kinds, expected_kind

        assert res["status"] == "placed"
        assert res["order"]["status"] == "placed"
        assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
        assert approved_orders == [res["order"]["order_id"]]

        trail = service.get_audit_trail(res["order"]["order_id"])
        assert trail["verify"]["ok"] is True
        assert costs.ledger_rows(run_ids=[res["run_id"]]) == []
    finally:
        stop.set()
        transport.close()


def test_mcp_and_inproc_transport_parity_in_replay_mode(isolated_replay_env: Path) -> None:
    req = "We need 200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
    stop = threading.Event()
    decided_ids: set[str] = set()
    approver_errors: list[Exception] = []
    decide_fn = getattr(service, "decide_approval", service.decide)

    def approve_all_worker() -> None:
        while not stop.is_set():
            try:
                for item in service.list_approvals("pending", "hotel-aurora"):
                    order = item["order"]
                    oid = order["order_id"]
                    if order["status"] == "pending_approval" and oid not in decided_ids:
                        decide_fn(
                            oid,
                            decision="approve",
                            approver="anna@hotel-aurora.example",
                            quote_hash=order["quote_hash"],
                            note="Parity test approval",
                            approver_role="procurement_manager",
                        )
                        decided_ids.add(oid)
            except Exception as exc:
                approver_errors.append(exc)
                return
            stop.wait(0.03)

    worker = threading.Thread(target=approve_all_worker, daemon=True)
    worker.start()

    inproc = InProcessTransport("buyer-1", "hotel-aurora", mandate=mandate_for("hotel-aurora"))
    mcp_t = MCPTransport(build_mcp(), timeout=30.0)
    try:
        r_inproc = BuyerAgent(inproc, "hotel-aurora", poll_s=0.03, approval_timeout_s=10.0, now=SAT).run(req)
        r_mcp = BuyerAgent(mcp_t, "hotel-aurora", poll_s=0.03, approval_timeout_s=10.0, now=SAT).run(req)
        assert not approver_errors, approver_errors

        assert r_inproc["mock"] is True and r_mcp["mock"] is True
        assert r_inproc["status"] == r_mcp["status"] == "placed"
        assert r_inproc["need"] == r_mcp["need"]

        inproc_lines = [
            (ln["product_id"], ln["qty_sales_units"], ln["unit_price"], ln["line_total"], ln["tier"])
            for ln in r_inproc["plan"]["lines"]
        ]
        mcp_lines = [
            (ln["product_id"], ln["qty_sales_units"], ln["unit_price"], ln["line_total"], ln["tier"])
            for ln in r_mcp["plan"]["lines"]
        ]
        assert inproc_lines == mcp_lines
        assert r_inproc["plan"]["total_net"] == pytest.approx(r_mcp["plan"]["total_net"])
        assert r_inproc["quote"]["total_net"] == pytest.approx(r_mcp["quote"]["total_net"])
        assert r_inproc["order"]["decision"]["outcome"] == r_mcp["order"]["decision"]["outcome"]
        assert verify_quote_detailed(service.get_quote(r_inproc["quote"]["quote_id"]))["ok"] is True
        assert verify_quote_detailed(service.get_quote(r_mcp["quote"]["quote_id"]))["ok"] is True
        assert costs.ledger_rows(run_ids=[r_inproc["run_id"], r_mcp["run_id"]]) == []
    finally:
        stop.set()
        worker.join(timeout=2.0)
        inproc.close()
        mcp_t.close()


def test_keyless_image_with_typed_text_places_order_without_gemini(keyless_env: Path) -> None:
    assert ORDER_IMAGE_PATH.is_file()
    img_bytes = ORDER_IMAGE_PATH.read_bytes()
    assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        image=(img_bytes, "image/png"),
        poll_s=0.03,
        now=SAT,
    )
    assert agent.mock is True
    res = agent.run(TYPED_MILK_BUTTER)

    assert res["mock"] is True
    assert res["status"] == "placed"
    assert res["summary"].startswith(MOCK_LABEL)
    assert len(res["needs"]) == 2
    assert res["order"] is not None and res["order"]["status"] == "placed"
    assert res["quote"] is not None and len(res["quote"]["lines"]) == 2
    quoted_by_pid = {ln["product_id"]: ln["qty_sales_units"] for ln in res["quote"]["lines"]}
    assert quoted_by_pid == {"464306": 200.0, "402105": 20.0}
    assert any(
        s["kind"] == "thought"
        and MOCK_LABEL in s["title"]
        and "using typed request text" in s["title"]
        and s["data"].get("has_image") is True
        for s in res["steps"]
    )
    assert costs.ledger_rows(run_ids=[res["run_id"]]) == []


@pytest.mark.parametrize("empty_text", ["", "   \n\t  "])
def test_keyless_image_with_empty_text_returns_no_order_and_mock_label(
    keyless_env: Path,
    empty_text: str,
) -> None:
    img_bytes = ORDER_IMAGE_PATH.read_bytes()
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        image=(img_bytes, "image/png"),
        poll_s=0.03,
        now=SAT,
    )
    res = agent.run(empty_text)

    assert res["mock"] is True
    assert res["status"] == "no_order"
    assert res["summary"].startswith(MOCK_LABEL)
    assert res["order"] is None
    assert res["quote"] is None
    assert res["plan"] is None
    assert res["needs"] == []
    assert gw.orders == {}
    assert gw.quotes == {}
    assert gw.calls == []
    assert any(s["kind"] == "error" and "GEMINI_API_KEY" in s["title"] for s in res["steps"])
    assert res["steps"][-1]["kind"] == "final"
    assert res["steps"][-1]["data"]["status"] == "no_order"
    assert costs.ledger_rows(run_ids=[res["run_id"]]) == []


def test_extract_order_from_image_retries_invalid_json_and_raises_after_two_bad_replies() -> None:
    img_bytes = ORDER_IMAGE_PATH.read_bytes()
    profile = get_profile("hotel-aurora")
    valid_payload = {
        "document_type": "handwritten_order",
        "language": "fi",
        "lines": [
            {
                "product": "lactose-free milk",
                "original_text": "Maito laktoositon 200 l",
                "quantity": 200,
                "unit": "l",
                "notes": "laktoositon",
            },
            {
                "product": "butter",
                "original_text": "Voi 10 kg",
                "quantity": 10,
                "unit": "kg",
                "notes": "",
            },
        ],
        "delivery_expression": "toimitus ma",
        "request_text": TYPED_MILK_BUTTER,
        "unreadable": [],
    }
    seen_calls: list[dict[str, Any]] = []

    def flaky_generate(**kw: Any) -> types.GenerateContentResponse:
        seen_calls.append(kw)
        if len(seen_calls) == 1:
            return _txt("{malformed json")
        return _txt(json.dumps(valid_payload))

    out = extract_order_from_image(
        img_bytes,
        "image/png",
        profile,
        SAT,
        run_id="run-vision-unit",
        generate=flaky_generate,
        hint="urgent breakfast prep",
    )
    assert len(seen_calls) == 2
    first = seen_calls[0]
    assert first["feature"] == "buyer.vision"
    assert first["meta"]["mime"] == "image/png"
    assert first["meta"]["bytes"] == len(img_bytes)
    assert first["contents"][0].inline_data.data == img_bytes
    assert first["contents"][0].inline_data.mime_type == "image/png"
    assert "urgent breakfast prep" in first["contents"][1]
    assert first["config"].response_mime_type == "application/json"
    assert first["config"].response_json_schema == ImageOrder.model_json_schema()
    assert out["document_type"] == "handwritten_order"
    assert len(out["lines"]) == 2
    assert out["request_text"] == TYPED_MILK_BUTTER

    bad_attempts = 0

    def always_bad_generate(**kw: Any) -> types.GenerateContentResponse:
        nonlocal bad_attempts
        bad_attempts += 1
        return _txt("not-json-at-all")

    with pytest.raises(Exception):
        extract_order_from_image(img_bytes, "image/png", profile, SAT, generate=always_bad_generate)
    assert bad_attempts == 2


def test_gemini_vision_intake_retries_invalid_json_and_completes_multi_item_order() -> None:
    img_bytes = ORDER_IMAGE_PATH.read_bytes()
    extracted = {
        "document_type": "handwritten_order",
        "language": "fi",
        "lines": [
            {
                "product": "lactose-free milk",
                "original_text": "Maito laktoositon 200 l",
                "quantity": 200,
                "unit": "l",
                "notes": "laktoositon",
            },
            {
                "product": "butter",
                "original_text": "Voi 10 kg",
                "quantity": 10,
                "unit": "kg",
                "notes": "",
            },
        ],
        "delivery_expression": "toimitus ma",
        "request_text": TYPED_MILK_BUTTER,
        "unreadable": [],
    }

    call_features: list[str] = []
    vision_attempts = 0

    def stub_generate(feature: str, model: str, contents: Any, config: Any = None, **_: Any) -> Any:
        nonlocal vision_attempts
        call_features.append(feature)
        if feature == "buyer.vision":
            vision_attempts += 1
            assert contents[0].inline_data.data == img_bytes
            assert contents[0].inline_data.mime_type == "image/png"
            if vision_attempts == 1:
                return _txt("{invalid-json-on-first-attempt")
            return _txt(json.dumps(extracted))
        if feature in ("buyer.explain", "buyer.summary"):
            return _txt(f"Grounded {feature} output.")
        done = [
            p.function_response.name
            for c in contents
            for p in (getattr(c, "parts", None) or [])
            if getattr(p, "function_response", None)
        ]
        if done.count("plan_need") == 0:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "lactose-free milk",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "quantity": 200,
                        "unit": "L",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
        if done.count("plan_need") == 1:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "butter",
                        "quantity": 10,
                        "unit": "KG",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
        if "create_quote" not in done:
            plans = _fn_responses(contents, "plan_need")
            merged_lines = [
                {"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln["tier"]}
                for pl in plans
                for ln in pl.get("lines", [])
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": merged_lines})
        if "place_order" not in done:
            quote = _fn_responses(contents, "create_quote")[-1]
            return _fc("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
        return _txt("Placed multi-item order from handwritten Finnish order slip.")

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        generate_fn=stub_generate,
        use_gemini_parse=False,
        poll_s=0.03,
        now=SAT,
        image=(img_bytes, "image/png"),
    )
    assert agent.mock is False

    res = agent.run("")
    assert res["mock"] is False
    assert vision_attempts == 2
    assert call_features.count("buyer.vision") == 2
    assert any(
        s["kind"] == "thought" and "Read handwritten_order with Gemini vision: 2 line(s)" in s["title"]
        for s in res["steps"]
    )
    assert len(res["needs"]) == 2
    assert [(n["product_query"], n["quantity"], n["unit"]) for n in res["needs"]] == [
        ("lactose-free milk", 200.0, "L"),
        ("butter", 10.0, "KG"),
    ]

    tool_seq = [name for name, _ in gw.calls]
    assert tool_seq.count("plan_need") == 2
    assert tool_seq.count("create_quote") == 1
    assert tool_seq.count("place_order") == 1
    assert tool_seq.index("plan_need") < tool_seq.index("create_quote") < tool_seq.index("place_order")

    assert res["quote"] is not None and len(res["quote"]["lines"]) == 2
    quoted_by_pid = {ln["product_id"]: ln["qty_sales_units"] for ln in res["quote"]["lines"]}
    assert quoted_by_pid == {"464306": 200.0, "402105": 20.0}
    assert res["status"] == "placed"
    assert res["order"] is not None and res["order"]["status"] == "placed"


def test_costs_row_cost_cached_10_pct_fraction_and_unpriced_models() -> None:
    assert costs.CACHED_FRACTION == 0.10
    assert set(costs.PRICES_PER_M) == {
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
        "gemini-4-carbon",
        "gemini-4-argon",
    }

    uncached = {"model": "gemini-3.8-flash", "in": 100_000, "cached": 0, "out": 0, "thinking": 0}
    fully_cached = {"model": "gemini-3.8-flash", "in": 100_000, "cached": 100_000, "out": 0, "thinking": 0}
    c_uncached = costs.row_cost(uncached)
    c_cached = costs.row_cost(fully_cached)
    assert c_uncached == pytest.approx(0.075)
    assert c_cached == pytest.approx(0.0075)
    assert c_cached == pytest.approx(c_uncached * costs.CACHED_FRACTION)

    mixed_flash = {
        "model": "gemini-3.8-flash",
        "in": 100_000,
        "cached": 60_000,
        "out": 2_000,
        "thinking": 1_000,
    }
    assert costs.row_cost(mixed_flash) == pytest.approx((40_000 * 0.75 + 60_000 * 0.075 + 3_000 * 3.75) / 1e6)

    hydrogen = {"model": "gemini-4-hydrogen", "in": 20_000, "cached": 10_000, "out": 1_000, "thinking": 1_000}
    assert costs.row_cost(hydrogen) == pytest.approx((10_000 * 1.5 + 10_000 * 0.15 + 2_000 * 7.5) / 1e6)

    carbon = {"model": "gemini-4-carbon", "in": 50_000, "cached": 20_000, "out": 500, "thinking": 1_500}
    assert costs.row_cost(carbon) == pytest.approx((30_000 * 2.0 + 20_000 * 0.2 + 2_000 * 10.0) / 1e6)

    argon = {"model": "gemini-4-argon", "in": 10_000, "cached": 0, "out": 1_000, "thinking": 0}
    assert costs.row_cost(argon) == pytest.approx((10_000 * 2.0 + 1_000 * 10.0) / 1e6)

    clamped = {"model": "gemini-3.8-flash", "in": 5_000, "cached": 8_000, "out": None, "thinking": None}
    assert costs.row_cost(clamped) == pytest.approx((8_000 * 0.75 * 0.10) / 1e6)

    assert costs.row_cost({"model": MOCK_MODEL_NAME, "in": 1000, "out": 200}) is None
    assert costs.row_cost({"model": "unknown-future-model", "in": 1000, "out": 200}) is None
    assert costs.row_cost({}) is None


def test_costs_summarize_escalation_counts_and_ledger_rows_filtering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)

    rows = [
        {
            "run_id": "run-1",
            "pid": 101,
            "feature": "buyer.parse_need",
            "model": "gemini-3.8-flash",
            "in": 1_000,
            "cached": 0,
            "out": 200,
            "thinking": 100,
            "latency_s": 0.42,
            "ok": True,
            "meta": {},
        },
        {
            "run_id": "run-1",
            "pid": 101,
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "in": 4_000,
            "cached": 2_000,
            "out": 300,
            "thinking": 200,
            "latency_s": 0.81,
            "ok": True,
            "meta": {"escalated": False, "step": 0},
        },
        {
            "run_id": "run-1",
            "pid": 101,
            "feature": "buyer.agent_step",
            "model": "gemini-4-carbon",
            "in": 6_000,
            "cached": 3_000,
            "out": 400,
            "thinking": 600,
            "latency_s": 1.45,
            "ok": True,
            "meta": {"escalated": True, "step": 1},
        },
        {
            "run_id": "run-1",
            "pid": 101,
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "in": 5_000,
            "cached": 4_000,
            "out": 250,
            "thinking": 150,
            "latency_s": 0.65,
            "ok": True,
            "meta": {"escalated": True, "step": 2},
        },
        {
            "run_id": "run-2",
            "pid": 202,
            "feature": "buyer.agent_step",
            "model": "gemini-3.8-flash",
            "in": 3_000,
            "cached": 1_000,
            "out": 150,
            "thinking": 50,
            "latency_s": 0.50,
            "ok": False,
            "meta": {"escalated": False, "step": 0},
        },
        {
            "run_id": "run-2",
            "pid": 202,
            "feature": "buyer.summary",
            "model": "unpriced-exp",
            "in": 500,
            "cached": 0,
            "out": 100,
            "thinking": 0,
            "latency_s": 0.21,
            "ok": True,
            "meta": {},
        },
        {
            "run_id": "run-cat",
            "pid": 101,
            "feature": "catalog.embed",
            "model": "gemini-3.8-flash",
            "in": 9_999,
            "cached": 0,
            "out": 0,
            "thinking": 0,
            "latency_s": 0.10,
            "ok": True,
            "meta": {},
        },
    ]
    ledger_file = ledger_dir / "node-101.jsonl"
    ledger_file.write_text(
        "\n".join([json.dumps(r) for r in rows] + ["{corrupted-jsonl-line"]) + "\n",
        encoding="utf-8",
    )

    buyer_rows = costs.ledger_rows()
    assert len(buyer_rows) == 6
    assert len(costs.ledger_rows(run_ids=["run-1"])) == 4
    assert len(costs.ledger_rows(pid=202)) == 2
    assert len(costs.ledger_rows(feature_prefix="catalog.")) == 1

    summary = costs.summarize(buyer_rows)
    assert summary["calls"] == 6
    assert summary["runs"] == 2
    assert summary["in"] == 19_500
    assert summary["cached"] == 10_000
    assert summary["out"] == 1_400
    assert summary["thinking"] == 1_100
    assert summary["gemini_s"] == round(0.42 + 0.81 + 1.45 + 0.65 + 0.50 + 0.21, 1)
    assert summary["agent_steps"] == 4
    assert summary["escalated_steps"] == 2
    assert summary["escalated_runs"] == 1
    assert summary["unpriced_calls"] == 1
    assert summary["failed"] == 1

    expected_cost = sum(c for c in (costs.row_cost(r) for r in buyer_rows) if c is not None)
    assert summary["cost_usd"] == pytest.approx(expected_cost)


def test_zero_gemini_ledger_rows_emitted_on_keyless_mock_runs(keyless_env: Path) -> None:
    img_bytes = ORDER_IMAGE_PATH.read_bytes()
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)

    stop = threading.Event()

    def approve_worker() -> None:
        while not stop.is_set():
            for o in gw.pending():
                gw.decide(o["order_id"], "approve", "anna@hotel-aurora.example", "approved")
                return
            stop.wait(0.02)

    worker = threading.Thread(target=approve_worker, daemon=True)
    worker.start()
    try:
        r1 = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.03, now=SAT).run(HERO)
        worker.join(timeout=2.0)
    finally:
        stop.set()

    r2 = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        image=(img_bytes, "image/png"),
        poll_s=0.03,
        now=SAT,
    ).run(TYPED_MILK_BUTTER)
    r3 = BuyerAgent(
        FakeTransport(gw),
        "hotel-aurora",
        image=(img_bytes, "image/png"),
        poll_s=0.03,
        now=SAT,
    ).run("")

    assert r1["status"] == "placed" and r1["mock"] is True
    assert r2["status"] == "placed" and r2["mock"] is True
    assert r3["status"] == "no_order" and r3["mock"] is True

    assert not list(keyless_env.glob("*.jsonl"))
    emitted = costs.ledger_rows(run_ids=[r1["run_id"], r2["run_id"], r3["run_id"]])
    assert emitted == []
    assert costs.ledger_rows() == []

    summary = costs.summarize(emitted)
    assert summary == {
        "calls": 0,
        "runs": 0,
        "in": 0,
        "out": 0,
        "thinking": 0,
        "cached": 0,
        "gemini_s": 0.0,
        "cost_usd": 0,
        "unpriced_calls": 0,
        "agent_steps": 0,
        "escalated_steps": 0,
        "escalated_runs": 0,
        "failed": 0,
    }


def test_gemini_record_to_costs_ledger_and_summarize_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)

    u1 = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=10_000,
        cached_content_token_count=8_000,
        candidates_token_count=400,
        thoughts_token_count=600,
    )
    u2 = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=12_000,
        cached_content_token_count=6_000,
        candidates_token_count=500,
        thoughts_token_count=1_500,
    )
    gemini.record("buyer.agent_step", "gemini-3.8-flash", usage=u1, latency_s=0.7, run_id="run-rt", meta={"step": 0})
    gemini.record(
        "buyer.agent_step",
        "gemini-4-carbon",
        usage=u2,
        latency_s=1.3,
        run_id="run-rt",
        meta={"step": 1, "escalated": True},
    )

    rows = costs.ledger_rows(run_ids=["run-rt"])
    assert len(rows) == 2
    s = costs.summarize(rows)
    assert s["calls"] == 2
    assert s["runs"] == 1
    assert s["agent_steps"] == 2
    assert s["escalated_steps"] == 1
    assert s["escalated_runs"] == 1
    assert s["in"] == 22_000
    assert s["cached"] == 14_000
    assert s["out"] == 900
    assert s["thinking"] == 2_100
    assert s["failed"] == 0
    expected = (2_000 * 0.75 + 8_000 * 0.075 + 1_000 * 3.75 + 6_000 * 2.0 + 6_000 * 0.2 + 2_000 * 10.0) / 1e6
    assert s["cost_usd"] == pytest.approx(expected)

