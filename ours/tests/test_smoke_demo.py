import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo.app import app
from scripts import smoke_demo

ROOT = Path(__file__).resolve().parent.parent


def test_smoke_demo_inprocess(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    t0 = time.perf_counter()
    rc = smoke_demo.main(["--inprocess"])
    elapsed = time.perf_counter() - t0
    out = capsys.readouterr().out
    assert rc == 0
    assert elapsed < 2.0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 6
    for label in (
        "/health",
        "Discovery",
        "UI views",
        "Mandate auth /v1/mandate",
        "MCP /mcp",
        "Parse + Audit + Checkpoint",
    ):
        assert label in out
    assert "401 without key, 200 with demo-hotel-aurora" in out
    assert "Summary: PASS (6/6" in out


def test_smoke_demo_default_mode(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    rc = smoke_demo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 6


def test_smoke_demo_url_mode_pass(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "url.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.setattr(smoke_demo.httpx, "Client", lambda **kw: TestClient(app))
    rc = smoke_demo.main(["--url", "http://localhost:8080"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 6
    assert "mode=http://localhost:8080" in out


def test_smoke_demo_failure_exit_code(capsys, monkeypatch):
    class BrokenClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, **kw):
            return httpx.Response(500, text="err", request=httpx.Request("GET", "http://localhost:8080" + path))

        def post(self, path, **kw):
            return httpx.Response(500, text="err", request=httpx.Request("POST", "http://localhost:8080" + path))

    monkeypatch.setattr(smoke_demo.httpx, "Client", BrokenClient)
    rc = smoke_demo.main(["--url", "http://localhost:8080"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("[FAIL]") == 6
    assert "Summary: FAIL (0/6" in out


def test_mandate_auth_boundary_check(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    with TestClient(app) as c:
        detail = smoke_demo.check_mandate_auth(c)
        assert "401 without key" in detail
        assert "200 with demo-hotel-aurora" in detail

    class OpenDoorClient:
        def get(self, path, headers=None, **kw):
            return httpx.Response(
                200,
                json={"customer": "hotel-aurora", "per_order_eur": 2500.0},
                request=httpx.Request("GET", "http://localhost:8080" + path),
            )

    with pytest.raises(AssertionError, match="expected 401"):
        smoke_demo.check_mandate_auth(OpenDoorClient())

    class LockedOutClient:
        def get(self, path, headers=None, **kw):
            return httpx.Response(
                401,
                json={"code": "unauthorized"},
                request=httpx.Request("GET", "http://localhost:8080" + path),
            )

    with pytest.raises(AssertionError, match="expected 200"):
        smoke_demo.check_mandate_auth(LockedOutClient())


def test_smoke_demo_cli_filters_rfc9864_warning(tmp_path):
    env = os.environ.copy()
    env["AIMO_DATA_DIR"] = str(tmp_path / "no_data")
    proc = subprocess.run(
        [sys.executable, "-W", "default", str(ROOT / "scripts" / "smoke_demo.py"), "--inprocess"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "RFC 9864" not in proc.stdout
    assert "Summary: PASS (6/6" in proc.stdout


def test_dockerignore_entries():
    lines = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines() if line.strip()}
    for required in (
        ".git",
        ".venv",
        "data",
        "var",
        ".env",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        "var-*.log",
    ):
        assert required in lines, f"missing .dockerignore entry: {required}"


def test_dockerfile_copy_lines():
    text = (ROOT / "Dockerfile").read_text()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for expected in (
        "COPY requirements.txt .",
        "COPY aimo aimo",
        "COPY agents agents",
        "COPY scenarios scenarios",
        "COPY scripts scripts",
        "COPY web web",
        "COPY data_extra data_extra",
        "USER aimo",
        "EXPOSE 8080",
    ):
        assert expected in lines, f"missing Dockerfile instruction: {expected}"
    for env_pair in (
        "AIMO_DATA_DIR=/app/data",
        "AIMO_DB=/app/var/aimo.db",
        "AIMO_KEY_DIR=/app/var/keys",
        "AIMO_CACHE_DIR=/app/var/cache",
    ):
        assert env_pair in text
    assert not any(line.startswith("COPY .env") or line.startswith("COPY data ") for line in lines)


def test_makefile_targets():
    text = (ROOT / "Makefile").read_text()
    lines = text.splitlines()
    phony_lines = [line for line in lines if line.startswith(".PHONY:")]
    assert phony_lines, "missing .PHONY declaration"
    phony_targets = set()
    for line in phony_lines:
        phony_targets.update(line.split(":", 1)[1].split())
    for required in ("setup", "check", "run", "test", "unit", "smoke", "mcp", "scenarios", "demo", "scen", "stage"):
        assert required in phony_targets, f"missing .PHONY target: {required}"
        assert any(line.startswith(f"{required}:") for line in lines), f"missing target rule: {required}"
    assert "$(PY) scripts/smoke_demo.py --inprocess" in text


def test_deploy_docs_and_owned_hygiene():
    mcp_doc = (ROOT / "deploy" / "mcp-clients.md").read_text(encoding="utf-8")
    pub_doc = (ROOT / "deploy" / "PUBLIC_URL.md").read_text(encoding="utf-8")
    for token in ("make smoke", "smoke_demo.py", "/.well-known/jwks.json", "aimo.quotes verify", "aimo.audit verify-checkpoint"):
        assert token in mcp_doc
    for token in ("make smoke", "smoke_demo.py", "/v1/mandate", "aimo.keys rotate", "/.well-known/jwks.json"):
        assert token in pub_doc
    for rel in ("scripts/smoke_demo.py", "tests/test_smoke_demo.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "\u2014" not in src, rel
        assert "\u0023" not in src, rel
