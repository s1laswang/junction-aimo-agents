from datetime import date

from scenarios._common import Run
from scenarios.s5_allergen_safe import run_scenario


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _item(allergen_code=None, deleted=False):
    vals = [{"id": allergen_code, "unit": "CONTAINS"}] if allergen_code else []
    return {
        "deleted": deleted,
        "synkkaData": {"classifications": [{"name": "allergen", "values": vals}]},
    }


class FakeClient:
    def __init__(self, quote_responses, audit_events=None):
        self.quote_responses = quote_responses
        self.quote_calls = []
        self.ordered_quotes = []
        self.audit_calls = []
        self.audit_events = (
            audit_events
            if audit_events is not None
            else [
                {"action": "order.placed", "why": "Agent submitted an order against a signed quote."},
                {"action": "order.blocked", "why": "Contains excluded allergen: tree nuts."},
            ]
        )

    def j(self, method, path, **kw):
        assert (method, path) == ("GET", "/v1/mandate")
        return {"exclude_allergens": ["peanut", "nuts", "egg"]}

    def parse(self, text, customer):
        assert customer == "hospital-tays"
        return {"customer": customer, "items": [{"query": "milk", "quantity": 100, "unit": "l"}]}

    def offers(self, need):
        return {"items": [{"offers": [{"product_id": "safe_milk"}]}]}

    def call(self, method, path, **kw):
        assert (method, path) == ("POST", "/v1/quotes")
        pid = kw["json"]["offers"][0]["product_id"]
        self.quote_calls.append((pid, kw["json"]["delivery_date"]))
        status, body = self.quote_responses[pid]
        return FakeResp(status, body)

    def order(self, quote):
        self.ordered_quotes.append(quote)
        return {
            "order_id": "ord_blocked_1",
            "status": "blocked",
            "decision": {
                "outcome": "blocked",
                "reasons": [{"code": "ALLERGEN_CONFLICT", "text": "Contains excluded allergen: tree nuts."}],
            },
        }

    def audit(self, order_id):
        self.audit_calls.append(order_id)
        return {"events": self.audit_events}


def test_s5_skips_unpriced_and_verifies_blocked_audit():
    db = {
        "safe_milk": _item(),
        "deleted_peanut": _item("AP", deleted=True),
        "bad_unpriced": _item("AP"),
        "bad_priced": _item("AN"),
        "bad_extra": _item("AE"),
    }
    c = FakeClient(
        {
            "bad_unpriced": (409, {"code": "UNPRICED", "text": "No price for bad_unpriced."}),
            "bad_priced": (200, {"quote_id": "q_2", "jws": "h.p.s"}),
            "bad_extra": (200, {"quote_id": "q_3", "jws": "h.p.s"}),
        }
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    assert c.quote_calls == [("bad_unpriced", "2026-11-16"), ("bad_priced", "2026-11-16")]
    assert c.ordered_quotes == [{"quote_id": "q_2", "jws": "h.p.s"}]
    assert c.audit_calls == ["ord_blocked_1"]

    by_label = {label: ok for label, ok, _ in r.results}
    assert len(r.results) == 7
    assert all(ok for _, ok, _ in r.results), r.results
    for expected in (
        "mandate excludes allergens",
        "offers returned",
        "no offer contains an excluded allergen",
        "found a violating product in data",
        "violating order blocked",
        "block reason is ALLERGEN_CONFLICT with text",
        "audit has order.blocked with why",
    ):
        assert by_label.get(expected) is True


def test_s5_audit_requires_non_empty_blocked_why():
    db = {"safe_milk": _item(), "bad_priced": _item("AP")}
    c = FakeClient(
        {"bad_priced": (200, {"quote_id": "q_1", "jws": "h.p.s"})},
        audit_events=[{"action": "order.blocked", "why": "   "}],
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["violating order blocked"] is True
    assert by_label["audit has order.blocked with why"] is False


def test_s5_caps_quote_attempts_at_15():
    db = {"safe_milk": _item()}
    quotes = {}
    for i in range(20):
        pid = f"bad_{i}"
        db[pid] = _item("AP")
        quotes[pid] = (409, {"code": "UNPRICED", "text": "No price"})
    c = FakeClient(quotes)
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    assert len(c.quote_calls) == 15
    assert c.ordered_quotes == []
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["found a violating product in data"] is False
