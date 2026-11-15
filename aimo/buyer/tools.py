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
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
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


def _sort_jsonable_set(items: list[Any]) -> list[Any]:
    if all(isinstance(i, str) for i in items):
        return sorted(items)
    if all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in items):
        return sorted(items)
    if all(isinstance(i, bool) for i in items):
        return sorted(items)
    return sorted(
        items,
        key=lambda v: (type(v).__qualname__, json.dumps(v, sort_keys=True, default=str)),
    )


def jsonable(x: Any) -> Any:
    if not isinstance(x, type) and hasattr(x, "model_dump") and callable(x.model_dump):
        try:
            dumped = x.model_dump(mode="json")
        except TypeError:
            dumped = x.model_dump()
        return jsonable(dumped)
    if isinstance(x, Enum):
        return jsonable(x.value)
    if isinstance(x, PurePath):
        return str(x)
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if isinstance(x, (list, tuple)):
        return [jsonable(i) for i in x]
    if isinstance(x, (set, frozenset)):
        return _sort_jsonable_set([jsonable(i) for i in x])
    if isinstance(x, dict):
        return {
            (jsonable(k) if isinstance(k, (Enum, PurePath, date, datetime)) else k): jsonable(v)
            for k, v in x.items()
        }
    return x


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


class InProcessTransport:
    """Calls aimo.gateway.service.<tool_name>(**args). Identity is passed to any parameter
    named agent_id / customer_id / mandate / identity that the function declares."""
    kind = "inproc"

    def __init__(self, agent_id: str, customer_id: str, mandate=None, faults: str | None = None):
        import aimo.gateway.service as service
        self.service = service
        self.faults = faults
        self.agent_id, self.customer_id, self.mandate = agent_id, customer_id, mandate
        missing = [n for n in TOOL_NAMES if not hasattr(service, n)]
        if len(missing) == len(TOOL_NAMES):
            raise ImportError("aimo.gateway.service exposes none of the tool functions")
        self.missing = missing

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS
                if t["name"] not in self.missing]

    def call(self, name: str, args: dict) -> Any:
        if getattr(self.service, "__name__", "") == "aimo.gateway.service":
            _ensure_replay_ready()
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
        for k, p in params.items():
            ann = p.annotation
            if k in kw and isinstance(kw[k], dict) and inspect.isclass(ann) and hasattr(ann, "model_validate"):
                kw[k] = ann.model_validate(kw[k])
        token = None
        if self.faults:
            try:
                from aimo.backend import clock
                from aimo.backend.client import set_request_faults
                if clock.request_overrides_allowed(True):
                    token = set_request_faults(self.faults)
            except ImportError:
                pass
        try:
            res = fn(**kw)
        finally:
            if token is not None:
                from aimo.backend.client import reset_request_faults
                reset_request_faults(token)
        if inspect.isawaitable(res):
            res = asyncio.run(res)
        return jsonable(res)

    def close(self) -> None:
        pass


