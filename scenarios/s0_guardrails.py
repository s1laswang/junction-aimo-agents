import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

base = None
r = None


def run_scenario(c, r, day=None):
    day = day or next_monday()
    m = c.j("GET", "/v1/mandate")
    r.say(f"mandate per_order_eur={m['per_order_eur']}")
    need = c.parse(f"I need 1000 litres of lactose-free milk by {day}", "hotel-aurora")
    picks = best_picks_covering(c.offers(need)["items"][0])
    q = c.quote(picks, day)
    mult = max(2, int(m["per_order_eur"] // q["total_net"]) + 2) if q["total_net"] > 0 else 2
    big = [{**p, "quantity": p["quantity"] * mult} for p in picks]
    qb = c.quote(big, day)
    r.check("over-limit quote exceeds mandate", qb["total_net"] > m["per_order_eur"], f"{qb['total_net']}")
    o = c.order(qb)
    r.check("over per_order_eur -> needs_approval", o["decision"]["outcome"] == "needs_approval" and o["status"] == "pending_approval", str(o["decision"]))
    r.check("order listed in approvals", any(a["order_id"] == o["order_id"] for a in c.approvals()))
    d = c.decide(o["order_id"], True, m["approvers"][0], "ok")
    r.check("approval -> placed", d["status"] in ("placed", "approved", "confirmed"), d["status"])
    h, p, s = q["jws"].split(".")
    tam = ".".join([h, p[:-2] + ("AA" if p[-2:] != "AA" else "BB"), s])
    resp = c.call("POST", "/v1/orders", json={"quote_id": q["quote_id"], "jws": tam})
    r.check("tampered jws rejected", resp.status_code in TAMPER_STATUS and "order_id" not in resp.json(), str(resp.status_code))


def go():
    b = base or parse_base()
    run = r or Run("s0_guardrails", b)
    run_scenario(Client(b, "hotel-aurora"), run)


if __name__ == "__main__":
    base = parse_base()
    r = Run("s0_guardrails", base)
    r.guard(go)
