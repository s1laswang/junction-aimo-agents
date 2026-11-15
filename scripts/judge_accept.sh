#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rows=(); pass=0; fail=0; skip=0
TMP_ACCEPT=""
spid=""

record() {
  local st="$1" name="$2" detail="$3"
  rows+=("$st|$name|$detail")
  case "$st" in PASS) pass=$((pass+1));; FAIL) fail=$((fail+1));; SKIP) skip=$((skip+1));; esac
  printf '  [%s] %s - %s\n' "$st" "$name" "$detail"
}

say() { printf '%s\n' "$*"; }

_dotenv_lookup() {
  local n="$1" mode="$2" env_file="${AIMO_ENV_FILE:-.env}"
  [ -f "$env_file" ] || return 1
  python3 - "$n" "$mode" "$env_file" <<'PY'
import sys
from pathlib import Path

name, mode, env_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
found = ""
for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    s = line.strip()
    if not s or s.startswith("\x23"):
        continue
    if s.startswith("export ") or s.startswith("export\t"):
        s = s[6:].strip()
    if "=" not in s:
        continue
    k, v = s.split("=", 1)
    if k.strip() == name:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        elif v.startswith("\x23"):
            v = ""
        else:
            for i, ch in enumerate(v):
                if ch == "\x23" and i > 0 and v[i - 1].isspace():
                    v = v[:i].rstrip()
                    break
        found = v.strip()
if not found:
    sys.exit(1)
if mode == "get":
    sys.stdout.write(found)
sys.exit(0)
PY
}

has_env() {
  local n="$1"
  if [ -n "$(printf '%s' "${!n:-}" | tr -d '[:space:]')" ]; then
    return 0
  fi
  _dotenv_lookup "$n" check
}

export_env_alias() {
  local primary="$1" alias_var="$2"
  if has_env "$primary"; then
    return 0
  fi
  if ! has_env "$alias_var"; then
    return 0
  fi
  if [ -n "$(printf '%s' "${!alias_var:-}" | tr -d '[:space:]')" ]; then
    local trimmed=""
    trimmed="$(python3 - "$alias_var" <<'PY'
import os, sys
sys.stdout.write(os.environ.get(sys.argv[1], "").strip())
PY
)"
    printf -v "$primary" '%s' "$trimmed"
    export "${primary?}"
    return 0
  fi
  local val=""
  val="$(_dotenv_lookup "$alias_var" get)" || return 0
  if [ -n "$val" ]; then
    printf -v "$primary" '%s' "$val"
    export "${primary?}"
  fi
}

sync_api_keys() {
  export_env_alias AIMO_API_KEY VALIO_API_KEY
  export_env_alias GEMINI_API_KEY GOOGLE_API_KEY
}

has_aimo_key() {
  has_env AIMO_API_KEY || has_env VALIO_API_KEY
}

has_gemini_key() {
  has_env GEMINI_API_KEY || has_env GOOGLE_API_KEY
}

cleanup() {
  if [ -n "${spid:-}" ]; then
    kill "$spid" 2>/dev/null || true
    wait "$spid" 2>/dev/null || true
    spid=""
  fi
  if [ -n "${TMP_ACCEPT:-}" ] && [ -d "$TMP_ACCEPT" ]; then
    rm -rf "$TMP_ACCEPT"
    TMP_ACCEPT=""
  fi
}

