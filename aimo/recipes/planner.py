"""Recipe planner: dish + portions -> one Need per ingredient -> catalog plans -> quote lines.

Credit: dish data, alias matching, cover parsing and swap rules are Momin's (junction26-valio,
`aimo/recipes.py` `menu_to_need`, `recipe_swaps`, `_dishes_in`, `_default_covers`; data
`data_extra/recipes.json`). Adapted here to our `Need` / `Plan` contract and our catalog planner.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from aimo.models import Need, Offer, Plan

log = logging.getLogger("aimo.recipes")

_DATA = json.loads((Path(__file__).resolve().parent / "recipes.json").read_text())
DISHES: list[dict[str, Any]] = _DATA["dishes"]
SWAPS: list[dict[str, Any]] = _DATA["swaps"]
DATA_NOTE = _DATA.get("note", "")
SOURCE = "aimo.recipes (recipe data and swap rules ported from Momin's junction26-valio)"

# Momin's unit table: recipe units -> our Need units (divisor)
_UNITS = {"g": ("KG", 1000.0), "ml": ("L", 1000.0), "pcs": ("PCS", 1.0)}
# Momin's cover words (en + fi)
_COVERS = r"(?:guests?|people|persons?|covers?|portions?|servings?|pax|hengelle|henkilölle|henkilolle|hengen|henkeä|annosta|annos)"


# Category guard per ingredient query (our addition): the planner only uses offers for products whose
# Valio category code starts with one of these prefixes, so "milk" never plans a milk-system cleaner and
# "sugar" never plans sugar peas. Prefixes were read off our catalog's search results for each query
# (data/catalog.sqlite, 14 Nov). Category NAMES are not in the data set; these are codes only.
CATEGORY_HINTS: dict[str, list[str]] = {
    "bacon": ["17202"], "beef cubes": ["1872", "17209"], "butter": ["1050"], "carrot": ["1743"],
    "chicken fillet": ["1875", "17210"], "cooking cream": ["10401"], "crushed tomatoes": ["21590"],
    "cucumber": ["17402"], "dill": ["22630", "17451"], "eggs": ["10590"], "gluten-free flour mix": ["2131", "2132", "2137"],
    "gluten-free oats": ["2137", "2138"], "gluten-free pasta": ["21380"], "grated cheese": ["1060"],
    "ground beef": ["41702"], "iceberg lettuce": ["17403"], "lactose free margarine": ["1051"],
    "lactose-free butter": ["10500"], "lactose-free cheese": ["106"], "lactose-free cream": ["10401"],
    "lactose-free grated cheese": ["10603"], "lactose-free cooking cream": ["10401"], "lactose-free whipping cream": ["10401"],
    "lactose-free milk": ["10400"], "lasagne": ["21380"], "leek": ["17405", "41731"], "lingonberry jam": ["21450"],
    "macaroni": ["21380"], "meatballs": ["17300", "41705"], "milk": ["10400"], "oat cooking": ["10402", "10406"],
    "oat drink": ["10402"], "oat flakes": ["21370"], "onion": ["17405"], "pasta": ["21380"], "pepper red": ["17406"],
    "pork shoulder": ["18710"], "potato": ["17421"], "rapeseed oil": ["10530"], "rice": ["21340"],
    "salmon fillet": ["1870"], "sugar": ["22100"], "tomatoes": ["17401"], "wheat flour": ["2130", "2131"],
}


# Name words that make a product the wrong thing for the recipe line even inside the right category
# (e.g. ground beef LIVER for a lasagne). Our addition.
NAME_EXCLUDE: dict[str, list[str]] = {
    "ground beef": ["liver", "maksa", "patty", "burger", "meatball", "bolognese", "cooked"],
    "milk": ["cleaner", "jug", "powder"],
    "onion": ["mix"],
    "pasta": ["gnocchi", "plate", "bowl"],
}


class RecipeError(ValueError):
    """Bad recipe request (unknown recipe, no portions...). The gateway maps it to HTTP 400/404."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


MAX_PORTIONS = 100_000
MAX_NEED_QTY = 1_000_000.0
MAX_TEXT_LEN = 4000
MAX_DELIVERY_LEN = 256
MAX_CUSTOMER_ID_LEN = 128
MAX_ALLERGEN_ITEMS = 128
MAX_ALLERGEN_LEN = 128
MAX_ON_HAND_ITEMS = 500
MAX_ON_HAND_KEY_LEN = 256
_NUM_TOKEN = r"(?<![\w.:/-])(?<!\d,)(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:[.,]\d+)?)(?![-./:%\d])(?![.,]\d)"
_POST_X_NUM_TOKEN = r"(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:[.,]\d+)?)(?![-./:%\w])(?![.,]\d)"


def _has_ctrl(value: str) -> bool:
    return any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value)


def _has_text_ctrl(value: str) -> bool:
    return any((ord(ch) < 32 and ch not in "\t\n\r") or 127 <= ord(ch) <= 159 for ch in value)


