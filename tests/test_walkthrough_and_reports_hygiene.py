from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import subprocess
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HASH = chr(35)
SHEBANG = f"{HASH}!/usr/bin/env bash"
EM_DASH = chr(0x2014)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
FRAME_3_NARRATION = (
    "The hotel's own policy says a human must approve: the total is over the five hundred "
    "euro limit, and the volume is three point three times the usual week. The agent pauses."
)


def test_demo_walkthrough_mp4_exists_size_and_iso_bmff_header():
    mp4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    assert mp4.is_file(), "Missing docs/pitch/demo-walkthrough.mp4"
    size = mp4.stat().st_size
    assert size == 2_251_113, f"Expected f3c5996 demo-walkthrough.mp4 size 2251113, got {size}"
    with mp4.open("rb") as fh:
        header = fh.read(32)
    assert len(header) >= 12
    box_size = int.from_bytes(header[0:4], "big")
    assert 8 <= box_size <= 64, f"Unexpected ISO BMFF ftyp box size: {box_size}"
    assert header[4:8] == b"ftyp", f"Missing ISO BMFF ftyp header at bytes 4..8: {header[4:8]!r}"


def test_demo_walkthrough_mp4_ffprobe_streams_and_duration():
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    if not pathlib.Path(ffprobe).is_file():
        pytest.skip("ffprobe not installed")
    mp4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,pix_fmt,width,height,sample_rate,channels",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(mp4),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(proc.stdout)
    streams = info.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    assert len(video_streams) == 1, f"Expected 1 video stream, got {video_streams}"
    assert len(audio_streams) == 1, f"Expected 1 audio stream, got {audio_streams}"

    vstream = video_streams[0]
    assert vstream.get("codec_name") == "h264"
    assert vstream.get("pix_fmt") == "yuv420p"
    assert int(vstream.get("width") or 0) == 1280
    assert int(vstream.get("height") or 0) == 720

    astream = audio_streams[0]
    assert astream.get("codec_name") == "aac"
    assert int(astream.get("sample_rate") or 0) == 44100
    assert int(astream.get("channels") or 0) == 1

    fmt = info.get("format") or {}
    assert int(fmt.get("size") or 0) == 2_251_113
    duration = float(fmt.get("duration") or 0.0)
    assert 109.5 <= duration <= 110.0, f"Unexpected demo-walkthrough.mp4 duration: {duration}"
    assert round(duration, 2) == 109.72
    assert round(duration) == 110


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
    assert FRAME_3_NARRATION in text
    assert "three things need a human" not in text
    assert "the pack is a swap" not in text

    frames_match = re.search(r"frames=\(\s*(.*?)\s*\)", text, flags=re.DOTALL)
    assert frames_match is not None, "Missing frames=(...) array in docs/pitch/build_walkthrough.sh"
    frames_body = frames_match.group(1)

    all_frame_refs = re.findall(r"\$F/([A-Za-z0-9._-]+\.png)", frames_body)
    paired_entries = re.findall(r'"\$F/([A-Za-z0-9._-]+\.png)\|([^"]+)"', frames_body)
    assert len(all_frame_refs) == 13
    assert len(all_frame_refs) == len(paired_entries)
    assert paired_entries[2] == ("live-plan.png", FRAME_3_NARRATION)

    frontend_dir = ROOT / "docs" / "reports" / "frontend"
    for png_name, narration in paired_entries:
        frame_path = frontend_dir / png_name
        assert frame_path.is_file(), f"Missing frame referenced by build_walkthrough.sh: {png_name}"
        assert frame_path.stat().st_size > 0, f"Empty frame PNG: {png_name}"
        with frame_path.open("rb") as fh:
            sig = fh.read(8)
        assert sig == PNG_SIG, f"Invalid PNG header in {png_name}"
        assert narration.strip(), f"Empty narration for frame {png_name}"


EXPECTED_REPORTS = {
    "buyer.md",
    "catalog.md",
    "frontend.md",
    "gateway.md",
    "harden.md",
    "live.md",
    "need.md",
    "pitch.md",
    "port.md",
    "replay.md",
    "submit.md",
    "tests.md",
    "trust.md",
    "ui.md",
    "windtunnel.md",
}


