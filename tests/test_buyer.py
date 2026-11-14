import asyncio
import contextlib
import sys
import types

import httpx
import pytest

import agents.buyer as buyer
from agents.buyer import MCP_CONFIG, pick, rank, units_for, usable


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


def test_usable_egg_and_tree_nut_traces_and_aliases():
    ni_egg = {"query": "pasta", "constraints": {"exclude_allergens": ["egg"]}}
    assert not usable(offer("e1", 10, allergens=["egg"]), ni_egg, "2026-11-17")
    assert not usable(offer("e2", 10, allergens=["eggs"]), ni_egg, "2026-11-17")
    assert not usable(offer("e3", 10, allergens=["may_contain:eggs"]), ni_egg, "2026-11-17")
    assert not usable(offer("e4", 10, allergens=["may:egg"]), ni_egg, "2026-11-17")
    assert usable(offer("e5", 10, allergens=["soy"]), ni_egg, "2026-11-17")

    ni_eggs = {"query": "pasta", "constraints": {"exclude_allergens": ["eggs"]}}
    assert not usable(offer("e6", 10, allergens=["egg"]), ni_eggs, "2026-11-17")
    assert not usable(offer("e7", 10, allergens=["may_contain:egg"]), ni_eggs, "2026-11-17")

    ni_tnuts = {"query": "granola", "constraints": {"exclude_allergens": ["tree nuts"]}}
    assert not usable(offer("t1", 10, allergens=["tree nut"]), ni_tnuts, "2026-11-17")
    assert not usable(offer("t2", 10, allergens=["may_contain:tree nut"]), ni_tnuts, "2026-11-17")
    assert not usable(offer("t3", 10, allergens=["may:tree nuts"]), ni_tnuts, "2026-11-17")
    assert usable(offer("t4", 10, allergens=["soy"]), ni_tnuts, "2026-11-17")

    ni_tnut = {"query": "granola", "constraints": {"exclude_allergens": ["tree nut"]}}
    assert not usable(offer("t5", 10, allergens=["tree nuts"]), ni_tnut, "2026-11-17")
    assert not usable(offer("t6", 10, allergens=["may_contain:tree nuts"]), ni_tnut, "2026-11-17")

    ni_nuts = {"query": "snack", "constraints": {"exclude_allergens": ["nuts"]}}
    assert not usable(offer("n1", 10, allergens=["nut"]), ni_nuts, "2026-11-17")
    assert not usable(offer("n2", 10, allergens=["may_contain:tree nut"]), ni_nuts, "2026-11-17")
    ni_nut = {"query": "snack", "constraints": {"exclude_allergens": ["nut"]}}
    assert not usable(offer("n3", 10, allergens=["nut"]), ni_nut, "2026-11-17")
    assert not usable(offer("n4", 10, allergens=["may:nuts"]), ni_nut, "2026-11-17")


def test_usable_rejects_none_unit_price_net():
    bad = offer("no_price", 10)
    bad["unit_price_net"] = None
    good = offer("with_price", 20)
    assert not usable(bad, NI, "2026-11-17")
    assert usable(good, NI, "2026-11-17")
    chosen, left = pick(NI, [bad, good], "2026-11-17")
    assert [c["product_id"] for c in chosen] == ["with_price"] and left == 0
    assert [o["product_id"] for o in rank([bad, good], NI)] == ["with_price", "no_price"]


def test_units_for_conversions_and_fallbacks():
    o_g = offer("flour", 10, units=99)
    o_g["product"]["unit_size"] = {"value": 500, "unit": "g"}
    assert units_for(o_g, {"query": "flour", "quantity": 2, "unit": "kg"}) == 4

    o_ml = offer("cream", 10, units=99)
    o_ml["product"]["unit_size"] = {"value": 250, "unit": "ml"}
    assert units_for(o_ml, {"query": "cream", "quantity": 1.5, "unit": "l"}) == 6

    o_dl = offer("juice", 10, units=99)
    o_dl["product"]["unit_size"] = {"value": 2, "unit": "dl"}
    assert units_for(o_dl, {"query": "juice", "quantity": 1, "unit": "l"}) == 5

    o_missing = offer("box", 10, units=7)
    assert units_for(o_missing, {"query": "box", "quantity": 3, "unit": "kg"}) == 7

    o_zero_size = offer("bad_size", 10, units=5)
    o_zero_size["product"]["unit_size"] = {"value": 0, "unit": "kg"}
    assert units_for(o_zero_size, {"query": "bad_size", "quantity": 3, "unit": "kg"}) == 5

    o_neg_size = offer("neg_size", 10, units=4)
    o_neg_size["product"]["unit_size"] = {"value": -1.5, "unit": "kg"}
    assert units_for(o_neg_size, {"query": "neg_size", "quantity": 3, "unit": "kg"}) == 4

    o_zero_units = offer("z", 10, units=1)
    o_zero_units["units_needed"] = 0
    assert units_for(o_zero_units, {"query": "z"}) == 1

    o_neg_units = offer("neg", 10, units=1)
    o_neg_units["units_needed"] = -3
    assert units_for(o_neg_units, {"query": "neg"}) == 1

    o_no_units = {"product_id": "none", "product": {"name": "none"}}
    assert units_for(o_no_units, {"query": "none"}) == 1


