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


def test_trace_and_alias_allergens_under_nuts_mandate():
    nuts_mandate = {**MANDATE, "exclude_allergens": ["nuts"]}
    unsafe_tree = {**ALT, "product_id": "3", "price": 5.0, "allergens": ["tree nuts"]}
    unsafe_trace = {**ALT, "product_id": "4", "price": 4.0, "allergens": ["may_contain:peanuts"]}
    f = kinds(run(after(allergens=["may_contain:peanuts"], alternatives=[unsafe_tree, unsafe_trace]), mandate=nuts_mandate))["mandate"]
    assert f["route"] == "approval" and f["after"] == "peanuts" and f["fix"]["action"] == "remove_line"
    f = kinds(run(after(allergens=["tree nuts"], alternatives=[unsafe_trace, ALT]), mandate=nuts_mandate))["mandate"]
    assert f["route"] == "approval" and f["after"] == "tree nuts" and f["fix"]["action"] == "substitute" and f["fix"]["product_id"] == "2"
    f = kinds(run(after(allergens=["may:wheat"], alternatives=[ALT]), mandate={**MANDATE, "exclude_allergens": ["gluten"]}))["mandate"]
    assert f["route"] == "approval" and f["after"] == "wheat"
    f = kinds(run(after(allergens=["lactose"], alternatives=[ALT]), mandate={**MANDATE, "exclude_allergens": ["dairy"]}))["mandate"]
    assert f["route"] == "approval" and f["after"] == "lactose"
    f = kinds(run(after(allergens=["egg"], alternatives=[ALT]), mandate={**MANDATE, "exclude_allergens": ["eggs"]}))["mandate"]
    assert f["route"] == "approval" and f["after"] == "egg"


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
        if req.url.path.startswith("/v1/products/"):
            return httpx.Response(404)
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


def test_apply_passes_substitute_for():
    sent = {}

    def handler(req):
        body = __import__("json").loads(req.content)
        if req.url.path == "/v1/quotes":
            sent["quote"] = body
            return httpx.Response(200, json={"quote_id": "q", "jws": "j"})
        sent["order"] = body
        return httpx.Response(200, json={"status": "placed", "substitutions": [{"original_id": "1", "substitute_id": "2"}]})

    real = httpx.Client
    drift.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        r = {
            "standing_order": {"delivery_date": "2026-11-17", "lines": [{"product_id": "1", "quantity": 5}, {"product_id": "9", "quantity": 3}]},
            "flags": [{"route": "auto", "product_id": "1", "why": "unavailable", "fix": {"action": "substitute", "product_id": "2", "quantity": 5}}],
        }
        out = drift.apply(r, "k", "http://t", ask_human=True)
    finally:
        drift.httpx.Client = real
    assert sent["quote"]["offers"] == [{"product_id": "2", "quantity": 5, "substitute_for": "1"}, {"product_id": "9", "quantity": 3}]
    assert out["substitutions"] == [{"original_id": "1", "substitute_id": "2"}]


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


def test_watch_reports_gateway_stockout_without_placing(tmp_path):
    import json
    sent = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=[{**ORDER, "status": "pending_approval", "substitutions": [{"original_id": "1", "substitute_id": "5"}],
                                              "decision": {"outcome": "needs_approval", "reasons": [{"code": "STOCKOUT", "text": "Milk out of stock"}]}},
                                             {**ORDER, "order_id": "ord_3", "status": "pending_approval", "decision": {"reasons": [{"code": "AGENT_REQUESTED", "text": "x"}]}}])
        sent.append(req.url.path)
        return httpx.Response(200, json={"items": []})

    c = httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))
    r = drift.watch_orders("c", "k", "", state_path=tmp_path / "s.json", client=c)
    assert len(r) == 1 and r[0]["kind"] == "stockout_handled" and r[0]["why"] == "Milk out of stock" and r[0]["new_order_id"] is None
    assert sent == []


def exact_client(quote_ok, found_elsewhere=False):
    import json

    def handler(req):
        p = req.url.path
        if p == "/v1/mandate":
            return httpx.Response(200, json=MANDATE)
        if p.startswith("/v1/products/"):
            return httpx.Response(200, json={"product_id": p.rsplit("/", 1)[1], "name": "Exact " + p.rsplit("/", 1)[1], "allergens": []})
        body = json.loads(req.content)
        if p == "/v1/quotes":
            if not quote_ok:
                return httpx.Response(422, json={"code": "x"})
            ln = body["offers"][0]
            return httpx.Response(200, json={"lines": [{"product_id": ln["product_id"], "unit_price_net": 7.5}]})
        items = []
        for i, it in enumerate(body["items"]):
            offers = []
            if found_elsewhere and i == 1:
                offers = [{"product_id": "460396", "product": {"name": "M", "allergens": []}, "unit_price_net": 3.0, "available_amount": 99, "available_date": "2026-11-17", "sales_block": None}]
            items.append({"need_item": it, "shortfall": 0, "offers": offers})
        return httpx.Response(200, json={"items": items})
    return httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))


