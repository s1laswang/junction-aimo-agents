from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from aimo.app import app

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
EXPECTED_CSS = ["style.css", "console.css", "inbox.css", "audit.css", "admin.css"]
PAGE_CSS = ["console.css", "inbox.css", "audit.css", "admin.css"]
CANONICAL_TOKENS = {
    "--paper": "\x23f5f4f0",
    "--sheet": "\x23ffffff",
    "--well": "\x23efede8",
    "--valio": "\x231f4e8c",
    "--ok": "\x232f6b4f",
    "--warn": "\x238f6412",
    "--stop": "\x23a3342b",
}
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2705\u274C\u2B55]"
)
PROP_RE = re.compile(r"^(--[a-zA-Z0-9_-]+|-?[a-zA-Z][a-zA-Z0-9_-]*)$")
GRAD_WORD = "grad" + "ient"
COMMENT_OPEN = "/" + "*"
COMMENT_CLOSE = "*" + "/"
LINE_COMMENT = "/" + "/"


def _all_css_sources():
    sources = []
    for path in sorted(WEB.glob("*.css")):
        sources.append((path.name, path.read_text(encoding="utf-8")))
    for html_path in sorted(WEB.glob("*.html")):
        html = html_path.read_text(encoding="utf-8")
        for idx, match in enumerate(
            re.finditer(r"<style\b[^>]*>([\s\S]*?)</style>", html, re.I)
        ):
            sources.append((f"{html_path.name}:style[{idx}]", match.group(1)))
    return sources


