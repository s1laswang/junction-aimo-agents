"""Ask instead of guessing: deterministic ambiguity detection with options from real catalog hits.

The agent asks (AgentStep kind `needs_clarification`) only when a reasonable buyer would ask:
  1. date: "next week" without a day, or no date at all;
  2. unit: a count ("20 packs") of a product sold by volume/weight, so the pack size is unclear;
  3. variant: a dairy product where fat/variant matters, the request names none, and the customer
     has no usual product, while the catalog hits really differ (>= 2 variants);
  4. conflict: milk is an excluded allergen but the request asks for a dairy product.
Never more than MAX_QUESTIONS per run. The hero request asks nothing: the hotel's usual product
(463038) resolves the variant, "by Monday" is a day, and litres are a unit.
Options carry a `patch` applied to the Need when chosen; product options cite real product ids
from search_products, never invented ones.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from aimo.buyer.need import _QTY_RE
from aimo.models import Need

MAX_QUESTIONS = 2
DAIRY_VARIANT = re.compile(
    r"\b(milk|maito|mjölk|cream|kerma|grädde|yogh?urt|jogurtti|rahka|quark|kvarg|piimä|filmjölk|buttermilk)\b",
    re.I,
)
VARIANT_WORDS = re.compile(
    r"\d+(?:[.,]\d+)?\s*%|skim|semi|whole|full[- ]fat|fat[- ]free|low[- ]fat|light|kevyt|rasvaton|täys|"
    r"lätt|fettfri|helmjölk|standardmjölk|whipping|vispi|kuohu|vispgrädd|cooking|ruoka|matlagning|"
    r"sour|hapan|barista|organic|luomu|ekologisk|\b\d+(?:[.,]\d+)?\s*(?:l|dl|ml|kg|g)\b",
    re.I,
)
DAIRY_ANY = re.compile(
    r"\b(milk|maito|mjölk|cream|kerma|grädde|butter|voi|smör|cheese|juusto|ost|yogh?urt|jogurtti|rahka|quark|kvarg)\b",
    re.I,
)
PLANT = re.compile(
    r"\b(oat|kaura|havre|soy|soija|soja|plant|vegan|vegansk|almond|manteli|mandel|pea|herne|ärt|coconut|kookos|kokos|rice|riisi|ris)\b",
    re.I,
)
LIQUID_OR_BULK = re.compile(
    r"\b(milk|maito|mjölk|cream|kerma|grädde|drink|juoma|dryck|juice|mehu|saft|butter|voi|smör|cheese|juusto|ost|"
    r"yogh?urt|jogurtti|flour|jauho|mjöl|sugar|sokeri|socker|oil|öljy|olja)\b",
    re.I,
)
SMALL_LIQUID_PACK = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:dl|ml|cl)\b", re.I)
NEXT_WEEK = re.compile(r"\bnext\s+week\b|\bensi\s+viikolla\b|\bensi\s+viikon\b|\bnästa\s+vecka\b", re.I)
WEEKDAY = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"
    r"|\b(maanantai|tiistai|keskiviikko|keskiviiko|torstai|perjantai|lauantai|sunnuntai)(?:na|nä|hin|on|oon|ksi|lle|n|lta|ltä)?\b"
    r"|\b(måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag)(?:en|ens|s)?\b"
    r"|\d{1,2}\.\d{1,2}\.|\d{4}-\d{2}-\d{2}",
    re.I,
)


def variant_key(name: str) -> str | None:
    n = name.lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", n)
    if m:
        return m.group(1).replace(",", ".") + " % fat"
    for pat, key in ((r"semi[- ]?skim|kevyt|lätt|1[.,]5", "semi-skimmed (1.5 %)"), (r"fat[- ]?free|skim|rasvaton|fettfri|minimjölk", "fat-free / skimmed"),
                     (r"whole|täys|helmjölk|standardmjölk|3[.,]5", "whole (3.5 %)"), (r"whipping|vispi|kuohu|vispgrädd", "whipping cream"),
                     (r"cooking|ruoka|matlagning", "cooking cream"), (r"low[- ]fat|light", "low-fat"), (r"sour|hapan", "sour"),
                     (r"barista", "barista")):
        if re.search(pat, n):
            return key
    return None


def _products(res: Any) -> list[dict]:
    if isinstance(res, dict):
        res = res.get("products", [])
    return [p for p in (res or []) if isinstance(p, dict) and p.get("id")]


def _fmt_pack(p: dict) -> str:
    a, u = p.get("content_amount"), p.get("content_unit")
    return f"{a:g} {u}" if isinstance(a, (int, float)) and u else "pack size unknown"


def find_questions(needs: list[Need], profile: dict, text: str, now: datetime,
                   search: Callable[[str, list[str]], Any]) -> list[dict]:
    qs: list[dict] = []
    t = text or ""
    # 1. date
    if NEXT_WEEK.search(t) and not WEEKDAY.search(t):
        monday = now.date() + timedelta(days=7 - now.weekday())
        days = [monday + timedelta(days=i) for i in (0, 2, 4)]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "Which day next week should it arrive?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    elif all(n.preferences.get("delivery_date_assumed") for n in needs) and not WEEKDAY.search(t):
        d0 = now.date() + timedelta(days=1)
        days = [d for d in (d0 + timedelta(days=i) for i in range(6)) if d.weekday() < 5][:3]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "When do you need the delivery?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    for i, n in enumerate(needs):
        if len(qs) >= MAX_QUESTIONS:
            break
        q = n.product_query
        # 4. conflict: milk excluded but a dairy product requested
        if "milk" in n.excluded_allergens and DAIRY_ANY.search(q) and not PLANT.search(q):
            base = "drink" if re.search(r"milk|maito|mjölk", q, re.I) else DAIRY_ANY.search(q).group(1)
            hits = _products(search(f"oat {base}", []))[:3]
            opts = [{"id": f"p{j}", "label": f"Plant-based instead: {h['name']}", "product_id": h["id"],
                     "patch": {"product_query": f"oat {base}", "preferred_product_ids": [h["id"]]}}
                    for j, h in enumerate(hits)]
            opts.append({"id": "skip", "label": f"Leave out the {q}", "patch": {"cancel": True}})
            qs.append({"question_id": f"q_conflict_{i}", "type": "conflict", "item_index": i,
                       "question": f"Milk is excluded for this customer, but the request asks for {q}. What should I buy?",
                       "options": opts})
            continue
        # 2. unit: a count of a product sold by volume or weight
        if n.unit == "PCS" and LIQUID_OR_BULK.search(q):
            hits = _products(search(q, n.required_claims))
            seen, opts = set(), []
            for h in hits:
                key = (h.get("content_amount"), h.get("content_unit"))
                if key in seen or not h.get("content_unit") or h.get("content_unit") == "PCS":
                    continue
                seen.add(key)
                amt = h["content_amount"] * n.quantity if isinstance(h.get("content_amount"), (int, float)) else None
                opts.append({"id": f"p{len(opts)}", "product_id": h["id"],
                             "label": f"{n.quantity:g} x {_fmt_pack(h)}: {h['name']}" + (f" (= {amt:g} {h['content_unit']})" if amt else ""),
                             "patch": {"unit": h["content_unit"], "quantity": amt or n.quantity,
                                       "preferred_product_ids": [h["id"]]}})
                if len(opts) == 4:
                    break
            if len(opts) >= 2:
                qs.append({"question_id": f"q_unit_{i}", "type": "unit", "item_index": i,
                           "question": f"You asked for {n.quantity:g} {q}: which pack size do you mean?", "options": opts})
            continue
        # 3. variant: dairy where fat/variant matters, none named, no usual product
        if DAIRY_VARIANT.search(q) and not VARIANT_WORDS.search(q) and not VARIANT_WORDS.search(_QTY_RE.sub(" ", t)) \
                and not n.preferred_product_ids:
            hits = _products(search(q, n.required_claims))[:10]
            pref_brand = str((n.preferences or {}).get("brand") or "").strip().lower()
            bulk_litres = n.unit == "L" and n.quantity >= 10
            if pref_brand or bulk_litres:
                hits = sorted(
                    hits,
                    key=lambda h: (
                        0 if (not pref_brand or str(h.get("brand") or "").strip().lower() == pref_brand) else 1,
                        1 if (bulk_litres and SMALL_LIQUID_PACK.search(str(h.get("name") or ""))) else 0,
                    ),
                )
            groups: dict[str, dict] = {}
            for h in hits:
                k = variant_key(h.get("name", ""))
                if k and k not in groups:
                    groups[k] = h
            if len(groups) >= 2:
                opts = [{"id": f"v{j}", "product_id": h["id"], "label": f"{k}: {h['name']}",
                         "patch": {"preferred_product_ids": [h["id"]],
                                   "product_query": f"{q} {k.split(' (')[0]}"}}
                        for j, (k, h) in enumerate(list(groups.items())[:4])]
                qs.append({"question_id": f"q_variant_{i}", "type": "variant", "item_index": i,
                           "question": f"Which {q} do you want? The catalog has several variants.", "options": opts})
    return qs[:MAX_QUESTIONS]


def apply_answer(needs: list[Need], question: dict, answer: dict | None) -> tuple[list[Need], str]:
    """Apply the chosen option's patch. Returns (needs, human-readable resolution)."""
    opt = None
    if answer:
        oid = answer.get("option_id")
        opt = next((o for o in question["options"] if o["id"] == oid), None)
        if opt is None and answer.get("text"):
            low = answer["text"].strip().lower()
            opt = next((o for o in question["options"] if low and (low in o["label"].lower() or o["label"].lower() in low)), None)
    if opt is None:
        return needs, ""
    patch = dict(opt.get("patch") or {})
    idxs = range(len(needs)) if question.get("item_index") is None else [question["item_index"]]
    out = list(needs)
    if patch.pop("cancel", False):
        out = [n for j, n in enumerate(out) if j not in idxs] or out
        return out, opt["label"]
    for j in idxs:
        n = out[j]
        upd: dict[str, Any] = {}
        if "delivery_by" in patch:
            upd["delivery_by"] = date.fromisoformat(patch["delivery_by"])
        for k in ("unit", "quantity", "product_query"):
            if k in patch:
                upd[k] = patch[k]
        if "preferred_product_ids" in patch:
            upd["preferred_product_ids"] = list(dict.fromkeys([*patch["preferred_product_ids"], *n.preferred_product_ids]))
        prefs = dict(n.preferences)
        prefs.pop("delivery_date_assumed", None) if "delivery_by" in patch else None
        prefs.setdefault("clarified", []).append(question["question_id"])
        upd["preferences"] = prefs
        out[j] = n.model_copy(update=upd)
    return out, opt["label"]
