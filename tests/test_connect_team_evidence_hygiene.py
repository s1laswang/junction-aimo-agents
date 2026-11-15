from __future__ import annotations

import asyncio
import io
import json
import pathlib
import re
import tokenize

from aimo.gateway.mcp_server import build_mcp
from conformance import profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HASH_CHAR = chr(35)

WAVE13_DOCS = (
    "docs/CONNECT.md",
    "docs/TEAM.md",
    "docs/evidence/README.md",
    "docs/evidence/live-run/README.md",
)

REQUIRED_SURFACE_TOKENS = (
    "Try to Scam the Agent",
    "One-Page Judge Walkthrough",
    "docs/arena/index.html",
    "docs/demo/index.html",
    "docs/demo/ASSETS.md",
    "CC0-1.0",
    "Kenney",
    "manifest.json",
)

EXTENSION_MCP_TOOLS = {
    "list_recipes",
    "plan_recipe",
    "check_standing_order_drift",
    "propose_offer",
}


def _read_doc(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing doc: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty doc: {rel_path}"
    return text


def _extract_resolved_links(rel_path: str) -> list[tuple[str, pathlib.Path]]:
    doc_path = ROOT / rel_path
    text = _read_doc(rel_path)
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", "", stripped)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    out: list[tuple[str, pathlib.Path]] = []
    for raw_target in link_re.findall(stripped):
        target = raw_target.strip().split()[0]
        if target.startswith(("http://", "https://", "mailto:", "data:", HASH_CHAR)):
            continue
        clean = target.split(HASH_CHAR, 1)[0].split("?", 1)[0]
        if not clean:
            continue
        out.append((raw_target, (doc_path.parent / clean).resolve()))
    return out


def test_wave13_docs_exist_and_have_zero_em_dashes_todos_or_conflicts():
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    conflict_start = "<" * 7 + " "
    conflict_mid = "=" * 7
    conflict_end = ">" * 7 + " "

    for rel in WAVE13_DOCS:
        text = _read_doc(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO/FIXME marker in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder in {rel}"
        for idx, line in enumerate(text.splitlines(), 1):
            assert not line.startswith(conflict_start), f"Conflict start marker in {rel}:{idx}"
            assert line.strip() != conflict_mid, f"Conflict separator marker in {rel}:{idx}"
            assert not line.startswith(conflict_end), f"Conflict end marker in {rel}:{idx}"


def test_wave13_docs_crosslink_arena_walkthrough_and_cc0_assets_ledger():
    arena_dir = (ROOT / "docs" / "arena").resolve()
    demo_dir = (ROOT / "docs" / "demo").resolve()
    assets_md = (ROOT / "docs" / "demo" / "ASSETS.md").resolve()
    manifest_json = (ROOT / "docs" / "arena" / "assets" / "manifest.json").resolve()

    assert arena_dir.is_dir()
    assert demo_dir.is_dir()
    assert assets_md.is_file() and assets_md.stat().st_size > 0
    assert manifest_json.is_file() and manifest_json.stat().st_size > 0

    manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
    assert manifest.get("license") == "CC0-1.0"
    assert manifest.get("author") == "Kenney Vleugels (Kenney.nl)"
    packs = manifest.get("packs") or {}
    assert len(packs) == 4
    for pack_meta in packs.values():
        assert pack_meta.get("license") == "CC0-1.0"
        assert pack_meta.get("author") == "Kenney Vleugels (Kenney.nl)"

    arena_targets = {arena_dir, (arena_dir / "index.html").resolve()}
    demo_targets = {demo_dir, (demo_dir / "index.html").resolve()}

    for rel in WAVE13_DOCS:
        text = _read_doc(rel)
        for token in REQUIRED_SURFACE_TOKENS:
            assert token in text, f"{rel} missing required cross-link token {token!r}"
        resolved_set = {resolved for _, resolved in _extract_resolved_links(rel)}
        assert resolved_set & arena_targets, f"{rel} missing relative link to docs/arena"
        assert resolved_set & demo_targets, f"{rel} missing relative link to docs/demo"
        assert assets_md in resolved_set, f"{rel} missing relative link to docs/demo/ASSETS.md"
        assert manifest_json in resolved_set, (
            f"{rel} missing relative link to docs/arena/assets/manifest.json"
        )

    team_text = _read_doc("docs/TEAM.md")
    fr8_heading = HASH_CHAR * 2 + " FR8 fleet collaboration"
    assert fr8_heading in team_text
    fr8_sec = team_text.split(fr8_heading, 1)[1]
    docs_rows = [
        ln for ln in fr8_sec.splitlines() if ln.strip().startswith("| `docs` | `fr8/valio/docs` |")
    ]
    assert len(docs_rows) == 1
    docs_row = docs_rows[0]
    for token in (
        "13-tool MCP",
        "docs/NUMBERS.md",
        "Try to Scam the Agent",
        "(arena/)",
        "`docs/arena/index.html`",
        "CC0-1.0",
        "Kenney",
        "(demo/ASSETS.md)",
        "(arena/assets/manifest.json)",
        "One-Page Judge Walkthrough",
        "(demo/)",
        "`docs/demo/index.html`",
    ):
        assert token in docs_row, f"docs/TEAM.md FR8 docs row missing {token!r}"


def test_connect_and_team_affirm_13_mcp_tools_and_zero_approval_tools():
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    assert len(set(tool_names)) == 13
    assert set(profile.MCP_TOOLS).issubset(set(tool_names))
    assert set(tool_names) - set(profile.MCP_TOOLS) == EXTENSION_MCP_TOOLS
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []

    connect = _read_doc("docs/CONNECT.md")
    for name in tool_names:
        assert f"`{name}`" in connect, f"MCP tool {name} missing from docs/CONNECT.md"
    assert "MCP tools (13" in connect
    assert "9 core `otap/0.1`" in connect
    assert "0 approval tools" in connect
    assert "no approval tool" in connect.lower()
    assert not re.search(r"\b(?:9|10|11|12|14)\s+tools\b", connect)

    json_blocks = re.findall(r"```json\s*\n(.*?)```", connect, flags=re.DOTALL)
    assert len(json_blocks) == 3
    for raw_block in json_blocks:
        parsed = json.loads(raw_block)
        assert "valio-aimo" in parsed.get("mcpServers", {})

    team = _read_doc("docs/TEAM.md")
    assert team.count("13 tools") >= 2
    assert "13-tool MCP" in team
    assert team.lower().count("no approval tool") >= 2
    assert not re.search(r"\b(?:9|10|11|12|14)\s+tools\b", team)
    for squad in ("inputs", "buyer", "allergen", "auth", "docs"):
        assert f"| `{squad}` | `fr8/valio/{squad}` |" in team


def test_wave13_docs_all_relative_links_strictly_resolve_without_pending_exemptions():
    root_resolved = ROOT.resolve()
    expected_raw_links = {
        "docs/CONNECT.md": {
            "../README.md",
            "evidence/live-run/README.md",
            "arena/",
            "demo/ASSETS.md",
            "arena/assets/manifest.json",
            "demo/",
            "interfaces.md",
        },
        "docs/TEAM.md": {
            "arena/",
            "demo/ASSETS.md",
            "arena/assets/manifest.json",
            "demo/",
        },
        "docs/evidence/README.md": {
            "../GEMINI.md",
            "../SUBMISSION.md",
            "../demo-montage.md",
            "../reports/live.md",
            "windtunnel/README.md",
            "live-run/README.md",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
            "../demo/",
        },
        "docs/evidence/live-run/README.md": {
            "../../arena/",
            "../../demo/ASSETS.md",
            "../../arena/assets/manifest.json",
            "../../demo/",
            "../README.md",
        },
    }

    for rel in WAVE13_DOCS:
        links = _extract_resolved_links(rel)
        assert links, f"Expected markdown relative links in {rel}"
        found_raw = {raw.strip().split()[0] for raw, _ in links}
        assert expected_raw_links[rel].issubset(found_raw), (
            f"{rel} missing expected relative links: {sorted(expected_raw_links[rel] - found_raw)}"
        )
        for raw_target, resolved in links:
            assert resolved.is_relative_to(root_resolved), (
                f"Relative link escapes repo root in {rel}: {raw_target}"
            )
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked file in {rel}: {raw_target}"


def test_evidence_readmes_preserve_offline_repro_commands_and_artifact_bindings():
    ev_readme = _read_doc("docs/evidence/README.md")
    for tok in (
        "uv run python docs/evidence/montage_stats.py",
        "montage-ledger-before-trim.jsonl",
        "montage-ledger-after-trim.jsonl",
        "montage-ledger-sun.jsonl",
        "55",
        "50",
        "53",
        "$0.0822",
        "$0.0339",
        "$0.0352",
        "225,503",
        "86,792",
        "38%",
        "15%",
    ):
        assert tok in ev_readme, f"docs/evidence/README.md missing {tok!r}"

    live_dir = ROOT / "docs" / "evidence" / "live-run"
    live_readme = _read_doc("docs/evidence/live-run/README.md")
    quote = json.loads((live_dir / "quote.json").read_text(encoding="utf-8"))
    order = json.loads((live_dir / "order.json").read_text(encoding="utf-8"))
    run = json.loads((live_dir / "run.json").read_text(encoding="utf-8"))

    for tok in (
        "scripts/verify_quote.py docs/evidence/live-run/quote.json docs/evidence/live-run/jwks.json",
        "scripts/verify_quote.py docs/evidence/live-run/audit-events.json docs/evidence/live-run/jwks.json",
        "python docs/evidence/live-run/summarize.py",
        "gemini-trace.jsonl",
        "transcript.md",
        "audit-report.md",
        quote["quote_id"],
        order["order_id"],
        run["run_id"],
    ):
        assert tok in live_readme, f"docs/evidence/live-run/README.md missing {tok!r}"


def test_self_zero_comments_zero_hash_and_zero_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert EN_DASH not in raw
    assert HASH_CHAR not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
