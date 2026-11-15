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

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from aimo.buyer.need import HELSINKI, _QTY_RE, _to_number, now_helsinki, parse_quantity, resolve_date
from aimo.models import Need

MAX_QUESTIONS = 2
DAIRY_VARIANT = re.compile(
    r"\b(milk|\w*maito\w*|maidon|\w*mjölk\w*|cream|kerm\w*|\w+kerma\w*|\w*grädd\w*|yogh?urt\w*|\w*jogurt\w*|\w*rahk\w*|quark|\w*kvarg\w*|\w*piim\w*|buttermilk)\b",
    re.I,
)
VARIANT_WORDS = re.compile(
    r"\d+(?:[.,]\d+)?\s*%|skim|semi|whole|full[- ]fat|fat[- ]free|low[- ]fat|light|kevyt|rasvato\w*|rasvattom\w*|täys|"
    r"lätt|mellanmjölk|minimjölk|fettfri|helmjölk|standardmjölk|whipping|vispi|kuohu|vispgrädd|cooking|ruoka|matlagning|"
    r"sour|hapan|barista|organic|luomu|ekologisk|\b\d+(?:[.,]\d+)?\s*(?:l|dl|ml|kg|g)\b",
    re.I,
)
DAIRY_ANY = re.compile(
    r"\b(milk|\w*maito\w*|maidon|\w*mjölk\w*|cream|kerm\w*|\w+kerma\w*|\w*grädd\w*|butter|voi(?:ta|n)?|smör\w*|cheese|\w*juust\w*|ost(?:en|ar)?|"
    r"yogh?urt\w*|\w*jogurt\w*|\w*rahk\w*|quark|\w*kvarg\w*|\w*piim\w*|buttermilk)\b",
    re.I,
)
DAIRY_FREE = re.compile(r"\b(dairy[- ]free|milk[- ]free|maidoto\w*|maidottom\w*|mjölkfri\w*)\b", re.I)
PLANT = re.compile(
    r"\b(oat|kaura\w*|havre\w*|soy|soija\w*|soja\w*|plant|vegan\w*|vegansk\w*|almond|manteli\w*|mandel\w*|"
    r"pea|herne\w*|ärt\w*|coconut|kookos\w*|kokos\w*|rice|riisi\w*|ris\w*|peanut\w*|maapähkin\w*|jordnöt\w*)\b",
    re.I,
)
LIQUID_OR_BULK = re.compile(
    r"\b(milk|\w*maito\w*|maidon|\w*mjölk\w*|cream|kerm\w*|\w+kerma\w*|\w*grädd\w*|drink|\w*juom\w*|\w*dryck\w*|juice|\w*mehu\w*|saft\w*|"
    r"butter|voi(?:ta|n)?|smör\w*|cheese|\w*juust\w*|ost(?:en|ar)?|yogh?urt\w*|\w*jogurt\w*|\w*rahk\w*|quark|\w*kvarg\w*|"
    r"\w*piim\w*|buttermilk|flour|\w*jauho\w*|mjöl|sugar|\w*sokeri\w*|socker|oil|\w*öljy\w*|olja)\b",
    re.I,
)
LIQUID_ONLY = re.compile(
    r"\b(milk|\w*maito\w*|maidon|\w*mjölk\w*|cream|kerm\w*|\w+kerma\w*|\w*grädd\w*|drink|\w*juom\w*|\w*dryck\w*|juice|\w*mehu\w*|saft\w*|"
    r"\w*piim\w*|buttermilk|oil|\w*öljy\w*|olja)\b",
    re.I,
)
SMALL_LIQUID_PACK = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:dl|ml|cl)\b", re.I)
NEXT_WEEK = re.compile(r"\bnext\s+week\b|\bensi\s+viikolla\b|\bensi\s+viikon\b|\bnästa\s+veckas?\b", re.I)
WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)(?:'s)?\b"
    r"|\b(maanantai|tiistai|keskiviikko|keskiviiko|torstai|perjantai|lauantai|sunnuntai)(?:na|nä|hin|on|oon|ksi|lle|n|lta|ltä)?\b"
    r"|\b(måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag)(?:en|ens|s)?\b"
    r"|\d{1,2}\.\d{1,2}\.|\d{4}-\d{2}-\d{2}",
    re.I,
)


