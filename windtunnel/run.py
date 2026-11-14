"""Wind tunnel runner.

pipeline mode (deterministic, offline, thousands; runs on freighter compute nodes):
    need (scenario ground truth) -> catalog.plan_need -> Quote -> trust.evaluate (+ trust.screen_text)
    The mock API is replaced by windtunnel.snapshot.ReplayClient with the scenario's faults.
agent mode (network-bound, laptop, ~50-100): POST /v1/agent/runs on a running gateway with X-Aimo-Faults,
    stream the AgentStep SSE events, and read the final decision/order.

Systems under test (--sut):
    team      the team's code on main (aimo.catalog / aimo.trust); a component that is not implemented yet
              falls back to the baseline and the result is tagged e.g. "catalog=baseline,trust=team"
    baseline  a naive agent with direct API access: keyword search, cheapest unit price, no allergen or policy
              checks, always orders (what happens WITHOUT the trusted gateway)

  uv run python -m windtunnel.run pipeline --scenarios data/windtunnel/scenarios/smoke160.jsonl --sut team
  uv run python -m windtunnel.run agent --scenarios ... --base-url http://localhost:8000 --limit 50
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from aimo.models import Mandate, Need, Offer, Plan, PlanLine, PolicyDecision, Quote, QuoteLine
from windtunnel import products as P
from windtunnel.config import RUNS_DIR
from windtunnel.customers import CUSTOMERS
from windtunnel.schema import LineOut, RunResult, Scenario
from windtunnel.snapshot import BackendError, ReplayClient, merge_offers, parse_dt

# ---------------------------------------------------------------- backend patching (team SUT reads offers via aimo.backend)
_CURRENT: dict[str, ReplayClient] = {}


def _replay_get_client():
    return _CURRENT["client"]


def _replay_get_offers(product_ids: list[str], requested_date: date, *_, **__) -> dict[str, Offer]:
    c = _CURRENT["client"]
    return merge_offers(c.price(list(product_ids), requested_date), c.availability(list(product_ids), requested_date),
                        requested_date, parse_dt(c.time_now))


def install_replay() -> list[str]:
    """Point every reference to aimo.backend.get_client / get_offers at the replay client. Returns patched names."""
    patched = []
    try:
        import aimo.backend as B
    except Exception:  # noqa: BLE001
        return patched
    originals = {"get_client": getattr(B, "get_client", None), "get_offers": getattr(B, "get_offers", None)}
    repl = {"get_client": _replay_get_client, "get_offers": _replay_get_offers}
    for name, fn in repl.items():
        setattr(B, name, fn)
        patched.append(f"aimo.backend.{name}")
    for mname, mod in list(sys.modules.items()):
        if not mname.startswith("aimo.") or mod is None:
            continue
        for name, orig in originals.items():
            if orig is not None and getattr(mod, name, None) is orig:
                setattr(mod, name, repl[name])
                patched.append(f"{mname}.{name}")
    os.environ["AIMO_OFFLINE"] = "1"
    return patched


# ---------------------------------------------------------------- SUT components
def _try(modname: str, attr: str) -> Optional[Callable]:
    try:
        mod = __import__(modname, fromlist=[attr])
        fn = getattr(mod, attr, None)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001
        return None


def baseline_plan(need: Need) -> Plan:
    """Naive agent: keyword search on names, cheapest per unit among available offers, no allergen/claim checks."""
    words = [w for w in need.product_query.lower().replace("-", " ").split() if len(w) > 2]
    cands = P.search(words, must_all=True, limit=60) or P.search(words, limit=60)
    from windtunnel.snapshot import pool
    pool_ids = set(pool()["ids"])
    cands = [p for p in cands if p.id in pool_ids][:40]
    offs = _replay_get_offers([p.id for p in cands], need.delivery_by) if cands else {}
    best = None
    for p in cands:
        o = offs.get(p.id)
        if not o or not o.unit_price or not o.available:
            continue
        cont, unit = P.content_per_unit(p, o.model_dump())
        if not cont:
            continue
        per = o.unit_price / cont
        if best is None or per < best[0]:
            best = (per, p, o, cont)
    lines = []
    covered = 0.0
    if best:
        _, p, o, cont = best
        units = math.ceil(need.quantity / cont - 1e-9)
        covered = units * cont
        lines.append(PlanLine(product_id=p.id, product_name=p.name, tier="best", qty_sales_units=units,
                              sales_unit=o.sales_unit or p.sales_unit, covered_amount=covered, unit_price=o.unit_price,
                              line_total=round(units * o.unit_price, 2)))
    return Plan(need=need, lines=lines, candidates=[], covered_amount=covered,
                shortfall=max(0.0, need.quantity - covered), total_net=round(sum(l.line_total for l in lines), 2))


def baseline_evaluate(quote: Quote, mandate: Mandate) -> PolicyDecision:
    return PolicyDecision(outcome="auto_approve", policy_id="none", policy_version="baseline", hits=[])


def baseline_screen(text: str) -> dict:
    return {"suspicious": False, "reasons": []}


class SUT:
    def __init__(self, kind: str):
        self.kind = kind
        parts = {}
        if kind == "team":
            self.plan_fn = _try("aimo.catalog", "plan_need")
            self.eval_fn = _try("aimo.trust", "evaluate")
            self.screen_fn = _try("aimo.trust", "screen_text")
        else:
            self.plan_fn = self.eval_fn = self.screen_fn = None
        self.plan_fn, parts["catalog"] = (self.plan_fn, "team") if self.plan_fn else (baseline_plan, "baseline")
        self.eval_fn, parts["trust"] = (self.eval_fn, "team") if self.eval_fn else (baseline_evaluate, "baseline")
        self.screen_fn, parts["screen"] = (self.screen_fn, "team") if self.screen_fn else (baseline_screen, "baseline")
        self.parts = parts
        self.name = kind if all(v == kind for v in parts.values()) else ",".join(f"{k}={v}" for k, v in parts.items())

    def plan(self, need: Need) -> Plan:
        try:
            if self.parts["catalog"] == "team":
                return self.plan_fn(need, offers_fn=_replay_get_offers)   # catalog's offline replay hook
            return self.plan_fn(need)
        except NotImplementedError:
            self.plan_fn, self.parts["catalog"] = baseline_plan, "baseline"
            return baseline_plan(need)

    def evaluate(self, quote: Quote, mandate: Mandate, **ctx) -> PolicyDecision:
        try:
            if self.parts["trust"] == "team":
                return self.eval_fn(quote, mandate, **ctx)
            return self.eval_fn(quote, mandate)
        except NotImplementedError:
            self.eval_fn, self.parts["trust"] = baseline_evaluate, "baseline"
            return baseline_evaluate(quote, mandate)

    def screen(self, text: str) -> dict:
        try:
            r = self.screen_fn(text, use_gemini=False) if self.parts["screen"] == "team" else self.screen_fn(text)
            return r if isinstance(r, dict) else {"suspicious": bool(r)}
        except NotImplementedError:
            self.screen_fn, self.parts["screen"] = baseline_screen, "baseline"
            return baseline_screen(text)


# ---------------------------------------------------------------- pipeline mode
def build_quote(sc: Scenario, plans: list[Plan], agent_id: str, now: datetime) -> Quote:
    lines = [QuoteLine(product_id=l.product_id, product_name=l.product_name, qty_sales_units=l.qty_sales_units,
                       sales_unit=l.sales_unit, unit_price=l.unit_price, line_total=l.line_total, tier=l.tier,
                       flags=list(l.flags)) for p in plans for l in p.lines]
    return Quote(quote_id=f"wt-{sc.scenario_id}-{uuid.uuid4().hex[:6]}", customer_id=sc.persona.customer_id,
                 agent_id=agent_id, delivery_date=plans[0].need.delivery_by if plans else sc.needs[0].delivery_by,
                 lines=lines, total_net=round(sum(l.line_total for l in lines), 2), created_at=now,
                 expires_at=now + timedelta(minutes=15), need=plans[0].need if plans else sc.needs[0])


def mandate_for(sc: Scenario, now: datetime) -> Mandate:
    cust = CUSTOMERS[sc.persona.customer_id]
    exp = None
    if sc.generator.get("bust") == "expired_mandate":
        exp = now - timedelta(days=1)
    return Mandate(customer_id=sc.persona.customer_id, agent_id=sc.persona.agent_id, policy_id=cust.policy_id,
                   expires_at=exp)


class MemoryAudit:
    """Per-scenario stand-in for the audit log: trust's period_spend rule reads order.placed events from it,
    the runner appends one for every auto-approved quote (what the gateway would do when it places the order)."""

    class _E:
        def __init__(self, action: str, ts: datetime, data: dict):
            self.action, self.ts, self.data = action, ts, data

    def __init__(self):
        self.events: list = []

    def events_by_action(self, action: str):
        return [e for e in self.events if e.action == action]

    def append(self, actor: str = "", action: str = "", subject_id: str = "", summary: str = "", data: dict | None = None,
               ts: datetime | None = None):
        self.events.append(MemoryAudit._E(action, ts or datetime.now(timezone.utc), data or {}))


def _products_override(sc: Scenario, q: Quote) -> dict | None:
    """Inject the scenario's untrusted supplier text into the products trust sees (marketing_text)."""
    if not sc.supplier_text:
        return None
    try:
        from aimo.catalog import get_product
    except Exception:  # noqa: BLE001
        return None
    out = {}
    for ln in q.lines:
        p = get_product(ln.product_id)
        if p is None:
            continue
        if ln.product_id in sc.supplier_text:
            p = p.model_copy(update={"marketing_text": sc.supplier_text[ln.product_id]})
        out[ln.product_id] = p
    return out


