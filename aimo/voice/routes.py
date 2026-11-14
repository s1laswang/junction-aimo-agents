"""/v1/voice/*: voice approval helpers. None of these routes can approve or reject anything.

- GET  /v1/voice/status       which engines are on (ElevenLabs or browser fallback, Gemini or rules)
- POST /v1/voice/prompt       {order_id} -> spoken approval prompt text (+ ElevenLabs mp3 when the key is set)
- POST /v1/voice/transcribe   audio (multipart `audio`) -> transcript via ElevenLabs Scribe (503 without a key)
- POST /v1/voice/intent       {transcript, order_id?} -> suggested intent approve|reject|unclear

All routes need a signed-in human approver session (`X-Approver-Session`) and refuse agent identity, because the
prompt reveals order details and the engines cost money. The decision itself always goes through
POST /v1/approvals/{order_id}/decision with the same session and the quote hash, after one confirm click.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from aimo.voice import core

router = APIRouter(prefix="/v1/voice", tags=["voice"])


class _Refused(Exception):
    def __init__(self, status: int, error: str, message: str):
        self.status, self.error, self.message = status, error, message


def _err(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def human_session(x_approver_session: Optional[str], x_agent_id: Optional[str]) -> str:
    """The signed-in approver id, or a refusal (agents never; humans need a session)."""
    from aimo.gateway import human_auth
    if x_agent_id:
        raise _Refused(403, "agent_cannot_approve", "Agents cannot use voice approval.")
    who = human_auth.session_approver(x_approver_session)
    if not who:
        raise _Refused(401, "approver_session_required",
                       "Sign in as an approver first (POST /v1/auth/approver-session), then send X-Approver-Session.")
    return who


async def _who(request: Request) -> str | JSONResponse:
    try:
        return human_session(request.headers.get("x-approver-session"), request.headers.get("x-agent-id"))
    except _Refused as e:
        return _err(e.status, e.error, e.message)


@router.get("/status", summary="Voice engines (ElevenLabs or browser fallback)")
def voice_status() -> dict[str, Any]:
    return core.status()


@router.post("/prompt", summary="Spoken approval prompt for a pending order (never decides)")
async def voice_prompt(request: Request, body: dict[str, Any] = Body(default={}, examples=[{"order_id": "O-..."}])):
    who = await _who(request)
    if isinstance(who, JSONResponse):
        return who
    from aimo.gateway import service
    order_id = str((body or {}).get("order_id") or "")
    try:
        order = service.get_order(order_id)
        quote = service.get_quote(order.quote_id)
    except service.ServiceError as e:
        return _err(e.status, e.code, e.message)
    o, q = order.model_dump(mode="json"), quote.model_dump(mode="json")
    name = next((c.get("name") for c in service.list_customers() if c.get("id") == o["customer_id"]), None)
    text = core.approval_prompt(o, q, name)
    out: dict[str, Any] = {"order_id": order_id, "status": o["status"], "quote_hash": q.get("quote_hash"),
                           "text": text, "approver": who, "tts": "browser"}
    if core.eleven_key() and (body or {}).get("audio", True):
        try:
            out.update(core.tts(text))
            out["tts"] = "elevenlabs"
        except core.VoiceError as e:  # fall back to browser speech, say why
            out["tts_error"] = e.message
    return out


@router.post("/transcribe", summary="Speech-to-text with ElevenLabs Scribe (503 without a key)")
async def voice_transcribe(request: Request):
    who = await _who(request)
    if isinstance(who, JSONResponse):
        return who
    ct = request.headers.get("content-type", "")
    language = None
    try:
        if ct.startswith("multipart/form-data"):
            form = await request.form()
            up = form.get("audio")
            if up is None or isinstance(up, str):
                return _err(422, "audio_required", "Send the recording as multipart field `audio`.")
            audio, mime = await up.read(), (up.content_type or "audio/webm")
            language = form.get("language") if isinstance(form.get("language"), str) else None
        else:
            audio, mime = await request.body(), ct or "audio/webm"
        res = core.stt(audio, mime, language)
    except core.VoiceError as e:
        return _err(e.status, e.error, e.message)
    return {**res, "approver": who}


@router.post("/intent", summary="Transcript -> suggested intent (approve | reject | unclear); never decides")
async def voice_intent(request: Request, body: dict[str, Any] = Body(default={}, examples=[{"transcript": "yes, approve it"}])):
    who = await _who(request)
    if isinstance(who, JSONResponse):
        return who
    t = str((body or {}).get("transcript") or "")
    res = core.classify_intent(t)
    return {**res, "order_id": (body or {}).get("order_id"), "approver": who, "decides": False,
            "confirm_with": {"method": "POST", "path": f"/v1/approvals/{(body or {}).get('order_id') or '{order_id}'}/decision",
                             "body": {"decision": res["intent"] if res["intent"] != "unclear" else None,
                                      "quote_hash": "<the order's quote hash>"}}}