def _strip_strings(css):
    out = []
    i = 0
    n = len(css)
    quote = None
    while i < n:
        ch = css[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")
        else:
            if ch in ('"', "'"):
                quote = ch
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    assert quote is None, "Unclosed string literal in CSS"
    return "".join(out)


def _iter_blocks(css):
    masked = _strip_strings(css)
    blocks = []
    stack = []
    start = 0
    for idx, ch in enumerate(masked):
        if ch == ";" and not stack:
            stmt = css[start : idx + 1].strip()
            if stmt:
                blocks.append(("stmt", stmt, ""))
            start = idx + 1
        elif ch == "{":
            header = css[start:idx].strip()
            stack.append((header, idx + 1, False))
            if len(stack) > 1:
                p_hdr, p_body_start, _ = stack[-2]
                stack[-2] = (p_hdr, p_body_start, True)
            start = idx + 1
        elif ch == "}":
            assert stack, f"Unexpected closing brace at offset {idx}"
            header, body_start, has_nested = stack.pop()
            body = css[body_start:idx]
            blocks.append(("nested" if has_nested else "leaf", header, body))
            start = idx + 1
    assert not stack, f"Unclosed brace for selector: {stack[-1][0]}"
    trailing = css[start:].strip()
    assert not trailing, f"Trailing text outside CSS block: {trailing!r}"
    return blocks


def test_expected_css_files_exist_and_nonempty():
    for name in EXPECTED_CSS:
        path = WEB / name
        assert path.is_file(), f"Missing {name}"
        assert path.stat().st_size > 0, f"Empty {name}"
    sources = _all_css_sources()
    assert len(sources) >= len(EXPECTED_CSS)
    if "<style" in (WEB / "index.html").read_text(encoding="utf-8").lower():
        assert any(label.startswith("index.html:style[") for label, _ in sources)


def test_balanced_braces_parens_and_no_empty_blocks():
    for label, css in _all_css_sources():
        masked = _strip_strings(css)
        assert masked.count("{") == masked.count("}"), f"Unbalanced braces in {label}"
        assert masked.count("(") == masked.count(")"), f"Unbalanced parens in {label}"
        brace_depth = 0
        paren_depth = 0
        for ch in masked:
            if ch == "{":
                brace_depth += 1
                assert brace_depth <= 2, f"Excessive nesting depth in {label}"
            elif ch == "}":
                brace_depth -= 1
                assert brace_depth >= 0, f"Negative brace depth in {label}"
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                assert paren_depth >= 0, f"Negative paren depth in {label}"
        assert brace_depth == 0, label
        assert paren_depth == 0, label
        assert not re.search(r"\{\s*\}", masked), f"Empty rule block in {label}"


def test_css_syntax_and_declarations_valid():
    for label, css in _all_css_sources():
        blocks = _iter_blocks(css)
        assert blocks, f"No CSS rules parsed in {label}"
        for kind, header, body in blocks:
            if kind == "stmt":
                assert header.startswith("@import "), f"Unexpected statement in {label}: {header}"
                continue
            assert header, f"Missing selector in {label}"
            assert not header.startswith(",") and not header.endswith(","), (
                f"Malformed selector list in {label}: {header}"
            )
            assert ";" not in _strip_strings(header), (
                f"Stray semicolon in selector in {label}: {header}"
            )
            if kind == "nested":
                assert header.startswith(("@media", "@keyframes", "@supports", "@layer", "@container")), (
                    f"Unexpected nested block in {label}: {header}"
                )
                continue
            masked_body = _strip_strings(body)
            assert ";;" not in masked_body, f"Double semicolon in {label}: {header}"
            parts = []
            cur_start = 0
            p_depth = 0
            for idx, ch in enumerate(masked_body):
                if ch == "(":
                    p_depth += 1
                elif ch == ")":
                    p_depth -= 1
                elif ch == ";" and p_depth == 0:
                    parts.append(body[cur_start:idx].strip())
                    cur_start = idx + 1
            tail = body[cur_start:].strip()
            if tail:
                parts.append(tail)
            assert parts, f"No declarations in {label}: {header}"
            seen_exact = set()
            for decl in parts:
                assert ":" in decl, f"Invalid declaration in {label} ({header}): {decl}"
                prop, val = decl.split(":", 1)
                prop = prop.strip()
                val = val.strip()
                assert PROP_RE.match(prop), f"Invalid property {prop!r} in {label} ({header})"
                assert val, f"Empty value for {prop!r} in {label} ({header})"
                assert "{" not in val and "}" not in val, f"Brace leak in {label} ({header})"
                if label in PAGE_CSS:
                    norm_val = " ".join(val.lower().split())
                    norm = f"{prop.lower()}:{norm_val}"
                    assert norm not in seen_exact, (
                        f"Duplicate declaration {norm!r} in {label} ({header})"
                    )
                    seen_exact.add(norm)


def test_zero_comments_dashes_emojis_and_fills():
    for label, css in _all_css_sources():
        assert COMMENT_OPEN not in css and COMMENT_CLOSE not in css, (
            f"CSS comment found in {label}"
        )
        masked = _strip_strings(css)
        assert LINE_COMMENT not in masked, f"Line comment token found in {label}"
        assert "\u2014" not in css, f"Em dash found in {label}"
        assert "\u2013" not in css, f"En dash found in {label}"
        assert not EMOJI_RE.search(css), f"Emoji found in {label}"
        assert GRAD_WORD not in css.lower(), f"Forbidden {GRAD_WORD} found in {label}"


def test_zero_external_urls_except_google_fonts_in_style():
    url_re = re.compile(r"url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)", re.I)
    scheme_re = re.compile(r"(?:https?:)?/[/'\"a-zA-Z0-9._-]+")
    for label, css in _all_css_sources():
        urls = url_re.findall(css)
        raw_http = re.findall(r"https?://[^'\"\s)]+", css, re.I)
        proto_rel = [m for m in scheme_re.findall(css) if m.startswith("/" + "/")]
        all_ext = [
            u
            for u in (urls + raw_http + proto_rel)
            if u.startswith(("http://", "https://", "/" + "/"))
        ]
        if label == "style.css":
            assert all(
                u.startswith(("https://fonts.googleapis.com/", "https://fonts.gstatic.com/"))
                for u in all_ext
            ), f"Disallowed external URL in style.css: {all_ext}"
        else:
            assert not all_ext, f"External URL found in {label}: {all_ext}"


def test_design_token_consistency_and_header_parity():
    style_css = (WEB / "style.css").read_text(encoding="utf-8")
    for token, expected_hex in CANONICAL_TOKENS.items():
        m = re.search(rf"{re.escape(token)}\s*:\s*([^;}}]+)", style_css)
        assert m, f"Token {token} missing in style.css"
        assert m.group(1).strip().lower() == expected_hex, (
            f"Token {token} in style.css is {m.group(1)!r}, expected {expected_hex!r}"
        )

    for label, css in _all_css_sources():
        if label == "style.css":
            continue
        for token, expected_hex in CANONICAL_TOKENS.items():
            for m in re.finditer(rf"{re.escape(token)}\s*:\s*([^;}}]+)", css):
                actual = m.group(1).strip().lower()
                assert actual == expected_hex, (
                    f"Token drift for {token} in {label}: {actual!r} != {expected_hex!r}"
                )
        if ":root" in css:
            assert ":where(:root)" in css, f"{label} must use :where(:root) so style.css wins"

    console_css = (WEB / "console.css").read_text(encoding="utf-8")
    assert "14px/1.45 var(--sans)" not in console_css
    for kind, header, _ in _iter_blocks(console_css):
        if kind == "leaf":
            selectors = [s.strip() for s in header.split(",")]
            for unscoped in (".tag", ".tag.ok", ".tag.warn", ".tag.stop", "body"):
                assert unscoped not in selectors, (
                    f"console.css must not globally override {unscoped!r}"
                )

    for name in PAGE_CSS:
        css = (WEB / name).read_text(encoding="utf-8")
        for kind, header, _ in _iter_blocks(css):
            if kind == "leaf" and ".top" in header:
                assert name == "audit.css", f"Unexpected .top override in {name}: {header}"


def test_reduced_motion_and_mobile_breakpoints():
    for name in EXPECTED_CSS:
        css = (WEB / name).read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in css, f"Missing prefers-reduced-motion in {name}"
        assert re.search(r"@media[^{]*max-width\s*:\s*640px", css), (
            f"Missing mobile max-width:640px media query in {name}"
        )
        assert not re.search(
            r"(?:^|\})\s*(?:html|body)\s*\{[^}]*overflow-x\s*:\s*hidden", css
        ), f"{name} must not set overflow-x:hidden on html/body (breaks sticky .top)"


def test_page_specific_polish_and_print_rules():
    console_css = (WEB / "console.css").read_text(encoding="utf-8")
    for sel in [".sec details", ".tw details", ".quote details", ".run .ro.auto", ".run .ro.needs_approval", ".run .ro.blocked"]:
        assert sel in console_css, f"Missing {sel} in console.css"

    inbox_css = (WEB / "inbox.css").read_text(encoding="utf-8")
    assert re.search(r"\.subs\s+li\s*\{[^}]*flex-wrap\s*:\s*wrap", inbox_css)
    assert re.search(
        r"\.act\.primary:hover:not\(:disabled\)\s*\{[^}]*background\s*:\s*\x231a4377",
        inbox_css,
    )
    assert re.search(
        r"\.act\.danger\s*\{[^}]*border-color\s*:\s*rgba\(163,52,43,\.4\)",
        inbox_css,
    )
    assert re.search(
        r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{[\s\S]*?transform\s*:\s*none",
        inbox_css,
    )

    audit_css = (WEB / "audit.css").read_text(encoding="utf-8")
    assert "@page" in audit_css and "A4" in audit_css
    assert "@media print" in audit_css
    assert ".titlebar .badge+*" not in audit_css
    assert re.search(r"\.receipt[^{]*\{[^}]*overflow-wrap\s*:\s*anywhere", audit_css)
    assert re.search(r"\.how[^{]*\{[^}]*overflow-wrap\s*:\s*anywhere", audit_css)
    assert re.search(r"\.hashes\s+\.h\s*\{[^}]*word-break\s*:\s*break-all", audit_css)

    admin_css = (WEB / "admin.css").read_text(encoding="utf-8")
    mobile_blocks = re.findall(r"@media\s*\(\s*max-width\s*:\s*640px\s*\)", admin_css)
    assert len(mobile_blocks) == 1, f"Expected 1 merged mobile @media block in admin.css, got {len(mobile_blocks)}"
    assert ".panel.form+.panel.form" in admin_css
    assert ".chip button:focus-visible" in admin_css


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    with TestClient(app) as c:
        yield c


def test_stylesheets_served_with_css_mime(client):
    for name in EXPECTED_CSS:
        resp = client.get(f"/{name}")
        assert resp.status_code == 200, name
        assert "text/css" in resp.headers.get("content-type", ""), name
        assert resp.text.strip(), name