def _plan_with_retry(sut: SUT, need: Need, retries: int = 3) -> Plan:
    last: Exception | None = None
    for _ in range(retries):
        try:
            return sut.plan(need)
        except BackendError as e:          # simulated 429/5xx: retry like a well-behaved client
            last = e
    raise last  # type: ignore[misc]


def run_pipeline_one(sc: Scenario, sut: SUT) -> RunResult:
    t0 = time.perf_counter()
    client = ReplayClient(faults=sc.faults, seed=sc.seed)
    _CURRENT["client"] = client
    now = parse_dt(client.time_now) or datetime.now(timezone.utc)
    res = RunResult(scenario_id=sc.scenario_id, family=sc.family, customer_id=sc.persona.customer_id, mode="pipeline",
                    sut=sut.name, need_quantity=sum(n.quantity for n in sc.needs))
    try:
        # injection screening: positives (supplier/request payloads) and negatives (clean request text)
        if sc.supplier_text:
            res.screen["supplier"] = [bool(sut.screen(t).get("suspicious")) for t in sc.supplier_text.values()]
        rq = sut.screen(sc.request_text)
        res.screen["request"] = bool(rq.get("suspicious"))
        if rq.get("suspicious"):
            # buyer-agent contract: report suspicious instructions in untrusted text via need.preferences
            for n in [x for seq in (sc.sequence or [sc.needs]) for x in seq]:
                n.preferences = {**(n.preferences or {}), "injection_flags": rq.get("reasons") or ["suspicious_request"]}
        mandate = mandate_for(sc, now)
        audit_log = MemoryAudit()
        orders = sc.sequence or [sc.needs]
        auto_total, all_auto, outcomes = 0.0, True, []
        last_quote, last_plans, last_dec = None, [], None
        for needs in orders:
            plans = [_plan_with_retry(sut, n) for n in needs]
            q = build_quote(sc, plans, sc.persona.agent_id, now)
            dec = sut.evaluate(q, mandate, now=now, audit_log=audit_log, products=_products_override(sc, q)) \
                if q.lines else PolicyDecision(
                outcome="needs_approval", policy_id="windtunnel", policy_version="0",
                hits=[])  # empty plan: nothing to order -> a human must look
            outcomes.append(dec.outcome)
            if dec.outcome == "auto_approve":
                auto_total += q.total_net
                audit_log.append(action="order.placed", subject_id=q.quote_id, ts=now,
                                 data={"customer_id": q.customer_id, "total_net": q.total_net, "quote_id": q.quote_id})
            else:
                all_auto = False
            last_quote, last_plans, last_dec = q, plans, dec
        assert last_quote is not None and last_dec is not None
        if sc.sequence:
            limit = CUSTOMERS[sc.persona.customer_id].auto_limit or float("inf")
            res.outcome = "auto_approve" if all_auto else ("reject" if "reject" in outcomes else "needs_approval")
            res.order_placed = auto_total > limit            # evasion succeeded
            res.total_net = round(auto_total if all_auto else last_quote.total_net, 2)
            res.extra["sequence_outcomes"] = outcomes
            res.extra["auto_total"] = round(auto_total, 2)
        else:
            res.outcome = last_dec.outcome
            res.order_placed = last_dec.outcome == "auto_approve" and bool(last_quote.lines)
            res.total_net = last_quote.total_net
        res.approver_role = last_dec.approver_role
        res.rule_hits = [f"{h.rule_id}:{h.outcome}" for h in last_dec.hits]
        res.lines = [LineOut(product_id=l.product_id, qty_sales_units=l.qty_sales_units, unit_price=l.unit_price,
                             line_total=l.line_total, tier=l.tier, flags=l.flags) for l in last_quote.lines]
        res.covered_amount = sum(p.covered_amount for p in last_plans) if not sc.sequence else \
            sum(p.covered_amount for p in last_plans) * len(orders)
        res.extra["shortfall"] = sum(p.shortfall for p in last_plans)
        res.extra["plan_total"] = sum(p.total_net for p in last_plans)
    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"[:300]
        res.extra["trace"] = traceback.format_exc()[-800:]
        res.outcome, res.order_placed = None, False
    res.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    res.sim_latency_ms = client.sim_latency_ms
    res.backend_calls, res.backend_errors = client.calls, client.errors
    return res


