from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient
import pytest

from aimo import config, gemini
import aimo.catalog as cat
from aimo.agents.seller import ProposalError, find_cheaper, propose_offer
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.catalog import allergens as allergens_mod
from aimo.catalog import embeddings, gemini_allergens, get_product, plan_need, textsearch
from aimo.catalog.allergens import derive
from aimo.gateway import human_auth, resilience, verify as verify_mod
from aimo.gateway.app import create_app
from aimo.models import EU14, Need, Product, Quote, QuoteLine
from aimo.trust import policy as trust_policy
from aimo.trust import signing
from aimo.trust.audit import audit as audit_log

DELIVERY = date(2026, 11, 18)
REAL_POLICY_DIR = Path(trust_policy.__file__).resolve().parent / "policies"

SCHOOL_QUERIES: list[tuple[str, str]] = [
    ("milk", "L"),
    ("lactose-free milk", "L"),
    ("whole milk", "L"),
    ("täysmaito", "L"),
    ("mjölk", "L"),
    ("cream", "L"),
    ("kerma", "L"),
    ("butter", "KG"),
    ("voi", "KG"),
    ("cheese", "KG"),
    ("juusto", "KG"),
    ("whey", "KG"),
    ("yoghurt", "KG"),
    ("almonds", "KG"),
    ("hazelnuts", "KG"),
    ("pähkinä", "KG"),
    ("oat drink", "L"),
    ("kaurajuoma", "L"),
]

SCHOOL_HTTP_QUERIES = frozenset({
    "milk",
    "lactose-free milk",
    "täysmaito",
    "kerma",
    "hazelnuts",
    "kaurajuoma",
})

HOSPITAL_QUERIES: list[tuple[str, str]] = [
    ("bread", "KG"),
    ("rye bread", "KG"),
    ("ruisleipä", "KG"),
    ("wheat flour", "KG"),
    ("vehnäjauho", "KG"),
    ("pasta", "KG"),
    ("barley", "KG"),
    ("spelt", "KG"),
    ("gluten-free bread", "KG"),
]

HOSPITAL_HTTP_QUERIES = frozenset({
    "bread",
    "ruisleipä",
    "pasta",
    "gluten-free bread",
})

RECIPE_CASES: list[tuple[str, str]] = [
    ("lasagne", "lasagne"),
    ("chicken_pasta", "chicken pasta"),
    ("oat_porridge", "oat-porridge"),
]

_PRODUCT_CACHE: dict[str, Product] = {}

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
    _PRODUCT_CACHE.clear()
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
    verify_mod._last_trust_policy_dir = None


def _reset_breakers() -> None:
    for br in resilience._breakers.values():
        br.reset()


@pytest.fixture(scope="module")
def replay_catalog_bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    product_gz = cat.FIXTURE_CATALOG_DIR / "valio_aimo_junction26_product_data.json.gz"
    if not (replay.BUNDLED_DIR / "meta.json").is_file() or not product_gz.is_file():
        pytest.skip("recorded replay snapshot or fixture catalog is not present")

    orig_config = {k: getattr(config, k) for k in _CONFIG_ATTRS}
    orig_cstore = {k: getattr(catalog_store, k) for k in _CSTORE_ATTRS}
    orig_cat_source = cat._SOURCE
    orig_store_db = store.DB_PATH
    orig_bc_client = bc._client
    orig_policy_dir = trust_policy.POLICY_DIR
    orig_allergen_pdir = allergens_mod._last_policy_dir
    orig_verify_pdir = verify_mod._last_trust_policy_dir

    base_dir = tmp_path_factory.mktemp("allergen_replay_e2e")
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
        mp.setenv("AIMO_REPLAY", "1")
        mp.setenv("AIMO_OFFLINE", "1")
        mp.setenv("AIMO_API_KEY", "")
        mp.setenv("VALIO_API_KEY", "")
        mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)
        mp.setattr(config, "DATA_DIR", data_dir)
        mp.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
        mp.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
        mp.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
        mp.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
        mp.setattr(config, "OFFLINE", True)
        mp.setattr(config, "AIMO_API_KEY", "")
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

        replay_client = replay.ReplayClient(replay.BUNDLED_DIR)
        app = create_app(with_mcp=False)
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
        verify_mod._last_trust_policy_dir = orig_verify_pdir
        human_auth.reset_for_tests()
        _reset_breakers()
        if hasattr(gemini.client, "cache_clear"):
            gemini.client.cache_clear()


