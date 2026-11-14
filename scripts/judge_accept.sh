#!/usr/bin/env bash
# One-command judge acceptance for Valio Aimo (s1laswang/junction-aimo-agents).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.local/bin:${PATH}"

rows=(); pass=0; fail=0; skip=0
record() {
  local st="$1" name="$2" detail="$3"
  rows+=("$st|$name|$detail")
  case "$st" in PASS) pass=$((pass+1));; FAIL) fail=$((fail+1));; SKIP) skip=$((skip+1));; esac
  printf '  [%s] %s — %s\n' "$st" "$name" "$detail"
}
say() { printf '%s\n' "$*"; }
has_env() {
  # True if the named variable is set and non-empty in the process env or in .env
  # (values are never printed).
  local n="$1"
  if [ -n "${!n:-}" ]; then return 0; fi
  if [ -f .env ]; then
    python3 - "$n" <<'PY'
import os, sys
from pathlib import Path
name = sys.argv[1]
for line in Path(".env").read_text().splitlines():
    s=line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k,v=s.split("=",1)
    if k.strip()==name and v.strip().strip('"').strip("'"):
        sys.exit(0)
sys.exit(1)
PY
    return $?
  fi
  return 1
}

say "== Aimo judge accept (junction-aimo-agents) =="
say "cwd=$ROOT"

if command -v uv >/dev/null 2>&1; then
  record PASS "uv on PATH" "$(uv --version | head -1)"
else
  record FAIL "uv on PATH" "install: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
if command -v python3 >/dev/null 2>&1; then
  record PASS "python3 on PATH" "$(python3 --version)"
else
  record FAIL "python3 on PATH" "Python 3.11+ required"
fi

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    record PASS "cp .env.example .env" "created (fill keys locally; not committed)"
  else
    record FAIL "cp .env.example .env" ".env.example missing"
  fi
else
  record PASS ".env present" "not printed"
fi

if has_env AIMO_API_KEY; then
  record PASS "AIMO_API_KEY" "set (value not printed)"
else
  record SKIP "AIMO_API_KEY" "unset — challenge data cannot be downloaded; catalog-backed tests and live milk flow are blocked"
fi
if has_env GEMINI_API_KEY; then
  record PASS "GEMINI_API_KEY" "set (value not printed)"
else
  record SKIP "GEMINI_API_KEY" "unset — uv run aimo demo is blocked; use qa_milk_flow.py when the catalog data is present"
fi

if [ "$fail" -gt 0 ]; then
  say "Prerequisites missing; stopping."
else
  say "-- pytest --"
  set +e
  uv run --extra dev pytest -q > /tmp/aimo-accept-pytest.txt 2>&1
  pec=$?
  set -e
  summary=$(grep -E "[0-9]+ failed|[0-9]+ passed" /tmp/aimo-accept-pytest.txt | tail -1 || true)
  summary=${summary:-"(no summary)"}
  if [ "$pec" -eq 0 ]; then
    record PASS "pytest" "$summary"
  elif ! has_env AIMO_API_KEY && grep -q "valio_aimo_junction26_product_data.json" /tmp/aimo-accept-pytest.txt; then
    record SKIP "pytest (full suite)" "catalog data missing without AIMO_API_KEY; see /tmp/aimo-accept-pytest.txt — $summary"
  else
    record FAIL "pytest" "exit $pec — $summary"
  fi

  say "-- aimo serve --"
  # pick a free port
  PORT=$(python3 - <<'PY'
import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()
PY
)
  set +e
  uv run aimo serve --no-build --host 127.0.0.1 --port "$PORT" > /tmp/aimo-accept-serve.txt 2>&1 &
  spid=$!
  set -e
  up=0
  for i in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${PORT}/healthz"; then up=1; break; fi
    if ! kill -0 "$spid" 2>/dev/null; then break; fi
    sleep 0.4
  done
  if [ "$up" -eq 1 ]; then
    record PASS "aimo serve starts" "http://127.0.0.1:${PORT} pid=$spid"
    for path in /.well-known/otap.json /.well-known/jwks.json /healthz /docs /ui; do
      code=$(curl -s -o /tmp/aimo-accept-curl.out -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}${path}" || echo 000)
      case "$code" in
        2*|3*) record PASS "GET $path" "HTTP $code" ;;
        *) record FAIL "GET $path" "HTTP $code" ;;
      esac
    done
    # otap fields
    if python3 - "$PORT" <<'PY'
import json,sys,urllib.request
port=sys.argv[1]
d=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/otap.json", timeout=5))
need=("name","version","protocol","openapi_url","mcp","jwks_url")
missing=[k for k in need if k not in d]
sys.exit(1 if missing else 0)
PY
    then
      record PASS "otap.json profile fields" "name/version/protocol/openapi_url/mcp/jwks_url present"
    else
      record FAIL "otap.json profile fields" "missing required keys"
    fi

    if has_env AIMO_API_KEY && [ -f data/valio_aimo_junction26_product_data.json ]; then
      set +e
      uv run python -m conformance --base "http://127.0.0.1:${PORT}" > /tmp/aimo-accept-conf.txt 2>&1
      cec=$?
      set -e
      if [ "$cec" -eq 0 ]; then
        record PASS "conformance suite" "$(tail -3 /tmp/aimo-accept-conf.txt | tr '\n' ' ')"
      else
        record FAIL "conformance suite" "exit $cec (see /tmp/aimo-accept-conf.txt)"
      fi
      set +e
      uv run python docs/pitch/qa_milk_flow.py --base "http://127.0.0.1:${PORT}" > /tmp/aimo-accept-milk.txt 2>&1
      mec=$?
      set -e
      if [ "$mec" -eq 0 ]; then
        record PASS "qa_milk_flow.py" "MILK FLOW OK"
      else
        record FAIL "qa_milk_flow.py" "exit $mec"
      fi
    else
      record SKIP "conformance suite" "needs downloaded catalog data (AIMO_API_KEY)"
      record SKIP "qa_milk_flow.py" "needs downloaded catalog data (AIMO_API_KEY)"
    fi

    if has_env GEMINI_API_KEY; then
      record SKIP "aimo demo" "key is set but live Gemini demo is not run inside this keyless-safe accept (run: uv run aimo demo --auto-approve-as anna@hotel-aurora.example)"
    else
      record SKIP "aimo demo" "GEMINI_API_KEY unset"
    fi
    kill "$spid" 2>/dev/null || true
    wait "$spid" 2>/dev/null || true
  else
    record FAIL "aimo serve starts" "did not bind; last log: $(tail -3 /tmp/aimo-accept-serve.txt | tr '\n' ' ')"
    kill "$spid" 2>/dev/null || true
  fi
fi

say ""
say "==== ACCEPT TABLE ===="
printf '%-6s  %-32s  %s\n' "STATUS" "CHECK" "DETAIL"
for r in "${rows[@]}"; do
  IFS='|' read -r st name detail <<<"$r"
  printf '%-6s  %-32s  %s\n' "$st" "$name" "$detail"
done
say "PASS=$pass  FAIL=$fail  SKIP=$skip"
if [ "$fail" -gt 0 ]; then
  say "RESULT: FAIL"
  exit 1
fi
say "RESULT: PASS"
exit 0
