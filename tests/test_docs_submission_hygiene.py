from __future__ import annotations

import asyncio
import pathlib
import re
import subprocess
import sys

from aimo.gateway.mcp_server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]

CORE_SUBMISSION_DOCS = (
    "README.md",
    "docs/SUBMISSION.md",
    "docs/GEMINI.md",
    "docs/DEMO.md",
    "docs/PLAN.md",
    "docs/pitch/deck.md",
)

SCAN_SOURCE_DIRS = (
    "docs",
    "aimo",
    "scripts",
    "web",
    "tests",
    "scenarios",
    "conformance",
    "toywholesaler",
    "windtunnel",
)

BINARY_SUFFIXES = {
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


def _doc_files() -> list[pathlib.Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def _primary_pytest_passed(rel_path: str) -> int:
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    all_matches = re.findall(r"\b(\d+)\s+passed\b", text)
    assert all_matches, f"No 'NNN passed' pytest count found in {rel_path}"
    for line in text.splitlines():
        if "pytest" in line:
            m = re.search(r"\b(\d+)\s+passed\b", line)
            if m:
                primary = int(m.group(1))
                assert primary == int(all_matches[0]), (
                    f"First 'passed' count ({all_matches[0]}) disagrees with pytest line ({primary}) in {rel_path}"
                )
                return primary
    return int(all_matches[0])


def test_mcp_tool_count_names_and_no_approval_tool_in_readme():
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    approval_like = [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()]
    assert approval_like == []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\b13\s+tools\b", readme)
    assert re.search(r"MCP tools\s*\(\s*13\b", readme)
    assert not re.search(r"MCP server[^\n]*\b(?:9|10|11|12|14)\s+tools\b", readme)
    assert not re.search(r"server advertises\s+(?:9|10|11|12|14)\s+tools\b", readme)

    missing = [name for name in tool_names if f"`{name}`" not in readme]
    assert missing == [], f"MCP tool names missing from README.md: {missing}"

    roster_lines = [ln for ln in readme.splitlines() if "`search_products`" in ln and "`propose_offer`" in ln]
    assert roster_lines, "Expected MCP tool roster line in README.md"
    listed = re.findall(r"`([a-z_]+)`", roster_lines[0])
    assert len(listed) == 13
    assert set(listed) == set(tool_names)


def test_core_docs_have_zero_todos_and_freeze_placeholders():
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    todo_re = re.compile(r"\bTODO\b", re.IGNORECASE)
    for rel in CORE_SUBMISSION_DOCS:
        path = ROOT / rel
        assert path.is_file(), f"Missing required doc: {rel}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"Empty doc: {rel}"
        todos = todo_re.findall(text)
        assert todos == [], f"Unclosed TODO marker in {rel}: {todos}"
        freeze_hits = freeze_re.findall(text)
        assert freeze_hits == [], f"Unclosed freeze placeholder in {rel}: {freeze_hits}"


def test_submission_links_repo_make_accept_and_backup_video():
    submission = (ROOT / "docs" / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "https://github.com/s1laswang/junction-aimo-agents" in submission
    assert "make accept" in submission
    assert "demo-backup.mp4" in submission


def test_markdown_relative_links_resolve():
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    anchor_sep = "\x23"
    allowed_pending = {
        (ROOT / "docs" / "demo-backup.mp4").resolve(),
        (ROOT / "docs" / "pitch" / "requote_beat.py").resolve(),
        (ROOT / "docs" / "arena" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "ASSETS.md").resolve(),
    }
    broken: list[str] = []

    for doc in _doc_files():
        raw_text = doc.read_text(encoding="utf-8")
        stripped = re.sub(r"```.*?```", "", raw_text, flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", anchor_sep)):
                continue
            rel = target.split(anchor_sep, 1)[0].split("?", 1)[0]
            if not rel:
                continue
            resolved = (doc.parent / rel).resolve()
            if resolved in allowed_pending and not resolved.exists():
                continue
            if not resolved.exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {raw_target}")
            elif resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked asset: {resolved.relative_to(ROOT)}"

    assert broken == [], f"Broken relative markdown links: {broken}"


def test_pytest_pass_counts_match_across_readme_numbers_and_submission():
    targets = ("README.md", "docs/NUMBERS.md", "docs/SUBMISSION.md")
    counts = {rel: _primary_pytest_passed(rel) for rel in targets}
    unique = set(counts.values())
    assert len(unique) == 1, f"Primary pytest pass count mismatch across docs: {counts}"
    assert next(iter(unique)) >= 300


def test_zero_em_dashes_in_docs():
    em_dash = chr(0x2014)
    doc_files = _doc_files()
    rel_prefixes = {p.relative_to(ROOT).parts[:2] for p in doc_files if len(p.relative_to(ROOT).parts) >= 2}
    for sub in (("docs", "reports"), ("docs", "evidence"), ("docs", "peppol"), ("docs", "pitch")):
        assert sub in rel_prefixes, f"Expected {sub} markdown files in _doc_files()"
    offenders: list[str] = []
    for doc in doc_files:
        text = doc.read_text(encoding="utf-8")
        if em_dash in text:
            lines = [idx for idx, ln in enumerate(text.splitlines(), 1) if em_dash in ln]
            offenders.append(f"{doc.relative_to(ROOT)}:{lines}")
    assert offenders == [], f"Em dashes found in docs: {offenders}"


def test_no_leaked_api_keys_and_dotenv_gitignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    ignore_entries = {ln.strip().rstrip("/") for ln in gitignore.splitlines() if ln.strip()}
    assert ".env" in ignore_entries

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in env_example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("\x23") or "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        if any(tok in key for tok in ("KEY", "PASSWORD", "PIN", "SECRET", "TOKEN")):
            assert val.strip() == "", f"Non-empty secret placeholder in .env.example: {key}"

    candidates: set[pathlib.Path] = {ROOT / "README.md", ROOT / ".env.example"}
    for sub in SCAN_SOURCE_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() in BINARY_SUFFIXES:
                continue
            parts = p.relative_to(ROOT).parts
            if any(part in {"__pycache__", ".venv", ".pytest_cache", "data", "keys"} for part in parts):
                continue
            if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
                continue
            candidates.add(p)

    if (ROOT / ".git").exists():
        chk = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False)
        assert chk.returncode == 0, ".env must be ignored by git"
        tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
        assert ".env" not in tracked
        assert not any(f.endswith("/.env") for f in tracked)
        for rel in tracked:
            fp = ROOT / rel
            if fp.is_file() and fp.suffix.lower() not in BINARY_SUFFIXES:
                candidates.add(fp)

    google_key_re = re.compile("AIza" + r"[0-9A-Za-z_-]{20,}")
    sk_key_re = re.compile(r"\bsk-[0-9A-Za-z]{20,}\b")
    pem_key_re = re.compile("-----BEGIN " + r"(?:[A-Z0-9 ]+ )?PRIVATE KEY-----")

    leaks: list[str] = []
    for fp in sorted(candidates):
        try:
            content = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if google_key_re.search(content) or sk_key_re.search(content) or pem_key_re.search(content):
            leaks.append(str(fp.relative_to(ROOT)))

    assert leaks == [], f"Potential leaked secret material in: {leaks}"


def test_submission_short_summary_under_500_chars():
    submission = (ROOT / "docs" / "SUBMISSION.md").read_text(encoding="utf-8")
    pattern = re.compile(
        r"\x23\x23 Short version[^\n]*\n+((?:>[^\n]*\n?)+)(?:\n*\((\d+)\s+characters\))?"
    )
    match = pattern.search(submission)
    assert match is not None, "Missing 'Short version' blockquote in docs/SUBMISSION.md"
    summary_lines = [ln.lstrip("> ").strip() for ln in match.group(1).strip().splitlines()]
    summary = " ".join(ln for ln in summary_lines if ln)
    assert 0 < len(summary) < 500, f"Short summary must be < 500 chars, got {len(summary)}"
    if match.group(2) is not None:
        stated = int(match.group(2))
        assert stated == len(summary), f"Stated char count ({stated}) != actual ({len(summary)})"
        assert stated < 500


def test_montage_stats_reproduces_documented_numbers():
    script = ROOT / "docs" / "evidence" / "montage_stats.py"
    assert script.is_file()
    res = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = res.stdout
    for expected in ("$0.0822", "$0.0339", "27,403", "31%"):
        assert expected in stdout, f"montage_stats.py did not reproduce {expected!r}"

    for rel in ("docs/GEMINI.md", "docs/NUMBERS.md"):
        doc_text = (ROOT / rel).read_text(encoding="utf-8")
        for expected in ("$0.0822", "$0.0339", "27,403", "31%"):
            assert expected in doc_text, f"{rel} missing reproduced montage stat {expected!r}"

    submission_text = (ROOT / "docs" / "SUBMISSION.md").read_text(encoding="utf-8")
    for expected in ("$0.0822", "$0.0339", "31%"):
        assert expected in submission_text, f"docs/SUBMISSION.md missing montage stat {expected!r}"


def test_no_stray_artifacts_or_conflict_markers_in_tracked_files():
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    assert tracked

    root_conformance = [
        rel
        for rel in tracked
        if "/" not in rel and rel.startswith("conformance-") and rel.endswith(".json")
    ]
    assert root_conformance == [], f"Stray root conformance JSON in git: {root_conformance}"

    stray_conformance = [
        rel
        for rel in tracked
        if pathlib.PurePosixPath(rel).name.startswith("conformance-")
        and rel.endswith(".json")
        and not rel.startswith("docs/openness/")
    ]
    assert stray_conformance == [], f"Stray conformance JSON outside docs/openness/: {stray_conformance}"

    allowed_conformance = [
        rel
        for rel in tracked
        if rel.startswith("docs/openness/conformance-") and rel.endswith(".json")
    ]
    assert len(allowed_conformance) >= 2

    forbidden_suffixes = (".pyc", ".sqlite", ".DS_Store", ".pem")
    forbidden_tracked = [
        rel
        for rel in tracked
        if pathlib.PurePosixPath(rel).name == ".DS_Store" or rel.endswith(forbidden_suffixes)
    ]
    assert forbidden_tracked == [], f"Forbidden artifact tracked in git: {forbidden_tracked}"

    conflict_re = re.compile(r"^(?:<{7}|>{7}) ", re.MULTILINE)
    conflict_hits: list[str] = []
    for rel in tracked:
        fp = ROOT / rel
        if not fp.is_file() or fp.suffix.lower() in BINARY_SUFFIXES:
            continue
        raw = fp.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if conflict_re.search(text):
            conflict_hits.append(rel)

    assert conflict_hits == [], f"Git merge conflict markers found in tracked files: {conflict_hits}"

