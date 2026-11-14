import argparse, os, re, shutil, subprocess, sys, tempfile, time
from glob import glob
from urllib.parse import urlsplit
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import table


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOT_DIRS = {}
_HTTP_429_RE = re.compile(
    r'\bHTTP(?:/\d(?:\.\d)?)?"?\s+429\b|\bstatus(?:_code)?[=: ]+429\b|\b429\s+Too Many Requests\b',
    re.I,
)
_HTTP_503_RE = re.compile(
    r'\bHTTP(?:/\d(?:\.\d)?)?"?\s+503\b|\bstatus(?:_code)?[=: ]+503\b|\b503\s+Service Unavailable\b',
    re.I,
)


def reason(p, log=""):
    blob = (p.stdout or "") + "\n" + (p.stderr or "") + "\n" + (log or "")
    if (
        _HTTP_429_RE.search(blob)
        or "upstream_unavailable" in blob.lower()
        or ("valio" in blob.lower() and _HTTP_503_RE.search(blob))
    ):
        return "VALIO 429 (mock rate limit), not a scenario bug"
    fails = [l.strip() for l in (p.stdout or "").splitlines() if "[FAIL]" in l]
    if fails:
        return fails[0].split("[FAIL]", 1)[1].strip()[:140]
    return ((p.stderr or "").strip().splitlines() or ["no output"])[-1][:140]


def boot(base):
    port = str(urlsplit(base).port or base.rsplit(":", 1)[1].split("/")[0])
    d = tempfile.mkdtemp(prefix="aimo-boot-")
    env = {**os.environ, "AIMO_DB": os.path.join(d, "aimo.db")}
    env.setdefault("AIMO_KEY_DIR", os.path.join(d, "keys"))
    env.setdefault("AIMO_DRIFT_STATE", os.path.join(d, "drift.json"))
    logf = open(os.path.join(d, "server.log"), "w+")
    try:
        srv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aimo.app:app", "--port", port],
            env=env,
            stdout=logf,
            stderr=logf,
            cwd=ROOT,
        )
    except BaseException:
        logf.close()
        shutil.rmtree(d, ignore_errors=True)
        raise
    _BOOT_DIRS[id(logf)] = d
    try:
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                if httpx.get(base + "/health").status_code == 200:
                    break
            except Exception:
                pass
            if srv.poll() is not None:
                break
            time.sleep(0.5)
    except BaseException:
        cleanup(srv, logf)
        raise
    return srv, logf


def stop(srv):
    try:
        srv.terminate()
    except ProcessLookupError:
        return
    try:
        srv.wait(10)
    except Exception:
        srv.kill()
        try:
            srv.wait(5)
        except Exception:
            pass


def cleanup(srv, logf):
    try:
        if srv:
            stop(srv)
    finally:
        if logf:
            d = _BOOT_DIRS.pop(id(logf), None)
            logf.close()
            if d:
                shutil.rmtree(d, ignore_errors=True)


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
        p = None
        try:
            if fresh:
                srv, logf = boot(base)
            run_env = dict(env)
            d = _BOOT_DIRS.get(id(logf)) if logf else None
            if d:
                run_env.setdefault("AIMO_DRIFT_STATE", os.path.join(d, "drift.json"))
            p = subprocess.run([sys.executable, path, "--base", base], capture_output=True, text=True, env=run_env, cwd=ROOT)
            log = ""
            if logf and p.returncode:
                logf.flush()
                logf.seek(0)
                log = logf.read()
            rows.append((p.returncode == 0, name, reason(p, log) if p.returncode else ""))
        finally:
            cleanup(srv, logf)
        if p and os.environ.get("VERBOSE"):
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
