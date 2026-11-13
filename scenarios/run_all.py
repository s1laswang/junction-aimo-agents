import argparse, os, subprocess, sys, tempfile, time
import httpx
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import table


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reason(p, log=""):
    blob = p.stdout + p.stderr + log
    if "429" in blob or "valio" in blob.lower() and "503" in blob:
        return "VALIO 429 (mock rate limit), not a scenario bug"
    fails = [l.strip() for l in p.stdout.splitlines() if "[FAIL]" in l]
    if fails:
        return fails[0].replace("[FAIL] ", "")[:140]
    return (p.stderr.strip().splitlines() or ["no output"])[-1][:140]


def boot(base):
    port = base.rsplit(":", 1)[1].split("/")[0]
    d = tempfile.mkdtemp()
    env = {**os.environ, "AIMO_DB": os.path.join(d, "aimo.db")}
    logf = open(os.path.join(d, "server.log"), "w+")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "aimo.app:app", "--port", port], env=env, stdout=logf, stderr=logf, cwd=ROOT)
    for _ in range(80):
        try:
            if httpx.get(base + "/health").status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    return srv, logf


def stop(srv):
    srv.terminate()
    try:
        srv.wait(10)
    except Exception:
        srv.kill()


def run_all(base, only=None, fresh=False):
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
    env["AIMO_URL"] = base
    rows = []
    for path in sorted(glob(os.path.join(here, "s[0-9]*_*.py"))):
        name = os.path.basename(path)[:-3]
        if only and not name.startswith(only):
            continue
        srv = logf = None
        if fresh:
            srv, logf = boot(base)
        try:
            p = subprocess.run([sys.executable, path, "--base", base], capture_output=True, text=True, env=env, cwd=ROOT)
            log = ""
            if logf and p.returncode:
                logf.seek(0)
                log = logf.read()
            rows.append((p.returncode == 0, name, reason(p, log) if p.returncode else ""))
        finally:
            if srv:
                stop(srv)
        if os.environ.get("VERBOSE"):
            print(p.stdout)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--only")
    ap.add_argument("--boot", action="store_true")
    a = ap.parse_args()
    out, fails = table(run_all(a.base, a.only, a.boot))
    print(out)
    sys.exit(fails)
