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
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from aimo.config import DATA_DIR

LEDGER_DIR = DATA_DIR / "gemini" / "ledger"


@lru_cache(maxsize=1)
def client():
    from google import genai
    return genai.Client()  # reads GEMINI_API_KEY from env (.env loaded by aimo.config)


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


def generate(feature: str, model: str, contents: Any, config: Any = None, run_id: str = "",
             retries: int = 5) -> Any:
    """generate_content with exponential backoff on 429/5xx and ledger logging."""
    last: Exception | None = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            r = client().models.generate_content(model=model, contents=contents, config=config)
            record(feature, model, usage=r.usage_metadata, latency_s=time.time() - t0, run_id=run_id)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            record(feature, model, latency_s=time.time() - t0, ok=False, error=type(e).__name__, run_id=run_id)
            msg = str(e)
            if not any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "500", "503", "UNAVAILABLE", "DEADLINE")):
                raise
            time.sleep(min(60, (2 ** attempt) + random.random()))
    raise last  # type: ignore[misc]


def embed(feature: str, texts: list[str], model: str | None = None) -> list[list[float]]:
    from aimo.config import GEMINI_EMBED_MODEL
    model = model or GEMINI_EMBED_MODEL
    t0 = time.time()
    r = client().models.embed_content(model=model, contents=texts)
    record(feature, model, kind="embed", latency_s=time.time() - t0, meta={"n": len(texts)})
    return [e.values for e in r.embeddings]
