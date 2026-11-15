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
            "maanantai": 0, "tiistai": 1, "keskiviikko": 2, "keskiviiko": 2, "torstai": 3, "perjantai": 4,
            "lauantai": 5, "sunnuntai": 6,
            "måndag": 0, "tisdag": 1, "onsdag": 2, "torsdag": 3, "fredag": 4, "lördag": 5, "söndag": 6,
            "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
            "sat": 5, "sun": 6}

_EN_WD = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun"
_FI_WD = r"maanantai|tiistai|keskiviikko|keskiviiko|torstai|perjantai|lauantai|sunnuntai"
_SV_WD = r"måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS.update({"sept": 9, "januari": 1, "februari": 2, "mars": 3, "maj": 5, "juni": 6, "juli": 7,
               "augusti": 8, "oktober": 10})


def now_helsinki() -> datetime:
    try:
        from aimo.backend import clock
        return clock.now().astimezone(HELSINKI)
    except Exception:
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
    r"(?P<num>(?<!\S)-?(?:\d{1,3}(?:[ ,.\u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?))\s*(?P<unit>"
    r"decilitres?|deciliters?|desilitraa?|millilitres?|milliliters?|millilitraa?|"
    r"litres?|liters?|litraa?|ltrs?|l|dl|ml|"
    r"kilogrammaa?|kilograms?|kilos?|kiloa?|kgs?|kg|grammaa?|grams?|g|"
    r"kappaletta|kappale|kpl|stycken|styck|(?<!1)st|"
    r"förpackningar|förpackning|förp|pakkausta|pakkaus|pakettia?|packs?|pkt|"
    r"purkkia?|tölkkiä|tolkkia|tölkki|tolkki|laatikkoa|laatikko|rasiaa?|pulloa?|"
    r"pieces?|pcs|units?|bottles?|cartons?|boxes|box|portions?)\b",
    re.I,
)


def _to_number(s: str) -> float:
    s = s.replace("\u00a0", " ").strip()
    sign = -1.0 if s.startswith("-") else 1.0
    if s.startswith("-"):
        s = s[1:].strip()
    if re.fullmatch(r"\d{1,3}(?:[ ,.]\d{3})+", s):
        return sign * float(re.sub(r"[ ,.]", "", s))
    m = re.fullmatch(r"(\d{1,3}(?:[ ,]\d{3})+)\.(\d+)", s)
    if m:
        return sign * float(re.sub(r"[ ,]", "", m.group(1)) + "." + m.group(2))
    m = re.fullmatch(r"(\d{1,3}(?:[ .]\d{3})+),(\d+)", s)
    if m:
        return sign * float(re.sub(r"[ .]", "", m.group(1)) + "." + m.group(2))
    return sign * float(s.replace(",", "."))


_DL_UNITS = {"dl", "desilitra", "desilitraa", "decilitre", "decilitres", "deciliter", "deciliters"}
_ML_UNITS = {"ml", "millilitra", "millilitraa", "millilitre", "millilitres", "milliliter", "milliliters"}
_L_UNITS = {"l", "ltr", "ltrs", "litre", "litres", "liter", "liters", "litra", "litraa"}
_G_UNITS = {"g", "gram", "grams", "gramma", "grammaa"}
_KG_UNITS = {"kg", "kgs", "kilo", "kilos", "kiloa", "kilogram", "kilograms", "kilogramma", "kilogrammaa"}


def parse_quantity(text: str) -> tuple[float, str] | None:
    m = _QTY_RE.search(text)
    if not m:
        return None
    q = _to_number(m.group("num"))
    u = m.group("unit").lower()
    if u in _DL_UNITS:
        return q / 10, "L"
    if u in _ML_UNITS:
        return q / 1000, "L"
    if u in _L_UNITS:
        return q, "L"
    if u in _G_UNITS:
        return q / 1000, "KG"
    if u in _KG_UNITS:
        return q, "KG"
    return q, "PCS"


_WEEKDAY_SEARCH_RE = re.compile(
    r"\b(?:(?:next|this|ensi|tänä|nästa|denna)\s+(?:viikon\s+|veckas?\s+)?)?(?:"
    r"(?P<wd_en>" + _EN_WD + r")(?:'s)?"
    r"|(?P<wd_fi>" + _FI_WD + r")(?:na|nä|hin|on|oon|ksi|lle|n|lta|ltä)?"
    r"|(?P<wd_sv>" + _SV_WD + r")(?:en|ens|s)?"
    r")\b",
    re.I,
)


