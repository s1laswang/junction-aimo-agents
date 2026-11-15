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

import math
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


_MAX_NAME_LEN = 1000
_MAX_MULT = 100_000


def _to_float(s: Any) -> Optional[float]:
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        try:
            fv = float(s)
        except OverflowError:
            return None
        return fv if math.isfinite(fv) else None
    if not isinstance(s, str):
        return None
    st = s.strip().replace(",", ".")
    if not st or len(st) > _MAX_NAME_LEN:
        return None
    try:
        fv = float(st)
    except (ValueError, OverflowError):
        return None
    return fv if math.isfinite(fv) else None


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


def _pos_finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        fv = float(v)
    except OverflowError:
        return None
    if not math.isfinite(fv) or fv <= 0.0:
        return None
    return fv


def _mul_pos(a: Any, b: Any) -> Optional[float]:
    fa = _pos_finite(a)
    fb = _pos_finite(b)
    if fa is None or fb is None:
        return None
    prod = fa * fb
    if not math.isfinite(prod) or prod <= 0.0:
        return None
    return prod


def _div_pos(a: Any, b: Any) -> Optional[float]:
    fa = _pos_finite(a)
    fb = _pos_finite(b)
    if fa is None or fb is None:
        return None
    quot = fa / fb
    if not math.isfinite(quot) or quot <= 0.0:
        return None
    return quot


def _round_pos(x: float, ndigits: int = 6) -> float:
    r = round(x, ndigits)
    if math.isfinite(r) and r > 0.0:
        return r
    return x


def drained_quantity(name: str) -> Optional[Qty]:
    """'Biona Sweetcorn 340g/285g' -> 0.285 KG (drained weight; the price API compares on it).
    Mixed weight/volume pairs ('1000ml/1090g', '280g/425ml') -> the volume."""
    if not isinstance(name, str) or not name:
        return None
    name = _ESTIMATE_RE.sub("", name[:_MAX_NAME_LEN])
    m = _PAIR_RE.search(name)
    if not m:
        return None
    a, ua, b, ub = m.group(1), m.group(2), m.group(3), m.group(4)
    ub_n, fb = _norm_unit(ub)
    ua_n, fa = _norm_unit(ua) if ua else (ub_n, fb)
    if ua_n != ub_n:
        amt = _mul_pos(_to_float(a), fa) if ua_n == "L" else _mul_pos(_to_float(b), fb)
        return Qty(amt, "L") if amt is not None else None
    amt = _mul_pos(_to_float(b), fb)
    return Qty(amt, ub_n) if amt is not None else None


def parse_name_quantity(name: str) -> tuple[Optional[Qty], int]:
    """Parse a product name. Returns (quantity of ONE inner item, multiplier N).

    "Valio Eila semi skimmed milk drink 10 l novobox" -> (10 L, 1)
    "Arla Portion milk 100x2 cl"                       -> (0.02 L, 100)
    "14 x Arla Low fat milkdrink 2 dl"                 -> (0.2 L, 14)
    "Valio Eila 1,75 l lactose free"                   -> (1.75 L, 1)
    """
    if not isinstance(name, str) or not name:
        return None, 1
    name = _ESTIMATE_RE.sub("", name[:_MAX_NAME_LEN])
    name = re.sub(r"\([^)]*\)", " ", name)
    mult = 1
    for a, b in _MULT_TOKEN_RE.findall(name):
        tok = (a or b).lstrip("0")
        if not tok:
            continue
        if len(tok) > 6:
            mult = _MAX_MULT
            continue
        k = int(tok)
        if k > 0:
            mult = min(mult * k, _MAX_MULT)
    m = _MULTI_RE.search(name)
    if m:
        unit, f = _norm_unit(m.group(3))
        amt = _mul_pos(_to_float(m.group(2)), f)
        return (Qty(amt, unit) if amt is not None else None), max(mult, 1)
    found = _QTY_RE.findall(name)
    if not found:
        return None, mult
    num, u = found[-1] if len(found) == 1 else _pick_qty(found)
    unit, f = _norm_unit(u)
    amt = _mul_pos(_to_float(num), f)
    return (Qty(amt, unit) if amt is not None else None), mult


def _pick_qty(found: list[tuple[str, str]]) -> tuple[str, str]:
    # prefer volume/weight with the largest absolute size (e.g. "10 l ... 2 dl spout" is rare);
    # in practice names have one size; return the first one.
    return found[0]


