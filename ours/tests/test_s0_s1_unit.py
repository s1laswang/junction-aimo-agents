import json
from datetime import date

import pytest
from joserfc import jws
from joserfc.jwk import KeySet, OKPKey

from scenarios import s0_guardrails, s1_milk

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

S0_LABELS = [
    "over-limit quote exceeds mandate",
    "over per_order_eur -> needs_approval",
    "order listed in approvals",
    "approval -> placed",
    "tampered jws rejected",
]

S1_LABELS = [
    "parsed 1000 L lactose-free",
    "offers returned",
    "offers cover 1000 L",
    "offers carry price and availability",
    "quote signed (EdDSA)",
    "jws verifies against jwks.json",
    "order has Decision",
    "decision has reasons",
    "audit trail has steps",
    "every audit event has why",
    "MCP lists all tools",
    "MCP buyer agent gets an order with Decision",
]


class FakeRun:
    def __init__(self):
        self.said = []
        self.results = []

    def say(self, text):
        self.said.append(text)

    def check(self, label, ok, detail=""):
        self.results.append((label, bool(ok), detail))
        return bool(ok)


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeS0Client:
    def __init__(
        self,
        per_order_eur=2500.0,
        unit_price=15.0,
        base_units=100,
        payload_suffix="xy",
        tamper_resp=(409, {"code": "BAD_SIGNATURE"}),
        approval_status="placed",
    ):
        self.per_order_eur = per_order_eur
        self.unit_price = unit_price
        self.base_units = base_units
        self.payload_suffix = payload_suffix
        self.tamper_resp = tamper_resp
        self.approval_status = approval_status
        self.quote_calls = []
        self.ordered_quotes = []
        self.decide_calls = []
        self.raw_calls = []

    def j(self, method, path, **kw):
        assert (method, path) == ("GET", "/v1/mandate")
        return {"per_order_eur": self.per_order_eur, "approvers": ["Laura Virtanen"]}

    def parse(self, text, customer):
        assert customer == "hotel-aurora"
        return {
            "customer": customer,
            "items": [{"query": "lactose-free milk", "quantity": 1000, "unit": "l", "constraints": {"lactose_free": True}}],
        }

    def offers(self, need):
        return {
            "items": [
                {
                    "shortfall": 0,
                    "offers": [
                        {
                            "product_id": "milk-lf-10l",
                            "units_needed": self.base_units,
                            "available_amount": 5000,
                            "unit_price_net": self.unit_price,
                        }
                    ],
                }
            ]
        }

    def quote(self, picks, delivery_date):
        self.quote_calls.append((list(picks), str(delivery_date)))
        idx = len(self.quote_calls)
        total = round(sum(p["quantity"] * self.unit_price for p in picks), 2)
        payload = "eyJxdW90ZV9pZCI6InFfMSJ9" + self.payload_suffix
        return {
            "quote_id": f"q_{idx}",
            "total_net": total,
            "jws": f"eyJhbGciOiJFZERTQSJ9.{payload}.sig_{idx}",
        }

    def order(self, quote):
        self.ordered_quotes.append(quote)
        return {
            "order_id": "ord_s0_1",
            "status": "pending_approval",
            "decision": {
                "outcome": "needs_approval",
                "reasons": [{"code": "OVER_ORDER_LIMIT", "text": "Exceeds per-order limit."}],
            },
        }

    def approvals(self):
        return [{"order_id": "ord_s0_1", "status": "pending_approval"}]

    def decide(self, order_id, approve, by=None, note="scenario"):
        self.decide_calls.append((order_id, approve, by, note))
        return {"order_id": order_id, "status": self.approval_status}

    def call(self, method, path, **kw):
        self.raw_calls.append((method, path, kw))
        status, body = self.tamper_resp
        return FakeResp(status, body)


