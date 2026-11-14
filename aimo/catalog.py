import csv
import json
import math
import os
import re
import threading
from pathlib import Path

from aimo.db import connect
from aimo.models import Constraint, Product, UnitSize

NAME_LANGS = ("fi", "en", "sv")

ALLERGEN_NAMES = {
    "AC": "crustaceans", "AE": "egg", "AF": "fish", "AM": "milk", "AN": "tree nuts",
    "AP": "peanuts", "AS": "sesame", "AU": "sulphites", "AW": "gluten", "AY": "soy",
    "BC": "celery", "BM": "mustard", "NL": "lupin", "UM": "molluscs", "ML": "lactose",
    "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye",
}
GLUTEN_CODES = {"AW", "UW", "GB", "NR"}
ALLERGEN_ALIASES = {
    "milk": ["AM", "ML"], "maito": ["AM", "ML"], "mjölk": ["AM", "ML"], "dairy": ["AM", "ML"],
    "lactose": ["ML"], "laktoosi": ["ML"], "laktos": ["ML"],
    "gluten": ["AW", "UW", "GB", "NR"], "gluteeni": ["AW", "UW", "GB", "NR"],
    "wheat": ["UW", "AW"], "vehnä": ["UW", "AW"], "egg": ["AE"], "eggs": ["AE"], "muna": ["AE"],
    "kananmuna": ["AE"], "fish": ["AF"], "kala": ["AF"], "soy": ["AY"], "soya": ["AY"], "soija": ["AY"],
    "peanut": ["AP"], "peanuts": ["AP"], "maapähkinä": ["AP"], "nuts": ["AN", "AP"], "nut": ["AN", "AP"],
    "pähkinä": ["AN", "AP"], "tree nuts": ["AN"], "sesame": ["AS"], "seesami": ["AS"],
    "celery": ["BC"], "selleri": ["BC"], "mustard": ["BM"], "sinappi": ["BM"],
    "sulphites": ["AU"], "sulfites": ["AU"], "crustaceans": ["AC"], "molluscs": ["UM"],
    "lupin": ["NL"], "oats": ["GO"], "barley": ["GB"], "rye": ["NR"],
}
SYNONYMS = [
    ["maito", "milk", "mjölk"],
    ["laktoositon", "lactose free", "laktosfri"],
    ["gluteeniton", "gluten free", "glutenfri"],
    ["kerma", "cream", "grädde"],
    ["voi", "butter", "smör"],
    ["juusto", "cheese", "ost"],
    ["jogurtti", "yogurt", "yoghurt", "yoghurt", "jogurt"],
    ["kananmuna", "egg", "ägg", "muna"],
    ["kana", "chicken", "kyckling"],
    ["liha", "meat", "kött"],
    ["nauta", "beef", "nöt"],
    ["possu", "sika", "pork", "fläsk"],
    ["kala", "fish", "fisk"],
    ["lohi", "salmon", "lax"],
    ["peruna", "potato", "potatis"],
    ["tomaatti", "tomato", "tomat"],
    ["sipuli", "onion", "lök"],
    ["porkkana", "carrot", "morot"],
    ["omena", "apple", "äpple"],
    ["jauho", "flour", "mjöl"],
    ["sokeri", "sugar", "socker"],
    ["suola", "salt"],
    ["öljy", "oil", "olja"],
    ["riisi", "rice"],
    ["leipä", "bread", "bröd"],
    ["kahvi", "coffee", "kaffe"],
    ["vesi", "water", "vatten"],
    ["luomu", "organic", "ekologisk"],
    ["vegaani", "vegan"],
    ["jauheliha", "minced meat", "ground meat", "mince"],
    ["pasta", "pasta"],
    ["rahka", "quark"],
]
LABEL_ALIASES = {
    "organic": ["organic", "eco"], "luomu": ["organic", "eco"],
    "finnish": ["finland", "finnish"], "suomalainen": ["finland", "finnish"],
    "vegan": ["vegan"], "vegaani": ["vegan"], "vegetarian": ["vegetarian"],
}
ORIGIN_ALIASES = {
    "finland": "fi", "suomi": "fi", "sweden": "se", "ruotsi": "se", "denmark": "dk", "tanska": "dk",
    "norway": "no", "norja": "no", "germany": "de", "saksa": "de", "estonia": "ee", "viro": "ee",
    "netherlands": "nl", "hollanti": "nl", "france": "fr", "ranska": "fr", "italy": "it", "italia": "it",
    "spain": "es", "espanja": "es",
}
MODIFIER_PREFIXES = ("kahvi", "suklaa", "mansikka", "vanilja", "kaakao", "banaani", "kookos",
                     "peruna", "kana", "liha", "kala", "riisi", "maissi", "soija", "kaura", "korppu")
