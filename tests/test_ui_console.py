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


def test_ids_present():
    html = (W / "console.html").read_text()
    for i in IDS:
        assert f'id="{i}"' in html, i


def test_trace_tools():
    js = (W / "console.js").read_text()
    for t in ["parse_request", "get_offers", "request_quote", "place_order", "/v1/audit"]:
        assert t in js, t
