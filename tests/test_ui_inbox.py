import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ["inbox.html", "inbox.js", "inbox.css"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")


def test_files_exist():
    for f in FILES:
        assert (WEB / f).stat().st_size > 0


def test_no_emoji_no_em_dash_no_gradient():
    for f in FILES:
        t = (WEB / f).read_text()
        assert not EMOJI.search(t), f
        assert "—" not in t, f
        assert "gradient" not in t, f


def test_links_shared_shell_and_mock():
    html = (WEB / "inbox.html").read_text()
    assert '/style.css' in html and '/shell.js' in html
    assert 'aria-current="page"' in html
    js = (WEB / "inbox.js").read_text()
    assert "mock=1" in js and "/v1/approvals" in js and "3000" in js


def test_error_detail_and_audit_link():
    js = (WEB / "inbox.js").read_text()
    assert "lastError" in js and "Gateway offline" in js
    assert "/audit.html?order_id=" in js
    assert "Reason for rejecting" in js


def test_mock_matches_real_system():
    js = (WEB / "inbox.js").read_text()
    assert "UNKNOWN_PRODUCT" in js and "allergen_flag" not in js
    assert "NON_PREFERRED_BRAND" in js and "Decided today" in (WEB / "inbox.html").read_text()
