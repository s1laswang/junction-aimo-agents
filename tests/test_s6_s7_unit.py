import json
import sys
from datetime import date

import httpx
import pytest

from agents import drift
from scenarios import s6_proactive_seller as s6
from scenarios._common import KEYS


class FakeRun:
    def __init__(self):
        self.said = []
        self.results = []

    def say(self, text):
        self.said.append(text)

    def check(self, label, ok, detail=""):
        self.results.append((label, bool(ok), detail))
        return bool(ok)


def _proposal(pid, category="dairy", why="cheaper per kg"):
    return {
        "why": why,
        "offer": {
            "product_id": pid,
            "product": {"name": f"Product {pid}", "category": category},
        },
    }


def _make_client_cls(mandates, seen_clients=None):
    class FakeClient:
        def __init__(self, base, cust):
            self.base = base
            self.cust = cust
            if seen_clients is not None:
                seen_clients.append((base, cust))

        def j(self, method, path, **kw):
            assert (method, path) == ("GET", "/v1/mandate")
            return mandates[self.cust]

    return FakeClient


@pytest.fixture(autouse=True)
def forbid_heavy_product_db(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("unit tests must not load 84MB product_db")

    for mod_name in ("_common", "scenarios._common"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            monkeypatch.setattr(mod, "product_db", _boom, raising=False)
            monkeypatch.setattr(mod, "_product_db", _boom, raising=False)


def test_s6_import_does_not_parse_cli_or_run():
    assert s6.base is None
    assert s6.r is None


def test_s6_happy_path_calls_propose_once_per_customer():
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["peanuts"], "allowed_categories": ["dairy", "bakery"]},
        "hospital-tays": {"exclude_allergens": ["nuts", "egg"], "allowed_categories": ["dairy"]},
    }
    proposals_by_cust = {
        "hotel-aurora": [_proposal("p1", "dairy"), _proposal("p2", "bakery")],
        "hospital-tays": [_proposal("p3", "dairy")],
    }
    seen_clients = []
    propose_calls = []
    violates_calls = []

    def fake_propose(cust, key, base):
        propose_calls.append((cust, key, base))
        return proposals_by_cust[cust]

    def fake_violates(pid, excl):
        violates_calls.append((pid, set(excl)))
        return False

    r = FakeRun()
    s6.run_scenario(
        "http://mock-gateway",
        r,
        client_cls=_make_client_cls(mandates, seen_clients),
        propose_fn=fake_propose,
        violates_fn=fake_violates,
    )

    assert seen_clients == [
        ("http://mock-gateway", "hotel-aurora"),
        ("http://mock-gateway", "hospital-tays"),
    ]
    assert propose_calls == [
        ("hotel-aurora", KEYS["hotel-aurora"], "http://mock-gateway"),
        ("hospital-tays", KEYS["hospital-tays"], "http://mock-gateway"),
    ]
    assert len(propose_calls) == 2
    assert violates_calls == [
        ("p1", {"peanuts"}),
        ("p2", {"peanuts"}),
        ("p3", {"nuts", "egg"}),
    ]
    assert r.said == ["hotel-aurora: 2 proposals", "hospital-tays: 1 proposals"]
    assert [label for label, _, _ in r.results] == [
        "hotel-aurora proposals have a why",
        "hotel-aurora proposals respect excluded allergens",
        "hotel-aurora proposals respect allowed categories",
        "hospital-tays proposals have a why",
        "hospital-tays proposals respect excluded allergens",
        "hospital-tays proposals respect allowed categories",
        "hotel got at least one proposal",
    ]
    assert all(ok for _, ok, _ in r.results)


def test_s6_missing_why_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
        "hospital-tays": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
    }
    proposals_by_cust = {
        "hotel-aurora": [_proposal("p1", "dairy", why="")],
        "hospital-tays": [_proposal("p2", "dairy", why="saves 4.20 EUR")],
    }
    r = FakeRun()
    s6.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        propose_fn=lambda cust, key, base: proposals_by_cust[cust],
        violates_fn=lambda pid, excl: False,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora proposals have a why"] is False
    assert by_label["hospital-tays proposals have a why"] is True