def test_run_no_usable_offers_and_empty_items(monkeypatch):
    calls = []

    class NoUsableSession:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if name == "parse_request":
                res = {"customer": args["customer"], "deliver_by": "2026-11-17",
                       "items": [{"query": "milk", "quantity": 10, "unit": "l", "constraints": {"exclude_allergens": ["egg"]}}]}
            elif name == "get_offers":
                no_price = offer("np", 10)
                no_price["unit_price_net"] = None
                res = {"items": [{"need_item": args["items"][0], "offers": [
                    offer("blk", 10, block=True),
                    offer("egg_item", 10, allergens=["may_contain:eggs"]),
                    no_price,
                ]}]}
            else:
                raise AssertionError(f"unexpected tool call: {name}")
            return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])

    @contextlib.asynccontextmanager
    async def cm1(url, key, stdio):
        yield NoUsableSession()

    monkeypatch.setattr(buyer, "_session", cm1)
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    log = []
    out = asyncio.run(buyer._run("10 l milk without egg", "hospital-tays", "k", "http://x/mcp", log, poll=2, interval=0))
    assert out["chosen"] == [] and out["quote"] is None and out["order"] is None
    assert [n for n, _ in calls] == ["parse_request", "get_offers"]
    assert any("No offer for milk is available" in line for line in log)
    assert log[-1] == "Nothing to order."

    calls2 = []

    class EmptyItemsSession:
        async def call_tool(self, name, args):
            calls2.append((name, args))
            if name == "parse_request":
                res = {"customer": args["customer"], "deliver_by": "2026-11-17", "items": []}
            elif name == "get_offers":
                res = {"items": []}
            else:
                raise AssertionError(f"unexpected tool call: {name}")
            return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])

    @contextlib.asynccontextmanager
    async def cm2(url, key, stdio):
        yield EmptyItemsSession()

    monkeypatch.setattr(buyer, "_session", cm2)
    log2 = []
    out2 = asyncio.run(buyer._run("nothing", "hotel-aurora", "k", "http://x/mcp", log2, poll=0, interval=0))
    assert out2["chosen"] == [] and out2["quote"] is None and out2["order"] is None
    assert [n for n, _ in calls2] == ["parse_request", "get_offers"]
    assert log2[-1] == "Nothing to order."


def test_run_blocked_outcome_and_main_exits_1(monkeypatch, capsys):
    calls = []
    blocked_order = {
        "order_id": "o_blk",
        "status": "blocked",
        "decision": {"outcome": "blocked", "reasons": [{"code": "ALLERGEN_CONFLICT", "text": "Contains excluded egg."}]},
    }
    monkeypatch.setattr(buyer, "_session", _fake_session(calls, blocked_order))
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    monkeypatch.setattr(buyer.llm, "explain", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["agents.buyer", "10 l milk no nuts", "--customer", "hospital-tays", "--poll", "0"])
    with pytest.raises(SystemExit) as exc:
        buyer.main()
    assert exc.value.code == 1
    captured = capsys.readouterr().out
    assert "Order o_blk blocked. Contains excluded egg." in captured
    assert [n for n, _ in calls] == ["parse_request", "get_offers", "request_quote", "place_order"]


