import argparse, base64, json, os, sys
from datetime import date, timedelta
from functools import lru_cache
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEYS = {
    "hotel-aurora": os.environ.get("KEY_HOTEL", "demo-hotel-aurora"),
    "hospital-tays": os.environ.get("KEY_HOSPITAL", "demo-hospital-tays"),
    "school-hervanta": os.environ.get("KEY_SCHOOL", "demo-school-hervanta"),
}
ADMIN = {"X-Admin-Key": os.environ.get("AIMO_ADMIN_KEY", "demo-admin")}
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
        return self.j("GET", "/v1/approvals", headers=ADMIN)

    def decide(self, order_id, approve, by=None, note="scenario"):
        by = by if by and by != "scenario-human" else self.j("GET", "/v1/mandate")["approvers"][0]
        return self.j("POST", f"/v1/approvals/{order_id}", json={"approve": approve, "by": by, "note": note}, headers=ADMIN)

    def audit(self, order_id):
        return self.j("GET", "/v1/audit", params={"order_id": order_id})


def next_monday(today=None):
    d = today or date.today()
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def next_delivery_day(min_days=2, today=None):
    d = (today or date.today()) + timedelta(days=min_days)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


def pick_of(o, qty):
    return {"product_id": o["product_id"], "quantity": qty, "substitute_for": o.get("substitute_for")}


def is_substitute(o):
    return bool(o.get("substitute_for"))


TAMPER_STATUS = (409,)


def picks_from(offers_resp, per_item=None):
    picks = []
    for it in offers_resp["items"]:
        for o in it["offers"][:per_item]:
            if o["units_needed"] > 0:
                picks.append(pick_of(o, o["units_needed"]))
    return picks


def best_picks_covering(item, need_units=None):
    if need_units is None:
        return [pick_of(o, min(o["units_needed"], o["available_amount"])) for o in item["offers"] if min(o["units_needed"], o["available_amount"]) > 0]
    out, got = [], 0.0
    for o in item["offers"]:
        if got >= need_units:
            break
        take = min(o["units_needed"], o["available_amount"], need_units - got)
        if take <= 0:
            continue
        out.append(pick_of(o, take))
        got += take
    return out


@lru_cache(maxsize=8)
def _product_db(path):
    with open(path, encoding="utf-8") as f:
        return {p["id"]: p for p in json.load(f)}


def product_db():
    return _product_db(os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json"))


def allergens_of(pid):
    try:
        db = product_db()
    except FileNotFoundError:
        return None
    p = db.get(str(pid))
    if not p:
        return None
    out = set()
    for c in p.get("synkkaData", {}).get("classifications") or []:
        if c.get("name") == "allergen":
            out |= {v["id"] for v in c.get("values") or [] if v.get("unit") in ("CONTAINS", "MAY_CONTAIN")}
    return out


def decode_jws(jws):
    h, p, _ = jws.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(pad(h))), json.loads(base64.urlsafe_b64decode(pad(p)))


def verify_jws(base, jws):
    from joserfc import jws as J
    from joserfc.jwk import KeySet
    keys = httpx.get(base + "/.well-known/jwks.json", timeout=30).json()
    return J.deserialize_compact(jws, KeySet.import_key_set(keys), algorithms=["EdDSA"])


def simulate(client, kind, **params):
    return client.j("POST", "/v1/admin/simulate", json={"product_id": str(params["product_id"]), "available_amount": params.get("available_amount", 0)}, headers=ADMIN)


def clear_simulation(client):
    client.call("DELETE", "/v1/admin/simulate", headers=ADMIN)


def table(rows):
    w = max([len(r[1]) for r in rows] + [8])
    lines = [f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {why}" for ok, n, why in rows]
    fails = sum(1 for r in rows if not r[0])
    lines.append(f"{len(rows) - fails}/{len(rows)} passed")
    return "\n".join(lines), fails


def excluded_codes(names):
    from aimo.catalog import ALLERGEN_ALIASES, ALLERGEN_NAMES
    out = set()
    for a in names:
        a = a.strip().lower()
        out |= set(ALLERGEN_ALIASES.get(a) or [k for k, v in ALLERGEN_NAMES.items() if v == a] or [a.upper()])
    return out


def violates(pid, names):
    return bool((allergens_of(pid) or set()) & excluded_codes(names))
