"""Fuzz every /v1 route (table-driven, no hypothesis dependency): wrong types, missing fields, huge strings,
unicode, negative numbers and unknown ids. No request may produce a 500, and every error is JSON with
`error` + `message`. Offline: recorded mock-API fixtures (tests/test_gateway.FixtureClient).

Run: uv run pytest -q tests/test_fuzz.py
"""
from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog, human_auth, service
from test_gateway import FixtureClient, H

HUGE = "x" * 20_000
BAD_SCALARS: list[Any] = [
    None,
    "",
    "   ",
    HUGE,
    "ü🥛ñ\u202e\x00",
    -1,
    -1e308,
    1e308,
    0,
    2.5,
    True,
    [],
    {},
    "2026-13-45",
    "' OR 1=1 --",
    "../../etc/passwd",
]
BAD_PATH = ["does-not-exist", "-1", "0", "ü🥛", "x" * 2000, "%00", "..", "Q-20261114-deadbeef", "999999999999"]
BAD_QUERY = ["", "   ", "-1", "abc", "x" * 5000, "ü🥛", "1e309", "true"]
SEEDS: dict[str, Any] = {
    "/v1/offers": {"product_ids": ["464306"], "requested_date": "2026-11-16"},
    "/v1/plan": {
        "customer_id": "hotel-aurora",
        "product_query": "milk",
        "quantity": 10,
        "unit": "L",
        "delivery_by": "2026-11-16",
    },
    "/v1/quotes": {"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
    "/v1/orders": {"quote_id": "Q-x", "quote_hash": "0" * 64},
    "/v1/agent/runs": {"text": "10 l milk", "customer_id": "hotel-aurora"},
    "/v1/recipes/plan": {
        "recipe_id": "oat-porridge",
        "portions": 20,
        "delivery_by": "2026-11-16",
        "customer_id": "hotel-aurora",
    },
    "/v1/proposals": {
        "customer_id": "hotel-aurora",
        "product_id": "464306",
        "qty_sales_units": 10,
        "delivery_date": "2026-11-16",
    },
    "/v1/auth/approver-session": {"approver": "anna@hotel-aurora.example"},
    "/v1/voice/prompt": {"order_id": "O-x", "audio": False},
    "/v1/voice/intent": {"transcript": "yes, approve it", "order_id": "O-x"},
}
QUERY_SEEDS: dict[str, dict[str, str]] = {"/v1/products/search": {"q": "milk"}}
STATUS_OK_ERRORS = {400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429, 501, 503}


def _synth_products() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]

    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        cat: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": cat,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": name_en}],
            },
        }

    return [
        _item("463038", "KI", "Valio Eila semi skimmed milk drink 10 l novobox lactose free", "Valio", "10400", [("KI", 2), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]),
        _item("464306", "ST", "Valio Eila semi skimmed milk drink 1 l lactose free", "Valio", "10400", [("ST", 1)], base_1l),
        _item("469363", "ST", "Arla Semi skimmed milkdrink 1 L UHT lactose free", "Arla", "10400", [("ST", 1)], base_1l),
        _item("900001", "ST", "Valio lactose free butter 1 kg", "Valio", "10500", [("ST", 1)], base_1kg),
        _item("900002", "ST", "Myllyn Paras oat flakes 1 kg", "Myllyn Paras", "21370", [("ST", 1)], base_1kg),
    ]


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("fuzz-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, synth_catalog_dir):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    if synth_catalog_dir is not None:
        pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
        cdb = synth_catalog_dir / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    human_auth.reset_for_tests()
    bc.set_client(FixtureClient())
    yield
    human_auth.reset_for_tests()
    bc.set_client(None)
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def app(monkeypatch):
    import aimo.catalog as cat
    from aimo.buyer import runs
    from aimo.gateway.app import create_app

    started: list[Any] = []

    class _Stub:
        run_id = "run-fuzz"

    monkeypatch.setattr(runs.registry, "start", lambda req, **kw: started.append(req) or _Stub())
    monkeypatch.setattr(
        service,
        "_catalog",
        lambda: SimpleNamespace(
            plan_need=service._plan_deterministic,
            search=lambda q, **kw: textsearch.search(q, semantic=False, **kw),
            get_product=cat.get_product,
        ),
    )
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


def _body_fields(spec: dict, path: str, op: dict) -> list[str]:
    try:
        sch = op["requestBody"]["content"]["application/json"]["schema"]
        props = list(_resolve(spec, sch).get("properties", {}))
    except KeyError:
        props = []
    return props or list(SEEDS.get(path, {}))


def _fill(path: str, value: str) -> str:
    return re.sub(r"\{[^}]+\}", value, path)


def _cases(app) -> Iterator[tuple[str, str, dict]]:
    spec = app.openapi()
    for method, path, op in _routes(app):
        templ = "{" in path
        seed_path = _fill(path, "unknown-id")
        query = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
        if templ:
            for v in BAD_PATH:
                yield method, _fill(path, v), {}
        qseed = QUERY_SEEDS.get(path, {})
        for q in query:
            for v in BAD_QUERY:
                yield method, seed_path, {"params": {**qseed, q: v}}
        if method != "POST":
            continue
        seed = SEEDS.get(path, {})
        for raw in (
            b"",
            b"{",
            b"null",
            b"[]",
            b'"str"',
            b"42",
            b"\xff\xfe",
            json.dumps({"x": HUGE}).encode(),
            b'{"a":' * 200 + b"1" + b"}" * 200,
        ):
            yield method, seed_path, {"content": raw, "headers": {"content-type": "application/json"}}
        for f in _body_fields(spec, path, op):
            yield method, seed_path, {"json": {k: v for k, v in seed.items() if k != f}}
            for v in BAD_SCALARS:
                yield method, seed_path, {"json": {**seed, f: v}}
        if path == "/v1/offers":
            for v in BAD_SCALARS:
                yield method, seed_path, {"json": {**seed, "product_ids": [v]}}
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


@pytest.mark.parametrize(
    "override,expected_codes",
    [
        ({"quantity": 0}, {"validation_error", "invalid_need"}),
        ({"quantity": -5}, {"validation_error", "invalid_need"}),
        ({"quantity": 1_000_001}, {"absurd_quantity", "validation_error", "invalid_need"}),
        ({"quantity": 1e308}, {"absurd_quantity", "validation_error", "invalid_need"}),
        ({"product_query": ""}, {"empty_need", "validation_error", "invalid_need"}),
        ({"product_query": "   "}, {"empty_need", "validation_error", "invalid_need"}),
        ({"product_query": "x" * 501}, {"query_too_long", "validation_error", "invalid_need"}),
        ({"raw_text": "x" * 4001}, {"invalid_need", "validation_error"}),
    ],
)
def test_plan_bad_inputs_return_422(app, override, expected_codes):
    api = TestClient(app, headers=H, raise_server_exceptions=False)
    r = api.post("/v1/plan", json={**SEEDS["/v1/plan"], **override})
    assert r.status_code == 422, (override, r.status_code, r.text)
    body = r.json()
    assert body.get("error") in expected_codes and body.get("message"), body


def test_search_and_schema_bounds_return_422_and_structured_errors(app):
    api = TestClient(app, headers=H, raise_server_exceptions=False)

    for bad_q in ("", "   ", "x" * 501):
        r = api.get("/v1/products/search", params={"q": bad_q})
        assert r.status_code == 422, (bad_q[:20], r.status_code, r.text)
        assert r.json().get("error") and r.json().get("message")

    for bad_pid in ("", "   ", "464 306", "x" * 65):
        r_off = api.post("/v1/offers", json={"product_ids": [bad_pid], "requested_date": "2026-11-16"})
        assert r_off.status_code == 422, (bad_pid[:20], r_off.status_code, r_off.text)
        r_q = api.post(
            "/v1/quotes",
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": bad_pid, "qty_sales_units": 1}]},
        )
        assert r_q.status_code == 422, (bad_pid[:20], r_q.status_code, r_q.text)

    for bad_qty in (0, -1, 1_000_001, 1e308):
        r_q = api.post(
            "/v1/quotes",
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}]},
        )
        assert r_q.status_code == 422, (bad_qty, r_q.status_code, r_q.text)

    for bad_need in (
        {**SEEDS["/v1/plan"], "quantity": 1e308},
        {**SEEDS["/v1/plan"], "product_query": ""},
        {**SEEDS["/v1/plan"], "product_query": "   "},
        {**SEEDS["/v1/plan"], "product_query": "x" * 501},
    ):
        r_q_need = api.post("/v1/quotes", json={**SEEDS["/v1/quotes"], "need": bad_need})
        assert r_q_need.status_code == 422, (bad_need, r_q_need.status_code, r_q_need.text)

    r_ord = api.post("/v1/orders", json={"quote_id": "   ", "quote_hash": "0" * 64})
    assert r_ord.status_code == 422 and r_ord.json().get("error") == "validation_error"

    r_404 = api.get("/v1/no-such-route")
    assert r_404.status_code == 404 and r_404.json().get("error") == "not_found" and r_404.json().get("message")

    r_405 = api.delete("/v1/offers")
    assert r_405.status_code == 405 and r_405.json().get("error") == "method_not_allowed" and r_405.json().get("message")
