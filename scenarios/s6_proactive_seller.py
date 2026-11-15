"""s6 proactive seller (after Momin's s6_proactive_seller): Valio Aimo's seller agent proposes a cheaper
equivalent to Hotel Aurora. The seller can only propose: it cannot accept, quote or order for the customer.
The hotel's own buyer agent accepts; the hotel's policy decides."""
from __future__ import annotations

from scenarios._common import SELLER, Client, Run, main


def run(r: Run) -> None:
    seller = Client(r.base, "hotel-aurora", agent=SELLER)
    res = seller.call("POST", "/v1/proposals", json={"customer_id": "hotel-aurora"})
    r.check("seller proposal created", res.status_code == 200, res.text[:200])
    p = res.json()
    r.say(f"proposal {p['proposal_id']}: {p['qty_sales_units']:g} x {p['product']['name']} "
          f"(replaces {(p.get('replaces') or {}).get('product_id')}); why: {p['why'][0][:140]}")
    r.check("proposal has a why", p["why"] and p["why"][0])
    r.check("proposal says the seller cannot order", p["seller_can_order"] is False)
    pv = p["policy_preview"]
    r.check("non-binding policy preview from the customer's own policy", pv.get("binding") is False and pv.get("outcome"), pv)
    r.say(f"preview: {pv.get('outcome')} ({pv.get('approver_role')})")
    # The customer's own policy decides. A pack swap the hotel listed as an approved alternative passes on its own
    # under the spend limit; a swap it has NOT listed (another brand) needs a human.
    r.check("preview outcome comes from the customer's policy", pv.get("outcome") in ("auto_approve", "needs_approval"),
            pv.get("outcome"))
    other = seller.call("POST", "/v1/proposals", json={"customer_id": "hotel-aurora", "product_id": "469363",
                                                       "qty_sales_units": 10, "replaces_product_id": "463038",
                                                       "note": "another brand"})
    if other.status_code == 200:
        r.check("a swap the customer has not listed needs a human (preview)",
                other.json()["policy_preview"].get("outcome") == "needs_approval",
                other.json()["policy_preview"].get("outcome"))
    else:
        r.say(f"other-brand proposal not orderable here ({other.status_code}); skipped")
    acc = seller.call("POST", f"/v1/proposals/{p['proposal_id']}/accept")
    r.check("seller cannot accept its own proposal (403 seller_cannot_order)",
            acc.status_code == 403 and acc.json().get("error") == "seller_cannot_order", acc.status_code)
    qres = seller.quote(p["quote_request"]["lines"], p["quote_request"]["delivery_date"])
    r.check("seller cannot create a quote for the customer (403)", qres.status_code == 403, qres.status_code)
    listed = seller.j("GET", "/v1/proposals", params={"customer_id": "hotel-aurora"})
    r.check("proposal listed for the customer", any(x["proposal_id"] == p["proposal_id"] for x in listed))
    buyer = Client(r.base, "hotel-aurora")
    out = buyer.j("POST", f"/v1/proposals/{p['proposal_id']}/accept")
    o = out["order"]
    r.check("buyer accept -> signed quote + policy decision", out["quote"]["signature"] and o["decision"]["outcome"])
    r.check("order belongs to the buyer agent, not the seller", o["agent_id"] == "buyer-1", o["agent_id"])
    r.say(f"buyer accepted: order {o['order_id']} {o['status']} ({o['decision']['outcome']})")
    again = buyer.call("POST", f"/v1/proposals/{p['proposal_id']}/accept")
    r.check("a proposal can be accepted only once (409)", again.status_code == 409, again.status_code)
    ev = buyer.audit(p["proposal_id"])
    r.check("audit: proposal logged as agent:valio-aimo/seller-1",
            any(e["action"] == "proposal.created" and e["actor"] == "agent:valio-aimo/seller-1" for e in ev),
            [(e["actor"], e["action"]) for e in ev])
    r.check("audit: seller's accept attempt refused and logged",
            any(e["action"] == "proposal.accept_refused" for e in ev))
    sp = seller.call("POST", "/v1/proposals", json={"customer_id": "school-tampere", "product_id": "460396",
                                                    "qty_sales_units": 2, "note": "winter promotion"})
    if sp.status_code == 200:
        pv2 = sp.json()["policy_preview"]
        r.check("school: a milk product proposed by the seller previews as reject (policy decides)",
                pv2.get("outcome") == "reject", pv2.get("outcome"))
    else:
        r.check("school proposal of unorderable item refused cleanly", sp.status_code in (404, 409), sp.status_code)


if __name__ == "__main__":
    main("s6_proactive_seller", run)
