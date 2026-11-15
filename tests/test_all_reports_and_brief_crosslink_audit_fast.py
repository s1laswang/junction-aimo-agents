from __future__ import annotations

import io
import json
import pathlib
import re
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HASH_CHAR = chr(35)
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
FORBIDDEN_TRAILER = "co-authored" + "-by"
PNG_SIG = b"\x89PNG\r\n\x1a\n"

SESSION_11_REPORT_STEMS = (
    "buyer",
    "catalog",
    "gateway",
    "harden",
    "live",
    "need",
    "pitch",
    "replay",
    "submit",
    "tests",
    "windtunnel",
)

OTHER_REPORT_STEMS = (
    "frontend",
    "port",
    "trust",
    "ui",
)

ALL_EXPECTED_REPORT_FILES = tuple(
    sorted(f"{stem}.md" for stem in (*SESSION_11_REPORT_STEMS, *OTHER_REPORT_STEMS))
)

TOP_AND_BRIEF_DOCS = (
    "docs/PLAN.md",
    "docs/SCENARIOS.md",
    "docs/SESSIONS.md",
    "docs/BRIEF-0800.md",
    "BRIEF.md",
)

REQUIRED_SESSION_REPORT_RAW_LINKS = frozenset(
    {
        "../demo/index.html",
        "../arena/",
        "../demo/ASSETS.md",
        "../arena/assets/manifest.json",
    }
)

FORMERLY_PENDING_TARGETS = (
    "docs/demo-backup.mp4",
    "docs/demo/index.html",
    "docs/arena/index.html",
    "docs/demo/ASSETS.md",
    "docs/arena/scenarios.js",
    "docs/arena/assets/manifest.json",
)


def _audit_markdown_paths() -> list[pathlib.Path]:
    report_paths = sorted((ROOT / "docs" / "reports").rglob("*.md"))
    extra_paths = [ROOT / rel for rel in TOP_AND_BRIEF_DOCS]
    return [*report_paths, *extra_paths]


def _read_utf8_non_empty(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT)
    assert path.is_file(), f"Missing markdown file: {rel}"
    raw_bytes = path.read_bytes()
    assert len(raw_bytes) > 0, f"Zero-byte markdown file: {rel}"
    text = raw_bytes.decode("utf-8")
    assert text.strip(), f"Whitespace-only markdown file: {rel}"
    return text


def _extract_relative_markdown_links(
    doc_path: pathlib.Path,
    *,
    strip_inline_code: bool = True,
) -> list[tuple[str, str, pathlib.Path]]:
    text = _read_utf8_non_empty(doc_path)
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    if strip_inline_code:
        stripped = re.sub(r"(`+)(?!`)(?:(?!\1)[^\n])*\1(?!`)", "", stripped)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    results: list[tuple[str, str, pathlib.Path]] = []
    for raw_expr in link_re.findall(stripped):
        token = raw_expr.strip().split()[0]
        if token.startswith(("http://", "https://", "mailto:", "data:", HASH_CHAR)):
            continue
        clean_rel = token.split(HASH_CHAR, 1)[0].split("?", 1)[0]
        if not clean_rel:
            continue
        resolved = (doc_path.parent / clean_rel).resolve()
        results.append((token, clean_rel, resolved))
    return results


