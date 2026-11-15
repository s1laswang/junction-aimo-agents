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
    if x_agent_id is not None and (not isinstance(x_agent_id, str) or x_agent_id.strip()):
        raise _Refused(403, "agent_cannot_approve", "Agents cannot use voice approval.")
    sess = x_approver_session if isinstance(x_approver_session, str) else None
    who = human_auth.session_approver(sess)
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


def _valid_order_id(raw: Any, *, required: bool) -> tuple[Optional[str], Optional[JSONResponse]]:
    if raw is None:
        if required:
            return None, _err(422, "invalid_request", "order_id must be a string")
        return None, None
    if not isinstance(raw, str):
        return None, _err(422, "invalid_request", "order_id must be a string")
    if "\x00" in raw or len(raw) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None, _err(422, "invalid_request", "order_id must be 1 to 128 printable characters")
    oid = raw.strip()
    if not oid:
        if required:
            return None, _err(422, "invalid_request", "order_id is required")
        return None, None
    return oid, None


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
    if not isinstance(body, dict):
        return _err(422, "invalid_request", "Request body must be a JSON object.")
    order_id, err = _valid_order_id(body.get("order_id"), required=True)
    if err is not None or order_id is None:
        return err or _err(422, "invalid_request", "order_id is required")
    want_audio = body.get("audio", True)
    if not isinstance(want_audio, bool):
        return _err(422, "invalid_request", "audio must be a boolean")
    from aimo.gateway import service
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
    if core.eleven_key() and want_audio:
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
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            cl_int = int(cl)
        except ValueError:
            return _err(400, "invalid_request", "Invalid Content-Length header.")
        if cl_int < 0:
            return _err(400, "invalid_request", "Invalid Content-Length header.")
        if cl_int > core.MAX_AUDIO_BYTES + 65_536:
            return _err(413, "audio_too_large", "Audio too large (max 10 MB).")
    ct = request.headers.get("content-type", "")
    ct_low = ct.strip().lower()
    language = None
    try:
        if ct_low.startswith("multipart/form-data"):
            try:
                async with request.form(max_files=4, max_fields=10) as form:
                    up = form.get("audio")
                    if up is None or isinstance(up, str) or not hasattr(up, "read"):
                        return _err(422, "audio_required", "Send the recording as multipart field `audio`.")
                    audio = await up.read(core.MAX_AUDIO_BYTES + 1)
                    mime = getattr(up, "content_type", None) or "audio/webm"
                    raw_lang = form.get("language")
                    language = raw_lang if isinstance(raw_lang, str) else None
            except Exception:
                return _err(400, "invalid_multipart", "Malformed multipart form upload.")
        else:
            try:
                chunks: list[bytes] = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > core.MAX_AUDIO_BYTES:
                        return _err(413, "audio_too_large", "Audio too large (max 10 MB).")
                    chunks.append(chunk)
                audio = b"".join(chunks)
            except Exception:
                return _err(400, "invalid_request", "Could not read audio request body.")
            mime = ct or "audio/webm"
        if len(audio) > core.MAX_AUDIO_BYTES:
            return _err(413, "audio_too_large", "Audio too large (max 10 MB).")
        if not audio:
            return _err(422, "audio_required", "No audio received.")
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
    if not isinstance(body, dict):
        return _err(422, "invalid_request", "Request body must be a JSON object.")
    raw_transcript = body.get("transcript")
    if not isinstance(raw_transcript, str):
        return _err(422, "invalid_request", "transcript must be a string")
    if "\x00" in raw_transcript or len(raw_transcript) > 4000:
        return _err(422, "invalid_request", "transcript must be a string up to 4,000 characters without null bytes")
    oid, err = _valid_order_id(body.get("order_id"), required=False)
    if err is not None:
        return err
    if oid is not None:
        from aimo.gateway import service
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
    res = core.classify_intent(raw_transcript)
    return {**res, "order_id": oid, "approver": who, "decides": False,
            "confirm_with": {"method": "POST", "path": f"/v1/approvals/{oid or '{order_id}'}/decision",
                             "body": {"decision": res["intent"] if res["intent"] != "unclear" else None,
                                      "quote_hash": "<the order's quote hash>"}}}
