"""Gemini cost accounting for buyer runs, computed from the shared usage ledger (data/gemini/ledger).

Prices (USD per 1M tokens, input / output) are from the team Gemini playbook (section 1, listed
Sat 14 Nov): gemini-3.8-flash 0.75 / 3.75, gemini-4-hydrogen 1.5 / 7.5, gemini-4-carbon and argon
2 / 10. Thinking tokens are billed as output. Cached input (implicit caching) is billed at
CACHED_FRACTION of the input price; the playbook says "about 5-10%", we use 10% (the conservative
end). Ledger "in" (prompt_token_count) INCLUDES the cached tokens.
"""
from __future__ import annotations

from collections.abc import Iterable
import glob
import json
import math
from pathlib import Path
from typing import Any

PRICES_PER_M = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-4-hydrogen": (1.5, 7.5),
    "gemini-4-carbon": (2.0, 10.0),
    "gemini-4-argon": (2.0, 10.0),
}
CACHED_FRACTION = 0.10


_MAX_INT_COERCE = 10**18


def _nonneg_num(v: Any) -> int | float:
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int):
        if v <= 0:
            return 0
        try:
            f = float(v)
        except OverflowError:
            return 0
        return v if math.isfinite(f) else 0
    if isinstance(v, float):
        if not math.isfinite(v) or v <= 0.0:
            return 0
        return int(v) if v <= _MAX_INT_COERCE and v.is_integer() else v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        try:
            x = float(s)
        except ValueError:
            return 0
        if not math.isfinite(x) or x <= 0.0:
            return 0
        return int(x) if x <= _MAX_INT_COERCE and x.is_integer() else x
    return 0


def _nonneg_float(v: Any) -> float:
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        try:
            x = float(v)
        except OverflowError:
            return 0.0
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        try:
            x = float(s)
        except ValueError:
            return 0.0
    else:
        return 0.0
    if not math.isfinite(x) or x <= 0.0:
        return 0.0
    return x


def _finite_sum(values: Iterable[int | float]) -> int | float:
    total: int | float = 0
    for val in values:
        total += val
    try:
        as_float = float(total)
    except OverflowError:
        return 0
    if not math.isfinite(as_float) or as_float <= 0.0:
        return 0
    if isinstance(total, float):
        return int(total) if total <= _MAX_INT_COERCE and total.is_integer() else total
    return total if total > 0 else 0


def _run_id(r: dict[str, Any]) -> str | None:
    rid = r.get("run_id")
    if isinstance(rid, str):
        s = rid.strip()
        return s if s else None
    if isinstance(rid, int) and not isinstance(rid, bool):
        return str(rid)
    return None


def _meta_escalated(r: dict[str, Any]) -> bool:
    meta = r.get("meta")
    return bool(meta.get("escalated")) if isinstance(meta, dict) else False


def ledger_rows(
    run_ids: Iterable[str] | None = None,
    pid: int | None = None,
    feature_prefix: str = "buyer.",
) -> list[dict[str, Any]]:
    from aimo.gemini import LEDGER_DIR

    if not LEDGER_DIR:
        return []
    try:
        ledger_path = Path(LEDGER_DIR)
    except TypeError:
        return []
    if not ledger_path.is_dir():
        return []

    ids: set[Any] | None = None
    if run_ids is not None:
        ids = set()
        if isinstance(run_ids, (str, bytes)):
            ids.add(run_ids)
        else:
            try:
                iterator = iter(run_ids)
            except TypeError:
                iterator = iter(())
            for item in iterator:
                try:
                    hash(item)
                except TypeError:
                    continue
                ids.add(item)

    prefix = feature_prefix if isinstance(feature_prefix, str) else str(feature_prefix or "")
    out: list[dict[str, Any]] = []
    for f in sorted(glob.glob(str(ledger_path / "*.jsonl"))):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(r, dict):
                        continue
                    feat = r.get("feature")
                    feat_str = feat if isinstance(feat, str) else ""
                    if not feat_str.startswith(prefix):
                        continue
                    if ids is not None:
                        rid = r.get("run_id")
                        try:
                            matched = rid in ids
                        except TypeError:
                            matched = False
                        if not matched:
                            continue
                    if pid is not None:
                        r_pid = r.get("pid")
                        if isinstance(r_pid, bool) or r_pid != pid:
                            continue
                    out.append(r)
        except OSError:
            continue
    return out


def row_cost(r: dict[str, Any]) -> float | None:
    if not isinstance(r, dict):
        return None
    model = r.get("model")
    if not isinstance(model, str):
        return None
    p = PRICES_PER_M.get(model)
    if p is None:
        return None
    in_tok = _nonneg_num(r.get("in"))
    cached = _nonneg_num(r.get("cached"))
    fresh = max(0, in_tok - cached)
    out = _nonneg_num(r.get("out")) + _nonneg_num(r.get("thinking"))
    try:
        cost = (fresh * p[0] + cached * p[0] * CACHED_FRACTION + out * p[1]) / 1e6
    except OverflowError:
        return 0.0
    if not math.isfinite(cost) or cost < 0.0:
        return 0.0
    return cost


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if rows is None or isinstance(rows, (str, bytes, dict)):
        valid_rows: list[dict[str, Any]] = []
    else:
        try:
            valid_rows = [r for r in rows if isinstance(r, dict)]
        except TypeError:
            valid_rows = []
    steps = [r for r in valid_rows if r.get("feature") == "buyer.agent_step"]
    esc_steps = [
        r for r in steps if _meta_escalated(r) or r.get("model") != "gemini-3.8-flash"
    ]
    runs = {rid for r in valid_rows if (rid := _run_id(r)) is not None}
    esc_runs = {rid for r in esc_steps if (rid := _run_id(r)) is not None}
    costs = [row_cost(r) for r in valid_rows]
    lat_sum = sum((_nonneg_float(r.get("latency_s")) for r in valid_rows), 0.0)
    gemini_s = round(lat_sum, 1) if math.isfinite(lat_sum) else 0.0
    priced = [c for c in costs if c is not None]
    cost_sum = sum(priced)
    cost_usd: int | float = (
        (cost_sum if math.isfinite(cost_sum) and cost_sum >= 0.0 else 0.0)
        if priced
        else 0
    )
    return {
        "calls": len(valid_rows),
        "runs": len(runs),
        "in": _finite_sum(_nonneg_num(r.get("in")) for r in valid_rows),
        "out": _finite_sum(_nonneg_num(r.get("out")) for r in valid_rows),
        "thinking": _finite_sum(_nonneg_num(r.get("thinking")) for r in valid_rows),
        "cached": _finite_sum(_nonneg_num(r.get("cached")) for r in valid_rows),
        "gemini_s": gemini_s,
        "cost_usd": cost_usd,
        "unpriced_calls": sum(1 for c in costs if c is None),
        "agent_steps": len(steps),
        "escalated_steps": len(esc_steps),
        "escalated_runs": len(esc_runs),
        "failed": sum(1 for r in valid_rows if not r.get("ok", True)),
    }
