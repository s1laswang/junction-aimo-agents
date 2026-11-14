from datetime import date

from fastapi.testclient import TestClient

from aimo.app import app
from aimo.models import Need, NeedItem


def test_health():
    assert TestClient(app).get("/health").json() == {"ok": True}


def test_need_roundtrip():
    n = Need(customer="hotel-aurora", deliver_by=date(2026, 11, 16), items=[NeedItem(query="maito", quantity=1000, unit="l")])
    assert Need.model_validate_json(n.model_dump_json()) == n
    assert n.items[0].constraints.exclude_allergens == []
