from __future__ import annotations

import asyncio
import functools
import http.server
import io
import json
import os
import pathlib
import re
import socketserver
import threading
import tokenize

import pytest
from playwright.sync_api import sync_playwright

from aimo.gateway.mcp_server import build_mcp

SHARED_PW = pathlib.Path("/tmp/fleet-root/shared/playwright")
if SHARED_PW.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARENA_DIR = ROOT / "docs" / "arena"
ARENA_HTML = ARENA_DIR / "index.html"
ARENA_CSS = ARENA_DIR / "style.css"
ARENA_JS = ARENA_DIR / "game.js"
MANIFEST_JSON = ARENA_DIR / "assets" / "manifest.json"
LIVE_QUOTE_JSON = ROOT / "docs" / "evidence" / "live-run" / "quote.json"
LIVE_JWKS_JSON = ROOT / "docs" / "evidence" / "live-run" / "jwks.json"
LIVE_AUDIT_JSON = ROOT / "docs" / "evidence" / "live-run" / "audit-events.json"

EM_DASH = chr(0x2014)
HASH_CHAR = chr(35)


class _QuietArenaHandler(http.server.SimpleHTTPRequestHandler):
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
def local_arena_server():
    handler = functools.partial(_QuietArenaHandler, directory=str(ROOT))
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


def test_arena_files_hygiene_links_and_zero_comments_em_dashes():
    for path in (ARENA_HTML, ARENA_CSS, ARENA_JS):
        assert path.is_file(), f"Missing arena file: {path}"
        raw = path.read_text(encoding="utf-8")
        assert len(raw.encode("utf-8")) > 2_000
        assert len(raw.encode("utf-8")) < 500_000
        for forbidden_tok in ("<!--", "-->", "/*", "*/", "//"):
            assert forbidden_tok not in raw, f"Forbidden comment token {forbidden_tok!r} in {path}"
        assert EM_DASH not in raw, f"Em dash found in {path}"
        assert re.findall(r"\b(?:TODO|FIXME)\b", raw, flags=re.IGNORECASE) == []

    html_raw = ARENA_HTML.read_text(encoding="utf-8")
    assert re.findall(r"""(?:src|href)\s*=\s*["']https?:""", html_raw, flags=re.IGNORECASE) == []

    expected_hrefs = (
        "style.css",
        "../demo/index.html",
        "../demo/ASSETS.md",
        "assets/manifest.json",
        "../../README.md",
        "../SUBMISSION.md",
        "../evidence/live-run/README.md",
        "../NUMBERS.md",
    )
    for rel_href in expected_hrefs:
        assert f'href="{rel_href}"' in html_raw, f"Missing link {rel_href!r} in docs/arena/index.html"
        resolved = (ARENA_DIR / rel_href).resolve()
        assert resolved.is_relative_to(ROOT.resolve())
        assert resolved.is_file() and resolved.stat().st_size > 0, f"Unresolved link {resolved}"

    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    js_raw = ARENA_JS.read_text(encoding="utf-8")
    for group in ("characters", "tiles", "icons"):
        for key in manifest[group]:
            assert f'"{key}"' in js_raw, f"Sprite key {group}.{key} missing from docs/arena/game.js"

    mcp_tools = [t.name for t in asyncio.run(build_mcp().list_tools())]
    assert len(mcp_tools) == 13
    for tool_name in mcp_tools:
        assert f'"{tool_name}"' in js_raw, f"MCP tool {tool_name!r} missing from docs/arena/game.js"


