"""Injection screen eval: trust.screen_text regex-only vs regex+Gemini, on the payload library (positives) and
Gemini-written clean requests plus real supplier marketing texts (negatives).

  uv run python -m windtunnel.screen_eval --scenarios data/windtunnel/scenarios/wt640_gem.jsonl --neg 120
"""
from __future__ import annotations

import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor

from windtunnel import products as P
from windtunnel.config import RESULTS_DIR
from windtunnel.generate import INJECTIONS, REQUEST_INJECTIONS, load


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--neg", type=int, default=120)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    from aimo.trust import screen_text
    rng = random.Random(3)
    clean_req = [s.request_text for s in load(a.scenarios) if not s.expected.injection and s.generator.get("surface") == "ok"]
    mk = [p.marketing for p in P.catalog().values() if p.marketing and len(p.marketing) > 80]
    rng.shuffle(clean_req)
    rng.shuffle(mk)
    pos = [("payload", t) for t in INJECTIONS + REQUEST_INJECTIONS]
    pos += [("embedded", m[:300] + " " + t) for m, t in zip(mk[-len(INJECTIONS):], INJECTIONS)]
    neg = [("request", t) for t in clean_req[:a.neg // 2]] + [("marketing", t) for t in mk[:a.neg // 2]]

    def run(item):
        kind, text = item
        r0 = screen_text(text, use_gemini=False)
        r1 = screen_text(text, use_gemini=True)
        return {"kind": kind, "regex": bool(r0.get("suspicious")), "gemini": bool(r1.get("suspicious")),
                "layers": r1.get("layers"), "text": text[:160]}

    with ThreadPoolExecutor(max_workers=min(a.workers, 16)) as ex:
        P_ = list(ex.map(run, pos))
        N_ = list(ex.map(run, neg))
    out = {}
    for layer in ("regex", "gemini"):
        tp = sum(r[layer] for r in P_)
        fp = sum(r[layer] for r in N_)
        out[layer] = {"recall": tp / len(P_), "false_positive_rate": fp / len(N_), "positives": len(P_),
                      "negatives": len(N_), "fp_by_kind": {k: sum(r[layer] for r in N_ if r["kind"] == k) for k in ("request", "marketing")}}
    out["gemini_layer_used"] = sum("gemini" in (r["layers"] or []) for r in P_ + N_)
    out["missed_by_both"] = [r["text"] for r in P_ if not r["regex"] and not r["gemini"]]
    (RESULTS_DIR / "screen_eval.json").write_text(json.dumps({**out, "rows": P_ + N_}, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
