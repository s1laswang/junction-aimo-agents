"""Fast offline switch for the catalog's optional Gemini paths (semantic recall, recipe check).

- AIMO_SEMANTIC=0 (or AIMO_OFFLINE=1) turns them off immediately, no network attempt.
- Otherwise a 1.5 s TCP probe to the Gemini endpoint decides; the answer is cached for 60 s, so a
  compute node without internet (FR8) never sits in retry loops. Cached query vectors still work offline.
"""
from __future__ import annotations

import os
import socket
import threading
import time

_HOST = os.environ.get("AIMO_GEMINI_PROBE_HOST", "generativelanguage.googleapis.com")
_lock = threading.Lock()
_state: dict[str, float | bool] = {"t": 0.0, "ok": False}
TTL_S = 60.0


def semantic_enabled() -> bool:
    if os.environ.get("AIMO_SEMANTIC", "1") == "0" or os.environ.get("AIMO_OFFLINE", "0") == "1":
        return False
    return True


def gemini_online(timeout_s: float = 1.5) -> bool:
    """True if the Gemini API host is reachable (cached 60 s). Never raises."""
    if not semantic_enabled():
        return False
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        return False
    with _lock:
        if time.time() - float(_state["t"]) < TTL_S:
            return bool(_state["ok"])
    ok = False
    try:
        with socket.create_connection((_HOST, 443), timeout=timeout_s):
            ok = True
    except OSError:
        ok = False
    with _lock:
        _state.update(t=time.time(), ok=ok)
    return ok


def mark_offline() -> None:
    """Called after a network failure so the next calls skip immediately for TTL_S."""
    with _lock:
        _state.update(t=time.time(), ok=False)