def _clean_text_arg(value: Any, field: str = "text") -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise RecipeError(f"bad_{field}", f"{field} must be a string (got {type(value).__name__})", 422)
    if _has_text_ctrl(value):
        raise RecipeError(f"bad_{field}", f"{field} contains invalid control characters", 422)
    if len(value) > MAX_TEXT_LEN:
        raise RecipeError(f"bad_{field}", f"{field} must be at most {MAX_TEXT_LEN} characters", 422)
    return value


def _validate_customer_id(customer_id: Any) -> str:
    if isinstance(customer_id, bool) or not isinstance(customer_id, str):
        raise RecipeError("bad_customer_id", "customer_id must be a non-empty string", 422)
    if _has_ctrl(customer_id):
        raise RecipeError("bad_customer_id", "customer_id contains invalid control characters", 422)
    cid = customer_id.strip()
    if not cid or len(cid) > MAX_CUSTOMER_ID_LEN or "/" in cid or "\\" in cid or ".." in cid:
        raise RecipeError("bad_customer_id", "customer_id must be a valid identifier (1-128 characters)", 422)
    return cid


def _validate_portions(portions: Any) -> Optional[int]:
    if portions is None:
        return None
    if isinstance(portions, bool) or not isinstance(portions, (int, float)):
        raise RecipeError(
            "bad_portions",
            f"portions must be a whole integer between 1 and {MAX_PORTIONS} (got {portions!r})",
            422,
        )
    if isinstance(portions, float):
        if not math.isfinite(portions) or not portions.is_integer():
            raise RecipeError(
                "bad_portions",
                f"portions must be a whole integer between 1 and {MAX_PORTIONS} (got {portions!r})",
                422,
            )
        iv = int(portions)
    else:
        iv = portions
    if iv <= 0 or iv > MAX_PORTIONS:
        raise RecipeError(
            "bad_portions",
            f"portions must be between 1 and {MAX_PORTIONS} (got {portions!r})",
            422,
        )
    return iv


def _parse_portion_token(raw: str) -> int:
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw):
        normalized = raw.replace(",", "")
    elif "." in raw or "," in raw:
        raise RecipeError(
            "bad_portions",
            f"portions must be a whole integer between 1 and {MAX_PORTIONS} (got {raw!r})",
            422,
        )
    else:
        normalized = raw
    try:
        val = int(normalized)
    except ValueError as e:
        raise RecipeError("bad_portions", f"invalid portion count {raw!r}", 422) from e
    if val <= 0 or val > MAX_PORTIONS:
        raise RecipeError("bad_portions", f"portions must be between 1 and {MAX_PORTIONS} (got {val})", 422)
    return val


def _validate_excluded_allergens(excluded: Any) -> list[str]:
    if excluded is None:
        return []
    if isinstance(excluded, str):
        if _has_ctrl(excluded) or len(excluded) > MAX_ALLERGEN_LEN:
            raise RecipeError(
                "bad_allergens",
                "excluded_allergens contains invalid characters or exceeds maximum length",
                422,
            )
        return [excluded]
    if isinstance(excluded, (bool, int, float, bytes, bytearray, dict)):
        raise RecipeError("bad_allergens", "excluded_allergens must be a sequence of allergen strings", 422)
    items: list[str] = []
    try:
        it = iter(excluded)
    except TypeError as e:
        raise RecipeError("bad_allergens", "excluded_allergens must be a sequence of allergen strings", 422) from e
    for item in it:
        if len(items) >= MAX_ALLERGEN_ITEMS:
            raise RecipeError(
                "bad_allergens",
                f"excluded_allergens exceeds maximum of {MAX_ALLERGEN_ITEMS} items",
                422,
            )
        if isinstance(item, bool) or not isinstance(item, str):
            raise RecipeError(
                "bad_allergens",
                f"excluded_allergens items must be strings (got {type(item).__name__})",
                422,
            )
        if _has_ctrl(item) or len(item) > MAX_ALLERGEN_LEN:
            raise RecipeError(
                "bad_allergens",
                "excluded_allergens item contains invalid characters or exceeds maximum length",
                422,
            )
        items.append(item)
    return items


