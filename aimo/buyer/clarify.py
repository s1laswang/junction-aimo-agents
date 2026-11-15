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

from aimo.buyer.need import HELSINKI, _QTY_RE, _to_number, parse_quantity, resolve_date
from aimo.models import Need

MAX_QUESTIONS = 2
DAIRY_VARIANT = re.compile(
    r"\b(milk|maito\w*|maidon|mjölk\w*|cream|kerm\w*|grädd\w*|yogh?urt\w*|jogurt\w*|rahk\w*|quark|kvarg\w*|piim\w*|filmjölk\w*|buttermilk)\b",
    re.I,
)
VARIANT_WORDS = re.compile(
    r"\d+(?:[.,]\d+)?\s*%|skim|semi|whole|full[- ]fat|fat[- ]free|low[- ]fat|light|kevyt|rasvato\w*|rasvattom\w*|täys|"
    r"lätt|mellanmjölk|minimjölk|fettfri|helmjölk|standardmjölk|whipping|vispi|kuohu|vispgrädd|cooking|ruoka|matlagning|"
    r"sour|hapan|barista|organic|luomu|ekologisk|\b\d+(?:[.,]\d+)?\s*(?:l|dl|ml|kg|g)\b",
    re.I,
)
DAIRY_ANY = re.compile(
    r"\b(milk|maito\w*|maidon|mjölk\w*|cream|kerm\w*|grädd\w*|butter|voi(?:ta|n)?|smör\w*|cheese|juust\w*|ost(?:en|ar)?|"
    r"yogh?urt\w*|jogurt\w*|rahk\w*|quark|kvarg\w*|piim\w*|filmjölk\w*|buttermilk)\b",
    re.I,
)
DAIRY_FREE = re.compile(r"\b(dairy[- ]free|milk[- ]free|maidoto\w*|maidottom\w*|mjölkfri\w*)\b", re.I)
PLANT = re.compile(
    r"\b(oat|kaura\w*|havre\w*|soy|soija\w*|soja\w*|plant|vegan\w*|vegansk\w*|almond|manteli\w*|mandel\w*|"
    r"pea|herne\w*|ärt\w*|coconut|kookos\w*|kokos\w*|rice|riisi\w*|ris\w*|peanut\w*|maapähkin\w*|jordnöt\w*)\b",
    re.I,
)
LIQUID_OR_BULK = re.compile(
    r"\b(milk|maito\w*|maidon|mjölk\w*|cream|kerm\w*|grädd\w*|drink|juom\w*|dryck\w*|juice|mehu\w*|saft\w*|"
    r"butter|voi(?:ta|n)?|smör\w*|cheese|juust\w*|ost(?:en|ar)?|yogh?urt\w*|jogurt\w*|rahk\w*|quark|kvarg\w*|"
    r"piim\w*|filmjölk\w*|buttermilk|flour|jauho\w*|mjöl|sugar|sokeri\w*|socker|oil|öljy\w*|olja)\b",
    re.I,
)
LIQUID_ONLY = re.compile(
    r"\b(milk|maito\w*|maidon|mjölk\w*|cream|kerm\w*|grädd\w*|drink|juom\w*|dryck\w*|juice|mehu\w*|saft\w*|"
    r"piim\w*|filmjölk\w*|buttermilk|oil|öljy\w*|olja)\b",
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


def variant_key(name: str) -> str | None:
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
    return [p for p in (res or []) if isinstance(p, dict) and p.get("id")]


def _fmt_pack(p: dict) -> str:
    a, u = p.get("content_amount"), p.get("content_unit")
    return f"{a:g} {u}" if isinstance(a, (int, float)) and u else "pack size unknown"


def _conflict_base(q: str) -> str:
    if re.search(r"\b(milk|maito\w*|maidon|mjölk\w*|piim\w*|filmjölk\w*|buttermilk)\b", q, re.I):
        return "drink"
    if re.search(r"\b(cream|kerm\w*|grädd\w*)\b", q, re.I):
        return "cream"
    if re.search(r"\b(yogh?urt\w*|jogurt\w*|rahk\w*|quark|kvarg\w*)\b", q, re.I):
        return "yogurt"
    if re.search(r"\b(butter|voi(?:ta|n)?|smör\w*)\b", q, re.I):
        return "butter"
    if re.search(r"\b(cheese|juust\w*|ost(?:en|ar)?)\b", q, re.I):
        return "cheese"
    m = DAIRY_ANY.search(q)
    return m.group(1).lower() if m else "drink"


def find_questions(needs: list[Need], profile: dict, text: str, now: datetime,
                   search: Callable[[str, list[str]], Any]) -> list[dict]:
    qs: list[dict] = []
    t = text or ""
    if NEXT_WEEK.search(t) and not WEEKDAY.search(t):
        monday = now.date() + timedelta(days=7 - now.weekday())
        days = [monday + timedelta(days=i) for i in (0, 2, 4)]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "Which day next week should it arrive?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    elif needs and all(n.preferences.get("delivery_date_assumed") for n in needs) and not WEEKDAY.search(t):
        d0 = now.date() + timedelta(days=1)
        days = [d for d in (d0 + timedelta(days=i) for i in range(6)) if d.weekday() < 5][:3]
        qs.append({"question_id": "q_date", "type": "date", "item_index": None,
                   "question": "When do you need the delivery?",
                   "options": [{"id": f"d{i}", "label": d.strftime("%A %d %b"), "patch": {"delivery_by": d.isoformat()}}
                               for i, d in enumerate(days)]})
    for i, n in enumerate(needs):
        if len(qs) >= MAX_QUESTIONS:
            break
        if n.quantity <= 0:
            continue
        q = n.product_query
        missing_qty = bool(t.strip()) and not _QTY_RE.search(t) and n.unit == "PCS" and n.quantity == 1.0
        if "milk" in n.excluded_allergens and DAIRY_ANY.search(q) and not PLANT.search(q) and not DAIRY_FREE.search(q):
            base = _conflict_base(q)
            plant_q = f"oat {base}"
            raw_hits = _products(search(plant_q, []))
            hits = [
                h for h in raw_hits
                if (PLANT.search(str(h.get("name") or "")) or "VEGAN" in (h.get("claims") or []))
                and (n.unit not in ("L", "KG") or h.get("content_unit") in (None, n.unit))
            ][:3]
            if not hits and base != "drink":
                alt_hits = _products(search(f"vegan {base}", ["VEGAN"]))
                hits = [
                    h for h in alt_hits
                    if (PLANT.search(str(h.get("name") or "")) or "VEGAN" in (h.get("claims") or []))
                    and (n.unit not in ("L", "KG") or h.get("content_unit") in (None, n.unit))
                ][:3]
                if hits:
                    plant_q = f"vegan {base}"
            non_dairy_claims = [c for c in n.required_claims if c not in ("FREE_FROM_LACTOSE", "LOW_LACTOSE")]
            claim_patch = {"required_claims": non_dairy_claims} if len(non_dairy_claims) != len(n.required_claims) else {}
            opts = [
                {
                    "id": f"p{j}",
                    "label": f"Plant-based instead: {h['name']}",
                    "product_id": h["id"],
                    "patch": {
                        "product_query": plant_q,
                        "preferred_product_ids": [h["id"]],
                        **claim_patch,
                        **(
                            {"unit": h["content_unit"], "quantity": float(h.get("content_amount") or 10.0)}
                            if missing_qty and h.get("content_unit") in ("L", "KG")
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
            hits = _products(search(q, n.required_claims))
            std = (profile or {}).get("standing_order") or {}
            match = (profile or {}).get("usual_match")
            has_std = bool(std.get("qty_per_week") and std.get("unit") and match and re.search(match, q, re.I))
            q_terms = [
                w for w in re.findall(r"\w+", q.lower())
                if w not in ("lactose", "low", "gluten", "dairy", "milk", "egg", "soy", "sugar", "free", "organic", "vegan")
            ]
            rel_hits = [h for h in hits if any(w in str(h.get("name") or "").lower() for w in q_terms)] if q_terms else hits
            u = (
                str(std["unit"])
                if has_std
                else (
                    "L"
                    if LIQUID_ONLY.search(q)
                    else (
                        "KG"
                        if LIQUID_OR_BULK.search(q)
                        else next((h["content_unit"] for h in (rel_hits or hits) if h.get("content_unit") in ("L", "KG", "PCS")), "PCS")
                    )
                )
            )
            base_amts = [10.0, 50.0, 100.0] if u == "L" else ([5.0, 10.0, 20.0] if u == "KG" else [10.0, 20.0, 50.0])
            opts = []
            if has_std:
                sq = float(std["qty_per_week"])
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
        elif n.unit == "PCS" and LIQUID_OR_BULK.search(q):
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
        if (
            DAIRY_VARIANT.search(q)
            and not PLANT.search(q)
            and not DAIRY_FREE.search(q)
            and not VARIANT_WORDS.search(q)
            and not VARIANT_WORDS.search(_QTY_RE.sub(" ", t))
            and not n.preferred_product_ids
        ):
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


def _target_query(question: dict) -> str | None:
    if question.get("type") == "conflict":
        skip = next((o for o in question.get("options", []) if o.get("id") == "skip"), None)
        label = (skip or {}).get("label", "")
        if label.startswith("Leave out the "):
            return label[len("Leave out the "):]
    text = question.get("question") or ""
    for pat in (
        r"^You asked for \S+ (.+): which pack size do you mean\?$",
        r"^How much (.+) do you need\?$",
        r"^Which (.+) do you want\? The catalog has several variants\.$",
    ):
        m = re.match(pat, text)
        if m:
            return m.group(1)
    return None


def _resolve_idxs(out: list[Need], question: dict) -> list[int]:
    if not out:
        return []
    idx = question.get("item_index")
    if idx is None:
        return list(range(len(out)))
    target_q = _target_query(question)
    if 0 <= idx < len(out):
        if target_q and out[idx].product_query != target_q:
            for j in range(idx - 1, -1, -1):
                if out[j].product_query == target_q:
                    return [j]
        return [idx]
    if target_q:
        for j in range(len(out) - 1, -1, -1):
            if out[j].product_query == target_q:
                return [j]
    return [max(0, min(idx, len(out) - 1))]


def _has_non_positive_qty(txt: str) -> bool:
    cleaned = re.sub(r"(?<!\S)-?\d+(?:[.,]\d+)?\s*%", " ", txt.strip())
    for m in _QTY_RE.finditer(cleaned):
        if _to_number(m.group("num")) <= 0:
            return True
    pq = parse_quantity(cleaned)
    if pq is not None and pq[0] <= 0:
        return True
    if re.fullmatch(r"-\s*\d+(?:[.,]\d+)?|0+(?:[.,]0+)?", cleaned.strip()):
        return True
    if re.search(r"(?<!\S)-\d+(?:[.,]\d+)?\b", cleaned):
        return True
    return False


def _boundary_contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return bool(re.search(r"(?<![\w\d])" + re.escape(needle) + r"(?![\w\d])", haystack))


def _match_text_option(question: dict, txt: str) -> dict | None:
    low = txt.strip().lower()
    if not low:
        return None
    options = question.get("options") or []
    by_id = next((o for o in options if o.get("id", "").lower() == low), None)
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
        lbl = o["label"].lower()
        if _boundary_contains(lbl, low) or _boundary_contains(low, lbl):
            return o
    if question.get("type") == "variant":
        vk = "fat-free / skimmed" if re.search(r"(?<!\d)0(?:[.,]0+)?\s*%", low) else variant_key(low)
        if vk:
            vk_low = vk.lower()
            hit = next((o for o in options if o["label"].lower().startswith(vk_low)), None)
            if hit is not None:
                return hit
            if vk_low.endswith(" % fat"):
                pct = vk_low[:-len(" fat")]
                hit = next((o for o in options if pct in o["label"].lower()), None)
                if hit is not None:
                    return hit
    return None


def _cancel_needs(out: list[Need], idxs: list[int], qid: str, invalid_qty: bool = False) -> list[Need]:
    if not out:
        return out
    if not invalid_qty:
        remaining = [n for j, n in enumerate(out) if j not in idxs]
        if remaining:
            return remaining
    extra = {"non_positive_quantity": True} if invalid_qty else {}
    targets = out if invalid_qty else [out[0]]
    return [
        n.model_copy(
            update={
                "preferences": {
                    **n.preferences,
                    "cancelled_by_clarification": True,
                    **extra,
                    "clarified": [*n.preferences.get("clarified", []), qid],
                }
            }
        )
        for n in targets
    ]


def apply_answer(needs: list[Need], question: dict, answer: dict | None) -> tuple[list[Need], str]:
    """Apply the chosen option's patch. Returns (needs, human-readable resolution)."""
    if not answer:
        return needs, ""
    out = list(needs)
    idxs = _resolve_idxs(out, question)
    qid = question["question_id"]
    oid = answer.get("option_id")
    opt = next((o for o in question["options"] if o["id"] == oid), None) if oid is not None else None
    raw_txt = (answer.get("text") or "").strip()
    if opt is None and raw_txt:
        if _has_non_positive_qty(raw_txt):
            return _cancel_needs(out, idxs, qid, invalid_qty=True), f"rejected non-positive quantity: {raw_txt}"
        opt = _match_text_option(question, raw_txt)
    patch: dict[str, Any] | None = None
    resolved_label = ""
    if opt is not None:
        patch = dict(opt.get("patch") or {})
        resolved_label = opt["label"]
    elif raw_txt:
        qtype = question.get("type")
        if qtype == "date":
            ref_now: datetime | None = None
            if question.get("question") == "Which day next week should it arrive?" and question.get("options"):
                d0 = date.fromisoformat(question["options"][0]["patch"]["delivery_by"])
                ref_now = datetime.combine(d0 - timedelta(days=1), datetime.min.time(), tzinfo=HELSINKI)
            elif out and (out[0].preferences or {}).get("delivery_date_assumed"):
                d_base = out[0].delivery_by - timedelta(days=2)
                ref_now = datetime.combine(d_base, datetime.min.time(), tzinfo=HELSINKI)
            rd = resolve_date(raw_txt, ref_now)
            if rd is not None:
                patch = {"delivery_by": rd[0].isoformat()}
                resolved_label = f"free text: {raw_txt}"
        elif qtype == "unit":
            pq = parse_quantity(raw_txt)
            if pq is not None and pq[0] > 0:
                patch = {"quantity": pq[0], "unit": pq[1]}
                resolved_label = f"free text: {raw_txt}"
            elif (question.get("question") or "").startswith("How much "):
                m_num = re.fullmatch(r"\+?\s*(\d+(?:[.,]\d+)?)", raw_txt)
                if m_num:
                    val = float(m_num.group(1).replace(",", "."))
                    if val > 0:
                        opt_u = ((question.get("options") or [{}])[0].get("patch") or {}).get("unit", "PCS")
                        patch = {"quantity": val, "unit": opt_u}
                        resolved_label = f"free text: {raw_txt}"
    if patch is None:
        return needs, ""
    if patch.pop("cancel", False):
        return _cancel_needs(out, idxs, qid, invalid_qty=False), resolved_label
    if "quantity" in patch:
        qval = patch["quantity"]
        if not isinstance(qval, (int, float)) or isinstance(qval, bool) or float(qval) <= 0:
            return _cancel_needs(out, idxs, qid, invalid_qty=True), "rejected non-positive quantity"
    for j in idxs:
        n = out[j]
        upd: dict[str, Any] = {}
        if "delivery_by" in patch:
            upd["delivery_by"] = date.fromisoformat(patch["delivery_by"])
        for k in ("unit", "quantity", "product_query", "required_claims"):
            if k in patch:
                upd[k] = patch[k]
        if "preferred_product_ids" in patch:
            upd["preferred_product_ids"] = list(dict.fromkeys([*patch["preferred_product_ids"], *n.preferred_product_ids]))
        prefs = dict(n.preferences)
        prefs.pop("delivery_date_assumed", None) if "delivery_by" in patch else None
        prefs.setdefault("clarified", []).append(qid)
        upd["preferences"] = prefs
        out[j] = n.model_copy(update=upd)
    return out, resolved_label
