import json
from datetime import date

import pytest

from agents import drift
from scenarios import _common, s2_need_recipe_order, s4_stockout
from scenarios.s2_need_recipe_order import run_scenario as run_s2
from scenarios.s4_stockout import run_scenario as run_s4

S2_LABELS = [
    "dish became several ingredient lines",
    "every line has quantity and unit",
    "lasagne basics present",
    "quantities scale with portions",
    "offers found for most lines",
    "quote has a line per picked ingredient",
    "order has Decision",
]

S4_LABELS = [
    "order placed before fault",
    "same order moved to pending_approval",
    "decision has STOCKOUT reason with text",
    "substitution or split present",
    "substitutes and lines allergen-safe",
    "substitutions carry a reason",
    "audit has a stockout event with a why",
    "watcher reports stockout_handled",
    "watcher places no duplicate order",
    "human approval -> placed",
]


def _clear_common_cache():
    for name in ("product_db", "_product_db"):
        fn = getattr(_common, name, None)
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture(autouse=True)
def synthetic_product_db(tmp_path, monkeypatch):
    path = tmp_path / "valio_aimo_junction26_product_data.json"
    path.write_text(
        json.dumps([
            {"id": "p-milk-1", "synkkaData": {"classifications": []}},
            {"id": "p-milk-2", "synkkaData": {"classifications": []}},
            {"id": "p-milk-3", "synkkaData": {"classifications": []}},
            {
                "id": "p-nutty",
                "synkkaData": {
                    "classifications": [
                        {"name": "allergen", "values": [{"id": "AP", "unit": "CONTAINS"}]}
                    ]
                },
            },
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_common, "DATA", str(path), raising=False)
    _clear_common_cache()
    yield
    _clear_common_cache()


class FakeRun:
    def __init__(self):
        self.said = []
        self.results = []

    def say(self, text):
        self.said.append(text)

    def check(self, label, ok, detail=""):
        self.results.append((label, bool(ok), detail))
        return bool(ok)

    def by_label(self):
        return {label: ok for label, ok, _ in self.results}


class FakeS2Client:
    def __init__(
        self,
        items_40=None,
        items_20=None,
        offered_indices=None,
        quote_lines=None,
        outcome="auto",
    ):
        self.items_40 = items_40 if items_40 is not None else [
            {"query": "lasagne pasta sheets", "quantity": 4.0, "unit": "kg"},
            {"query": "minced beef", "quantity": 6.0, "unit": "kg"},
            {"query": "crushed tomatoes", "quantity": 4.0, "unit": "kg"},
            {"query": "lactose-free milk", "quantity": 3.0, "unit": "L"},
        ]
        self.items_20 = items_20 if items_20 is not None else [
            {"query": i["query"], "quantity": i["quantity"] / 2.0, "unit": i["unit"]}
            for i in self.items_40
        ]
        self.offered_indices = (
            set(range(len(self.items_40))) if offered_indices is None else set(offered_indices)
        )
        self.quote_lines = quote_lines
        self.outcome = outcome
        self.parse_calls = []
        self.quoted_picks = None
        self.quoted_day = None

    def parse(self, text, customer):
        self.parse_calls.append((text, customer))
        items = self.items_20 if "20 portions" in text else self.items_40
        return {"customer": customer, "items": [dict(x) for x in items]}

    def offers(self, need):
        out = []
        for idx, it in enumerate(need["items"]):
            if idx in self.offered_indices:
                out.append({
                    "need_item": it,
                    "offers": [{"product_id": f"prod-{idx}", "units_needed": 2, "substitute_for": None}],
                })
            else:
                out.append({"need_item": it, "offers": []})
        return {"items": out}

    def quote(self, picks, delivery_date):
        self.quoted_picks = list(picks)
        self.quoted_day = str(delivery_date)
        lines = self.quote_lines if self.quote_lines is not None else list(picks)
        return {"quote_id": "q_s2", "jws": "h.p.s", "lines": lines}

    def order(self, quote):
        return {
            "order_id": "ord_s2",
            "status": "placed" if self.outcome == "auto" else "pending_approval",
            "decision": {"outcome": self.outcome, "reasons": []},
        }


class FakeS4Client:
    def __init__(
        self,
        initial_status="confirmed",
        after_status="pending_approval",
        after_reasons=None,
        after_lines=None,
        after_subs=None,
        audit_events=None,
        mandate_excl=None,
        fail_on_get_order=False,
        final_status="confirmed",
    ):
        self.initial_status = initial_status
        self.after_status = after_status
        self.after_reasons = (
            after_reasons
            if after_reasons is not None
            else [{"code": "STOCKOUT", "text": "Stock dropped after the order."}]
        )
        self.after_lines = (
            after_lines
            if after_lines is not None
            else [{"product_id": "p-milk-2", "quantity": 20}]
        )
        self.after_subs = (
            after_subs
            if after_subs is not None
            else [
                {
                    "original_id": "p-milk-1",
                    "substitute_id": "p-milk-2",
                    "reason": "Stock dropped, refilled from the next offer.",
                }
            ]
        )
        self.audit_events = (
            audit_events
            if audit_events is not None
            else [
                {"action": "order.placed", "why": "Submitted."},
                {"action": "order.stockout", "why": "Stock dropped after the order."},
            ]
        )
        self.mandate_excl = mandate_excl if mandate_excl is not None else ["nuts", "peanuts"]
        self.fail_on_get_order = fail_on_get_order
        self.final_status = final_status
        self.orders_list = []
        self.decide_calls = []
        self.sim_calls = []
        self.clear_calls = 0
        self.sim_active = False

    def parse(self, text, customer):
        return {
            "customer": customer,
            "items": [{"query": "lactose-free milk", "quantity": 200, "unit": "L"}],
        }

    def offers(self, need):
        return {
            "items": [
                {
                    "offers": [
                        {
                            "product_id": "p-milk-1",
                            "units_needed": 20,
                            "available_amount": 50,
                            "substitute_for": None,
                        }
                    ]
                }
            ]
        }

    def quote(self, picks, delivery_date):
        return {"quote_id": "q_s4", "jws": "h.p.s", "lines": list(picks)}

    def order(self, quote):
        rec = {
            "order_id": "ord_s4",
            "status": self.initial_status,
            "lines": [{"product_id": "p-milk-1", "quantity": 20}],
            "substitutions": [],
            "decision": {"outcome": "auto", "reasons": []},
        }
        self.orders_list = [dict(rec)]
        return rec

    def decide(self, order_id, approve, by=None, note="scenario"):
        self.decide_calls.append((order_id, approve, by, note, self.sim_active))
        status = "confirmed" if note == "initial ok" else self.final_status
        return {
            "order_id": order_id,
            "status": status,
            "lines": [{"product_id": "p-milk-1", "quantity": 20}],
        }

    def audit(self, order_id):
        return {"events": list(self.audit_events)}

    def j(self, method, path, **kw):
        if (method, path) == ("GET", "/v1/mandate"):
            return {"exclude_allergens": list(self.mandate_excl), "approvers": ["Laura Virtanen"]}
        if (method, path) == ("POST", "/v1/admin/simulate"):
            self.sim_calls.append(dict(kw["json"]))
            self.sim_active = True
            return {"ok": True}
        if (method, path) == ("GET", "/v1/orders/ord_s4"):
            if self.fail_on_get_order:
                raise RuntimeError("boom on GET /v1/orders/ord_s4")
            snap = {
                "order_id": "ord_s4",
                "status": self.after_status,
                "decision": {"outcome": "needs_approval", "reasons": list(self.after_reasons)},
                "lines": [dict(x) for x in self.after_lines],
                "substitutions": [dict(x) for x in self.after_subs],
            }
            self.orders_list = [snap] + self.orders_list[1:]
            return snap
        if (method, path) == ("GET", "/v1/orders"):
            assert kw.get("params") == {"customer": "hotel-aurora"}
            return list(self.orders_list)
        raise AssertionError(f"unexpected j({method}, {path}, {kw})")

    def call(self, method, path, **kw):
        if (method, path) == ("DELETE", "/v1/admin/simulate"):
            self.clear_calls += 1
            self.sim_active = False
            return None
        raise AssertionError(f"unexpected call({method}, {path}, {kw})")


def _ok_watcher(customer, key, url, state_path=None):
    assert customer == "hotel-aurora"
    assert state_path is not None
    return [
        {
            "order_id": "ord_s4",
            "product_id": None,
            "kind": "stockout_handled",
            "new_order_id": None,
            "why": "Stock dropped after the order.",
        }
    ]


def test_modules_do_not_execute_on_import():
    assert s2_need_recipe_order.base is None
    assert s2_need_recipe_order.r is None
    assert callable(s2_need_recipe_order.run_scenario)
    assert callable(s2_need_recipe_order.go)
    assert s4_stockout.base is None
    assert s4_stockout.r is None
    assert callable(s4_stockout.run_scenario)
    assert callable(s4_stockout.go)


@pytest.mark.parametrize("outcome", ["auto", "needs_approval"])
def test_s2_happy_path_all_7_checks_pass(outcome):
    c = FakeS2Client(outcome=outcome)
    r = FakeRun()
    run_s2(c, r, day=date(2026, 11, 16))
    assert [label for label, _, _ in r.results] == S2_LABELS
    assert len(r.results) == 7
    assert all(ok for _, ok, _ in r.results), r.results
    assert len(c.parse_calls) == 2
    assert "40 portions" in c.parse_calls[0][0]
    assert "20 portions" in c.parse_calls[1][0]
    assert c.quoted_day == "2026-11-16"


def test_s2_default_day_works():
    c = FakeS2Client()
    r = FakeRun()
    run_s2(c, r)
    assert len(r.results) == 7
    assert all(ok for _, ok, _ in r.results)


def test_s2_scaling_mismatch_detected():
    bad_half = [
        {"query": "lasagne pasta sheets", "quantity": 2.0, "unit": "kg"},
        {"query": "minced beef", "quantity": 4.5, "unit": "kg"},
        {"query": "crushed tomatoes", "quantity": 2.0, "unit": "kg"},
        {"query": "lactose-free milk", "quantity": 1.5, "unit": "L"},
    ]
    c = FakeS2Client(items_20=bad_half)
    r = FakeRun()
    run_s2(c, r, day=date(2026, 11, 16))
    res = r.by_label()
    assert res["quantities scale with portions"] is False
    assert all(res[k] for k in S2_LABELS if k != "quantities scale with portions")

    missing_half = bad_half[:3]
    c2 = FakeS2Client(items_20=missing_half)
    r2 = FakeRun()
    run_s2(c2, r2, day=date(2026, 11, 16))
    assert r2.by_label()["quantities scale with portions"] is False


@pytest.mark.parametrize(
    "n_items,offered,expected",
    [
        (4, {0, 1}, True),
        (5, {0, 2}, True),
        (4, {0}, False),
        (4, set(), False),
    ],
)
def test_s2_partial_ingredient_offer_coverage(n_items, offered, expected):
    base_queries = [
        "lasagne pasta",
        "minced beef",
        "tomato sauce",
        "milk",
        "cheese",
    ][:n_items]
    items_40 = [{"query": q, "quantity": 4.0, "unit": "kg"} for q in base_queries]
    c = FakeS2Client(items_40=items_40, offered_indices=offered)
    r = FakeRun()
    run_s2(c, r, day=date(2026, 11, 16))
    res = r.by_label()
    assert res["offers found for most lines"] is expected
    if expected:
        assert all(ok for _, ok, _ in r.results), r.results


@pytest.mark.parametrize(
    "queries,expected",
    [
        (["boiled potato", "minced beef", "tomato"], False),
        (["lasagne sheets", "smoked tofu", "tomato"], False),
        (["lasagna sheets", "ground meat", "tomato"], True),
        (["pasta sheets", "mince", "tomato"], True),
    ],
)
def test_s2_missing_pasta_or_meat_detection(queries, expected):
    items_40 = [{"query": q, "quantity": 2.0, "unit": "kg"} for q in queries]
    c = FakeS2Client(items_40=items_40)
    r = FakeRun()
    run_s2(c, r, day=date(2026, 11, 16))
    assert r.by_label()["lasagne basics present"] is expected


def test_s2_line_count_unit_quote_and_decision_failures():
    short_items = [
        {"query": "lasagne pasta", "quantity": 2.0, "unit": ""},
        {"query": "beef", "quantity": 2.0, "unit": "kg"},
    ]
    c = FakeS2Client(items_40=short_items, quote_lines=[], outcome="blocked")
    r = FakeRun()
    run_s2(c, r, day=date(2026, 11, 16))
    res = r.by_label()
    assert res["dish became several ingredient lines"] is False
    assert res["every line has quantity and unit"] is False
    assert res["quote has a line per picked ingredient"] is False
    assert res["order has Decision"] is False


@pytest.mark.parametrize("initial_status,expected_decides", [("placed", 1), ("confirmed", 1), ("pending_approval", 2)])
def test_s4_happy_path_auto_and_pending_initial_order(initial_status, expected_decides):
    c = FakeS4Client(initial_status=initial_status)
    r = FakeRun()
    run_s4(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: False,
    )
    assert [label for label, _, _ in r.results] == S4_LABELS
    assert len(r.results) == 10
    assert all(ok for _, ok, _ in r.results), r.results
    assert c.sim_calls == [{"product_id": "p-milk-1", "available_amount": 0}]
    assert c.clear_calls == 1
    assert c.sim_active is False
    assert len(c.decide_calls) == expected_decides
    if initial_status == "pending_approval":
        assert c.decide_calls[0][3] == "initial ok"
        assert c.decide_calls[0][4] is False
    assert c.decide_calls[-1][3] == "approve reaction"
    assert c.decide_calls[-1][4] is False


def test_s4_split_without_subs_vs_subs_path():
    c_split = FakeS4Client(
        after_subs=[],
        after_lines=[
            {"product_id": "p-milk-1", "quantity": 8},
            {"product_id": "p-milk-3", "quantity": 12},
        ],
    )
    r_split = FakeRun()
    run_s4(
        c_split,
        r_split,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: False,
    )
    assert all(ok for _, ok, _ in r_split.results), r_split.results

    c_reduced = FakeS4Client(
        after_subs=[],
        after_lines=[{"product_id": "p-milk-1", "quantity": 10}],
    )
    r_reduced = FakeRun()
    run_s4(
        c_reduced,
        r_reduced,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: False,
    )
    assert all(ok for _, ok, _ in r_reduced.results), r_reduced.results

    c_unchanged = FakeS4Client(
        after_subs=[],
        after_lines=[{"product_id": "p-milk-1", "quantity": 20}],
    )
    r_unchanged = FakeRun()
    run_s4(
        c_unchanged,
        r_unchanged,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: False,
    )
    assert r_unchanged.by_label()["substitution or split present"] is False

    c_no_reason = FakeS4Client(
        after_subs=[{"original_id": "p-milk-1", "substitute_id": "p-milk-2", "reason": ""}],
    )
    r_no_reason = FakeRun()
    run_s4(
        c_no_reason,
        r_no_reason,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: False,
    )
    assert r_no_reason.by_label()["substitutions carry a reason"] is False


def test_s4_allergen_violation_caught_in_subs_and_after_lines():
    c_bad_sub = FakeS4Client(
        after_subs=[{"original_id": "p-milk-1", "substitute_id": "p-nutty", "reason": "swap"}],
        after_lines=[{"product_id": "p-milk-2", "quantity": 20}],
    )
    r_bad_sub = FakeRun()
    run_s4(
        c_bad_sub,
        r_bad_sub,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
        violates_fn=lambda pid, excl: pid == "p-nutty" and "nuts" in excl,
    )
    assert r_bad_sub.by_label()["substitutes and lines allergen-safe"] is False

    c_bad_line = FakeS4Client(
        after_subs=[],
        after_lines=[
            {"product_id": "p-milk-1", "quantity": 10},
            {"product_id": "p-nutty", "quantity": 10},
        ],
    )
    r_bad_line = FakeRun()
    run_s4(
        c_bad_line,
        r_bad_line,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=_ok_watcher,
    )
    assert r_bad_line.by_label()["substitutes and lines allergen-safe"] is False


def test_s4_duplicate_order_from_watcher_caught():
    c_extra_order = FakeS4Client()
    r_extra_order = FakeRun()

    def spawns_order_watcher(customer, key, url, state_path=None):
        c_extra_order.orders_list.append({"order_id": "ord_dup", "status": "pending_approval"})
        return [
            {
                "order_id": "ord_s4",
                "kind": "stockout_handled",
                "new_order_id": None,
            }
        ]

    run_s4(
        c_extra_order,
        r_extra_order,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=spawns_order_watcher,
        violates_fn=lambda pid, excl: False,
    )
    res_extra = r_extra_order.by_label()
    assert res_extra["watcher reports stockout_handled"] is True
    assert res_extra["watcher places no duplicate order"] is False

    c_new_id = FakeS4Client()
    r_new_id = FakeRun()

    def returns_new_id_watcher(customer, key, url, state_path=None):
        return [
            {
                "order_id": "ord_s4",
                "kind": "stockout_handled",
                "new_order_id": "ord_dup_2",
            }
        ]

    run_s4(
        c_new_id,
        r_new_id,
        "http://test.local",
        day=date(2026, 11, 16),
        watch_fn=returns_new_id_watcher,
        violates_fn=lambda pid, excl: False,
    )
    res_new_id = r_new_id.by_label()
    assert res_new_id["watcher reports stockout_handled"] is False
    assert res_new_id["watcher places no duplicate order"] is False


def test_s4_clear_simulation_guaranteed_in_finally_when_get_order_raises():
    c = FakeS4Client(fail_on_get_order=True)
    r = FakeRun()
    with pytest.raises(RuntimeError, match="boom on GET"):
        run_s4(
            c,
            r,
            "http://test.local",
            day=date(2026, 11, 16),
            watch_fn=_ok_watcher,
            violates_fn=lambda pid, excl: False,
        )
    assert c.sim_calls == [{"product_id": "p-milk-1", "available_amount": 0}]
    assert c.clear_calls == 1
    assert c.sim_active is False


def test_s4_with_real_drift_watch_orders_offline():
    c = FakeS4Client()
    r = FakeRun()

    class FakeHttpResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return self

        def json(self):
            return self._data

    class FakeDriftHttp:
        def get(self, path, params=None):
            assert path == "/v1/orders"
            return FakeHttpResp(c.orders_list)

    run_s4(
        c,
        r,
        "http://test.local",
        watch_fn=lambda cust, key, url, state_path=None: drift.watch_orders(
            cust, key, url, state_path=state_path, client=FakeDriftHttp()
        ),
    )
    assert len(r.results) == 10
    assert all(ok for _, ok, _ in r.results), r.results
