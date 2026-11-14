import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from agents import drift
from scenarios import s6_proactive_seller as s6
from scenarios import s7_drift as s7
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


def _flag(pid, kind="price", why="price moved +6.0%", route="auto", fix=None):
    return {
        "product_id": pid,
        "kind": kind,
        "why": why,
        "route": route,
        "fix": fix if fix is not None else {"action": "requote"},
    }


def test_s7_import_does_not_parse_cli_or_run():
    assert s7.base is None
    assert s7.r is None


def test_s7_happy_path_all_nine_checks_and_tempdir_cleanup():
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["Peanuts"]},
        "school-hervanta": {"exclude_allergens": ["Nuts", "Egg"]},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    seen_clients = []
    check_calls = []
    temp_dirs = []
    violates_calls = []
    sim_calls = []
    clear_calls = []

    def fake_check(cust, key, base, state_path=None):
        assert state_path is not None and state_path.endswith("drift.json")
        td = os.path.dirname(state_path)
        assert os.path.isdir(td)
        temp_dirs.append(td)
        p = Path(state_path)
        had_state = p.exists()
        p.write_text(json.dumps({cust: {"ok": True}}), encoding="utf-8")
        check_calls.append((cust, key, base, state_path, had_state))
        if len(sim_calls) > len(clear_calls):
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [
                    _flag(
                        aurora_pid,
                        kind="unavailable",
                        why="not offered after stockout",
                        route="auto",
                        fix={"action": "substitute", "product_id": "sub-aurora-sim"},
                    )
                ],
            }
        if cust == "hotel-aurora":
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [
                    _flag(
                        "463982",
                        kind="price",
                        why="price moved +8.0%",
                        route="approval",
                        fix={"action": "substitute", "product_id": "sub-aurora"},
                    ),
                    _flag(
                        "460884",
                        kind="mandate",
                        why="allergen added",
                        route="approval",
                        fix={"action": "remove_line"},
                    ),
                ],
            }
        return {
            "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
            "flags": [
                _flag(
                    "460396",
                    kind="qty_creep",
                    why="qty grew +50%",
                    route="approval",
                    fix={"action": "confirm_qty", "quantity": 8},
                ),
                _flag(
                    "461578",
                    kind="unavailable",
                    why="not offered",
                    route="auto",
                    fix={"action": "substitute", "product_id": "sub-school"},
                ),
            ],
        }

    def fake_violates(pid, excl):
        violates_calls.append((pid, set(excl)))
        return False

    def fake_simulate(c, kind, **kw):
        sim_calls.append((c.cust, kind, kw))

    def fake_clear(c):
        clear_calls.append(c.cust)

    r = FakeRun()
    s7.run_scenario(
        "http://mock-gateway",
        r,
        client_cls=_make_client_cls(mandates, seen_clients),
        check_fn=fake_check,
        violates_fn=fake_violates,
        simulate_fn=fake_simulate,
        clear_fn=fake_clear,
    )

    assert seen_clients == [
        ("http://mock-gateway", "hotel-aurora"),
        ("http://mock-gateway", "school-hervanta"),
        ("http://mock-gateway", "hotel-aurora"),
    ]
    assert [c[:3] for c in check_calls] == [
        ("hotel-aurora", KEYS["hotel-aurora"], "http://mock-gateway"),
        ("school-hervanta", KEYS["school-hervanta"], "http://mock-gateway"),
        ("hotel-aurora", KEYS["hotel-aurora"], "http://mock-gateway"),
        ("hotel-aurora", KEYS["hotel-aurora"], "http://mock-gateway"),
    ]
    assert [c[4] for c in check_calls] == [False, False, False, True]
    assert check_calls[2][3] == check_calls[3][3]
    assert len({c[3] for c in check_calls}) == 3
    assert violates_calls == [
        ("sub-aurora", {"peanuts"}),
        ("sub-school", {"nuts", "egg"}),
    ]
    assert sim_calls == [
        ("hotel-aurora", "stockout", {"product_id": aurora_pid, "available_amount": 0}),
    ]
    assert clear_calls == ["hotel-aurora"]
    assert r.said == ["hotel-aurora: 2 flags", "school-hervanta: 2 flags"]
    assert [label for label, _, _ in r.results] == [
        "hotel-aurora standing order returned",
        "hotel-aurora flags have a why and a route",
        "hotel-aurora substitutes respect mandate allergens",
        "hotel-aurora allergen flags never auto",
        "school-hervanta standing order returned",
        "school-hervanta flags have a why and a route",
        "school-hervanta substitutes respect mandate allergens",
        "school-hervanta allergen flags never auto",
        "injected stock-out is flagged for that product",
    ]
    assert len(r.results) == 9
    assert all(ok for _, ok, _ in r.results)
    assert all(not os.path.exists(td) for td in temp_dirs)


