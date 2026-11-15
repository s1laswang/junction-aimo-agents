"""Static catalog -> data/catalog.sqlite (normalized rows + FTS5 trigram index). Owner: catalog.

`load_catalog()` is idempotent: it rebuilds only when the source files or BUILD_VERSION change
(takes ~5-10 s once, ~1 ms after). Everything comes from the files in data/; nothing is invented.
"""
from __future__ import annotations

from collections.abc import Mapping
import csv
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from aimo.catalog import allergens as A
from aimo.catalog import units as U
from aimo.config import CATALOG_DB, CO2_CSV, PRODUCT_JSON, RELIABILITY_CSV

BUILD_VERSION = "catalog-v5"

TEMPERATURE_LABELS = {"warmTemperature", "coolTemperature", "freezeTemperature"}
ORGANIC_LABELS = {"EU_ORGANIC_FARMING", "KRAV_MARK", "SOIL_ASSOCIATION_ORGANIC_SYMBOL", "OFFICIAL_ECO_LABEL_SUN"}

# words indexed next to claims so that text search finds claim-only matches in en/fi/sv
CLAIM_WORDS = {
    "FREE_FROM_LACTOSE": "lactose free lactose-free laktoositon laktosfri",
    "LOW_LACTOSE": "low lactose vähälaktoosinen laktosreducerad",
    "FREE_FROM_GLUTEN": "gluten free gluten-free gluteeniton glutenfri",
    "VEGAN": "vegan vegaani",
    "VEGETARIAN": "vegetarian kasvis",
    "FREE_FROM_EGGS": "egg free munaton",
    "FREE_FROM_SOY": "soy free soijaton",
    "ORGANIC": "organic luomu ekologisk",
    "FREE_FROM_FAT": "fat free rasvaton",
    "LOW_FAT": "low fat kevyt",
}

_lock = threading.Lock()


def _file_sig(p: Path) -> str:
    st = p.stat()
    return f"{p.name}:{st.st_size}:{int(st.st_mtime)}"


