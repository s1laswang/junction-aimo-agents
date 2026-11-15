from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
ANCHOR_SEP = chr(35)

WAVE13_OWNED_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/DECISIONS.md",
    "docs/ASSUMPTIONS.md",
    "docs/interfaces.md",
    "docs/windtunnel-results.md",
)

WAVE13_REQUIRED_CROSS_LINK_TOKENS = (
    "[docs/demo/index.html](demo/index.html)",
    "One-Page Judge Walkthrough",
    "WebCrypto",
    "Ed25519",
    "agent_cannot_approve",
    "requote_needed",
    "AP",
    "[docs/arena/](arena/)",
    "Try to Scam the Agent",
    "`docs/arena/index.html`",
    "CC0-1.0",
    "[docs/demo/ASSETS.md](demo/ASSETS.md)",
    "7990fa3",
    "fr8/valio/main",
    "d89db12",
    "4,137",
)


def _read_text(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing file: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty file: {rel_path}"
    return text


def test_wave13_owned_docs_cross_link_demo_walkthrough_and_arena_assets() -> None:
    demo_html = _read_text("docs/demo/index.html")
    assets_md = _read_text("docs/demo/ASSETS.md")

    for demo_tok in (
        "One-Page Judge Walkthrough",
        "Try to Scam the Agent",
        "crypto.subtle",
        "Ed25519",
        "agent_cannot_approve",
        "requote_needed",
        "run-b9690772fe",
        "Q-20261115-964294b6",
        "ORD-20261115-06eda13d",
        'href="./ASSETS.md"',
    ):
        assert demo_tok in demo_html, f"docs/demo/index.html missing {demo_tok!r}"

    for asset_tok in (
        "CC0 1.0 Universal",
        "CC0-1.0",
        "Kenney Vleugels",
        "docs/arena/",
        "4,382",
        "12,151",
        "16,533",
    ):
        assert asset_tok in assets_md, f"docs/demo/ASSETS.md missing {asset_tok!r}"

    for rel in WAVE13_OWNED_DOCS:
        doc_text = _read_text(rel)
        for token in WAVE13_REQUIRED_CROSS_LINK_TOKENS:
            assert token in doc_text, f"{rel} missing required Wave 13 token {token!r}"


def test_wave13_demo_and_arena_artifact_consistency() -> None:
    live_dir = ROOT / "docs" / "evidence" / "live-run"
    quote_data = json.loads((live_dir / "quote.json").read_text(encoding="utf-8"))
    order_data = json.loads((live_dir / "order.json").read_text(encoding="utf-8"))
    jwks_data = json.loads((live_dir / "jwks.json").read_text(encoding="utf-8"))
    events_data = json.loads((live_dir / "audit-events.json").read_text(encoding="utf-8"))
    assert len(events_data) == 6
    assert order_data["response_code"] == "AP"

    demo_html = _read_text("docs/demo/index.html")
    assert quote_data["quote_id"] in demo_html
    assert quote_data["quote_hash"] in demo_html
    assert order_data["order_id"] in demo_html
    assert jwks_data["keys"][0]["kid"] in demo_html

    assets_dir = ROOT / "docs" / "arena" / "assets"
    manifest_path = assets_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["license"] == "CC0-1.0"

    png_sum = 0
    sprite_count = 0
    for group in ("characters", "tiles", "icons"):
        for meta in manifest[group].values():
            sprite_path = assets_dir / meta["file"]
            assert sprite_path.is_file()
            raw_png = sprite_path.read_bytes()
            assert len(raw_png) == meta["bytes"]
            assert hashlib.sha256(raw_png).hexdigest()[:16] == meta["sha256_16"]
            png_sum += len(raw_png)
            sprite_count += 1

    assert sprite_count == 24
    assert png_sum == 4_382
    assert manifest_path.stat().st_size == 12_151
    assert png_sum + manifest_path.stat().st_size == 16_533


def test_wave13_owned_docs_strict_link_resolution_zero_todos_and_em_dashes() -> None:
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    root_resolved = ROOT.resolve()

    for rel in (*WAVE13_OWNED_DOCS, "docs/demo/ASSETS.md", "docs/demo/index.html"):
        text = _read_text(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"Unclosed TODO/FIXME in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder in {rel}"

    for rel in (*WAVE13_OWNED_DOCS, "docs/demo/ASSETS.md"):
        doc_path = ROOT / rel
        text = _read_text(rel)
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for raw_target in link_re.findall(stripped):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:", "data:", ANCHOR_SEP)):
                continue
            clean = target.split(ANCHOR_SEP, 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (doc_path.parent / clean).resolve()
            assert resolved.is_relative_to(root_resolved), f"Link escapes repo root in {rel}: {raw_target}"
            assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
            if resolved.is_file():
                assert resolved.stat().st_size > 0, f"Empty linked asset in {rel}: {raw_target}"


def test_wave13_pytest_collect_only_floor_at_least_4137() -> None:
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
        timeout=90,
        check=True,
    )
    match = re.search(r"\b(\d+)\s+tests?\s+collected\b", proc.stdout)
    assert match is not None, f"Could not parse collected count:\n{proc.stdout[-500:]}"
    collected = int(match.group(1))
    assert collected >= 4137, f"Expected >= 4137 collected tests on top of d89db12, got {collected}"


def test_wave13_arch_adr_demo_sync_self_hygiene() -> None:
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert EN_DASH not in raw
    assert ANCHOR_SEP not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
