"""MOCK model for running the end-to-end demo with no Gemini API key. NOT Gemini.

When neither GEMINI_API_KEY nor GOOGLE_API_KEY is set (or AIMO_LLM_MODE=mock/scripted), BuyerAgent
uses MockModel instead of Gemini. It replays the fixed tool sequence the real agent follows
(plan_need for every parsed item -> create_quote -> place_order) through the SAME harness, so the
gateway, policies, signing, approvals and audit trail are all real; only the "LLM" is scripted.
Need parsing uses the deterministic parser (aimo.buyer.need, use_gemini=False). Every text the
mock writes starts with MOCK_LABEL, and the agent's model name becomes MOCK_MODEL_NAME, so a mock
run can never be mistaken for a Gemini run (UI, CLI, audit and run JSON all show it).

AIMO_LLM_MODE: "auto" (default: mock only when no key), "mock" or "scripted" (always), "live"
(never; a missing key then fails as before).
"""
from __future__ import annotations

import os
from typing import Any

MOCK_MODEL_NAME = "mock-replay (no Gemini)"
MOCK_LABEL = "[MOCK MODEL: no GEMINI_API_KEY, scripted replay, not Gemini]"


def mock_wanted() -> bool:
    mode = os.environ.get("AIMO_LLM_MODE", "auto").strip().lower()
    if mode in ("mock", "scripted"):
        return True
    if mode == "live":
        return False
    from aimo import config
    key = (
        (config.GEMINI_API_KEY or "").strip()
        or (os.environ.get("GEMINI_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    return not key


def _resp(parts: list[Any]):
    from google.genai import types
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


def _call(name: str, args: dict):
    from google.genai import types
    return _resp([types.Part(function_call=types.FunctionCall(name=name, args=args))])


def _text(text: str):
    from google.genai import types
    return _resp([types.Part(text=text)])


def _collect_plan_lines(a: Any) -> list[dict]:
    plans = getattr(a, "plans", None) or {}
    raw = (
        [l for i in sorted(plans) for l in (plans[i] or {}).get("lines", [])]
        if plans
        else list((getattr(a, "plan", None) or {}).get("lines", []))
    )
    out: list[dict] = []
    by_pid: dict[str, dict] = {}
    for l in raw:
        pid = str(l["product_id"])
        if pid in by_pid:
            total = by_pid[pid]["qty_sales_units"] + l["qty_sales_units"]
            by_pid[pid]["qty_sales_units"] = round(total, 6) if isinstance(total, float) else total
        else:
            entry: dict[str, Any] = {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
            if l.get("tier"):
                entry["tier"] = l["tier"]
            by_pid[pid] = entry
            out.append(entry)
    return out


def _detail_candidate_ids(a: Any) -> list[str]:
    plans = getattr(a, "plans", None) or {}
    cands = (
        (getattr(a, "plan", None) or {}).get("candidates")
        or [c for i in sorted(plans) for c in (plans[i] or {}).get("candidates", [])]
    )
    need = getattr(a, "need", None)
    pref_ids = {str(x) for x in (need.preferred_product_ids if need else [])}
    line_ids = {str(l.get("product_id")) for l in _collect_plan_lines(a) if l.get("product_id")}
    buckets: tuple[list[str], ...] = ([], [], [], [], [])
    seen: set[str] = set()
    for c in cands:
        pid = str((c.get("product") or {}).get("id") or c.get("product_id") or c.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        tier = c.get("tier")
        if tier == "rejected":
            buckets[4].append(pid)
        elif tier == "preferred" or pid in pref_ids:
            buckets[3].append(pid)
        elif tier in ("better", "good") and pid not in line_ids:
            buckets[0].append(pid)
        elif pid in line_ids:
            buckets[1].append(pid)
        else:
            buckets[2].append(pid)
    ordered: list[str] = []
    if buckets[0]:
        ordered.append(buckets[0][0])
    if buckets[1]:
        ordered.append(buckets[1][0])
    for b in buckets:
        for pid in b:
            if pid not in ordered:
                ordered.append(pid)
    return ordered[:2]


def _inspected_products(contents: Any) -> set[str]:
    out: set[str] = set()
    for c in contents:
        for p in c.parts or []:
            fc = getattr(p, "function_call", None)
            if fc and fc.name == "get_product":
                pid = (fc.args or {}).get("product_id")
                if pid:
                    out.add(str(pid))
            fr = getattr(p, "function_response", None)
            if fr and fr.name == "get_product" and isinstance(fr.response, dict):
                pid = fr.response.get("id") or fr.response.get("product_id")
                if pid:
                    out.add(str(pid))
    return out


def _tool_errors(contents: Any) -> list[str]:
    if isinstance(contents, str) or not contents:
        return []
    out: list[str] = []
    for c in contents:
        for p in getattr(c, "parts", None) or []:
            fr = getattr(p, "function_response", None)
            if fr and isinstance(fr.response, dict) and fr.response.get("error"):
                item = f"{fr.name}: {fr.response['error']}"
                if item not in out:
                    out.append(item)
    return out


class MockModel:
    """Drop-in for BuyerAgent(generate_fn=...). Reads the agent's harness state, never invents
    quantities: the harness locks the need and corrects quote lines to the planner's lines."""

    def __init__(self, agent: Any):
        self.agent = agent
        self.calls: list[str] = []
        self._last_errors: list[str] = []

    def __call__(self, feature: str, model: str, contents: Any, config: Any = None):
        self.calls.append(feature)
        a = self.agent
        if isinstance(contents, str):
            return _text(self._summary(feature))
        self._last_errors = _tool_errors(contents)
        done = [p.function_response.name for c in contents for p in (c.parts or []) if p.function_response]
        for i, need in enumerate(a.needs):
            if i not in a.plans and done.count("plan_need") <= i:
                return _call("plan_need", {"need": need.model_dump(mode="json")})
        lines = _collect_plan_lines(a)
        if a.quote is None and a.need and any(
            k in (a.need.raw_text or "").lower() for k in ("detail", "alternative", "ingredient")
        ):
            inspected = _inspected_products(contents)
            n_get = done.count("get_product")
            if n_get == 0 or (n_get < 2 and inspected):
                for pid in _detail_candidate_ids(a):
                    if pid not in inspected:
                        return _call("get_product", {"product_id": pid})
        if (a.needs and len(a.plans) < len(a.needs)) or (a.quote is None and not lines):
            return _text(self._summary("buyer.agent_step"))
        if a.quote is None and "create_quote" not in done:
            delivery_date = (
                min(x.delivery_by for x in a.needs).isoformat()
                if a.needs
                else (a.need.delivery_by.isoformat() if a.need else None)
            )
            return _call("create_quote", {"delivery_date": delivery_date, "lines": lines})
        if a.quote is not None and "place_order" not in done and not a.blocked:
            return _call("place_order", {"quote_id": a.quote["quote_id"], "quote_hash": a.quote["quote_hash"]})
        return _text(self._summary("buyer.agent_step"))

    def _summary(self, feature: str) -> str:
        a = self.agent
        if feature == "buyer.explain":
            return f"{MOCK_LABEL} Plan from the deterministic planner; see the reasons on each line."
        status = (a.order or {}).get("status") or ("needs_confirmation" if a.drift and a.quote else "no_order")
        q = a.quote or {}
        plans = getattr(a, "plans", None) or {}
        shortfall = (
            sum((plans[i] or {}).get("shortfall") or 0 for i in sorted(plans))
            if plans
            else ((a.plan or {}).get("shortfall") or 0)
        )
        lines = _collect_plan_lines(a)
        bits = [f"status {status}"]
        if q:
            bits.append(f"quote {q.get('quote_id')} EUR {q.get('total_net', 0):.2f}")
        if shortfall > 0:
            bits.append(f"shortfall {shortfall:g}")
        if not q and not lines:
            bits.append("no orderable lines")
        if not q and self._last_errors:
            bits.append(f"tool error ({', '.join(self._last_errors[:2])})")
        if (a.order or {}).get("approved_by"):
            bits.append(f"approved by {a.order['approved_by']}")
        if a.injections:
            bits.append("suspicious supplier text was detected and ignored")
        return f"{MOCK_LABEL} " + "; ".join(bits) + "."
