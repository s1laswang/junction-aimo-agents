"""Natural-language need -> aimo.models.Need.

Primary path: Gemini structured output (response_json_schema) validated with pydantic.
Fallback: deterministic regex parser (no network), also used to cross-check the date, because
relative dates ("by Monday") must be resolved against Europe/Helsinki "now", not the model's
idea of today.

Claim codes are the nutritionalClaim ids that occur in the product data
(FREE_FROM_LACTOSE 4178x, FREE_FROM_GLUTEN 3049x, VEGAN, VEGETARIAN, ...).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from aimo.models import EU14, Need

HELSINKI = ZoneInfo("Europe/Helsinki")

CLAIMS = ["FREE_FROM_LACTOSE", "LOW_LACTOSE", "FREE_FROM_GLUTEN", "VEGAN", "VEGETARIAN",
          "FREE_FROM_EGGS", "FREE_FROM_SOY", "FREE_FROM_WHEAT", "ORGANIC", "NO_ADDED_SUGARS",
          "HIGH_PROTEIN", "LOW_FAT", "FREE_FROM_FAT", "FREE_FROM_PRESERVATIVES",
          "FREE_FROM_ADDITIVES", "HIGH_FIBRE", "LOW_SODIUM_SALT"]

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
            "saturday": 5, "sunday": 6,
            "maanantai": 0, "tiistai": 1, "keskiviikko": 2, "torstai": 3, "perjantai": 4,
            "lauantai": 5, "sunnuntai": 6,
            "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
            "sat": 5, "sun": 6}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9


def now_helsinki() -> datetime:
    # the gateway's (simulated) clock: AIMO_NOW / replay snapshot time, else real time (S1 replay: "by Monday"
    # must resolve to the date the replay snapshot recorded, on any real day)
    try:
        from aimo.backend import clock
        return clock.now().astimezone(HELSINKI)
    except Exception:  # noqa: BLE001
        return datetime.now(HELSINKI)


# ---------------------------------------------------------------- Gemini output schema
class ParsedNeed(BaseModel):
    """What Gemini fills in. Kept flat and enum-heavy so structured output is reliable."""
    product_query: str = Field(description="Short catalog search phrase, English, e.g. 'lactose-free milk'. No quantities, dates or site names.")
    required_claims: list[str] = Field(default_factory=list, description=f"Hard product claims, from: {', '.join(CLAIMS)}")
    excluded_allergens: list[str] = Field(default_factory=list, description=f"EU14 allergen keys that must be excluded, from: {', '.join(EU14)}")
    quantity: float = Field(description="Total quantity requested, as a number (1,000 -> 1000)")
    unit: Literal["L", "KG", "PCS"] = Field(description="L for litres/dl/ml, KG for kg/g, PCS for pieces/packs/units")
    delivery_by: str = Field(description="Delivery deadline as ISO date YYYY-MM-DD, resolved relative to today")
    delivery_expression: str = Field(default="", description="The words in the request that state the date, verbatim, e.g. 'by Monday'")
    site: str = Field(default="", description="Delivery site / location if stated")
    notes: str = Field(default="", description="Anything else relevant (pack size, brand, fat %), short")


class ParsedRequest(BaseModel):
    """A request may name several products ('40 L cream, 20 kg butter'): one item each."""
    items: list[ParsedNeed] = Field(description="One item per distinct product requested, in order of mention")


def _gemini_schema() -> dict[str, Any]:
    s = ParsedRequest.model_json_schema()
    item = s["$defs"]["ParsedNeed"]
    item["properties"]["required_claims"]["items"] = {"type": "string", "enum": CLAIMS}
    item["properties"]["excluded_allergens"]["items"] = {"type": "string", "enum": EU14}
    return s


# ---------------------------------------------------------------- deterministic pieces
_QTY_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[ ,. ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(?P<unit>"
    r"litres?|liters?|ltrs?|l|dl|ml|kilograms?|kilos?|kgs?|kg|grams?|g|pieces?|pcs|kpl|packs?|"
    r"units?|bottles?|cartons?|boxes|box|portions?)\b", re.I)


def _to_number(s: str) -> float:
    s = s.replace(" ", " ").strip()
    # thousands separators: "1,000" "1 000" "1.000" (exactly three digits after)
    if re.fullmatch(r"\d{1,3}(?:[ ,.]\d{3})+", s):
        return float(re.sub(r"[ ,.]", "", s))
    m = re.fullmatch(r"(\d{1,3}(?:[ ,]\d{3})+)\.(\d+)", s)  # 1,000.5
    if m:
        return float(re.sub(r"[ ,]", "", m.group(1)) + "." + m.group(2))
    return float(s.replace(",", "."))


def parse_quantity(text: str) -> tuple[float, str] | None:
    m = _QTY_RE.search(text)
    if not m:
        return None
    q = _to_number(m.group("num"))
    u = m.group("unit").lower()
    if u in ("dl",):
        return q / 10, "L"
    if u in ("ml",):
        return q / 1000, "L"
    if u.startswith("l") and not u.startswith("lb"):
        return q, "L"
    if u in ("g", "gram", "grams"):
        return q / 1000, "KG"
    if u.startswith("k") and u != "kpl":
        return q, "KG"
    return q, "PCS"


def resolve_date(text: str, now: datetime | None = None) -> tuple[date, str] | None:
    """Resolve the delivery date mentioned in text relative to now (Europe/Helsinki).
    Weekday names mean the next such day strictly after today."""
    now = (now or now_helsinki()).astimezone(HELSINKI)
    today = now.date()
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])), m[0]
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)", t)  # Finnish 16.11. / 16.11.2026
    if m:
        y = int(m[3]) if m[3] else today.year
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, int(m[2]), int(m[1]))
            if not m[3] and d < today:
                d = date(y + 1, d.month, d.day)
            return d, m[0]
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\b", t) or \
        re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.group(1), m.group(2)
        day, mon = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
        try:
            d = date(today.year, mon, day)
            if d < today:
                d = date(today.year + 1, mon, day)
            return d, m[0]
        except ValueError:
            pass
    if re.search(r"\bday after tomorrow\b|\bylihuomenna\b", t):
        return today + timedelta(days=2), "day after tomorrow"
    if re.search(r"\btomorrow\b|\bhuomenna\b", t):
        return today + timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b|\btänään\b|\btonight\b", t):
        return today, "today"
    m = re.search(r"\b(next\s+week)\b", t)
    if m:
        return today + timedelta(days=7 - today.weekday()), m[0]  # next Monday
    m = re.search(r"\b(?:next\s+)?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")(?:na|nä|hin)?\b", t)
    if m:
        wd = WEEKDAYS[m.group(1)]
        ahead = (wd - today.weekday()) % 7 or 7  # "next Monday" == the coming Monday
        return today + timedelta(days=ahead), m[0]
    return None


_CLAIM_PATTERNS = [
    (r"lactose[- ]?free|laktoositon|without lactose|no lactose", "FREE_FROM_LACTOSE"),
    (r"low[- ]lactose|vähälaktoos", "LOW_LACTOSE"),
    (r"gluten[- ]?free|gluteeniton|coeliac|celiac", "FREE_FROM_GLUTEN"),
    (r"\bvegan\b|vegaan", "VEGAN"),
    (r"vegetarian|kasvis", "VEGETARIAN"),
    (r"\borganic\b|luomu", "ORGANIC"),
    (r"egg[- ]free", "FREE_FROM_EGGS"),
    (r"soy[- ]free", "FREE_FROM_SOY"),
    (r"no added sugar|sugar[- ]free", "NO_ADDED_SUGARS"),
]
_ALLERGEN_PATTERNS = [
    (r"milk[- ]allerg|dairy[- ]free|milk[- ]free|maitoallerg|cow'?s milk protein", "milk"),
    (r"\bnut[- ]allerg|nut[- ]free|tree nut|pähkinäallerg|nuts allerg", "nuts"),
    (r"peanut", "peanuts"),
    (r"gluten[- ]?free|coeliac|celiac|keliak", "gluten"),
    (r"egg[- ]allerg|egg[- ]free", "eggs"),
    (r"fish[- ]allerg", "fish"),
    (r"soy[- ]allerg|soy[- ]free", "soy"),
    (r"sesame", "sesame"),
    (r"celery", "celery"),
    (r"mustard", "mustard"),
]


def detect_claims(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(c for p, c in _CLAIM_PATTERNS if re.search(p, t)))


def detect_allergens(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(a for p, a in _ALLERGEN_PATTERNS if re.search(p, t)))


_MEAL = r"(?:\s+(?:lunch|dinner|evening|morning|breakfast|night|afternoon|service))?"
_DATE_PHRASE = re.compile(
    r"\b(?:(?:by|on|for|before|until|due|delivered|delivery)\s+)?(?:(?:next|this)\s+)?"
    r"(?:" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")(?:na|nä|hin)?(?:'s)?\b" + _MEAL +
    r"|\b(?:(?:by|on|for|before|until|delivered)\s+)?(?:day after tomorrow|tomorrow|today|tonight|next week|this week)\b" + _MEAL +
    r"|\b(?:(?:by|on|for|before|until)\s+)?\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:(?:by|on|for|before|until)\s+)?\d{1,2}\.\d{1,2}\.?(?:\d{2,4})?", re.I)
_ALLERGY_CLAUSE = re.compile(r"allerg|intoleran|coeliac|celiac|keliak|cannot eat|can't eat", re.I)
_LEAD_FILLER = {"we", "i", "need", "needs", "want", "please", "order", "get", "buy", "some", "about", "around",
                "approx", "approx.", "the", "a", "an", "urgent", "urgently", "also", "of", "just", "more", "x"}
_TAIL = re.compile(r"\s+(?:at|for|by|on|to|before|delivered|until|with|because|from|in)\b.*$", re.I)


def _clauses(text: str) -> list[str]:
    # split on sentence punctuation and on commas not followed by a digit ("1,000" / "2,5 kg" stay)
    return [c.strip() for c in re.split(r"[.;:!?\n](?!\d)|,(?!\d)", text) if c.strip()]


def _residual(clause: str) -> str:
    """The product noun phrase of a clause: quantities, date phrases, filler and trailing
    'for/at/by ...' tails removed. '120 l oat drink for Wednesday lunch' -> 'oat drink'."""
    words = re.sub(r"\s+", " ", _QTY_RE.sub(" ", clause)).strip().split(" ")
    while words and words[0].lower() in _LEAD_FILLER:
        words.pop(0)
    s = _TAIL.sub("", " ".join(words))  # cut 'for Wednesday's lunch' / 'at Hotel ...' first
    s = re.sub(r"\s+", " ", _DATE_PHRASE.sub(" ", s)).strip()  # then a leading 'by Wednesday'
    words = s.split(" ")
    while words and words[0].lower() in _LEAD_FILLER:
        words.pop(0)
    while words and words[-1].lower() in _LEAD_FILLER:
        words.pop()
    return " ".join(words).strip(" -'\"")


def split_items(text: str) -> list[dict[str, Any]]:
    """Deterministic multi-item split: one item per quantity, named by its noun phrase. A quantity
    without a name ('200 L by Wednesday') takes the next unused product phrase from a clause
    without a quantity ('Oat drink for a school'). Allergy clauses never name a product."""
    items: list[dict[str, Any]] = []
    names: list[str] = []
    for clause in _clauses(text):
        if _ALLERGY_CLAUSE.search(clause):
            continue
        n_qty = len(list(_QTY_RE.finditer(clause)))
        if n_qty == 0:
            r = _residual(clause)
            if r:
                names.append(r)
            continue
        parts = re.split(r"\s+(?:and|plus|&|\+)\s+", clause) if n_qty > 1 else [clause]
        for p in parts:
            q = parse_quantity(p)
            if q:
                items.append({"quantity": q[0], "unit": q[1], "query": _residual(p), "clause": p})
    for it in items:
        if not it["query"] and names:
            it["query"] = names.pop(0)
    return items


def guess_query(text: str) -> str:
    items = split_items(text)
    if items and items[0]["query"]:
        return items[0]["query"]
    for clause in _clauses(text):
        if not _ALLERGY_CLAUSE.search(clause):
            r = _residual(clause)
            if r:
                return r
    return re.sub(r"\s+", " ", text.strip())[:60]


def _global_claims(text: str) -> list[str]:
    """Claims stated in clauses without a quantity apply to every item ('..., lactose-free')."""
    out: list[str] = []
    for clause in _clauses(text):
        if not _QTY_RE.search(clause):
            out += detect_claims(clause)
    return list(dict.fromkeys(out))


def fallback_parse_items(text: str, customer_id: str, now: datetime | None = None,
                         profile: dict | None = None) -> list[Need]:
    d = resolve_date(text, now)
    today = (now or now_helsinki()).astimezone(HELSINKI).date()
    prefs: dict[str, Any] = {"parsed_by": "regex"}
    if d is None:
        prefs["delivery_date_assumed"] = True
    allergens = detect_allergens(text)
    items = [it for it in split_items(text) if it["query"]]
    if len(items) <= 1:
        qty = (items[0]["quantity"], items[0]["unit"]) if items else (parse_quantity(text) or (1.0, "PCS"))
        specs = [(qty, guess_query(text), detect_claims(text))]
    else:
        g = _global_claims(text)
        specs = [((it["quantity"], it["unit"]), it["query"], list(dict.fromkeys([*detect_claims(it["clause"]), *g])))
                 for it in items]
    needs = [Need(raw_text=text, customer_id=customer_id, product_query=q, required_claims=claims,
                  excluded_allergens=allergens, quantity=qty[0], unit=qty[1],
                  delivery_by=d[0] if d else today + timedelta(days=2), preferences=dict(prefs))
             for qty, q, claims in specs]
    return [apply_profile(n, profile) for n in needs]


def fallback_parse(text: str, customer_id: str, now: datetime | None = None,
                   profile: dict | None = None) -> Need:
    return fallback_parse_items(text, customer_id, now, profile)[0]


def apply_profile(need: Need, profile: dict | None) -> Need:
    """Merge customer standing preferences into the need. Profile allergens are ADDED (never
    removed); preferred products are only attached if they plausibly match the request."""
    if not profile:
        return need
    allergens = list(dict.fromkeys([*need.excluded_allergens, *profile.get("excluded_allergens", [])]))
    prefs = dict(need.preferences)
    preferred = list(need.preferred_product_ids)
    std = profile.get("standing_order") or {}
    match = profile.get("usual_match")
    if profile.get("preferred_product_ids") and match and re.search(match, need.product_query, re.I):
        # The usual product (and its pack/brand preferences) only applies to matching requests,
        # e.g. the hotel's dispenser milk applies to milk requests, not to butter.
        preferred = list(dict.fromkeys([*preferred, *profile["preferred_product_ids"]]))
        prefs = {**profile.get("preferences", {}), **prefs}
        if std.get("qty_per_week") and need.unit == std.get("unit"):
            prefs["standing_order_qty_per_week"] = std["qty_per_week"]
            prefs["standing_order_ratio"] = round(need.quantity / float(std["qty_per_week"]), 2)
    return need.model_copy(update={"excluded_allergens": allergens, "preferences": prefs,
                                   "preferred_product_ids": preferred})


# ---------------------------------------------------------------- main entry
PARSE_PROMPT = """You turn a B2B food-service purchasing request into structured JSON for Valio Aimo.
Today is {today_long} (Europe/Helsinki). Resolve relative dates against today: a weekday name
means the next such day after today; "tomorrow" is {tomorrow}.
Customer: {customer_name} ({segment}). Standing allergen exclusions: {allergens}.
Rules: quantities are totals ("1,000 litres" -> 1000, unit L). Use only claim codes and allergen
keys from the allowed enums. "lactose-free" -> FREE_FROM_LACTOSE. A stated allergy -> excluded
allergen (milk allergy -> milk). Do not invent products, brands or quantities that are not in the
request. product_query is a short English catalog search phrase (the product noun phrase only:
no quantities, dates, sites or allergy wording). Return one item per distinct product; a claim
stated for the whole order (e.g. "..., lactose-free") applies to every item.

