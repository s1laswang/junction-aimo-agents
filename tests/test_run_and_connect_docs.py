from __future__ import annotations

import asyncio
import json
import pathlib
import re

from aimo.gateway.mcp_server import build_mcp
from conformance import profile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read_doc(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing doc: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty doc: {rel_path}"
    return text


def test_run_and_connect_docs_exist_and_have_zero_em_dashes_or_todos():
    em_dash = chr(0x2014)
    todo_re = re.compile(r"\bTODO\b", re.IGNORECASE)
    for rel in ("docs/RUN.md", "docs/CONNECT.md", "docs/OPENNESS.md"):
        text = _read_doc(rel)
        assert em_dash not in text, f"Em dash found in {rel}"
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


def test_openness_has_production_path_and_pilot_kpis():
    openness = _read_doc("docs/OPENNESS.md")
    heading = chr(0x23) * 2 + " Production path"
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
    anchor_sep = chr(0x23)
    for rel in ("docs/RUN.md", "docs/CONNECT.md", "docs/OPENNESS.md"):
        doc = ROOT / rel
        stripped = re.sub(r"```.*?```", "", _read_doc(rel), flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", anchor_sep)):
                continue
            rel_target = target.split(anchor_sep, 1)[0].split("?", 1)[0]
            if not rel_target:
                continue
            resolved = (doc.parent / rel_target).resolve()
            assert resolved.exists(), f"Broken link in {rel}: {raw_target}"
