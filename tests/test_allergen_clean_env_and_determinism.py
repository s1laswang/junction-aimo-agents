from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import tokenize
from typing import Any, Iterator

from fastapi.testclient import TestClient
from mcp import Client
import numpy as np
import pytest

from aimo import config, gemini
import aimo.catalog as cat
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.catalog import allergens as allergens_mod
from aimo.catalog import embeddings, gemini_allergens, get_product, netcheck, plan as plan_mod, plan_need, textsearch
from aimo.catalog.substitution import rank_substitutes
from aimo.gateway import auth, human_auth, resilience, service, verify as verify_mod
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import EU14, AllergenStatus, Need, Offer, Product, Quote, QuoteLine
from aimo.trust import policy as trust_policy
from aimo.trust import signing
from aimo.trust.audit import audit as audit_log

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REAL_POLICY_DIR = Path(trust_policy.__file__).resolve().parent / "policies"
DELIVERY = date(2026, 11, 18)

_CONFIG_ATTRS = (
    "DATA_DIR",
    "PRODUCT_JSON",
    "RELIABILITY_CSV",
    "CO2_CSV",
    "CATALOG_DB",
    "STATE_DB",
    "KEY_DIR",
    "OFFLINE",
    "AIMO_API_KEY",
    "GEMINI_API_KEY",
)

_CSTORE_ATTRS = (
    "PRODUCT_JSON",
    "RELIABILITY_CSV",
    "CO2_CSV",
    "CATALOG_DB",
)


def _close_cached_catalog_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


def _clear_policy_caches() -> None:
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    allergens_mod._last_policy_dir = None
    plan_mod._last_policy_dir = None
    verify_mod._last_trust_policy_dir = None


def _reset_breakers() -> None:
    for br in resilience._breakers.values():
        br.reset()


def _point_optional_caches(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_allergens, "CACHE", data_dir / "cache" / "gemini_allergens.json")
    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", data_dir / "cache" / "gemini_recipe.json")
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setattr(embeddings, "EMB_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(embeddings, "IDS_PATH", data_dir / "embeddings_ids.json")
    monkeypatch.setattr(embeddings, "CHUNK_DIR", data_dir / "cache" / "emb_chunks")
    monkeypatch.setattr(embeddings, "QCACHE", data_dir / "cache" / "query_embeddings.json")
    embeddings._matrix.cache_clear()
    embeddings.embed_query.cache_clear()


@pytest.fixture(scope="module")
def clean_replay_bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    product_gz = cat.FIXTURE_CATALOG_DIR / "valio_aimo_junction26_product_data.json.gz"
    if not (REPLAY_DIR / "meta.json").is_file() or not product_gz.is_file():
        pytest.skip("recorded replay snapshot or fixture catalog is not present")

    orig_config = {k: getattr(config, k) for k in _CONFIG_ATTRS}
    orig_cstore = {k: getattr(catalog_store, k) for k in _CSTORE_ATTRS}
    orig_cat_source = cat._SOURCE
    orig_store_db = store.DB_PATH
    orig_bc_client = bc._client
    orig_policy_dir = trust_policy.POLICY_DIR
    orig_allergen_pdir = allergens_mod._last_policy_dir
    orig_plan_pdir = plan_mod._last_policy_dir
    orig_verify_pdir = verify_mod._last_trust_policy_dir
    orig_ga_cache = gemini_allergens.CACHE
    orig_ga_rcache = gemini_allergens.RECIPE_CACHE
    orig_ga_mem = gemini_allergens._mem
    orig_emb_path = embeddings.EMB_PATH
    orig_ids_path = embeddings.IDS_PATH
    orig_chunk_dir = embeddings.CHUNK_DIR
    orig_qcache = embeddings.QCACHE
    orig_ledger_dir = gemini.LEDGER_DIR
    orig_netcheck_state = dict(netcheck._state)

    base_dir = tmp_path_factory.mktemp("allergen_clean_env_det")
    data_dir = base_dir / "data"
    key_dir = base_dir / "keys"
    state_db = base_dir / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    _close_cached_catalog_con()
    _clear_policy_caches()
    orig_get_rows = textsearch.get_rows

    try:
        mp = pytest.MonkeyPatch()
        mp.setenv("AIMO_REPLAY", str(REPLAY_DIR))
        mp.setenv("AIMO_OFFLINE", "1")
        mp.setenv("AIMO_API_KEY", "")
        mp.setenv("VALIO_API_KEY", "")
        mp.setenv("GEMINI_API_KEY", "")
        mp.setenv("GOOGLE_API_KEY", "")
        mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)
        mp.setattr(config, "DATA_DIR", data_dir)
        mp.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
        mp.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
        mp.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
        mp.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
        mp.setattr(config, "OFFLINE", True)
        mp.setattr(config, "AIMO_API_KEY", "")
        mp.setattr(config, "GEMINI_API_KEY", "")
        mp.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
        try:
            src = cat._use_fixture_catalog()
            assert src == "fixture-subset"
            pjson = config.PRODUCT_JSON
            rel_csv = config.RELIABILITY_CSV
            co2_csv = config.CO2_CSV
            cat_db = config.CATALOG_DB
            mp.setattr(catalog_store, "PRODUCT_JSON", pjson)
            mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
            mp.setattr(catalog_store, "CO2_CSV", co2_csv)
            mp.setattr(catalog_store, "CATALOG_DB", cat_db)
            catalog_store.load_catalog(cat_db, force=True)
            con = textsearch._con()
            all_ids = [r["id"] for r in con.execute("select id from products").fetchall()]
            rows_by_id = orig_get_rows(all_ids)
        finally:
            _close_cached_catalog_con()
            mp.undo()

        replay_client = replay.ReplayClient(REPLAY_DIR)
        app = create_app(with_mcp=False)
        mcp_srv = build_mcp()
        with TestClient(app) as http_client:
            yield {
                "data_dir": data_dir,
                "key_dir": key_dir,
                "state_db": state_db,
                "product_json": pjson,
                "rel_csv": rel_csv,
                "co2_csv": co2_csv,
                "catalog_db": cat_db,
                "rows_by_id": rows_by_id,
                "orig_get_rows": orig_get_rows,
                "replay_client": replay_client,
                "http_client": http_client,
                "mcp_srv": mcp_srv,
            }
    finally:
        _close_cached_catalog_con()
        for k, v in orig_config.items():
            setattr(config, k, v)
        for k, v in orig_cstore.items():
            setattr(catalog_store, k, v)
        cat._SOURCE = orig_cat_source
        store.DB_PATH = orig_store_db
        with store._lock:
            cached_state_con = store._conns.pop(str(state_db), None)
        if cached_state_con is not None:
            try:
                cached_state_con.close()
            except Exception:
                pass
        audit_log._ready.discard(str(state_db))
        signing._keys.pop(str(key_dir / "ed25519-signing.pem"), None)
        bc.set_client(orig_bc_client)
        trust_policy.POLICY_DIR = orig_policy_dir
        _clear_policy_caches()
        allergens_mod._last_policy_dir = orig_allergen_pdir
        plan_mod._last_policy_dir = orig_plan_pdir
        verify_mod._last_trust_policy_dir = orig_verify_pdir
        gemini_allergens.CACHE = orig_ga_cache
        gemini_allergens.RECIPE_CACHE = orig_ga_rcache
        gemini_allergens._mem = orig_ga_mem
        embeddings.EMB_PATH = orig_emb_path
        embeddings.IDS_PATH = orig_ids_path
        embeddings.CHUNK_DIR = orig_chunk_dir
        embeddings.QCACHE = orig_qcache
        embeddings._matrix.cache_clear()
        embeddings.embed_query.cache_clear()
        gemini.LEDGER_DIR = orig_ledger_dir
        netcheck._state.clear()
        netcheck._state.update(orig_netcheck_state)
        human_auth.reset_for_tests()
        _reset_breakers()
        if hasattr(gemini.client, "cache_clear"):
            gemini.client.cache_clear()