def test_s7_tempdirs_cleaned_up_when_run_scenario_raises():
    mandates = {
        "hotel-aurora": {"exclude_allergens": []},
        "school-hervanta": {"exclude_allergens": []},
    }
    loop_dirs = []

    def boom_in_loop(cust, key, base, state_path=None):
        td = os.path.dirname(state_path)
        assert os.path.isdir(td)
        Path(state_path).write_text("{}", encoding="utf-8")
        loop_dirs.append(td)
        if cust == "school-hervanta":
            raise RuntimeError("loop check boom")
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": []}

    with pytest.raises(RuntimeError, match="loop check boom"):
        s7.run_scenario(
            "http://mock",
            FakeRun(),
            client_cls=_make_client_cls(mandates),
            check_fn=boom_in_loop,
            violates_fn=lambda pid, excl: False,
            simulate_fn=lambda c, kind, **kw: None,
            clear_fn=lambda c: None,
        )
    assert len(loop_dirs) == 2
    assert all(not os.path.exists(td) for td in loop_dirs)

    sim_dirs = []
    counts = {"sim": 0, "clear": 0}

    def boom_after_simulate(cust, key, base, state_path=None):
        td = os.path.dirname(state_path)
        assert os.path.isdir(td)
        Path(state_path).write_text("{}", encoding="utf-8")
        sim_dirs.append(td)
        if counts["sim"] > counts["clear"]:
            raise RuntimeError("post-sim boom")
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": []}

    with pytest.raises(RuntimeError, match="post-sim boom"):
        s7.run_scenario(
            "http://mock",
            FakeRun(),
            client_cls=_make_client_cls(mandates),
            check_fn=boom_after_simulate,
            violates_fn=lambda pid, excl: False,
            simulate_fn=lambda c, kind, **kw: counts.__setitem__("sim", counts["sim"] + 1),
            clear_fn=lambda c: counts.__setitem__("clear", counts["clear"] + 1),
        )
    assert counts == {"sim": 1, "clear": 1}
    assert len(set(sim_dirs)) == 3
    assert all(not os.path.exists(td) for td in sim_dirs)

    sim_raise_dirs = []
    clear_after_sim_err = []

    def record_check(cust, key, base, state_path=None):
        td = os.path.dirname(state_path)
        Path(state_path).write_text("{}", encoding="utf-8")
        sim_raise_dirs.append(td)
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": []}

    def boom_simulate(c, kind, **kw):
        raise RuntimeError("simulate endpoint failed")

    with pytest.raises(RuntimeError, match="simulate endpoint failed"):
        s7.run_scenario(
            "http://mock",
            FakeRun(),
            client_cls=_make_client_cls(mandates),
            check_fn=record_check,
            violates_fn=lambda pid, excl: False,
            simulate_fn=boom_simulate,
            clear_fn=lambda c: clear_after_sim_err.append(c.cust),
        )
    assert clear_after_sim_err == ["hotel-aurora"]
    assert len(sim_raise_dirs) == 3
    assert all(not os.path.exists(td) for td in sim_raise_dirs)


def test_s7_empty_standing_order_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": []},
        "school-hervanta": {"exclude_allergens": []},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    calls = {"n": 0}

    def fake_check(cust, key, base, state_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"standing_order": {"lines": []}, "flags": []}
        if calls["n"] == 4:
            return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": [_flag(aurora_pid)]}
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": []}

    r = FakeRun()
    s7.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        check_fn=fake_check,
        violates_fn=lambda pid, excl: False,
        simulate_fn=lambda c, kind, **kw: None,
        clear_fn=lambda c: None,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora standing order returned"] is False
    assert by_label["school-hervanta standing order returned"] is True


def test_s7_missing_why_or_invalid_route_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": []},
        "school-hervanta": {"exclude_allergens": []},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    calls = {"n": 0}

    def fake_check(cust, key, base, state_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [_flag("463982", why="", route="auto")],
            }
        if calls["n"] == 2:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [_flag("460396", why="valid reason", route="manual_review")],
            }
        return {
            "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
            "flags": [_flag(aurora_pid)],
        }

    r = FakeRun()
    s7.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        check_fn=fake_check,
        violates_fn=lambda pid, excl: False,
        simulate_fn=lambda c, kind, **kw: None,
        clear_fn=lambda c: None,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora flags have a why and a route"] is False
    assert by_label["school-hervanta flags have a why and a route"] is False
    assert by_label["injected stock-out is flagged for that product"] is True


def test_s7_substitute_violating_allergens_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["Peanuts"]},
        "school-hervanta": {"exclude_allergens": ["Nuts"]},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    calls = {"n": 0}

    def fake_check(cust, key, base, state_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [
                    _flag("463982", fix={"action": "substitute", "product_id": "safe-sub"}),
                ],
            }
        if calls["n"] == 2:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [
                    _flag("460396", fix={"action": "substitute", "product_id": "nutty-sub"}),
                ],
            }
        return {
            "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
            "flags": [_flag(aurora_pid)],
        }

    r = FakeRun()
    s7.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        check_fn=fake_check,
        violates_fn=lambda pid, excl: pid == "nutty-sub" and "nuts" in excl,
        simulate_fn=lambda c, kind, **kw: None,
        clear_fn=lambda c: None,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora substitutes respect mandate allergens"] is True
    assert by_label["school-hervanta substitutes respect mandate allergens"] is False


