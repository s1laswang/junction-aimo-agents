"""Allergen contradiction sweep: structured FREE_FROM vs the product's own ingredient text.

If the structured data says an EU14 group is FREE (nonAllergen FREE_FROM or a FREE_FROM_* claim) but
the ingredient text names it, the status becomes `unknown` with allergen_source `contradiction`, and
the product gets the flag `allergen_data_conflict`. Unknown is never safe, so a conflicting product can
never pass an allergen exclusion.

Evidence sources:
  1. deterministic multilingual lexicon over ingredients (en/fi/sv), with negation and false-friend
     handling (coconut milk, cocoa butter, peanut butter, buckwheat, "free from milk", "lactose-free" ...)
  2. Gemini taxonomy labels (`allergens_in_ingredients`, data/cache/taxonomy.json), when present.
Gluten conflicts are not raised when the product carries the FREE_FROM_GLUTEN claim (legally
gluten-free products may list gluten-free oats or wheat starch).

    uv run python -m aimo.catalog.data_quality report
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from functools import lru_cache
from typing import Any, Optional

W = r"(?<![\wåäö])"
E = r"(?![\wåäö])"
LEXICON: dict[str, list[str]] = {
    "milk": [r"milk", r"milks", r"whole ?milk powder", r"cream", r"butter", r"buttermilk", r"whey", r"lactose",
             r"casein\w*", r"caseinate", r"cheese", r"yoghurt", r"yogurt", r"maito\w*", r"\w*maito", r"kerma\w*",
             r"\w*kerma", r"voi(?:ta|rasva\w*|öljy\w*|sula\w*)", r"heraj\w*", r"hera", r"laktoosi", r"juusto\w*", r"\w*juusto", r"jogurtti",
             r"mjölk\w*", r"\w*mjölk", r"grädde", r"smör", r"vassle\w*", r"ost"],
    "gluten": [r"wheat\w*", r"rye", r"barley", r"spelt", r"kamut", r"oats?", r"vehnä\w*", r"\w*vehnä\w*", r"ruis\w*",
               r"\w*ruis", r"ohra\w*", r"\w*ohra", r"speltti\w*", r"kaura\w*", r"vete\w*", r"råg\w*", r"korn",
               r"havre\w*", r"malt", r"malts", r"malt extract", r"barley malt", r"mallas\w*", r"seitan"],
    "eggs": [r"eggs?", r"egg ?(?:white|yolk|powder)", r"\w*kananmuna\w*", r"muna\w*", r"ägg\w*", r"albumen"],
    "nuts": [r"almonds?", r"hazelnuts?", r"walnuts?", r"cashews?", r"pecans?", r"pistachios?", r"macadamias?",
             r"brazil nuts?", r"manteli\w*", r"\w*manteli", r"hasselpähkinä\w*", r"saksanpähkinä\w*",
             r"cashewpähkinä\w*", r"pekaanipähkinä\w*", r"pistaasi\w*", r"mandel\w*", r"hasselnöt\w*",
             r"valnöt\w*", r"tree nuts?"],
    "peanuts": [r"peanuts?", r"groundnuts?", r"maapähkinä\w*", r"jordnöt\w*"],
    "soy": [r"soy", r"soya", r"soybeans?", r"soja\w*", r"soija\w*", r"\w*soija\w*", r"edamame", r"tofu"],
    "fish": [r"fish", r"salmon", r"tuna", r"cod", r"herring", r"anchov\w*", r"kala\w*", r"lohi\w*", r"silakka\w*",
             r"tonnikala\w*", r"fisk\w*", r"lax"],
    "crustaceans": [r"shrimps?", r"prawns?", r"crabs?", r"lobsters?", r"crayfish", r"katkarap\w*", r"rapu\w*",
                    r"hummeri\w*", r"räk\w*"],
    "molluscs": [r"mussels?", r"squid", r"octopus", r"oysters?", r"clams?", r"scallops?", r"simpuk\w*",
                 r"mustekala\w*", r"osteri\w*"],
    "sesame": [r"sesame", r"seesami\w*", r"sesam\w*", r"tahini"],
    "celery": [r"celery", r"celeriac", r"selleri\w*", r"\w*selleri"],
    "mustard": [r"mustard", r"sinappi\w*", r"\w*sinappi", r"senap\w*"],
    "lupin": [r"lupin\w*", r"lupiini\w*"],
    "sulphites": [r"sulphites?", r"sulfites?", r"sulphur dioxide", r"sulfur dioxide", r"e ?22[0-8]",
                  r"sulfiit\w*", r"rikkidioksidi", r"disulfi\w*", r"metabisulfi\w*", r"metabisulphi\w*"],
}
FALSE_FRIENDS: dict[str, list[str]] = {
    "milk": [r"coconut ?milk", r"kookosmaito", r"kokosmjölk", r"cocoa ?butter", r"kaakaovoi", r"kakaosmör",
             r"peanut ?butter", r"maapähkinävoi", r"shea ?butter", r"oat ?milk", r"almond ?milk", r"soy ?milk",
             r"rice ?milk", r"kauramaito", r"milk thistle", r"maitohap\w*", r"lactic", r"maitohappobakteer\w*",
             r"juustokumina\w*", r"milk[- ]?caps?", r"coconut ?butter", r"yogh?urt cultures?", r"jogurttibakteer\w*",
             r"(?:soy|soya|oat|rice|coconut|plant) ?cream", r"(?:soija|kaura|kasvi|kookos)kerma\w*", r"rousku\w*", r"lactate", r"laktaatti", r"butter ?beans?", r"butternut", r"cream of tartar",
             r"ice ?cream ?cone", r"lactose[- ]?free", r"laktoositon\w*", r"laktosfri", r"ostronsås",
             r"buttermilk ?substitute", r"vegan ?butter", r"kasvirasva"],
    "gluten": [r"buckwheat", r"tattari\w*", r"bovete", r"gluten[- ]?free \w+", r"gluteeniton \w+",
               r"wheat ?starch", r"vehnätärkkelys", r"ruiskauno\w*"],
    "eggs": [r"eggplant", r"munakoiso", r"äggplanta", r"egg[- ]?free", r"munaton"],
    "nuts": [r"coconut", r"kookos\w*", r"nutmeg", r"muskot\w*", r"butternut", r"pine ?nuts?"],
    "molluscs": [r"oyster ?mushrooms?", r"osterivinok\w*", r"ostronskivling\w*"],
    "fish": [r"fish sauce substitute", r"kalaton", r"fiskfri"],
    "soy": [r"soy[- ]?free", r"soijaton", r"soy(?:a|bean)? ?oil", r"soijaöljy\w*", r"sojaolja"],
}
NEG_SHORT = re.compile(r"(?<![\wåäö])(?:no|not|non|ei|without|ilman|utan|inga|ingen)\s+(?:[\wåäö-]+\s+){0,2}$", re.I)
NEGATION = re.compile(r"(?:free from|free of|does not contain|without|no added|ei sisällä|ilman|fri från|utan)\s+"
                      r"(?:[\wåäö-]+[\s,]+){0,4}$", re.I)
MAX_INGREDIENT_SCAN_CHARS = 16_384


@lru_cache(maxsize=None)
def _rx(group: str) -> re.Pattern:
    return re.compile(W + "(?:" + "|".join(LEXICON[group]) + ")" + E, re.I)


@lru_cache(maxsize=None)
def _ff(group: str) -> Optional[re.Pattern]:
    ffs = FALSE_FRIENDS.get(group)
    return re.compile("|".join(ffs), re.I) if ffs else None


def _str_set(val: Any) -> set[str]:
    if val is None or isinstance(val, (str, bytes, bytearray, dict)):
        return set()
    try:
        it = iter(val)
    except TypeError:
        return set()
    return {x for x in it if isinstance(x, str) and x}


def keyword_evidence(group: str, text: str) -> Optional[str]:
    """The ingredient words naming `group`, or None. Negated mentions and false friends are ignored."""
    if isinstance(group, bool) or not isinstance(group, str) or group not in LEXICON:
        return None
    if isinstance(text, bool) or not isinstance(text, str) or not text:
        return None
    t = text[:MAX_INGREDIENT_SCAN_CHARS]
    if "\x00" in t:
        t = t.replace("\x00", " ")
    if not t.strip():
        return None
    ff = _ff(group)
    if ff:
        t = ff.sub(" ", t)
    for m in _rx(group).finditer(t):
        before = t[max(0, m.start() - 60):m.start()]
        if NEGATION.search(before) or NEG_SHORT.search(before[-30:]):
            continue
        return t[max(0, m.start() - 25):m.end() + 25].strip()
    return None


def _ingredients(d: dict[str, Any]) -> str:
    if not isinstance(d, dict):
        return ""
    parts: list[str] = []
    for key in ("ingredients_text", "ingredients_fi"):
        val = d.get(key)
        if isinstance(val, str):
            cleaned = val[:MAX_INGREDIENT_SCAN_CHARS].replace("\x00", " ").strip()
            if cleaned:
                parts.append(cleaned)
    return " | ".join(parts)


def conflicts(d: dict[str, Any], gemini_label: Optional[dict] = None) -> dict[str, dict[str, str]]:
    """{group: {"evidence": ..., "by": "lexicon"|"gemini"|"lexicon+gemini"}} for groups marked free."""
    if not isinstance(d, dict):
        return {}
    raw_allergens = d.get("allergens")
    if not isinstance(raw_allergens, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    claims = _str_set(d.get("claims"))
    text = _ingredients(d)
    gem = _str_set(gemini_label.get("allergens_in_ingredients")) if isinstance(gemini_label, dict) else set()
    for g, st in raw_allergens.items():
        if not isinstance(g, str) or not g:
            continue
        st_val = getattr(st, "value", st)
        if st_val != "free":
            continue
        if g == "gluten" and "FREE_FROM_GLUTEN" in claims:
            continue
        ev = keyword_evidence(g, text)
        by: list[str] = []
        if ev:
            by.append("lexicon")
        if g in gem and text:
            by.append("gemini")
            ev = ev or "Gemini reads the ingredients as containing " + g
        if by:
            out[g] = {"evidence": ev or "", "by": "+".join(by)}
    return out


def apply(d: dict[str, Any]) -> dict[str, Any]:
    """Mutate a product row in place: conflicting groups -> unknown / source 'contradiction'."""
    if not isinstance(d, dict):
        raise ValueError("product row must be a dict")
    lab = None
    raw_id = d.get("id")
    if not isinstance(raw_id, bool) and isinstance(raw_id, str) and raw_id.strip():
        try:
            from aimo.catalog.taxonomy import label
            cand = label(raw_id.strip())
            lab = cand if isinstance(cand, dict) else None
        except Exception:
            lab = None
    c = conflicts(d, lab)
    if c:
        raw_all = d.get("allergens")
        allergens = dict(raw_all) if isinstance(raw_all, dict) else {}
        raw_src = d.get("allergen_source")
        source = dict(raw_src) if isinstance(raw_src, dict) else {}
        for g in c:
            allergens[g] = "unknown"
            source[g] = "contradiction"
        d["allergens"], d["allergen_source"] = allergens, source
    d["allergen_conflicts"] = c
    return d


def sweep() -> list[dict[str, Any]]:
    from aimo.backend.catalog_store import connect, row_to_dict
    con = connect()
    try:
        from aimo.catalog.taxonomy import labels_for
        raw_labs = labels_for()
        labs = raw_labs if isinstance(raw_labs, dict) else {}
    except Exception:
        labs = {}
    out = []
    for r in con.execute("select * from products where has_allergen_data = 1"):
        d = row_to_dict(r)
        pid = str(d.get("id") or "")
        c = conflicts(d, labs.get(pid))
        if c:
            raw_src = d.get("allergen_source")
            src_map = raw_src if isinstance(raw_src, dict) else {}
            name = d.get("name") if isinstance(d.get("name"), str) else pid
            out.append({"id": pid, "name": name, "brand": d.get("brand"), "conflicts": c,
                        "source": {g: src_map.get(g) for g in c}})
    return out


def write_report(path=None) -> str:
    if path is not None and (
        isinstance(path, bool)
        or not isinstance(path, (str, os.PathLike))
        or not str(path).strip()
    ):
        raise ValueError("path must be a non-empty path or None")
    from collections import Counter

    from aimo.config import ROOT
    rows = sweep()
    by_group = Counter(g for r in rows for g in r["conflicts"])
    by_method = Counter(v["by"] for r in rows for v in r["conflicts"].values())
    n_pairs = sum(len(r["conflicts"]) for r in rows)
    lines = [
        "# Data quality: allergen contradictions in the product data",
        "",
        "For Valio Aimo. Generated by `uv run python -m aimo.catalog.data_quality report` from",
        "`valio_aimo_junction26_product_data.json` (17,546 products).",
        "",
        "**What we checked.** Structured allergen data says an EU14 allergen group is *free* (a nonAllergen",
        "FREE_FROM code, or a FREE_FROM_GLUTEN/EGGS/SOY claim), but the product's own ingredient text names that",
        "allergen. We test this with a multilingual keyword lexicon (en/fi/sv, with negations such as \"free from\"",
        "and false friends such as coconut milk, cocoa butter, buckwheat handled) and with Gemini's reading of",
        "the ingredients (product taxonomy labels). Gluten is not flagged when the product carries the",
        "FREE_FROM_GLUTEN claim, because gluten-free products may legally contain gluten-free oats or wheat starch.",
        "",
        "**What the system does.** Each conflicting group becomes `unknown` with allergen_source `contradiction`",
        "and the product gets the flag `allergen_data_conflict`. Unknown is never treated as safe, so these",
        "products can never be ordered for a customer who excludes that allergen without a human check.",
        "",
        f"## Result: {len(rows)} products, {n_pairs} product-allergen conflicts",
        "",
        "| Allergen group | Conflicts |",
        "|---|---|",
        *[f"| {g} | {n} |" for g, n in by_group.most_common()],
        "",
        "Evidence source: " + ", ".join(f"{k}: {v}" for k, v in by_method.most_common()) + ".",
        "",
        "## Examples (first 40)",
        "",
        "| Product | Name | Marked free from | Evidence in ingredients | Found by |",
        "|---|---|---|---|---|",
    ]
    for r in rows[:40]:
        for g, v in r["conflicts"].items():
            ev = v["evidence"].replace("|", "/").replace("\n", " ")[:90]
            lines.append(f"| {r['id']} | {r['name'][:50]} | {g} | {ev} | {v['by']} |")
    lines += ["", "## All affected product ids", "", ", ".join(r["id"] for r in rows), ""]
    text = "\n".join(lines)
    p = Path(path) if path is not None else (ROOT / "docs" / "data-quality.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        print(write_report())
    else:
        rows = sweep()
        print(len(rows), "products with conflicts")
        for r in rows[:30]:
            print(r["id"], r["name"][:50], r["conflicts"])
