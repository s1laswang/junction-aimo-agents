"""Static catalog -> data/catalog.sqlite (normalized rows + FTS5 trigram index). Owner: catalog.

`load_catalog()` is idempotent: it rebuilds only when the source files or BUILD_VERSION change
(takes ~5-10 s once, ~1 ms after). Everything comes from the files in data/; nothing is invented.
"""
from __future__ import annotations

import csv
import json
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
    return "|".join([BUILD_VERSION] + [_file_sig(p) for p in (PRODUCT_JSON, RELIABILITY_CSV, CO2_CSV) if p.exists()])


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Read connection (builds the DB first if needed)."""
    load_catalog(db_path)
    con = sqlite3.connect(str(db_path or CATALOG_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _text(items: list[dict] | None, lang: str) -> Optional[str]:
    for it in items or []:
        if it.get("language") == lang and it.get("value"):
            return it["value"]
    return None


def _read_csv(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) >= 2 and row[1].strip():
                try:
                    out[row[0].strip()] = float(row[1])
                except ValueError:
                    pass
    return out


def normalize(raw: dict, reliability: dict[str, float] | None = None, co2: dict[str, float] | None = None) -> dict[str, Any]:
    """One raw product JSON -> flat row dict (also used by tests)."""
    s = raw.get("synkkaData") or {}
    names = s.get("names") or []
    name_en, name_fi, name_sv = _text(names, "en"), _text(names, "fi"), _text(names, "sv")
    cls = s.get("classifications") or []
    claims: list[str] = []
    labels: list[str] = []
    temp_label = None
    nutrition: dict[str, float] = {}
    for c in cls:
        nm = c.get("name")
        for v in c.get("values") or []:
            vid = v.get("id")
            if nm == "nutritionalClaim" and vid and vid not in claims:
                claims.append(vid)
            elif nm == "packagingMarkedLabel" and vid:
                if vid in TEMPERATURE_LABELS:
                    temp_label = vid
                elif vid not in labels:
                    labels.append(vid)
            elif nm == "nutritionFact" and vid and v.get("value") is not None:
                nutrition[vid] = float(v["value"])
    for n in s.get("nutritionalContent") or []:
        if n.get("id") and n.get("value") is not None and (n.get("basisQuantity") in (100, 100.0, None)):
            nutrition.setdefault(n["id"], float(n["value"]))
    if any(l in ORGANIC_LABELS for l in labels) and "ORGANIC" not in claims:
        claims.append("ORGANIC")  # derived from packagingMarkedLabel (documented quirk)
    allergens, allergen_source, allergen_raw = A.derive(cls)
    content = U.compute_content(raw)
    ingredients_text = _text(s.get("keyIngredients"), "en") or _text(s.get("keyIngredients"), "fi")
    if not ingredients_text and s.get("ingredients"):
        parts = []
        for ing in sorted(s["ingredients"], key=lambda i: i.get("sequence") or 0):
            nm = _text(ing.get("names"), "en") or _text(ing.get("names"), "fi")
            if nm:
                parts.append(nm)
        ingredients_text = ", ".join(parts) or None
    ingredients_fi = _text(s.get("keyIngredients"), "fi")
    pid = raw["id"]
    name = name_en or name_fi or name_sv or pid
    subs = [x.get("substituteProduct") for x in raw.get("substitutions") or [] if x.get("substituteProduct")]
    return {
        "id": pid,
        "gtin": raw.get("salesUnitGtin") or s.get("gtin"),
        "name": name, "name_en": name_en, "name_fi": name_fi, "name_sv": name_sv,
        "brand": (s.get("brand") or "").strip() or None,
        "vendor": raw.get("vendorName"),
        "category": raw.get("category"),
        "sales_unit": raw.get("salesUnit") or "",
        "base_unit": raw.get("baseUnit"),
        "allowed_lot_size": raw.get("allowedLotSize"),
        "temperature_condition": raw.get("temperatureCondition"),
        "temperature_label": temp_label,
        "country_of_origin": raw.get("countryOfOrigin"),
        **U.as_dict(content),
        "variable_unit": 1 if s.get("variableUnit") else 0,
        "claims": claims, "labels": labels,
        "allergens": allergens, "allergen_source": allergen_source, "allergen_raw": allergen_raw,
        "has_allergen_data": 1 if A.has_structured_data(allergen_source) else 0,
        "ingredients_text": ingredients_text, "ingredients_fi": ingredients_fi,
        "nutrition": nutrition,
        "fat_pct": U.fat_pct(nutrition, name_en or name_fi or ""),
        "marketing_text": _text(s.get("marketingTexts"), "en") or _text(s.get("marketingTexts"), "fi"),
        "delivery_reliability": (reliability or {}).get(pid),
        "co2_kg_per_kg": (co2 or {}).get(pid),
        "substitutes": subs,
    }


JSON_COLS = ("claims", "labels", "allergens", "allergen_source", "allergen_raw", "nutrition", "substitutes")
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
                row = con.execute("select v from meta where k='sig'").fetchone()
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
    rel, co2 = _read_csv(RELIABILITY_CSV), _read_csv(CO2_CSV)
    tmp = db_path.with_suffix(".tmp.sqlite")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    con.execute("create table meta(k text primary key, v text)")
    con.execute(f"create table products({', '.join(c + (' text primary key' if c == 'id' else '') for c in COLS)})")
    con.execute("create table claims(id text, claim text)")
    con.execute("create table subs(product text, substitute text)")
    con.execute("""create virtual table fts using fts5(id unindexed, name_en, name_fi, name_sv, brand,
                   ingredients, claims_text, tokenize='trigram remove_diacritics 1')""")
    n = skipped = 0
    for raw in products:
        if raw.get("deleted"):
            skipped += 1
            continue
        r = normalize(raw, rel, co2)
        r["claims_text"] = " ".join(CLAIM_WORDS.get(c, c.replace("_", " ").lower()) for c in r["claims"])
        vals = [json.dumps(r[c], ensure_ascii=False) if c in JSON_COLS else r.get(c) for c in COLS]
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
    con.close()
    tmp.replace(db_path)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for c in JSON_COLS:
        if c in d and isinstance(d[c], str):
            d[c] = json.loads(d[c])
    return d


if __name__ == "__main__":
    t = time.time()
    load_catalog(force=True)
    con = connect()
    print({r["k"]: r["v"] for r in con.execute("select * from meta")}, f"{time.time() - t:.1f}s")
