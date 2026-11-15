from __future__ import annotations

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

SHARED_PW = pathlib.Path("/tmp/fleet-root/shared/playwright")
if SHARED_PW.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_HTML = ROOT / "docs" / "demo" / "index.html"
LIVE_RUN_DIR = ROOT / "docs" / "evidence" / "live-run"
EM_DASH = chr(0x2014)
HASH_CHAR = chr(35)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class _ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture(scope="module")
def local_demo_server():
    handler = functools.partial(_QuietHandler, directory=str(ROOT))
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


def test_demo_html_hygiene_zero_comments_em_dashes_todos_and_links():
    assert DEMO_HTML.is_file(), f"Missing {DEMO_HTML}"
    raw = DEMO_HTML.read_text(encoding="utf-8")
    assert len(raw.encode("utf-8")) > 10_000
    assert len(raw.encode("utf-8")) < 500_000

    for forbidden_comment_tok in ("<!--", "-->", "/*", "*/", "//"):
        assert forbidden_comment_tok not in raw, f"Comment token {forbidden_comment_tok!r} found in {DEMO_HTML}"

    assert EM_DASH not in raw, "Em dash found in docs/demo/index.html"
    assert re.findall(r"\b(?:TODO|FIXME)\b", raw, flags=re.IGNORECASE) == []
    assert re.findall(r"""(?:src|href)\s*=\s*["']https?:""", raw, flags=re.IGNORECASE) == []

    required_links = (
        "../arena/index.html",
        "./ASSETS.md",
        "../../README.md",
        "../SUBMISSION.md",
        "../evidence/live-run/README.md",
        "../NUMBERS.md",
    )
    pending_arena = (ROOT / "docs" / "arena" / "index.html").resolve()
    for rel_href in required_links:
        assert f'href="{rel_href}"' in raw, f"Missing required header link {rel_href!r}"
        resolved = (DEMO_HTML.parent / rel_href).resolve()
        assert resolved.is_relative_to(ROOT.resolve())
        if resolved == pending_arena and not resolved.exists():
            continue
        assert resolved.is_file() and resolved.stat().st_size > 0, f"Unresolved link target: {resolved}"

    quote_disk = json.loads((LIVE_RUN_DIR / "quote.json").read_text(encoding="utf-8"))
    order_disk = json.loads((LIVE_RUN_DIR / "order.json").read_text(encoding="utf-8"))
    jwks_disk = json.loads((LIVE_RUN_DIR / "jwks.json").read_text(encoding="utf-8"))
    events_disk = json.loads((LIVE_RUN_DIR / "audit-events.json").read_text(encoding="utf-8"))

    for required_token in (
        "One-Page Judge Walkthrough",
        "Try to Scam the Agent",
        "run-b9690772fe",
        quote_disk["quote_id"],
        quote_disk["quote_hash"],
        order_disk["order_id"],
        jwks_disk["keys"][0]["kid"],
        jwks_disk["keys"][0]["x"],
        events_disk[0]["hash"],
        events_disk[2]["hash"],
        events_disk[5]["hash"],
        "8/8",
        "92",
        "44/44",
        "0 vs 372",
        "0 vs 193",
        "80/80",
        "13 MCP",
    ):
        assert required_token in raw, f"docs/demo/index.html missing required artifact/scorecard token {required_token!r}"