def run_pipeline(scs: list[Scenario], sut_kind: str = "team") -> tuple[list[RunResult], dict]:
    os.environ.setdefault("AIMO_RECIPE_CHECK", "0")     # no Gemini calls inside pipeline mode (offline nodes)
    os.environ.setdefault("AIMO_SCREEN_GEMINI", "0")
    patched = install_replay() if sut_kind == "team" else []
    sut = SUT(sut_kind)
    out = [run_pipeline_one(sc, sut) for sc in scs]
    for r in out:
        r.sut = sut.name
    return out, {"sut": sut.name, "parts": sut.parts, "patched": patched}


# ---------------------------------------------------------------- agent mode (gateway + Gemini buyer)
def run_agent_one(sc: Scenario, base_url: str, timeout_s: float = 240.0, model: str | None = None) -> RunResult:
    """Start a buyer-agent run on the gateway and follow its SSE events. Field names inside AgentStep.data are
    read defensively (contract: docs/interfaces.md lists only kinds)."""
    import httpx
    t0 = time.perf_counter()
    res = RunResult(scenario_id=sc.scenario_id, family=sc.family, customer_id=sc.persona.customer_id, mode="agent",
                    sut="gateway+buyer", need_quantity=sum(n.quantity for n in sc.needs))
    headers = {"X-Customer-Id": sc.persona.customer_id, "X-Agent-Id": sc.persona.agent_id}
    faults = sc.faults
    if sc.supplier_text:
        faults = ";".join(x for x in [faults] + [f"inject:{pid}" for pid in sc.supplier_text] if x)
    if faults:
        headers["X-Aimo-Faults"] = faults
    body: dict[str, Any] = {"text": sc.request_text, "customer_id": sc.persona.customer_id}
    if model:
        body["model"] = model
    if sc.supplier_text:
        body["supplier_text"] = sc.supplier_text          # UNVERIFIED: gateway may ignore; see report
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=timeout_s) as c:
            r = c.post("/v1/agent/runs", json=body)
            r.raise_for_status()
            run_id = r.json()["run_id"]
            res.extra["run_id"] = run_id
            with c.stream("GET", f"/v1/agent/runs/{run_id}/events", timeout=timeout_s) as s:
                for line in s.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        step = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    res.transcript.append(step)
                    if step.get("kind") in ("final", "error", "approval_wait") or time.perf_counter() - t0 > timeout_s:
                        if step.get("kind") != "approval_wait":
                            break
                        # approval_wait: the run pauses for a human. That IS an escalation; stop here.
                        break
        _summarise_agent(res)
    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"[:300]
    res.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return res


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, dict):
                x = _dig(v, *keys)
                if x is not None:
                    return x
    return None


