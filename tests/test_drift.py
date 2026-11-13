import os
from datetime import date

import httpx
import pytest

from agents import drift
from agents.drift import SEEDS, drift_rules, next_delivery

LINE = {"product_id": "1", "name": "Milk", "unit": "CS", "weeks": [10, 10, 10, 10]}
MANDATE = {"exclude_allergens": ["peanut"], "substitutions": "within_prefs", "per_order_eur": 1000}
ALT = {"product_id": "2", "name": "Milk B", "price": 9.0, "allergens": [], "available_amount": 50, "sales_block": False}


def after(**kw):
    a = {"found": True, "price": 10.0, "available_amount": 50, "available_date": "2026-11-17", "sales_block": False, "allergens": [], "alternatives": [ALT]}
    a.update(kw)
    return a


def run(a, before=None, line=LINE, mandate=MANDATE):
    return drift_rules(line, before or {"price": 10.0}, a, mandate, "2026-11-17")


def kinds(fl):
    return {f["kind"]: f for f in fl}


def test_clean():
    assert run(after()) == []


def test_price_up_small_auto_big_approval():
    f = kinds(run(after(price=10.7)))["price"]
    assert f["route"] == "auto" and "+7.0%" in f["why"]
    f = kinds(run(after(price=12.0)))["price"]
    assert f["route"] == "approval" and f["fix"]["action"] == "substitute" and f["fix"]["product_id"] == "2"


def test_price_cap_forces_approval():
    f = kinds(run(after(price=10.6), mandate={**MANDATE, "per_order_eur": 100}))["price"]
    assert f["route"] == "approval"


def test_price_drop_auto():
    assert kinds(run(after(price=8.0)))["price"]["route"] == "auto"


def test_unavailable_substitute():
    f = kinds(run({"found": False, "alternatives": [ALT]}))["unavailable"]
    assert f["fix"]["action"] == "substitute" and f["route"] == "auto"


def test_unavailable_needs_human_when_ask():
    f = kinds(run({"found": False, "alternatives": [ALT]}, mandate={**MANDATE, "substitutions": "ask"}))["unavailable"]
    assert f["route"] == "approval"


def test_short_reduces():
    f = kinds(run(after(available_amount=6)))["short"]
    assert f["fix"] == {"action": "reduce_qty", "quantity": 6} and f["route"] == "auto"


def test_sales_block():
    f = kinds(run(after(sales_block=True)))
    assert "sales_block" in f and "short" not in f


def test_late_moves_date():
    f = kinds(run(after(available_date="2026-11-19")))["late"]
    assert f["fix"]["date"] == "2026-11-19" and f["route"] == "approval"


def test_mandate_allergen_blocks_and_skips_unsafe_alt():
    bad = {**ALT, "allergens": ["Peanut"]}
    f = kinds(run(after(allergens=["peanut"], alternatives=[bad])))["mandate"]
    assert f["route"] == "approval" and f["fix"]["action"] == "remove_line"
    f = kinds(run(after(allergens=["peanut"])))["mandate"]
    assert f["fix"]["product_id"] == "2" and f["route"] == "approval"


def test_qty_creep():
    line = {**LINE, "weeks": [8, 8, 9, 10, 12]}
    f = kinds(run(after(), line=line))["qty_creep"]
    assert f["before"] == 8 and f["after"] == 12 and f["fix"]["quantity"] == 8
    assert "qty_creep" not in kinds(run(after(), line={**LINE, "weeks": [8, 12, 9, 12]}))


def test_next_delivery():
    assert next_delivery(1, date(2026, 11, 13)) == date(2026, 11, 17)
    assert next_delivery(4, date(2026, 11, 13)) == date(2026, 11, 20)


def mock_client(price):
    def handler(req):
        if req.url.path == "/v1/mandate":
            return httpx.Response(200, json=MANDATE)
        items = []
        for it in __import__("json").loads(req.content)["items"]:
            line = next(l for s in SEEDS.values() for l in s["lines"] if l["name"] == it["query"])
            items.append({"need_item": it, "shortfall": 0, "offers": [{
                "product_id": line["product_id"], "product": {"name": line["name"], "allergens": []},
                "unit_price_net": price, "available_amount": 999, "available_date": "2026-11-17", "sales_block": False}]})
        return httpx.Response(200, json={"items": items})
    return httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))


def test_check_baseline_then_drift(tmp_path):
    st = tmp_path / "d.json"
    r = drift.check("school-hervanta", "k", "", today=date(2026, 11, 13), state_path=st, client=mock_client(10.0))
    assert {f["kind"] for f in r["flags"]} == {"qty_creep"}
    r = drift.check("school-hervanta", "k", "", today=date(2026, 11, 13), state_path=st, client=mock_client(12.0))
    assert sum(f["kind"] == "price" for f in r["flags"]) == 3


@pytest.mark.skipif(not os.environ.get("AIMO_URL"), reason="gateway not configured")
def test_live(tmp_path):
    try:
        r = drift.check("school-hervanta", os.environ.get("AIMO_AGENT_KEY", ""), os.environ["AIMO_URL"], state_path=tmp_path / "d.json")
    except httpx.TransportError:
        pytest.skip("gateway down")
    assert r["standing_order"]["lines"]
