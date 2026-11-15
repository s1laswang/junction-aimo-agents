from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "judge_accept.sh"
KEY_VARS = (
    "AIMO_API_KEY",
    "VALIO_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AIMO_ENV_FILE",
    "AIMO_REPLAY",
    "AIMO_STATE_DB",
    "AIMO_KEY_DIR",
    "AIMO_APPROVER_PIN",
    "AIMO_ACCEPT_LOG_PREFIX",
)


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    if extra:
        env.update(extra)
    return env


def _run_helper(cwd: Path, body: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = f'source "$1"\n{body}'
    return subprocess.run(
        ["bash", "-c", cmd, "_", str(SCRIPT)],
        cwd=cwd,
        env=_clean_env(extra_env),
        capture_output=True,
        text=True,
        check=False,
    )


def _setup_stub_repo(tmp_path: Path) -> tuple[Path, Path]:
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    log_prefix = str(tmp_path / "accept-log-")
    patched = SCRIPT.read_text(encoding="utf-8").replace("/tmp/aimo-accept-", log_prefix)
    target_script = fake_root / "scripts" / "judge_accept.sh"
    target_script.write_text(patched, encoding="utf-8")
    target_script.chmod(target_script.stat().st_mode | stat.S_IXUSR)
    (fake_root / ".env.example").write_text("AIMO_API_KEY=\nGEMINI_API_KEY=\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_stub = fake_bin / "uv"
    uv_code = (
        "\x23!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out_dir = Path(os.environ['STUB_OUT_DIR'])\n"
        "if args == ['--version']:\n"
        "    print('uv 0.5.0')\n"
        "    sys.exit(0)\n"
        "if 'pytest' in args:\n"
        "    print('4 passed in 0.05s')\n"
        "    sys.exit(0)\n"
        "if args[:3] == ['run', 'python', '-m'] and 'scenarios.run_all' in args:\n"
        "    (out_dir / 'scenarios.json').write_text(json.dumps({\n"
        "        'state_db': os.environ.get('AIMO_STATE_DB', ''),\n"
        "        'key_dir': os.environ.get('AIMO_KEY_DIR', ''),\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "        'aimo_approver_pin': os.environ.get('AIMO_APPROVER_PIN', ''),\n"
        "    }))\n"
        "    print(os.environ.get('STUB_SCENARIOS_OUTPUT', '8/8 passed'))\n"
        "    sys.exit(int(os.environ.get('STUB_SCENARIOS_EXIT', '0')))\n"
        "if args[:2] == ['run', 'aimo'] and len(args) >= 3 and args[2] == 'serve':\n"
        "    port = int(args[args.index('--port') + 1])\n"
        "    (out_dir / 'serve.json').write_text(json.dumps({\n"
        "        'state_db': os.environ.get('AIMO_STATE_DB', ''),\n"
        "        'key_dir': os.environ.get('AIMO_KEY_DIR', ''),\n"
        "        'aimo_api_key': os.environ.get('AIMO_API_KEY', ''),\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "        'uv_project_environment': os.environ.get('UV_PROJECT_ENVIRONMENT', ''),\n"
        "        'port': port,\n"
        "    }))\n"
        "    otap = json.dumps({\n"
        "        'name': 'aimo', 'version': '0.1.0', 'protocol': 'otap/0.1',\n"
        "        'openapi_url': '/openapi.json', 'mcp': {'url': '/mcp'}, 'jwks_url': '/.well-known/jwks.json',\n"
        "    }).encode()\n"
        "    class H(BaseHTTPRequestHandler):\n"
        "        def log_message(self, *a):\n"
        "            pass\n"
        "        def do_GET(self):\n"
        "            body = otap if self.path == '/.well-known/otap.json' else b'ok'\n"
        "            self.send_response(200)\n"
        "            self.end_headers()\n"
        "            self.wfile.write(body)\n"
        "    HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
        "if args[:3] == ['run', 'python', '-m'] and 'conformance' in args:\n"
        "    (out_dir / 'conformance.json').write_text(json.dumps({\n"
        "        'args': args[3:],\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "    }))\n"
        "    print('conformance 42/42 PASS')\n"
        "    sys.exit(0)\n"
        "if 'docs/pitch/qa_milk_flow.py' in args:\n"
        "    (out_dir / 'milk.json').write_text(json.dumps({\n"
        "        'args': args,\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "    }))\n"
        "    if os.environ.get('STUB_MILK_ORDER_LINE'):\n"
        "        print(os.environ['STUB_MILK_ORDER_LINE'])\n"
        "    print('MILK FLOW OK')\n"
        "    sys.exit(int(os.environ.get('STUB_MILK_EXIT', '0')))\n"
        "if 'docs/pitch/requote_beat.py' in args:\n"
        "    (out_dir / 'requote.json').write_text(json.dumps({\n"
        "        'args': args,\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "        'aimo_approver_pin': os.environ.get('AIMO_APPROVER_PIN', ''),\n"
        "    }))\n"
        "    os.execv(sys.executable, [sys.executable, *args[2:]])\n"
        "if args[:2] == ['run', 'aimo'] and len(args) >= 3 and args[2] == 'demo':\n"
        "    (out_dir / 'demo.json').write_text(json.dumps({\n"
        "        'args': args[3:],\n"
        "        'state_db': os.environ.get('AIMO_STATE_DB', ''),\n"
        "        'key_dir': os.environ.get('AIMO_KEY_DIR', ''),\n"
        "        'aimo_api_key': os.environ.get('AIMO_API_KEY', ''),\n"
        "        'aimo_replay': os.environ.get('AIMO_REPLAY', ''),\n"
        "    }))\n"
        "    if os.environ.get('STUB_DEMO_OUTPUT'):\n"
        "        print(os.environ['STUB_DEMO_OUTPUT'])\n"
        "    sys.exit(int(os.environ.get('STUB_DEMO_EXIT', '0')))\n"
        "sys.exit(99)\n"
    )
    uv_stub.write_text(uv_code, encoding="utf-8")
    uv_stub.chmod(0o755)
    return target_script, fake_bin


