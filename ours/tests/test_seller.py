from datetime import date, datetime, timezone

import httpx
import pytest

from agents.seller import accept, allowed, delivery_day, explain, main, propose, rank, show, usable, usual_items


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


def test_delivery_day_rolls_weekends_and_keeps_weekdays(monkeypatch):
    import agents.seller as sl

    assert delivery_day(date(2026, 11, 11)) == date(2026, 11, 16)
    assert delivery_day(date(2026, 11, 12)) == date(2026, 11, 16)
    assert delivery_day(date(2026, 11, 14)) == date(2026, 11, 17)
    assert delivery_day(date(2026, 11, 16)) == date(2026, 11, 19)

    for fake_today in (date(2026, 11, 11), date(2026, 11, 12)):
        class FakeDate(date):
            @classmethod
            def today(cls):
                return fake_today

        monkeypatch.setattr(sl, "date", FakeDate)
        seen_deliver_by = []
        seen_quotes = []

        def fake_call(method, path, key, url, **kw):
            if path == "/mandate":
                return M
            if path == "/orders":
                return [{"lines": [{"product_id": "1", "name": "milk", "quantity": 100}]}]
            if path == "/offers":
                seen_deliver_by.append(kw["json"]["deliver_by"])
                return {"items": [{"offers": [offer("1", 5, 500, 0.8), offer("2", 4, 500, 0.9)]}]}
            if path == "/quotes":
                seen_quotes.append(kw["json"]["delivery_date"])
                return {"quote_id": "q_1", "jws": "sig"}
            if path == "/orders" and method == "POST":
                return {"order_id": "ord_1", "status": "placed"}
            raise AssertionError((method, path))

        monkeypatch.setattr(sl, "_call", fake_call)
        _, props = sl.explain("hotel-aurora", "k", "u")
        assert seen_deliver_by == ["2026-11-16"]
        assert props and props[0]["offer"]["product_id"] == "2"

        sl.accept({"offer": offer("2", 4, 500, 0.9, need=10)}, "k", "u")
        assert seen_quotes == ["2026-11-16"]


def test_usable_and_allowed_reject_none_prices():
    no_unit = {**offer("2", 4, 500, 0.9), "unit_price_net": None}
    no_total = {**offer("3", 4, 500, 0.9), "total_net": None}
    no_both = {**offer("4", 4, 500, 0.9), "unit_price_net": None, "total_net": None}
    for bad in (no_unit, no_total, no_both):
        assert not usable(bad)
        assert not allowed(bad, M)
    assert rank("1", [offer("1", 5, 500, 0.8), no_unit, no_total, no_both], M) == []


def test_explain_audit_fallback_and_name_of_lookup(monkeypatch):
    import agents.seller as sl

    calls = []

    def fake_call(method, path, key, url, **kw):
        calls.append((method, path, kw))
        if path == "/mandate":
            return M
        if path == "/orders":
            return {"orders": []}
        if path == "/audit":
            return {
                "events": [
                    {"detail": {"lines": [{"product_id": "99", "quantity": 5}]}},
                    {"detail": {"lines": [{"product_id": "42", "quantity": 15, "sales_unit": "l"}]}},
                ]
            }
        if path == "/products/99":
            raise httpx.ConnectError("lookup failed")
        if path == "/products/42":
            return {"product_id": "42", "name": "Valio Kevytmaito"}
        if path == "/offers":
            return {"items": [{"offers": [offer("42", 5, 500, 0.8), offer("43", 4, 500, 0.9)]}]}
        raise AssertionError((method, path))

    monkeypatch.setattr(sl, "_call", fake_call)
    notes, props = explain("hotel-aurora", "k", "u")
    assert notes == []
    assert len(props) == 1
    assert props[0]["origin"] == "from your order history"
    assert props[0]["offer"]["product_id"] == "43"
    paths = [p for _, p, _ in calls]
    assert "/audit" in paths and "/products/42" in paths and "/products/99" in paths
    offer_calls = [kw["json"] for m, p, kw in calls if m == "POST" and p == "/offers"]
    assert len(offer_calls) == 1
    assert offer_calls[0]["items"][0]["query"] == "Valio Kevytmaito"
    assert offer_calls[0]["items"][0]["quantity"] == 15.0
    assert offer_calls[0]["items"][0]["unit"] == "l"