def test_s6_allergen_leak_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["peanuts"], "allowed_categories": ["dairy"]},
        "hospital-tays": {"exclude_allergens": ["nuts"], "allowed_categories": ["dairy"]},
    }
    proposals_by_cust = {
        "hotel-aurora": [_proposal("safe-1", "dairy")],
        "hospital-tays": [_proposal("leak-nut", "dairy")],
    }
    r = FakeRun()
    s6.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        propose_fn=lambda cust, key, base: proposals_by_cust[cust],
        violates_fn=lambda pid, excl: pid == "leak-nut" and "nuts" in excl,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora proposals respect excluded allergens"] is True
    assert by_label["hospital-tays proposals respect excluded allergens"] is False


def test_s6_disallowed_category_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
        "hospital-tays": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
    }
    proposals_by_cust = {
        "hotel-aurora": [_proposal("p-meat", "meat")],
        "hospital-tays": [_proposal("p-dairy", "dairy")],
    }
    r = FakeRun()
    s6.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        propose_fn=lambda cust, key, base: proposals_by_cust[cust],
        violates_fn=lambda pid, excl: False,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora proposals respect allowed categories"] is False
    assert by_label["hospital-tays proposals respect allowed categories"] is True


def test_s6_empty_hotel_proposals_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
        "hospital-tays": {"exclude_allergens": [], "allowed_categories": ["dairy"]},
    }
    calls = []

    def fake_propose(cust, key, base):
        calls.append(cust)
        return [] if cust == "hotel-aurora" else [_proposal("p-hosp", "dairy")]

    r = FakeRun()
    s6.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        propose_fn=fake_propose,
        violates_fn=lambda pid, excl: False,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert calls == ["hotel-aurora", "hospital-tays"]
    assert by_label["hotel got at least one proposal"] is False
    assert by_label["hotel-aurora proposals have a why"] is True
    assert by_label["hospital-tays proposals have a why"] is True


def test_s6_allowed_categories_none_allowed():
    mandates = {
        "hotel-aurora": {"exclude_allergens": [], "allowed_categories": None},
        "hospital-tays": {"exclude_allergens": []},
    }
    proposals_by_cust = {
        "hotel-aurora": [_proposal("p-meat", "meat")],
        "hospital-tays": [_proposal("p-produce", "produce")],
    }
    r = FakeRun()
    s6.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        propose_fn=lambda cust, key, base: proposals_by_cust[cust],
        violates_fn=lambda pid, excl: False,
    )
    assert len(r.results) == 7
    assert all(ok for _, ok, _ in r.results)


def test_s6_go_uses_default_seller_propose_and_violates(monkeypatch):
    from agents import seller

    mandates = {
        "hotel-aurora": {"exclude_allergens": ["peanuts"], "allowed_categories": ["dairy"]},
        "hospital-tays": {"exclude_allergens": ["nuts"], "allowed_categories": ["dairy"]},
    }
    propose_calls = []

    def fake_propose(cust, key, base):
        propose_calls.append((cust, key, base))
        return [_proposal(f"{cust}-1", "dairy")]

    r = FakeRun()
    monkeypatch.setattr(s6, "base", "http://mock-default")
    monkeypatch.setattr(s6, "r", r)
    monkeypatch.setattr(s6, "Client", _make_client_cls(mandates))
    monkeypatch.setattr(seller, "propose", fake_propose)
    monkeypatch.setattr(s6, "violates", lambda pid, excl: False)

    s6.go()
    assert len(propose_calls) == 2
    assert len(r.results) == 7
    assert all(ok for _, ok, _ in r.results)


def _offer(pid, amt, **kw):
    o = {
        "product_id": pid,
        "product": {"name": f"Item {pid}"},
        "units_needed": 10,
        "available_amount": amt,
        "available_date": "2026-11-17",
        "sales_block": False,
    }
    o.update(kw)
    return o