def _validate_on_hand(on_hand: Any) -> dict[str, float]:
    if on_hand is None:
        return {}
    if not isinstance(on_hand, dict):
        raise RecipeError(
            "bad_on_hand",
            f"on_hand must be a dict mapping ingredient query to quantity >= 0 (got {type(on_hand).__name__})",
            422,
        )
    if len(on_hand) > MAX_ON_HAND_ITEMS:
        raise RecipeError("bad_on_hand", f"on_hand exceeds maximum of {MAX_ON_HAND_ITEMS} entries", 422)
    cleaned: dict[str, float] = {}
    for k, v in on_hand.items():
        if isinstance(k, bool) or not isinstance(k, str) or _has_ctrl(k):
            raise RecipeError("bad_on_hand", f"on_hand keys must be valid strings (got {k!r})", 422)
        key = k.strip().lower()
        if not key or len(key) > MAX_ON_HAND_KEY_LEN:
            raise RecipeError("bad_on_hand", f"on_hand key must be 1-{MAX_ON_HAND_KEY_LEN} characters (got {k!r})", 422)
        if v is None or isinstance(v, bool):
            raise RecipeError("bad_on_hand", f"on_hand[{k!r}] must be a finite number >= 0 (got {v!r})", 422)
        if isinstance(v, (int, float)):
            try:
                fv = float(v)
            except (ValueError, TypeError, OverflowError) as e:
                raise RecipeError("bad_on_hand", f"on_hand[{k!r}] must be a finite number >= 0", 422) from e
        elif isinstance(v, str):
            sv = v.strip()
            if not sv or _has_ctrl(v):
                raise RecipeError("bad_on_hand", f"on_hand[{k!r}] must be a finite number >= 0 (got {v!r})", 422)
            try:
                fv = float(sv)
            except (ValueError, OverflowError) as e:
                raise RecipeError("bad_on_hand", f"on_hand[{k!r}] must be a finite number >= 0 (got {v!r})", 422) from e
        else:
            raise RecipeError(
                "bad_on_hand",
                f"on_hand[{k!r}] must be a finite number >= 0 (got {type(v).__name__})",
                422,
            )
        if not math.isfinite(fv) or fv < 0:
            raise RecipeError("bad_on_hand", f"on_hand[{k!r}] must be a finite number >= 0 (got {v!r})", 422)
        total_v = cleaned.get(key, 0.0) + fv
        if not math.isfinite(total_v):
            raise RecipeError("bad_on_hand", f"on_hand[{k!r}] overflows finite float range", 422)
        cleaned[key] = total_v
    return cleaned


# ------------------------------------------------------------------ data access
def list_recipes() -> list[dict[str, Any]]:
    """Every dish with its per-cover ingredients (illustrative quantities, not Valio figures)."""
    return [{"id": d["id"], "name": d["en"], "name_fi": d.get("fi"), "aliases": d["aliases"],
             "ingredients": [{"query": i["query"], "per_portion": i["per_cover"], "unit": i["unit"]}
                             for i in d["ingredients"]],
             "note": DATA_NOTE} for d in DISHES]


def get_recipe(recipe_id: str) -> Optional[dict[str, Any]]:
    clean = _clean_text_arg(recipe_id, field="recipe")
    rid = (clean or "").strip().lower()
    if not rid:
        return None
    for d in DISHES:
        if d["id"] == rid or rid in [a.lower() for a in d["aliases"]] or d["en"].lower() == rid:
            return d
    return None


def match_dishes(text: str) -> list[tuple[dict[str, Any], Optional[int]]]:
    """Momin's `_dishes_in`: dishes named in free text, with an optional per-dish count ("40 lasagne")."""
    clean = _clean_text_arg(text, field="text")
    t = (clean or "").lower()
    if not t.strip():
        return []
    found = []
    for d in DISHES:
        for a in sorted(d["aliases"], key=len, reverse=True):
            pat = r"(?:" + _NUM_TOKEN + r"\s*(?:x\s*|portions?\s+of\s*|annosta\s*)?)?\b" + re.escape(a)
            m = re.search(pat, t)
            if m:
                raw_n = m.group(1)
                if raw_n is None:
                    mx = re.search(r"\b" + re.escape(a) + r"\s*x\s*" + _POST_X_NUM_TOKEN, t)
                    if mx:
                        raw_n = mx.group(1)
                found.append((d, _parse_portion_token(raw_n) if raw_n is not None else None))
                break
    return found


def default_portions(text: str) -> Optional[int]:
    """Momin's `_default_covers`: "for 120", "120 guests", "120 annosta"."""
    clean = _clean_text_arg(text, field="text")
    t = (clean or "").lower()
    if not t.strip():
        return None
    first: Optional[int] = None
    for m in re.finditer(_NUM_TOKEN + r"\s*" + _COVERS, t):
        val = _parse_portion_token(m.group(1))
        if first is None:
            first = val
    for m in re.finditer(r"\bfor\s+" + _NUM_TOKEN, t):
        val = _parse_portion_token(m.group(1))
        if first is None:
            first = val
    return first


# ------------------------------------------------------------------ swaps
_FALLBACK_ALIASES: dict[str, tuple[str, ...]] = {
    "dairy": ("milk",), "cow's milk": ("milk",), "cows milk": ("milk",), "cow milk": ("milk",),
    "maito": ("milk",), "mjölk": ("milk",), "am": ("milk",), "ml": ("milk",),
    "nut": ("nuts",), "tree nut": ("nuts",), "tree nuts": ("nuts",), "tree_nuts": ("nuts",),
    "pähkinä": ("nuts",), "pähkinät": ("nuts",), "nöt": ("nuts",), "nötter": ("nuts",), "an": ("nuts",),
    "wheat": ("gluten",), "rye": ("gluten",), "barley": ("gluten",), "spelt": ("gluten",),
    "vehnä": ("gluten",), "vete": ("gluten",), "ruis": ("gluten",), "råg": ("gluten",),
    "gluteeni": ("gluten",), "aw": ("gluten",), "uw": ("gluten",), "nr": ("gluten",),
}


def _normalize_excluded(excluded: Any) -> list[str]:
    items = _validate_excluded_allergens(excluded)
    try:
        from aimo.catalog.allergens import normalize_allergens
        return normalize_allergens(items)
    except Exception:
        out: list[str] = []
        for x in items:
            s = re.sub(r"\s+", " ", str(x or "").replace("\u2019", "'")).strip().lower()
            if not s:
                continue
            for g in _FALLBACK_ALIASES.get(s, (s,)):
                if g not in out:
                    out.append(g)
        return out


