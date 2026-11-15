"""Thin Gemini wrapper: one shared client + usage ledger. Every Gemini call in the product goes
through here so we can report usage for the Google DeepMind submission. Owner: CEO (buyer may extend).

Ledger schema (shared cross-team): one JSONL row per call
{ts, feature, model, kind, in, out, thinking, cached, latency_s, ok, error, run_id, host, pid, meta}
Never records prompts or keys.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import quote as _url_quote

from aimo import config as _config
from aimo.config import DATA_DIR

LEDGER_DIR = DATA_DIR / "gemini" / "ledger"

_MAX_TOKEN_COUNT = 10**18
_MAX_RAW_DEPTH = 32
_MIN_SECRET_LEN = 6
_SAFE_HOST_RE = re.compile(r"[^A-Za-z0-9._-]+")
_REDACTED_HEADER_KEYS = frozenset({"x-goog-api-key", "x-api-key"})

_client_lock = threading.RLock()
_ledger_lock = threading.Lock()
_trace_lock = threading.Lock()
_exhausted_lock = threading.Lock()

_tl = threading.local()
_exhausted: dict[str, float] = {}
EXHAUSTED_S = 1800.0


def _safe_str(x: Any) -> str:
    if isinstance(x, str):
        return x
    try:
        return str(x)
    except Exception:
        return "<unprintable>"


def _clean_token(x: Any, default: str = "") -> str:
    if x is None or isinstance(x, bool):
        return default
    s = _safe_str(x).replace("\x00", "").strip()
    return s if s else default


def _clean_model_name(model: Any, default: str) -> str:
    if not isinstance(model, str):
        return default
    cleaned = model.replace("\x00", "").strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        return default
    return cleaned


def _safe_latency(v: Any) -> float:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        f = float(v)
    except OverflowError:
        return 0.0
    if not math.isfinite(f) or f < 0.0:
        return 0.0
    return round(f, 3)


def _safe_host() -> str:
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    head = _clean_token(raw).split(".")[0]
    cleaned = _SAFE_HOST_RE.sub("_", head).strip("._-")
    return cleaned or "host"


def _secrets() -> tuple[str, ...]:
    candidates: list[str] = []
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AIMO_API_KEY", "VALIO_API_KEY"):
        raw = os.environ.get(env_name, "")
        if isinstance(raw, str):
            candidates.append(raw.replace("\x00", "").strip())
    for attr in ("GEMINI_API_KEY", "AIMO_API_KEY"):
        cfg_val = getattr(_config, attr, "")
        if isinstance(cfg_val, str):
            candidates.append(cfg_val.replace("\x00", "").strip())
    expanded: list[str] = []
    for s in candidates:
        if len(s) >= _MIN_SECRET_LEN:
            expanded.append(s)
            encoded = _url_quote(s, safe="")
            if encoded != s and len(encoded) >= _MIN_SECRET_LEN:
                expanded.append(encoded)
    unique = list(dict.fromkeys(expanded))
    unique.sort(key=len, reverse=True)
    return tuple(unique)


@lru_cache(maxsize=1)
def _cached_client():
    from google import genai

    key = ""
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        raw = os.environ.get(env_name, "")
        if isinstance(raw, str):
            cleaned = raw.replace("\x00", "").strip()
            if cleaned:
                key = cleaned
                break
    return genai.Client(api_key=key) if key else genai.Client()


def client():
    with _client_lock:
        return _cached_client()


def _clear_client_cache() -> None:
    with _client_lock:
        _cached_client.cache_clear()


setattr(client, "cache_clear", _clear_client_cache)
setattr(client, "cache_info", _cached_client.cache_info)


def _n(u: Any, attr: str) -> int:
    if u is None or not isinstance(attr, str) or not attr:
        return 0
    if isinstance(u, dict):
        v = u.get(attr, 0)
    else:
        try:
            v = getattr(u, attr, 0)
        except Exception:
            return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v if 0 <= v <= _MAX_TOKEN_COUNT else 0
    if isinstance(v, float):
        if math.isfinite(v) and 0.0 <= v <= float(_MAX_TOKEN_COUNT) and v.is_integer():
            return int(v)
        return 0
    return 0


def _raw(x: Any, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    """JSON-safe copy of a google-genai request/response object for the raw trace."""
    if x is None or isinstance(x, (str, bool, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, (bytes, bytearray)):
        return bytes(x).decode("utf-8", errors="replace")
    if _depth >= _MAX_RAW_DEPTH:
        return "<max_depth>"
    seen = set() if _seen is None else _seen
    obj_id = id(x)
    if obj_id in seen:
        return "<cycle>"
    next_seen = seen | {obj_id}
    dump_fn = getattr(x, "model_dump", None)
    if callable(dump_fn):
        try:
            dumped = dump_fn(mode="json", exclude_none=True)
        except Exception:
            return _safe_str(x)
        return _raw(dumped, next_seen, _depth + 1)
    if isinstance(x, dict):
        return {_safe_str(k): _raw(v, next_seen, _depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_raw(v, next_seen, _depth + 1) for v in x]
    if isinstance(x, (set, frozenset)):
        return [_raw(v, next_seen, _depth + 1) for v in sorted(x, key=_safe_str)]
    return _safe_str(x)


def _scrub(x: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(x, str):
        if not secrets:
            return x
        for s in secrets:
            if s in x:
                x = x.replace(s, "[REDACTED]")
        return x
    if isinstance(x, dict):
        out: dict[str, Any] = {}
        for k, v in x.items():
            ks = _scrub(_safe_str(k), secrets)
            if ks.strip().lower() in _REDACTED_HEADER_KEYS and isinstance(v, str) and v.strip():
                out[ks] = "[REDACTED]"
            else:
                out[ks] = _scrub(v, secrets)
        return out
    if isinstance(x, (list, tuple)):
        return [_scrub(v, secrets) for v in x]
    return x


def record(
    feature: str,
    model: str,
    kind: str = "generate",
    usage: Any = None,
    latency_s: float = 0.0,
    ok: bool = True,
    error: str = "",
    run_id: str = "",
    meta: dict | None = None,
) -> None:
    if not LEDGER_DIR:
        return
    try:
        ledger_dir = Path(LEDGER_DIR)
    except (TypeError, ValueError):
        return
    secrets = _secrets()
    clean_meta: dict[str, Any] = {}
    if isinstance(meta, dict):
        scrubbed_meta = _scrub(_raw(meta), secrets)
        if isinstance(scrubbed_meta, dict):
            clean_meta = scrubbed_meta
    clean_err = ""
    if error is not None and not isinstance(error, bool):
        clean_err = _scrub(_safe_str(error), secrets)[:200]
    host = _safe_host()
    pid = os.getpid()
    row = {
        "ts": round(time.time(), 3),
        "feature": _scrub(_clean_token(feature), secrets),
        "model": _scrub(_clean_token(model), secrets),
        "kind": _scrub(_clean_token(kind, default="generate"), secrets),
        "in": _n(usage, "prompt_token_count"),
        "out": _n(usage, "candidates_token_count"),
        "thinking": _n(usage, "thoughts_token_count"),
        "cached": _n(usage, "cached_content_token_count"),
        "latency_s": _safe_latency(latency_s),
        "ok": bool(ok),
        "error": clean_err,
        "run_id": _scrub(_clean_token(run_id), secrets),
        "host": host,
        "pid": pid,
        "meta": clean_meta,
    }
    try:
        line = json.dumps(row, allow_nan=False) + "\n"
        with _ledger_lock:
            ledger_dir.mkdir(parents=True, exist_ok=True)
            path = ledger_dir / f"{host}-{pid}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, TypeError, ValueError):
        pass


def _trace(row: dict) -> None:
    """Raw request/response log, one JSON line per attempt, when AIMO_GEMINI_TRACE=<file> is set (evidence runs).
    Off by default. It records prompts and model output, never the API key."""
    if not isinstance(row, dict):
        return
    raw_path = os.environ.get("AIMO_GEMINI_TRACE", "")
    if not isinstance(raw_path, str):
        return
    path = raw_path.replace("\x00", "").strip()
    if not path:
        return
    try:
        secrets = _secrets()
        clean = _scrub(_raw(row), secrets)
        if not isinstance(clean, dict):
            return
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", **clean}
        line = json.dumps(out, default=_safe_str, ensure_ascii=False, allow_nan=False) + "\n"
        with _trace_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def fallback_chain(model: str) -> list[str]:
    """Models tried in order: the requested one, then GEMINI_FALLBACK_MODELS (comma separated)."""
    default_primary = _clean_model_name(getattr(_config, "GEMINI_AGENT_MODEL", ""), "gemini-3.8-flash")
    primary = _clean_model_name(model, default_primary)
    raw = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-4-hydrogen")
    if not isinstance(raw, str):
        raw = "gemini-3.5-flash,gemini-4-hydrogen"
    items: list[str] = []
    for part in raw.split(","):
        cleaned = _clean_model_name(part, "")
        if cleaned:
            items.append(cleaned)
    chain = list(dict.fromkeys(items))
    return [primary] + [m for m in chain if m != primary]


def last_model(default: str | None = None) -> str | None:
    """The model that actually answered the last generate() call on this thread (after any fallback)."""
    val = getattr(_tl, "model", None)
    return val if isinstance(val, str) and val else default


def _is_quota(msg: str) -> bool:
    if not isinstance(msg, str):
        return False
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg.upper()


def _is_daily_quota(msg: str) -> bool:
    if not isinstance(msg, str):
        return False
    low = msg.lower()
    return _is_quota(msg) and ("per_day" in low or "perday" in low or "per day" in low or "daily" in low)


def _select_candidates(chain: list[str], now: float) -> list[str]:
    primary = chain[0]
    with _exhausted_lock:
        for k, exp in list(_exhausted.items()):
            if not isinstance(exp, (int, float)) or not math.isfinite(exp) or exp <= now:
                _exhausted.pop(k, None)
        active = [m for m in chain if m not in _exhausted or _exhausted[m] <= now]
    return active or [primary]


def _mark_exhausted(model_name: str, until_ts: float) -> None:
    with _exhausted_lock:
        _exhausted[model_name] = until_ts


def _is_retryable_error(exc: BaseException, msg: str) -> bool:
    err_name = type(exc).__name__
    if any(s in err_name for s in ("Timeout", "ConnectError", "RemoteProtocolError", "ConnectionError", "ReadError", "WriteError")):
        return True
    if _is_quota(msg):
        return True
    up = msg.upper()
    if any(tok in up for tok in ("INVALID_ARGUMENT", "PERMISSION_DENIED", "UNAUTHENTICATED", "NOT_FOUND")):
        return False
    return any(s in msg for s in ("500", "502", "503", "504")) or any(
        s in up for s in ("UNAVAILABLE", "DEADLINE", "TIMED OUT")
    )


def generate(
    feature: str,
    model: str,
    contents: Any,
    config: Any = None,
    run_id: str = "",
    retries: int = 5,
    meta: dict | None = None,
) -> Any:
    """generate_content with exponential backoff on 429/5xx, model fallback and ledger logging.
    `meta` (e.g. {"thinking_level": "LOW", "step": 3}) is copied into the ledger row.

    Fallback: when a model's DAILY quota is exhausted (waiting cannot help), or it is rate-limited twice in a
    row, or its retries run out, the next model in fallback_chain() answers instead. An exhausted model is
    skipped for 30 minutes. The ledger row names the model that answered, with meta.fallback_from."""
    _tl.model = None
    if retries is None:
        max_retries = 5
    elif isinstance(retries, bool) or not isinstance(retries, int):
        raise ValueError("retries must be an integer")
    elif retries <= 0:
        raise RuntimeError("generate called with zero retries")
    else:
        max_retries = min(retries, 20)

    chain = fallback_chain(model)
    primary = chain[0]
    candidates = _select_candidates(chain, time.time())
    base_meta = dict(meta) if isinstance(meta, dict) else None

    last: Exception | None = None
    for idx, m in enumerate(candidates):
        more = idx < len(candidates) - 1
        if m != primary:
            row_meta: dict[str, Any] | None = {**(base_meta or {}), "fallback_from": primary}
        else:
            row_meta = dict(base_meta) if base_meta is not None else None
        for attempt in range(max_retries):
            t0 = time.time()
            try:
                r = client().models.generate_content(model=m, contents=contents, config=config)
                usage = getattr(r, "usage_metadata", None)
                elapsed = max(0.0, time.time() - t0)
                record(feature, m, usage=usage, latency_s=elapsed, run_id=run_id, meta=row_meta)
                _trace(
                    {
                        "feature": feature,
                        "run_id": run_id,
                        "requested_model": primary,
                        "model": m,
                        "attempt": attempt,
                        "ok": True,
                        "latency_s": _safe_latency(elapsed),
                        "meta": row_meta,
                        "usage": _raw(usage),
                        "request": {"contents": _raw(contents), "config": _raw(config)},
                        "response": _raw(r),
                    }
                )
                _tl.model = m
                return r
            except Exception as e:
                last = e
                elapsed = max(0.0, time.time() - t0)
                record(
                    feature,
                    m,
                    latency_s=elapsed,
                    ok=False,
                    error=type(e).__name__,
                    run_id=run_id,
                    meta=row_meta,
                )
                msg = _safe_str(e)
                _trace(
                    {
                        "feature": feature,
                        "run_id": run_id,
                        "requested_model": primary,
                        "model": m,
                        "attempt": attempt,
                        "ok": False,
                        "latency_s": _safe_latency(elapsed),
                        "meta": row_meta,
                        "error": f"{type(e).__name__}: {msg[:600]}",
                    }
                )
                if _is_daily_quota(msg):
                    _mark_exhausted(m, time.time() + EXHAUSTED_S)
                    break
                if not _is_retryable_error(e, msg):
                    raise
                if _is_quota(msg) and attempt >= 1 and more:
                    break
                time.sleep(min(60, (2**attempt) + random.random()))
    if last is not None:
        raise last
    raise RuntimeError("generate called with zero retries")


def _normalize_embed_texts(texts: Any) -> list[str]:
    if texts is None or isinstance(texts, (bool, int, float, dict)):
        return []
    if isinstance(texts, (bytes, bytearray)):
        decoded = bytes(texts).decode("utf-8", errors="replace")
        return [decoded] if decoded.strip() else []
    if isinstance(texts, str):
        return [texts] if texts.strip() else []
    try:
        iterator = iter(texts)
    except Exception:
        return []
    out: list[str] = []
    try:
        for item in iterator:
            if item is None or isinstance(item, bool):
                continue
            if isinstance(item, (bytes, bytearray)):
                out.append(bytes(item).decode("utf-8", errors="replace"))
            elif isinstance(item, str):
                out.append(item)
            else:
                out.append(_safe_str(item))
    except Exception:
        return []
    return out


def _coerce_vector(vec: Any) -> list[float]:
    if vec is None or isinstance(vec, (str, bytes, bytearray, dict)):
        return []
    try:
        iterator = iter(vec)
        raw_items = list(iterator)
    except Exception:
        return []
    out: list[float] = []
    for item in raw_items:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            out.append(0.0)
            continue
        try:
            f = float(item)
        except OverflowError:
            out.append(0.0)
            continue
        out.append(f if math.isfinite(f) else 0.0)
    return out


def embed(feature: str, texts: list[str], model: str | None = None) -> list[list[float]]:
    from aimo.config import GEMINI_EMBED_MODEL

    clean_texts = _normalize_embed_texts(texts)
    if not clean_texts:
        return []
    default_embed = _clean_model_name(GEMINI_EMBED_MODEL, "gemini-embedding-2")
    resolved_model = _clean_model_name(model, default_embed)
    t0 = time.time()
    try:
        r = client().models.embed_content(model=resolved_model, contents=clean_texts)
        elapsed = max(0.0, time.time() - t0)
        record(feature, resolved_model, kind="embed", latency_s=elapsed, meta={"n": len(clean_texts)})
        _trace(
            {
                "feature": feature,
                "run_id": "",
                "requested_model": resolved_model,
                "model": resolved_model,
                "attempt": 0,
                "ok": True,
                "latency_s": _safe_latency(elapsed),
                "meta": {"n": len(clean_texts)},
                "request": {"contents_count": len(clean_texts)},
            }
        )
    except Exception as e:
        elapsed = max(0.0, time.time() - t0)
        record(
            feature,
            resolved_model,
            kind="embed",
            latency_s=elapsed,
            ok=False,
            error=type(e).__name__,
            meta={"n": len(clean_texts)},
        )
        msg = _safe_str(e)
        _trace(
            {
                "feature": feature,
                "run_id": "",
                "requested_model": resolved_model,
                "model": resolved_model,
                "attempt": 0,
                "ok": False,
                "latency_s": _safe_latency(elapsed),
                "meta": {"n": len(clean_texts)},
                "error": f"{type(e).__name__}: {msg[:600]}",
            }
        )
        raise
    embeddings = getattr(r, "embeddings", None)
    if embeddings is None or isinstance(embeddings, (str, bytes, dict)):
        return []
    try:
        emb_list = list(embeddings)
    except Exception:
        return []
    return [_coerce_vector(getattr(e, "values", None)) for e in emb_list]
