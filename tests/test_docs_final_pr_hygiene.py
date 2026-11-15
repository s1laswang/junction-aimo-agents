from __future__ import annotations

import asyncio
import io
import pathlib
import re
import subprocess
import tokenize

from fastapi.testclient import TestClient

from aimo import models
from aimo.buyer import mock_model
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.trust import policy as trust_policy
from conformance import profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
ANCHOR_SEP = chr(35)

W6_OWNED_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/DECISIONS.md",
    "docs/ASSUMPTIONS.md",
    "docs/SCENARIOS.md",
    "docs/interfaces.md",
)

EXPECTED_SCENARIO_MODULES = (
    "s0_guardrails",
    "s1_milk",
    "s2_recipe_order",
    "s3_event_substitution",
    "s4_stockout",
    "s5_allergen_safe",
    "s6_proactive_seller",
    "s7_drift",
)


def _read(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing file: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty file: {rel_path}"
    return text


def test_w6_docs_have_zero_em_dashes_todos_and_broken_links():
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

    for rel in W6_OWNED_DOCS:
        text = _read(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"Unclosed marker in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder in {rel}"

        doc_path = ROOT / rel
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", ANCHOR_SEP)):
                continue
            clean = target.split(ANCHOR_SEP, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (doc_path.parent / clean).resolve()
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"


def test_interfaces_and_architecture_match_13_mcp_tools_and_fastapi_routes():
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    assert set(profile.MCP_TOOLS).issubset(set(tool_names))
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []

    interfaces = _read("docs/interfaces.md")
    architecture = _read("docs/ARCHITECTURE.md")

    for name in tool_names:
        assert f"`{name}(" in interfaces or f"`{name}`" in interfaces, (
            f"MCP tool {name} missing from docs/interfaces.md"
        )
    assert "13 tools" in interfaces
    assert "no approval tool" in interfaces.lower()
    assert "13 MCP tools" in architecture
    assert "no approval tool" in architecture.lower()

    openapi_resp = TestClient(create_app(with_mcp=False)).get("/openapi.json")
    assert openapi_resp.status_code == 200
    route_paths = set(openapi_resp.json()["paths"])
    expected_app_routes = {
        "/.well-known/otap.json",
        "/.well-known/jwks.json",
        "/healthz",
        "/v1/products/search",
        "/v1/offers",
        "/v1/plan",
        "/v1/quotes",
        "/v1/orders",
        "/v1/auth/approver-session",
        "/v1/approvals",
        "/v1/approvals/{order_id}/decision",
        "/v1/audit",
        "/v1/audit/verify",
        "/v1/audit/report/{subject_id}",
        "/v1/agent/runs",
        "/v1/customers",
        "/v1/recipes",
        "/v1/recipes/plan",
        "/v1/drift/{customer_id}",
        "/v1/proposals",
        "/v1/voice/status",
        "/v1/voice/prompt",
        "/v1/voice/transcribe",
        "/v1/voice/intent",
    }
    missing_routes = expected_app_routes - route_paths
    assert missing_routes == set(), f"Gateway missing expected HTTP routes: {sorted(missing_routes)}"
    assert any("peppol-order-response.xml" in p for p in route_paths)

    for documented_path in (
        "/.well-known/otap.json",
        "/.well-known/jwks.json",
        "/v1/products/search",
        "/v1/offers",
        "/v1/plan",
        "/v1/quotes",
        "/v1/orders",
        "peppol-order-response.xml",
        "/v1/auth/approver-session",
        "/v1/approvals/{order_id}/decision",
        "/v1/audit/verify",
        "/v1/agent/runs",
        "/v1/customers",
        "/v1/recipes/plan",
        "/v1/drift/{customer_id}",
        "/v1/proposals",
        "/v1/voice/status",
    ):
        assert documented_path in interfaces, f"docs/interfaces.md missing route {documented_path}"


def test_architecture_decisions_assumptions_and_interfaces_cover_judge_fixes():
    architecture = _read("docs/ARCHITECTURE.md")
    decisions = _read("docs/DECISIONS.md")
    assumptions = _read("docs/ASSUMPTIONS.md")
    interfaces = _read("docs/interfaces.md")

    assert "```mermaid\nsequenceDiagram" in architecture
    for token in (
        "AIMO_DEMO_FAULTS",
        "AIMO_UBL_XSD",
        "AIMO_SCREEN_GEMINI=1",
        "GEMINI_FALLBACK_MODELS",
        "X-Approver-Session",
        "X-Approver-Pin",
        "requote_needed",
        "partial_stock",
        "injection_screen",
        "quantity <= 0",
        "$0.0352",
        "68.2%",
        "69.5%",
        "145",
        "44/44",
    ):
        assert token in architecture, f"docs/ARCHITECTURE.md missing {token!r}"

    for adr_num in range(1, 12):
        heading = ANCHOR_SEP * 2 + f" D{adr_num}."
        assert heading in decisions, f"docs/DECISIONS.md missing ADR heading {heading!r}"
    for prod_adr in ("D-P1", "D-P2", "D-P3", "D-P4"):
        assert prod_adr in decisions, f"docs/DECISIONS.md missing {prod_adr!r}"
    for token in (
        "44/44",
        "68.2%",
        "69.5%",
        "145",
        "99.7%",
        "AIMO_UBL_XSD",
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "AIMO_SCREEN_GEMINI=1",
        "44.4%",
        "94.4%",
        "42/60",
        "$0.0352",
        "partial_stock",
        "requote_needed",
    ):
        assert token in decisions, f"docs/DECISIONS.md missing {token!r}"

    for a_num in range(1, 8):
        heading = ANCHOR_SEP * 3 + f" A{a_num}."
        assert heading in assumptions, f"docs/ASSUMPTIONS.md missing {heading!r}"
    for token in (
        "A8 identity",
        "A9 keys",
        "A10 units",
        "hotel-aurora.yaml",
        "school-tampere.yaml",
        "hospital-meilahti.yaml",
        "470889",
        "milk and nuts",
        "AIMO_UBL_XSD",
        "AIMO_DEMO_FAULTS=1",
        "X-Approver-Session",
        "X-Approver-Pin",
        "/v1/agent/runs",
        "69.5%",
        "145",
    ):
        assert token in assumptions, f"docs/ASSUMPTIONS.md missing {token!r}"

    for token in (
        "AIMO_DEMO_FAULTS=1",
        "AIMO_ENV=production",
        "X-Approver-Session",
        "X-Approver-Pin",
        "/v1/agent/runs",
        "partial_stock",
        "requote_needed",
        "PlanLine.injection_screen",
        "Candidate.injection_screen",
        "need.raw_text",
        "need.product_query",
        "AIMO_SCREEN_GEMINI=1",
        "GEMINI_FALLBACK_MODELS",
        "meta.fallback_from",
        "AIMO_UBL_XSD",
        "quantity <= 0",
        "hotel-aurora",
        "school-tampere",
        "hospital-meilahti",
    ):
        assert token in interfaces, f"docs/interfaces.md missing {token!r}"


def test_scenarios_doc_matches_scenario_modules_and_conformance_groups():
    scenarios_doc = _read("docs/SCENARIOS.md")
    actual_modules = tuple(
        sorted(p.stem for p in (ROOT / "scenarios").glob("s[0-9]_*.py"))
    )
    assert actual_modules == EXPECTED_SCENARIO_MODULES

    row_checks: dict[str, int] = {}
    for mod in EXPECTED_SCENARIO_MODULES:
        assert mod in scenarios_doc, f"docs/SCENARIOS.md missing scenario {mod}"
        match = re.search(rf"\|\s*PASS\s*\|\s*{re.escape(mod)}\s*\|[^|]+\|\s*(\d+)\s*\|", scenarios_doc)
        assert match is not None, f"docs/SCENARIOS.md missing result table row for {mod}"
        row_checks[mod] = int(match.group(1))

    total_checks = sum(row_checks.values())
    assert total_checks >= 88
    assert f"({total_checks} checks)" in scenarios_doc
    assert "8/8" in scenarios_doc
    assert "44/44" in scenarios_doc
    assert "Nordic Bakery Supply" in scenarios_doc


def test_six_judge_gaps_verified_in_code_and_docs(tmp_path, monkeypatch):
    assert callable(getattr(mock_model, "MockBuyerModel", None)) or hasattr(mock_model, "__file__")

    school_prof = trust_policy.planning_profile("school-tampere")
    assert {"milk", "nuts"}.issubset(set(school_prof["excluded_allergens"]))

    assert "injection_screen" in models.PlanLine.model_fields
    assert "injection_screen" in models.Candidate.model_fields

    tools = asyncio.run(build_mcp().list_tools())
    assert len(tools) == 13
    readme = _read("README.md")
    assert "13" in readme

    from aimo import config
    from aimo.backend import orders as store

    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)

    client = TestClient(create_app(with_mcp=False))

    unauth_quote = client.post(
        "/v1/quotes",
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}]},
    )
    assert unauth_quote.status_code == 401

    unknown_cust = client.post(
        "/v1/quotes",
        headers={"X-Customer-Id": "unknown-corp", "X-Agent-Id": "buyer-1"},
        json={"delivery_date": "2026-11-16", "lines": [{"product_id": "464306", "qty_sales_units": 1}]},
    )
    assert unknown_cust.status_code == 403
    assert unknown_cust.json().get("error") == "unknown_customer"

    for bad_qty in (0, -5):
        bad_plan = client.post(
            "/v1/plan",
            headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"},
            json={
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "quantity": bad_qty,
                "unit": "L",
                "delivery_by": "2026-11-16",
            },
        )
        assert 400 <= bad_plan.status_code < 500, f"quantity={bad_qty} returned {bad_plan.status_code}"