def _dietary(excluded: list[str], text: str, lactose_free: bool, gluten_free: bool) -> dict[str, bool]:
    if not isinstance(lactose_free, bool) or not isinstance(gluten_free, bool):
        raise RecipeError("bad_flag", "lactose_free and gluten_free must be booleans", 422)
    clean_t = (_clean_text_arg(text, field="text") or "").lower()
    norm = set(_normalize_excluded(excluded))
    return {
        "milk": "milk" in norm,
        "lactose": lactose_free or "milk" in norm or bool(re.search(r"lactose[- ]?free|laktoositon", clean_t)),
        "gluten": gluten_free or "gluten" in norm or bool(re.search(r"gluten[- ]?free|gluteeniton", clean_t)),
    }


def _swap_rule(ingredient: str) -> Optional[dict[str, Any]]:
    if isinstance(ingredient, bool) or not isinstance(ingredient, str) or _has_text_ctrl(ingredient):
        return None
    q = ingredient.lower().strip()
    if not q:
        return None
    for r in SWAPS:
        if any(q == m or q.endswith(" " + m) for m in r["match"]):
            return r
    return None


def choose_swap(ingredient: str, diet: dict[str, bool]) -> Optional[dict[str, Any]]:
    """Momin's `recipe_swaps` picked options[0]. We keep his order but, for a milk ALLERGY, only accept an
    option his data marks "dairy free" (lactose-free cream/milk/butter still contain milk protein)."""
    if not isinstance(diet, dict):
        return None
    r = _swap_rule(ingredient)
    if not r or not diet.get(r["trigger"]):
        return None
    opts = list(r["options"])
    if r["trigger"] == "lactose" and diet.get("milk"):
        opts = [o for o in opts if "dairy free" in o.get("why", "").lower()]
        if not opts:
            return {"rule": r["id"], "query": None, "ratio": 1.0,
                    "why": "no dairy-free swap in the recipe data; planned with milk excluded, a human must confirm"}
    o = opts[0]
    query = o["query"]
    q = ingredient.lower().strip()
    term = next((m for m in sorted(r["match"], key=len) if q == m or q.endswith(" " + m)), q)
    qualifier = q[: len(q) - len(term)].strip()
    if qualifier and query.startswith("lactose-free "):
        query = f"lactose-free {qualifier} {query[len('lactose-free '):]}"
    return {"rule": r["id"], "query": query, "ratio": float(o.get("ratio", 1.0)), "why": o.get("why", "")}


# ------------------------------------------------------------------ dates and policy
def _today() -> date:
    try:
        from aimo.buyer.need import now_helsinki
        return now_helsinki().date()
    except Exception:  # noqa: BLE001
        return date.today()


def resolve_delivery(delivery_by: Any, text: str = "") -> date:
    clean_text = _clean_text_arg(text, field="text") or ""
    if delivery_by is not None:
        if isinstance(delivery_by, bool):
            raise RecipeError("bad_delivery_date", f"delivery_by must be a date or date string (got {delivery_by!r})", 422)
        if isinstance(delivery_by, datetime):
            return delivery_by.date()
        if isinstance(delivery_by, date):
            return delivery_by
        if not isinstance(delivery_by, str):
            raise RecipeError(
                "bad_delivery_date",
                f"delivery_by must be a date or date string (got {type(delivery_by).__name__})",
                422,
            )
        if _has_ctrl(delivery_by) or len(delivery_by) > MAX_DELIVERY_LEN:
            raise RecipeError(
                "bad_delivery_date",
                "delivery_by contains invalid control characters or exceeds maximum length",
                422,
            )
        s = delivery_by.strip()
        if s:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                try:
                    return date.fromisoformat(s)
                except ValueError as e:
                    raise RecipeError("bad_delivery_date", f"invalid delivery_by date {delivery_by!r}", 422) from e
            if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
                if "/" in s[:10]:
                    raise RecipeError(
                        "bad_delivery_date",
                        f"delivery_by must use YYYY-MM-DD format (got {delivery_by!r})",
                        422,
                    )
                try:
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
                except ValueError as e:
                    raise RecipeError("bad_delivery_date", f"invalid delivery_by date {delivery_by!r}", 422) from e
            try:
                from aimo.buyer.need import resolve_date
            except Exception:
                resolve_date = None
            if resolve_date is not None:
                try:
                    r = resolve_date(s)
                except ValueError as e:
                    raise RecipeError("bad_delivery_date", f"invalid delivery_by date {delivery_by!r}", 422) from e
                if r:
                    return r[0]
            raise RecipeError(
                "bad_delivery_date",
                f"unrecognized delivery_by {delivery_by!r}; expected YYYY-MM-DD or weekday",
                422,
            )
    if clean_text:
        try:
            from aimo.buyer.need import resolve_date
            r = resolve_date(clean_text)
        except ValueError as e:
            raise RecipeError("bad_delivery_date", "invalid delivery date in text", 422) from e
        except Exception:
            r = None
        if r:
            return r[0]
    return _today() + timedelta(days=2)


