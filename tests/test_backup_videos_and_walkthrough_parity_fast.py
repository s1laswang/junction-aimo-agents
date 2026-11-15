from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import subprocess
import tokenize
import xml.etree.ElementTree as ET
from typing import Any

from conformance.verify import thumbprint, verify_events, verify_quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_RUN_DIR = ROOT / "docs" / "evidence" / "live-run"
DEMO_HTML = ROOT / "docs" / "demo" / "index.html"
DEMO_BACKUP_MP4 = ROOT / "docs" / "demo-backup.mp4"
WALKTHROUGH_MP4 = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"

HASH = chr(35)
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
FORBIDDEN_TRAILER = "co-authored" + "-by"

EXPECTED_DEMO_BACKUP_BYTES = 2_161_978
EXPECTED_WALKTHROUGH_BYTES = 2_251_113
EXPECTED_QUOTE_ID = "Q-20261115-964294b6"
EXPECTED_ORDER_ID = "ORD-20261115-06eda13d"
EXPECTED_RUN_ID = "run-b9690772fe"
EXPECTED_QUOTE_HASH = "6def7dae163f23324fb97037000ac9ab7fa7276971eead120069e09f3270fbda"
EXPECTED_JWKS_KID = "CIbMjdup7xejhizQv2eS1sKdEZ_rIOdetD4U1D-Xavc"


def _find_ffprobe() -> str | None:
    found = shutil.which("ffprobe")
    if found and pathlib.Path(found).is_file():
        return found
    fallback = pathlib.Path("/usr/bin/ffprobe")
    if fallback.is_file():
        return str(fallback)
    return None


def _assert_iso_bmff_header(mp4_path: pathlib.Path) -> None:
    with mp4_path.open("rb") as fh:
        header = fh.read(32)
    assert len(header) >= 12
    box_size = int.from_bytes(header[0:4], "big")
    assert 8 <= box_size <= 64, f"Unexpected ISO BMFF ftyp box size in {mp4_path.name}: {box_size}"
    assert header[4:8] == b"ftyp", f"Missing ISO BMFF ftyp signature in {mp4_path.name}: {header[4:8]!r}"


