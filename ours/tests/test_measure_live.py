import datetime
import os
import re
import statistics
from pathlib import Path
import httpx
from aimo import catalog, valio
from aimo.models import Product, UnitSize
from pitch import measure_live as m

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"


def test_table_skips_404(monkeypatch):
    class C:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            return httpx.Response(404, request=httpx.Request("GET", "http://x"))

    monkeypatch.setattr(m.httpx, "Client", C)
    r = m.flow("http://x", "hotel-aurora", "k")
    t = m.table([r])
    assert "404 skipped" in t and "NOT FOUND" in t


def test_monday():
    assert m.monday(datetime.date(2026, 11, 14)).weekday() == 0


def test_flow_records_picked_fields_and_alternatives(monkeypatch):
    routes = {
        ("GET", "/v1/mandate"): {"customer": "hotel-aurora", "per_order_eur": 2500.0},
        ("POST", "/v1/needs/parse"): {
            "customer": "hotel-aurora",
            "deliver_by": "2026-11-16",
            "items": [{"query": "milk", "quantity": 1000, "unit": "L", "constraints": {"lactose_free": True}}],
        },
        ("POST", "/v1/offers"): {
            "items": [
                {
                    "offers": [
                        {
                            "product_id": "464119",
                            "product": {"product_id": "464119", "name": "Valio rasvaton maito 1 l laktoositon UHT"},
                            "units_needed": 100.0,
                            "unit_price_net": 13.32,
                            "total_net": 1332.0,
                            "total_gross": 1512.0,
                            "available_amount": 9999999999,
                        }
                    ],
                    "shortfall": 0.0,
                    "alternatives": [{"product_id": "1"}, {"product_id": "2"}, {"product_id": "3"}],
                }
            ]
        },
        ("POST", "/v1/quotes"): {
            "quote_id": "q-1",
            "jws": "tok",
            "total_net": 1332.0,
            "total_gross": 1512.0,
        },
        ("POST", "/v1/orders"): {
            "order_id": "ord-1",
            "decision": {"outcome": "auto", "reasons": [{"code": "WITHIN_MANDATE", "text": "ok"}]},
        },
        ("GET", "/v1/approvals"): [],
        ("GET", "/v1/audit"): {"events": [{}, {}, {}, {}, {}]},
    }

    class FakeClient:
        def __init__(self, *a, **k):
            self.closed = False

        def request(self, method, path, **kw):
            body = routes[(method, path)]
            return httpx.Response(200, json=body, request=httpx.Request(method, "http://x" + path))

        def close(self):
            self.closed = True

    monkeypatch.setattr(m.httpx, "Client", FakeClient)
    r = m.flow("http://x", "hotel-aurora", "demo-hotel-aurora")
    assert r["picked_product_id"] == "464119"
    assert r["picked_name"] == "Valio rasvaton maito 1 l laktoositon UHT"
    assert r["units_needed"] == 100.0
    assert r["unit_price_net"] == 13.32
    assert r["total_net"] == 1332.0
    assert r["total_gross"] == 1512.0
    assert r["alternatives_count"] == 3
    assert r["outcome"] == "auto"
    assert r["audit_events"] == 5
    out = m.table([r])
    assert "picked 464119 Valio rasvaton maito 1 l laktoositon UHT" in out
    assert "alternatives=3" in out


