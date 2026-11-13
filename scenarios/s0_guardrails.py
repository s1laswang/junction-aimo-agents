from _common import *

base = parse_base()
r = Run("s0_guardrails", base)


def go():
    c = Client(base, "hotel-aurora")
    day = next_monday()
    m = c.j("GET", "/v1/mandate")
    r.say(f"mandate per_order_eur={m['per_order_eur']}")
    need = c.parse(f"I need 1000 litres of lactose-free milk by {day}", "hotel-aurora")
    picks = best_picks_covering(c.offers(need)["items"][0])
    q = c.quote(picks, day)
    big = [{**p, "quantity": p["quantity"] * 1000} for p in picks]
    qb = c.quote(big, day)
    r.check("over-limit quote exceeds mandate", qb["total_net"] > m["per_order_eur"], f"{qb['total_net']}")
    o = c.order(qb)
    r.check("over per_order_eur -> needs_approval", o["decision"]["outcome"] == "needs_approval" and o["status"] == "pending_approval", str(o["decision"]))
    r.check("order listed in approvals", any(a["order_id"] == o["order_id"] for a in c.approvals()))
    d = c.decide(o["order_id"], True, "scenario-human", "ok")
    r.check("approval -> placed", d["status"] in ("placed", "approved", "confirmed"), d["status"])
    h, p, s = q["jws"].split(".")
    tam = ".".join([h, p[:-2] + ("AA" if p[-2:] != "AA" else "BB"), s])
    resp = c.call("POST", "/v1/orders", json={"quote_id": q["quote_id"], "jws": tam})
    r.check("tampered jws rejected", resp.status_code in TAMPER_STATUS and "order_id" not in resp.json(), str(resp.status_code))


r.guard(go)