def _pos_finite(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f > 0.0 else None


def _parse_iso_date(val: Any) -> date | None:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if not isinstance(val, str):
        return None
    try:
        return date.fromisoformat(val.strip())
    except ValueError:
        return None


def _safe_options(question: Any) -> list[dict]:
    if not isinstance(question, dict):
        return []
    raw_opts = question.get("options")
    if not isinstance(raw_opts, (list, tuple)):
        return []
    return [o for o in raw_opts if isinstance(o, dict)]


def variant_key(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    n = name.lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", n)
    if m:
        return m.group(1).replace(",", ".") + " % fat"
    for pat, key in ((r"semi[- ]?skim|kevyt|lätt|mellanmjölk|1[.,]5", "semi-skimmed (1.5 %)"),
                     (r"fat[- ]?free|skim|rasvato|rasvattom|fettfri|minimjölk", "fat-free / skimmed"),
                     (r"whole|täys|helmjölk|standardmjölk|3[.,]5", "whole (3.5 %)"), (r"whipping|vispi|kuohu|vispgrädd", "whipping cream"),
                     (r"cooking|ruoka|matlagning", "cooking cream"), (r"low[- ]fat|light", "low-fat"), (r"sour|hapan", "sour"),
                     (r"barista", "barista")):
        if re.search(pat, n):
            return key
    return None


def _products(res: Any) -> list[dict]:
    if isinstance(res, dict):
        res = res.get("products", [])
    if not isinstance(res, (list, tuple)):
        return []
    out: list[dict] = []
    for p in res:
        if hasattr(p, "model_dump") and callable(p.model_dump):
            try:
                p = p.model_dump()
            except Exception:
                continue
        if not isinstance(p, dict):
            continue
        raw_id = p.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        raw_name = p.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
        raw_claims = p.get("claims")
        claims = [c for c in raw_claims if isinstance(c, str)] if isinstance(raw_claims, (list, tuple, set)) else []
        cu = p.get("content_unit")
        content_unit = cu if cu in ("L", "KG", "PCS") else None
        content_amount = _pos_finite(p.get("content_amount"))
        raw_brand = p.get("brand")
        brand = raw_brand if isinstance(raw_brand, str) else ""
        out.append({
            **p,
            "id": raw_id.strip(),
            "name": name,
            "claims": claims,
            "content_unit": content_unit,
            "content_amount": content_amount,
            "brand": brand,
        })
    return out


def _call_search(search: Any, query: str, claims: list[str]) -> list[dict]:
    if not callable(search):
        return []
    try:
        res = search(query, list(claims))
    except Exception:
        return []
    return _products(res)


def _fmt_pack(p: Any) -> str:
    if not isinstance(p, dict):
        return "pack size unknown"
    a = _pos_finite(p.get("content_amount"))
    u = p.get("content_unit")
    return f"{a:g} {u}" if a is not None and u in ("L", "KG", "PCS") else "pack size unknown"


def _conflict_base(q: Any) -> str:
    if not isinstance(q, str):
        return "drink"
    if re.search(r"\bbuttermilk\b", q, re.I):
        return "drink"
    if re.search(r"\b(cheese|\w*juust\w*|ost(?:en|ar)?)\b", q, re.I):
        return "cheese"
    if re.search(r"\b(yogh?urt\w*|\w*jogurt\w*|\w*rahk\w*|quark|\w*kvarg\w*)\b", q, re.I):
        return "yogurt"
    if re.search(r"\b(butter|voi(?:ta|n)?|smör\w*)\b", q, re.I):
        return "butter"
    if re.search(r"\b(cream|kerm\w*|\w+kerma\w*|\w*grädd\w*)\b", q, re.I):
        return "cream"
    if re.search(r"\b(milk|\w*maito\w*|maidon|\w*mjölk\w*|\w*piim\w*)\b", q, re.I):
        return "drink"
    m = DAIRY_ANY.search(q)
    return m.group(1).lower() if m else "drink"


def find_questions(needs: list[Need], profile: dict, text: str, now: datetime,
                   search: Callable[[str, list[str]], Any]) -> list[dict]:
    qs: list[dict] = []
    t = text if isinstance(text, str) else ""
    needs_list = [n for n in needs if isinstance(n, Need)] if isinstance(needs, (list, tuple)) else []
    active_needs = [
        n for n in needs_list
        if _pos_finite(getattr(n, "quantity", None)) is not None
        and not (isinstance(getattr(n, "preferences", None), dict) and n.preferences.get("non_positive_quantity"))
    ]
    if not active_needs:
        return []
    if isinstance(now, datetime):
        now_d = now.date()
    elif isinstance(now, date):
        now_d = now
    else:
        now_d = now_helsinki().date()
    if now_d > date.max - timedelta(days=14):
        now_d = now_helsinki().date()
    if NEXT_WEEK.search(t) and not WEEKDAY.search(t):
        monday = now_d + timedelta(days=7 - now_d.weekday())
        days = [monday + timedelta(days=i) for i in (0, 2, 4)]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "Which day next week should it arrive?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    elif all(
        isinstance(getattr(n, "preferences", None), dict) and n.preferences.get("delivery_date_assumed")
        for n in active_needs
    ) and not WEEKDAY.search(t):
        d0 = now_d + timedelta(days=1)
        days = [d for d in (d0 + timedelta(days=i) for i in range(6)) if d.weekday() < 5][:3]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "When do you need the delivery?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    prof = profile if isinstance(profile, dict) else {}
    for i, n in enumerate(needs_list):
        if len(qs) >= MAX_QUESTIONS:
            break
        n_qty = _pos_finite(getattr(n, "quantity", None))
        n_prefs = n.preferences if isinstance(getattr(n, "preferences", None), dict) else {}
        if n_qty is None or n_prefs.get("non_positive_quantity"):
            continue
        q = n.product_query if isinstance(getattr(n, "product_query", None), str) else ""
        if not q.strip():
            continue
        n_unit = n.unit if getattr(n, "unit", None) in ("L", "KG", "PCS") else "PCS"
        raw_excl = getattr(n, "excluded_allergens", None)
        excl = [a for a in raw_excl if isinstance(a, str)] if isinstance(raw_excl, (list, tuple, set)) else []
        raw_claims = getattr(n, "required_claims", None)
        req_claims = [c for c in raw_claims if isinstance(c, str)] if isinstance(raw_claims, (list, tuple, set)) else []
        raw_pref_ids = getattr(n, "preferred_product_ids", None)
        pref_ids = [p for p in raw_pref_ids if isinstance(p, str) and p.strip()] if isinstance(raw_pref_ids, (list, tuple, set)) else []
        missing_qty = bool(t.strip()) and not _QTY_RE.search(t) and n_unit == "PCS" and n_qty == 1.0
        if "milk" in excl and DAIRY_ANY.search(q) and not PLANT.search(q) and not DAIRY_FREE.search(q):
            base = _conflict_base(q)
            plant_q = f"oat {base}"
            raw_hits = _call_search(search, plant_q, [])
            hits = [
                h for h in raw_hits
                if (PLANT.search(h["name"]) or "VEGAN" in h["claims"])
                and (n_unit not in ("L", "KG") or h["content_unit"] in (None, n_unit))
            ][:3]
            if not hits and base != "drink":
                alt_hits = _call_search(search, f"vegan {base}", ["VEGAN"])
                hits = [
                    h for h in alt_hits
                    if (PLANT.search(h["name"]) or "VEGAN" in h["claims"])
                    and (n_unit not in ("L", "KG") or h["content_unit"] in (None, n_unit))
                ][:3]
                if hits:
                    plant_q = f"vegan {base}"
            non_dairy_claims = [c for c in req_claims if c not in ("FREE_FROM_LACTOSE", "LOW_LACTOSE")]
            claim_patch = {"required_claims": non_dairy_claims} if len(non_dairy_claims) != len(req_claims) else {}
            opts = [
                {
                    "id": f"p{j}",
                    "label": f"Plant-based instead: {h['name'].strip() or h['id']}",
                    "product_id": h["id"],
                    "patch": {
                        "product_query": plant_q,
                        "preferred_product_ids": [h["id"]],
                        **claim_patch,
                        **(
                            {
                                "unit": h["content_unit"],
                                "quantity": h["content_amount"] if h["content_amount"] is not None else 10.0,
                            }
                            if missing_qty and h["content_unit"] in ("L", "KG")
                            else {}
                        ),
                    },
                }
                for j, h in enumerate(hits)
            ]
            opts.append({"id": "skip", "label": f"Leave out the {q}", "patch": {"cancel": True}})
            qs.append({"question_id": f"q_conflict_{i}", "type": "conflict", "item_index": i,
                       "question": f"Milk is excluded for this customer, but the request asks for {q}. What should I buy?",
                       "options": opts})
            continue
        if missing_qty:
            hits = _call_search(search, q, req_claims)
            raw_std = prof.get("standing_order")
            std = raw_std if isinstance(raw_std, dict) else {}
            std_qty = _pos_finite(std.get("qty_per_week"))
            std_u = std.get("unit") if std.get("unit") in ("L", "KG", "PCS") else None
            match = prof.get("usual_match")
            matched_usual = False
            if isinstance(match, str) and match:
                try:
                    matched_usual = bool(re.search(match, q, re.I))
                except re.error:
                    matched_usual = False
            has_std = bool(std_qty is not None and std_u is not None and matched_usual)
            q_terms = [
                w for w in re.findall(r"\w+", q.lower())
                if w not in ("lactose", "low", "gluten", "dairy", "milk", "egg", "soy", "sugar", "free", "organic", "vegan")
            ]
            rel_hits = [h for h in hits if any(w in h["name"].lower() for w in q_terms)] if q_terms else hits
            u = (
                std_u
                if has_std and std_u is not None
                else (
                    "L"
                    if LIQUID_ONLY.search(q)
                    else (
                        "KG"
                        if LIQUID_OR_BULK.search(q)
                        else next((h["content_unit"] for h in (rel_hits or hits) if h["content_unit"] in ("L", "KG", "PCS")), "PCS")
                    )
                )
            )
            base_amts = [10.0, 50.0, 100.0] if u == "L" else ([5.0, 10.0, 20.0] if u == "KG" else [10.0, 20.0, 50.0])
            opts = []
            if has_std and std_qty is not None:
                sq = std_qty
                opts.append({"id": "q0", "label": f"{sq:g} {u} (usual weekly order)", "patch": {"quantity": sq, "unit": u}})
                for amt in base_amts:
                    if amt != sq and len(opts) < 3:
                        opts.append({"id": f"q{len(opts)}", "label": f"{amt:g} {u}", "patch": {"quantity": amt, "unit": u}})
            else:
                opts = [{"id": f"q{j}", "label": f"{amt:g} {u}", "patch": {"quantity": amt, "unit": u}} for j, amt in enumerate(base_amts)]
            qs.append({"question_id": f"q_unit_{i}", "type": "unit", "item_index": i,
                       "question": f"How much {q} do you need?", "options": opts})
            if len(qs) >= MAX_QUESTIONS:
                break
        elif n_unit == "PCS" and LIQUID_OR_BULK.search(q):
            hits = _call_search(search, q, req_claims)
            seen, opts = set(), []
            for h in hits:
                ca, cu = h["content_amount"], h["content_unit"]
                if ca is None or cu not in ("L", "KG"):
                    continue
                key = (ca, cu)
                if key in seen:
                    continue
                amt = _pos_finite(ca * n_qty)
                if amt is None:
                    continue
                seen.add(key)
                disp_name = h["name"].strip() or h["id"]
                opts.append({"id": f"p{len(opts)}", "product_id": h["id"],
                             "label": f"{n_qty:g} x {_fmt_pack(h)}: {disp_name} (= {amt:g} {cu})",
                             "patch": {"unit": cu, "quantity": amt,
                                       "preferred_product_ids": [h["id"]]}})
                if len(opts) == 4:
                    break
            if len(opts) >= 2:
                qs.append({"question_id": f"q_unit_{i}", "type": "unit", "item_index": i,
                           "question": f"You asked for {n_qty:g} {q}: which pack size do you mean?", "options": opts})
            continue
        if (
            DAIRY_VARIANT.search(q)
            and not PLANT.search(q)
            and not DAIRY_FREE.search(q)
            and not VARIANT_WORDS.search(q)
            and not VARIANT_WORDS.search(_QTY_RE.sub(" ", t))
            and not pref_ids
        ):
            hits = _call_search(search, q, req_claims)[:10]
            raw_brand_pref = n_prefs.get("brand")
            pref_brand = raw_brand_pref.strip().lower() if isinstance(raw_brand_pref, str) else ""
            bulk_litres = n_unit == "L" and n_qty >= 10
            if pref_brand or bulk_litres:
                hits = sorted(
                    hits,
                    key=lambda h: (
                        0 if (not pref_brand or h["brand"].strip().lower() == pref_brand) else 1,
                        1 if (bulk_litres and SMALL_LIQUID_PACK.search(h["name"])) else 0,
                    ),
                )
            groups: dict[str, dict] = {}
            for h in hits:
                k = variant_key(h["name"])
                if k and k not in groups:
                    groups[k] = h
            if len(groups) >= 2:
                opts = [{"id": f"v{j}", "product_id": h["id"], "label": f"{k}: {h['name'].strip() or h['id']}",
                         "patch": {"preferred_product_ids": [h["id"]],
                                   "product_query": f"{q} {k.split(' (')[0]}"}}
                        for j, (k, h) in enumerate(list(groups.items())[:4])]
                qs.append({"question_id": f"q_variant_{i}", "type": "variant", "item_index": i,
                           "question": f"Which {q} do you want? The catalog has several variants.", "options": opts})
    return qs[:MAX_QUESTIONS]


def _target_query(question: Any) -> str | None:
    if not isinstance(question, dict):
        return None
    opts = _safe_options(question)
    if question.get("type") == "conflict":
        skip = next((o for o in opts if o.get("id") == "skip"), None)
        label = (skip or {}).get("label")
        if isinstance(label, str) and label.startswith("Leave out the "):
            return label[len("Leave out the "):]
    raw_q = question.get("question")
    text = raw_q if isinstance(raw_q, str) else ""
    for pat in (
        r"^You asked for \S+ (.+): which pack size do you mean\?$",
        r"^How much (.+) do you need\?$",
        r"^Which (.+) do you want\? The catalog has several variants\.$",
    ):
        m = re.match(pat, text)
        if m:
            return m.group(1)
    return None


def _resolve_idxs(out: list[Need], question: Any) -> list[int]:
    if not isinstance(out, (list, tuple)) or not out:
        return []
    if not isinstance(question, dict):
        return list(range(len(out)))
    idx = question.get("item_index")
    if idx is None:
        return list(range(len(out)))
    target_q = _target_query(question)
    if isinstance(idx, int) and not isinstance(idx, bool):
        if 0 <= idx < len(out):
            if target_q and getattr(out[idx], "product_query", None) != target_q:
                for j in range(idx - 1, -1, -1):
                    if getattr(out[j], "product_query", None) == target_q:
                        return [j]
                for j in range(idx + 1, len(out)):
                    if getattr(out[j], "product_query", None) == target_q:
                        return [j]
            return [idx]
        if target_q:
            for j in range(len(out) - 1, -1, -1):
                if getattr(out[j], "product_query", None) == target_q:
                    return [j]
        return [max(0, min(idx, len(out) - 1))]
    if target_q:
        for j in range(len(out) - 1, -1, -1):
            if getattr(out[j], "product_query", None) == target_q:
                return [j]
    return [0]


def _has_non_positive_qty(txt: Any) -> bool:
    if isinstance(txt, bool):
        return True
    if isinstance(txt, (int, float)):
        return _pos_finite(txt) is None
    if not isinstance(txt, str):
        return False
    s = txt.strip()
    if not s:
        return False
    if re.fullmatch(r"true|false", s, re.I):
        return True
    cleaned = re.sub(r"(?<!\S)[+-]?\d+(?:[.,]\d+)?\s*%", " ", s)
    cleaned = re.sub(r"(?<![A-Za-z0-9_-])\d+(?:[.,]\d+)?\s*%", " ", cleaned).strip()
    if not cleaned:
        return False
    if re.search(r"(?<![A-Za-z0-9_])[+-]?\s*(?:nan|inf(?:inity)?)(?![A-Za-z0-9_])", cleaned, re.I):
        return True
    for m_sci in re.finditer(r"(?<![A-Za-z0-9_])([+-]?\s*\d+(?:[.,]\d+)?[eE][+-]?\d+)\b", cleaned):
        sci_tok = re.sub(r"\s+", "", m_sci.group(1)).replace(",", ".")
        try:
            sv = float(sci_tok)
        except (ValueError, OverflowError):
            return True
        if not math.isfinite(sv) or sv <= 0.0:
            return True
    qty_scan = re.sub(r"^([+-])\s+(?=\d)", r"\1", cleaned)
    qty_scan_no_plus = re.sub(r"(?<!\S)\+(?=\d)", "", qty_scan)
    for m in _QTY_RE.finditer(qty_scan_no_plus):
        try:
            num = _to_number(m.group("num"))
        except (ValueError, OverflowError):
            return True
        if not math.isfinite(num) or num <= 0.0:
            return True
    try:
        pq = parse_quantity(qty_scan_no_plus)
    except (ValueError, OverflowError):
        return True
    if pq is not None and (not math.isfinite(pq[0]) or pq[0] <= 0.0):
        return True
    if re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)", qty_scan):
        try:
            bv = float(qty_scan.replace(",", "."))
        except (ValueError, OverflowError):
            return True
        if not math.isfinite(bv) or bv <= 0.0:
            return True
    if re.search(r"(?<!\S)-(?:\d+(?:[.,]\d*)?|[.,]\d+)", qty_scan):
        return True
    if re.search(r"(?<![A-Za-z0-9_.:-])[+-]?0+(?:[.,]0+)?(?!\d|[.,]\d|\s*:)(?:\b|(?=[A-Za-z]))", qty_scan):
        return True
    return False