def test_exact_quote_prevents_false_unavailable(tmp_path):
    r = drift.check("school-hervanta", "k", "", today=date(2026, 11, 13), state_path=tmp_path / "d.json", client=exact_client(True))
    assert not [f for f in r["flags"] if f["kind"] == "unavailable"]


def test_quote_error_means_not_offered(tmp_path):
    r = drift.check("school-hervanta", "k", "", today=date(2026, 11, 13), state_path=tmp_path / "d.json", client=exact_client(False))
    assert len([f for f in r["flags"] if f["kind"] == "unavailable"]) == 3


def test_match_across_all_groups(tmp_path):
    r = drift.check("school-hervanta", "k", "", today=date(2026, 11, 13), state_path=tmp_path / "d.json", client=exact_client(False, True))
    assert len([f for f in r["flags"] if f["kind"] == "unavailable"]) == 2


def aurora_stockout_client(quotes_called, stockout_group=None, mandate_override=None):
    import json

    aurora_mandate = mandate_override or {"exclude_allergens": [], "substitutions": "within_prefs", "per_order_eur": 2500.0}
    lines = SEEDS["hotel-aurora"]["lines"]

    def handler(req):
        p = req.url.path
        if p == "/v1/mandate":
            return httpx.Response(200, json=aurora_mandate)
        if p.startswith("/v1/products/"):
            pid = p.rsplit("/", 1)[1]
            ln = next(l for l in lines if l["product_id"] == pid)
            return httpx.Response(200, json={"product_id": pid, "name": ln["name"], "allergens": ["egg"] if pid == "463982" else []})
        body = json.loads(req.content)
        if p == "/v1/quotes":
            ln = body["offers"][0]
            quotes_called.append(ln["product_id"])
            return httpx.Response(200, json={"lines": [{"product_id": ln["product_id"], "unit_price_net": 10.0}]})
        items = []
        for i, it in enumerate(body["items"]):
            ln = lines[i]
            if i == 0 and stockout_group is not None:
                items.append({"need_item": it, **stockout_group})
            else:
                items.append({
                    "need_item": it,
                    "shortfall": 0,
                    "offers": [{
                        "product_id": ln["product_id"],
                        "product": {"name": ln["name"], "allergens": ["egg"] if ln["product_id"] == "463982" else []},
                        "unit_price_net": 10.0,
                        "available_amount": 50,
                        "available_date": "2026-11-19",
                        "sales_block": None,
                        "notes": [],
                    }],
                    "alternatives": [],
                    "options": [],
                })
        return httpx.Response(200, json={"items": items})

    return httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))


def test_baseline_offered_then_omitted_flags_unavailable_without_exact_quote(tmp_path):
    st = tmp_path / "d.json"
    quotes_called = []
    drift.check("hotel-aurora", "k", "", today=date(2026, 11, 13), state_path=st, client=aurora_stockout_client(quotes_called))
    assert quotes_called == []
    saved = drift.load_state(st)["hotel-aurora"]["baseline"]
    assert saved["463982"]["offered"] is True
    grp = {
        "shortfall": 0,
        "offers": [{
            "product_id": "469999",
            "product": {"name": "Free Range Eggs L180", "allergens": ["egg"]},
            "unit_price_net": 11.0,
            "available_amount": 20,
            "available_date": "2026-11-19",
            "sales_block": None,
            "notes": [],
        }],
        "alternatives": [],
        "options": [],
    }
    r = drift.check("hotel-aurora", "k", "", today=date(2026, 11, 13), state_path=st, client=aurora_stockout_client(quotes_called, grp))
    assert quotes_called == []
    by_pid = {f["product_id"]: f for f in r["flags"]}
    assert by_pid["463982"]["kind"] == "unavailable"
    assert by_pid["463982"]["fix"] == {"action": "substitute", "product_id": "469999", "name": "Free Range Eggs L180", "price": 11.0, "quantity": 4}


def test_first_run_shortfall_and_signals_skip_exact_quote(tmp_path):
    sub_alt = {
        "product_id": "469999",
        "product": {"name": "Free Range Eggs L180", "allergens": ["egg"]},
        "unit_price_net": 11.0,
        "available_amount": 20,
        "available_date": "2026-11-19",
        "sales_block": None,
        "notes": [],
    }
    cases = [
        {"shortfall": 4, "offers": [], "alternatives": [sub_alt], "options": []},
        {"shortfall": 0, "offers": [{**sub_alt, "notes": ["skipped Kieku Barn Eggs L180 12300g (463982): no stock"]}], "alternatives": [], "options": []},
        {"shortfall": 0, "offers": [{**sub_alt, "substitute_for": "463982"}], "alternatives": [], "options": []},
        {"shortfall": 0, "offers": [], "alternatives": [sub_alt], "options": [{"kind": "late", "date": "2026-11-21"}]},
    ]
    for idx, grp in enumerate(cases):
        quotes_called = []
        r = drift.check("hotel-aurora", "k", "", today=date(2026, 11, 13), state_path=tmp_path / f"d{idx}.json", client=aurora_stockout_client(quotes_called, grp))
        assert quotes_called == []
        by_pid = {f["product_id"]: f for f in r["flags"]}
        assert by_pid["463982"]["kind"] == "unavailable"
        assert by_pid["463982"]["fix"]["product_id"] == "469999"


