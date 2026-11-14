"""Multimodal intake: a photo of a handwritten order, a menu or a supplier invoice -> request text.

Gemini reads the image (types.Part.from_bytes) with structured output, so a kitchen can snap the
order slip instead of typing. The extracted request text then goes through the same parse_request
path as typed text, so every downstream guardrail is unchanged. Ledger feature: buyer.vision.
Only what is legible in the image is extracted; unreadable parts are reported, never guessed.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


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
    request_text: str = Field(description="One English purchasing request sentence that restates ALL legible lines "
                                          "with quantities, units, claims and the delivery day, e.g. "
                                          "'We need 200 litres of lactose-free milk and 10 kg butter by Monday.'")
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


def extract_order_from_image(data: bytes, mime: str, profile: dict, now: datetime, *, run_id: str = "",
                             generate: Callable[..., Any] | None = None, hint: str = "",
                             model: str | None = None) -> dict:
    from google.genai import types

    from aimo import gemini
    from aimo.config import GEMINI_AGENT_MODEL
    model = model or GEMINI_AGENT_MODEL
    prompt = PROMPT.format(customer=profile.get("name"), segment=profile.get("segment"),
                           today=now.strftime("%A %d %B %Y"),
                           hint=f"The buyer also typed: \"{hint}\"" if hint else "")
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=ImageOrder.model_json_schema(),
        thinking_config=types.ThinkingConfig(thinking_level="LOW"), max_output_tokens=4096,
        http_options=types.HttpOptions(timeout=60_000))
    contents = [types.Part.from_bytes(data=data, mime_type=mime or "image/png"), prompt]
    last: Exception | None = None
    for _ in range(2):
        if generate is not None:
            r = generate(feature="buyer.vision", model=model, contents=contents, config=cfg,
                         meta={"thinking_level": "LOW", "mime": mime, "bytes": len(data)})
        else:
            r = gemini.generate("buyer.vision", model, contents, cfg, run_id=run_id,
                                meta={"thinking_level": "LOW", "mime": mime, "bytes": len(data)})
        try:
            return ImageOrder.model_validate(json.loads(r.text or "")).model_dump()
        except Exception as e:  # noqa: BLE001  empty / invalid JSON: one retry
            last = e
    raise last  # type: ignore[misc]
