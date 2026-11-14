"""s7 standing-order drift (after Momin's s7_drift): volume creep over the seeded 8-week history, price creep
against the first observed live price (shown with an injected price shock), stock-outs, plain-language messages."""
from __future__ import annotations

from scenarios._common import Client, Run, main


def run(r: Run) -> None:
    c = Client(r.base, "hotel-aurora")
    d = c.j("GET", "/v1/drift/hotel-aurora")
    r.check("standing order returned", d["standing_order"]["lines"], d)
    vol = [f for f in d["flags"] if f["kind"] == "volume" and f["product_id"] == "460396"]
    r.check("milk volume creep flagged (+50%, 8 -> 12 cases)", vol and vol[0]["change_pct"] == 50.0, d["flags"])
    if vol:
        r.say(vol[0]["message"])
        r.check("message is plain language with numbers", "50%" in vol[0]["message"] and "8 → 12 cases" in vol[0]["message"])
    r.check("every flag has a message and a route", all(f["message"] and f["route"] in ("auto", "approval") for f in d["flags"]))
    shock = Client(r.base, "hotel-aurora", faults="price_shock:460884:1.12").j("GET", "/v1/drift/hotel-aurora")
    pf = [f for f in shock["flags"] if f["kind"] == "price" and f["product_id"] == "460884"]
    r.check("croissant price creep +12% flagged vs baseline", pf and abs(pf[0]["change_pct"] - 12.0) < 0.6, shock["flags"])
    if pf:
        r.say(pf[0]["message"])
        r.check("<= 10% band rule: +12% routes to approval", pf[0]["route"] == "approval", pf[0]["route"])
    again = c.j("GET", "/v1/drift/hotel-aurora")
    r.check("faulted price never became the baseline", not [f for f in again["flags"] if f["kind"] == "price"],
            [f for f in again["flags"] if f["kind"] == "price"])
    for cust in ("school-tampere", "hospital-meilahti"):
        x = c.j("GET", f"/v1/drift/{cust}")
        r.check(f"{cust}: flags have plain-language messages", all(f["message"] for f in x["flags"]))
        r.check(f"{cust}: allergen flags never auto", all(f["route"] == "approval" for f in x["flags"] if f["kind"] == "allergen"))
        r.say(f"{cust}: " + x["summary"][:200])
    ev = c.j("GET", "/v1/audit", params={"subject_id": "drift-hotel-aurora"})
    r.check("drift checks are audit-logged", ev and ev[-1]["actor"] == "agent:valio-aimo/drift-watch-1", len(ev))
    nf = c.call("GET", "/v1/drift/unknown-customer")
    r.check("unknown customer -> 404", nf.status_code == 404, nf.status_code)


if __name__ == "__main__":
    main("s7_drift", run)
