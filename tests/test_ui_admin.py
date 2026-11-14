import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ["index.html", "style.css", "shell.js", "admin.html", "admin.js", "admin.css"]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⭐⭕]")


def test_files_exist():
    for f in FILES:
        assert (WEB / f).is_file(), f


def test_no_emoji_or_em_dash():
    for f in FILES:
        t = (WEB / f).read_text()
        assert not EMOJI.search(t), f
        assert "—" not in t, f


def test_no_external_scripts():
    for f in ["index.html", "admin.html"]:
        for src in re.findall(r'<script[^>]+src="([^"]+)"', (WEB / f).read_text()):
            assert src.startswith("/"), src
    css = (WEB / "style.css").read_text()
    urls = re.findall(r"https?://[^\"')\s]+", css)
    assert all(u.startswith("https://fonts.googleapis.com/") for u in urls)


def test_tokens_and_header():
    css = (WEB / "style.css").read_text()
    for t in ["--paper:#F5F4F0", "--valio:#1F4E8C", "--stop:#A3342B", "prefers-reduced-motion", "table.ledger"]:
        assert t in css
    assert 'aria-current="page"' in (WEB / "admin.html").read_text()


def test_mock_approvers_match_mandates():
    js = (WEB / "admin.js").read_text()
    for name in ["Laura Virtanen", "Mikko Heikkinen", "Helena Korhonen", "Antti Lahtinen", "Sari Nieminen"]:
        assert name in js
    for stale in ["head.chef", "kitchen.manager", "dietitian", "school.cook"]:
        assert stale not in js
    assert "Limits must be greater than zero." in js
