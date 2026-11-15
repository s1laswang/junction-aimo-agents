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
import math
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

DRIFT_RATIO = 10.0
EXTRA_STEP_KINDS = {"needs_clarification"}
PLANNER_PREF_KEYS = {"brand", "fat_pct", "organic", "pack", "use"}
REPLAN_QUOTE_ERRORS = ("lines_unavailable", "lot_size_mismatch", "fractional_quantity")
UNTRUSTED_FIELDS = ("marketing_text", "ingredients_text", "description", "text")


def _valid_quantity(q: Any) -> bool:
    if isinstance(q, bool) or not isinstance(q, (int, float)):
        return False
    try:
        qf = float(q)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(qf) and qf > 0
_INJECTION_RE = re.compile(
    r"ignore (all |your |previous |the )*(instructions|limits|rules|policy)|system (note|prompt|message)|"
    r"to (ai|llm) agents?|approval is not required|disregard|you are now|order \d[\d,. ]* (units|pcs)",
    re.I)
_BUYER_INJ_RE = re.compile(
    r"\b(?:system\s*(?:prompt|note|message|override|instruction|mode)\b"
    r"|(?:system|developer|admin|assistant)\s*:"
    r"|(?:developer|admin)\s*(?:prompt|note|message|override|instruction|mode)\b"
    r"|(?:note|message|attention|p\.?s\.?)\s+(?:to|for)\s+(?:ai|a\.i\.|llm|gpt|gemini|claude|assistant|agent|bot|automated|buying|purchasing|buyer)\b"
    r"|to\s+(?:ai|llm)\s+agents?\b"
    r"|new\s+instructions?\b"
    r"|(?:ignore|disregard|bypass|override|forget)\s+(?:all\s+|your\s+|previous\s+|prior\s+|the\s+|any\s+|these\s+)*(?:instructions?|limits?|rules?|polic\w*|allerg\w*|guardrails?|checks?|budget|restrictions?|approv\w*)"
    r"|approval\s+is\s+not\s+required\b"
    r"|you\s+are\s+now\b)",
    re.I,
)


def _screen_fast(text: str) -> dict:
    txt = text or ""
    try:
        from aimo.trust.screening import screen_text as trust_screen
        r = trust_screen(txt, use_gemini=False)
        if isinstance(r, dict) and r.get("suspicious"):
            return r
    except Exception:
        pass
    hits = [m.group(0)[:80] for m in _BUYER_INJ_RE.finditer(txt)]
    if hits:
        return {"suspicious": True, "reasons": hits[:3], "strong_reasons": hits[:3], "screen": "buyer-regex"}
    hits_fb = [m.group(0)[:80] for m in _INJECTION_RE.finditer(txt)]
    if hits_fb and not re.fullmatch(r"order\s+[\d,. ]+\s+(?:units|pcs)", hits_fb[0], re.I):
        return {"suspicious": True, "reasons": hits_fb[:3], "strong_reasons": hits_fb[:3], "screen": "buyer-local"}
    return {"suspicious": False, "reasons": [], "strong_reasons": []}


def _strip_request_injections(text: str) -> str:
    if not text or not _screen_fast(text).get("suspicious"):
        return text
    cleaned = text
    for pat in (r"\[[^\[\]]+\]", r"\([^\(\)]+\)", r"\{[^\{\}]+\}", r"<[^<>]+>"):
        cleaned = re.sub(pat, lambda m: " " if _screen_fast(m.group(0)).get("suspicious") else m.group(0), cleaned)
    strong_rxs: list[Any] = []
    try:
        from aimo.trust import screening as ts
        strong_rxs = [rx for _, rx in getattr(ts, "_S", [])]
    except Exception:
        strong_rxs = []
    kept: list[str] = []
    for seg in re.split(r"(?<!\d)[.;!?\n]+(?!\d)", cleaned):
        s = re.sub(r"\s+", " ", seg).strip()
        if not s:
            continue
        if not _screen_fast(s).get("suspicious"):
            kept.append(s)
            continue
        cut = len(s)
        m0 = _BUYER_INJ_RE.search(s)
        if m0:
            cut = min(cut, m0.start())
        for rx in strong_rxs:
            ms = rx.search(s)
            if ms:
                cut = min(cut, ms.start())
        prefix = s[:cut].strip(" -,:;\t")
        if prefix and not _screen_fast(prefix).get("suspicious"):
            kept.append(prefix)
    out = ". ".join(kept).strip()
    return out


def _extract_quote_patch(candidate: Any, *, prefix: str = "") -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    out: dict[str, Any] = {}
    for k in ("quote_id", "quote_hash"):
        v = candidate.get(f"{prefix}{k}")
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    lines = candidate.get(f"{prefix}lines")
    if isinstance(lines, list) and lines and all(isinstance(x, dict) for x in lines):
        out["lines"] = [dict(x) for x in lines]
    for k in ("total_net", "total_gross"):
        v = candidate.get(f"{prefix}{k}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    for k in ("delivery_date", "expires_at"):
        v = candidate.get(f"{prefix}{k}")
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, str) and v.strip():
            out[k] = v.strip()
    if not prefix and "signature" in candidate:
        for k in ("signature", "kid", "created_at", "currency", "provenance", "availability_snapshot", "need", "customer_id", "agent_id"):
            if candidate.get(k) is not None:
                out[k] = candidate[k]
    return out


def screen_text(text: str) -> dict:
    """Prompt-injection screen. Prefer trust's screen_text when it exists."""
    try:
        from aimo.trust import screen_text as trust_screen
        r = trust_screen(text)
        if isinstance(r, dict) and "suspicious" in r:
            return r
    except Exception:
        pass
    hits = [m.group(0) for m in _INJECTION_RE.finditer(text or "")]
    return {"suspicious": bool(hits), "reasons": hits[:3], "screen": "buyer-local"}


_TIER_ORDER = ["preferred", "best", "better", "good", "rejected"]


def _rs(reasons: Any, n: int = 2, m: int = 110) -> list[str]:
    return [str(r)[:m] for r in (reasons or [])[:n]]


def _pick(d: dict, keys: tuple) -> dict:
    return {k: d[k] for k in keys if k in d and d[k] not in (None, [], {}, "")}


