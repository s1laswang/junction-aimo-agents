"""Content per sales unit (litres / kg / pieces) from static product data.

This is the main bug risk in the whole catalog: a sales unit (KI, ST, PAK ...) can be a single
1 l carton, a case of 2 x 10 l bag-in-box, or a 100 x 2 cl portion box. We compute
content_amount/content_unit for ONE sales unit from, in priority order:

1. synkkaData.unitConversions[BASE_UNIT_OR_EACH].volume / netWeight (content of one base unit)
2. the product name ("1 l", "2,5 dl", "12 x 1 L", "100x2 cl", "400g", "5 kg")
3. baseUnit == KG (variable weight goods: 1 base unit = 1 kg)

then multiply by units[salesUnit].sizeInBaseUnits. When a live price is available,
`reconcile_with_price` cross-checks against catalogPrice / comparisonPrice and the price-derived
value wins if they disagree by more than 3 %. No LLM is ever involved in quantities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

VOLUME_TO_L = {"LTR": 1.0, "L": 1.0, "MLT": 0.001, "ML": 0.001, "CLT": 0.01, "CL": 0.01,
               "DLT": 0.1, "DL": 0.1}
WEIGHT_TO_KG = {"KGM": 1.0, "KG": 1.0, "GRM": 0.001, "G": 0.001, "GR": 0.001, "MGM": 1e-6}

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(ml|cl|dl|l|ltr|litra?a?|kg|g|gr)"
# "12 x 1 L", "100x2 cl", "6 x 2 dl", "14 x Arla Low fat milkdrink 2 dl"
_MULTI_RE = re.compile(rf"(?<![\w.,])(\d+)\s*[x×]\s*{_NUM}\s*{_UNIT}(?![a-zåäö])", re.I)
_QTY_RE = re.compile(rf"(?<![\w.,]){_NUM}\s*{_UNIT}(?![a-zåäö])", re.I)


@dataclass
class Qty:
    amount: float          # in L or KG
    unit: str              # "L" | "KG"


@dataclass
class Content:
    amount: Optional[float]          # per ONE sales unit
    unit: Optional[str]              # "L" | "KG" | "PCS"
    source: str                      # how we got it (for audit / UI)
    base_amount: Optional[float] = None   # per ONE base unit
    sales_unit_size: float = 1.0          # base units per sales unit


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _norm_unit(u: str) -> tuple[str, float]:
    u = u.lower()
    if u in ("l", "ltr", "litra", "litraa"):
        return "L", 1.0
    if u == "dl":
        return "L", 0.1
    if u == "cl":
        return "L", 0.01
    if u == "ml":
        return "L", 0.001
    if u == "kg":
        return "KG", 1.0
    return "KG", 0.001  # g, gr


_ESTIMATE_RE = re.compile(r"(?<![\w])[e℮]\s?(?=\d)", re.I)   # "e1 kg", "℮200 g" (estimated sign)
_MULT_TOKEN_RE = re.compile(r"(?<![\w.,])(\d+)\s*[x×](?=\s|\d|[a-zåäö])|(?<![\w.,])(\d+)-pack", re.I)
# drained / net pair: "340g/285g", "3/1,5 kg", "850/500 g", "1KG/,64KG" -> second value (drained weight)
_PAIR_RE = re.compile(rf"(?<![\w.,]){_NUM}\s*{_UNIT}?\s*/\s*(\d*[.,]?\d+)\s*{_UNIT}(?![a-zåäö])", re.I)


def drained_quantity(name: str) -> Optional[Qty]:
    """'Biona Sweetcorn 340g/285g' -> 0.285 KG (drained weight; the price API compares on it).
    Mixed weight/volume pairs ('1000ml/1090g', '280g/425ml') -> the volume."""
    if not name:
        return None
    name = _ESTIMATE_RE.sub("", name)
    m = _PAIR_RE.search(name)
    if not m:
        return None
    a, ua, b, ub = m.group(1), m.group(2), m.group(3), m.group(4)
    ub_n, fb = _norm_unit(ub)
    ua_n, fa = _norm_unit(ua) if ua else (ub_n, fb)
    if ua_n != ub_n:  # weight/volume pair: prefer volume
        return Qty(_to_float(a) * fa, "L") if ua_n == "L" else Qty(_to_float(b) * fb, "L")
    return Qty(_to_float(b) * fb, ub_n)


def parse_name_quantity(name: str) -> tuple[Optional[Qty], int]:
    """Parse a product name. Returns (quantity of ONE inner item, multiplier N).

    "Valio Eila semi skimmed milk drink 10 l novobox" -> (10 L, 1)
    "Arla Portion milk 100x2 cl"                       -> (0.02 L, 100)
    "14 x Arla Low fat milkdrink 2 dl"                 -> (0.2 L, 14)
    "Valio Eila 1,75 l lactose free"                   -> (1.75 L, 1)
    """
    if not name:
        return None, 1
    name = _ESTIMATE_RE.sub("", name)
    name = re.sub(r"\([^)]*\)", " ", name)   # "4 x 1200g (~20x60g)" -> ignore the parenthetical
    mult = 1
    for a, b in _MULT_TOKEN_RE.findall(name):   # nested: "4 x 6 x 0,33 l", "6x 4-pack 0,5 L"
        mult *= int(a or b)
    m = _MULTI_RE.search(name)
    if m:
        unit, f = _norm_unit(m.group(3))
        return Qty(_to_float(m.group(2)) * f, unit), max(mult, 1)
    found = _QTY_RE.findall(name)
    if not found:
        return None, mult
    # take the last quantity that is not a percentage context (names put size near the end)
    num, u = found[-1] if len(found) == 1 else _pick_qty(found)
    unit, f = _norm_unit(u)
    return Qty(_to_float(num) * f, unit), mult


def _pick_qty(found: list[tuple[str, str]]) -> tuple[str, str]:
    # prefer volume/weight with the largest absolute size (e.g. "10 l ... 2 dl spout" is rare);
    # in practice names have one size; return the first one.
    return found[0]


def _conv_level(raw: dict, level: str) -> Optional[dict]:
    for uc in (raw.get("synkkaData") or {}).get("unitConversions") or []:
        if uc.get("synkkaPackagingType") == level:
            return uc
    return None


def _conv_qty(uc: Optional[dict]) -> Optional[Qty]:
    """Volume first (litres), else net weight (kg). Handles the data quirk volume.unit == GRM."""
    if not uc:
        return None
    vol = uc.get("volume") or {}
    vu, vv = (vol.get("unit") or "").upper(), vol.get("value")
    if vv:
        if vu in VOLUME_TO_L:
            return Qty(float(vv) * VOLUME_TO_L[vu], "L")
        if vu in WEIGHT_TO_KG:  # quirk: some products give "volume" in GRM
            return Qty(float(vv) * WEIGHT_TO_KG[vu], "KG")
    nw = uc.get("netWeight") or {}
    nu, nv = (nw.get("unit") or "").upper(), nw.get("value")
    if nv and nu in WEIGHT_TO_KG:
        return Qty(float(nv) * WEIGHT_TO_KG[nu], "KG")
    return None


def sales_unit_size(raw: dict) -> Optional[float]:
    """How many base units ONE sales unit contains (units[].sizeInBaseUnits)."""
    su, bu = raw.get("salesUnit"), raw.get("baseUnit")
    for u in raw.get("units") or []:
        if u.get("unitId") == su and u.get("sizeInBaseUnits"):
            return float(u["sizeInBaseUnits"])
    if su == bu:
        return 1.0
    return None


def english_or_finnish_name(raw: dict) -> str:
    names = {n.get("language"): n.get("value") for n in (raw.get("synkkaData") or {}).get("names") or []}
    return names.get("en") or names.get("fi") or names.get("sv") or ""


def compute_content(raw: dict) -> Content:
    """Content of ONE sales unit, from static data only."""
    size = sales_unit_size(raw)
    size_known = size is not None
    size = size or 1.0
    name = english_or_finnish_name(raw)
    names_all = [n.get("value") or "" for n in (raw.get("synkkaData") or {}).get("names") or []]

    base: Optional[Qty] = None
    src = ""
    if (raw.get("salesUnit") or "").upper() == "KG":
        # sold by weight: one sales unit IS one kilogram (unitConversions then describe one piece)
        return Content(1.0, "KG", "salesUnit=KG", 1.0, 1.0)
    q, mult = parse_name_quantity(name)
    if q is None:
        for alt in names_all:
            q, mult = parse_name_quantity(alt)
            if q:
                break
    if q is not None and mult > 1:
        # "12 x 1 L", "100x2 cl", "4 x 6 x 0,33 l": in this catalog the multipack in the name
        # describes the whole sales unit (verified against live comparison prices on a sample).
        total = q.amount * mult
        return Content(round(total, 6), q.unit, f"name(multipack {mult} x {q.amount:g} {q.unit} = sales unit)",
                       round(total / size, 6) if size else None, size)
    conv = _conv_qty(_conv_level(raw, "BASE_UNIT_OR_EACH"))
    drained = drained_quantity(name)
    if drained and (not conv or conv.unit != drained.unit or abs(conv.amount - drained.amount) > 1e-9):
        base, src = drained, "name(drained/net pair)"
    elif conv:
        base, src = conv, "unitConversions.base"
    elif q is not None:
        base, src = q, "name"
    elif (raw.get("baseUnit") or "").upper() == "KG":
        base, src = Qty(1.0, "KG"), "baseUnit=KG"

    if base is None:
        # last resort: CASE-level conversion describes the sales unit when it is a case
        case = _conv_qty(_conv_level(raw, "CASE"))
        if case and raw.get("salesUnit") != raw.get("baseUnit"):
            return Content(round(case.amount, 6), case.unit, "unitConversions.case", None, size)
        return Content(size, "PCS", "pieces(no size data)" + ("" if size_known else ";size_assumed"), 1.0, size)

    amount = base.amount * size
    if size != 1.0:
        src += f" x {size:g} base units per sales unit"
    if not size_known:
        src += ";sales_unit_size_assumed_1"
    return Content(round(amount, 6), base.unit, src, round(base.amount, 6), size)


def reconcile_with_price(content_amount: Optional[float], content_unit: Optional[str],
                         unit_price: Optional[float], comparison_price: Optional[float],
                         comparison_unit: Optional[str], tol: float = 0.03) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Cross-check static content with live price. Returns (amount, unit, note).

    price-derived content = unit_price / comparison_price (per L or KG). The comparison price is
    rounded to cents, so allow `tol` relative error; beyond that the price-derived value wins.
    """
    if not unit_price or not comparison_price or comparison_unit not in ("L", "KG"):
        return content_amount, content_unit, None
    derived = unit_price / comparison_price
    if content_amount and content_unit == comparison_unit:
        if abs(derived - content_amount) / content_amount <= tol:
            return content_amount, content_unit, None
    # disagree (or static missing / different unit family): trust price, snap to a clean number
    snapped = _snap(derived)
    note = (f"content per sales unit from price ({unit_price:.2f} / {comparison_price:.2f} per {comparison_unit}"
            f" = {snapped:g} {comparison_unit}); static data said "
            f"{content_amount if content_amount is not None else 'n/a'} {content_unit or ''}".strip())
    return snapped, comparison_unit, note