def test_allergen_unsafe_product_still_calls_exact_quote_and_flags_mandate(tmp_path):
    st = tmp_path / "d.json"
    quotes_called = []
    drift.check("hotel-aurora", "k", "", today=date(2026, 11, 13), state_path=st, client=aurora_stockout_client(quotes_called))
    safe_sub = {
        "product_id": "469888",
        "product": {"name": "Oat Scramble Base", "allergens": []},
        "unit_price_net": 9.0,
        "available_amount": 20,
        "available_date": "2026-11-19",
        "sales_block": None,
        "notes": [],
    }
    grp = {"shortfall": 4, "offers": [], "alternatives": [safe_sub], "options": []}
    mandate_no_egg = {"exclude_allergens": ["egg"], "substitutions": "within_prefs", "per_order_eur": 2500.0}
    r = drift.check("hotel-aurora", "k", "", today=date(2026, 11, 13), state_path=st, client=aurora_stockout_client(quotes_called, grp, mandate_override=mandate_no_egg))
    assert quotes_called == ["463982"]
    by_pid = {f["product_id"]: f for f in r["flags"]}
    assert by_pid["463982"]["kind"] == "mandate"
    assert by_pid["463982"]["route"] == "approval"
    assert by_pid["463982"]["fix"]["product_id"] == "469888"


def test_snapshot_alternatives_and_dedupe():
    lines = [{"product_id": "463982", "name": "Eggs", "unit": "KI", "weeks": [4, 4]}]
    a1 = {"product_id": "469001", "product": {"name": "Alt 1", "allergens": ["egg"]}, "unit_price_net": 9.5, "available_amount": 10, "sales_block": None}
    a2 = {"product_id": "469002", "product": {"name": "Alt 2", "allergens": ["egg"]}, "unit_price_net": 10.5, "available_amount": 15, "sales_block": None}
    mine_zero = {"product_id": "463982", "product": {"name": "Eggs", "allergens": ["egg"]}, "unit_price_net": 10.0, "available_amount": 0, "available_date": "2026-11-19", "sales_block": None}
    snap = drift.snapshot([{"offers": [a1], "alternatives": [a1, a2, mine_zero]}], lines)
    assert snap["463982"]["found"] is True
    assert snap["463982"]["offered"] is True
    assert snap["463982"]["available_amount"] == 0
    assert [x["product_id"] for x in snap["463982"]["alternatives"]] == ["469001", "469002"]
    fl = drift_rules(lines[0], {"price": 10.0}, snap["463982"], {"exclude_allergens": [], "substitutions": "within_prefs"}, "2026-11-19")
    assert any(f["product_id"] == "463982" and f["kind"] == "short" for f in fl)
    snap_missing = drift.snapshot([{"offers": [a1], "alternatives": [a2]}], lines)
    assert snap_missing["463982"]["found"] is False
    assert snap_missing["463982"]["offered"] is False


def test_s7_run_scenario_clears_simulation_on_success_and_error(monkeypatch):
    from scenarios import s7_drift
    from scenarios._common import Run

    calls = {"sim": 0, "clear": 0, "checks": 0}

    class FakeClient:
        def __init__(self, base, cust):
            self.cust = cust

        def j(self, method, path, **kw):
            return {"exclude_allergens": ["peanut", "nuts"] if self.cust == "school-hervanta" else []}

    def fake_check_ok(cust, key, base, state_path=None):
        calls["checks"] += 1
        pid = SEEDS[cust]["lines"][0]["product_id"]
        flags = []
        if calls["sim"] > calls["clear"]:
            flags.append({"product_id": pid, "kind": "unavailable", "why": "not offered", "route": "auto", "fix": {"action": "none"}})
        return {"standing_order": {"lines": SEEDS[cust]["lines"]}, "flags": flags}

    monkeypatch.setattr(s7_drift, "Client", FakeClient)
    monkeypatch.setattr(s7_drift, "violates", lambda pid, excl: False)
    monkeypatch.setattr(s7_drift, "simulate", lambda c, kind, **kw: calls.__setitem__("sim", calls["sim"] + 1))
    monkeypatch.setattr(s7_drift, "clear_simulation", lambda c: calls.__setitem__("clear", calls["clear"] + 1))
    monkeypatch.setattr(drift, "check", fake_check_ok)

    r = Run("s7_drift", "http://t")
    s7_drift.run_scenario("http://t", r)
    assert calls["sim"] == 1 and calls["clear"] == 1
    assert all(ok for _, ok, _ in r.results)

    def fake_check_boom(cust, key, base, state_path=None):
        if calls["sim"] > calls["clear"]:
            raise RuntimeError("boom during check")
        return {"standing_order": {"lines": SEEDS[cust]["lines"]}, "flags": []}

    monkeypatch.setattr(drift, "check", fake_check_boom)
    with pytest.raises(RuntimeError, match="boom"):
        s7_drift.run_scenario("http://t", Run("s7_drift", "http://t"))
    assert calls["sim"] == 2 and calls["clear"] == 2
