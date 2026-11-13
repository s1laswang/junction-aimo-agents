import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ["audit.html", "audit.js", "audit.css"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")


def test_files_exist():
    for f in FILES:
        assert (WEB / f).stat().st_size > 0


def test_no_emoji_or_em_dash():
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert not EMOJI.search(t), f
        assert "—" not in t, f


def test_page_wiring():
    h = (WEB / "audit.html").read_text()
    assert '/style.css' in h and '/shell.js' in h and 'aria-current="page">Audit' in h
