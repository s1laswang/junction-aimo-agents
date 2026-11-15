from __future__ import annotations

import io
import pathlib
import re
import subprocess
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
HASH = chr(35)
SHEBANG = f"{HASH}!/usr/bin/env bash"
EM_DASH = chr(0x2014)
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_demo_walkthrough_mp4_exists_size_and_iso_bmff_header():
    mp4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    assert mp4.is_file(), "Missing docs/pitch/demo-walkthrough.mp4"
    size = mp4.stat().st_size
    assert 1_000_000 < size < 10_000_000, f"Unexpected demo-walkthrough.mp4 size: {size}"
    with mp4.open("rb") as fh:
        header = fh.read(32)
    assert len(header) >= 12
    box_size = int.from_bytes(header[0:4], "big")
    assert 8 <= box_size <= 64, f"Unexpected ISO BMFF ftyp box size: {box_size}"
    assert header[4:8] == b"ftyp", f"Missing ISO BMFF ftyp header at bytes 4..8: {header[4:8]!r}"


def test_build_walkthrough_script_syntax_hygiene_and_referenced_frames():
    script = ROOT / "docs" / "pitch" / "build_walkthrough.sh"
    assert script.is_file(), "Missing docs/pitch/build_walkthrough.sh"
    text = script.read_text(encoding="utf-8")
    assert EM_DASH not in text, "Em dash found in docs/pitch/build_walkthrough.sh"

    lines = text.splitlines()
    assert lines and lines[0] == SHEBANG, "Line 1 of build_walkthrough.sh must be bash shebang"
    comment_lines = [
        idx for idx, ln in enumerate(lines[1:], start=2) if ln.lstrip().startswith(HASH)
    ]
    assert comment_lines == [], (
        f"Comment lines found after shebang in docs/pitch/build_walkthrough.sh: {comment_lines}"
    )

    syntax = subprocess.run(
        ["bash", "-n", str(script)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, f"bash -n failed on build_walkthrough.sh: {syntax.stderr}"

    assert "F=docs/reports/frontend" in text
    assert "OUT=docs/pitch/demo-walkthrough.mp4" in text
    assert "signed in with her own passphrase before she started the run" in text

    frames_match = re.search(r"frames=\(\s*(.*?)\s*\)", text, flags=re.DOTALL)
    assert frames_match is not None, "Missing frames=(...) array in docs/pitch/build_walkthrough.sh"
    frames_body = frames_match.group(1)

    all_frame_refs = re.findall(r"\$F/([A-Za-z0-9._-]+\.png)", frames_body)
    paired_entries = re.findall(r'"\$F/([A-Za-z0-9._-]+\.png)\|([^"]+)"', frames_body)
    assert len(all_frame_refs) >= 10
    assert len(all_frame_refs) == len(paired_entries)

    frontend_dir = ROOT / "docs" / "reports" / "frontend"
    for png_name, narration in paired_entries:
        frame_path = frontend_dir / png_name
        assert frame_path.is_file(), f"Missing frame referenced by build_walkthrough.sh: {png_name}"
        assert frame_path.stat().st_size > 0, f"Empty frame PNG: {png_name}"
        with frame_path.open("rb") as fh:
            sig = fh.read(8)
        assert sig == PNG_SIG, f"Invalid PNG header in {png_name}"
        assert narration.strip(), f"Empty narration for frame {png_name}"


def test_reports_plan_and_sessions_contain_zero_em_dashes():
    report_files = sorted((ROOT / "docs" / "reports").glob("*.md"))
    assert len(report_files) >= 10, "Expected markdown reports under docs/reports/"
    targets = [*report_files, ROOT / "docs" / "PLAN.md", ROOT / "docs" / "SESSIONS.md"]
    offenders: list[str] = []
    for path in targets:
        assert path.is_file(), f"Missing markdown file: {path.relative_to(ROOT)}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"Empty markdown file: {path.relative_to(ROOT)}"
        if EM_DASH in text:
            bad_lines = [idx for idx, ln in enumerate(text.splitlines(), start=1) if EM_DASH in ln]
            offenders.append(f"{path.relative_to(ROOT)}:{bad_lines}")
    assert offenders == [], f"Em dashes found in reports/PLAN/SESSIONS: {offenders}"


def test_submit_report_plan_and_sessions_cite_4ccc1fb_walkthrough_and_0f0ece1():
    synced_docs = (
        "docs/reports/submit.md",
        "docs/PLAN.md",
        "docs/SESSIONS.md",
    )
    for rel in synced_docs:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for required in (
            "4ccc1fb",
            "2fe8975",
            "0f0ece1",
            "demo-walkthrough.mp4",
            "build_walkthrough.sh",
            "111 s",
            "371 passed, 7 skipped, 1 xfailed, 1 xpassed",
            "270 passed",
        ):
            assert required in text, f"{rel} missing {required!r}"

    submit = (ROOT / "docs" / "reports" / "submit.md").read_text(encoding="utf-8")
    assert "Sun 15 Nov 05:22-05:33" in submit
    assert "keyless 270 passed / 0 failed" in submit
    assert "sign-in-before-run" in submit


def test_self_zero_comments_and_zero_em_dashes():
    self_path = pathlib.Path(__file__)
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw, "Em dash found in test_walkthrough_and_reports_hygiene.py"
    assert HASH not in raw, "Hash character found in test_walkthrough_and_reports_hygiene.py"
    comment_tokens = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comment_tokens == [], f"Comment tokens found in {self_path.name}: {comment_tokens}"
