import argparse
import json
import logging
import os
import sys
import tempfile
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ignore_rfc9864():
    warnings.filterwarnings("ignore", message=".*RFC 9864.*")
    warnings.filterwarnings("ignore", message="EdDSA is deprecated")


_ignore_rfc9864()

import httpx
from aimo import audit

EXPECTED_TOOLS = {
    "search_products",
    "get_product",
    "parse_request",
    "get_offers",
    "request_quote",
    "place_order",
    "get_order",
    "list_orders",
    "list_approvals",
    "decide_approval",
    "get_audit_trail",
    "get_mandate",
}
UI_PATHS = ["/", "/console.html", "/inbox.html", "/audit.html", "/admin.html"]
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AGENT_KEY = "demo-hotel-aurora"
CUSTOMER = "hotel-aurora"
PARSE_TEXT = "I need 1000 litres of lactose-free milk by Monday"


def _mcp_rpc(client, method, params=None, session_id=None, extra_headers=None, req_id=1):
    headers = dict(MCP_HEADERS)
    if session_id and method != "initialize":
        headers["mcp-session-id"] = session_id
    if extra_headers:
        headers.update(extra_headers)
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        headers=headers,
    )
    if r.status_code != 200:
        raise AssertionError(f"MCP {method} HTTP {r.status_code}: {r.text[:160]}")
    sid = r.headers.get("mcp-session-id") or session_id
    body = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        if not lines:
            raise AssertionError(f"MCP {method} returned empty event stream")
        body = lines[0]
    return json.loads(body), sid


def check_health(client):
    r = client.get("/health")
    if r.status_code != 200:
        raise AssertionError(f"HTTP {r.status_code}")
    body = r.json()
    if body != {"ok": True}:
        raise AssertionError(f"unexpected body {body!r}")
    return 'returns {"ok": True}'


def check_discovery(client):
    r_card = client.get("/.well-known/agent-card.json")
    if r_card.status_code != 200:
        raise AssertionError(f"agent-card.json HTTP {r_card.status_code}")
    card = r_card.json()
    card_tools = [t.get("name") for t in card.get("tools", []) if isinstance(t, dict)]
    if len(card_tools) != 12 or set(card_tools) != EXPECTED_TOOLS:
        raise AssertionError(f"agent-card tools={len(card_tools)}, expected 12")

    r_jwks = client.get("/.well-known/jwks.json")
    if r_jwks.status_code != 200:
        raise AssertionError(f"jwks.json HTTP {r_jwks.status_code}")
    jwks = r_jwks.json()
    keys_list = jwks.get("keys") or []
    if not keys_list:
        raise AssertionError("jwks.json contains no keys")
    pub = keys_list[0]
    if pub.get("alg") != "EdDSA" or pub.get("kty") != "OKP" or pub.get("crv") != "Ed25519" or not pub.get("kid") or "d" in pub:
        raise AssertionError(f"jwks.json key is not a valid EdDSA public key: {pub!r}")

    r_oa = client.get("/openapi.json")
    if r_oa.status_code != 200:
        raise AssertionError(f"openapi.json HTTP {r_oa.status_code}")
    oa = r_oa.json()
    if not oa.get("openapi") or "/v1/needs/parse" not in oa.get("paths", {}):
        raise AssertionError("openapi.json missing expected routes")
    return f"agent-card (12 tools), jwks (EdDSA kid={pub['kid'][:8]}...), openapi.json"


def check_ui(client):
    for path in UI_PATHS:
        r = client.get(path)
        if r.status_code != 200 or not r.text.strip():
            raise AssertionError(f"{path} HTTP {r.status_code}")
    return ", ".join(UI_PATHS) + " -> 200"


def check_mandate_auth(client):
    r_unauth = client.get("/v1/mandate")
    if r_unauth.status_code != 401:
        raise AssertionError(f"unauthenticated GET /v1/mandate HTTP {r_unauth.status_code}, expected 401")
    r_auth = client.get("/v1/mandate", headers={"X-Agent-Key": AGENT_KEY})
    if r_auth.status_code != 200:
        raise AssertionError(f"authenticated GET /v1/mandate HTTP {r_auth.status_code}, expected 200")
    body = r_auth.json()
    if body.get("customer") != CUSTOMER or "per_order_eur" not in body:
        raise AssertionError(f"unexpected mandate body: {body!r}")
    return f"401 without key, 200 with {AGENT_KEY} (customer={CUSTOMER}, per_order_eur={body['per_order_eur']})"


def check_mcp(client):
    init_res, sid = _mcp_rpc(
        client,
        "initialize",
        params={"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-demo", "version": "0.1.0"}},
        req_id=1,
    )
    if "result" not in init_res:
        raise AssertionError(f"initialize failed: {init_res!r}")
    init_headers = dict(MCP_HEADERS)
    if sid:
        init_headers["mcp-session-id"] = sid
    notif = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=init_headers)
    if notif.status_code not in (200, 202, 204):
        raise AssertionError(f"notifications/initialized HTTP {notif.status_code}")

    listed, sid = _mcp_rpc(client, "tools/list", session_id=sid, req_id=2)
    tools = [t.get("name") for t in listed.get("result", {}).get("tools", []) if isinstance(t, dict)]
    if len(tools) != 12 or set(tools) != EXPECTED_TOOLS:
        raise AssertionError(f"tools/list returned {len(tools)} tools")

    called, sid = _mcp_rpc(
        client,
        "tools/call",
        params={"name": "get_mandate", "arguments": {}},
        session_id=sid,
        extra_headers={"X-Agent-Key": AGENT_KEY},
        req_id=3,
    )
    res = called.get("result", {})
    if res.get("isError"):
        raise AssertionError(f"get_mandate error: {res.get('content')!r}")
    mandate = res.get("structuredContent")
    if not isinstance(mandate, dict) or "per_order_eur" not in mandate:
        texts = [c.get("text", "") for c in res.get("content", []) if isinstance(c, dict) and c.get("text")]
        if not texts:
            raise AssertionError("get_mandate returned empty content")
        mandate = json.loads(texts[0])
    if mandate.get("per_order_eur") != 2500.0:
        raise AssertionError(f"per_order_eur={mandate.get('per_order_eur')!r}, expected 2500.0")
    return "initialize + tools/list (12 tools) + get_mandate (per_order_eur=2500.0)"


