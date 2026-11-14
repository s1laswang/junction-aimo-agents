"""Gold-set evaluation of the buyer agent.

    uv run python -m aimo.buyer.eval_gold tests/fixtures/gold/gold-buyer-20.jsonl \
        --base-url http://localhost:8000 [--concurrency 8] [--out data/eval/<name>]
    (swarm gold: swarm/A/out/gold-*.jsonl, several files allowed)

Gold JSONL schema (one object per line):
  {id, segment, lang, request, customer_id,
   gold: {acceptable_product_ids: [[ids for item 1], [ids for item 2], ...]   # [] = correct is NO order
          qty_range: [lo, hi] or [[lo, hi] per item]                            # in the request unit (L/KG/PCS)
          must_escalate: bool                                                    # a human must approve / confirm
          forbidden_allergens: [EU14 keys]}}

Scores per example (all from what the run returned, plus get_product for allergen status):
- zero_correction: the final cart (quote lines; equals the plan by construction) needs no edit:
  every line's product is acceptable, every item is covered by >= 1 acceptable line, and the
  covered amount per item is inside qty_range. With an empty gold set: correct iff no cart.
- escalation_match: (policy needs_approval/reject, or the agent's drift hold) == must_escalate.
- allergen_safe: no cart product has contains / may_contain / unknown for a forbidden allergen.
- asked: the agent asked a clarifying question (answered from the gold: the option whose product
  is acceptable, else the first option), reported separately.
Simulated approval: a human approves every pending order (HTTP, the policy's named approvers).
Clock: if the gateway runs with AIMO_REPLAY / AIMO_NOW, set the same AIMO_NOW (ISO) here so
relative dates ("by Monday") resolve against the same day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

APPROVERS = {"hotel-aurora": "anna@hotel-aurora.example", "school-tampere": "liisa@school-tampere.example",
             "hospital-meilahti": "sara@hospital-meilahti.example"}
UNSAFE = {"contains", "may_contain", "unknown"}


def load_gold(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        for i, line in enumerate(open(p)):
            line = line.strip()
            if line:
                rec = json.loads(line)
                rec.setdefault("id", f"{Path(p).stem}:{i}")
                rec["_file"] = p
                out.append(rec)
    return out


def _now() -> datetime | None:
    v = os.environ.get("AIMO_NOW")
    if not v:
        return None
    from aimo.buyer.need import HELSINKI
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=HELSINKI)


def _ranges(qr: Any, n: int) -> list[list[float] | None]:
    if not qr:
        return [None] * n
    if isinstance(qr[0], (int, float)):
        return [list(qr)] + [None] * (n - 1)
    return [list(r) if r else None for r in qr] + [None] * (n - len(qr))


def score(example: dict, result: dict, allergen_lookup) -> dict:
    g = example.get("gold") or {}
    sets = [set(map(str, s)) for s in (g.get("acceptable_product_ids") or [])]
    union = set().union(*sets) if sets else set()
    quote = result.get("quote") or {}
    plan_lines = {str(l.get("product_id")): l for l in ((result.get("plan") or {}).get("lines") or [])}
    cart = [{"product_id": str(l.get("product_id")), "qty_sales_units": l.get("qty_sales_units"),
             "covered_amount": (plan_lines.get(str(l.get("product_id"))) or {}).get("covered_amount"),
             "product_name": l.get("product_name")} for l in (quote.get("lines") or [])]
    ids = [c["product_id"] for c in cart]
    reasons = []
    if not sets:
        zero = not cart
        if cart:
            reasons.append("gold says no acceptable product exists, but the agent quoted one")
    else:
        bad = [i for i in ids if i not in union]
        if bad:
            reasons.append(f"not acceptable: {bad}")
        for k, s in enumerate(sets):
            if not any(i in s for i in ids):
                reasons.append(f"item {k + 1} not covered")
        for k, (s, rng) in enumerate(zip(sets, _ranges(g.get("qty_range"), len(sets)))):
            if rng is None:
                continue
            amt = sum((c["covered_amount"] or 0) for c in cart if c["product_id"] in s)
            if not (rng[0] - 1e-6 <= amt <= rng[1] + 1e-6):
                reasons.append(f"item {k + 1} covers {amt:g}, gold range {rng}")
        zero = not reasons
    steps = result.get("steps") or []
    dec = next((s["data"].get("decision") or {} for s in steps if s["kind"] == "decision"), {})
    escalated = dec.get("outcome") in ("needs_approval", "reject", "needs_confirmation") or \
        result.get("status") in ("needs_confirmation", "needs_clarification")
    forbidden = g.get("forbidden_allergens") or []
    unsafe = []
    for pid in ids:
        if forbidden:
            st = allergen_lookup(pid)
            for a in forbidden:
                if st.get(a, "unknown") in UNSAFE:
                    unsafe.append(f"{pid}:{a}={st.get(a, 'unknown')}")
    return {"zero_correction": zero, "zero_correction_reasons": reasons,
            "escalated": escalated, "must_escalate": bool(g.get("must_escalate")),
            "escalation_match": escalated == bool(g.get("must_escalate")),
            "allergen_safe": not unsafe, "unsafe": unsafe,
            "asked": bool(result.get("clarifications")), "status": result.get("status"),
            "cart": cart, "total_net": quote.get("total_net")}


class Approver(threading.Thread):
    """Simulated humans: approve every pending order of the customers under evaluation."""

    def __init__(self, base_url: str):
        super().__init__(daemon=True)
        self.base, self.stop = base_url, threading.Event()

    def run(self) -> None:
        import httpx
        seen: set[str] = set()
        while not self.stop.is_set():
            try:
                from aimo.gateway.human_auth import local_pin
                human = {"X-Approver-Pin": local_pin()}
                items = httpx.get(f"{self.base}/v1/approvals", params={"status": "pending"}, headers=human, timeout=5).json()
                for it in items if isinstance(items, list) else []:
                    o = it.get("order", it)
                    oid, cust = o.get("order_id"), o.get("customer_id")
                    if not oid or oid in seen or cust not in APPROVERS:
                        continue
                    seen.add(oid)
                    httpx.post(f"{self.base}/v1/approvals/{oid}/decision", timeout=10, headers=human,
                               json={"decision": "approve", "approver": APPROVERS[cust], "note": "gold eval",
                                     "quote_hash": o.get("quote_hash") or (it.get("quote") or {}).get("quote_hash")})
            except Exception:  # noqa: BLE001
                pass
            self.stop.wait(1.0)


def gold_answer(example: dict):
    sets = (example.get("gold") or {}).get("acceptable_product_ids") or []
    union = {str(i) for s in sets for i in s}

    def answer(q: dict) -> dict:
        for o in q.get("options", []):
            if o.get("product_id") and str(o["product_id"]) in union:
                return {"option_id": o["id"]}
        return {"option_id": q["options"][0]["id"]}
    return answer


def run_one(example: dict, a, lookup) -> dict:
    from aimo.buyer.agent import BuyerAgent
    from aimo.buyer.profiles import get_profile, mandate_for
    from aimo.buyer.tools import connect
    cust = example["customer_id"]
    prof = get_profile(cust)
    t0 = time.time()
    transport = None
    try:
        transport = connect(a.transport, agent_id=prof["agent_id"], customer_id=cust,
                            mcp_url=f"{a.base_url}/mcp", mandate=mandate_for(cust))
        agent = BuyerAgent(transport, cust, model=a.model, thinking=a.thinking, escalate=not a.no_escalate,
                           approval_timeout_s=a.approval_timeout, now=_now(), answer_fn=gold_answer(example),
                           audit_base_url=a.base_url)
        result = agent.run(example["request"])
    except Exception as e:  # noqa: BLE001
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "steps": []}
    finally:
        if transport is not None:
            transport.close()
    s = score(example, result, lookup)
    s.update({"id": example["id"], "segment": example.get("segment"), "lang": example.get("lang"),
              "customer_id": cust, "request": example["request"], "run_id": result.get("run_id"),
              "wall_s": round(time.time() - t0, 1)})
    print(f"  {example['id']:>6} {'OK ' if s['zero_correction'] else 'FIX'} esc={'ok' if s['escalation_match'] else 'X '} "
          f"safe={'ok' if s['allergen_safe'] else 'X '} {s['status']:<19} {', '.join(c['product_id'] for c in s['cart'])}",
          flush=True)
    return s


def make_lookup(a):
    from aimo.buyer.tools import connect
    t = connect(a.transport, agent_id="eval/scorer", customer_id="hotel-aurora", mcp_url=f"{a.base_url}/mcp")
    cache: dict[str, dict] = {}
    lock = threading.Lock()

    def lookup(pid: str) -> dict:
        with lock:
            if pid not in cache:
                try:
                    p = t.call("get_product", {"product_id": pid})
                    cache[pid] = {k: str(v) for k, v in (p.get("allergens") or {}).items()}
                except Exception:  # noqa: BLE001
                    cache[pid] = {}
            return cache[pid]
    return lookup, t


def summarize(rows: list[dict]) -> dict:
    n = len(rows) or 1
    by_seg: dict[str, list] = {}
    for r in rows:
        by_seg.setdefault(r.get("segment") or "?", []).append(r)
    agg = lambda rs: {"n": len(rs), "zero_correction": sum(r["zero_correction"] for r in rs) / max(len(rs), 1),
                      "escalation_match": sum(r["escalation_match"] for r in rs) / max(len(rs), 1),
                      "allergen_safe": sum(r["allergen_safe"] for r in rs) / max(len(rs), 1),
                      "asked": sum(r["asked"] for r in rs)}
    return {"overall": agg(rows), "by_segment": {k: agg(v) for k, v in sorted(by_seg.items())},
            "errors": sum(1 for r in rows if r["status"] == "error"), "n": n}


def write_report(out: Path, rows: list[dict], summ: dict, a, started: str, cost: dict | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"summary": summ, "cost": cost, "rows": rows, "args": vars(a),
                                                  "started": started}, indent=1, default=str))
    o = summ["overall"]
    md = [f"# Buyer agent gold eval", "",
          f"Started {started}; gold: {', '.join(a.gold)}; gateway {a.base_url} ({a.transport}); model "
          f"{a.model or 'gemini-3.8-flash'} thinking {a.thinking}; AIMO_NOW={os.environ.get('AIMO_NOW', '(real clock)')}.", "",
          f"**Zero-correction {o['zero_correction']:.0%}**, escalation match {o['escalation_match']:.0%}, "
          f"allergen-safe {o['allergen_safe']:.0%} over {o['n']} examples ({o['asked']} asked a clarifying question; "
          f"{summ['errors']} errors).", ""]
    if cost:
        md += [f"Gemini: {cost['calls']} calls, {cost['in']:,} input tokens ({cost['cached']:,} cached), "
               f"${cost['cost_usd']:.4f} total.", ""]
    md += ["| Segment | n | Zero-correction | Escalation match | Allergen-safe | Asked |", "|---|---|---|---|---|---|"]
    for seg, s in summ["by_segment"].items():
        md.append(f"| {seg} | {s['n']} | {s['zero_correction']:.0%} | {s['escalation_match']:.0%} | {s['allergen_safe']:.0%} | {s['asked']} |")
    md += ["", "| id | segment | lang | status | zero-corr | esc (got/gold) | safe | cart | why not |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['segment']} | {r['lang']} | {r['status']} | {'yes' if r['zero_correction'] else 'NO'} | "
                  f"{'Y' if r['escalated'] else 'N'}/{'Y' if r['must_escalate'] else 'N'} | {'yes' if r['allergen_safe'] else 'NO'} | "
                  f"{', '.join(c['product_id'] + ' x' + format(c['qty_sales_units'] or 0, 'g') for c in r['cart']) or '-'} | "
                  f"{'; '.join(r['zero_correction_reasons'] + r['unsafe'])[:160]} |")
    (out / "report.md").write_text("\n".join(md) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval_gold")
    ap.add_argument("gold", nargs="+")
    ap.add_argument("--base-url", default=os.environ.get("AIMO_BASE", "http://localhost:8000"))
    ap.add_argument("--transport", default="mcp", choices=["mcp", "inproc"])
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model")
    ap.add_argument("--thinking", default="LOW")
    ap.add_argument("--no-escalate", action="store_true")
    ap.add_argument("--approval-timeout", type=float, default=180)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    a.base_url = a.base_url.rstrip("/")
    a.concurrency = max(1, min(8, a.concurrency))  # shared Gemini key: never more than 8
    examples = load_gold(a.gold)[: a.limit]
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = Path(a.out or f"data/eval/gold-{datetime.now():%Y%m%d-%H%M}")
    print(f"Gold eval: {len(examples)} examples, concurrency {a.concurrency}, gateway {a.base_url}", flush=True)
    approver = Approver(a.base_url)
    approver.start()
    lookup, lt = make_lookup(a)
    try:
        with ThreadPoolExecutor(a.concurrency) as pool:
            rows = list(pool.map(lambda e: run_one(e, a, lookup), examples))
    finally:
        approver.stop.set()
        lt.close()
    from aimo.buyer.costs import ledger_rows, summarize as cost_sum
    run_ids = [r["run_id"] for r in rows if r.get("run_id")]
    cost = cost_sum(ledger_rows(run_ids=run_ids)) if run_ids else None
    summ = summarize(rows)
    write_report(out, rows, summ, a, started, cost)
    o = summ["overall"]
    print(f"Zero-correction {o['zero_correction']:.0%} | escalation match {o['escalation_match']:.0%} | "
          f"allergen-safe {o['allergen_safe']:.0%} | n={o['n']} | wrote {out}/report.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
