"""MOCK model for running the end-to-end demo with no Gemini API key. NOT Gemini.

When neither GEMINI_API_KEY nor GOOGLE_API_KEY is set (or AIMO_LLM_MODE=mock), BuyerAgent uses
MockModel instead of Gemini. It replays the fixed tool sequence the real agent follows
(plan_need for every parsed item -> create_quote -> place_order) through the SAME harness, so the
gateway, policies, signing, approvals and audit trail are all real; only the "LLM" is scripted.
Need parsing uses the deterministic parser (aimo.buyer.need, use_gemini=False). Every text the
mock writes starts with MOCK_LABEL, and the agent's model name becomes MOCK_MODEL_NAME, so a mock
run can never be mistaken for a Gemini run (UI, CLI, audit and run JSON all show it).

AIMO_LLM_MODE: "auto" (default: mock only when no key), "mock" (always), "live" (never; a missing
key then fails as before).
"""
from __future__ import annotations

import os
from typing import Any

MOCK_MODEL_NAME = "mock-replay (no Gemini)"
MOCK_LABEL = "[MOCK MODEL: no GEMINI_API_KEY, scripted replay, not Gemini]"


def mock_wanted() -> bool:
    mode = os.environ.get("AIMO_LLM_MODE", "auto").strip().lower()
    if mode == "mock":
        return True
    if mode == "live":
        return False
    from aimo import config
    return not (config.GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _resp(parts: list[Any]):
    from google.genai import types
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


def _call(name: str, args: dict):
    from google.genai import types
    return _resp([types.Part(function_call=types.FunctionCall(name=name, args=args))])


def _text(text: str):
    from google.genai import types
    return _resp([types.Part(text=text)])


class MockModel:
    """Drop-in for BuyerAgent(generate_fn=...). Reads the agent's harness state, never invents
    quantities: the harness locks the need and corrects quote lines to the planner's lines."""

    def __init__(self, agent: Any):
        self.agent = agent
        self.calls: list[str] = []

    def __call__(self, feature: str, model: str, contents: Any, config: Any = None):
        self.calls.append(feature)
        a = self.agent
        if isinstance(contents, str):  # buyer.explain / buyer.summary: grounded facts, labelled
            return _text(self._summary(feature))
        done = [p.function_response.name for c in contents for p in (c.parts or []) if p.function_response]
        # 1. plan every parsed item once
        for i, need in enumerate(a.needs):
            if i not in a.plans and done.count("plan_need") <= i:
                return _call("plan_need", {"need": need.model_dump(mode="json")})
        # 2. quote the planner's lines once
        if a.quote is None and "create_quote" not in done:
            lines = [{"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"],
                      **({"tier": l["tier"]} if l.get("tier") else {})} for l in (a.plan or {}).get("lines", [])]
            need = a.need
            return _call("create_quote", {"delivery_date": need.delivery_by.isoformat() if need else None,
                                          "lines": lines})
        # 3. submit once (policy, drift hold and approvals stay with the harness/gateway)
        if a.quote is not None and "place_order" not in done and not a.blocked:
            return _call("place_order", {"quote_id": a.quote["quote_id"], "quote_hash": a.quote["quote_hash"]})
        return _text(self._summary("buyer.agent_step"))

    def _summary(self, feature: str) -> str:
        a = self.agent
        if feature == "buyer.explain":
            return f"{MOCK_LABEL} Plan from the deterministic planner; see the reasons on each line."
        status = (a.order or {}).get("status") or ("needs_confirmation" if a.drift and a.quote else "no_order")
        q = a.quote or {}
        bits = [f"status {status}"]
        if q:
            bits.append(f"quote {q.get('quote_id')} EUR {q.get('total_net', 0):.2f}")
        if (a.order or {}).get("approved_by"):
            bits.append(f"approved by {a.order['approved_by']}")
        if a.injections:
            bits.append("suspicious supplier text was detected and ignored")
        return f"{MOCK_LABEL} " + "; ".join(bits) + "."