def _summarise_agent(res: RunResult) -> None:
    outcome, quote, order = None, None, None
    for st in res.transcript:
        k, data = st.get("kind"), st.get("data") or {}
        if k == "decision":
            outcome = _dig(data, "outcome") or outcome
            res.approver_role = _dig(data, "approver_role") or res.approver_role
            hits = _dig(data, "hits") or []
            res.rule_hits = [f"{h.get('rule_id')}:{h.get('outcome')}" for h in hits if isinstance(h, dict)]
        elif k == "quote":
            quote = data.get("quote", data)
        elif k == "order":
            order = data.get("order", data)
        elif k == "approval_wait":
            outcome = outcome or "needs_approval"
        elif k == "tool_result" and _dig(data, "suspicious") is not None:
            res.screen.setdefault("agent_tool_flags", []).append(bool(_dig(data, "suspicious")))
    if order:
        st = order.get("status")
        res.order_placed = st in ("placed", "approved") and not order.get("approved_by")
        outcome = outcome or (_dig(order, "outcome") if isinstance(order, dict) else None)
    if quote and isinstance(quote, dict):
        res.total_net = quote.get("total_net")
        res.lines = [LineOut(**{k: l.get(k) for k in ("product_id", "qty_sales_units", "unit_price", "line_total", "tier")
                                if l.get(k) is not None}) for l in quote.get("lines", []) if isinstance(l, dict)]
    res.outcome = outcome if outcome in ("auto_approve", "needs_approval", "reject") else None
    if res.outcome == "auto_approve" and not order:
        res.order_placed = True
    res.extra["kinds"] = [s.get("kind") for s in res.transcript]