def _compact_order(o: Any) -> Any:
    if not isinstance(o, dict) or "order_id" not in o:
        return o
    out = _pick(o, ("order_id", "quote_id", "quote_hash", "status", "approved_by", "approver_role",
                    "approval_note", "response_code", "response_lines", "timed_out"))
    dec = o.get("decision") or {}
    out["decision"] = {**_pick(dec, ("outcome", "approver_role")),
                       "hits": [{"rule_id": h.get("rule_id"), "outcome": h.get("outcome"), "message": str(h.get("message", ""))[:160]}
                                for h in dec.get("hits", []) if h.get("outcome") != "allow"]}
    return out


def compact_for_model(name: str, result: Any) -> Any:
    """What the MODEL sees of a tool result (replayed on every later turn, so it dominates input
    tokens). The full result still goes to the AgentStep stream for the UI and audit.
    Keeps every id, hash, price, quantity, flag and reason the agent needs to decide."""
    if not isinstance(result, (dict, list)) or (isinstance(result, dict) and "error" in result):
        return result
    if name == "search_products":
        items = result.get("products", []) if isinstance(result, dict) else result
        return {"products": [_pick(p, ("id", "name", "brand", "sales_unit", "content_amount", "content_unit",
                                        "claims", "delivery_reliability", "_untrusted_text_flagged"))
                             for p in items[:12]], "total_returned": len(items)}
    if name == "get_offers":
        items = result.get("offers", []) if isinstance(result, dict) else result
        return {"offers": [_pick(o, ("product_id", "available", "available_amount", "unit_price", "comparison_price",
                                     "comparison_unit", "sales_unit", "closing_time", "cutoff_passed",
                                     "logistic_model", "issues")) for o in items]}
    if name == "get_product" and isinstance(result, dict):
        out = _pick(result, ("id", "name", "brand", "sales_unit", "content_amount", "content_unit", "claims",
                             "allergens", "country_of_origin", "delivery_reliability", "_untrusted_text_flagged"))
        for k in ("ingredients_text", "marketing_text"):
            if result.get(k):
                out[k] = str(result[k])[:300]
        return out
    if name == "plan_need" and isinstance(result, dict) and "lines" in result:
        lines = [{**_pick(l, ("product_id", "product_name", "tier", "qty_sales_units", "sales_unit", "covered_amount",
                              "unit_price", "line_total", "flags", "injection_screen")),
                  "reasons": _rs(l.get("reasons"))}
                 for l in result.get("lines", [])]
        by_tier: dict[str, list] = {}
        for cd in result.get("candidates", []):
            by_tier.setdefault(cd.get("tier", "?"), []).append(cd)
        cands = []
        for t in _TIER_ORDER + [t for t in by_tier if t not in _TIER_ORDER]:
            for cd in by_tier.get(t, [])[:3]:
                p, o = cd.get("product") or {}, cd.get("offer") or {}
                item = {"id": p.get("id"), "name": p.get("name"), "tier": t,
                        **_pick(o, ("unit_price", "available_amount", "cutoff_passed")),
                        "flags": cd.get("flags") or [], "reasons": _rs(cd.get("reasons"))}
                if cd.get("injection_screen"):
                    item["injection_screen"] = cd.get("injection_screen")
                cands.append(item)
        return {"lines": lines, "covered_amount": result.get("covered_amount"), "shortfall": result.get("shortfall"),
                "total_net": result.get("total_net"), "explanation": str(result.get("explanation") or "")[:400],
                "candidates_top3_per_tier": cands, "candidates_total": len(result.get("candidates", []))}
    if name == "create_quote" and isinstance(result, dict) and "quote_id" in result:
        return {**_pick(result, ("quote_id", "quote_hash", "total_net", "total_gross", "delivery_date", "expires_at")),
                "lines": [_pick(l, ("product_id", "product_name", "qty_sales_units", "sales_unit", "unit_price",
                                    "line_total", "tier", "flags")) for l in result.get("lines", [])]}
    if name in ("place_order", "get_order_status") and isinstance(result, dict):
        if "human_decision" in result:
            return {"order_as_submitted": _compact_order(result.get("order_as_submitted")),
                    "human_decision": _compact_order(result.get("human_decision")), "note": result.get("note")}
        return _compact_order(result)
    if name == "get_audit_trail" and isinstance(result, dict):
        return {"events": len(result.get("events") or []), "verify": result.get("verify"),
                "report": str(result.get("report") or "")[:1500]}
    return result