def resolve_date(text: str, now: datetime | None = None) -> tuple[date, str] | None:
    """Resolve the delivery date mentioned in text relative to now (Europe/Helsinki).
    Weekday names mean the next such day strictly after today."""
    now = (now or now_helsinki()).astimezone(HELSINKI)
    today = now.date()
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])), m[0]
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)", t)
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
    month_pat = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th|:a|:e)?\s+(" + month_pat + r")\b", t) or \
        re.search(r"\b(" + month_pat + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
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
    if re.search(r"\bday after tomorrow\b|\bylihuomenna\b|\bylihuomiseksi\b|\bi\s+övermorgon\b|\bövermorgon\b", t):
        return today + timedelta(days=2), "day after tomorrow"
    if re.search(r"\btomorrow\b|\bhuomenna\b|\bhuomiseksi\b|\bimorgon\b|\bi\s+morgon\b", t):
        return today + timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b|\btänään\b|\btonight\b|\bidag\b|\bi\s+dag\b|\bikväll\b|\bi\s+kväll\b", t):
        return today, "today"
    m = _WEEKDAY_SEARCH_RE.search(t)
    if m:
        key = (m.group("wd_en") or m.group("wd_fi") or m.group("wd_sv") or "").lower()
        wd = WEEKDAYS[key]
        ahead = (wd - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), m.group(0)
    m = re.search(r"\b(next\s+week|ensi\s+viikolla|ensi\s+viikon|nästa\s+vecka)\b", t)
    if m:
        return today + timedelta(days=7 - today.weekday()), m.group(0)
    return None


_CLAIM_PATTERNS = [
    (r"lactose[- ]?free|without lactose|no lactose|\blaktoosito\w*|\blaktoosittom\w*|\blaktosfri\w*", "FREE_FROM_LACTOSE"),
    (r"low[- ]lactose|\bvähälaktoos\w*|\blaktosreducer\w*", "LOW_LACTOSE"),
    (r"gluten[- ]?free|coeliac|celiac|\bgluteenito\w*|\bgluteenittom\w*|\bglutenfri\w*|\bkelia\w*", "FREE_FROM_GLUTEN"),
    (r"\bvegan\b|\bvegaan\w*|\bvegansk\w*", "VEGAN"),
    (r"vegetarian|\bkasvis\w*|\bvegetarisk\w*", "VEGETARIAN"),
    (r"\borganic\b|\bluomu\w*|\bekologisk\w*", "ORGANIC"),
    (r"egg[- ]free|\bmunato\w*|\bmunattom\w*|\bkananmunato\w*|\bkananmunattom\w*|\bäggfri\w*", "FREE_FROM_EGGS"),
    (r"soy[- ]free|\bsoijato\w*|\bsoijattom\w*|\bsojafri\w*", "FREE_FROM_SOY"),
    (
        r"no added sugar|sugar[- ]free|\bsokerito\w*|\bsokerittom\w*|\bsockerfri\w*|"
        r"ei lisättyä sokeria|utan tillsatt socker",
        "NO_ADDED_SUGARS",
    ),
]
_ALLERGEN_PATTERNS = [
    (
        r"milk[- ]allerg|dairy[- ]allerg|dairy[- ]free|milk[- ]free|cow'?s milk protein|"
        r"\bmaito-?allerg\w*|\bmaidoto\w*|\bmaidottom\w*|\bmjölk-?allerg\w*|\bmjölkfri\w*",
        "milk",
    ),
    (r"\bnuts?[- ]allerg|\bnut[- ]free|\btree[- ]?nuts?\b|\bpähkinä\w*|\bnöt-?allerg\w*|\bnötfri\w*", "nuts"),
    (r"\bpeanuts?\b|\bmaapähkinä\w*|\bjordnöt\w*", "peanuts"),
    (
        r"gluten[- ]?free|gluten[- ]allerg|coeliac|celiac|"
        r"\bkelia\w*|\bgluteenito\w*|\bgluteenittom\w*|\bglutenfri\w*",
        "gluten",
    ),
    (
        r"\beggs?[- ]allerg|\begg[- ]free|\bkananmuna-?allerg\w*|\bmuna-?allerg\w*|"
        r"\bmunato\w*|\bmunattom\w*|\bkananmunato\w*|\bkananmunattom\w*|\bägg-?allerg\w*|\bäggfri\w*",
        "eggs",
    ),
    (r"\bfish[- ]allerg|\bkala-?allerg\w*|\bfisk-?allerg\w*", "fish"),
    (
        r"\bsoy[- ]allerg|\bsoy[- ]free|\bsoija-?allerg\w*|\bsoijato\w*|\bsoijattom\w*|"
        r"\bsoja-?allerg\w*|\bsojafri\w*",
        "soy",
    ),
    (r"\bsesame\b|\bseesam\w*|\bsesam\w*", "sesame"),
    (r"\bcelery\b|\bselleri\w*", "celery"),
    (r"\bmustard\b|\bsinappi\w*|\bsenap\w*", "mustard"),
    (r"\bcrustaceans?\b|\bshellfish[- ]allerg|\bäyriäis-?allerg\w*|\bskaldjurs?-?allerg\w*", "crustaceans"),
    (r"\bmolluscs?\b|\bnilviäis-?allerg\w*|\bblötdjurs?-?allerg\w*", "molluscs"),
    (r"\blupin\b|\blupiini-?allerg\w*", "lupin"),
    (r"\bsulphites?\b|\bsulfites?\b|\bsulfiitti-?allerg\w*|\bsulfit-?allerg\w*", "sulphites"),
]
_ALLERGEN_WORD_MAP = [
    (r"\b(?:milk|dairy|mai[dt]o\w*|mjölk\w*)\b", "milk"),
    (r"\b(?:tree\s+nuts?|nuts?|pähkin\w*|nöt(?:ter|allerg\w*)?)\b", "nuts"),
    (r"\b(?:peanuts?|maapähkin\w*|jordnöt\w*)\b", "peanuts"),
    (r"\b(?:gluten|wheat|gluteeni\w*|vehnä\w*|gluten\w*|vete\w*)\b", "gluten"),
    (r"\b(?:eggs?|kananmun\w*|muna\w*|ägg\w*)\b", "eggs"),
    (r"\b(?:fish|kala\w*|fisk\w*)\b", "fish"),
    (r"\b(?:soy|soya|soija\w*|soja\w*)\b", "soy"),
    (r"\b(?:sesame|seesam\w*|sesam\w*)\b", "sesame"),
    (r"\b(?:celery|selleri\w*)\b", "celery"),
    (r"\b(?:mustard|sinapp\w*|senap\w*)\b", "mustard"),
    (r"\b(?:crustaceans?|shellfish|äyriäis\w*|skaldjur\w*)\b", "crustaceans"),
    (r"\b(?:molluscs?|nilviäis\w*|blötdjur\w*)\b", "molluscs"),
    (r"\b(?:lupin|lupiini\w*)\b", "lupin"),
    (r"\b(?:sulphites?|sulfites?|sulfiitt\w*|sulfit\w*)\b", "sulphites"),
]
_ALLERGEN_ITEM = (
    r"(?:cow'?s\s+milk|tree\s+nuts?|milk|dairy|nuts?|peanuts?|gluten|wheat|eggs?|fish|soya?|sesame|"
    r"celery|mustard|shellfish|crustaceans?|molluscs?|lupin|sulphites?|sulfites?|"
    r"maito|maido\w*|pähkin\w*|maapähkin\w*|muna|kananmun\w*|kala|soija|gluteeni|vehnä|seesam\w*|selleri\w*|sinapp\w*|"
    r"mjölk|nötter|jordnötter|jordnöts?|nöt|ägg|fisk|soja|vete|sesam|senap|skaldjurs?|blötdjurs?|sulfit)"
)
_ALLERGY_SEP = (
    r"(?:\s*-\s*,\s*"
    r"|\s*-?\s*(?:and|or|ja|tai|sekä|och|eller)\s+"
    r"|,\s*(?:and|or|ja|tai|sekä|och|eller)\s+"
    r"|,\s*)"
)
_ALLERGY_LIST_RE = re.compile(
    r"\b(" + _ALLERGEN_ITEM + r"(?:" + _ALLERGY_SEP + _ALLERGEN_ITEM + r")*)\s*-?\s*(?:allerg\w*|intoleran\w*|yliherk\w*)"
    r"|\b(?:allerg(?:ic|y|ies)\s+to|allerg(?:isk\w*|i\w*)\s+mot|allergi\w*\s+|yliherk\w*\s+)([^.;:!?\n]+)",
    re.I,
)


def detect_claims(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(c for p, c in _CLAIM_PATTERNS if re.search(p, t)))


def detect_allergens(text: str) -> list[str]:
    t = text.lower()
    hits: list[tuple[int, str]] = []
    for m in _ALLERGY_LIST_RE.finditer(t):
        grp = 1 if m.group(1) is not None else 2
        span = m.group(grp) or ""
        base = m.start(grp)
        for p, a in _ALLERGEN_WORD_MAP:
            sm = re.search(p, span)
            if sm:
                hits.append((base + sm.start(), a))
    for p, a in _ALLERGEN_PATTERNS:
        pm = re.search(p, t)
        if pm:
            hits.append((pm.start(), a))
    hits.sort(key=lambda x: x[0])
    return list(dict.fromkeys(a for _, a in hits))


_DATE_PREP = (
    r"(?:(?:by|on|for|before|until|till|due|delivered|delivery|"
    r"på|senast|före|innan|leverans|toimitus|viimeistään)\s+){0,3}"
)
_WEEK_MOD = (
    r"(?:(?:next\s+week(?:'s)?|this\s+week(?:'s)?|ensi\s+viikon|ensi\s+viikolla|"
    r"tämän\s+viikon|nästa\s+veckas?|next|this|ensi|tänä|nästa|denna)\s+){0,2}"
)
_WD_ANY = (
    r"(?:(?:" + _EN_WD + r")(?:'s)?"
    r"|(?:" + _FI_WD + r")(?:na|nä|hin|on|oon|ksi|lle|n|lta|ltä)?"
    r"|(?:" + _SV_WD + r")(?:en|ens|s)?)"
)
_MEAL = (
    r"(?:\s+(?:lunch|dinner|evening|morning|breakfast|night|afternoon|service|"
    r"lounaalle|lounaaksi|lounas|aamiaiselle|aamiainen|päivälliselle|illalliselle|frukost|middag))?"
)
_POST_DATE = r"(?:\s+mennessä)?"
_MONTH_PAT = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_PHRASE = re.compile(
    r"\b" + _DATE_PREP + _WEEK_MOD + _WD_ANY + r"\b" + _MEAL + _POST_DATE
    + r"|\b" + _DATE_PREP
    + r"(?:day after tomorrow|tomorrow|today|tonight|next week|this week|"
    + r"ylihuomenna|ylihuomiseksi|huomenna|huomiseksi|tänään|ensi viikolla|ensi viikon|"
    + r"i övermorgon|övermorgon|imorgon|i morgon|idag|i dag|ikväll|i kväll|nästa vecka)\b" + _MEAL + _POST_DATE
    + r"|\b" + _DATE_PREP + r"\d{4}-\d{2}-\d{2}\b" + _POST_DATE
    + r"|\b" + _DATE_PREP + r"\d{1,2}\.\d{1,2}\.?(?:\d{2,4})?" + _POST_DATE
    + r"|\b" + _DATE_PREP + r"\d{1,2}(?:st|nd|rd|th|:a|:e)?\s+(?:" + _MONTH_PAT + r")\b" + _POST_DATE
    + r"|\b" + _DATE_PREP + r"(?:" + _MONTH_PAT + r")\s+\d{1,2}(?:st|nd|rd|th)?\b" + _POST_DATE,
    re.I,
)
_ALLERGY_CLAUSE = re.compile(r"allerg\w*|intoleran\w*|coeliac|celiac|kelia\w*|yliherk\w*|cannot eat|can't eat", re.I)
_META_CLAUSE = re.compile(
    r"^\s*(?:please\s+)?(?:check|inspect|review|verify)\s+(?:the\s+)?(?:product\s+)?(?:details?|alternatives?|ingredients?)\b",
    re.I,
)
_LEAD_FILLER = {
    "we", "i", "need", "needs", "want", "please", "order", "get", "buy", "some", "about", "around",
    "approx", "approx.", "the", "a", "an", "urgent", "urgently", "also", "of", "just", "more", "x",
    "me", "meille", "tarvitsemme", "tarvitaan", "tarvitsisimme", "tilaa", "tilataan", "tilaisimme",
    "haluamme", "haluaisimme", "pyydän", "pyydämme", "saisimmeko", "saisiko", "kiitos", "noin",
    "vi", "behöver", "vill", "ha", "beställa", "beställ", "tack", "gärna", "vänligen", "snälla",
    "ungefär", "cirka", "ca", "av",
}
_TAIL = re.compile(
    r"\s+(?:at|for|by|on|to|before|delivered|until|till|with|because|from|in|"
    r"till|senast|för|på|leverans|toimitus|mennessä|koululle|hotellille|sairaalalle|"
    r"keliaakik\w*|oppilaill\w*|potilaill\w*)\b.*$"
    r"|\s+med\s+[^.;:!?\n]*(?:allerg\w*|intoleran\w*).*$"
    r"|\s+\S+\s+varten\b.*$"
    r"|\s+varten\b.*$"
    r"|\s+(?:and\s+)?(?:please\s+)?(?:check|inspect|review|verify)\s+(?:the\s+)?(?:product\s+)?(?:details?|alternatives?|ingredients?)\b.*$",
    re.I,
)
_PRODUCT_NORMALIZATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bluomu(?=(?:mai[dt]o|kerm|voi|kaura|juust|jogurt|leip|leiv|ruis|kananmun))", re.I), "organic "),
    (re.compile(r"\b(?:laktoosito\w*|laktoosittom\w*|laktosfri\w*)\b", re.I), "lactose-free"),
    (re.compile(r"\b(?:vähälaktoos\w*|laktosreducer\w*)\b", re.I), "low-lactose"),
    (re.compile(r"\b(?:gluteenito\w*|gluteenittom\w*|glutenfri\w*)\b", re.I), "gluten-free"),
    (re.compile(r"\b(?:maidoto\w*|maidottom\w*|mjölkfri\w*)\b", re.I), "dairy-free"),
    (re.compile(r"\b(?:munato\w*|munattom\w*|kananmunato\w*|kananmunattom\w*|äggfri\w*)\b", re.I), "egg-free"),
    (re.compile(r"\b(?:soijato\w*|soijattom\w*|sojafri\w*)\b", re.I), "soy-free"),
    (re.compile(r"\b(?:sokerito\w*|sokerittom\w*|sockerfri\w*)\b", re.I), "sugar-free"),
    (re.compile(r"\b(?:luomu\w*|ekologisk\w*)\b", re.I), "organic"),
    (re.compile(r"\b(?:vegaan\w*|vegansk\w*)\b", re.I), "vegan"),
    (re.compile(r"\b(?:kaurajuom\w*|havredryck\w*)\b", re.I), "oat drink"),
    (re.compile(r"\b(?:kauramai[dt]o\w*|havremjölk\w*)\b", re.I), "oat milk"),
    (re.compile(r"\b(?:soijajuom\w*|sojadryck\w*)\b", re.I), "soy drink"),
    (re.compile(r"\b(?:soijamai[dt]o\w*|sojamjölk\w*)\b", re.I), "soy milk"),
    (re.compile(r"\b(?:mantelijuom\w*|mandeldryck\w*)\b", re.I), "almond drink"),
    (re.compile(r"\b(?:mantelimai[dt]o\w*|mandelmjölk\w*)\b", re.I), "almond milk"),
    (re.compile(r"\b(?:kookosmai[dt]o\w*|kokosmjölk\w*)\b", re.I), "coconut milk"),
    (re.compile(r"\b(?:riisimai[dt]o\w*|rismjölk\w*)\b", re.I), "rice milk"),
    (re.compile(r"\b(?:ruokakerm\w*|matlagningsgrädd\w*)\b", re.I), "cooking cream"),
    (re.compile(r"\b(?:vispikerm\w*|kuohukerm\w*|vispgrädd\w*)\b", re.I), "whipping cream"),
    (re.compile(r"\b(?:kaurakerm\w*|havregrädd\w*)\b", re.I), "oat cream"),
    (re.compile(r"\b(?:kerm\w*|grädd\w*)\b", re.I), "cream"),
    (re.compile(r"\b(?:kevytmai[dt]o\w*|lättmjölk\w*)\b", re.I), "semi-skimmed milk"),
    (
        re.compile(
            r"\b(?:(?:rasvato\w*|rasvattom\w*)\s+mai[dt]o\w*|rasvatonmai[dt]o\w*|fettfri\w*\s+mjölk\w*|minimjölk\w*)\b",
            re.I,
        ),
        "skimmed milk",
    ),
    (re.compile(r"\b(?:täysmai[dt]o\w*|helmjölk\w*|standardmjölk\w*)\b", re.I), "whole milk"),
    (re.compile(r"\b(?:mai[dt]ojauhe\w*|mjölkpulver\w*)\b", re.I), "milk powder"),
    (re.compile(r"\b(?:maito(?:a|on|ja|jen)?|maidon|maidot|maitojuom\w*|mjölk(?:en|dryck\w*)?)\b", re.I), "milk"),
    (re.compile(r"\b(?:(?:suolato\w*|suolattom\w*)\s+voi(?:ta|n)?|osaltat\s+smör)\b", re.I), "unsalted butter"),
    (
        re.compile(
            r"\b(?:(?:suolattu\w*|suolai[sn]\w*)\s+voi(?:ta|n)?|suolavoi(?:ta|n)?|(?:normal)?saltat\s+smör)\b",
            re.I,
        ),
        "salted butter",
    ),
    (re.compile(r"\b(?:maapähkinävoi\w*|jordnötssmör\w*)\b", re.I), "peanut butter"),
    (re.compile(r"\b(?:voi(?:ta|n)?|smör(?:et)?)\b", re.I), "butter"),
    (re.compile(r"\b(?:ruisleip\w*|ruisleiv\w*|rågbröd\w*)\b", re.I), "rye bread"),
    (re.compile(r"\b(?:kauraleip\w*|kauraleiv\w*|havrebröd\w*)\b", re.I), "oat bread"),
    (re.compile(r"\b(?:paahtoleip\w*|paahtoleiv\w*|rostbröd\w*)\b", re.I), "toast bread"),
    (re.compile(r"\b(?:leip\w*|leiv(?:än|ät|ä)|bröd\w*)\b", re.I), "bread"),
    (re.compile(r"\b(?:jogurtt\w*|jogurt\w*|yoghurt\w*)\b", re.I), "yogurt"),
    (re.compile(r"\b(?:juustoraast\w*|riven\s+ost)\b", re.I), "grated cheese"),
    (re.compile(r"\b(?:juust\w*|ost(?:en|ar)?)\b", re.I), "cheese"),
    (re.compile(r"\b(?:rahk\w*|kvarg\w*)\b", re.I), "quark"),
    (re.compile(r"\b(?:piim\w*|filmjölk\w*)\b", re.I), "buttermilk"),
]


