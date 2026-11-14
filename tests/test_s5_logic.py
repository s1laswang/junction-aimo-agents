from datetime import date

import httpx

from scenarios._common import Run
from scenarios.s5_allergen_safe import _item_violates, run_scenario


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _item(allergen_code=None, deleted=False, unit="CONTAINS"):
    vals = [{"id": allergen_code, "unit": unit}] if allergen_code else []
    return {
        "deleted": deleted,
        "synkkaData": {"classifications": [{"name": "allergen", "values": vals}]},
    }


class FakeClient:
    def __init__(self, quote_responses, audit_events=None, offer_ids=None, raw_audit=None):
        self.quote_responses = quote_responses
        self.offer_ids = offer_ids if offer_ids is not None else ["safe_milk"]
        self.raw_audit = raw_audit
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
        return {"items": [{"offers": [{"product_id": pid} for pid in self.offer_ids]}]}

    def call(self, method, path, **kw):
        assert (method, path) == ("POST", "/v1/quotes")
        pid = kw["json"]["offers"][0]["product_id"]
        self.quote_calls.append((pid, kw["json"]["delivery_date"]))
        entry = self.quote_responses[pid]
        if isinstance(entry, Exception):
            raise entry
        status, body = entry
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
        if self.raw_audit is not None:
            return self.raw_audit
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


def test_s5_handles_null_synkka_and_may_contain_traces():
    db = {
        "safe_milk": {"deleted": False, "synkkaData": None},
        "null_classes": {"deleted": False, "synkkaData": {"classifications": None}},
        "null_values": {"deleted": False, "synkkaData": {"classifications": [{"name": "allergen", "values": None}]}},
        "trace_unpriced": _item("AP", unit="MAY_CONTAIN"),
        "trace_priced": _item("AN", unit="MAY_CONTAIN"),
    }
    c = FakeClient(
        {
            "trace_unpriced": (409, {"code": "UNPRICED", "text": "No price"}),
            "trace_priced": (200, {"quote_id": "q_trace", "jws": "h.p.s"}),
        }
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    assert c.quote_calls == [("trace_unpriced", "2026-11-16"), ("trace_priced", "2026-11-16")]
    assert c.ordered_quotes == [{"quote_id": "q_trace", "jws": "h.p.s"}]
    assert all(ok for _, ok, _ in r.results), r.results


def test_s5_skips_http_status_error_on_quote_until_next_candidate_succeeds():
    req = httpx.Request("POST", "http://test/v1/quotes")
    err_500 = httpx.HTTPStatusError("upstream failure", request=req, response=httpx.Response(500, request=req))
    err_422 = httpx.HTTPStatusError("invalid offer", request=req, response=httpx.Response(422, request=req))
    db = {
        "safe_milk": _item(),
        "bad_500": _item("AP"),
        "bad_422": _item("AN"),
        "bad_ok": _item("AE"),
        "bad_unused": _item("AP"),
    }
    c = FakeClient(
        {
            "bad_500": err_500,
            "bad_422": err_422,
            "bad_ok": (200, {"quote_id": "q_ok", "jws": "h.p.s"}),
            "bad_unused": (200, {"quote_id": "q_unused", "jws": "h.p.s"}),
        }
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    assert c.quote_calls == [
        ("bad_500", "2026-11-16"),
        ("bad_422", "2026-11-16"),
        ("bad_ok", "2026-11-16"),
    ]
    assert c.ordered_quotes == [{"quote_id": "q_ok", "jws": "h.p.s"}]
    assert all(ok for _, ok, _ in r.results), r.results


def test_item_violates_with_simplified_allergens_list():
    db = {
        "may_peanut": {"allergens": ["may_contain:peanuts"]},
        "may_egg": {"allergens": ["may:egg"]},
        "tree_nuts": {"allergens": ["tree nuts"]},
        "safe_milk": {"allergens": ["milk"]},
        "empty": {"allergens": []},
    }
    assert _item_violates("may_peanut", db, {"peanut"}) is True
    assert _item_violates("may_peanut", db, {"egg"}) is False
    assert _item_violates("may_egg", db, {"egg"}) is True
    assert _item_violates("may_egg", db, {"peanuts"}) is False
    assert _item_violates("tree_nuts", db, {"nuts"}) is True
    assert _item_violates("tree_nuts", db, {"tree nuts"}) is True
    assert _item_violates("tree_nuts", db, {"egg"}) is False
    assert _item_violates("safe_milk", db, {"peanut", "nuts", "egg"}) is False
    assert _item_violates("empty", db, {"peanut", "nuts", "egg"}) is False

    c = FakeClient(
        {
            "may_peanut": (409, {"code": "UNPRICED", "text": "No price"}),
            "may_egg": (409, {"code": "UNPRICED", "text": "No price"}),
            "tree_nuts": (200, {"quote_id": "q_tn", "jws": "h.p.s"}),
        }
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)
    assert c.quote_calls == [
        ("may_peanut", "2026-11-16"),
        ("may_egg", "2026-11-16"),
        ("tree_nuts", "2026-11-16"),
    ]
    assert c.ordered_quotes == [{"quote_id": "q_tn", "jws": "h.p.s"}]
    assert all(ok for _, ok, _ in r.results), r.results


def test_s5_offer_allergen_leak_records_false():
    db = {
        "safe_milk": _item(),
        "leaky_nut_bread": {"allergens": ["tree nuts"]},
        "bad_priced": _item("AP"),
    }
    c = FakeClient(
        {
            "leaky_nut_bread": (200, {"quote_id": "q_leak", "jws": "h.p.s"}),
            "bad_priced": (200, {"quote_id": "q_bad", "jws": "h.p.s"}),
        },
        offer_ids=["safe_milk", "leaky_nut_bread"],
    )
    r = Run("s5_allergen_safe", "http://test")
    run_scenario(c, r, day=date(2026, 11, 16), db_fn=lambda: db)

    by_label = {label: ok for label, ok, _ in r.results}
    detail_by_label = {label: detail for label, _, detail in r.results}
    assert by_label["offers returned"] is True
    assert by_label["no offer contains an excluded allergen"] is False
    assert "leaky_nut_bread" in detail_by_label["no offer contains an excluded allergen"]


def test_s5_audit_bare_list_supported_and_empty_why_detected():
    db = {"safe_milk": _item(), "bad_priced": _item("AP")}
    c_ok = FakeClient(
        {"bad_priced": (200, {"quote_id": "q_1", "jws": "h.p.s"})},
        raw_audit=[
            {"action": "order.placed", "why": "submitted"},
            {"action": "order.blocked", "why": "Contains excluded allergen: peanuts."},
        ],
    )
    r_ok = Run("s5_allergen_safe", "http://test")
    run_scenario(c_ok, r_ok, day=date(2026, 11, 16), db_fn=lambda: db)
    assert all(ok for _, ok, _ in r_ok.results), r_ok.results
    assert dict((l, ok) for l, ok, _ in r_ok.results)["audit has order.blocked with why"] is True

    c_bad = FakeClient(
        {"bad_priced": (200, {"quote_id": "q_2", "jws": "h.p.s"})},
        raw_audit=[{"action": "order.blocked", "why": ""}],
    )
    r_bad = Run("s5_allergen_safe", "http://test")
    run_scenario(c_bad, r_bad, day=date(2026, 11, 16), db_fn=lambda: db)
    assert dict((l, ok) for l, ok, _ in r_bad.results)["audit has order.blocked with why"] is False

