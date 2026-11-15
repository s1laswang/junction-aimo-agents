from __future__ import annotations

import asyncio
import json
import pathlib
import re

from aimo.gateway.mcp_server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
PITCH_DOCS = ("docs/pitch/QA.md", "docs/pitch/deck.md")


def _read_doc(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing doc: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty doc: {rel_path}"
    return text


def test_self_has_zero_comments_and_zero_em_dashes() -> None:
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_qa_and_deck_have_zero_em_dashes_zero_todos_and_valid_links() -> None:
    em_dash = chr(0x2014)
    todo_re = re.compile(r"\bTODO\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    anchor_sep = chr(0x23)
    allowed_pending = {
        (ROOT / "docs" / "demo-backup.mp4").resolve(),
        (ROOT / "docs" / "pitch" / "requote_beat.py").resolve(),
    }

    for rel in PITCH_DOCS:
        text = _read_doc(rel)
        assert em_dash not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO marker found in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder found in {rel}"

        doc_path = ROOT / rel
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", anchor_sep)):
                continue
            rel_target = target.split(anchor_sep, 1)[0].split("?", 1)[0]
            if not rel_target:
                continue
            resolved = (doc_path.parent / rel_target).resolve()
            if resolved in allowed_pending and not resolved.exists():
                continue
            assert resolved.exists(), f"Broken link in {rel}: {raw_target}"


def test_qa_and_deck_cite_13_mcp_tools_and_never_claim_9_total() -> None:
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []

    stale_patterns = (
        re.compile(r"MCP\s+server[^\n]*\b(?:9|10|11|12|14)\s+tools\b", re.IGNORECASE),
        re.compile(r"server\s+advertises\s+(?:9|10|11|12|14)\s+tools\b", re.IGNORECASE),
        re.compile(r"MCP\s+tools\s*\(\s*9\b", re.IGNORECASE),
        re.compile(r"\b9\s+MCP\s+tools\b", re.IGNORECASE),
        re.compile(r"\(\s*9\s+tools\b", re.IGNORECASE),
    )

    for rel in PITCH_DOCS:
        text = _read_doc(rel)
        assert re.search(r"\b13\s+(?:MCP\s+)?tools\b", text), f"{rel} must cite 13 MCP tools"
        for pat in stale_patterns:
            assert not pat.search(text), f"{rel} contains stale MCP tool count matching {pat.pattern!r}"


def test_qa_and_deck_windtunnel_injection_and_precision_contracts() -> None:
    qa = _read_doc("docs/pitch/QA.md")
    deck = _read_doc("docs/pitch/deck.md")

    for rel, text in (("docs/pitch/QA.md", qa), ("docs/pitch/deck.md", deck)):
        assert "80 of 80" in text, f"{rel} missing '80 of 80' injection block"
        assert "0 of 80" in text, f"{rel} missing '0 of 80' naive injection block"
        assert "1 false positive in 560 clean requests" in text, f"{rel} missing clean-request FP rate"
        assert "product_query" in text, f"{rel} missing product_query screening note"
        assert "372" in text, f"{rel} missing 372 naive policy violations"
        assert "193" in text, f"{rel} missing 193 naive allergen violations"
        assert "68.2%" in text, f"{rel} missing 68.2% precision caveat"
        assert "99.7%" in text, f"{rel} missing 99.7% recall"
        assert "90% target is not met" in text, f"{rel} missing honest 90% target caveat"
        assert "6 planted texts" in text or "6 texts flagged and redacted" in text

    assert "0 policy and 0 allergen violations" in qa
    assert "| Policy violations | **0** | 372 |" in deck
    assert "| Allergen violations | **0** | 193 |" in deck
    assert "| Injection payloads blocked | 80 of 80 | 0 of 80 |" in deck
    assert "56.2%" in deck
    assert "2,560" in deck and "11" in deck and "1,452" in deck and "728" in deck
    assert "`docs/evidence/windtunnel/README.md`" in deck


def test_qa_and_deck_requote_demo_faults_per_approver_conformance_and_montage() -> None:
    for report_rel in (
        "docs/openness/conformance-valio-aimo.json",
        "docs/openness/conformance-nordic-bakery.json",
    ):
        payload = json.loads(_read_doc(report_rel))
        assert payload["summary"] == {"pass": 44, "fail": 0, "skip": 0, "total": 44}

    qa = _read_doc("docs/pitch/QA.md")
    deck = _read_doc("docs/pitch/deck.md")

    for rel, text in (("docs/pitch/QA.md", qa), ("docs/pitch/deck.md", deck)):
        assert "409" in text and "requote_needed" in text, f"{rel} missing 409 requote_needed"
        assert "AIMO_DEMO_FAULTS=1" in text, f"{rel} missing AIMO_DEMO_FAULTS=1"
        assert "X-Aimo-Faults" in text and "X-Aimo-Now" in text, f"{rel} missing operator-only headers"
        assert "data/keys/approver_pins.json" in text, f"{rel} missing per-approver credentials path"
        assert "44/44" in text, f"{rel} missing 44/44 conformance"
        assert "$0.0352" in text and "$0.0339" in text and "$0.0822" in text, f"{rel} missing montage costs"
        assert "5 of 34" in text and "38%" in text, f"{rel} missing Sunday montage step/cache stats"

    assert "partial_stock" in qa
    assert "never a silently partial order" in qa
    assert "approver_locked" in qa
    assert "approver_mismatch" in qa