def test_judge_accept_bash_syntax() -> None:
    assert SCRIPT.is_file()
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_judge_accept_no_em_dashes_no_comments_and_updated_demo_checks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "\x23!/usr/bin/env bash"
    assert all("\x23" not in line for line in lines[1:])
    assert "\u2014" not in text
    assert "\u2013" not in text
    assert "uv run aimo demo is blocked" not in text
    assert 'SKIP "GEMINI_API_KEY" "unset - aimo demo runs via scripted mock buyer"' in text
    assert (
        'SKIP "AIMO_API_KEY" "unset - keyless: replay snapshot + committed catalog subset (tests/fixtures/snapshot/replay)"'
        in text
    )
    assert "scenarios.run_all" in text
    assert 'PASS "Peppol Order Response"' in text
    assert 'PASS "audit verify"' in text
    assert 'PASS "requote_beat.py"' in text
    assert 'FAIL "requote_beat.py"' in text
    assert 'SKIP "requote_beat.py"' in text
    assert "[ -f docs/pitch/requote_beat.py ]" in text
    assert 'PASS "aimo demo (scripted buyer)"' in text
    assert 'FAIL "aimo demo (scripted buyer)"' in text
    assert "--transport fake --auto-approve-as anna@hotel-aurora.example" in text
    assert '--base-url "http://127.0.0.1:${PORT}" --auto-approve-as anna@hotel-aurora.example' in text
    assert 'AIMO_STATE_DB="$TMP_ACCEPT/state.sqlite"' in text
    assert 'AIMO_KEY_DIR="$TMP_ACCEPT/keys"' in text
    assert '--json "$TMP_ACCEPT/conformance.json"' in text
    assert "mktemp -d" in text
    assert "trap cleanup" in text