def policy_exclusions(customer_id: str) -> list[str]:
    if isinstance(customer_id, bool) or not isinstance(customer_id, str) or _has_ctrl(customer_id):
        return []
    cid = customer_id.strip()
    if not cid or len(cid) > MAX_CUSTOMER_ID_LEN or "/" in cid or "\\" in cid or ".." in cid:
        return []
    try:
        from aimo.catalog.allergens import effective_excluded_allergens, normalize_allergens
        excl = list(effective_excluded_allergens(cid))
        if excl:
            return excl
        from aimo.gateway.verify import policy_excluded_allergens
        return normalize_allergens(policy_excluded_allergens(cid))
    except Exception:
        try:
            from aimo.gateway.verify import policy_excluded_allergens
            return list(policy_excluded_allergens(cid))
        except Exception:
            return []


def house_lactose_free(customer_id: str) -> bool:
    """True when the customer's usual products (trust policy preferred_products) include a FREE_FROM_LACTOSE
    product: the gateway then verifies any same-kind line (e.g. milk) against that usual product and requires
    the claim, so the recipe must plan lactose-free dairy (Hotel Aurora's house milk is lactose-free)."""
    if isinstance(customer_id, bool) or not isinstance(customer_id, str) or _has_ctrl(customer_id):
        return False
    cid = customer_id.strip()
    if not cid or len(cid) > MAX_CUSTOMER_ID_LEN or "/" in cid or "\\" in cid or ".." in cid:
        return False
    try:
        from aimo.catalog import get_product
        from aimo.gateway.verify import usual_products
        for pid in usual_products(cid):
            p = get_product(pid)
            if p is not None and "FREE_FROM_LACTOSE" in (p.claims or []):
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ needs
@dataclass
class IngredientNeed:
    dish: str
    ingredient: str                 # recipe ingredient as written ("cooking cream")
    query: str                      # what we search for (after a swap)
    quantity: float
    unit: str
    need: Need
    swap: Optional[dict[str, Any]] = None


@dataclass
class RecipeNeeds:
    dishes: list[dict[str, Any]]
    portions: dict[str, int]
    customer_id: str
    delivery_by: date
    excluded_allergens: list[str]
    items: list[IngredientNeed] = field(default_factory=list)


