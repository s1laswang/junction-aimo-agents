import re
from pathlib import Path

W = Path(__file__).resolve().parent.parent / "web"
FILES = ["console.html", "console.js", "console.css"]
IDS = ["text", "run", "scenarios", "result", "trace", "customer"]


def test_files_exist():
    for f in FILES:
        assert (W / f).stat().st_size > 0


def test_no_emoji():
    for f in FILES:
        assert not re.search("[\U0001F000-\U0001FAFF☀-➿]", (W / f).read_text()), f


def test_no_em_dash_or_gradient():
    for f in FILES:
        t = (W / f).read_text(encoding="utf-8")
        assert "\u2014" not in t, f
        assert "gradient" not in t.lower(), f


def test_ids_present():
    html = (W / "console.html").read_text()
    for i in IDS:
        assert f'id="{i}"' in html, i


def test_shared_shell_and_nav():
    html = (W / "console.html").read_text(encoding="utf-8")
    assert '/style.css' in html
    assert '/shell.js' in html
    assert 'aria-current="page">Console</a>' in html
    assert not re.search(r'<script[^>]+src=["\']?(?:https?:)?//', html, re.I)


def test_trace_tools():
    js = (W / "console.js").read_text()
    for t in ["parse_request", "get_offers", "request_quote", "place_order", "/v1/audit"]:
        assert t in js, t


def test_mock_and_demo_wiring():
    js = (W / "console.js").read_text(encoding="utf-8")
    assert "URLSearchParams(location.search)" in js
    assert '.has("mock")' in js
    assert '.has("demo")' in js
    assert "/v1/admin/simulate" in js
    assert "Ed25519" in js
    assert 'id="copy"' in js and "clipboard.writeText" in js
    assert "/audit.html?order_id=" in js