@pytest.fixture(autouse=True)
def clean_keyless_env(
    clean_replay_bundle: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    _reset_breakers()
    netcheck._state.update(t=0.0, ok=False)

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_DATA_DIR", str(clean_replay_bundle["data_dir"]))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.delenv("AIMO_NOW_FROZEN", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_SEMANTIC", raising=False)
    monkeypatch.delenv("AIMO_RECIPE_CHECK", raising=False)

    data_dir = clean_replay_bundle["data_dir"]
    state_db = clean_replay_bundle["state_db"]

    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", clean_replay_bundle["product_json"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", clean_replay_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", clean_replay_bundle["co2_csv"])
    monkeypatch.setattr(config, "CATALOG_DB", clean_replay_bundle["catalog_db"])
    monkeypatch.setattr(config, "KEY_DIR", clean_replay_bundle["key_dir"])
    monkeypatch.setattr(config, "STATE_DB", state_db)

    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", clean_replay_bundle["product_json"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", clean_replay_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", clean_replay_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", clean_replay_bundle["catalog_db"])
    monkeypatch.setattr(cat, "_SOURCE", "fixture-subset")
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)
    monkeypatch.setattr(gemini, "LEDGER_DIR", data_dir / "gemini" / "ledger")

    _point_optional_caches(data_dir, monkeypatch)

    rows_by_id = clean_replay_bundle["rows_by_id"]

    def _fast_get_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
        return {str(i): dict(rows_by_id[str(i)]) for i in ids if str(i) in rows_by_id}

    monkeypatch.setattr(textsearch, "get_rows", _fast_get_rows)

    def _forbid_gemini(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("live Gemini call is forbidden in clean keyless mode")

    def _forbid_upstream(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("live Valio upstream call is forbidden in clean keyless mode")

    def _forbid_tcp(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("live TCP socket probe is forbidden in clean keyless mode")

    monkeypatch.setattr(gemini, "generate", _forbid_gemini)
    monkeypatch.setattr(gemini, "embed", _forbid_gemini)
    monkeypatch.setattr(gemini, "client", _forbid_gemini)
    monkeypatch.setattr(bc.ValioClient, "_post", _forbid_upstream)
    monkeypatch.setattr(bc.ValioClient, "_post_retrying", _forbid_upstream)
    monkeypatch.setattr(bc.ValioClient, "ping", _forbid_upstream)
    monkeypatch.setattr(socket, "create_connection", _forbid_tcp)

    if textsearch._con.cache_info().currsize == 0:
        textsearch._con()

    human_auth.reset_for_tests()
    prev_client = bc._client
    rc = clean_replay_bundle["replay_client"]
    rc.stats["upstream_calls"] = 0
    rc.stats["replay_calls"] = 0
    bc.set_client(rc)
    try:
        yield
    finally:
        bc.set_client(prev_client)
        human_auth.reset_for_tests()
        _reset_breakers()


@pytest.fixture
def client(clean_replay_bundle: dict[str, Any]) -> TestClient:
    return clean_replay_bundle["http_client"]


def _allergen_val(prod: Product, group: str) -> str:
    raw = prod.allergens.get(group, "unknown")
    return raw.value if hasattr(raw, "value") else str(raw)


def _assert_structured_free(product_id: str, groups: tuple[str, ...]) -> Product:
    prod = get_product(product_id)
    assert prod is not None, f"missing catalog product {product_id}"
    for group in groups:
        status = _allergen_val(prod, group)
        source = prod.allergen_source.get(group, "")
        assert status == "free", (product_id, prod.name, group, status, source)
        assert source.startswith("structured"), (product_id, prod.name, group, status, source)
        assert not source.startswith("inferred"), (product_id, prod.name, group, status, source)
    return prod


def _assert_plan_hardblock(plan_obj: Any, groups: tuple[str, ...]) -> None:
    need_excl = (
        plan_obj.need.excluded_allergens
        if hasattr(plan_obj, "need")
        else plan_obj["need"]["excluded_allergens"]
    )
    assert set(groups) <= set(need_excl)

    lines = plan_obj.lines if hasattr(plan_obj, "lines") else plan_obj["lines"]
    candidates = plan_obj.candidates if hasattr(plan_obj, "candidates") else plan_obj["candidates"]

    for ln in lines:
        pid = ln.product_id if hasattr(ln, "product_id") else ln["product_id"]
        flags = ln.flags if hasattr(ln, "flags") else ln.get("flags", [])
        _assert_structured_free(pid, groups)
        assert "allergen_unknown" not in flags
        for group in groups:
            assert f"contains:{group}" not in flags
            assert f"may_contain:{group}" not in flags

    for cand in candidates:
        tier = cand.tier if hasattr(cand, "tier") else cand["tier"]
        if tier == "rejected":
            continue
        pid = cand.product.id if hasattr(cand, "product") else cand["product"]["id"]
        flags = cand.flags if hasattr(cand, "flags") else cand.get("flags", [])
        _assert_structured_free(pid, groups)
        assert "allergen_unknown" not in flags
        for group in groups:
            assert f"contains:{group}" not in flags
            assert f"may_contain:{group}" not in flags


def _plan_signature(plan_obj: Any) -> dict[str, Any]:
    data = plan_obj.model_dump(mode="json") if hasattr(plan_obj, "model_dump") else dict(plan_obj)
    return {
        "line_pids": [ln["product_id"] for ln in data["lines"]],
        "line_tiers": [ln["tier"] for ln in data["lines"]],
        "line_qtys": [float(ln["qty_sales_units"]) for ln in data["lines"]],
        "line_covered": [float(ln["covered_amount"]) for ln in data["lines"]],
        "line_unit_prices": [float(ln["unit_price"]) for ln in data["lines"]],
        "line_totals": [float(ln["line_total"]) for ln in data["lines"]],
        "line_flags": [list(ln["flags"]) for ln in data["lines"]],
        "covered_amount": float(data["covered_amount"]),
        "total_net": float(data["total_net"]),
        "shortfall": float(data["shortfall"]),
        "candidate_order": [
            (
                c["product"]["id"],
                c["tier"],
                float(c["rank_score"]),
                list(c["flags"]),
            )
            for c in data["candidates"]
        ],
    }


def _strip_volatile_offer_ts(plan_dump: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(plan_dump))
    for cand in out.get("candidates") or []:
        offer = cand.get("offer")
        if isinstance(offer, dict):
            offer.pop("fetched_at", None)
    return out


def _mcp_plan_need(
    mcp_srv: Any,
    monkeypatch: pytest.MonkeyPatch,
    customer_id: str,
    need_payload: dict[str, Any],
) -> dict[str, Any]:
    monkeypatch.setenv("AIMO_CUSTOMER_ID", customer_id)
    monkeypatch.setenv("AIMO_AGENT_ID", "buyer-1")

    async def _run() -> dict[str, Any]:
        async with Client(mcp_srv) as mcp_client:
            res = await mcp_client.call_tool("plan_need", {"need": need_payload})
            assert not bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
            sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
            if isinstance(sc, dict):
                return sc
            raw = " ".join(getattr(c, "text", "") for c in res.content)
            return json.loads(raw)

    return asyncio.run(_run())


def test_hygiene_zero_comments_and_zero_em_or_en_dashes() -> None:
    raw_text = Path(__file__).read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(raw_text).readline))
    comment_tokens = [tok for tok in tokens if tok.type == tokenize.COMMENT]
    assert comment_tokens == []
    assert chr(0x2014) not in raw_text
    assert chr(0x2013) not in raw_text


@pytest.mark.parametrize("key_mode", ["unset", "empty"])
def test_keyless_env_modes_unset_and_empty_make_zero_live_calls(
    key_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AIMO_API_KEY", "VALIO_API_KEY"):
        if key_mode == "unset":
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, "")

    bc.set_client(None)
    assert os.environ.get("AIMO_OFFLINE") == "1"
    assert Path(os.environ["AIMO_REPLAY"]).resolve() == REPLAY_DIR.resolve()
    assert netcheck.semantic_enabled() is False
    assert netcheck.gemini_online() is False
    assert replay.mode() == "forced"
    assert replay.replay_dir() == REPLAY_DIR

    backend = bc.get_client()
    assert isinstance(backend, replay.ReplayClient)
    assert backend.stats["upstream_calls"] == 0

    school_need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=20.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    school_plan = plan_need(school_need)
    _assert_plan_hardblock(school_plan, ("milk", "nuts"))
    assert [ln.product_id for ln in school_plan.lines] == ["470889"]

    hosp_need = Need(
        customer_id="hospital-meilahti",
        product_query="bread",
        excluded_allergens=[],
        quantity=6.0,
        unit="KG",
        delivery_by=DELIVERY,
    )
    hosp_plan = service.plan_need(
        hosp_need,
        identity=service.Identity(agent_id="buyer-1", customer_id="hospital-meilahti"),
    )
    _assert_plan_hardblock(hosp_plan, ("gluten",))
    assert hosp_plan.lines[0].product_id == "473328"

    assert backend.stats["upstream_calls"] == 0
    assert backend.stats["replay_calls"] >= 2


@pytest.mark.parametrize(
    ("customer_id", "query", "unit", "groups"),
    [
        ("school-tampere", "milk", "L", ("milk", "nuts")),
        ("school-tampere", "almonds", "KG", ("milk", "nuts")),
        ("hospital-meilahti", "bread", "KG", ("gluten",)),
        ("hospital-meilahti", "wheat flour", "KG", ("gluten",)),
    ],
)
def test_catalog_and_service_plan_need_enforce_policy_hardblock_when_excluded_empty(
    customer_id: str,
    query: str,
    unit: str,
    groups: tuple[str, ...],
) -> None:
    need = Need(
        customer_id=customer_id,
        product_query=query,
        excluded_allergens=[],
        quantity=15.0,
        unit=unit,
        delivery_by=DELIVERY,
    )
    cat_plan = plan_need(need)
    _assert_plan_hardblock(cat_plan, groups)

    svc_plan = service.plan_need(
        need,
        identity=service.Identity(agent_id="buyer-1", customer_id=customer_id),
    )
    _assert_plan_hardblock(svc_plan, groups)

    det_plan = service._plan_deterministic(need)
    _assert_plan_hardblock(det_plan, groups)

    assert bc.get_client().stats["upstream_calls"] == 0


@pytest.mark.parametrize(
    ("customer_id", "query", "unit", "groups"),
    [
        ("school-tampere", "lactose-free milk", "L", ("milk", "nuts")),
        ("school-tampere", "hazelnuts", "KG", ("milk", "nuts")),
        ("hospital-meilahti", "bread", "KG", ("gluten",)),
        ("hospital-meilahti", "ruisleipä", "KG", ("gluten",)),
    ],
)
def test_http_post_v1_plan_keyless_enforces_school_and_hospital_hardblock(
    client: TestClient,
    customer_id: str,
    query: str,
    unit: str,
    groups: tuple[str, ...],
) -> None:
    resp = client.post(
        "/v1/plan",
        headers={"X-Customer-Id": customer_id, "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": customer_id,
            "product_query": query,
            "excluded_allergens": [],
            "quantity": 12.0,
            "unit": unit,
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_plan_hardblock(body, groups)
    assert bc.get_client().stats["upstream_calls"] == 0


@pytest.mark.parametrize(
    ("customer_id", "query", "unit", "groups"),
    [
        ("school-tampere", "milk", "L", ("milk", "nuts")),
        ("school-tampere", "butter", "KG", ("milk", "nuts")),
        ("hospital-meilahti", "bread", "KG", ("gluten",)),
        ("hospital-meilahti", "rye bread", "KG", ("gluten",)),
    ],
)
def test_mcp_plan_need_keyless_enforces_school_and_hospital_hardblock(
    clean_replay_bundle: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    customer_id: str,
    query: str,
    unit: str,
    groups: tuple[str, ...],
) -> None:
    payload = {
        "customer_id": customer_id,
        "product_query": query,
        "excluded_allergens": [],
        "quantity": 10.0,
        "unit": unit,
        "delivery_by": DELIVERY.isoformat(),
    }
    mcp_plan = _mcp_plan_need(clean_replay_bundle["mcp_srv"], monkeypatch, customer_id, payload)
    _assert_plan_hardblock(mcp_plan, groups)
    assert bc.get_client().stats["upstream_calls"] == 0


@pytest.mark.parametrize(
    ("customer_id", "recipe_id", "groups"),
    [
        ("school-tampere", "oat-porridge", ("milk", "nuts")),
        ("school-tampere", "lasagne", ("milk", "nuts")),
        ("hospital-meilahti", "chicken pasta", ("gluten",)),
    ],
)
def test_http_post_v1_recipes_plan_keyless_enforces_school_and_hospital_hardblock(
    client: TestClient,
    customer_id: str,
    recipe_id: str,
    groups: tuple[str, ...],
) -> None:
    resp = client.post(
        "/v1/recipes/plan",
        headers={"X-Customer-Id": customer_id, "X-Agent-Id": "buyer-1"},
        json={
            "recipe_id": recipe_id,
            "portions": 25,
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
            "include_candidates": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer_id"] == customer_id
    assert set(groups) <= set(body["excluded_allergens"])
    assert body["provenance"]["offers"]["served_from"] == "replay"
    assert bc.get_client().stats["upstream_calls"] == 0

    for ing in body["ingredients"]:
        assert set(groups) <= set(ing["need"]["excluded_allergens"])
        _assert_plan_hardblock(ing["plan"], groups)

    for ql in body["quote_request"]["lines"]:
        _assert_structured_free(ql["product_id"], groups)


@pytest.mark.parametrize(
    ("customer_id", "query", "qty", "unit", "groups"),
    [
        ("school-tampere", "milk", 25.0, "L", ("milk", "nuts")),
        ("school-tampere", "oat drink", 40.0, "L", ("milk", "nuts")),
        ("school-tampere", "butter", 10.0, "KG", ("milk", "nuts")),
        ("hospital-meilahti", "bread", 12.0, "KG", ("gluten",)),
    ],
)
def test_repeated_plan_need_5x_exact_determinism(
    customer_id: str,
    query: str,
    qty: float,
    unit: str,
    groups: tuple[str, ...],
) -> None:
    runs = [
        plan_need(
            Need(
                customer_id=customer_id,
                product_query=query,
                excluded_allergens=[],
                quantity=qty,
                unit=unit,
                delivery_by=DELIVERY,
            )
        )
        for _ in range(5)
    ]
    baseline_sig = _plan_signature(runs[0])
    baseline_dump = _strip_volatile_offer_ts(runs[0].model_dump(mode="json"))
    _assert_plan_hardblock(runs[0], groups)

    for run in runs[1:]:
        _assert_plan_hardblock(run, groups)
        assert _plan_signature(run) == baseline_sig
        assert _strip_volatile_offer_ts(run.model_dump(mode="json")) == baseline_dump


@pytest.mark.parametrize(
    ("customer_id", "query", "qty", "unit", "include_empty"),
    [
        ("school-tampere", "milk", 20.0, "L", True),
        ("school-tampere", "oat drink", 30.0, "L", True),
        ("hotel-aurora", "oat drink", 20.0, "L", False),
    ],
)
def test_milk_and_nuts_alias_and_permutation_invariance(
    customer_id: str,
    query: str,
    qty: float,
    unit: str,
    include_empty: bool,
) -> None:
    variants: list[list[str]] = [
        ["milk", "nuts"],
        ["nuts", "milk"],
        ["dairy", "tree nuts", "AM", "AN", "maito", "pähkinä"],
        ["pähkinä", "AN", "nötter", "almond", "mjölk", "cow's milk", "am", "ML", "dairy"],
    ]
    if include_empty:
        variants.append([])

    plans = [
        plan_need(
            Need(
                customer_id=customer_id,
                product_query=query,
                excluded_allergens=excl,
                quantity=qty,
                unit=unit,
                delivery_by=DELIVERY,
            )
        )
        for excl in variants
    ]

    ref_sig = _plan_signature(plans[0])
    _assert_plan_hardblock(plans[0], ("milk", "nuts"))

    for p in plans[1:]:
        _assert_plan_hardblock(p, ("milk", "nuts"))
        assert _plan_signature(p) == ref_sig


def test_gluten_and_shellfish_alias_permutation_invariance() -> None:
    gluten_variants = [
        ["gluten"],
        ["wheat", "AW", "vehnä", "ruis", "UW", "gluteeni", "barley"],
        ["ohra", "NR", "spelt", "cereals containing gluten", "vete", "aw"],
    ]
    gluten_plans = [
        plan_need(
            Need(
                customer_id="hospital-meilahti",
                product_query="bread",
                excluded_allergens=v,
                quantity=8.0,
                unit="KG",
                delivery_by=DELIVERY,
            )
        )
        for v in gluten_variants
    ]
    ref_gluten = _plan_signature(gluten_plans[0])
    for gp in gluten_plans:
        _assert_plan_hardblock(gp, ("gluten",))
        assert _plan_signature(gp) == ref_gluten

    compound_variants = [
        ["crustaceans", "molluscs", "peanuts"],
        ["peanuts", "molluscs", "crustaceans"],
        ["maapähkinä", "AP", "shellfish", "UM", "AC", "jordnöt"],
    ]
    compound_plans = [
        plan_need(
            Need(
                customer_id="hotel-aurora",
                product_query="sugar",
                excluded_allergens=v,
                quantity=10.0,
                unit="KG",
                delivery_by=DELIVERY,
            )
        )
        for v in compound_variants
    ]
    ref_compound = _plan_signature(compound_plans[0])
    assert compound_plans[0].lines
    for cp in compound_plans:
        _assert_plan_hardblock(cp, ("crustaceans", "molluscs", "peanuts"))
        assert _plan_signature(cp) == ref_compound


def test_cross_surface_determinism_catalog_service_http_and_mcp(
    clean_replay_bundle: dict[str, Any],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    need = Need(
        customer_id="school-tampere",
        product_query="milk",
        excluded_allergens=[],
        quantity=18.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    cat_plan = plan_need(need)
    svc_plan = service.plan_need(
        need,
        identity=service.Identity(agent_id="buyer-1", customer_id="school-tampere"),
    )
    http_resp = client.post(
        "/v1/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "school-tampere",
            "product_query": "milk",
            "excluded_allergens": [],
            "quantity": 18.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert http_resp.status_code == 200, http_resp.text
    http_plan = http_resp.json()
    mcp_plan = _mcp_plan_need(
        clean_replay_bundle["mcp_srv"],
        monkeypatch,
        "school-tampere",
        {
            "customer_id": "school-tampere",
            "product_query": "milk",
            "excluded_allergens": [],
            "quantity": 18.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )

    sig_cat = _plan_signature(cat_plan)
    assert _plan_signature(svc_plan) == sig_cat
    assert _plan_signature(http_plan) == sig_cat
    assert _plan_signature(mcp_plan) == sig_cat
    _assert_plan_hardblock(cat_plan, ("milk", "nuts"))


def _write_policy_yaml(
    target_dir: Path,
    policy_id: str,
    *,
    excluded: list[str],
    reason: str = "custom allergy rule",
    on_contains: str = "reject",
    on_may_contain: str = "reject",
    on_unknown: str = "reject",
    preferred_ids: list[str] | None = None,
) -> Path:
    prefs_block = "preferred_products: []"
    if preferred_ids:
        items = ", ".join(f'{{product_id: "{pid}", label: "Preferred {pid}"}}' for pid in preferred_ids)
        prefs_block = f"preferred_products: [{items}]"
    excl_str = ", ".join(excluded)
    text = "\n".join(
        [
            f"policy_id: {policy_id}",
            'version: "1.0.0"',
            "customer:",
            f"  id: {policy_id}",
            f"  name: Custom {policy_id}",
            "approver_roles: [kitchen_manager]",
            "agents:",
            f"  - agent_id: {policy_id}/buyer-1",
            "    scopes: [catalog:read, quote:create, order:submit]",
            prefs_block,
            "approved_alternatives: []",
            "rules:",
            "  - id: mandate_scope",
            "    type: scope",
            "    required_scopes: [order:submit]",
            "  - id: spend",
            "    type: spend_threshold",
            "    auto_max: 500",
            "    approval_max: 5000",
            "    approver_role: kitchen_manager",
            "  - id: substitution",
            "    type: substitution",
            "    require_claims: true",
            "    tiers: {preferred: allow, best: needs_approval, better: needs_approval, good: needs_approval, rejected: reject}",
            "    outside_preference: needs_approval",
            "    unverified: needs_approval",
            "    new_product: allow",
            "    approver_role: kitchen_manager",
            "  - id: allergens",
            "    type: allergen_block",
            f"    excluded: [{excl_str}]",
            f"    reason: {reason}",
            f"    on_contains: {on_contains}",
            f"    on_may_contain: {on_may_contain}",
            f"    on_unknown: {on_unknown}",
            "    approver_role: kitchen_manager",
            "  - id: delivery",
            "    type: delivery",
        ]
    )
    out = target_dir / f"{policy_id}.yaml"
    out.write_text(text + "\n", encoding="utf-8")
    return out


def test_policy_dir_custom_customer_immediate_across_allergens_plan_verify_and_http(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert allergens_mod.policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert verify_mod.policy_excluded_allergens("school-tampere") == ["milk", "nuts"]

    custom_dir = tmp_path / "policies"
    custom_dir.mkdir()
    _write_policy_yaml(
        custom_dir,
        "daycare-espoo",
        excluded=["dairy", "AN", "wheat", "kananmuna"],
        reason="toddlers with milk, nut, gluten and egg allergies",
        preferred_ids=["460325"],
    )

    monkeypatch.setattr(trust_policy, "POLICY_DIR", custom_dir)

    blk = allergens_mod.policy_allergen_block("daycare-espoo")
    assert blk.excluded == ("milk", "nuts", "gluten", "eggs")
    assert blk.on_unknown == "reject"
    assert verify_mod.policy_excluded_allergens("daycare-espoo") == ["milk", "nuts", "gluten", "eggs"]
    assert verify_mod.usual_products("daycare-espoo") == ["460325"]
    assert auth.is_known_customer("daycare-espoo") is True

    oat_plan = plan_need(
        Need(
            customer_id="daycare-espoo",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(oat_plan, ("milk", "nuts", "gluten", "eggs"))
    assert "470889" not in [ln.product_id for ln in oat_plan.lines]

    sugar_plan = plan_need(
        Need(
            customer_id="daycare-espoo",
            product_query="sugar",
            excluded_allergens=[],
            quantity=5.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(sugar_plan, ("milk", "nuts", "gluten", "eggs"))
    assert sugar_plan.lines[0].product_id == "460325"
    assert sugar_plan.lines[0].tier == "preferred"

    products = {pid: get_product(pid) for pid in ("460325", "470889", "464306", "460573")}
    offers = bc.get_offers(["460325", "470889", "464306", "460573"], DELIVERY, "daycare-espoo")
    verified = verify_mod.verify_lines(
        "daycare-espoo",
        DELIVERY,
        {"460325": 2.0, "470889": 2.0, "464306": 2.0, "460573": 2.0},
        offers,
        None,
        {k: v for k, v in products.items() if v is not None},
        lambda _p: {"suspicious": False, "reasons": []},
    )
    assert verified["460325"]["tier"] == "preferred"
    assert verified["470889"]["tier"] == "rejected"
    assert "contains:gluten" in verified["470889"]["flags"]
    assert verified["464306"]["tier"] == "rejected"
    assert "contains:milk" in verified["464306"]["flags"]
    assert verified["460573"]["tier"] == "rejected"
    assert "allergen_unknown" in verified["460573"]["flags"]

    r_http = client.post(
        "/v1/plan",
        headers={"X-Customer-Id": "daycare-espoo", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "daycare-espoo",
            "product_query": "sugar",
            "excluded_allergens": [],
            "quantity": 4.0,
            "unit": "KG",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_http.status_code == 200, r_http.text
    assert r_http.json()["lines"][0]["product_id"] == "460325"


@pytest.mark.parametrize("first_caller", ["allergens", "plan", "verify"])
def test_policy_dir_same_id_same_mtime_no_stale_cache_leak(
    first_caller: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert allergens_mod.policy_excluded_allergens("hotel-aurora") == []
    assert verify_mod.policy_excluded_allergens("hotel-aurora") == []
    base_hotel_plan = plan_need(
        Need(
            customer_id="hotel-aurora",
            product_query="lactose-free milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    assert [ln.product_id for ln in base_hotel_plan.lines] == ["464306"]

    custom_dir = tmp_path / f"same_mtime_{first_caller}"
    custom_dir.mkdir()
    hotel_yaml = _write_policy_yaml(
        custom_dir,
        "hotel-aurora",
        excluded=["milk", "nuts"],
        reason="temporary milk and nut ban",
        preferred_ids=["470889"],
    )
    real_stat = (REAL_POLICY_DIR / "hotel-aurora.yaml").stat()
    os.utime(hotel_yaml, (real_stat.st_atime, real_stat.st_mtime))

    monkeypatch.setattr(trust_policy, "POLICY_DIR", custom_dir)

    prod_464306 = get_product("464306")
    assert prod_464306 is not None
    offers = bc.get_offers(["464306", "470889"], DELIVERY, "hotel-aurora")

    if first_caller == "allergens":
        assert allergens_mod.policy_excluded_allergens("hotel-aurora") == ["milk", "nuts"]
    elif first_caller == "plan":
        switched_plan = plan_need(
            Need(
                customer_id="hotel-aurora",
                product_query="milk",
                excluded_allergens=[],
                quantity=10.0,
                unit="L",
                delivery_by=DELIVERY,
            )
        )
        _assert_plan_hardblock(switched_plan, ("milk", "nuts"))
        assert [ln.product_id for ln in switched_plan.lines] == ["470889"]
    else:
        v = verify_mod.verify_lines(
            "hotel-aurora",
            DELIVERY,
            {"464306": 2.0},
            offers,
            None,
            {"464306": prod_464306},
            lambda _p: {"suspicious": False, "reasons": []},
        )
        assert v["464306"]["tier"] == "rejected"
        assert "contains:milk" in v["464306"]["flags"]

    assert allergens_mod.policy_excluded_allergens("hotel-aurora") == ["milk", "nuts"]
    assert verify_mod.policy_excluded_allergens("hotel-aurora") == ["milk", "nuts"]
    switched_after = plan_need(
        Need(
            customer_id="hotel-aurora",
            product_query="lactose-free milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(switched_after, ("milk", "nuts"))
    assert "464306" not in [ln.product_id for ln in switched_after.lines]


def test_policy_dir_in_place_yaml_edit_reloads_on_mtime_bump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_dir = tmp_path / "live_edit"
    custom_dir.mkdir()
    pol_file = _write_policy_yaml(
        custom_dir,
        "clinic-turku",
        excluded=["milk"],
        preferred_ids=["470889"],
    )
    monkeypatch.setattr(trust_policy, "POLICY_DIR", custom_dir)

    assert allergens_mod.policy_excluded_allergens("clinic-turku") == ["milk"]
    p1 = plan_need(
        Need(
            customer_id="clinic-turku",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=5.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    assert [ln.product_id for ln in p1.lines] == ["470889"]

    old_mtime = pol_file.stat().st_mtime
    _write_policy_yaml(
        custom_dir,
        "clinic-turku",
        excluded=["milk", "gluten"],
        preferred_ids=["470889"],
    )
    os.utime(pol_file, (old_mtime + 5.0, old_mtime + 5.0))

    assert allergens_mod.policy_excluded_allergens("clinic-turku") == ["milk", "gluten"]
    assert verify_mod.policy_excluded_allergens("clinic-turku") == ["milk", "gluten"]
    p2 = plan_need(
        Need(
            customer_id="clinic-turku",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=5.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(p2, ("milk", "gluten"))
    assert "470889" not in [ln.product_id for ln in p2.lines]


def test_policy_dir_restore_cleanly_restores_school_hospital_and_hotel(
    tmp_path: Path,
) -> None:
    custom_dir = tmp_path / "override_all"
    custom_dir.mkdir()
    _write_policy_yaml(custom_dir, "school-tampere", excluded=["soy"])
    _write_policy_yaml(custom_dir, "hospital-meilahti", excluded=["eggs"])
    _write_policy_yaml(custom_dir, "hotel-aurora", excluded=["milk", "gluten"])
    _write_policy_yaml(custom_dir, "daycare-espoo", excluded=["peanuts"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(trust_policy, "POLICY_DIR", custom_dir)
        assert allergens_mod.policy_excluded_allergens("school-tampere") == ["soy"]
        assert allergens_mod.policy_excluded_allergens("hospital-meilahti") == ["eggs"]
        assert allergens_mod.policy_excluded_allergens("hotel-aurora") == ["milk", "gluten"]
        assert allergens_mod.policy_excluded_allergens("daycare-espoo") == ["peanuts"]
        assert verify_mod.policy_excluded_allergens("school-tampere") == ["soy"]
        _ = plan_need(
            Need(
                customer_id="hotel-aurora",
                product_query="sugar",
                excluded_allergens=[],
                quantity=2.0,
                unit="KG",
                delivery_by=DELIVERY,
            )
        )

    assert trust_policy.POLICY_DIR == REAL_POLICY_DIR
    assert allergens_mod.policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert allergens_mod.policy_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert allergens_mod.policy_excluded_allergens("hotel-aurora") == []
    assert allergens_mod.policy_allergen_block("daycare-espoo").policy_id is None
    assert auth.is_known_customer("daycare-espoo") is False

    assert verify_mod.policy_excluded_allergens("school-tampere") == ["milk", "nuts"]
    assert verify_mod.policy_excluded_allergens("hospital-meilahti") == ["gluten"]
    assert verify_mod.policy_excluded_allergens("hotel-aurora") == []
    assert verify_mod.usual_products("school-tampere") == ["470889"]
    assert verify_mod.usual_products("hospital-meilahti") == ["473328"]
    assert verify_mod.usual_products("hotel-aurora") == ["463038"]

    school_plan = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(school_plan, ("milk", "nuts"))
    assert [ln.product_id for ln in school_plan.lines] == ["470889"]

    hosp_plan = plan_need(
        Need(
            customer_id="hospital-meilahti",
            product_query="bread",
            excluded_allergens=[],
            quantity=5.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(hosp_plan, ("gluten",))
    assert hosp_plan.lines[0].product_id == "473328"

    hotel_plan = plan_need(
        Need(
            customer_id="hotel-aurora",
            product_query="lactose-free milk",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    assert [ln.product_id for ln in hotel_plan.lines] == ["464306"]


def test_missing_optional_caches_and_embeddings_degrades_to_structured_fts_hardblock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_data = tmp_path / "empty_aimo_data"
    isolated_data.mkdir()
    monkeypatch.setenv("AIMO_DATA_DIR", str(isolated_data))
    monkeypatch.setattr(config, "DATA_DIR", isolated_data)
    _point_optional_caches(isolated_data, monkeypatch)

    assert not gemini_allergens.CACHE.exists()
    assert not gemini_allergens.RECIPE_CACHE.exists()
    assert not embeddings.EMB_PATH.exists()
    assert not embeddings.IDS_PATH.exists()
    assert not embeddings.QCACHE.exists()
    assert embeddings.available() is False
    assert gemini_allergens.cached_inference("460573") is None
    assert textsearch.semantic_hits("oat drink") == []

    school_oat = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=12.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(school_oat, ("milk", "nuts"))
    assert [ln.product_id for ln in school_oat.lines] == ["470889"]

    school_potato = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="potato",
            excluded_allergens=[],
            quantity=10.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(school_potato, ("milk", "nuts"))
    assert "460573" not in [ln.product_id for ln in school_potato.lines]

    hosp_bread = plan_need(
        Need(
            customer_id="hospital-meilahti",
            product_query="bread",
            excluded_allergens=[],
            quantity=6.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(hosp_bread, ("gluten",))
    assert hosp_bread.lines[0].product_id == "473328"


@pytest.mark.parametrize(
    "corruption_mode",
    ["truncated_invalid_json", "wrong_json_types", "valid_emb_matrix_corrupt_qcache"],
)
def test_corrupt_optional_cache_files_degrade_cleanly_and_hardblock(
    corruption_mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt_data = tmp_path / f"corrupt_{corruption_mode}"
    (corrupt_data / "cache").mkdir(parents=True)
    monkeypatch.setenv("AIMO_DATA_DIR", str(corrupt_data))
    monkeypatch.setattr(config, "DATA_DIR", corrupt_data)
    _point_optional_caches(corrupt_data, monkeypatch)

    if corruption_mode == "truncated_invalid_json":
        gemini_allergens.CACHE.write_text("{truncated: [invalid", encoding="utf-8")
        gemini_allergens.RECIPE_CACHE.write_text("{broken_recipe:", encoding="utf-8")
        embeddings.EMB_PATH.write_bytes(b"\x00\xffnot-a-valid-npy-file")
        embeddings.IDS_PATH.write_text("{broken_ids", encoding="utf-8")
        embeddings.QCACHE.write_text("{broken_qcache", encoding="utf-8")
    elif corruption_mode == "wrong_json_types":
        gemini_allergens.CACHE.write_text("[1, 2, 3]", encoding="utf-8")
        gemini_allergens.RECIPE_CACHE.write_text("null", encoding="utf-8")
        embeddings.EMB_PATH.write_bytes(b"corrupt")
        embeddings.IDS_PATH.write_text('{"not": "a list"}', encoding="utf-8")
        embeddings.QCACHE.write_text("42", encoding="utf-8")
    else:
        mat = np.ones((2, embeddings.DIM), dtype=np.float16)
        np.save(embeddings.EMB_PATH, mat)
        embeddings.IDS_PATH.write_text(json.dumps(["470889", "464306"]), encoding="utf-8")
        embeddings.QCACHE.write_text("{corrupt-query-cache", encoding="utf-8")
        gemini_allergens.CACHE.write_text("{corrupt-allergen-cache", encoding="utf-8")
        gemini_allergens.RECIPE_CACHE.write_text("{corrupt-recipe-cache", encoding="utf-8")
        monkeypatch.setenv("AIMO_OFFLINE", "0")
        monkeypatch.setenv("AIMO_SEMANTIC", "1")

    embeddings._matrix.cache_clear()
    embeddings.embed_query.cache_clear()
    gemini_allergens._mem = None

    prod = get_product("460573")
    assert prod is not None
    assert _allergen_val(prod, "milk") == "unknown"

    school_plan = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="milk",
            excluded_allergens=[],
            quantity=15.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(school_plan, ("milk", "nuts"))
    assert [ln.product_id for ln in school_plan.lines] == ["470889"]

    hosp_plan = plan_need(
        Need(
            customer_id="hospital-meilahti",
            product_query="bread",
            excluded_allergens=[],
            quantity=8.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(hosp_plan, ("gluten",))
    assert hosp_plan.lines[0].product_id == "473328"


def test_inferred_gemini_not_found_or_free_in_cache_never_unlocks_unknown_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_data = tmp_path / "inferred_cache_data"
    (cache_data / "cache").mkdir(parents=True)
    monkeypatch.setenv("AIMO_DATA_DIR", str(cache_data))
    monkeypatch.setattr(config, "DATA_DIR", cache_data)
    _point_optional_caches(cache_data, monkeypatch)

    fake_cache = {
        "id:460573": {
            "allergens": {g: ("free" if g == "milk" else "not_found") for g in EU14},
            "evidence": {},
            "source": "inferred:gemini",
        },
        "id:469269": {
            "allergens": {g: "free" for g in EU14},
            "evidence": {},
            "source": "inferred:gemini",
        },
    }
    gemini_allergens.CACHE.write_text(json.dumps(fake_cache), encoding="utf-8")
    gemini_allergens._mem = None

    p_potato = get_product("460573")
    assert p_potato is not None
    assert _allergen_val(p_potato, "milk") == "unknown"
    assert _allergen_val(p_potato, "nuts") == "unknown"
    assert _allergen_val(p_potato, "gluten") == "unknown"
    assert p_potato.allergen_source["milk"] == "inferred:gemini"
    assert p_potato.allergen_source["nuts"] == "inferred:gemini"

    p_muesli = get_product("469269")
    assert p_muesli is not None
    assert _allergen_val(p_muesli, "milk") == "unknown"
    assert p_muesli.allergen_source["milk"] == "inferred:gemini"
    assert _allergen_val(p_muesli, "gluten") == "free"
    assert p_muesli.allergen_source["gluten"] == "structured:claim"

    potato_plan = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="potato",
            excluded_allergens=[],
            quantity=10.0,
            unit="KG",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(potato_plan, ("milk", "nuts"))
    assert "460573" not in [ln.product_id for ln in potato_plan.lines]
    cand_460573 = next((c for c in potato_plan.candidates if c.product.id == "460573"), None)
    assert cand_460573 is not None
    assert cand_460573.tier == "rejected"
    assert "allergen_unknown" in cand_460573.flags
    assert "allergen_inferred_hint" in cand_460573.flags
    assert any("inferred:gemini" in r and "unknown is never treated as safe" in r for r in cand_460573.reasons)


def test_inferred_gemini_free_source_blocked_across_allergens_plan_verify_and_policy() -> None:
    inferred_free_allergens = {g: "free" for g in EU14}
    inferred_sources = {
        g: ("inferred:gemini" if g in ("milk", "nuts", "gluten") else "structured")
        for g in EU14
    }

    assert allergens_mod.blocking(
        inferred_free_allergens,
        ["milk", "nuts"],
        allergen_source=inferred_sources,
    ) == [("milk", "unknown"), ("nuts", "unknown")]

    synth_row = {
        "id": "SYNTH-INF-1",
        "name": "Synthetic Oat Drink 1 L",
        "name_en": "Synthetic Oat Drink 1 L",
        "name_fi": "Synteettinen kaurajuoma 1 L",
        "brand": "Synth",
        "category": "10402",
        "sales_unit": "ST",
        "content_amount": 1.0,
        "content_unit": "L",
        "claims": ["VEGAN"],
        "labels": [],
        "allergens": dict(inferred_free_allergens),
        "allergen_source": dict(inferred_sources),
    }
    now_utc = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    synth_offer = Offer(
        product_id="SYNTH-INF-1",
        requested_date=DELIVERY,
        available=True,
        available_amount=100.0,
        sales_unit="ST",
        unit_price=1.20,
        comparison_price=1.20,
        comparison_unit="L",
        fetched_at=now_utc,
    )
    need = Need(
        customer_id="school-tampere",
        product_query="oat drink",
        excluded_allergens=[],
        quantity=10.0,
        unit="L",
        delivery_by=DELIVERY,
    )
    cands = rank_substitutes(
        need,
        {"SYNTH-INF-1": synth_offer},
        rows={"SYNTH-INF-1": synth_row},
        relevance={"SYNTH-INF-1": 10.0},
    )
    assert len(cands) == 1
    assert cands[0].tier == "rejected"
    assert "allergen_unknown" in cands[0].flags

    filled = plan_mod.fill(need, cands)
    assert filled.lines == []
    assert filled.shortfall == 10.0

    synth_prod = Product(
        id="SYNTH-INF-1",
        name="Synthetic Oat Drink 1 L",
        brand="Synth",
        category="10402",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["VEGAN"],
        allergens={g: AllergenStatus.free for g in EU14},
        allergen_source=dict(inferred_sources),
    )
    verified = verify_mod.verify_lines(
        "school-tampere",
        DELIVERY,
        {"SYNTH-INF-1": 5.0},
        {"SYNTH-INF-1": synth_offer},
        need,
        {"SYNTH-INF-1": synth_prod},
        lambda _p: {"suspicious": False, "reasons": []},
    )
    assert verified["SYNTH-INF-1"]["tier"] == "rejected"
    assert "allergen_unknown" in verified["SYNTH-INF-1"]["flags"]
    assert "allergen_inferred_hint" in verified["SYNTH-INF-1"]["flags"]

    now_utc = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    for cid, expected_outcome in (("school-tampere", "reject"), ("hospital-meilahti", "needs_approval")):
        q = Quote(
            quote_id=f"Q-{cid}",
            customer_id=cid,
            agent_id="buyer-1",
            delivery_date=DELIVERY,
            lines=[
                QuoteLine(
                    product_id="SYNTH-INF-1",
                    product_name=synth_prod.name,
                    qty_sales_units=2.0,
                    sales_unit="ST",
                    unit_price=1.20,
                    line_total=2.40,
                    tier="preferred",
                )
            ],
            total_net=2.40,
            total_gross=2.74,
            created_at=now_utc,
            expires_at=now_utc + timedelta(minutes=15),
        )
        decision = trust_policy.evaluate(
            q,
            trust_policy.get_mandate(cid, "buyer-1"),
            products={"SYNTH-INF-1": synth_prod},
            now=now_utc,
        )
        hit = next(h for h in decision.hits if h.rule_id == "allergens:SYNTH-INF-1")
        assert hit.outcome == expected_outcome
        assert "ai-inferred" in hit.message.lower()


def test_inferred_gemini_contains_conservatively_blocks_without_overwriting_structured_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_data = tmp_path / "inferred_contains_data"
    (cache_data / "cache").mkdir(parents=True)
    monkeypatch.setenv("AIMO_DATA_DIR", str(cache_data))
    monkeypatch.setattr(config, "DATA_DIR", cache_data)
    _point_optional_caches(cache_data, monkeypatch)

    fake_cache = {
        "id:460573": {
            "allergens": {**{g: "not_found" for g in EU14}, "milk": "contains"},
            "evidence": {"milk": "milk powder"},
            "source": "inferred:gemini",
        },
        "id:470889": {
            "allergens": {**{g: "not_found" for g in EU14}, "milk": "contains"},
            "evidence": {"milk": "bogus cache poisoning attempt"},
            "source": "inferred:gemini",
        },
    }
    gemini_allergens.CACHE.write_text(json.dumps(fake_cache), encoding="utf-8")
    gemini_allergens._mem = None

    p_potato = get_product("460573")
    assert p_potato is not None
    assert _allergen_val(p_potato, "milk") == "contains"
    assert p_potato.allergen_source["milk"] == "inferred:gemini"

    p_oatly = get_product("470889")
    assert p_oatly is not None
    assert _allergen_val(p_oatly, "milk") == "free"
    assert p_oatly.allergen_source["milk"] == "structured"

    oat_plan = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="oat drink",
            excluded_allergens=[],
            quantity=10.0,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    _assert_plan_hardblock(oat_plan, ("milk", "nuts"))
    assert [ln.product_id for ln in oat_plan.lines] == ["470889"]