def test_helpers_with_missing_or_empty_dotenv(tmp_path: Path) -> None:
    r_missing = _run_helper(
        tmp_path,
        "sync_api_keys\n"
        "if has_aimo_key || has_gemini_key; then exit 1; fi\n"
        'test -z "${AIMO_API_KEY:-}" && test -z "${GEMINI_API_KEY:-}"',
    )
    assert r_missing.returncode == 0, r_missing.stderr
    assert r_missing.stdout == ""
    assert r_missing.stderr == ""

    (tmp_path / ".env").write_text(
        "\x23 comment line\n"
        "AIMO_API_KEY=\n"
        "VALIO_API_KEY=   \n"
        'GEMINI_API_KEY=""\n'
        "GOOGLE_API_KEY=''\n",
        encoding="utf-8",
    )
    r_empty = _run_helper(
        tmp_path,
        "sync_api_keys\n"
        "if has_aimo_key || has_gemini_key; then exit 1; fi\n"
        'test -z "${AIMO_API_KEY:-}" && test -z "${GEMINI_API_KEY:-}"',
    )
    assert r_empty.returncode == 0, r_empty.stderr
    assert r_empty.stdout == ""
    assert r_empty.stderr == ""


def test_valio_api_key_in_dotenv_aliases_to_aimo_api_key_silently(tmp_path: Path) -> None:
    secret = "valio-dummy-secret-98765"
    (tmp_path / ".env").write_text(
        f"\x23 VALIO_API_KEY=commented-out\nAIMO_API_KEY=\nVALIO_API_KEY='{secret}'\n",
        encoding="utf-8",
    )
    res = _run_helper(
        tmp_path,
        "has_aimo_key\n"
        "if has_env AIMO_API_KEY; then exit 2; fi\n"
        "has_env VALIO_API_KEY\n"
        "sync_api_keys\n"
        "has_env AIMO_API_KEY\n"
        "python3 -c 'import os, sys; sys.exit(0 if os.environ.get(\"AIMO_API_KEY\") == sys.argv[1] else 3)' "
        f"'{secret}'",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert res.stderr == ""
    assert secret not in res.stdout
    assert secret not in res.stderr


def test_google_api_key_in_dotenv_aliases_to_gemini_api_key_silently(tmp_path: Path) -> None:
    secret = "google-dummy-secret-54321"
    (tmp_path / ".env").write_text(
        f'GEMINI_API_KEY=\nexport GOOGLE_API_KEY="{secret}"\n',
        encoding="utf-8",
    )
    res = _run_helper(
        tmp_path,
        "has_gemini_key\n"
        "if has_env GEMINI_API_KEY; then exit 2; fi\n"
        "has_env GOOGLE_API_KEY\n"
        "sync_api_keys\n"
        "has_env GEMINI_API_KEY\n"
        "python3 -c 'import os, sys; sys.exit(0 if os.environ.get(\"GEMINI_API_KEY\") == sys.argv[1] else 3)' "
        f"'{secret}'",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert res.stderr == ""


def test_env_alias_from_process_env_and_primary_key_precedence(tmp_path: Path) -> None:
    res_alias = _run_helper(
        tmp_path,
        "has_aimo_key\n"
        "sync_api_keys\n"
        "python3 -c 'import os, sys; sys.exit(0 if os.environ.get(\"AIMO_API_KEY\") == \"from-env-valio\" else 1)'",
        extra_env={"VALIO_API_KEY": "  from-env-valio  "},
    )
    assert res_alias.returncode == 0, res_alias.stderr
    assert res_alias.stdout == ""

    (tmp_path / ".env").write_text(
        "AIMO_API_KEY=primary-aimo\nVALIO_API_KEY=secondary-valio\n",
        encoding="utf-8",
    )
    res_prec = _run_helper(
        tmp_path,
        "sync_api_keys\n"
        'test "${AIMO_API_KEY:-}" != "secondary-valio"\n'
        "has_aimo_key",
    )
    assert res_prec.returncode == 0, res_prec.stderr


def test_cleanup_removes_isolated_temp_dir(tmp_path: Path) -> None:
    target = tmp_path / "accept-isolated"
    target.mkdir()
    (target / "keys").mkdir()
    (target / "state.sqlite").write_text("x", encoding="utf-8")
    res = _run_helper(
        tmp_path,
        f'TMP_ACCEPT="{target}"\ncleanup',
    )
    assert res.returncode == 0, res.stderr
    assert not target.exists()


def test_main_keyless_runs_fake_demo_and_cleans_up_isolated_state(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "unset - aimo demo runs via scripted mock buyer" in res.stdout
    assert "[PASS] aimo demo (scripted buyer)" in res.stdout
    assert "RESULT: PASS" in res.stdout

    serve_info = json.loads((out_dir / "serve.json").read_text(encoding="utf-8"))
    demo_info = json.loads((out_dir / "demo.json").read_text(encoding="utf-8"))
    assert serve_info["state_db"].endswith("/state.sqlite")
    assert serve_info["key_dir"].endswith("/keys")
    assert demo_info["state_db"] == serve_info["state_db"]
    assert demo_info["key_dir"] == serve_info["key_dir"]
    assert "--transport" in demo_info["args"] and "fake" in demo_info["args"]
    assert not Path(serve_info["state_db"]).parent.exists()


def test_main_with_valio_api_key_and_catalog_runs_base_url_demo(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    fake_root = target_script.parents[1]
    secret = "valio-live-like-secret-000"
    (fake_root / ".env").write_text(f"VALIO_API_KEY={secret}\n", encoding="utf-8")
    (fake_root / "data").mkdir()
    (fake_root / "data" / "valio_aimo_junction26_product_data.json").write_text("[]", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert secret not in res.stdout
    assert secret not in res.stderr
    assert "[PASS] AIMO_API_KEY" in res.stdout
    assert "[PASS] scenarios.run_all - 8/8 passed (live)" in res.stdout
    assert "[PASS] qa_milk_flow.py - MILK FLOW OK (live)" in res.stdout
    assert "[PASS] aimo demo (scripted buyer)" in res.stdout
    assert "RESULT: PASS" in res.stdout

    scenarios_info = json.loads((out_dir / "scenarios.json").read_text(encoding="utf-8"))
    serve_info = json.loads((out_dir / "serve.json").read_text(encoding="utf-8"))
    conf_info = json.loads((out_dir / "conformance.json").read_text(encoding="utf-8"))
    demo_info = json.loads((out_dir / "demo.json").read_text(encoding="utf-8"))
    expected_conf_json = str(Path(serve_info["state_db"]).parent / "conformance.json")
    assert scenarios_info["state_db"] == ""
    assert scenarios_info["key_dir"] == ""
    assert serve_info["aimo_api_key"] == secret
    assert demo_info["aimo_api_key"] == secret
    assert conf_info["args"] == [
        "conformance",
        "--base",
        f"http://127.0.0.1:{serve_info['port']}",
        "--json",
        expected_conf_json,
    ]
    assert "--base-url" in demo_info["args"]
    assert f"http://127.0.0.1:{serve_info['port']}" in demo_info["args"]
    assert not Path(serve_info["state_db"]).parent.exists()


def test_main_keyless_with_replay_snapshot_runs_full_real_http_gates(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    fake_root = target_script.parents[1]
    replay_dir = fake_root / "tests" / "fixtures" / "snapshot" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "meta.json").write_text('{"time_now": "2026-11-15T10:00:00+02:00"}', encoding="utf-8")

    out_dir = tmp_path / "out-pass"
    out_dir.mkdir()
    order_line = (
        "ORDER ord-replay-1 placed for 2026-11-16; total_net EUR 1450.0; "
        "Peppol OrderResponseCode AP; audit verify ok=True (9 events); served_from=replay"
    )
    shared_venv = str(tmp_path / "shared-venv")
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
        "STUB_MILK_ORDER_LINE": order_line,
        "STUB_DEMO_OUTPUT": "  status=placed  latency=0.42 s  escalated=False",
        "UV_PROJECT_ENVIRONMENT": shared_venv,
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        "[SKIP] AIMO_API_KEY - unset - keyless: replay snapshot + committed catalog subset (tests/fixtures/snapshot/replay)"
        in res.stdout
    )
    assert "no AIMO_API_KEY: replay mode from tests/fixtures/snapshot/replay (served_from: replay)" in res.stdout
    assert "unset - aimo demo runs via scripted mock buyer" in res.stdout
    assert "[PASS] scenarios.run_all - 8/8 passed (replay)" in res.stdout
    assert "[PASS] conformance suite" in res.stdout
    assert "[PASS] qa_milk_flow.py - MILK FLOW OK (replay)" in res.stdout
    assert "[PASS] order placed - ord-replay-1" in res.stdout
    assert "[PASS] Peppol Order Response - OrderResponseCode AP (UBL 2.3, T116)" in res.stdout
    assert "[PASS] audit verify - ok=True (9 events)" in res.stdout
    assert f"  {order_line}" in res.stdout
    assert "[PASS] aimo demo (scripted buyer)" in res.stdout
    assert "RESULT: PASS" in res.stdout

    scenarios_info = json.loads((out_dir / "scenarios.json").read_text(encoding="utf-8"))
    serve_info = json.loads((out_dir / "serve.json").read_text(encoding="utf-8"))
    conf_info = json.loads((out_dir / "conformance.json").read_text(encoding="utf-8"))
    milk_info = json.loads((out_dir / "milk.json").read_text(encoding="utf-8"))
    demo_info = json.loads((out_dir / "demo.json").read_text(encoding="utf-8"))

    assert scenarios_info["state_db"] == ""
    assert scenarios_info["key_dir"] == ""
    assert scenarios_info["aimo_replay"] == str(replay_dir)
    assert scenarios_info["aimo_approver_pin"] != ""

    assert serve_info["aimo_replay"] == str(replay_dir)
    assert serve_info["uv_project_environment"] == shared_venv
    assert serve_info["state_db"].endswith("/state.sqlite")
    assert serve_info["key_dir"].endswith("/keys")
    assert not Path(serve_info["state_db"]).parent.exists()

    base_url = f"http://127.0.0.1:{serve_info['port']}"
    expected_conf_json = str(Path(serve_info["state_db"]).parent / "conformance.json")
    assert conf_info["args"] == ["conformance", "--base", base_url, "--json", expected_conf_json]
    assert "--base" in milk_info["args"] and base_url in milk_info["args"]
    assert "--base-url" in demo_info["args"] and base_url in demo_info["args"]
    assert "--auto-approve-as" in demo_info["args"] and "anna@hotel-aurora.example" in demo_info["args"]
    assert "--transport" not in demo_info["args"]

    out_err = tmp_path / "out-err"
    out_err.mkdir()
    env_err = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_err),
        "STUB_MILK_ORDER_LINE": order_line,
        "STUB_DEMO_EXIT": "0",
        "STUB_DEMO_OUTPUT": "  status=error  latency=0.11 s  escalated=False",
    })
    res_err = subprocess.run(["bash", str(target_script)], env=env_err, capture_output=True, text=True, check=False)
    assert res_err.returncode == 1, res_err.stdout + res_err.stderr
    assert "[FAIL] aimo demo (scripted buyer)" in res_err.stdout
    assert "status=error" in res_err.stdout
    assert "RESULT: FAIL" in res_err.stdout


def test_main_records_fail_when_scripted_demo_fails(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
        "STUB_DEMO_EXIT": "3",
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 1
    assert "[FAIL] aimo demo (scripted buyer)" in res.stdout
    assert "RESULT: FAIL" in res.stdout


def _write_requote_stub(fake_root: Path) -> Path:
    stub_path = fake_root / "docs" / "pitch" / "requote_beat.py"
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(
        "import argparse, os, sys\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--base', required=True)\n"
        "ns = ap.parse_args()\n"
        "if not ns.base.startswith('http://127.0.0.1:'):\n"
        "    sys.exit(4)\n"
        "summary = os.environ.get('STUB_REQUOTE_SUMMARY', '')\n"
        "if summary:\n"
        "    print(summary)\n"
        "marker = os.environ.get('STUB_REQUOTE_MARKER', 'REQUOTE BEAT OK')\n"
        "if marker:\n"
        "    print(marker)\n"
        "sys.exit(int(os.environ.get('STUB_REQUOTE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    return stub_path


def test_main_replay_invokes_requote_beat_and_records_pass(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    fake_root = target_script.parents[1]
    replay_dir = fake_root / "tests" / "fixtures" / "snapshot" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "meta.json").write_text('{"time_now": "2026-11-15T10:00:00+02:00"}', encoding="utf-8")
    _write_requote_stub(fake_root)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    summary = "REQUOTE ord-stale -> ord-fresh; 409 requote_needed; Peppol OrderResponseCode AP; audit verify ok=True"
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
        "STUB_REQUOTE_SUMMARY": summary,
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[PASS] requote_beat.py - 409 requote_needed -> fresh quote -> AP + audit verify (replay)" in res.stdout
    assert f"  {summary}" in res.stdout
    assert "RESULT: PASS" in res.stdout

    serve_info = json.loads((out_dir / "serve.json").read_text(encoding="utf-8"))
    requote_info = json.loads((out_dir / "requote.json").read_text(encoding="utf-8"))
    base_url = f"http://127.0.0.1:{serve_info['port']}"
    assert requote_info["args"] == ["run", "python", "docs/pitch/requote_beat.py", "--base", base_url]
    assert requote_info["aimo_replay"] == str(replay_dir)
    assert requote_info["aimo_approver_pin"] != ""


def test_main_replay_records_fail_when_requote_beat_exits_nonzero_or_omits_marker(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    fake_root = target_script.parents[1]
    replay_dir = fake_root / "tests" / "fixtures" / "snapshot" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "meta.json").write_text('{"time_now": "2026-11-15T10:00:00+02:00"}', encoding="utf-8")
    _write_requote_stub(fake_root)

    out_nonzero = tmp_path / "out-nonzero"
    out_nonzero.mkdir()
    env_nonzero = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_nonzero),
        "STUB_REQUOTE_EXIT": "2",
    })
    res_nonzero = subprocess.run(
        ["bash", str(target_script)], env=env_nonzero, capture_output=True, text=True, check=False
    )
    assert res_nonzero.returncode == 1, res_nonzero.stdout + res_nonzero.stderr
    assert "[FAIL] requote_beat.py - exit 2" in res_nonzero.stdout
    assert "RESULT: FAIL" in res_nonzero.stdout

    out_no_marker = tmp_path / "out-no-marker"
    out_no_marker.mkdir()
    env_no_marker = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_no_marker),
        "STUB_REQUOTE_EXIT": "0",
        "STUB_REQUOTE_MARKER": "",
        "STUB_REQUOTE_SUMMARY": "REQUOTE partial output without final ok line",
    })
    res_no_marker = subprocess.run(
        ["bash", str(target_script)], env=env_no_marker, capture_output=True, text=True, check=False
    )
    assert res_no_marker.returncode == 1, res_no_marker.stdout + res_no_marker.stderr
    assert "[FAIL] requote_beat.py - exit 0" in res_no_marker.stdout
    assert "RESULT: FAIL" in res_no_marker.stdout


