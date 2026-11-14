"""Gemini cost accounting for buyer runs, computed from the shared usage ledger (data/gemini/ledger).

Prices (USD per 1M tokens, input / output) are from the team Gemini playbook (section 1, listed
Sat 14 Nov): gemini-3.8-flash 0.75 / 3.75, gemini-4-hydrogen 1.5 / 7.5, gemini-4-carbon and argon
2 / 10. Thinking tokens are billed as output. Cached input (implicit caching) is billed at
CACHED_FRACTION of the input price; the playbook says "about 5-10%", we use 10% (the conservative
end). Ledger "in" (prompt_token_count) INCLUDES the cached tokens.
"""
from __future__ import annotations

import glob
import json
from typing import Any, Iterable

PRICES_PER_M = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-4-hydrogen": (1.5, 7.5),
    "gemini-4-carbon": (2.0, 10.0),
    "gemini-4-argon": (2.0, 10.0),
}
CACHED_FRACTION = 0.10


def ledger_rows(run_ids: Iterable[str] | None = None, pid: int | None = None,
                feature_prefix: str = "buyer.") -> list[dict[str, Any]]:
    from aimo.gemini import LEDGER_DIR
    ids = set(run_ids) if run_ids is not None else None
    out = []
    for f in glob.glob(str(LEDGER_DIR / "*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(r.get("feature", "")).startswith(feature_prefix):
                continue
            if ids is not None and r.get("run_id") not in ids:
                continue
            if pid is not None and r.get("pid") != pid:
                continue
            out.append(r)
    return out


def row_cost(r: dict[str, Any]) -> float | None:
    p = PRICES_PER_M.get(r.get("model", ""))
    if p is None:
        return None
    cached = r.get("cached", 0) or 0
    fresh = max(0, (r.get("in", 0) or 0) - cached)
    out = (r.get("out", 0) or 0) + (r.get("thinking", 0) or 0)
    return (fresh * p[0] + cached * p[0] * CACHED_FRACTION + out * p[1]) / 1e6


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    steps = [r for r in rows if r.get("feature") == "buyer.agent_step"]
    esc_steps = [r for r in steps if (r.get("meta") or {}).get("escalated") or r.get("model") != "gemini-3.8-flash"]
    runs = {r.get("run_id") for r in rows if r.get("run_id")}
    esc_runs = {r.get("run_id") for r in esc_steps}
    costs = [row_cost(r) for r in rows]
    return {
        "calls": len(rows), "runs": len(runs),
        "in": sum(r.get("in", 0) for r in rows), "out": sum(r.get("out", 0) for r in rows),
        "thinking": sum(r.get("thinking", 0) for r in rows), "cached": sum(r.get("cached", 0) for r in rows),
        "gemini_s": round(sum(r.get("latency_s", 0.0) for r in rows), 1),
        "cost_usd": sum(c for c in costs if c is not None), "unpriced_calls": sum(1 for c in costs if c is None),
        "agent_steps": len(steps), "escalated_steps": len(esc_steps), "escalated_runs": len(esc_runs),
        "failed": sum(1 for r in rows if not r.get("ok", True)),
    }
