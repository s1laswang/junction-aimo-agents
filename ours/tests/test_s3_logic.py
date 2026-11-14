import json
import sys
from datetime import date

import pytest

sys.path.insert(0, "scenarios")
import _common
from s3_event_substitution import run_scenario


class FakeRun:
    def __init__(self):
        self.said = []
        self.results = []

    def say(self, text):
        self.said.append(text)

    def check(self, label, ok, detail=""):
        self.results.append((label, bool(ok), detail))
        return bool(ok)


class FakeClient:
    def __init__(self, natural_sub=False, produce_sub_on_sim=True, fail_step=None):
        self.natural_sub = natural_sub
        self.produce_sub_on_sim = produce_sub_on_sim
        self.fail_step = fail_step
        self.simulated = {}
        self.sim_calls = []
        self.clear_calls = 0
        self.offers_calls = 0
        self.quoted_picks = None
        self.quoted_day = None
        self.decided = []
        self.parsed = []

    def parse(self, text, customer):
        self.parsed.append((text, customer))
        return {
            "customer": customer,
            "deliver_by": "2026-11-17",
            "items": [
                {"query": "pasta", "quantity": 40, "unit": "kg"},
                {"query": "bacon", "quantity": 16, "unit": "kg"},
                {"query": "eggs", "quantity": 16, "unit": "kg"},
            ],
        }

    def offers(self, need):
        self.offers_calls += 1
        if self.offers_calls == 2 and self.fail_step == "refetch":
            raise RuntimeError("refetch boom")
        if self.natural_sub:
            pasta_offers = [
                {"product_id": "p-pasta-1", "units_needed": 2, "substitute_for": None, "notes": []},
                {"product_id": "p-pasta-2", "units_needed": 6, "substitute_for": "p-pasta-1", "notes": ["swap"]},
                {"product_id": "p-zero", "units_needed": 0, "substitute_for": None, "notes": []},
            ]
            pasta_alts = []
        elif "p-pasta-1" in self.simulated and self.produce_sub_on_sim:
            amt = self.simulated["p-pasta-1"]
            pasta_offers = [
                {"product_id": "p-pasta-1", "units_needed": amt, "substitute_for": None, "notes": []},
                {"product_id": "p-pasta-2", "units_needed": 8 - amt, "substitute_for": "p-pasta-1", "notes": ["10% pricier per kg"]},
            ]
            pasta_alts = []
        else:
            pasta_offers = [{"product_id": "p-pasta-1", "units_needed": 8, "substitute_for": None, "notes": []}]
            pasta_alts = [{"product_id": "p-pasta-2", "units_needed": 8, "substitute_for": None, "notes": ["alt"]}]
        return {
            "items": [
                {"shortfall": 0, "offers": pasta_offers, "alternatives": pasta_alts},
                {"shortfall": 0, "offers": [{"product_id": "p-bacon", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                {"shortfall": 0, "offers": [{"product_id": "p-eggs", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
            ]
        }

    def quote(self, picks, delivery_date):
        if self.fail_step == "quote":
            raise RuntimeError("quote boom")
        self.quoted_picks = list(picks)
        self.quoted_day = str(delivery_date)
        return {"quote_id": "q_1", "jws": "h.p.s", "lines": picks}

    def order(self, quote):
        subs = [
            {
                "original_id": l["substitute_for"],
                "substitute_id": l["product_id"],
                "reason": f"Substitute for {l['substitute_for']}: {l['product_id']}.",
            }
            for l in quote["lines"]
            if l.get("substitute_for")
        ]
        return {"order_id": "ord_1", "status": "pending_approval", "substitutions": subs}

    def decide(self, order_id, approve, by=None, note="scenario"):
        self.decided.append((order_id, approve, by, note))
        subs = [
            {
                "original_id": l["substitute_for"],
                "substitute_id": l["product_id"],
                "reason": f"Substitute for {l['substitute_for']}: {l['product_id']}.",
            }
            for l in self.quoted_picks
            if l.get("substitute_for")
        ]
        return {"order_id": order_id, "status": "confirmed", "substitutions": subs}

    def audit(self, order_id):
        return {"events": [{"action": "order.placed", "why": "submitted"}, {"action": "order.confirmed", "why": "confirmed"}]}

    def j(self, method, path, **kw):
        if method == "POST" and path == "/v1/admin/simulate":
            body = kw["json"]
            self.sim_calls.append(body)
            self.simulated[body["product_id"]] = body["available_amount"]
            return {"ok": True}
        if method == "GET" and path == "/v1/mandate":
            return {"exclude_allergens": ["nuts"], "approvers": ["Laura Virtanen"]}
        raise AssertionError(f"unexpected {method} {path}")

    def call(self, method, path, **kw):
        if method == "DELETE" and path == "/v1/admin/simulate":
            self.clear_calls += 1
            self.simulated.clear()
            return None
        raise AssertionError(f"unexpected {method} {path}")


def _clear_common_cache():
    for name in ("product_db", "_product_db"):
        fn = getattr(_common, name, None)
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture(autouse=True)
def fake_product_db(tmp_path, monkeypatch):
    p = tmp_path / "valio_aimo_junction26_product_data.json"
    p.write_text(
        json.dumps([
            {"id": "p-pasta-1", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "UW", "unit": "CONTAINS"}]}]}},
            {"id": "p-pasta-2", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "UW", "unit": "CONTAINS"}]}]}},
            {"id": "p-bacon", "synkkaData": {"classifications": []}},
            {"id": "p-eggs", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "AE", "unit": "CONTAINS"}]}]}},
            {"id": "p-eggs-2", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "AE", "unit": "CONTAINS"}]}]}},
            {"id": "p-pasta-nut", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "AN", "unit": "CONTAINS"}]}]}},
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_common, "DATA", str(p), raising=False)
    _clear_common_cache()
    yield
    _clear_common_cache()


