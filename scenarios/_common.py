import argparse, base64, json, os, sys
from datetime import date, timedelta
from functools import lru_cache
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEYS = {
    "hotel-aurora": os.environ.get("KEY_HOTEL", "key-hotel-aurora"),
    "hospital-tays": os.environ.get("KEY_HOSPITAL", "key-hospital-tays"),
    "school-hervanta": os.environ.get("KEY_SCHOOL", "key-school-hervanta"),
}
DATA = os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json")


def parse_base():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    return ap.parse_args().base.rstrip("/")


class Run:
    def __init__(self, name, base):
        self.name, self.base, self.results = name, base, []

    def say(self, text):
        print(text)

    def check(self, label, ok, detail=""):
        self.results.append((label, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail and not ok else ""))
        return bool(ok)

    def finish(self):
        bad = [r for r in self.results if not r[1]]
        print(f"{self.name}: {'FAIL' if bad else 'PASS'}" + (f" - {bad[0][0]}" if bad else ""))
        sys.exit(1 if bad else 0)

    def guard(self, fn):
        try:
            fn()
        except Exception as e:
            self.check("ran without error", False, f"{type(e).__name__}: {e}"[:200])
        self.finish()


class Client:
    def __init__(self, base, customer):
        self.h = httpx.Client(base_url=base, headers={"X-Agent-Key": KEYS[customer]}, timeout=60)

    def call(self, method, path, **kw):
        r = self.h.request(method, path, **kw)
        r.raise_for_status() if r.status_code >= 500 else None
        return r

    def j(self, method, path, **kw):
        r = self.call(method, path, **kw)
        r.raise_for_status()
        return r.json()

    def parse(self, text, customer):
        return self.j("POST", "/v1/needs/parse", json={"text": text, "customer": customer})

    def offers(self, need):
        return self.j("POST", "/v1/offers", json=need)

    def quote(self, picks, delivery_date):
        return self.j("POST", "/v1/quotes", json={"offers": picks, "delivery_date": str(delivery_date)})

    def order(self, quote):
        return self.j("POST", "/v1/orders", json={"quote_id": quote["quote_id"], "jws": quote["jws"]})

    def approvals(self):
        return self.j("GET", "/v1/approvals")

    def decide(self, order_id, approve, by="scenario-human", note="scenario"):
        return self.j("POST", f"/v1/approvals/{order_id}", json={"approve": approve, "by": by, "note": note})

    def audit(self, order_id):
        return self.j("GET", "/v1/audit", params={"order_id": order_id})


def next_monday(today=None):
    d = today or date.today()
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def picks_from(offers_resp, per_item=None):
    picks = []
    for it in offers_resp["items"]:
        for o in it["offers"][: per_item or 1]:
            picks.append({"product_id": o["product_id"], "quantity": o["units_needed"]})
    return picks


def best_picks_covering(item, need_units=None):
    out, got = [], 0.0
    need = need_units if need_units is not None else item["need_item"]["quantity"]
    for o in item["offers"]:
        take = min(o["units_needed"], o["available_amount"])
        if take <= 0:
            continue
        out.append({"product_id": o["product_id"], "quantity": take})
        got += take
        if got >= o["units_needed"]:
            break
    return out


@lru_cache(maxsize=1)
def product_db():
    with open(DATA) as f:
        return {p["id"]: p for p in json.load(f)}


def allergens_of(pid):
    p = product_db().get(str(pid))
    if not p:
        return None
    out = set()
    for c in p["synkkaData"].get("classifications", []):
        if c["name"] == "allergen":
            out |= {v["id"] for v in c["values"] if v.get("unit") == "CONTAINS"}
    return out


def decode_jws(jws):
    h, p, _ = jws.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(pad(h))), json.loads(base64.urlsafe_b64decode(pad(p)))


def verify_jws(base, jws):
    from joserfc import jws as J
    from joserfc.jwk import KeySet
    keys = httpx.get(base + "/.well-known/jwks.json", timeout=30).json()
    return J.deserialize_compact(jws, KeySet.import_key_set(keys))


def simulate(client, kind, **params):
    return client.j("POST", "/v1/admin/simulate", json={"kind": kind, **params})


def table(rows):
    w = max([len(r[1]) for r in rows] + [8])
    lines = [f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {why}" for ok, n, why in rows]
    fails = sum(1 for r in rows if not r[0])
    lines.append(f"{len(rows) - fails}/{len(rows)} passed")
    return "\n".join(lines), fails