@pytest.fixture(autouse=True)
def isolated_replay_env(
    replay_catalog_bundle: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    _reset_breakers()

    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.delenv("AIMO_NOW_FROZEN", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)

    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "DATA_DIR", replay_catalog_bundle["data_dir"])
    monkeypatch.setattr(config, "PRODUCT_JSON", replay_catalog_bundle["product_json"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", replay_catalog_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", replay_catalog_bundle["co2_csv"])
    monkeypatch.setattr(config, "CATALOG_DB", replay_catalog_bundle["catalog_db"])
    monkeypatch.setattr(config, "KEY_DIR", replay_catalog_bundle["key_dir"])

    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", replay_catalog_bundle["product_json"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", replay_catalog_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", replay_catalog_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", replay_catalog_bundle["catalog_db"])
    monkeypatch.setattr(cat, "_SOURCE", "fixture-subset")
    monkeypatch.setattr(trust_policy, "POLICY_DIR", REAL_POLICY_DIR)

    state_db = replay_catalog_bundle["state_db"]
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)

    def _forbid_llm(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("Gemini network calls must not run in keyless replay tests")

    rows_by_id = replay_catalog_bundle["rows_by_id"]

    def _fast_get_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
        return {str(i): dict(rows_by_id[str(i)]) for i in ids if str(i) in rows_by_id}

    monkeypatch.setattr("aimo.gemini.generate", _forbid_llm)
    monkeypatch.setattr("aimo.gemini.embed", _forbid_llm)
    monkeypatch.setattr(textsearch, "get_rows", _fast_get_rows)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *_a, **_k: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *_a, **_k: [])
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda _pid: None)

    if textsearch._con.cache_info().currsize == 0:
        textsearch._con()

    human_auth.reset_for_tests()
    prev_client = bc._client
    bc.set_client(replay_catalog_bundle["replay_client"])
    try:
        yield
    finally:
        bc.set_client(prev_client)
        human_auth.reset_for_tests()
        _reset_breakers()


@pytest.fixture
def client(replay_catalog_bundle: dict[str, Any]) -> TestClient:
    return replay_catalog_bundle["http_client"]


def _allergen_val(prod: Product, group: str) -> str:
    raw = prod.allergens.get(group, "unknown")
    return raw.value if hasattr(raw, "value") else str(raw)


def _assert_product_structured_free(product_id: str, groups: tuple[str, ...]) -> Product:
    prod = _PRODUCT_CACHE.get(product_id)
    if prod is None:
        prod = get_product(product_id)
        assert prod is not None, f"missing catalog product {product_id}"
        _PRODUCT_CACHE[product_id] = prod
    for group in groups:
        status = _allergen_val(prod, group)
        source = prod.allergen_source.get(group, "")
        assert status == "free", (product_id, prod.name, group, status, source)
        assert source.startswith("structured"), (product_id, prod.name, group, status, source)
    return prod


def _assert_plan_allergen_safe(
    plan_obj: Any,
    groups: tuple[str, ...],
) -> None:
    lines = plan_obj.lines if hasattr(plan_obj, "lines") else plan_obj["lines"]
    candidates = plan_obj.candidates if hasattr(plan_obj, "candidates") else plan_obj["candidates"]

    for ln in lines:
        pid = ln.product_id if hasattr(ln, "product_id") else ln["product_id"]
        flags = ln.flags if hasattr(ln, "flags") else ln.get("flags", [])
        _assert_product_structured_free(pid, groups)
        assert "allergen_unknown" not in flags

    for cand in candidates:
        tier = cand.tier if hasattr(cand, "tier") else cand["tier"]
        if tier == "rejected":
            continue
        pid = cand.product.id if hasattr(cand, "product") else cand["product"]["id"]
        flags = cand.flags if hasattr(cand, "flags") else cand.get("flags", [])
        _assert_product_structured_free(pid, groups)
        assert "allergen_unknown" not in flags


def test_file_hygiene_zero_comments_and_zero_em_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src