def recipe_needs(recipe: Optional[str] = None, portions: Optional[int] = None, delivery_by: Any = None,
                 customer_id: str = "hotel-aurora", *, text: str = "", excluded_allergens: Optional[list[str]] = None,
                 lactose_free: bool = False, gluten_free: bool = False,
                 on_hand: Optional[dict[str, float]] = None) -> RecipeNeeds:
    """Scale a dish (by id/alias) or every dish named in `text` to Needs, one per ingredient.

    Quantities: per_portion x portions, g -> KG and ml -> L, rounded UP to 0.01; ingredients shared by
    several dishes are summed (Momin's menu_to_need). Allergen exclusions = the customer's policy +
    `excluded_allergens` + allergens named in the text; every Need carries them so the catalog never
    plans an unsafe product (unknown is never safe).
    """
    cid = _validate_customer_id(customer_id)
    clean_recipe = _clean_text_arg(recipe, field="recipe")
    clean_text = _clean_text_arg(text, field="text") or ""
    if not isinstance(lactose_free, bool):
        raise RecipeError("bad_flag", "lactose_free must be a boolean", 422)
    if not isinstance(gluten_free, bool):
        raise RecipeError("bad_flag", "gluten_free must be a boolean", 422)
    validated_portions = _validate_portions(portions)
    excl = _validate_excluded_allergens(excluded_allergens)
    clean_on_hand = _validate_on_hand(on_hand)
    when = resolve_delivery(delivery_by, clean_text)
    text_default = default_portions(clean_text)
    recipe_default = default_portions(clean_recipe) if clean_recipe and clean_recipe.strip() else None

    if clean_recipe and clean_recipe.strip():
        d = get_recipe(clean_recipe)
        if d is None:
            found = match_dishes(clean_recipe)
            if not found:
                raise RecipeError("recipe_not_found", f"unknown recipe {recipe!r}; see GET /v1/recipes", 404)
            dishes = found
        else:
            dishes = [(d, None)]
    else:
        dishes = match_dishes(clean_text)
        if not dishes:
            raise RecipeError("recipe_not_found", "no known dish in the text; see GET /v1/recipes", 404)
    if validated_portions is not None:
        default = validated_portions
    elif text_default is not None:
        default = text_default
    else:
        default = recipe_default
    try:
        from aimo.buyer.need import detect_allergens
        excl = [*excl, *detect_allergens(clean_text)]
    except Exception:
        pass
    excl = _normalize_excluded([*policy_exclusions(cid), *excl])
    diet = _dietary(excl, clean_text, lactose_free or house_lactose_free(cid), gluten_free)

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    used: dict[str, int] = {}
    for d, n in dishes:
        covers = n if n is not None else default
        if covers is None or covers <= 0:
            raise RecipeError("portions_required", f"how many portions of {d['en']}? pass portions")
        if covers > MAX_PORTIONS:
            raise RecipeError("bad_portions", f"portions must be between 1 and {MAX_PORTIONS} (got {covers})", 422)
        used[d["id"]] = int(covers)
        for ing in d["ingredients"]:
            q = ing["query"]
            if not isinstance(q, str) or not q.strip() or _has_ctrl(q) or len(q) > 500:
                raise RecipeError("bad_recipe", f"invalid ingredient query {q!r}", 422)
            per_cover = float(ing["per_cover"])
            if not math.isfinite(per_cover) or per_cover <= 0:
                raise RecipeError("absurd_quantity", f"invalid per_cover for {q!r}", 422)
            amount = per_cover * covers
            unit = ing["unit"]
            sw = choose_swap(q, diet)
            if sw and sw.get("query"):
                ratio = float(sw["ratio"])
                if not math.isfinite(ratio) or ratio <= 0:
                    raise RecipeError("absurd_quantity", f"invalid swap ratio for {q!r}", 422)
                amount *= ratio
            if not math.isfinite(amount):
                raise RecipeError("absurd_quantity", f"scaled quantity for {q!r} overflows finite float range", 422)
            query = sw["query"] if sw and sw.get("query") else q
            key = (query, unit)
            t = totals.setdefault(key, {"amount": 0.0, "ingredient": q, "dishes": [], "swap": sw})
            t["amount"] += amount
            if not math.isfinite(t["amount"]):
                raise RecipeError("absurd_quantity", f"scaled quantity for {query!r} overflows finite float range", 422)
            t["dishes"].append(d["id"])
    items: list[IngredientNeed] = []
    for (query, unit), t in totals.items():
        if unit not in _UNITS:
            raise RecipeError("bad_recipe", f"unsupported ingredient unit {unit!r}", 422)
        out_unit, div = _UNITS[unit]
        raw_qty = t["amount"] / div - clean_on_hand.get(query.strip().lower(), 0.0)
        if not math.isfinite(raw_qty):
            raise RecipeError("absurd_quantity", f"scaled quantity for {query!r} is not finite", 422)
        if raw_qty <= 0:
            continue
        if raw_qty > MAX_NEED_QTY:
            raise RecipeError(
                "absurd_quantity",
                f"scaled quantity for {query!r} exceeds maximum 1,000,000 {out_unit}",
                422,
            )
        qty = math.ceil(raw_qty * 100 - 1e-9) / 100
        if not math.isfinite(qty) or qty <= 0 or qty > MAX_NEED_QTY:
            raise RecipeError(
                "absurd_quantity",
                f"scaled quantity for {query!r} exceeds maximum 1,000,000 {out_unit}",
                422,
            )
        claims = ["FREE_FROM_LACTOSE"] if (t["swap"] and "lactose-free" in (t["swap"].get("query") or "")) else []
        need = Need(
            raw_text=clean_text or f"{t['ingredient']} for {', '.join(t['dishes'])}",
            customer_id=cid,
            product_query=query,
            required_claims=claims,
            excluded_allergens=excl,
            quantity=qty,
            unit=out_unit,
            delivery_by=when,
        )
        items.append(
            IngredientNeed(
                dish=",".join(dict.fromkeys(t["dishes"])),
                ingredient=t["ingredient"],
                query=query,
                quantity=qty,
                unit=out_unit,
                need=need,
                swap=t["swap"],
            )
        )
    return RecipeNeeds(
        dishes=[d for d, _ in dishes],
        portions=used,
        customer_id=cid,
        delivery_by=when,
        excluded_allergens=excl,
        items=items,
    )


def _batched_offers(needs: list[Need], plan_fn: Callable, offers_fn: Optional[Callable]) -> tuple[dict[str, Offer], dict]:
    """Pass 1 runs the planner with a recording offers_fn (the candidate shortlist does not depend on
    offers), then ONE price + availability call for the union of all shortlists."""
    wanted: list[str] = []

    def record(ids, requested_date, customer_id=None):
        del requested_date, customer_id
        wanted.extend(str(i) for i in ids)
        return {}

    for n in needs:
        try:
            plan_fn(n, offers_fn=record)
        except FileNotFoundError as e:
            from aimo.catalog import CATALOG_MISSING_MESSAGE
            raise RecipeError("catalog_missing", CATALOG_MISSING_MESSAGE, 503) from e
        except Exception as e:
            log.warning("recipe pass-1 plan failed for %r: %s", n.product_query, e)
    ids = list(dict.fromkeys(wanted))
    if not ids:
        return {}, {}
    if offers_fn is not None:
        raw_offers = offers_fn(ids, needs[0].delivery_by, customer_id=needs[0].customer_id)
        try:
            mapped = dict(raw_offers or {})
        except (TypeError, ValueError) as e:
            raise RecipeError("bad_offers", "offers_fn must return a mapping of product_id to Offer", 422) from e
        return mapped, {"source": "offers_fn"}
    from aimo.backend.client import get_offers_with_meta
    offers, meta = get_offers_with_meta(ids, needs[0].delivery_by, needs[0].customer_id)
    return offers, meta


