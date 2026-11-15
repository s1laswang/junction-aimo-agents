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

from datetime import date, datetime
import math
import os
from typing import Any

MOCK_MODEL_NAME = "mock-replay (no Gemini)"
MOCK_LABEL = "[MOCK MODEL: no GEMINI_API_KEY, scripted replay, not Gemini]"
_MISSING = object()


def _finite_num(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        num = float(val)
    except (OverflowError, TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _clean_id(val: Any) -> str:
    if isinstance(val, bool) or not isinstance(val, (str, int)):
        return ""
    return str(val).strip()


def _iter_seq(items: Any) -> list[Any]:
    if items is None or isinstance(items, (str, bytes, bytearray, dict)):
        return []
    if isinstance(items, (list, tuple)):
        return list(items)
    try:
        return list(items)
    except Exception:
        return []


def _plan_sort_key(key: Any) -> tuple[int, int, str]:
    if isinstance(key, int) and not isinstance(key, bool):
        return (0, key, "")
    return (1, 0, str(key))


def _need_prefs(need: Any) -> dict[str, Any]:
    if need is None:
        return {}
    prefs = need.get("preferences") if isinstance(need, dict) else getattr(need, "preferences", None)
    return prefs if isinstance(prefs, dict) else {}


def _need_has_valid_qty(need: Any) -> bool:
    if need is None or isinstance(need, (bool, int, float, str, bytes, bytearray, list, tuple, set)):
        return False
    raw = need.get("quantity", _MISSING) if isinstance(need, dict) else getattr(need, "quantity", _MISSING)
    if raw is _MISSING:
        return not isinstance(need, dict) or bool(need)
    qty = _finite_num(raw)
    return qty is not None and qty > 0


def _need_raw_text(need: Any) -> str:
    if need is None:
        return ""
    raw = need.get("raw_text") if isinstance(need, dict) else getattr(need, "raw_text", None)
    return raw if isinstance(raw, str) else ""


def _coerce_delivery_iso(val: Any) -> str | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None
        head = text[:10] if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-" else text
        try:
            return date.fromisoformat(head).isoformat()
        except ValueError:
            return None
    return None


def _need_delivery_iso(need: Any) -> str | None:
    if need is None:
        return None
    raw = need.get("delivery_by") if isinstance(need, dict) else getattr(need, "delivery_by", None)
    return _coerce_delivery_iso(raw)


def _dump_need(need: Any) -> dict[str, Any]:
    if isinstance(need, dict):
        return dict(need)
    dump_fn = getattr(need, "model_dump", None)
    if callable(dump_fn):
        dumped = dump_fn(mode="json")
        if isinstance(dumped, dict):
            return dumped
    if hasattr(need, "__dict__"):
        return {
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in vars(need).items()
            if not k.startswith("_")
        }
    return {}


def _safe_shortfall(plan: Any) -> float:
    if not isinstance(plan, dict):
        return 0.0
    val = _finite_num(plan.get("shortfall"))
    return val if val is not None and val > 0 else 0.0


def mock_wanted() -> bool:
    mode = (os.environ.get("AIMO_LLM_MODE") or "auto").strip().lower()
    if mode in ("mock", "scripted"):
        return True
    if mode == "live":
        return False
    from aimo import config
    cfg_key = getattr(config, "GEMINI_API_KEY", None)
    key = (
        (cfg_key.strip() if isinstance(cfg_key, str) else "")
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
    raw_plans = getattr(a, "plans", None)
    plans = raw_plans if isinstance(raw_plans, dict) else {}
    raw_needs = getattr(a, "needs", None)
    needs = list(raw_needs) if isinstance(raw_needs, (list, tuple)) else []
    raw: list[Any] = []
    if plans:
        for i in sorted(plans, key=_plan_sort_key):
            if (
                isinstance(i, int)
                and not isinstance(i, bool)
                and 0 <= i < len(needs)
                and _need_prefs(needs[i]).get("cancelled_by_clarification")
            ):
                continue
            plan_entry = plans[i]
            if not isinstance(plan_entry, dict):
                continue
            plan_lines = plan_entry.get("lines")
            if isinstance(plan_lines, list):
                raw.extend(plan_lines)
    else:
        raw_plan = getattr(a, "plan", None)
        if isinstance(raw_plan, dict):
            plan_lines = raw_plan.get("lines")
            if isinstance(plan_lines, list):
                raw = list(plan_lines)
    out: list[dict] = []
    by_pid: dict[str, dict] = {}
    for l in raw:
        if not isinstance(l, dict):
            continue
        raw_pid = l.get("product_id")
        pid = _clean_id(raw_pid)
        if not pid:
            continue
        raw_qty = l.get("qty_sales_units")
        qty = _finite_num(raw_qty)
        if qty is None or qty <= 0:
            continue
        qty_val: int | float = raw_qty if isinstance(raw_qty, int) and not isinstance(raw_qty, bool) else qty
        if pid in by_pid:
            total = by_pid[pid]["qty_sales_units"] + qty_val
            total_check = _finite_num(total)
            if total_check is None or total_check <= 0:
                continue
            by_pid[pid]["qty_sales_units"] = round(total, 6) if isinstance(total, float) else total
        else:
            entry_pid = pid if isinstance(raw_pid, str) else raw_pid
            entry: dict[str, Any] = {"product_id": entry_pid, "qty_sales_units": qty_val}
            tier = l.get("tier")
            if isinstance(tier, str) and tier:
                entry["tier"] = tier
            elif tier and not isinstance(tier, bool):
                entry["tier"] = tier
            by_pid[pid] = entry
            out.append(entry)
    return out


def _detail_candidate_ids(a: Any) -> list[str]:
    raw_plans = getattr(a, "plans", None)
    plans = raw_plans if isinstance(raw_plans, dict) else {}
    raw_plan = getattr(a, "plan", None)
    plan_cands = raw_plan.get("candidates") if isinstance(raw_plan, dict) else None
    if isinstance(plan_cands, list) and plan_cands:
        cands: list[Any] = list(plan_cands)
    else:
        cands = []
        for i in sorted(plans, key=_plan_sort_key):
            plan_entry = plans[i]
            if isinstance(plan_entry, dict):
                entry_cands = plan_entry.get("candidates")
                if isinstance(entry_cands, list):
                    cands.extend(entry_cands)
    need = getattr(a, "need", None)
    raw_prefs = (
        need.get("preferred_product_ids")
        if isinstance(need, dict)
        else getattr(need, "preferred_product_ids", None)
    )
    pref_ids = {
        pid
        for x in (raw_prefs if isinstance(raw_prefs, (list, tuple, set)) else [])
        if (pid := _clean_id(x))
    }
    plan_lines = _collect_plan_lines(a)
    line_ids = {pid for l in plan_lines if (pid := _clean_id(l.get("product_id")))}
    buckets: tuple[list[str], ...] = ([], [], [], [], [])
    seen: set[str] = set()
    for c in cands:
        if not isinstance(c, dict):
            continue
        prod = c.get("product")
        pid = (
            (_clean_id(prod.get("id")) if isinstance(prod, dict) else "")
            or _clean_id(c.get("product_id"))
            or _clean_id(c.get("id"))
        )
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
    for l in plan_lines:
        pid = _clean_id(l.get("product_id"))
        if pid and pid not in ordered:
            ordered.append(pid)
    return ordered[:2]


def _inspected_products(contents: Any) -> set[str]:
    out: set[str] = set()
    for c in _iter_seq(contents):
        for p in _iter_seq(getattr(c, "parts", None)):
            fc = getattr(p, "function_call", None)
            if getattr(fc, "name", None) == "get_product":
                fc_args = getattr(fc, "args", None)
                if isinstance(fc_args, dict):
                    pid = _clean_id(fc_args.get("product_id"))
                    if pid:
                        out.add(pid)
            fr = getattr(p, "function_response", None)
            if getattr(fr, "name", None) == "get_product":
                fr_resp = getattr(fr, "response", None)
                if isinstance(fr_resp, dict):
                    pid = _clean_id(fr_resp.get("id")) or _clean_id(fr_resp.get("product_id"))
                    if pid:
                        out.add(pid)
    return out


def _tool_errors(contents: Any) -> list[str]:
    out: list[str] = []
    for c in _iter_seq(contents):
        for p in _iter_seq(getattr(c, "parts", None)):
            fr = getattr(p, "function_response", None)
            fr_resp = getattr(fr, "response", None)
            if isinstance(fr_resp, dict):
                err = (
                    fr_resp.get("error")
                    or (
                        fr_resp.get("detail")
                        if fr_resp.get("detail")
                        and "lines" not in fr_resp
                        and "quote_id" not in fr_resp
                        and "order_id" not in fr_resp
                        else None
                    )
                    or ("tool_error" if fr_resp.get("isError") else None)
                )
                if err:
                    fr_name = getattr(fr, "name", None) or "unknown_tool"
                    item = f"{fr_name}: {err}"
                    if item not in out:
                        out.append(item)
    return out


_REPLAN_ERRORS = ("lines_unavailable", "lot_size_mismatch", "fractional_quantity")


class MockModel:
    """Drop-in for BuyerAgent(generate_fn=...). Reads the agent's harness state, never invents
    quantities: the harness locks the need and corrects quote lines to the planner's lines."""

    def __init__(self, agent: Any):
        self.agent = agent
        self.calls: list[str] = []
        self._last_errors: list[str] = []
        self._replan_started = False

    def __call__(self, feature: str, model: str, contents: Any, config: Any = None):
        self.calls.append(feature)
        a = self.agent
        if isinstance(contents, str):
            return _text(self._summary(feature))
        self._last_errors = _tool_errors(contents)
        raw_order = getattr(a, "order", None)
        order = raw_order if isinstance(raw_order, dict) else {}
        if (
            getattr(a, "blocked", False)
            or order.get("status") in ("placed", "approved", "rejected", "cancelled", "expired", "failed")
        ):
            return _text(self._summary("buyer.agent_step"))
        raw_needs = getattr(a, "needs", None)
        needs = list(raw_needs) if isinstance(raw_needs, (list, tuple)) else []
        if needs and all(_need_prefs(x).get("cancelled_by_clarification") for x in needs):
            return _text(self._summary("buyer.agent_step"))
        active = [
            (i, need)
            for i, need in enumerate(needs)
            if not _need_prefs(need).get("cancelled_by_clarification")
        ]
        raw_need = getattr(a, "need", None)
        if any(not _need_has_valid_qty(x) for _, x in active):
            return _text(self._summary("buyer.agent_step"))
        if not active and raw_need is not None and not _need_has_valid_qty(raw_need):
            return _text(self._summary("buyer.agent_step"))
        raw_plans = getattr(a, "plans", None)
        plans = raw_plans if isinstance(raw_plans, dict) else {}
        raw_quote = getattr(a, "quote", None)
        frs = [
            fr
            for c in _iter_seq(contents)
            for p in _iter_seq(getattr(c, "parts", None))
            if (fr := getattr(p, "function_response", None)) is not None and getattr(fr, "name", None)
        ]
        done = [str(getattr(fr, "name", "")) for fr in frs]
        plan_failed = any(err.startswith("plan_need:") for err in self._last_errors)
        if not plan_failed and "create_quote" not in done:
            for seq, (i, need) in enumerate(active):
                if i not in plans and done.count("plan_need") <= seq:
                    return _call("plan_need", {"need": _dump_need(need)})
        quote_idxs = [idx for idx, fr in enumerate(frs) if getattr(fr, "name", None) == "create_quote"]
        replan_needed = bool(getattr(a, "_replan_needed", False))
        if len(quote_idxs) == 1 and raw_quote is None:
            q_resp = getattr(frs[quote_idxs[0]], "response", None)
            q_err = q_resp.get("error") if isinstance(q_resp, dict) else None
            if replan_needed or q_err in _REPLAN_ERRORS:
                self._replan_started = True
        replan_items = [need for _, need in active] or ([raw_need] if raw_need is not None else [])
        replanned = (
            sum(1 for fr in frs[quote_idxs[0] + 1 :] if getattr(fr, "name", None) == "plan_need")
            if len(quote_idxs) == 1
            else 0
        )
        if not plan_failed and len(quote_idxs) == 1 and raw_quote is None and self._replan_started:
            if replanned < len(replan_items):
                return _call("plan_need", {"need": _dump_need(replan_items[replanned])})
        lines = _collect_plan_lines(a)
        raw_texts = (
            " ".join(_need_raw_text(x) for _, x in active)
            if active
            else _need_raw_text(raw_need)
        ).lower()
        if not plan_failed and raw_quote is None and "create_quote" not in done and any(
            k in raw_texts for k in ("detail", "alternative", "ingredient", "ainesos", "innehåll", "ingrediens")
        ):
            inspected = _inspected_products(contents)
            n_get = done.count("get_product")
            if n_get == 0 or (n_get < 2 and inspected):
                for pid in _detail_candidate_ids(a):
                    if pid not in inspected:
                        return _call("get_product", {"product_id": pid})
        if plan_failed or (active and any(i not in plans for i, _ in active)) or (raw_quote is None and not lines):
            return _text(self._summary("buyer.agent_step"))
        can_quote = "create_quote" not in done or (
            len(quote_idxs) == 1
            and self._replan_started
            and not replan_needed
            and replanned >= len(replan_items)
        )
        if raw_quote is None and can_quote:
            if active:
                dates = [d for _, x in active if (d := _need_delivery_iso(x)) is not None]
                delivery_date = min(dates) if dates else _need_delivery_iso(raw_need)
            else:
                delivery_date = _need_delivery_iso(raw_need)
            return _call("create_quote", {"delivery_date": delivery_date, "lines": lines})
        quote_id = raw_quote.get("quote_id") if isinstance(raw_quote, dict) else None
        quote_hash = raw_quote.get("quote_hash") if isinstance(raw_quote, dict) else None
        if (
            isinstance(quote_id, str)
            and quote_id.strip()
            and isinstance(quote_hash, str)
            and quote_hash.strip()
            and "place_order" not in done
            and not getattr(a, "blocked", False)
        ):
            return _call("place_order", {"quote_id": quote_id.strip(), "quote_hash": quote_hash.strip()})
        return _text(self._summary("buyer.agent_step"))

    def _summary(self, feature: str) -> str:
        a = self.agent
        if feature == "buyer.explain":
            return f"{MOCK_LABEL} Plan from the deterministic planner; see the reasons on each line."
        raw_order = getattr(a, "order", None)
        order = raw_order if isinstance(raw_order, dict) else {}
        drift = getattr(a, "drift", None)
        raw_quote = getattr(a, "quote", None)
        q = raw_quote if isinstance(raw_quote, dict) else {}
        raw_status = order.get("status")
        status = (
            raw_status.strip()
            if isinstance(raw_status, str) and raw_status.strip()
            else ("needs_confirmation" if drift and q else "no_order")
        )
        raw_plans = getattr(a, "plans", None)
        plans = raw_plans if isinstance(raw_plans, dict) else {}
        if plans:
            shortfall = 0.0
            for i in sorted(plans, key=_plan_sort_key):
                sf = _safe_shortfall(plans[i])
                if _finite_num(shortfall + sf) is not None:
                    shortfall += sf
        else:
            shortfall = _safe_shortfall(getattr(a, "plan", None))
        lines = _collect_plan_lines(a)
        raw_needs = getattr(a, "needs", None)
        needs = list(raw_needs) if isinstance(raw_needs, (list, tuple)) else []
        cancelled = bool(needs) and all(
            _need_prefs(x).get("cancelled_by_clarification") for x in needs
        )
        raw_need = getattr(a, "need", None)
        check_needs = needs if needs else ([raw_need] if raw_need is not None else [])
        non_pos = any(
            bool(_need_prefs(x).get("non_positive_quantity")) or not _need_has_valid_qty(x)
            for x in check_needs
        )
        bits = [f"status {status}"]
        if q:
            total_net = _finite_num(q.get("total_net"))
            total_val = total_net if total_net is not None else 0.0
            bits.append(f"quote {q.get('quote_id')} EUR {total_val:.2f}")
        if shortfall > 0:
            bits.append(f"shortfall {shortfall:g}")
        if non_pos:
            bits.append("non-positive quantity rejected")
        if cancelled:
            bits.append("cancelled per buyer clarification")
        elif not q and not lines and not non_pos:
            bits.append("no orderable lines")
        errs = [str(e) for e in self._last_errors[:2] if e] if isinstance(self._last_errors, list) else []
        if not order and status != "needs_confirmation" and errs:
            bits.append(f"tool error ({', '.join(errs)})")
        raw_who = order.get("approved_by")
        who = raw_who.strip() if isinstance(raw_who, str) else ""
        if status == "rejected":
            bits.append(f"rejected by {who}" if who else "rejected by policy")
        elif status == "cancelled":
            bits.append("order cancelled")
        elif order.get("timed_out"):
            bits.append("approval timed out")
        elif who:
            bits.append(f"approved by {who}")
        elif status == "needs_confirmation":
            bits.append("held for human confirmation")
        if order.get("response_code") == "CA":
            bits.append("partial confirmation (Peppol CA)")
        if getattr(a, "injections", None):
            bits.append("suspicious supplier text was detected and ignored")
        req_fn = getattr(a, "_request_injection_flags", None)
        if callable(req_fn):
            try:
                has_req_inj = bool(req_fn())
            except Exception:
                has_req_inj = False
            if has_req_inj:
                bits.append("suspicious instructions in buyer request were ignored")
        return f"{MOCK_LABEL} " + "; ".join(bits) + "."
