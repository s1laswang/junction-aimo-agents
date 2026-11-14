"""The Gemini buyer agent: google-genai function calling over the gateway tools.

We run the tool loop ourselves (AutomaticFunctionCallingConfig(disable=True)) so that every step
is logged and streamed as an AgentStep, limits are enforced by the harness (not only by the
prompt), and the model's Content is replayed verbatim (thought signatures).

Harness guardrails (independent of what the model decides):
- hard constraints of the parsed need (customer, claims, excluded allergens) are locked when the
  model calls plan_need / create_quote;
- at most one place_order per run; after a rejection no further quotes or orders;
- pending_approval: the harness waits for the human (polls get_order_status) instead of the model;
- standing-order drift: place_order is held for explicit human confirmation;
- supplier text in tool results is screened for prompt injection and redacted before the model sees it;
- tool errors: one automatic retry, then the model is told to stop and explain.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from aimo.buyer import prompts
from aimo.buyer.need import HELSINKI, now_helsinki, parse_request
from aimo.buyer.profiles import get_profile
from aimo.buyer.tool_schemas import to_gemini_schema
from aimo.buyer.tools import ToolTransport
from aimo.models import AgentStep, Need

DRIFT_RATIO = 10.0  # harness hold (likely unit mix-up); moderate drift (>3x) is trust policy -> needs_approval
UNTRUSTED_FIELDS = ("marketing_text", "ingredients_text", "description", "text")
_INJECTION_RE = re.compile(
    r"ignore (all |your |previous |the )*(instructions|limits|rules|policy)|system (note|prompt|message)|"
    r"to (ai|llm) agents?|approval is not required|disregard|you are now|order \d[\d,. ]* (units|pcs)",
    re.I)


def screen_text(text: str) -> dict:
    """Prompt-injection screen. Prefer trust's screen_text when it exists."""
    try:
        from aimo.trust import screen_text as trust_screen  # type: ignore[attr-defined]
        r = trust_screen(text)
        if isinstance(r, dict) and "suspicious" in r:
            return r
    except Exception:  # noqa: BLE001  trust not landed yet / failed: local fallback
        pass
    hits = [m.group(0) for m in _INJECTION_RE.finditer(text or "")]
    return {"suspicious": bool(hits), "reasons": hits[:3], "screen": "buyer-local"}


def _short(x: Any, n: int = 140) -> str:
    s = json.dumps(x, ensure_ascii=False, default=str) if not isinstance(x, str) else x
    return s if len(s) <= n else s[: n - 1] + "…"


