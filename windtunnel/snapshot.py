"""Record the Valio mock API ONCE for a fixed product pool, then replay it offline (freighter compute nodes
have no internet) with deterministic fault injection.

  uv run python -m windtunnel.snapshot record           # laptop only, ~14 polite calls (<=1000 ids each)
  uv run python -m windtunnel.snapshot info

Snapshot layout (data/windtunnel/snapshot/):
  pool.json                       {"groups": {group: [ids]}, "ids": [...], "created": ...}
  price_<YYYY-MM-DD>.json         raw /api/price response for all pool ids
  availability_<YYYY-MM-DD>.json  raw /api/availability response for all pool ids
  meta.json                       {"time_now": <server timeNow of first call>, "dates": [...], ...}

Fault spec (same syntax as the gateway's AIMO_FAULTS / X-Aimo-Faults; parsed independently here):
  stockout:<id>              availability -> scheduleLines [] (cannot be delivered)
  partial:<id>:<amount>      availableAmount capped at <amount>
  price_shock:<id>:<factor>  every price of <id> multiplied by factor
  cutoff:<id>                closingTime moved before time_now
  sales_block:<id>           salesBlock true
  price_error:<id>           price entry error=true, prices null
  error_rate:<p>             each backend call raises with probability p (deterministic per seed)
  latency:<ms>               adds simulated latency (accounted, not slept, in pipeline mode)
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from aimo.models import Offer
from windtunnel.config import SNAPSHOT_DIR
from windtunnel import products as P

# Product groups for scenarios: (group, any-of terms, all-of terms). Selection is deterministic.
POOL_GROUPS: list[tuple[str, list[str], list[str]]] = [
    ("milk_lactose_free", [], ["milk", "lactose free"]),
    ("milk_lactose_free_fi", [], ["maito", "laktoositon"]),
    ("milk", ["milk drink", "maitojuoma", "milk 1", "skimmed milk", "whole milk"], []),
    ("plant_drink", ["oat drink", "kaurajuoma", "soy drink", "soijajuoma", "oat based", "almond drink", "pea drink"], []),
    ("cream", ["cream", "kerma"], []),
    ("butter", ["butter", "voi 500", "margarine", "levite"], []),
    ("cheese", ["cheese", "juusto"], []),
    ("yoghurt_quark", ["yoghurt", "yogurt", "jogurtti", "quark", "rahka"], []),
    ("eggs", ["egg ", "eggs", "kananmuna"], []),
    ("bread", ["bread", "leipä", "bun", "sämpylä", "roll"], []),
    ("gluten_free", ["gluten free", "gluteeniton"], []),
    ("flour", ["flour", "jauho"], []),
    ("pasta_rice", ["pasta", "spaghetti", "macaroni", "rice", "riisi"], []),
    ("potato", ["potato", "peruna"], []),
    ("vegetables", ["tomato", "tomaatti", "cucumber", "kurkku", "carrot", "porkkana", "onion", "sipuli", "lettuce"], []),
    ("chicken_meat", ["chicken", "broiler", "kana", "beef", "pork", "minced"], []),
    ("fish", ["salmon", "lohi", "fish", "kala", "tuna"], []),
    ("coffee_tea", ["coffee", "kahvi", "tea "], []),
    ("juice", ["juice", "mehu"], []),
    ("ice_cream_dessert", ["ice cream", "jäätelö", "dessert", "vanilla sauce", "vaniljakastike"], []),
    ("vegan", ["vegan", "plant-based", "plant based", "kasvis"], []),
    ("oil", ["rapeseed oil", "rypsiöljy", "olive oil", "oil"], []),
    ("nuts_snacks", ["peanut", "almond", "hazelnut", "cashew", "nut mix", "cookie", "biscuit", "keksi"], []),
    ("textured_hospital", ["puree", "sose", "thickener", "sakeute", "smoothie", "porridge", "puuro"], []),
]
DEMO_IDS = ["463038", "464306", "469363", "462775"]
PER_GROUP = 40
MAX_POOL = 1000


def select_pool() -> dict[str, Any]:
    cat = P.catalog()
    groups: dict[str, list[str]] = {}
    seen: set[str] = set(DEMO_IDS)
    for g, any_terms, all_terms in POOL_GROUPS:
        if all_terms:
            cands = P.search(all_terms, must_all=True, limit=5000)
        else:
            cands = P.search(any_terms, limit=5000)
        # prefer products with structured allergen data, then stable id order
        cands.sort(key=lambda p: (not p.has_allergen_data, p.id))
        picked = []
        for p in cands:
            if p.id in seen:
                continue
            picked.append(p.id)
            seen.add(p.id)
            if len(picked) >= PER_GROUP:
                break
        groups[g] = picked
    groups["demo"] = list(DEMO_IDS)
    ids = list(DEMO_IDS) + [i for g, v in groups.items() if g != "demo" for i in v]
    ids = ids[:MAX_POOL]
    assert all(i in cat for i in ids)
    return {"groups": groups, "ids": ids, "created": datetime.now().isoformat(timespec="seconds")}


def default_dates(today: date | None = None) -> list[str]:
    t = today or date.today()
    offs = [-1, 1, 2, 3, 4, 6, 9]  # yesterday (impossible), tomorrow (Sunday), Mon..Thu, next-week
    return [(t + timedelta(days=o)).isoformat() for o in offs]


def record(dates: list[str] | None = None, pause_s: float = 2.0) -> None:
    """Laptop only. 2 calls per date, <=1000 ids each, pause between calls."""
    import httpx
    from aimo.config import AIMO_API_KEY, AIMO_BASE_URL
    pool = select_pool()
    (SNAPSHOT_DIR / "pool.json").write_text(json.dumps(pool, indent=1))
    dates = dates or default_dates()
    meta: dict[str, Any] = {"dates": dates, "n_ids": len(pool["ids"]), "calls": 0, "recorded_at": time.time()}
    headers = {"X-API-Key": AIMO_API_KEY}
    with httpx.Client(base_url=AIMO_BASE_URL, headers=headers, timeout=60) as c:
        for d in dates:
            for ep in ("availability", "price"):
                body = {"customer": "windtunnel", "requestedDate": d, "products": pool["ids"]}
                r = c.post(f"/api/{ep}", json=body)
                meta["calls"] += 1
                if r.status_code != 200:
                    print(f"{ep} {d}: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
                    (SNAPSHOT_DIR / f"{ep}_{d}.error.json").write_text(json.dumps({"status": r.status_code, "body": r.text[:2000]}))
                else:
                    js = r.json()
                    (SNAPSHOT_DIR / f"{ep}_{d}.json").write_text(json.dumps(js))
                    if ep == "availability" and "time_now" not in meta:
                        meta["time_now"] = js.get("timeNow")
                    print(f"{ep} {d}: {len(js.get('products', []))} products")
                time.sleep(pause_s)
    (SNAPSHOT_DIR / "meta.json").write_text(json.dumps(meta, indent=1))


# ---------------------------------------------------------------- faults
@dataclass
class Faults:
    stockout: set[str] = field(default_factory=set)
    partial: dict[str, float] = field(default_factory=dict)
    price_shock: dict[str, float] = field(default_factory=dict)
    cutoff: set[str] = field(default_factory=set)
    sales_block: set[str] = field(default_factory=set)
    price_error: set[str] = field(default_factory=set)
    error_rate: float = 0.0
    latency_ms: float = 0.0
    unknown: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, spec: str | None) -> "Faults":
        f = cls()
        for part in (spec or "").split(";"):
            part = part.strip()
            if not part:
                continue
            k, *a = part.split(":")
            try:
                if k == "stockout":
                    f.stockout.add(a[0])
                elif k == "partial":
                    f.partial[a[0]] = float(a[1])
                elif k == "price_shock":
                    f.price_shock[a[0]] = float(a[1])
                elif k == "cutoff":
                    f.cutoff.add(a[0])
                elif k == "sales_block":
                    f.sales_block.add(a[0])
                elif k == "price_error":
                    f.price_error.add(a[0])
                elif k == "error_rate":
                    f.error_rate = float(a[0])
                elif k == "latency":
                    f.latency_ms = float(a[0])
                else:
                    f.unknown.append(part)
            except (IndexError, ValueError):
                f.unknown.append(part)
        return f


class BackendError(RuntimeError):
    """Simulated 429/5xx from the mock API."""


def _scale_price(pr: dict | None, k: float) -> dict | None:
    if not pr:
        return pr
    pr = dict(pr)
    for key in ("value", "valueWithTax"):
        if isinstance(pr.get(key), (int, float)):
            pr[key] = round(pr[key] * k, 2)
    return pr


_FILE_CACHE: dict[str, dict] = {}
MISSING: set[str] = set()          # ids a system under test asked for that the snapshot lacks (see `extend`)


class ReplayClient:
    """Implements the BackendClient protocol (docs/interfaces.md) from the recorded snapshot."""

    def __init__(self, faults: str | Faults | None = None, seed: int = 0, snapshot_dir=SNAPSHOT_DIR):
        self.dir = snapshot_dir
        self.faults = faults if isinstance(faults, Faults) else Faults.parse(faults)
        self.rng = random.Random(seed)
        self.calls = 0
        self.errors = 0
        self.sim_latency_ms = 0.0
        self._cache = _FILE_CACHE if snapshot_dir == SNAPSHOT_DIR else {}
        meta = self.meta()
        self.time_now = meta.get("time_now")

    def meta(self) -> dict:
        key = f"meta:{self.dir}"
        if key not in _FILE_CACHE:
            try:
                _FILE_CACHE[key] = json.loads((self.dir / "meta.json").read_text())
            except FileNotFoundError:
                return {}
        return _FILE_CACHE[key]

    def _load(self, ep: str, d: date | str) -> dict:
        key = f"{ep}_{d if isinstance(d, str) else d.isoformat()}"
        if self._cache is _FILE_CACHE:
            key = f"{self.dir}:{key}"
        if key not in self._cache:
            path = self.dir / f"{key.rsplit(':', 1)[-1]}.json"
            if not path.exists():
                raise KeyError(f"no snapshot for {key}; recorded dates: {self.meta().get('dates')}")
            self._cache[key] = json.loads(path.read_text())
        return self._cache[key]

    def _tick(self) -> None:
        self.calls += 1
        self.sim_latency_ms += self.faults.latency_ms
        if self.faults.error_rate and self.rng.random() < self.faults.error_rate:
            self.errors += 1
            raise BackendError(self.rng.choice(["429 Too Many Requests", "503 Service Unavailable", "500 Internal Server Error"]))

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        self._tick()
        js = self._load("availability", requested_date)
        by_id = {p["productId"]: p for p in js["products"]}
        out = []
        MISSING.update(pid for pid in product_ids if pid not in by_id)
        for pid in product_ids:
            p = deepcopy(by_id.get(pid)) if pid in by_id else {"productId": pid, "salesBlock": False, "scheduleLines": [],
                                                                 "closingTime": None, "logisticModel": None, "_missing": True}
            f = self.faults
            if pid in f.stockout:
                p["scheduleLines"] = []
            if pid in f.partial:
                for sl in p.get("scheduleLines", []):
                    sl["availableAmount"] = min(sl.get("availableAmount") or 0, f.partial[pid])
            if pid in f.cutoff and self.time_now:
                p["closingTime"] = (datetime.fromisoformat(_iso(self.time_now)) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
            if pid in f.sales_block:
                p["salesBlock"] = True
                for sl in p.get("scheduleLines", []):
                    sl["salesBlock"] = True
            out.append(p)
        return {"timeNow": js.get("timeNow"), "requestedDate": js.get("requestedDate"), "products": out}

    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        self._tick()
        js = self._load("price", requested_date)
        by_id = {p["productId"]: p for p in js["products"]}
        out = []
        for pid in product_ids:
            p = deepcopy(by_id.get(pid)) if pid in by_id else {"productId": pid, "quantity": 1.0, "error": True,
                                                                 "netPriceFound": False, "_missing": True}
            if pid in self.faults.price_shock:
                k = self.faults.price_shock[pid]
                for key in ("catalogPrice", "comparisonPrice", "listPrice"):
                    p[key] = _scale_price(p.get(key), k)
                if p.get("priceWithTax"):
                    p["priceWithTax"] = _scale_price(p["priceWithTax"], k)
            if pid in self.faults.price_error:
                p.update({"error": True, "catalogPrice": None, "comparisonPrice": None, "priceWithTax": None})
            out.append(p)
        return {"customerId": js.get("customerId"), "currency": js.get("currency"), "pricingDate": js.get("pricingDate"),
                "products": out}


def _iso(s: str) -> str:
    # "2026-11-14T14:17:46+0200" -> python 3.11 fromisoformat accepts +0200 too, but normalise anyway
    if s and len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        return s[:-2] + ":" + s[-2:]
    return s


def parse_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(_iso(s))
    except ValueError:
        return None


def merge_offers(price_js: dict, avail_js: dict, requested_date: date, now: datetime | None = None) -> dict[str, Offer]:
    """Independent price+availability merge into aimo.models.Offer (oracle side). `now` is frozen to the
    snapshot's server time for reproducibility."""
    now = now or parse_dt(avail_js.get("timeNow")) or datetime.now().astimezone()
    prices = {p["productId"]: p for p in price_js.get("products", [])}
    out: dict[str, Offer] = {}
    for a in avail_js.get("products", []):
        pid = a["productId"]
        pr = prices.get(pid, {})
        sl = (a.get("scheduleLines") or [None])[0]
        closing = parse_dt(a.get("closingTime"))
        cutoff_passed = bool(closing and closing < now)
        amt = sl.get("availableAmount") if sl else 0.0
        avail_date = date.fromisoformat(sl["availableDate"][:10]) if sl and sl.get("availableDate") else None
        sales_block = bool(a.get("salesBlock") or (sl and sl.get("salesBlock")))
        issues = []
        cp = (pr.get("catalogPrice") or {})
        if pr.get("error") or not cp:
            issues.append("price_error")
        if cutoff_passed:
            issues.append("cutoff_passed")
        if sl is None or not amt:
            issues.append("not_available")
        if avail_date and avail_date > requested_date:
            issues.append("late")
        if sales_block:
            issues.append("sales_block")
        logistic = a.get("logisticModel")
        known_amount = None if (logistic == "Terminal" or (amt or 0) >= 999999) else amt
        if known_amount is not None and known_amount < 1e6:
            issues.append("limited_stock")
        available = not ({"price_error", "cutoff_passed", "not_available", "late", "sales_block"} & set(issues))
        cmp = pr.get("comparisonPrice") or {}
        lp = pr.get("listPrice") or {}
        out[pid] = Offer(
            product_id=pid, requested_date=requested_date, available=available, available_amount=known_amount,
            available_date=avail_date, closing_time=closing, cutoff_passed=cutoff_passed, logistic_model=logistic,
            sales_block=sales_block, sales_unit=sl.get("salesUnit") if sl else None,
            unit_price=cp.get("value"), unit_price_gross=cp.get("valueWithTax"), list_price=lp.get("value"),
            comparison_price=cmp.get("value"), comparison_unit=pr.get("comparisonUnit"),
            tax_rate=(pr.get("tax") or {}).get("taxRate"), price_valid_to=parse_dt(cp.get("validTo")),
            fetched_at=now, issues=issues)
    return out