def _plan_summary(plan: Plan, include_candidates: bool) -> dict[str, Any]:
    d = plan.model_dump(mode="json")
    if not include_candidates:
        ok = [c for c in d["candidates"] if c["tier"] != "rejected"][:3]
        d["candidates"] = [
            {
                "product_id": c["product"]["id"],
                "name": c["product"]["name"],
                "tier": c["tier"],
                "unit_price": (c.get("offer") or {}).get("unit_price"),
                "reasons": list(dict.fromkeys([*c["reasons"][:2], *(r for r in c["reasons"] if "prompt-injection screen" in r.lower())])),
                "flags": c["flags"],
                "injection_screen": c.get("injection_screen"),
            }
            for c in ok
        ]
        d["candidates_rejected"] = sum(1 for c in plan.candidates if c.tier == "rejected")
    return d


def _line_blocks_allergens(
    ln: Any,
    row: Optional[dict[str, Any]],
    excl: list[str],
    cand_flags: Iterable[str] = (),
) -> bool:
    excl_norm = _normalize_excluded(excl)
    if not excl_norm:
        return False
    excl_set = set(excl_norm)
    flags = [*(getattr(ln, "flags", None) or []), *cand_flags]
    if "allergen_unknown" in flags:
        return True
    for f in flags:
        if isinstance(f, str) and f.startswith(("contains:", "may_contain:")):
            if set(_normalize_excluded([f.split(":", 1)[1]])) & excl_set:
                return True
    if row is not None:
        try:
            from aimo.catalog.allergens import blocking
            if blocking(row.get("allergens") or {}, excl_norm, allergen_source=row.get("allergen_source")):
                return True
        except Exception:
            amap = row.get("allergens") or {}
            smap = row.get("allergen_source") or {}
            for g in excl_norm:
                st = amap.get(g, "unknown")
                st_val = st.value if hasattr(st, "value") else st
                if st_val != "free" or str(smap.get(g) or "").startswith("inferred"):
                    return True
    return False


def _guard_plan_allergens(plan: Plan, need: Need, excluded: list[str]) -> Plan:
    excl = _normalize_excluded([*excluded, *(need.excluded_allergens or [])])
    if not excl or not plan.lines:
        return plan
    rows: dict[str, dict[str, Any]] = {}
    try:
        from aimo.catalog.textsearch import get_rows
        rows = get_rows([ln.product_id for ln in plan.lines])
    except Exception:
        rows = {}
    cand_flags_by_id = {c.product.id: list(c.flags or []) for c in (plan.candidates or [])}
    safe_lines = []
    removed_ids = []
    for ln in plan.lines:
        if _line_blocks_allergens(ln, rows.get(ln.product_id), excl, cand_flags_by_id.get(ln.product_id, ())):
            removed_ids.append(ln.product_id)
        else:
            safe_lines.append(ln)
    if not removed_ids:
        return plan
    covered = round(sum(float(ln.covered_amount) for ln in safe_lines), 4)
    total_net = round(sum(float(ln.line_total) for ln in safe_lines), 2)
    shortfall = round(max(float(plan.shortfall), max(0.0, float(need.quantity) - covered)), 4)
    note = (
        f"Filtered unsafe allergen line(s) {', '.join(removed_ids)} "
        f"for excluded allergens {', '.join(excl)}."
    )
    explanation = f"{plan.explanation} {note}".strip() if plan.explanation else note
    return plan.model_copy(
        update={
            "lines": safe_lines,
            "covered_amount": covered,
            "total_net": total_net,
            "shortfall": shortfall,
            "explanation": explanation,
        }
    )


