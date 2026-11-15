from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import subprocess

from aimo.gateway.mcp_server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
PITCH_DOCS = ("docs/pitch/QA.md", "docs/pitch/deck.md")
OWNED_DOCS = (
    "docs/RUN.md",
    "docs/CONNECT.md",
    "docs/GEMINI.md",
    "docs/pitch/QA.md",
    "docs/pitch/deck.md",
)


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
    live_run_readme = (ROOT / "docs" / "evidence" / "live-run" / "README.md").resolve()
    assert live_run_readme.is_file()

    for rel in OWNED_DOCS:
        text = _read_doc(rel)
        assert em_dash not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO marker found in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder found in {rel}"

        doc_path = ROOT / rel
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        resolved_targets: set[pathlib.Path] = set()
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", anchor_sep)):
                continue
            rel_target = target.split(anchor_sep, 1)[0].split("?", 1)[0]
            if not rel_target:
                continue
            resolved = (doc_path.parent / rel_target).resolve()
            resolved_targets.add(resolved)
            if resolved in allowed_pending and not resolved.exists():
                continue
            assert resolved.exists(), f"Broken link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked file in {rel}: {raw_target}"
        assert live_run_readme in resolved_targets, (
            f"{rel} must link to docs/evidence/live-run/README.md via a valid relative link"
        )


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


def test_run_exception_matrix_has_all_11_rows_and_commands() -> None:
    run_doc = _read_doc("docs/RUN.md")
    h2 = chr(0x23) * 2 + " Exceptions: what happens, and how to see it"
    h3 = chr(0x23) * 3 + " Approving as a human"
    assert h2 in run_doc
    assert h3 in run_doc

    matrix_sec = run_doc.split(h2, 1)[1].split(h3, 1)[0]
    table_rows: list[list[str]] = []
    for line in matrix_sec.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells[0] == "Exception" or set(cells[0]) <= {"-"}:
            continue
        table_rows.append(cells)

    expected_exceptions = [
        "**Partial availability**",
        "**Substitutions**",
        "**Delivery constraints: cut-off**",
        "**Delivery constraints: late delivery**",
        "**Allergen safety**",
        "**Price change**",
        "**Stock changing during approval**",
        "**Cut-off passing during approval**",
        "**LLM quota exhausted**",
        "**Prompt injection**",
        "**Agent trying to move the clock or fake stock**",
    ]
    assert [r[0] for r in table_rows] == expected_exceptions
    for row in table_rows:
        assert len(row) == 4
        assert all(cell for cell in row)
        assert row[2].startswith("`uv run ") and row[2].endswith("`")

    for required in (
        "tests/test_overrides.py",
        "POST /v1/agent/runs",
        "4ccc1fb",
        "GEMINI_FALLBACK_MODELS",
        "AIMO_SCREEN_GEMINI=1",
        "AIMO_DEMO_FAULTS=1",
        "401",
    ):
        assert required in matrix_sec, f"RUN.md exception matrix missing {required!r}"


def test_qa_has_23_numbered_questions_including_q21_q22_q23() -> None:
    qa = _read_doc("docs/pitch/QA.md")
    h1 = chr(0x23) + " Judge Q&A: 23 hard questions\n"
    assert qa.startswith(h1)

    matches = list(re.finditer(r"^\*\*(\d+)\.\s+(.+?)\*\*$", qa, flags=re.MULTILINE))
    assert [int(m.group(1)) for m in matches] == list(range(1, 24))

    sections: dict[int, str] = {}
    for idx, match in enumerate(matches):
        qnum = int(match.group(1))
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(qa)
        sections[qnum] = qa[start:end]

    q10 = sections[10]
    for tok in (
        "AIMO_GEMINI_TRACE",
        "live-run/README.md",
        "summarize.py",
        "off by default",
        "never",
    ):
        assert tok in q10, f"QA Q10 missing {tok!r}"

    q12 = sections[12]
    for tok in (
        "0 of 34,159",
        "44.4%",
        "36-payload",
        "AIMO_SCREEN_GEMINI=1",
        "94.4%",
        "42 of 60",
        "80 of 80",
    ):
        assert tok in q12, f"QA Q12 missing {tok!r}"

    q15 = sections[15]
    for tok in (
        "70%",
        "69.5%",
        "99.7%",
        "live-run/README.md",
        "windtunnel/README.md",
    ):
        assert tok in q15, f"QA Q15 missing {tok!r}"

    q21 = sections[21]
    assert "(69.5% precision)" in q21
    for tok in (
        "68.2%",
        "69.5%",
        "stop when unsure",
        "0 policy and 0 allergen violations",
        "99.7%",
        "145",
        "price jump",
        "aimo/trust/learned.py",
    ):
        assert tok in q21, f"QA Q21 missing {tok!r}"

    q22 = sections[22]
    for tok in (
        "X-Aimo-Now",
        "X-Aimo-Faults",
        "AIMO_DEMO_FAULTS=1",
        "POST /v1/agent/runs",
        "4ccc1fb",
        "X-Approver-Session",
        "X-Approver-Pin",
        "401",
        "tests/test_overrides.py",
    ):
        assert tok in q22, f"QA Q22 missing {tok!r}"

    q23 = sections[23]
    for tok in (
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "30 minutes",
        "meta.fallback_from",
        "AIMO_GEMINI_TRACE",
        "requested_model",
        "live-run/README.md",
        "summarize.py",
        "29 s",
        "05:00",
    ):
        assert tok in q23, f"QA Q23 missing {tok!r}"


