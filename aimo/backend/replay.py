"""Replay mode: serve /api/price and /api/availability from a recorded snapshot. Owner: gateway.

Reuses the wind tunnel snapshot layout (windtunnel/snapshot.py on team/windtunnel):
  <dir>/price_<YYYY-MM-DD>.json         raw /api/price response for the pool ids
  <dir>/availability_<YYYY-MM-DD>.json  raw /api/availability response for the pool ids
  <dir>/meta.json                       {"time_now": <server timeNow at recording>, "dates": [...], ...}

  AIMO_REPLAY=<dir>            replay only (no network): the full demo runs without Wi-Fi
  AIMO_REPLAY=1                replay from the bundled snapshot (tests/fixtures/snapshot/replay, committed)
  no AIMO_API_KEY              replay from the bundled snapshot automatically (keyless clone = working demo);
                               AIMO_REPLAY=0 or AIMO_OFFLINE=1 (tests) turn the automatic replay off
  (unset)                      live; if the upstream is unreachable (after retries / circuit open) the gateway
                               falls back to the snapshot at data/windtunnel/snapshot (or AIMO_REPLAY_FALLBACK=<dir>;
                               AIMO_REPLAY_FALLBACK=0 disables). Provenance says served_from: "replay".
Snapshot data is real recorded mock-API output; dates that were not recorded are refused, never invented.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
import zlib
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from aimo import config


BUNDLED_DIR = config.ROOT / "tests" / "fixtures" / "snapshot" / "replay"
_ON = ("1", "true", "yes", "on", "auto", "bundled")


def _keyless() -> bool:
    return not (os.environ.get("AIMO_API_KEY", "").strip() or config.AIMO_API_KEY)


def mode() -> str:
    """'forced' (AIMO_REPLAY set), 'auto' (no AIMO_API_KEY, bundled snapshot) or 'off'."""
    v = os.environ.get("AIMO_REPLAY", "").strip()
    if v == "0":
        return "off"
    if v:
        return "forced"
    if _keyless() and not config.OFFLINE and os.environ.get("AIMO_OFFLINE", "0") != "1":
        return "auto"
    return "off"


def replay_dir() -> Path | None:
    v = os.environ.get("AIMO_REPLAY", "").strip()
    m = mode()
    if m == "off":
        return None
    if m == "auto" or v.lower() in _ON:
        try:
            return BUNDLED_DIR if (BUNDLED_DIR / "meta.json").is_file() else None
        except OSError:
            return None
    try:
        p = Path(v).expanduser()
        return p if p.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def fallback_dir() -> Path | None:
    v = os.environ.get("AIMO_REPLAY_FALLBACK", "").strip()
    if v == "0":
        return None
    ext_path: Path | None = None
    if v:
        try:
            ext_path = Path(v).expanduser()
        except (OSError, RuntimeError, ValueError):
            ext_path = None
    for p in (replay_dir(), ext_path, config.DATA_DIR / "windtunnel" / "snapshot", BUNDLED_DIR):
        if p is None:
            continue
        try:
            if p.is_dir() and (p / "meta.json").is_file():
                return p
        except (OSError, ValueError):
            continue
    return None


def _reject_json_constant(val: str) -> Any:
    raise ValueError(f"invalid JSON constant: {val}")


@lru_cache(maxsize=8)
def _meta(path: str, mtime: float, size: int = -1) -> dict:
    try:
        raw_text = (Path(path) / "meta.json").read_text(encoding="utf-8")
        data = json.loads(raw_text, parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_path(d: Any) -> Path | None:
    if d is None or isinstance(d, bool) or not isinstance(d, (str, os.PathLike)):
        return None
    try:
        raw = os.fspath(d)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not raw.strip() or "\x00" in raw:
        return None
    try:
        return Path(raw)
    except (TypeError, ValueError, OSError):
        return None


def meta(d: Path) -> dict:
    base = _clean_path(d)
    if base is None:
        return {}
    try:
        p = base / "meta.json"
        st = p.stat() if p.is_file() else None
    except OSError:
        return {}
    if st is None:
        return {}
    return _meta(str(base), st.st_mtime, st.st_size)


def snapshot_time_now(d: Path) -> datetime | None:
    from aimo.backend.clock import parse

    tn = meta(d).get("time_now")
    return parse(tn) if isinstance(tn, str) and tn.strip() else None


def _is_iso_date(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        return date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


class ReplayClient:
    """BackendClient over a snapshot directory. Same return shape as ValioClient."""

    ttl_s = 0

    def __init__(self, directory: Path):
        if directory is None or isinstance(directory, bool) or not isinstance(directory, (str, os.PathLike)):
            raise ValueError("directory must be a path")
        raw_dir = os.fspath(directory)
        if isinstance(raw_dir, bytes):
            raw_dir = raw_dir.decode("utf-8", errors="strict")
        if not raw_dir.strip() or "\x00" in raw_dir:
            raise ValueError("directory must be a non-empty path")
        self.dir = Path(raw_dir)
        self.base_url = f"replay://{self.dir}"
        self.stats = {"upstream_calls": 0, "cache_hits": 0, "retries": 0, "replay_calls": 0}
        self._files: dict[tuple[str, str], tuple[dict, dict[str, dict]]] = {}
        self._lock = threading.Lock()

    def dates(self) -> list[str]:
        found: set[str] = set()
        try:
            for p in self.dir.glob("availability_*.json*"):
                try:
                    if not p.is_file():
                        continue
                except OSError:
                    continue
                name = p.name
                if not name.startswith("availability_"):
                    continue
                if name.endswith(".json.gz"):
                    mid = name[len("availability_"):-len(".json.gz")]
                elif name.endswith(".json"):
                    mid = name[len("availability_"):-len(".json")]
                else:
                    continue
                if _is_iso_date(mid):
                    found.add(mid)
        except OSError:
            return []
        return sorted(found)

    def _load(self, endpoint: str, d: str) -> tuple[dict, dict[str, dict]]:
        if endpoint not in ("price", "availability"):
            raise ValueError(f"invalid replay endpoint: {endpoint!r}")
        if not _is_iso_date(d):
            raise ValueError("requested_date must be an ISO date YYYY-MM-DD")
        key = (endpoint, d)
        with self._lock:
            if key not in self._files:
                from aimo.backend.client import UpstreamError

                p = self.dir / f"{endpoint}_{d}.json"
                try:
                    if not p.exists() and p.with_suffix(".json.gz").exists():
                        p = p.with_suffix(".json.gz")
                    exists = p.exists()
                except OSError as exc:
                    raise UpstreamError(f"replay snapshot {p.name} is unreadable or corrupt", 503) from exc
                if not exists:
                    raise UpstreamError(f"replay snapshot has no {endpoint} data for {d} (recorded dates: "
                                        f"{', '.join(self.dates()) or 'none'})", 503)
                try:
                    text = (
                        gzip.decompress(p.read_bytes()).decode("utf-8")
                        if p.suffix == ".gz"
                        else p.read_text(encoding="utf-8")
                    )
                    raw = json.loads(text, parse_constant=_reject_json_constant)
                except (OSError, EOFError, zlib.error, UnicodeDecodeError, ValueError) as exc:
                    raise UpstreamError(f"replay snapshot {p.name} is unreadable or corrupt", 503) from exc
                if not isinstance(raw, dict):
                    raise UpstreamError(f"replay snapshot {p.name} is unreadable or corrupt", 503)
                prods = raw.get("products")
                by_id: dict[str, dict] = {}
                if isinstance(prods, list):
                    for x in prods:
                        if not isinstance(x, dict):
                            continue
                        raw_pid = x.get("productId")
                        if raw_pid is None or isinstance(raw_pid, bool) or not isinstance(raw_pid, (str, int)):
                            continue
                        pid = str(raw_pid).strip()
                        if not pid:
                            continue
                        by_id[pid] = x
                head = {k: v for k, v in raw.items() if k != "products"}
                self._files[key] = (head, by_id)
            return self._files[key]

    def _serve(self, endpoint: str, ids: list[str], requested_date: date) -> dict:
        if not isinstance(requested_date, date) or isinstance(requested_date, datetime):
            raise ValueError("requested_date must be a date")
        if isinstance(ids, (str, bytes, bytearray, dict, bool)):
            raise ValueError("product_ids must be an iterable of product IDs")
        try:
            raw_ids = list(ids)
        except TypeError as exc:
            raise ValueError("product_ids must be an iterable of product IDs") from exc
        norm_ids: list[str] = []
        for x in raw_ids:
            if x is None or isinstance(x, bool) or not isinstance(x, (str, int)):
                continue
            s = str(x).strip()
            if s:
                norm_ids.append(s)
        head, by_id = self._load(endpoint, requested_date.isoformat())
        self.stats["replay_calls"] += 1
        now = time.time()
        m = dict(head)
        snap_meta = meta(self.dir)
        tn = snap_meta.get("time_now")
        if endpoint == "availability" and "timeNow" not in m and isinstance(tn, str) and tn.strip():
            m["timeNow"] = tn.strip()
        products = [{**by_id[i], "_fetched_at": now, "_meta": m} for i in dict.fromkeys(norm_ids) if i in by_id]
        return {**m, "products": products, "_oldest_fetch": now, "_replay": True,
                "_recorded_at": tn.strip() if isinstance(tn, str) and tn.strip() else None}

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
    if d is not None:
        target = _clean_path(d)
    else:
        target = replay_dir() or fallback_dir()
    if target is None:
        return {"available": False}
    m = meta(target)
    tn = m.get("time_now")
    n_ids = m.get("n_ids")
    rdir = replay_dir()
    return {"available": True, "dir": str(target), "time_now": tn if isinstance(tn, str) else None,
            "dates": ReplayClient(target).dates(),
            "n_ids": n_ids if isinstance(n_ids, int) and not isinstance(n_ids, bool) else None,
            "forced": rdir is not None, "mode": mode() if rdir is not None else "fallback"}