def _probe_mp4_streams(ffprobe: str, mp4_path: pathlib.Path) -> dict[str, Any]:
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
            str(mp4_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(proc.stdout)


def _extract_js_const_json(html_text: str, const_name: str) -> Any:
    match = re.search(rf"const\s+{re.escape(const_name)}\s*=\s*(.+?);\s*$", html_text, flags=re.MULTILINE)
    assert match is not None, f"Missing embedded constant {const_name} in docs/demo/index.html"
    return json.loads(match.group(1))


def test_demo_backup_mp4_size_iso_header_and_ffprobe_h264_yuv420p() -> None:
    assert DEMO_BACKUP_MP4.is_file(), "Missing docs/demo-backup.mp4"
    size = DEMO_BACKUP_MP4.stat().st_size
    assert size == EXPECTED_DEMO_BACKUP_BYTES, (
        f"Expected docs/demo-backup.mp4 size {EXPECTED_DEMO_BACKUP_BYTES}, got {size}"
    )
    _assert_iso_bmff_header(DEMO_BACKUP_MP4)

    ffprobe = _find_ffprobe()
    if ffprobe is not None:
        info = _probe_mp4_streams(ffprobe, DEMO_BACKUP_MP4)
        streams = info.get("streams") or []
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        assert len(video_streams) == 1, f"Expected 1 video stream in demo-backup.mp4, got {video_streams}"
        vstream = video_streams[0]
        assert vstream.get("codec_name") == "h264"
        assert vstream.get("pix_fmt") == "yuv420p"
        assert int(vstream.get("width") or 0) == 1280
        assert int(vstream.get("height") or 0) == 800

        fmt = info.get("format") or {}
        assert int(fmt.get("size") or 0) == EXPECTED_DEMO_BACKUP_BYTES
        duration = float(fmt.get("duration") or 0.0)
        assert 62.0 <= duration <= 62.5, f"Unexpected demo-backup.mp4 duration: {duration}"
        assert round(duration, 2) == 62.17


def test_demo_walkthrough_mp4_size_iso_header_and_ffprobe_h264_aac() -> None:
    assert WALKTHROUGH_MP4.is_file(), "Missing docs/pitch/demo-walkthrough.mp4"
    size = WALKTHROUGH_MP4.stat().st_size
    assert size == EXPECTED_WALKTHROUGH_BYTES, (
        f"Expected docs/pitch/demo-walkthrough.mp4 size {EXPECTED_WALKTHROUGH_BYTES}, got {size}"
    )
    _assert_iso_bmff_header(WALKTHROUGH_MP4)

    ffprobe = _find_ffprobe()
    if ffprobe is not None:
        info = _probe_mp4_streams(ffprobe, WALKTHROUGH_MP4)
        streams = info.get("streams") or []
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        assert len(video_streams) == 1, f"Expected 1 video stream in demo-walkthrough.mp4, got {video_streams}"
        assert len(audio_streams) == 1, f"Expected 1 audio stream in demo-walkthrough.mp4, got {audio_streams}"

        vstream = video_streams[0]
        assert vstream.get("codec_name") == "h264"
        assert vstream.get("pix_fmt") == "yuv420p"
        assert int(vstream.get("width") or 0) == 1280
        assert int(vstream.get("height") or 0) == 720

        astream = audio_streams[0]
        assert astream.get("codec_name") == "aac"
        assert int(astream.get("sample_rate") or 0) == 44_100
        assert int(astream.get("channels") or 0) == 1

        fmt = info.get("format") or {}
        assert int(fmt.get("size") or 0) == EXPECTED_WALKTHROUGH_BYTES
        duration = float(fmt.get("duration") or 0.0)
        assert 109.5 <= duration <= 110.0, f"Unexpected demo-walkthrough.mp4 duration: {duration}"
        assert round(duration, 2) == 109.72


def test_both_videos_cited_and_linked_in_demo_submission_and_brief_docs() -> None:
    cited_docs = (
        "docs/DEMO.md",
        "docs/SUBMISSION.md",
        "docs/BRIEF-0800.md",
    )
    for rel in cited_docs:
        doc_path = ROOT / rel
        assert doc_path.is_file(), f"Missing required doc: {rel}"
        text = doc_path.read_text(encoding="utf-8")
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert "docs/demo-backup.mp4" in text, f"{rel} must cite docs/demo-backup.mp4"
        assert "docs/pitch/demo-walkthrough.mp4" in text, f"{rel} must cite docs/pitch/demo-walkthrough.mp4"
        assert "docs/demo/index.html" in text, f"{rel} must cite docs/demo/index.html"
        assert "docs/arena/index.html" in text, f"{rel} must cite docs/arena/index.html"
        assert "docs/demo/ASSETS.md" in text, f"{rel} must cite docs/demo/ASSETS.md"
        assert "docs/evidence/live-run/" in text, f"{rel} must cite docs/evidence/live-run/"

    demo_md = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")
    for token in (
        f"{EXPECTED_DEMO_BACKUP_BYTES:,}",
        "62.17 s",
        "1280x800",
        "yuv420p",
        "109.72 s",
        "110 s",
        "scripts/record_demo_video.py",
        "docs/pitch/build_walkthrough.sh",
    ):
        assert token in demo_md, f"docs/DEMO.md missing video parity token {token!r}"

    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for rel in (*cited_docs, "docs/evidence/live-run/README.md", "docs/demo/ASSETS.md"):
        doc_path = ROOT / rel
        raw = doc_path.read_text(encoding="utf-8")
        stripped = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
        stripped = re.sub(r"`[^`\n]+`", "", stripped)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", "data:", HASH)):
                continue
            clean = target.split(HASH, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (doc_path.parent / clean).resolve()
            assert resolved.is_relative_to(ROOT.resolve()), f"Link escapes repo root in {rel}: {raw_target}"
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty target linked from {rel}: {raw_target}"


def test_demo_walkthrough_html_embeds_exact_live_run_evidence_parity() -> None:
    assert DEMO_HTML.is_file(), "Missing docs/demo/index.html"
    html_raw = DEMO_HTML.read_text(encoding="utf-8")

    run_disk = json.loads((LIVE_RUN_DIR / "run.json").read_text(encoding="utf-8"))
    quote_disk = json.loads((LIVE_RUN_DIR / "quote.json").read_text(encoding="utf-8"))
    order_disk = json.loads((LIVE_RUN_DIR / "order.json").read_text(encoding="utf-8"))
    jwks_disk = json.loads((LIVE_RUN_DIR / "jwks.json").read_text(encoding="utf-8"))
    events_disk = json.loads((LIVE_RUN_DIR / "audit-events.json").read_text(encoding="utf-8"))
    trace_rows = [
        json.loads(line)
        for line in (LIVE_RUN_DIR / "gemini-trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert run_disk["run_id"] == EXPECTED_RUN_ID
    assert {row["run_id"] for row in trace_rows} == {EXPECTED_RUN_ID}
    assert quote_disk["quote_id"] == EXPECTED_QUOTE_ID
    assert quote_disk["quote_hash"] == EXPECTED_QUOTE_HASH
    assert order_disk["order_id"] == EXPECTED_ORDER_ID
    assert order_disk["quote_id"] == EXPECTED_QUOTE_ID
    assert order_disk["quote_hash"] == EXPECTED_QUOTE_HASH

    jwk_entry = jwks_disk["keys"][0]
    assert jwk_entry["kid"] == EXPECTED_JWKS_KID
    assert quote_disk["kid"] == EXPECTED_JWKS_KID
    assert thumbprint(jwk_entry) == EXPECTED_JWKS_KID

    for expected_id in (
        EXPECTED_QUOTE_ID,
        EXPECTED_ORDER_ID,
        EXPECTED_RUN_ID,
        EXPECTED_QUOTE_HASH,
        EXPECTED_JWKS_KID,
        jwk_entry["x"],
    ):
        assert expected_id in html_raw, f"docs/demo/index.html missing live-run artifact token {expected_id!r}"

    embedded_jwks = _extract_js_const_json(html_raw, "LIVE_JWKS")
    embedded_quote = _extract_js_const_json(html_raw, "LIVE_QUOTE")
    embedded_order = _extract_js_const_json(html_raw, "LIVE_ORDER")
    embedded_events = _extract_js_const_json(html_raw, "INITIAL_AUDIT_EVENTS")

    assert embedded_jwks == jwks_disk
    assert embedded_quote == quote_disk
    assert embedded_events == events_disk
    assert len(embedded_events) == 6

    quote_verdict = verify_quote(embedded_quote, embedded_jwks)
    assert quote_verdict["ok"] is True, f"Embedded LIVE_QUOTE failed verify_quote: {quote_verdict}"
    assert quote_verdict["quote_hash"] == EXPECTED_QUOTE_HASH

    events_verdict = verify_events(embedded_events, embedded_jwks, contiguous=True)
    assert events_verdict["ok"] is True, f"Embedded INITIAL_AUDIT_EVENTS failed verify_events: {events_verdict}"
    assert events_verdict["checked"] == 6

    assert embedded_order["order_id"] == EXPECTED_ORDER_ID
    assert embedded_order["quote_id"] == EXPECTED_QUOTE_ID
    assert embedded_order["quote_hash"] == EXPECTED_QUOTE_HASH
    assert embedded_order["status"] == "placed"
    assert embedded_order["response_code"] == "AP"
    assert embedded_order["approved_by"] == "anna@hotel-aurora.example"
    for field_name, field_value in embedded_order.items():
        assert order_disk[field_name] == field_value, f"LIVE_ORDER.{field_name} diverges from order.json"

    xml_match = re.search(r'const\s+PEPPOL_AP_XML\s*=\s*\[(.*?)\]\.join\("\\n"\);', html_raw, flags=re.DOTALL)
    assert xml_match is not None, "Missing PEPPOL_AP_XML preview in docs/demo/index.html"
    xml_lines = re.findall(r"'([^']*)'", xml_match.group(1))
    xml_text = "\n".join(xml_lines)
    xml_root = ET.fromstring(xml_text)
    assert xml_root.tag.endswith("OrderResponse")
    for expected_xml_tok in (
        "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>",
        EXPECTED_QUOTE_ID,
        EXPECTED_ORDER_ID,
        EXPECTED_QUOTE_HASH,
        EXPECTED_JWKS_KID,
    ):
        assert expected_xml_tok in xml_text


def test_judge_demo_static_surfaces_and_scripts_hygiene_parity() -> None:
    text_artifacts = (
        "docs/demo/index.html",
        "docs/demo/ASSETS.md",
        "docs/arena/index.html",
        "docs/arena/scenarios.js",
        "scripts/record_demo_video.py",
        "docs/pitch/build_walkthrough.sh",
        "docs/evidence/live-run/README.md",
    )
    secret_patterns = (
        re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bsk-[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bghp_[0-9A-Za-z]{20,}"),
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    )
    for rel in text_artifacts:
        path = ROOT / rel
        assert path.is_file(), f"Missing judge demo artifact: {rel}"
        raw = path.read_text(encoding="utf-8")
        assert raw.strip(), f"Empty judge demo artifact: {rel}"
        assert EM_DASH not in raw, f"Em dash found in {rel}"
        for pat in secret_patterns:
            assert pat.search(raw) is None, f"Secret pattern {pat.pattern!r} matched in {rel}"

    for html_rel in ("docs/demo/index.html", "docs/arena/index.html"):
        html_path = (ROOT / html_rel).resolve()
        html_raw = html_path.read_text(encoding="utf-8")
        assert EN_DASH not in html_raw, f"En dash found in {html_rel}"
        assert re.findall(r"""(?:src|href)\s*=\s*["']https?:""", html_raw, flags=re.IGNORECASE) == []
        for attr_target in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html_raw, flags=re.IGNORECASE):
            if attr_target.startswith(("data:", "mailto:", HASH)):
                continue
            clean = attr_target.split(HASH, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (html_path.parent / clean).resolve()
            assert resolved.is_relative_to(ROOT.resolve()), f"Escaping link in {html_rel}: {attr_target}"
            assert resolved.is_file() and resolved.stat().st_size > 0, (
                f"Broken or empty asset/link in {html_rel}: {attr_target}"
            )


def test_self_hygiene_zero_comments_zero_dashes_and_zero_co_authored_by() -> None:
    self_text = pathlib.Path(__file__).resolve().read_text(encoding="utf-8")
    assert HASH not in self_text
    assert EM_DASH not in self_text
    assert EN_DASH not in self_text
    assert FORBIDDEN_TRAILER not in self_text.casefold()
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(self_text).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
