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

    def parse(self, text, customer):
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


@pytest.fixture(autouse=True)
def fake_product_db(tmp_path, monkeypatch):
    p = tmp_path / "products.json"
    p.write_text(json.dumps([
        {"id": "p-pasta-1", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "UW", "unit": "CONTAINS"}]}]}},
        {"id": "p-pasta-2", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "UW", "unit": "CONTAINS"}]}]}},
        {"id": "p-bacon", "synkkaData": {"classifications": []}},
        {"id": "p-eggs", "synkkaData": {"classifications": [{"name": "allergen", "values": [{"id": "AE", "unit": "CONTAINS"}]}]}},
    ]))
    _common.product_db.cache_clear()
    monkeypatch.setattr(_common, "DATA", str(p))
    yield
    _common.product_db.cache_clear()


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

