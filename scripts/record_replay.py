"""Record the keyless replay snapshot ONCE from the live Valio Aimo mock API (owner: S1 replay).

    uv run python scripts/record_replay.py                 (needs AIMO_API_KEY + the challenge files in data/)
    uv run python scripts/record_replay.py --collect-only  (phase 1 only: print the ids/dates the flows touch)

Phase 1 (collect): boots a scratch gateway whose backend client is wrapped by a recorder (same code, live
upstream, Gemini off so the keyless code paths run), then drives every flow the demo and the judge gate use:
scenarios s0..s7 (scenarios.run_all --base), the conformance suite, docs/pitch/qa_milk_flow.py, mcp-check style
offers, and the buyer agent (mock model) on the UI's default requests and the montage requests. The recorder logs
every (endpoint, date, product ids) the gateway asks upstream, and every catalog row the gateway reads.

Phase 2 (record): one polite pass over the union: per date, POST /api/price and /api/availability with <= 1,000
ids per call (a few calls in total), written as raw responses to tests/fixtures/snapshot/replay/
{price,availability}_<date>.json.gz plus meta.json (time_now = the upstream timeNow at recording). The replay
clock anchors at that time, so the scenarios ask for exactly the recorded dates on any real day.

Phase 3 (catalog subset): the raw public challenge records (data/valio_aimo_junction26_product_data.json and
the two CSVs) for the recorded (priced) products, gzipped under tests/fixtures/snapshot/replay/catalog/. A keyless clone
uses them (aimo/catalog/__init__.py) instead of the 87 MB file.

Never prints or writes secrets. Dates that were not recorded are refused by the replay client, never invented.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
DATA_LINK = ("valio_aimo_junction26_product_data.json", "valio_aimo_junction26_product_data_schema.json",
             "valio_aimo_junction26_delivery_reliability.csv", "valio_aimo_junction26_carbon_footprint.csv",
             "catalog.sqlite", "embeddings.npy", "embeddings_ids.json")

AGENT_RUNS = [
    ("hotel-aurora", "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.", None),
    ("school-tampere", "We need 120 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.", None),
    ("hospital-meilahti", "We need 40 kg of gluten-free bread for the Meilahti wards by Wednesday.", None),
    ("hotel-aurora", "We need 1,500 litres of lactose-free milk at Hotel Aurora Helsinki by Monday for a conference.", "partial:*:400"),
    ("school-tampere", "We need 200 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.", None),
    ("hotel-aurora", "We need 300 litres of lactose-free milk by Monday. Check the product details of the alternatives before choosing.", "inject_text:*"),
    ("hotel-aurora", "Standing order for next week: 4,000 litres of lactose-free milk by Monday.", None),
    ("hotel-aurora", "Last-minute conference dinner for 150 on Monday evening: we need 40 L cream and 20 kg butter, lactose-free.", None),
]


def serve(port: int, log_path: str) -> None:
    """Run the scenarios app with a recording backend client and catalog-row logger."""
    lock = threading.Lock()
    log = open(log_path, "a", buffering=1)

    def emit(rec: dict) -> None:
        with lock:
            log.write(json.dumps(rec) + "\n")

    from aimo.backend import client as C

    class Recorder:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def price(self, product_ids, requested_date, customer=None, fresh=False):
            emit({"kind": "offers", "endpoint": "price", "date": requested_date.isoformat(), "customer": customer,
                  "ids": [str(i) for i in product_ids]})
            return self._inner.price(product_ids, requested_date, customer, fresh=fresh)

        def availability(self, product_ids, requested_date, customer=None, fresh=False):
            emit({"kind": "offers", "endpoint": "availability", "date": requested_date.isoformat(),
                  "customer": customer, "ids": [str(i) for i in product_ids]})
            return self._inner.availability(product_ids, requested_date, customer, fresh=fresh)

    C.set_client(Recorder(C.ValioClient()))

    from aimo.catalog import textsearch as T

    orig_rows, orig_fts = T.get_rows, T.fts_search

    def get_rows(ids):
        out = orig_rows(ids)
        emit({"kind": "rows", "ids": list(out)})
        return out

    def fts_search(*a, **kw):
        out = orig_fts(*a, **kw)
        emit({"kind": "rows", "ids": [pid for pid, _ in out]})
        return out

    T.get_rows, T.fts_search = get_rows, fts_search
    for modname in ("aimo.catalog.plan", "aimo.catalog.substitution", "aimo.recipes.planner", "aimo.gateway.service"):
        try:
            mod = __import__(modname, fromlist=["x"])
            if getattr(mod, "get_rows", None) is orig_rows:
                mod.get_rows = get_rows
            if getattr(mod, "fts_search", None) is orig_fts:
                mod.fts_search = fts_search
        except Exception:
            pass

    import uvicorn

    from scenarios.app_with_extras import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def collect(verbose: bool = False) -> tuple[list[dict], str]:
    import httpx

    data = Path(os.environ.get("AIMO_DATA_DIR", ROOT / "data"))
    if not (data / DATA_LINK[0]).exists():
        sys.exit(f"needs the challenge catalog in {data} (uv run aimo fetch-data)")
    scratch = Path(tempfile.mkdtemp(prefix="aimo-record-"))
    for name in DATA_LINK:
        if (data / name).exists():
            os.symlink(os.path.realpath(data / name), scratch / name)
    log_path = scratch / "calls.jsonl"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    pin = os.environ.get("AIMO_APPROVER_PIN") or secrets.token_urlsafe(9)
    env = {**os.environ, "AIMO_DATA_DIR": str(scratch), "AIMO_APPROVER_PIN": pin, "AIMO_DEMO_FAULTS": "1",
           "AIMO_REPLAY": "0", "AIMO_REPLAY_FALLBACK": "0", "AIMO_LLM_MODE": "mock", "GEMINI_API_KEY": "",
           "GOOGLE_API_KEY": "", "AIMO_RECIPE_CHECK": "0", "AIMO_URL": base}
    env.pop("AIMO_NOW", None)
    srv_log = open(scratch / "server.log", "w")
    srv = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_serve", str(port), str(log_path)],
                           env=env, cwd=ROOT, stdout=srv_log, stderr=srv_log)
    try:
        for _ in range(120):
            try:
                if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            sys.exit("recording gateway did not start; see " + str(scratch / "server.log"))
        clock_now = httpx.get(base + "/healthz", timeout=5).json()["clock"]["now"]
        print(f"recording gateway at {base} (clock {clock_now}); driving flows")

        def run(label: str, cmd: list[str]) -> None:
            t = time.time()
            p = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)
            tail = (p.stdout + p.stderr).strip().splitlines()[-12:]
            print(f"  {label}: exit {p.returncode} ({time.time() - t:.0f}s)")
            if verbose or p.returncode:
                print("    " + "\n    ".join(tail))

        run("scenarios s0..s7", [sys.executable, "-m", "scenarios.run_all", "--base", base])
        run("conformance", [sys.executable, "-m", "conformance", "--base", base, "--quiet",
                            "--json", str(scratch / "conformance.json")])
        run("qa_milk_flow", [sys.executable, "docs/pitch/qa_milk_flow.py", "--base", base])
        agent_runs(base, pin)
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    recs = [json.loads(x) for x in log_path.read_text().splitlines() if x.strip()]
    shutil.rmtree(scratch, ignore_errors=True)
    return recs, clock_now


def agent_runs(base: str, pin: str) -> None:
    """Buyer agent (mock model, deterministic) on the UI + montage requests; approves what waits for a human."""
    import httpx

    from aimo.trust import load_policy

    for cust, text, faults in AGENT_RUNS:
        t = time.time()
        try:
            run_hdrs = {"X-Customer-Id": cust, "X-Approver-Pin": os.environ.get("AIMO_APPROVER_PIN", "") or pin}
            r = httpx.post(base + "/v1/agent/runs", json={"text": text, "customer_id": cust, "faults": faults,
                                                          "clarify": False},
                           headers=run_hdrs,
                           timeout=60)
            rid = r.json().get("run_id")
            status = "?"
            for _ in range(240):
                v = httpx.get(f"{base}/v1/agent/runs/{rid}", headers=run_hdrs, timeout=30).json()
                status = v.get("status")
                if status in ("finished", "done", "error", "failed", "completed", "cancelled"):
                    break
                for a in httpx.get(base + "/v1/approvals", params={"status": "pending"},
                                   headers={"X-Customer-Id": cust}, timeout=30).json():
                    o = a.get("order") or {}
                    role = (o.get("decision") or {}).get("approver_role")
                    appr = next((x for x in load_policy(o.get("customer_id") or cust).get("approvers", [])
                                 if not role or x["role"] == role), None)
                    if appr:
                        httpx.post(f"{base}/v1/approvals/{o['order_id']}/decision",
                                   json={"decision": "approve", "approver": appr["id"], "approver_role": appr["role"],
                                         "note": "record_replay", "quote_hash": o.get("quote_hash")},
                                   headers={"X-Approver-Pin": pin}, timeout=60)
                time.sleep(1.0)
            print(f"  agent run {cust} '{text[:48]}...' faults={faults}: {status} ({time.time() - t:.0f}s)")
        except Exception as e:
            print(f"  agent run {cust} failed: {type(e).__name__}: {e}")


def record(ids: list[str], dates: list[str], out: Path) -> dict:
    from aimo.backend.client import ValioClient, chunked

    c = ValioClient(cache_dir=None)
    if not c._api_key:
        sys.exit("AIMO_API_KEY is not set: recording needs the live mock API")
    time_now = None
    calls = 0
    for d in dates:
        for endpoint in ("price", "availability"):
            head: dict = {}
            products: list[dict] = []
            for chunk in chunked(ids):
                resp = c._post(endpoint, {"customer": "aimo-gateway", "requestedDate": d, "products": chunk})
                calls += 1
                head = head or {k: v for k, v in resp.items() if k != "products"}
                products += resp.get("products", []) or []
            if endpoint == "availability" and time_now is None:
                time_now = head.get("timeNow")
            blob = json.dumps({**head, "products": products}, ensure_ascii=False, sort_keys=True).encode()
            (out / f"{endpoint}_{d}.json.gz").write_bytes(gzip.compress(blob, 9, mtime=0))
            print(f"  {endpoint} {d}: {len(products)} products")
    return {"time_now": time_now, "upstream_calls": calls}


def catalog_subset(ids: set[str], out: Path) -> int:
    from aimo import config

    cat = out / "catalog"
    cat.mkdir(parents=True, exist_ok=True)
    raw = json.loads(Path(config.PRODUCT_JSON).read_text(encoding="utf-8"))
    sub = [p for p in raw if str(p.get("id")) in ids]
    blob = json.dumps(sub, ensure_ascii=False, separators=(",", ":")).encode()
    (cat / (config.PRODUCT_JSON.name + ".gz")).write_bytes(gzip.compress(blob, 9, mtime=0))
    for path in (config.RELIABILITY_CSV, config.CO2_CSV):
        path = Path(path)
        if not path.exists():
            continue
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(rows[0])
        w.writerows(r for r in rows[1:] if r and r[0].strip() in ids)
        (cat / (path.name + ".gz")).write_bytes(gzip.compress(buf.getvalue().encode(), 9, mtime=0))
    return len(sub)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--_serve":
        serve(int(sys.argv[2]), sys.argv[3])
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--extra-weekdays", type=int, default=5, help="also record the next N weekdays after today")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    recs, clock_now = collect(a.verbose)
    offer_ids = Counter()
    dates = Counter()
    customers = Counter()
    row_ids: set[str] = set()
    for r in recs:
        if r["kind"] == "offers":
            offer_ids.update(r["ids"])
            dates[r["date"]] += 1
            customers[r.get("customer") or "-"] += 1
        else:
            row_ids.update(r["ids"])
    today = date.fromisoformat(clock_now[:10])
    extra = []
    d = today
    while len(extra) < a.extra_weekdays:
        d += timedelta(days=1)
        if d.weekday() < 5:
            extra.append(d.isoformat())
    all_dates = sorted(set(dates) | set(extra))
    ids = sorted(offer_ids)
    print(f"collected {len(recs)} calls: {len(ids)} offer ids, {len(row_ids)} catalog rows, dates {dict(dates)}, "
          f"customers {dict(customers)}")
    if a.collect_only:
        return 0
    out = Path(a.out)
    if out.exists():
        for f in out.glob("*.json*"):
            f.unlink()
    out.mkdir(parents=True, exist_ok=True)
    print(f"recording {len(ids)} ids x {len(all_dates)} dates ({', '.join(all_dates)}) into {out}")
    info = record(ids, all_dates, out)
    n_cat = catalog_subset(set(ids), out)
    meta = {"time_now": info["time_now"], "dates": all_dates, "n_ids": len(ids), "n_catalog_products": n_cat,
            "upstream_calls": info["upstream_calls"], "recorded_with": "scripts/record_replay.py",
            "flows": ["scenarios s0..s7", "conformance", "docs/pitch/qa_milk_flow.py",
                      "buyer agent (mock model): UI defaults + montage requests"],
            "source": "public Valio Aimo mock API responses (junction26.valioaimo.fi), raw; catalog subset from the "
                      "public challenge files"}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(f"wrote {out} ({info['upstream_calls']} upstream calls; {n_cat} catalog products; time_now {info['time_now']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