def test_rank_zero_base_total_co2_and_limit():
    zero_base = offer("1", 0, 500, 0.8)
    neg_alt = {**offer("2", 0, 500, 0.8), "total_net": -10.0}
    r_zero = rank("1", [zero_base, neg_alt], M)
    assert len(r_zero) == 1 and r_zero[0]["offer"]["product_id"] == "2"
    assert r_zero[0]["score"] == pytest.approx(1.0)

    r_zero_rel = rank("1", [zero_base, offer("3", 0, 500, 0.95)], M)
    assert len(r_zero_rel) == 1 and r_zero_rel[0]["offer"]["product_id"] == "3"

    base_co2 = offer("1", 5, 500, 0.9, co2=2.5)
    same_high_co2 = offer("2", 4, 500, 0.9, co2=2.6)
    same_low_co2 = offer("3", 4, 500, 0.9, co2=1.2)
    r_tie = rank("1", [base_co2, same_high_co2, same_low_co2], M, limit=2)
    assert [x["offer"]["product_id"] for x in r_tie] == ["3", "2"]
    assert "CO2 1.20 vs 2.50 kg CO2e/kg" in r_tie[0]["why"]
    assert "CO2" not in r_tie[1]["why"]

    cands = [
        offer("1", 5, 500, 0.8, co2=2.0),
        offer("2", 4, 500, 0.95, co2=1.0),
        offer("3", 4, 500, 0.90, co2=1.5),
        offer("4", 4, 500, 0.85, co2=2.0),
    ]
    assert [x["offer"]["product_id"] for x in rank("1", cands, M, limit=1)] == ["2"]
    assert [x["offer"]["product_id"] for x in rank("1", cands, M, limit=2)] == ["2", "3"]
    assert [x["offer"]["product_id"] for x in rank("1", cands, M, limit=3)] == ["2", "3", "4"]


def test_usual_items_malformed_events_and_quantities():
    assert usual_items(None) == []
    assert usual_items("not-a-list") == []
    assert usual_items({"lines": []}) == []

    events = [
        None,
        "corrupt",
        123,
        ["nested"],
        {"detail": "bad-detail"},
        {"detail": {"lines": ["not-a-dict", None, {"product_id": None, "name": "ghost", "quantity": 5}]}},
        {
            "detail": {
                "lines": [
                    {"product_id": "10", "name": "milk", "quantity": "unparseable"},
                    {"product_id": "11", "name": "butter", "quantity": 0},
                    {"product_id": "12", "name": "quark", "quantity": -4},
                    {"product_id": "13", "name": "cream", "quantity": None},
                    {"product_id": "14", "name": "cheese", "quantity": True},
                    {"product_id": "15", "name": "yogurt", "quantity": "nan"},
                    {"product_id": "16", "name": "oat", "quantity": "inf"},
                    {"product_id": "17", "name": "kefir", "quantity": "4.5", "unit": "l"},
                    {"product_id": "10", "name": "milk duplicate", "quantity": 99},
                ]
            }
        },
    ]
    got = {it["product_id"]: it for it in usual_items(events)}
    assert set(got) == {"10", "11", "12", "13", "14", "15", "16", "17"}
    for pid in ("10", "11", "12", "13", "14", "15", "16"):
        assert got[pid]["quantity"] == 1.0
    assert got["10"]["query"] == "milk"
    assert got["17"] == {"product_id": "17", "query": "kefir", "quantity": 4.5, "unit": "l"}


