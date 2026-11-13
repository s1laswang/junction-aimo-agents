import httpx
import pytest

from agents.seller import propose, rank, usual_items


def offer(pid, price, avail, rel, allergens=(), cat="dairy", need=100, co2=1.0):
    return {"product_id": pid, "product": {"name": f"p{pid}", "allergens": list(allergens), "category": cat}, "units_needed": need,
            "unit_price_net": price, "total_net": price * need, "available_amount": avail, "reliability": rel, "co2_per_kg": co2, "sales_block": False}


M = {"per_order_eur": 1000, "exclude_allergens": ["nuts"], "allowed_categories": ["dairy"]}


def test_cheaper_and_reliable():
    r = rank("1", [offer("1", 5, 500, 0.8), offer("2", 4, 500, 0.9)], M)
    assert len(r) == 1 and r[0]["offer"]["product_id"] == "2"
    assert "4.00 vs 5.00" in r[0]["why"] and "0.90 vs 0.80" in r[0]["why"]


def test_mandate_filters():
    offers = [offer("1", 5, 500, 0.8), offer("2", 1, 500, 0.9, ["nuts"]), offer("3", 1, 500, 0.9, cat="meat"), offer("4", 100, 500, 0.99), offer("5", 1, 5, 0.9)]
    assert rank("1", offers, M) == []


def test_stock_short():
    r = rank("1", [offer("1", 5, 10, 0.9), offer("2", 6, 500, 0.9)], M)
    assert r and "only 10" in r[0]["why"]


def test_usual_items():
    ev = [{"detail": {"lines": [{"product_id": 7, "name": "milk", "quantity": 3}]}}]
    assert usual_items(ev)[0]["query"] == "milk"


def test_integration():
    try:
        httpx.get("http://localhost:8080/openapi.json", timeout=2)
    except Exception:
        pytest.skip("gateway down")
    import os
    key = os.environ.get("HOTEL_AURORA_KEY")
    if not key:
        pytest.skip("no key")
    assert isinstance(propose("hotel-aurora", key), list)
