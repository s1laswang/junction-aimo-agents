from __future__ import annotations

import json
import time
import types
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import auth, human_auth, service
from aimo.models import AllergenStatus, Product, ProductSummary

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 11, 16)
H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SCHOOL = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
ANNA = "anna@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"


class _FixtureClient:
    base_url = "fixture://valio-mock"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        if not any(p["productId"] == "470889" for p in self.price_raw["products"]):
            for item in self.price_raw["products"]:
                if item["productId"] == "464306":
                    self.price_raw["products"].append(dict(item, productId="470889"))
                    break
        if not any(p["productId"] == "470889" for p in self.avail_raw["products"]):
            for item in self.avail_raw["products"]:
                if item["productId"] == "464306":
                    self.avail_raw["products"].append(dict(item, productId="470889"))
                    break

    def _sel(self, raw: dict, ids: list[str]) -> dict:
        now = time.time()
        meta = {k: v for k, v in raw.items() if k != "products"}
        prods = [{**p, "_fetched_at": now, "_meta": meta} for p in raw["products"] if p["productId"] in ids]
        return {**meta, "products": prods, "_oldest_fetch": now}

    def price(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.price_raw, ids)

    def availability(self, ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        return self._sel(self.avail_raw, ids)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    products = {
        "464306": Product(
            id="464306",
            name="Valio Eila lactose free semi-skimmed milk drink 1 l",
            brand="Valio",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_LACTOSE"],
        ),
        "470889": Product(
            id="470889",
            name="Oatly Oat Drink Whole 1 L",
            brand="Oatly",
            category="10400",
            sales_unit="ST",
            content_amount=1.0,
            content_unit="L",
            claims=["FREE_FROM_MILK", "VEGAN"],
            allergens={"milk": AllergenStatus.free, "nuts": AllergenStatus.free},
            allergen_source={"milk": "structured", "nuts": "structured"},
        ),
    }
    stub_cat = types.SimpleNamespace(
        search=lambda q, **kw: [
            ProductSummary(**p.model_dump(include=set(ProductSummary.model_fields))) for p in products.values()
        ],
        get_product=lambda pid: products.get(str(pid)),
    )
    monkeypatch.setattr(service, "_catalog", lambda: stub_cat)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-shared-approver-pin")
    human_auth.reset_for_tests()
    bc.set_client(_FixtureClient())
    yield
    human_auth.reset_for_tests()
    bc.set_client(None)


def _client(headers: dict[str, str] | None = None) -> TestClient:
    from aimo.gateway.app import create_app

    return TestClient(create_app(with_mcp=False), headers=headers or {})


def _session_for(c: TestClient, approver: str) -> str:
    pin = human_auth.expected_pin_for(approver) or "test-shared-approver-pin"
    res = c.post("/v1/auth/approver-session", headers={"X-Approver-Pin": pin}, json={"approver": approver})
    assert res.status_code == 200, res.text
    return res.json()["session"]


def _pending_aurora(c: TestClient) -> tuple[dict, dict]:
    q = c.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": D.isoformat(), "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
    ).json()
    o = c.post("/v1/orders", headers=H_AURORA, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


def _pending_school(c: TestClient) -> tuple[dict, dict]:
    q = c.post(
        "/v1/quotes",
        headers=H_SCHOOL,
        json={"delivery_date": D.isoformat(), "lines": [{"product_id": "470889", "qty_sales_units": 500, "tier": "preferred"}]},
    ).json()
    o = c.post("/v1/orders", headers=H_SCHOOL, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


def test_cross_customer_approver_blocked_on_voice_prompt_and_intent() -> None:
    c = _client()
    q_aurora, o_aurora = _pending_aurora(c)
    _, o_school = _pending_school(c)

    anna_sess = {"X-Approver-Session": _session_for(c, ANNA)}
    liisa_sess = {"X-Approver-Session": _session_for(c, LIISA)}

    r_bad_prompt = c.post("/v1/voice/prompt", headers=liisa_sess, json={"order_id": o_aurora["order_id"]})
    assert r_bad_prompt.status_code == 403
    assert r_bad_prompt.json()["error"] == "forbidden_approver"
    assert r_bad_prompt.json()["message"]
    assert "quote_hash" not in r_bad_prompt.json()

    r_bad_intent = c.post(
        "/v1/voice/intent",
        headers=liisa_sess,
        json={"transcript": "yes approve", "order_id": o_aurora["order_id"]},
    )
    assert r_bad_intent.status_code == 403
    assert r_bad_intent.json()["error"] == "forbidden_approver"
    assert r_bad_intent.json()["message"]

    events = c.get("/v1/audit", params={"subject_id": o_aurora["order_id"]}).json()
    refusals = [e for e in events if e.get("action") == "approval.refused" and e.get("actor") == f"human:{LIISA}"]
    assert len(refusals) == 2
    assert any("voice prompt" in (e.get("summary") or "") for e in refusals)
    assert any("voice intent" in (e.get("summary") or "") for e in refusals)

    r_ok_prompt = c.post("/v1/voice/prompt", headers=anna_sess, json={"order_id": o_aurora["order_id"]})
    assert r_ok_prompt.status_code == 200
    body_prompt = r_ok_prompt.json()
    assert body_prompt["quote_hash"] == q_aurora["quote_hash"]
    assert "Hotel Aurora" in body_prompt["text"]

    r_ok_intent = c.post(
        "/v1/voice/intent",
        headers=anna_sess,
        json={"transcript": "yes approve", "order_id": o_aurora["order_id"]},
    )
    assert r_ok_intent.status_code == 200
    assert r_ok_intent.json()["intent"] == "approve"
    assert r_ok_intent.json()["decides"] is False

    r_no_order_intent = c.post("/v1/voice/intent", headers=liisa_sess, json={"transcript": "approve"})
    assert r_no_order_intent.status_code == 200
    assert r_no_order_intent.json()["intent"] == "approve"

    r_missing_order_intent = c.post(
        "/v1/voice/intent",
        headers=anna_sess,
        json={"transcript": "approve", "order_id": "ORD-missing-999"},
    )
    assert r_missing_order_intent.status_code == 404
    assert r_missing_order_intent.json()["error"] == "order_not_found"

    assert c.post("/v1/voice/prompt", headers=anna_sess, json={"order_id": o_school["order_id"]}).status_code == 403
    r_school_ok = c.post("/v1/voice/prompt", headers=liisa_sess, json={"order_id": o_school["order_id"]})
    assert r_school_ok.status_code == 200
    assert "Tampere Hervanta School Kitchen" in r_school_ok.json()["text"]


def test_voice_routes_reject_unknown_and_mismatched_customer_header() -> None:
    c = _client()
    _, o_aurora = _pending_aurora(c)
    anna_sess = {"X-Approver-Session": _session_for(c, ANNA)}

    for path, kw in (
        ("/v1/voice/prompt", {"json": {"order_id": o_aurora["order_id"]}}),
        ("/v1/voice/intent", {"json": {"transcript": "approve", "order_id": o_aurora["order_id"]}}),
        ("/v1/voice/transcribe", {"content": b"x", "headers": {"Content-Type": "audio/webm"}}),
    ):
        extra = kw.get("headers", {})
        call_kw = {k: v for k, v in kw.items() if k != "headers"}

        r_unk_sess = c.post(path, headers={**anna_sess, **extra, "X-Customer-Id": "rogue-corp"}, **call_kw)
        assert r_unk_sess.status_code == 403
        assert r_unk_sess.json()["error"] == "unknown_customer"
        assert r_unk_sess.json()["message"]

        r_unk_anon = c.post(path, headers={**extra, "X-Customer-Id": "rogue-corp"}, **call_kw)
        assert r_unk_anon.status_code == 403
        assert r_unk_anon.json()["error"] == "unknown_customer"
        assert r_unk_anon.json()["message"]

    for path, payload in (
        ("/v1/voice/prompt", {"order_id": o_aurora["order_id"]}),
        ("/v1/voice/intent", {"transcript": "approve", "order_id": o_aurora["order_id"]}),
    ):
        r_mis = c.post(path, headers={**anna_sess, "X-Customer-Id": "school-tampere"}, json=payload)
        assert r_mis.status_code == 403
        assert r_mis.json()["error"] == "customer_mismatch"
        assert r_mis.json()["message"]

        r_match = c.post(path, headers={**anna_sess, "X-Customer-Id": "hotel-aurora"}, json=payload)
        assert r_match.status_code == 200


def test_demo_tamper_and_untamper_refuse_agent_identity() -> None:
    pin = "test-shared-approver-pin"
    c = _client(headers={"X-Approver-Pin": pin})
    _, o_aurora = _pending_aurora(c)
    seq = c.get("/v1/audit", params={"subject_id": o_aurora["order_id"]}).json()[0]["seq"]
    sess = _session_for(c, ANNA)

    for endpoint in (f"/v1/demo/tamper/{seq}", f"/v1/demo/untamper/{seq}", "/v1/demo/tamper/1", "/v1/demo/untamper/1"):
        r_pin = c.post(endpoint, headers={"X-Agent-Id": "buyer-1", "X-Approver-Pin": pin})
        assert r_pin.status_code == 403
        assert r_pin.json()["error"] == "agent_cannot_approve"
        assert r_pin.json()["message"]

        r_sess = c.post(endpoint, headers={"X-Agent-Id": "buyer-1", "X-Approver-Session": sess})
        assert r_sess.status_code == 403
        assert r_sess.json()["error"] == "agent_cannot_approve"

    assert c.get("/v1/audit/verify").json()["ok"] is True
    refusals = [
        e
        for e in c.get("/v1/audit", params={"subject_id": "audit:1"}).json()
        if e.get("action") == "approval.refused" and e.get("actor") == "agent:buyer-1"
    ]
    assert len(refusals) >= 2

    r_tamper = c.post(f"/v1/demo/tamper/{seq}")
    assert r_tamper.status_code == 200 and r_tamper.json()["verify"]["ok"] is False
    r_untamper = c.post(f"/v1/demo/untamper/{seq}")
    assert r_untamper.status_code == 200 and r_untamper.json()["verify"]["ok"] is True


def test_agent_keys_equals_separator_and_unsafe_customer_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora=k1,*=k2")
    assert auth._configured_agent_keys() == {"hotel-aurora": "k1", "*": "k2"}

    assert c.get("/v1/products/search", params={"q": "milk"}, headers=H_AURORA).status_code == 401
    assert (
        c.get("/v1/products/search", params={"q": "milk"}, headers={**H_AURORA, "X-Agent-Key": "k2"}).status_code == 403
    )
    assert (
        c.get("/v1/products/search", params={"q": "milk"}, headers={**H_AURORA, "X-Agent-Key": "k1"}).status_code == 200
    )
    assert (
        c.get("/v1/products/search", params={"q": "milk"}, headers={**H_SCHOOL, "X-Agent-Key": "k2"}).status_code == 200
    )

    monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora:key==,school-tampere=k-school")
    assert auth._configured_agent_keys() == {"hotel-aurora": "key==", "school-tampere": "k-school"}
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    lookup_calls: list[int] = []
    real_known = auth.known_customer_ids

    def _tracked_known() -> set[str]:
        lookup_calls.append(1)
        return real_known()

    monkeypatch.setattr(auth, "known_customer_ids", _tracked_known)

    unsafe_ids = [
        "a" * 129,
        "x" * 400,
        "../hotel-aurora",
        "..",
        "hotel-aurora/../../etc/passwd",
        "hotel\\aurora",
        "\x00",
        "hotel-aurora\x00",
        "hotel\naurora",
        "\x1f",
    ]
    for bad in unsafe_ids:
        assert auth.is_known_customer(bad) is False
        with pytest.raises(service.ServiceError) as exc:
            auth.require_known_customer(bad)
        assert exc.value.status == 403
        assert exc.value.code == "unknown_customer"
        preview = exc.value.detail["customer_id"]
        assert len(preview) <= 80
        assert "\x00" not in preview
        assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in preview)
    assert lookup_calls == []

    for blank in (None, "", "   ", "\t\r\n"):
        assert auth.is_known_customer(blank) is False
        with pytest.raises(service.ServiceError) as blank_exc:
            auth.require_known_customer(blank)
        assert blank_exc.value.status == 401
        assert blank_exc.value.code == "identity_required"

    for hdr_bad in ("a" * 200, "../hotel-aurora", "hotel-aurora/sub"):
        r = c.get("/v1/products/search", params={"q": "milk"}, headers={"X-Customer-Id": hdr_bad})
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"

    for body_bad in ("a" * 200, "../hotel-aurora", "hotel-aurora\x00", "\x00"):
        r = c.post(
            "/v1/offers",
            json={"product_ids": ["464306"], "requested_date": D.isoformat(), "customer_id": body_bad},
        )
        assert r.status_code == 403 and r.json()["error"] == "unknown_customer"