def short_description(desc: str, limit: int = 320) -> str:
    """Tool descriptions are sent on every step. Keep the first paragraph (MCP docstrings add Args/
    Returns sections that duplicate the parameter schema), cut at a sentence end within `limit`."""
    d = (desc or "").strip()
    d = re.split(r"\n\s*\n|\n\s*(?:Args|Returns|Parameters):", d, maxsplit=1)[0]
    d = re.sub(r"\s+", " ", d).strip()
    if len(d) <= limit:
        return d
    cut = d[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


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
                 explain: bool = True, now: Optional[datetime] = None, audit_base_url: str = "",
                 answer_fn: Optional[Callable[[dict], Optional[dict]]] = None, clarify: bool = True,
                 image: Optional[tuple[bytes, str]] = None):
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
        self.mock = False
        if generate_fn is None:
            from aimo.buyer.mock_model import MOCK_MODEL_NAME, MockModel, mock_wanted
            if mock_wanted():
                self.mock = True
                self._generate = MockModel(self)
                self.use_gemini_parse = False
                self.model = self.reasoning_model = MOCK_MODEL_NAME
                self.escalate = False
        self.explain_enabled = explain
        self.answer_fn = answer_fn
        self.clarify_enabled = clarify
        self.image = image
        self.clarifications: list[dict] = []
        self.now = now
        self.audit_base_url = audit_base_url.rstrip("/")
        self.steps: list[AgentStep] = []
        self._seq = 0
        self.need: Optional[Need] = None
        self.needs: list[Need] = []
        self.plan: Optional[dict] = None
        self.plans: dict[int, dict] = {}
        self._plan_idx = 0
        self._replan_needed = False
        self._replan_pending: set[int] = set()
        self.quote: Optional[dict] = None
        self.order: Optional[dict] = None
        self.orders_submitted = 0
        self.done = False
        self.blocked = False
        self.drift: Optional[dict] = None
        self.tool_failures: dict[str, int] = {}
        self.injections: list[dict] = []
        self._tool_injections: list[tuple[str, str]] = []
        self._suspicious_queries: list[str] = []
        self.escalate_next = False
        self.escalated = False
        self.models_used: list[dict] = []
        self.t0 = time.time()

    def emit(self, kind: str, title: str, **data: Any) -> AgentStep:
        self._seq += 1
        fields = dict(run_id=self.run_id, seq=self._seq, ts=datetime.now(HELSINKI), kind=kind, title=title, data=data)
        if kind in EXTRA_STEP_KINDS:
            step = AgentStep.model_construct(**fields)
        else:
            step = AgentStep(**fields)
        self.steps.append(step)
        if self.emit_fn:
            try:
                self.emit_fn(step)
            except Exception:
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

    def run(self, text: str) -> dict:
        try:
            return self._run(text)
        except Exception as e:
            self.emit("error", f"Run failed: {type(e).__name__}", error=str(e)[:500])
            final = (f"The purchasing run stopped because of an error ({type(e).__name__}). "
                     "Nothing further was ordered; a human should take over.")
            self.emit("final", "Run ended with an error", summary=final, status="error",
                      latency_s=round(time.time() - self.t0, 2))
            return self.result(final, status="error")

    def _reject_invalid_quantities(self) -> dict:
        self.need = None
        self.needs = []
        msg = "Requested quantity must be a finite number greater than zero; nothing was ordered."
        self.emit("error", "Invalid quantity in purchasing request", error=msg)
        if self.mock:
            from aimo.buyer.mock_model import MOCK_LABEL
            summary = f"{MOCK_LABEL} status error; non-positive quantity rejected. {msg}"
        else:
            summary = msg
        self.emit("final", "Run ended with an error", summary=summary, status="error",
                  latency_s=round(time.time() - self.t0, 2), models_used=self.models_used,
                  escalated=False, injections=[])
        return self.result(summary, status="error")

    def _run(self, text: str) -> dict:
        now = (self.now or now_helsinki()).astimezone(HELSINKI)
        self.emit("thought", "Received purchasing request", text=text, customer_id=self.customer_id,
                  agent_id=self.agent_id, transport=getattr(self.t, "kind", "?"), model=self.model,
                  thinking=self.thinking, has_image=bool(self.image), mock=self.mock)
        if self.mock:
            from aimo.buyer.mock_model import MOCK_LABEL
            self.emit("thought", f"{MOCK_LABEL} Running the demo without Gemini", mock=True)
        if self.image and not self.mock:
            text = self._read_image(text, now)
        elif self.image and self.mock:
            from aimo.buyer.mock_model import MOCK_LABEL
            if (text or "").strip():
                self.emit("thought", f"{MOCK_LABEL} Image OCR requires GEMINI_API_KEY; using typed request text",
                          mock=True, has_image=True)
            else:
                self.emit("error", "Image OCR requires GEMINI_API_KEY",
                          error="Cannot extract an order from an image without GEMINI_API_KEY or accompanying text.",
                          mock=True)
                summary = (f"{MOCK_LABEL} Reading an order from a photo requires GEMINI_API_KEY or accompanying "
                           "request text. Nothing was ordered.")
                self.emit("final", "Done: no_order", summary=summary, status="no_order",
                          latency_s=round(time.time() - self.t0, 2), models_used=self.models_used,
                          escalated=False, injections=[])
                return self.result(summary, status="no_order")
        t = time.time()
        parse_text = _strip_request_injections(text)
        parsed = parse_request(parse_text, self.profile, now, use_gemini=self.use_gemini_parse, run_id=self.run_id)
        if not parsed or any(not _valid_quantity(getattr(x, "quantity", None)) for x in parsed):
            return self._reject_invalid_quantities()
        self.needs = [
            x.model_copy(update={"raw_text": text})
            for x in parsed
        ]
        self.need = n = self.needs[0]
        items = "; ".join(f"{x.quantity:g} {x.unit} {x.product_query}" for x in self.needs)
        self.emit("thought", f"Parsed need: {items}, delivery by {n.delivery_by}",
                  need=n.model_dump(mode="json"), needs=[x.model_dump(mode="json") for x in self.needs],
                  latency_s=round(time.time() - t, 2), parsed_by=n.preferences.get("parsed_by"))
        if self.clarify_enabled and not self._clarify(text, now):
            return self._finish_unanswered()
        if not self.needs or any(not _valid_quantity(getattr(x, "quantity", None)) for x in self.needs):
            return self._reject_invalid_quantities()
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

    def _read_image(self, text: str, now: datetime) -> str:
        from aimo.buyer.vision import extract_order_from_image
        t = time.time()
        data, mime = self.image
        try:
            ex = extract_order_from_image(data, mime, self.profile, now, run_id=self.run_id,
                                          generate=lambda **kw: self.generate(**kw), hint=text)
        except Exception as e:
            self.emit("error", f"Could not read the image ({type(e).__name__})", error=str(e)[:300])
            if not text:
                raise
            return text
        lines = ex.get("lines", [])
        self.emit("thought", f"Read {ex.get('document_type') or 'image'} with Gemini vision: {len(lines)} line(s)",
                  extracted=ex, lines=lines, request_text=ex.get("request_text"), latency_s=round(time.time() - t, 2))
        img_text = ex.get("request_text") or ""
        return f"{text} {img_text}".strip() if text else img_text

    def _clarify(self, text: str, now: datetime) -> bool:
        """Ask the human when the need is genuinely ambiguous. False = no answer, stop the run."""
        from aimo.buyer.clarify import apply_answer, find_questions

        def search(q: str, claims: list[str]) -> Any:
            return self._tool("search_products", {"query": q, "claims": claims, "limit": 10})

        clean_text = _strip_request_injections(text)
        raw_acc = text
        clean_acc = clean_text
        questions = find_questions(self.needs, self.profile, clean_text, now, search)
        for q in questions:
            public = {k: v for k, v in q.items() if k != "options"}
            public["options"] = [{k: v for k, v in o.items() if k != "patch"} for o in q["options"]]
            self.emit("needs_clarification", q["question"], **public, allow_free_text=True,
                      answer_url=f"/v1/agent/runs/{self.run_id}/answer")
            answer = None
            if self.answer_fn is not None:
                try:
                    answer = self.answer_fn(public)
                except Exception:
                    answer = None
            if not answer:
                self.clarifications.append({**public, "answer": None})
                self._pending_question = public
                return False
            ans_text = str(answer.get("text") or "").strip()
            clean_ans_text = _strip_request_injections(ans_text) if ans_text else ""
            eff_answer = {**answer, "text": clean_ans_text} if ans_text else answer
            if ans_text:
                raw_acc = f"{raw_acc} ({ans_text})"
                if clean_ans_text:
                    clean_acc = f"{clean_acc} ({clean_ans_text})"
            self.needs, label = apply_answer(self.needs, q, eff_answer)
            if not label and clean_ans_text:
                self.needs = [
                    x.model_copy(update={"raw_text": raw_acc})
                    for x in parse_request(clean_acc, self.profile, now, use_gemini=self.use_gemini_parse, run_id=self.run_id)
                ]
                label = f"free text: {clean_ans_text}"
            elif ans_text:
                self.needs = [x.model_copy(update={"raw_text": raw_acc}) for x in self.needs]
                if not label:
                    label = "free text: (ignored untrusted instructions)"
            self.clarifications.append({**public, "answer": answer, "resolved": label})
            self.emit("thought", f"Clarified: {label}", question_id=q["question_id"], answer=answer,
                      needs=[x.model_dump(mode="json") for x in self.needs])
        self.need = self.needs[0]
        return True

    def _finish_unanswered(self) -> dict:
        q = getattr(self, "_pending_question", {}) or {}
        summary = (f"I need an answer before I can order: {q.get('question', '')} "
                   f"Options: {'; '.join(o['label'] for o in q.get('options', []))}. Nothing was ordered.")
        self.emit("final", "Waiting for clarification", summary=summary, status="needs_clarification",
                  latency_s=round(time.time() - self.t0, 2), question=q)
        return self.result(summary, status="needs_clarification")

    def _loop(self, system: str, task: str) -> str:
        from google.genai import types
        decls = []
        for spec in self.t.list_tools():
            decls.append(types.FunctionDeclaration(name=spec.name, description=short_description(spec.description),
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
                http_options=types.HttpOptions(timeout=90_000))
            t = time.time()
            try:
                r = self.generate("buyer.agent_step", model, contents, cfg,
                                  meta={"thinking_level": level, "step": step, "escalated": model != self.model})
            except Exception as e:
                if model != self.model:
                    self.emit("error", f"Escalation model {model} failed ({type(e).__name__}); falling back",
                              error=str(e)[:300])
                    self.escalate_next = False
                    continue
                raise
            dt = round(time.time() - t, 2)
            escalated_step = model != self.model
            if self._generate is None:
                from aimo import gemini
                last_fn = getattr(gemini, "last_model", None)
                answered = (last_fn(model) if last_fn else model) or model
                if answered != model:
                    noted = getattr(self, "_fallback_noted", set())
                    if (model, answered) not in noted:
                        noted.add((model, answered))
                        self._fallback_noted = noted
                        self.emit("thought", f"{model} was unavailable (quota); {answered} answers instead",
                                  fallback_from=model, model=answered)
                    model = answered
            self.models_used.append({"step": step, "model": model, "thinking": level, "latency_s": dt})
            if escalated_step:
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
            contents.append(content)
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
                result = compact_for_model(fc.name, result)
                payload = result if isinstance(result, dict) else {"result": result}
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name, response=payload)))
            contents.append(types.Content(role="user", parts=responses))
            if self.done:
                break
        else:
            self.emit("error", f"Max steps ({self.max_steps}) reached", max_steps=self.max_steps)
        return final_text

    def _mark_replan_needed(self) -> None:
        active = [
            i for i, x in enumerate(self.needs)
            if not (x.preferences or {}).get("cancelled_by_clarification")
        ]
        self._replan_pending = set(active) if active else (set(self.plans) or {0})
        self._replan_needed = bool(self._replan_pending)

    def _tool(self, name: str, args: dict, **meta: Any) -> Any:
        args = self._guard_args(name, args)
        if isinstance(args, dict) and args.get("__blocked__"):
            msg = args["__blocked__"]
            self.emit("tool_call", f"{name} (blocked by harness)", name=name, blocked=msg, **meta)
            if name == "place_order" and self.drift:
                self.done = True
                self.emit("decision", "Held for human confirmation: standing-order drift",
                          status="needs_confirmation", quote_id=(self.quote or {}).get("quote_id"),
                          decision={"outcome": "needs_confirmation", "approver_role": "buyer",
                                    "hits": [{"rule_id": "buyer.standing_order_drift", "outcome": "needs_approval",
                                              "message": msg}]})
            return {"error": "blocked_by_buyer_harness", "message": msg}
        self.emit("tool_call", f"{name}({_short(args, 120)})", name=name, args=args, **meta)
        t = time.time()
        result, err = None, None
        for attempt in range(2):
            try:
                result = self.t.call(name, args)
                err = None
                break
            except Exception as e:
                err = e
                self.tool_failures[name] = self.tool_failures.get(name, 0) + 1
                status = getattr(e, "status", None)
                if isinstance(status, int) and status < 500:
                    body = e.to_dict() if hasattr(e, "to_dict") else {"error": str(e)[:300]}
                    if name == "create_quote" and body.get("error") in REPLAN_QUOTE_ERRORS:
                        self._mark_replan_needed()
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
        if name == "place_order":
            if not isinstance(self.quote, dict) or not self.quote.get("quote_id") or not self.quote.get("quote_hash"):
                return {"__blocked__": "Call create_quote first: an order requires a signed quote from this run."}
            return {"quote_id": self.quote["quote_id"], "quote_hash": self.quote["quote_hash"]}
        if name == "plan_need":
            active_items = [
                x for x in (self.needs or [n])
                if not (x.preferences or {}).get("cancelled_by_clarification")
            ] or (self.needs or [n])
            if any(not _valid_quantity(getattr(x, "quantity", None)) for x in active_items):
                return {"__blocked__": "Requested quantity must be a finite number greater than zero."}
            given = args.get("need") if isinstance(args.get("need"), dict) else {}
            self._plan_idx = idx = self._match_need(given)
            base_need = self.needs[idx] if self.needs else n
            if not _valid_quantity(getattr(base_need, "quantity", None)):
                return {"__blocked__": "Requested quantity must be a finite number greater than zero."}
            locked = base_need.model_dump(mode="json")
            locked["customer_id"] = self.customer_id
            locked["excluded_allergens"] = list(dict.fromkeys([
                *(locked.get("excluded_allergens") or []),
                *(self.profile.get("excluded_allergens") or []),
            ]))
            raw_pq = str(locked.get("product_query") or "")
            clean_locked_pq = _strip_request_injections(raw_pq).strip() or raw_pq
            if clean_locked_pq != raw_pq and not locked.get("raw_text"):
                locked["raw_text"] = raw_pq
            locked["product_query"] = clean_locked_pq
            given_pq = given.get("product_query").strip() if isinstance(given.get("product_query"), str) else ""
            given_raw = given.get("raw_text").strip() if isinstance(given.get("raw_text"), str) else ""
            pq_screen = self._record_tool_injection("product_query", given_pq)
            self._record_tool_injection("raw_text", given_raw)
            given_pq_susp = bool(given_pq and pq_screen.get("suspicious"))
            if given_pq_susp and given_pq not in self._suspicious_queries:
                self._suspicious_queries.append(given_pq)
            has_inj = bool(self.injections or self._request_injection_flags() or given_pq_susp)
            if given_pq and not given_pq_susp:
                if not has_inj:
                    chosen_pq = given_pq
                else:
                    base_tokens = {w for w in re.findall(r"[a-zA-ZåäöÅÄÖ]{3,}", clean_locked_pq.lower())}
                    given_tokens = {w for w in re.findall(r"[a-zA-ZåäöÅÄÖ]{3,}", given_pq.lower())}
                    chosen_pq = given_pq if (base_tokens and (base_tokens & given_tokens)) else clean_locked_pq
            else:
                chosen_pq = clean_locked_pq
            given_prefs = given.get("preferences") if isinstance(given.get("preferences"), dict) else {}
            prefs_src = (locked.get("preferences") or {}) if has_inj else {**(locked.get("preferences") or {}), **given_prefs}
            merged = {
                **locked,
                "raw_text": self._with_tool_injections_raw_text(locked.get("raw_text")),
                "product_query": chosen_pq,
                "preferences": {k: v for k, v in prefs_src.items() if k in PLANNER_PREF_KEYS},
            }
            return {"need": merged}
        if name == "create_quote":
            active_items = [
                x for x in (self.needs or [n])
                if not (x.preferences or {}).get("cancelled_by_clarification")
            ] or (self.needs or [n])
            if any(not _valid_quantity(getattr(x, "quantity", None)) for x in active_items):
                return {"__blocked__": "Requested quantity must be a finite number greater than zero."}
            args = dict(args) if isinstance(args, dict) else {}
            quote_need = dict(self._quote_need())
            quote_need["customer_id"] = self.customer_id
            quote_need["excluded_allergens"] = list(dict.fromkeys([
                *(quote_need.get("excluded_allergens") or []),
                *(self.profile.get("excluded_allergens") or []),
            ]))
            given_need = args.get("need") if isinstance(args.get("need"), dict) else {}
            if isinstance(given_need.get("product_query"), str):
                given_q_pq = given_need.get("product_query").strip()
                if self._record_tool_injection("product_query", given_q_pq).get("suspicious") and given_q_pq not in self._suspicious_queries:
                    self._suspicious_queries.append(given_q_pq)
            if isinstance(given_need.get("raw_text"), str):
                self._record_tool_injection("raw_text", given_need.get("raw_text"))
            locked_keys = (
                "customer_id",
                "product_query",
                "required_claims",
                "excluded_allergens",
                "quantity",
                "unit",
                "delivery_by",
                "preferred_product_ids",
                "raw_text",
            )
            merged_need = {**given_need, **{k: quote_need[k] for k in locked_keys if k in quote_need}}
            merged_need["raw_text"] = self._with_tool_injections_raw_text(merged_need.get("raw_text"))
            prefs = dict(quote_need.get("preferences") or {})
            inj_flags = list(dict.fromkeys([*self._injection_flags(), *self._request_injection_flags()]))
            if inj_flags:
                prefs["injection_flags"] = inj_flags
            merged_need["preferences"] = prefs
            if isinstance(self.plan, dict):
                warn = self._request_injection_warning()
                exp = str(self.plan.get("explanation") or "")
                if warn and "Warning: the request contains instructions aimed at AI agents" not in exp:
                    self.plan = {**self.plan, "explanation": f"{warn} {exp}".strip()}
            args["need"] = merged_need
            args["delivery_date"] = (
                min(x.delivery_by for x in self.needs).isoformat()
                if self.needs
                else n.delivery_by.isoformat()
            )
            return self._enforce_plan_lines(args)
        return args

    def _enforce_plan_lines(self, args: dict) -> dict:
        """Quantities come from the deterministic planner, never from the LLM: the quote must carry
        exactly the plan's lines (product_id, qty_sales_units). A mismatch is corrected to the plan
        and logged as a thought, so the Plan and the Quote/Approval views always agree."""
        if not self.plans:
            return {"__blocked__": "Call plan_need first: quote lines must come from the planner."}
        if getattr(self, "_replan_needed", False) or getattr(self, "_replan_pending", None):
            rem = [
                self.needs[i].product_query
                for i in sorted(getattr(self, "_replan_pending", set()))
                if 0 <= i < len(self.needs)
            ]
            suffix = f" (remaining items to re-plan: {rem})" if len(self.needs) > 1 and rem else ""
            return {"__blocked__": f"Call plan_need again before re-quoting: the previous plan's lines were refused by the gateway{suffix}."}
        unplanned = [
            x.product_query
            for i, x in enumerate(self.needs)
            if i not in self.plans and not (x.preferences or {}).get("cancelled_by_clarification")
        ]
        if unplanned:
            return {"__blocked__": f"Call plan_need for every item first (not planned yet: {unplanned})."}
        plan_lines: list[dict] = []
        by_pid: dict[str, dict] = {}
        for i in sorted(self.plans):
            if 0 <= i < len(self.needs) and (self.needs[i].preferences or {}).get("cancelled_by_clarification"):
                continue
            for l in (self.plans[i] or {}).get("lines", []):
                pid = str(l["product_id"])
                if pid in by_pid:
                    total = by_pid[pid]["qty_sales_units"] + l["qty_sales_units"]
                    by_pid[pid]["qty_sales_units"] = round(total, 6) if isinstance(total, float) else total
                else:
                    entry = {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"],
                             **({"tier": l["tier"]} if l.get("tier") else {})}
                    by_pid[pid] = entry
                    plan_lines.append(entry)
        if not plan_lines:
            return {"__blocked__": "The plan has no orderable lines (full shortfall), so there is nothing to quote. "
                                   "Report the shortfall and its reasons to the buyer."}

        def _safe_qty(val: Any) -> float:
            try:
                return round(float(val or 0), 4)
            except (TypeError, ValueError):
                return -1.0

        def key(ls: Any) -> list[tuple[str, float]]:
            if not isinstance(ls, list):
                return []
            return sorted((str(l.get("product_id")), _safe_qty(l.get("qty_sales_units"))) for l in ls if isinstance(l, dict))

        raw_proposed = args.get("lines")
        valid_list = isinstance(raw_proposed, list) and all(isinstance(l, dict) for l in raw_proposed)
        if not valid_list or key(raw_proposed) != key(plan_lines):
            shown = (
                [{"product_id": l.get("product_id"), "qty_sales_units": l.get("qty_sales_units")}
                 for l in raw_proposed if isinstance(l, dict)]
                if isinstance(raw_proposed, list)
                else []
            )
            self.emit("thought", "Quote lines corrected to the planner's lines (quantities never come from the model)",
                      proposed=shown, plan_lines=plan_lines)
        return {**args, "lines": [dict(l) for l in plan_lines]}

    def _match_need(self, given: dict) -> int:
        """Which parsed item a plan_need call is about: by product_query, else the next unplanned one."""
        if len(self.needs) <= 1:
            return 0
        pending = getattr(self, "_replan_pending", set())
        active = [
            i for i, x in enumerate(self.needs)
            if not (x.preferences or {}).get("cancelled_by_clarification")
        ] or list(range(len(self.needs)))
        unplanned = [i for i in active if i not in self.plans or i in pending]
        q = str(given.get("product_query") or "").lower().strip()
        for i in unplanned:
            if q and q == self.needs[i].product_query.lower():
                return i
        for i in active:
            if q and q == self.needs[i].product_query.lower():
                return i
        for i, x in enumerate(self.needs):
            if q and q == x.product_query.lower():
                return i
        qw = set(re.findall(r"\w+", q))
        pool = unplanned or active
        best = max(pool, key=lambda i: len(qw & set(re.findall(r"\w+", self.needs[i].product_query.lower()))))
        if qw & set(re.findall(r"\w+", self.needs[best].product_query.lower())):
            return best
        return unplanned[0] if unplanned else active[0]

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
        explanations = list(dict.fromkeys(str(p.get("explanation") or "").strip() for p in ps if str(p.get("explanation") or "").strip()))
        return {"need": self._quote_need(), "lines": [l for p in ps for l in p.get("lines", [])],
                "candidates": [c for p in ps for c in p.get("candidates", [])],
                "covered_amount": sum(p.get("covered_amount") or 0 for p in ps),
                "shortfall": sum(p.get("shortfall") or 0 for p in ps),
                "total_net": round(sum(p.get("total_net") or 0 for p in ps), 2),
                "explanation": " ".join(explanations),
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

    def _record_tool_injection(self, field_name: str, text: Any) -> dict:
        val = str(text or "").strip()
        if not val:
            return {"suspicious": False, "reasons": [], "strong_reasons": []}
        s = _screen_fast(val)
        if s.get("suspicious"):
            store = getattr(self, "_tool_injections", None)
            if store is None:
                store = []
                self._tool_injections = store
            pair = (field_name, val)
            if pair not in store:
                store.append(pair)
        return s

    def _with_tool_injections_raw_text(self, base_raw: Any) -> str:
        raw = str(base_raw or "")
        extras: list[str] = []
        for _, val in getattr(self, "_tool_injections", ()):
            if val and val not in raw and val not in extras:
                extras.append(val)
        if not extras:
            return raw
        return f"{raw}\n" + "\n".join(extras) if raw else "\n".join(extras)

    def _request_injection_warning(self) -> str:
        items = self.needs or ([self.need] if self.need is not None else [])
        reasons: list[str] = []
        candidates = [
            *(val for x in items for val in (x.raw_text, x.product_query) if val),
            *(val for _, val in getattr(self, "_tool_injections", ()) if val),
        ]
        for val in candidates:
            s = _screen_fast(val)
            if s.get("suspicious"):
                for r in (s.get("strong_reasons") or s.get("reasons") or ["suspicious_instruction"]):
                    rs = str(r)
                    if rs and rs not in reasons:
                        reasons.append(rs)
        if not reasons:
            return ""
        return f"Warning: the request contains instructions aimed at AI agents ({', '.join(reasons)[:200]})."

    def _request_injection_flags(self) -> list[str]:
        items = self.needs or ([self.need] if self.need is not None else [])
        seen: set[str] = set()
        out: list[str] = []
        pairs = [
            *((field_name, val) for x in items for field_name, val in (("raw_text", x.raw_text), ("product_query", x.product_query))),
            *getattr(self, "_tool_injections", ()),
        ]
        for field_name, val in pairs:
            if not val:
                continue
            s = _screen_fast(val)
            if s.get("suspicious"):
                rs = list(dict.fromkeys(s.get("strong_reasons") or s.get("reasons") or ["suspicious buyer text"]))
                entry = f"buyer {field_name}: {', '.join(str(r) for r in rs[:3])}"
                if entry not in seen:
                    seen.add(entry)
                    out.append(entry)
        return out

    def _after(self, name: str, args: dict, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        if name == "plan_need" and "lines" in result:
            self._replan_pending.discard(self._plan_idx)
            self._replan_needed = bool(self._replan_pending)
            warn = self._request_injection_warning()
            exp = str(result.get("explanation") or "")
            if warn and "Warning: the request contains instructions aimed at AI agents" not in exp:
                result = {**result, "explanation": f"{warn} {exp}".strip()}
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
        elif name == "create_quote" and result.get("error") in REPLAN_QUOTE_ERRORS:
            self._mark_replan_needed()
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
            self.done = True
        return result

    def _sync_quote_from_order(self, cur: Any, *, handoff: bool = False) -> bool:
        if not isinstance(self.quote, dict) or not isinstance(cur, dict):
            return False
        prov = cur.get("provenance") if isinstance(cur.get("provenance"), dict) else {}
        patch: dict[str, Any] = {}
        if handoff:
            old_qid = cur.get("quote_id")
            rq_id = prov.get("requote_quote_id")
            for nk in ("requote_quote", "new_quote", "quote"):
                nq = prov.get(nk)
                if isinstance(nq, dict) and (nk != "quote" or not old_qid or nq.get("quote_id") != old_qid):
                    patch.update(_extract_quote_patch(nq))
            cq = cur.get("quote")
            if isinstance(cq, dict) and (
                (isinstance(rq_id, str) and rq_id and cq.get("quote_id") == rq_id)
                or (old_qid and cq.get("quote_id") and cq.get("quote_id") != old_qid)
            ):
                patch.update(_extract_quote_patch(cq))
            patch.update(_extract_quote_patch(prov, prefix="requote_"))
        else:
            cur_qid = cur.get("quote_id")
            if cur.get("status") != "expired" and prov.get("requote_quote_id") in (None, "", cur_qid):
                prov_patch = _extract_quote_patch(prov, prefix="requote_")
                if cur_qid:
                    prov_patch.pop("quote_id", None)
                patch.update(prov_patch)
            for nk in ("quote", "new_quote", "requote_quote"):
                nq = prov.get(nk)
                if isinstance(nq, dict) and (not cur_qid or not nq.get("quote_id") or nq.get("quote_id") == cur_qid):
                    if cur.get("status") != "expired" or nq.get("quote_id") == cur_qid:
                        patch.update(_extract_quote_patch(nq))
            cq = cur.get("quote")
            if isinstance(cq, dict) and (not cur_qid or not cq.get("quote_id") or cq.get("quote_id") == cur_qid):
                patch.update(_extract_quote_patch(cq))
            patch.update(_extract_quote_patch(cur))
        if patch.get("quote_id") and patch["quote_id"] != self.quote.get("quote_id") and "quote_hash" not in patch:
            patch["quote_hash"] = None
        if patch:
            self.quote = {**self.quote, **patch}
        return bool("lines" in patch and "total_net" in patch)

    def _sync_quote_from_audit(self, quote_id: str, order_id: str) -> bool:
        if not isinstance(self.quote, dict) or not quote_id:
            return False
        try:
            trail = self.t.call("get_audit_trail", {"subject_id": quote_id})
        except Exception:
            return False
        events = trail.get("events") if isinstance(trail, dict) else (trail if isinstance(trail, list) else [])
        if not isinstance(events, list):
            return False
        patch: dict[str, Any] = {}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("subject_id") not in (quote_id, order_id) and ev.get("action") != "quote.created":
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            nq = data.get("quote")
            if isinstance(nq, dict) and nq.get("quote_id") in (None, quote_id):
                patch.update(_extract_quote_patch(nq))
            if ev.get("action") == "quote.created" and ev.get("subject_id") == quote_id:
                patch.update(_extract_quote_patch({"quote_id": quote_id, **data}))
        if patch:
            self.quote = {**self.quote, **patch}
        return bool("lines" in patch and "total_net" in patch)

    def _finalize_replacement_quote_sync(
        self,
        cur: Any,
        initial_qid: str | None,
        full_synced_qids: set[str],
        audit_tried_qids: set[str],
        order_id: str,
    ) -> None:
        if isinstance(cur, dict) and self._sync_quote_from_order(cur, handoff=False):
            qid_now = (self.quote or {}).get("quote_id")
            if isinstance(qid_now, str) and qid_now:
                full_synced_qids.add(qid_now)
        target_qid = (self.quote or {}).get("quote_id") if isinstance(self.quote, dict) else None
        if (
            isinstance(target_qid, str)
            and target_qid
            and target_qid != initial_qid
            and target_qid not in full_synced_qids
            and target_qid not in audit_tried_qids
        ):
            audit_tried_qids.add(target_qid)
            if self._sync_quote_from_audit(target_qid, order_id):
                full_synced_qids.add(target_qid)

    def _wait_for_approval(self, order: dict) -> dict:
        dec = order.get("decision") or {}
        oid = order["order_id"]
        seen_orders = {oid}
        self._approved_polls = 0
        initial_qid = (self.quote or {}).get("quote_id") if isinstance(self.quote, dict) else order.get("quote_id")
        full_synced_qids: set[str] = {initial_qid} if isinstance(initial_qid, str) and initial_qid else set()
        audit_tried_qids: set[str] = set()
        self.order = order
        self.emit("approval_wait", f"Waiting for {dec.get('approver_role') or 'a human'} to approve order {oid}",
                  order_id=oid, approver_role=dec.get("approver_role"), quote_id=order.get("quote_id"),
                  quote_hash=order.get("quote_hash"), hits=dec.get("hits", []), poll_s=self.poll_s)
        deadline = time.time() + self.approval_timeout_s
        cur = order
        stale_handoff_keys = {
            "quote", "lines", "total_net", "total_gross", "expires_at",
            "response_code", "response_lines", "approved_by", "approval_note",
        }
        while time.time() < deadline:
            time.sleep(self.poll_s)
            try:
                polled = self.t.call("get_order_status", {"order_id": oid})
            except Exception as e:
                self.emit("error", f"get_order_status failed ({type(e).__name__}); still waiting", error=str(e)[:200])
                continue
            if not isinstance(polled, dict):
                self.emit("error", f"get_order_status returned {type(polled).__name__}; still waiting",
                          order_id=oid, error=f"expected dict, got {type(polled).__name__}")
                continue
            cur = polled
            if isinstance(cur, dict) and cur.get("status") == "approved":
                if self._sync_quote_from_order(cur, handoff=False):
                    qid_cur = (self.quote or {}).get("quote_id")
                    if isinstance(qid_cur, str) and qid_cur:
                        full_synced_qids.add(qid_cur)
                self.order = cur
                self._approved_polls = getattr(self, "_approved_polls", 0) + 1
                if self._approved_polls < 15:
                    continue
                break
            if isinstance(cur, dict) and cur.get("status") == "expired":
                prov = cur.get("provenance") or {}
                next_oid = prov.get("requoted_as") if isinstance(prov, dict) else None
                if isinstance(next_oid, str) and next_oid and next_oid not in seen_orders:
                    old_oid = oid
                    seen_orders.add(next_oid)
                    self._approved_polls = 0
                    oid = next_oid
                    rq_id = prov.get("requote_quote_id") if isinstance(prov, dict) else None
                    rq_hash = prov.get("requote_quote_hash") if isinstance(prov, dict) else None
                    if self._sync_quote_from_order(cur, handoff=True):
                        qid_cur = (self.quote or {}).get("quote_id")
                        if isinstance(qid_cur, str) and qid_cur:
                            full_synced_qids.add(qid_cur)
                    reason = (prov.get("requote_reason") if isinstance(prov, dict) else None) or "requote_needed"
                    dec = cur.get("decision") or dec
                    eff_qid = rq_id if isinstance(rq_id, str) and rq_id else (self.quote or {}).get("quote_id")
                    eff_qhash = rq_hash if isinstance(rq_hash, str) and rq_hash else (self.quote or {}).get("quote_hash")
                    base_cur = {k: v for k, v in cur.items() if k not in stale_handoff_keys}
                    cur = {
                        **base_cur,
                        "order_id": oid,
                        "status": "pending_approval",
                        **(
                            {"quote_id": eff_qid, "quote_hash": eff_qhash if isinstance(eff_qhash, str) and eff_qhash else None}
                            if isinstance(eff_qid, str) and eff_qid
                            else {}
                        ),
                    }
                    self.order = cur
                    self.emit(
                        "approval_wait",
                        f"Order re-quoted ({old_oid} -> {oid}); waiting for {dec.get('approver_role') or 'a human'} to approve order {oid}",
                        order_id=oid,
                        old_order_id=old_oid,
                        requote_reason=reason,
                        approver_role=dec.get("approver_role"),
                        quote_id=eff_qid,
                        quote_hash=eff_qhash,
                        hits=dec.get("hits", []),
                        poll_s=self.poll_s,
                    )
                    continue
                break
            if isinstance(cur, dict):
                if self._sync_quote_from_order(cur, handoff=False):
                    qid_cur = (self.quote or {}).get("quote_id")
                    if isinstance(qid_cur, str) and qid_cur:
                        full_synced_qids.add(qid_cur)
                self.order = cur
            if isinstance(cur, dict) and cur.get("status") != "pending_approval":
                break
        else:
            self._finalize_replacement_quote_sync(cur, initial_qid, full_synced_qids, audit_tried_qids, oid)
            timed_out = {**cur, "status": cur.get("status", "pending_approval"), "timed_out": True} if isinstance(cur, dict) else {"order_id": oid, "status": "pending_approval", "timed_out": True}
            self.order = timed_out
            self.emit("error", f"No human decision within {self.approval_timeout_s:.0f}s", order_id=oid)
            return timed_out
        self._finalize_replacement_quote_sync(cur, initial_qid, full_synced_qids, audit_tried_qids, oid)
        if not isinstance(cur, dict):
            cur = {**(order if isinstance(order, dict) else {}), "order_id": oid, "status": "pending_approval"}
        self.order = cur
        who = cur.get("approved_by") or "the approver"
        self.emit("order", f"Order {oid} {cur.get('status')} (human decision by {who})", order=cur)
        return cur

    def _explain(self, plan: dict) -> str:
        facts = {
            "need": {k: (plan.get("need") or (self.need.model_dump(mode="json") if self.need else {})).get(k)
                     for k in ("product_query", "quantity", "unit", "delivery_by", "required_claims",
                               "excluded_allergens", "preferred_product_ids")},
            "lines": [{k: l.get(k) for k in ("product_id", "product_name", "tier", "qty_sales_units", "sales_unit",
                                             "covered_amount", "unit_price", "line_total", "flags", "injection_screen")}
                      | {"reasons": _rs(l.get("reasons"), 3, 140)} for l in plan.get("lines", [])],
            "candidates": compact_for_model("plan_need", plan).get("candidates_top3_per_tier", [])[:8],
            "covered_amount": plan.get("covered_amount"), "shortfall": plan.get("shortfall"),
            "total_net": plan.get("total_net"),
        }
        try:
            return self._text_call("buyer.explain", prompts.EXPLAIN.format(
                plan_json=json.dumps(facts, ensure_ascii=False, default=str)))
        except Exception as e:
            return f"(explanation unavailable: {type(e).__name__})"

    def _audit_link(self) -> str:
        subject = (self.order or {}).get("order_id") or (self.quote or {}).get("quote_id") or self.run_id
        return f"{self.audit_base_url}/v1/audit/report/{subject}"

    def _finish(self, agent_text: str) -> dict:
        status = (self.order or {}).get("status") or ("needs_confirmation" if self.drift and self.quote else
                                                      "no_order")
        facts = {
            "request": self.need.raw_text if self.need else "",
            "needs": [_pick(x.model_dump(mode="json"), ("product_query", "quantity", "unit", "delivery_by",
                                                         "required_claims", "excluded_allergens", "preferred_product_ids"))
                      for x in self.needs],
            "plan_lines": [{**_pick(l, ("product_id", "product_name", "tier", "qty_sales_units", "sales_unit", "line_total")),
                            "reasons": _rs(l.get("reasons"), 2, 140)} for l in (self.plan or {}).get("lines", [])],
            "shortfall": (self.plan or {}).get("shortfall"),
            "quote": {k: (self.quote or {}).get(k) for k in ("quote_id", "total_net")} if self.quote else None,
            "order_status": status,
            "response_code": (self.order or {}).get("response_code"),
            "response_lines": (self.order or {}).get("response_lines") or None,
            "policy_decision": (_compact_order(self.order) or {}).get("decision") if self.order else None,
            "approved_by": (self.order or {}).get("approved_by"),
            "approval_note": (self.order or {}).get("approval_note"),
            "drift_hold": self.drift,
            "injections_flagged": list(dict.fromkeys([*self._injection_flags(), *self._request_injection_flags()])),
            "agent_final_text": agent_text,
            "audit_report_link": self._audit_link(),
        }
        try:
            summary = self._text_call("buyer.summary", prompts.SUMMARY.format(
                facts_json=json.dumps(facts, ensure_ascii=False, default=str)))
        except Exception as e:
            summary = ""
            self.emit("error", f"Summary generation failed ({type(e).__name__})")
        summary = summary or agent_text or f"Run finished with status {status}. Audit: {facts['audit_report_link']}"
        latency = round(time.time() - self.t0, 2)
        self.emit("final", f"Done: {status}", summary=summary, status=status, agent_text=agent_text,
                  audit_report=facts["audit_report_link"], latency_s=latency, models_used=self.models_used,
                  escalated=self.escalated, injections=self.injections)
        return self.result(summary, status=status)

    def result(self, summary: str, status: str) -> dict:
        return {"run_id": self.run_id, "status": status, "summary": summary, "mock": self.mock,
                "need": self.need.model_dump(mode="json") if self.need else None, "needs": [x.model_dump(mode="json") for x in self.needs],
                "quote": self.quote, "order": self.order, "plan": self.plan,
                "latency_s": round(time.time() - self.t0, 2), "models_used": self.models_used, "clarifications": self.clarifications,
                "steps": [s.model_dump(mode="json") for s in self.steps]}

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