def test_replay_snapshot_and_fixture_catalog_isolated_from_live_data(
    client: TestClient,
    replay_catalog_bundle: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert replay.mode() == "forced"
    assert replay.replay_dir() == replay.BUNDLED_DIR
    assert cat.catalog_source() == "fixture-subset"
    assert cat.catalog_available() is True
    assert config.DATA_DIR != config.ROOT / "data"
    assert config.PRODUCT_JSON.is_relative_to(replay_catalog_bundle["data_dir"])
    assert trust_policy.POLICY_DIR == REAL_POLICY_DIR
    assert len(replay_catalog_bundle["rows_by_id"]) == 669
    raw_rows = replay_catalog_bundle["orig_get_rows"](["470889", "473328"])
    assert raw_rows["470889"]["allergens"]["milk"] == "free"
    assert raw_rows["473328"]["allergen_source"]["gluten"] == "structured:claim"
    assert "2026-11-18" in bc.get_client().dates()

    health = client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert body["offline"] is True
    assert body["replay"]["available"] is True
    assert body["replay"]["forced"] is True
    assert body["replay"]["mode"] == "forced"

    empty_snap = tmp_path / "missing_snap"
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir()
    monkeypatch.setattr(replay, "BUNDLED_DIR", empty_snap)
    monkeypatch.setattr(config, "DATA_DIR", empty_data)
    monkeypatch.setattr(config, "PRODUCT_JSON", empty_data / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(cat, "FIXTURE_CATALOG_DIR", empty_snap / "catalog")
    assert replay.replay_dir() is None
    assert cat._use_fixture_catalog() == "missing"


@pytest.mark.parametrize(("query", "unit"), SCHOOL_QUERIES, ids=[q for q, _ in SCHOOL_QUERIES])
def test_school_tampere_replay_plan_need_and_http_plan_hardblock(
    client: TestClient,
    query: str,
    unit: str,
) -> None:
    need_omitted = Need(
        customer_id="school-tampere",
        product_query=query,
        quantity=20.0,
        unit=unit,
        delivery_by=DELIVERY,
    )
    assert need_omitted.excluded_allergens == []

    direct_plan = plan_need(need_omitted)
    assert {"milk", "nuts"} <= set(direct_plan.need.excluded_allergens)
    _assert_plan_allergen_safe(direct_plan, ("milk", "nuts"))

    if query in ("oat drink", "kaurajuoma"):
        assert [ln.product_id for ln in direct_plan.lines] == ["470889"]
        assert "allergen_safe_alternative" not in direct_plan.lines[0].flags

    if query in SCHOOL_HTTP_QUERIES:
        payload: dict[str, Any] = {
            "customer_id": "school-tampere",
            "product_query": query,
            "quantity": 20.0,
            "unit": unit,
            "delivery_by": DELIVERY.isoformat(),
        }
        if query in ("lactose-free milk", "kerma", "voi", "juusto", "hazelnuts", "kaurajuoma"):
            payload["excluded_allergens"] = []

        http_resp = client.post(
            "/v1/plan",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
            json=payload,
        )
        assert http_resp.status_code == 200, http_resp.text
        http_plan = http_resp.json()
        assert {"milk", "nuts"} <= set(http_plan["need"]["excluded_allergens"])
        _assert_plan_allergen_safe(http_plan, ("milk", "nuts"))


@pytest.mark.parametrize("qty", [2.0, 25.0])
def test_school_tampere_milk_plans_470889_alternative_and_requires_kitchen_manager_approval(
    client: TestClient,
    qty: float,
) -> None:
    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}

    direct = plan_need(
        Need(
            customer_id="school-tampere",
            product_query="milk",
            quantity=qty,
            unit="L",
            delivery_by=DELIVERY,
        )
    )
    assert {"milk", "nuts"} <= set(direct.need.excluded_allergens)
    assert [ln.product_id for ln in direct.lines] == ["470889"]
    assert "allergen_safe_alternative" in direct.lines[0].flags
    _assert_plan_allergen_safe(direct, ("milk", "nuts"))

    r_plan = client.post(
        "/v1/plan",
        headers=headers,
        json={
            "customer_id": "school-tampere",
            "product_query": "milk",
            "quantity": qty,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    plan_json = r_plan.json()
    assert {"milk", "nuts"} <= set(plan_json["need"]["excluded_allergens"])
    assert [ln["product_id"] for ln in plan_json["lines"]] == ["470889"]
    assert "allergen_safe_alternative" in plan_json["lines"][0]["flags"]
    _assert_plan_allergen_safe(plan_json, ("milk", "nuts"))

    r_quote = client.post(
        "/v1/quotes",
        headers=headers,
        json={
            "delivery_date": DELIVERY.isoformat(),
            "lines": [
                {
                    "product_id": ln["product_id"],
                    "qty_sales_units": ln["qty_sales_units"],
                    "tier": ln["tier"],
                    "flags": ln["flags"],
                }
                for ln in plan_json["lines"]
            ],
            "need": plan_json["need"],
        },
    )
    assert r_quote.status_code == 200, r_quote.text
    quote = r_quote.json()
    assert [ln["product_id"] for ln in quote["lines"]] == ["470889"]
    for qline in quote["lines"]:
        prod = _assert_product_structured_free(qline["product_id"], ("milk", "nuts"))
        assert _allergen_val(prod, "milk") == "free"

    r_order = client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    order = r_order.json()
    assert order["status"] == "pending_approval"
    assert order["decision"]["outcome"] == "needs_approval"
    assert order["decision"]["outcome"] != "auto_approve"
    assert order["decision"]["approver_role"] == "kitchen_manager"


@pytest.mark.parametrize(("query", "unit"), HOSPITAL_QUERIES, ids=[q for q, _ in HOSPITAL_QUERIES])
def test_hospital_meilahti_replay_plan_need_and_http_plan_hardblock(
    client: TestClient,
    query: str,
    unit: str,
) -> None:
    need = Need(
        customer_id="hospital-meilahti",
        product_query=query,
        quantity=10.0,
        unit=unit,
        delivery_by=DELIVERY,
    )
    assert need.excluded_allergens == []

    direct_plan = plan_need(need)
    assert "gluten" in direct_plan.need.excluded_allergens
    _assert_plan_allergen_safe(direct_plan, ("gluten",))

    if query in ("bread", "gluten-free bread"):
        assert len(direct_plan.lines) >= 1
        assert direct_plan.lines[0].product_id == "473328"

    if query in HOSPITAL_HTTP_QUERIES:
        payload: dict[str, Any] = {
            "customer_id": "hospital-meilahti",
            "product_query": query,
            "quantity": 10.0,
            "unit": unit,
            "delivery_by": DELIVERY.isoformat(),
        }
        if query in ("ruisleipä", "pasta", "gluten-free bread"):
            payload["excluded_allergens"] = []

        http_resp = client.post(
            "/v1/plan",
            headers={"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"},
            json=payload,
        )
        assert http_resp.status_code == 200, http_resp.text
        http_plan = http_resp.json()
        assert "gluten" in http_plan["need"]["excluded_allergens"]
        _assert_plan_allergen_safe(http_plan, ("gluten",))


def test_gateway_deterministic_fallback_plan_enforces_school_and_hospital_hardblock(
    client: TestClient,
) -> None:
    br = resilience.breaker("catalog.plan_need")
    br.fail("synthetic-1")
    br.fail("synthetic-2")
    assert br.is_open() is True

    r_school = client.post(
        "/v1/plan",
        headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "school-tampere",
            "product_query": "milk",
            "quantity": 10.0,
            "unit": "L",
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
        },
    )
    assert r_school.status_code == 200, r_school.text
    school_plan = r_school.json()
    assert {"milk", "nuts"} <= set(school_plan["need"]["excluded_allergens"])
    _assert_plan_allergen_safe(school_plan, ("milk", "nuts"))

    r_hosp = client.post(
        "/v1/plan",
        headers={"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"},
        json={
            "customer_id": "hospital-meilahti",
            "product_query": "bread",
            "quantity": 6.0,
            "unit": "KG",
            "delivery_by": DELIVERY.isoformat(),
            "excluded_allergens": [],
        },
    )
    assert r_hosp.status_code == 200, r_hosp.text
    hosp_plan = r_hosp.json()
    assert "gluten" in hosp_plan["need"]["excluded_allergens"]
    _assert_plan_allergen_safe(hosp_plan, ("gluten",))


def test_hospital_meilahti_claim_only_structured_claim_escalates_to_dietitian_with_nutrition_claim(
    client: TestClient,
) -> None:
    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}

    for real_claim_pid in ("473328", "468256"):
        prod = get_product(real_claim_pid)
        assert prod is not None
        assert _allergen_val(prod, "gluten") == "free"
        assert prod.allergen_source["gluten"] == "structured:claim"

        r_quote = client.post(
            "/v1/quotes",
            headers=headers,
            json={
                "delivery_date": DELIVERY.isoformat(),
                "lines": [{"product_id": real_claim_pid, "qty_sales_units": 2}],
            },
        )
        assert r_quote.status_code == 200, r_quote.text
        quote = r_quote.json()
        assert quote["total_net"] < 100.0

        r_order = client.post(
            "/v1/orders",
            headers=headers,
            json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        )
        assert r_order.status_code == 200, r_order.text
        order = r_order.json()
        assert order["status"] == "pending_approval"
        assert order["decision"]["outcome"] == "needs_approval"
        assert order["decision"]["approver_role"] == "dietitian"
        allergen_hits = [
            h
            for h in order["decision"]["hits"]
            if h["rule_id"] == f"allergens:{real_claim_pid}"
        ]
        assert len(allergen_hits) == 1
        assert allergen_hits[0]["outcome"] == "needs_approval"
        assert "nutrition claim" in allergen_hits[0]["message"].lower()

    status_map, source_map, _raw = derive(
        [{"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_GLUTEN"}]}]
    )
    assert status_map["gluten"] == "free"
    assert source_map["gluten"] == "structured:claim"

    synth_461893 = Product(
        id="461893",
        name="Claim-only Gluten-Free Bread 500 g",
        brand="TestBakery",
        category="21200",
        sales_unit="ST",
        content_amount=0.5,
        content_unit="KG",
        claims=["FREE_FROM_GLUTEN"],
        allergens={g: status_map[g] for g in EU14},
        allergen_source={g: source_map[g] for g in EU14},
    )
    now_utc = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    synth_quote = Quote(
        quote_id="Q-SYNTH-461893",
        customer_id="hospital-meilahti",
        agent_id="buyer-1",
        delivery_date=DELIVERY,
        lines=[
            QuoteLine(
                product_id="461893",
                product_name=synth_461893.name,
                qty_sales_units=2.0,
                sales_unit="ST",
                unit_price=3.50,
                line_total=7.00,
                tier="preferred",
            )
        ],
        total_net=7.00,
        total_gross=7.95,
        created_at=now_utc,
        expires_at=now_utc + timedelta(minutes=15),
    )
    mandate = trust_policy.get_mandate("hospital-meilahti", "buyer-1")
    decision = trust_policy.evaluate(
        synth_quote,
        mandate,
        products={"461893": synth_461893},
        now=now_utc,
    )
    assert decision.outcome == "needs_approval"
    assert decision.approver_role == "dietitian"
    synth_hits = [h for h in decision.hits if h.rule_id == "allergens:461893"]
    assert len(synth_hits) == 1
    assert synth_hits[0].outcome == "needs_approval"
    assert "nutrition claim" in synth_hits[0].message.lower()


