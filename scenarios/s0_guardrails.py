"""s0 guardrails (after Momin's s0_guardrails): over-limit order needs a named human, approval places it,
a tampered quote hash is refused, an unregistered agent cannot quote, and a human-only approval path."""
from __future__ import annotations

from scenarios._common import Client, Run, main, next_monday


def run(r: Run) -> None:
    c = Client(r.base, "hotel-aurora")
    day = next_monday()
    plan = c.plan("lactose-free milk", 1000, "L", day, claims=["FREE_FROM_LACTOSE"], preferred=["463038"])
    r.check("plan covers 1000 L", plan["shortfall"] == 0 and plan["lines"], f"shortfall {plan['shortfall']}")
    q = c.quote_ok(plan["lines"], day, plan["need"])
    r.say(f"quote {q['quote_id']} EUR {q['total_net']}")
    r.check("quote is over the EUR 500 auto-approve limit", q["total_net"] > 500, q["total_net"])
    bad = c.order(q, quote_hash="0" * 64)
    r.check("tampered quote_hash refused (409)", bad.status_code == 409 and "order_id" not in bad.json(), bad.status_code)
    o = c.order(q).json()
    r.check("over limit -> pending_approval", o["status"] == "pending_approval" and o["decision"]["outcome"] == "needs_approval",
            o["decision"]["outcome"])
    r.check("approver role named", o["decision"].get("approver_role") == "procurement_manager", o["decision"].get("approver_role"))
    r.check("hits are plain language", all(h["message"] for h in o["decision"]["hits"]))
    inbox = c.j("GET", "/v1/approvals", params={"status": "pending", "customer_id": "hotel-aurora"})
    r.check("order is in the approval inbox", any(a["order"]["order_id"] == o["order_id"] for a in inbox))
    agent_try = c.call("POST", f"/v1/approvals/{o['order_id']}/decision",
                       json={"decision": "approve", "approver": "agent:hotel-aurora/buyer-1", "quote_hash": q["quote_hash"]})
    r.check("an agent cannot approve (403)", agent_try.status_code == 403, agent_try.status_code)
    d = c.approve(o, q["quote_hash"], True, "s0 ok")
    r.check("human approval -> placed", d["status"] == "placed" and d["response_code"] in ("AP", "CA"), d["status"])
    mallory = Client(r.base, "hotel-aurora", agent="mallory")
    m = mallory.quote(plan["lines"][:1], day)
    r.check("unregistered agent cannot quote (403 scope_missing)", m.status_code == 403, m.status_code)
    v = c.j("GET", "/v1/audit/verify")
    r.check("audit chain verifies", v.get("ok"), v)


if __name__ == "__main__":
    main("s0_guardrails", run)
