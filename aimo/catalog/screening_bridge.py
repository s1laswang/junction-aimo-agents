"""Deterministic prompt-injection screening bridge for catalog rows and plan lines."""
from __future__ import annotations

import itertools
import math
import re
import unicodedata
from typing import Any, Optional

MAX_FIELD_CHARS = 65_536
_HEAD_CHARS = 49_152
_TAIL_CHARS = MAX_FIELD_CHARS - _HEAD_CHARS - 1
MAX_REASON_ITEMS = 10
MAX_REASON_CHARS = 200
_MAX_LIST_SCAN = 1_000
_ZW_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_CTRL_WS_RE = re.compile(r"[\r\n\t]+")

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


def _clean_str(val: Any, max_len: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(val, str) or not val or max_len <= 0:
        return ""
    raw = val[: max_len * 2] if len(val) > max_len * 2 else val
    s = raw.replace("\x00", "")
    if len(s) > max_len:
        s = s[:max_len]
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


def _bound_field(val: Any, max_len: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(val, str) or not val or max_len <= 0:
        return ""
    if len(val) <= max_len:
        return _clean_str(val, max_len)
    if max_len < 64:
        return _clean_str(val, max_len)
    tail_len = min(_TAIL_CHARS, max_len // 4)
    head_len = max_len - tail_len - 1
    head = _clean_str(val[:head_len], head_len)
    tail = _clean_str(val[-tail_len:], tail_len)
    return (head + "\n" + tail)[:max_len]


def _clean_str_list(
    val: Any,
    *,
    max_items: int = MAX_REASON_ITEMS,
    max_chars: int = MAX_REASON_CHARS,
) -> list[str]:
    if max_items <= 0 or max_chars <= 0:
        return []
    if isinstance(val, (list, tuple)):
        candidates: Any = itertools.islice(val, _MAX_LIST_SCAN)
    elif isinstance(val, str):
        candidates = (val,)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, str):
            continue
        cleaned = _CTRL_WS_RE.sub(" ", _clean_str(item, max_chars * 2)).strip()
        if not cleaned:
            continue
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars].rstrip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _is_suspicious(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        try:
            f = float(val)
        except (OverflowError, TypeError, ValueError):
            return False
        return math.isfinite(f) and f != 0.0
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


def _fallback_screen(text: Any) -> dict[str, Any]:
    safe = _bound_field(text, MAX_FIELD_CHARS)
    if not safe:
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"]}
    norm = unicodedata.normalize("NFKC", safe)
    norm = _ZW_RE.sub("", norm)
    low = re.sub(r"[ \t]+", " ", norm).lower()
    hits = [k for k in FALLBACK_KEYWORDS if k in low]
    return {"suspicious": bool(hits), "reasons": list(hits), "strong_reasons": list(hits), "layers": ["regex"]}


def screen_product_row(pid: str, row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"], "injected": False}
    raw_labels = row.get("labels")
    label_items = itertools.islice(raw_labels, _MAX_LIST_SCAN) if isinstance(raw_labels, (list, tuple, set, frozenset)) else ()
    injected = any(isinstance(x, str) and x == "injected:inject_text" for x in label_items)
    marketing_text = _bound_field(row.get("marketing_text"), MAX_FIELD_CHARS)
    if isinstance(pid, str):
        pid_key = _clean_str(pid, 256).strip()
    elif isinstance(pid, int) and not isinstance(pid, bool):
        pid_key = str(pid)[:256]
    else:
        pid_key = ""
    try:
        from aimo.backend.client import INJECTION_TEXT, current_faults

        faults = current_faults()
        if faults.inject_text and faults._hit(faults.inject_text, pid_key):
            marketing_text = _bound_field((marketing_text.rstrip() + "\n\n" + INJECTION_TEXT).strip(), MAX_FIELD_CHARS)
            injected = True
    except Exception:
        pass
    ingredients_text = _bound_field(row.get("ingredients_text"), MAX_FIELD_CHARS)
    text = "\n".join(x for x in (marketing_text.strip(), ingredients_text.strip()) if x).strip()
    if not text:
        return {"suspicious": False, "reasons": [], "strong_reasons": [], "layers": ["regex"], "injected": bool(injected)}
    res: Any = None
    try:
        from aimo.trust import screen_text

        if callable(screen_text):
            try:
                res = screen_text(text, use_gemini=False)
            except TypeError:
                res = screen_text(text)
    except Exception:
        res = None
    res_data = res if (isinstance(res, dict) and "suspicious" in res) else _fallback_screen(text)
    reasons = _clean_str_list(res_data.get("reasons"), max_items=MAX_REASON_ITEMS, max_chars=MAX_REASON_CHARS)
    strong_reasons = _clean_str_list(
        res_data.get("strong_reasons"),
        max_items=MAX_REASON_ITEMS,
        max_chars=MAX_REASON_CHARS,
    ) or list(reasons)
    layers = _clean_str_list(res_data.get("layers"), max_items=MAX_REASON_ITEMS, max_chars=64) or ["regex"]
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
    reasons = (
        _clean_str_list(screen.get("strong_reasons"), max_items=MAX_REASON_ITEMS, max_chars=MAX_REASON_CHARS)
        or _clean_str_list(screen.get("reasons"), max_items=MAX_REASON_ITEMS, max_chars=MAX_REASON_CHARS)
        or ["suspicious supplier text"]
    )
    why = ", ".join(reasons)
    return f"Supplier text flagged by prompt-injection screen ({why}); order requires human approval"
