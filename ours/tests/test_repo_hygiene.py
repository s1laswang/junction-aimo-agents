from pathlib import Path
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from aimo.app import app

ROOT = Path(__file__).resolve().parent.parent

EM_DASH_PATTERNS = [
    "README.md",
    "CONTRACT.md",
    "docs/*.md",
    "pitch/*.md",
    "deploy/*.md",
    "web/*.html",
    "web/*.js",
    "web/*.css",
    "aimo/*.py",
    "agents/*.py",
    "scenarios/*.py",
]

MD_PATTERNS = [
    "README.md",
    "CONTRACT.md",
    "docs/*.md",
    "pitch/*.md",
    "deploy/*.md",
]

PUBLIC_AND_UI_ROUTES = [
    "/health",
    "/.well-known/agent-card.json",
    "/.well-known/jwks.json",
    "/openapi.json",
    "/",
    "/console.html",
    "/inbox.html",
    "/audit.html",
    "/admin.html",
    "/style.css",
    "/shell.js",
]


def _expand(patterns):
    seen = set()
    out = []
    for pat in patterns:
        for path in sorted(ROOT.glob(pat)):
            if path.is_file() and path not in seen:
                seen.add(path)
                out.append(path)
    return out


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    with TestClient(app) as c:
        yield c


def test_no_em_dashes():
    files = _expand(EM_DASH_PATTERNS)
    assert files
    offenders = [
        str(p.relative_to(ROOT))
        for p in files
        if "\u2014" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Em dashes found in: {offenders}"


def test_no_secrets_committed():
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True
    ).splitlines()
    assert tracked
    env_files = [f for f in tracked if f == ".env" or f.endswith("/.env")]
    assert not env_files, f".env tracked in git: {env_files}"
    key_files = [
        f for f in tracked if f == "var/keys" or f.startswith("var/keys/")
    ]
    assert not key_files, f"var/keys tracked in git: {key_files}"
    priv_field = '"' + "d" + '":'
    crv_re = re.compile(r'"crv"\s*:\s*"Ed' + r'25519"')
    jwk_leaks = []
    for rel in tracked:
        fp = ROOT / rel
        if not fp.is_file():
            continue
        text = fp.read_text(encoding="utf-8", errors="ignore")
        if priv_field in text and crv_re.search(text):
            jwk_leaks.append(rel)
    assert not jwk_leaks, f"Private Ed25519 JWK found in: {jwk_leaks}"


def test_markdown_links_resolve():
    md_files = _expand(MD_PATTERNS)
    assert md_files
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    missing = []
    checked = 0
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        cleaned = re.sub(r"```[\s\S]*?```", "", text)
        cleaned = re.sub(r"`[^`]*`", "", cleaned)
        for match in link_re.finditer(cleaned):
            target = match.group(1).strip().strip("<>").split()[0]
            if target.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            checked += 1
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                missing.append(f"{md.relative_to(ROOT)} -> {target}")
    assert checked > 0
    assert not missing, f"Broken markdown links: {missing}"


def test_public_and_ui_routes_serve_200(client):
    for route in PUBLIC_AND_UI_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} returned {resp.status_code}"
        assert resp.content, f"{route} returned empty body"
