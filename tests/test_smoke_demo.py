import time
import httpx
from fastapi.testclient import TestClient

from aimo.app import app
from scripts import smoke_demo


def test_smoke_demo_inprocess(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    t0 = time.perf_counter()
    rc = smoke_demo.main(["--inprocess"])
    elapsed = time.perf_counter() - t0
    out = capsys.readouterr().out
    assert rc == 0
    assert elapsed < 2.0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 5
    for label in ("/health", "Discovery", "UI views", "MCP /mcp", "Parse + Audit + Checkpoint"):
        assert label in out
    assert "Summary: PASS (5/5" in out


def test_smoke_demo_default_mode(capsys):
    rc = smoke_demo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 5


def test_smoke_demo_url_mode_pass(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "url.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setattr(smoke_demo.httpx, "Client", lambda **kw: TestClient(app))
    rc = smoke_demo.main(["--url", "http://localhost:8080"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == 5
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
    assert out.count("[FAIL]") == 5
    assert "Summary: FAIL (0/5" in out
