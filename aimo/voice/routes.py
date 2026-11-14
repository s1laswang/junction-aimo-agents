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
    if x_agent_id and x_agent_id.strip():
        raise _Refused(403, "agent_cannot_approve", "Agents cannot use voice approval.")
    who = human_auth.session_approver(x_approver_session)
    if not who:
        raise _Refused(401, "approver_session_required",
                       "Sign in as an approver first (POST /v1/auth/approver-session), then send X-Approver-Session.")
    return who


def _check_customer_header(request: Request) -> Optional[str] | JSONResponse:
    from aimo.gateway import auth, service
    raw = request.headers.get("x-customer-id")
    if raw is None or not raw.strip(" \t\r\n"):
        return None
    if not auth.is_known_customer(raw):
        try:
            auth.require_known_customer(raw)
        except service.ServiceError as e:
            return _err(e.status, e.code, e.message)
        return _err(403, "unknown_customer", "Unknown X-Customer-Id; call GET /v1/customers for valid customer ids.")
    return raw.strip(" \t\r\n")


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
    xcid = _check_customer_header(request)
    if isinstance(xcid, JSONResponse):
        return xcid
    who = await _who(request)
    if isinstance(who, JSONResponse):
        return who
    from aimo.gateway import service
    order_id = str((body or {}).get("order_id") or "").strip()
    try:
        order = service.get_order(order_id)
        quote = service.get_quote(order.quote_id)
    except service.ServiceError as e:
        return _err(e.status, e.code, e.message)
    o, q = order.model_dump(mode="json"), quote.model_dump(mode="json")
    if xcid and xcid != o["customer_id"]:
        return _err(403, "customer_mismatch",
                    f"request header names customer {xcid!r} but order {order_id!r} belongs to {o['customer_id']!r}")
    if not service._directory_role(o["customer_id"], who):
        service.audit_refusal(order_id, f"human:{who}",
                              f"approver {who} is not in {o['customer_id']} directory (voice prompt)")
        return _err(403, "forbidden_approver",
                    f"Approver {who} is not in the {o['customer_id']} approver directory.")
    name = next((c.get("name") for c in service.list_customers()
                 if (c.get("customer_id") or c.get("id")) == o["customer_id"]), None)
    text = core.approval_prompt(o, q, name)
    out: dict[str, Any] = {"order_id": order_id, "status": o["status"], "quote_hash": q.get("quote_hash"),
                           "text": text, "approver": who, "tts": "browser"}
    if core.eleven_key() and (body or {}).get("audio", True):
        try:
            out.update(core.tts(text))
            out["tts"] = "elevenlabs"
        except core.VoiceError as e:
            out["tts_error"] = e.message
    return out


@router.post("/transcribe", summary="Speech-to-text with ElevenLabs Scribe (503 without a key)")
async def voice_transcribe(request: Request):
    xcid = _check_customer_header(request)
    if isinstance(xcid, JSONResponse):
        return xcid
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
    xcid = _check_customer_header(request)
    if isinstance(xcid, JSONResponse):
        return xcid
    who = await _who(request)
    if isinstance(who, JSONResponse):
        return who
    raw_order_id = (body or {}).get("order_id")
    if raw_order_id is not None and str(raw_order_id).strip():
        from aimo.gateway import service
        oid = str(raw_order_id).strip()
        try:
            order = service.get_order(oid)
        except service.ServiceError as e:
            return _err(e.status, e.code, e.message)
        if xcid and xcid != order.customer_id:
            return _err(403, "customer_mismatch",
                        f"request header names customer {xcid!r} but order {oid!r} belongs to {order.customer_id!r}")
        if not service._directory_role(order.customer_id, who):
            service.audit_refusal(oid, f"human:{who}",
                                  f"approver {who} is not in {order.customer_id} directory (voice intent)")
            return _err(403, "forbidden_approver",
                        f"Approver {who} is not in the {order.customer_id} approver directory.")
    t = str((body or {}).get("transcript") or "")
    res = core.classify_intent(t)
    return {**res, "order_id": (body or {}).get("order_id"), "approver": who, "decides": False,
            "confirm_with": {"method": "POST", "path": f"/v1/approvals/{(body or {}).get('order_id') or '{order_id}'}/decision",
                             "body": {"decision": res["intent"] if res["intent"] != "unclear" else None,
                                      "quote_hash": "<the order's quote hash>"}}}