def _boundary_contains(haystack: Any, needle: Any) -> bool:
    if not isinstance(haystack, str) or not isinstance(needle, str) or not needle:
        return False
    return bool(re.search(r"(?<![\w\d])" + re.escape(needle) + r"(?![\w\d])", haystack))


def _match_text_option(question: Any, txt: Any) -> dict | None:
    if not isinstance(question, dict) or not isinstance(txt, str):
        return None
    low = txt.strip().lower()
    if not low:
        return None
    options = _safe_options(question)
    by_id = next(
        (o for o in options if isinstance(o.get("id"), str) and o["id"].lower() == low),
        None,
    )
    if by_id is not None:
        return by_id
    if question.get("type") == "conflict" and re.search(
        r"\b(skip|cancel|leave\s+out|omit|drop|remove|jätä\s+pois|peru\w*|utelämna|hoppa\s+över|ta\s+bort)\b",
        low,
    ):
        skip_opt = next((o for o in options if o.get("id") == "skip"), None)
        if skip_opt is not None:
            return skip_opt
    for o in options:
        raw_lbl = o.get("label")
        if not isinstance(raw_lbl, str) or not raw_lbl.strip():
            continue
        lbl = raw_lbl.lower()
        if _boundary_contains(lbl, low) or _boundary_contains(low, lbl):
            return o
    if question.get("type") == "variant":
        vk = "fat-free / skimmed" if re.search(r"(?<!\d)0(?:[.,]0+)?\s*%", low) else variant_key(low)
        if vk:
            vk_low = vk.lower()
            hit = next(
                (
                    o for o in options
                    if isinstance(o.get("label"), str) and o["label"].lower().startswith(vk_low)
                ),
                None,
            )
            if hit is not None:
                return hit
            if vk_low.endswith(" % fat"):
                pct = vk_low[:-len(" fat")]
                hit = next(
                    (
                        o for o in options
                        if isinstance(o.get("label"), str) and pct in o["label"].lower()
                    ),
                    None,
                )
                if hit is not None:
                    return hit
    return None


