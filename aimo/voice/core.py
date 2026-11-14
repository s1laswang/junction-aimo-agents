"""Voice approval: the spoken prompt, the transcript -> intent step, and ElevenLabs TTS/STT.

Voice never decides anything. It turns an order into a short spoken prompt, turns the approver's spoken answer
into a transcript and a *suggested* intent (approve / reject / unclear), and hands that back to the UI. The human
then confirms with one click, which calls the normal decision endpoint (`POST /v1/approvals/{id}/decision`) with
their approver session and the exact quote hash. So the human-only approval rules apply unchanged.

ElevenLabs is used when ELEVENLABS_API_KEY is set (TTS: text-to-speech, STT: Scribe). Without it, the browser's
Web Speech API speaks and listens instead; this module then only builds the prompt text and classifies intent.
Intent uses Gemini through `aimo.gemini` (ledger feature `voice.intent`) when GEMINI_API_KEY is set, with a
deterministic rule fallback that is also the safety net: anything negated, hedged or mixed is "unclear".
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Literal

ELEVEN_BASE = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"          # ElevenLabs premade voice; override with ELEVENLABS_VOICE_ID
TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")
MAX_AUDIO_BYTES = 10_000_000
MAX_TRANSCRIPT = 500

Intent = Literal["approve", "reject", "unclear"]


class VoiceError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def eleven_key() -> str | None:
    k = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    return k or None


def status() -> dict[str, Any]:
    on = eleven_key() is not None
    return {"elevenlabs": on, "tts": "elevenlabs" if on else "browser", "stt": "elevenlabs" if on else "browser",
            "intent": "gemini" if os.environ.get("GEMINI_API_KEY") else "rules",
            "decides": False,
            "note": "Voice only suggests approve/reject; the human confirms with one click on the decision endpoint."}


# ---------------------------------------------------------------- prompt text
def _num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def _eur(x: float) -> str:
    whole, cents = divmod(round(float(x) * 100), 100)
    return f"{whole:,} euros" + (f" {cents} cents" if cents else "")


def approval_prompt(order: dict, quote: dict, customer_name: str | None = None) -> str:
    """A short, speakable summary of what the approver is about to decide (no ids, no hashes)."""
    lines = quote.get("lines") or []
    who = customer_name or order.get("customer_id") or "the customer"
    parts = []
    for l in lines[:3]:
        parts.append(f"{_num(l.get('qty_sales_units', 0))} {str(l.get('sales_unit') or 'units').lower()} of "
                     f"{l.get('product_name') or l.get('product_id')}")
    more = f", and {len(lines) - 3} more line{'s' if len(lines) - 3 != 1 else ''}" if len(lines) > 3 else ""
    what = "; ".join(parts) + more if parts else "an order"
    dec = order.get("decision") or {}
    reasons = [h.get("message", "") for h in dec.get("hits") or [] if h.get("outcome") == "needs_approval"]
    why = f" It needs you because: {reasons[0].rstrip('.')}." if reasons else ""
    role = str(dec.get("approver_role") or "an approver").replace("_", " ")
    return (f"Approval request for {who}: {what}. Total {_eur(quote.get('total_net') or 0)} before VAT, "
            f"delivery {quote.get('delivery_date')}.{why} This needs {role} approval. "
            f"Say approve, or reject. You will confirm on screen before anything is ordered.")


# ---------------------------------------------------------------- intent
_NEG = re.compile(r"\b(don'?t|do not|never|not|no way|cancel|wait|hold|stop)\b", re.I)
_APPROVE = re.compile(r"\b(approve[ds]?|yes|yeah|yep|confirm(ed)?|go ahead|accept(ed)?|ok(ay)?|sounds good|"
                      r"place it|order it|hyväksy[nt]?|hyväksytään|kyllä|joo|godkänn?|ja)\b", re.I)
_REJECT = re.compile(r"\b(reject(ed)?|decline[ds]?|deny|denied|refuse[ds]?|hylkää|hylätään|ei|nej|avslå)\b", re.I)


def rule_intent(transcript: str) -> dict[str, Any]:
    t = (transcript or "").strip()
    a, r, n = bool(_APPROVE.search(t)), bool(_REJECT.search(t)), bool(_NEG.search(t))
    if r and not a:
        return {"intent": "reject", "confidence": 0.9, "reason": "heard a rejection word", "source": "rules"}
    if a and not r and not n:
        return {"intent": "approve", "confidence": 0.9, "reason": "heard an approval word, no negation", "source": "rules"}
    if a and n and not r:
        # "don't approve", "no, wait": a negated approval is never read as an approval
        return {"intent": "unclear", "confidence": 0.5, "reason": "approval word with a negation or hedge",
                "source": "rules"}
    return {"intent": "unclear", "confidence": 0.3, "reason": "no clear approve or reject", "source": "rules"}


INTENT_PROMPT = """An approver was asked by voice to approve or reject a purchase order.
They answered (speech-to-text transcript, may be Finnish, Swedish or English):
"{t}"
Classify their decision. Use "unclear" for anything hedged, conditional, negated, mixed, a question, or unrelated.
Never guess approve. Also return a short note (max 15 words) with any condition they stated, else empty."""


def gemini_intent(transcript: str) -> dict[str, Any] | None:
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    try:
        from google.genai import types

        from aimo import gemini
        from aimo.config import GEMINI_FAST_MODEL
        schema = {"type": "object", "properties": {
            "intent": {"type": "string", "enum": ["approve", "reject", "unclear"]},
            "confidence": {"type": "number"}, "note": {"type": "string"}}, "required": ["intent", "confidence"]}
        cfg = types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=schema,
                                          max_output_tokens=200, temperature=0)
        r = gemini.generate("voice.intent", GEMINI_FAST_MODEL, INTENT_PROMPT.format(t=transcript[:MAX_TRANSCRIPT]),
                            cfg, retries=2)
        d = json.loads(r.text or "{}")
        if d.get("intent") not in ("approve", "reject", "unclear"):
            return None
        return {"intent": d["intent"], "confidence": float(d.get("confidence") or 0), "note": str(d.get("note") or "")[:120],
                "source": "gemini", "model": GEMINI_FAST_MODEL}
    except Exception:  # noqa: BLE001 - voice must never break approvals; fall back to rules
        return None


def classify_intent(transcript: str) -> dict[str, Any]:
    t = (transcript or "").strip()[:MAX_TRANSCRIPT]
    if not t:
        return {"intent": "unclear", "confidence": 0.0, "reason": "empty transcript", "source": "rules",
                "transcript": t}
    rules = rule_intent(t)
    g = gemini_intent(t)
    out = rules
    if g is not None:
        out = g
        # The rules are the safety net: if they see a negated or contradictory answer, never return "approve".
        if g["intent"] == "approve" and rules["intent"] != "approve":
            out = {**g, "intent": "unclear", "reason": "model said approve but the words are negated or mixed",
                   "rules": rules}
        elif g["intent"] != rules["intent"]:
            out = {**g, "rules": rules}
    return {**out, "transcript": t}


# ---------------------------------------------------------------- ElevenLabs
def _client(timeout: float = 20.0):
    import httpx
    return httpx.Client(base_url=ELEVEN_BASE, timeout=timeout)


def tts(text: str) -> dict[str, Any]:
    """ElevenLabs text-to-speech -> {audio_b64, mime}. Raises VoiceError(503) without a key."""
    key = eleven_key()
    if not key:
        raise VoiceError(503, "voice_unavailable", "ELEVENLABS_API_KEY is not set; the browser speaks instead.")
    voice = os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
    try:
        with _client() as c:
            r = c.post(f"/v1/text-to-speech/{voice}", params={"output_format": "mp3_44100_128"},
                       headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                       json={"text": text[:1500], "model_id": TTS_MODEL})
    except Exception as e:  # noqa: BLE001
        raise VoiceError(502, "voice_upstream", f"ElevenLabs TTS unreachable ({type(e).__name__})") from e
    if r.status_code != 200:
        raise VoiceError(502, "voice_upstream", f"ElevenLabs TTS returned {r.status_code}")
    return {"audio_b64": base64.b64encode(r.content).decode(), "mime": "audio/mpeg", "engine": "elevenlabs",
            "voice_id": voice, "model": TTS_MODEL}


def stt(audio: bytes, mime: str = "audio/webm", language: str | None = None) -> dict[str, Any]:
    """ElevenLabs Scribe speech-to-text -> {transcript, language}. Raises VoiceError(503) without a key."""
    key = eleven_key()
    if not key:
        raise VoiceError(503, "voice_unavailable", "ELEVENLABS_API_KEY is not set; the browser listens instead.")
    if not audio:
        raise VoiceError(422, "audio_required", "No audio received.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(413, "audio_too_large", "Audio too large (max 10 MB).")
    data = {"model_id": STT_MODEL}
    if language:
        data["language_code"] = language
    try:
        with _client(30.0) as c:
            r = c.post("/v1/speech-to-text", headers={"xi-api-key": key}, data=data,
                       files={"file": ("answer" + _ext(mime), audio, mime)})
    except Exception as e:  # noqa: BLE001
        raise VoiceError(502, "voice_upstream", f"ElevenLabs STT unreachable ({type(e).__name__})") from e
    if r.status_code != 200:
        raise VoiceError(502, "voice_upstream", f"ElevenLabs STT returned {r.status_code}")
    j = r.json()
    return {"transcript": str(j.get("text") or "").strip()[:MAX_TRANSCRIPT], "language": j.get("language_code"),
            "engine": "elevenlabs", "model": STT_MODEL}


def _ext(mime: str) -> str:
    return {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/wav": ".wav",
            "audio/mp4": ".m4a"}.get((mime or "").split(";")[0], ".webm")
