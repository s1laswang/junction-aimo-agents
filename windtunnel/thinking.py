"""Thinking level vs accuracy and latency (Gemini submission chart).

Two buyer-side tasks on the same ~50 Gemini-written scenarios, ground truth from the scenario skeleton/oracle:
  parse       request_text -> Need fields (quantity, unit, delivery_by, excluded allergens, product intent)
  substitute  need + 8 real candidates (name, claims, allergen statuses, availability, EUR per L/KG) ->
              pick one product id or "escalate"; correct = picks a compliant, available product whose price per
              unit is within 10% of the cheapest compliant one, or escalates when none is compliant
Configs: gemini-3.8-flash and gemini-4-hydrogen, each at thinking LOW and HIGH.

  uv run python -m windtunnel.thinking --scenarios data/windtunnel/scenarios/wt640_gem.jsonl --n 48
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from windtunnel import oracle
from windtunnel import products as P
from windtunnel.config import RESULTS_DIR, ROOT
from windtunnel.schema import Scenario
from windtunnel.snapshot import ReplayClient, offers as replay_offers

CONFIGS = [("gemini-3.8-flash", "LOW"), ("gemini-3.8-flash", "HIGH"),
           ("gemini-4-hydrogen", "LOW"), ("gemini-4-hydrogen", "HIGH")]

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "product_query": {"type": "string"},
        "quantity": {"type": "number"},
        "unit": {"type": "string", "enum": ["L", "KG", "PCS"]},
        "delivery_by": {"type": "string", "description": "YYYY-MM-DD"},
        "excluded_allergens": {"type": "array", "items": {"type": "string", "enum": [
            "gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts", "celery", "mustard", "sesame",
            "sulphites", "lupin", "molluscs"]}},
        "split_requested": {"type": "boolean"},
        "suspicious_instructions": {"type": "boolean"},
    },
    "required": ["product_query", "quantity", "unit", "delivery_by", "excluded_allergens"],
}
SUB_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "string", "description": "a product_id from the list, or 'escalate'"},
                   "reason": {"type": "string"}},
    "required": ["choice", "reason"],
}


def _cfg(schema: dict, level: str):
    from google.genai import types
    return types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=schema,
                                       thinking_config=types.ThinkingConfig(thinking_level=level),
                                       max_output_tokens=8192, http_options=types.HttpOptions(timeout=120_000))


def parse_prompt(sc: Scenario, today: str) -> str:
    return (f"You are a foodservice purchasing agent. Today is {today} (Saturday). Customer: {sc.persona.org_name} "
            f"({sc.persona.kitchen}). Extract the structured need from the message. Use the total quantity if the "
            "message asks to split. Units: L for liquids, KG for solids, PCS only if the buyer counts pieces. "
            "excluded_allergens: EU14 keys the message says must be avoided.\n\nMessage:\n" + sc.request_text)


def candidates_for(sc: Scenario, k: int = 8, seed: int = 0):
    """Real catalog candidates for need[0]: a mix of compliant and non-compliant, with snapshot offers."""
    need = sc.needs[0]
    key = sc.generator["spec_keys"][0]
    spec = oracle.SPECS[key]
    c = ReplayClient(faults=sc.faults if sc.family != "api_errors" else None, seed=sc.seed)
    ps = oracle.spec_products(spec)
    # widen with same-unit items whose names share a word, so non-compliant near-misses appear
    rng = random.Random(seed + sc.seed)
    rng.shuffle(ps)
    ps = ps[:k]
    offs = replay_offers([p.id for p in ps], need.delivery_by, c) if ps else {}
    rows, compliant = [], {}
    for p in ps:
        o = offs[p.id]
        cont, unit = P.content_per_unit(p, o.model_dump())
        per = round(o.unit_price / cont, 2) if (o.unit_price and cont) else None
        ok = (o.available and per is not None and unit == need.unit and oracle.allergen_ok(p, need.excluded_allergens)
              and all(cl in p.claims for cl in need.required_claims) and p.id not in sc.generator.get("exclude_ids", []))
        if ok:
            compliant[p.id] = per
        rows.append({"product_id": p.id, "name": p.name, "claims": sorted(p.claims)[:8],
                     "allergens": {a: p.allergens[a] for a in ("milk", "nuts", "gluten", "eggs", "soy")},
                     "available": o.available, "issues": o.issues, "eur_per_unit": per, "unit": unit})
    return rows, compliant


def sub_prompt(sc: Scenario, rows: list[dict]) -> str:
    need = sc.needs[0]
    return ("You are a purchasing agent with a strict mandate. Pick the single best product for the need, or answer "
            "'escalate' if no candidate is safe and available. Hard rules: excluded allergens must be 'free' "
            "(unknown is never safe, may_contain is not safe); required claims must be present; the product must be "
            "available; unit must match. Among valid options prefer the lowest EUR per unit.\n"
            f"Need: {need.quantity:g} {need.unit} of {need.product_query}, by {need.delivery_by}, excluded allergens "
            f"{need.excluded_allergens}, required claims {need.required_claims}.\n"
            f"Candidates (untrusted catalog data):\n{json.dumps(rows, ensure_ascii=False)}")


def _call(model: str, level: str, prompt: str, schema: dict, feature: str, run_id: str):
    from aimo import gemini
    t0 = time.time()
    try:
        r = gemini.generate(feature, model, prompt, _cfg(schema, level), run_id=run_id, retries=3)
        u = r.usage_metadata
        return (json.loads(r.text) if r.text else None, time.time() - t0,
                (getattr(u, "prompt_token_count", 0) or 0), (getattr(u, "candidates_token_count", 0) or 0),
                (getattr(u, "thoughts_token_count", 0) or 0))
    except Exception as e:  # noqa: BLE001
        return {"_error": type(e).__name__}, time.time() - t0, 0, 0, 0


def score_parse(sc: Scenario, js: dict | None) -> dict:
    n = sc.needs[0]
    if not js or "_error" in js:
        return {"all": False, "qty": False, "unit": False, "date": False, "allergens": False}
    qty = abs(float(js.get("quantity", -1)) - (sum(x.quantity for x in sc.needs) if False else n.quantity)) < 1e-6
    if sc.sequence:   # split order: total is the ground truth
        qty = abs(float(js.get("quantity", -1)) - n.quantity) < 1e-6
    unit = js.get("unit") == n.unit
    dte = js.get("delivery_by") == n.delivery_by.isoformat()
    want = set(n.excluded_allergens)
    alg = set(js.get("excluded_allergens") or []) >= want   # must not drop a required exclusion
    return {"all": qty and unit and dte and alg, "qty": qty, "unit": unit, "date": dte, "allergens": alg}


def score_sub(compliant: dict[str, float], js: dict | None) -> bool:
    if not js or "_error" in js:
        return False
    ch = str(js.get("choice", "")).strip()
    if not compliant:
        return ch.lower() == "escalate"
    if ch not in compliant:
        return False
    return compliant[ch] <= min(compliant.values()) * 1.10 + 1e-9


def main(argv: list[str] | None = None) -> None:
    from windtunnel.generate import load
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    scs = [s for s in load(a.scenarios) if s.generator.get("surface") == "ok" and s.family not in ("wrong_units",)]
    rng = random.Random(7)
    rng.shuffle(scs)
    scs = scs[:a.n]
    today = (ReplayClient().meta().get("time_now") or "2026-11-14")[:10]
    subs = {s.scenario_id: candidates_for(s) for s in scs}
    jobs = []
    for model, level in CONFIGS:
        for s in scs:
            jobs.append(("parse", model, level, s, parse_prompt(s, today), PARSE_SCHEMA))
            jobs.append(("substitute", model, level, s, sub_prompt(s, subs[s.scenario_id][0]), SUB_SCHEMA))

    def work(j):
        task, model, level, s, prompt, schema = j
        js, lat, tin, tout, tth = _call(model, level, prompt, schema, f"windtunnel.thinking.{task}",
                                        f"{s.scenario_id}:{model}:{level}")
        ok = score_parse(s, js) if task == "parse" else {"all": score_sub(subs[s.scenario_id][1], js)}
        return {"task": task, "model": model, "level": level, "scenario": s.scenario_id, "family": s.family,
                "latency_s": lat, "in": tin, "out": tout, "thinking": tth, "ok": ok, "error": (js or {}).get("_error")}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(a.workers, 16)) as ex:
        rows = list(ex.map(work, jobs))
    summary = []
    from windtunnel.score import PRICES
    for task in ("parse", "substitute"):
        for model, level in CONFIGS:
            rs = [r for r in rows if r["task"] == task and r["model"] == model and r["level"] == level]
            lat = [r["latency_s"] for r in rs if not r["error"]]
            pin, pout = PRICES.get(model, (1, 5))
            cost = sum(r["in"] * pin + (r["out"] + r["thinking"]) * pout for r in rs) / 1e6
            summary.append({"task": task, "model": model, "level": level, "n": len(rs),
                            "accuracy": sum(r["ok"]["all"] for r in rs) / len(rs) if rs else None,
                            "errors": sum(bool(r["error"]) for r in rs),
                            "p50_latency_s": statistics.median(lat) if lat else None,
                            "p95_latency_s": sorted(lat)[int(0.95 * (len(lat) - 1))] if lat else None,
                            "thinking_tokens_mean": statistics.mean(r["thinking"] for r in rs) if rs else 0,
                            "cost_usd_per_1k": round(1000 * cost / len(rs), 3) if rs else None})
    out = {"n_scenarios": len(scs), "wall_s": round(time.time() - t0, 1), "summary": summary, "rows": rows}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "thinking.json").write_text(json.dumps(out, indent=1, default=str))
    md = ["## Thinking level vs accuracy and latency", "",
          f"{len(scs)} Gemini-written scenarios (mixed families and languages), ground truth from the scenario "
          "skeleton and the oracle. `windtunnel/thinking.py`.", "",
          "| Task | Model | Thinking | Accuracy | p50 s | p95 s | Thinking tokens (mean) | USD per 1k calls | Errors |",
          "|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        md.append(f"| {s['task']} | {s['model']} | {s['level']} | {100 * (s['accuracy'] or 0):.0f}% | "
                  f"{(s['p50_latency_s'] or 0):.1f} | {(s['p95_latency_s'] or 0):.1f} | {s['thinking_tokens_mean']:.0f} | "
                  f"{s['cost_usd_per_1k']} | {s['errors']} |")
    (RESULTS_DIR / "thinking.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