def offers(product_ids: list[str], requested_date: date, client: ReplayClient) -> dict[str, Offer]:
    now = parse_dt(client.time_now)
    return merge_offers(client.price(product_ids, requested_date), client.availability(product_ids, requested_date),
                        requested_date, now)


@__import__("functools").lru_cache(maxsize=1)
def pool() -> dict[str, Any]:  # noqa: D103
    return json.loads((SNAPSHOT_DIR / "pool.json").read_text())


def fingerprint() -> str:
    """Stable hash of the snapshot (for reproducibility stamps in results)."""
    h = hashlib.sha256()
    for f in sorted(SNAPSHOT_DIR.glob("*.json")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def extend(missing_path: str, pause_s: float = 2.0) -> None:
    """Laptop only. Fetch ids that systems under test requested but the snapshot lacks (written by
    `windtunnel.run --collect-missing`), for every recorded date, and merge them into the snapshot files."""
    import httpx
    from aimo.config import AIMO_API_KEY, AIMO_BASE_URL
    from windtunnel import products as P
    cat = P.catalog()
    meta = json.loads((SNAPSHOT_DIR / "meta.json").read_text())
    pl = json.loads((SNAPSHOT_DIR / "pool.json").read_text())
    have = set(pl["ids"])
    want = sorted({i for i in json.loads(open(missing_path).read()) if i in cat and i not in have})
    print(f"extending snapshot with {len(want)} ids x {len(meta['dates'])} dates")
    if not want:
        return
    with httpx.Client(base_url=AIMO_BASE_URL, headers={"X-API-Key": AIMO_API_KEY}, timeout=60) as c:
        for d in meta["dates"]:
            for ep in ("availability", "price"):
                path = SNAPSHOT_DIR / f"{ep}_{d}.json"
                js = json.loads(path.read_text())
                for k in range(0, len(want), 1000):
                    chunk = want[k:k + 1000]
                    r = c.post(f"/api/{ep}", json={"customer": "windtunnel", "requestedDate": d, "products": chunk})
                    meta["calls"] = meta.get("calls", 0) + 1
                    r.raise_for_status()
                    js["products"] += r.json().get("products", [])
                    time.sleep(pause_s)
                path.write_text(json.dumps(js))
                print(f"{ep} {d}: now {len(js['products'])} products")
    pl["ids"] = pl["ids"] + want
    pl["groups"]["sut_requested"] = want
    (SNAPSHOT_DIR / "pool.json").write_text(json.dumps(pl, indent=1))
    meta["n_ids"] = len(pl["ids"])
    meta.setdefault("extended", []).append({"at": time.time(), "n": len(want)})
    (SNAPSHOT_DIR / "meta.json").write_text(json.dumps(meta, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "extend":
        extend(sys.argv[2])
    elif cmd == "record":
        record(sys.argv[2].split(",") if len(sys.argv) > 2 else None)
    elif cmd == "pool":
        pl = select_pool()
        print({g: len(v) for g, v in pl["groups"].items()}, len(pl["ids"]))
    else:
        print(json.dumps(ReplayClient().meta(), indent=1), fingerprint())
