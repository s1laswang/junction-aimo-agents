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
(AIMO_BUYER_TRANSPORT=auto|mcp|inproc|fake).

MCP URL (harden/S2): AIMO_MCP_URL if set explicitly, else the URL `aimo serve` exported for the port it
actually serves (AIMO_SERVE_URL), else the local socket this request arrived on. Never a hard-coded :8000,
and never the client's Host header (a caller must not be able to point our agent at another server).
`auto` = MCP if it answers a fast probe, else the in-process gateway (same server, same guardrails). The
FakeGateway is used only when asked for explicitly (transport=fake): a run that cannot reach the gateway
fails fast with an `error` event and a `final`, it never silently buys from a fake. Every run has a
wall-clock budget (AIMO_BUYER_RUN_TIMEOUT, default 300 s, time spent waiting for a human answer excluded)
and a bounded wait for a free slot (AIMO_BUYER_QUEUE_TIMEOUT, default 30 s).
"""
from __future__ import annotations

import asyncio
import base64
import queue
import json
import os
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from aimo.models import AgentStep

MAX_RUNS = int(os.environ.get("AIMO_BUYER_MAX_RUNS", "8"))
_sem = threading.BoundedSemaphore(MAX_RUNS)
RUN_TIMEOUT_S = float(os.environ.get("AIMO_BUYER_RUN_TIMEOUT", "300"))
QUEUE_TIMEOUT_S = float(os.environ.get("AIMO_BUYER_QUEUE_TIMEOUT", "30"))
PROBE_TIMEOUT_S = float(os.environ.get("AIMO_BUYER_PROBE_TIMEOUT", "2"))
MAX_IMAGE_BYTES = 15_000_000
_CUSTOMER_RE = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$"
_THINKING = ("MINIMAL", "LOW", "MEDIUM", "HIGH")
_TRANSPORTS = ("auto", "mcp", "inproc", "fake")


class RunRequest(BaseModel):
    text: str = Field("", max_length=8000, description="What the buyer needs, in plain language")
    customer_id: str = Field("hotel-aurora", pattern=_CUSTOMER_RE)
    model: Optional[str] = Field(None, max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")
    thinking: Optional[str] = None          # MINIMAL | LOW | MEDIUM | HIGH
    transport: Optional[str] = None         # auto | mcp | inproc | fake (fake only when asked for explicitly)
    escalate: bool = True
    faults: Optional[str] = Field(None, max_length=500)   # X-Aimo-Faults spec (demo fault injection)
    image_b64: Optional[str] = Field(None, max_length=21_000_000)  # photo of a handwritten order / menu (base64)
    image_mime: Optional[str] = Field(None, pattern=r"^image/[A-Za-z0-9.+-]{1,40}$")  # e.g. image/png
    clarify: bool = True                    # ask instead of guessing when the need is ambiguous

    @field_validator("thinking")
    @classmethod
    def _thinking(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if v.upper() not in _THINKING:
            raise ValueError(f"thinking must be one of {', '.join(_THINKING)}")
        return v.upper()

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if v.lower() not in _TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(_TRANSPORTS)}")
        return v.lower()


def _human_error(request: Request):
    """Only a signed-in human may instruct or answer the hosted buyer agent: an approver session
    (X-Approver-Session) or the approver credential (X-Approver-Pin). Returns a JSON error response, or None."""
    from fastapi.responses import JSONResponse
    from aimo.gateway import human_auth
    h = request.headers
    src = request.client.host if request.client else "?"
    if human_auth.check_session(h.get("x-approver-session")):
        return None
    if human_auth.locked_for(src) > 0:
        return JSONResponse({"error": "approver_locked", "message": "Too many wrong credentials from this client; "
                             "try again in a few minutes."}, status_code=429)
    if human_auth.check_pin(h.get("x-approver-pin"), src):
        return None
    return JSONResponse({"error": "approver_auth_required", "message": "Starting or answering an agent run needs a "
                         "signed-in human (X-Approver-Session from POST /v1/auth/approver-session, or "
                         "X-Approver-Pin)."}, status_code=401)


def demo_faults_enabled() -> bool:
    """Fault injection and test transports are operator-only: AIMO_DEMO_FAULTS=1 on the gateway, never in production."""
    from aimo.backend import clock
    return clock.request_overrides_allowed(True)


def mcp_url_for(request: Request | None = None) -> str | None:
    """The gateway's own MCP endpoint: explicit AIMO_MCP_URL, else what `aimo serve --port` exported
    (AIMO_SERVE_URL), else the local socket the request arrived on (scope["server"], not the Host header)."""
    explicit = os.environ.get("AIMO_MCP_URL", "").strip()
    if explicit:
        return explicit
    served = os.environ.get("AIMO_SERVE_URL", "").strip().rstrip("/")
    if served:
        return f"{served}/mcp"
    server = request.scope.get("server") if request is not None else None
    if server and len(server) == 2 and server[1]:
        host = str(server[0])
        if host in ("0.0.0.0", "::", "", "testserver"):
            host = "127.0.0.1" if host != "testserver" else host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{int(server[1])}/mcp"
    return None


def _probe(url: str) -> str | None:
    """None if something answers HTTP at `url` within PROBE_TIMEOUT_S, else a short reason."""
    import httpx
    try:
        httpx.get(url, timeout=PROBE_TIMEOUT_S)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:160]}"


def open_transport(req: "RunRequest", *, agent_id: str, mandate, mcp_url: str | None):
    """Transport for one run. auto = MCP (fast probe) then in-process; never a silent FakeGateway.
    Raises GatewayUnreachable with every attempt's reason when nothing works."""
    from aimo.buyer.tools import connect
    kind = req.transport or os.environ.get("AIMO_BUYER_TRANSPORT", "auto").strip().lower() or "auto"
    kw = dict(agent_id=agent_id, customer_id=req.customer_id, mandate=mandate, faults=req.faults)
    if kind == "fake":
        return connect("fake", **kw)
    errors: list[str] = []
    if kind in ("auto", "mcp"):
        if not mcp_url:
            errors.append("mcp: no MCP URL (set AIMO_MCP_URL or start the gateway with `aimo serve --port N`)")
        else:
            why = _probe(mcp_url)
            if why is None:
                try:
                    return connect("mcp", mcp_url=mcp_url, **kw)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"mcp {mcp_url}: {type(e).__name__}: {str(e)[:200]}")
            else:
                errors.append(f"mcp {mcp_url} unreachable ({why})")
    if kind in ("auto", "inproc"):
        try:
            return connect("inproc", **kw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"inproc: {type(e).__name__}: {str(e)[:200]}")
    raise GatewayUnreachable("The buyer agent could not reach the gateway (no fake fallback): " + "; ".join(errors))