def _cancel_needs(out: Any, idxs: Any, qid: Any, invalid_qty: bool = False) -> list[Need]:
    if not isinstance(out, (list, tuple)) or not out:
        return []
    need_items = [n for n in out if isinstance(n, Need)]
    if not need_items:
        return []
    idx_set = set(idxs) if isinstance(idxs, (list, tuple, set)) else set()
    qid_str = qid.strip() if isinstance(qid, str) and qid.strip() else "q_clarify"
    if not invalid_qty:
        remaining = [n for j, n in enumerate(need_items) if j not in idx_set]
        if remaining:
            return remaining
    extra = {"non_positive_quantity": True} if invalid_qty else {}
    targets = need_items if invalid_qty else [need_items[0]]
    cancelled: list[Need] = []
    for n in targets:
        prefs = dict(n.preferences) if isinstance(getattr(n, "preferences", None), dict) else {}
        prev_clar = prefs.get("clarified")
        clar_list = list(prev_clar) if isinstance(prev_clar, (list, tuple)) else []
        clar_list.append(qid_str)
        cancelled.append(
            n.model_copy(
                update={
                    "preferences": {
                        **prefs,
                        "cancelled_by_clarification": True,
                        **extra,
                        "clarified": clar_list,
                    }
                }
            )
        )
    return cancelled


