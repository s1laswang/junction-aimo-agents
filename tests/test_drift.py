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


def test_apply_holds_on_approval():
    r = {"standing_order": {"lines": []}, "flags": [{"route": "approval", "product_id": "1", "fix": {}}]}
    assert drift.apply(r, "k", "http://x", ask_human=False)["status"] == "needs_approval"


def test_apply_ask_human_sends_note():
    sent = {}

    def handler(req):
        body = __import__("json").loads(req.content)
        if req.url.path == "/v1/quotes":
            return httpx.Response(200, json={"quote_id": "q", "jws": "j"})
        sent.update(body)
        return httpx.Response(200, json={"status": "pending_approval"})

    real = httpx.Client
    drift.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        r = {"standing_order": {"delivery_date": "d", "lines": [{"product_id": "1", "quantity": 5}]},
             "flags": [{"route": "approval", "product_id": "1", "why": "creep", "fix": {"action": "confirm_qty", "quantity": 4}}]}
        drift.apply(r, "k", "http://t", ask_human=True)
    finally:
        drift.httpx.Client = real
    assert sent["ask_human"] is True and sent["note"] == "creep"


ORDER = {"order_id": "ord_1", "status": "confirmed", "delivery_date": "2026-11-17",
         "lines": [{"product_id": "1", "name": "Milk", "quantity": 10, "sales_unit": "CS"},
                   {"product_id": "9", "name": "Bread", "quantity": 4, "sales_unit": "CS"}]}


def offer(pid, amt, **kw):
    o = {"product_id": pid, "product": {"name": "N" + pid}, "units_needed": 4, "available_amount": amt, "available_date": "2026-11-17", "sales_block": None}
    o.update(kw)
    return o


def watch_client(groups, sent):
    import json

    def handler(req):
        p = req.url.path
        if req.method == "GET":
            return httpx.Response(200, json=[ORDER, {**ORDER, "order_id": "ord_2", "status": "pending_approval"}])
        body = json.loads(req.content)
        if p == "/v1/offers":
            return httpx.Response(200, json={"items": groups})
        if p == "/v1/quotes":
            sent["quote"] = body
            return httpx.Response(200, json={"quote_id": "q", "jws": "j"})
        sent["order"] = body
        return httpx.Response(200, json={"order_id": "ord_new"})
    return httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))


def test_watch_split_with_substitute(tmp_path):
    sent = {}
    groups = [{"offers": [offer("1", 6), offer("5", 0, units_needed=4, substitute_for="1")]}, {"offers": [offer("9", 99)]}]
    r = drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=watch_client(groups, sent))
    assert len(r) == 1 and r[0]["product_id"] == "1" and r[0]["after"] == 6 and r[0]["new_order_id"] == "ord_new"
    assert r[0]["fix"]["action"] == "split"
    assert sent["quote"]["offers"] == [{"product_id": "1", "quantity": 6}, {"product_id": "5", "quantity": 4, "substitute_for": "1"}, {"product_id": "9", "quantity": 4}]
    assert sent["order"]["ask_human"] is True and "ord_1" in sent["order"]["note"]
    sent.clear()
    assert drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=watch_client(groups, sent)) == [] and not sent


def test_watch_stockout_full_substitute(tmp_path):
    sent = {}
    groups = [{"offers": [offer("5", 50, substitute_for="1", units_needed=10)]}, {"offers": [offer("9", 99)]}]
    r = drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=watch_client(groups, sent))
    assert r[0]["fix"]["action"] == "substitute" and "no longer available" in r[0]["why"]


def test_watch_move_date(tmp_path):
    sent = {}
    groups = [{"offers": [offer("1", 50, available_date="2026-11-19")]}, {"offers": [offer("9", 99)]}]
    r = drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=watch_client(groups, sent))
    assert r[0]["fix"]["action"] == "move_date" and sent["quote"]["delivery_date"] == "2026-11-19"


def test_watch_all_fine(tmp_path):
    groups = [{"offers": [offer("1", 50)]}, {"offers": [offer("9", 99)]}]
    assert drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=watch_client(groups, {})) == []