def test_natural_substitution():
    c = FakeClient(natural_sub=True)
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.offers_calls == 1
    assert c.sim_calls == []
    assert c.clear_calls == 0
    assert {"product_id": "p-pasta-1", "quantity": 2, "substitute_for": None} in c.quoted_picks
    assert {"product_id": "p-pasta-2", "quantity": 6, "substitute_for": "p-pasta-1"} in c.quoted_picks
    assert all(p["product_id"] != "p-zero" for p in c.quoted_picks)
    assert c.decided == [("ord_1", True, "scenario-human", "event ok")]
    assert all(ok for _, ok, _ in r.results)
    labels = [label for label, _, _ in r.results]
    assert "at least one substitute offer" in labels
    assert "substitution recorded with reason" in labels


def test_simulated_partial_stock_substitution_and_cleanup():
    c = FakeClient(natural_sub=False)
    r = FakeRun()
    run_scenario(c, r)
    assert c.offers_calls == 2
    assert c.sim_calls == [{"product_id": "p-pasta-1", "available_amount": 1}]
    assert c.clear_calls == 1
    assert c.simulated == {}
    assert {"product_id": "p-pasta-1", "quantity": 1, "substitute_for": None} in c.quoted_picks
    assert {"product_id": "p-pasta-2", "quantity": 7, "substitute_for": "p-pasta-1"} in c.quoted_picks
    assert all(ok for _, ok, _ in r.results)
    labels = [label for label, _, _ in r.results]
    assert "at least one substitute offer" in labels
    assert "substitution recorded with reason" in labels


@pytest.mark.parametrize("fail_step", ["refetch", "quote"])
def test_simulated_cleanup_in_finally_on_failure(fail_step):
    c = FakeClient(natural_sub=False, fail_step=fail_step)
    r = FakeRun()
    with pytest.raises(RuntimeError):
        run_scenario(c, r, day=date(2026, 11, 17))
    assert c.sim_calls == [{"product_id": "p-pasta-1", "available_amount": 1}]
    assert c.clear_calls == 1


