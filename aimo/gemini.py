"""Thin Gemini wrapper: one shared client + usage ledger. Every Gemini call in the product goes
through here so we can report usage for the Google DeepMind submission. Owner: CEO (buyer may extend).

Ledger schema (shared cross-team): one JSONL row per call
{ts, feature, model, kind, in, out, thinking, cached, latency_s, ok, error, run_id, host, pid, meta}
Never records prompts or keys.
"""
from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from aimo.config import DATA_DIR

LEDGER_DIR = DATA_DIR / "gemini" / "ledger"


@lru_cache(maxsize=1)
def client():
    from google import genai
    return genai.Client()


def _n(u: Any, attr: str) -> int:
    v = getattr(u, attr, 0) if u is not None else 0
    return v if isinstance(v, int) else 0


def record(feature: str, model: str, kind: str = "generate", usage: Any = None, latency_s: float = 0.0,
           ok: bool = True, error: str = "", run_id: str = "", meta: dict | None = None) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": round(time.time(), 3), "feature": feature, "model": model, "kind": kind,
           "in": _n(usage, "prompt_token_count"), "out": _n(usage, "candidates_token_count"),
           "thinking": _n(usage, "thoughts_token_count"), "cached": _n(usage, "cached_content_token_count"),
           "latency_s": round(latency_s, 3), "ok": ok, "error": error[:200], "run_id": run_id,
           "host": socket.gethostname().split(".")[0], "pid": os.getpid(), "meta": meta or {}}
    path = LEDGER_DIR / f"{row['host']}-{row['pid']}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _raw(x: Any) -> Any:
    """JSON-safe copy of a google-genai request/response object for the raw trace."""
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if hasattr(x, "model_dump"):
        try:
            return x.model_dump(mode="json", exclude_none=True)
        except Exception:
            return str(x)
    if isinstance(x, dict):
        return {str(k): _raw(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_raw(v) for v in x]
    return str(x)


def _scrub(x: Any, secrets: tuple[str, ...]) -> Any:
    if not secrets:
        return x
    if isinstance(x, str):
        for s in secrets:
            if s in x:
                x = x.replace(s, "[REDACTED]")
        return x
    if isinstance(x, dict):
        return {_scrub(str(k), secrets): _scrub(v, secrets) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_scrub(v, secrets) for v in x]
    return x


def _trace(row: dict) -> None:
    """Raw request/response log, one JSON line per attempt, when AIMO_GEMINI_TRACE=<file> is set (evidence runs).
    Off by default. It records prompts and model output, never the API key."""
    path = os.environ.get("AIMO_GEMINI_TRACE", "").strip()
    if not path:
        return
    try:
        secrets = tuple(
            s for s in (
                os.environ.get("GEMINI_API_KEY", "").strip(),
                os.environ.get("GOOGLE_API_KEY", "").strip(),
            )
            if s
        )
        clean = _scrub(_raw(row), secrets)
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", **clean}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


_tl = threading.local()
_exhausted: dict[str, float] = {}
EXHAUSTED_S = 1800.0


def fallback_chain(model: str) -> list[str]:
    """Models tried in order: the requested one, then GEMINI_FALLBACK_MODELS (comma separated)."""
    raw = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-4-hydrogen")
    chain = list(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip()))
    return [model] + [m for m in chain if m != model]


def last_model(default: str | None = None) -> str | None:
    """The model that actually answered the last generate() call on this thread (after any fallback)."""
    return getattr(_tl, "model", None) or default


def _is_quota(msg: str) -> bool:
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _is_daily_quota(msg: str) -> bool:
    low = msg.lower()
    return _is_quota(msg) and ("per_day" in low or "perday" in low or "per day" in low)


def generate(feature: str, model: str, contents: Any, config: Any = None, run_id: str = "",
             retries: int = 5, meta: dict | None = None) -> Any:
    """generate_content with exponential backoff on 429/5xx, model fallback and ledger logging.
    `meta` (e.g. {"thinking_level": "LOW", "step": 3}) is copied into the ledger row.

    Fallback: when a model's DAILY quota is exhausted (waiting cannot help), or it is rate-limited twice in a
    row, or its retries run out, the next model in fallback_chain() answers instead. An exhausted model is
    skipped for 30 minutes. The ledger row names the model that answered, with meta.fallback_from."""
    _tl.model = None
    now = time.time()
    candidates = [m for m in fallback_chain(model) if _exhausted.get(m, 0.0) < now] or [model]
    last: Exception | None = None
    for idx, m in enumerate(candidates):
        more = idx < len(candidates) - 1
        row_meta = dict(meta or {}, fallback_from=model) if m != model else meta
        for attempt in range(retries):
            t0 = time.time()
            try:
                r = client().models.generate_content(model=m, contents=contents, config=config)
                record(feature, m, usage=r.usage_metadata, latency_s=time.time() - t0, run_id=run_id, meta=row_meta)
                _trace({"feature": feature, "run_id": run_id, "requested_model": model, "model": m, "attempt": attempt,
                        "ok": True, "latency_s": round(time.time() - t0, 3), "meta": row_meta,
                        "usage": _raw(getattr(r, "usage_metadata", None)),
                        "request": {"contents": _raw(contents), "config": _raw(config)}, "response": _raw(r)})
                _tl.model = m
                return r
            except Exception as e:
                last = e
                record(feature, m, latency_s=time.time() - t0, ok=False, error=type(e).__name__, run_id=run_id,
                       meta=row_meta)
                msg = str(e)
                _trace({"feature": feature, "run_id": run_id, "requested_model": model, "model": m, "attempt": attempt,
                        "ok": False, "latency_s": round(time.time() - t0, 3), "meta": row_meta,
                        "error": f"{type(e).__name__}: {msg[:600]}"})
                if _is_daily_quota(msg):
                    _exhausted[m] = time.time() + EXHAUSTED_S
                    break
                retryable_type = any(s in type(e).__name__ for s in ("Timeout", "ConnectError", "RemoteProtocolError"))
                if not retryable_type and not any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "500", "503",
                                                                     "UNAVAILABLE", "DEADLINE", "timed out")):
                    raise
                if _is_quota(msg) and attempt >= 1 and more:
                    break
                time.sleep(min(60, (2 ** attempt) + random.random()))
    if last is not None:
        raise last
    raise RuntimeError("generate called with zero retries")


def embed(feature: str, texts: list[str], model: str | None = None) -> list[list[float]]:
    from aimo.config import GEMINI_EMBED_MODEL
    model = model or GEMINI_EMBED_MODEL
    t0 = time.time()
    r = client().models.embed_content(model=model, contents=texts)
    record(feature, model, kind="embed", latency_s=time.time() - t0, meta={"n": len(texts)})
    return [e.values for e in r.embeddings]
