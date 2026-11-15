"""Run registry + streaming for the buyer agent. The gateway mounts `router`:

    from aimo.buyer.runs import router as agent_router
    app.include_router(agent_router)

POST /v1/agent/runs             {text, customer_id, model?, thinking?, transport?} -> {run_id}
GET  /v1/agent/runs             recent runs (id, status, customer, text)
GET  /v1/agent/runs/{run_id}    status, summary, steps so far
GET  /v1/agent/runs/{run_id}/events   SSE: one `AgentStep` JSON per event (event: <kind>), replayed
                                      from the start (or after Last-Event-ID), closes after `final`.

Runs execute in background threads (max AIMO_BUYER_MAX_RUNS concurrent, default 8, to respect
the shared Gemini key). The agent reaches the gateway through MCP by default
(AIMO_BUYER_TRANSPORT=auto|mcp|inproc|fake, AIMO_MCP_URL=http://localhost:8000/mcp).
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import queue
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from aimo.models import AgentStep

_UNSET = object()


def _parse_max_runs(raw: object = _UNSET) -> int:
    val = os.environ.get("AIMO_BUYER_MAX_RUNS") if raw is _UNSET else raw
    if val is None or isinstance(val, bool):
        return 8
    s = val.strip() if isinstance(val, str) else str(val).strip()
    if not s:
        return 8
    try:
        n = int(s)
    except (ValueError, TypeError):
        return 8
    return max(1, n)


def _parse_answer_timeout(raw: object = _UNSET) -> float:
    val = os.environ.get("AIMO_BUYER_ANSWER_TIMEOUT") if raw is _UNSET else raw
    if val is None or isinstance(val, bool):
        return 900.0
    s = val.strip() if isinstance(val, str) else str(val).strip()
    if not s:
        return 900.0
    try:
        t = float(s)
    except (ValueError, TypeError):
        return 900.0
    if not math.isfinite(t) or t <= 0.0:
        return 900.0
    return t


def _safe_run_id(run_id: object) -> str:
    if not isinstance(run_id, str):
        return ""
    cleaned = "".join(
        ch
        for ch in run_id
        if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F) and not (0xD800 <= ord(ch) <= 0xDFFF)
    ).strip()
    return cleaned[:80]


def _invalid_run_id(run_id: object) -> bool:
    if not isinstance(run_id, str):
        return True
    return "\x00" in run_id or len(run_id) > 128 or not run_id.strip()


def _strip_surrogates(text: str) -> str:
    return "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


def _parse_last_event_id(raw: object) -> int:
    if not isinstance(raw, str):
        return 0
    s = raw.strip(" \t\r\n")
    if not s or len(s) > 64 or not (s.isascii() and s.isdigit()):
        return 0
    return int(s)


def mcp_url_for(base_url: str | None = None) -> str:
    explicit = (os.environ.get("AIMO_MCP_URL") or "").strip()
    if explicit:
        return explicit
    if isinstance(base_url, str):
        raw_base = base_url
    elif base_url is not None and hasattr(base_url, "scheme") and hasattr(base_url, "netloc"):
        raw_base = str(base_url)
    else:
        raw_base = ""
    base = raw_base.strip().rstrip("/")
    if base:
        return f"{base}/mcp"
    serve = (os.environ.get("AIMO_SERVE_URL") or "").strip().rstrip("/")
    if serve:
        return f"{serve}/mcp"
    return "http://localhost:8000/mcp"


def demo_faults_enabled() -> bool:
    from aimo.backend import clock

    return clock.request_overrides_allowed(True)


MAX_RUNS = _parse_max_runs()
_sem = threading.BoundedSemaphore(MAX_RUNS)


class RunRequest(BaseModel):
    text: str
    customer_id: str = "hotel-aurora"
    model: Optional[str] = None
    thinking: Optional[str] = None
    transport: Optional[str] = None
    escalate: bool = True
    faults: Optional[str] = None
    image_b64: Optional[str] = None
    image_mime: Optional[str] = None
    clarify: bool = True


class AnswerRequest(BaseModel):
    option_id: Optional[str] = None
    text: Optional[str] = None
    question_id: Optional[str] = None
    customer_id: Optional[str] = None


ANSWER_TIMEOUT_S = _parse_answer_timeout()


class Run:
    def __init__(self, run_id: str, req: RunRequest):
        self.run_id, self.req = run_id, req
        self.steps: list[AgentStep] = []
        self.status = "queued"
        self.result: dict[str, Any] | None = None
        self.created = time.time()
        self.cond = threading.Condition()
        self.answers: "queue.Queue[dict]" = queue.Queue()
        self.pending_question: dict | None = None
        self.image: tuple[bytes, str] | None = None

    def wait_answer(self, question: dict) -> dict | None:
        """Called by the agent thread: block until POST /answer (or timeout -> None)."""
        self.pending_question, prev = question, self.status
        self.status = "waiting_for_answer"
        raw_timeout = ANSWER_TIMEOUT_S
        if (
            isinstance(raw_timeout, (int, float))
            and not isinstance(raw_timeout, bool)
            and math.isfinite(raw_timeout)
            and raw_timeout > 0.0
        ):
            timeout = min(float(raw_timeout), float(threading.TIMEOUT_MAX))
        else:
            timeout = 900.0
        try:
            return self.answers.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.pending_question, self.status = None, prev

    def add(self, step: AgentStep) -> None:
        with self.cond:
            self.steps.append(step)
            self.cond.notify_all()

    def done(self) -> bool:
        return self.status in ("finished", "error")

    def view(self, with_steps: bool = True) -> dict:
        with self.cond:
            steps_snapshot = list(self.steps)
        res = self.result if isinstance(self.result, dict) else {}
        d = {"run_id": self.run_id, "status": self.status, "customer_id": self.req.customer_id,
             "text": self.req.text, "created_at": self.created, "n_steps": len(steps_snapshot),
             "summary": res.get("summary"), "order_status": res.get("status"),
             "latency_s": res.get("latency_s"), "mock": res.get("mock"),
             "pending_question": self.pending_question, "has_image": self.image is not None}
        if with_steps:
            d["steps"] = [s.model_dump(mode="json") for s in steps_snapshot]
        return d


class RunRegistry:
    def __init__(self):
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()

    def start(self, req: RunRequest, *, transport_factory=None, agent_kwargs: dict | None = None,
              image: tuple[bytes, str] | None = None, mcp_url: str | None = None) -> Run:
        run_id = "run-" + uuid.uuid4().hex[:10]
        run = Run(run_id, req)
        run.image = image
        resolved_mcp = (mcp_url.strip() if isinstance(mcp_url, str) else "") or mcp_url_for()
        with self.lock:
            self.runs[run_id] = run
        threading.Thread(target=self._execute, args=(run, transport_factory, agent_kwargs or {}, resolved_mcp),
                         daemon=True, name=f"buyer-{run_id}").start()
        return run

    def get(self, run_id: str) -> Run:
        with self.lock:
            run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        return run

    def _execute(self, run: Run, transport_factory, agent_kwargs: dict, mcp_url: str | None = None) -> None:
        from aimo.buyer.agent import BuyerAgent
        from aimo.buyer.mock_model import MOCK_MODEL_NAME, mock_wanted
        from aimo.buyer.profiles import get_profile, mandate_for
        from aimo.buyer.tools import connect
        req = run.req
        with _sem:
            run.status = "running"
            transport = None
            try:
                prof = get_profile(req.customer_id)
                if transport_factory is not None:
                    transport = transport_factory(req)
                else:
                    target_mcp = (mcp_url.strip() if isinstance(mcp_url, str) else "") or mcp_url_for()
                    transport = connect(req.transport or os.environ.get("AIMO_BUYER_TRANSPORT", "auto"),
                                        agent_id=prof["agent_id"], customer_id=req.customer_id,
                                        mcp_url=target_mcp,
                                        mandate=mandate_for(req.customer_id), faults=req.faults)
                kw = dict(agent_kwargs or {})
                if req.model and not ("generate_fn" not in kw and mock_wanted()):
                    kw["model"] = req.model
                if req.thinking:
                    kw["thinking"] = req.thinking
                kw.setdefault("escalate", req.escalate)
                kw.setdefault("clarify", req.clarify)
                kw.setdefault("answer_fn", run.wait_answer)
                if run.image is not None:
                    kw.setdefault("image", run.image)
                agent = BuyerAgent(transport, req.customer_id, emit=run.add, run_id=run.run_id, **kw)
                if agent.mock:
                    agent.model = MOCK_MODEL_NAME
                run.result = agent.run(req.text)
                res_status = run.result.get("status") if isinstance(run.result, dict) else None
                run.status = "finished" if res_status != "error" else "error"
            except Exception as e:
                from datetime import datetime

                from aimo.buyer.need import HELSINKI
                err_txt = _strip_surrogates(str(e))
                run.add(AgentStep(run_id=run.run_id, seq=len(run.steps) + 1, ts=datetime.now(HELSINKI),
                                  kind="error", title=f"Run could not start: {type(e).__name__}",
                                  data={"error": err_txt[:500]}))
                run.add(AgentStep(run_id=run.run_id, seq=len(run.steps) + 1, ts=datetime.now(HELSINKI),
                                  kind="final", title="Run failed", data={"summary": err_txt[:300], "status": "error"}))
                run.status = "error"
            finally:
                with run.cond:
                    run.cond.notify_all()
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass


registry = RunRegistry()
router = APIRouter(prefix="/v1/agent", tags=["buyer-agent"])


_RUN_BODY = {"requestBody": {"content": {
    "application/json": {"schema": RunRequest.model_json_schema()},
    "multipart/form-data": {"schema": {"type": "object", "properties": {
        "text": {"type": "string"}, "customer_id": {"type": "string"}, "model": {"type": "string"},
        "thinking": {"type": "string"}, "image": {"type": "string", "format": "binary"}}}}}}}


@router.post("/runs", openapi_extra=_RUN_BODY)
async def create_run(request: Request):
    """Start a buyer-agent run. JSON (RunRequest, optional image_b64) or multipart/form-data with an
    `image` file (photo of a handwritten order, menu or invoice) plus text/customer_id fields."""
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError

    from aimo.gateway.auth import check_agent_key, is_known_customer

    def _bad(code: str, message: str, status: int = 422) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    hdr_cust = (request.headers.get("x-customer-id") or "").strip()
    raw_cust = ""
    image = None
    try:
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            try:
                form = await request.form()
            except Exception:
                return _bad("invalid_body", "Invalid multipart form data.")
            fields = {k: v for k, v in form.items() if isinstance(v, str)}
            raw_cust = (fields.get("customer_id") or "").strip()
            up = form.get("image")
            if up is not None and not isinstance(up, str):
                image = (await up.read(15_000_001), up.content_type or "image/png")
            fields["customer_id"] = hdr_cust or raw_cust or "hotel-aurora"
            fields.setdefault("text", "")
            req = RunRequest.model_validate(fields)
        else:
            raw_body = await request.body()
            if not raw_body.strip():
                return _bad("invalid_body", "JSON object body required.")
            try:
                payload = json.loads(raw_body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return _bad("invalid_json", "The request body is not valid JSON.")
            if not isinstance(payload, dict):
                return _bad("invalid_body", "JSON object body required.")
            raw_val = payload.get("customer_id")
            if raw_val is not None and not isinstance(raw_val, str):
                req = RunRequest.model_validate(payload)
            else:
                raw_cust = raw_val.strip() if isinstance(raw_val, str) else ""
                merged = dict(payload)
                merged["customer_id"] = hdr_cust or raw_cust or "hotel-aurora"
                req = RunRequest.model_validate(merged)
    except ValidationError as exc:
        errs = [{"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in exc.errors()[:10]]
        return JSONResponse(
            {"error": "validation_error", "message": errs[0]["msg"] if errs else "Invalid request.", "detail": errs},
            status_code=422,
        )
    if req.image_b64:
        if len(req.image_b64) > 22_000_000:
            return _bad("image_too_large", "image too large (max 15 MB)", 413)
        try:
            image = (base64.b64decode(req.image_b64, validate=True), req.image_mime or "image/png")
        except (ValueError, TypeError):
            return _bad("invalid_image", "image_b64 is not valid base64.")
    if not req.text.strip() and image is None:
        return _bad("text_required", "text or image is required.")
    if image is not None and len(image[0]) > 15_000_000:
        return _bad("image_too_large", "image too large (max 15 MB)", 413)
    if not hdr_cust and not raw_cust:
        return _bad("identity_required", "POST /v1/agent/runs requires X-Customer-Id or customer_id.", 401)
    if hdr_cust and raw_cust and hdr_cust != raw_cust:
        return _bad("customer_mismatch", "X-Customer-Id disagrees with body customer_id.", 403)
    eff_cust = hdr_cust or raw_cust
    req = req.model_copy(update={"customer_id": eff_cust})
    if getattr(registry.start, "__func__", None) is RunRegistry.start:
        if not is_known_customer(eff_cust):
            return _bad("unknown_customer", f"unknown customer {_strip_surrogates(eff_cust[:80])!r}", 403)
    try:
        check_agent_key(eff_cust, request.headers.get("x-agent-key"))
    except Exception as exc:
        return _bad(getattr(exc, "code", "invalid_agent_key"), getattr(exc, "message", str(exc)), getattr(exc, "status", 403))
    ignored: list[str] = []
    norm_transport = req.transport.strip().lower() if isinstance(req.transport, str) else ""
    if not demo_faults_enabled():
        if req.faults:
            ignored.append("faults")
        if norm_transport in ("inproc", "fake"):
            ignored.append("transport")
        if ignored:
            req = req.model_copy(
                update={
                    "faults": None,
                    "transport": None if "transport" in ignored else req.transport,
                }
            )
    base_mcp = mcp_url_for(str(request.base_url))
    run = registry.start(req.model_copy(update={"image_b64": None}), image=image, mcp_url=base_mcp)
    out: dict[str, Any] = {
        "run_id": run.run_id,
        "events_url": f"/v1/agent/runs/{run.run_id}/events",
        "answer_url": f"/v1/agent/runs/{run.run_id}/answer",
    }
    if ignored:
        out["ignored"] = ignored
        out["message"] = (
            "Ignored "
            + " and ".join(ignored)
            + ": fault injection and test transports are operator-only "
            "(start the gateway with AIMO_DEMO_FAULTS=1)."
        )
    return out


@router.post("/runs/{run_id}/answer")
def answer_run(run_id: str, ans: AnswerRequest, request: Request):
    """Answer the run's pending needs_clarification question; the run resumes."""
    from fastapi.responses import JSONResponse

    from aimo.gateway.auth import check_agent_key, is_known_customer

    def _bad(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    hdr_cust = (request.headers.get("x-customer-id") or "").strip()
    body_cust = (ans.customer_id or "").strip()
    if hdr_cust and body_cust and hdr_cust != body_cust:
        return _bad("customer_mismatch", "X-Customer-Id disagrees with body customer_id.", 403)
    eff_cust = hdr_cust or body_cust
    unpatched_real = getattr(registry.start, "__func__", None) is RunRegistry.start
    bad_id = _invalid_run_id(run_id)
    with registry.lock:
        run = None if bad_id else registry.runs.get(run_id)
    if eff_cust:
        if unpatched_real and not is_known_customer(eff_cust):
            return _bad("unknown_customer", f"unknown customer {_strip_surrogates(eff_cust[:80])!r}", 403)
        try:
            check_agent_key(eff_cust, request.headers.get("x-agent-key"))
        except Exception as exc:
            return _bad(
                getattr(exc, "code", "invalid_agent_key"),
                getattr(exc, "message", str(exc)),
                getattr(exc, "status", 403),
            )
        if run is not None and eff_cust != run.req.customer_id:
            return _bad("customer_mismatch", "caller customer_id does not match this run", 403)
    else:
        if unpatched_real:
            return _bad(
                "identity_required",
                "POST /v1/agent/runs/{run_id}/answer requires X-Customer-Id or customer_id.",
                401,
            )
    if bad_id:
        return _bad("invalid_run_id", "invalid run_id", 422)
    if run is None:
        return _bad("run_not_found", f"unknown run {_safe_run_id(run_id)}", 404)
    q = run.pending_question
    if not isinstance(q, dict):
        return _bad("not_waiting_for_answer", "this run is not waiting for an answer", 409)
    if ans.question_id and ans.question_id != q.get("question_id"):
        return _bad("question_mismatch", f"pending question is {_safe_run_id(str(q.get('question_id') or ''))}", 409)
    if not (ans.option_id or "").strip() and not (ans.text or "").strip():
        return _bad("answer_required", "option_id or text is required", 422)
    valid_options = {o["id"] for o in (q.get("options") or []) if isinstance(o, dict) and "id" in o}
    if ans.option_id and ans.option_id not in valid_options:
        return _bad("unknown_option", f"unknown option {_safe_run_id(ans.option_id)}", 422)
    run.answers.put(ans.model_dump(exclude_none=True, exclude={"customer_id"}))
    return {"ok": True, "run_id": run_id, "question_id": q.get("question_id")}


@router.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    eff_limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else 20
    eff_limit = max(1, min(200, eff_limit))
    with registry.lock:
        snapshot = list(registry.runs.values())
    runs = sorted(snapshot, key=lambda r: r.created, reverse=True)[:eff_limit]
    return [r.view(with_steps=False) for r in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    if _invalid_run_id(run_id):
        raise HTTPException(422, "invalid run_id")
    try:
        return registry.get(run_id).view()
    except KeyError:
        raise HTTPException(404, f"unknown run {_safe_run_id(run_id)}")


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    from sse_starlette.sse import EventSourceResponse
    if _invalid_run_id(run_id):
        raise HTTPException(422, "invalid run_id")
    try:
        run = registry.get(run_id)
    except KeyError:
        raise HTTPException(404, f"unknown run {_safe_run_id(run_id)}")
    start = _parse_last_event_id(request.headers.get("last-event-id"))

    async def gen():
        i = start
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            while i < len(run.steps):
                s = run.steps[i]
                i += 1
                yield {"id": str(s.seq), "event": s.kind, "data": s.model_dump_json()}
                if s.kind == "final":
                    return
            if run.done() and i >= len(run.steps):
                return
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= 15:
                idle = 0.0
                yield {"comment": "keep-alive"}

    return EventSourceResponse(gen())
