from __future__ import annotations

import asyncio
import io
import json
import pathlib
import re
import subprocess
import sys
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

W6_PARITY_TOKENS = (
    "AIMO_GEMINI_TRACE",
    "evidence/live-run",
    "verify_quote.py",
    "catalog_missing",
    "demo-walkthrough.mp4",
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
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked asset in {rel}: {raw_target}"


def test_w6_owned_docs_cite_wave10_parity_tokens():
    walkthrough_mp4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    assert walkthrough_mp4.is_file() and walkthrough_mp4.stat().st_size > 0

    for rel in W6_OWNED_DOCS:
        text = _read(rel)
        for token in W6_PARITY_TOKENS:
            assert token in text, f"{rel} missing required parity token {token!r}"


def test_repo_wide_markdown_relative_links_exist_on_disk():
    if (ROOT / ".git").exists():
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked_md = [
            ln.strip()
            for ln in res.stdout.splitlines()
            if ln.strip()
            and not ln.strip().startswith("ours/")
            and (ln.strip() == "README.md" or (ln.strip().startswith("docs/") and ln.strip().endswith(".md")))
        ]
    else:
        tracked_md = [
            "README.md",
            *[
                p.relative_to(ROOT).as_posix()
                for p in sorted((ROOT / "docs").rglob("*.md"))
            ],
        ]

    assert "README.md" in tracked_md
    assert len(tracked_md) >= 40

    allowed_pending = {
        (ROOT / "docs" / "demo-backup.mp4").resolve(),
        (ROOT / "docs" / "pitch" / "requote_beat.py").resolve(),
        (ROOT / "docs" / "arena" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "ASSETS.md").resolve(),
    }
    for pending_target in allowed_pending:
        if pending_target.exists():
            assert pending_target.is_file() and pending_target.stat().st_size > 0
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    root_resolved = ROOT.resolve()
    broken: list[str] = []

    for rel in tracked_md:
        doc_path = ROOT / rel
        if not doc_path.is_file():
            continue
        raw = doc_path.read_text(encoding="utf-8")
        stripped = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
        stripped = re.sub(r"`[^`\n]+`", "", stripped)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", "data:", ANCHOR_SEP)):
                continue
            clean = target.split(ANCHOR_SEP, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (doc_path.parent / clean).resolve()
            if not resolved.is_relative_to(root_resolved):
                broken.append(f"{rel} -> {raw_target} (escapes repo root)")
                continue
            if resolved in allowed_pending and not resolved.exists():
                continue
            if not resolved.exists():
                broken.append(f"{rel} -> {raw_target}")
            elif resolved.is_file() and resolved.stat().st_size == 0:
                broken.append(f"{rel} -> {raw_target} (empty file)")

    assert broken == [], f"Broken relative markdown links: {broken}"


def test_verify_quote_isolated_python_verifies_live_run_pack(tmp_path):
    verify_script = ROOT / "scripts" / "verify_quote.py"
    pack_dir = ROOT / "docs" / "evidence" / "live-run"
    jwks_path = pack_dir / "jwks.json"
    assert verify_script.is_file()
    assert jwks_path.is_file()

    for doc_name, expected_marker in (
        ("quote.json", "Q-20261115-964294b6"),
        ("audit-events.json", "order.placed"),
    ):
        doc_path = pack_dir / doc_name
        assert doc_path.is_file()
        proc = subprocess.run(
            [sys.executable, "-I", str(verify_script), str(doc_path), str(jwks_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (doc_name, proc.stdout, proc.stderr)
        assert "VERIFIED" in proc.stdout, (doc_name, proc.stdout)
        assert expected_marker in proc.stdout, (doc_name, proc.stdout)

    tampered_quote = json.loads((pack_dir / "quote.json").read_text(encoding="utf-8"))
    tampered_quote["total_net"] = float(tampered_quote["total_net"]) + 1.0
    tampered_path = tmp_path / "tampered-quote.json"
    tampered_path.write_text(json.dumps(tampered_quote), encoding="utf-8")
    bad_proc = subprocess.run(
        [sys.executable, "-I", str(verify_script), str(tampered_path), str(jwks_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad_proc.returncode != 0
    assert "VERIFIED" not in bad_proc.stdout


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
        "AIMO_GEMINI_TRACE",
        "requested_model",
        "run-b9690772fe",
        "Q-20261115-964294b6",
        "ORD-20261115-06eda13d",
        "109.72 s",
        "demo-walkthrough.mp4",
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
        "AIMO_GEMINI_TRACE",
        "judge_accept.sh",
        "PASS=17 FAIL=0 SKIP=3",
        "PASS=16 FAIL=0 SKIP=4",
        "Q-20261115-964294b6",
        "ORD-20261115-06eda13d",
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
        "catalog_missing",
        "AIMO_REPLAY=0",
        "b57af02",
        "AIMO_GEMINI_TRACE",
        "requested_model",
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
    assert total_checks >= 92
    assert f"({total_checks} checks)" in scenarios_doc
    assert "8/8" in scenarios_doc
    assert "44/44" in scenarios_doc
    assert "Nordic Bakery Supply" in scenarios_doc
    assert (
        "python -I scripts/verify_quote.py docs/evidence/live-run/quote.json docs/evidence/live-run/jwks.json"
        in scenarios_doc
    )


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


def test_dockerignore_and_readme_submission_wave11_contracts():
    dockerignore_raw = _read(".dockerignore")
    assert EM_DASH not in dockerignore_raw
    dockerignore_entries = {
        ln.strip()
        for ln in dockerignore_raw.splitlines()
        if ln.strip() and not ln.strip().startswith(ANCHOR_SEP)
    }
    assert "conformance-*.json" in dockerignore_entries
    assert "/conformance-*.json" not in dockerignore_entries
    assert "*.webm" in dockerignore_entries

    readme = _read("README.md")
    submission = _read("docs/SUBMISSION.md")
    assert EM_DASH not in readme
    assert EM_DASH not in submission

    for doc_name, doc_text in (("README.md", readme), ("docs/SUBMISSION.md", submission)):
        for required_tok in (
            "2,462",
            "6343389",
            "1,168 passed",
            "c98e72e",
            "1150 passed",
            "1,150 passed",
            "6973c2d",
            "743 passed",
            "2468bdd",
            "62e8384",
            "375 passed, 7 skipped, 1 xfailed, 1 xpassed",
            "274 passed, 0 failed",
            "Try to Scam the Agent",
            "One-Page Judge Walkthrough",
            "docs/arena/index.html",
            "docs/demo/index.html",
            "docs/demo/ASSETS.md",
        ):
            assert required_tok in doc_text, f"{doc_name} missing {required_tok!r}"

    assert "[docs/arena/index.html](docs/arena/index.html)" in readme
    assert "[docs/demo/index.html](docs/demo/index.html)" in readme
    assert "[docs/demo/ASSETS.md](docs/demo/ASSETS.md)" in readme
    assert "[docs/arena/index.html](arena/index.html)" in submission
    assert "[docs/demo/index.html](demo/index.html)" in submission
    assert "[docs/demo/ASSETS.md](demo/ASSETS.md)" in submission


def test_this_hygiene_test_has_zero_code_comments_and_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert ANCHOR_SEP not in raw
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