def _normalize_product_query(q: str) -> str:
    s = q
    for pat, repl in _PRODUCT_NORMALIZATIONS:
        s = pat.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip(" -'\"")


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[.;:!?\n](?!\d)|,(?!\d)", text) if c.strip()]


def _residual(clause: str) -> str:
    """The product noun phrase of a clause: quantities, date phrases, filler and trailing
    'for/at/by ...' tails removed. '120 l oat drink for Wednesday lunch' -> 'oat drink'."""
    words = re.sub(r"\s+", " ", _QTY_RE.sub(" ", clause)).strip().split(" ")
    while words and words[0].lower() in _LEAD_FILLER:
        words.pop(0)
    s = _TAIL.sub("", " ".join(words))
    s = re.sub(r"\s+", " ", _DATE_PHRASE.sub(" ", s)).strip()
    s = _TAIL.sub("", s)
    words = [w for w in s.split(" ") if w]
    while words and words[0].lower() in _LEAD_FILLER:
        words.pop(0)
    while words and words[-1].lower() in _LEAD_FILLER:
        words.pop()
    return _normalize_product_query(" ".join(words).strip(" -'\""))


def split_items(text: str) -> list[dict[str, Any]]:
    """Deterministic multi-item split: one item per quantity, named by its noun phrase. A quantity
    without a name ('200 L by Wednesday') takes the next unused product phrase from a clause
    without a quantity ('Oat drink for a school'). Allergy clauses never name a product."""
    items: list[dict[str, Any]] = []
    names: list[str] = []
    for clause in _clauses(text):
        n_qty = len(list(_QTY_RE.finditer(clause)))
        if n_qty == 0:
            if _ALLERGY_CLAUSE.search(clause) or _META_CLAUSE.search(clause):
                continue
            r = _residual(clause)
            if r:
                names.append(r)
            continue
        parts = re.split(r"\s+(?:and|plus|ja|och|&|\+)\s+", clause, flags=re.I) if n_qty > 1 else [clause]
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
        if not _ALLERGY_CLAUSE.search(clause) and not _META_CLAUSE.search(clause):
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