@pytest.mark.parametrize(
    ("customer_id", "policy_groups", "expected_role", "unsafe_tamper_pid"),
    [
        ("school-tampere", ("milk", "nuts"), "kitchen_manager", "464306"),
        ("hospital-meilahti", ("gluten",), "dietitian", "460600"),
    ],
)
@pytest.mark.parametrize(
    ("case_label", "recipe_id"),
    RECIPE_CASES,
    ids=[label for label, _ in RECIPE_CASES],
)
def test_recipes_plan_quote_and_order_enforce_policy_allergens_on_replay_catalog(
    client: TestClient,
    customer_id: str,
    policy_groups: tuple[str, ...],
    expected_role: str,
    unsafe_tamper_pid: str,
    case_label: str,
    recipe_id: str,
) -> None:
    headers = {"X-Customer-Id": customer_id, "X-Agent-Id": "buyer-1"}

    r_plan = client.post(
        "/v1/recipes/plan",
        headers=headers,
        json={
            "recipe_id": recipe_id,
            "portions": 25,
            "delivery_by": DELIVERY.isoformat(),
        },
    )
    assert r_plan.status_code == 200, r_plan.text
    body = r_plan.json()
    assert body["customer_id"] == customer_id
    assert set(policy_groups) <= set(body["excluded_allergens"])

    if customer_id == "school-tampere":
        for ing in body["ingredients"]:
            assert not ing["query"].startswith("lactose-free ")

    planned_line_count = 0
    for ing in body["ingredients"]:
        assert set(policy_groups) <= set(ing["need"]["excluded_allergens"])
        for ln in ing["plan"]["lines"]:
            planned_line_count += 1
            _assert_product_structured_free(ln["product_id"], policy_groups)
            assert "allergen_unknown" not in ln.get("flags", [])
        for cand in ing["plan"]["candidates"]:
            if cand.get("tier") != "rejected":
                _assert_product_structured_free(cand["product_id"], policy_groups)

    assert planned_line_count >= 1
    qr = body["quote_request"]
    assert qr["lines"]
    for ql in qr["lines"]:
        _assert_product_structured_free(ql["product_id"], policy_groups)

    r_quote = client.post("/v1/quotes", headers=headers, json=qr)
    assert r_quote.status_code == 200, r_quote.text
    quote = r_quote.json()
    for ql in quote["lines"]:
        _assert_product_structured_free(ql["product_id"], policy_groups)

    r_order = client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert r_order.status_code == 200, r_order.text
    order = r_order.json()
    assert order["status"] == "pending_approval"
    assert order["decision"]["outcome"] == "needs_approval"
    assert order["decision"]["approver_role"] == expected_role

    if case_label == "lasagne":
        tampered_qr = {
            "customer_id": customer_id,
            "delivery_date": qr["delivery_date"],
            "lines": [*qr["lines"], {"product_id": unsafe_tamper_pid, "qty_sales_units": 1}],
        }
        r_bad_quote = client.post("/v1/quotes", headers=headers, json=tampered_qr)
        assert r_bad_quote.status_code == 200, r_bad_quote.text
        bad_quote = r_bad_quote.json()
        r_bad_order = client.post(
            "/v1/orders",
            headers=headers,
            json={"quote_id": bad_quote["quote_id"], "quote_hash": bad_quote["quote_hash"]},
        )
        assert r_bad_order.status_code == 200, r_bad_order.text
        bad_order = r_bad_order.json()
        assert bad_order["status"] == "rejected"
        assert bad_order["decision"]["outcome"] == "reject"


