"""s2 menu -> recipe -> order (after Momin's s2_need_recipe_order): "Lasagne for 120 on Friday at Hotel Aurora"
becomes per-ingredient Needs, catalog plans with live offers, one multi-line signed quote, then the policy verdict."""
from __future__ import annotations

from scenarios._common import Client, Run, main


def run(r: Run) -> None:
    c = Client(r.base, "hotel-aurora")
    recipes = c.j("GET", "/v1/recipes")
    r.check("recipe list includes lasagne", any(x["id"] == "lasagne" for x in recipes), [x["id"] for x in recipes])
    p = c.j("POST", "/v1/recipes/plan", json={"text": "Lasagne for 120 on Friday at Hotel Aurora"})
    r.say(p["summary"])
    ing = p["ingredients"]
    r.check("dish became >= 5 ingredient lines", len(ing) >= 5, len(ing))
    r.check("portions parsed as 120", p["recipe"]["portions"].get("lasagne") == 120, p["recipe"]["portions"])
    r.check("delivery is a Friday", __import__("datetime").date.fromisoformat(p["delivery_by"]).weekday() == 4, p["delivery_by"])
    names = " ".join(i["query"] for i in ing)
    r.check("lasagne basics present", "lasagne" in names and "beef" in names, names)
    half = c.j("POST", "/v1/recipes/plan", json={"recipe_id": "lasagne", "portions": 60, "delivery_by": p["delivery_by"]})
    hq = {i["query"]: i["quantity"] for i in half["ingredients"]}
    r.check("quantities scale with portions", all(abs(i["quantity"] - 2 * hq[i["query"]]) <= 0.02 for i in ing
                                                  if i["query"] in hq), {i["query"]: (i["quantity"], hq.get(i["query"])) for i in ing})
    covered = [i for i in ing if i["plan"]["lines"]]
    r.check("most ingredients mapped to catalog products", len(covered) >= len(ing) - 2, f"{len(covered)}/{len(ing)}")
    r.check("credit + provenance on the plan", "Momin" in p["provenance"]["credit"] and p["provenance"]["offers"])
    qr = p["quote_request"]
    q = c.quote_ok(qr["lines"], qr["delivery_date"])
    r.check("multi-line signed quote", len(q["lines"]) >= 4 and q["signature"], len(q["lines"]))
    r.say(f"quote {q['quote_id']}: {len(q['lines'])} lines, EUR {q['total_net']}")
    o = c.order(q).json()
    r.check("policy verdict returned", o["decision"]["outcome"] in ("auto_approve", "needs_approval", "reject"))
    r.say(f"verdict {o['decision']['outcome']} ({o['decision'].get('approver_role')}): " +
          " | ".join(h["message"][:90] for h in o["decision"]["hits"] if h["outcome"] != "allow")[:400])
    if o["status"] == "pending_approval":
        d = c.approve(o, q["quote_hash"], True, "lasagne night ok")
        r.check("approved by the procurement manager -> placed", d["status"] == "placed", d["status"])
    else:
        r.check("order placed or rejected with reasons", o["status"] in ("placed", "rejected"), o["status"])


if __name__ == "__main__":
    main("s2_recipe_order", run)