def test_line_fix_sales_block_triggers_substitute():
    line = {"product_id": "1", "name": "Milk", "quantity": 10}
    group = {
        "offers": [
            _offer("1", 50, sales_block=True),
            _offer("2", 40, units_needed=10, substitute_for="1"),
        ]
    }
    res = drift.line_fix(line, group, "2026-11-17")
    assert res is not None
    assert res["after"] == 0
    assert res["date"] == "2026-11-17"
    assert res["fix"] == {
        "action": "substitute",
        "lines": [{"product_id": "2", "quantity": 10, "substitute_for": "1"}],
    }
    assert "no longer available" in res["why"]
    assert "Item 2" in res["why"]


def test_line_fix_sales_block_without_substitute_returns_none_action():
    line = {"product_id": "1", "name": "Milk", "quantity": 10}
    group = {"offers": [_offer("1", 50, sales_block=True)]}
    res = drift.line_fix(line, group, "2026-11-17")
    assert res is not None
    assert res["after"] == 0
    assert res["date"] == "2026-11-17"
    assert res["fix"] == {"action": "none", "lines": []}
    assert "not available" in res["why"]
    assert "no substitute was found" in res["why"]


def test_line_fix_partial_stock_without_substitute_reduces_qty():
    line = {"product_id": "1", "name": "Milk", "quantity": 10}
    group = {"offers": [_offer("1", 4, sales_block=False)]}
    res = drift.line_fix(line, group, "2026-11-17")
    assert res is not None
    assert res["after"] == 4
    assert res["date"] == "2026-11-17"
    assert res["fix"] == {
        "action": "reduce_qty",
        "lines": [{"product_id": "1", "quantity": 4}],
    }
    assert "Only 4 of 10" in res["why"]
    assert "reducing to 4" in res["why"]


def test_line_fix_healthy_zero_stock_and_split_branches():
    line = {"product_id": "1", "name": "Milk", "quantity": 10}
    assert drift.line_fix(line, {"offers": [_offer("1", 10)]}, "2026-11-17") is None

    zero = drift.line_fix(line, {"offers": [_offer("1", 0)]}, "2026-11-17")
    assert zero["after"] == 0 and zero["fix"] == {"action": "none", "lines": []}

    missing = drift.line_fix(line, {"offers": []}, "2026-11-17")
    assert missing["after"] == 0 and missing["fix"] == {"action": "none", "lines": []}

    split = drift.line_fix(
        line,
        {"offers": [_offer("1", 3), _offer("2", 20, units_needed=7, substitute_for="1")]},
        "2026-11-17",
    )
    assert split["after"] == 3
    assert split["fix"] == {
        "action": "split",
        "lines": [
            {"product_id": "1", "quantity": 3},
            {"product_id": "2", "quantity": 7, "substitute_for": "1"},
        ],
    }


def _watch_transport(orders, groups_by_call, calls):
    offer_idx = {"n": 0}

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/orders":
            return httpx.Response(200, json=orders)
        body = json.loads(req.content)
        if req.method == "POST" and req.url.path == "/v1/offers":
            idx = offer_idx["n"]
            offer_idx["n"] += 1
            return httpx.Response(200, json={"items": groups_by_call[idx]})
        if req.method == "POST" and req.url.path == "/v1/quotes":
            calls.append(("QUOTE_BODY", body))
            return httpx.Response(200, json={"quote_id": "q_repl", "jws": "h.p.s"})
        if req.method == "POST" and req.url.path == "/v1/orders":
            calls.append(("ORDER_BODY", body))
            return httpx.Response(200, json={"order_id": "ord_repl"})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    return httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))