def _conv_level(raw: dict, level: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    sd = raw.get("synkkaData")
    if not isinstance(sd, dict):
        return None
    ucs = sd.get("unitConversions")
    if not isinstance(ucs, (list, tuple)):
        return None
    for uc in ucs:
        if isinstance(uc, dict) and uc.get("synkkaPackagingType") == level:
            return uc
    return None


def _conv_qty(uc: Optional[dict]) -> Optional[Qty]:
    """Volume first (litres), else net weight (kg). Handles the data quirk volume.unit == GRM."""
    if not isinstance(uc, dict):
        return None
    vol = uc.get("volume")
    if isinstance(vol, dict):
        vu = vol.get("unit")
        vu_s = vu.upper() if isinstance(vu, str) else ""
        vv = _pos_finite(vol.get("value"))
        if vv is not None:
            if vu_s in VOLUME_TO_L:
                amt = _mul_pos(vv, VOLUME_TO_L[vu_s])
                if amt is not None:
                    return Qty(amt, "L")
            if vu_s in WEIGHT_TO_KG:
                amt = _mul_pos(vv, WEIGHT_TO_KG[vu_s])
                if amt is not None:
                    return Qty(amt, "KG")
    nw = uc.get("netWeight")
    if isinstance(nw, dict):
        nu = nw.get("unit")
        nu_s = nu.upper() if isinstance(nu, str) else ""
        nv = _pos_finite(nw.get("value"))
        if nv is not None and nu_s in WEIGHT_TO_KG:
            amt = _mul_pos(nv, WEIGHT_TO_KG[nu_s])
            if amt is not None:
                return Qty(amt, "KG")
    return None


def sales_unit_size(raw: dict) -> Optional[float]:
    """How many base units ONE sales unit contains (units[].sizeInBaseUnits)."""
    if not isinstance(raw, dict):
        return None
    su, bu = raw.get("salesUnit"), raw.get("baseUnit")
    units_raw = raw.get("units")
    if isinstance(su, str) and su and isinstance(units_raw, (list, tuple)):
        for u in units_raw:
            if isinstance(u, dict) and u.get("unitId") == su:
                sz = _pos_finite(u.get("sizeInBaseUnits"))
                if sz is not None:
                    return sz
    if isinstance(su, str) and su and su == bu:
        return 1.0
    return None


def english_or_finnish_name(raw: dict) -> str:
    if not isinstance(raw, dict):
        return ""
    sd = raw.get("synkkaData")
    if not isinstance(sd, dict):
        return ""
    raw_names = sd.get("names")
    if not isinstance(raw_names, (list, tuple)):
        return ""
    names = {
        n.get("language"): n.get("value")
        for n in raw_names
        if isinstance(n, dict) and isinstance(n.get("value"), str)
    }
    return names.get("en") or names.get("fi") or names.get("sv") or ""


def compute_content(raw: dict) -> Content:
    """Content of ONE sales unit, from static data only."""
    if not isinstance(raw, dict):
        return Content(1.0, "PCS", "pieces(no size data);size_assumed", 1.0, 1.0)
    size = _pos_finite(sales_unit_size(raw))
    size_known = size is not None
    size_val = size if size is not None else 1.0
    name = english_or_finnish_name(raw)
    sd = raw.get("synkkaData")
    raw_names = sd.get("names") if isinstance(sd, dict) else None
    names_all = [
        n.get("value")
        for n in (raw_names if isinstance(raw_names, (list, tuple)) else [])
        if isinstance(n, dict) and isinstance(n.get("value"), str)
    ]

    base: Optional[Qty] = None
    src = ""
    su_raw = raw.get("salesUnit")
    su_up = su_raw.upper() if isinstance(su_raw, str) else ""
    bu_raw = raw.get("baseUnit")
    bu_up = bu_raw.upper() if isinstance(bu_raw, str) else ""
    if su_up == "KG":
        return Content(1.0, "KG", "salesUnit=KG", 1.0, 1.0)
    q, mult = parse_name_quantity(name)
    if q is None:
        for alt in names_all:
            q, mult = parse_name_quantity(alt)
            if q:
                break
    if q is not None and mult > 1:
        total = _mul_pos(q.amount, mult)
        if total is not None:
            base_div = _div_pos(total, size_val)
            return Content(
                _round_pos(total, 6),
                q.unit,
                f"name(multipack {mult} x {q.amount:g} {q.unit} = sales unit)",
                _round_pos(base_div, 6) if base_div is not None else None,
                size_val,
            )
    conv = _conv_qty(_conv_level(raw, "BASE_UNIT_OR_EACH"))
    drained = drained_quantity(name)
    if drained and (not conv or conv.unit != drained.unit or abs(conv.amount - drained.amount) > 1e-9):
        base, src = drained, "name(drained/net pair)"
    elif conv:
        base, src = conv, "unitConversions.base"
    elif q is not None:
        base, src = q, "name"
    elif bu_up == "KG":
        base, src = Qty(1.0, "KG"), "baseUnit=KG"

    if base is None:
        case = _conv_qty(_conv_level(raw, "CASE"))
        if case and su_raw != bu_raw:
            case_amt = _pos_finite(case.amount)
            if case_amt is not None:
                return Content(_round_pos(case_amt, 6), case.unit, "unitConversions.case", None, size_val)
        return Content(size_val, "PCS", "pieces(no size data)" + ("" if size_known else ";size_assumed"), 1.0, size_val)

    amount = _mul_pos(base.amount, size_val)
    if amount is None:
        return Content(None, None, "invalid_overflow", None, size_val)
    if size_val != 1.0:
        src += f" x {size_val:g} base units per sales unit"
    if not size_known:
        src += ";sales_unit_size_assumed_1"
    base_amt = _pos_finite(base.amount)
    return Content(
        _round_pos(amount, 6),
        base.unit,
        src,
        _round_pos(base_amt, 6) if base_amt is not None else None,
        size_val,
    )


def reconcile_with_price(content_amount: Optional[float], content_unit: Optional[str],
                         unit_price: Optional[float], comparison_price: Optional[float],
                         comparison_unit: Optional[str], tol: float = 0.03) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Cross-check static content with live price. Returns (amount, unit, note).

    price-derived content = unit_price / comparison_price (per L or KG). The comparison price is
    rounded to cents, so allow `tol` relative error; beyond that the price-derived value wins.
    """
    ca = _pos_finite(content_amount)
    cu = content_unit.strip() if isinstance(content_unit, str) and content_unit.strip() else None
    fallback_amt = ca if content_amount is not None else None
    up = _pos_finite(unit_price)
    cp = _pos_finite(comparison_price)
    if up is None or cp is None or comparison_unit not in ("L", "KG"):
        return fallback_amt, cu, None
    derived = _div_pos(up, cp)
    if derived is None:
        return fallback_amt, cu, None
    tol_f = 0.03
    if not isinstance(tol, bool) and isinstance(tol, (int, float)):
        try:
            tf = float(tol)
            if math.isfinite(tf) and tf >= 0.0:
                tol_f = tf
        except OverflowError:
            pass
    if ca is not None and cu == comparison_unit:
        rel_err = abs(derived - ca) / ca
        if math.isfinite(rel_err) and rel_err <= tol_f:
            return ca, cu, None
    snapped = _snap(derived)
    if not math.isfinite(snapped) or snapped <= 0.0:
        return fallback_amt, cu, None
    note = (f"content per sales unit from price ({up:.2f} / {cp:.2f} per {comparison_unit}"
            f" = {snapped:g} {comparison_unit}); static data said "
            f"{ca if ca is not None else 'n/a'} {cu or ''}".strip())
    return snapped, comparison_unit, note


def _snap(x: float) -> float:
    """19.94 -> 20.0, 4.98 -> 5.0, 0.333 -> 0.333 (snap within 1.5 % to a 'nice' value)."""
    xf = _pos_finite(x)
    if xf is None:
        return 0.0
    for step in (1.0, 0.5, 0.25, 0.1, 0.05, 0.01):
        q = _div_pos(xf, step)
        if q is None:
            continue
        try:
            nice = round(q) * step
        except OverflowError:
            continue
        if math.isfinite(nice) and nice > 0.0:
            rel_err = abs(nice - xf) / xf
            if math.isfinite(rel_err) and rel_err <= 0.015:
                snapped = round(nice, 4)
                if math.isfinite(snapped) and snapped > 0.0:
                    return snapped
    res = round(xf, 3)
    return res if (math.isfinite(res) and res > 0.0) else xf


def fat_pct(raw_nutrition: dict[str, float], name: str) -> Optional[float]:
    if isinstance(raw_nutrition, dict) and "FAT" in raw_nutrition:
        fv = _to_float(raw_nutrition["FAT"])
        if fv is not None and 0.0 <= fv <= 100.0:
            return 0.0 if fv == 0.0 else fv
    s_name = name[:_MAX_NAME_LEN] if isinstance(name, str) else ""
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", s_name):
        fv = _to_float(m.group(1))
        if fv is not None and 0.0 <= fv <= 100.0:
            return 0.0 if fv == 0.0 else fv
    n = s_name.lower()
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
    su = sales_unit if isinstance(sales_unit, str) else ""
    amt = _pos_finite(amount)
    u = unit.strip() if isinstance(unit, str) else ""
    if amt is None or not u:
        return su
    def f(x: float) -> str:
        return f"{x:g}"
    if u == "PCS":
        return f"{f(amt)} pcs per {su}"
    sz = _pos_finite(size)
    ba = _pos_finite(base_amount)
    if sz is not None and sz > 1.0 and ba is not None:
        return f"{f(sz)} x {f(ba)} {u} ({f(amt)} {u} per {su})"
    return f"{f(amt)} {u} per {su}"


def as_dict(c: Content) -> dict[str, Any]:
    return {"content_amount": c.amount, "content_unit": c.unit, "content_source": c.source,
            "base_content_amount": c.base_amount, "sales_unit_size": c.sales_unit_size}
