import json
import os
from datetime import date, timedelta

import pytest

pytest.importorskip("aimo.api")
from fastapi.testclient import TestClient

from aimo import audit, mandates

DAY = (date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)).isoformat()
ADMIN = {"X-Admin-Key": "demo-admin"}


@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_CACHE_DIR", "var/cache")
    from aimo.app import app
    with TestClient(app) as cl:
        yield cl


def order(c, key, pid, qty):
    h = {"X-Agent-Key": key}
    q = c.post("/v1/quotes", headers=h, json={"offers": [{"product_id": pid, "quantity": qty}], "delivery_date": DAY})
    assert q.status_code == 200, q.text
    q = q.json()
    o = c.post("/v1/orders", headers=h, json={"quote_id": q["quote_id"], "jws": q["jws"]})
    assert o.status_code == 200, o.text
    return o.json()


def risky(codes, unit):
    path = os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json")
    for p in json.load(open(path)):
        if p["deleted"] or p["synkkaData"].get("names") is None:
            continue
        for cl in p["synkkaData"].get("classifications") or []:
            if cl["name"] == "allergen" and any(v["id"] in codes and v.get("unit") == unit for v in cl["values"]):
                return p["id"]


def test_hospital_blocked_contains(c):
    o = order(c, "demo-hospital-tays", risky({"AP"}, "CONTAINS"), 1)
    assert o["status"] == "blocked" and o["decision"]["outcome"] == "blocked"
    assert "ALLERGEN_CONFLICT" in [r["code"] for r in o["decision"]["reasons"]]


def test_hospital_blocked_traces(c):
    o = order(c, "demo-hospital-tays", risky({"AP", "AN"}, "MAY_CONTAIN"), 1)
    assert o["status"] == "blocked"
    assert any("traces" in r["text"] for r in o["decision"]["reasons"])


def test_school_week_limit(c):
    outs = [order(c, "demo-school-hervanta", "464073", 350) for _ in range(7)]
    codes = [[r["code"] for r in o["decision"]["reasons"]] for o in outs]
    assert codes[0] == ["WITHIN_MANDATE"] and outs[0]["status"] in ("placed", "confirmed")
    hit = next(i for i, cs in enumerate(codes) if "OVER_WEEK_LIMIT" in cs)
    assert hit > 0 and outs[hit]["status"] == "pending_approval"
    spent = sum(o["total_net"] for o in outs[:hit])
    assert spent + outs[hit]["total_net"] > mandates.get("school-hervanta").per_week_eur >= spent


def test_admin_edit_flips_milk(c):
    pid, key = "464073", "demo-hotel-aurora"
    assert order(c, key, pid, 1000)["decision"]["outcome"] == "auto"
    m = mandates.get("hotel-aurora").model_copy(update={"per_order_eur": 1000.0})
    r = c.put("/v1/mandate", headers=ADMIN, json=m.model_dump())
    assert r.status_code == 200, r.text
    assert order(c, key, pid, 1000)["decision"]["outcome"] == "needs_approval"
    ev = [e for e in audit.events(customer="hotel-aurora") if e.action == "mandate_updated"]
    assert any("per_order_eur 2500.0 -> 1000.0" in e.why and e.actor == "human:admin" for e in ev)


def test_reseed_keeps_edits_and_adds_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "r.db"))
    mandates.put(mandates.get("hotel-aurora").model_copy(update={"per_order_eur": 7.0}))
    assert mandates.get("hotel-aurora").per_order_eur == 7.0 and len(mandates.all()) == 3