def _sig() -> str:
    return "|".join(
        [BUILD_VERSION]
        + [_file_sig(Path(p)) for p in (PRODUCT_JSON, RELIABILITY_CSV, CO2_CSV) if isinstance(p, (str, Path)) and Path(p).is_file()]
    )


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Read connection (builds the DB first if needed)."""
    load_catalog(db_path)
    con = sqlite3.connect(str(db_path or CATALOG_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_SQLITE_INT_MIN = -(1 << 63)
_SQLITE_INT_MAX = (1 << 63) - 1
_INT_RE = re.compile(r"[+-]?\d+")


def _opt_str(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    st = val.strip()
    return st or None


def _nonneg_finite(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return None
    if isinstance(val, str):
        val = val.strip()
        if not val or len(val) > 256:
            return None
    try:
        fv = float(val)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(fv) or fv < 0.0:
        return None
    return 0.0 if fv == 0.0 else fv


def _pos_finite(val: Any) -> Optional[float]:
    fv = _nonneg_finite(val)
    if fv is None or fv <= 0.0:
        return None
    return fv


def _opt_int(val: Any) -> Optional[int]:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if _SQLITE_INT_MIN <= val <= _SQLITE_INT_MAX else None
    if isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            return None
        iv = int(val)
        return iv if _SQLITE_INT_MIN <= iv <= _SQLITE_INT_MAX else None
    if isinstance(val, str):
        st = val.strip()
        if not st or len(st) > 32 or not _INT_RE.fullmatch(st):
            return None
        iv = int(st)
        return iv if _SQLITE_INT_MIN <= iv <= _SQLITE_INT_MAX else None
    return None


def _clean_reliability(val: Any) -> Optional[float]:
    fv = _nonneg_finite(val)
    if fv is None or fv > 1.0:
        return None
    return fv


def _seq_num(val: Any) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return 0.0
    if isinstance(val, str):
        val = val.strip()
        if not val or len(val) > 256:
            return 0.0
    try:
        fv = float(val)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return fv if math.isfinite(fv) else 0.0


def _truthy_flag(val: Any) -> int:
    if isinstance(val, str):
        return 1 if val.strip().lower() in ("1", "true", "yes") else 0
    return 1 if bool(val) else 0


def _text(items: Any, lang: str) -> Optional[str]:
    if not isinstance(items, (list, tuple)):
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        val = it.get("value")
        if it.get("language") == lang and isinstance(val, str) and val.strip():
            return val
    return None


def _read_csv(path: Path, *, max_val: float | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(path, (str, Path)):
        return out
    p = Path(path)
    if not p.is_file():
        return out
    try:
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 2:
                    continue
                key = row[0].strip() if isinstance(row[0], str) else ""
                if not key:
                    continue
                fv = _nonneg_finite(row[1])
                if fv is None:
                    continue
                if max_val is not None and fv > max_val:
                    continue
                out[key] = fv
    except (OSError, csv.Error):
        return out
    return out


def normalize(raw: dict, reliability: dict[str, float] | None = None, co2: dict[str, float] | None = None) -> dict[str, Any]:
    """One raw product JSON -> flat row dict (also used by tests)."""
    if not isinstance(raw, dict):
        raise ValueError("product raw payload must be a dict")
    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("product id must be a non-empty string")
    pid = raw_id.strip()
    raw_s = raw.get("synkkaData")
    s = raw_s if isinstance(raw_s, dict) else {}
    names = s.get("names")
    name_en, name_fi, name_sv = _text(names, "en"), _text(names, "fi"), _text(names, "sv")
    cls_raw = s.get("classifications")
    clean_cls: list[dict[str, Any]] = []
    claims: list[str] = []
    labels: list[str] = []
    temp_label = None
    nutrition: dict[str, float] = {}
    for c in cls_raw if isinstance(cls_raw, (list, tuple)) else []:
        if not isinstance(c, dict):
            continue
        nm = c.get("name")
        if not isinstance(nm, str) or not nm:
            continue
        vals_raw = c.get("values")
        clean_vals: list[dict[str, Any]] = []
        for v in vals_raw if isinstance(vals_raw, (list, tuple)) else []:
            if not isinstance(v, dict):
                continue
            vid = _opt_str(v.get("id"))
            if not vid:
                continue
            vunit_raw = v.get("unit")
            vunit = vunit_raw.strip() if isinstance(vunit_raw, str) else ""
            clean_vals.append({"id": vid, "unit": vunit})
            if nm == "nutritionalClaim" and vid not in claims:
                claims.append(vid)
            elif nm == "packagingMarkedLabel":
                if vid in TEMPERATURE_LABELS:
                    temp_label = vid
                elif vid not in labels:
                    labels.append(vid)
            elif nm == "nutritionFact" and v.get("value") is not None:
                nv = _nonneg_finite(v.get("value"))
                if nv is not None:
                    nutrition[vid] = nv
        clean_cls.append({"name": nm, "values": clean_vals})
    nc_raw = s.get("nutritionalContent")
    for n in nc_raw if isinstance(nc_raw, (list, tuple)) else []:
        if not isinstance(n, dict):
            continue
        nid = _opt_str(n.get("id"))
        bq = n.get("basisQuantity")
        bq_ok = bq is None or (not isinstance(bq, bool) and isinstance(bq, (int, float)) and bq == 100)
        if nid and bq_ok and n.get("value") is not None:
            nv = _nonneg_finite(n.get("value"))
            if nv is not None:
                nutrition.setdefault(nid, nv)
    if any(l in ORGANIC_LABELS for l in labels) and "ORGANIC" not in claims:
        claims.append("ORGANIC")
    allergens, allergen_source, allergen_raw = A.derive(clean_cls)
    content = U.compute_content(raw)
    key_ing = s.get("keyIngredients")
    ingredients_text = _text(key_ing, "en") or _text(key_ing, "fi")
    ings_raw = s.get("ingredients")
    if not ingredients_text and isinstance(ings_raw, (list, tuple)) and ings_raw:
        valid_ings = [ing for ing in ings_raw if isinstance(ing, dict)]
        parts = []
        for _idx, ing in sorted(enumerate(valid_ings), key=lambda pair: (_seq_num(pair[1].get("sequence")), pair[0])):
            nm = _text(ing.get("names"), "en") or _text(ing.get("names"), "fi")
            if nm:
                parts.append(nm)
        ingredients_text = ", ".join(parts) or None
    ingredients_fi = _text(key_ing, "fi")
    name = name_en or name_fi or name_sv or pid
    subs_raw = raw.get("substitutions")
    subs: list[str] = []
    for x in subs_raw if isinstance(subs_raw, (list, tuple)) else []:
        if isinstance(x, dict):
            sp = _opt_str(x.get("substituteProduct"))
            if sp and sp not in subs:
                subs.append(sp)
    rel_val = _clean_reliability(reliability.get(pid)) if isinstance(reliability, dict) else None
    co2_val = _nonneg_finite(co2.get(pid)) if isinstance(co2, dict) else None
    return {
        "id": pid,
        "gtin": _opt_str(raw.get("salesUnitGtin")) or _opt_str(s.get("gtin")),
        "name": name, "name_en": name_en, "name_fi": name_fi, "name_sv": name_sv,
        "brand": _opt_str(s.get("brand")),
        "vendor": _opt_str(raw.get("vendorName")),
        "category": _opt_str(raw.get("category")),
        "sales_unit": _opt_str(raw.get("salesUnit")) or "",
        "base_unit": _opt_str(raw.get("baseUnit")),
        "allowed_lot_size": _pos_finite(raw.get("allowedLotSize")),
        "temperature_condition": _opt_int(raw.get("temperatureCondition")),
        "temperature_label": temp_label,
        "country_of_origin": _opt_str(raw.get("countryOfOrigin")),
        **U.as_dict(content),
        "variable_unit": _truthy_flag(s.get("variableUnit")),
        "claims": claims, "labels": labels,
        "allergens": allergens, "allergen_source": allergen_source, "allergen_raw": allergen_raw,
        "has_allergen_data": 1 if A.has_structured_data(allergen_source) else 0,
        "ingredients_text": ingredients_text, "ingredients_fi": ingredients_fi,
        "nutrition": nutrition,
        "fat_pct": U.fat_pct(nutrition, name_en or name_fi or ""),
        "marketing_text": _text(s.get("marketingTexts"), "en") or _text(s.get("marketingTexts"), "fi"),
        "delivery_reliability": rel_val,
        "co2_kg_per_kg": co2_val,
        "substitutes": subs,
    }


JSON_COLS = ("claims", "labels", "allergens", "allergen_source", "allergen_raw", "nutrition", "substitutes")
_LIST_JSON_COLS = frozenset({"claims", "labels", "substitutes"})
COLS = ["id", "gtin", "name", "name_en", "name_fi", "name_sv", "brand", "vendor", "category", "sales_unit",
        "base_unit", "allowed_lot_size", "temperature_condition", "temperature_label", "country_of_origin",
        "content_amount", "content_unit", "content_source", "base_content_amount", "sales_unit_size",
        "variable_unit", "claims", "labels", "allergens", "allergen_source", "allergen_raw", "has_allergen_data",
        "ingredients_text", "ingredients_fi", "nutrition", "fat_pct", "marketing_text", "delivery_reliability",
        "co2_kg_per_kg", "substitutes", "claims_text"]


def load_catalog(db_path: Path | None = None, force: bool = False) -> None:
    """Build data/catalog.sqlite from the product JSON + CSVs if missing or stale."""
    db_path = Path(db_path or CATALOG_DB)
    sig = _sig()
    with _lock:
        if not force and db_path.exists():
            try:
                con = sqlite3.connect(str(db_path))
                try:
                    row = con.execute("select v from meta where k='sig'").fetchone()
                finally:
                    con.close()
                if row and row[0] == sig:
                    return
            except sqlite3.Error:
                pass
        _build(db_path, sig)


def _build(db_path: Path, sig: str) -> None:
    t0 = time.time()
    with open(PRODUCT_JSON, encoding="utf-8") as f:
        products = json.load(f)
    if not isinstance(products, list):
        raise ValueError("product catalog JSON root must be a list")
    rel, co2 = _read_csv(RELIABILITY_CSV, max_val=1.0), _read_csv(CO2_CSV)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp.sqlite")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    try:
        con.execute("create table meta(k text primary key, v text)")
        con.execute(f"create table products({', '.join(c + (' text primary key' if c == 'id' else '') for c in COLS)})")
        con.execute("create table claims(id text, claim text)")
        con.execute("create table subs(product text, substitute text)")
        con.execute("""create virtual table fts using fts5(id unindexed, name_en, name_fi, name_sv, brand,
                       ingredients, claims_text, tokenize='trigram remove_diacritics 1')""")
        n = skipped = 0
        seen_ids: set[str] = set()
        for raw in products:
            if not isinstance(raw, dict) or raw.get("deleted"):
                skipped += 1
                continue
            try:
                r = normalize(raw, rel, co2)
            except ValueError:
                skipped += 1
                continue
            if r["id"] in seen_ids:
                skipped += 1
                continue
            seen_ids.add(r["id"])
            r["claims_text"] = " ".join(CLAIM_WORDS.get(c, c.replace("_", " ").lower()) for c in r["claims"])
            vals = [json.dumps(r[c], ensure_ascii=False, allow_nan=False) if c in JSON_COLS else r.get(c) for c in COLS]
            con.execute(f"insert into products values ({','.join('?' * len(COLS))})", vals)
            con.executemany("insert into claims values (?,?)", [(r["id"], c) for c in r["claims"]])
            con.executemany("insert into subs values (?,?)", [(r["id"], s) for s in r["substitutes"]])
            con.execute("insert into fts values (?,?,?,?,?,?,?)",
                        (r["id"], r["name_en"] or "", r["name_fi"] or "", r["name_sv"] or "", r["brand"] or "",
                         " ".join(x for x in (r["ingredients_text"], r["ingredients_fi"]) if x), r["claims_text"]))
            n += 1
        con.execute("create index claims_claim on claims(claim)")
        con.execute("create index claims_id on claims(id)")
        con.execute("create index products_cat on products(category)")
        con.executemany("insert into meta values (?,?)", [("sig", sig), ("n", str(n)), ("skipped_deleted", str(skipped)),
                                                        ("built_at", str(time.time())), ("build_s", f"{time.time() - t0:.1f}")])
        con.commit()
    except Exception:
        con.close()
        if tmp.exists():
            tmp.unlink()
        raise
    con.close()
    tmp.replace(db_path)


def row_to_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row) or isinstance(row, Mapping):
        d = dict(row)
    else:
        return {}
    for c in JSON_COLS:
        if c not in d:
            continue
        val = d[c]
        want_list = c in _LIST_JSON_COLS
        fallback: Any = [] if want_list else {}
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
            except (ValueError, TypeError):
                parsed = fallback
        else:
            parsed = val
        if want_list:
            d[c] = [x for x in parsed if isinstance(x, str) and x.strip()] if isinstance(parsed, list) else []
        elif not isinstance(parsed, dict):
            d[c] = {}
        elif c == "nutrition":
            d[c] = {
                k: fv
                for k, v in parsed.items()
                if isinstance(k, str) and k and (fv := _nonneg_finite(v)) is not None
            }
        elif c == "allergen_raw":
            d[c] = {
                k: [u for u in v if isinstance(u, str)]
                for k, v in parsed.items()
                if isinstance(k, str) and isinstance(v, list)
            }
        else:
            d[c] = {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}
    return d


if __name__ == "__main__":
    t = time.time()
    load_catalog(force=True)
    con = connect()
    print({r["k"]: r["v"] for r in con.execute("select * from meta")}, f"{time.time() - t:.1f}s")
