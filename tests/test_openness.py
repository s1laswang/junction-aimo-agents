"""Openness proof, fully offline: boot the synthetic second wholesaler (Nordic Bakery Supply, static backend,
own key) and run the conformance suite against it; check the independent verifiers and the offline script.

    uv run pytest -q tests/test_openness.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _port() -> int:
    for p in range(8860, 8940):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port")


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    d = tmp_path_factory.mktemp("toy")
    port = _port()
    log = open(d / "server.log", "w+")
    env = {**os.environ, "AIMO_DATA_DIR": str(d), "GEMINI_API_KEY": ""}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "toywholesaler.app:app", "--port", str(port)],
                           env=env, stdout=log, stderr=log, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    else:
        log.seek(0)
        srv.kill()
        pytest.fail("toy wholesaler did not start:\n" + log.read()[-2000:])
    yield base, d
    srv.terminate()
    srv.wait(10)


def test_conformance_passes_on_second_wholesaler(toy):
    from conformance.__main__ import run

    base, _ = toy
    rep = run(base, query="rye bread", quiet=True)
    fails = [r for r in rep["results"] if r["status"] != "PASS"]
    assert not fails, fails
    assert rep["name"] == "Nordic Bakery Supply Trusted Trade Gateway" and rep["summary"]["pass"] >= 40


def test_second_wholesaler_has_its_own_key_and_policy(toy):
    base, d = toy
    jwks = httpx.get(base + "/.well-known/jwks.json").json()
    assert (Path(d) / "keys").exists()
    otap = httpx.get(base + "/.well-known/otap.json").json()
    assert otap["mcp"]["server_name"] == "nordic-bakery-supply-trusted-trade" and otap["protocol"] == "otap/0.1"
    cust = {c["customer_id"]: c for c in httpx.get(base + "/v1/customers").json()}
    assert cust["hotel-aurora"]["policy_version"] == "1.0.0-nbs"
    assert [p["product_id"] for p in cust["hotel-aurora"]["preferred_products"]] == ["NB1001"]
    assert jwks["keys"][0]["kid"]


def test_offline_verifier_script(toy, tmp_path):
    base, _ = toy
    h = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
    q = httpx.post(base + "/v1/quotes", headers=h, json={"delivery_date": "2026-12-02",
                                                        "lines": [{"product_id": "NB1001", "qty_sales_units": 2}]}).json()
    (tmp_path / "q.json").write_text(json.dumps(q))
    (tmp_path / "jwks.json").write_text(httpx.get(base + "/.well-known/jwks.json").text)
    script = str(ROOT / "scripts" / "verify_quote.py")
    ok = subprocess.run([sys.executable, "-I", script, str(tmp_path / "q.json"), str(tmp_path / "jwks.json")],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and "VERIFIED" in ok.stdout, ok.stdout + ok.stderr
    (tmp_path / "bad.json").write_text(json.dumps({**q, "total_net": 0.01}))
    bad = subprocess.run([sys.executable, "-I", script, str(tmp_path / "bad.json"), str(tmp_path / "jwks.json")],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and "FAILED" in bad.stdout
    events = httpx.get(base + "/v1/audit").json()
    (tmp_path / "ev.json").write_text(json.dumps(events))
    ev = subprocess.run([sys.executable, "-I", script, str(tmp_path / "ev.json"), base + "/.well-known/jwks.json"],
                        capture_output=True, text=True)
    assert ev.returncode == 0 and "VERIFIED" in ev.stdout, ev.stdout[-500:]


def test_independent_verifier_matches_trust_primitives():
    from conformance import verify as V
    from aimo.trust import signing

    obj = {"b": 1.0, "a": [2.50, {"z": "ä", "y": 3.0}], "c": None}
    assert V.canonical_json(obj) == signing.canonical_json(obj)
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert V.thumbprint(jwk) == signing.thumbprint(jwk)
    with pytest.raises(ValueError):
        V.verify_jws("eyJhbGciOiJub25lIn0.e30.", {"keys": []})   # alg none refused
