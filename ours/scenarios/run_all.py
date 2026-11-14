import argparse, inspect, os, re, shutil, subprocess, sys, tempfile, time
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


def clean_env(base=None, state_dir=None, environ=None):
    src = os.environ if environ is None else environ
    env = {k: v for k, v in src.items() if not k.startswith("LLM_")}
    if base:
        env["AIMO_URL"] = base
    if state_dir:
        env["AIMO_DB"] = os.path.join(state_dir, "aimo.db")
        env["AIMO_KEY_DIR"] = os.path.join(state_dir, "keys")
        env["AIMO_DRIFT_STATE"] = os.path.join(state_dir, "drift.json")
    return env


def reason(p, log=""):
    out = getattr(p, "stdout", "") or ""
    err = getattr(p, "stderr", "") or ""
    blob = out + "\n" + err + "\n" + (log or "")
    if (
        _HTTP_429_RE.search(blob)
        or "upstream_unavailable" in blob.lower()
        or ("valio" in blob.lower() and _HTTP_503_RE.search(blob))
    ):
        return "VALIO 429 (mock rate limit), not a scenario bug"
    fails = [l.strip() for l in out.splitlines() if "[FAIL]" in l]
    if fails:
        return fails[0].split("[FAIL]", 1)[1].strip()[:140]
    return (err.strip().splitlines() or ["no output"])[-1][:140]


def _health_get(url):
    try:
        params = inspect.signature(httpx.get).parameters
        has_timeout = "timeout" in params or any(
            v.kind == inspect.Parameter.VAR_KEYWORD for v in params.values()
        )
    except (TypeError, ValueError):
        has_timeout = True
    return httpx.get(url, timeout=2.0) if has_timeout else httpx.get(url)


def boot(base, state_dir=None):
    base_clean = base.rstrip("/")
    port = str(urlsplit(base_clean).port or base_clean.rsplit(":", 1)[1].split("/")[0])
    d = state_dir or tempfile.mkdtemp(prefix="aimo-boot-")
    os.makedirs(d, exist_ok=True)
    env = clean_env(base=base_clean, state_dir=d)
    os.makedirs(env["AIMO_KEY_DIR"], exist_ok=True)
    logf = open(os.path.join(d, "server.log"), "w+", encoding="utf-8")
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
        health_url = base_clean + "/health"
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                if _health_get(health_url).status_code == 200:
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
    if srv is None:
        return
    try:
        srv.terminate()
    except OSError:
        return
    try:
        srv.wait(10)
    except Exception:
        try:
            srv.kill()
        except OSError:
            return
        try:
            srv.wait(5)
        except Exception:
            pass


def cleanup(srv, logf):
    try:
        if srv is not None:
            stop(srv)
    finally:
        if logf is not None:
            d = _BOOT_DIRS.pop(id(logf), None)
            try:
                logf.close()
            except Exception:
                pass
            if d:
                shutil.rmtree(d, ignore_errors=True)


def _call_boot(base, state_dir):
    try:
        params = inspect.signature(boot).parameters
    except (TypeError, ValueError):
        params = {}
    if "state_dir" in params:
        return boot(base, state_dir=state_dir)
    return boot(base)


def _run_scenario_proc(cmd, env, timeout):
    kw = {"capture_output": True, "text": True, "env": env, "cwd": ROOT}
    try:
        params = inspect.signature(subprocess.run).parameters
        has_timeout = "timeout" in params or any(
            v.kind == inspect.Parameter.VAR_KEYWORD for v in params.values()
        )
    except (TypeError, ValueError):
        has_timeout = True
    if has_timeout and timeout is not None:
        kw["timeout"] = timeout
    return subprocess.run(cmd, **kw)


def run_all(base, only=None, fresh=False, timeout=None):
    here = os.path.dirname(os.path.abspath(__file__))
    base = base.rstrip("/")
    scen_timeout = float(os.environ.get("AIMO_SCENARIO_TIMEOUT", "120")) if timeout is None else timeout
    rows = []
    for path in sorted(glob(os.path.join(here, "s[0-9]*_*.py"))):
        name = os.path.basename(path)[:-3]
        if only and not name.startswith(only):
            continue
        state_dir = tempfile.mkdtemp(prefix=f"aimo-{name}-") if fresh else None
        srv = logf = None
        p = None
        try:
            if fresh:
                srv, logf = _call_boot(base, state_dir)
            d = (_BOOT_DIRS.get(id(logf)) if logf else None) or state_dir
            run_env = clean_env(base=base, state_dir=d)
            p = _run_scenario_proc([sys.executable, path, "--base", base], run_env, scen_timeout)
            log = ""
            if logf and p.returncode:
                logf.flush()
                logf.seek(0)
                log = logf.read()
            rows.append((p.returncode == 0, name, reason(p, log) if p.returncode else ""))
        except subprocess.TimeoutExpired as exc:
            rows.append((False, name, f"timed out after {exc.timeout:g}s"))
        finally:
            try:
                cleanup(srv, logf)
            finally:
                if state_dir and os.path.exists(state_dir):
                    shutil.rmtree(state_dir, ignore_errors=True)
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
