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
import queue
import json
import os
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aimo.models import AgentStep

MAX_RUNS = int(os.environ.get("AIMO_BUYER_MAX_RUNS", "8"))
_sem = threading.BoundedSemaphore(MAX_RUNS)


class RunRequest(BaseModel):
    text: str
    customer_id: str = "hotel-aurora"
    model: Optional[str] = None
    thinking: Optional[str] = None          # MINIMAL | LOW | MEDIUM | HIGH
    transport: Optional[str] = None         # auto | mcp | inproc | fake
    escalate: bool = True
    faults: Optional[str] = None            # X-Aimo-Faults spec (demo fault injection)
    image_b64: Optional[str] = None         # photo of a handwritten order / menu / invoice (base64)
    image_mime: Optional[str] = None        # e.g. image/png, image/jpeg
    clarify: bool = True                    # ask instead of guessing when the need is ambiguous


class AnswerRequest(BaseModel):
    option_id: Optional[str] = None         # one of the needs_clarification step's options[].id
    text: Optional[str] = None              # or free text
    question_id: Optional[str] = None


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

    def wait_answer(self, question: dict) -> dict | None:
        """Called by the agent thread: block until POST /answer (or timeout -> None)."""
        self.pending_question, prev = question, self.status
        self.status = "waiting_for_answer"
        try:
            return self.answers.get(timeout=ANSWER_TIMEOUT_S)
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
              image: tuple[bytes, str] | None = None) -> Run:
        run_id = "run-" + uuid.uuid4().hex[:10]
        run = Run(run_id, req)
        run.image = image
        with self.lock:
            self.runs[run_id] = run
        threading.Thread(target=self._execute, args=(run, transport_factory, agent_kwargs or {}),
                         daemon=True, name=f"buyer-{run_id}").start()
        return run

    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        return run

    def _execute(self, run: Run, transport_factory, agent_kwargs: dict) -> None:
        from aimo.buyer.agent import BuyerAgent
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
                    transport = connect(req.transport or os.environ.get("AIMO_BUYER_TRANSPORT", "auto"),
                                        agent_id=prof["agent_id"], customer_id=req.customer_id,
                                        mcp_url=os.environ.get("AIMO_MCP_URL", "http://localhost:8000/mcp"),
                                        mandate=mandate_for(req.customer_id), faults=req.faults)
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
                run.result = agent.run(req.text)
                run.status = "finished" if run.result.get("status") != "error" else "error"
            except Exception as e:  # noqa: BLE001
                from datetime import datetime

                from aimo.buyer.need import HELSINKI
                run.add(AgentStep(run_id=run.run_id, seq=len(run.steps) + 1, ts=datetime.now(HELSINKI),
                                  kind="error", title=f"Run could not start: {type(e).__name__}",
                                  data={"error": str(e)[:500]}))
                run.add(AgentStep(run_id=run.run_id, seq=len(run.steps) + 1, ts=datetime.now(HELSINKI),
                                  kind="final", title="Run failed", data={"summary": str(e)[:300], "status": "error"}))
                run.status = "error"
            finally:
                with run.cond:
                    run.cond.notify_all()
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:  # noqa: BLE001
                        pass


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
    image = None
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        up = form.get("image")
        if up is not None and not isinstance(up, str):
            image = (await up.read(), up.content_type or "image/png")
        fields.setdefault("text", "")
        req = RunRequest.model_validate(fields)
    else:
        req = RunRequest.model_validate(await request.json())
    if req.image_b64:
        image = (base64.b64decode(req.image_b64), req.image_mime or "image/png")
    if not req.text and image is None:
        raise HTTPException(422, "text or image is required")
    if image is not None and len(image[0]) > 15_000_000:
        raise HTTPException(413, "image too large (max 15 MB)")
    run = registry.start(req.model_copy(update={"image_b64": None}), image=image)
    return {"run_id": run.run_id, "events_url": f"/v1/agent/runs/{run.run_id}/events",
            "answer_url": f"/v1/agent/runs/{run.run_id}/answer"}


@router.post("/runs/{run_id}/answer")
def answer_run(run_id: str, ans: AnswerRequest) -> dict:
    """Answer the run's pending needs_clarification question; the run resumes."""
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
def list_runs(limit: int = 20) -> list[dict]:
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