Request: {text}"""


def parse_need(text: str, customer: str | dict, now: datetime | None = None, *,
               use_gemini: bool = True, run_id: str = "", model: str | None = None) -> Need:
    """Parse free text into a Need (the first item if the request names several products)."""
    return parse_request(text, customer, now, use_gemini=use_gemini, run_id=run_id, model=model)[0]


def parse_request(text: str, customer: str | dict, now: datetime | None = None, *,
                  use_gemini: bool = True, run_id: str = "", model: str | None = None) -> list[Need]:
    """Parse free text into one Need per product. `customer` is a customer_id or a profile dict."""
    from aimo.buyer.profiles import get_profile
    profile = customer if isinstance(customer, dict) else get_profile(customer)
    customer_id = profile["customer_id"]
    now = (now or now_helsinki()).astimezone(HELSINKI)
    fallback = fallback_parse_items(text, customer_id, now, profile)
    if not use_gemini:
        return fallback
    try:
        parsed = _gemini_parse(text, profile, now, run_id=run_id, model=model).items
        if not parsed:
            raise ValueError("no items")
    except Exception as e:  # noqa: BLE001  network, quota, schema: fall back deterministically
        for n in fallback:
            n.preferences["gemini_error"] = type(e).__name__
        return fallback
    det = resolve_date(text, now)
    det_items = [it for it in split_items(text) if it["query"]] or split_items(text)
    aligned = len(det_items) == len(parsed)
    single = len(parsed) == 1
    global_claims = detect_claims(text) if single else _global_claims(text)
    allergens_text = detect_allergens(text)  # allergies always apply to the whole order
    needs = []
    for i, p in enumerate(parsed):
        try:
            d_model = date.fromisoformat(p.delivery_by[:10])
        except ValueError:
            d_model = fallback[0].delivery_by
        prefs: dict[str, Any] = {"parsed_by": "gemini"}
        if det and det[0] != d_model:
            prefs["date_note"] = f"date corrected from {d_model} to {det[0]} (deterministic '{det[1]}')"
            d_model = det[0]
        item_claims = detect_claims(det_items[i]["clause"]) if aligned and not single else []
        # Hard constraints are a union: the deterministic detector can only add, never remove.
        claims = list(dict.fromkeys([*(c for c in p.required_claims if c in CLAIMS), *global_claims, *item_claims]))
        allergens = list(dict.fromkeys([*(a for a in p.excluded_allergens if a in EU14), *allergens_text]))
        if p.notes:
            prefs["notes"] = p.notes
        if p.site:
            prefs["site"] = p.site
        qty, unit = p.quantity, p.unit
        det_q = (det_items[i]["quantity"], det_items[i]["unit"]) if aligned else (parse_quantity(text) if single else None)
        if det_q and (abs(det_q[0] - qty) > 1e-6 or det_q[1] != unit):
            prefs["quantity_note"] = f"model said {qty} {unit}, text says {det_q[0]} {det_q[1]}"
            qty, unit = det_q
        query = p.product_query.strip() or (fallback[min(i, len(fallback) - 1)].product_query)
        needs.append(apply_profile(Need(raw_text=text, customer_id=customer_id, product_query=query,
                                        required_claims=claims, excluded_allergens=allergens, quantity=qty,
                                        unit=unit, delivery_by=d_model, preferences=prefs), profile))
    return needs


def _gemini_parse(text: str, profile: dict, now: datetime, run_id: str = "",
                  model: str | None = None) -> ParsedRequest:
    from google.genai import types

    from aimo import gemini
    from aimo.config import GEMINI_AGENT_MODEL
    prompt = PARSE_PROMPT.format(
        today_long=now.strftime("%A %d %B %Y"), tomorrow=(now.date() + timedelta(days=1)).isoformat(),
        customer_name=profile.get("name"), segment=profile.get("segment"),
        allergens=", ".join(profile.get("excluded_allergens") or []) or "none", text=text)
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=_gemini_schema(),
        thinking_config=types.ThinkingConfig(thinking_level="LOW"), max_output_tokens=4096,
        http_options=types.HttpOptions(timeout=45_000))  # ms
    last: Exception | None = None
    for _ in range(2):  # one retry on empty / invalid JSON
        r = gemini.generate("buyer.parse_need", model or GEMINI_AGENT_MODEL, prompt, cfg, run_id=run_id)
        try:
            return ParsedRequest.model_validate(json.loads(r.text or ""))
        except (ValidationError, json.JSONDecodeError, TypeError) as e:
            last = e
    raise last  # type: ignore[misc]
