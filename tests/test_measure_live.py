import httpx
from pitch import measure_live as m


def test_table_skips_404(monkeypatch):
    class C:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k):
            return httpx.Response(404, request=httpx.Request("GET", "http://x"))
    monkeypatch.setattr(m.httpx, "Client", C)
    r = m.flow("http://x", "hotel-aurora", "k")
    t = m.table([r])
    assert "404 skipped" in t and "NOT FOUND" in t


def test_monday():
    import datetime
    assert m.monday(datetime.date(2026, 11, 14)).weekday() == 0
