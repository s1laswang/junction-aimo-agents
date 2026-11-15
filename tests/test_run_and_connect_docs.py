from __future__ import annotations

import asyncio
import io
import json
import pathlib
import re
import tokenize

from fastapi.testclient import TestClient
import pytest

from aimo import config
import aimo.catalog as cat
from aimo.backend import catalog_store, client as bc, orders as store, replay
from aimo.catalog import textsearch
from aimo.gateway import human_auth
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from conformance import profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
HASH_CHAR = chr(0x23)
TEST_PIN = "test-approver-pin-12ch"
AGENT_HEADERS = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}

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


def _read_doc(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing doc: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty doc: {rel_path}"
    return text


def _close_cached_catalog() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def scratch_gateway(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    orig_config = {k: getattr(config, k) for k in _CONFIG_ATTRS}
    orig_cstore = {k: getattr(catalog_store, k) for k in _CSTORE_ATTRS}
    orig_cat_source = cat._SOURCE
    orig_store_db = store.DB_PATH
    orig_bc_client = bc._client

    data_dir = tmp_path / "data"
    key_dir = tmp_path / "keys"
    state_db = tmp_path / "state.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    _close_cached_catalog()
    human_auth.reset_for_tests()

    try:
        monkeypatch.setenv("AIMO_OFFLINE", "1")
        monkeypatch.setenv("AIMO_REPLAY", "1")
        monkeypatch.setenv("AIMO_API_KEY", "")
        monkeypatch.setenv("VALIO_API_KEY", "")
        monkeypatch.setenv("AIMO_APPROVER_PIN", TEST_PIN)
        monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
        monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
        monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
        monkeypatch.delenv("AIMO_FAULTS", raising=False)
        monkeypatch.delenv("AIMO_ENV", raising=False)
        monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)

        config.DATA_DIR = data_dir
        config.PRODUCT_JSON = data_dir / "valio_aimo_junction26_product_data.json"
        config.RELIABILITY_CSV = data_dir / "valio_aimo_junction26_delivery_reliability.csv"
        config.CO2_CSV = data_dir / "valio_aimo_junction26_carbon_footprint.csv"
        config.CATALOG_DB = data_dir / "catalog.sqlite"
        config.STATE_DB = state_db
        config.KEY_DIR = key_dir
        config.OFFLINE = True
        config.AIMO_API_KEY = ""
        store.DB_PATH = state_db

        src = cat._use_fixture_catalog()
        assert src == "fixture-subset"
        cat._SOURCE = src
        catalog_store.PRODUCT_JSON = config.PRODUCT_JSON
        catalog_store.RELIABILITY_CSV = config.RELIABILITY_CSV
        catalog_store.CO2_CSV = config.CO2_CSV
        catalog_store.CATALOG_DB = config.CATALOG_DB
        _close_cached_catalog()

        bc.set_client(replay.ReplayClient(replay.BUNDLED_DIR))
        app = create_app(with_mcp=False)
        with TestClient(app) as client:
            yield {"app": app, "client": client, "state_db": state_db, "key_dir": key_dir}
    finally:
        _close_cached_catalog()
        store.close_all()
        for k, v in orig_config.items():
            setattr(config, k, v)
        for k, v in orig_cstore.items():
            setattr(catalog_store, k, v)
        cat._SOURCE = orig_cat_source
        store.DB_PATH = orig_store_db
        bc.set_client(orig_bc_client)
        human_auth.reset_for_tests()


def _matches_openapi_template(cited_path: str, openapi_paths: set[str]) -> bool:
    if cited_path in openapi_paths:
        return True
    if cited_path.endswith("/*"):
        prefix = cited_path[:-1]
        return any(route.startswith(prefix) for route in openapi_paths)
    cited_parts = [seg for seg in cited_path.strip("/").split("/") if seg]
    for route in openapi_paths:
        route_parts = [seg for seg in route.strip("/").split("/") if seg]
        if len(route_parts) != len(cited_parts):
            continue
        ok = True
        for c_seg, r_seg in zip(cited_parts, route_parts):
            if c_seg == r_seg:
                continue
            if r_seg.startswith("{") and r_seg.endswith("}"):
                if (c_seg.startswith("{") and c_seg.endswith("}")) or "..." in c_seg:
                    continue
            ok = False
            break
        if ok:
            return True
    return False


def test_run_and_connect_docs_exist_and_have_zero_em_dashes_or_todos():
    todo_re = re.compile(r"\bTODO\b", re.IGNORECASE)
    for rel in ("docs/RUN.md", "docs/CONNECT.md", "docs/OPENNESS.md"):
        text = _read_doc(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO marker found in {rel}"


def test_connect_json_blocks_parse_for_mcp_clients():
    connect = _read_doc("docs/CONNECT.md")
    blocks = re.findall(r"```json\s*\n(.*?)```", connect, flags=re.DOTALL)
    assert len(blocks) == 3
    parsed_servers: list[dict] = []
    for raw in blocks:
        payload = json.loads(raw)
        assert "mcpServers" in payload
        assert "valio-aimo" in payload["mcpServers"]
        server_cfg = payload["mcpServers"]["valio-aimo"]
        assert isinstance(server_cfg, dict)
        parsed_servers.append(server_cfg)

    assert any("command" in cfg and cfg.get("env", {}).get("AIMO_CUSTOMER_ID") == "hotel-aurora" for cfg in parsed_servers)
    assert any(cfg.get("url") == "http://localhost:8000/mcp" for cfg in parsed_servers)
    assert any(cfg.get("httpUrl") == "http://localhost:8000/mcp" for cfg in parsed_servers)


def test_connect_lists_all_13_mcp_tools_no_approval_tool_and_known_customer_auth():
    connect = _read_doc("docs/CONNECT.md")
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    assert set(profile.MCP_TOOLS).issubset(set(tool_names))
    expected_extensions = {"list_recipes", "plan_recipe", "check_standing_order_drift", "propose_offer"}
    assert set(tool_names) - set(profile.MCP_TOOLS) == expected_extensions
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []
    for tool in tools:
        assert (tool.description or "").strip(), f"MCP tool {tool.name} has empty description"
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        assert isinstance(schema, dict) and schema.get("type") == "object"

    missing = [name for name in tool_names if f"`{name}`" not in connect]
    assert missing == [], f"MCP tools missing from docs/CONNECT.md: {missing}"

    assert "13" in connect
    assert "otap/0.1" in connect
    assert "no approval tool" in connect.lower()
    assert "X-Customer-Id" in connect
    assert "AIMO_CUSTOMER_ID" in connect
    assert "/v1/agent/runs" in connect
    assert "/v1/products/search" in connect
    assert "X-Aimo-Faults" in connect
    for step_path in (
        "/v1/products/search",
        "/v1/offers",
        "/v1/plan",
        "/v1/quotes",
        "/v1/orders",
        "/v1/approvals/ORD-",
        "/v1/auth/approver-session",
        "/v1/audit/verify",
    ):
        assert step_path in connect


def test_run_documents_replay_approver_pins_requote_and_13_tools():
    run_doc = _read_doc("docs/RUN.md")
    for required in (
        "AIMO_REPLAY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_APPROVER_PINS",
        "AIMO_APPROVER_PIN",
        "approver_pins.json",
        "approver_locked",
        "409",
        "requote_needed",
        "13 tools",
        "make accept",
    ):
        assert required in run_doc, f"docs/RUN.md missing {required!r}"

    assert "pytest has 8 failures" not in run_doc.lower()
    assert "8 tests that need it are not yet marked to skip" not in run_doc.lower()


def test_run_exception_matrix_covers_rubric_rows_and_referenced_tests():
    run_doc = _read_doc("docs/RUN.md")
    section_heading = HASH_CHAR * 2 + " Exceptions: what happens, and how to see it"
    assert section_heading in run_doc
    after_heading = run_doc.split(section_heading, 1)[1]
    next_h = re.search(rf"\n{re.escape(HASH_CHAR)}{{2,3}}\s+", after_heading)
    section = after_heading[: next_h.start()] if next_h else after_heading

    table_lines = [ln.strip() for ln in section.splitlines() if ln.strip().startswith("|")]
    assert len(table_lines) >= 11
    header_cells = [c.strip() for c in table_lines[0].strip("|").split("|")]
    assert header_cells == ["Exception", "Scenario or test", "One command", "What you see"]

    data_rows = []
    for raw_line in table_lines[2:]:
        cells = [c.strip() for c in raw_line.strip("|").split("|")]
        assert len(cells) == 4, f"Malformed exception matrix row: {raw_line}"
        assert all(cells), f"Empty cell in exception matrix row: {raw_line}"
        data_rows.append(cells)

    assert len(data_rows) >= 9

    labels_blob = "\n".join(r[0] for r in data_rows)
    for expected_exception in (
        "Partial availability",
        "Substitutions",
        "Delivery constraints: cut-off",
        "Delivery constraints: late delivery",
        "Allergen safety",
        "Price change",
        "Stock changing during approval",
        "Cut-off passing during approval",
        "LLM quota exhausted",
        "Prompt injection",
        "Agent trying to move the clock or fake stock",
    ):
        assert expected_exception in labels_blob, f"Exception matrix missing row {expected_exception!r}"

    test_file_re = re.compile(r"tests/test_[a-z0-9_]+\.py")
    for exc_label, scenario_or_test, one_cmd, what_you_see in data_rows:
        assert "uv run " in one_cmd, f"Row {exc_label!r} command missing 'uv run ': {one_cmd}"
        assert len(what_you_see) >= 20, f"Row {exc_label!r} explanation too short"
        cited_files = test_file_re.findall(scenario_or_test)
        assert cited_files, f"Row {exc_label!r} does not cite any tests/test_*.py file"
        existing = [rel for rel in cited_files if (ROOT / rel).is_file()]
        assert existing, f"Row {exc_label!r} cites no existing test file on disk: {cited_files}"


def test_every_cited_gateway_path_in_run_and_connect_exists_on_live_app(scratch_gateway):
    app = scratch_gateway["app"]
    client = scratch_gateway["client"]
    openapi_paths = set(app.openapi()["paths"].keys())
    special_gateway_paths = {"/mcp", "/ui", "/docs", "/healthz"}

    assert "/healthz" in openapi_paths
    assert client.get("/healthz").status_code == 200
    assert app.docs_url == "/docs"
    assert client.get("/docs").status_code == 200
    assert any(getattr(r, "path", None) == "/ui" for r in app.routes)
    assert client.get("/ui/").status_code == 200
    otap_resp = client.get("/.well-known/otap.json")
    assert otap_resp.status_code == 200
    assert otap_resp.json()["mcp"]["url"].endswith("/mcp")

    path_re = re.compile(
        r"(?:(?<=:8000)|(?<![A-Za-z0-9_./-]))"
        r"(/(?:v1|\.well-known)(?:/[A-Za-z0-9_.{}*-]+)+"
        r"|/mcp(?![A-Za-z0-9_-])"
        r"|/ui(?![A-Za-z0-9_-])"
        r"|/docs(?![A-Za-z0-9_/-])"
        r"|/healthz(?![A-Za-z0-9_-]))"
    )

    all_cited: set[str] = set()
    for rel in ("docs/RUN.md", "docs/CONNECT.md"):
        cited = set(path_re.findall(_read_doc(rel)))
        assert cited, f"No gateway paths extracted from {rel}"
        all_cited |= cited
        unresolved = [
            p
            for p in sorted(cited)
            if p not in special_gateway_paths and not _matches_openapi_template(p, openapi_paths)
        ]
        assert unresolved == [], f"Unresolved gateway paths in {rel}: {unresolved}"

    assert special_gateway_paths.issubset(all_cited)
    for expected in (
        "/.well-known/otap.json",
        "/.well-known/jwks.json",
        "/v1/products/search",
        "/v1/offers",
        "/v1/plan",
        "/v1/quotes",
        "/v1/orders",
        "/v1/approvals",
        "/v1/auth/approver-session",
        "/v1/audit/verify",
        "/v1/agent/runs",
    ):
        assert expected in all_cited


def test_live_http_status_contracts_from_run_and_connect_docs(scratch_gateway):
    client = scratch_gateway["client"]

    otap = client.get("/.well-known/otap.json")
    assert otap.status_code == 200
    otap_body = otap.json()
    assert otap_body["protocol"] == "otap/0.1"
    assert otap_body["mcp"]["server_name"] == "valio-aimo-trusted-trade"
    assert {"MCP", "OpenAPI 3.1"}.issubset(set(otap_body["supported_standards"]))

    jwks = client.get("/.well-known/jwks.json")
    assert jwks.status_code == 200
    keys = jwks.json().get("keys", [])
    assert keys and keys[0]["kty"] == "OKP" and keys[0]["crv"] == "Ed25519"

    r_no_cust_search = client.get("/v1/products/search", params={"q": "milk"})
    assert r_no_cust_search.status_code == 401
    assert r_no_cust_search.json().get("error") == "identity_required"

    r_no_cust_offers = client.post(
        "/v1/offers",
        json={"product_ids": ["464306"], "requested_date": "2026-11-16"},
    )
    assert r_no_cust_offers.status_code == 401
    assert r_no_cust_offers.json().get("error") == "identity_required"

    r_no_cust_quotes = client.post(
        "/v1/quotes",
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
    )
    assert r_no_cust_quotes.status_code == 401
    assert r_no_cust_quotes.json().get("error") == "identity_required"

    r_no_cust_orders = client.post("/v1/orders", json={"quote_id": "Q-missing", "quote_hash": "0" * 64})
    assert r_no_cust_orders.status_code == 401
    assert r_no_cust_orders.json().get("error") == "identity_required"

    r_no_cust_approvals = client.get("/v1/approvals")
    assert r_no_cust_approvals.status_code == 401
    assert r_no_cust_approvals.json().get("error") == "identity_required"

    unk_headers = {"X-Customer-Id": "unknown-corp", "X-Agent-Id": "buyer-1"}
    for resp in (
        client.get("/v1/products/search", params={"q": "milk"}, headers=unk_headers),
        client.post("/v1/offers", headers=unk_headers, json={"product_ids": ["464306"], "requested_date": "2026-11-16"}),
        client.post(
            "/v1/plan",
            headers=unk_headers,
            json={
                "customer_id": "unknown-corp",
                "product_query": "lactose-free milk",
                "quantity": 10,
                "unit": "L",
                "delivery_by": "2026-11-16",
            },
        ),
        client.post(
            "/v1/quotes",
            headers=unk_headers,
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 10}]},
        ),
        client.post("/v1/orders", headers=unk_headers, json={"quote_id": "Q-missing", "quote_hash": "0" * 64}),
    ):
        assert resp.status_code == 403
        assert resp.json().get("error") == "unknown_customer"

    for bad_qty in (0, -5):
        r_bad_plan = client.post(
            "/v1/plan",
            headers=AGENT_HEADERS,
            json={
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "quantity": bad_qty,
                "unit": "L",
                "delivery_by": "2026-11-16",
            },
        )
        assert r_bad_plan.status_code == 422

        r_bad_quote = client.post(
            "/v1/quotes",
            headers=AGENT_HEADERS,
            json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": bad_qty}]},
        )
        assert r_bad_quote.status_code == 422

    r_unauth_runs = client.post("/v1/agent/runs", json={"text": "10 L lactose-free milk by Monday"})
    assert r_unauth_runs.status_code == 401
    assert r_unauth_runs.json().get("error") in {"identity_required", "approver_auth_required"}

    r_unauth_answer = client.post("/v1/agent/runs/run-missing/answer", json={"option_id": "d0"})
    assert r_unauth_answer.status_code == 401

    r_search = client.get(
        "/v1/products/search",
        params={"q": "lactose-free milk", "limit": 5},
        headers=AGENT_HEADERS,
    )
    assert r_search.status_code == 200
    assert len(r_search.json()) >= 1

    r_offers = client.post(
        "/v1/offers",
        headers=AGENT_HEADERS,
        json={"product_ids": ["463038", "464306", "469363"], "requested_date": "2026-11-16"},
    )
    assert r_offers.status_code == 200
    offers_by_id = {o["product_id"]: o for o in r_offers.json()}
    assert offers_by_id["463038"]["cutoff_passed"] is True
    assert offers_by_id["464306"]["available"] is True
    assert offers_by_id["464306"]["unit_price"] == pytest.approx(1.01)

    r_plan = client.post(
        "/v1/plan",
        headers=AGENT_HEADERS,
        json={
            "customer_id": "hotel-aurora",
            "product_query": "lactose-free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": 1000,
            "unit": "L",
            "delivery_by": "2026-11-16",
            "preferred_product_ids": ["463038"],
        },
    )
    assert r_plan.status_code == 200
    plan_body = r_plan.json()
    assert plan_body["lines"][0]["product_id"] == "464306"
    assert "injection_screen" in plan_body["lines"][0]
    assert "injection_screen" in plan_body["candidates"][0]

    r_quote = client.post(
        "/v1/quotes",
        headers=AGENT_HEADERS,
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
    )
    assert r_quote.status_code == 200
    quote = r_quote.json()
    assert quote["total_net"] == pytest.approx(1010.0)
    assert quote["quote_hash"] and quote["signature"] and quote["kid"]

    r_order = client.post(
        "/v1/orders",
        headers=AGENT_HEADERS,
        json={"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
    )
    assert r_order.status_code == 200
    order = r_order.json()
    order_id = order["order_id"]
    assert order["status"] == "pending_approval"

    r_agent_approve = client.post(
        f"/v1/approvals/{order_id}/decision",
        headers=AGENT_HEADERS,
        json={"decision": "approve", "approver": "anna@hotel-aurora.example", "quote_hash": quote["quote_hash"]},
    )
    assert r_agent_approve.status_code == 403
    assert r_agent_approve.json().get("error") == "agent_cannot_approve"

    r_agent_with_pin = client.post(
        f"/v1/approvals/{order_id}/decision",
        headers={**AGENT_HEADERS, "X-Approver-Pin": TEST_PIN},
        json={"decision": "approve", "approver": "anna@hotel-aurora.example", "quote_hash": quote["quote_hash"]},
    )
    assert r_agent_with_pin.status_code == 403
    assert r_agent_with_pin.json().get("error") == "agent_cannot_approve"

    r_agent_session = client.post(
        "/v1/auth/approver-session",
        headers={**AGENT_HEADERS, "X-Approver-Pin": TEST_PIN},
        json={"approver": "anna@hotel-aurora.example"},
    )
    assert r_agent_session.status_code == 403
    assert r_agent_session.json().get("error") == "agent_cannot_approve"

    r_missing_human_cred = client.post(
        f"/v1/approvals/{order_id}/decision",
        json={"decision": "approve", "approver": "anna@hotel-aurora.example", "quote_hash": quote["quote_hash"]},
    )
    assert r_missing_human_cred.status_code == 401
    assert r_missing_human_cred.json().get("error") == "approver_auth_required"

    r_session = client.post(
        "/v1/auth/approver-session",
        headers={"X-Approver-Pin": TEST_PIN},
        json={"approver": "anna@hotel-aurora.example"},
    )
    assert r_session.status_code == 200
    session_token = r_session.json()["session"]

    r_mismatch = client.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": session_token},
        json={"decision": "approve", "approver": "mikko@hotel-aurora.example", "quote_hash": quote["quote_hash"]},
    )
    assert r_mismatch.status_code == 403
    assert r_mismatch.json().get("error") == "approver_mismatch"

    r_wrong_hash = client.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": session_token},
        json={"decision": "approve", "quote_hash": "0" * 64},
    )
    assert r_wrong_hash.status_code == 409
    assert r_wrong_hash.json().get("error") == "quote_hash_mismatch"

    lock_statuses = []
    for _ in range(5):
        bad_login = client.post(
            "/v1/auth/approver-session",
            headers={"X-Approver-Pin": "wrong-passphrase-attempt"},
            json={"approver": "mikko@hotel-aurora.example"},
        )
        lock_statuses.append((bad_login.status_code, bad_login.json().get("error")))
    assert [s for s, _ in lock_statuses[:4]] == [401, 401, 401, 401]
    assert lock_statuses[4] == (429, "approver_locked")

    r_approved = client.post(
        f"/v1/approvals/{order_id}/decision",
        headers={"X-Approver-Session": session_token},
        json={"decision": "approve", "quote_hash": quote["quote_hash"]},
    )
    assert r_approved.status_code == 200
    approved_body = r_approved.json()
    assert approved_body["status"] == "placed"
    assert approved_body["response_code"] == "AP"

    r_report = client.get(f"/v1/audit/report/{order_id}")
    assert r_report.status_code == 200
    assert "approval.refused" in r_report.text

    r_verify = client.get("/v1/audit/verify")
    assert r_verify.status_code == 200
    assert r_verify.json()["ok"] is True

    q2 = client.post(
        "/v1/quotes",
        headers=AGENT_HEADERS,
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1000}]},
    ).json()
    o2 = client.post(
        "/v1/orders",
        headers=AGENT_HEADERS,
        json={"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]},
    ).json()
    r_requote = client.post(
        f"/v1/approvals/{o2['order_id']}/decision",
        headers={"X-Approver-Session": session_token, "X-Aimo-Faults": "partial:464306:400"},
        json={"decision": "approve", "quote_hash": q2["quote_hash"]},
    )
    assert r_requote.status_code == 409
    assert r_requote.json().get("error") == "requote_needed"


def test_live_catalog_missing_returns_503_when_replay_off(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    orig_config = {k: getattr(config, k) for k in _CONFIG_ATTRS}
    orig_cstore = {k: getattr(catalog_store, k) for k in _CSTORE_ATTRS}
    orig_cat_source = cat._SOURCE
    orig_store_db = store.DB_PATH

    empty_dir = tmp_path / "empty_data"
    empty_dir.mkdir(parents=True, exist_ok=True)
    state_db = tmp_path / "state.sqlite"
    _close_cached_catalog()

    try:
        monkeypatch.setenv("AIMO_OFFLINE", "1")
        monkeypatch.setenv("AIMO_REPLAY", "0")
        monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
        monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

        config.DATA_DIR = empty_dir
        config.PRODUCT_JSON = empty_dir / "valio_aimo_junction26_product_data.json"
        config.RELIABILITY_CSV = empty_dir / "valio_aimo_junction26_delivery_reliability.csv"
        config.CO2_CSV = empty_dir / "valio_aimo_junction26_carbon_footprint.csv"
        config.CATALOG_DB = empty_dir / "catalog.sqlite"
        config.STATE_DB = state_db
        store.DB_PATH = state_db
        catalog_store.PRODUCT_JSON = config.PRODUCT_JSON
        catalog_store.RELIABILITY_CSV = config.RELIABILITY_CSV
        catalog_store.CO2_CSV = config.CO2_CSV
        catalog_store.CATALOG_DB = config.CATALOG_DB
        cat._SOURCE = "missing"

        with TestClient(create_app(with_mcp=False)) as client:
            r_search = client.get("/v1/products/search", params={"q": "milk"}, headers=AGENT_HEADERS)
            assert r_search.status_code == 503
            assert r_search.json().get("error") == "catalog_missing"

            r_plan = client.post(
                "/v1/plan",
                headers=AGENT_HEADERS,
                json={
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose-free milk",
                    "quantity": 10,
                    "unit": "L",
                    "delivery_by": "2026-11-16",
                },
            )
            assert r_plan.status_code == 503
            assert r_plan.json().get("error") == "catalog_missing"
    finally:
        _close_cached_catalog()
        store.close_all()
        for k, v in orig_config.items():
            setattr(config, k, v)
        for k, v in orig_cstore.items():
            setattr(catalog_store, k, v)
        cat._SOURCE = orig_cat_source
        store.DB_PATH = orig_store_db


def test_valio_mock_openapi_spec_and_brief_md_integrity():
    openapi_path = ROOT / "docs" / "valio-mock-openapi.json"
    assert openapi_path.is_file(), "Missing docs/valio-mock-openapi.json"
    spec = json.loads(openapi_path.read_text(encoding="utf-8"))
    assert isinstance(spec, dict)
    assert str(spec.get("openapi", "")).startswith("3.")
    assert {"openapi", "info", "paths", "components"}.issubset(set(spec.keys()))
    assert isinstance(spec["info"], dict)
    assert str(spec["info"].get("title", "")).strip()
    assert str(spec["info"].get("version", "")).strip()
    assert isinstance(spec["paths"], dict) and spec["paths"]
    for upstream_path in (
        "/api/ping",
        "/api/price",
        "/api/availability",
        "/api/products/download",
        "/api/delivery-reliability/download",
        "/api/carbon-footprint/download",
    ):
        assert upstream_path in spec["paths"], f"docs/valio-mock-openapi.json missing {upstream_path}"

    brief_text = _read_doc("BRIEF.md")
    assert len(brief_text) >= 500
    assert EM_DASH not in brief_text
    assert "Valio Aimo" in brief_text
    assert "Core requirements" in brief_text
    assert "Judging" in brief_text
    conflict_re = re.compile(r"^(?:<{7}|={7}|>{7})(?:\s|$)", re.MULTILINE)
    assert conflict_re.findall(brief_text) == []


def test_openness_has_production_path_and_pilot_kpis():
    openness = _read_doc("docs/OPENNESS.md")
    heading = HASH_CHAR * 2 + " Production path"
    assert heading in openness
    section = openness.split(heading, 1)[1]
    for required in (
        "Read-only pilot",
        "quote:create",
        "order:submit",
        "Autonomy inside limits",
        "BackendClient",
        "OAuth 2.1",
        "Pilot KPIs",
    ):
        assert required in section, f"docs/OPENNESS.md Production path missing {required!r}"


def test_relative_links_in_run_connect_and_openness_resolve():
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for rel in ("docs/RUN.md", "docs/CONNECT.md", "docs/OPENNESS.md"):
        doc = ROOT / rel
        stripped = re.sub(r"```.*?```", "", _read_doc(rel), flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", HASH_CHAR)):
                continue
            rel_target = target.split(HASH_CHAR, 1)[0].split("?", 1)[0]
            if not rel_target:
                continue
            resolved = (doc.parent / rel_target).resolve()
            assert resolved.exists(), f"Broken link in {rel}: {raw_target}"


def test_self_hygiene_zero_comments_and_zero_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"Comments found in {self_path.name}: {comments}"
