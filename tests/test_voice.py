"""Voice approval (S5): voice suggests, a human confirms on the normal decision endpoint. Voice never decides."""
import base64

import pytest
from fastapi.testclient import TestClient

from test_gateway import APPROVER, H, _quote, api, isolated  # noqa: F401  (fixtures reused)


def _anon():
    from aimo.gateway.app import create_app
    return TestClient(create_app(with_mcp=False))


def _session(api):
    from aimo.gateway import human_auth
    human_auth.reset_for_tests()
    return api.post("/v1/auth/approver-session", json={"approver": "anna@hotel-aurora.example"}).json()["session"]


def _pending(api):
    q = _quote(api, [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]).json()
    o = api.post("/v1/orders", headers=H, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    assert o["status"] == "pending_approval"
    return q, o


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.mark.parametrize("t,want", [
    ("Yes, approve it", "approve"), ("approve", "approve"), ("ok go ahead", "approve"), ("hyväksytään", "approve"),
    ("reject", "reject"), ("no, reject that", "reject"), ("hylkää", "reject"),
    ("don't approve", "unclear"), ("do not approve this", "unclear"), ("wait, not yet", "unclear"),
    ("approve, no actually reject", "unclear"), ("what is the price?", "unclear"), ("", "unclear"),
])
def test_rule_intent_never_guesses_approve(t, want):
    from aimo.voice.core import classify_intent
    assert classify_intent(t)["intent"] == want


def test_gemini_approve_is_overruled_by_negation(monkeypatch):
    from aimo.voice import core
    monkeypatch.setattr(core, "gemini_intent", lambda t: {"intent": "approve", "confidence": 0.9, "source": "gemini"})
    assert core.classify_intent("don't approve this")["intent"] == "unclear"
    assert core.classify_intent("yes approve")["intent"] == "approve"


def test_prompt_text_is_speakable():
    from aimo.voice.core import approval_prompt
    o = {"customer_id": "hotel-aurora", "decision": {"approver_role": "procurement_manager", "hits": [
        {"rule_id": "x", "outcome": "needs_approval", "message": "Substitute for your usual milk."}]}}
    q = {"lines": [{"qty_sales_units": 1000, "sales_unit": "ST", "product_name": "Valio Eila milk 1 L"}],
         "total_net": 1010.5, "delivery_date": "2026-11-16"}
    t = approval_prompt(o, q, "Hotel Aurora")
    assert "Hotel Aurora" in t and "1,010 euros 50 cents" in t and "procurement manager" in t
    assert "Substitute for your usual milk" in t and "confirm on screen" in t


def test_voice_routes_need_a_human_session_and_refuse_agents(api):
    anon = _anon()
    assert anon.get("/v1/voice/status").json()["decides"] is False
    for path, body in (("/v1/voice/prompt", {"order_id": "x"}), ("/v1/voice/intent", {"transcript": "approve"})):
        r = anon.post(path, json=body)
        assert r.status_code == 401 and r.json()["error"] == "approver_session_required"
        r = anon.post(path, json=body, headers={"X-Agent-Id": "buyer-1", "X-Approver-Session": _session(api)})
        assert r.status_code == 403 and r.json()["error"] == "agent_cannot_approve"
    # a PIN alone is not enough for voice: sign in first
    assert api.post("/v1/voice/intent", json={"transcript": "approve"}).status_code == 401


def test_voice_flow_suggests_then_human_confirms_with_quote_hash(api):
    q, o = _pending(api)
    anon = _anon()
    s = {"X-Approver-Session": _session(api)}
    p = anon.post("/v1/voice/prompt", headers=s, json={"order_id": o["order_id"]}).json()
    assert p["tts"] == "browser" and p["quote_hash"] == q["quote_hash"] and "approve, or reject" in p["text"]
    i = anon.post("/v1/voice/intent", headers=s, json={"transcript": "yes approve", "order_id": o["order_id"]}).json()
    assert i["intent"] == "approve" and i["decides"] is False
    # voice changed nothing
    assert anon.get(f"/v1/orders/{o['order_id']}", headers=H).json()["status"] == "pending_approval"
    # without a key, STT is the browser's job
    assert anon.post("/v1/voice/transcribe", headers={**s, "Content-Type": "audio/webm"}, content=b"x").status_code == 503
    # the one confirm click: the normal decision endpoint, same session, the quote hash
    r = anon.post(f"/v1/approvals/{o['order_id']}/decision", headers=s,
                  json={"decision": i["intent"], "quote_hash": q["quote_hash"], "note": "Voice: “yes approve”"})
    assert r.status_code == 200 and r.json()["status"] == "placed"
    assert r.json()["approved_by"].startswith("anna")


def test_elevenlabs_used_when_key_present(api, monkeypatch):
    from aimo.voice import core
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    calls = []

    class Resp:
        def __init__(self, code, content=b"", js=None):
            self.status_code, self.content, self._js = code, content, js

        def json(self):
            return self._js

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, **kw):
            calls.append((path, kw))
            assert kw["headers"]["xi-api-key"] == "test-key"
            if "text-to-speech" in path:
                return Resp(200, b"ID3mp3")
            return Resp(200, js={"text": "Approve.", "language_code": "eng"})

    monkeypatch.setattr(core, "_client", lambda timeout=20.0: Fake())
    q, o = _pending(api)
    s = {"X-Approver-Session": _session(api)}
    anon = _anon()
    p = anon.post("/v1/voice/prompt", headers=s, json={"order_id": o["order_id"]}).json()
    assert p["tts"] == "elevenlabs" and base64.b64decode(p["audio_b64"]) == b"ID3mp3"
    r = anon.post("/v1/voice/transcribe", headers=s, files={"audio": ("a.webm", b"OggS...", "audio/webm")})
    assert r.status_code == 200 and r.json()["transcript"] == "Approve."
    assert [c[0] for c in calls][-1] == "/v1/speech-to-text"
    assert anon.get(f"/v1/orders/{o['order_id']}", headers=H).json()["status"] == "pending_approval"
