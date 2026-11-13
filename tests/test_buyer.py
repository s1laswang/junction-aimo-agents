import httpx
import pytest

from agents.buyer import MCP_CONFIG, pick


def offer(pid, total, units=10, avail=100, rel=0.9, date="2026-11-16", block=False, allergens=()):
    return {"product_id": pid, "product": {"name": pid, "allergens": list(allergens)}, "units_needed": units,
            "total_net": total, "unit_price_net": total / units, "available_amount": avail,
            "available_date": date, "sales_block": block, "reliability": rel, "currency": "EUR"}


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
