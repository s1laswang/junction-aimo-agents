"""Replay mode: serve /api/price and /api/availability from a recorded snapshot. Owner: gateway.

Reuses the wind tunnel snapshot layout (windtunnel/snapshot.py on team/windtunnel):
  <dir>/price_<YYYY-MM-DD>.json         raw /api/price response for the pool ids
  <dir>/availability_<YYYY-MM-DD>.json  raw /api/availability response for the pool ids
  <dir>/meta.json                       {"time_now": <server timeNow at recording>, "dates": [...], ...}

  AIMO_REPLAY=<dir>            replay only (no network): the full demo runs without Wi-Fi
  (unset)                      live; if the upstream is unreachable (after retries / circuit open) the gateway
                               falls back to the snapshot at data/windtunnel/snapshot (or AIMO_REPLAY_FALLBACK=<dir>;
                               AIMO_REPLAY_FALLBACK=0 disables). Provenance says served_from: "replay".
Snapshot data is real recorded mock-API output; dates that were not recorded are refused, never invented.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from aimo import config


def replay_dir() -> Path | None:
    v = os.environ.get("AIMO_REPLAY", "").strip()
    if not v or v == "0":
        return None
    p = Path(v).expanduser()
    return p if p.is_dir() else None


def fallback_dir() -> Path | None:
    v = os.environ.get("AIMO_REPLAY_FALLBACK", "").strip()
    if v == "0":
        return None
    for p in (replay_dir(), Path(v).expanduser() if v else None, config.DATA_DIR / "windtunnel" / "snapshot"):
        if p is not None and p.is_dir() and (p / "meta.json").exists():
            return p
    return None


@lru_cache(maxsize=8)
def _meta(path: str, mtime: float) -> dict:
    try:
        return json.loads((Path(path) / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def meta(d: Path) -> dict:
    p = d / "meta.json"
    return _meta(str(d), p.stat().st_mtime if p.exists() else 0.0)


def snapshot_time_now(d: Path) -> datetime | None:
    from aimo.backend.clock import parse

    return parse(meta(d).get("time_now"))


class ReplayClient:
    """BackendClient over a snapshot directory. Same return shape as ValioClient."""

    ttl_s = 0

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.base_url = f"replay://{self.dir}"
        self.stats = {"upstream_calls": 0, "cache_hits": 0, "retries": 0, "replay_calls": 0}
        self._files: dict[tuple[str, str], tuple[dict, dict[str, dict]]] = {}
        self._lock = threading.Lock()

    def dates(self) -> list[str]:
        return sorted({p.stem.split("_", 1)[1] for p in self.dir.glob("availability_*.json")})

    def _load(self, endpoint: str, d: str) -> tuple[dict, dict[str, dict]]:
        key = (endpoint, d)
        with self._lock:
            if key not in self._files:
                p = self.dir / f"{endpoint}_{d}.json"
                if not p.exists():
                    from aimo.backend.client import UpstreamError

                    raise UpstreamError(f"replay snapshot has no {endpoint} data for {d} (recorded dates: "
                                        f"{', '.join(self.dates()) or 'none'})", 503)
                raw = json.loads(p.read_text())
                head = {k: v for k, v in raw.items() if k != "products"}
                self._files[key] = (head, {str(x.get("productId")): x for x in raw.get("products", []) or []})
            return self._files[key]

    def _serve(self, endpoint: str, ids: list[str], requested_date: date) -> dict:
        head, by_id = self._load(endpoint, requested_date.isoformat())
        self.stats["replay_calls"] += 1
        now = time.time()
        m = dict(head)
        if endpoint == "availability" and "timeNow" not in m and meta(self.dir).get("time_now"):
            m["timeNow"] = meta(self.dir)["time_now"]
        products = [{**by_id[i], "_fetched_at": now, "_meta": m} for i in dict.fromkeys(str(x) for x in ids) if i in by_id]
        return {**m, "products": products, "_oldest_fetch": now, "_replay": True,
                "_recorded_at": meta(self.dir).get("time_now")}

    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None, fresh: bool = False) -> dict:
        return self._serve("price", product_ids, requested_date)

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None,
                     fresh: bool = False) -> dict:
        return self._serve("availability", product_ids, requested_date)


_fallback: dict[str, ReplayClient] = {}


def fallback_client() -> ReplayClient | None:
    d = fallback_dir()
    if d is None:
        return None
    return _fallback.setdefault(str(d), ReplayClient(d))


def info(d: Path | None = None) -> dict[str, Any]:
    d = d or fallback_dir()
    if d is None:
        return {"available": False}
    m = meta(d)
    return {"available": True, "dir": str(d), "time_now": m.get("time_now"), "dates": ReplayClient(d).dates(),
            "n_ids": m.get("n_ids"), "forced": replay_dir() is not None}
