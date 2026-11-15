from __future__ import annotations

import asyncio
import io
import json
import math
from pathlib import Path
import sqlite3
import tokenize
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from aimo import catalog as cat_mod
from aimo import config, gemini
from aimo.backend import catalog_store, client as bc, orders as store
from aimo.catalog import offers as offers_mod, plan as plan_mod, textsearch
from aimo.gateway import extras, fallback_catalog, human_auth, resilience, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from conformance.profile import MCP_FORBIDDEN, MCP_TOOLS
from test_fuzz import _synth_products
from test_gateway import FixtureClient, H

DELIVERY = "2026-11-16"
CORE_9_TOOLS = frozenset(MCP_TOOLS.keys())
EXTRA_4_TOOLS = frozenset(extras.EXTRA_TOOLS)
EXPECTED_13_TOOLS = CORE_9_TOOLS | EXTRA_4_TOOLS

STRICT_HEADERS = {"x-customer-id": "hotel-aurora", "x-agent-id": "buyer-1"}
FORBIDDEN_EXCEPTIONS = (
    ZeroDivisionError,
    KeyError,
    IndexError,
    sqlite3.Error,
    MemoryError,
    RecursionError,
    SystemError,
)
FORBIDDEN_TEXT_MARKERS = (
    "Traceback (most recent call last)",
    "ZeroDivisionError",
    "KeyError:",
    "IndexError:",
    "sqlite3.",
)

NEED_SEED: dict[str, Any] = {
    "customer_id": "hotel-aurora",
    "product_query": "lactose-free milk",
    "required_claims": ["FREE_FROM_LACTOSE"],
    "quantity": 10,
    "unit": "L",
    "delivery_by": DELIVERY,
}


def _reject_nonfinite_constant(token: str) -> Any:
    raise ValueError(f"Non-finite JSON constant forbidden: {token!r}")


def _assert_strict_json_value(val: Any) -> str:
    dumped = json.dumps(val, allow_nan=False)
    dumped.encode("utf-8")
    reparsed = json.loads(dumped, parse_constant=_reject_nonfinite_constant)
    json.dumps(reparsed, allow_nan=False)
    return dumped


def _parse_strict_json_str(raw: str) -> Any:
    raw.encode("utf-8")
    parsed = json.loads(raw, parse_constant=_reject_nonfinite_constant)
    _assert_strict_json_value(parsed)
    return parsed


def _iter_exception_chain(exc: BaseException | None) -> list[BaseException]:
    seen: set[int] = set()
    chain: list[BaseException] = []
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    return chain


def _assert_no_forbidden_crash(exc: BaseException) -> None:
    for item in _iter_exception_chain(exc):
        assert not isinstance(item, FORBIDDEN_EXCEPTIONS), (
            f"Unhandled crash {type(item).__name__}: {item}"
        )


def _extract_error_json(exc: ToolError) -> dict[str, Any] | None:
    candidates: list[str] = []
    for item in _iter_exception_chain(exc):
        s = str(item).strip()
        if s:
            candidates.append(s)
            if "{" in s:
                candidates.append(s[s.index("{"):])
    for cand in candidates:
        if cand.startswith("{") and cand.endswith("}"):
            parsed = _parse_strict_json_str(cand)
            if isinstance(parsed, dict):
                return parsed
    return None


def _reset_breakers() -> None:
    for breaker in list(resilience._breakers.values()):
        breaker.reset()


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cat_dir = tmp_path_factory.mktemp("mcp13_synth_catalog")
    pjson = cat_dir / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products(), allow_nan=False), encoding="utf-8")
    return cat_dir


@pytest.fixture(scope="module")
def shared_mcp_server() -> Any:
    return build_mcp()


