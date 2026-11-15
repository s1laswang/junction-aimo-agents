"""Multimodal intake: a photo of a handwritten order, a menu or a supplier invoice -> request text.

Gemini reads the image (types.Part.from_bytes) with structured output, so a kitchen can snap the
order slip instead of typing. The extracted request text then goes through the same parse_request
path as typed text, so every downstream guardrail is unchanged. Ledger feature: buyer.vision.
Only what is legible in the image is extracted; unreadable parts are reported, never guessed.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

MAX_IMAGE_BYTES = 15_000_000
SUPPORTED_MIMES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "application/pdf",
})
_DOC_TYPES = frozenset({"handwritten_order", "menu", "invoice", "shopping_list", "other"})
_ORDER_KEYS = frozenset({"document_type", "language", "lines", "delivery_expression", "request_text", "unreadable"})
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)


class ImageLine(BaseModel):
    product: str = Field(description="Product as written, translated to a short English catalog phrase")
    original_text: str = Field(default="", description="The line exactly as written in the image")
    quantity: float | None = Field(default=None, description="Quantity as written, null if not legible")
    unit: str = Field(default="", description="Unit as written: l, dl, kg, g, kpl/pcs, box ... empty if none")
    notes: str = Field(default="", description="Extra words on the line (brand, fat %, lactose-free ...)")


class ImageOrder(BaseModel):
    document_type: Literal["handwritten_order", "menu", "invoice", "shopping_list", "other"]
    language: str = Field(default="", description="ISO 639-1 code of the writing, e.g. fi, en, sv")
    lines: list[ImageLine] = Field(default_factory=list)
    delivery_expression: str = Field(default="", description="Any delivery day/date as written, e.g. 'ma' (Monday)")
    request_text: str = Field(
        description=(
            "One English purchasing request sentence that restates ALL legible lines "
            "with quantities, units, claims and the delivery day, e.g. "
            "'We need 200 litres of lactose-free milk and 10 kg butter by Monday.'"
        )
    )
    unreadable: list[str] = Field(default_factory=list, description="Parts that could not be read with confidence")


PROMPT = """You read a photo for {customer} ({segment}), a Finnish food-service customer of Valio Aimo.
It may be a handwritten order slip, a menu, a shopping list or a supplier invoice, in Finnish,
Swedish or English. Today is {today}.
Extract only what is legible: never invent products, quantities or dates; list doubtful parts in
'unreadable'. Finnish hints: maito = milk, laktoositon = lactose-free, voi = butter, kerma = cream,
juusto = cheese, kaurajuoma = oat drink, kpl = pieces, ma/ti/ke/to/pe = Mon/Tue/Wed/Thu/Fri.
For a menu (dishes, no quantities), set quantity null and describe the main ingredients per dish.
Then write request_text as one English purchasing sentence covering every legible line.
{hint}"""


def _has_valid_magic(raw: bytes, mime: str) -> bool:
    if mime == "image/png":
        return len(raw) >= 8 and raw.startswith(b"\x89PNG")
    if mime == "image/jpeg":
        return len(raw) >= 4 and raw.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a")
    if mime == "image/webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if mime in ("image/heic", "image/heif"):
        return len(raw) >= 12 and raw[4:8] == b"ftyp"
    if mime == "application/pdf":
        return len(raw) >= 5 and raw.startswith(b"%PDF-")
    return False


def _validate_image_input(data: Any, mime: Any) -> tuple[bytes, str]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("image data must be bytes")
    raw = bytes(data)
    if not raw:
        raise ValueError("image data is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds maximum size of {MAX_IMAGE_BYTES} bytes")
    if not isinstance(mime, str) or not mime.strip():
        raise ValueError("image mime type must be a non-empty string")
    clean_mime = mime.strip().lower().split(";", 1)[0].strip()
    if clean_mime == "image/jpg":
        clean_mime = "image/jpeg"
    if clean_mime not in SUPPORTED_MIMES:
        raise ValueError(f"unsupported image mime type: {mime!r}")
    if not _has_valid_magic(raw, clean_mime):
        raise ValueError(f"malformed image bytes for {clean_mime}")
    return raw, clean_mime


def fallback_image_order(hint: str = "", reason: str = "vision_unavailable") -> dict[str, Any]:
    clean_hint = hint.strip() if isinstance(hint, str) else ""
    notes = [reason.strip()] if isinstance(reason, str) and reason.strip() else []
    return ImageOrder(
        document_type="other",
        language="",
        lines=[],
        delivery_expression="",
        request_text=clean_hint,
        unreadable=notes,
    ).model_dump()


def _response_text(resp: Any) -> str:
    if resp is None:
        return ""
    try:
        txt = getattr(resp, "text", None)
    except Exception:
        txt = None
    if isinstance(txt, str) and txt.strip():
        return txt
    cands = getattr(resp, "candidates", None)
    if isinstance(cands, list):
        chunks: list[str] = []
        for cand in cands:
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                pt = getattr(part, "text", None)
                if isinstance(pt, str) and pt:
                    chunks.append(pt)
        if chunks:
            return "".join(chunks)
    return ""


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip() if isinstance(raw_text, str) else ""
    if not cleaned:
        raise ValueError("empty vision response")
    m = _FENCE_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(cleaned[start : end + 1])
        else:
            raise
    if not isinstance(obj, dict) or not obj:
        raise ValueError("vision response is not a non-empty JSON object")
    return obj


def _synthesize_request_text(lines: list[dict[str, Any]], delivery_expr: str) -> str:
    phrases: list[str] = []
    for ln in lines:
        prod = str(ln.get("product") or "").strip()
        if not prod:
            continue
        qty = ln.get("quantity")
        unit = str(ln.get("unit") or "").strip()
        notes = str(ln.get("notes") or "").strip()
        if isinstance(qty, (int, float)) and not isinstance(qty, bool):
            head = f"{qty:g} {unit} {prod}".strip() if unit else f"{qty:g} {prod}".strip()
        else:
            head = prod
        if notes and notes.lower() not in head.lower():
            head = f"{head} ({notes})"
        phrases.append(head)
    if not phrases:
        return ""
    joined = " and ".join(phrases) if len(phrases) <= 2 else ", ".join(phrases[:-1]) + f" and {phrases[-1]}"
    suffix = f" {delivery_expr}" if delivery_expr else ""
    return f"We need {joined}{suffix}."


def _coerce_image_order(obj: dict[str, Any], clean_hint: str) -> dict[str, Any]:
    if not (_ORDER_KEYS & set(obj)):
        raise ValueError("vision JSON missing ImageOrder fields")
    raw_doc = str(obj.get("document_type") or "").strip().lower()
    doc_type = raw_doc if raw_doc in _DOC_TYPES else "other"
    lang = str(obj.get("language") or "").strip()
    delivery_expr = str(obj.get("delivery_expression") or "").strip()

    raw_unreadable = obj.get("unreadable")
    if isinstance(raw_unreadable, list):
        unreadable = [str(u).strip() for u in raw_unreadable if u is not None and str(u).strip()]
    elif isinstance(raw_unreadable, str) and raw_unreadable.strip():
        unreadable = [raw_unreadable.strip()]
    else:
        unreadable = []

    raw_lines = obj.get("lines")
    lines: list[dict[str, Any]] = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            orig = str(item.get("original_text") or "").strip()
            prod = str(item.get("product") or "").strip() or orig
            if not prod:
                continue
            unit = str(item.get("unit") or "").strip()
            notes = str(item.get("notes") or "").strip()
            qty_raw = item.get("quantity")
            qty: float | None = None
            if qty_raw is not None and qty_raw != "":
                if isinstance(qty_raw, bool):
                    unreadable.append(f"invalid quantity on {prod}")
                else:
                    try:
                        q_val = float(qty_raw)
                        if math.isfinite(q_val) and q_val > 0:
                            qty = q_val
                        else:
                            unreadable.append(f"non-positive quantity on {prod}")
                    except (TypeError, ValueError):
                        unreadable.append(f"unparseable quantity on {prod}")
            lines.append({
                "product": prod,
                "original_text": orig,
                "quantity": qty,
                "unit": unit,
                "notes": notes,
            })

    req_text = str(obj.get("request_text") or "").strip()
    if not req_text and lines:
        req_text = _synthesize_request_text(lines, delivery_expr)
    if not req_text and clean_hint:
        req_text = clean_hint

    return ImageOrder.model_validate({
        "document_type": doc_type,
        "language": lang,
        "lines": lines,
        "delivery_expression": delivery_expr,
        "request_text": req_text,
        "unreadable": unreadable,
    }).model_dump()


def extract_order_from_image(
    data: bytes,
    mime: str,
    profile: dict,
    now: datetime,
    *,
    run_id: str = "",
    generate: Callable[..., Any] | None = None,
    hint: str = "",
    model: str | None = None,
    fallback: bool = False,
) -> dict:
    raw, clean_mime = _validate_image_input(data, mime)
    prof = profile if isinstance(profile, dict) else {}
    clean_hint = hint.strip() if isinstance(hint, str) else ""
    if not isinstance(now, datetime):
        from aimo.buyer.need import HELSINKI
        now = datetime.now(HELSINKI)

    if generate is None:
        from aimo.buyer.mock_model import mock_wanted
        if mock_wanted():
            return fallback_image_order(clean_hint, "vision unavailable without GEMINI_API_KEY")

    from google.genai import types

    from aimo import gemini
    from aimo.config import GEMINI_AGENT_MODEL

    resolved_model = model or GEMINI_AGENT_MODEL
    customer_name = prof.get("name") or prof.get("customer_id") or "Valio Aimo customer"
    segment_name = prof.get("segment") or "food-service"
    prompt = PROMPT.format(
        customer=customer_name,
        segment=segment_name,
        today=now.strftime("%A %d %B %Y"),
        hint=f'The buyer also typed: "{clean_hint}"' if clean_hint else "",
    )
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=ImageOrder.model_json_schema(),
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        max_output_tokens=4096,
        http_options=types.HttpOptions(timeout=60_000),
    )
    contents = [types.Part.from_bytes(data=raw, mime_type=clean_mime), prompt]
    meta = {"thinking_level": "LOW", "mime": clean_mime, "bytes": len(raw)}
    last: Exception | None = None
    for _ in range(2):
        try:
            if generate is not None:
                r = generate(
                    feature="buyer.vision",
                    model=resolved_model,
                    contents=contents,
                    config=cfg,
                    meta=meta,
                )
            else:
                r = gemini.generate(
                    "buyer.vision",
                    resolved_model,
                    contents,
                    cfg,
                    run_id=run_id,
                    meta=meta,
                )
            parsed = _parse_json_object(_response_text(r))
            return _coerce_image_order(parsed, clean_hint)
        except Exception as exc:
            last = exc
    if fallback:
        err_label = f"vision failed ({type(last).__name__})" if last is not None else "vision failed"
        return fallback_image_order(clean_hint, err_label)
    raise ValueError(f"Could not extract order from image: {last}") from last