class MCPTransport:
    """Sync facade over the async mcp.Client. A private event loop thread owns the client
    session (anyio requires enter/exit in the same task); calls are submitted to it."""
    kind = "mcp"

    def __init__(self, server: Any = "http://localhost:8000/mcp", headers: dict | None = None,
                 timeout: float = 60.0):
        self.server, self.headers, self.timeout = server, headers or {}, timeout
        self._closed = False
        self._lock = threading.Lock()
        self._pending: set[Future] = set()
        self._loop = asyncio.new_event_loop()
        self._ready: Future = Future()
        self._queue: asyncio.Queue | None = None
        self._tools: list[ToolSpec] = []
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-client")
        self._thread.start()
        try:
            self._tools = self._ready.result(timeout=timeout)
        except BaseException:
            self.close()
            raise

    def _fail_pending_futures(self, message: str = "MCPTransport is closed") -> None:
        q = getattr(self, "_queue", None)
        if q is not None:
            while True:
                try:
                    item = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    continue
                _, _, fut = item
                if not fut.done():
                    fut.set_exception(ToolError(message))
        lock = getattr(self, "_lock", None)
        pending_set = getattr(self, "_pending", None)
        if pending_set is not None:
            if lock is not None:
                with lock:
                    pending = list(pending_set)
                    pending_set.clear()
            else:
                pending = list(pending_set)
                pending_set.clear()
            for fut in pending:
                if not fut.done():
                    fut.set_exception(ToolError(message))

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as e:
            if not self._ready.done():
                self._ready.set_exception(e)
        finally:
            self._fail_pending_futures()
            if not self._ready.done():
                self._ready.set_exception(ToolError("MCPTransport is closed"))

    async def _main(self) -> None:
        from mcp import Client
        self._queue = asyncio.Queue()
        try:
            if isinstance(self.server, str):
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client
                http = create_mcp_http_client(headers=self.headers)
                async with http:
                    async with Client(streamable_http_client(self.server, http_client=http)) as client:
                        await self._serve(client)
            else:
                async with Client(self.server) as client:
                    await self._serve(client)
        finally:
            self._fail_pending_futures()

    async def _serve(self, client) -> None:
        try:
            tools, cursor = [], None
            while True:
                res = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
                tools += res.tools
                cursor = getattr(res, "next_cursor", None) or getattr(res, "nextCursor", None)
                if not cursor:
                    break
            specs = []
            for t in tools:
                schema = getattr(t, "input_schema", None)
                if schema is None:
                    schema = getattr(t, "inputSchema", None)
                specs.append(ToolSpec(t.name, t.description or "", schema if isinstance(schema, dict) else {}))
            if not self._ready.done():
                self._ready.set_result(specs)
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                name, args, fut = item
                if fut.done():
                    continue
                try:
                    r = await client.call_tool(name, args)
                    if not fut.done():
                        fut.set_result(r)
                except asyncio.CancelledError:
                    if not fut.done():
                        fut.set_exception(ToolError("MCPTransport is closed"))
                    raise
                except BaseException as e:
                    if not fut.done():
                        fut.set_exception(e)
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
        finally:
            self._fail_pending_futures()

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def call(self, name: str, args: dict) -> Any:
        fut: Future = Future()
        lock = getattr(self, "_lock", None)
        if lock is None:
            raise ToolError("MCPTransport is closed")
        with lock:
            if (
                self._closed
                or self._queue is None
                or self._thread is None
                or not self._thread.is_alive()
                or self._loop is None
                or self._loop.is_closed()
                or not self._loop.is_running()
            ):
                raise ToolError("MCPTransport is closed")
            self._pending.add(fut)
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, args, fut))
            except RuntimeError as exc:
                self._pending.discard(fut)
                raise ToolError("MCPTransport is closed") from exc
        try:
            r = fut.result(timeout=self.timeout)
        finally:
            with lock:
                self._pending.discard(fut)
        return _parse_tool_result(r)

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is not None:
            with lock:
                self._closed = True
        else:
            self._closed = True
        loop = getattr(self, "_loop", None)
        queue = getattr(self, "_queue", None)
        thread = getattr(self, "_thread", None)
        if queue is not None and loop is not None and not loop.is_closed() and loop.is_running():
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=5)
        self._fail_pending_futures()
        if loop is not None and not loop.is_running() and not loop.is_closed():
            loop.close()


def _reject_json_constant(const: str) -> Any:
    raise ValueError(f"invalid JSON constant: {const}")


def _parse_tool_result(r) -> Any:
    content = getattr(r, "content", None) or []
    text = "\n".join(getattr(c, "text", "") or "" for c in content)
    if bool(getattr(r, "is_error", False) or getattr(r, "isError", False)):
        raise ToolError(text or "tool error")
    sc = getattr(r, "structured_content", None)
    if sc is None:
        sc = getattr(r, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"text": text}


def gateway_agent_id(agent_id: str, customer_id: str) -> str:
    """The gateway composes actors as agent:<customer>/<agent>, so it wants the bare agent name."""
    prefix = customer_id + "/"
    return agent_id[len(prefix):] if agent_id.startswith(prefix) else agent_id


def _has_forced_replay() -> bool:
    from aimo import config
    from aimo.backend import replay
    if replay.mode() not in ("forced", "auto"):
        return False
    rdir = replay.replay_dir()
    if rdir is None or not (rdir / "meta.json").exists():
        return False
    has_real_catalog = (
        config.PRODUCT_JSON.exists()
        and "catalog-fixture" not in config.PRODUCT_JSON.parts
    )
    if has_real_catalog:
        return True
    if os.environ.get("AIMO_CATALOG_FIXTURE", "").strip() == "0":
        return False
    if config.PRODUCT_JSON.exists():
        return True
    from aimo.catalog import FIXTURE_CATALOG_DIR
    return (
        (rdir / "catalog" / (config.PRODUCT_JSON.name + ".gz")).exists()
        or (FIXTURE_CATALOG_DIR / (config.PRODUCT_JSON.name + ".gz")).exists()
    )


