"""Gold-set evaluation of the buyer agent.

    uv run python -m aimo.buyer.eval_gold tests/fixtures/gold/gold-buyer-20.jsonl \
        --base-url http://localhost:8000 [--concurrency 8] [--out data/eval/<name>]
    (swarm gold: swarm/A/out/gold-*.jsonl, several files allowed)

Gold JSONL schema (one object per line):
  {id, segment, lang, request, customer_id,
   gold: {acceptable_product_ids: [[ids for item 1], [ids for item 2], ...] (empty = NO order)
          qty_range: [lo, hi] or [[lo, hi] per item] (in the request unit: L/KG/PCS)
          must_escalate: bool (a human must approve or confirm)
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
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

APPROVERS = {
    "hotel-aurora": "anna@hotel-aurora.example",
    "school-tampere": "liisa@school-tampere.example",
    "hospital-meilahti": "sara@hospital-meilahti.example",
}
UNSAFE = {"contains", "may_contain", "unknown"}


def _finite_float(val: Any, default: float = 0.0) -> float:
    if isinstance(val, bool) or val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _coerce_pair(pair: Any) -> list[float] | None:
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return None
    lo, hi = pair[0], pair[1]
    if isinstance(lo, bool) or isinstance(hi, bool):
        return None
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lo_f) and math.isfinite(hi_f)):
        return None
    return [lo_f, hi_f]


def load_gold(paths: list[str]) -> list[dict]:
    out: list[dict] = []
    for p in paths or []:
        p_str = str(p)
        with open(p_str, encoding="utf-8") as fh:
            for i, raw_line in enumerate(fh):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{p_str}:{i + 1}: invalid JSON ({exc.msg})") from exc
                if not isinstance(rec, dict):
                    raise ValueError(f"{p_str}:{i + 1}: gold record must be a JSON object")
                rec.setdefault("id", f"{Path(p_str).stem}:{i}")
                rec["_file"] = p_str
                out.append(rec)
    return out


def _now(base_url: str | None = None, transport: str | None = None) -> datetime | None:
    v = (os.environ.get("AIMO_NOW") or "").strip()
    if v:
        from aimo.buyer.need import HELSINKI
        try:
            d = datetime.fromisoformat(v)
            return d if d.tzinfo else d.replace(tzinfo=HELSINKI)
        except ValueError:
            pass
    if base_url:
        try:
            import httpx
            from aimo.backend.clock import parse
            r = httpx.get(f"{base_url.rstrip('/')}/healthz", timeout=2)
            if getattr(r, "status_code", 200) < 400:
                body = r.json()
                if isinstance(body, dict) and isinstance(body.get("clock"), dict):
                    parsed = parse(body["clock"].get("now"))
                    if parsed is not None:
                        return parsed
        except Exception:
            pass
    if transport == "inproc":
        from aimo.backend import clock
        from aimo.buyer.need import HELSINKI
        if clock.simulated():
            return clock.now().astimezone(HELSINKI)
    return None


def _ranges(qr: Any, n: int) -> list[list[float] | None]:
    if n <= 0:
        return []
    if not isinstance(qr, (list, tuple)) or not qr:
        return [None] * n
    if not isinstance(qr[0], (list, tuple)):
        pair = _coerce_pair(qr)
        return ([pair] + [None] * (n - 1))[:n]
    parsed = [_coerce_pair(r) for r in qr[:n]]
    return parsed + [None] * (n - len(parsed))


def _step_decision(steps: Any, order: Any = None) -> dict[str, Any]:
    if isinstance(steps, list):
        for s in steps:
            if isinstance(s, dict):
                kind = s.get("kind")
                data = s.get("data")
            else:
                kind = getattr(s, "kind", None)
                data = getattr(s, "data", None)
            if kind == "decision" and isinstance(data, dict):
                dec = data.get("decision")
                if isinstance(dec, dict):
                    return dec
    if isinstance(order, dict) and isinstance(order.get("decision"), dict):
        return order["decision"]
    return {}


def score(example: dict, result: dict, allergen_lookup) -> dict:
    ex = example if isinstance(example, dict) else {}
    res = result if isinstance(result, dict) else {}
    g = ex.get("gold") if isinstance(ex.get("gold"), dict) else {}
    raw_sets = g.get("acceptable_product_ids")
    sets = [
        {str(item) for item in s if item is not None}
        for s in (raw_sets if isinstance(raw_sets, (list, tuple)) else [])
        if isinstance(s, (list, tuple, set))
    ]
    union = set().union(*sets) if sets else set()
    quote = res.get("quote") if isinstance(res.get("quote"), dict) else {}
    plan = res.get("plan") if isinstance(res.get("plan"), dict) else {}
    raw_plan_lines = plan.get("lines") if isinstance(plan.get("lines"), list) else []
    plan_lines = {
        str(l.get("product_id")): l
        for l in raw_plan_lines
        if isinstance(l, dict) and l.get("product_id") is not None
    }
    raw_quote_lines = quote.get("lines") if isinstance(quote.get("lines"), list) else []
    cart = []
    for l in raw_quote_lines:
        if not isinstance(l, dict) or l.get("product_id") is None:
            continue
        pid = str(l.get("product_id"))
        pl = plan_lines.get(pid)
        cov = pl.get("covered_amount") if isinstance(pl, dict) and "covered_amount" in pl else l.get("covered_amount")
        cart.append({
            "product_id": pid,
            "qty_sales_units": l.get("qty_sales_units"),
            "covered_amount": cov,
            "product_name": l.get("product_name"),
        })
    ids = [c["product_id"] for c in cart]
    reasons: list[str] = []
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
            amt = sum(_finite_float(c.get("covered_amount"), 0.0) for c in cart if c["product_id"] in s)
            if not (rng[0] - 1e-6 <= amt <= rng[1] + 1e-6):
                reasons.append(f"item {k + 1} covers {amt:g}, gold range {rng}")
        zero = not reasons
    dec = _step_decision(res.get("steps"), res.get("order"))
    escalated = dec.get("outcome") in ("needs_approval", "reject", "needs_confirmation") or (
        res.get("status") in ("needs_confirmation", "needs_clarification")
    )
    raw_forbidden = g.get("forbidden_allergens")
    forbidden = [str(a) for a in raw_forbidden if a] if isinstance(raw_forbidden, (list, tuple, set)) else []
    unsafe: list[str] = []
    for pid in ids:
        if forbidden:
            try:
                raw_st = allergen_lookup(pid) if callable(allergen_lookup) else {}
            except Exception:
                raw_st = {}
            st = (
                {str(k): str(getattr(v, "value", v)) for k, v in raw_st.items()}
                if isinstance(raw_st, dict)
                else {}
            )
            for a in forbidden:
                val = st.get(a, "unknown")
                if val in UNSAFE:
                    unsafe.append(f"{pid}:{a}={val}")
    return {
        "zero_correction": zero,
        "zero_correction_reasons": reasons,
        "escalated": escalated,
        "must_escalate": bool(g.get("must_escalate")),
        "escalation_match": escalated == bool(g.get("must_escalate")),
        "allergen_safe": not unsafe,
        "unsafe": unsafe,
        "asked": bool(res.get("clarifications")),
        "status": res.get("status"),
        "cart": cart,
        "total_net": quote.get("total_net"),
    }


def _extract_requote_target(detail: Any, default_cust: str, seen: set[str]) -> tuple[str, str, str] | None:
    if not isinstance(detail, dict):
        return None
    old_oid = detail.get("old_order_id")
    if isinstance(old_oid, str) and old_oid:
        seen.add(old_oid)
    new_o = detail.get("new_order")
    if not isinstance(new_o, dict):
        return None
    new_oid = new_o.get("order_id")
    if not isinstance(new_oid, str) or not new_oid:
        return None
    prov = new_o.get("provenance") if isinstance(new_o.get("provenance"), dict) else {}
    if new_o.get("expired") or new_o.get("status") not in (None, "pending_approval") or prov.get("requoted_as"):
        seen.add(new_oid)
        return None
    cust = str(new_o.get("customer_id") or default_cust)
    if cust not in APPROVERS or new_oid in seen:
        return None
    new_q = detail.get("new_quote") if isinstance(detail.get("new_quote"), dict) else {}
    app_with = detail.get("approve_with") if isinstance(detail.get("approve_with"), dict) else {}
    app_body = app_with.get("body") if isinstance(app_with.get("body"), dict) else {}
    qhash = str(new_o.get("quote_hash") or new_q.get("quote_hash") or app_body.get("quote_hash") or "")
    return new_oid, cust, qhash


class Approver(threading.Thread):
    """Simulated humans: approve every pending order of the customers under evaluation."""

    def __init__(self, base_url: str, poll_s: float = 1.0):
        super().__init__(daemon=True)
        self.base = str(base_url or "").rstrip("/")
        self.poll_s = max(0.005, _finite_float(poll_s, 1.0))
        self.stop = threading.Event()

    def run(self) -> None:
        import httpx
        from aimo.gateway.human_auth import local_pin
        seen: set[str] = set()
        while not self.stop.is_set():
            try:
                human = {"X-Approver-Pin": local_pin()}
                r = httpx.get(f"{self.base}/v1/approvals", params={"status": "pending"}, headers=human, timeout=5)
                items = r.json() if getattr(r, "status_code", 200) < 400 else []
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    o = it.get("order", it)
                    if not isinstance(o, dict):
                        continue
                    oid, cust = o.get("order_id"), o.get("customer_id")
                    if not isinstance(oid, str) or not oid:
                        continue
                    prov = o.get("provenance") if isinstance(o.get("provenance"), dict) else {}
                    if it.get("expired") or o.get("status") not in (None, "pending_approval") or prov.get("requoted_as"):
                        seen.add(oid)
                        continue
                    if oid in seen or cust not in APPROVERS:
                        continue
                    qobj = it.get("quote") if isinstance(it.get("quote"), dict) else {}
                    target: tuple[str, str, str] | None = (
                        oid,
                        cust,
                        str(o.get("quote_hash") or qobj.get("quote_hash") or ""),
                    )
                    hops = 0
                    while target is not None and not self.stop.is_set() and hops < 8:
                        cur_oid, cur_cust, cur_qhash = target
                        target = None
                        hops += 1
                        if cur_oid in seen or cur_cust not in APPROVERS:
                            break
                        approver = APPROVERS[cur_cust]
                        decision_headers = {"X-Approver-Pin": local_pin(approver)}
                        try:
                            resp = httpx.post(
                                f"{self.base}/v1/approvals/{cur_oid}/decision",
                                timeout=10,
                                headers=decision_headers,
                                json={
                                    "decision": "approve",
                                    "approver": approver,
                                    "note": "gold eval",
                                    "quote_hash": cur_qhash,
                                },
                            )
                        except Exception:
                            break
                        code = getattr(resp, "status_code", 200)
                        if code < 500:
                            seen.add(cur_oid)
                        if code == 409:
                            try:
                                body = resp.json()
                            except Exception:
                                body = {}
                            if isinstance(body, dict) and body.get("error") == "requote_needed":
                                raw_detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
                                target = _extract_requote_target(raw_detail, cur_cust, seen)
            except Exception:
                pass
            self.stop.wait(self.poll_s)


LiveApprover = Approver


class InprocApprover(threading.Thread):
    def __init__(self, poll_s: float = 0.1):
        super().__init__(daemon=True)
        self.poll_s = max(0.005, _finite_float(poll_s, 0.1))
        self.stop = threading.Event()

    def run(self) -> None:
        from aimo.gateway import service
        seen: set[str] = set()
        while not self.stop.is_set():
            try:
                items = service.list_approvals(status="pending")
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    o = it.get("order", it)
                    if not isinstance(o, dict):
                        continue
                    oid, cust = o.get("order_id"), o.get("customer_id")
                    if not isinstance(oid, str) or not oid:
                        continue
                    prov = o.get("provenance") if isinstance(o.get("provenance"), dict) else {}
                    if it.get("expired") or o.get("status") not in (None, "pending_approval") or prov.get("requoted_as"):
                        seen.add(oid)
                        continue
                    if oid in seen or cust not in APPROVERS:
                        continue
                    qobj = it.get("quote") if isinstance(it.get("quote"), dict) else {}
                    target: tuple[str, str, str] | None = (
                        oid,
                        cust,
                        str(o.get("quote_hash") or qobj.get("quote_hash") or ""),
                    )
                    hops = 0
                    while target is not None and not self.stop.is_set() and hops < 8:
                        cur_oid, cur_cust, cur_qhash = target
                        target = None
                        hops += 1
                        if cur_oid in seen or cur_cust not in APPROVERS:
                            break
                        decide_fn = getattr(service, "decide_approval", None) or service.decide
                        try:
                            decide_fn(
                                cur_oid,
                                decision="approve",
                                approver=APPROVERS[cur_cust],
                                note="gold eval (inproc)",
                                quote_hash=cur_qhash,
                            )
                            seen.add(cur_oid)
                        except service.ServiceError as e:
                            status = getattr(e, "status", 500)
                            if status < 500:
                                seen.add(cur_oid)
                            if status == 409 and getattr(e, "code", "") == "requote_needed":
                                target = _extract_requote_target(getattr(e, "detail", None), cur_cust, seen)
                        except Exception:
                            break
            except Exception:
                pass
            self.stop.wait(self.poll_s)


def gold_answer(example: dict):
    ex = example if isinstance(example, dict) else {}
    g = ex.get("gold") if isinstance(ex.get("gold"), dict) else {}
    raw_sets = g.get("acceptable_product_ids")
    union = {
        str(i)
        for s in (raw_sets if isinstance(raw_sets, (list, tuple)) else [])
        if isinstance(s, (list, tuple, set))
        for i in s
        if i is not None
    }

    def answer(q: dict) -> dict:
        if not isinstance(q, dict):
            return {}
        raw_opts = q.get("options")
        opts = [o for o in (raw_opts if isinstance(raw_opts, list) else []) if isinstance(o, dict) and o.get("id") is not None]
        for o in opts:
            if o.get("product_id") and str(o["product_id"]) in union:
                return {"option_id": o["id"]}
        if opts:
            return {"option_id": opts[0]["id"]}
        return {}

    return answer


def run_one(example: dict, a, lookup) -> dict:
    from aimo.buyer.agent import BuyerAgent
    from aimo.buyer.profiles import get_profile, mandate_for
    from aimo.buyer.tools import connect

    ex = example if isinstance(example, dict) else {}
    ex_id = str(ex.get("id") or "?")
    cust = str(ex.get("customer_id") or "").strip()
    req_text = str(ex.get("request") or "")
    transport_kind = str(getattr(a, "transport", "mcp") or "mcp")
    base_url = str(getattr(a, "base_url", "http://localhost:8000") or "http://localhost:8000").rstrip("/")
    model = getattr(a, "model", None)
    thinking = str(getattr(a, "thinking", "LOW") or "LOW")
    no_escalate = bool(getattr(a, "no_escalate", False))
    approval_timeout = max(0.1, _finite_float(getattr(a, "approval_timeout", 180), 180.0))

    t0 = time.time()
    transport = None
    fake_stop = threading.Event()
    try:
        if not cust:
            raise ValueError("missing customer_id")
        prof = get_profile(cust)
        transport = connect(
            transport_kind,
            agent_id=prof["agent_id"],
            customer_id=cust,
            mcp_url=f"{base_url}/mcp",
            mandate=mandate_for(cust),
        )
        gw = getattr(transport, "gw", None) if transport_kind == "fake" else None
        if gw is not None and cust in APPROVERS:
            def _approve_fake() -> None:
                while not fake_stop.is_set():
                    try:
                        for o in gw.pending():
                            if isinstance(o, dict) and o.get("order_id"):
                                gw.decide(o["order_id"], "approve", APPROVERS[cust], "gold eval")
                    except Exception:
                        pass
                    fake_stop.wait(0.02)

            threading.Thread(target=_approve_fake, daemon=True).start()
        agent = BuyerAgent(
            transport,
            cust,
            model=model,
            thinking=thinking,
            escalate=not no_escalate,
            approval_timeout_s=approval_timeout,
            poll_s=0.05 if transport_kind in ("fake", "inproc") else 2.0,
            now=_now(base_url if transport_kind == "mcp" else None, transport_kind),
            answer_fn=gold_answer(ex),
            audit_base_url=base_url if transport_kind != "fake" else "",
        )
        result = agent.run(req_text)
    except Exception as e:
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "steps": []}
    finally:
        fake_stop.set()
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
    s = score(ex, result, lookup)
    s.update({
        "id": ex_id,
        "segment": ex.get("segment"),
        "lang": ex.get("lang"),
        "customer_id": cust,
        "request": req_text,
        "run_id": result.get("run_id") if isinstance(result, dict) else None,
        "mock": bool(result.get("mock")) if isinstance(result, dict) else False,
        "wall_s": round(time.time() - t0, 1),
    })
    status_label = str(s.get("status") or "error")
    cart_ids = ", ".join(str(c.get("product_id") or "") for c in (s.get("cart") or []) if isinstance(c, dict))
    print(
        f"  {ex_id:>6} {'OK ' if s['zero_correction'] else 'FIX'} esc={'ok' if s['escalation_match'] else 'X '} "
        f"safe={'ok' if s['allergen_safe'] else 'X '} {status_label:<19} {cart_ids}",
        flush=True,
    )
    return s


def make_lookup(a):
    from aimo.buyer.tools import connect

    transport_kind = str(getattr(a, "transport", "mcp") or "mcp")
    base_url = str(getattr(a, "base_url", "http://localhost:8000") or "http://localhost:8000").rstrip("/")
    t = connect(transport_kind, agent_id="eval/scorer", customer_id="hotel-aurora", mcp_url=f"{base_url}/mcp")
    cache: dict[str, dict] = {}
    lock = threading.Lock()

    def lookup(pid: str) -> dict:
        if not pid:
            return {}
        key = str(pid)
        with lock:
            if key not in cache:
                try:
                    p = t.call("get_product", {"product_id": key})
                    raw_all = p.get("allergens") if isinstance(p, dict) else None
                    cache[key] = (
                        {str(k): str(getattr(v, "value", v)) for k, v in raw_all.items()}
                        if isinstance(raw_all, dict)
                        else {}
                    )
                except Exception:
                    cache[key] = {}
            return cache[key]

    return lookup, t


def summarize(rows: list[dict]) -> dict:
    clean = [r for r in (rows or []) if isinstance(r, dict)]
    by_seg: dict[str, list[dict]] = {}
    for r in clean:
        by_seg.setdefault(str(r.get("segment") or "?"), []).append(r)

    def agg(rs: list[dict]) -> dict[str, Any]:
        denom = max(len(rs), 1)
        return {
            "n": len(rs),
            "zero_correction": sum(bool(r.get("zero_correction")) for r in rs) / denom,
            "escalation_match": sum(bool(r.get("escalation_match")) for r in rs) / denom,
            "allergen_safe": sum(bool(r.get("allergen_safe")) for r in rs) / denom,
            "asked": sum(bool(r.get("asked")) for r in rs),
        }

    return {
        "overall": agg(clean),
        "by_segment": {k: agg(v) for k, v in sorted(by_seg.items())},
        "errors": sum(1 for r in clean if r.get("status") == "error"),
        "n": len(clean),
    }


def _resolve_report_model(a, rows: list[dict]) -> str:
    from aimo.buyer.mock_model import MOCK_MODEL_NAME, mock_wanted

    explicit = getattr(a, "model", None)
    if isinstance(explicit, str) and explicit.strip():
        cleaned = explicit.strip()
        return MOCK_MODEL_NAME if cleaned in ("mock", MOCK_MODEL_NAME) else cleaned
    clean_rows = [r for r in (rows or []) if isinstance(r, dict)]
    if mock_wanted() or (clean_rows and all(r.get("mock") is True for r in clean_rows)):
        return MOCK_MODEL_NAME
    return "gemini-3.8-flash"


def write_report(out: Path, rows: list[dict], summ: dict, a, started: str, cost: dict | None) -> None:
    from aimo.backend import clock
    from aimo.buyer.mock_model import MOCK_MODEL_NAME

    out_dir = Path(out)
    clean_rows = [r for r in (rows or []) if isinstance(r, dict)]
    safe_summ = summ if isinstance(summ, dict) and "overall" in summ else summarize(clean_rows)
    model_name = _resolve_report_model(a, clean_rows)
    env_now = (os.environ.get("AIMO_NOW") or "").strip()
    if env_now:
        clock_label = env_now
    elif clock.simulated():
        cd = clock.describe()
        clock_label = f"{cd['mode']} ({cd['now']})"
    else:
        clock_label = "(real clock)"

    gold_list = getattr(a, "gold", None) or []
    gold_label = ", ".join(str(g) for g in gold_list) if isinstance(gold_list, (list, tuple)) else str(gold_list)
    base_url = str(getattr(a, "base_url", "") or "")
    transport_kind = str(getattr(a, "transport", "") or "")
    thinking = str(getattr(a, "thinking", "LOW") or "LOW")
    args_dict = vars(a) if hasattr(a, "__dict__") else {}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "summary": safe_summ,
                "cost": cost,
                "rows": clean_rows,
                "args": args_dict,
                "model": model_name,
                "started": started,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    o = safe_summ["overall"]
    md = [
        "# Buyer agent gold eval",
        "",
        f"Started {started}; gold: {gold_label}; gateway {base_url} ({transport_kind}); model "
        f"{model_name} thinking {thinking}; AIMO_NOW={clock_label}.",
        "",
        f"**Zero-correction {o['zero_correction']:.0%}**, escalation match {o['escalation_match']:.0%}, "
        f"allergen-safe {o['allergen_safe']:.0%} over {o['n']} examples ({o['asked']} asked a clarifying question; "
        f"{safe_summ.get('errors', 0)} errors).",
        "",
    ]
    if model_name == MOCK_MODEL_NAME and (not cost or not cost.get("calls")):
        md += ["Mode: deterministic scripted buyer (keyless mock; 0 Gemini API calls).", ""]
    elif isinstance(cost, dict):
        md += [
            f"Gemini: {int(cost.get('calls', 0))} calls, {int(cost.get('in', 0)):,} input tokens "
            f"({int(cost.get('cached', 0)):,} cached), ${_finite_float(cost.get('cost_usd'), 0.0):.4f} total.",
            "",
        ]
    md += ["| Segment | n | Zero-correction | Escalation match | Allergen-safe | Asked |", "|---|---|---|---|---|---|"]
    for seg, s in (safe_summ.get("by_segment") or {}).items():
        md.append(
            f"| {seg} | {s['n']} | {s['zero_correction']:.0%} | {s['escalation_match']:.0%} | "
            f"{s['allergen_safe']:.0%} | {s['asked']} |"
        )
    md += [
        "",
        "| id | segment | lang | status | zero-corr | esc (got/gold) | safe | cart | why not |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in clean_rows:
        cart_items = [
            f"{c.get('product_id')} x{_finite_float(c.get('qty_sales_units'), 0.0):g}"
            for c in (r.get("cart") or [])
            if isinstance(c, dict)
        ]
        why_not = "; ".join(
            [str(x) for x in (r.get("zero_correction_reasons") or [])]
            + [str(x) for x in (r.get("unsafe") or [])]
        )[:160]
        md.append(
            f"| {r.get('id')} | {r.get('segment')} | {r.get('lang')} | {r.get('status')} | "
            f"{'yes' if r.get('zero_correction') else 'NO'} | "
            f"{'Y' if r.get('escalated') else 'N'}/{'Y' if r.get('must_escalate') else 'N'} | "
            f"{'yes' if r.get('allergen_safe') else 'NO'} | "
            f"{', '.join(cart_items) or '-'} | {why_not} |"
        )
    (out_dir / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval_gold")
    ap.add_argument("gold", nargs="+")
    ap.add_argument("--base-url", default=os.environ.get("AIMO_BASE", "http://localhost:8000"))
    ap.add_argument("--transport", default="mcp", choices=["mcp", "inproc", "fake"])
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model")
    ap.add_argument("--thinking", default="LOW")
    ap.add_argument("--no-escalate", action="store_true")
    ap.add_argument("--approval-timeout", type=float, default=180)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    a.base_url = (a.base_url or "http://localhost:8000").rstrip("/")
    a.concurrency = max(1, min(8, a.concurrency))
    a.approval_timeout = max(0.1, _finite_float(a.approval_timeout, 180.0))
    all_examples = load_gold(a.gold)
    examples = all_examples[: max(0, a.limit)] if a.limit is not None else all_examples
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = Path(a.out or f"data/eval/gold-{datetime.now():%Y%m%d-%H%M}")
    print(f"Gold eval: {len(examples)} examples, concurrency {a.concurrency}, gateway {a.base_url}", flush=True)
    approver = (
        InprocApprover()
        if a.transport == "inproc"
        else (Approver(a.base_url) if a.transport == "mcp" else None)
    )
    if approver is not None:
        approver.start()
    lookup, lt = make_lookup(a)
    try:
        with ThreadPoolExecutor(a.concurrency) as pool:
            rows = list(pool.map(lambda e: run_one(e, a, lookup), examples))
    finally:
        if approver is not None:
            approver.stop.set()
            approver.join(timeout=2.0)
        try:
            lt.close()
        except Exception:
            pass
    from aimo.buyer.costs import ledger_rows, summarize as cost_sum

    run_ids = [r["run_id"] for r in rows if r.get("run_id")]
    cost = cost_sum(ledger_rows(run_ids=run_ids)) if run_ids else None
    summ = summarize(rows)
    write_report(out, rows, summ, a, started, cost)
    o = summ["overall"]
    print(
        f"Zero-correction {o['zero_correction']:.0%} | escalation match {o['escalation_match']:.0%} | "
        f"allergen-safe {o['allergen_safe']:.0%} | n={o['n']} | wrote {out}/report.md",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
