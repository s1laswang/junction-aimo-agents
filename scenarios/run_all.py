"""Run the acceptance scenarios s0..s7 against our HTTP API and print a PASS/FAIL table.

Ported from Momin's junction26-valio `scenarios/run_all.py` (boot a fresh server, run every scenario, table,
exit code = number of failures), with credit. Default: boots `scenarios.app_with_extras:app` with uvicorn on a
free port and a fresh scratch data dir (catalog + data files symlinked from ./data, empty state DB and keys),
so it never touches anyone's state. Upstream = the live Valio Aimo mock API (needs AIMO_API_KEY in .env).

    uv run python -m scenarios.run_all                 # boot fresh server, run s0..s7
    uv run python -m scenarios.run_all --only s2 -v    # one scenario, verbose
    uv run python -m scenarios.run_all --base http://127.0.0.1:8000   # against a running server
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scenarios._common import Run, table

SCENARIOS = ["s0_guardrails", "s1_milk", "s2_recipe_order", "s3_event_substitution", "s4_stockout",
             "s5_allergen_safe", "s6_proactive_seller", "s7_drift"]
LINK = ("valio_aimo_junction26_product_data.json", "valio_aimo_junction26_product_data_schema.json",
        "valio_aimo_junction26_delivery_reliability.csv", "valio_aimo_junction26_carbon_footprint.csv",
        "catalog.sqlite", "embeddings.npy", "embeddings_ids.json", "cache")


def free_port(start: int = 8810) -> int:
    if start == 8810:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
    for p in range(start, start + 80):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port")


def scratch_data_dir(src: str) -> str:
    d = tempfile.mkdtemp(prefix="aimo-scenarios-")
    for name in LINK:
        p = os.path.join(src, name)
        if os.path.exists(p):
            os.symlink(os.path.realpath(p), os.path.join(d, name))
    return d


def boot(port: int, data_dir: str):
    env = {**os.environ, "AIMO_DATA_DIR": data_dir, "AIMO_RECIPE_CHECK": os.environ.get("AIMO_RECIPE_CHECK", "0"),
           "AIMO_DEMO_FAULTS": "1"}
    log = open(os.path.join(data_dir, "server.log"), "w+")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "scenarios.app_with_extras:app", "--host", "127.0.0.1",
                            "--port", str(port)], env=env, stdout=log, stderr=log, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if srv.poll() is not None:
            break
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                return srv, log, base
        except Exception:
            time.sleep(0.5)
    log.seek(0)
    raise RuntimeError("server did not start:\n" + log.read()[-2000:])


def stop(srv) -> None:
    srv.terminate()
    try:
        srv.wait(10)
    except Exception:
        srv.kill()


def run_all(base: str, only: str | None = None, verbose: bool = False) -> list[tuple[bool, str, str]]:
    rows = []
    for name in SCENARIOS:
        if only and not name.startswith(only):
            continue
        if verbose:
            print(f"== {name}")
        t = time.time()
        r = Run(name, base, quiet=not verbose)
        try:
            mod = importlib.import_module(f"scenarios.{name}")
            r.guard(mod.run)
        except Exception as e:
            r.check("scenario loads", False, f"{type(e).__name__}: {e}")
        rows.append((r.ok, name, f"{r.reason()} ({time.time() - t:.1f}s)"))
    return rows


def main() -> int:
    if not os.environ.get("AIMO_APPROVER_PIN"):
        import secrets
        os.environ["AIMO_APPROVER_PIN"] = secrets.token_urlsafe(9)
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="run against an already running server instead of booting one")
    ap.add_argument("--only")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the scratch data dir (server.log, state.sqlite)")
    a = ap.parse_args()
    srv = data_dir = None
    base = a.base
    if not base:
        data_dir = scratch_data_dir(os.environ.get("AIMO_DATA_DIR", os.path.join(ROOT, "data")))
        srv, _, base = boot(a.port or free_port(), data_dir)
        print(f"booted fresh gateway at {base} (scratch data dir {data_dir})")
    try:
        out, fails = table(run_all(base, a.only, a.verbose))
    finally:
        if srv:
            stop(srv)
        if data_dir and not a.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    print(out)
    return fails


if __name__ == "__main__":
    sys.exit(main())