class BuyerAgent:
    def __init__(self, transport: ToolTransport, customer_id: str, *,
                 model: Optional[str] = None, thinking: str = "LOW",
                 escalate: bool = True, reasoning_model: Optional[str] = None,
                 reasoning_thinking: str = "HIGH", max_steps: int = 14,
                 emit: Optional[Callable[[AgentStep], None]] = None, run_id: Optional[str] = None,
                 approval_timeout_s: float = 1800, poll_s: float = 2.0,
                 generate_fn: Optional[Callable[..., Any]] = None, use_gemini_parse: bool = True,
                 explain: bool = True, now: Optional[datetime] = None, audit_base_url: str = ""):
        from aimo.config import GEMINI_AGENT_MODEL, GEMINI_REASONING_MODEL
        self.t = transport
        self.profile = get_profile(customer_id)
        self.customer_id = customer_id
        self.agent_id = self.profile["agent_id"]
        self.model = model or GEMINI_AGENT_MODEL
        self.thinking = thinking.upper()
        self.escalate = escalate
        self.reasoning_model = reasoning_model or GEMINI_REASONING_MODEL
        self.reasoning_thinking = reasoning_thinking.upper()
        self.max_steps = max_steps
        self.emit_fn = emit
        self.run_id = run_id or "run-" + uuid.uuid4().hex[:10]
        self.approval_timeout_s, self.poll_s = approval_timeout_s, poll_s
        self._generate = generate_fn
        self.use_gemini_parse = use_gemini_parse
        self.explain_enabled = explain
        self.now = now
        self.audit_base_url = audit_base_url.rstrip("/")
        self.steps: list[AgentStep] = []
        self._seq = 0
        # harness state
        self.need: Optional[Need] = None          # first item (compat)
        self.needs: list[Need] = []               # one per product in the request
        self.plan: Optional[dict] = None          # merged view over self.plans
        self.plans: dict[int, dict] = {}          # need index -> Plan
        self._plan_idx = 0
        self.quote: Optional[dict] = None
        self.order: Optional[dict] = None
        self.orders_submitted = 0
        self.blocked = False
        self.drift: Optional[dict] = None
        self.tool_failures: dict[str, int] = {}
        self.injections: list[dict] = []
        self.escalate_next = False
        self.escalated = False
        self.models_used: list[dict] = []
        self.t0 = time.time()

    # ------------------------------------------------------------ plumbing
    def emit(self, kind: str, title: str, **data: Any) -> AgentStep:
        self._seq += 1
        step = AgentStep(run_id=self.run_id, seq=self._seq, ts=datetime.now(HELSINKI), kind=kind,
                         title=title, data=data)
        self.steps.append(step)
        if self.emit_fn:
            try:
                self.emit_fn(step)
            except Exception:  # noqa: BLE001  a broken listener must not kill the run
                pass
        return step

    def generate(self, feature: str, model: str, contents: Any, config: Any, meta: dict | None = None):
        if self._generate is not None:
            return self._generate(feature=feature, model=model, contents=contents, config=config)
        from aimo import gemini
        return gemini.generate(feature, model, contents, config, run_id=self.run_id, meta=meta)

    def _text_call(self, feature: str, prompt: str, thinking: str = "LOW") -> str:
        from google.genai import types
        cfg = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=thinking),
                                          max_output_tokens=4096, http_options=types.HttpOptions(timeout=45_000))
        for _ in range(2):
            r = self.generate(feature, self.model, prompt, cfg, meta={"thinking_level": thinking})
            txt = (getattr(r, "text", None) or "").strip()
            if txt:
                return txt
        return ""

    # ------------------------------------------------------------ main entry
    def run(self, text: str) -> dict:
        try:
            return self._run(text)
        except Exception as e:  # noqa: BLE001
            self.emit("error", f"Run failed: {type(e).__name__}", error=str(e)[:500])
            final = (f"The purchasing run stopped because of an error ({type(e).__name__}). "
                     "Nothing further was ordered; a human should take over.")
            self.emit("final", "Run ended with an error", summary=final, status="error",
                      latency_s=round(time.time() - self.t0, 2))
            return self.result(final, status="error")

    def _run(self, text: str) -> dict:
        now = (self.now or now_helsinki()).astimezone(HELSINKI)
        self.emit("thought", "Received purchasing request", text=text, customer_id=self.customer_id,
                  agent_id=self.agent_id, transport=getattr(self.t, "kind", "?"), model=self.model,
                  thinking=self.thinking)
        t = time.time()
        self.needs = parse_request(text, self.profile, now, use_gemini=self.use_gemini_parse, run_id=self.run_id)
        self.need = n = self.needs[0]
        items = "; ".join(f"{x.quantity:g} {x.unit} {x.product_query}" for x in self.needs)
        self.emit("thought", f"Parsed need: {items}, delivery by {n.delivery_by}",
                  need=n.model_dump(mode="json"), needs=[x.model_dump(mode="json") for x in self.needs],
                  latency_s=round(time.time() - t, 2), parsed_by=n.preferences.get("parsed_by"))
        for x in self.needs:
            ratio = x.preferences.get("standing_order_ratio")
            if ratio and float(ratio) >= DRIFT_RATIO and not self.drift:
                self.drift = {"ratio": ratio, "std": x.preferences.get("standing_order_qty_per_week"), "unit": x.unit}
                self.emit("thought", f"Standing-order drift: {ratio}x the usual weekly quantity", drift=self.drift)

        mandate = self.profile["mandate_summary"]
        pol = self._tool("list_policies", {})
        if isinstance(pol, dict) and "error" not in pol:
            mandate = f"{mandate}\nFrom list_policies: {_short(pol, 1500)}"

        system = prompts.SYSTEM.format(
            customer_name=self.profile["name"], segment=self.profile["segment"], agent_id=self.agent_id,
            site=self.profile["site"], approver=self.profile["approver"],
            usual=", ".join(self.profile.get("preferred_product_ids") or []) or "none",
            allergens=", ".join(self.profile.get("excluded_allergens") or []) or "none", mandate=mandate)
        extra = prompts.DRIFT.format(**self.drift) if self.drift else ""
        if len(self.needs) > 1:
            extra += prompts.MULTI.format(n=len(self.needs))
            need_json = json.dumps([x.model_dump(mode="json") for x in self.needs], indent=1, ensure_ascii=False)
        else:
            need_json = n.model_dump_json(indent=1)
        task = prompts.TASK.format(customer_name=self.profile["name"], raw_text=text,
                                   need_json=need_json, today=now.strftime("%A %Y-%m-%d %H:%M"),
                                   delivery_by=n.delivery_by.isoformat(), extra=extra)
        final_text = self._loop(system, task)
        return self._finish(final_text)

    # ------------------------------------------------------------ the tool loop
    def _loop(self, system: str, task: str) -> str:
        from google.genai import types
        decls = []
        for spec in self.t.list_tools():
            decls.append(types.FunctionDeclaration(name=spec.name, description=spec.description[:1000],
                                                   parameters_json_schema=to_gemini_schema(spec.input_schema)))
        tools = [types.Tool(function_declarations=decls)]
        contents: list[Any] = [types.Content(role="user", parts=[types.Part(text=task)])]
        empty_retries = 0
        final_text = ""
        for step in range(self.max_steps):
            model, level = self.model, self.thinking
            if self.escalate_next:
                model, level = self.reasoning_model, self.reasoning_thinking
            cfg = types.GenerateContentConfig(
                system_instruction=system, tools=tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                thinking_config=types.ThinkingConfig(thinking_level=level), max_output_tokens=8192,
                http_options=types.HttpOptions(timeout=90_000))  # ms; a hung call is retried by aimo.gemini
            t = time.time()
            try:
                r = self.generate("buyer.agent_step", model, contents, cfg,
                                  meta={"thinking_level": level, "step": step, "escalated": model != self.model})
            except Exception as e:  # noqa: BLE001
                if model != self.model:  # escalation failed: fall back to the default model
                    self.emit("error", f"Escalation model {model} failed ({type(e).__name__}); falling back",
                              error=str(e)[:300])
                    self.escalate_next = False
                    continue
                raise
            dt = round(time.time() - t, 2)
            self.models_used.append({"step": step, "model": model, "thinking": level, "latency_s": dt})
            if model != self.model:
                self.escalate_next = False
                self.escalated = True
            content = r.candidates[0].content if getattr(r, "candidates", None) else None
            parts = list(getattr(content, "parts", None) or [])
            if not parts:
                empty_retries += 1
                if empty_retries > 2:
                    raise RuntimeError("model returned empty replies three times")
                contents.append(types.Content(role="user", parts=[types.Part(text=prompts.NUDGE)]))
                continue
            contents.append(content)  # verbatim: keeps thought signatures for the next turn
            texts = [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            if texts and calls:
                self.emit("thought", _short(" ".join(texts), 200), text=" ".join(texts), model=model,
                          thinking=level, latency_s=dt)
            if not calls:
                final_text = " ".join(texts).strip()
                if self.order is None and not self.blocked and self.quote is None and step < 2 and not final_text:
                    contents.append(types.Content(role="user", parts=[types.Part(text=prompts.NUDGE)]))
                    continue
                break
            responses = []
            for fc in calls:
                args = dict(fc.args or {})
                result = self._tool(fc.name, args, model=model, thinking=level, latency_s=dt)
                payload = result if isinstance(result, dict) else {"result": result}
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name, response=payload)))
            contents.append(types.Content(role="user", parts=responses))
        else:
            self.emit("error", f"Max steps ({self.max_steps}) reached", max_steps=self.max_steps)
        return final_text

    # ------------------------------------------------------------ tool execution with guardrails
    def _tool(self, name: str, args: dict, **meta: Any) -> Any:
        args = self._guard_args(name, args)
        if isinstance(args, dict) and args.get("__blocked__"):
            msg = args["__blocked__"]
            self.emit("tool_call", f"{name} (blocked by harness)", name=name, blocked=msg, **meta)
            if name == "place_order" and self.drift:
                self.emit("decision", "Held for human confirmation: standing-order drift",
                          status="needs_confirmation", quote_id=(self.quote or {}).get("quote_id"),
                          decision={"outcome": "needs_confirmation", "approver_role": "buyer",
                                    "hits": [{"rule_id": "buyer.standing_order_drift", "outcome": "needs_approval",
                                              "message": msg}]})
            return {"error": "blocked_by_buyer_harness", "message": msg}
        self.emit("tool_call", f"{name}({_short(args, 120)})", name=name, args=args, **meta)
        t = time.time()
        result, err = None, None
        for attempt in range(2):  # retry once
            try:
                result = self.t.call(name, args)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = e
                self.tool_failures[name] = self.tool_failures.get(name, 0) + 1
                status = getattr(e, "status", None)
                if isinstance(status, int) and status < 500:  # a business error, not infrastructure
                    body = e.to_dict() if hasattr(e, "to_dict") else {"error": str(e)[:300]}
                    self.emit("tool_result", f"{name}: refused ({body.get('error')})", name=name, result=body)
                    return body
                if attempt == 0:
                    self.emit("error", f"{name} failed ({type(e).__name__}); retrying once", name=name,
                              error=str(e)[:300])
                    time.sleep(0.5)
        dt = round(time.time() - t, 2)
        if err is not None:
            self.emit("error", f"{name} failed twice; asking the agent to explain", name=name,
                      error=str(err)[:300])
            return {"error": f"{type(err).__name__}: {str(err)[:300]}",
                    "instruction": "This tool failed twice. Do not call it again. Explain to the buyer "
                                   "what could not be done and that a human should follow up."}
        result = self._screen(name, result)
        self.emit("tool_result", self._result_title(name, result), name=name, latency_s=dt,
                  result=self._trim(result))
        return self._after(name, args, result)

    def _guard_args(self, name: str, args: dict) -> dict:
        n = self.need
        if name in ("create_quote", "place_order") and self.blocked:
            return {"__blocked__": "An order for this request was already rejected; do not retry it in another form."}
        if name == "place_order" and self.orders_submitted >= 1:
            return {"__blocked__": "One order per request: an order was already submitted. Do not split orders."}
        if name == "place_order" and self.drift:
            return {"__blocked__": (f"Quantity is {self.drift['ratio']}x the usual standing order; the human must "
                                    "confirm the quantity before any order is placed. Stop and report.")}
        if n is None:
            return args
        if name == "plan_need":
            given = args.get("need") if isinstance(args.get("need"), dict) else {}
            self._plan_idx = idx = self._match_need(given)
            locked = self.needs[idx].model_dump(mode="json") if self.needs else n.model_dump(mode="json")
            merged = {**locked, **{k: v for k, v in given.items() if k in ("product_query", "preferences")}}
            merged["preferences"] = {**locked.get("preferences", {}), **(given.get("preferences") or {})}
            # quantity/unit/date and the hard constraints may only come from the parsed need
            return {"need": merged}
        if name == "create_quote":
            args = dict(args)
            quote_need = self._quote_need()
            args.setdefault("need", quote_need)
            if isinstance(args.get("need"), dict):
                args["need"] = {**args["need"], **{k: v for k, v in quote_need.items()
                                                   if k in ("customer_id", "required_claims", "excluded_allergens", "raw_text")}}
            args.setdefault("delivery_date", n.delivery_by.isoformat())
            if self.injections and isinstance(args.get("need"), dict):
                # trust convention (docs/interfaces.md): screen results go to need.preferences
                prefs = dict(args["need"].get("preferences") or {})
                prefs["injection_flags"] = self._injection_flags()
                args["need"] = {**args["need"], "preferences": prefs}
            return self._enforce_plan_lines(args)
        return args

    def _enforce_plan_lines(self, args: dict) -> dict:
        """Quantities come from the deterministic planner, never from the LLM: the quote must carry
        exactly the plan's lines (product_id, qty_sales_units). A mismatch is corrected to the plan
        and logged as a thought, so the Plan and the Quote/Approval views always agree."""
        if not self.plans:
            return {"__blocked__": "Call plan_need first: quote lines must come from the planner."}
        unplanned = [x.product_query for i, x in enumerate(self.needs) if i not in self.plans]
        if unplanned:
            return {"__blocked__": f"Call plan_need for every item first (not planned yet: {unplanned})."}
        plan_lines = [{"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"],
                       **({"tier": l["tier"]} if l.get("tier") else {})} for l in self.plan.get("lines", [])]
        if not plan_lines:
            return {"__blocked__": "The plan has no orderable lines (full shortfall), so there is nothing to quote. "
                                   "Report the shortfall and its reasons to the buyer."}

        def key(ls):
            return sorted((str(l.get("product_id")), round(float(l.get("qty_sales_units") or 0), 4)) for l in ls)

        proposed = args.get("lines") or []
        if key(proposed) != key(plan_lines):
            self.emit("thought", "Quote lines corrected to the planner's lines (quantities never come from the model)",
                      proposed=[{"product_id": l.get("product_id"), "qty_sales_units": l.get("qty_sales_units")}
                                for l in proposed], plan_lines=plan_lines)
            args = {**args, "lines": plan_lines}
        return args

    def _match_need(self, given: dict) -> int:
        """Which parsed item a plan_need call is about: by product_query, else the next unplanned one."""
        if len(self.needs) <= 1:
            return 0
        q = str(given.get("product_query") or "").lower().strip()
        for i, x in enumerate(self.needs):
            if q and q == x.product_query.lower():
                return i
        qw = set(re.findall(r"\w+", q))
        best = max(range(len(self.needs)), key=lambda i: len(qw & set(re.findall(r"\w+", self.needs[i].product_query.lower()))))
        if qw & set(re.findall(r"\w+", self.needs[best].product_query.lower())):
            return best
        return next((i for i in range(len(self.needs)) if i not in self.plans), 0)

    def _quote_need(self) -> dict:
        """The need attached to the quote. Several items -> one combined need: hard constraints are
        the intersection of claims and the union of excluded allergens; items listed in preferences."""
        if len(self.needs) <= 1:
            return self.need.model_dump(mode="json")
        first = self.needs[0].model_dump(mode="json")
        claims = set(self.needs[0].required_claims)
        for x in self.needs[1:]:
            claims &= set(x.required_claims)
        allergens = list(dict.fromkeys(a for x in self.needs for a in x.excluded_allergens))
        return {**first, "product_query": "; ".join(x.product_query for x in self.needs),
                "required_claims": [c for c in first["required_claims"] if c in claims],
                "excluded_allergens": allergens,
                "delivery_by": min(x.delivery_by for x in self.needs).isoformat(),
                "preferences": {**first.get("preferences", {}),
                                "items": [{"product_query": x.product_query, "quantity": x.quantity, "unit": x.unit,
                                           "required_claims": x.required_claims} for x in self.needs]}}

    def _merge_plans(self) -> dict:
        ps = [self.plans[i] for i in sorted(self.plans)]
        if len(ps) == 1:
            return ps[0]
        return {"need": self._quote_need(), "lines": [l for p in ps for l in p.get("lines", [])],
                "candidates": [c for p in ps for c in p.get("candidates", [])],
                "covered_amount": sum(p.get("covered_amount") or 0 for p in ps),
                "shortfall": sum(p.get("shortfall") or 0 for p in ps),
                "total_net": round(sum(p.get("total_net") or 0 for p in ps), 2),
                "items": [{"product_query": (p.get("need") or {}).get("product_query"), "shortfall": p.get("shortfall"),
                           "total_net": p.get("total_net")} for p in ps]}

    def _screen(self, name: str, result: Any) -> Any:
        found: list[dict] = []

        def visit(x: Any, path: str) -> Any:
            if isinstance(x, dict):
                out = {}
                pid = x.get("id") or x.get("product_id")
                for k, v in x.items():
                    if k in UNTRUSTED_FIELDS and isinstance(v, str) and v:
                        s = screen_text(v)
                        if s.get("suspicious"):
                            found.append({"tool": name, "product_id": pid, "field": f"{path}.{k}",
                                          "reasons": list(s.get("reasons") or []), "excerpt": v[:160]})
                            out[k] = "[REDACTED by buyer harness: suspected prompt injection in supplier text]"
                            out["_untrusted_text_flagged"] = True
                            continue
                    out[k] = visit(v, f"{path}.{k}")
                return out
            if isinstance(x, list):
                return [visit(v, f"{path}[{i}]") for i, v in enumerate(x)]
            return x
        screened = visit(result, name)
        if found:
            self.injections += found
            ids = [f["product_id"] for f in found if f["product_id"]]
            reasons = sorted({r for f in found for r in f["reasons"]})
            self.emit("thought", f"Flagged suspected prompt injection in supplier text of {len(found)} item(s); "
                                 f"redacted before the model saw it",
                      tool=name, product_ids=ids, reasons=reasons, excerpt=found[0]["excerpt"])
        return screened

    def _injection_flags(self) -> list[str]:
        """Plain-language flags for need.preferences.injection_flags (read by trust's policy)."""
        seen, out = set(), []
        for f in self.injections:
            key = (f.get("product_id"), f.get("field", "").rsplit(".", 1)[-1])
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{f.get('product_id') or f.get('tool')} {key[1]}: {', '.join(f.get('reasons') or [])}")
        return out

    def _after(self, name: str, args: dict, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        if name == "plan_need" and "lines" in result:
            self.plans[self._plan_idx] = result
            self.plan = self._merge_plans()
            rationale = self._explain(result) if self.explain_enabled else ""
            lines = result.get("lines", [])
            flagged = (any(l.get("flags") or l.get("tier") not in ("preferred",) for l in lines)
                       or (result.get("shortfall") or 0) > 0)
            item = f" for {self.needs[self._plan_idx].product_query}" if len(self.needs) > 1 else ""
            self.emit("plan", f"Plan{item}: {len(lines)} line(s), EUR {result.get('total_net', 0):.2f}, "
                              f"shortfall {result.get('shortfall', 0):g}",
                      plan=self._trim(result), rationale=rationale, item_index=self._plan_idx)
            if flagged and self.escalate:
                self.escalate_next = True
                self.emit("thought", f"Substitution decision needed: escalating to {self.reasoning_model} "
                                     f"(thinking {self.reasoning_thinking})",
                          model=self.reasoning_model, thinking=self.reasoning_thinking)
            if rationale:
                result = {**result, "explanation": result.get("explanation") or rationale}
        elif name == "create_quote" and "quote_id" in result:
            self.quote = result
            self.emit("quote", f"Quote {result['quote_id']}: EUR {result.get('total_net', 0):.2f}, "
                               f"hash {str(result.get('quote_hash', ''))[:12]}…", quote=result)
        elif name == "place_order" and "order_id" in result:
            self.orders_submitted += 1
            self.order = result
            dec = result.get("decision") or {}
            self.emit("decision", f"Policy decision: {dec.get('outcome', result.get('status'))}",
                      order_id=result["order_id"], status=result.get("status"), decision=dec)
            if result.get("status") == "pending_approval":
                final = self._wait_for_approval(result)
                self.order = final
                result = {"order_as_submitted": result, "human_decision": final,
                          "note": "The human has decided; report the outcome. Do not place another order."}
                if final.get("status") in ("rejected", "expired", "failed"):
                    self.blocked = True
            elif result.get("status") == "rejected":
                self.blocked = True
                self.emit("order", f"Order {result['order_id']} rejected by policy", order=result)
            else:
                self.emit("order", f"Order {result['order_id']}: {result.get('status')}", order=result)
        return result

    def _wait_for_approval(self, order: dict) -> dict:
        dec = order.get("decision") or {}
        oid = order["order_id"]
        self.emit("approval_wait", f"Waiting for {dec.get('approver_role') or 'a human'} to approve order {oid}",
                  order_id=oid, approver_role=dec.get("approver_role"), quote_id=order.get("quote_id"),
                  quote_hash=order.get("quote_hash"), hits=dec.get("hits", []), poll_s=self.poll_s)
        deadline = time.time() + self.approval_timeout_s
        cur = order
        while time.time() < deadline:
            time.sleep(self.poll_s)
            try:
                cur = self.t.call("get_order_status", {"order_id": oid})
            except Exception as e:  # noqa: BLE001
                self.emit("error", f"get_order_status failed ({type(e).__name__}); still waiting", error=str(e)[:200])
                continue
            if isinstance(cur, dict) and cur.get("status") != "pending_approval":
                break
        else:
            self.emit("error", f"No human decision within {self.approval_timeout_s:.0f}s", order_id=oid)
            return {**cur, "status": cur.get("status", "pending_approval"), "timed_out": True}
        who = cur.get("approved_by") or "the approver"
        self.emit("order", f"Order {oid} {cur.get('status')} (human decision by {who})", order=cur)
        return cur

    # ------------------------------------------------------------ explanations and summary
    def _explain(self, plan: dict) -> str:
        facts = {
            "need": {k: (plan.get("need") or (self.need.model_dump(mode="json") if self.need else {})).get(k)
                     for k in ("product_query", "quantity", "unit", "delivery_by", "required_claims",
                               "excluded_allergens", "preferred_product_ids")},
            "lines": [{k: l.get(k) for k in ("product_id", "product_name", "tier", "qty_sales_units", "sales_unit",
                                             "covered_amount", "unit_price", "line_total", "reasons", "flags")}
                      for l in plan.get("lines", [])],
            "candidates": [{"id": c.get("product", {}).get("id"), "name": c.get("product", {}).get("name"),
                            "tier": c.get("tier"), "reasons": c.get("reasons"), "flags": c.get("flags"),
                            "unit_price": (c.get("offer") or {}).get("unit_price"),
                            "available_amount": (c.get("offer") or {}).get("available_amount")}
                           for c in plan.get("candidates", [])[:10]],
            "covered_amount": plan.get("covered_amount"), "shortfall": plan.get("shortfall"),
            "total_net": plan.get("total_net"),
        }
        try:
            return self._text_call("buyer.explain", prompts.EXPLAIN.format(
                plan_json=json.dumps(facts, ensure_ascii=False, default=str)))
        except Exception as e:  # noqa: BLE001
            return f"(explanation unavailable: {type(e).__name__})"

    def _audit_link(self) -> str:
        subject = (self.order or {}).get("order_id") or (self.quote or {}).get("quote_id") or self.run_id
        return f"{self.audit_base_url}/v1/audit/report/{subject}"

    def _finish(self, agent_text: str) -> dict:
        status = (self.order or {}).get("status") or ("needs_confirmation" if self.drift and self.quote else
                                                      "no_order")
        facts = {
            "request": self.need.raw_text if self.need else "",
            "needs": [x.model_dump(mode="json") for x in self.needs],
            "plan_lines": (self.plan or {}).get("lines"), "shortfall": (self.plan or {}).get("shortfall"),
            "quote": {k: (self.quote or {}).get(k) for k in ("quote_id", "total_net", "lines", "quote_hash")} if self.quote else None,
            "order_status": status,
            "policy_decision": (self.order or {}).get("decision"),
            "approved_by": (self.order or {}).get("approved_by"),
            "approval_note": (self.order or {}).get("approval_note"),
            "drift_hold": self.drift,
            "injections_flagged": self._injection_flags(),
            "agent_final_text": agent_text,
            "audit_report_link": self._audit_link(),
        }
        try:
            summary = self._text_call("buyer.summary", prompts.SUMMARY.format(
                facts_json=json.dumps(facts, ensure_ascii=False, default=str)))
        except Exception as e:  # noqa: BLE001
            summary = ""
            self.emit("error", f"Summary generation failed ({type(e).__name__})")
        summary = summary or agent_text or f"Run finished with status {status}. Audit: {facts['audit_report_link']}"
        latency = round(time.time() - self.t0, 2)
        self.emit("final", f"Done: {status}", summary=summary, status=status, agent_text=agent_text,
                  audit_report=facts["audit_report_link"], latency_s=latency, models_used=self.models_used,
                  escalated=self.escalated, injections=self.injections)
        return self.result(summary, status=status)

    def result(self, summary: str, status: str) -> dict:
        return {"run_id": self.run_id, "status": status, "summary": summary,
                "need": self.need.model_dump(mode="json") if self.need else None, "needs": [x.model_dump(mode="json") for x in self.needs],
                "quote": self.quote, "order": self.order, "plan": self.plan,
                "latency_s": round(time.time() - self.t0, 2), "models_used": self.models_used,
                "steps": [s.model_dump(mode="json") for s in self.steps]}

    # ------------------------------------------------------------ formatting helpers
    @staticmethod
    def _result_title(name: str, r: Any) -> str:
        if isinstance(r, list):
            return f"{name}: {len(r)} result(s)"
        if isinstance(r, dict) and len(r) <= 3:
            for k in ("products", "offers", "events", "policies"):
                if isinstance(r.get(k), list):
                    return f"{name}: {len(r[k])} {k}"
        if isinstance(r, dict):
            if "error" in r:
                return f"{name}: error {r['error']}"
            if name == "get_order_status":
                return f"{name}: {r.get('status')}"
            if name == "place_order":
                return f"{name}: {r.get('status')}"
            if name == "create_quote":
                return f"{name}: {r.get('quote_id')} EUR {r.get('total_net')}"
        return f"{name}: ok"

    @staticmethod
    def _trim(r: Any, n: int = 25) -> Any:
        if isinstance(r, list) and len(r) > n:
            return r[:n] + [{"_truncated": len(r) - n}]
        if isinstance(r, dict) and isinstance(r.get("candidates"), list) and len(r["candidates"]) > n:
            return {**r, "candidates": r["candidates"][:n]}
        return r


def run_agent(text: str, customer_id: str, transport: ToolTransport, **kw: Any) -> dict:
    return BuyerAgent(transport, customer_id, **kw).run(text)