class GatewayUnreachable(RuntimeError):
    pass


class AnswerRequest(BaseModel):
    option_id: Optional[str] = Field(None, max_length=200)    # one of the needs_clarification step's options[].id
    text: Optional[str] = Field(None, max_length=4000)        # or free text
    question_id: Optional[str] = Field(None, max_length=200)


ANSWER_TIMEOUT_S = float(os.environ.get("AIMO_BUYER_ANSWER_TIMEOUT", "900"))


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
        self.waited_s = 0.0                 # time spent waiting for a human answer (not counted against the budget)
        self.finalized = False

    def wait_answer(self, question: dict) -> dict | None:
        """Called by the agent thread: block until POST /answer (or timeout -> None)."""
        self.pending_question, prev = question, self.status
        self.status = "waiting_for_answer"
        t0 = time.time()
        try:
            return self.answers.get(timeout=ANSWER_TIMEOUT_S)
        except queue.Empty:
            return None
        finally:
            self.waited_s += time.time() - t0
            self.pending_question = None
            if not self.done():
                self.status = prev

    def add(self, step: AgentStep) -> None:
        with self.cond:
            if self.finalized:   # the watchdog already closed this run: late steps from the thread are dropped
                return
            self.steps.append(step)
            if step.kind == "final":
                self.finalized = True
            self.cond.notify_all()

    def fail(self, title: str, error: str, summary: str | None = None) -> None:
        """Close the run with an `error` step and a `final` step (once)."""
        from datetime import datetime

        from aimo.buyer.need import HELSINKI
        with self.cond:
            if self.finalized:
                return
            self.steps.append(AgentStep(run_id=self.run_id, seq=len(self.steps) + 1, ts=datetime.now(HELSINKI),
                                        kind="error", title=title, data={"error": error[:500]}))
            self.steps.append(AgentStep(run_id=self.run_id, seq=len(self.steps) + 1, ts=datetime.now(HELSINKI),
                                        kind="final", title="Run failed",
                                        data={"summary": (summary or error)[:300], "status": "error"}))
            self.finalized = True
            self.status = "error"
            self.cond.notify_all()

    def done(self) -> bool:
        return self.status in ("finished", "error")

    def view(self, with_steps: bool = True) -> dict:
        d = {"run_id": self.run_id, "status": self.status, "customer_id": self.req.customer_id,
             "text": self.req.text, "created_at": self.created, "n_steps": len(self.steps),
             "summary": (self.result or {}).get("summary"), "order_status": (self.result or {}).get("status"),
             "latency_s": (self.result or {}).get("latency_s"), "pending_question": self.pending_question,
             "has_image": self.image is not None}
        if with_steps:
            d["steps"] = [s.model_dump(mode="json") for s in self.steps]
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
        run.mcp_url = mcp_url if mcp_url is not None else mcp_url_for(None)  # type: ignore[attr-defined]
        with self.lock:
            self.runs[run_id] = run
        threading.Thread(target=self._execute, args=(run, transport_factory, agent_kwargs or {}),
                         daemon=True, name=f"buyer-{run_id}").start()
        threading.Thread(target=self._watchdog, args=(run,), daemon=True, name=f"buyer-wd-{run_id}").start()
        return run

    @staticmethod
    def _watchdog(run: Run) -> None:
        """A run never waits forever: past its budget (human answer time excluded) it is closed with an error."""
        while not run.done():
            budget = RUN_TIMEOUT_S + run.waited_s + (ANSWER_TIMEOUT_S if run.status == "waiting_for_answer" else 0)
            if time.time() - run.created > budget:
                run.fail("Run timed out", f"no result after {RUN_TIMEOUT_S:.0f} s (AIMO_BUYER_RUN_TIMEOUT)",
                         "The buyer agent did not finish in time; nothing was ordered by this run after the timeout.")
                return
            time.sleep(0.5)

    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        return run

    def _execute(self, run: Run, transport_factory, agent_kwargs: dict) -> None:
        from aimo.buyer.agent import BuyerAgent
        from aimo.buyer.profiles import get_profile, mandate_for
        req = run.req
        if not _sem.acquire(timeout=QUEUE_TIMEOUT_S):
            run.fail("Run could not start: busy", f"all {MAX_RUNS} agent slots stayed busy for {QUEUE_TIMEOUT_S:.0f} s",
                     "Too many agent runs at once; try again in a minute.")
            return
        try:
            if run.done():  # the watchdog closed it while it queued
                return
            run.status = "running"
            transport = None
            try:
                prof = get_profile(req.customer_id)
                if transport_factory is not None:
                    transport = transport_factory(req)
                else:
                    transport = open_transport(req, agent_id=prof["agent_id"], mandate=mandate_for(req.customer_id),
                                               mcp_url=getattr(run, "mcp_url", None))
                kw = dict(agent_kwargs)
                if req.model:
                    kw["model"] = req.model
                if req.thinking:
                    kw["thinking"] = req.thinking
                kw.setdefault("escalate", req.escalate)
                kw.setdefault("clarify", req.clarify)
                kw.setdefault("answer_fn", run.wait_answer)
                if run.image is not None:
                    kw.setdefault("image", run.image)
                agent = BuyerAgent(transport, req.customer_id, emit=run.add, run_id=run.run_id, **kw)
                result = agent.run(req.text)
                if run.status != "error":   # the watchdog may have closed it meanwhile
                    run.result = result
                    run.status = "finished" if result.get("status") != "error" else "error"
            except Exception as e:  # noqa: BLE001
                run.fail(f"Run could not start: {type(e).__name__}" if not run.steps else f"Run failed: {type(e).__name__}",
                         str(e))
            finally:
                with run.cond:
                    run.cond.notify_all()
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            _sem.release()