def test_reports_inventory_and_formerly_pending_surfaces_exist_with_zero_exemptions() -> None:
    reports_dir = ROOT / "docs" / "reports"
    assert reports_dir.is_dir(), "Missing docs/reports directory"
    top_reports = tuple(sorted(p.name for p in reports_dir.glob("*.md")))
    nested_reports = tuple(sorted(str(p.relative_to(reports_dir)) for p in reports_dir.rglob("*.md")))
    assert top_reports == ALL_EXPECTED_REPORT_FILES, (
        f"Unexpected docs/reports/*.md set: {top_reports}"
    )
    assert nested_reports == ALL_EXPECTED_REPORT_FILES

    for rel in FORMERLY_PENDING_TARGETS:
        target = ROOT / rel
        assert target.is_file(), f"Missing required submission target: {rel}"
        size = target.stat().st_size
        assert size > 0, f"Empty required submission target: {rel}"

    mp4_header = (ROOT / "docs" / "demo-backup.mp4").read_bytes()[:12]
    assert len(mp4_header) == 12
    assert mp4_header[4:8] == b"ftyp", (
        f"Invalid MP4 ISO BMFF header in docs/demo-backup.mp4: {mp4_header!r}"
    )

    manifest = json.loads(
        (ROOT / "docs" / "arena" / "assets" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("license") == "CC0-1.0"
    assert manifest.get("author") == "Kenney Vleugels (Kenney.nl)"

    root_resolved = ROOT.resolve()
    attr_re = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", flags=re.IGNORECASE)
    for html_rel in ("docs/demo/index.html", "docs/arena/index.html"):
        html_path = ROOT / html_rel
        html_raw = html_path.read_text(encoding="utf-8")
        refs = attr_re.findall(html_raw)
        assert refs, f"No href/src attributes found in {html_rel}"
        for raw_ref in refs:
            assert not raw_ref.lower().startswith(("http://", "https://")), (
                f"External URL in offline surface {html_rel}: {raw_ref}"
            )
            if raw_ref.startswith(("data:", "mailto:", HASH_CHAR)):
                continue
            clean_ref = raw_ref.split(HASH_CHAR, 1)[0].split("?", 1)[0]
            if not clean_ref:
                continue
            resolved_ref = (html_path.parent / clean_ref).resolve()
            assert resolved_ref.is_relative_to(root_resolved), (
                f"Relative href/src escapes repo root in {html_rel}: {raw_ref}"
            )
            assert resolved_ref.exists(), (
                f"Broken href/src in {html_rel} (zero exemptions): {raw_ref}"
            )
            if resolved_ref.is_file():
                assert resolved_ref.stat().st_size > 0, (
                    f"Empty href/src target in {html_rel}: {raw_ref}"
                )


@pytest.mark.parametrize(
    "rel_doc",
    [
        *(f"docs/reports/{name}" for name in ALL_EXPECTED_REPORT_FILES),
        *TOP_AND_BRIEF_DOCS,
    ],
    ids=[
        *(f"doc_report_{name.removesuffix('.md')}" for name in ALL_EXPECTED_REPORT_FILES),
        *(f"doc_top_{rel.replace('/', '_').removesuffix('.md')}" for rel in TOP_AND_BRIEF_DOCS),
    ],
)
def test_markdown_utf8_non_empty_zero_em_dashes_todos_and_conflict_markers(rel_doc: str) -> None:
    path = ROOT / rel_doc
    text = _read_utf8_non_empty(path)

    if EM_DASH in text:
        bad_lines = [idx for idx, ln in enumerate(text.splitlines(), start=1) if EM_DASH in ln]
        pytest.fail(f"Em dash found in {rel_doc} at lines {bad_lines}")

    todo_hits = re.findall(r"\b(?:TODO|FIXME)\b", text, flags=re.IGNORECASE)
    assert todo_hits == [], f"TODO/FIXME marker found in {rel_doc}: {todo_hits}"

    freeze_hits = re.findall(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", text, flags=re.IGNORECASE)
    assert freeze_hits == [], f"Stale freeze placeholder found in {rel_doc}: {freeze_hits}"

    for secret_re in (
        re.compile("AIza" + r"[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bsk-[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bghp_[0-9A-Za-z]{20,}"),
        re.compile("-----BEGIN " + r"[A-Z0-9 ]*PRIVATE KEY-----"),
    ):
        assert secret_re.search(text) is None, f"Potential secret pattern in {rel_doc}"

    conflict_open = "<" * 7
    conflict_sep = "=" * 7
    conflict_close = ">" * 7
    for line_no, line in enumerate(text.splitlines(), start=1):
        assert not line.startswith(conflict_open), f"Conflict start marker in {rel_doc}:{line_no}"
        assert line.strip() != conflict_sep, f"Conflict middle marker in {rel_doc}:{line_no}"
        assert not line.startswith(conflict_close), f"Conflict end marker in {rel_doc}:{line_no}"


@pytest.mark.parametrize("strip_inline", [True, False], ids=["strip_inline", "raw_fenced_only"])
def test_every_relative_markdown_link_resolves_with_zero_pending_exemptions(
    strip_inline: bool,
) -> None:
    root_resolved = ROOT.resolve()
    docs = _audit_markdown_paths()
    assert len(docs) == 20

    broken_links: list[str] = []
    empty_targets: list[str] = []
    escaped_links: list[str] = []
    total_checked = 0

    for doc_path in docs:
        rel_doc = doc_path.relative_to(ROOT)
        links = _extract_relative_markdown_links(doc_path, strip_inline_code=strip_inline)
        for token, clean_rel, resolved in links:
            total_checked += 1
            if not resolved.is_relative_to(root_resolved):
                escaped_links.append(f"{rel_doc} -> {token}")
                continue
            if not resolved.exists():
                broken_links.append(f"{rel_doc} -> {token} (clean={clean_rel})")
                continue
            if resolved.is_file():
                if resolved.stat().st_size == 0:
                    empty_targets.append(f"{rel_doc} -> {resolved.relative_to(root_resolved)}")
                elif resolved.suffix.lower() == ".png":
                    assert resolved.read_bytes()[:8] == PNG_SIG, (
                        f"Invalid PNG header linked from {rel_doc}: {token}"
                    )
                elif resolved.suffix.lower() == ".mp4":
                    hdr = resolved.read_bytes()[:12]
                    assert len(hdr) == 12 and hdr[4:8] == b"ftyp", (
                        f"Invalid MP4 header linked from {rel_doc}: {token}"
                    )
            elif resolved.is_dir():
                entries = list(resolved.iterdir())
                if not entries:
                    empty_targets.append(f"{rel_doc} -> {resolved.relative_to(root_resolved)}/")

    assert total_checked >= 80, f"Expected >= 80 relative links across audited docs, got {total_checked}"
    assert escaped_links == [], f"Relative links escaping repo root: {escaped_links}"
    assert broken_links == [], f"Broken relative links (zero allowed_pending exemptions): {broken_links}"
    assert empty_targets == [], f"Empty linked files or directories: {empty_targets}"


@pytest.mark.parametrize(
    "stem",
    SESSION_11_REPORT_STEMS,
    ids=[f"session_report_{s}" for s in SESSION_11_REPORT_STEMS],
)
def test_all_11_session_reports_crosslink_walkthrough_arena_assets_and_manifest(stem: str) -> None:
    assert len(SESSION_11_REPORT_STEMS) == 11
    report_path = ROOT / "docs" / "reports" / f"{stem}.md"
    links = _extract_relative_markdown_links(report_path, strip_inline_code=True)
    raw_targets = {clean_rel for _, clean_rel, _ in links}
    missing_raw = sorted(REQUIRED_SESSION_REPORT_RAW_LINKS - raw_targets)
    assert missing_raw == [], (
        f"docs/reports/{stem}.md missing required cross-links: {missing_raw}"
    )

    resolved_targets = {resolved for _, _, resolved in links}
    expected_resolved = {
        (ROOT / "docs" / "demo" / "index.html").resolve(),
        (ROOT / "docs" / "arena").resolve(),
        (ROOT / "docs" / "demo" / "ASSETS.md").resolve(),
        (ROOT / "docs" / "arena" / "assets" / "manifest.json").resolve(),
    }
    assert expected_resolved.issubset(resolved_targets), (
        f"docs/reports/{stem}.md resolved links missing targets: "
        f"{sorted(str(p) for p in expected_resolved - resolved_targets)}"
    )


def test_remaining_4_reports_plan_scenarios_sessions_and_brief0800_crosslinks() -> None:
    demo_index = (ROOT / "docs" / "demo" / "index.html").resolve()
    assets_md = (ROOT / "docs" / "demo" / "ASSETS.md").resolve()
    manifest_json = (ROOT / "docs" / "arena" / "assets" / "manifest.json").resolve()
    arena_dir = (ROOT / "docs" / "arena").resolve()
    arena_index = (ROOT / "docs" / "arena" / "index.html").resolve()

    for stem in OTHER_REPORT_STEMS:
        report_path = ROOT / "docs" / "reports" / f"{stem}.md"
        links = _extract_relative_markdown_links(report_path, strip_inline_code=True)
        raw_targets = {clean_rel for _, clean_rel, _ in links}
        assert REQUIRED_SESSION_REPORT_RAW_LINKS.issubset(raw_targets), (
            f"docs/reports/{stem}.md missing required cross-links"
        )

    for rel_doc in ("docs/PLAN.md", "docs/SCENARIOS.md", "docs/SESSIONS.md"):
        resolved_set = {
            resolved for _, _, resolved in _extract_relative_markdown_links(ROOT / rel_doc)
        }
        assert demo_index in resolved_set, f"{rel_doc} missing link to docs/demo/index.html"
        assert arena_dir in resolved_set or arena_index in resolved_set, (
            f"{rel_doc} missing link to docs/arena/"
        )
        assert assets_md in resolved_set, f"{rel_doc} missing link to docs/demo/ASSETS.md"
        assert manifest_json in resolved_set, (
            f"{rel_doc} missing link to docs/arena/assets/manifest.json"
        )

    brief_0800 = ROOT / "docs" / "BRIEF-0800.md"
    brief_0800_text = _read_utf8_non_empty(brief_0800)
    brief_links = {resolved for _, _, resolved in _extract_relative_markdown_links(brief_0800)}
    assert (ROOT / "README.md").resolve() in brief_links
    assert (ROOT / "docs" / "SUBMISSION.md").resolve() in brief_links
    assert (ROOT / "docs" / "evidence" / "live-run" / "README.md").resolve() in brief_links
    assert (ROOT / "docs" / "pitch" / "demo-walkthrough.mp4").resolve() in brief_links
    assert demo_index in brief_links
    assert arena_index in brief_links
    assert assets_md in brief_links
    assert "docs/demo-backup.mp4" in brief_0800_text

    root_brief_text = _read_utf8_non_empty(ROOT / "BRIEF.md")
    assert "Valio Aimo: Trusted Trade Agents" in root_brief_text
    assert "10:00 Helsinki" in root_brief_text


def test_self_hygiene_zero_hash_comments_dashes_and_co_authored_by() -> None:
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert HASH_CHAR not in raw, f"Literal hash char found in {self_path.name}"
    assert EM_DASH not in raw, f"Em dash found in {self_path.name}"
    assert EN_DASH not in raw, f"En dash found in {self_path.name}"
    assert FORBIDDEN_TRAILER not in raw.casefold(), (
        f"Forbidden co-authored trailer found in {self_path.name}"
    )
    comment_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comment_tokens == [], f"Comment tokens found in {self_path.name}: {comment_tokens}"