def _snap(x: float) -> float:
    """19.94 -> 20.0, 4.98 -> 5.0, 0.333 -> 0.333 (snap within 1.5 % to a 'nice' value)."""
    for step in (1.0, 0.5, 0.25, 0.1, 0.05, 0.01):
        nice = round(x / step) * step
        if nice > 0 and abs(nice - x) / x <= 0.015:
            return round(nice, 4)
    return round(x, 3)


def fat_pct(raw_nutrition: dict[str, float], name: str) -> Optional[float]:
    if "FAT" in raw_nutrition:
        return float(raw_nutrition["FAT"])
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", name or "")
    if m:
        return _to_float(m.group(1))
    n = (name or "").lower()
    if "semi skimmed" in n or "semi-skimmed" in n or "kevyt" in n:
        return 1.5
    if "skimmed" in n or "fat free" in n or "fatfree" in n or "rasvaton" in n:
        return 0.0
    if "whole milk" in n or "täysmaito" in n:
        return 3.5
    return None


def describe_pack(amount: Optional[float], unit: Optional[str], size: float, base_amount: Optional[float],
                  sales_unit: str) -> str:
    """'2 x 10 L (20 L per KI)' style label."""
    if amount is None or unit is None:
        return sales_unit
    def f(x: float) -> str:
        return f"{x:g}"
    if unit == "PCS":
        return f"{f(amount)} pcs per {sales_unit}"
    if size and size > 1 and base_amount:
        return f"{f(size)} x {f(base_amount)} {unit} ({f(amount)} {unit} per {sales_unit})"
    return f"{f(amount)} {unit} per {sales_unit}"


def as_dict(c: Content) -> dict[str, Any]:
    return {"content_amount": c.amount, "content_unit": c.unit, "content_source": c.source,
            "base_content_amount": c.base_amount, "sales_unit_size": c.sales_unit_size}