registry = RunRegistry()
router = APIRouter(prefix="/v1/agent", tags=["buyer-agent"])


_RUN_BODY = {"requestBody": {"content": {
    "application/json": {"schema": RunRequest.model_json_schema()},
    "multipart/form-data": {"schema": {"type": "object", "properties": {
        "text": {"type": "string"}, "customer_id": {"type": "string"}, "model": {"type": "string"},
        "thinking": {"type": "string"}, "image": {"type": "string", "format": "binary"}}}}}}}


@router.post("/runs", openapi_extra=_RUN_BODY)
async def create_run(request: Request) -> dict:
    """Start a buyer-agent run. JSON (RunRequest, optional image_b64) or multipart/form-data with an
    `image` file (photo of a handwritten order, menu or invoice) plus text/customer_id fields."""
    from aimo.gateway.schemas import validation_error_body
    from fastapi.responses import JSONResponse

    def bad(code: str, message: str, status: int = 422) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    denied = _human_error(request)
    if denied is not None:
        return denied
    image = None
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            fields = {k: v for k, v in form.items() if isinstance(v, str)}
            up = form.get("image")
            if up is not None and not isinstance(up, str):
                image = (await up.read(MAX_IMAGE_BYTES + 1), up.content_type or "image/png")
            fields.setdefault("text", "")
            req = RunRequest.model_validate(fields)
        else:
            raw = await request.body()
            if not raw.strip():
                return bad("invalid_body", "Send a JSON object, e.g. {\"text\": \"10 l lactose-free milk for Monday\", "
                                           "\"customer_id\": \"hotel-aurora\"}.")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return bad("invalid_json", "The request body is not valid JSON.")
            if not isinstance(body, dict):
                return bad("invalid_body", "The request body must be a JSON object with at least `text`.")
            req = RunRequest.model_validate(body)
    except ValidationError as e:
        return JSONResponse(validation_error_body(e.errors()), status_code=422)
    if req.image_b64:
        try:
            image = (base64.b64decode(req.image_b64, validate=True), req.image_mime or "image/png")
        except (ValueError, TypeError):
            return bad("invalid_image", "image_b64 is not valid base64.")
    if not req.text.strip() and image is None:
        return bad("text_required", "text or image is required")
    if image is not None and len(image[0]) > MAX_IMAGE_BYTES:
        return bad("image_too_large", "image too large (max 15 MB)", 413)
    ignored: list[str] = []
    if not demo_faults_enabled():
        # a caller must not inject faults (forged prices, fake stock) or pick a test transport through the run API
        if req.faults:
            ignored.append("faults")
        if req.transport in ("inproc", "fake"):
            ignored.append("transport")
        if ignored:
            req = req.model_copy(update={"faults": None, "transport": None if "transport" in ignored else req.transport})
    run = registry.start(req.model_copy(update={"image_b64": None}), image=image, mcp_url=mcp_url_for(request))
    out = {"run_id": run.run_id, "events_url": f"/v1/agent/runs/{run.run_id}/events",
           "answer_url": f"/v1/agent/runs/{run.run_id}/answer"}
    if ignored:
        out["ignored"] = ignored
        out["message"] = ("Ignored " + " and ".join(ignored) + ": fault injection and test transports are operator-only "
                          "(start the gateway with AIMO_DEMO_FAULTS=1).")
    return out


