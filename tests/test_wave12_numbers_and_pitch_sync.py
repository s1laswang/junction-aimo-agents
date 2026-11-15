from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import tokenize

from aimo.gateway.mcp_server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HASH_CHAR = chr(35)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

WAVE12_OWNED_DOCS = (
    "docs/NUMBERS.md",
    "docs/COMBINE.md",
    "docs/MERGE-MOMIN.md",
    "docs/pitch/deck.md",
    "docs/pitch/QA.md",
)

CROSS_CHECK_DOCS = (
    *WAVE12_OWNED_DOCS,
    "README.md",
    "docs/SUBMISSION.md",
    "docs/demo/ASSETS.md",
)


def _read_text(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing file: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty file: {rel_path}"
    return text


def _primary_pytest_passed(rel_path: str) -> int:
    text = _read_text(rel_path)
    all_matches = re.findall(r"\b(\d+)\s+passed\b", text)
    assert all_matches, f"No 'NNN passed' pytest count found in {rel_path}"
    for line in text.splitlines():
        if "pytest" in line:
            match = re.search(r"\b(\d+)\s+passed\b", line)
            if match:
                primary = int(match.group(1))
                assert primary == int(all_matches[0]), (
                    f"First passed count ({all_matches[0]}) != pytest line ({primary}) in {rel_path}"
                )
                return primary
    return int(all_matches[0])


def _png_dimensions(raw_png: bytes) -> tuple[int, int]:
    assert len(raw_png) >= 24
    assert raw_png[:8] == PNG_MAGIC
    ihdr_len, ihdr_tag = struct.unpack(">I4s", raw_png[8:16])
    assert ihdr_len == 13 and ihdr_tag == b"IHDR"
    width, height = struct.unpack(">II", raw_png[16:24])
    assert width > 0 and height > 0
    return width, height


def test_wave12_docs_cite_b5856cf_6343389_and_wave11_judge_surfaces() -> None:
    required_wave12_tokens = (
        "b5856cf",
        "6343389",
        "2,467",
        "2,462",
        "2,448",
        "1d0b238",
        "docs/arena/index.html",
        "Try to Scam the Agent",
        "docs/demo/index.html",
        "One-Page Judge Walkthrough",
        "ASSETS.md",
        "CC0-1.0",
        "24",
        "4,382",
        "12,151",
        "16,533",
    )
    for rel in WAVE12_OWNED_DOCS:
        text = _read_text(rel)
        for token in required_wave12_tokens:
            assert token in text, f"{rel} missing Wave 12 parity token {token!r}"

    for rel in ("README.md", "docs/SUBMISSION.md"):
        text = _read_text(rel)
        for token in (
            "6343389",
            "2,462",
            "docs/arena/index.html",
            "Try to Scam the Agent",
            "docs/demo/index.html",
            "One-Page Judge Walkthrough",
            "docs/demo/ASSETS.md",
        ):
            assert token in text, f"{rel} missing Wave 11/12 parity token {token!r}"


def test_arena_manifest_and_png_bytes_match_wave12_docs() -> None:
    assets_dir = ROOT / "docs" / "arena" / "assets"
    manifest_path = assets_dir / "manifest.json"
    assert assets_dir.is_dir()
    assert manifest_path.is_file()

    manifest_raw = manifest_path.read_text(encoding="utf-8")
    assert EM_DASH not in manifest_raw
    manifest = json.loads(manifest_raw)

    assert manifest["license"] == "CC0-1.0"
    assert manifest["author"] == "Kenney Vleugels (Kenney.nl)"
    assert set(manifest["packs"]) == {
        "tiny-dungeon",
        "tiny-town",
        "emotes-pack",
        "pixel-platformer",
    }
    assert len(manifest["characters"]) == 5
    assert len(manifest["tiles"]) == 11
    assert len(manifest["icons"]) == 8

    seen_pngs: set[str] = set()
    png_byte_sum = 0
    for group in ("characters", "tiles", "icons"):
        for key, meta in manifest[group].items():
            fname = meta["file"]
            png_path = assets_dir / fname
            assert png_path.is_file(), f"Missing PNG for {group}.{key}: {fname}"
            raw_png = png_path.read_bytes()
            width, height = _png_dimensions(raw_png)
            assert (width, height) == (meta["width"], meta["height"])
            assert len(raw_png) == meta["bytes"]
            assert hashlib.sha256(raw_png).hexdigest()[:16] == meta["sha256_16"]
            assert meta["license"] == "CC0-1.0"
            assert fname not in seen_pngs, f"Duplicate PNG entry {fname}"
            seen_pngs.add(fname)
            png_byte_sum += len(raw_png)

    disk_pngs = {p.name for p in assets_dir.glob("*.png")}
    assert disk_pngs == seen_pngs
    assert len(seen_pngs) == 24
    assert png_byte_sum == 4_382

    manifest_bytes = manifest_path.stat().st_size
    assert manifest_bytes == 12_151

    all_asset_files = [p for p in assets_dir.iterdir() if p.is_file()]
    total_dir_bytes = sum(p.stat().st_size for p in all_asset_files)
    assert len(all_asset_files) == 25
    assert total_dir_bytes == png_byte_sum + manifest_bytes == 16_533
    assert total_dir_bytes < 1_500_000

    for rel in ("docs/demo/ASSETS.md", *WAVE12_OWNED_DOCS):
        text = _read_text(rel)
        assert f"{png_byte_sum:,}" in text, f"{rel} missing exact PNG byte sum {png_byte_sum:,}"
        assert f"{manifest_bytes:,}" in text, f"{rel} missing exact manifest byte size {manifest_bytes:,}"
        assert f"{total_dir_bytes:,}" in text, f"{rel} missing exact directory byte sum {total_dir_bytes:,}"


def test_cross_doc_pytest_mcp_scenarios_and_conformance_parity() -> None:
    tools = asyncio.run(build_mcp().list_tools())
    tool_names = [t.name for t in tools]
    assert len(tool_names) == 13
    assert [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()] == []

    primary_targets = ("README.md", "docs/NUMBERS.md", "docs/SUBMISSION.md")
    primary_counts = {rel: _primary_pytest_passed(rel) for rel in primary_targets}
    assert primary_counts == {
        "README.md": 1150,
        "docs/NUMBERS.md": 1150,
        "docs/SUBMISSION.md": 1150,
    }

    for rel in (*WAVE12_OWNED_DOCS, "README.md", "docs/SUBMISSION.md"):
        text = _read_text(rel)
        assert "13" in text, f"{rel} missing 13 MCP tools count"
        assert "8/8" in text, f"{rel} missing 8/8 scenarios"
        assert "92" in text, f"{rel} missing 92 scenario checks"
        assert "62e8384" in text, f"{rel} missing standalone main SHA 62e8384"
        assert "02575d8" in text, f"{rel} missing live-run SHA 02575d8"
        assert "375" in text and "274" in text, f"{rel} missing standalone main 375/274 counts"
        assert "1,168" in text and "c98e72e" in text, f"{rel} missing 1,168 passed on c98e72e"
        assert "1,150" in text or "1150" in text, f"{rel} missing 1,150 passed on 6973c2d"
        assert "6973c2d" in text, f"{rel} missing 6973c2d SHA"
        assert "44/44" in text, f"{rel} missing 44/44 conformance"


def test_pytest_collect_only_floor_matches_wave12_baseline() -> None:
    env = {
        **os.environ,
        "AIMO_OFFLINE": "1",
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    match = re.search(r"\b(\d+)\s+tests?\s+collected\b", proc.stdout)
    assert match is not None, f"Could not parse collected count from pytest output:\n{proc.stdout[-500:]}"
    collected = int(match.group(1))
    assert collected >= 2467, f"Expected >= 2467 collected tests on top of b5856cf, got {collected}"


def test_wave12_docs_links_todos_and_zero_em_dashes() -> None:
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    allowed_pending = {
        (ROOT / "docs" / "demo-backup.mp4").resolve(),
        (ROOT / "docs" / "pitch" / "requote_beat.py").resolve(),
        (ROOT / "docs" / "arena" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "index.html").resolve(),
    }

    for rel in CROSS_CHECK_DOCS:
        text = _read_text(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"Unclosed TODO/FIXME in {rel}"
        assert freeze_re.findall(text) == [], f"Unclosed freeze placeholder in {rel}"

        doc_path = ROOT / rel
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        stripped = re.sub(r"`[^`\n]+`", "", stripped)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", "data:", HASH_CHAR)):
                continue
            clean = target.split(HASH_CHAR, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (doc_path.parent / clean).resolve()
            assert resolved.is_relative_to(ROOT.resolve()), f"Link escapes repo root in {rel}: {raw_target}"
            if resolved in allowed_pending and not resolved.exists():
                continue
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked target in {rel}: {raw_target}"


def test_wave12_python_test_files_zero_comments_and_zero_em_dashes() -> None:
    self_path = pathlib.Path(__file__).resolve()
    self_raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in self_raw
    assert EN_DASH not in self_raw
    assert HASH_CHAR not in self_raw

    for rel in (
        "tests/test_wave12_numbers_and_pitch_sync.py",
        "tests/test_arena_assets_hygiene.py",
        "tests/test_combine_and_dockerfile_hygiene.py",
        "tests/test_docs_final_pr_hygiene.py",
        "tests/test_numbers_reproducibility.py",
    ):
        raw = _read_text(rel)
        assert EM_DASH not in raw, f"Em dash found in {rel}"
        comments = [
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
            if tok.type == tokenize.COMMENT
        ]
        assert comments == [], f"Python comments found in {rel}: {comments}"
