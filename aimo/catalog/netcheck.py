"""Fast offline switch for the catalog's optional Gemini paths (semantic recall, recipe check).

- AIMO_SEMANTIC=0 (or AIMO_OFFLINE=1) turns them off immediately, no network attempt.
- Otherwise a 1.5 s TCP probe to the Gemini endpoint decides; the answer is cached for 60 s, so a
  compute node without internet (FR8) never sits in retry loops. Cached query vectors still work offline.
"""
from __future__ import annotations

import math
import os
import socket
import threading
import time
from typing import Any, Optional

_DEFAULT_HOST = "generativelanguage.googleapis.com"
_HOST = os.environ.get("AIMO_GEMINI_PROBE_HOST", _DEFAULT_HOST)
_INIT_HOST = _HOST
_lock = threading.Lock()
_state: dict[str, float | bool] = {"t": 0.0, "ok": False}
TTL_S = 60.0
_DEFAULT_TIMEOUT_S = 1.5
_MIN_TIMEOUT_S = 0.05
_MAX_TIMEOUT_S = 10.0
_MAX_HOST_LEN = 253


def _clean_timeout(timeout_s: Any) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        return _DEFAULT_TIMEOUT_S
    try:
        val = float(timeout_s)
    except (ValueError, OverflowError, TypeError):
        return _DEFAULT_TIMEOUT_S
    if not math.isfinite(val):
        return _DEFAULT_TIMEOUT_S
    if val < _MIN_TIMEOUT_S:
        return _MIN_TIMEOUT_S
    if val > _MAX_TIMEOUT_S:
        return _MAX_TIMEOUT_S
    return val


def _clean_ttl() -> float:
    raw = TTL_S
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 60.0
    try:
        val = float(raw)
    except (ValueError, OverflowError, TypeError):
        return 60.0
    return val if (math.isfinite(val) and val >= 0.0) else 60.0


def _valid_host(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_HOST_LEN:
        return None
    for ch in raw:
        code = ord(ch)
        if ch.isspace() or code <= 31 or code == 127 or 0xD800 <= code <= 0xDFFF:
            return None
    if ".." in raw or raw.startswith("."):
        return None
    labels = raw.rstrip(".").split(".")
    if not labels or any(not lbl or len(lbl) > 63 for lbl in labels):
        return None
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return raw


def _probe_host() -> Optional[str]:
    if _HOST != _INIT_HOST:
        raw = _HOST
    elif "AIMO_GEMINI_PROBE_HOST" in os.environ:
        raw = os.environ.get("AIMO_GEMINI_PROBE_HOST")
    else:
        raw = _HOST
    return _valid_host(raw)


def semantic_enabled() -> bool:
    try:
        if os.environ.get("AIMO_SEMANTIC", "1") == "0" or os.environ.get("AIMO_OFFLINE", "0") == "1":
            return False
        return True
    except Exception:
        return False


def gemini_online(timeout_s: float = 1.5) -> bool:
    """True if the Gemini API host is reachable (cached 60 s). Never raises."""
    try:
        if not semantic_enabled():
            return False
        if not (os.environ.get("GEMINI_API_KEY") or "").strip() and not (os.environ.get("GOOGLE_API_KEY") or "").strip():
            return False
        host = _probe_host()
        if host is None:
            return False
        timeout = _clean_timeout(timeout_s)
        ttl = _clean_ttl()
        now = time.time()
        with _lock:
            if isinstance(_state, dict):
                raw_t = _state.get("t", 0.0)
                try:
                    cached_t = 0.0 if isinstance(raw_t, bool) else float(raw_t)
                except (ValueError, OverflowError, TypeError):
                    cached_t = 0.0
                if math.isfinite(cached_t) and cached_t > 0.0 and 0.0 <= (now - cached_t) < ttl:
                    return bool(_state.get("ok", False))
        ok = False
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                ok = True
        except (OSError, ValueError, OverflowError, TypeError):
            ok = False
        with _lock:
            if isinstance(_state, dict):
                _state.update(t=time.time(), ok=ok)
        return ok
    except Exception:
        return False


def mark_offline() -> None:
    """Called after a network failure so the next calls skip immediately for TTL_S."""
    try:
        with _lock:
            if isinstance(_state, dict):
                _state.update(t=time.time(), ok=False)
    except Exception:
        return
