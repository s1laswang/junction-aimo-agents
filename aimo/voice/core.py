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
import math
import os
import re
from typing import Any, Literal

ELEVEN_BASE = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"
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


def _safe_float(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _num(x: Any) -> str:
    v = _safe_float(x)
    if v == 0.0:
        return "0"
    return f"{v:,.0f}" if v.is_integer() else f"{v:,.2f}"


def _eur(x: Any) -> str:
    v = _safe_float(x)
    try:
        cents_total = round(abs(v) * 100)
    except (OverflowError, ValueError):
        return "0 euros"
    whole, cents = divmod(cents_total, 100)
    prefix = "-" if v < 0 and (whole or cents) else ""
    return f"{prefix}{whole:,} euros" + (f" {cents} cents" if cents else "")


def approval_prompt(order: dict, quote: dict, customer_name: str | None = None) -> str:
    """A short, speakable summary of what the approver is about to decide (no ids, no hashes)."""
    o = order if isinstance(order, dict) else {}
    q = quote if isinstance(quote, dict) else {}
    raw_lines = q.get("lines")
    lines = [l for l in raw_lines if isinstance(l, dict)] if isinstance(raw_lines, (list, tuple)) else []
    cname = customer_name.replace("\x00", "").strip() if isinstance(customer_name, str) else ""
    cid_raw = o.get("customer_id")
    cid = cid_raw.replace("\x00", "").strip() if isinstance(cid_raw, str) else ""
    who = cname or cid or "the customer"
    parts = []
    for l in lines[:3]:
        raw_unit = l.get("sales_unit")
        unit = (raw_unit.replace("\x00", "").strip().lower()
                if isinstance(raw_unit, str) and raw_unit.replace("\x00", "").strip()
                else "units")
        raw_pname = l.get("product_name")
        raw_pid = l.get("product_id")
        if isinstance(raw_pname, str) and raw_pname.replace("\x00", "").strip():
            item = raw_pname.replace("\x00", "").strip()
        elif isinstance(raw_pid, (str, int)) and not isinstance(raw_pid, bool) and str(raw_pid).replace("\x00", "").strip():
            item = str(raw_pid).replace("\x00", "").strip()
        else:
            item = "item"
        parts.append(f"{_num(l.get('qty_sales_units', 0))} {unit} of {item}")
    more = f", and {len(lines) - 3} more line{'s' if len(lines) - 3 != 1 else ''}" if len(lines) > 3 else ""
    what = "; ".join(parts) + more if parts else "an order"
    raw_dec = o.get("decision")
    dec = raw_dec if isinstance(raw_dec, dict) else {}
    raw_hits = dec.get("hits")
    hits = [h for h in raw_hits if isinstance(h, dict)] if isinstance(raw_hits, (list, tuple)) else []
    reasons = [
        h["message"].replace("\x00", "").strip()
        for h in hits
        if h.get("outcome") == "needs_approval"
        and isinstance(h.get("message"), str)
        and h["message"].replace("\x00", "").strip()
    ]
    why = f" It needs you because: {reasons[0].rstrip('.')}." if reasons else ""
    raw_role = dec.get("approver_role")
    role = (raw_role.replace("\x00", "").strip().replace("_", " ")
            if isinstance(raw_role, str) and raw_role.replace("\x00", "").strip()
            else "an approver")
    raw_dd = q.get("delivery_date")
    if isinstance(raw_dd, str) and raw_dd.replace("\x00", "").strip():
        delivery = raw_dd.replace("\x00", "").strip()
    elif hasattr(raw_dd, "isoformat"):
        delivery = str(raw_dd.isoformat())
    else:
        delivery = "unspecified"
    return (f"Approval request for {who}: {what}. Total {_eur(q.get('total_net'))} before VAT, "
            f"delivery {delivery}.{why} This needs {role} approval. "
            f"Say approve, or reject. You will confirm on screen before anything is ordered.")


_NEG = re.compile(r"\b(don'?t|do not|never|not|no way|cancel|wait|hold|stop)\b", re.I)
_APPROVE = re.compile(r"\b(approve[ds]?|yes|yeah|yep|confirm(ed)?|go ahead|accept(ed)?|ok(ay)?|sounds good|"
                      r"place it|order it|hyväksy[nt]?|hyväksytään|kyllä|joo|godkänn?|ja)\b", re.I)
_REJECT = re.compile(r"\b(reject(ed)?|decline[ds]?|deny|denied|refuse[ds]?|hylkää|hylätään|ei|nej|avslå)\b", re.I)


def rule_intent(transcript: str) -> dict[str, Any]:
    if not isinstance(transcript, str):
        return {"intent": "unclear", "confidence": 0.0, "reason": "invalid transcript", "source": "rules"}
    t = transcript.replace("\x00", "").strip()
    if not t:
        return {"intent": "unclear", "confidence": 0.0, "reason": "empty transcript", "source": "rules"}
    a, r, n = bool(_APPROVE.search(t)), bool(_REJECT.search(t)), bool(_NEG.search(t))
    if r and not a:
        return {"intent": "reject", "confidence": 0.9, "reason": "heard a rejection word", "source": "rules"}
    if a and not r and not n:
        return {"intent": "approve", "confidence": 0.9, "reason": "heard an approval word, no negation", "source": "rules"}
    if a and n and not r:
        return {"intent": "unclear", "confidence": 0.5, "reason": "approval word with a negation or hedge",
                "source": "rules"}
    return {"intent": "unclear", "confidence": 0.3, "reason": "no clear approve or reject", "source": "rules"}


INTENT_PROMPT = """An approver was asked by voice to approve or reject a purchase order.
They answered (speech-to-text transcript, may be Finnish, Swedish or English):
"{t}"
Classify their decision. Use "unclear" for anything hedged, conditional, negated, mixed, a question, or unrelated.
Never guess approve. Also return a short note (max 15 words) with any condition they stated, else empty."""


def gemini_intent(transcript: str) -> dict[str, Any] | None:
    if not isinstance(transcript, str) or not os.environ.get("GEMINI_API_KEY"):
        return None
    clean = transcript.replace("\x00", "").strip()[:MAX_TRANSCRIPT]
    if not clean:
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
        r = gemini.generate("voice.intent", GEMINI_FAST_MODEL, INTENT_PROMPT.format(t=clean),
                            cfg, retries=2)
        d = json.loads(getattr(r, "text", None) or "{}")
        if not isinstance(d, dict) or d.get("intent") not in ("approve", "reject", "unclear"):
            return None
        raw_conf = d.get("confidence")
        if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)):
            conf = 0.0
        else:
            try:
                conf = float(raw_conf)
            except (TypeError, ValueError, OverflowError):
                conf = 0.0
            if not math.isfinite(conf):
                conf = 0.0
            else:
                conf = max(0.0, min(1.0, conf))
        raw_note = d.get("note")
        note = raw_note.replace("\x00", "").strip()[:120] if isinstance(raw_note, str) else ""
        return {"intent": d["intent"], "confidence": conf, "note": note,
                "source": "gemini", "model": GEMINI_FAST_MODEL}
    except Exception:
        return None


