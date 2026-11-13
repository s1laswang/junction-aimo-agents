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


def test_hash_recipe_matches_python(tmp_path, monkeypatch):
    import json
    import pytest

    sync_api = pytest.importorskip("playwright.sync_api")
    from aimo import audit

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "a.db"))
    audit.record("hotel-aurora", "agent:buyer", "quote.issued", "q1", "Laktoositon maito, kevytmaito 1 l.", {"quote_id": "q1", "item": "Valio Eilaä ääripää jälkiruoka", "total_gross": 1240.0, "unit": 12.5})
    audit.record("hotel-aurora", "agent:buyer", "order.placed", "o1", "Under the 2,000 EUR per-order limit.", {"order_id": "o1", "quote_id": "q1", "n": {"b": [1, 2], "a": None}})
    audit.record("hospital-tays", "human:päivi", "approval.approved", "o2", "Checked allergens – ok.")
    evs = json.loads(json.dumps([e.model_dump(mode="json") for e in audit.events()]))
    assert audit.verify() == (True, None)
    import functools
    import http.server
    import threading

    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB))
    h.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("chromium not installed")
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{srv.server_port}/audit.html?mock=1")
        pg.wait_for_function("window.aimoAudit")
        assert pg.evaluate("e => window.aimoAudit.verify(e)", evs) is None
        evs[1]["why"] = "edited"
        bad = pg.evaluate("e => window.aimoAudit.verify(e)", evs)
        b.close()
    srv.shutdown()
    assert bad["at"] == 2
