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


class Run:
    def __init__(self, run_id: str, req: RunRequest):
        self.run_id, self.req = run_id, req
        self.steps: list[AgentStep] = []
        self.status = "queued"
        self.result: dict[str, Any] | None = None
        self.created = time.time()
        self.cond = threading.Condition()

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
             "latency_s": (self.result or {}).get("latency_s")}
        if with_steps:
            d["steps"] = [s.model_dump(mode="json") for s in self.steps]
        return d


class RunRegistry:
    def __init__(self):
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()

    def start(self, req: RunRequest, *, transport_factory=None, agent_kwargs: dict | None = None) -> Run:
        run_id = "run-" + uuid.uuid4().hex[:10]
        run = Run(run_id, req)
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
                                        mandate=mandate_for(req.customer_id))
                kw = dict(agent_kwargs)
                if req.model:
                    kw["model"] = req.model
                if req.thinking:
                    kw["thinking"] = req.thinking
                kw.setdefault("escalate", req.escalate)
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


@router.post("/runs")
def create_run(req: RunRequest) -> dict:
    run = registry.start(req)
    return {"run_id": run.run_id, "events_url": f"/v1/agent/runs/{run.run_id}/events"}


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
