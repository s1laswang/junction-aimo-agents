"""Assemble docs/windtunnel-results.md and docs/windtunnel-results.json (small, committed, for UI and pitch) from
the result JSONs in data/windtunnel/results/.

  uv run python -m windtunnel.report --main wt640 --scale wt2560
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from windtunnel.config import RESULTS_DIR, RESULTS_MD, ROOT
from windtunnel.score import _row, pct


def _load(name: str) -> dict | None:
    p = RESULTS_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def svg_thinking(summary: list[dict], path: Path) -> None:
    """Tiny dependency-free SVG: accuracy vs p50 latency per config, one panel per task."""
    W, H, pad = 640, 260, 40
    colors = {"gemini-3.8-flash": "#2f6fdf", "gemini-4-hydrogen": "#d9480f"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="sans-serif" font-size="11">',
             '<rect width="100%" height="100%" fill="white"/>']
    tasks = ["parse", "substitute"]
    pw = (W - 3 * pad) / 2
    maxlat = max((s["p50_latency_s"] or 0) for s in summary) * 1.15 or 1
    for i, t in enumerate(tasks):
        x0 = pad + i * (pw + pad)
        parts.append(f'<text x="{x0}" y="16" font-weight="bold">{t}: accuracy vs p50 latency</text>')
        parts.append(f'<line x1="{x0}" y1="{H - pad}" x2="{x0 + pw}" y2="{H - pad}" stroke="#999"/>')
        parts.append(f'<line x1="{x0}" y1="{pad}" x2="{x0}" y2="{H - pad}" stroke="#999"/>')
        for a in (0.5, 0.75, 1.0):
            y = H - pad - (a - 0.4) / 0.6 * (H - 2 * pad)
            parts.append(f'<text x="{x0 - 30}" y="{y + 4}">{int(a * 100)}%</text>')
        parts.append(f'<text x="{x0 + pw - 60}" y="{H - pad + 16}">p50 s (max {maxlat:.0f})</text>')
        for s in summary:
            if s["task"] != t or s["accuracy"] is None:
                continue
            x = x0 + (s["p50_latency_s"] or 0) / maxlat * pw
            y = H - pad - (max(s["accuracy"], 0.4) - 0.4) / 0.6 * (H - 2 * pad)
            c = colors.get(s["model"], "#555")
            r = 5 if s["level"] == "LOW" else 8
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{c}" fill-opacity="0.75"/>')
            parts.append(f'<text x="{x + 9:.1f}" y="{y + 4:.1f}">{s["model"].replace("gemini-", "")} {s["level"]}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", default="wt640")
    ap.add_argument("--scale", default="")
    a = ap.parse_args(argv)
    main_r, scale_r = _load(a.main), _load(a.scale) if a.scale else None
    th, se = _load("thinking"), _load("screen_eval")
    L = ["# Wind tunnel results", "",
         f"Generated {datetime.now().isoformat(timespec='minutes')} by `windtunnel/report.py` (owner: windtunnel). "
         "Pipeline mode = the team's real catalog `plan_need` and trust `evaluate`/`screen_text`, run offline against a "
         "recorded snapshot of the Valio mock API with injected faults. The naive agent is the same pipeline with no "
         "gateway: keyword search, cheapest price, no allergen or policy checks, always orders.", ""]
    hdr = ["| System | N | Escalation precision | Escalation recall | Outcome accuracy | Policy violations | "
           "Allergen violations | Injection block | Screen false-pos | Plan coverage | Line acceptability | "
           "EUR over optimum (median) | Latency p50 / p95 ms |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for title, r in ((f"Gemini-written scenarios ({a.main})", main_r), (f"Scale run, template text ({a.scale})", scale_r)):
        if not r:
            continue
        sc = r["scenarios"]
        L += [f"## {title}", "", f"{sc['n']} scenarios, {sc['families']} families, {sc['customers']} policy archetypes, "
              f"snapshot `{sc['snapshot']}`, host {r['systems'][0]['meta'].get('host', '?')}, git "
              f"{r['systems'][0]['meta'].get('git', '?')}.", ""] + hdr
        for s in r["systems"]:
            L.append(_row(s["label"], s))
        team = r["systems"][0]
        L += ["", f"Policy-backed escalation recall (labels from the written policy only): "
                  f"{pct(team.get('policy_backed_recall'))}. Violation types: "
                  f"{', '.join(f'{k} x{v}' for k, v in team['policy_violation_types'].items()) or 'none'}.", ""]
        L += ["| Family | N | Correct | Must escalate | Escalated | Violations | Allergen viol. | Errors |",
              "|---|---|---|---|---|---|---|---|"]
        for f, c in team["by_family"].items():
            L.append(f"| {f} | {c['n']} | {c['ok']} | {c['must_escalate']} | {c['escalated']} | {c['violations']} | "
                     f"{c['allergen_violations']} | {c['errors']} |")
        L.append("")
    if se:
        L += ["## Injection screen: regex vs regex + Gemini", "",
              f"{se['regex']['positives']} payloads (library plus payloads embedded in real supplier texts) and "
              f"{se['regex']['negatives']} negatives (half Gemini-written buyer requests, half real supplier marketing "
              "texts). `windtunnel/screen_eval.py` calls `aimo.trust.screen_text`.", "",
              "| Layer | Recall | False-positive rate | FP on buyer requests | FP on supplier texts |", "|---|---|---|---|---|"]
        for layer in ("regex", "gemini"):
            x = se[layer]
            half = max(1, x["negatives"] // 2)
            L.append(f"| {layer} | {pct(x['recall'])} | {pct(x['false_positive_rate'])} | "
                     f"{x['fp_by_kind']['request']}/{half} | {x['fp_by_kind']['marketing']}/{half} |")
        L.append("")
    if th:
        svg = ROOT / "docs" / "windtunnel-thinking.svg"
        svg_thinking(th["summary"], svg)
        L += ["## Thinking level vs accuracy and latency (buyer tasks)", "",
              f"{th['n_scenarios']} Gemini-written scenarios. `parse` = request text to structured need (quantity, unit, "
              "date, allergen exclusions all correct). `substitute` = choose a compliant, available product within 10% "
              "of the cheapest compliant price, or escalate when none is compliant. `windtunnel/thinking.py`.", "",
              "| Task | Model | Thinking | Accuracy (errors = wrong) | Accuracy (answered) | p50 s | p95 s | "
              "Thinking tokens (mean) | USD per 1k calls | Infra errors |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        rows = th.get("rows") or []
        for s in th["summary"]:
            rs = [r for r in rows if r["task"] == s["task"] and r["model"] == s["model"] and r["level"] == s["level"]
                  and not r.get("error")]
            ans = sum(r["ok"]["all"] for r in rs) / len(rs) if rs else None
            L.append(f"| {s['task']} | {s['model']} | {s['level']} | {pct(s['accuracy'])} | {pct(ans)} | "
                     f"{(s['p50_latency_s'] or 0):.1f} | {(s['p95_latency_s'] or 0):.1f} | {s['thinking_tokens_mean']:.0f} | "
                     f"{s['cost_usd_per_1k']} | {s['errors']} |")
        L += ["", "![thinking vs accuracy](windtunnel-thinking.svg)", ""]
    ag, aj = _load("agent48"), _load("agent48_judge")
    if ag:
        s0 = ag["systems"][0]
        e = s0["escalation"]
        L += ["## Agent mode: Gemini buyer agent + real gateway", "",
              f"{s0['n']} scenarios (3 per family) through `POST /v1/agent/runs` on the real `aimo.gateway.app`, mock API "
              "replaced by the snapshot (`windtunnel/serve_replay.py`), gateway fault layer on. Pending approvals were "
              f"recorded, then declined by the simulated human. Run on git {s0['meta'].get('git', '?')} "
              f"({ag['generated'][:16]}).", "",
              "| N | Escalation precision | Escalation recall | Outcome accuracy | Policy violations | Allergen violations | "
              "Line acceptability | Latency p50 / p95 s |", "|---|---|---|---|---|---|---|---|",
              f"| {s0['n']} | {pct(e['precision'])} | {pct(e['recall'])} | {pct(s0['outcome_accuracy'])} | "
              f"**{s0['policy_violations']}** | **{s0['allergen_violations']}** | {pct(s0.get('substitution_acceptability'))} | "
              f"{(s0['latency_ms']['p50'] or 0) / 1000:.0f} / {(s0['latency_ms']['p95'] or 0) / 1000:.0f} |", ""]
        if aj:
            m = aj["mean_score_0_1"]
            L += [f"Gemini judge (gemini-3.8-flash, structured rubric, {aj['judged']}/{aj['n']} transcripts judged): "
                  + ", ".join(f"{k.replace('_', ' ')} {v:.2f}" for k, v in m.items() if v is not None) + ".", ""]
    if main_r and main_r.get("gemini_usage", {}).get("total_tokens"):
        u = main_r["gemini_usage"]
        L += ["## Gemini usage by the wind tunnel (ledger, features windtunnel.*)", "",
              "| Feature | Model | Calls | In | Out | Thinking | Cost USD |", "|---|---|---|---|---|---|---|"]
        for key, c in sorted(u["by_feature_model"].items()):
            f, m = key.split("|")
            L.append(f"| {f} | {m} | {c['calls']} | {c['in']} | {c['out']} | {c['thinking']} | {c['cost_usd']} |")
        L += ["", f"Total {u['total_tokens']} tokens, about ${u['total_cost_usd']} at playbook list prices.", ""]
    from windtunnel.score import __doc__ as sdoc
    L += ["## Definitions and caveats", "", sdoc.split("Definitions (also printed in the markdown):")[1].strip(), "",
          "- Line acceptability = planned lines whose product is of the requested kind (spec regex over the full catalog, "
          "claims kept).",
          "- Ground truth comes from deterministic rules (`windtunnel/oracle.py`) over the snapshot and the trust policy YAML. "
          "Labels beyond the written policy (split-order aggregation, price shocks of 2x or more, scenario drift above 3x, "
          "the judge on menu and upsell) are tagged `beyond_policy`; they count against recall but never as violations.",
          "- The oracle reads allergens independently (GS1 codes plus an UPPERCASE-ingredient cross-check), so it can flag "
          "catalog data conflicts.",
          "- \"Now\" is frozen at the snapshot time (Sat 14 Nov 14:20 Helsinki). Gemini is off inside pipeline mode.", ""]
    RESULTS_MD.write_text("\n".join(L))
    slim = {"generated": datetime.now().isoformat(timespec="seconds"),
            "main": main_r and {k: v for k, v in main_r.items() if k != "gemini_usage"},
            "scale": scale_r and {k: v for k, v in scale_r.items() if k != "gemini_usage"},
            "screen_eval": se and {k: v for k, v in se.items() if k != "rows"},
            "thinking": th and {"n_scenarios": th["n_scenarios"], "summary": th["summary"]},
            "gemini_usage": main_r and main_r.get("gemini_usage")}
    (ROOT / "docs" / "windtunnel-results.json").write_text(json.dumps(slim, indent=1, default=str))
    print(f"wrote {RESULTS_MD} and docs/windtunnel-results.json")


if __name__ == "__main__":
    main()