def test_watch_orders_reduce_qty_places_replacement_order(tmp_path):
    orders = [
        {
            "order_id": "ord_partial",
            "status": "placed",
            "delivery_date": "2026-11-17",
            "lines": [
                {"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"},
                {"product_id": "9", "name": "Bread", "quantity": 4, "sales_unit": "CS"},
            ],
        }
    ]
    groups = [[{"offers": [_offer("1", 4)]}, {"offers": [_offer("9", 50)]}]]
    calls = []
    st = tmp_path / "drift.json"
    c = _watch_transport(orders, groups, calls)

    res = drift.watch_orders("hotel-aurora", "k", "http://mock", today=date(2026, 11, 14), state_path=st, client=c)
    assert len(res) == 1
    assert res[0]["order_id"] == "ord_partial"
    assert res[0]["product_id"] == "1"
    assert res[0]["before"] == 10
    assert res[0]["after"] == 4
    assert res[0]["fix"]["action"] == "reduce_qty"
    assert res[0]["new_order_id"] == "ord_repl"

    quote_bodies = [b for tag, b in calls if tag == "QUOTE_BODY"]
    order_bodies = [b for tag, b in calls if tag == "ORDER_BODY"]
    assert quote_bodies == [
        {
            "offers": [{"product_id": "1", "quantity": 4}, {"product_id": "9", "quantity": 4}],
            "delivery_date": "2026-11-17",
        }
    ]
    assert len(order_bodies) == 1
    assert order_bodies[0]["ask_human"] is True
    assert "Replaces order ord_partial" in order_bodies[0]["note"]

    calls_second = []
    c2 = _watch_transport(orders, groups, calls_second)
    assert drift.watch_orders("hotel-aurora", "k", "http://mock", today=date(2026, 11, 14), state_path=st, client=c2) == []
    assert calls_second == [("GET", "/v1/orders")]


def test_watch_orders_sales_block_with_substitute_places_replacement(tmp_path):
    orders = [
        {
            "order_id": "ord_sb_sub",
            "status": "confirmed",
            "delivery_date": "2026-11-17",
            "lines": [{"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"}],
        }
    ]
    groups = [
        [
            {
                "offers": [
                    _offer("1", 50, sales_block=True),
                    _offer("2", 50, units_needed=10, substitute_for="1"),
                ]
            }
        ]
    ]
    calls = []
    c = _watch_transport(orders, groups, calls)
    res = drift.watch_orders("hotel-aurora", "k", "http://mock", state_path=tmp_path / "drift.json", client=c)

    assert len(res) == 1
    assert res[0]["fix"]["action"] == "substitute"
    assert res[0]["new_order_id"] == "ord_repl"
    quote_bodies = [b for tag, b in calls if tag == "QUOTE_BODY"]
    assert quote_bodies == [
        {
            "offers": [{"product_id": "2", "quantity": 10, "substitute_for": "1"}],
            "delivery_date": "2026-11-17",
        }
    ]


def test_watch_orders_action_none_single_and_mixed_never_places_replacement(tmp_path):
    orders = [
        {
            "order_id": "ord_single_none",
            "status": "confirmed",
            "delivery_date": "2026-11-17",
            "lines": [{"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"}],
        },
        {
            "order_id": "ord_mixed_none",
            "status": "approved",
            "delivery_date": "2026-11-17",
            "lines": [
                {"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"},
                {"product_id": "9", "name": "Bread", "quantity": 4, "sales_unit": "CS"},
            ],
        },
    ]
    groups = [
        [{"offers": [_offer("1", 50, sales_block=True)]}],
        [{"offers": []}, {"offers": [_offer("9", 50)]}],
    ]
    calls = []
    st = tmp_path / "drift.json"
    c = _watch_transport(orders, groups, calls)

    res = drift.watch_orders("hotel-aurora", "k", "http://mock", state_path=st, client=c)
    assert len(res) == 2
    assert [r["order_id"] for r in res] == ["ord_single_none", "ord_mixed_none"]
    assert all(r["fix"]["action"] == "none" for r in res)
    assert all(r["new_order_id"] is None for r in res)
    assert ("POST", "/v1/quotes") not in calls
    assert ("POST", "/v1/orders") not in calls
    assert not st.exists()


def test_watch_orders_empty_lines_and_closed_orders_do_not_place_replacement(tmp_path):
    orders = [
        {
            "order_id": "ord_empty",
            "status": "placed",
            "delivery_date": "2026-11-17",
            "lines": [],
        },
        {
            "order_id": "ord_blocked",
            "status": "blocked",
            "delivery_date": "2026-11-17",
            "lines": [{"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"}],
        },
    ]
    groups = [[]]
    calls = []
    c = _watch_transport(orders, groups, calls)

    res = drift.watch_orders("hotel-aurora", "k", "http://mock", state_path=tmp_path / "drift.json", client=c)
    assert res == []
    assert calls == [("GET", "/v1/orders"), ("POST", "/v1/offers")]
