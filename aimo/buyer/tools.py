"""Tool transports for the buyer agent. One interface, three implementations:

- MCPTransport (default): official `mcp` SDK client over streamable HTTP to the gateway's /mcp.
  Tools and their JSON schemas come from the server's tools/list, so any MCP server that
  speaks the same tool names works. Also accepts an in-process MCPServer object (tests).
- InProcessTransport: calls aimo.gateway.service functions directly (fallback when the HTTP
  server is not running but the gateway code is importable).
- FakeTransport: aimo.buyer.fakes.FakeGateway, for tests and offline demos only.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from aimo.buyer.tool_schemas import TOOL_NAMES, TOOL_SCHEMAS


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


class ToolError(Exception):
    pass


class ToolTransport(Protocol):
    kind: str

    def list_tools(self) -> list[ToolSpec]: ...
    def call(self, name: str, args: dict) -> Any: ...
    def close(self) -> None: ...


def jsonable(x: Any) -> Any:
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    if isinstance(x, (list, tuple)):
        return [jsonable(i) for i in x]
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    return x


# ---------------------------------------------------------------- fake
class FakeTransport:
    kind = "fake"

    def __init__(self, gateway):
        self.gw = gateway

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS]

    def call(self, name: str, args: dict) -> Any:
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool {name}")
        return jsonable(self.gw.call(name, args))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- in-process gateway
class InProcessTransport:
    """Calls aimo.gateway.service.<tool_name>(**args). Identity is passed to any parameter
    named agent_id / customer_id / mandate / identity that the function declares."""
    kind = "inproc"

    def __init__(self, agent_id: str, customer_id: str, mandate=None):
        import aimo.gateway.service as service  # raises ImportError if gateway not landed yet
        self.service = service
        self.agent_id, self.customer_id, self.mandate = agent_id, customer_id, mandate
        missing = [n for n in TOOL_NAMES if not hasattr(service, n)]
        if len(missing) == len(TOOL_NAMES):
            raise ImportError("aimo.gateway.service exposes none of the tool functions")
        self.missing = missing

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS
                if t["name"] not in self.missing]

    def call(self, name: str, args: dict) -> Any:
        fn = getattr(self.service, name, None)
        if fn is None:
            raise ToolError(f"tool {name} not available in-process")
        params = inspect.signature(fn).parameters
        kw = dict(args)
        ident = {"agent_id": self.agent_id, "customer_id": self.customer_id,
                 "mandate": self.mandate, "identity": {"agent_id": self.agent_id, "customer_id": self.customer_id}}
        for k, v in ident.items():
            if k in params and k not in kw and v is not None:
                kw[k] = v
        # Coerce dict args into pydantic models when the function is annotated that way.
        for k, p in params.items():
            ann = p.annotation
            if k in kw and isinstance(kw[k], dict) and inspect.isclass(ann) and hasattr(ann, "model_validate"):
                kw[k] = ann.model_validate(kw[k])
        res = fn(**kw)
        if inspect.isawaitable(res):
            res = asyncio.run(res)
        return jsonable(res)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- MCP
class MCPTransport:
    """Sync facade over the async mcp.Client. A private event loop thread owns the client
    session (anyio requires enter/exit in the same task); calls are submitted to it."""
    kind = "mcp"

    def __init__(self, server: Any = "http://localhost:8000/mcp", headers: dict | None = None,
                 timeout: float = 60.0):
        self.server, self.headers, self.timeout = server, headers or {}, timeout
        self._loop = asyncio.new_event_loop()
        self._ready: Future = Future()
        self._queue: asyncio.Queue | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-client")
        self._thread.start()
        self._tools: list[ToolSpec] = self._ready.result(timeout=timeout)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001
            if not self._ready.done():
                self._ready.set_exception(e)

    async def _main(self) -> None:
        from mcp import Client
        self._queue = asyncio.Queue()
        if isinstance(self.server, str):
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
            http = create_mcp_http_client(headers=self.headers)
            async with http:
                async with Client(streamable_http_client(self.server, http_client=http)) as client:
                    await self._serve(client)
        else:  # in-process MCPServer / Server (tests)
            async with Client(self.server) as client:
                await self._serve(client)

    async def _serve(self, client) -> None:
        tools, cursor = [], None
        while True:
            res = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
            tools += res.tools
            cursor = getattr(res, "next_cursor", None)
            if not cursor:
                break
        self._ready.set_result([ToolSpec(t.name, t.description or "", t.input_schema or {}) for t in tools])
        while True:
            item = await self._queue.get()
            if item is None:
                return
            name, args, fut = item
            try:
                r = await client.call_tool(name, args)
                fut.set_result(r)
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def call(self, name: str, args: dict) -> Any:
        fut: Future = Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, args, fut))
        r = fut.result(timeout=self.timeout)
        return _parse_tool_result(r)

    def close(self) -> None:
        if self._queue is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join(timeout=5)


def _parse_tool_result(r) -> Any:
    text = "\n".join(getattr(c, "text", "") or "" for c in (r.content or []))
    if r.is_error:
        raise ToolError(text or "tool error")
    sc = r.structured_content
    if sc is not None:
        # FastMCP wraps non-object returns as {"result": ...}
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text}


# ---------------------------------------------------------------- factory
def connect(kind: str, *, agent_id: str, customer_id: str, mcp_url: str = "http://localhost:8000/mcp",
            gateway=None, mandate=None) -> ToolTransport:
    """kind: mcp | inproc | fake | auto (mcp, then inproc, then fake)."""
    headers = {"X-Agent-Id": agent_id, "X-Customer-Id": customer_id}
    errors = []
    order = [kind] if kind != "auto" else ["mcp", "inproc", "fake"]
    for k in order:
        try:
            if k == "mcp":
                return MCPTransport(mcp_url, headers=headers, timeout=20)
            if k == "inproc":
                return InProcessTransport(agent_id, customer_id, mandate)
            if k == "fake":
                from aimo.buyer.fakes import FakeGateway
                return FakeTransport(gateway or FakeGateway(customer_id, agent_id))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{k}: {type(e).__name__}: {e}"[:300])
    raise ToolError("no tool transport available: " + "; ".join(errors))
