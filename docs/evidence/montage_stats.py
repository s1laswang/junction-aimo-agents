"""Recompute the montage Gemini numbers (cost per order, tokens, cache share, escalation) from the committed,
sanitized ledgers in this directory. Offline, no keys, no network:

    uv run python docs/evidence/montage_stats.py

Uses the same cost formula as `uv run aimo demo --montage` (aimo/buyer/costs.py: playbook prices, cached input
at 10% of the input price, thinking billed as output). 5 of 6 montage scenarios place an order (drift is held for
human confirmation by design), so cost per order = total cost / 5.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aimo.buyer.costs import row_cost, summarize  # noqa: E402

HERE = Path(__file__).resolve().parent
ORDERS_PLACED = 5


def load(label: str) -> list[dict]:
    return [json.loads(line) for line in open(HERE / f"montage-ledger-{label}.jsonl") if line.strip()]


def main() -> None:
    print("| Run | Gemini calls | Input tokens | Input per run | Cached share | Hero input | Total cost | "
          "Cost per order | Escalated steps |")
    print("|---|---|---|---|---|---|---|---|---|")
    for label in ("before-trim", "after-trim"):
        rows = load(label)
        s = summarize(rows)
        hero = sum(r["in"] for r in rows if r["scenario"] == "happy")
        print(f"| {label} | {s['calls']} | {s['in']:,} | {s['in'] / s['runs']:,.0f} | {s['cached'] / s['in']:.0%} | "
              f"{hero:,} | ${s['cost_usd']:.4f} | ${s['cost_usd'] / ORDERS_PLACED:.4f} | "
              f"{s['escalated_runs']}/{s['runs']} runs, {s['escalated_steps']}/{s['agent_steps']} steps "
              f"({s['escalated_steps'] / s['agent_steps']:.0%}) |")
    print()
    print("After trim, per scenario:")
    per: dict[str, list[dict]] = defaultdict(list)
    for r in load("after-trim"):
        per[r["scenario"]].append(r)
    for name, rows in per.items():
        cost = sum(row_cost(r) or 0 for r in rows)
        print(f"  {name:16s} calls={len(rows):2d} in={sum(r['in'] for r in rows):6,d} "
              f"cached={sum(r['cached'] for r in rows):6,d} out={sum(r['out'] for r in rows):5,d} "
              f"thinking={sum(r['thinking'] for r in rows):4d} cost=${cost:.4f}")


if __name__ == "__main__":
    main()
