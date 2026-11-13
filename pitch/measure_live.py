import argparse, datetime, json, statistics, time
import httpx

CUSTOMERS = ["hotel-aurora", "hospital-tays", "school-hervanta"]
TEXT = "I need 1,000 litres of lactose-free milk by Monday"


def monday(today=None):
    d = today or datetime.date.today()
    return d + datetime.timedelta(days=(7 - d.weekday()) % 7 or 7)


def pick(offers, need):
    out, left = [], need
    for o in offers:
        if left <= 0:
            break
        q = min(left, o["available_amount"]) if o.get("available_amount") else left
        if q > 0:
            out.append({"product_id": o["product_id"], "quantity": q})
            left -= q
    return out


def step(c, name, method, path, res, **kw):
    t = time.perf_counter()
    try:
        r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        res["steps"][name] = (None, f"error {type(e).__name__}")
        return None
    ms = round((time.perf_counter() - t) * 1000)
    if r.status_code == 404:
        res["steps"][name] = (None, "404 skipped")
        return None
    res["steps"][name] = (ms, r.status_code)
    return r.json() if r.status_code < 300 else None


def flow(url, customer, key):
    res = {"customer": customer, "steps": {}}
    c = httpx.Client(base_url=url.rstrip("/") + "/v1", headers={"X-Agent-Key": key}, timeout=120)
    step(c, "mandate", "GET", "/mandate", res)
    need = step(c, "parse", "POST", "/needs/parse", res, json={"text": TEXT, "customer": customer})
    offers = need and step(c, "offers", "POST", "/offers", res, json=need)
    quote = None
    if offers and offers["items"]:
        it = offers["items"][0]
        res["offers"] = len(it["offers"])
        res["shortfall"] = it.get("shortfall")
        lines = pick(it["offers"], it["offers"][0]["units_needed"]) if it["offers"] else []
        if lines:
            quote = step(c, "quote", "POST", "/quotes", res, json={"offers": lines, "delivery_date": str(need["deliver_by"])})
    order = quote and step(c, "order", "POST", "/orders", res, json={"quote_id": quote["quote_id"], "jws": quote["jws"]})
    if quote:
        res["total_net"] = quote["total_net"]
    if order:
        res["outcome"] = order["decision"]["outcome"]
        res["reasons"] = [r["code"] for r in order["decision"]["reasons"]]
    step(c, "approvals", "GET", "/approvals", res)
    a = step(c, "audit", "GET", "/audit", res, params={"customer": customer, **({"order_id": order["order_id"]} if order else {})})
    if a is not None:
        res["audit_events"] = len(a["events"] if isinstance(a, dict) and "events" in a else a)
    return res


def table(results):
    names = ["mandate", "parse", "offers", "quote", "order", "approvals", "audit"]
    L = ["| Customer | " + " | ".join(n + " ms" for n in names) + " | offers | total net | outcome | reasons | audit events |", "|" + "---|" * (len(names) + 6)]
    for r in results:
        cells = [str(r["steps"].get(n, (None, "-"))[0] if r["steps"].get(n, (None,))[0] is not None else r["steps"].get(n, (None, "-"))[1]) for n in names]
        L.append("| " + " | ".join([r["customer"]] + cells + [str(r.get("offers", "NOT FOUND")), str(r.get("total_net", "NOT FOUND")), r.get("outcome", "NOT FOUND"), ",".join(r.get("reasons", [])) or "-", str(r.get("audit_events", "NOT FOUND"))]) + " |")
    oc = [r.get("outcome") for r in results]
    L += ["", f"auto {oc.count('auto')}, needs_approval {oc.count('needs_approval')}, blocked {oc.count('blocked')} of {len(results)} customers (milk request)."]
    ms = [v[0] for r in results for v in r["steps"].values() if v[0] is not None]
    if ms:
        L.append(f"median step latency {statistics.median(ms)} ms over {len(ms)} calls.")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8124")
    a = ap.parse_args()
    print(table([flow(a.url, c, "demo-" + c) for c in CUSTOMERS]))
