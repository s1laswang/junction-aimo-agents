import httpx
import pytest

from agents.seller import accept, allowed, propose, rank, usable, usual_items


def offer(pid, price, avail, rel, allergens=(), cat="dairy", need=100, co2=1.0):
    return {"product_id": pid, "product": {"name": f"p{pid}", "allergens": list(allergens), "category": cat}, "units_needed": need,
            "unit_price_net": price, "total_net": price * need, "available_amount": avail, "reliability": rel, "co2_per_kg": co2, "sales_block": False}


M = {"per_order_eur": 1000, "exclude_allergens": ["nuts"], "allowed_categories": ["dairy"]}


def test_cheaper_and_reliable():
    r = rank("1", [offer("1", 5, 500, 0.8), offer("2", 4, 500, 0.9)], M)
    assert len(r) == 1 and r[0]["offer"]["product_id"] == "2"
    assert "400.00 vs 500.00" in r[0]["why"] and "0.90 vs 0.80" in r[0]["why"]


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


def test_missing_usual_uses_cheapest():
    r = rank("9", [offer("1", 5, 500, 0.8), offer("2", 4, 500, 0.9), offer("3", 3, 500, 0.7)], M)
    assert "not in the offers" in r[0]["why"]


def test_total_not_unit():
    big = offer("2", 10, 500, 0.8, need=100)
    big["total_net"] = 900
    r = rank("1", [offer("1", 5, 500, 0.8), big], M)
    assert r and "900.00 vs 500.00" not in r[0]["why"] or r == []


def test_usual_items_count_only():
    assert usual_items([{"detail": {"lines": 1, "quote_id": "q"}}]) == []


def test_origin_and_note(monkeypatch):
    import agents.seller as sl
    base = offer("1", 5, 500, 0.8)

    def fake(method, path, key, url, **kw):
        if path == "/mandate":
            return M
        if path == "/orders":
            return [{"lines": [{"product_id": 1, "name": "milk", "quantity": 100}]}]
        return {"items": [{"offers": [base]}]}

    monkeypatch.setattr(sl, "_call", fake)
    notes, ps = sl.explain("hotel-aurora", "k", "u")
    assert notes and "already the best offer" in notes[0]
    assert ps and ps[0]["origin"] == "catalog suggestion"


def test_allergen_traces_and_aliases_hospital_school():
    hospital = {"customer": "hospital-tays", "per_order_eur": 1500, "exclude_allergens": ["peanut", "nuts", "egg"], "allowed_categories": None}
    school = {"customer": "school-hervanta", "per_order_eur": 600, "exclude_allergens": ["peanut", "nuts"], "allowed_categories": None}
    school_no_eggs = {**school, "exclude_allergens": ["peanut", "nuts", "eggs"]}

    for bad in ("peanuts", "tree nuts", "may_contain:egg", "may:egg", "may_contain:peanuts", "may:tree nuts"):
        assert not allowed(offer("2", 3, 500, 0.95, [bad]), hospital)
    assert allowed(offer("9", 3, 500, 0.95, ["milk"]), hospital)

    for bad in ("peanuts", "tree nuts", "may_contain:peanuts", "may_contain:tree nuts"):
        assert not allowed(offer("2", 3, 500, 0.95, [bad]), school)
    assert not allowed(offer("2", 3, 500, 0.95, ["may_contain:egg"]), school_no_eggs)

    base = offer("1", 5, 500, 0.8)
    risky = [
        offer("2", 2, 500, 0.95, ["peanuts"]),
        offer("3", 2, 500, 0.95, ["tree nuts"]),
        offer("4", 2, 500, 0.95, ["may_contain:egg"]),
    ]
    assert rank("1", [base, *risky], hospital) == []
    assert rank("1", [base, risky[0], risky[1]], school) == []
    assert rank("1", [base, *risky], school_no_eggs) == []

    safe = offer("5", 4, 500, 0.9, ["milk"])
    r = rank("1", [base, *risky, safe], hospital)
    assert len(r) == 1 and r[0]["offer"]["product_id"] == "5"


def test_other_allergen_aliases():
    for m_al, prod_als in (
        ("nut", ("nuts", "peanuts", "peanut", "tree nuts")),
        ("milk", ("milk", "lactose", "may_contain:milk")),
        ("dairy", ("milk", "lactose")),
        ("gluten", ("gluten", "wheat", "barley", "rye", "oats", "may:wheat")),
        ("eggs", ("egg", "eggs", "may_contain:egg")),
    ):
        mandate = {"per_order_eur": 1000, "exclude_allergens": [m_al]}
        for pa in prod_als:
            assert not allowed(offer("2", 3, 500, 0.9, [pa]), mandate)


def test_zero_stock_and_delivery_cutoff_sanity():
    zero = offer("2", 0, 0, 0.95, need=0)
    neg = offer("3", 3, -5, 0.95)
    assert not usable(zero) and not allowed(zero, M)
    assert not usable(neg) and not allowed(neg, M)
    assert rank("1", [offer("1", 5, 500, 0.8), zero, neg], M) == []

    late = {**offer("4", 3, 500, 0.95), "available_date": "2026-11-20"}
    expired = {**offer("5", 3, 500, 0.95), "closing_time": "2020-01-01T12:00:00+00:00"}
    assert not usable(late, deliver_by="2026-11-18")
    assert not allowed(late, M, deliver_by="2026-11-18")
    assert not usable(expired) and not allowed(expired, M)
    assert rank("1", [offer("1", 5, 500, 0.8), late, expired], M, deliver_by="2026-11-18") == []


def test_accept_monkeypatched(monkeypatch):
    import agents.seller as sl

    calls = []

    def fake_call(method, path, key, url, **kw):
        calls.append((method, path, key, url, kw))
        if path == "/quotes":
            return {"quote_id": "q_42", "jws": "hdr.pay.sig"}
        if path == "/orders":
            return {"order_id": "ord_99", "status": "placed", "decision": {"outcome": "auto"}}
        raise AssertionError(path)

    monkeypatch.setattr(sl, "_call", fake_call)
    prop = {"offer": {**offer("42", 4, 500, 0.9, need=25), "available_date": "2026-11-18"}}
    res = accept(prop, "demo-hotel-aurora", "http://gw.test")
    assert res["order_id"] == "ord_99" and res["status"] == "placed"
    assert calls == [
        ("POST", "/quotes", "demo-hotel-aurora", "http://gw.test", {"json": {"offers": [{"product_id": "42", "quantity": 25}], "delivery_date": "2026-11-18"}}),
        ("POST", "/orders", "demo-hotel-aurora", "http://gw.test", {"json": {"quote_id": "q_42", "jws": "hdr.pay.sig"}}),
    ]

    calls.clear()
    res2 = accept({"offer": offer("7", 4, 500, 0.9, need=10)}, "demo-school-hervanta", "http://gw.test")
    assert res2["order_id"] == "ord_99"
    assert len(calls[0][4]["json"]["delivery_date"]) == 10