_has_replay = _has_forced_replay


def _ensure_replay_ready() -> None:
    from aimo import config
    from aimo.backend import client as backend_client, replay
    if replay.mode() not in ("forced", "auto"):
        return
    rdir = replay.replay_dir()
    if rdir is None or not (rdir / "meta.json").exists():
        return
    if backend_client._client is None or isinstance(backend_client._client, backend_client.ValioClient) or (
        isinstance(backend_client._client, replay.ReplayClient) and backend_client._client.dir != rdir
    ):
        backend_client.set_client(replay.ReplayClient(rdir))
    if not config.PRODUCT_JSON.exists():
        import aimo.catalog as cat
        if cat._use_fixture_catalog() == "fixture-subset":
            cat._SOURCE = "fixture-subset"
    if config.PRODUCT_JSON.exists():
        import aimo.backend.catalog_store as cs
        if cs.PRODUCT_JSON != config.PRODUCT_JSON or cs.CATALOG_DB != config.CATALOG_DB:
            cs.PRODUCT_JSON = config.PRODUCT_JSON
            cs.RELIABILITY_CSV = config.RELIABILITY_CSV
            cs.CO2_CSV = config.CO2_CSV
            cs.CATALOG_DB = config.CATALOG_DB
            from aimo.catalog import textsearch
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()


def connect(kind: str, *, agent_id: str, customer_id: str, mcp_url: str = "http://localhost:8000/mcp",
            gateway=None, mandate=None, faults: str | None = None) -> ToolTransport:
    """kind: mcp | inproc | fake | auto (mcp, then inproc, then fake).
    faults: optional X-Aimo-Faults spec for the gateway's fault injection (demo scenarios)."""
    if not isinstance(kind, str):
        raise ToolError(f"unknown tool transport kind: {kind!r}")
    norm_kind = kind.strip().lower()
    if norm_kind not in ("mcp", "inproc", "fake", "auto"):
        raise ToolError(f"unknown tool transport kind: {kind!r}")
    kind = norm_kind
    agent_id = gateway_agent_id(agent_id, customer_id)
    headers = {"X-Agent-Id": agent_id, "X-Customer-Id": customer_id}
    if faults:
        headers["X-Aimo-Faults"] = faults
    errors = []
    order = [kind] if kind != "auto" else ["mcp", "inproc", "fake"]
    for k in order:
        try:
            if k == "mcp":
                if kind == "auto":
                    import httpx
                    httpx.get(mcp_url, timeout=1.5)
                return MCPTransport(mcp_url, headers=headers, timeout=60)
            if k == "inproc":
                if kind == "auto":
                    import aimo.backend.client as backend_client
                    from aimo import config
                    from aimo.backend import replay
                    has_custom_client = (
                        backend_client._client is not None
                        and not isinstance(backend_client._client, (backend_client.ValioClient, replay.ReplayClient))
                    )
                    has_real_catalog = (
                        config.PRODUCT_JSON.exists()
                        and (
                            "catalog-fixture" not in config.PRODUCT_JSON.parts
                            or os.environ.get("AIMO_CATALOG_FIXTURE", "").strip() == "1"
                        )
                    )
                    is_offline = bool(config.OFFLINE or os.environ.get("AIMO_OFFLINE", "0") == "1")
                    has_key = bool((config.AIMO_API_KEY or os.environ.get("VALIO_API_KEY") or "").strip() or is_offline)
                    if not (has_custom_client or (has_real_catalog and has_key) or _has_forced_replay()):
                        raise ToolError("in-process gateway needs challenge catalog data and AIMO_API_KEY")
                return InProcessTransport(agent_id, customer_id, mandate, faults=faults)
            if k == "fake":
                from aimo.buyer.fakes import FakeGateway
                return FakeTransport(gateway or FakeGateway(customer_id, agent_id))
        except Exception as e:
            errors.append(f"{k}: {type(e).__name__}: {e}"[:300])
    raise ToolError("no tool transport available: " + "; ".join(errors))