def test_demo_html_interactive_walkthrough_over_http(local_demo_server):
    page_errors: list[str] = []
    console_errors: list[str] = []
    external_requests: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1360, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _guard_routes(route):
            url = route.request.url
            if url.startswith(local_demo_server) or url.startswith("data:"):
                route.continue_()
            else:
                external_requests.append(url)
                route.abort()

        page.route("**/*", _guard_routes)
        resp = page.goto(f"{local_demo_server}/docs/demo/index.html", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200

        assert "Step 1 / 7" in page.locator('[id="walk-step-indicator"]').inner_text()
        page.locator('[id="btn-next-step"]').click()
        assert "Step 2 / 7" in page.locator('[id="walk-step-indicator"]').inner_text()
        page.locator('[id="btn-prev-step"]').click()
        assert "Step 1 / 7" in page.locator('[id="walk-step-indicator"]').inner_text()
        page.locator('[id="step-tab-4"]').click()
        assert "Step 4 / 7" in page.locator('[id="walk-step-indicator"]').inner_text()

        page.locator('[id="btn-probe-qty"]').click()
        assert "422" in page.locator('[id="guardrail-probe-output"]').inner_text()
        page.locator('[id="btn-probe-allergen"]').click()
        assert "school-tampere" in page.locator('[id="guardrail-probe-output"]').inner_text()
        page.locator('[id="btn-probe-customer"]').click()
        assert "unknown_customer" in page.locator('[id="guardrail-probe-output"]').inner_text()

        inj_box = page.locator('[id="injection-screen-output"]')
        assert '"suspicious": false' in inj_box.inner_text()
        page.locator('[id="btn-toggle-injection"]').click()
        assert '"suspicious": true' in inj_box.inner_text()
        assert "80/80" in inj_box.inner_text()

        page.locator('[id="btn-verify-jws"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('VERIFIED')"""
        )
        jws_text = page.locator('[id="jws-verify-output"]').inner_text()
        assert "Q-20261115-964294b6" in jws_text
        assert "6def7dae163f23324fb97037000ac9ab7fa7276971eead120069e09f3270fbda" in jws_text
        assert "CIbMjdup7xejhizQv2eS1sKdEZ_rIOdetD4U1D-Xavc" in jws_text

        page.locator('[id="btn-tamper-quote"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('FAILED')"""
        )
        page.locator('[id="btn-verify-jws"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('VERIFIED')"""
        )

        assert page.locator('[id="attacks-blocked-badge"]').inner_text() == "0 attacks blocked"
        page.locator('[id="btn-attack-403"]').click()
        attack_text = page.locator('[id="attack-403-output"]').inner_text()
        assert "403" in attack_text
        assert "agent_cannot_approve" in attack_text
        assert "approval.refused" in attack_text
        assert page.locator('[id="attacks-blocked-badge"]').inner_text() == "1 attack blocked"

        page.locator('[id="btn-requote-409"]').click()
        requote_text = page.locator('[id="requote-409-output"]').inner_text()
        assert "409" in requote_text
        assert "requote_needed" in requote_text
        assert "EUR 1,010.00" in requote_text
        assert "EUR 1,250.00" in requote_text
        assert "partial_stock" in requote_text
        assert "order.requoted" in requote_text

        page.locator('[id="btn-human-signin"]').click()
        assert "X-Approver-Session" in page.locator('[id="approver-session-chip"]').inner_text()
        page.locator('[id="btn-human-approve"]').click()
        approve_text = page.locator('[id="human-approve-output"]').inner_text()
        assert "ORD-20261115-06eda13d" in approve_text
        assert "placed" in approve_text
        assert "AP" in approve_text
        xml_preview = page.locator('[id="peppol-xml-preview"]')
        assert xml_preview.is_visible()
        assert "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>" in xml_preview.inner_text()

        page.locator('[id="btn-audit-verify"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )
        assert page.locator('[id="audit-events-tbody"] tr').count() == 6

        page.locator('[id="btn-audit-tamper"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain broken / hash mismatch')"""
        )
        assert "event" in page.locator('[id="audit-verify-output"]').inner_text().lower()

        page.locator('[id="btn-audit-restore"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )

        page.locator('[id="btn-reset-all"]').click()
        assert page.locator('[id="attacks-blocked-badge"]').inner_text() == "0 attacks blocked"
        assert "Signed out" in page.locator('[id="approver-session-chip"]').inner_text()
        assert not page.locator('[id="peppol-xml-preview"]').is_visible()

        page.locator('[id="btn-auto-walk"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )
        assert "VERIFIED" in page.locator('[id="jws-verify-output"]').inner_text()
        assert page.locator('[id="attacks-blocked-badge"]').inner_text() == "1 attack blocked"
        assert "placed" in page.locator('[id="human-approve-output"]').inner_text()

        context.close()
        browser.close()

    assert external_requests == [], f"Unexpected external network requests: {external_requests}"
    assert page_errors == [], f"Uncaught page errors: {page_errors}"
    assert console_errors == [], f"Console errors: {console_errors}"


def test_demo_html_works_offline_over_file_uri():
    page_errors: list[str] = []
    external_requests: list[str] = []
    file_uri = DEMO_HTML.resolve().as_uri()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
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

        page.locator('[id="btn-verify-jws"]').click()
        page.wait_for_function(
            """() => document.getElementById('jws-verify-output').textContent.includes('VERIFIED')"""
        )
        page.locator('[id="btn-attack-403"]').click()
        assert "agent_cannot_approve" in page.locator('[id="attack-403-output"]').inner_text()
        page.locator('[id="btn-requote-409"]').click()
        assert "requote_needed" in page.locator('[id="requote-409-output"]').inner_text()
        page.locator('[id="btn-human-approve"]').click()
        assert "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>" in page.locator('[id="peppol-xml-preview"]').inner_text()

        page.locator('[id="btn-audit-tamper"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain broken / hash mismatch')"""
        )
        page.locator('[id="btn-audit-restore"]').click()
        page.wait_for_function(
            """() => document.getElementById('audit-verify-output').textContent.includes('Chain intact')"""
        )

        browser.close()

    assert external_requests == [], f"Unexpected external requests on file URI: {external_requests}"
    assert page_errors == [], f"Uncaught page errors on file URI: {page_errors}"


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
