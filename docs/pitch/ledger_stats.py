"""Summarise Gemini usage from the ledger (data/gemini/ledger/*.jsonl) for docs/GEMINI.md.

    uv run python docs/pitch/ledger_stats.py                 # markdown tables
    uv run python docs/pitch/ledger_stats.py --json          # machine-readable
    uv run python docs/pitch/ledger_stats.py --dir a/ledger --dir b/ledger   # several ledgers (deduplicated)

Ledger row schema (aimo/gemini.py): {ts, feature, model, kind, in, out, thinking, cached, latency_s,
ok, error, run_id, host, pid, meta}. Embedding rows carry no token counts (the embed API returns none);
their text count is meta.n. The ledger never contains prompts or keys.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(dirs: list[str]) -> tuple[list[dict], int]:
    rows, bad, seen_files, seen_rows = [], 0, set(), set()
    paths = [p for d in dirs for p in sorted(glob.glob(os.path.join(d, "*.jsonl")))]
    for p in paths:
        real = os.path.realpath(p)
        if real in seen_files:  # symlinked data dirs point at the same ledger
            continue
        seen_files.add(real)
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line in seen_rows:
                    continue
                seen_rows.add(line)
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
    return rows, bad


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarise(rows: list[dict], key) -> list[dict]:
    g: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    out = []
    for k, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        lat = [r.get("latency_s", 0.0) for r in rs if r.get("ok")]
        out.append({
            "key": k, "calls": len(rs), "ok": sum(1 for r in rs if r.get("ok")),
            "errors": sum(1 for r in rs if not r.get("ok")),
            "in": sum(r.get("in", 0) for r in rs), "out": sum(r.get("out", 0) for r in rs),
            "thinking": sum(r.get("thinking", 0) for r in rs), "cached": sum(r.get("cached", 0) for r in rs),
            "texts_embedded": sum((r.get("meta") or {}).get("n", 0) for r in rs if r.get("kind") == "embed" and r.get("ok")),
            "lat_p50": round(statistics.median(lat), 2) if lat else 0.0,
            "lat_p95": round(pct(lat, 0.95), 2), "lat_mean": round(statistics.fmean(lat), 2) if lat else 0.0,
        })
    return out


def fmt_table(title: str, cols: list[str], groups: list[dict]) -> str:
    head = cols + ["calls", "ok", "err", "in tok", "out tok", "think tok", "texts embedded", "p50 s", "p95 s"]
    lines = [f"### {title}", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for s in groups:
        k = s["key"] if isinstance(s["key"], tuple) else (s["key"],)
        vals = [f"`{x}`" for x in k] + [s["calls"], s["ok"], s["errors"], f"{s['in']:,}", f"{s['out']:,}",
                                        f"{s['thinking']:,}", f"{s['texts_embedded']:,}", s["lat_p50"], s["lat_p95"]]
        lines.append("| " + " | ".join(str(v) for v in vals) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", help="ledger directory (repeatable); default data/gemini/ledger")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    dirs = a.dir or [os.path.join(ROOT, "data", "gemini", "ledger")]
    rows, bad = load(dirs)
    if not rows:
        raise SystemExit(f"no ledger rows in {dirs}")
    ts = [r["ts"] for r in rows if "ts" in r]
    span = (datetime.fromtimestamp(min(ts), timezone.utc), datetime.fromtimestamp(max(ts), timezone.utc))
    total = summarise(rows, lambda r: "all")[0]
    by_feature = summarise(rows, lambda r: (r.get("feature", "?"), r.get("model", "?")))
    by_model = summarise(rows, lambda r: r.get("model", "?"))
    errors = defaultdict(int)
    for r in rows:
        if not r.get("ok"):
            errors[(r.get("feature"), r.get("error") or "?")] += 1
    if a.json:
        print(json.dumps({"rows": len(rows), "unparseable": bad, "from": span[0].isoformat(), "to": span[1].isoformat(),
                          "total": total, "by_feature_model": by_feature, "by_model": by_model,
                          "errors": [{"feature": f, "error": e, "n": n} for (f, e), n in errors.items()]},
                         default=list, indent=2))
        return
    print(f"Ledgers: {len(dirs)} dir(s)  rows={len(rows)} unparseable={bad}  "
          f"span {span[0]:%Y-%m-%d %H:%M} to {span[1]:%Y-%m-%d %H:%M} UTC\n")
    print(f"**Total:** {total['calls']:,} calls ({total['ok']:,} ok, {total['errors']:,} errors), "
          f"{total['in']:,} input tokens, {total['out']:,} output tokens, {total['thinking']:,} thinking tokens, "
          f"{total['texts_embedded']:,} texts embedded.\n")
    print(fmt_table("By feature and model", ["feature", "model"], by_feature) + "\n")
    print(fmt_table("By model", ["model"], by_model) + "\n")
    if errors:
        print("### Errors\n\n| feature | error | n |\n|---|---|---|")
        for (f, e), n in sorted(errors.items(), key=lambda kv: -kv[1]):
            print(f"| `{f}` | {e} | {n} |")


if __name__ == "__main__":
    main()