def classify_intent(transcript: str) -> dict[str, Any]:
    if not isinstance(transcript, str):
        return {"intent": "unclear", "confidence": 0.0, "reason": "invalid transcript", "source": "rules",
                "transcript": ""}
    t = transcript.replace("\x00", "").strip()[:MAX_TRANSCRIPT]
    if not t:
        return {"intent": "unclear", "confidence": 0.0, "reason": "empty transcript", "source": "rules",
                "transcript": t}
    rules = rule_intent(t)
    g = gemini_intent(t)
    out = rules
    if isinstance(g, dict) and g.get("intent") in ("approve", "reject", "unclear"):
        gc = max(0.0, min(1.0, _safe_float(g.get("confidence"))))
        g = {**g, "confidence": gc}
        out = g
        if g["intent"] == "approve" and rules["intent"] != "approve":
            out = {**g, "intent": "unclear", "reason": "model said approve but the words are negated or mixed",
                   "rules": rules}
        elif g["intent"] != rules["intent"]:
            out = {**g, "rules": rules}
    return {**out, "transcript": t}


def _client(timeout: float = 20.0):
    import httpx
    return httpx.Client(base_url=ELEVEN_BASE, timeout=timeout)


def tts(text: str) -> dict[str, Any]:
    """ElevenLabs text-to-speech -> {audio_b64, mime}. Raises VoiceError(503) without a key."""
    key = eleven_key()
    if not key:
        raise VoiceError(503, "voice_unavailable", "ELEVENLABS_API_KEY is not set; the browser speaks instead.")
    if not isinstance(text, str) or not text.replace("\x00", "").strip():
        raise VoiceError(422, "text_required", "Text to speak must be a non-empty string.")
    clean_text = text.replace("\x00", "").strip()[:1500]
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip() or DEFAULT_VOICE
    try:
        with _client() as c:
            r = c.post(f"/v1/text-to-speech/{voice}", params={"output_format": "mp3_44100_128"},
                       headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                       json={"text": clean_text, "model_id": TTS_MODEL})
    except Exception as e:
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
    if not isinstance(audio, (bytes, bytearray, memoryview)) or not audio:
        raise VoiceError(422, "audio_required", "No audio received.")
    raw_audio = bytes(audio)
    if len(raw_audio) > MAX_AUDIO_BYTES:
        raise VoiceError(413, "audio_too_large", "Audio too large (max 10 MB).")
    safe_mime = mime if isinstance(mime, str) and mime.strip() else "audio/webm"
    data = {"model_id": STT_MODEL}
    if isinstance(language, str) and language.replace("\x00", "").strip():
        data["language_code"] = language.replace("\x00", "").strip()[:32]
    try:
        with _client(30.0) as c:
            r = c.post("/v1/speech-to-text", headers={"xi-api-key": key}, data=data,
                       files={"file": ("answer" + _ext(safe_mime), raw_audio, safe_mime)})
    except Exception as e:
        raise VoiceError(502, "voice_upstream", f"ElevenLabs STT unreachable ({type(e).__name__})") from e
    if r.status_code != 200:
        raise VoiceError(502, "voice_upstream", f"ElevenLabs STT returned {r.status_code}")
    try:
        j = r.json()
    except Exception as e:
        raise VoiceError(502, "voice_upstream", "ElevenLabs STT returned invalid JSON") from e
    if not isinstance(j, dict):
        raise VoiceError(502, "voice_upstream", "ElevenLabs STT returned unexpected payload")
    raw_txt = j.get("text")
    txt = raw_txt.replace("\x00", "").strip()[:MAX_TRANSCRIPT] if isinstance(raw_txt, str) else ""
    raw_lang = j.get("language_code")
    lang = raw_lang.strip()[:32] if isinstance(raw_lang, str) and raw_lang.strip() else None
    return {"transcript": txt, "language": lang,
            "engine": "elevenlabs", "model": STT_MODEL}


def _ext(mime: str) -> str:
    m = mime if isinstance(mime, str) else ""
    return {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/wav": ".wav",
            "audio/mp4": ".m4a"}.get(m.split(";")[0].strip().lower(), ".webm")
