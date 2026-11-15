from __future__ import annotations

import functools
import http.server
import io
import os
import pathlib
import re
import socketserver
import threading
import tokenize

import pytest
from playwright.sync_api import sync_playwright

SHARED_PW = pathlib.Path("/tmp/fleet-root/shared/playwright")
if SHARED_PW.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIEF_0800_MD = ROOT / "docs" / "BRIEF-0800.md"
DEMO_HTML = ROOT / "docs" / "demo" / "index.html"
ARENA_DIR = ROOT / "docs" / "arena"
ARENA_HTML = ARENA_DIR / "index.html"

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HASH_CHAR = chr(35)


class _QuietJudgeSurfaceHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/docs/arena/scenarios.js" and not (ARENA_DIR / "scenarios.js").is_file():
            body = b"window.__arenaScenariosMissing = true;\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return


class _ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture(scope="module")
def local_judge_server():
    handler = functools.partial(_QuietJudgeSurfaceHandler, directory=str(ROOT))
    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_brief_0800_exists_preserves_morning_brief_and_has_zero_em_dashes_or_todos():
    assert BRIEF_0800_MD.is_file(), "Missing docs/BRIEF-0800.md"
    text = BRIEF_0800_MD.read_text(encoding="utf-8")
    assert text.strip(), "Empty docs/BRIEF-0800.md"
    assert len(text) >= 1_200

    assert EM_DASH not in text, "Em dash found in docs/BRIEF-0800.md"
    assert EN_DASH not in text, "En dash found in docs/BRIEF-0800.md"
    assert re.findall(r"\b(?:TODO|FIXME)\b", text, flags=re.IGNORECASE) == []
    assert re.findall(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", text, flags=re.IGNORECASE) == []

    conflict_start = "<" * 7 + " "
    conflict_end = ">" * 7 + " "
    for idx, line in enumerate(text.splitlines(), 1):
        assert not line.startswith(conflict_start) and not line.startswith(conflict_end), (
            f"Conflict marker in docs/BRIEF-0800.md:{idx}"
        )

    for required_token in (
        "Aimo: morning brief (Sun 15 Nov, 09:00)",
        "https://github.com/s1laswang/junction-aimo-agents",
        "aimo-v1",
        "What changed overnight",
        "AIMO_DEMO_FAULTS=1",
        "99.7%",
        "69.5%",
        "docs/evidence/live-run/",
        "docs/pitch/demo-walkthrough.mp4",
        "Verified at the tagged commit",
        "375 passed, 0 failed",
        "274 passed, 0 failed",
        "Scenarios 8/8 live and in replay",
        "Human-only, before 10:00",
        "data/keys/approver_pin",
        "AIMO_NOW=2026-11-15T10:00:00+02:00",
        "gemini-3.8-flash",
        "Not done",
        "docs/arena/index.html",
        "docs/demo/index.html",
        "docs/demo-backup.mp4",
    ):
        assert required_token in text, f"docs/BRIEF-0800.md missing required token {required_token!r}"


def test_brief_0800_and_judge_surfaces_have_zero_broken_relative_links():
    raw = BRIEF_0800_MD.read_text(encoding="utf-8")
    stripped = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", "", stripped)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    root_resolved = ROOT.resolve()

    resolved_targets: set[pathlib.Path] = set()
    for raw_target in link_re.findall(stripped):
        target = raw_target.strip().split()[0]
        if target.startswith(("http://", "https://", "mailto:", "data:", HASH_CHAR)):
            continue
        clean = target.split(HASH_CHAR, 1)[0].split("?", 1)[0]
        if not clean:
            continue
        resolved = (BRIEF_0800_MD.parent / clean).resolve()
        assert resolved.is_relative_to(root_resolved), f"Link escapes repo root: {raw_target}"
        assert resolved.exists(), f"Broken relative link in docs/BRIEF-0800.md: {raw_target}"
        if resolved.is_file():
            assert resolved.stat().st_size > 0, f"Empty linked target in docs/BRIEF-0800.md: {raw_target}"
        resolved_targets.add(resolved)

    expected_brief_links = {
        (ROOT / "README.md").resolve(),
        (ROOT / "docs" / "SUBMISSION.md").resolve(),
        (ROOT / "docs" / "arena" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "index.html").resolve(),
        (ROOT / "docs" / "demo" / "ASSETS.md").resolve(),
        (ROOT / "docs" / "evidence" / "live-run" / "README.md").resolve(),
        (ROOT / "docs" / "pitch" / "demo-walkthrough.mp4").resolve(),
    }
    assert expected_brief_links.issubset(resolved_targets)

    attr_re = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", flags=re.IGNORECASE)
    pending_scenarios_js = (ARENA_DIR / "scenarios.js").resolve()
    for html_path in (DEMO_HTML, ARENA_HTML):
        assert html_path.is_file() and html_path.stat().st_size > 0
        html_raw = html_path.read_text(encoding="utf-8")
        refs = attr_re.findall(html_raw)
        assert refs, f"No href/src attributes found in {html_path}"
        for raw_ref in refs:
            assert not raw_ref.lower().startswith(("http://", "https://")), (
                f"External URL in offline surface {html_path}: {raw_ref}"
            )
            if raw_ref.startswith(("data:", "mailto:", HASH_CHAR)):
                continue
            clean_ref = raw_ref.split(HASH_CHAR, 1)[0].split("?", 1)[0]
            if not clean_ref:
                continue
            resolved_ref = (html_path.parent / clean_ref).resolve()
            assert resolved_ref.is_relative_to(root_resolved)
            if resolved_ref == pending_scenarios_js and not resolved_ref.exists():
                continue
            assert resolved_ref.is_file() and resolved_ref.stat().st_size > 0, (
                f"Broken asset/doc link in {html_path.relative_to(ROOT)}: {raw_ref}"
            )


def test_playwright_offline_navigation_across_demo_and_arena_over_http(local_judge_server):
    page_errors: list[str] = []
    console_errors: list[str] = []
    external_requests: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1380, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _guard_routes(route):
            url = route.request.url
            if url.startswith(local_judge_server) or url.startswith("data:"):
                route.continue_()
            else:
                external_requests.append(url)
                route.abort()

        page.route("**/*", _guard_routes)

        resp = page.goto(f"{local_judge_server}/docs/demo/index.html", wait_until="load")
        assert resp is not None and resp.status == 200

        page.locator('[id="btn-verify-jws"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('VERIFIED')"""
        )
        assert "VERIFIED (Ed25519)" in page.locator('[id="insp-jws"]').inner_text()
        assert "ok" in (page.locator('[id="insp-jws"]').get_attribute("class") or "")

        page.locator('[id="btn-audit-verify"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )
        assert "Chain intact (6/6)" in page.locator('[id="insp-audit"]').inner_text()
        assert "ok" in (page.locator('[id="insp-audit"]').get_attribute("class") or "")
        assert "Chain intact (6/6)" in page.locator('[id="step7-chain-pill"]').inner_text()
        assert page.locator('[id="audit-events-tbody"] .pill.ok').count() == 6

        with page.expect_navigation(wait_until="load"):
            page.locator('[id="link-arena"]').click()
        assert page.url.endswith("/docs/arena/index.html")

        page.wait_for_function(
            """() => document.getElementById('sprite-mode-badge').textContent.includes('24/24')"""
        )
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )
        page.wait_for_function(
            """() => document.getElementById('audit-verify-banner').textContent.includes('Audit chain intact: ok=true')"""
        )

        assert "Ed25519 JWS VERIFIED" in page.locator('[data-testid="proof-jws-badge"]').inner_text()
        assert "ok" in (page.locator('[data-testid="proof-jws-badge"]').get_attribute("class") or "")
        assert "Peppol AP" in page.locator('[data-testid="proof-peppol-badge"]').inner_text()
        assert "13 MCP TOOLS | 0 APPROVE TOOLS" in page.locator('[data-testid="mcp-surface-badge"]').inner_text()
        assert page.locator('[id="proof-audit-list"] li').count() >= 1
        assert page.locator('[id="proof-audit-list"] li.tampered').count() == 0
        assert "JWS+SHA256 OK" in page.locator('[id="proof-audit-list"]').inner_text()

        page.locator('[data-testid="btn-verify-quote"]').click()
        page.locator('[data-testid="btn-verify-chain"]').click()
        assert "Ed25519 JWS VERIFIED" in page.locator('[data-testid="proof-jws-badge"]').inner_text()
        assert "Audit chain intact: ok=true" in page.locator('[data-testid="audit-verify-banner"]').inner_text()

        with page.expect_navigation(wait_until="load"):
            page.locator('[data-testid="arena-nav"] a[href="../demo/index.html"]').click()
        assert page.url.endswith("/docs/demo/index.html")

        page.locator('[id="btn-auto-walk"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )
        assert "VERIFIED (Ed25519)" in page.locator('[id="insp-jws"]').inner_text()
        assert "Chain intact (6/6)" in page.locator('[id="insp-audit"]').inner_text()

        context.close()
        browser.close()

    assert external_requests == [], f"Unexpected external requests: {external_requests}"
    assert page_errors == [], f"Uncaught page errors: {page_errors}"
    assert console_errors == [], f"Browser console errors: {console_errors}"


def test_playwright_offline_navigation_across_demo_and_arena_over_file_uri():
    page_errors: list[str] = []
    console_errors: list[str] = []
    external_requests: list[str] = []
    demo_uri = DEMO_HTML.resolve().as_uri()
    scenarios_exists = (ARENA_DIR / "scenarios.js").is_file()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1320, "height": 860})
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _guard_file_routes(route):
            url = route.request.url
            if url.endswith("/docs/arena/scenarios.js") and not scenarios_exists:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="window.__arenaScenariosMissing = true;\n",
                )
            elif url.startswith("file://") or url.startswith("data:"):
                route.continue_()
            else:
                external_requests.append(url)
                route.abort()

        page.route("**/*", _guard_file_routes)
        page.goto(demo_uri, wait_until="domcontentloaded")

        page.locator('[id="btn-verify-jws"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('VERIFIED')"""
        )
        page.locator('[id="btn-audit-verify"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )
        assert "VERIFIED (Ed25519)" in page.locator('[id="insp-jws"]').inner_text()
        assert "Chain intact (6/6)" in page.locator('[id="insp-audit"]').inner_text()

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('[id="link-arena"]').click()
        assert page.url.endswith("/docs/arena/index.html")

        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )
        page.wait_for_function(
            """() => document.getElementById('audit-verify-banner').textContent.includes('Audit chain intact: ok=true')"""
        )
        assert "Ed25519 JWS VERIFIED" in page.locator('[data-testid="proof-jws-badge"]').inner_text()
        assert "JWS+SHA256 OK" in page.locator('[id="proof-audit-list"]').inner_text()

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('[data-testid="arena-nav"] a[href="../demo/index.html"]').click()
        assert page.url.endswith("/docs/demo/index.html")

        browser.close()

    assert external_requests == [], f"Unexpected external requests on file URI: {external_requests}"
    assert page_errors == [], f"Uncaught page errors on file URI: {page_errors}"
    assert console_errors == [], f"Browser console errors on file URI: {console_errors}"


def test_self_hygiene_zero_comments_and_zero_em_or_en_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert EN_DASH not in raw
    assert HASH_CHAR not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