def check_parse_audit_checkpoint(client):
    r_parse = client.post(
        "/v1/needs/parse",
        json={"text": PARSE_TEXT, "customer": CUSTOMER},
        headers={"X-Agent-Key": AGENT_KEY},
    )
    if r_parse.status_code != 200:
        raise AssertionError(f"POST /v1/needs/parse HTTP {r_parse.status_code}: {r_parse.text[:160]}")
    need = r_parse.json()
    if need.get("customer") != CUSTOMER or not need.get("items"):
        raise AssertionError(f"unexpected parse response: {need!r}")

    r_audit = client.get("/v1/audit", params={"customer": CUSTOMER}, headers={"X-Agent-Key": AGENT_KEY})
    if r_audit.status_code != 200:
        raise AssertionError(f"GET /v1/audit HTTP {r_audit.status_code}")
    audit_body = r_audit.json()
    events = audit_body.get("events") or []
    if not any(e.get("action") == "parse.request" and e.get("detail", {}).get("text") == PARSE_TEXT for e in events):
        raise AssertionError("audit trail missing parse.request event")
    if audit_body.get("verified", {}).get("ok") is not True:
        raise AssertionError(f"audit verified={audit_body.get('verified')!r}")

    r_jwks = client.get("/.well-known/jwks.json")
    if r_jwks.status_code != 200:
        raise AssertionError(f"jwks.json HTTP {r_jwks.status_code}")
    jwks = r_jwks.json()
    cp = audit_body.get("checkpoint") or {}
    if not cp.get("jws"):
        raise AssertionError("audit response missing checkpoint jws")
    head = audit.verify_checkpoint(cp["jws"], jwks)
    if head.get("seq") != cp.get("seq") or head.get("hash") != cp.get("hash") or head.get("seq", 0) < 1:
        raise AssertionError(f"checkpoint mismatch: {head!r} vs {cp!r}")
    return f"parse.request recorded, verified.ok=True, checkpoint seq={head['seq']} JWS valid"


CHECKS = [
    ("1. /health", check_health),
    ("2. Discovery", check_discovery),
    ("3. UI views", check_ui),
    ("4. Mandate auth /v1/mandate", check_mandate_auth),
    ("5. MCP /mcp", check_mcp),
    ("6. Parse + Audit + Checkpoint", check_parse_audit_checkpoint),
]


def run_checks(client, target):
    t0 = time.perf_counter()
    results = []
    with warnings.catch_warnings():
        _ignore_rfc9864()
        for label, fn in CHECKS:
            try:
                detail = fn(client)
                results.append((label, True, detail))
                print(f"[PASS] {label}: {detail}")
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                results.append((label, False, detail))
                print(f"[FAIL] {label}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    elapsed = time.perf_counter() - t0
    status = "PASS" if passed == total else "FAIL"
    print(f"Summary: {status} ({passed}/{total} checks in {elapsed:.2f}s, mode={target})")
    return results


def _run_inprocess():
    saved = os.environ.copy()
    prev_disable = logging.root.manager.disable
    logging.disable(logging.INFO)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["AIMO_DB"] = str(Path(tmp) / "aimo.db")
            os.environ["AIMO_KEY_DIR"] = str(Path(tmp) / "keys")
            os.environ["LLM_BASE_URL"] = ""
            os.environ["LLM_API_KEY"] = ""
            os.environ["LLM_MODEL"] = ""
            os.environ.pop("AIMO_AGENT_KEYS", None)
            from fastapi.testclient import TestClient
            from aimo.app import app
            os.environ["LLM_BASE_URL"] = ""
            os.environ["LLM_API_KEY"] = ""
            os.environ["LLM_MODEL"] = ""
            os.environ.pop("AIMO_AGENT_KEYS", None)
            with TestClient(app) as client:
                return run_checks(client, "inprocess")
        finally:
            logging.disable(prev_disable)
            os.environ.clear()
            os.environ.update(saved)


def _run_url(url):
    prev_disable = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        base = url.rstrip("/")
        with httpx.Client(base_url=base, timeout=15.0) as client:
            return run_checks(client, base)
    finally:
        logging.disable(prev_disable)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pre-stage readiness verifier for AIMO Trusted Trade Gateway.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--inprocess", action="store_true", help="Run against FastAPI TestClient with temporary DB and keys")
    group.add_argument("--url", default=None, help="Run over HTTP against a live gateway URL (e.g. http://localhost:8080)")
    args = parser.parse_args(argv)
    with warnings.catch_warnings():
        _ignore_rfc9864()
        results = _run_url(args.url) if args.url else _run_inprocess()
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