def test_reports_plan_sessions_and_team_non_empty_and_zero_em_dashes():
    report_files = sorted((ROOT / "docs" / "reports").rglob("*.md"))
    top_reports = sorted((ROOT / "docs" / "reports").glob("*.md"))
    assert len(top_reports) >= 14, f"Expected >= 14 markdown reports, got {len(top_reports)}"
    found_names = {p.name for p in top_reports}
    missing_reports = sorted(EXPECTED_REPORTS - found_names)
    assert missing_reports == [], f"Missing expected report files: {missing_reports}"
    targets = [
        *report_files,
        ROOT / "docs" / "TEAM.md",
        ROOT / "docs" / "PLAN.md",
        ROOT / "docs" / "SESSIONS.md",
    ]
    offenders: list[str] = []
    for path in targets:
        assert path.is_file(), f"Missing markdown file: {path.relative_to(ROOT)}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"Empty markdown file: {path.relative_to(ROOT)}"
        if EM_DASH in text:
            bad_lines = [idx for idx, ln in enumerate(text.splitlines(), start=1) if EM_DASH in ln]
            offenders.append(f"{path.relative_to(ROOT)}:{bad_lines}")
    assert offenders == [], f"Em dashes found in reports/TEAM/PLAN/SESSIONS: {offenders}"