def test_proactive_seller_find_cheaper_and_proposals_respect_customer_policy_allergens(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller_headers = {"X-Agent-Id": "valio-aimo/seller-1"}

    for cust, groups in (
        ("school-tampere", ("milk", "nuts")),
        ("hospital-meilahti", ("gluten",)),
    ):
        props, notes = find_cheaper(cust, DELIVERY)
        assert isinstance(notes, list)
        for p in props:
            _assert_product_structured_free(p["product"].id, groups)

        with pytest.raises(ProposalError) as exc_info:
            propose_offer(cust, delivery_date=DELIVERY)
        assert exc_info.value.status == 404
        assert exc_info.value.code == "no_better_offer"

        r_none = client.post(
            "/v1/proposals",
            headers=seller_headers,
            json={"customer_id": cust, "delivery_date": DELIVERY.isoformat()},
        )
        assert r_none.status_code == 404
        assert r_none.json()["error"] == "no_better_offer"

    monkeypatch.setenv("AIMO_FAULTS", "stockout:470889;stockout:470863")
    direct_school_prop = propose_offer("school-tampere", delivery_date=DELIVERY)
    assert direct_school_prop["kind"] == "cheaper_equivalent"
    assert direct_school_prop["seller_can_order"] is False
    _assert_product_structured_free(direct_school_prop["product"]["product_id"], ("milk", "nuts"))
    assert direct_school_prop["policy_preview"]["outcome"] == "needs_approval"
    assert direct_school_prop["policy_preview"]["approver_role"] == "kitchen_manager"
    monkeypatch.delenv("AIMO_FAULTS", raising=False)

    for fault_spec in (
        "stockout:470889;stockout:470863",
        "price_shock:470889:3.0;price_shock:470863:3.0",
    ):
        r_prop = client.post(
            "/v1/proposals",
            headers={**seller_headers, "X-Aimo-Faults": fault_spec},
            json={"customer_id": "school-tampere", "delivery_date": DELIVERY.isoformat()},
        )
        assert r_prop.status_code == 200, r_prop.text
        prop = r_prop.json()
        assert prop["kind"] == "cheaper_equivalent"
        assert prop["seller_can_order"] is False
        proposed_pid = prop["product"]["product_id"]
        _assert_product_structured_free(proposed_pid, ("milk", "nuts"))
        assert prop["policy_preview"]["outcome"] == "needs_approval"
        assert prop["policy_preview"]["approver_role"] == "kitchen_manager"

        r_accept = client.post(
            f"/v1/proposals/{prop['proposal_id']}/accept",
            headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"},
        )
        assert r_accept.status_code == 200, r_accept.text
        accepted = r_accept.json()
        assert accepted["order"]["status"] == "pending_approval"
        assert accepted["order"]["decision"]["outcome"] == "needs_approval"

    r_hosp_fault = client.post(
        "/v1/proposals",
        headers={
            **seller_headers,
            "X-Aimo-Faults": "stockout:462775;price_shock:473328:3.0;price_shock:461765:3.0",
        },
        json={"customer_id": "hospital-meilahti", "delivery_date": DELIVERY.isoformat()},
    )
    assert r_hosp_fault.status_code in (200, 404)
    if r_hosp_fault.status_code == 200:
        hosp_prop = r_hosp_fault.json()
        assert hosp_prop["kind"] == "cheaper_equivalent"
        _assert_product_structured_free(hosp_prop["product"]["product_id"], ("gluten",))


@pytest.mark.parametrize(
    ("customer_id", "unsafe_pid", "expected_allergen_word"),
    [
        ("school-tampere", "464306", "milk"),
        ("school-tampere", "460768", "milk"),
        ("hospital-meilahti", "461578", "gluten"),
        ("hospital-meilahti", "460091", "gluten"),
    ],
)
def test_proactive_seller_forced_seller_pick_preview_rejects_and_buyer_accept_is_rejected(
    client: TestClient,
    customer_id: str,
    unsafe_pid: str,
    expected_allergen_word: str,
) -> None:
    if unsafe_pid in ("464306", "461578"):
        direct_prop = propose_offer(
            customer_id,
            product_id=unsafe_pid,
            qty_sales_units=2.0,
            delivery_date=DELIVERY,
            agent_id="valio-aimo/seller-1",
        )
        assert direct_prop["kind"] == "seller_pick"
        assert direct_prop["seller_can_order"] is False
        assert direct_prop["policy_preview"]["binding"] is False
        assert direct_prop["policy_preview"]["outcome"] == "reject"
        direct_allergen_hits = [
            h
            for h in direct_prop["policy_preview"]["hits"]
            if h["rule_id"] == f"allergens:{unsafe_pid}" and h["outcome"] == "reject"
        ]
        assert len(direct_allergen_hits) == 1
        assert expected_allergen_word in direct_allergen_hits[0]["message"].lower()

    r_create = client.post(
        "/v1/proposals",
        headers={"X-Agent-Id": "valio-aimo/seller-1"},
        json={
            "customer_id": customer_id,
            "product_id": unsafe_pid,
            "qty_sales_units": 2.0,
            "delivery_date": DELIVERY.isoformat(),
            "note": "seasonal promotion",
        },
    )
    assert r_create.status_code == 200, r_create.text
    prop = r_create.json()
    proposal_id = prop["proposal_id"]
    assert prop["kind"] == "seller_pick"
    assert prop["policy_preview"]["outcome"] == "reject"
    http_allergen_hits = [
        h
        for h in prop["policy_preview"]["hits"]
        if h["rule_id"] == f"allergens:{unsafe_pid}" and h["outcome"] == "reject"
    ]
    assert len(http_allergen_hits) == 1

    r_seller_accept = client.post(
        f"/v1/proposals/{proposal_id}/accept",
        headers={"X-Customer-Id": customer_id, "X-Agent-Id": "valio-aimo/seller-1"},
    )
    assert r_seller_accept.status_code == 403
    assert r_seller_accept.json()["error"] == "seller_cannot_order"

    r_buyer_accept = client.post(
        f"/v1/proposals/{proposal_id}/accept",
        headers={"X-Customer-Id": customer_id, "X-Agent-Id": "buyer-1"},
    )
    assert r_buyer_accept.status_code == 200, r_buyer_accept.text
    accepted = r_buyer_accept.json()
    assert accepted["proposal"]["status"] == "accepted"
    assert accepted["proposal"]["order_status"] == "rejected"
    assert accepted["order"]["status"] == "rejected"
    assert accepted["order"]["decision"]["outcome"] == "reject"
    order_allergen_hits = [
        h
        for h in accepted["order"]["decision"]["hits"]
        if h["rule_id"] == f"allergens:{unsafe_pid}" and h["outcome"] == "reject"
    ]
    assert len(order_allergen_hits) == 1
    assert expected_allergen_word in order_allergen_hits[0]["message"].lower()

    if unsafe_pid == "460091":
        audit_ver = client.get("/v1/audit/verify")
        assert audit_ver.status_code == 200
        assert audit_ver.json()["ok"] is True
        assert audit_ver.json()["broken_at"] is None