def _make_eddsa_quote_fixture():
    key = OKPKey.generate_key("Ed25519")
    kid = key.thumbprint()
    ks = KeySet.import_key_set(
        {"keys": [{**key.as_dict(private=False), "kid": kid, "alg": "EdDSA", "use": "sig"}]}
    )
    payload = json.dumps(
        {"quote_id": "q_s1_1", "customer": "hotel-aurora", "total_net": 1200.0, "currency": "EUR"},
        separators=(",", ":"),
    ).encode()
    token = jws.serialize_compact({"alg": "EdDSA", "kid": kid}, payload, key, algorithms=["EdDSA"])

    def verify(base, compact_jws):
        return jws.deserialize_compact(compact_jws, ks, algorithms=["EdDSA"])

    return token, verify


class FakeS1Client:
    def __init__(self, token, audit_payload=None):
        self.token = token
        self.parse_calls = []
        self.quote_calls = []
        self.order_calls = []
        self.audit_calls = []
        default_events = [
            {"action": "quote.issued", "why": "Issued signed quote."},
            {"action": "quote.redeemed", "why": "Redeemed signed quote."},
            {"action": "order.confirmed", "why": "Confirmed within mandate."},
        ]
        self.audit_payload = {"events": default_events} if audit_payload is None else audit_payload

    def parse(self, text, customer):
        self.parse_calls.append((text, customer))
        return {
            "customer": customer,
            "items": [
                {
                    "query": "milk",
                    "quantity": 1000,
                    "unit": "l",
                    "constraints": {"lactose_free": True},
                }
            ],
        }

    def offers(self, need):
        return {
            "items": [
                {
                    "shortfall": 0,
                    "offers": [
                        {
                            "product_id": "milk-lf-1",
                            "units_needed": 100,
                            "available_amount": 500,
                            "unit_price_net": 12.0,
                        }
                    ],
                }
            ]
        }

    def quote(self, picks, delivery_date):
        self.quote_calls.append((list(picks), str(delivery_date)))
        return {"quote_id": "q_s1_1", "total_net": 1200.0, "jws": self.token}

    def order(self, quote):
        self.order_calls.append(quote)
        return {
            "order_id": "ord_s1_1",
            "status": "confirmed",
            "decision": {
                "outcome": "auto",
                "reasons": [{"code": "WITHIN_MANDATE", "text": "Within mandate limits."}],
            },
        }

    def audit(self, order_id):
        self.audit_calls.append(order_id)
        return self.audit_payload


def test_import_does_not_execute_cli():
    assert s0_guardrails.base is None
    assert s0_guardrails.r is None
    assert s1_milk.base is None
    assert s1_milk.r is None


def test_s0_happy_path_all_5_checks_pass():
    c = FakeS0Client(per_order_eur=2500.0, unit_price=15.0, base_units=100)
    r = FakeRun()
    s0_guardrails.run_scenario(c, r, day=date(2026, 11, 16))

    assert [label for label, _, _ in r.results] == S0_LABELS
    assert all(ok for _, ok, _ in r.results), r.results
    assert c.quote_calls[0] == ([{"product_id": "milk-lf-10l", "quantity": 100, "substitute_for": None}], "2026-11-16")
    assert c.quote_calls[1] == ([{"product_id": "milk-lf-10l", "quantity": 300, "substitute_for": None}], "2026-11-16")
    assert c.decide_calls == [("ord_s0_1", True, "Laura Virtanen", "ok")]


def test_s0_dynamic_multiplier_when_initial_quote_is_small():
    c = FakeS0Client(per_order_eur=2500.0, unit_price=4.0, base_units=100)
    r = FakeRun()
    s0_guardrails.run_scenario(c, r, day=date(2026, 11, 16))

    assert c.quote_calls[0][0][0]["quantity"] == 100
    assert c.quote_calls[1][0][0]["quantity"] == 800
    assert c.ordered_quotes[0]["total_net"] == 3200.0
    assert [label for label, _, _ in r.results] == S0_LABELS
    assert all(ok for _, ok, _ in r.results), r.results


