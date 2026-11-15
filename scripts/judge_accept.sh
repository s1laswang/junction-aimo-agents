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

# per-run log folder: two accept runs in parallel must not overwrite each other's logs
ACCEPT_TMP="$(mktemp -d "${TMPDIR:-/tmp}/aimo-accept.XXXXXX")"
say "== Aimo judge accept (junction-aimo-agents) =="
say "logs=$ACCEPT_TMP"
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
  record SKIP "AIMO_API_KEY" "unset — keyless: replay snapshot + committed catalog subset (tests/fixtures/snapshot/replay)"
fi
if has_env GEMINI_API_KEY; then
  record PASS "GEMINI_API_KEY" "set (value not printed)"
else
  record SKIP "GEMINI_API_KEY" "unset — buyer agent runs the labelled mock model; deterministic planner unchanged"
fi

if [ "$fail" -gt 0 ]; then
  say "Prerequisites missing; stopping."
else
  say "-- pytest --"
  set +e
  uv run --extra dev pytest -q > $ACCEPT_TMP/aimo-accept-pytest.txt 2>&1
  pec=$?
  set -e
  summary=$(grep -E "[0-9]+ failed|[0-9]+ passed" $ACCEPT_TMP/aimo-accept-pytest.txt | tail -1 || true)
  summary=${summary:-"(no summary)"}
  if [ "$pec" -eq 0 ]; then
    if has_env AIMO_API_KEY; then
      record PASS "pytest" "$summary"
    else
      record PASS "pytest (keyless)" "$summary; tests marked needs_data skip without the challenge catalog"
    fi
  elif ! has_env AIMO_API_KEY && grep -q "valio_aimo_junction26_product_data.json" $ACCEPT_TMP/aimo-accept-pytest.txt; then
    # keyless failures are real: a test that needs the full catalog must carry @pytest.mark.needs_data
    record FAIL "pytest (keyless)" "exit $pec — a test needs the full catalog but is not marked needs_data; see $ACCEPT_TMP/aimo-accept-pytest.txt — $summary"
  else
    record FAIL "pytest" "exit $pec — $summary"
  fi

  say "-- aimo serve --"
  # one human-approver credential shared by the gateway and the acceptance scripts (never given to agents)
  if [ -z "${AIMO_APPROVER_PIN:-}" ]; then
    AIMO_APPROVER_PIN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  fi
  export AIMO_APPROVER_PIN
  # keyless: serve the committed replay snapshot (recorded once from the live mock API by
  # scripts/record_replay.py) plus the committed catalog subset instead of the live API and the 87 MB file
  if ! has_env AIMO_API_KEY && [ -z "${AIMO_REPLAY:-}" ] && [ -f tests/fixtures/snapshot/replay/meta.json ]; then
    export AIMO_REPLAY="$ROOT/tests/fixtures/snapshot/replay"
    say "no AIMO_API_KEY: replay mode from tests/fixtures/snapshot/replay (served_from: replay)"
  fi
  replay_on=0
  if [ -n "${AIMO_REPLAY:-}" ] && [ "${AIMO_REPLAY}" != "0" ]; then replay_on=1; fi
  mode_label=$([ "$replay_on" -eq 1 ] && echo replay || echo live)

  say "-- scenarios s0..s7 ($mode_label) --"
  set +e
  uv run python -m scenarios.run_all > $ACCEPT_TMP/aimo-accept-scenarios.txt 2>&1
  sec=$?
  set -e
  ssum=$(grep -E "^[0-9]+/[0-9]+ passed" $ACCEPT_TMP/aimo-accept-scenarios.txt | tail -1 || true)
  if [ "$sec" -eq 0 ] && [ -n "$ssum" ]; then
    record PASS "scenarios.run_all" "$ssum ($mode_label)"
  else
    record FAIL "scenarios.run_all" "exit $sec — ${ssum:-no table} ($mode_label; see $ACCEPT_TMP/aimo-accept-scenarios.txt)"
  fi
  # pick a free port
  PORT=$(python3 - <<'PY'
import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()
PY
)
  set +e
  uv run aimo serve --no-build --host 127.0.0.1 --port "$PORT" > $ACCEPT_TMP/aimo-accept-serve.txt 2>&1 &
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
      code=$(curl -s -o $ACCEPT_TMP/aimo-accept-curl.out -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}${path}" || echo 000)
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

    if { has_env AIMO_API_KEY && [ -f data/valio_aimo_junction26_product_data.json ]; } || [ "$replay_on" -eq 1 ]; then
      set +e
      uv run python -m conformance --base "http://127.0.0.1:${PORT}" > $ACCEPT_TMP/aimo-accept-conf.txt 2>&1
      cec=$?
      set -e
      if [ "$cec" -eq 0 ]; then
        record PASS "conformance suite" "$(tail -3 $ACCEPT_TMP/aimo-accept-conf.txt | tr '\n' ' ')"
      else
        record FAIL "conformance suite" "exit $cec (see $ACCEPT_TMP/aimo-accept-conf.txt)"
      fi
      set +e
      uv run python docs/pitch/qa_milk_flow.py --base "http://127.0.0.1:${PORT}" > $ACCEPT_TMP/aimo-accept-milk.txt 2>&1
      mec=$?
      set -e
      order_line=$(grep -E "^ORDER " $ACCEPT_TMP/aimo-accept-milk.txt | tail -1 || true)
      if [ "$mec" -eq 0 ] && [ -n "$order_line" ]; then
        record PASS "qa_milk_flow.py" "MILK FLOW OK ($mode_label)"
        oid=$(sed -E 's/^ORDER ([^ ]+).*/\1/' <<<"$order_line")
        pcode=$(sed -nE 's/.*Peppol OrderResponseCode ([A-Z]+).*/\1/p' <<<"$order_line")
        aver=$(sed -nE 's/.*audit verify (ok=[A-Za-z]+[^;]*);.*/\1/p' <<<"$order_line")
        record PASS "order placed" "$oid"
        record PASS "Peppol Order Response" "OrderResponseCode ${pcode:-?} (UBL 2.3, T116)"
        record PASS "audit verify" "${aver:-ok}"
        say "  $order_line"
      else
        record FAIL "qa_milk_flow.py" "exit $mec (see $ACCEPT_TMP/aimo-accept-milk.txt)"
      fi
    else
      record SKIP "conformance suite" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
      record SKIP "qa_milk_flow.py" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
    fi

    if has_env GEMINI_API_KEY; then
      record SKIP "aimo demo" "key is set but live Gemini demo is not run inside this keyless-safe accept (run: uv run aimo demo --auto-approve-as anna@hotel-aurora.example)"
    else
      record SKIP "aimo demo" "GEMINI_API_KEY unset"
    fi
    kill "$spid" 2>/dev/null || true
    wait "$spid" 2>/dev/null || true
  else
    record FAIL "aimo serve starts" "did not bind; last log: $(tail -3 $ACCEPT_TMP/aimo-accept-serve.txt | tr '\n' ' ')"
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
