"""Deterministic prompt-injection screening bridge for catalog rows and plan lines."""
from __future__ import annotations

import math
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


def _clean_str(val: str) -> str:
    s = val.replace("\x00", "")
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


def _clean_str_list(val: Any) -> list[str]:
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for item in val:
            if isinstance(item, str):
                cleaned = _clean_str(item).strip()
                if cleaned:
                    out.append(cleaned)
        return out
    if isinstance(val, str):
        cleaned = _clean_str(val).strip()
        return [cleaned] if cleaned else []
    return []


def _is_suspicious(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return math.isfinite(float(val)) and val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


def _fallback_screen(text: str) -> dict[str, Any]:
    safe = _clean_str(text) if isinstance(text, str) else ""
    low = safe.lower()
    hits = [k for k in FALLBACK_KEYWORDS if k in low]
    return {"suspicious": bool(hits), "reasons": hits, "strong_reasons": hits, "layers": ["regex"]}


def screen_product_row(pid: str, row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"], "injected": False}
    raw_labels = row.get("labels")
    label_items = raw_labels if isinstance(raw_labels, (list, tuple, set, frozenset)) else ()
    injected = any(x == "injected:inject_text" for x in label_items)
    raw_mkt = row.get("marketing_text")
    marketing_text = _clean_str(raw_mkt) if isinstance(raw_mkt, str) else ""
    if isinstance(pid, str):
        pid_key = _clean_str(pid).strip()
    elif isinstance(pid, int) and not isinstance(pid, bool):
        pid_key = str(pid)
    else:
        pid_key = ""
    try:
        from aimo.backend.client import INJECTION_TEXT, current_faults

        faults = current_faults()
        if faults.inject_text and faults._hit(faults.inject_text, pid_key):
            marketing_text = (marketing_text.rstrip() + "\n\n" + INJECTION_TEXT).strip()
            injected = True
    except ImportError:
        pass
    raw_ing = row.get("ingredients_text")
    ingredients_text = _clean_str(raw_ing) if isinstance(raw_ing, str) else ""
    text = "\n".join(x for x in (marketing_text.strip(), ingredients_text.strip()) if x).strip()
    if not text:
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"], "injected": bool(injected)}
    try:
        from aimo.trust import screen_text

        try:
            res = screen_text(text, use_gemini=False)
        except TypeError:
            res = screen_text(text)
    except (ImportError, NotImplementedError, AttributeError):
        res = _fallback_screen(text)
    res_data = res if isinstance(res, dict) else {}
    reasons = _clean_str_list(res_data.get("reasons"))
    strong_reasons = _clean_str_list(res_data.get("strong_reasons")) or list(reasons)
    layers = _clean_str_list(res_data.get("layers")) or ["regex"]
    return {
        "suspicious": _is_suspicious(res_data.get("suspicious")),
        "reasons": reasons,
        "strong_reasons": strong_reasons,
        "layers": layers,
        "injected": bool(injected),
    }


def screen_reason(screen: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(screen, dict) or not _is_suspicious(screen.get("suspicious")):
        return None
    reasons = _clean_str_list(screen.get("strong_reasons")) or _clean_str_list(screen.get("reasons")) or ["suspicious supplier text"]
    why = ", ".join(reasons)
    return f"Supplier text flagged by prompt-injection screen ({why}); order requires human approval"