def test_sales_block_cutoff_and_synkka_allergen_codes():
    ref = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    for blk in (True, "blocked", "X"):
        b = {**offer("2", 3, 500, 0.95), "sales_block": blk}
        assert not usable(b, now=ref) and not allowed(b, M)

    past_z = {**offer("3", 3, 500, 0.95), "closing_time": "2026-11-16T11:59:00Z"}
    exact_now = {**offer("4", 3, 500, 0.95), "closing_time": "2026-11-16T12:00:00"}
    bad_iso = {**offer("5", 3, 500, 0.95), "closing_time": "not-a-timestamp"}
    future_z = {**offer("6", 3, 500, 0.95), "closing_time": "2026-11-16T14:00:00Z"}
    for bad in (past_z, exact_now, bad_iso):
        assert not usable(bad, now=ref)
    assert usable(future_z, now=ref)

    hospital = {"per_order_eur": 1500, "exclude_allergens": ["peanut", "nuts", "egg", "soy", "shellfish"], "allowed_categories": None}
    for code in ("AP", "AN", "AE", "AY", "AC", "UM", "may_contain:AP", "may:AN", "soya", "crustaceans"):
        assert not allowed(offer("9", 3, 500, 0.95, [code]), hospital)


def test_dry_run_vs_accept_and_cli_http_errors(monkeypatch, capsys):
    import agents.seller as sl

    calls = []

    def fake_call(method, path, key, url, **kw):
        calls.append((method, path, key))
        if path == "/mandate":
            return {"per_order_eur": 1500, "exclude_allergens": ["nuts"], "allowed_categories": ["dairy"]}
        if path == "/orders" and method == "GET":
            return [{"lines": [{"product_id": "1", "name": "milk", "quantity": 100}]}]
        if path == "/offers":
            return {"items": [{"offers": [offer("1", 5, 500, 0.8), {**offer("2", 4, 500, 0.9), "available_date": delivery_day().isoformat()}]}]}
        if path == "/quotes":
            return {"quote_id": "q_demo", "jws": "hdr.pay.sig"}
        if path == "/orders" and method == "POST":
            return {"order_id": "ord_demo", "status": "placed", "decision": {"outcome": "auto"}}
        raise AssertionError((method, path))

    monkeypatch.setattr(sl, "_call", fake_call)

    ps = show("hotel-aurora", "demo-hotel-aurora", "http://gw.test")
    out = capsys.readouterr().out
    assert len(ps) == 1 and "[from your order history]" in out
    assert ("POST", "/quotes", "demo-hotel-aurora") not in calls
    assert ("POST", "/orders", "demo-hotel-aurora") not in calls

    calls.clear()
    rc = main(["--demo", "--url", "http://gw.test"])
    demo_out = capsys.readouterr().out
    assert rc == 0 and "== hotel-aurora" in demo_out and "== hospital-tays" in demo_out
    assert not any(m == "POST" and p in ("/quotes", "/orders") for m, p, _ in calls)

    calls.clear()
    rc = main(["--demo", "--accept", "--url", "http://gw.test"])
    acc_out = capsys.readouterr().out
    assert rc == 0 and "ord_demo placed" in acc_out
    assert ("POST", "/quotes", "demo-hotel-aurora") in calls
    assert ("POST", "/orders", "demo-hotel-aurora") in calls

    rc_bad_idx = main(["--customer", "hotel-aurora", "--accept", "9", "--url", "http://gw.test"])
    err = capsys.readouterr().err
    assert rc_bad_idx == 1 and "out of range" in err

    with pytest.raises(SystemExit):
        main([])
    capsys.readouterr()

    flaky_calls = []

    def flaky_call(method, path, key, url, **kw):
        flaky_calls.append(path)
        if path == "/mandate":
            return M
        if path in ("/orders", "/audit"):
            raise httpx.ConnectError("history unavailable")
        if path == "/offers":
            if kw["json"]["items"][0]["query"] == "Valio maito":
                raise httpx.ReadTimeout("upstream slow")
            return {"items": [{"offers": [offer("10", 4, 500, 0.9)]}]}
        raise AssertionError(path)

    monkeypatch.setattr(sl, "_call", flaky_call)
    res_props = propose("hotel-aurora", "demo-hotel-aurora", "http://gw.test")
    assert res_props and res_props[0]["origin"] == "catalog suggestion"
    assert "/orders" in flaky_calls and "/audit" in flaky_calls

    monkeypatch.setattr(sl, "_call", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("gateway down")))
    rc_down = main(["--customer", "hotel-aurora", "--url", "http://gw.test"])
    err_down = capsys.readouterr().err
    assert rc_down == 1 and "gateway down" in err_down
