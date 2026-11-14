import asyncio
import contextlib
import types

import httpx
import pytest

import agents.buyer as buyer
from agents.buyer import MCP_CONFIG, pick, usable


def offer(pid, total, units=10, avail=100, rel=0.9, date="2026-11-16", block=False, allergens=(), substitute_for=None):
    return {"product_id": pid, "product": {"name": pid, "allergens": list(allergens)}, "units_needed": units,
            "total_net": total, "unit_price_net": total / units, "available_amount": avail,
            "available_date": date, "sales_block": block, "reliability": rel, "currency": "EUR",
            "substitute_for": substitute_for}


NI = {"query": "milk", "constraints": {"exclude_allergens": []}}


def test_cheapest_wins():
    c, left = pick(NI, [offer("a", 50), offer("b", 40)], "2026-11-17")
    assert [x["product_id"] for x in c] == ["b"] and c[0]["quantity"] == 10 and left == 0


def test_reliability_breaks_tie():
    c, _ = pick(NI, [offer("a", 40, rel=0.5), offer("b", 40, rel=0.95)], "2026-11-17")
    assert c[0]["product_id"] == "b"


def test_too_late_blocked_and_allergen_dropped():
    offers = [offer("late", 10, date="2026-11-20"), offer("blk", 10, block=True), offer("al", 10, allergens=["Nuts"]), offer("ok", 60)]
    ni = {"query": "milk", "constraints": {"exclude_allergens": ["nuts"]}}
    c, _ = pick(ni, offers, "2026-11-17")
    assert [x["product_id"] for x in c] == ["ok"]


def test_split_and_shortfall():
    c, left = pick(NI, [offer("a", 40, avail=6), offer("b", 50, avail=3)], "2026-11-17")
    assert [(x["product_id"], x["quantity"]) for x in c] == [("a", 6), ("b", 3)]
    assert left == pytest.approx(0.1)


def test_llm_preference_first():
    c, _ = pick(NI, [offer("a", 40), offer("b", 50)], "2026-11-17", first="b")
    assert c[0]["product_id"] == "b"


def test_mcp_config():
    assert "claude mcp add" in MCP_CONFIG and "aimo.mcp_server" in MCP_CONFIG


def _up():
    try:
        return httpx.get("http://127.0.0.1:8080/.well-known/agent-card.json", timeout=1).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _up(), reason="gateway not up")
def test_milk_end_to_end():
    import os
    from agents.buyer import run
    out = run("I need 1000 litres of lactose-free milk by Monday", "hotel-aurora", os.environ.get("AIMO_TEST_KEY", "demo-hotel-aurora"))
    assert out["chosen"] and out["order"]["decision"]["outcome"] in ("auto", "needs_approval", "blocked")


def test_split_offers_cover_full_need():
    ni = {"query": "oat", "quantity": 200, "unit": "L", "constraints": {}}
    a = offer("a", 96.6, units=69, avail=1414)
    b = offer("b", 174.23, units=131, avail=131)
    for o, price in ((a, 1.4), (b, 1.33)):
        o["unit_price_net"] = price
        o["product"]["unit_size"] = {"value": 1.0, "unit": "l"}
    c, left = pick(ni, [a, b], "2026-11-17")
    assert [(x["product_id"], x["quantity"]) for x in c] == [("b", 131), ("a", 69)] and left == 0


def test_usable_rejects_allergen_aliases_and_traces():
    ni_nuts = {"query": "bar", "constraints": {"exclude_allergens": ["nuts"]}}
    assert not usable(offer("p1", 10, allergens=["may_contain:peanuts"]), ni_nuts, "2026-11-17")
    assert not usable(offer("p2", 10, allergens=["may:peanuts"]), ni_nuts, "2026-11-17")
    assert not usable(offer("p3", 10, allergens=["tree nuts"]), ni_nuts, "2026-11-17")
    assert usable(offer("p4", 10, allergens=["soy"]), ni_nuts, "2026-11-17")
    ni_dairy = {"query": "drink", "constraints": {"exclude_allergens": ["dairy"]}}
    assert not usable(offer("d1", 10, allergens=["lactose"]), ni_dairy, "2026-11-17")
    assert not usable(offer("d2", 10, allergens=["may_contain:milk"]), ni_dairy, "2026-11-17")
    ni_gluten = {"query": "pasta", "constraints": {"exclude_allergens": ["gluten"]}}
    assert not usable(offer("g1", 10, allergens=["wheat"]), ni_gluten, "2026-11-17")
    assert not usable(offer("g2", 10, allergens=["may_contain:oats"]), ni_gluten, "2026-11-17")


