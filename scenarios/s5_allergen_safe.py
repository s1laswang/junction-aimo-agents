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


def _structured_leaks(c: Client, ids: list[str], groups: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for pid in dict.fromkeys(ids):
        p = c.j("GET", f"/v1/products/{pid}")
        st = allergen_status(c, pid, groups)
        src = p.get("allergen_source") or {}
        bad = {g: f"{st[g]}:{src.get(g, 'none')}" for g in groups
               if st[g] != "free" or not str(src.get(g, "")).startswith("structured")}
        if bad:
            out[pid] = bad
    return out


def run(r: Run) -> None:
    day = next_delivery_day()
    s = Client(r.base, "school-tampere")
    plan = s.plan("oat drink", 100, "L", day, excluded=["milk", "nuts"])
    r.check("school plan has lines", plan["lines"], plan["explanation"][:200])
    sts = {l["product_id"]: allergen_status(s, l["product_id"], ["milk", "nuts"]) for l in plan["lines"]}
    leaks = {pid: st for pid, st in sts.items() if any(v != "free" for v in st.values())}
    r.check("school: every planned product is structured FREE of milk and nuts", not leaks, leaks)
    milk_plan = s.plan("milk", 50, "L", day)
    milk_line_leaks = _structured_leaks(s, [l["product_id"] for l in milk_plan["lines"]], ["milk", "nuts"])
    milk_cand_leaks = _structured_leaks(s, [c["product"]["id"] for c in milk_plan["candidates"] if c["tier"] != "rejected"],
                                        ["milk", "nuts"])
    r.check("school milk plan (omitted excluded): zero dairy/nut lines, policy hard-block active",
            {"milk", "nuts"} <= set(milk_plan["need"]["excluded_allergens"])
            and (bool(milk_plan["lines"]) or milk_plan["shortfall"] > 0) and not milk_line_leaks,
            milk_line_leaks or milk_plan["need"]["excluded_allergens"])
    r.check("school milk plan (omitted excluded): every non-rejected candidate structured FREE of milk and nuts",
            not milk_cand_leaks, milk_cand_leaks)
    oat_plan = s.plan("oat drink", 100, "L", day)
    oat_leaks = _structured_leaks(s, [l["product_id"] for l in oat_plan["lines"]], ["milk", "nuts"])
    r.check("school oat drink plan (omitted excluded): has lines and every line structured FREE of milk and nuts",
            bool(oat_plan["lines"]) and {"milk", "nuts"} <= set(oat_plan["need"]["excluded_allergens"]) and not oat_leaks,
            oat_leaks or oat_plan["explanation"][:200])
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
    hbp = h.plan("bread", 20, "KG", day)
    hbp_leaks = _structured_leaks(h, [l["product_id"] for l in hbp["lines"]], ["gluten"])
    hbp_cand_leaks = _structured_leaks(h, [c["product"]["id"] for c in hbp["candidates"] if c["tier"] != "rejected"],
                                       ["gluten"])
    r.check("hospital bread plan (omitted excluded): has lines and every line structured FREE of gluten",
            bool(hbp["lines"]) and "gluten" in hbp["need"]["excluded_allergens"]
            and not hbp_leaks and not hbp_cand_leaks,
            hbp_leaks or hbp_cand_leaks or hbp["explanation"][:150])
    hb = h.quote_ok([{"product_id": unsafe(r, "rye bread", "gluten", "KG", day), "qty_sales_units": 1}], day)
    ho = h.order(hb).json()
    r.check("hospital: forced gluten order rejected", ho["status"] == "rejected", ho["status"])
    gh = [x["message"] for x in ho["decision"]["hits"] if x["outcome"] == "reject" and "gluten" in x["message"].lower()]
    r.check("hospital reject reason names gluten", gh, [x["message"][:80] for x in ho["decision"]["hits"]][:4])
    if gh:
        r.say("hospital reject: " + gh[0][:160])


if __name__ == "__main__":
    main("s5_allergen_safe", run)
