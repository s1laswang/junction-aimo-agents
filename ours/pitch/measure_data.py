import csv
import json
import os
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = os.environ.get("AIMO_DATA_DIR", "data")
MILK_RE = re.compile(r"\bmilk\b|\bmaito\b")


def cls(p, name):
    for c in p["synkkaData"]["classifications"]:
        if c["name"] == name:
            return c.get("values") or []
    return []


def quantile(values, fraction):
    return values[int(fraction * (len(values) - 1))]


def _resolve_data_dir(data_dir=None):
    if data_dir is not None:
        return Path(data_dir)
    if "AIMO_DATA_DIR" in os.environ:
        return Path(os.environ["AIMO_DATA_DIR"])
    candidate = Path(D)
    if candidate.exists() or D != "data":
        return candidate
    return ROOT / "data"


def measure(data_dir=None):
    base = _resolve_data_dir(data_dir)
    with open(base / "valio_aimo_junction26_product_data.json", encoding="utf-8") as f:
        prods = json.load(f)
    with open(base / "valio_aimo_junction26_delivery_reliability.csv", newline="", encoding="utf-8") as f:
        rel = {r["productId"]: float(r["deliveryReliability"]) for r in csv.DictReader(f)}
    with open(base / "valio_aimo_junction26_carbon_footprint.csv", newline="", encoding="utf-8") as f:
        co2 = {r["productId"]: float(r["AVG kg CO2-eq/kg"]) for r in csv.DictReader(f)}
    ids = {p["id"] for p in prods}
    product_claims = [{v["id"] for v in cls(p, "nutritionalClaim")} for p in prods]
    allergens = [cls(p, "allergen") for p in prods]
    non_allergens = [cls(p, "nonAllergen") for p in prods]
    rv = sorted(rel.values())
    cv = sorted(co2.values())
    return {
        "products": len(prods),
        "deleted": sum(p["deleted"] for p in prods),
        "lactose_free_claim": sum("FREE_FROM_LACTOSE" in c for c in product_claims),
        "low_lactose_claim": sum("LOW_LACTOSE" in c for c in product_claims),
        "gluten_free_claim": sum("FREE_FROM_GLUTEN" in c for c in product_claims),
        "vegan_claim": sum("VEGAN" in c for c in product_claims),
        "with_allergen_declared": sum(bool(a) for a in allergens),
        "with_free_from_declared": sum(bool(na) for na in non_allergens),
        "with_any_allergen_info": sum(bool(a or na) for a, na in zip(allergens, non_allergens)),
        "with_milk_allergen_contains": sum(
            any(v["id"] == "AM" and v["unit"] == "CONTAINS" for v in a) for a in allergens
        ),
        "lactose_free_name_has_milk_or_maito": sum(
            "FREE_FROM_LACTOSE" in c
            and any(
                MILK_RE.search(n["value"].lower())
                for n in p["synkkaData"]["names"]
                if n["language"] in ("en", "fi")
            )
            for p, c in zip(prods, product_claims)
        ),
        "categories": len({p["category"] for p in prods}),
        "names_en": sum(any(n["language"] == "en" for n in p["synkkaData"]["names"]) for p in prods),
        "reliability_rows": len(rel),
        "reliability_in_catalog": sum(k in ids for k in rel),
        "reliability_min": rv[0],
        "reliability_p10": quantile(rv, 0.1),
        "reliability_median": statistics.median(rv),
        "reliability_mean": round(statistics.mean(rv), 3),
        "reliability_perfect_1": sum(v == 1.0 for v in rv),
        "reliability_below_0.9": sum(v < 0.9 for v in rv),
        "co2_rows": len(co2),
        "co2_in_catalog": sum(k in ids for k in co2),
        "co2_min": cv[0],
        "co2_median": statistics.median(cv),
        "co2_max": cv[-1],
    }


if __name__ == "__main__":
    json.dump(measure(), sys.stdout, indent=1)
    sys.stdout.write("\n")
