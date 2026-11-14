import argparse, datetime, logging, os, statistics, tempfile, time
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


def flow(url, customer, key, client=None):
    res = {"customer": customer, "steps": {}}
    c = client or httpx.Client(base_url=url.rstrip("/"), headers={"X-Agent-Key": key}, timeout=120)
    hdrs = {"X-Agent-Key": key}
    try:
        step(c, "mandate", "GET", "/v1/mandate", res, headers=hdrs)
        need = step(c, "parse", "POST", "/v1/needs/parse", res, headers=hdrs, json={"text": TEXT, "customer": customer})
        offers = need and step(c, "offers", "POST", "/v1/offers", res, headers=hdrs, json=need)
        quote = None
        if offers and offers["items"]:
            it = offers["items"][0]
            res["offers"] = len(it["offers"])
            res["shortfall"] = it.get("shortfall")
            res["alternatives_count"] = len(it.get("alternatives", []))
            if it["offers"]:
                top = it["offers"][0]
                res["picked_product_id"] = top["product_id"]
                res["picked_name"] = (top.get("product") or {}).get("name")
                res["units_needed"] = top.get("units_needed")
                res["unit_price_net"] = top.get("unit_price_net")
                if top.get("total_gross") is not None:
                    res["total_gross"] = top["total_gross"]
            lines = pick(it["offers"], it["offers"][0]["units_needed"]) if it["offers"] else []
            if lines:
                quote = step(c, "quote", "POST", "/v1/quotes", res, headers=hdrs, json={"offers": lines, "delivery_date": str(need["deliver_by"])})
        order = quote and step(c, "order", "POST", "/v1/orders", res, headers=hdrs, json={"quote_id": quote["quote_id"], "jws": quote["jws"]})
        if quote:
            res["total_net"] = quote["total_net"]
            if quote.get("total_gross") is not None:
                res["total_gross"] = quote["total_gross"]
        if order:
            res["outcome"] = order["decision"]["outcome"]
            res["reasons"] = [r["code"] for r in order["decision"]["reasons"]]
        step(c, "approvals", "GET", "/v1/approvals", res, headers=hdrs)
        a = step(c, "audit", "GET", "/v1/audit", res, headers=hdrs, params={"customer": customer, **({"order_id": order["order_id"]} if order else {})})
        if a is not None:
            res["audit_events"] = len(a["events"] if isinstance(a, dict) and "events" in a else a)
        return res
    finally:
        if client is None and hasattr(c, "close"):
            c.close()


def inprocess(customers=CUSTOMERS):
    saved = {k: os.environ.get(k) for k in ("AIMO_DB", "AIMO_KEY_DIR")}
    prev_log = logging.root.manager.disable
    logging.disable(logging.INFO)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AIMO_DB"] = os.path.join(tmp, "aimo.db")
        os.environ["AIMO_KEY_DIR"] = os.path.join(tmp, "keys")
        try:
            from fastapi.testclient import TestClient
            from aimo.app import app
            with TestClient(app) as client:
                return [flow("", c, "demo-" + c, client=client) for c in customers]
        finally:
            logging.disable(prev_log)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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
    r0 = next((r for r in results if r.get("picked_product_id")), None)
    if r0:
        L.append(
            f"picked {r0['picked_product_id']} {r0.get('picked_name') or ''}: "
            f"units_needed={r0.get('units_needed')}, unit_price_net={r0.get('unit_price_net')}, "
            f"total_net={r0.get('total_net')}, total_gross={r0.get('total_gross')}, "
            f"alternatives={r0.get('alternatives_count')}."
        )
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--url", default=None)
    g.add_argument("--inprocess", action="store_true")
    a = ap.parse_args(argv)
    if a.inprocess:
        results = inprocess()
    else:
        url = a.url or "http://localhost:8124"
        results = [flow(url, c, "demo-" + c) for c in CUSTOMERS]
    print(table(results))
    return results


if __name__ == "__main__":
    main()