def test_s7_mandate_flag_with_auto_route_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["egg"]},
        "school-hervanta": {"exclude_allergens": ["nuts"]},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    calls = {"n": 0}

    def fake_check(cust, key, base, state_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [_flag("463982", kind="mandate", why="contains egg", route="auto")],
            }
        if calls["n"] == 2:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [_flag("460396", kind="mandate", why="contains nuts", route="approval")],
            }
        return {
            "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
            "flags": [_flag(aurora_pid)],
        }

    r = FakeRun()
    s7.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        check_fn=fake_check,
        violates_fn=lambda pid, excl: False,
        simulate_fn=lambda c, kind, **kw: None,
        clear_fn=lambda c: None,
    )
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["hotel-aurora flags have a why and a route"] is True
    assert by_label["hotel-aurora allergen flags never auto"] is False
    assert by_label["school-hervanta allergen flags never auto"] is True


def test_s7_unflagged_stockout_caught():
    mandates = {
        "hotel-aurora": {"exclude_allergens": []},
        "school-hervanta": {"exclude_allergens": []},
    }
    calls = {"n": 0}

    def fake_check(cust, key, base, state_path=None):
        calls["n"] += 1
        if calls["n"] == 4:
            return {
                "standing_order": {"lines": drift.SEEDS[cust]["lines"]},
                "flags": [_flag("unrelated-product", kind="price", why="price moved", route="auto")],
            }
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": []}

    r = FakeRun()
    s7.run_scenario(
        "http://mock",
        r,
        client_cls=_make_client_cls(mandates),
        check_fn=fake_check,
        violates_fn=lambda pid, excl: False,
        simulate_fn=lambda c, kind, **kw: None,
        clear_fn=lambda c: None,
    )
    assert len(r.results) == 9
    assert all(ok for _, ok, _ in r.results[:8])
    assert r.results[8][:2] == ("injected stock-out is flagged for that product", False)


def test_s7_go_wiring_and_dynamic_module_defaults(monkeypatch):
    mandates = {
        "hotel-aurora": {"exclude_allergens": ["peanuts"]},
        "school-hervanta": {"exclude_allergens": ["nuts"]},
    }
    aurora_pid = drift.SEEDS["hotel-aurora"]["lines"][0]["product_id"]
    check_calls = []
    temp_dirs = []
    counts = {"sim": 0, "clear": 0}

    def fake_check(cust, key, base, state_path=None):
        check_calls.append((cust, key, base))
        temp_dirs.append(os.path.dirname(state_path))
        flags = [_flag(aurora_pid)] if counts["sim"] > counts["clear"] else []
        return {"standing_order": {"lines": drift.SEEDS[cust]["lines"]}, "flags": flags}

    r1 = FakeRun()
    monkeypatch.setattr(s7, "base", "http://mock-go")
    monkeypatch.setattr(s7, "r", r1)
    monkeypatch.setattr(s7, "Client", _make_client_cls(mandates))
    monkeypatch.setattr(drift, "check", fake_check)
    monkeypatch.setattr(s7, "violates", lambda pid, excl: False)
    monkeypatch.setattr(s7, "simulate", lambda c, kind, **kw: counts.__setitem__("sim", counts["sim"] + 1))
    monkeypatch.setattr(s7, "clear_simulation", lambda c: counts.__setitem__("clear", counts["clear"] + 1))

    s7.go()
    assert len(check_calls) == 4
    assert all(b == "http://mock-go" for _, _, b in check_calls)
    assert counts == {"sim": 1, "clear": 1}
    assert len(r1.results) == 9
    assert all(ok for _, ok, _ in r1.results)
    assert all(not os.path.exists(td) for td in temp_dirs)

    created_runs = []

    def fake_run_factory(name, base_url):
        fr = FakeRun()
        created_runs.append((name, base_url, fr))
        return fr

    monkeypatch.setattr(s7, "base", None)
    monkeypatch.setattr(s7, "r", None)
    monkeypatch.setattr(s7, "parse_base", lambda: "http://parsed-cli")
    monkeypatch.setattr(s7, "Run", fake_run_factory)

    s7.go()
    assert len(created_runs) == 1
    assert created_runs[0][:2] == ("s7_drift", "http://parsed-cli")
    assert len(created_runs[0][2].results) == 9
    assert all(ok for _, ok, _ in created_runs[0][2].results)
    assert [b for _, _, b in check_calls[4:]] == ["http://parsed-cli"] * 4
    assert counts == {"sim": 2, "clear": 2}
    assert all(not os.path.exists(td) for td in temp_dirs)