def test_gemini_and_deck_screen_opt_in_fallback_models_and_walkthrough_video() -> None:
    gemini = _read_doc("docs/GEMINI.md")
    deck = _read_doc("docs/pitch/deck.md")

    for tok in (
        "AIMO_SCREEN_GEMINI=1",
        "opt-in, off by default",
        "44.4%",
        "94.4%",
        "42 of 60",
        "- **Fallback models in the same API.**",
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "meta.fallback_from",
        "29 s",
    ):
        assert tok in gemini, f"docs/GEMINI.md missing {tok!r}"

    for tok in (
        "AIMO_SCREEN_GEMINI=1",
        "44.4%",
        "94.4%",
        "69.5%",
        "`docs/pitch/demo-walkthrough.mp4`",
        "docs/pitch/build_walkthrough.sh",
        "110 s",
        "f3c5996",
        "GEMINI_FALLBACK_MODELS",
    ):
        assert tok in deck, f"docs/pitch/deck.md missing {tok!r}"

    assert "](docs/pitch/demo-walkthrough.mp4)" not in deck
    assert "](demo-walkthrough.mp4)" not in deck

    mp4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    assert mp4.is_file(), "Missing docs/pitch/demo-walkthrough.mp4"
    size = mp4.stat().st_size
    assert 1_000_000 < size < 10_000_000, f"Unexpected walkthrough size: {size}"
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(mp4)],
            capture_output=True,
            text=True,
            check=True,
        )
        duration = float((json.loads(probe.stdout).get("format") or {}).get("duration") or 0.0)
        assert round(duration) in {110, 111}, f"Expected ~110s or ~111s walkthrough duration, got {duration}"


def test_sign_in_before_hosted_agent_runs_across_run_connect_and_qa() -> None:
    run_doc = _read_doc("docs/RUN.md")
    connect = _read_doc("docs/CONNECT.md")
    qa = _read_doc("docs/pitch/QA.md")

    h3 = chr(0x23) * 3 + " Approving as a human"
    h2_repo = chr(0x23) * 2 + " Repo map"
    human_sec = run_doc.split(h3, 1)[1].split(h2_repo, 1)[0]
    for tok in (
        "Run agent",
        "sign-in panel",
        "POST /v1/agent/runs",
        "X-Approver-Session",
        "X-Approver-Pin",
        "401",
        "inproc",
        "fake",
        "AIMO_DEMO_FAULTS=1",
    ):
        assert tok in human_sec, f"docs/RUN.md Approving as a human missing {tok!r}"

    for tok in (
        "/v1/agent/runs",
        "X-Approver-Session",
        "X-Approver-Pin",
        "401",
        "/mcp",
        "/v1",
        "X-Customer-Id",
        "X-Agent-Id",
    ):
        assert tok in connect, f"docs/CONNECT.md missing {tok!r}"

    json_blocks = re.findall(r"```json\s*\n(.*?)```", connect, flags=re.DOTALL)
    assert len(json_blocks) == 3
    assert "Run agent" in qa and "sign-in panel" in qa


def test_live_run_evidence_cited_across_owned_docs() -> None:
    live_dir = ROOT / "docs" / "evidence" / "live-run"
    run = json.loads((live_dir / "run.json").read_text(encoding="utf-8"))
    quote = json.loads((live_dir / "quote.json").read_text(encoding="utf-8"))
    order = json.loads((live_dir / "order.json").read_text(encoding="utf-8"))
    trace_rows = [
        json.loads(line)
        for line in (live_dir / "gemini-trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(trace_rows) == 8
    assert run["run_id"] == "run-b9690772fe"
    assert quote["quote_id"] == "Q-20261115-964294b6"
    assert order["order_id"] == "ORD-20261115-06eda13d"

    for rel in ("docs/GEMINI.md", "docs/RUN.md", "docs/pitch/QA.md", "docs/pitch/deck.md"):
        text = _read_doc(rel)
        for tok in (
            "live-run/README.md",
            "summarize.py",
            "AIMO_GEMINI_TRACE",
            run["run_id"],
            quote["quote_id"],
            order["order_id"],
            str(run["latency_s"]),
            "8 Gemini calls",
        ):
            assert tok in text, f"{rel} missing live-run evidence citation {tok!r}"

    connect = _read_doc("docs/CONNECT.md")
    for tok in ("live-run/README.md", "summarize.py", "AIMO_GEMINI_TRACE", "verify_quote.py"):
        assert tok in connect, f"docs/CONNECT.md missing live-run citation {tok!r}"


