import csv, json, re, os, statistics, sys

D = os.environ.get("AIMO_DATA_DIR", "data")


def cls(p, name):
    for c in p["synkkaData"]["classifications"]:
        if c["name"] == name:
            return c.get("values") or []
    return []


def measure():
    prods = json.load(open(f"{D}/valio_aimo_junction26_product_data.json"))
    ids = {p["id"] for p in prods}
    claims = lambda p: {v["id"] for v in cls(p, "nutritionalClaim")}
    rel = {r["productId"]: float(r["deliveryReliability"]) for r in csv.DictReader(open(f"{D}/valio_aimo_junction26_delivery_reliability.csv"))}
    co2 = {r["productId"]: float(r["AVG kg CO2-eq/kg"]) for r in csv.DictReader(open(f"{D}/valio_aimo_junction26_carbon_footprint.csv"))}
    rv = sorted(rel.values())
    cv = sorted(co2.values())
    q = lambda v, f: v[int(f * (len(v) - 1))]
    return {
        "products": len(prods),
        "deleted": sum(p["deleted"] for p in prods),
        "lactose_free_claim": sum("FREE_FROM_LACTOSE" in claims(p) for p in prods),
        "low_lactose_claim": sum("LOW_LACTOSE" in claims(p) for p in prods),
        "gluten_free_claim": sum("FREE_FROM_GLUTEN" in claims(p) for p in prods),
        "vegan_claim": sum("VEGAN" in claims(p) for p in prods),
        "with_allergen_declared": sum(bool(cls(p, "allergen")) for p in prods),
        "with_free_from_declared": sum(bool(cls(p, "nonAllergen")) for p in prods),
        "with_any_allergen_info": sum(bool(cls(p, "allergen") or cls(p, "nonAllergen")) for p in prods),
        "with_milk_allergen_contains": sum(any(v["id"] == "AM" and v["unit"] == "CONTAINS" for v in cls(p, "allergen")) for p in prods),
        "lactose_free_name_has_milk_or_maito": sum("FREE_FROM_LACTOSE" in claims(p) and any(re.search(r"\bmilk\b|\bmaito\b", n["value"].lower()) for n in p["synkkaData"]["names"] if n["language"] in ("en", "fi")) for p in prods),
        "categories": len({p["category"] for p in prods}),
        "names_en": sum(any(n["language"] == "en" for n in p["synkkaData"]["names"]) for p in prods),
        "reliability_rows": len(rel),
        "reliability_in_catalog": sum(k in ids for k in rel),
        "reliability_min": rv[0],
        "reliability_p10": q(rv, 0.1),
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
