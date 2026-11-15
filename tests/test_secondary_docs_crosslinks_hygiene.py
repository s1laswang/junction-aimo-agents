from __future__ import annotations

import io
import json
import pathlib
import re
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
HASH_CHAR = chr(35)

SECONDARY_DOCS = (
    "docs/RUN.md",
    "docs/OPENNESS.md",
    "docs/TRUST.md",
    "docs/GEMINI.md",
)

PRIMARY_AND_SECONDARY_DOCS = (
    "README.md",
    "docs/SUBMISSION.md",
    *SECONDARY_DOCS,
)

REQUIRED_SURFACE_TOKENS = (
    "Try to Scam the Agent",
    "One-Page Judge Walkthrough",
    "docs/arena/index.html",
    "docs/demo/index.html",
    "docs/demo/ASSETS.md",
)


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


def test_secondary_docs_exist_and_have_zero_em_dashes_todos_or_conflicts():
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    conflict_start = "<" * 7 + " "
    conflict_end = ">" * 7 + " "

    for rel in SECONDARY_DOCS:
        text = _read_doc(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO/FIXME marker in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder in {rel}"
        for idx, line in enumerate(text.splitlines(), 1):
            assert not line.startswith(conflict_start) and not line.startswith(conflict_end), (
                f"Conflict marker in {rel}:{idx}"
            )


def test_secondary_docs_crosslink_arena_walkthrough_and_cc0_assets_ledger():
    arena_targets = {
        (ROOT / "docs" / "arena").resolve(),
        (ROOT / "docs" / "arena" / "index.html").resolve(),
    }
    demo_targets = {
        (ROOT / "docs" / "demo").resolve(),
        (ROOT / "docs" / "demo" / "index.html").resolve(),
    }
    assets_target = (ROOT / "docs" / "demo" / "ASSETS.md").resolve()
    assert assets_target.is_file() and assets_target.stat().st_size > 0
    assert (ROOT / "docs" / "arena").is_dir()
    assert (ROOT / "docs" / "demo").is_dir()

    for rel in SECONDARY_DOCS:
        text = _read_doc(rel)
        for token in (*REQUIRED_SURFACE_TOKENS, "CC0-1.0", "Kenney"):
            assert token in text, f"{rel} missing required cross-link token {token!r}"

        resolved_set = {resolved for _, resolved in _extract_resolved_links(rel)}
        assert resolved_set & arena_targets, f"{rel} missing relative link to docs/arena"
        assert resolved_set & demo_targets, f"{rel} missing relative link to docs/demo"
        assert assets_target in resolved_set, f"{rel} missing relative link to docs/demo/ASSETS.md"


def test_secondary_docs_all_relative_links_resolve_inside_repo():
    root_resolved = ROOT.resolve()
    allowed_pending = {
        (ROOT / "docs" / "demo-backup.mp4").resolve(),
        (ROOT / "docs" / "pitch" / "requote_beat.py").resolve(),
    }

    for rel in SECONDARY_DOCS:
        links = _extract_resolved_links(rel)
        assert links, f"Expected markdown relative links in {rel}"
        for raw_target, resolved in links:
            assert resolved.is_relative_to(root_resolved), (
                f"Relative link escapes repo root in {rel}: {raw_target}"
            )
            if resolved in allowed_pending and not resolved.exists():
                continue
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked file in {rel}: {raw_target}"

    for optional_html in (
        ROOT / "docs" / "arena" / "index.html",
        ROOT / "docs" / "demo" / "index.html",
    ):
        if optional_html.exists():
            assert optional_html.is_file() and optional_html.stat().st_size > 0


def test_primary_and_secondary_docs_agree_on_judge_surface_names_and_cc0_manifest():
    for rel in PRIMARY_AND_SECONDARY_DOCS:
        text = _read_doc(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        for token in REQUIRED_SURFACE_TOKENS:
            assert token in text, f"{rel} missing judge surface token {token!r}"

    manifest_path = ROOT / "docs" / "arena" / "assets" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("license") == "CC0-1.0"
    packs = manifest.get("packs") or {}
    assert len(packs) >= 4
    for pack_meta in packs.values():
        assert pack_meta.get("license") == "CC0-1.0"
        assert pack_meta.get("author") == "Kenney Vleugels (Kenney.nl)"


def test_run_open_table_and_repo_map_include_arena_and_demo_surfaces():
    run_doc = _read_doc("docs/RUN.md")
    exc_h2 = HASH_CHAR * 2 + " Exceptions: what happens, and how to see it"
    repo_h2 = HASH_CHAR * 2 + " Repo map"
    assert "Then open:" in run_doc
    assert exc_h2 in run_doc
    assert repo_h2 in run_doc

    open_sec = run_doc.split("Then open:", 1)[1].split(exc_h2, 1)[0]
    for token in (
        "docs/arena/index.html",
        "docs/demo/index.html",
        "docs/demo/ASSETS.md",
        "Try to Scam the Agent",
        "One-Page Judge Walkthrough",
    ):
        assert token in open_sec, f"docs/RUN.md 'Then open:' section missing {token!r}"

    repo_sec = run_doc.split(repo_h2, 1)[1]
    for token in (
        "| `docs/arena/` |",
        "| `docs/demo/` |",
        "[ASSETS](demo/ASSETS.md)",
    ):
        assert token in repo_sec, f"docs/RUN.md Repo map missing {token!r}"


def test_this_test_file_has_zero_comments_and_zero_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert HASH_CHAR not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