def apply_answer(needs: list[Need], question: dict, answer: dict | None) -> tuple[list[Need], str]:
    """Apply the chosen option's patch. Returns (needs, human-readable resolution)."""
    unchanged = [n for n in needs if isinstance(n, Need)] if isinstance(needs, (list, tuple)) else []
    if not unchanged or not answer or not isinstance(answer, dict) or not isinstance(question, dict):
        return unchanged, ""
    out = list(unchanged)
    idxs = _resolve_idxs(out, question)
    raw_qid = question.get("question_id")
    qid = raw_qid.strip() if isinstance(raw_qid, str) and raw_qid.strip() else "q_clarify"
    options = _safe_options(question)
    oid = answer.get("option_id")
    opt = (
        next(
            (
                o for o in options
                if isinstance(o.get("id"), (str, int))
                and not isinstance(o.get("id"), bool)
                and o.get("id") == oid
            ),
            None,
        )
        if isinstance(oid, (str, int)) and not isinstance(oid, bool)
        else None
    )
    raw_text_field = answer.get("text")
    raw_txt = ""
    if opt is None and raw_text_field is not None:
        if _has_non_positive_qty(raw_text_field):
            return _cancel_needs(out, idxs, qid, invalid_qty=True), f"rejected non-positive quantity: {raw_text_field}"
        if isinstance(raw_text_field, int) and not isinstance(raw_text_field, bool):
            pf_txt = _pos_finite(raw_text_field)
            raw_txt = str(raw_text_field) if pf_txt is not None else ""
        elif isinstance(raw_text_field, float):
            pf_txt = _pos_finite(raw_text_field)
            if pf_txt is not None:
                raw_txt = str(int(pf_txt)) if pf_txt.is_integer() and abs(pf_txt) < 1e16 else str(pf_txt)
        elif isinstance(raw_text_field, str):
            raw_txt = raw_text_field.strip()
    if opt is None and raw_txt:
        if _has_non_positive_qty(raw_txt):
            return _cancel_needs(out, idxs, qid, invalid_qty=True), f"rejected non-positive quantity: {raw_txt}"
        opt = _match_text_option(question, raw_txt)
    patch: dict[str, Any] | None = None
    resolved_label = ""
    if opt is not None:
        raw_patch = opt.get("patch")
        if not isinstance(raw_patch, dict):
            return unchanged, ""
        patch = dict(raw_patch)
        raw_lbl = opt.get("label")
        oid_lbl = str(opt["id"]) if isinstance(opt.get("id"), (str, int)) and not isinstance(opt.get("id"), bool) else ""
        resolved_label = raw_lbl if isinstance(raw_lbl, str) and raw_lbl.strip() else (oid_lbl or "selected option")
    elif raw_txt:
        qtype = question.get("type")
        q_prompt = question.get("question") if isinstance(question.get("question"), str) else ""
        if qtype == "date":
            ref_now: datetime | None = None
            if q_prompt == "Which day next week should it arrive?" and options:
                first_patch = options[0].get("patch") if isinstance(options[0].get("patch"), dict) else {}
                d0 = _parse_iso_date(first_patch.get("delivery_by"))
                if d0 is not None:
                    try:
                        ref_now = datetime.combine(d0 - timedelta(days=1), datetime.min.time(), tzinfo=HELSINKI)
                    except (ValueError, OverflowError):
                        ref_now = None
            elif out and isinstance(getattr(out[0], "preferences", None), dict) and out[0].preferences.get("delivery_date_assumed"):
                d0_need = _parse_iso_date(getattr(out[0], "delivery_by", None))
                if d0_need is not None:
                    try:
                        d_base = d0_need - timedelta(days=2)
                        ref_now = datetime.combine(d_base, datetime.min.time(), tzinfo=HELSINKI)
                    except (ValueError, OverflowError):
                        ref_now = None
            try:
                rd = resolve_date(raw_txt, ref_now)
            except (ValueError, OverflowError, TypeError):
                rd = None
            if rd is not None:
                patch = {"delivery_by": rd[0].isoformat()}
                resolved_label = f"free text: {raw_txt}"
        elif qtype == "unit":
            norm_qty_txt = re.sub(r"(?<!\S)\+\s*(?=\d)", "", raw_txt)
            try:
                pq = parse_quantity(norm_qty_txt)
            except (ValueError, OverflowError):
                pq = None
            pq_val = _pos_finite(pq[0]) if pq is not None else None
            if pq is not None and pq_val is not None and pq[1] in ("L", "KG", "PCS"):
                patch = {"quantity": pq_val, "unit": pq[1]}
                resolved_label = f"free text: {raw_txt}"
            elif q_prompt.startswith("How much "):
                m_num = re.fullmatch(r"\+?\s*(\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)", raw_txt)
                if m_num:
                    try:
                        raw_num = float(m_num.group(1).replace(",", "."))
                    except (ValueError, OverflowError):
                        raw_num = 0.0
                    val = _pos_finite(raw_num)
                    if val is not None:
                        first_patch = options[0].get("patch") if options and isinstance(options[0].get("patch"), dict) else {}
                        raw_u = first_patch.get("unit")
                        opt_u = raw_u if raw_u in ("L", "KG", "PCS") else "PCS"
                        patch = {"quantity": val, "unit": opt_u}
                        resolved_label = f"free text: {raw_txt}"
    if patch is None:
        return unchanged, ""
    if patch.pop("cancel", False) is True:
        return _cancel_needs(out, idxs, qid, invalid_qty=False), resolved_label
    if "quantity" in patch:
        qval = _pos_finite(patch["quantity"])
        if qval is None:
            return _cancel_needs(out, idxs, qid, invalid_qty=True), "rejected non-positive quantity"
        patch["quantity"] = qval
    parsed_delivery: date | None = None
    if "delivery_by" in patch:
        parsed_delivery = _parse_iso_date(patch["delivery_by"])
        if parsed_delivery is None:
            return unchanged, ""
    if "unit" in patch and patch["unit"] not in ("L", "KG", "PCS"):
        return unchanged, ""
    if "product_query" in patch:
        pq_str = patch["product_query"]
        if not isinstance(pq_str, str) or not pq_str.strip():
            return unchanged, ""
        patch["product_query"] = pq_str.strip()
    if "required_claims" in patch:
        rc = patch["required_claims"]
        if not isinstance(rc, (list, tuple)):
            return unchanged, ""
        patch["required_claims"] = [c for c in rc if isinstance(c, str)]
    if "preferred_product_ids" in patch:
        pp = patch["preferred_product_ids"]
        if not isinstance(pp, (list, tuple)):
            return unchanged, ""
        clean_pp = [p.strip() for p in pp if isinstance(p, str) and p.strip()]
        if pp and not clean_pp:
            return unchanged, ""
        patch["preferred_product_ids"] = clean_pp
    recognized = {"delivery_by", "unit", "quantity", "product_query", "required_claims", "preferred_product_ids"}
    if not (recognized & set(patch.keys())) or not idxs:
        return unchanged, ""
    for j in idxs:
        n = out[j]
        upd: dict[str, Any] = {}
        if parsed_delivery is not None:
            upd["delivery_by"] = parsed_delivery
        for k in ("unit", "quantity", "product_query", "required_claims"):
            if k in patch:
                upd[k] = patch[k]
        if "preferred_product_ids" in patch:
            raw_existing_pp = getattr(n, "preferred_product_ids", None)
            existing_pp = (
                [p for p in raw_existing_pp if isinstance(p, str) and p.strip()]
                if isinstance(raw_existing_pp, (list, tuple, set))
                else []
            )
            upd["preferred_product_ids"] = list(dict.fromkeys([*patch["preferred_product_ids"], *existing_pp]))
        prefs = dict(n.preferences) if isinstance(getattr(n, "preferences", None), dict) else {}
        if parsed_delivery is not None:
            prefs.pop("delivery_date_assumed", None)
        prev_clar = prefs.get("clarified")
        clar_list = list(prev_clar) if isinstance(prev_clar, (list, tuple)) else []
        clar_list.append(qid)
        prefs["clarified"] = clar_list
        upd["preferences"] = prefs
        out[j] = n.model_copy(update=upd)
    return out, resolved_label