NONFOOD_PREFIXES = ("20", "30", "32", "63", "64", "67", "77", "90")
HEAD_SUFFIXES = ("juoma",)
WORD_RE = re.compile(r"[^\W\d_]+", re.U)
TEMPERATURE_LABELS = {"warmTemperature", "coolTemperature", "freezeTemperature"}
LITRE_UNITS = {"l": 1.0, "ltr": 1.0, "litre": 1.0, "litres": 1.0, "liter": 1.0, "liters": 1.0, "litra": 1.0,
               "litraa": 1.0, "dl": 0.1, "cl": 0.01, "ml": 0.001, "mlt": 0.001, "clt": 0.01}
KG_UNITS = {"kg": 1.0, "kgm": 1.0, "kilo": 1.0, "kilos": 1.0, "kilogram": 1.0, "kilograms": 1.0,
            "g": 0.001, "grm": 0.001, "gram": 0.001, "grams": 0.001}
PIECE_UNITS = {"pcs", "pc", "piece", "pieces", "kpl", "st", "unit", "units", "h87"}
VOL_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(l|dl|cl|ml)\b", re.I)
WGT_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g)\b", re.I)

_lock = threading.RLock()
_state: dict = {}


def _data_dir() -> Path:
    return Path(os.environ.get("AIMO_DATA_DIR", "data"))


def _json_path() -> Path:
    return _data_dir() / "valio_aimo_junction26_product_data.json"


def _classes(item: dict, name: str) -> list:
    for c in item.get("synkkaData", {}).get("classifications") or []:
        if c.get("name") == name:
            return c.get("values") or []
    return []


def _names(item: dict) -> dict:
    return {n["language"]: n["value"] for n in item.get("synkkaData", {}).get("names") or []
            if n.get("value")}


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _unit_size(item: dict, names: dict) -> UnitSize | None:
    sales, base = item.get("salesUnit"), item.get("baseUnit")
    units = {u["unitId"]: u.get("sizeInBaseUnits") for u in item.get("units") or []}
    mult = units.get(sales)
    if mult is None:
        mult = 1.0 if sales == base else None
    if mult is None:
        return None
    conv = None
    for u in item.get("synkkaData", {}).get("unitConversions") or []:
        if u.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH":
            conv = u
    vol = (conv or {}).get("volume") or {}
    if vol.get("unit") in LITRE_UNITS and vol.get("value"):
        return UnitSize(value=round(vol["value"] * LITRE_UNITS[vol["unit"].lower()] * mult, 6), unit="l")
    text = " ".join(names.get(k, "") for k in ("fi", "en"))
    m = VOL_RE.search(text)
    if m:
        v = _num(m.group(2)) * LITRE_UNITS[m.group(3).lower()] * int(m.group(1) or 1)
        return UnitSize(value=round(v * mult, 6), unit="l")
    wt = (conv or {}).get("netWeight") or {}
    if wt.get("unit") in ("GRM", "KGM") and wt.get("value"):
        return UnitSize(value=round(wt["value"] * KG_UNITS[wt["unit"].lower()] * mult, 6), unit="kg")
    if base == "KG":
        return UnitSize(value=mult, unit="kg")
    m = WGT_RE.search(text)
    if m:
        v = _num(m.group(2)) * KG_UNITS[m.group(3).lower()] * int(m.group(1) or 1)
        return UnitSize(value=round(v * mult, 6), unit="kg")
    return UnitSize(value=mult, unit="pcs")


def _product(item: dict) -> tuple[Product, dict]:
    names = _names(item)
    name = next((names[l] for l in NAME_LANGS if l in names), item["id"])
    allergens = []
    for a in _classes(item, "allergen"):
        n = ALLERGEN_NAMES.get(a["id"], a["id"].lower())
        if a.get("unit") == "CONTAINS":
            allergens.append(n)
        elif a.get("unit") == "MAY_CONTAIN":
            allergens.append("may_contain:" + n)
    labels = [l["id"].lower() for l in _classes(item, "packagingMarkedLabel")
              if l["id"] not in TEMPERATURE_LABELS]
    labels += [c["id"].lower() for c in _classes(item, "nutritionalClaim")]
    origin = item.get("countryOfOrigin")
    p = Product(
        product_id=item["id"],
        name=name,
        brand=item.get("synkkaData", {}).get("brand") or None,
        vendor=item.get("vendorName") or None,
        category=item.get("category") or None,
        sales_unit=item.get("salesUnit"),
        unit_size=_unit_size(item, names),
        allergens=allergens,
        labels=labels,
        origin=origin.upper() if origin else None,
        temperature=item.get("temperatureCondition"),
    )
    return p, names


