import argparse, os, subprocess, sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import table


def run_all(base):
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
    env["AIMO_URL"] = base
    rows = []
    for path in sorted(glob(os.path.join(here, "s[0-9]*_*.py"))):
        name = os.path.basename(path)[:-3]
        p = subprocess.run([sys.executable, path, "--base", base], capture_output=True, text=True, env=env, cwd=os.path.dirname(here))
        last = (p.stdout.strip().splitlines() or [""])[-1]
        why = last.split(" - ", 1)[1] if p.returncode and " - " in last else (p.stderr.strip().splitlines() or [""])[-1][:100] if p.returncode else ""
        rows.append((p.returncode == 0, name, why))
        if os.environ.get("VERBOSE"):
            print(p.stdout)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8080"))
    out, fails = table(run_all(ap.parse_args().base))
    print(out)
    sys.exit(fails)
