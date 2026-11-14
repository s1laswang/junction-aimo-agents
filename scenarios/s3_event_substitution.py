"""s3 event substitution (after Momin's s3_event_substitution): a last-minute event, 400 portions of pasta
carbonara; the planned cream is out of stock (X-Aimo-Faults), the planner substitutes with reasons, the gateway
re-verifies the tier, and the substitution goes to a human before ordering."""
from __future__ import annotations

from scenarios._common import Client, Run, main, next_delivery_day


def run(r: Run) -> None:
    day = next_delivery_day()
    c = Client(r.base, "hotel-aurora")
    p = c.j("POST", "/v1/recipes/plan", json={"recipe_id": "carbonara", "portions": 400, "delivery_by": str(day)})
    r.say(p["summary"])
    r.check("recipe expanded to >= 4 ingredient lines", len(p["ingredients"]) >= 4, len(p["ingredients"]))
    cream = next((i for i in p["ingredients"] if "cream" in i["query"]), None)
    r.check("cream line planned", cream and cream["plan"]["lines"], cream and cream["plan"]["explanation"])
    first = cream["plan"]["lines"][0]["product_id"]
    cf = Client(r.base, "hotel-aurora", faults=f"stockout:{first}")
    p2 = cf.j("POST", "/v1/recipes/plan", json={"recipe_id": "carbonara", "portions": 400, "delivery_by": str(day)})
    cream2 = next(i for i in p2["ingredients"] if "cream" in i["query"])
    sub = [l for l in cream2["plan"]["lines"] if l["product_id"] != first]
    r.check("stock-out on the planned cream -> a substitute is planned", sub and all(l["product_id"] != first
                                                                                     for l in cream2["plan"]["lines"]),
            cream2["plan"]["explanation"][:200])
    r.check("substitute carries reasons", sub and all(l["reasons"] for l in sub))
    r.say(f"{first} out of stock -> {sub[0]['product_id']} {sub[0]['product_name']} ({sub[0]['tier']})" if sub else "no sub")
    q = cf.quote_ok(p2["quote_request"]["lines"], p2["quote_request"]["delivery_date"])
    r.check("quote does not contain the stocked-out product", all(l["product_id"] != first for l in q["lines"]))
    tiers = {l["product_id"]: l["tier"] for l in q["lines"]}
    r.check("gateway verified tiers server-side", q["provenance"].get("line_verification"), tiers)
    o = cf.order(q).json()
    r.say(f"verdict {o['decision']['outcome']}: " + " | ".join(h["message"][:80] for h in o["decision"]["hits"]
                                                                 if h["outcome"] != "allow")[:300])
    r.check("event order is not auto-placed without a human (substitutes / categories / spend)",
            o["status"] in ("pending_approval", "rejected"), o["status"])
    if o["status"] == "pending_approval":
        d = cf.approve(o, q["quote_hash"], True, "event ok")
        r.check("human approval -> placed", d["status"] == "placed", d["status"])
    ev = c.audit(o["order_id"])
    r.check("audit explains every step", ev and all(e["summary"] for e in ev))


if __name__ == "__main__":
    main("s3_event_substitution", run)