def _read_csv(name: str, col: str) -> dict:
    out = {}
    path = _data_dir() / name
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                out[str(row["productId"]).strip()] = float(row[col])
            except (KeyError, ValueError):
                pass
    return out


def _fts_query(groups: list[list[tuple]]) -> str | None:
    terms = {v for g in groups for v, _, _ in g if len(v) >= 3}
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in sorted(terms))


def _load() -> dict:
    with _lock:
        path = _json_path()
        if _state and _state.get("path") == path:
            return _state
        if _state:
            reset()
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        products, raws, names_blob, blob, primary = {}, {}, {}, {}, {}
        for item in items:
            if item.get("deleted"):
                continue
            p, names = _product(item)
            pid = p.product_id
            products[pid] = p
            raws[pid] = item
            nb = " ".join(names.values()).lower()
            names_blob[pid] = nb
            primary[pid] = WORD_RE.findall(next((names[l] for l in NAME_LANGS if l in names), "").lower())
            blob[pid] = " ".join([nb, (p.brand or "").lower(), (p.vendor or "").lower(), p.category or ""])
        stat = path.stat()
        sig = f"{stat.st_size}:{stat.st_mtime_ns}:{len(products)}"
        con = connect()
        con.execute("create table if not exists catalog_meta(k text primary key, v text)")
        row = con.execute("select v from catalog_meta where k='sig'").fetchone()
        if not row or row[0] != sig:
            con.execute("drop table if exists catalog_fts")
            con.execute("create virtual table catalog_fts using fts5(pid unindexed, txt, tokenize='trigram')")
            con.executemany("insert into catalog_fts(pid, txt) values (?, ?)", blob.items())
            con.execute("insert or replace into catalog_meta values('sig', ?)", (sig,))
            con.commit()
        _state.update(
            path=path, products=products, raws=raws, names=names_blob, blob=blob, primary=primary, con=con,
            reliability=_read_csv("valio_aimo_junction26_delivery_reliability.csv", "deliveryReliability"),
            co2=_read_csv("valio_aimo_junction26_carbon_footprint.csv", "AVG kg CO2-eq/kg"),
        )
        return _state


def _groups(q: str) -> list[list[tuple]]:
    q = re.sub(r"(lactose|laktos)[\s-]*free|laktoositon|laktosfri", " laktoositon ", q.lower())
    q = re.sub(r"minced meat|ground meat", " jauheliha ", q)
    q = re.sub(r"gluten[\s-]*free|gluteeniton|glutenfri", " gluteeniton ", q)
    out = []
    for tok in re.findall(r"[\wäöåÄÖÅ%]+", q):
        stems = _stems(tok)
        group = [(tok, 1.0, True)] + [(s, 0.95, False) for s in stems]
        for syn in SYNONYMS:
            if tok in syn or any(s in syn for s in stems):
                group = [(v, 1.0 if v == syn[0] else 0.85, v == syn[0]) for v in syn]
                break
        out.append(group)
    return out


def _stems(tok: str) -> list[str]:
    out = []
    if tok.endswith("ies") and len(tok) > 4:
        out.append(tok[:-3] + "y")
    if tok.endswith("es") and len(tok) > 4:
        out.append(tok[:-2])
    if tok.endswith("s") and len(tok) > 3:
        out.append(tok[:-1])
    return out


def _has(raw: dict, section: str, ident: str, unit: str | None = None) -> bool:
    return any(v["id"] == ident and (unit is None or v.get("unit") == unit)
               for v in _classes(raw, section))


def _contains_codes(raw: dict, codes, may: bool = False) -> bool:
    units = ("CONTAINS", "MAY_CONTAIN") if may else ("CONTAINS",)
    return any(a["id"] in codes and a.get("unit") in units for a in _classes(raw, "allergen"))


def _lactose_free(raw: dict, nb: str) -> bool:
    if _contains_codes(raw, {"ML"}):
        return False
    return "laktoositon" in nb or "lactose free" in nb or "laktosfri" in nb or _has(raw, "nutritionalClaim", "FREE_FROM_LACTOSE")