def test_inprocess_mode_uses_temp_db_and_keys(monkeypatch, capsys, tmp_path):
    prods = [
        Product(
            product_id="464119",
            name="Valio rasvaton maito 1 l laktoositon UHT",
            brand="Valio",
            category="10400",
            sales_unit="CASE",
            unit_size=UnitSize(value=10.0, unit="L"),
            allergens=["milk"],
            labels=["FREE_FROM_LACTOSE"],
        ),
        Product(
            product_id="464120",
            name="Valio kevytmaito 1 l laktoositon UHT",
            brand="Valio",
            category="10400",
            sales_unit="CASE",
            unit_size=UnitSize(value=10.0, unit="L"),
            allergens=["milk"],
            labels=["FREE_FROM_LACTOSE"],
        ),
        Product(
            product_id="464121",
            name="Valio täysmaito 1 l laktoositon UHT",
            brand="Valio",
            category="10400",
            sales_unit="CASE",
            unit_size=UnitSize(value=10.0, unit="L"),
            allergens=["milk"],
            labels=["FREE_FROM_LACTOSE"],
        ),
        Product(
            product_id="464122",
            name="Valio Eila maitojuoma 1 l laktoositon",
            brand="Valio",
            category="10400",
            sales_unit="CASE",
            unit_size=UnitSize(value=10.0, unit="L"),
            allergens=["milk"],
            labels=["FREE_FROM_LACTOSE"],
        ),
    ]
    by_id = {p.product_id: p for p in prods}
    net_by_id = {"464119": 13.32, "464120": 14.0, "464121": 14.5, "464122": 15.0}
    gross_by_id = {"464119": 15.12, "464120": 15.96, "464121": 16.53, "464122": 17.1}

    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: list(prods))
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.1)
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, day, ids: {
            str(i): {
                "productId": str(i),
                "currency": "EUR",
                "catalogPrice": {"value": net_by_id[str(i)]},
                "priceWithTax": {"value": gross_by_id[str(i)]},
            }
            for i in ids
            if str(i) in net_by_id
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, day, ids: {
            str(i): {
                "productId": str(i),
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 9999999999,
                        "availableDate": day.isoformat(),
                        "salesUnit": "CASE",
                    }
                ],
            }
            for i in ids
            if str(i) in net_by_id
        },
    )

    outer_db = str(tmp_path / "outer.db")
    outer_keys = str(tmp_path / "outer_keys")
    monkeypatch.setenv("AIMO_DB", outer_db)
    monkeypatch.setenv("AIMO_KEY_DIR", outer_keys)
    monkeypatch.setenv("LLM_BASE_URL", "")

    results = m.main(["--inprocess"])
    out = capsys.readouterr().out

    assert os.environ.get("AIMO_DB") == outer_db
    assert os.environ.get("AIMO_KEY_DIR") == outer_keys
    assert not (tmp_path / "outer.db").exists()
    assert not (tmp_path / "outer_keys").exists()

    assert [r["customer"] for r in results] == m.CUSTOMERS
    assert [r["outcome"] for r in results] == ["auto", "auto", "needs_approval"]
    for r in results:
        assert r["picked_product_id"] == "464119"
        assert r["picked_name"] == "Valio rasvaton maito 1 l laktoositon UHT"
        assert r["units_needed"] == 100.0
        assert r["unit_price_net"] == 13.32
        assert r["total_net"] == 1332.0
        assert r["total_gross"] == 1512.0
        assert r["alternatives_count"] == 3
    assert "auto 2, needs_approval 1, blocked 0 of 3 customers" in out
    assert "picked 464119 Valio rasvaton maito 1 l laktoositon UHT" in out


def test_numbers_md_live_section_consistency():
    text = (ROOT / "pitch" / "NUMBERS.md").read_text(encoding="utf-8")
    assert "464119" in text
    assert "Valio rasvaton maito 1 l laktoositon UHT" in text
    assert "1,332.00 EUR" in text
    assert "1,512.00 EUR" in text
    rows = {}
    latencies = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cols = [c.strip() for c in line[1:-1].split("|")]
        if len(cols) == 13 and cols[0] in m.CUSTOMERS:
            rows[cols[0]] = cols
            latencies.extend(int(c) for c in cols[1:8])
    assert set(rows) == set(m.CUSTOMERS)
    assert len(latencies) == 21
    assert rows["hotel-aurora"][9:12] == ["1332.0", "auto", "WITHIN_MANDATE"]
    assert rows["hospital-tays"][9:12] == ["1332.0", "auto", "WITHIN_MANDATE"]
    assert rows["school-hervanta"][9:12] == ["1332.0", "needs_approval", "OVER_ORDER_LIMIT"]
    med = statistics.median(latencies)
    med_str = str(int(med)) if med == int(med) else str(med)
    assert re.search(rf"Median step latency `{re.escape(med_str)} ms` over 21 calls", text)


def test_measure_live_hygiene():
    for rel in ("pitch/measure_live.py", "tests/test_measure_live.py", "pitch/NUMBERS.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert EM_DASH not in text, rel
    for rel in ("pitch/measure_live.py", "tests/test_measure_live.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert HASH_CHAR not in text, rel
