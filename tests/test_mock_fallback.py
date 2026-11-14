"""No Gemini API key -> the buyer agent falls back to a clearly labelled MOCK model (scripted replay
of the tool sequence through the real harness), so the end-to-end demo still runs. Offline."""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, MockModel, mock_wanted
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)

    def boom(*a, **k):  # any real Gemini call in mock mode is a bug
        raise AssertionError("Gemini must not be called in mock mode")
    monkeypatch.setattr("aimo.gemini.generate", boom)


def _approve_soon(gw):
    def run():
        for _ in range(200):
            time.sleep(0.05)
            for o in gw.pending():
                gw.decide(o["order_id"], "approve", "anna@aurora", "test")
                return
    threading.Thread(target=run, daemon=True).start()


def test_mode_switch(no_key, monkeypatch):
    assert mock_wanted()
    monkeypatch.setenv("AIMO_LLM_MODE", "live")
    assert not mock_wanted()
    monkeypatch.setenv("AIMO_LLM_MODE", "mock")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "set-but-forced-mock")
    assert mock_wanted()


def test_key_present_means_no_mock(monkeypatch):
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "x")
    a = BuyerAgent(FakeTransport(FakeGateway()), "hotel-aurora", now=SAT)
    assert a.mock is False and a.model != MOCK_MODEL_NAME


def test_explicit_generate_fn_is_never_replaced(no_key):
    fn = lambda **k: None  # noqa: E731
    a = BuyerAgent(FakeTransport(FakeGateway()), "hotel-aurora", generate_fn=fn, now=SAT)
    assert a.mock is False and a._generate is fn


def test_no_key_end_to_end_fake_gateway_is_labelled_mock(no_key):
    gw = FakeGateway()
    _approve_soon(gw)
    a = BuyerAgent(FakeTransport(gw), gw.customer_id, poll_s=0.05, now=SAT)
    assert a.mock and isinstance(a._generate, MockModel) and a.model == MOCK_MODEL_NAME
    r = a.run(HERO)
    assert r["status"] == "placed" and r["mock"] is True
    assert r["summary"].startswith(MOCK_LABEL)
    kinds = [s["kind"] for s in r["steps"]]
    assert "quote" in kinds and "decision" in kinds and "approval_wait" in kinds
    assert any(MOCK_LABEL in s["title"] for s in r["steps"])
    assert {m["model"] for m in r["models_used"]} == {MOCK_MODEL_NAME}


@pytest.mark.skipif(not config.PRODUCT_JSON.exists(), reason="product data not present")
def test_no_key_end_to_end_real_gateway_inproc(no_key, tmp_path, monkeypatch):
    """Real gateway + policy + signing + audit (recorded mock-API fixtures); only the LLM is mocked."""
    from test_gateway import FixtureClient

    from aimo.buyer.profiles import mandate_for
    from aimo.buyer.tools import connect
    from aimo.gateway import service

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    bc.set_client(FixtureClient())
    try:
        t = connect("inproc", agent_id="buyer-1", customer_id="hotel-aurora", mandate=mandate_for("hotel-aurora"))
        a = BuyerAgent(t, "hotel-aurora", poll_s=0.05, now=SAT, approval_timeout_s=0.5)
        r = a.run("We need 100 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
        assert r["mock"] is True and r["quote"] and r["quote"]["quote_hash"]
        assert r["order"] and r["order"]["status"] in ("placed", "pending_approval", "expired")
        assert service.get_audit_trail(r["order"]["order_id"])["verify"]["ok"]
    finally:
        bc.set_client(None)


def test_auto_transport_without_any_keys_falls_back_to_labelled_fake(no_key, monkeypatch):
    """`aimo demo` with no GEMINI_API_KEY and no AIMO_API_KEY (and no server) still runs: FAKE gateway + MOCK model."""
    from aimo.buyer import run_demo

    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    rc = run_demo.main(["--scenario", "happy", "--auto-approve-as", "anna@hotel-aurora.example",
                        "--base-url", "http://127.0.0.1:9", "--approval-timeout", "20"])
    assert rc == 0