def _gluten_free(raw: dict, nb: str) -> bool:
    if _contains_codes(raw, GLUTEN_CODES):
        return False
    return ("gluteeniton" in nb or "gluten free" in nb or "glutenfri" in nb
            or _has(raw, "nutritionalClaim", "FREE_FROM_GLUTEN"))


def _origin_code(o: str) -> str:
    o = o.strip().lower()
    return ORIGIN_ALIASES.get(o, o[:2])


def _word_score(v: str, words: list[str], nb: str, compound: bool = True) -> float:
    if " " in v:
        return 3.0 if v in " ".join(words) else (0.5 if v in nb else 0.0)
    best = 0.5 if v in nb else 0.0
    for w in words:
        if w == v:
            return 3.0
        if not compound:
            continue
        if w.endswith(v):
            best = max(best, 1.5 if any(m in w[: -len(v)] for m in MODIFIER_PREFIXES) else 3.0)
        elif w.startswith(v):
            best = max(best, 3.0 if w[len(v):] in HEAD_SUFFIXES else 0.5)
        elif v in w:
            best = max(best, 0.5)
    return best


def _passes(pid: str, c: Constraint, s: dict) -> bool:
    raw, p, nb = s["raws"][pid], s["products"][pid], s["names"][pid]
    if c.lactose_free and not _lactose_free(raw, nb):
        return False
    if c.gluten_free and not _gluten_free(raw, nb):
        return False
    for a in c.exclude_allergens:
        codes = ALLERGEN_ALIASES.get(a.strip().lower())
        if codes is None:
            codes = [k for k, v in ALLERGEN_NAMES.items() if v == a.strip().lower()] or [a.strip().upper()]
        if _contains_codes(raw, set(codes), may=True):
            return False
    for lab in c.labels:
        keys = LABEL_ALIASES.get(lab.strip().lower(), [lab.strip().lower()])
        if not any(k in l for k in keys for l in p.labels):
            return False
    if c.origin and (p.origin or "").lower() != _origin_code(c.origin):
        return False
    if c.brands and not any(b.strip().lower() in (p.brand or "").lower() for b in c.brands):
        return False
    return True


def search(q: str, constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
    s = _load()
    c = constraints or Constraint()
    groups = _groups(q or "")
    if groups:
        fq = _fts_query(groups)
        if fq:
            cands = [r[0] for r in s["con"].execute("select pid from catalog_fts where catalog_fts match ?", (fq,))]
        else:
            cands = list(s["products"])
        scored = []
        for pid in cands:
            nb = s["names"][pid]
            words = s["primary"][pid]
            score = 0.0
            hits = 0
            for g in groups:
                hits += any(v in nb for v, _, _ in g)
                score += max(_word_score(v, words, nb, c) * w for v, w, c in g)
            if hits == len(groups):
                score += 3
            if (s["products"][pid].category or "").startswith(NONFOOD_PREFIXES):
                score -= 4
            if hits:
                scored.append((-score, len(nb), pid))
        scored.sort()
        order = [pid for _, _, pid in scored]
    else:
        order = list(s["products"])
    out = []
    for pid in order:
        if _passes(pid, c, s):
            out.append(s["products"][pid])
            if len(out) >= limit:
                break
    return out


def get(product_id: str) -> Product | None:
    return _load()["products"].get(str(product_id))


def raw(product_id: str) -> dict | None:
    return _load()["raws"].get(str(product_id))


def reliability(product_id: str) -> float | None:
    return _load()["reliability"].get(str(product_id))


def co2_per_kg(product_id: str) -> float | None:
    return _load()["co2"].get(str(product_id))


def names(product_id: str) -> dict:
    r = raw(product_id)
    return _names(r) if r else {}


def units_needed(product: Product | str, quantity: float, unit: str) -> float | None:
    p = get(product) if isinstance(product, str) else product
    if p is None:
        return None
    u = unit.strip().lower().rstrip(".")
    if p.sales_unit and u == p.sales_unit.lower():
        return float(math.ceil(quantity))
    if u in LITRE_UNITS:
        dim, amount = "l", quantity * LITRE_UNITS[u]
    elif u in KG_UNITS:
        dim, amount = "kg", quantity * KG_UNITS[u]
    elif u in PIECE_UNITS:
        dim, amount = "pcs", quantity
    else:
        return None
    if not p.unit_size or p.unit_size.unit != dim or p.unit_size.value <= 0:
        return None
    return float(math.ceil(round(amount / p.unit_size.value, 9)))


def reset() -> None:
    with _lock:
        con = _state.get("con")
        if con:
            con.close()
        _state.clear()