@pytest.fixture(autouse=True)
def isolated_mcp_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synth_catalog_dir: Path,
) -> Path:
    cat_dir = synth_catalog_dir
    pjson = cat_dir / "valio_aimo_junction26_product_data.json"
    cdb = cat_dir / "catalog.sqlite"

    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    ledger_dir = tmp_path / "ledger"
    offer_cache = tmp_path / "offer_cache"

    for env_key in (
        "AIMO_FAULTS",
        "AIMO_NOW",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_API_KEY",
        "VALIO_API_KEY",
        "ELEVENLABS_API_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "AIMO_CUSTOMER_ID",
        "AIMO_AGENT_ID",
        "AIMO_LLM_MODE",
    ):
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")

    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", cat_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "RELIABILITY_CSV", cat_dir / "missing_rel.csv")
    monkeypatch.setattr(config, "CO2_CSV", cat_dir / "missing_co2.csv")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(offers_mod, "CACHE_DIR", offer_cache)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", cat_dir / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", cat_dir / "missing_co2.csv")
    monkeypatch.setattr(cat_mod, "_SOURCE", "full")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    def _forbid_gemini(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Gemini API must not be invoked in keyless MCP bad-input e2e suite")

    monkeypatch.setattr("aimo.gemini.generate", _forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", _forbid_gemini)

    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(FixtureClient())

    yield tmp_path

    store.close_all()
    human_auth.reset_for_tests()
    _reset_breakers()
    bc.set_client(None)
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _ctx(headers: dict[str, Any] | None) -> Any:
    return SimpleNamespace(headers=headers) if headers is not None else None


def _extract_call_result_payload(res: Any) -> dict[str, Any]:
    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    assert not is_err, f"Expected clean valid result, got error: {res}"
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    texts = [getattr(c, "text", "") for c in (getattr(res, "content", None) or []) if getattr(c, "text", "")]
    assert texts or sc is not None
    if texts:
        parsed = _parse_strict_json_str(texts[0])
        assert isinstance(parsed, dict)
        return parsed
    _assert_strict_json_value(sc)
    assert isinstance(sc, dict)
    return sc


async def _assert_tool_error_inproc(
    srv: Any,
    tool_name: str,
    arguments: Any,
    headers: dict[str, Any] | None = None,
    expected_code: str | None = None,
) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc_info:
        await srv.call_tool(tool_name, arguments, context=_ctx(headers))
    _reset_breakers()
    exc = exc_info.value
    _assert_no_forbidden_crash(exc)
    msg = str(exc)
    assert msg.strip(), f"Empty ToolError message for {tool_name}"
    for marker in FORBIDDEN_TEXT_MARKERS:
        assert marker not in msg, f"Leaked {marker!r} in {tool_name}: {msg[:240]}"
    structured = _extract_error_json(exc)
    if structured is not None:
        assert isinstance(structured.get("error"), str) and structured["error"]
        assert isinstance(structured.get("message"), str) and structured["message"]
        envelope = structured
    else:
        envelope = {"error": "tool_validation_error", "tool": tool_name, "message": msg}
        _assert_strict_json_value(envelope)
    if expected_code is not None:
        assert expected_code in json.dumps(envelope, allow_nan=False), (tool_name, expected_code, envelope)
    return envelope


async def _verify_all_13_valid_calls_succeed(srv: Any) -> dict[str, Any]:
    r_search = _extract_call_result_payload(
        await srv.call_tool("search_products", {"query": "milk", "claims": ["FREE_FROM_LACTOSE"], "limit": 5})
    )
    assert any(p["id"] == "464306" for p in r_search["products"])

    r_prod = _extract_call_result_payload(
        await srv.call_tool("get_product", {"product_id": "464306"})
    )
    assert r_prod["id"] == "464306"

    r_offers = _extract_call_result_payload(
        await srv.call_tool("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY})
    )
    assert r_offers["offers"][0]["product_id"] == "464306"
    assert r_offers["offers"][0]["available"] is True

    r_plan = _extract_call_result_payload(
        await srv.call_tool("plan_need", {"need": dict(NEED_SEED)})
    )
    assert r_plan["covered_amount"] == pytest.approx(10.0)
    assert len(r_plan["lines"]) >= 1

    r_quote = _extract_call_result_payload(
        await srv.call_tool(
            "create_quote",
            {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
            context=_ctx(STRICT_HEADERS),
        )
    )
    assert r_quote["quote_id"] and r_quote["quote_hash"]

    r_order = _extract_call_result_payload(
        await srv.call_tool(
            "place_order",
            {"quote_id": r_quote["quote_id"], "quote_hash": r_quote["quote_hash"]},
            context=_ctx(STRICT_HEADERS),
        )
    )
    assert r_order["order_id"] and r_order["status"] in ("placed", "pending_approval")

    r_status = _extract_call_result_payload(
        await srv.call_tool("get_order_status", {"order_id": r_order["order_id"]})
    )
    assert r_status["order_id"] == r_order["order_id"]

    r_audit = _extract_call_result_payload(
        await srv.call_tool("get_audit_trail", {"subject_id": r_order["order_id"]})
    )
    assert len(r_audit["events"]) >= 1
    assert r_audit["verify"]["ok"] is True

    r_policies = _extract_call_result_payload(
        await srv.call_tool("list_policies", {}, context=_ctx(STRICT_HEADERS))
    )
    assert len(r_policies["policies"]) >= 1

    r_recipes = _extract_call_result_payload(
        await srv.call_tool("list_recipes", {})
    )
    assert len(r_recipes["recipes"]) >= 1

    r_plan_recipe = _extract_call_result_payload(
        await srv.call_tool(
            "plan_recipe",
            {"recipe_id": "oat-porridge", "portions": 20, "delivery_by": DELIVERY},
            context=_ctx(STRICT_HEADERS),
        )
    )
    assert r_plan_recipe["recipe"]["ids"] == ["oat-porridge"]
    assert len(r_plan_recipe["ingredients"]) >= 1

    r_drift = _extract_call_result_payload(
        await srv.call_tool("check_standing_order_drift", {"customer_id": "hotel-aurora"})
    )
    assert r_drift["customer_id"] == "hotel-aurora"
    assert isinstance(r_drift["flags"], list)

    r_propose = _extract_call_result_payload(
        await srv.call_tool(
            "propose_offer",
            {
                "customer_id": "hotel-aurora",
                "product_id": "464306",
                "qty_sales_units": 10,
                "delivery_date": DELIVERY,
                "note": "Weekly milk restock",
            },
        )
    )
    assert r_propose["proposal_id"].startswith("P-")
    assert r_propose["seller_can_order"] is False

    return {
        "quote_id": r_quote["quote_id"],
        "order_id": r_order["order_id"],
        "proposal_id": r_propose["proposal_id"],
    }


async def _verify_single_valid_tool_call(srv: Any, tool_name: str) -> dict[str, Any]:
    if tool_name == "search_products":
        out = _extract_call_result_payload(
            await srv.call_tool("search_products", {"query": "milk", "claims": ["FREE_FROM_LACTOSE"], "limit": 5})
        )
        assert any(p["id"] == "464306" for p in out["products"])
        return out
    if tool_name == "get_product":
        out = _extract_call_result_payload(await srv.call_tool("get_product", {"product_id": "464306"}))
        assert out["id"] == "464306"
        return out
    if tool_name == "get_offers":
        out = _extract_call_result_payload(
            await srv.call_tool("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY})
        )
        assert out["offers"][0]["product_id"] == "464306"
        return out
    if tool_name == "plan_need":
        out = _extract_call_result_payload(await srv.call_tool("plan_need", {"need": dict(NEED_SEED)}))
        assert out["covered_amount"] == pytest.approx(10.0)
        return out
    if tool_name == "create_quote":
        out = _extract_call_result_payload(
            await srv.call_tool(
                "create_quote",
                {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 4}]},
                context=_ctx(STRICT_HEADERS),
            )
        )
        assert out["quote_id"].startswith("Q-") and out["quote_hash"]
        return out
    if tool_name in ("place_order", "get_order_status", "get_audit_trail"):
        q = _extract_call_result_payload(
            await srv.call_tool(
                "create_quote",
                {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 4}]},
                context=_ctx(STRICT_HEADERS),
            )
        )
        if tool_name == "get_audit_trail":
            out = _extract_call_result_payload(
                await srv.call_tool("get_audit_trail", {"subject_id": q["quote_id"]})
            )
            assert out["verify"]["ok"] is True and len(out["events"]) >= 1
            return out
        o = _extract_call_result_payload(
            await srv.call_tool(
                "place_order",
                {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
                context=_ctx(STRICT_HEADERS),
            )
        )
        if tool_name == "place_order":
            assert o["order_id"].startswith("ORD-")
            return o
        st = _extract_call_result_payload(
            await srv.call_tool("get_order_status", {"order_id": o["order_id"]})
        )
        assert st["order_id"] == o["order_id"]
        return st
    if tool_name == "list_policies":
        out = _extract_call_result_payload(
            await srv.call_tool("list_policies", {}, context=_ctx(STRICT_HEADERS))
        )
        assert len(out["policies"]) >= 1
        return out
    if tool_name == "list_recipes":
        out = _extract_call_result_payload(await srv.call_tool("list_recipes", {}))
        assert len(out["recipes"]) >= 1
        return out
    if tool_name == "plan_recipe":
        out = _extract_call_result_payload(
            await srv.call_tool(
                "plan_recipe",
                {"recipe_id": "oat-porridge", "portions": 15, "delivery_by": DELIVERY},
                context=_ctx(STRICT_HEADERS),
            )
        )
        assert out["recipe"]["ids"] == ["oat-porridge"]
        return out
    if tool_name == "check_standing_order_drift":
        out = _extract_call_result_payload(
            await srv.call_tool("check_standing_order_drift", {"customer_id": "hotel-aurora"})
        )
        assert out["customer_id"] == "hotel-aurora"
        return out
    if tool_name == "propose_offer":
        out = _extract_call_result_payload(
            await srv.call_tool(
                "propose_offer",
                {
                    "customer_id": "hotel-aurora",
                    "product_id": "464306",
                    "qty_sales_units": 6,
                    "delivery_date": DELIVERY,
                    "note": "Restock offer",
                },
            )
        )
        assert out["proposal_id"].startswith("P-")
        return out
    raise AssertionError(f"Unhandled tool {tool_name}")


def _per_tool_bad_cases() -> dict[str, list[tuple[str, Any, dict[str, Any] | None, str | None]]]:
    oversized_600 = "x" * 600
    oversized_5000 = "x" * 5000
    return {
        "search_products": [
            ("qty_zero", {"query": "milk", "limit": 0}, None, "bad_limit"),
            ("qty_neg", {"query": "milk", "limit": -5}, None, "bad_limit"),
            ("qty_huge", {"query": "milk", "limit": 1_000_000}, None, "bad_limit"),
            ("qty_nan", {"query": "milk", "limit": float("nan")}, None, None),
            ("qty_inf", {"query": "milk", "limit": float("inf")}, None, None),
            ("qty_bool", {"query": "milk", "limit": False}, None, "bad_limit"),
            ("bad_date_as_limit", {"query": "milk", "limit": "2026-13-45"}, None, None),
            ("ctrl_nul", {"query": "milk\x00"}, None, "bad_query"),
            ("ctrl_c1", {"query": "milk\x85"}, None, "bad_query"),
            ("lone_surrogate", {"query": "milk\ud800"}, None, "bad_query"),
            ("blank_str", {"query": "   \t\n"}, None, "empty_query"),
            ("oversized_str", {"query": oversized_600}, None, "query_too_long"),
            ("path_traversal", {"query": "milk"}, {"x-customer-id": "../etc/passwd"}, "unknown_customer"),
            ("unknown_customer", {"query": "milk"}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
        ],
        "get_product": [
            ("qty_zero", {"product_id": 0}, None, None),
            ("qty_neg", {"product_id": -1}, None, None),
            ("qty_huge", {"product_id": 1_000_001}, None, None),
            ("qty_nan", {"product_id": float("nan")}, None, None),
            ("qty_bool", {"product_id": True}, None, None),
            ("invalid_date", {"product_id": "2026-13-45"}, None, "product_not_found"),
            ("ctrl_nul", {"product_id": "464306\x00"}, None, "bad_product_id"),
            ("lone_surrogate", {"product_id": "464306\ud800"}, None, "bad_product_id"),
            ("blank_str", {"product_id": "   "}, None, "product_not_found"),
            ("oversized_str", {"product_id": oversized_600}, None, "product_not_found"),
            ("path_traversal", {"product_id": "../../etc/passwd"}, None, "product_not_found"),
            ("unknown_customer", {"product_id": "464306"}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
        ],
        "get_offers": [
            ("qty_zero", {"product_ids": [0], "requested_date": DELIVERY}, None, None),
            ("qty_neg", {"product_ids": [-1], "requested_date": DELIVERY}, None, None),
            ("qty_huge_list", {"product_ids": [str(i) for i in range(1001)], "requested_date": DELIVERY}, None, "too_many_products"),
            ("qty_nan", {"product_ids": [float("nan")], "requested_date": DELIVERY}, None, None),
            ("qty_bool", {"product_ids": [False], "requested_date": DELIVERY}, None, None),
            ("invalid_date_month", {"product_ids": ["464306"], "requested_date": "2026-13-45"}, None, "bad_date"),
            ("invalid_date_feb30", {"product_ids": ["464306"], "requested_date": "2026-02-30"}, None, "bad_date"),
            ("invalid_date_slash", {"product_ids": ["464306"], "requested_date": "2026/11/16"}, None, "bad_date"),
            ("ctrl_nul", {"product_ids": ["464306\x00"], "requested_date": DELIVERY}, None, "bad_product_id"),
            ("lone_surrogate", {"product_ids": ["464306\udfff"], "requested_date": DELIVERY}, None, "bad_product_id"),
            ("blank_str", {"product_ids": ["", "   "], "requested_date": DELIVERY}, None, "no_products"),
            ("oversized_str", {"product_ids": [oversized_600], "requested_date": DELIVERY}, None, "bad_product_id"),
            ("path_traversal", {"product_ids": ["../" * 25], "requested_date": DELIVERY}, None, "bad_product_id"),
            ("unknown_customer", {"product_ids": ["464306"], "requested_date": DELIVERY}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
        ],
        "plan_need": [
            ("qty_zero", {"need": {**NEED_SEED, "quantity": 0}}, None, None),
            ("qty_neg_zero", {"need": {**NEED_SEED, "quantity": -0.0}}, None, None),
            ("qty_neg", {"need": {**NEED_SEED, "quantity": -12.5}}, None, None),
            ("qty_huge", {"need": {**NEED_SEED, "quantity": 1_000_001}}, None, None),
            ("qty_inf", {"need": {**NEED_SEED, "quantity": float("inf")}}, None, None),
            ("qty_nan", {"need": {**NEED_SEED, "quantity": float("nan")}}, None, None),
            ("qty_nan_str", {"need": {**NEED_SEED, "quantity": "NaN"}}, None, None),
            ("qty_bool_true", {"need": {**NEED_SEED, "quantity": True}}, None, None),
            ("qty_bool_false", {"need": {**NEED_SEED, "quantity": False}}, None, None),
            ("invalid_date", {"need": {**NEED_SEED, "delivery_by": "2026-13-45"}}, None, None),
            ("ctrl_nul", {"need": {**NEED_SEED, "product_query": "milk\x00"}}, None, None),
            ("lone_surrogate", {"need": {**NEED_SEED, "raw_text": "milk\ud800"}}, None, None),
            ("blank_str", {"need": {**NEED_SEED, "product_query": "   "}}, None, None),
            ("oversized_str", {"need": {**NEED_SEED, "product_query": oversized_600}}, None, None),
            ("path_traversal", {"need": {**NEED_SEED, "customer_id": "../etc/passwd"}}, None, "unknown_customer"),
            ("unknown_customer", {"need": {**NEED_SEED, "customer_id": "unknown-customer"}}, None, "unknown_customer"),
        ],
        "create_quote": [
            ("qty_zero", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 0}]}, STRICT_HEADERS, None),
            ("qty_neg", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": -3.5}]}, STRICT_HEADERS, None),
            ("qty_huge", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1_000_001}]}, STRICT_HEADERS, None),
            ("qty_split_overflow", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 600_000}, {"product_id": "464306", "qty_sales_units": 600_000}]}, STRICT_HEADERS, "absurd_quantity"),
            ("qty_nan", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": float("nan")}]}, STRICT_HEADERS, None),
            ("qty_bool_true", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": True}]}, STRICT_HEADERS, None),
            ("qty_bool_false", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": False}]}, STRICT_HEADERS, None),
            ("invalid_date", {"delivery_date": "2026-02-30", "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, STRICT_HEADERS, "bad_date"),
            ("ctrl_nul", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306\x00", "qty_sales_units": 1}]}, STRICT_HEADERS, None),
            ("lone_surrogate", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306\ud800", "qty_sales_units": 1}]}, STRICT_HEADERS, None),
            ("blank_str", {"delivery_date": DELIVERY, "lines": [{"product_id": "   ", "qty_sales_units": 1}]}, STRICT_HEADERS, None),
            ("oversized_str", {"delivery_date": DELIVERY, "lines": [{"product_id": oversized_600, "qty_sales_units": 1}]}, STRICT_HEADERS, None),
            ("path_traversal", {"delivery_date": DELIVERY, "lines": [{"product_id": "../etc/passwd", "qty_sales_units": 1}]}, STRICT_HEADERS, None),
            ("unknown_customer", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, {"x-customer-id": "unknown-customer", "x-agent-id": "buyer-1"}, "unknown_customer"),
            ("strict_identity_missing", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, None, "identity_required"),
        ],
        "place_order": [
            ("qty_zero", {"quote_id": 0, "quote_hash": "0" * 64}, STRICT_HEADERS, None),
            ("qty_neg", {"quote_id": -1, "quote_hash": "0" * 64}, STRICT_HEADERS, None),
            ("qty_huge", {"quote_id": "Q-1", "quote_hash": 10**20}, STRICT_HEADERS, None),
            ("qty_nan", {"quote_id": float("nan"), "quote_hash": "0" * 64}, STRICT_HEADERS, None),
            ("qty_bool", {"quote_id": False, "quote_hash": True}, STRICT_HEADERS, None),
            ("invalid_date_id", {"quote_id": "2026-13-45", "quote_hash": "0" * 64}, STRICT_HEADERS, "quote_not_found"),
            ("ctrl_nul", {"quote_id": "Q-1\x00", "quote_hash": "0" * 64}, STRICT_HEADERS, "bad_quote_id"),
            ("lone_surrogate", {"quote_id": "Q-1\ud800", "quote_hash": "0" * 64}, STRICT_HEADERS, "bad_quote_id"),
            ("blank_str", {"quote_id": "   ", "quote_hash": "0" * 64}, STRICT_HEADERS, "bad_quote_id"),
            ("oversized_str", {"quote_id": oversized_600, "quote_hash": "0" * 64}, STRICT_HEADERS, "bad_quote_id"),
            ("path_traversal", {"quote_id": "../../state.sqlite", "quote_hash": "0" * 64}, STRICT_HEADERS, "quote_not_found"),
            ("unknown_customer", {"quote_id": "Q-1", "quote_hash": "0" * 64}, {"x-customer-id": "unknown-customer", "x-agent-id": "buyer-1"}, "unknown_customer"),
            ("strict_identity_missing", {"quote_id": "Q-1", "quote_hash": "0" * 64}, None, "identity_required"),
        ],
        "get_order_status": [
            ("qty_zero", {"order_id": 0}, None, None),
            ("qty_neg", {"order_id": -1}, None, None),
            ("qty_huge", {"order_id": 10**20}, None, None),
            ("qty_nan", {"order_id": float("nan")}, None, None),
            ("qty_bool", {"order_id": False}, None, None),
            ("invalid_date", {"order_id": "2026-13-45"}, None, "order_not_found"),
            ("ctrl_nul", {"order_id": "ORD-1\x00"}, None, "bad_order_id"),
            ("lone_surrogate", {"order_id": "ORD-1\ud800"}, None, "bad_order_id"),
            ("blank_str", {"order_id": "   "}, None, "order_not_found"),
            ("oversized_str", {"order_id": oversized_600}, None, "order_not_found"),
            ("path_traversal", {"order_id": "../../etc/passwd"}, None, "order_not_found"),
            ("unknown_customer_id", {"order_id": "unknown-customer"}, None, "order_not_found"),
        ],
        "get_audit_trail": [
            ("qty_zero", {"subject_id": 0}, None, None),
            ("qty_neg", {"subject_id": -1}, None, None),
            ("qty_huge", {"subject_id": 10**20}, None, None),
            ("qty_nan", {"subject_id": float("nan")}, None, None),
            ("qty_bool", {"subject_id": True}, None, None),
            ("invalid_date_non_str", {"subject_id": ["2026-13-45"]}, None, None),
            ("ctrl_nul", {"subject_id": "Q-1\x00"}, None, "bad_subject_id"),
            ("lone_surrogate", {"subject_id": "Q-1\ud800"}, None, "bad_subject_id"),
            ("missing_blank_arg", {}, None, None),
            ("oversized_str", {"subject_id": oversized_5000}, None, "bad_subject_id"),
            ("path_traversal_non_dict", "../etc/passwd", None, None),
            ("unknown_customer_non_dict", ["unknown-customer"], None, None),
        ],
        "list_policies": [
            ("qty_zero_non_dict", 0, None, None),
            ("qty_neg_non_dict", -1, None, None),
            ("qty_huge_non_dict", 10**20, None, None),
            ("qty_nan_non_dict", float("nan"), None, None),
            ("qty_bool_non_dict", False, None, None),
            ("invalid_date_non_dict", "2026-13-45", None, None),
            ("ctrl_customer_hdr", {}, {"x-customer-id": "hotel\x00aurora"}, "unknown_customer"),
            ("surrogate_customer_hdr", {}, {"x-customer-id": "hotel\ud800aurora"}, "unknown_customer"),
            ("oversized_customer_hdr", {}, {"x-customer-id": oversized_600}, "unknown_customer"),
            ("path_traversal_customer", {}, {"x-customer-id": "../etc/passwd"}, "unknown_customer"),
            ("path_traversal_agent", {}, {"x-customer-id": "hotel-aurora", "x-agent-id": "../etc/passwd"}, "invalid_agent_id"),
            ("unknown_customer", {}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
        ],
        "list_recipes": [
            ("qty_zero_non_dict", 0, None, None),
            ("qty_neg_non_dict", -5, None, None),
            ("qty_huge_non_dict", 10**20, None, None),
            ("qty_nan_non_dict", float("nan"), None, None),
            ("qty_bool_non_dict", False, None, None),
            ("invalid_date_non_dict", "2026-13-45", None, None),
            ("ctrl_non_dict", "\x00\x07", None, None),
            ("surrogate_non_dict", "\ud800", None, None),
            ("blank_non_dict", "   ", None, None),
            ("oversized_non_dict", oversized_5000, None, None),
            ("path_traversal_non_dict", "../etc/passwd", None, None),
            ("unknown_customer_non_dict", ["unknown-customer"], None, None),
        ],
        "plan_recipe": [
            ("qty_zero", {"recipe_id": "lasagne", "portions": 0}, None, None),
            ("qty_neg", {"recipe_id": "lasagne", "portions": -10}, None, None),
            ("qty_huge", {"recipe_id": "lasagne", "portions": 100_001}, None, None),
            ("qty_nan", {"recipe_id": "lasagne", "portions": float("nan")}, None, None),
            ("qty_inf", {"recipe_id": "lasagne", "portions": float("inf")}, None, None),
            ("qty_bool_false", {"recipe_id": "lasagne", "portions": False}, None, None),
            ("qty_text_neg", {"text": "-10 lasagne"}, None, "bad_portions"),
            ("qty_text_zero", {"text": "0 lasagne"}, None, "bad_portions"),
            ("invalid_date", {"recipe_id": "lasagne", "portions": 10, "delivery_by": "2026-13-45"}, None, "bad_delivery_date"),
            ("ctrl_nul", {"recipe_id": "lasagne\x00", "portions": 10}, None, "bad_recipe"),
            ("lone_surrogate", {"recipe_id": "lasagne", "portions": 10, "text": "friday\ud800"}, None, None),
            ("blank_str", {"recipe_id": "   ", "text": "   "}, None, "recipe_not_found"),
            ("oversized_str", {"text": oversized_5000}, None, "bad_text"),
            ("path_traversal", {"recipe_id": "../etc/passwd", "portions": 10}, None, "recipe_not_found"),
            ("unknown_customer", {"recipe_id": "lasagne", "portions": 10}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
        ],
        "check_standing_order_drift": [
            ("qty_zero", {"customer_id": 0}, None, None),
            ("qty_neg", {"customer_id": -1}, None, None),
            ("qty_huge", {"customer_id": 10**20}, None, None),
            ("qty_nan", {"customer_id": float("nan")}, None, None),
            ("qty_bool", {"customer_id": False}, None, None),
            ("invalid_date", {"customer_id": "2026-13-45"}, None, "unknown_customer"),
            ("ctrl_nul", {"customer_id": "hotel\x00aurora"}, None, "unknown_customer"),
            ("lone_surrogate", {"customer_id": "hotel\ud800aurora"}, None, "unknown_customer"),
            ("blank_str_bad_hdr", {"customer_id": "   "}, {"x-customer-id": "unknown-customer"}, "unknown_customer"),
            ("oversized_str", {"customer_id": oversized_600}, None, "unknown_customer"),
            ("path_traversal", {"customer_id": "../../etc/passwd"}, None, "unknown_customer"),
            ("unknown_customer", {"customer_id": "unknown-customer"}, None, "unknown_customer"),
        ],
        "propose_offer": [
            ("qty_zero", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 0, "delivery_date": DELIVERY}, None, None),
            ("qty_neg", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": -5, "delivery_date": DELIVERY}, None, None),
            ("qty_huge", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 1_000_001, "delivery_date": DELIVERY}, None, "bad_quantity"),
            ("qty_nan", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": float("nan"), "delivery_date": DELIVERY}, None, None),
            ("qty_inf", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": float("inf"), "delivery_date": DELIVERY}, None, "bad_quantity"),
            ("qty_bool_false", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": False, "delivery_date": DELIVERY}, None, None),
            ("invalid_date", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": "2026-13-45"}, None, None),
            ("invalid_date_text", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": "not-a-date"}, None, None),
            ("ctrl_nul_note", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY, "note": "bad\x00note"}, None, "bad_note"),
            ("lone_surrogate_note", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY, "note": "bad\ud800note"}, None, None),
            ("blank_customer", {"customer_id": "   ", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY}, None, "identity_required"),
            ("oversized_note", {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY, "note": oversized_5000}, None, "bad_note"),
            ("path_traversal_product", {"customer_id": "hotel-aurora", "product_id": "../etc/passwd", "qty_sales_units": 5, "delivery_date": DELIVERY}, None, "product_not_found"),
            ("path_traversal_customer", {"customer_id": "../etc/passwd", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY}, None, "unknown_customer"),
            ("unknown_customer", {"customer_id": "unknown-customer", "product_id": "464306", "qty_sales_units": 5, "delivery_date": DELIVERY}, None, "unknown_customer"),
        ],
    }


class _McpHttpSession:
    BASE_HEADERS = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def __init__(self, tc: TestClient) -> None:
        self.tc = tc
        self.sid: str | None = None
        self._rpc_id = 0

    def _parse(self, resp: Any) -> dict[str, Any]:
        assert resp.status_code < 500, (resp.status_code, resp.text[:300])
        if resp.headers.get("mcp-session-id"):
            self.sid = resp.headers["mcp-session-id"]
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            data_lines = [ln[6:] for ln in resp.text.splitlines() if ln.startswith("data: ")]
            assert data_lines, resp.text[:300]
            parsed = _parse_strict_json_str(data_lines[0])
        else:
            parsed = _parse_strict_json_str(resp.text)
        assert isinstance(parsed, dict)
        return parsed

    def initialize(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._rpc_id += 1
        r = self.tc.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp13-zero-crash-e2e", "version": "1.0.0"},
                },
            },
            headers={**self.BASE_HEADERS, **(headers or {})},
        )
        assert r.status_code == 200, r.text[:300]
        body = self._parse(r)
        nh = {**self.BASE_HEADERS, **(headers or {})}
        if self.sid:
            nh["mcp-session-id"] = self.sid
        r_notif = self.tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=nh,
        )
        assert r_notif.status_code < 500, r_notif.text[:300]
        return body

    def rpc(self, method: str, params: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._rpc_id += 1
        req_headers = {**self.BASE_HEADERS, **(headers or {})}
        if self.sid and method != "initialize":
            req_headers.setdefault("mcp-session-id", self.sid)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        r = self.tc.post("/mcp", json=payload, headers=req_headers)
        assert r.status_code < 500, (method, r.status_code, r.text[:300])
        return self._parse(r)

    def call_tool(self, name: str, arguments: Any, headers: dict[str, str] | None = None) -> tuple[bool, str]:
        msg = self.rpc("tools/call", {"name": name, "arguments": arguments}, headers=headers)
        _reset_breakers()
        if "error" in msg:
            return True, _assert_strict_json_value(msg["error"])
        res = msg.get("result") or {}
        is_err = bool(res.get("isError") or res.get("is_error"))
        txt = " ".join(c.get("text", "") for c in (res.get("content") or []) if isinstance(c, dict))
        if not txt and res.get("structuredContent") is not None:
            txt = _assert_strict_json_value(res["structuredContent"])
        return is_err, txt


def test_file_hygiene_zero_comments_and_zero_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    assert [tok for tok in tokens if tok.type == tokenize.COMMENT] == []


def test_build_mcp_registers_exact_13_tools_and_rejects_forbidden_tools() -> None:
    srv = build_mcp()
    direct_tools = asyncio.run(srv.list_tools())
    assert len(direct_tools) == 13

    direct_names = {t.name for t in direct_tools}
    assert len(CORE_9_TOOLS) == 9
    assert len(EXTRA_4_TOOLS) == 4
    assert CORE_9_TOOLS.isdisjoint(EXTRA_4_TOOLS)
    assert direct_names == EXPECTED_13_TOOLS
    assert direct_names.isdisjoint(MCP_FORBIDDEN)
    assert not [n for n in direct_names if "approv" in n.lower() or "decid" in n.lower()]

    for t in direct_tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        assert (t.description or "").strip(), t.name
        assert schema.get("type") == "object", t.name
        _assert_strict_json_value(schema)

    async def _check_client_and_bad_names() -> None:
        async with Client(srv) as client:
            listed = (await client.list_tools()).tools
            assert len(listed) == 13
            assert {t.name for t in listed} == EXPECTED_13_TOOLS
        for forbidden_name in [*MCP_FORBIDDEN, "unknown_tool", "", "../etc/passwd", "bad\x00tool", "bad\ud800tool"]:
            await _assert_tool_error_inproc(srv, forbidden_name, {})

    asyncio.run(_check_client_and_bad_names())


def test_plan_need_rejects_bad_quantities_before_coverage_division(
    monkeypatch: pytest.MonkeyPatch,
    shared_mcp_server: Any,
) -> None:
    bad_quantities: list[Any] = [
        0,
        0.0,
        -0.0,
        -1,
        -0.0001,
        -1e9,
        1_000_001,
        1e18,
        float("inf"),
        float("-inf"),
        float("nan"),
        "NaN",
        "-Infinity",
        "Infinity",
        True,
        False,
    ]
    invoked: list[str] = []

    def _forbid_candidates(*_a: Any, **_kw: Any) -> Any:
        invoked.append("find_candidates")
        raise AssertionError("find_candidates must not execute when quantity is invalid")

    class _NoCallBackend(FixtureClient):
        def price(self, ids: Any, requested_date: Any, customer: Any = None) -> Any:
            invoked.append("price")
            raise AssertionError("price must not execute when quantity is invalid")

        def availability(self, ids: Any, requested_date: Any, customer: Any = None) -> Any:
            invoked.append("availability")
            raise AssertionError("availability must not execute when quantity is invalid")

    srv = shared_mcp_server
    orig_find = plan_mod.find_candidates
    monkeypatch.setattr(plan_mod, "find_candidates", _forbid_candidates)
    bc.set_client(_NoCallBackend())

    async def _check_all_bad_quantities() -> None:
        for bad_qty in bad_quantities:
            await _assert_tool_error_inproc(srv, "plan_need", {"need": {**NEED_SEED, "quantity": bad_qty}})
            assert invoked == []

    asyncio.run(_check_all_bad_quantities())

    monkeypatch.setattr(plan_mod, "find_candidates", orig_find)
    bc.set_client(FixtureClient())
    recovered = asyncio.run(srv.call_tool("plan_need", {"need": dict(NEED_SEED)}))
    plan_payload = _extract_call_result_payload(recovered)
    assert plan_payload["covered_amount"] == pytest.approx(10.0)


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_13_TOOLS))
def test_each_of_13_mcp_tools_bad_inputs_and_valid_recovery(
    shared_mcp_server: Any,
    tool_name: str,
) -> None:
    srv = shared_mcp_server
    cases = _per_tool_bad_cases()[tool_name]
    assert len(cases) >= 12

    async def _exercise_tool() -> dict[str, Any]:
        for label, args, hdrs, expected_code in cases:
            env = await _assert_tool_error_inproc(
                srv,
                tool_name,
                args,
                headers=hdrs,
                expected_code=expected_code,
            )
            assert env["message"], (tool_name, label)
        return await _verify_single_valid_tool_call(srv, tool_name)

    recovered = asyncio.run(_exercise_tool())
    _assert_strict_json_value(recovered)


def test_all_13_mcp_tools_bad_args_produce_clean_tool_error_and_preserve_state() -> None:
    srv = build_mcp()
    matrix = _per_tool_bad_cases()
    assert set(matrix.keys()) == EXPECTED_13_TOOLS
    assert len(matrix) == 13

    async def _run_matrix_and_recover() -> tuple[int, dict[str, Any]]:
        executed = 0
        for tool_name in sorted(EXPECTED_13_TOOLS):
            cases = matrix[tool_name]
            assert len(cases) >= 12, tool_name
            for label, args, hdrs, expected_code in cases:
                env = await _assert_tool_error_inproc(
                    srv,
                    tool_name,
                    args,
                    headers=hdrs,
                    expected_code=expected_code,
                )
                assert env["message"], (tool_name, label)
                executed += 1
        recovery = await _verify_all_13_valid_calls_succeed(srv)
        return executed, recovery

    total_bad_calls, recovery_ids = asyncio.run(_run_matrix_and_recover())
    assert total_bad_calls >= 170
    assert recovery_ids["quote_id"].startswith("Q-")
    assert recovery_ids["order_id"].startswith("ORD-")
    assert recovery_ids["proposal_id"].startswith("P-")


def test_mcp_sdk_client_session_survives_bad_inputs_across_all_13_tools() -> None:
    srv = build_mcp()
    client_calls: list[tuple[str, dict[str, Any], bool]] = [
        ("search_products", {"query": "", "limit": 0}, True),
        ("search_products", {"query": "milk\x00", "limit": -3}, True),
        ("get_product", {"product_id": "../../etc/passwd"}, True),
        ("get_product", {"product_id": "464306\x00"}, True),
        ("get_offers", {"product_ids": [], "requested_date": "2026-13-45"}, True),
        ("get_offers", {"product_ids": ["464306\x00"], "requested_date": DELIVERY}, True),
        ("plan_need", {"need": {**NEED_SEED, "quantity": 0}}, True),
        ("plan_need", {"need": {**NEED_SEED, "quantity": -9, "delivery_by": "not-a-date"}}, True),
        ("plan_need", {"need": {**NEED_SEED, "customer_id": "unknown-customer"}}, True),
        ("create_quote", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 0}]}, True),
        ("place_order", {"quote_id": "../etc/passwd", "quote_hash": "0" * 64}, True),
        ("get_order_status", {"order_id": "../../etc/passwd"}, True),
        ("get_order_status", {"order_id": "ORD-1\x00"}, True),
        ("get_audit_trail", {"subject_id": "Q-1\x00"}, True),
        ("get_audit_trail", {"subject_id": "x" * 5000}, True),
        ("get_audit_trail", {"subject_id": "../etc/passwd"}, False),
        ("list_policies", {"quantity": -1, "customer_id": "unknown-customer"}, False),
        ("list_recipes", {"quantity": -1, "delivery_by": "2026-13-45", "recipe_id": "../etc/passwd"}, False),
        ("plan_recipe", {"recipe_id": "lasagne", "portions": 0}, True),
        ("plan_recipe", {"recipe_id": "../etc/passwd", "portions": 10}, True),
        ("plan_recipe", {"recipe_id": "lasagne", "portions": 10, "delivery_by": "2026-13-45"}, True),
        ("check_standing_order_drift", {"customer_id": "unknown-customer"}, True),
        ("check_standing_order_drift", {"customer_id": "../etc/passwd"}, True),
        ("check_standing_order_drift", {"customer_id": "   "}, False),
        ("propose_offer", {"customer_id": "unknown-customer", "qty_sales_units": 5, "delivery_date": DELIVERY}, True),
        ("propose_offer", {"customer_id": "hotel-aurora", "qty_sales_units": -2, "delivery_date": "2026-13-45"}, True),
    ]
    assert {name for name, _, _ in client_calls} == EXPECTED_13_TOOLS

    async def _run_client_session() -> None:
        async with Client(srv) as client:
            assert len((await client.list_tools()).tools) == 13
            for name, args, expect_err in client_calls:
                res = await client.call_tool(name, args)
                _reset_breakers()
                is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                assert is_err is expect_err, (name, args, is_err)
                text = " ".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))
                assert text.strip(), (name, args)
                text.encode("utf-8")
                for marker in FORBIDDEN_TEXT_MARKERS:
                    assert marker not in text, (name, marker, text[:240])
                if not is_err:
                    parsed = _parse_strict_json_str(text)
                    assert isinstance(parsed, dict)
                elif "{" in text:
                    err_obj = _parse_strict_json_str(text[text.index("{"):])
                    assert isinstance(err_obj, dict) and err_obj.get("error")

            assert len((await client.list_tools()).tools) == 13
            valid_plan = _extract_call_result_payload(await client.call_tool("plan_need", {"need": dict(NEED_SEED)}))
            assert valid_plan["covered_amount"] == pytest.approx(10.0)
            valid_recipes = _extract_call_result_payload(await client.call_tool("list_recipes", {}))
            assert len(valid_recipes["recipes"]) >= 1
            valid_policies = _extract_call_result_payload(await client.call_tool("list_policies", {}))
            assert len(valid_policies["policies"]) >= 1

    asyncio.run(_run_client_session())


