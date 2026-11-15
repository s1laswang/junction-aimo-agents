from __future__ import annotations

import asyncio
from concurrent.futures import Future
import copy
from datetime import date, datetime, timezone
from enum import Enum
import gc
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
import time
import tokenize
from types import SimpleNamespace
from typing import Any

from google.genai import types
from mcp.server import MCPServer
import pytest

from aimo.buyer import tool_schemas, tools
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.tool_schemas import TOOL_NAMES, TOOL_SCHEMAS, to_gemini_schema
from aimo.buyer.tools import (
    FakeTransport,
    MCPTransport,
    ToolError,
    _parse_tool_result,
    connect,
    jsonable,
)
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Mandate, Need

REF_HASH = "\x23"
DEAD_MCP = "http://127.0.0.1:9/mcp"

FORBIDDEN_SCHEMA_KEYS = {
    "$ref",
    "$defs",
    "definitions",
    "allOf",
    "const",
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


def _build_fake_mcp_server() -> MCPServer:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1")
    srv = MCPServer("aimo-buyer-hardening-test")

    @srv.tool()
    def search_products(query: str, claims: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Search catalog products."""
        return gw.search_products(query, claims, limit)

    @srv.tool()
    def list_policies() -> dict:
        """Return customer policies."""
        return gw.list_policies()

    return srv


def test_owned_files_hygiene_zero_comments_and_dashes() -> None:
    owned_files = (
        Path(tools.__file__),
        Path(tool_schemas.__file__),
        Path(__file__),
    )
    for path in owned_files:
        src = path.read_text(encoding="utf-8")
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert len(comments) == 0, f"Unexpected comments in {path}: {comments}"
        assert chr(35) not in src, f"Unexpected hash character in {path}"
        assert chr(0x2014) not in src, f"Unexpected em dash in {path}"
        assert chr(0x2013) not in src, f"Unexpected en dash in {path}"


def test_mcp_transport_close_idempotent_and_closes_loop_without_fd_leak() -> None:
    srv = _build_fake_mcp_server()
    t = MCPTransport(srv, timeout=15.0)
    assert t._closed is False
    assert t._thread.is_alive() is True
    assert t._loop.is_running() is True
    assert t._loop.is_closed() is False

    policies = t.call("list_policies", {})
    assert isinstance(policies, dict)

    t.close()
    assert t._closed is True
    assert t._thread.is_alive() is False
    assert t._loop.is_running() is False
    assert t._loop.is_closed() is True

    t.close()
    t.close()
    assert t._closed is True
    assert t._loop.is_closed() is True

    fd_dir = Path("/proc/self/fd")
    if fd_dir.exists():
        w0 = MCPTransport(srv, timeout=15.0)
        w0.close()
        gc.collect()
        baseline_fds = len(os.listdir(fd_dir))
        for _ in range(10):
            inst = MCPTransport(srv, timeout=15.0)
            res = inst.call("search_products", {"query": "milk"})
            assert isinstance(res, list)
            inst.close()
            assert inst._loop.is_closed() is True
        for _ in range(4):
            with pytest.raises(Exception):
                MCPTransport(DEAD_MCP, timeout=1.5)
        gc.collect()
        after_fds = len(os.listdir(fd_dir))
        assert after_fds <= baseline_fds + 3, (baseline_fds, after_fds)


def test_mcp_transport_call_after_close_or_dead_worker_raises_clean_tool_error() -> None:
    srv = _build_fake_mcp_server()
    t = MCPTransport(srv, timeout=15.0)
    t.close()

    with pytest.raises(ToolError, match="MCPTransport is closed"):
        t.call("list_policies", {})

    t2 = MCPTransport(srv, timeout=15.0)
    try:
        assert t2._queue is not None
        t2._loop.call_soon_threadsafe(t2._queue.put_nowait, None)
        t2._thread.join(timeout=5.0)
        assert not t2._thread.is_alive()
        assert t2._closed is False
        with pytest.raises(ToolError, match="MCPTransport is closed"):
            t2.call("list_policies", {})
    finally:
        t2.close()
        assert t2._loop.is_closed() is True


def test_mcp_transport_serve_exit_fails_queued_futures_without_hanging() -> None:
    async def _exercise_direct_queue_drain() -> None:
        stub = MCPTransport.__new__(MCPTransport)
        stub._closed = False
        stub._lock = threading.Lock()
        stub._pending = set()
        stub._ready = Future()
        stub._queue = asyncio.Queue()

        fut_after_sentinel: Future = Future()
        fut_second: Future = Future()
        stub._queue.put_nowait(None)
        stub._queue.put_nowait(("list_policies", {}, fut_after_sentinel))
        stub._queue.put_nowait(("search_products", {"query": "milk"}, fut_second))

        class _FakeClient:
            async def list_tools(self, cursor: str | None = None) -> Any:
                return SimpleNamespace(tools=[], next_cursor=None)

            async def call_tool(self, name: str, args: dict) -> Any:
                return SimpleNamespace(content=[], is_error=False, structured_content={})

        await stub._serve(_FakeClient())
        assert fut_after_sentinel.done()
        assert fut_second.done()
        with pytest.raises(ToolError, match="MCPTransport is closed"):
            fut_after_sentinel.result(timeout=0.1)
        with pytest.raises(ToolError, match="MCPTransport is closed"):
            fut_second.result(timeout=0.1)

    async def _exercise_crash_drain() -> None:
        stub = MCPTransport.__new__(MCPTransport)
        stub._closed = False
        stub._lock = threading.Lock()
        stub._pending = set()
        stub._ready = Future()
        stub._queue = asyncio.Queue()

        queued_fut: Future = Future()
        stub._queue.put_nowait(("list_policies", {}, queued_fut))

        class _BrokenClient:
            async def list_tools(self, cursor: str | None = None) -> Any:
                raise RuntimeError("list_tools transport failure")

        with pytest.raises(RuntimeError, match="list_tools transport failure"):
            await stub._serve(_BrokenClient())
        assert queued_fut.done()
        with pytest.raises(ToolError, match="MCPTransport is closed"):
            queued_fut.result(timeout=0.1)

    asyncio.run(_exercise_direct_queue_drain())
    asyncio.run(_exercise_crash_drain())

    entered = threading.Event()
    release = threading.Event()
    srv = MCPServer("aimo-buyer-slow-close-test")

    @srv.tool()
    async def blocking_tool(step: int) -> dict[str, int]:
        """Slow tool used to queue concurrent calls."""
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"step": step}

    t = MCPTransport(srv, timeout=15.0)
    outcomes: list[Any] = []
    outcomes_lock = threading.Lock()

    def _caller(step: int) -> None:
        try:
            val = t.call("blocking_tool", {"step": step})
            with outcomes_lock:
                outcomes.append(("ok", step, val))
        except BaseException as exc:
            with outcomes_lock:
                outcomes.append(("err", step, exc))

    w1 = threading.Thread(target=_caller, args=(1,), daemon=True)
    w1.start()
    assert entered.wait(timeout=5.0)

    t.close()
    w2 = threading.Thread(target=_caller, args=(2,), daemon=True)
    w2.start()
    release.set()

    start = time.monotonic()
    w1.join(timeout=5.0)
    w2.join(timeout=5.0)
    elapsed = time.monotonic() - start
    t.close()

    assert not w1.is_alive()
    assert not w2.is_alive()
    assert elapsed < 4.0
    step2 = [item for item in outcomes if item[1] == 2]
    assert len(step2) == 1
    assert step2[0][0] == "err"
    assert isinstance(step2[0][2], ToolError)


def test_parse_tool_result_snake_and_camel_case_and_nan_infinity_constants() -> None:
    snake_err = SimpleNamespace(
        content=[SimpleNamespace(text="bad request snake")],
        is_error=True,
        structured_content=None,
    )
    with pytest.raises(ToolError, match="bad request snake"):
        _parse_tool_result(snake_err)

    camel_err = SimpleNamespace(
        content=[SimpleNamespace(text="bad request camel")],
        isError=True,
        structuredContent=None,
    )
    with pytest.raises(ToolError, match="bad request camel"):
        _parse_tool_result(camel_err)

    empty_camel_err = SimpleNamespace(content=[], isError=True)
    with pytest.raises(ToolError, match="tool error"):
        _parse_tool_result(empty_camel_err)

    snake_sc_wrapped = SimpleNamespace(
        content=[],
        is_error=False,
        structured_content={"result": [{"id": "464306"}]},
    )
    assert _parse_tool_result(snake_sc_wrapped) == [{"id": "464306"}]

    snake_sc_empty_list = SimpleNamespace(
        content=[SimpleNamespace(text="ignored")],
        is_error=False,
        structured_content=[],
    )
    assert _parse_tool_result(snake_sc_empty_list) == []

    camel_sc_wrapped = SimpleNamespace(
        content=[],
        isError=False,
        structuredContent={"result": {"status": "placed"}},
    )
    assert _parse_tool_result(camel_sc_wrapped) == {"status": "placed"}

    camel_sc_plain = SimpleNamespace(
        content=[],
        isError=False,
        structuredContent={"quote_id": "Q-1", "ok": True},
    )
    assert _parse_tool_result(camel_sc_plain) == {"quote_id": "Q-1", "ok": True}

    for bad_const_text in (
        "NaN",
        "Infinity",
        "-Infinity",
        '{"price": NaN}',
        '{"qty": Infinity}',
        '{"delta": -Infinity}',
    ):
        msg = SimpleNamespace(content=[SimpleNamespace(text=bad_const_text)], isError=False)
        parsed = _parse_tool_result(msg)
        assert parsed == {"text": bad_const_text}
        json.dumps(parsed, allow_nan=False)

    valid_json_with_nan_word = SimpleNamespace(
        content=[SimpleNamespace(text='{"note": "NaN string literal", "qty": 12}')],
        isError=False,
    )
    assert _parse_tool_result(valid_json_with_nan_word) == {
        "note": "NaN string literal",
        "qty": 12,
    }
    assert _parse_tool_result(None) == {"text": ""}


def test_jsonable_handles_enum_path_sets_dates_pydantic_and_nested_containers() -> None:
    class TierEnum(str, Enum):
        BEST = "best"
        GOOD = "good"

    class CodeEnum(int, Enum):
        TEN = 10
        TWO = 2

    class NestedEnum(Enum):
        TARGET = PurePosixPath("/var/data/catalog.json")

    class LegacyModel:
        def model_dump(self) -> dict[str, Any]:
            return {
                "paths": {PurePosixPath("/b/file"), PurePosixPath("/a/file")},
                "tier": TierEnum.BEST,
            }

    need = Need(
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        quantity=20,
        unit="L",
        delivery_by="2026-11-16",
    )
    mandate = Mandate(
        customer_id="hotel-aurora",
        agent_id="hotel-aurora/buyer-1",
        policy_id="hotel-aurora",
    )

    payload = {
        TierEnum.GOOD: (
            Path("/tmp/aimo/order.json"),
            PureWindowsPath("C:/orders/q1.json"),
            NestedEnum.TARGET,
        ),
        "str_set": {"zeta", "alpha", "mu"},
        "num_frozenset": frozenset({10, 2, 1}),
        "enum_set": {CodeEnum.TEN, CodeEnum.TWO},
        "mixed_set": {1, "alpha", None, False},
        "dates": (
            date(2026, 11, 16),
            datetime(2026, 11, 15, 8, 30, tzinfo=timezone.utc),
        ),
        "pydantic_need": need,
        "pydantic_mandate": mandate,
        "legacy_dump": LegacyModel(),
        "model_class_passthrough_guard": {"ok": True},
    }

    converted = jsonable(payload)
    assert converted["good"] == [
        "/tmp/aimo/order.json",
        "C:\\orders\\q1.json" if os.name == "nt" else str(PureWindowsPath("C:/orders/q1.json")),
        "/var/data/catalog.json",
    ]
    assert converted["str_set"] == ["alpha", "mu", "zeta"]
    assert converted["num_frozenset"] == [1, 2, 10]
    assert converted["enum_set"] == [2, 10]
    assert converted["mixed_set"] == [None, False, 1, "alpha"]
    assert converted["dates"] == ["2026-11-16", "2026-11-15T08:30:00+00:00"]
    assert converted["pydantic_need"]["customer_id"] == "hotel-aurora"
    assert converted["pydantic_mandate"]["policy_id"] == "hotel-aurora"
    assert converted["legacy_dump"] == {
        "paths": ["/a/file", "/b/file"],
        "tier": "best",
    }
    serialized = json.dumps(converted, allow_nan=False, sort_keys=True)
    assert json.loads(serialized)["str_set"] == ["alpha", "mu", "zeta"]
    assert jsonable(Need) is Need


def test_connect_normalizes_kind_and_rejects_unknown_kind() -> None:
    for raw_fake in ("fake", "FAKE", "  Fake \n\t "):
        t = connect(raw_fake, agent_id="hotel-aurora/buyer-1", customer_id="hotel-aurora")
        try:
            assert isinstance(t, FakeTransport)
            assert t.kind == "fake"
        finally:
            t.close()

    t_auto = connect(
        "  AUTO  ",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
    )
    try:
        assert t_auto.kind in ("inproc", "fake")
        assert len(t_auto.list_tools()) >= 9
    finally:
        t_auto.close()

    bad_kinds: tuple[Any, ...] = ("bogus", "http", "stdio", "", "   ", None, 42)
    for bad_kind in bad_kinds:
        with pytest.raises(ToolError, match="unknown tool transport kind"):
            connect(bad_kind, agent_id="buyer-1", customer_id="hotel-aurora")


def test_mcp_transport_serve_camelcase_pagination_and_uninitialized_call_guard() -> None:
    bare = MCPTransport.__new__(MCPTransport)
    with pytest.raises(ToolError, match="MCPTransport is closed"):
        bare.call("list_policies", {})
    bare.close()

    async def _exercise_pagination() -> None:
        stub = MCPTransport.__new__(MCPTransport)
        stub._closed = False
        stub._lock = threading.Lock()
        stub._pending = set()
        stub._ready = Future()
        stub._queue = asyncio.Queue()
        stub._queue.put_nowait(None)

        class _PagedCamelClient:
            async def list_tools(self, cursor: str | None = None) -> Any:
                if cursor is None:
                    t1 = SimpleNamespace(
                        name="tool_one",
                        description="First page tool",
                        inputSchema={"type": "object", "properties": {"a": {"type": "string"}}},
                    )
                    return SimpleNamespace(tools=[t1], nextCursor="page-2")
                assert cursor == "page-2"
                t2 = SimpleNamespace(
                    name="tool_two",
                    description=None,
                    inputSchema=None,
                )
                return SimpleNamespace(tools=[t2], nextCursor=None)

        await stub._serve(_PagedCamelClient())
        specs = stub._ready.result(timeout=0.1)
        assert [s.name for s in specs] == ["tool_one", "tool_two"]
        assert specs[0].input_schema == {"type": "object", "properties": {"a": {"type": "string"}}}
        assert specs[1].description == ""
        assert specs[1].input_schema == {}

    asyncio.run(_exercise_pagination())


def test_to_gemini_schema_recursive_tree_items_optional_and_allof_cycle() -> None:
    ref_prefix = f"{REF_HASH}/$defs/"
    tree_schema = {
        "$defs": {
            "TreeNode": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "parent": {
                        "anyOf": [
                            {"$ref": f"{ref_prefix}TreeNode"},
                            {"type": "null"},
                        ]
                    },
                    "children": {
                        "type": "array",
                        "items": {"$ref": f"{ref_prefix}TreeNode"},
                    },
                    "composed_self": {
                        "allOf": [
                            {"$ref": f"{ref_prefix}TreeNode"},
                            {"properties": {"tag": {"const": "leaf"}}},
                        ]
                    },
                },
                "required": ["id"],
            }
        },
        "$ref": f"{ref_prefix}TreeNode",
    }
    cleaned = to_gemini_schema(tree_schema)
    assert _collect_schema_keyword_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)
    assert cleaned["type"] == "object"
    assert cleaned["properties"]["parent"] == {"type": "object"}
    assert cleaned["properties"]["children"] == {"type": "array", "items": {"type": "object"}}
    assert cleaned["properties"]["composed_self"] == {
        "type": "object",
        "properties": {"tag": {"enum": ["leaf"]}},
    }
    assert to_gemini_schema({"type": "string", "const": "solo"}) == {
        "type": "string",
        "enum": ["solo"],
    }
    types.FunctionDeclaration(
        name="tree_tool",
        description="Recursive tree schema",
        parameters_json_schema=cleaned,
    )


def test_tool_schemas_contracts_and_non_dict_inputs() -> None:
    assert TOOL_NAMES == [
        "search_products",
        "get_product",
        "get_offers",
        "plan_need",
        "create_quote",
        "place_order",
        "get_order_status",
        "get_audit_trail",
        "list_policies",
    ]
    assert len(TOOL_SCHEMAS) == 9

    for non_dict in (
        None,
        True,
        False,
        [],
        [{"type": "string"}],
        "",
        "object",
        0,
        99,
        {},
        {"title": "DroppedOnly", "additionalProperties": False},
    ):
        cleaned = to_gemini_schema(non_dict)
        assert cleaned == {"type": "object", "properties": {}}
        types.FunctionDeclaration(
            name="safe_fallback",
            description="Safe fallback schema",
            parameters_json_schema=cleaned,
        )

    bool_subschemas = {
        "type": "object",
        "properties": {
            "accept_anything": True,
            "accept_nothing": False,
            "empty_prop": {},
        },
    }
    cleaned_bool = to_gemini_schema(bool_subschemas)
    assert cleaned_bool == {
        "type": "object",
        "properties": {
            "accept_anything": {"type": "object"},
            "accept_nothing": {"type": "object"},
            "empty_prop": {"type": "object"},
        },
    }
    types.FunctionDeclaration(
        name="bool_props",
        description="Boolean property subschemas",
        parameters_json_schema=cleaned_bool,
    )


def test_to_gemini_schema_cyclic_refs_per_branch_and_diamond_reuse() -> None:
    ref_prefix = f"{REF_HASH}/$defs/"
    schema = {
        "$defs": {
            "Address": {
                "type": "object",
                "title": "Address",
                "properties": {
                    "street": {"type": "string"},
                    "country": {"$ref": f"{ref_prefix}Country"},
                    "fallback_country": {"$ref": f"{ref_prefix}Country"},
                },
                "required": ["street"],
            },
            "Country": {
                "type": "object",
                "properties": {"iso2": {"type": "string"}},
                "required": ["iso2"],
            },
            "CycleA": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "to_b": {"$ref": f"{ref_prefix}CycleB"},
                },
            },
            "CycleB": {
                "type": "object",
                "properties": {
                    "to_c": {"$ref": f"{ref_prefix}CycleC"},
                },
            },
            "CycleC": {
                "type": "object",
                "properties": {
                    "back_to_a": {
                        "$ref": f"{ref_prefix}CycleA",
                        "description": "Back-edge stops here",
                    },
                },
            },
            "AliasLoop1": {"$ref": f"{ref_prefix}AliasLoop2"},
            "AliasLoop2": {"$ref": f"{ref_prefix}AliasLoop1"},
        },
        "type": "object",
        "properties": {
            "billing": {"$ref": f"{ref_prefix}Address"},
            "shipping": {"$ref": f"{ref_prefix}Address"},
            "graph": {"$ref": f"{ref_prefix}CycleA"},
            "alias_loop": {"$ref": f"{ref_prefix}AliasLoop1"},
            "root_self": {"$ref": REF_HASH},
        },
    }
    snapshot = copy.deepcopy(schema)
    cleaned = to_gemini_schema(schema)
    assert schema == snapshot
    assert _collect_schema_keyword_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)

    expected_addr = {
        "type": "object",
        "properties": {
            "street": {"type": "string"},
            "country": {"type": "object", "properties": {"iso2": {"type": "string"}}, "required": ["iso2"]},
            "fallback_country": {"type": "object", "properties": {"iso2": {"type": "string"}}, "required": ["iso2"]},
        },
        "required": ["street"],
    }
    assert cleaned["properties"]["billing"] == expected_addr
    assert cleaned["properties"]["shipping"] == expected_addr

    back_edge = (
        cleaned["properties"]["graph"]["properties"]["to_b"]["properties"]["to_c"]["properties"]["back_to_a"]
    )
    assert back_edge == {"type": "object", "description": "Back-edge stops here"}
    assert "properties" not in back_edge
    assert cleaned["properties"]["alias_loop"] == {"type": "object"}
    assert cleaned["properties"]["root_self"] == {"type": "object"}

    assert to_gemini_schema({"$ref": REF_HASH}) == {"type": "object", "properties": {}}

    decl = types.FunctionDeclaration(
        name="cyclic_and_diamond",
        description="Cyclic and diamond schema test",
        parameters_json_schema=cleaned,
    )
    tool = types.Tool(function_declarations=[decl])
    assert tool.function_declarations is not None and len(tool.function_declarations) == 1


def test_to_gemini_schema_merges_allof_and_converts_const_to_enum() -> None:
    ref_prefix = f"{REF_HASH}/$defs/"
    raw = {
        "$defs": {
            "Identifiable": {
                "type": "object",
                "title": "Identifiable",
                "properties": {
                    "customer_id": {"type": "string"},
                    "nested": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                        "required": ["x"],
                    },
                },
                "required": ["customer_id"],
            },
            "Auditable": {
                "type": "object",
                "properties": {
                    "trace_id": {"type": "string"},
                    "nested": {
                        "type": "object",
                        "properties": {"y": {"type": "string"}},
                        "required": ["y", "x"],
                    },
                },
                "required": ["trace_id", "customer_id"],
            },
        },
        "allOf": [
            {"$ref": f"{ref_prefix}Identifiable"},
            {"$ref": f"{ref_prefix}Auditable"},
            {
                "properties": {
                    "currency": {"type": "string", "const": "EUR"},
                    "zero_const": {"type": "integer", "const": 0},
                    "false_const": {"type": "boolean", "const": False},
                    "empty_str_const": {"type": "string", "const": ""},
                    "untyped_const": {"const": "fixed-token"},
                    "kept_enum": {"type": "string", "const": "EUR", "enum": ["EUR", "SEK"]},
                    "opt_literal": {
                        "anyOf": [
                            {"type": "string", "const": "fast"},
                            {"const": None},
                        ]
                    },
                },
                "required": ["currency"],
            },
        ],
        "description": "Merged request payload",
        "properties": {
            "const": {"type": "string", "description": "Field literally named const"},
            "allOf": {"type": "integer", "description": "Field literally named allOf"},
        },
        "required": ["const"],
    }

    cleaned = to_gemini_schema(raw)
    assert _collect_schema_keyword_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)
    assert cleaned["type"] == "object"
    assert cleaned["description"] == "Merged request payload"
    assert cleaned["required"] == ["customer_id", "trace_id", "currency", "const"]

    props = cleaned["properties"]
    assert props["customer_id"] == {"type": "string"}
    assert props["trace_id"] == {"type": "string"}
    assert props["nested"] == {
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "string"}},
        "required": ["x", "y"],
    }
    assert props["currency"] == {"type": "string", "enum": ["EUR"]}
    assert props["zero_const"] == {"type": "integer", "enum": [0]}
    assert props["false_const"] == {"type": "boolean", "enum": [False]}
    assert props["empty_str_const"] == {"type": "string", "enum": [""]}
    assert props["untyped_const"] == {"enum": ["fixed-token"]}
    assert props["kept_enum"] == {"type": "string", "enum": ["EUR", "SEK"]}
    assert props["opt_literal"] == {"type": "string", "enum": ["fast"]}
    assert props["const"] == {"type": "string", "description": "Field literally named const"}
    assert props["allOf"] == {"type": "integer", "description": "Field literally named allOf"}

    decl = types.FunctionDeclaration(
        name="merged_allof_const_tool",
        description="Merged allOf and const schema",
        parameters_json_schema=cleaned,
    )
    assert decl.name == "merged_allof_const_tool"

    mcp_tools = asyncio.run(build_mcp().list_tools())
    assert len(mcp_tools) == 13
    for t in mcp_tools:
        conv = to_gemini_schema(t.input_schema)
        assert _collect_schema_keyword_keys(conv).isdisjoint(FORBIDDEN_SCHEMA_KEYS)
        types.FunctionDeclaration(name=t.name, description=t.description or "tool", parameters_json_schema=conv)
