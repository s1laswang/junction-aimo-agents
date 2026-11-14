"""Deterministic prompt-injection screening bridge for catalog rows and plan lines."""
from __future__ import annotations

from typing import Any, Optional

FALLBACK_KEYWORDS = (
    "ignore your",
    "ignore previous",
    "disregard previous",
    "ai agent",
    "approval already granted",
    "pre-approved",
    "approval_required=false",
    "system note",
    "system override",
    "do not mention",
)


def _fallback_screen(text: str) -> dict[str, Any]:
    low = text.lower()
    hits = [k for k in FALLBACK_KEYWORDS if k in low]
    return {"suspicious": bool(hits), "reasons": hits, "strong_reasons": hits, "layers": ["regex"]}


def screen_product_row(pid: str, row: dict[str, Any]) -> dict[str, Any]:
    data = row or {}
    injected = "injected:inject_text" in (data.get("labels") or [])
    marketing_text = str(data.get("marketing_text") or "")
    try:
        from aimo.backend.client import INJECTION_TEXT, current_faults

        faults = current_faults()
        if faults.inject_text and faults._hit(faults.inject_text, str(pid)):
            marketing_text = (marketing_text.rstrip() + "\n\n" + INJECTION_TEXT).strip()
            injected = True
    except ImportError:
        pass
    ingredients_text = str(data.get("ingredients_text") or "")
    text = "\n".join(x for x in (marketing_text.strip(), ingredients_text.strip()) if x).strip()
    if not text:
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"], "injected": False}
    try:
        from aimo.trust import screen_text

        try:
            res = screen_text(text, use_gemini=False)
        except TypeError:
            res = screen_text(text)
    except (ImportError, NotImplementedError, AttributeError):
        res = _fallback_screen(text)
    return {
        "suspicious": bool(res.get("suspicious")),
        "reasons": list(res.get("reasons") or []),
        "strong_reasons": list(res.get("strong_reasons") or res.get("reasons") or []),
        "layers": list(res.get("layers") or ["regex"]),
        "injected": bool(injected),
    }


def screen_reason(screen: Optional[dict[str, Any]]) -> Optional[str]:
    if not screen or not screen.get("suspicious"):
        return None
    why = ", ".join(screen.get("strong_reasons") or screen.get("reasons") or ["suspicious supplier text"])
    return f"Supplier text flagged by prompt-injection screen ({why}); order requires human approval"