def test_main_keyless_without_replay_records_skip_for_requote_beat(tmp_path: Path) -> None:
    target_script, fake_bin = _setup_stub_repo(tmp_path)
    fake_root = target_script.parents[1]
    _write_requote_stub(fake_root)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env = _clean_env({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_OUT_DIR": str(out_dir),
    })
    res = subprocess.run(["bash", str(target_script)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        "[SKIP] requote_beat.py - needs the catalog data (AIMO_API_KEY + uv run aimo fetch-data) or replay"
        in res.stdout
    )
    assert not (out_dir / "requote.json").exists()
    assert "RESULT: PASS" in res.stdout


def test_judge_accept_real_keyless_stack_end_to_end(tmp_path: Path) -> None:
    real_bin = tmp_path / "bin"
    real_bin.mkdir()
    uv_wrapper = real_bin / "uv"
    uv_wrapper.write_text(
        "\x23!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL_PYTHON = {sys.executable!r}\n"
        f"REPO_ROOT = {str(ROOT)!r}\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print(f'uv-wrapper ({REAL_PYTHON})')\n"
        "    sys.exit(0)\n"
        "if not args or args[0] != 'run':\n"
        "    sys.exit(98)\n"
        "rest = args[1:]\n"
        "while rest and rest[0].startswith('-'):\n"
        "    opt = rest.pop(0)\n"
        "    if opt in ('--extra', '--group', '--package', '--python', '-p', '--directory') and rest:\n"
        "        rest.pop(0)\n"
        "if not rest:\n"
        "    sys.exit(97)\n"
        "if rest[0] == 'pytest':\n"
        "    print('1 passed in 0.01s')\n"
        "    sys.exit(0)\n"
        "cur_pp = os.environ.get('PYTHONPATH', '')\n"
        "os.environ['PYTHONPATH'] = f'{REPO_ROOT}:{cur_pp}' if cur_pp else REPO_ROOT\n"
        "if os.environ.get('AIMO_ENV_FILE'):\n"
        "    for k in ('AIMO_API_KEY', 'VALIO_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY'):\n"
        "        os.environ.setdefault(k, '')\n"
        "if rest[0] in ('python', 'python3'):\n"
        "    os.execv(REAL_PYTHON, [REAL_PYTHON, *rest[1:]])\n"
        "if rest[0] == 'aimo':\n"
        "    os.execv(REAL_PYTHON, [REAL_PYTHON, '-m', 'aimo.cli', *rest[1:]])\n"
        "os.execvp(rest[0], rest)\n",
        encoding="utf-8",
    )
    uv_wrapper.chmod(0o755)

    env_file = tmp_path / ".env"
    shutil.copyfile(ROOT / ".env.example", env_file)
    dotenv_existed_before = (ROOT / ".env").exists()
    existing_conf_files = set(ROOT.glob("conformance-*.json"))

    env = _clean_env({
        "PATH": f"{real_bin}:{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "AIMO_ENV_FILE": str(env_file),
        "AIMO_DATA_DIR": str(tmp_path / "data"),
        "AIMO_ACCEPT_LOG_PREFIX": str(tmp_path / "accept-log-"),
    })
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[PASS] scenarios.run_all - 8/8 passed (replay)" in res.stdout
    assert "[PASS] conformance suite" in res.stdout
    assert "[PASS] qa_milk_flow.py - MILK FLOW OK (replay)" in res.stdout
    assert "[PASS] Peppol Order Response - OrderResponseCode AP (UBL 2.3, T116)" in res.stdout
    assert "[PASS] audit verify - ok=True" in res.stdout
    assert "[PASS] aimo demo (scripted buyer)" in res.stdout
    assert "RESULT: PASS" in res.stdout
    assert set(ROOT.glob("conformance-*.json")) == existing_conf_files
    assert list(ROOT.glob("conformance-*.json")) == []
    assert (ROOT / ".env").exists() == dotenv_existed_before