@router.post("/runs/{run_id}/answer")
def answer_run(run_id: str, ans: AnswerRequest, request: Request):
    """Answer the run's pending needs_clarification question; the run resumes. Needs a signed-in human."""
    denied = _human_error(request)
    if denied is not None:
        return denied
    try:
        run = registry.get(run_id)
    except KeyError:
        raise HTTPException(404, f"unknown run {run_id}")
    q = run.pending_question
    if q is None:
        raise HTTPException(409, "this run is not waiting for an answer")
    if ans.question_id and ans.question_id != q.get("question_id"):
        raise HTTPException(409, f"pending question is {q.get('question_id')}")
    if not ans.option_id and not ans.text:
        raise HTTPException(422, "option_id or text is required")
    if ans.option_id and ans.option_id not in {o["id"] for o in q.get("options", [])}:
        raise HTTPException(422, f"unknown option {ans.option_id}")
    run.answers.put(ans.model_dump(exclude_none=True))
    return {"ok": True, "run_id": run_id, "question_id": q.get("question_id")}


@router.get("/runs")
def list_runs(request: Request, limit: int = Query(20, ge=1, le=500)):
    """Recent runs across customers: operator view, needs a signed-in human."""
    denied = _human_error(request)
    if denied is not None:
        return denied
    runs = sorted(registry.runs.values(), key=lambda r: r.created, reverse=True)[:limit]
    return [r.view(with_steps=False) for r in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    try:
        return registry.get(run_id).view()
    except KeyError:
        raise HTTPException(404, f"unknown run {run_id}")


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    from sse_starlette.sse import EventSourceResponse
    try:
        run = registry.get(run_id)
    except KeyError:
        raise HTTPException(404, f"unknown run {run_id}")
    start = 0
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        start = int(last)

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
            if idle >= 15:  # keep-alive comment for proxies
                idle = 0.0
                yield {"comment": "keep-alive"}

    return EventSourceResponse(gen())