def test_reports_relative_links_all_resolve_to_non_empty_repo_targets():
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    report_files = sorted((ROOT / "docs" / "reports").rglob("*.md"))
    targets = [
        *report_files,
        ROOT / "docs" / "TEAM.md",
        ROOT / "docs" / "PLAN.md",
        ROOT / "docs" / "SESSIONS.md",
    ]
    broken: list[str] = []
    for doc in targets:
        raw_text = doc.read_text(encoding="utf-8")
        stripped = re.sub(r"```.*?```", "", raw_text, flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", HASH)):
                continue
            rel = target.split(HASH, 1)[0].split("?", 1)[0]
            if not rel:
                continue
            resolved = (doc.parent / rel).resolve()
            if not resolved.exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {raw_target}")
                continue
            assert ROOT in (resolved, *resolved.parents), (
                f"Relative link escapes repo root: {doc.relative_to(ROOT)} -> {raw_target}"
            )
            if resolved.is_file():
                assert resolved.stat().st_size > 0, (
                    f"Empty linked target: {doc.relative_to(ROOT)} -> {resolved.relative_to(ROOT)}"
                )
    assert broken == [], f"Broken relative links in reports/TEAM/PLAN/SESSIONS: {broken}"


def test_all_frontend_report_pngs_have_valid_header_and_size():
    frontend_dir = ROOT / "docs" / "reports" / "frontend"
    assert frontend_dir.is_dir(), "Missing docs/reports/frontend directory"
    png_files = sorted(frontend_dir.rglob("*.png"))
    assert len(png_files) >= 20, f"Expected >= 20 frontend PNGs, found {len(png_files)}"
    for png_path in png_files:
        size = png_path.stat().st_size
        assert size > 1000, f"PNG size <= 1000 bytes ({size}): {png_path.relative_to(ROOT)}"
        with png_path.open("rb") as fh:
            sig = fh.read(8)
        assert sig == PNG_SIG, f"Invalid PNG magic header in {png_path.relative_to(ROOT)}: {sig!r}"


def test_team_doc_interfaces_and_fr8_fleet_squads():
    team_path = ROOT / "docs" / "TEAM.md"
    assert team_path.is_file(), "Missing docs/TEAM.md"
    team = team_path.read_text(encoding="utf-8")
    assert team.strip(), "Empty docs/TEAM.md"
    assert EM_DASH not in team, "Em dash found in docs/TEAM.md"
    assert "13 tools" in team
    assert "no approval tool" in team.lower()
    assert "/v1/agent/runs" in team
    assert "X-Approver-Session" in team
    assert "X-Approver-Pin" in team
    assert "signed-in human" in team.lower()
    for squad in ("inputs", "buyer", "allergen", "auth", "docs"):
        assert f"fr8/valio/{squad}" in team, f"docs/TEAM.md missing credit for fr8/valio/{squad}"


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


def test_submit_report_plan_and_sessions_cite_62e8384_policy_and_live_run_evidence():
    synced_docs = (
        "docs/reports/submit.md",
        "docs/PLAN.md",
        "docs/SESSIONS.md",
    )
    for rel in synced_docs:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for required in (
            "7225d06",
            "f3c5996",
            "02575d8",
            "62e8384",
            "470d2f3",
            "docs/evidence/live-run/",
            "verify_quote.py",
            "375 passed, 7 skipped, 1 xfailed, 1 xpassed",
            "69.5%",
        ):
            assert required in text, f"{rel} missing {required!r}"

    submit = (ROOT / "docs" / "reports" / "submit.md").read_text(encoding="utf-8")
    assert "Sun 15 Nov 05:36-05:40" in submit
    assert "Sun 15 Nov 05:42-05:44" in submit
    assert "AIMO_GEMINI_TRACE" in submit


def test_submit_report_plan_and_sessions_cite_f3c5996_walkthrough_and_450743d_gate():
    synced_docs = (
        "docs/reports/submit.md",
        "docs/PLAN.md",
        "docs/SESSIONS.md",
    )
    for rel in synced_docs:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for required in (
            "450743d",
            "039d2fa",
            "2,251,113",
            "110 s",
            "109.72 s",
            "70 passed",
            "2,393",
            "92",
            "8/8",
            "two-reason",
        ):
            assert required in text, f"{rel} missing {required!r}"

    submit = (ROOT / "docs" / "reports" / "submit.md").read_text(encoding="utf-8")
    assert "Sun 15 Nov 04:15 (UTC)" in submit
    assert "1280x720" in submit
    assert "70 passed in 11.95s" in submit


def test_pitch_report_reconciled_with_62e8384_final_policy_walkthrough_and_live_run():
    pitch = (ROOT / "docs" / "reports" / "pitch.md").read_text(encoding="utf-8")
    for stale in (
        "03:20 Helsinki",
        "Q1-Q22",
        "425 chars",
        "short version (425",
        "across 88 checks",
    ):
        assert stale not in pitch, f"docs/reports/pitch.md still contains stale {stale!r}"

    for required in (
        "62e8384",
        "02575d8",
        "f3c5996",
        "7225d06",
        "2fe8975",
        "4ccc1fb",
        "1d0b238",
        "69.5%",
        "99.7%",
        "74.8%",
        "331",
        "145",
        "spend",
        "quantity_drift",
        "substitution:464306",
        "demo-walkthrough.mp4",
        "build_walkthrough.sh",
        "110 s",
        "109.72 s",
        "2,251,113",
        "1280x720",
        "demo-backup.mp4",
        "docs/evidence/live-run/",
        "run-b9690772fe",
        "Q-20261115-964294b6",
        "ORD-20261115-06eda13d",
        "24.53 s",
        "AIMO_GEMINI_TRACE",
        "verify_quote.py",
        "/v1/agent/runs",
        "X-Approver-Session",
        "X-Approver-Pin",
        "AIMO_DEMO_FAULTS=1",
        "441",
        "(441 characters)",
        "Q1-Q23",
        "92 checks",
        "92",
        "8/8",
        "44/44",
        "375 passed",
        "274 passed",
        "2,448",
    ):
        assert required in pitch, f"docs/reports/pitch.md missing {required!r}"

    submission = (ROOT / "docs" / "SUBMISSION.md").read_text(encoding="utf-8")
    short_re = re.compile(
        rf"{HASH}{HASH} Short version[^\n]*\n+((?:>[^\n]*\n?)+)\n*\((\d+)\s+characters\)"
    )
    short_match = short_re.search(submission)
    assert short_match is not None, "Missing Short version character count in docs/SUBMISSION.md"
    short_lines = [ln.lstrip("> ").strip() for ln in short_match.group(1).strip().splitlines()]
    short_text = " ".join(ln for ln in short_lines if ln)
    assert len(short_text) == 441
    assert int(short_match.group(2)) == 441

    qa = (ROOT / "docs" / "pitch" / "QA.md").read_text(encoding="utf-8")
    qa_nums = [int(n) for n in re.findall(r"^\*\*(\d+)\.\s+", qa, flags=re.MULTILINE)]
    assert qa_nums == list(range(1, 24)), f"Expected QA questions 1..23, got {qa_nums}"


def test_live_need_replay_and_tests_reports_alignment_contracts():
    live = (ROOT / "docs" / "reports" / "live.md").read_text(encoding="utf-8")
    for required in (
        "02575d8",
        "62e8384",
        "7225d06",
        "f3c5996",
        "docs/evidence/live-run/",
        "run-b9690772fe",
        "Q-20261115-964294b6",
        "ORD-20261115-06eda13d",
        "AIMO_GEMINI_TRACE",
        "verify_quote.py",
    ):
        assert required in live, f"docs/reports/live.md missing {required!r}"

    need = (ROOT / "docs" / "reports" / "need.md").read_text(encoding="utf-8")
    for required in (
        "7225d06",
        "f3c5996",
        "69.5%",
        "145",
        "74.8%",
        "331",
        "99.7%",
        "substitution:464306",
        "quantity_drift",
    ):
        assert required in need, f"docs/reports/need.md missing {required!r}"

    replay = (ROOT / "docs" / "reports" / "replay.md").read_text(encoding="utf-8")
    for required in (
        "62e8384",
        "1d0b238",
        "92 checks",
        "92",
        "s5_allergen_safe",
        "Dockerfile",
        "tests/fixtures/snapshot",
        "2,448",
    ):
        assert required in replay, f"docs/reports/replay.md missing {required!r}"

    tests_report = (ROOT / "docs" / "reports" / "tests.md").read_text(encoding="utf-8")
    for required in (
        "02575d8",
        "62e8384",
        "1d0b238",
        "judge_accept.sh",
        "375 passed",
        "274 passed",
        "92 checks",
        "92",
        "2,448",
    ):
        assert required in tests_report, f"docs/reports/tests.md missing {required!r}"


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