def test_streamable_http_mcp_bad_envelopes_headers_and_13_tool_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(create_app(with_mcp=True), raise_server_exceptions=False) as tc:
        for raw_body in (b"", b"{", b"NaN", b"Infinity", b"\xff\xfe"):
            r = tc.post("/mcp", content=raw_body, headers=_McpHttpSession.BASE_HEADERS)
            assert r.status_code < 500, (raw_body, r.status_code, r.text[:200])

        sess = _McpHttpSession(tc)
        sess.initialize(H)
        listed = sess.rpc("tools/list", {}, headers=H)
        assert len(listed["result"]["tools"]) == 13
        assert {t["name"] for t in listed["result"]["tools"]} == EXPECTED_13_TOOLS

        for tool_name in sorted(EXPECTED_13_TOOLS):
            is_err, txt = sess.call_tool(tool_name, "not-an-object", headers=H)
            assert is_err, (tool_name, txt)
            for marker in FORBIDDEN_TEXT_MARKERS:
                assert marker not in txt, (tool_name, txt[:200])

        bad_header_calls: list[tuple[str, dict[str, Any], dict[str, str], str]] = [
            ("search_products", {"query": "milk"}, {"X-Customer-Id": "unknown-customer", "X-Agent-Id": "buyer-1"}, "unknown_customer"),
            ("list_policies", {}, {"X-Customer-Id": "../etc/passwd", "X-Agent-Id": "buyer-1"}, "unknown_customer"),
            ("plan_need", {"need": dict(NEED_SEED)}, {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}, "customer_mismatch"),
            ("create_quote", {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1}]}, {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "../etc/passwd"}, "invalid_agent_id"),
            ("place_order", {"quote_id": "Q-none", "quote_hash": "0" * 64}, {"X-Customer-Id": "unknown-customer", "X-Agent-Id": "buyer-1"}, "unknown_customer"),
            ("plan_recipe", {"recipe_id": "oat-porridge", "portions": 10}, {"X-Customer-Id": "unknown-customer", "X-Agent-Id": "buyer-1"}, "unknown_customer"),
            ("check_standing_order_drift", {}, {"X-Customer-Id": "unknown-customer", "X-Agent-Id": "buyer-1"}, "unknown_customer"),
        ]
        for tool_name, args, hdrs, expected_code in bad_header_calls:
            is_err, txt = sess.call_tool(tool_name, args, headers=hdrs)
            assert is_err, (tool_name, hdrs, txt)
            assert "{" in txt, (tool_name, txt)
            err_obj = _parse_strict_json_str(txt[txt.index("{"):])
            assert err_obj["error"] == expected_code

        monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora:secret-mcp-key")
        is_err_missing, txt_missing = sess.call_tool("list_policies", {}, headers=H)
        assert is_err_missing and _parse_strict_json_str(txt_missing[txt_missing.index("{"):])["error"] == "agent_key_required"
        is_err_wrong, txt_wrong = sess.call_tool("list_policies", {}, headers={**H, "X-Agent-Key": "wrong"})
        assert is_err_wrong and _parse_strict_json_str(txt_wrong[txt_wrong.index("{"):])["error"] == "invalid_agent_key"
        is_err_ok, txt_ok = sess.call_tool("list_policies", {}, headers={**H, "X-Agent-Key": "secret-mcp-key"})
        assert not is_err_ok and len(_parse_strict_json_str(txt_ok)["policies"]) >= 1
        monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

        noisy_headers = {**H, "X-Aimo-Now": "not-a-valid-timestamp", "X-Aimo-Faults": "unknown_fault:123"}
        ok_q_err, ok_q_txt = sess.call_tool(
            "create_quote",
            {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 5}]},
            headers=noisy_headers,
        )
        assert not ok_q_err, ok_q_txt
        quote = _parse_strict_json_str(ok_q_txt)
        assert math.isfinite(quote["total_net"]) and quote["total_net"] > 0

        ok_o_err, ok_o_txt = sess.call_tool(
            "place_order",
            {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
            headers=H,
        )
        assert not ok_o_err, ok_o_txt
        order = _parse_strict_json_str(ok_o_txt)

        ok_s_err, ok_s_txt = sess.call_tool("get_order_status", {"order_id": order["order_id"]}, headers=H)
        assert not ok_s_err and _parse_strict_json_str(ok_s_txt)["order_id"] == order["order_id"]

        ok_a_err, ok_a_txt = sess.call_tool("get_audit_trail", {"subject_id": order["order_id"]}, headers=H)
        assert not ok_a_err and _parse_strict_json_str(ok_a_txt)["verify"]["ok"] is True