def test_pick_preserves_substitute_for():
    exact = offer("orig", 40, avail=4)
    sub = offer("sub", 45, avail=10, substitute_for="orig")
    c, left = pick(NI, [exact, sub], "2026-11-17")
    assert left == 0
    assert "substitute_for" not in c[0] and c[0]["product_id"] == "orig" and c[0]["quantity"] == 4
    assert c[1]["product_id"] == "sub" and c[1]["quantity"] == 6 and c[1]["substitute_for"] == "orig"


def _fake_session(calls, order_reply, polled_reply=None):
    class FakeSession:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if name == "parse_request":
                res = {"customer": args["customer"], "deliver_by": "2026-11-17",
                       "items": [{"query": "milk", "quantity": 10, "unit": "l", "constraints": {"exclude_allergens": ["nuts"]}}]}
            elif name == "get_offers":
                res = {"items": [{"need_item": args["items"][0], "offers": [
                    offer("orig", 40, units=10, avail=4),
                    offer("bad", 30, units=10, avail=10, allergens=["may_contain:peanuts"], substitute_for="orig"),
                    offer("sub", 45, units=10, avail=10, substitute_for="orig"),
                ]}]}
            elif name == "request_quote":
                res = {"quote_id": "q1", "total_net": 43.0, "currency": "EUR", "valid_until": "2026-11-16T16:00:00Z", "jws": "sig.q1"}
            elif name == "place_order":
                res = order_reply
            elif name == "get_order":
                res = polled_reply
            else:
                raise AssertionError(name)
            return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])

    @contextlib.asynccontextmanager
    async def cm(url, key, stdio):
        yield FakeSession()

    return cm


def test_run_sends_substitute_for_and_handles_auto(monkeypatch):
    calls, log = [], []
    order = {"order_id": "o1", "status": "placed",
             "decision": {"outcome": "auto", "reasons": [{"code": "WITHIN_MANDATE", "text": "Within limits."}]}}
    monkeypatch.setattr(buyer, "_session", _fake_session(calls, order))
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    out = asyncio.run(buyer._run("10 l milk no nuts", "hotel-aurora", "k", "http://x/mcp", log, poll=2, interval=0))
    q_args = dict(calls)["request_quote"]
    assert q_args == {"offers": [{"product_id": "orig", "quantity": 4},
                                 {"product_id": "sub", "quantity": 6, "substitute_for": "orig"}],
                      "delivery_date": "2026-11-17"}
    assert [n for n, _ in calls] == ["parse_request", "get_offers", "request_quote", "place_order"]
    assert out["order"]["status"] == "placed"
    assert any("placed automatically within mandate" in line for line in log)


def test_run_sends_substitute_for_and_polls_needs_approval(monkeypatch):
    calls, log = [], []
    pending = {"order_id": "o2", "status": "pending_approval",
               "decision": {"outcome": "needs_approval", "reasons": [{"code": "SUBSTITUTION_NEEDS_APPROVAL", "text": "Swap needs approval."}]}}
    approved = {**pending, "status": "placed"}
    monkeypatch.setattr(buyer, "_session", _fake_session(calls, pending, approved))
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    out = asyncio.run(buyer._run("10 l milk no nuts", "hospital-tays", "k", "http://x/mcp", log, poll=3, interval=0))
    q_args = dict(calls)["request_quote"]
    assert q_args["offers"] == [{"product_id": "orig", "quantity": 4},
                                {"product_id": "sub", "quantity": 6, "substitute_for": "orig"}]
    assert [n for n, _ in calls] == ["parse_request", "get_offers", "request_quote", "place_order", "get_order"]
    assert out["order"]["status"] == "placed"
    assert any("needs human approval" in line for line in log) and any("Order o2 is now placed." in line for line in log)