def test_run_poll_zero_vs_multi_poll(monkeypatch):
    calls0, log0 = [], []
    pending = {
        "order_id": "o_p0",
        "status": "pending_approval",
        "decision": {"outcome": "needs_approval", "reasons": [{"code": "OVER_ORDER_LIMIT", "text": "Over limit."}]},
    }
    monkeypatch.setattr(buyer, "_session", _fake_session(calls0, pending))
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    out0 = asyncio.run(buyer._run("10 l milk", "hotel-aurora", "k", "http://x/mcp", log0, poll=0, interval=0))
    assert [n for n, _ in calls0] == ["parse_request", "get_offers", "request_quote", "place_order"]
    assert out0["order"]["status"] == "pending_approval"
    assert any("Order o_p0 needs human approval. Over limit." in line for line in log0)
    assert log0[-1] == "Order o_p0 is now pending_approval."

    calls_m, log_m = [], []
    replies = [{**pending, "order_id": "o_pm", "status": "pending_approval"},
               {**pending, "order_id": "o_pm", "status": "confirmed"}]

    class MultiPollSession:
        async def call_tool(self, name, args):
            calls_m.append((name, args))
            if name == "parse_request":
                res = {"customer": args["customer"], "deliver_by": "2026-11-17",
                       "items": [{"query": "milk", "quantity": 10, "unit": "l", "constraints": {}}]}
            elif name == "get_offers":
                res = {"items": [{"need_item": args["items"][0], "offers": [offer("m1", 40, units=10, avail=10)]}]}
            elif name == "request_quote":
                res = {"quote_id": "qm", "total_net": 40.0, "currency": "EUR", "valid_until": "2026-11-16T16:00:00Z", "jws": "sig.qm"}
            elif name == "place_order":
                res = {**pending, "order_id": "o_pm"}
            elif name == "get_order":
                res = replies.pop(0)
            else:
                raise AssertionError(name)
            return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])

    @contextlib.asynccontextmanager
    async def cm_m(url, key, stdio):
        yield MultiPollSession()

    monkeypatch.setattr(buyer, "_session", cm_m)
    out_m = asyncio.run(buyer._run("10 l milk", "hotel-aurora", "k", "http://x/mcp", log_m, poll=5, interval=0))
    assert [n for n, _ in calls_m] == ["parse_request", "get_offers", "request_quote", "place_order", "get_order", "get_order"]
    assert out_m["order"]["status"] == "confirmed"
    assert log_m[-1] == "Order o_pm is now confirmed."


def test_run_get_offers_need_wrapper_fallback(monkeypatch):
    calls, log = [], []

    class WrapperSession:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if name == "parse_request":
                res = {"customer": args["customer"], "deliver_by": "2026-11-17",
                       "items": [{"query": "milk", "quantity": 10, "unit": "l", "constraints": {}}]}
                return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])
            if name == "get_offers":
                if "need" not in args:
                    err = types.SimpleNamespace(text="validation error: field need required")
                    return types.SimpleNamespace(is_error=True, structured_content=None, content=[err])
                ni = args["need"]["items"][0]
                res = {"items": [{"need_item": ni, "offers": [offer("m1", 40, units=10, avail=10)]}]}
                return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])
            if name == "request_quote":
                res = {"quote_id": "q_wrap", "total_net": 40.0, "currency": "EUR", "valid_until": "2026-11-16T16:00:00Z", "jws": "sig.wrap"}
                return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])
            if name == "place_order":
                res = {"order_id": "o_wrap", "status": "placed",
                       "decision": {"outcome": "auto", "reasons": [{"code": "WITHIN_MANDATE", "text": "OK."}]}}
                return types.SimpleNamespace(is_error=False, structured_content={"result": res}, content=[])
            raise AssertionError(name)

    @contextlib.asynccontextmanager
    async def cm(url, key, stdio):
        yield WrapperSession()

    monkeypatch.setattr(buyer, "_session", cm)
    monkeypatch.setattr(buyer.llm, "choose", lambda *_: None)
    out = asyncio.run(buyer._run("10 l milk", "hotel-aurora", "k", "http://x/mcp", log, poll=0, interval=0))
    offer_calls = [args for name, args in calls if name == "get_offers"]
    assert len(offer_calls) == 2
    assert "need" not in offer_calls[0] and offer_calls[1] == {"need": out["need"]}
    assert out["order"]["order_id"] == "o_wrap"


def test_main_mcp_config_and_demo(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(buyer, "run", lambda *a, **kw: called.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["agents.buyer", "--mcp-config"])
    buyer.main()
    out_cfg = capsys.readouterr().out
    assert "claude mcp add" in out_cfg and "aimo.mcp_server" in out_cfg
    assert called == []

    demo_calls = []

    def fake_run(text, customer, key=None, url="http://127.0.0.1:8080/mcp", poll=0, interval=2.0, stdio=False):
        demo_calls.append((text, customer, key, url, poll, stdio))
        if len(demo_calls) == len(buyer.DEMO):
            raise RuntimeError("simulated network error")
        return {"log": [f"ok {customer}"], "order": {"order_id": "o1", "status": "placed"}}

    monkeypatch.setattr(buyer, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["agents.buyer", "--demo", "--poll", "10", "--url", "http://mock/mcp"])
    buyer.main()
    out_demo = capsys.readouterr().out
    assert len(demo_calls) == len(buyer.DEMO)
    assert all(poll == 2 and url == "http://mock/mcp" for _, _, _, url, poll, _ in demo_calls)
    assert "== hotel-aurora:" in out_demo and "ok hotel-aurora" in out_demo
    assert "failed: simulated network error" in out_demo