def test_arena_interactive_stage_scams_crypto_and_scoreboard_over_http(local_arena_server):
    page_errors: list[str] = []
    console_errors: list[str] = []
    external_requests: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 920})
        page = context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _guard_routes(route):
            url = route.request.url
            if url.startswith(local_arena_server) or url.startswith("data:"):
                route.continue_()
            else:
                external_requests.append(url)
                route.abort()

        page.route("**/*", _guard_routes)
        resp = page.goto(f"{local_arena_server}/docs/arena/index.html", wait_until="load")
        assert resp is not None and resp.status == 200

        page.wait_for_function(
            """() => document.getElementById('sprite-mode-badge').textContent.includes('24/24')"""
        )
        assert "13 MCP TOOLS | 0 APPROVE TOOLS" in page.locator('[data-testid="mcp-surface-badge"]').inner_text()
        assert page.locator('[id="proof-mcp-tools"] .mcp-chip').count() == 13

        canvas_check = page.evaluate(
            """() => {
                const c = document.getElementById('warehouse-canvas');
                const ctx = c.getContext('2d');
                const data = ctx.getImageData(0, 0, c.width, c.height).data;
                const colors = new Set();
                for (let i = 0; i < data.length; i += 64) {
                    colors.add(data[i] + ',' + data[i + 1] + ',' + data[i + 2]);
                }
                return { width: c.width, height: c.height, distinctColors: colors.size };
            }"""
        )
        assert canvas_check["width"] == 800
        assert canvas_check["height"] == 432
        assert canvas_check["distinctColors"] >= 8

        assert "Beat 1 /" in page.locator('[data-testid="step-counter"]').inner_text()
        page.locator('[data-testid="btn-next"]').click()
        assert "Beat 2 /" in page.locator('[data-testid="step-counter"]').inner_text()
        page.locator('[data-testid="btn-prev"]').click()
        assert "Beat 1 /" in page.locator('[data-testid="step-counter"]').inner_text()

        page.locator('[data-testid="btn-speed"]').click()
        assert "2x Speed" in page.locator('[data-testid="btn-speed"]').inner_text()
        page.locator('[data-testid="btn-speed"]').click()
        assert "4x Speed" in page.locator('[data-testid="btn-speed"]').inner_text()

        page.locator('[data-testid="warehouse-canvas"]').click()
        assert "Beat 2 /" in page.locator('[data-testid="step-counter"]').inner_text()
        page.locator('[data-testid="btn-reset"]').click()
        assert "Beat 1 /" in page.locator('[data-testid="step-counter"]').inner_text()

        page.locator('[data-testid="btn-verify-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )
        page.locator('[data-testid="btn-tamper-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('TAMPER DETECTED')"""
        )
        assert "TAMPER DETECTED" in page.locator('[data-testid="proof-jws-badge"]').inner_text()
        page.locator('[data-testid="btn-restore-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )

        page.locator('[data-testid="btn-verify-chain"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-banner').textContent.includes('Audit chain intact: ok=true')"""
        )
        page.locator('[data-testid="btn-tamper-chain"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-banner').textContent.includes('TAMPER DETECTED')"""
        )
        assert page.locator('[id="proof-audit-list"] li.tampered').count() >= 1
        page.locator('[data-testid="btn-restore-chain"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-banner').textContent.includes('Audit chain intact: ok=true')"""
        )
        assert page.locator('[id="proof-audit-list"] li.tampered').count() == 0

        scam_ids = (
            "price_mid_approval",
            "slip_allergen",
            "fake_approver",
            "prompt_injection",
            "unknown_customer",
        )
        for scam_id in scam_ids:
            page.locator(f'[data-testid="scenario-pill-{scam_id}"]').click()
            page.locator('[data-testid="mode-naive"]').click()
            assert "COMPROMISED" in page.locator('[data-testid="verdict-banner"]').inner_text()
            page.locator('[data-testid="btn-compare-both"]').click()
            assert page.locator('[data-testid="compare-toast"]').is_visible()

            if scam_id == "prompt_injection":
                assert "SUSPICIOUS" in page.locator('[data-testid="proof-plan-lines"]').inner_text()
            if scam_id == "slip_allergen":
                assert "school-tampere" in page.locator('[data-testid="active-customer-pill"]').inner_text()
            if scam_id == "unknown_customer":
                assert "shadow-bistro-99" in page.locator('[data-testid="active-customer-pill"]').inner_text()

        assert page.locator('[data-testid="score-scams-tried"]').inner_text() == "5"
        assert page.locator('[data-testid="score-gateway-compromised"]').inner_text() == "0"
        assert page.locator('[data-testid="score-gateway-blocked"]').inner_text() == "5"
        assert page.locator('[data-testid="score-naive-compromised"]').inner_text() == "5"

        wt_text = page.locator('[data-testid="wt-benchmark-table"]').inner_text()
        for expected_metric in ("0 violations", "372 violations", "193 allergen violations", "80 / 80 flagged"):
            assert expected_metric in wt_text

        context.close()
        browser.close()

    assert external_requests == [], f"Unexpected external requests: {external_requests}"
    assert page_errors == [], f"Uncaught page errors: {page_errors}"
    assert console_errors == [], f"Browser console errors: {console_errors}"


def test_arena_consumes_external_scenarios_js_contract(local_arena_server):
    quote_obj = json.loads(LIVE_QUOTE_JSON.read_text(encoding="utf-8"))
    jwks_obj = json.loads(LIVE_JWKS_JSON.read_text(encoding="utf-8"))
    audit_obj = json.loads(LIVE_AUDIT_JSON.read_text(encoding="utf-8"))
    mcp_tools = [t.name for t in asyncio.run(build_mcp().list_tools())]

    mock_scenarios_payload = {
        "schema_version": 1,
        "jwks": jwks_obj,
        "mcp_tool_count": 13,
        "mcp_tools": mcp_tools,
        "audit_chain": audit_obj,
        "scenarios": [
            {
                "id": "happy_path",
                "customer_id": "hotel-aurora",
                "quotes": [quote_obj],
                "audit_events": audit_obj,
                "steps": [
                    {
                        "step": 1,
                        "id": "plan_milk",
                        "actor": "buyer_agent",
                        "method": "POST",
                        "path": "/v1/plan",
                        "http_status": 200,
                        "label": "Recorded Plan 1,000 L Milk",
                        "highlight": "Recorded scenarios.js contract active",
                        "response_body": {"selected": "464306"},
                    }
                ],
            }
        ],
    }
    js_body = (
        "window.AIMO_ARENA_DATA = "
        + json.dumps(mock_scenarios_payload)
        + ";\nwindow.ARENA_SCENARIOS = window.AIMO_ARENA_DATA.scenarios;\n"
    )

    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 840})
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _route_handler(route):
            url = route.request.url
            if url.endswith("/docs/arena/scenarios.js"):
                route.fulfill(status=200, content_type="application/javascript", body=js_body)
            elif url.startswith(local_arena_server) or url.startswith("data:"):
                route.continue_()
            else:
                route.abort()

        page.route("**/*", _route_handler)
        page.goto(f"{local_arena_server}/docs/arena/index.html", wait_until="load")

        assert "SCENARIOS.JS" in page.locator('[data-testid="fixture-source-badge"]').inner_text()
        assert "Recorded scenarios.js contract active" in page.locator('[data-testid="step-narrative"]').inner_text()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )

        browser.close()

    assert page_errors == []
    assert console_errors == []


def test_arena_works_offline_over_file_uri():
    page_errors: list[str] = []
    external_requests: list[str] = []
    file_uri = ARENA_HTML.resolve().as_uri()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 820})
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def _block_remote(route):
            url = route.request.url
            if url.startswith("file://") or url.startswith("data:"):
                route.continue_()
            else:
                external_requests.append(url)
                route.abort()

        page.route("**/*", _block_remote)
        page.goto(file_uri, wait_until="domcontentloaded")

        page.locator('[data-testid="scenario-pill-fake_approver"]').click()
        page.locator('[data-testid="btn-compare-both"]').click()
        assert page.locator('[data-testid="score-gateway-blocked"]').inner_text() == "1"
        assert page.locator('[data-testid="score-gateway-compromised"]').inner_text() == "0"

        page.locator('[data-testid="btn-tamper-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('TAMPER DETECTED')"""
        )
        page.locator('[data-testid="btn-restore-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('quote-verify-banner').textContent.includes('VERIFIED')"""
        )

        browser.close()

    assert external_requests == []
    assert page_errors == []


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