def test_s0_multiplier_fallback_when_initial_total_zero():
    c = FakeS0Client(per_order_eur=2500.0, unit_price=0.0, base_units=50)
    r = FakeRun()
    s0_guardrails.run_scenario(c, r, day=date(2026, 11, 16))

    assert c.quote_calls[0][0][0]["quantity"] == 50
    assert c.quote_calls[1][0][0]["quantity"] == 100
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["over-limit quote exceeds mandate"] is False


@pytest.mark.parametrize(
    "suffix,expected_tampered_suffix",
    [
        ("xy", "AA"),
        ("AA", "BB"),
    ],
)
def test_s0_tampered_jws_suffix_flip(suffix, expected_tampered_suffix):
    c = FakeS0Client(payload_suffix=suffix)
    r = FakeRun()
    s0_guardrails.run_scenario(c, r, day=date(2026, 11, 16))

    assert len(c.raw_calls) == 1
    method, path, kw = c.raw_calls[0]
    assert (method, path) == ("POST", "/v1/orders")
    sent_jws = kw["json"]["jws"]
    _, tam_payload, _ = sent_jws.split(".")
    assert tam_payload.endswith(expected_tampered_suffix)
    assert sent_jws != f"eyJhbGciOiJFZERTQSJ9.eyJxdW90ZV9pZCI6InFfMSJ9{suffix}.sig_1"
    assert dict((l, ok) for l, ok, _ in r.results)["tampered jws rejected"] is True


@pytest.mark.parametrize(
    "bad_resp",
    [
        (200, {"order_id": "ord_bad", "status": "placed"}),
        (409, {"order_id": "ord_leaked", "code": "BAD_SIGNATURE"}),
    ],
)
def test_s0_detects_failure_when_tampered_jws_accepted(bad_resp):
    c = FakeS0Client(tamper_resp=bad_resp)
    r = FakeRun()
    s0_guardrails.run_scenario(c, r, day=date(2026, 11, 16))

    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["tampered jws rejected"] is False
    assert all(by_label[l] is True for l in S0_LABELS[:-1])


def test_s1_happy_path_all_12_checks_pass():
    token, verify_fn = _make_eddsa_quote_fixture()
    c = FakeS1Client(token)
    r = FakeRun()
    mcp_calls = []
    buyer_calls = []

    def mcp_tools_fn(base):
        mcp_calls.append(base)
        return {"parse_request", "get_offers", "request_quote", "place_order", "get_audit_trail"}

    def buyer_run_fn(text, customer, key, url):
        buyer_calls.append((text, customer, key, url))
        return {
            "order": {"order_id": "ord_mcp_1", "decision": {"outcome": "auto"}},
            "log": ["Order ord_mcp_1 placed automatically within mandate."],
        }

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local/",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=mcp_tools_fn,
        buyer_run_fn=buyer_run_fn,
    )

    assert [label for label, _, _ in r.results] == S1_LABELS
    assert all(ok for _, ok, _ in r.results), r.results
    assert mcp_calls == ["http://test.local"]
    assert len(buyer_calls) == 1
    assert buyer_calls[0][1] == "hotel-aurora"
    assert buyer_calls[0][3] == "http://test.local/mcp"
    assert "2026-11-16" in buyer_calls[0][0]


def test_s1_verify_jws_exception_fails_check_cleanly():
    token, _ = _make_eddsa_quote_fixture()
    c = FakeS1Client(token)
    r = FakeRun()

    def failing_verify(base, compact_jws):
        raise ValueError("jwks mismatch")

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=failing_verify,
        mcp_tools_fn=lambda: {"get_offers", "request_quote", "place_order", "get_audit_trail"},
        buyer_run_fn=lambda text, customer, key, url: {"order": {"decision": {"outcome": "auto"}}, "log": ["ok"]},
    )

    assert [label for label, _, _ in r.results] == S1_LABELS
    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["quote signed (EdDSA)"] is True
    assert by_label["jws verifies against jwks.json"] is False
    assert sum(1 for _, ok, _ in r.results if ok) == 11