def test_fails_when_alternatives_exist_but_no_substitute_returned():
    c = FakeClient(natural_sub=False, produce_sub_on_sim=False)
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.sim_calls == [{"product_id": "p-pasta-1", "available_amount": 1}]
    assert c.clear_calls == 1
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["at least one substitute offer"] is False


def test_selects_first_item_with_multiple_candidates_when_earlier_has_none():
    class MultiCandClient(FakeClient):
        def offers(self, need):
            self.offers_calls += 1
            if "p-bacon" in self.simulated:
                bacon_offers = [
                    {"product_id": "p-bacon", "units_needed": 1, "substitute_for": None, "notes": []},
                    {"product_id": "p-pasta-2", "units_needed": 3, "substitute_for": "p-bacon", "notes": ["swap"]},
                ]
            else:
                bacon_offers = [
                    {"product_id": "p-bacon", "units_needed": 2, "substitute_for": None, "notes": []},
                    {"product_id": "p-pasta-2", "units_needed": 2, "substitute_for": None, "notes": []},
                ]
            return {
                "items": [
                    {"shortfall": 0, "offers": [{"product_id": "p-pasta-1", "units_needed": 8, "substitute_for": None, "notes": []}], "alternatives": []},
                    {"shortfall": 0, "offers": bacon_offers, "alternatives": []},
                    {"shortfall": 0, "offers": [{"product_id": "p-eggs", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                ]
            }

    c = MultiCandClient()
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.sim_calls == [{"product_id": "p-bacon", "available_amount": 1}]
    assert c.clear_calls == 1
    assert all(ok for _, ok, _ in r.results)


def test_prefers_lower_score_alternative_candidate():
    class ScoreClient(FakeClient):
        def offers(self, need):
            self.offers_calls += 1
            eggs_offers = [{"product_id": "p-eggs", "units_needed": 2, "substitute_for": None, "notes": [], "match_score": 0.98}]
            eggs_alts = [{"product_id": "p-eggs-2", "units_needed": 2, "substitute_for": None, "notes": [], "match_score": 0.98}]
            if "p-pasta-1" in self.simulated:
                pasta_offers = [
                    {"product_id": "p-pasta-1", "units_needed": 1, "substitute_for": None, "notes": [], "match_score": 0.775},
                    {"product_id": "p-pasta-2", "units_needed": 5, "substitute_for": "p-pasta-1", "notes": ["swap"], "match_score": 0.77},
                ]
                pasta_alts = []
            else:
                pasta_offers = [{"product_id": "p-pasta-1", "units_needed": 6, "substitute_for": None, "notes": [], "match_score": 0.775}]
                pasta_alts = [{"product_id": "p-pasta-2", "units_needed": 6, "substitute_for": None, "notes": [], "match_score": 0.77}]
            return {
                "items": [
                    {"shortfall": 0, "offers": eggs_offers, "alternatives": eggs_alts},
                    {"shortfall": 0, "offers": [{"product_id": "p-bacon", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                    {"shortfall": 0, "offers": pasta_offers, "alternatives": pasta_alts},
                ]
            }

    c = ScoreClient()
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.sim_calls == [{"product_id": "p-pasta-1", "available_amount": 1}]
    assert c.clear_calls == 1
    assert all(ok for _, ok, _ in r.results)


def test_retries_next_candidate_when_first_sim_produces_no_substitute():
    class RetryClient(FakeClient):
        def offers(self, need):
            self.offers_calls += 1
            if "p-eggs" in self.simulated:
                eggs_offers = [
                    {"product_id": "p-eggs", "units_needed": 1, "substitute_for": None, "notes": []},
                    {"product_id": "p-eggs-2", "units_needed": 3, "substitute_for": None, "notes": []},
                ]
                eggs_alts = []
            else:
                eggs_offers = [{"product_id": "p-eggs", "units_needed": 4, "substitute_for": None, "notes": []}]
                eggs_alts = [{"product_id": "p-eggs-2", "units_needed": 4, "substitute_for": None, "notes": []}]
            if "p-pasta-1" in self.simulated:
                pasta_offers = [
                    {"product_id": "p-pasta-1", "units_needed": 1, "substitute_for": None, "notes": []},
                    {"product_id": "p-pasta-2", "units_needed": 7, "substitute_for": "p-pasta-1", "notes": ["swap"]},
                ]
                pasta_alts = []
            else:
                pasta_offers = [{"product_id": "p-pasta-1", "units_needed": 8, "substitute_for": None, "notes": []}]
                pasta_alts = [{"product_id": "p-pasta-2", "units_needed": 8, "substitute_for": None, "notes": []}]
            return {
                "items": [
                    {"shortfall": 0, "offers": eggs_offers, "alternatives": eggs_alts},
                    {"shortfall": 0, "offers": pasta_offers, "alternatives": pasta_alts},
                    {"shortfall": 0, "offers": [{"product_id": "p-bacon", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                ]
            }

    c = RetryClient()
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.sim_calls == [
        {"product_id": "p-eggs", "available_amount": 1},
        {"product_id": "p-pasta-1", "available_amount": 1},
    ]
    assert c.clear_calls == 2
    assert c.simulated == {}
    assert all(ok for _, ok, _ in r.results)


@pytest.mark.parametrize(
    "today_val,expected_day",
    [
        (date(2026, 11, 11), date(2026, 11, 13)),
        (date(2026, 11, 12), date(2026, 11, 16)),
        (date(2026, 11, 13), date(2026, 11, 16)),
        (date(2026, 11, 14), date(2026, 11, 16)),
        (date(2026, 11, 15), date(2026, 11, 17)),
    ],
)
def test_default_day_skips_weekends_via_next_delivery_day(monkeypatch, today_val, expected_day):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return today_val

    monkeypatch.setattr(_common, "date", FakeDate)
    c = FakeClient(natural_sub=True)
    r = FakeRun()
    run_scenario(c, r)
    assert c.quoted_day == expected_day.isoformat()
    assert f"by {expected_day.isoformat()}" in c.parsed[0][0]
    assert date.fromisoformat(c.quoted_day).weekday() < 5
    assert all(ok for _, ok, _ in r.results)


def test_multi_candidate_retry_clears_before_next_candidate_and_again_in_finally():
    class OrderedRetryClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.trace = []
            self.simulated_snapshot_on_sim = []

        def offers(self, need):
            self.offers_calls += 1
            self.trace.append("offers")
            if "p-eggs" in self.simulated:
                eggs_offers = [
                    {"product_id": "p-eggs", "units_needed": 1, "substitute_for": None, "notes": []},
                    {"product_id": "p-eggs-2", "units_needed": 3, "substitute_for": None, "notes": []},
                ]
                eggs_alts = []
            else:
                eggs_offers = [{"product_id": "p-eggs", "units_needed": 4, "substitute_for": None, "notes": []}]
                eggs_alts = [{"product_id": "p-eggs-2", "units_needed": 4, "substitute_for": None, "notes": []}]
            if "p-pasta-1" in self.simulated:
                pasta_offers = [
                    {"product_id": "p-pasta-1", "units_needed": 1, "substitute_for": None, "notes": []},
                    {"product_id": "p-pasta-2", "units_needed": 7, "substitute_for": "p-pasta-1", "notes": ["swap"]},
                ]
                pasta_alts = []
            else:
                pasta_offers = [{"product_id": "p-pasta-1", "units_needed": 8, "substitute_for": None, "notes": []}]
                pasta_alts = [{"product_id": "p-pasta-2", "units_needed": 8, "substitute_for": None, "notes": []}]
            bacon_offers = [{"product_id": "p-bacon", "units_needed": 4, "substitute_for": None, "notes": []}]
            bacon_alts = [{"product_id": "p-pasta-2", "units_needed": 4, "substitute_for": None, "notes": []}]
            return {
                "items": [
                    {"shortfall": 0, "offers": eggs_offers, "alternatives": eggs_alts},
                    {"shortfall": 0, "offers": pasta_offers, "alternatives": pasta_alts},
                    {"shortfall": 0, "offers": bacon_offers, "alternatives": bacon_alts},
                ]
            }

        def quote(self, picks, delivery_date):
            self.trace.append("quote")
            return super().quote(picks, delivery_date)

        def order(self, quote):
            self.trace.append("order")
            return super().order(quote)

        def decide(self, order_id, approve, by=None, note="scenario"):
            self.trace.append("decide")
            return super().decide(order_id, approve, by, note)

        def audit(self, order_id):
            self.trace.append("audit")
            return super().audit(order_id)

        def j(self, method, path, **kw):
            if method == "POST" and path == "/v1/admin/simulate":
                pid = kw["json"]["product_id"]
                self.simulated_snapshot_on_sim.append((pid, dict(self.simulated)))
                self.trace.append(f"simulate:{pid}")
            return super().j(method, path, **kw)

        def call(self, method, path, **kw):
            if method == "DELETE" and path == "/v1/admin/simulate":
                self.trace.append("clear")
            return super().call(method, path, **kw)

    c = OrderedRetryClient()
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    assert c.simulated_snapshot_on_sim == [
        ("p-eggs", {}),
        ("p-pasta-1", {}),
    ]
    assert c.trace == [
        "offers",
        "simulate:p-eggs",
        "offers",
        "clear",
        "simulate:p-pasta-1",
        "offers",
        "quote",
        "order",
        "decide",
        "audit",
        "clear",
    ]
    assert c.clear_calls == 2
    assert c.simulated == {}
    assert all(ok for _, ok, _ in r.results)


def test_audit_bare_list_supported_and_missing_why_detected():
    class BareListAuditClient(FakeClient):
        def __init__(self, events):
            super().__init__(natural_sub=True)
            self._events = events

        def audit(self, order_id):
            return list(self._events)

    c_ok = BareListAuditClient([
        {"action": "order.placed", "why": "submitted"},
        {"action": "order.confirmed", "why": "substituted pasta"},
    ])
    r_ok = FakeRun()
    run_scenario(c_ok, r_ok, day=date(2026, 11, 17))
    assert all(ok for _, ok, _ in r_ok.results)
    assert dict((l, ok) for l, ok, _ in r_ok.results)["audit explains substitutions"] is True

    c_bad = BareListAuditClient([
        {"action": "order.placed", "why": "submitted"},
        {"action": "order.confirmed", "why": ""},
    ])
    r_bad = FakeRun()
    run_scenario(c_bad, r_bad, day=date(2026, 11, 17))
    assert dict((l, ok) for l, ok, _ in r_bad.results)["audit explains substitutions"] is False


def test_allergen_violating_substitute_records_false():
    class NutSubClient(FakeClient):
        def offers(self, need):
            self.offers_calls += 1
            return {
                "items": [
                    {
                        "shortfall": 0,
                        "offers": [
                            {"product_id": "p-pasta-1", "units_needed": 2, "substitute_for": None, "notes": []},
                            {"product_id": "p-pasta-nut", "units_needed": 6, "substitute_for": "p-pasta-1", "notes": ["swap"]},
                        ],
                        "alternatives": [],
                    },
                    {"shortfall": 0, "offers": [{"product_id": "p-bacon", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                    {"shortfall": 0, "offers": [{"product_id": "p-eggs", "units_needed": 4, "substitute_for": None, "notes": []}], "alternatives": []},
                ]
            }

    c = NutSubClient()
    r = FakeRun()
    run_scenario(c, r, day=date(2026, 11, 17))
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["at least one substitute offer"] is True
    assert by_label["every picked substitute is in Order.substitutions"] is True
    assert by_label["substitution recorded with reason"] is True
    assert by_label["substitutes respect mandate allergens"] is False