def plan_recipe(recipe: Optional[str] = None, portions: Optional[int] = None, delivery_by: Any = None,
                customer_id: str = "hotel-aurora", *, text: str = "", excluded_allergens: Optional[list[str]] = None,
                lactose_free: bool = False, gluten_free: bool = False, on_hand: Optional[dict[str, float]] = None,
                include_candidates: bool = False, offers_fn: Optional[Callable] = None,
                plan_fn: Optional[Callable] = None) -> dict[str, Any]:
    """recipe (id/alias) or free text ("Lasagne for 120 on Friday") -> per-ingredient Needs and Plans,
    plus `quote_request` ready for POST /v1/quotes (or the MCP tool create_quote)."""
    if not isinstance(include_candidates, bool):
        raise RecipeError("bad_flag", "include_candidates must be a boolean", 422)
    if offers_fn is not None and not callable(offers_fn):
        raise RecipeError("bad_offers_fn", "offers_fn must be callable", 422)
    if plan_fn is not None and not callable(plan_fn):
        raise RecipeError("bad_plan_fn", "plan_fn must be callable", 422)
    rn = recipe_needs(recipe, portions, delivery_by, customer_id, text=text, excluded_allergens=excluded_allergens,
                      lactose_free=lactose_free, gluten_free=gluten_free, on_hand=on_hand)
    cid = rn.customer_id
    if plan_fn is None:
        from aimo.catalog import plan_need as effective_plan_fn
    else:
        effective_plan_fn = plan_fn
    needs = [it.need for it in rn.items]
    offers, meta = _batched_offers(needs, effective_plan_fn, offers_fn)

    cats: dict[str, str] = {}
    names: dict[str, str] = {}
    try:
        from aimo.catalog.textsearch import get_rows
        for pid, r in get_rows(list(offers)).items():
            cats[pid] = str(r.get("category") or "")
            names[pid] = " ".join(str(r.get(k) or "") for k in ("name", "name_en", "name_fi")).lower()
    except Exception:
        pass

    def from_batch_for(query: str):
        hints = CATEGORY_HINTS.get(query.lower())
        bad = NAME_EXCLUDE.get(query.lower(), [])

        def ok(i: str) -> bool:
            if hints and cats and not any(cats.get(i, "").startswith(h) for h in hints):
                return False
            return not any(w in names.get(i, "") for w in bad)

        def fn(ids, requested_date, customer_id=None):
            del requested_date, customer_id
            return {i: offers[i] for i in ids if i in offers and ok(i)}
        return fn

    ingredients, uncovered, swaps = [], [], []
    qty_by: dict[str, float] = {}
    total = 0.0
    for it in rn.items:
        try:
            plan = effective_plan_fn(it.need, offers_fn=from_batch_for(it.query))
        except FileNotFoundError as e:
            from aimo.catalog import CATALOG_MISSING_MESSAGE
            raise RecipeError("catalog_missing", CATALOG_MISSING_MESSAGE, 503) from e
        except ValueError as e:
            raise RecipeError("bad_recipe_need", str(e), 422) from e
        if not isinstance(plan, Plan):
            raise RecipeError("bad_plan", "plan_fn must return a Plan instance", 422)
        if rn.excluded_allergens:
            plan = _guard_plan_allergens(plan, it.need, rn.excluded_allergens)
        for ln in plan.lines:
            qty_by[ln.product_id] = qty_by.get(ln.product_id, 0.0) + ln.qty_sales_units
        total += plan.total_net
        if it.swap:
            swaps.append({"ingredient": it.ingredient, "to": it.swap.get("query"), "ratio": it.swap.get("ratio"),
                          "why": it.swap.get("why"), "rule": it.swap.get("rule")})
        if plan.shortfall > 1e-9 or not plan.lines:
            uncovered.append({"ingredient": it.ingredient, "query": it.query, "shortfall": plan.shortfall,
                              "unit": it.unit, "why": plan.explanation})
        ingredients.append({"dish": it.dish, "ingredient": it.ingredient, "query": it.query, "quantity": it.quantity,
                            "unit": it.unit, "swap": it.swap, "need": it.need.model_dump(mode="json"),
                            "plan": _plan_summary(plan, include_candidates)})
    lines = [{"product_id": pid, "qty_sales_units": q} for pid, q in qty_by.items()]
    names = ", ".join(f"{d['en']} x{rn.portions[d['id']]}" for d in rn.dishes)
    covered = len(rn.items) - len(uncovered)
    injection_flagged_lines = [
        {"ingredient": ing["ingredient"], "product_id": ln["product_id"], "product_name": ln["product_name"],
         "injection_screen": ln.get("injection_screen"), "flags": ln["flags"]}
        for ing in ingredients
        for ln in ing["plan"]["lines"]
        if (ln.get("injection_screen") or {}).get("suspicious")
        or "supplier_text_injection_suspected" in (ln.get("flags") or [])
    ]
    flagged_pids = list(dict.fromkeys(ln["product_id"] for ln in injection_flagged_lines))
    summary = (f"{names} for {cid}, delivery by {rn.delivery_by:%a %d %b}: {len(rn.items)} ingredient(s), "
               f"{covered} fully covered, {len(lines)} product line(s), est. €{total:,.2f} net"
               + (f"; excluded allergens {', '.join(rn.excluded_allergens)}" if rn.excluded_allergens else "")
               + (f"; swaps: " + "; ".join(f"{s['ingredient']} -> {s['to'] or 'none'}" for s in swaps) if swaps else "")
               + (f"; NOT covered: " + ", ".join(u["ingredient"] for u in uncovered) if uncovered else "")
               + (f"; prompt-injection screen flagged supplier text on {', '.join(flagged_pids)} (human approval required)"
                  if flagged_pids else "") + ".")
    return {
        "recipe": {"ids": [d["id"] for d in rn.dishes], "names": [d["en"] for d in rn.dishes], "portions": rn.portions},
        "customer_id": cid,
        "delivery_by": rn.delivery_by.isoformat(),
        "excluded_allergens": rn.excluded_allergens,
        "swaps": swaps,
        "ingredients": ingredients,
        "injection_flagged_lines": injection_flagged_lines,
        "uncovered": uncovered,
        "total_net_estimate": round(total, 2),
        "summary": summary,
        "quote_request": {"customer_id": cid, "delivery_date": rn.delivery_by.isoformat(), "lines": lines},
        "provenance": {"source": SOURCE, "recipe_data_note": DATA_NOTE,
                       "credit": "Recipe data and swap logic by Momin (junction26-valio)",
                       "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "offers": {k: meta.get(k) for k in ("source", "served_from", "fetched_at", "valid_for_s", "faults")
                                  if k in meta},
                       "offer_ids_fetched": len(offers)},
    }
