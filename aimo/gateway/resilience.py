"""Timeouts + circuit breakers for the Gemini-dependent paths the gateway calls. Owner: gateway.

  guarded(name, primary, fallback, timeout_s) runs `primary` on a worker thread. On timeout or error it
  returns `fallback()` instead and counts a failure; after `fail_max` consecutive failures the breaker
  opens for `reset_s` seconds and calls go straight to the deterministic fallback (no waiting).

Guarded paths (see service.py):
  catalog.search      Gemini semantic recall      -> FTS only (aimo.catalog.textsearch.search(semantic=False))
  catalog.plan_need   embeddings + recipe check   -> deterministic planner (FTS + rank_substitutes + fill)
  trust.screen_text   Gemini layer-2 screen       -> regex layer only (screen_text(use_gemini=False))
Env: AIMO_SEARCH_TIMEOUT_S (4), AIMO_PLAN_TIMEOUT_S (20), AIMO_SCREEN_TIMEOUT_S (3), AIMO_BREAKER_RESET_S (60).
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from typing import Any, Callable

log = logging.getLogger("aimo.gateway.resilience")

_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="aimo-guard")


def timeout_for(name: str, default: float) -> float:
    key = {"catalog.search": "AIMO_SEARCH_TIMEOUT_S", "catalog.plan_need": "AIMO_PLAN_TIMEOUT_S",
           "trust.screen_text": "AIMO_SCREEN_TIMEOUT_S"}.get(name, "")
    try:
        return float(os.environ.get(key, default)) if key else default
    except ValueError:
        return default


class Breaker:
    def __init__(self, name: str, fail_max: int = 2, reset_s: float | None = None):
        self.name, self.fail_max = name, fail_max
        self.reset_s = reset_s if reset_s is not None else float(os.environ.get("AIMO_BREAKER_RESET_S", "60"))
        self.failures = 0
        self.open_until = 0.0
        self.last_error: str | None = None
        self.stats = {"primary": 0, "fallback": 0, "timeouts": 0, "errors": 0, "opened": 0}
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return time.time() < self.open_until

    def ok(self) -> None:
        with self._lock:
            self.failures = 0
            self.stats["primary"] += 1

    def fail(self, why: str) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = why
            if self.failures >= self.fail_max:
                self.open_until = time.time() + self.reset_s
                self.failures = 0
                self.stats["opened"] += 1
                log.warning("breaker %s opened for %.0fs (%s)", self.name, self.reset_s, why)

    def state(self) -> dict[str, Any]:
        return {"open": self.is_open(), "retry_in_s": max(0, round(self.open_until - time.time())),
                "last_error": self.last_error, **self.stats}

    def reset(self) -> None:
        with self._lock:
            self.failures, self.open_until, self.last_error = 0, 0.0, None


_breakers: dict[str, Breaker] = {}


def breaker(name: str) -> Breaker:
    return _breakers.setdefault(name, Breaker(name))


def states() -> dict[str, dict[str, Any]]:
    return {k: b.state() for k, b in _breakers.items()}


def guarded(name: str, primary: Callable[[], Any], fallback: Callable[[], Any], timeout_s: float) -> tuple[Any, str]:
    """(result, path) where path is "primary" | "fallback:circuit_open" | "fallback:timeout" | "fallback:error"."""
    b = breaker(name)
    if b.is_open():
        b.stats["fallback"] += 1
        return fallback(), "fallback:circuit_open"
    ctx = contextvars.copy_context()  # keep per-request faults / clock in the worker thread
    fut = _pool.submit(ctx.run, primary)
    try:
        res = fut.result(timeout=timeout_s)
        b.ok()
        return res, "primary"
    except FutTimeout:
        b.stats["timeouts"] += 1
        b.fail(f"timeout after {timeout_s:g}s")
        path = "fallback:timeout"
    except Exception as e:  # noqa: BLE001
        from aimo.backend.client import UpstreamError
        from aimo.gateway.service import ServiceError

        if isinstance(e, (ServiceError, UpstreamError)):  # a real answer / upstream problem, not a Gemini failure
            b.ok()
            raise
        b.stats["errors"] += 1
        b.fail(f"{type(e).__name__}: {e}"[:200])
        path = "fallback:error"
    b.stats["fallback"] += 1
    log.warning("%s degraded to deterministic fallback (%s)", name, path)
    return fallback(), path
