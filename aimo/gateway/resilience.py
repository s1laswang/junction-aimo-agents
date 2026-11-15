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
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from typing import Any, Callable

log = logging.getLogger("aimo.gateway.resilience")

_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="aimo-guard")


def _parse_positive_float(raw: Any, default: float) -> float:
    safe_default = (
        float(default)
        if (not isinstance(default, bool) and isinstance(default, (int, float)) and math.isfinite(default) and default > 0)
        else 5.0
    )
    if raw is None or isinstance(raw, bool):
        return safe_default
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return safe_default
    if not math.isfinite(val) or val <= 0:
        return safe_default
    return val


def timeout_for(name: str, default: float) -> float:
    safe_default = _parse_positive_float(None, default)
    key = {"catalog.search": "AIMO_SEARCH_TIMEOUT_S", "catalog.plan_need": "AIMO_PLAN_TIMEOUT_S",
           "trust.screen_text": "AIMO_SCREEN_TIMEOUT_S"}.get(name if isinstance(name, str) else "", "")
    if not key:
        return safe_default
    raw = os.environ.get(key)
    if raw is None or isinstance(raw, bool):
        return safe_default
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return safe_default
    if not math.isfinite(val) or val <= 0:
        return safe_default
    return min(300.0, max(0.05, val))


def _parse_reset_s(raw: Any, default: float = 60.0) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(val) or val <= 0:
        return default
    return min(86400.0, val)


def _parse_fail_max(raw: Any, default: int = 2) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw < 1 or int(raw) != raw:
            return default
        return min(1000, int(raw))
    if isinstance(raw, int):
        return min(1000, raw) if raw >= 1 else default
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            val = int(s)
        except (TypeError, ValueError, OverflowError):
            return default
        return min(1000, val) if val >= 1 else default
    return default


class Breaker:
    def __init__(self, name: str, fail_max: int = 2, reset_s: float | None = None):
        self.name = str(name)
        self.fail_max = _parse_fail_max(fail_max)
        self.reset_s = (
            _parse_reset_s(reset_s)
            if reset_s is not None
            else _parse_reset_s(os.environ.get("AIMO_BREAKER_RESET_S"))
        )
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
        rem = self.open_until - time.time()
        retry_in = max(0, round(rem)) if math.isfinite(rem) else 0
        return {"open": self.is_open(), "retry_in_s": retry_in,
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
    eff_timeout = min(300.0, _parse_positive_float(timeout_s, 5.0))
    ctx = contextvars.copy_context()
    fut = _pool.submit(ctx.run, primary)
    try:
        res = fut.result(timeout=eff_timeout)
        b.ok()
        return res, "primary"
    except FutTimeout:
        b.stats["timeouts"] += 1
        b.fail(f"timeout after {eff_timeout:g}s")
        path = "fallback:timeout"
    except Exception as e:
        from pydantic import ValidationError

        from aimo.backend.client import UpstreamError
        from aimo.gateway.service import ServiceError

        if isinstance(e, (ServiceError, UpstreamError, ValidationError)) or (
            isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError)
        ):
            b.ok()
            raise
        b.stats["errors"] += 1
        b.fail(f"{type(e).__name__}: {e}"[:200])
        path = "fallback:error"
    b.stats["fallback"] += 1
    log.warning("%s degraded to deterministic fallback (%s)", name, path)
    return fallback(), path
