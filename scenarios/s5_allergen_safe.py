"""s5 allergen-safe (after Momin's s5_allergen_safe), for our school (milk + nuts excluded) and hospital
(gluten excluded): plans only contain products whose structured allergen data says FREE; a recipe swaps to the
dairy-free option; a forced unsafe order is rejected by policy with a plain-language reason."""
from __future__ import annotations

from scenarios._common import Client, Run, allergen_status, main, next_delivery_day


def unsafe(r: Run, query: str, allergen: str, unit: str, day) -> str:
    """An ORDERABLE product whose structured data says it contains `allergen` (found via the hotel's plan)."""
    c = Client(r.base, "hotel-aurora")
    plan = c.plan(query, 10, unit, day)
    for cand in plan["candidates"]:
        o = cand.get("offer") or {}
        if cand["tier"] != "rejected" and o.get("available") and \
                allergen_status(c, cand["product"]["id"], [allergen])[allergen] == "contains":
            return cand["product"]["id"]
    raise RuntimeError(f"no orderable product containing {allergen} found for {query!r}")


def run(r: Run) -> None:
    day = next_delivery_day()
    s = Client(r.base, "school-tampere")
    plan = s.plan("oat drink", 100, "L", day, excluded=["milk", "nuts"])
    r.check("school plan has lines", plan["lines"], plan["explanation"][:200])
    sts = {l["product_id"]: allergen_status(s, l["product_id"], ["milk", "nuts"]) for l in plan["lines"]}
    leaks = {pid: st for pid, st in sts.items() if any(v != "free" for v in st.values())}
    r.check("school: every planned product is structured FREE of milk and nuts", not leaks, leaks)
    rp = s.j("POST", "/v1/recipes/plan", json={"recipe_id": "pancakes", "portions": 650, "delivery_by": str(day)})
    sw = {x["ingredient"]: x["to"] for x in rp["swaps"]}
    r.check("school recipe: milk swapped to a dairy-free option (not lactose-free milk)", sw.get("milk") == "oat drink", sw)
    r.check("school recipe carries policy exclusions", {"milk", "nuts"} <= set(rp["excluded_allergens"]), rp["excluded_allergens"])
    rleaks = [l["product_id"] for i in rp["ingredients"] for l in i["plan"]["lines"]
              if any(v != "free" for v in allergen_status(s, l["product_id"], ["milk", "nuts"]).values())]
    r.check("school recipe: no planned product contains or may contain milk/nuts (or is unknown)", not rleaks, rleaks)
    bad = s.quote_ok([{"product_id": unsafe(r, "milk", "milk", "L", day), "qty_sales_units": 1}], day)
    o = s.order(bad).json()
    r.check("school: forced milk order rejected", o["status"] == "rejected" and o["decision"]["outcome"] == "reject", o["status"])
    hit = [h for h in o["decision"]["hits"] if h["outcome"] == "reject" and "milk" in h["message"].lower()]
    r.check("reject reason names milk in plain language", hit, [h["message"] for h in o["decision"]["hits"]][:3])
    h = Client(r.base, "hospital-meilahti")
    hp = h.plan("gluten free bread", 20, "KG", day, excluded=["gluten"])
    hleaks = [l["product_id"] for l in hp["lines"] if allergen_status(h, l["product_id"], ["gluten"])["gluten"] != "free"]
    r.check("hospital: bread plan only structured gluten-free", hp["lines"] and not hleaks, hleaks or hp["explanation"][:150])
    hb = h.quote_ok([{"product_id": unsafe(r, "rye bread", "gluten", "KG", day), "qty_sales_units": 1}], day)
    ho = h.order(hb).json()
    r.check("hospital: forced gluten order rejected", ho["status"] == "rejected", ho["status"])
    gh = [x["message"] for x in ho["decision"]["hits"] if x["outcome"] == "reject" and "gluten" in x["message"].lower()]
    r.check("hospital reject reason names gluten", gh, [x["message"][:80] for x in ho["decision"]["hits"]][:4])
    if gh:
        r.say("hospital reject: " + gh[0][:160])


if __name__ == "__main__":
    main("s5_allergen_safe", run)