@pytest.mark.parametrize("as_dict", [True, False])
def test_s1_audit_list_vs_dict_handling(as_dict):
    token, verify_fn = _make_eddsa_quote_fixture()
    events = [
        {"action": "quote.issued", "why": "Issued quote."},
        {"action": "quote.redeemed", "why": "Redeemed quote."},
        {"action": "order.placed", "why": "Placed order."},
    ]
    payload = {"events": events} if as_dict else events
    c = FakeS1Client(token, audit_payload=payload)
    r = FakeRun()

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=lambda base: {"get_offers", "request_quote", "place_order", "get_audit_trail"},
        buyer_run_fn=lambda text, customer, key, url: {"order": {"decision": {"outcome": "needs_approval"}}, "log": ["pending"]},
    )

    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["audit trail has steps"] is True
    assert by_label["every audit event has why"] is True
    assert all(ok for _, ok, _ in r.results)


def test_s1_audit_missing_why_fails_check():
    token, verify_fn = _make_eddsa_quote_fixture()
    events = [
        {"action": "quote.issued", "why": "Issued quote."},
        {"action": "quote.redeemed", "why": ""},
        {"action": "order.placed", "why": "Placed order."},
    ]
    c = FakeS1Client(token, audit_payload=events)
    r = FakeRun()

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=lambda base: {"get_offers", "request_quote", "place_order", "get_audit_trail"},
        buyer_run_fn=lambda text, customer, key, url: {"order": {"decision": {"outcome": "auto"}}, "log": ["ok"]},
    )

    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["audit trail has steps"] is True
    assert by_label["every audit event has why"] is False


def test_s1_audit_too_few_events_fails_steps_check():
    token, verify_fn = _make_eddsa_quote_fixture()
    events = [
        {"action": "quote.issued", "why": "Issued quote."},
        {"action": "order.placed", "why": "Placed order."},
    ]
    c = FakeS1Client(token, audit_payload={"events": events})
    r = FakeRun()

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=lambda base: {"get_offers", "request_quote", "place_order", "get_audit_trail"},
        buyer_run_fn=lambda text, customer, key, url: {"order": {"decision": {"outcome": "auto"}}, "log": ["ok"]},
    )

    by_label = {label: ok for label, ok, _ in r.results}
    assert by_label["audit trail has steps"] is False
    assert by_label["every audit event has why"] is True


def test_s1_mcp_tools_exception_records_mcp_leg_failure():
    token, verify_fn = _make_eddsa_quote_fixture()
    c = FakeS1Client(token)
    r = FakeRun()

    def broken_tools(base):
        raise ConnectionError("mcp endpoint unreachable")

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=broken_tools,
        buyer_run_fn=lambda *a: {"order": {"decision": {"outcome": "auto"}}},
    )

    assert [label for label, _, _ in r.results] == S1_LABELS[:10] + ["MCP leg"]
    label, ok, detail = r.results[-1]
    assert label == "MCP leg"
    assert ok is False
    assert "ConnectionError: mcp endpoint unreachable" in detail


def test_s1_buyer_run_exception_records_mcp_leg_failure():
    token, verify_fn = _make_eddsa_quote_fixture()
    c = FakeS1Client(token)
    r = FakeRun()

    def broken_buyer(text, customer, key, url):
        raise RuntimeError("buyer crashed")

    s1_milk.run_scenario(
        c,
        r,
        "http://test.local",
        day=date(2026, 11, 16),
        verify_fn=verify_fn,
        mcp_tools_fn=lambda base: {"get_offers", "request_quote", "place_order", "get_audit_trail"},
        buyer_run_fn=broken_buyer,
    )

    assert [label for label, _, _ in r.results] == S1_LABELS[:11] + ["MCP leg"]
    label, ok, detail = r.results[-1]
    assert label == "MCP leg"
    assert ok is False
    assert "RuntimeError: buyer crashed" in detail