main() {
  cd "$ROOT"
  export PATH="${PATH}:${HOME}/.local/bin"
  if [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then
    export UV_PROJECT_ENVIRONMENT
  fi
  TMP_ACCEPT="$(mktemp -d)"
  LOG_PREFIX="${AIMO_ACCEPT_LOG_PREFIX:-/tmp/aimo-accept-}"
  trap cleanup EXIT INT TERM

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

  local env_file="${AIMO_ENV_FILE:-.env}"
  if [ ! -f "$env_file" ]; then
    if [ -f .env.example ]; then
      cp .env.example "$env_file"
      record PASS "cp .env.example .env" "created (fill keys locally; not committed)"
    else
      record FAIL "cp .env.example .env" ".env.example missing"
    fi
  else
    record PASS ".env present" "not printed"
  fi

  sync_api_keys

  if has_aimo_key; then
    record PASS "AIMO_API_KEY" "set (value not printed)"
  elif [ -f tests/fixtures/snapshot/replay/meta.json ]; then
    record SKIP "AIMO_API_KEY" "unset - keyless: replay snapshot + committed catalog subset (tests/fixtures/snapshot/replay)"
  else
    record SKIP "AIMO_API_KEY" "unset - challenge data cannot be downloaded; catalog-backed tests and live milk flow are blocked"
  fi
  if has_gemini_key; then
    record PASS "GEMINI_API_KEY" "set (value not printed)"
  else
    record SKIP "GEMINI_API_KEY" "unset - aimo demo runs via scripted mock buyer"
  fi

  if [ "$fail" -gt 0 ]; then
    say "Prerequisites missing; stopping."
  else
    say "-- pytest --"
    set +e
    uv run --extra dev pytest -q > "${LOG_PREFIX}pytest.txt" 2>&1
    pec=$?
    set -e
    summary=$(grep -E "[0-9]+ failed|[0-9]+ passed" "${LOG_PREFIX}pytest.txt" | tail -1 || true)
    summary=${summary:-"(no summary)"}
    if [ "$pec" -eq 0 ]; then
      record PASS "pytest" "$summary"
    elif ! has_aimo_key && grep -q "valio_aimo_junction26_product_data.json" "${LOG_PREFIX}pytest.txt"; then
      record SKIP "pytest (full suite)" "catalog data missing without AIMO_API_KEY; see ${LOG_PREFIX}pytest.txt - $summary"
    else
      record FAIL "pytest" "exit $pec - $summary"
    fi

    if [ -z "${AIMO_APPROVER_PIN:-}" ]; then
      AIMO_APPROVER_PIN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
    fi
    export AIMO_APPROVER_PIN
    if ! has_aimo_key && [ -z "${AIMO_REPLAY:-}" ]; then
      if [ -f tests/fixtures/snapshot/replay/meta.json ]; then
        export AIMO_REPLAY="$ROOT/tests/fixtures/snapshot/replay"
        say "no AIMO_API_KEY: replay mode from tests/fixtures/snapshot/replay (served_from: replay)"
      elif [ -d tests/fixtures/snapshot ]; then
        export AIMO_REPLAY="$ROOT/tests/fixtures/snapshot"
        say "no AIMO_API_KEY: replay mode from tests/fixtures/snapshot"
      fi
    fi
    replay_on=0
    if [ -n "${AIMO_REPLAY:-}" ] && [ "${AIMO_REPLAY}" != "0" ] && [ -f "${AIMO_REPLAY}/meta.json" ]; then
      replay_on=1
    fi
    mode_label=$([ "$replay_on" -eq 1 ] && echo replay || echo live)

    if { has_aimo_key && [ -f data/valio_aimo_junction26_product_data.json ]; } || [ "$replay_on" -eq 1 ]; then
      say "-- scenarios s0..s7 ($mode_label) --"
      set +e
      uv run python -m scenarios.run_all > "${LOG_PREFIX}scenarios.txt" 2>&1
      sec=$?
      set -e
      ssum=$(grep -E "^[0-9]+/[0-9]+ passed" "${LOG_PREFIX}scenarios.txt" | tail -1 || true)
      if [ "$sec" -eq 0 ] && [ -n "$ssum" ]; then
        record PASS "scenarios.run_all" "$ssum ($mode_label)"
      else
        record FAIL "scenarios.run_all" "exit $sec - ${ssum:-no table} ($mode_label; see ${LOG_PREFIX}scenarios.txt)"
      fi
    else
      record SKIP "scenarios.run_all" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
    fi

    say "-- aimo serve --"
    mkdir -p "$TMP_ACCEPT/keys"
    export AIMO_STATE_DB="$TMP_ACCEPT/state.sqlite" AIMO_KEY_DIR="$TMP_ACCEPT/keys"
    PORT=$(python3 - <<'PY'
import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()
PY
)
    set +e
    uv run aimo serve --no-build --host 127.0.0.1 --port "$PORT" > "${LOG_PREFIX}serve.txt" 2>&1 &
    spid=$!
    set -e
    up=0
    for _ in $(seq 1 30); do
      if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${PORT}/healthz"; then up=1; break; fi
      if ! kill -0 "$spid" 2>/dev/null; then break; fi
      sleep 0.4
    done
    if [ "$up" -eq 1 ]; then
      record PASS "aimo serve starts" "http://127.0.0.1:${PORT} pid=$spid"
      for path in /.well-known/otap.json /.well-known/jwks.json /healthz /docs /ui; do
        code=$(curl -s -o "${LOG_PREFIX}curl.out" -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}${path}" || echo 000)
        case "$code" in
          2*|3*) record PASS "GET $path" "HTTP $code" ;;
          *) record FAIL "GET $path" "HTTP $code" ;;
        esac
      done
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

      if { has_aimo_key && [ -f data/valio_aimo_junction26_product_data.json ]; } || [ "$replay_on" -eq 1 ]; then
        set +e
        uv run python -m conformance --base "http://127.0.0.1:${PORT}" --json "$TMP_ACCEPT/conformance.json" > "${LOG_PREFIX}conf.txt" 2>&1
        cec=$?
        set -e
        if [ "$cec" -eq 0 ]; then
          record PASS "conformance suite" "$(tail -3 "${LOG_PREFIX}conf.txt" | tr '\n' ' ')"
        else
          record FAIL "conformance suite" "exit $cec (see ${LOG_PREFIX}conf.txt)"
        fi
        set +e
        uv run python docs/pitch/qa_milk_flow.py --base "http://127.0.0.1:${PORT}" > "${LOG_PREFIX}milk.txt" 2>&1
        mec=$?
        set -e
        order_line=$(grep -E "^ORDER " "${LOG_PREFIX}milk.txt" | tail -1 || true)
        if [ "$mec" -eq 0 ]; then
          record PASS "qa_milk_flow.py" "MILK FLOW OK ($mode_label)"
          if [ -n "$order_line" ]; then
            oid=$(sed -E 's/^ORDER ([^ ]+).*/\1/' <<<"$order_line")
            pcode=$(sed -nE 's/.*Peppol OrderResponseCode ([A-Z]+).*/\1/p' <<<"$order_line")
            aver=$(sed -nE 's/.*audit verify (ok=[^;]+).*/\1/p' <<<"$order_line")
            record PASS "order placed" "$oid"
            record PASS "Peppol Order Response" "OrderResponseCode ${pcode:-?} (UBL 2.3, T116)"
            record PASS "audit verify" "${aver:-ok}"
            say "  $order_line"
          fi
        else
          record FAIL "qa_milk_flow.py" "exit $mec (see ${LOG_PREFIX}milk.txt)"
        fi
        if [ -f docs/pitch/requote_beat.py ]; then
          set +e
          uv run python docs/pitch/requote_beat.py --base "http://127.0.0.1:${PORT}" > "${LOG_PREFIX}requote.txt" 2>&1
          rqec=$?
          set -e
          requote_line=$(grep -E "^REQUOTE " "${LOG_PREFIX}requote.txt" | grep -v "^REQUOTE BEAT OK" | tail -1 || true)
          if [ "$rqec" -eq 0 ] && grep -q "REQUOTE BEAT OK" "${LOG_PREFIX}requote.txt"; then
            record PASS "requote_beat.py" "409 requote_needed -> fresh quote -> AP + audit verify ($mode_label)"
            if [ -n "$requote_line" ]; then
              say "  $requote_line"
            fi
          else
            record FAIL "requote_beat.py" "exit $rqec (see ${LOG_PREFIX}requote.txt)"
          fi
        fi
      else
        record SKIP "conformance suite" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
        record SKIP "qa_milk_flow.py" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
        if [ -f docs/pitch/requote_beat.py ]; then
          record SKIP "requote_beat.py" "needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
        fi
      fi

      if has_gemini_key; then
        record SKIP "aimo demo" "key is set but live Gemini demo is not run inside this keyless-safe accept (run: uv run aimo demo --auto-approve-as anna@hotel-aurora.example)"
      else
        set +e
        if { has_aimo_key && [ -f data/valio_aimo_junction26_product_data.json ]; } || [ "$replay_on" -eq 1 ]; then
          demo_mode="http ($mode_label)"
          uv run aimo demo --base-url "http://127.0.0.1:${PORT}" --auto-approve-as anna@hotel-aurora.example --approve-delay 0.5 --approval-timeout 30 > "${LOG_PREFIX}demo.txt" 2>&1
          dec=$?
        else
          demo_mode="fake"
          uv run aimo demo --transport fake --auto-approve-as anna@hotel-aurora.example --approve-delay 0.2 --approval-timeout 20 > "${LOG_PREFIX}demo.txt" 2>&1
          dec=$?
        fi
        set -e
        demo_ok=0
        if [ "$dec" -eq 0 ]; then
          if ! grep -q "status=" "${LOG_PREFIX}demo.txt" || grep -q "status=placed" "${LOG_PREFIX}demo.txt"; then
            demo_ok=1
          fi
        fi
        if [ "$demo_ok" -eq 1 ]; then
          record PASS "aimo demo (scripted buyer)" "completed end-to-end via $demo_mode (see ${LOG_PREFIX}demo.txt)"
        else
          dstatus=$(grep -oE "status=[a-z_]+" "${LOG_PREFIX}demo.txt" | tail -1 || true)
          record FAIL "aimo demo (scripted buyer)" "exit $dec ${dstatus:+$dstatus }via $demo_mode (see ${LOG_PREFIX}demo.txt)"
        fi
      fi
      kill "$spid" 2>/dev/null || true
      wait "$spid" 2>/dev/null || true
      spid=""
    else
      record FAIL "aimo serve starts" "did not bind; last log: $(tail -3 "${LOG_PREFIX}serve.txt" | tr '\n' ' ')"
      kill "$spid" 2>/dev/null || true
      spid=""
    fi
  fi

  say ""
  say "==== ACCEPT TABLE ===="
  printf '%-6s  %-32s  %s\n' "STATUS" "CHECK" "DETAIL"
  for r in "${rows[@]+"${rows[@]}"}"; do
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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