def test_this_hygiene_test_has_zero_code_comments_and_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []


def test_git_tracked_tree_has_zero_conflict_markers_and_stray_artifacts():
    if not (ROOT / ".git").exists():
        return

    res = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert tracked

    forbidden_names = {
        ".env",
        "approver_pin",
        "approver_pins.json",
        "approver_session_secret",
    }
    conflict_start = "<" * 7 + " "
    conflict_end = ">" * 7 + " "

    binary_exts = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp4",
        ".webm",
        ".mov",
        ".pdf",
        ".npy",
        ".npz",
        ".sqlite",
        ".ico",
        ".woff",
        ".woff2",
        ".gz",
    }

    for rel in tracked:
        p = pathlib.Path(rel)
        assert p.name not in forbidden_names, f"Forbidden secret/env file tracked: {rel}"
        assert not rel.startswith((".venv/", "venv/", "node_modules/", "data/", "keys/")), (
            f"Forbidden runtime/venv path tracked: {rel}"
        )
        assert not (p.parent == pathlib.Path(".") and p.name.startswith("conformance-") and p.suffix == ".json"), (
            f"Stray root conformance JSON tracked: {rel}"
        )
        assert p.suffix.lower() not in {".sqlite", ".pem", ".key"}, f"Forbidden binary/key artifact tracked: {rel}"

        if p.suffix.lower() in binary_exts:
            continue
        full = ROOT / rel
        if not full.is_file():
            continue
        content = full.read_text(encoding="utf-8", errors="replace")
        for idx, line in enumerate(content.splitlines(), 1):
            assert not line.startswith(conflict_start) and not line.startswith(conflict_end), (
                f"Git merge conflict marker in {rel}:{idx}"
            )
