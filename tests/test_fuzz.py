"""Fuzz every /v1 route (table-driven, no hypothesis dependency): wrong types, missing fields, huge strings,
unicode, negative numbers and unknown ids. No request may produce a 500, and every error is JSON with
`error` + `message`. Offline: recorded mock-API fixtures (tests/test_gateway.FixtureClient). Owner: harden (S2).

Run: uv run pytest -q tests/test_fuzz.py
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store

from test_gateway import H, FixtureClient  # noqa: E402  (tests/ is on sys.path under pytest rootdir)

HUGE = "x" * 20_000
BAD_SCALARS: list[Any] = [None, "", HUGE, "ü🥛ñ‮\x00", -1, -1e308, 1e308, 0, 2.5, True, [], {}, "2026-13-45",
                          "' OR 1=1 --", "../../etc/passwd"]
BAD_PATH = ["does-not-exist", "-1", "0", "ü🥛", "x" * 2000, "%00", "..", "Q-20261114-deadbeef", "999999999999"]
BAD_QUERY = ["", "-1", "abc", "x" * 5000, "ü🥛", "1e309", "true"]
# Valid-looking seeds so mutations reach deeper than the first missing field.
SEEDS: dict[str, Any] = {
    "/v1/offers": {"product_ids": ["464306"], "requested_date": "2026-11-16"},
    "/v1/plan": {"customer_id": "hotel-aurora", "product_query": "milk", "quantity": 10, "unit": "L",
                 "delivery_by": "2026-11-16"},
    "/v1/quotes": {"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
    "/v1/orders": {"quote_id": "Q-x", "quote_hash": "0" * 64},
    "/v1/agent/runs": {"text": "10 l milk", "customer_id": "hotel-aurora"},
}
QUERY_SEEDS: dict[str, dict[str, str]] = {"/v1/products/search": {"q": "milk"}}
STATUS_OK_ERRORS = {400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429, 501, 503}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)


@pytest.fixture
def app(monkeypatch):
    from aimo.buyer import runs
    from aimo.gateway.app import create_app

    started: list[Any] = []

    class _Stub:  # never start a real agent thread from the fuzzer
        run_id = "run-fuzz"

    monkeypatch.setattr(runs.registry, "start", lambda req, **kw: started.append(req) or _Stub())
    # Deterministic planner (FTS + offers + ranking, no Gemini/embeddings): same input handling, seconds not minutes.
    from types import SimpleNamespace
    from aimo.gateway import service
    monkeypatch.setattr(service, "_catalog", lambda: SimpleNamespace(plan_need=service._plan_deterministic))
    return create_app(with_mcp=False)


def _routes(app) -> list[tuple[str, str, dict]]:
    spec = app.openapi()
    out = []
    for path, ops in spec["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, op in ops.items():
            out.append((method.upper(), path, op))
    return out


def _resolve(spec: dict, schema: dict) -> dict:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _body_fields(spec: dict, op: dict) -> list[str]:
    try:
        sch = op["requestBody"]["content"]["application/json"]["schema"]
    except KeyError:
        return []
    return list(_resolve(spec, sch).get("properties", {}))


def _fill(path: str, value: str) -> str:
    return re.sub(r"\{[^}]+\}", value, path)


def _cases(app) -> Iterator[tuple[str, str, dict]]:
    spec = app.openapi()
    for method, path, op in _routes(app):
        templ = "{" in path
        seed_path = _fill(path, "unknown-id")
        query = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
        # 1) path params
        if templ:
            for v in BAD_PATH:
                yield method, _fill(path, v), {}
        # 2) query params
        qseed = QUERY_SEEDS.get(path, {})
        for q in query:
            for v in BAD_QUERY:
                yield method, seed_path, {"params": {**qseed, q: v}}
        if method != "POST":
            continue
        seed = SEEDS.get(path, {})
        # 3) whole-body garbage
        for raw in (b"", b"{", b"null", b"[]", b'"str"', b"42", b"\xff\xfe", json.dumps({"x": HUGE}).encode(),
                    b'{"a":' * 200 + b"1" + b"}" * 200):
            yield method, seed_path, {"content": raw, "headers": {"content-type": "application/json"}}
        # 4) per-field mutations on the seed
        for f in _body_fields(spec, op):
            yield method, seed_path, {"json": {k: v for k, v in seed.items() if k != f}}
            for v in BAD_SCALARS:
                yield method, seed_path, {"json": {**seed, f: v}}
        # 5) nested quote-line mutations
        if path == "/v1/quotes":
            for f in ("product_id", "qty_sales_units", "tier", "flags"):
                for v in BAD_SCALARS:
                    yield method, seed_path, {"json": {**seed, "lines": [{**seed["lines"][0], f: v}]}}


def _check(r, method: str, url: str) -> str | None:
    if r.status_code < 400:
        return None
    where = f"{method} {url[:80]} -> {r.status_code}"
    if r.status_code not in STATUS_OK_ERRORS:
        return f"{where}: unexpected status {r.text[:200]}"
    try:
        body = r.json()
    except ValueError:
        return f"{where}: non-JSON error body {r.text[:120]!r}"
    if not isinstance(body, dict) or not body.get("error") or not body.get("message"):
        return f"{where}: error body lacks error/message {str(body)[:200]}"
    return None


@pytest.mark.parametrize("ident", [H, {}], ids=["identified", "anonymous"])
def test_fuzz_every_v1_route_no_500(app, ident):
    api = TestClient(app, headers=ident, raise_server_exceptions=False)
    problems: list[str] = []
    slow: dict[str, float] = {}
    n = 0
    for method, url, kw in _cases(app):
        n += 1
        t0 = time.time()
        r = api.request(method, url, **kw)
        slow[f"{method} {url[:40]}"] = slow.get(f"{method} {url[:40]}", 0) + time.time() - t0
        p = _check(r, method, url)
        if p:
            problems.append(p + f"  kw={str(kw)[:160]}")
    print("slowest:", sorted(slow.items(), key=lambda kv: -kv[1])[:8])
    assert n > 300, n
    assert not problems, f"{len(problems)} of {n} fuzz requests misbehaved:\n" + "\n".join(problems[:40])
