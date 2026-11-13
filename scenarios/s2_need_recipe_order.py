from _common import *

base = parse_base()
r = Run("s2_need_recipe_order", base)


def go():
    c = Client(base, "hotel-aurora")
    day = next_monday()
    r.say("need -> recipe -> order: lasagne for 40 portions")
    items = c.parse(f"We need to cook lasagne for 40 portions by {day}", "hotel-aurora")["items"]
    r.check("dish became several ingredient lines", len(items) >= 3, f"{len(items)} lines")
    r.check("every line has quantity and unit", all(i["quantity"] > 0 and i["unit"] for i in items))
    names = " ".join(i["query"].lower() for i in items)
    r.check("lasagne basics present", any(w in names for w in ("pasta", "lasagne", "lasagna")) and any(w in names for w in ("beef", "meat", "mince")), names[:150])
    need = c.parse(f"We need to cook lasagne for 40 portions by {day}", "hotel-aurora")
    half = {i["query"]: i["quantity"] for i in c.parse(f"We need to cook lasagne for 20 portions by {day}", "hotel-aurora")["items"]}
    r.check("quantities scale with portions", all(abs(i["quantity"] - 2 * half.get(i["query"], -1)) < 1e-6 * max(1, i["quantity"]) for i in need["items"]))
    offers = c.offers(need)
    got = [it for it in offers["items"] if it["offers"]]
    r.check("offers found for most lines", len(got) >= max(1, len(items) // 2), f"{len(got)}/{len(items)}")
    q = c.quote(picks_from(offers), day)
    r.check("quote has a line per picked ingredient", len(q["lines"]) >= len(got))
    o = c.order(q)
    r.check("order has Decision", o["decision"]["outcome"] in ("auto", "needs_approval"), str(o["decision"]))


r.guard(go)
