import argparse, os, subprocess, sys, tempfile, time
import httpx
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import table


def reason(p):
    fails = [l.strip() for l in p.stdout.splitlines() if "[FAIL]" in l]
    if fails:
        return fails[0].replace("[FAIL] ", "")[:140]
    err = (p.stderr.strip().splitlines() or ["no output"])[-1]
    return err[:140]


def run_all(base, only=None):
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
    env["AIMO_URL"] = base
    rows = []
    for path in sorted(glob(os.path.join(here, "s[0-9]*_*.py"))):
        name = os.path.basename(path)[:-3]
        if only and not name.startswith(only):
            continue
        p = subprocess.run([sys.executable, path, "--base", base], capture_output=True, text=True, env=env, cwd=os.path.dirname(here))
        why = reason(p) if p.returncode else ""
        rows.append((p.returncode == 0, name, why))
        if os.environ.get("VERBOSE"):
            print(p.stdout)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--only")
    ap.add_argument("--boot", action="store_true")
    a = ap.parse_args()
    srv = None
    if a.boot:
        port = a.base.rsplit(":", 1)[1].split("/")[0]
        env = {**os.environ, "AIMO_DB": os.path.join(tempfile.mkdtemp(), "aimo.db")}
        srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "aimo.app:app", "--port", port], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for _ in range(60):
            try:
                if httpx.get(a.base + "/health").status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
    try:
        out, fails = table(run_all(a.base, a.only))
    finally:
        if srv:
            srv.terminate()
    print(out)
    sys.exit(fails)