def run_agent(scs: list[Scenario], base_url: str, workers: int = 4, model: str | None = None) -> list[RunResult]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(workers, 16)) as ex:
        return list(ex.map(lambda s: run_agent_one(s, base_url, model=model), scs))


# ---------------------------------------------------------------- CLI
def save_results(rs: list[RunResult], path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rs:
            f.write(r.model_dump_json() + "\n")
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1, default=str))


def main(argv: list[str] | None = None) -> None:
    from windtunnel.generate import load
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["pipeline", "agent"])
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--sut", default="team", choices=["team", "baseline"])
    ap.add_argument("--shard", default="0/1", help="i/n: run every n-th scenario starting at i (Slurm arrays)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--collect-missing", default="", help="write ids the SUT requested but the snapshot lacks")
    a = ap.parse_args(argv)
    scs = load(a.scenarios)
    i, n = map(int, a.shard.split("/"))
    scs = scs[i::n]
    if a.limit:
        scs = scs[:a.limit]
    t0 = time.time()
    if a.mode == "pipeline":
        rs, meta = run_pipeline(scs, a.sut)
    else:
        rs, meta = run_agent(scs, a.base_url, a.workers, a.model), {"sut": "gateway+buyer", "base_url": a.base_url}
    from windtunnel.snapshot import fingerprint
    meta.update(mode=a.mode, scenarios=a.scenarios, n=len(rs), shard=a.shard, wall_s=round(time.time() - t0, 2),
                host=os.uname().nodename, snapshot=fingerprint(), started=datetime.now().isoformat(timespec="seconds"),
                git=os.popen("git rev-parse --short HEAD 2>/dev/null").read().strip())
    stem = Path(a.scenarios).stem
    out = Path(a.out) if a.out else RUNS_DIR / f"{stem}__{a.mode}__{a.sut}__{i}of{n}.jsonl"
    save_results(rs, out, meta)
    if a.collect_missing:
        from windtunnel.snapshot import MISSING
        Path(a.collect_missing).write_text(json.dumps(sorted(MISSING)))
        print(f"missing ids: {len(MISSING)} -> {a.collect_missing}", file=sys.stderr)
    errs = sum(1 for r in rs if r.error)
    print(json.dumps({"out": str(out), "n": len(rs), "errors": errs, "sut": meta.get("sut"), "wall_s": meta["wall_s"]}))


if __name__ == "__main__":
    main()
