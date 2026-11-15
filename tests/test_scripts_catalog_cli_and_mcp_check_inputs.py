from __future__ import annotations

import asyncio
import base64
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx
import pytest

from aimo import config
import aimo.catalog.__main__ as catalog_cli
from aimo.gateway import mcp_check
from aimo.models import Candidate, Need, Plan, ProductSummary

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
LIVE_PACK_DIR = ROOT / "docs" / "evidence" / "live-run"


def _load_script(module_name: str, filename: str) -> Any:
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch_data_mod = _load_script("w6_fetch_data_mod", "fetch_data.py")
verify_quote_mod = _load_script("w6_verify_quote_mod", "verify_quote.py")
export_peppol_mod = _load_script("w6_export_peppol_mod", "export_peppol_order_response.py")


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_ed25519_signer(kid: str = "test-kid-1") -> tuple[Ed25519PrivateKey, dict[str, Any]]:
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": _b64u(pub_bytes)}]}
    return priv, jwks


def _sign_jws(priv: Ed25519PrivateKey, kid: str, payload: dict[str, Any]) -> str:
    hdr = _b64u(json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":")).encode("utf-8"))
    pl = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64u(priv.sign(f"{hdr}.{pl}".encode("ascii")))
    return f"{hdr}.{pl}.{sig}"


def test_w6_files_have_zero_em_dashes_and_clean_comment_hygiene() -> None:
    self_src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert "\u2014" not in self_src
    assert "\u2013" not in self_src

    checked_py = (
        ROOT / "aimo" / "catalog" / "__main__.py",
        ROOT / "aimo" / "gateway" / "mcp_check.py",
        SCRIPTS_DIR / "fetch_data.py",
        SCRIPTS_DIR / "verify_quote.py",
        SCRIPTS_DIR / "export_peppol_order_response.py",
    )
    for py_file in checked_py:
        txt = py_file.read_text(encoding="utf-8")
        assert "\u2014" not in txt, py_file
        assert "\u2013" not in txt, py_file

    assert "\x23" not in (SCRIPTS_DIR / "verify_quote.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_argv",
    [
        True,
        False,
        "build",
        b"build",
        bytearray(b"build"),
        123,
        {"cmd": "build"},
        [True],
        [None],
        [123],
        ["search", "milk\x00bad"],
        ["search", "milk\ud800bad"],
    ],
)
def test_catalog_cli_clean_argv_rejects_bad_containers_and_items(bad_argv: Any) -> None:
    with pytest.raises(ValueError):
        catalog_cli.main(bad_argv)


@pytest.mark.parametrize("help_args", [[], ["help"], ["-h"], ["--help"], ["  help  "]])
def test_catalog_cli_help_prints_docstring(help_args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    catalog_cli.main(help_args)
    out = capsys.readouterr().out
    assert "Catalog CLI" in out


@pytest.mark.parametrize("bad_cmd", ["", "   ", "\n", "build\x00", "build\x1f", "unknown", "drop-db"])
def test_catalog_cli_rejects_blank_control_or_unknown_commands(bad_cmd: str) -> None:
    with pytest.raises(ValueError):
        catalog_cli.main([bad_cmd])


def test_catalog_cli_build_validates_flags_and_skips_gemini_keyless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ValueError, match="unknown build option"):
        catalog_cli.main(["build", "--bogus"])

    calls: list[str] = []
    monkeypatch.setattr("aimo.backend.catalog_store.load_catalog", lambda: calls.append("load_catalog"))
    monkeypatch.setattr("aimo.catalog.embeddings.available", lambda: False)
    monkeypatch.setattr("aimo.catalog.embeddings.build", lambda: calls.append("embed_build"))
    fake_cache = tmp_path / "missing_allergens.json"
    monkeypatch.setattr("aimo.catalog.gemini_allergens.CACHE", fake_cache)
    monkeypatch.setattr("aimo.catalog.gemini_allergens.precompute", lambda: calls.append("allergen_precompute"))

    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)

    catalog_cli.main(["build"])
    out = capsys.readouterr().out
    assert calls == ["load_catalog"]
    assert "catalog.sqlite ready" in out
    assert "embeddings skipped (GEMINI_API_KEY not set)" in out
    assert "allergen hints skipped (GEMINI_API_KEY not set)" in out

    calls.clear()
    monkeypatch.setattr(config, "GEMINI_API_KEY", "valid-key")
    monkeypatch.setenv("GEMINI_API_KEY", "valid-key")
    catalog_cli.main(["build", "--no-build"])
    out_nb = capsys.readouterr().out
    assert calls == ["load_catalog"]
    assert "skipped (--no-build)" in out_nb

    calls.clear()
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    catalog_cli.main(["build"])
    out_off = capsys.readouterr().out
    assert calls == ["load_catalog"]
    assert "AIMO_OFFLINE=1" in out_off

    calls.clear()
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "bad\nkey")
    monkeypatch.setenv("GEMINI_API_KEY", "bad\nkey")
    catalog_cli.main(["build"])
    out_bad_key = capsys.readouterr().out
    assert calls == ["load_catalog"]
    assert "GEMINI_API_KEY not set" in out_bad_key

    calls.clear()
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clean-key")
    monkeypatch.setenv("GEMINI_API_KEY", "clean-key")
    catalog_cli.main(["build", "--force"])
    assert calls == ["load_catalog", "embed_build", "allergen_precompute"]


def test_catalog_cli_search_and_demo_guard_bad_inputs_and_edge_scores(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for bad_query in (["search", "   "], ["search", "milk\n"], ["search", "milk\x7f"], ["search", "x" * 501]):
        with pytest.raises(ValueError):
            catalog_cli.main(bad_query)

    seen_queries: list[str] = []

    def _fake_search(q: str, limit: int = 15) -> list[Any]:
        seen_queries.append(q)
        return [
            SimpleNamespace(id="P1", score=None, content_amount=1.0, content_unit="L", sales_unit="ST", name="Milk A"),
            SimpleNamespace(
                id="P2",
                score=float("nan"),
                content_amount=10.0,
                content_unit="L",
                sales_unit="BOX",
                name="Milk B",
            ),
            SimpleNamespace(id="P3", score=0.912, content_amount=1.0, content_unit="L", sales_unit="ST", name="Milk C"),
        ]

    monkeypatch.setattr("aimo.catalog.search", _fake_search)
    catalog_cli.main(["search"])
    catalog_cli.main(["search", "  oat", "drink  "])
    out = capsys.readouterr().out
    assert seen_queries == ["lactose-free milk", "oat drink"]
    assert "P1        -" in out
    assert "P2        -" in out
    assert "0.912" in out

    with pytest.raises(ValueError, match="demo takes no extra arguments"):
        catalog_cli.main(["demo", "unexpected"])

    prod = ProductSummary(id="464306", name="Milk", sales_unit="ST", content_amount=1.0, content_unit="L")
    cand = Candidate(product=prod, tier="best", rank_score=0.85, flags=[], reasons=[])
    fake_plan = Plan(
        need=Need(customer_id="hotel-aurora", product_query="lactose-free milk", quantity=1000.0, unit="L", delivery_by=date(2026, 11, 16)),
        delivery_date=date(2026, 11, 16),
        lines=[],
        candidates=[cand],
        covered_amount=1000.0,
        coverage_ratio=1.0,
        total_net=1010.0,
        explanation="Plan ready",
    )
    monkeypatch.setattr("aimo.catalog.plan_need", lambda _need: fake_plan)
    catalog_cli.main(["demo"])
    demo_out = capsys.readouterr().out
    assert "Plan ready" in demo_out
    assert "464306" in demo_out


def test_catalog_cli_subprocess_exits_2_without_traceback_on_bad_command() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "aimo.catalog", "not-a-subcommand"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert res.returncode == 2
    assert "error:" in res.stderr
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize(
    "valid_target",
    [
        "stdio",
        "http://localhost:8000/mcp",
        "https://gateway.example.com:8443/mcp",
        "http://127.0.0.1:18080/mcp",
        "http://[::1]:8000/mcp",
    ],
)
def test_mcp_check_validate_target_accepts_valid_targets(valid_target: str) -> None:
    assert mcp_check._validate_target(valid_target) == valid_target


@pytest.mark.parametrize(
    "bad_target",
    [
        None,
        True,
        False,
        8000,
        ["stdio"],
        "",
        "   ",
        "stdio ",
        " stdio",
        "stdio\n",
        "stdio\x00",
        "http://localhost:8000/mcp\ud800",
        "ftp://localhost:8000/mcp",
        "ws://localhost:8000/mcp",
        "file:///etc/passwd",
        "http:///mcp",
        "http://:8000/mcp",
        "http://.badhost/mcp",
        "http://bad..host/mcp",
        "http://user:pass@localhost:8000/mcp",
        "http://user@localhost:8000/mcp",
        "http://localhost:0/mcp",
        "http://localhost:65536/mcp",
        "http://localhost:notaport/mcp",
        "http://localhost:8000/mcp extra",
        "http://localhost:8000/" + ("a" * 2100),
    ],
)
def test_mcp_check_validate_target_rejects_bad_targets(bad_target: Any) -> None:
    with pytest.raises(ValueError):
        mcp_check._validate_target(bad_target)


def test_mcp_check_text_and_main_handle_edge_payloads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert mcp_check._text(None) == ""
    assert mcp_check._text(SimpleNamespace(content="not-a-list")) == ""
    assert "464306" in mcp_check._text(SimpleNamespace(structured_content={"id": "464306"}, content=[]))
    assert "ok 42" == mcp_check._text(
        SimpleNamespace(structured_content=None, content=[None, SimpleNamespace(text="ok"), SimpleNamespace(text=42)])
    )

    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        def __init__(self, server: Any) -> None:
            self.server = server

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def list_tools(self) -> Any:
            return SimpleNamespace(tools=[SimpleNamespace(name="search_products"), SimpleNamespace(name="get_offers")])

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            calls.append((name, args))
            return SimpleNamespace(structured_content={"tool": name}, content=[])

    monkeypatch.setattr(mcp_check, "Client", _FakeClient)
    asyncio.run(mcp_check.main("http://localhost:8000/mcp"))
    out = capsys.readouterr().out
    assert "2 tools: search_products, get_offers" in out
    assert [c[0] for c in calls] == ["search_products", "get_offers"]
    date.fromisoformat(calls[1][1]["requested_date"])


def test_mcp_check_subprocess_rejects_bad_cli_args_with_exit_2() -> None:
    too_many = subprocess.run(
        [sys.executable, "-m", "aimo.gateway.mcp_check", "stdio", "extra"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert too_many.returncode == 2
    assert "usage:" in too_many.stderr

    bad_url = subprocess.run(
        [sys.executable, "-m", "aimo.gateway.mcp_check", "ftp://evil.example/mcp"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert bad_url.returncode == 2
    assert "error:" in bad_url.stderr
    assert "Traceback" not in bad_url.stderr


def test_fetch_data_url_origin_endpoint_and_dest_validation(tmp_path: Path) -> None:
    for bad_url in (
        None,
        True,
        123,
        "",
        "   ",
        "ftp://junction26.valioaimo.fi/api/products/download",
        "javascript:alert(1)",
        "https://aimo@evil.example/file.json",
        "https://user:secret@junction26.valioaimo.fi/file.json",
        "https://..evil.example/file.json",
        "https://.evil.example/file.json",
        "https://junction26.valioaimo.fi:0/file.json",
        "https://junction26.valioaimo.fi:70000/file.json",
        "https://junction26.valioaimo.fi:abc/file.json",
        "https://junction26.valioaimo.fi/file\n.json",
        "https://junction26.valioaimo.fi/file name.json",
    ):
        with pytest.raises(ValueError):
            fetch_data_mod._validate_http_url(bad_url)

    base = "https://junction26.valioaimo.fi"
    assert fetch_data_mod._same_origin_for_auth("https://junction26.valioaimo.fi/api/x", base) is True
    assert fetch_data_mod._same_origin_for_auth("https://JUNCTION26.VALIOAIMO.FI:443/api/x", base) is True
    assert fetch_data_mod._same_origin_for_auth("http://junction26.valioaimo.fi/api/x", base) is False
    assert fetch_data_mod._same_origin_for_auth("https://junction26.valioaimo.fi:8443/api/x", base) is False
    assert fetch_data_mod._same_origin_for_auth("https://cdn.example.com/bucket/file.json", base) is False

    for bad_ep in (None, True, "", "api/products", "//evil.com/x", "/api/../secret", "/api/bad path", "/api/x\r\n"):
        with pytest.raises(ValueError):
            fetch_data_mod._validate_endpoint(bad_ep)

    existing_dir = tmp_path / "dir_dest"
    existing_dir.mkdir()
    for bad_dest in (None, True, "", "   ", ".", "..", "file\x00.json", existing_dir):
        with pytest.raises(ValueError):
            fetch_data_mod._validate_dest(bad_dest)


def test_fetch_data_download_redirects_never_leak_api_key_and_reject_bad_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen_requests: list[tuple[str, dict[str, str]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_requests.append((str(req.url), dict(req.headers)))
        url = str(req.url)
        if url == "https://junction26.valioaimo.fi/api/products/download":
            return httpx.Response(302, headers={"Location": "https://storage.example.org/signed/products.json"})
        if url == "https://storage.example.org/signed/products.json":
            return httpx.Response(200, content=b'[{"id":"464306"}]', headers={"Content-Type": "application/octet-stream"})
        if url == "https://junction26.valioaimo.fi/api/co2/download":
            return httpx.Response(
                200,
                content=b'{"downloadUrl": "https://cdn.example.org/co2.csv"}',
                headers={"Content-Type": "application/json"},
            )
        if url == "https://cdn.example.org/co2.csv":
            return httpx.Response(200, content=b"id,co2\n464306,0.9\n", headers={"Content-Type": "text/csv"})
        if url == "https://junction26.valioaimo.fi/api/empty/download":
            return httpx.Response(200, content=b"")
        if url == "https://junction26.valioaimo.fi/api/loop/download":
            return httpx.Response(302, headers={"Location": "/api/loop/download"})
        if url == "https://junction26.valioaimo.fi/api/badloc/download":
            return httpx.Response(302, headers={"Location": "file:///etc/passwd"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(config, "AIMO_BASE_URL", "https://junction26.valioaimo.fi")
    monkeypatch.setattr(config, "AIMO_API_KEY", "super-secret-aimo-key")

    dest_json = tmp_path / "products.json"
    fetch_data_mod._download("/api/products/download", dest_json)
    assert dest_json.read_bytes() == b'[{"id":"464306"}]'
    assert seen_requests[0][1].get("x-api-key") == "super-secret-aimo-key"
    assert "x-api-key" not in {k.lower() for k in seen_requests[1][1]}

    seen_requests.clear()
    dest_csv = tmp_path / "co2.csv"
    fetch_data_mod._download("/api/co2/download", dest_csv)
    assert dest_csv.read_text(encoding="utf-8").startswith("id,co2")
    assert seen_requests[0][1].get("x-api-key") == "super-secret-aimo-key"
    assert "x-api-key" not in {k.lower() for k in seen_requests[1][1]}

    with pytest.raises(ValueError, match="empty payload"):
        fetch_data_mod._download("/api/empty/download", tmp_path / "empty.json")
    assert not (tmp_path / "empty.json").exists()
    assert not (tmp_path / "empty.json.part").exists()

    with pytest.raises(ValueError, match="too many redirects"):
        fetch_data_mod._download("/api/loop/download", tmp_path / "loop.json")

    with pytest.raises(ValueError):
        fetch_data_mod._download("/api/badloc/download", tmp_path / "badloc.json")

    monkeypatch.setattr(config, "AIMO_API_KEY", "bad\r\nkey")
    with pytest.raises(ValueError, match="control characters"):
        fetch_data_mod._download("/api/products/download", tmp_path / "ctrl.json")


def test_fetch_data_ensure_data_and_main_bad_inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fetch_data_mod.ensure_data(force="yes")
    with pytest.raises(ValueError):
        fetch_data_mod.ensure_data(quiet=1)
    with pytest.raises(ValueError):
        fetch_data_mod.main("not-a-list")
    with pytest.raises(ValueError):
        fetch_data_mod.main(["--force\x00"])

    f1 = tmp_path / "p.json"
    f1.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fetch_data_mod, "FILES", [("/api/products/download", f1)])
    monkeypatch.setattr(config, "AIMO_API_KEY", "   ")
    assert fetch_data_mod.ensure_data(force=False, quiet=True) == []
    with pytest.raises(SystemExit):
        fetch_data_mod.ensure_data(force=True, quiet=True)


@pytest.mark.parametrize(
    "empty_or_bad_doc",
    [
        {},
        [],
        {"events": []},
        {"quote": None, "events": []},
        {"quote": {}, "events": []},
        {"events": "not-a-list"},
        {"quote": "not-a-dict", "events": []},
        None,
        True,
        False,
        42,
        "raw-string",
        {"quote_id": "", "quote_hash": "0" * 64, "signature": "a.b.c"},
        {"quote_id": "Q-1"},
        [{"seq": 0, "action": "x", "prev_hash": "0" * 64, "hash": "0" * 64, "signature": "a.b.c"}],
        ["not-an-event-dict"],
    ],
)
def test_verify_quote_rejects_empty_and_malformed_documents(empty_or_bad_doc: Any) -> None:
    _, jwks = _make_ed25519_signer()
    ok, _lines = verify_quote_mod.verify_document(empty_or_bad_doc, jwks)
    assert ok is False


def test_verify_quote_chain_integrity_catches_genesis_tamper_reordering_and_gap_forgery() -> None:
    priv, jwks = _make_ed25519_signer("chain-kid")

    def _make_event(seq: int, action: str, prev_hash: str, signed_seq: int | None = None) -> dict[str, Any]:
        core = {"seq": seq, "action": action, "summary": f"step {seq}", "prev_hash": prev_hash}
        h = verify_quote_mod.sha(prev_hash + verify_quote_mod.canon(core))
        pl: dict[str, Any] = {"typ": "audit-event", "seq": seq if signed_seq is None else signed_seq, "hash": h}
        return {**core, "hash": h, "kid": "chain-kid", "signature": _sign_jws(priv, "chain-kid", pl)}

    e1 = _make_event(1, "quote.created", "0" * 64)
    e2 = _make_event(2, "policy.evaluated", e1["hash"])
    e3 = _make_event(3, "order.placed", e2["hash"])

    ok_full, _ = verify_quote_mod.verify_document([e1, e2, e3], jwks)
    assert ok_full is True

    ok_subset, _ = verify_quote_mod.verify_document([e1, e3], jwks)
    assert ok_subset is True

    bad_genesis = _make_event(1, "quote.created", "f" * 64)
    assert verify_quote_mod.verify_document([bad_genesis], jwks)[0] is False

    bad_link_e2 = _make_event(2, "policy.evaluated", "a" * 64)
    assert verify_quote_mod.verify_document([e1, bad_link_e2], jwks)[0] is False

    assert verify_quote_mod.verify_document([e2, e1], jwks)[0] is False
    assert verify_quote_mod.verify_document([e1, e1], jwks)[0] is False

    mismatched_jws_seq = _make_event(1, "quote.created", "0" * 64, signed_seq=99)
    assert verify_quote_mod.verify_document([mismatched_jws_seq], jwks)[0] is False


def test_verify_quote_isolated_cli_handles_nan_bad_jwks_local_http_filename_and_bad_args(tmp_path: Path) -> None:
    script = str(SCRIPTS_DIR / "verify_quote.py")
    live_quote = LIVE_PACK_DIR / "quote.json"
    live_jwks = LIVE_PACK_DIR / "jwks.json"

    http_named_jwks = tmp_path / "http_local_jwks.json"
    http_named_jwks.write_text(live_jwks.read_text(encoding="utf-8"), encoding="utf-8")
    ok_res = subprocess.run(
        [sys.executable, "-I", script, str(live_quote), str(http_named_jwks)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert ok_res.returncode == 0, ok_res.stdout + ok_res.stderr
    assert "VERIFIED" in ok_res.stdout

    empty_doc = tmp_path / "empty.json"
    empty_doc.write_text("{}", encoding="utf-8")
    empty_res = subprocess.run(
        [sys.executable, "-I", script, str(empty_doc), str(live_jwks)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert empty_res.returncode == 1
    assert "FAILED" in empty_res.stdout
    assert "VERIFIED" not in empty_res.stdout

    nan_doc = tmp_path / "nan_quote.json"
    nan_doc.write_text('{"quote_id": "Q-1", "total_net": NaN}', encoding="utf-8")
    nan_res = subprocess.run(
        [sys.executable, "-I", script, str(nan_doc), str(live_jwks)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert nan_res.returncode == 1
    assert "FAILED" in nan_res.stdout
    assert "Traceback" not in nan_res.stderr

    bad_jwks = tmp_path / "bad_jwks.json"
    bad_jwks.write_text('{"keys": "not-a-list"}', encoding="utf-8")
    jwks_res = subprocess.run(
        [sys.executable, "-I", script, str(live_quote), str(bad_jwks)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert jwks_res.returncode == 1
    assert "FAILED" in jwks_res.stdout
    assert "Traceback" not in jwks_res.stderr

    bad_url_res = subprocess.run(
        [sys.executable, "-I", script, str(live_quote), "http://user@localhost:8000/.well-known/jwks.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert bad_url_res.returncode == 1
    assert "FAILED" in bad_url_res.stdout

    no_args_res = subprocess.run(
        [sys.executable, "-I", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert no_args_res.returncode == 2
    assert "usage:" in no_args_res.stderr
    assert "FAILED" in no_args_res.stdout


def test_export_peppol_validates_sample_codes_order_ids_out_dir_and_xsd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for bad_code in ("", "ap", "AB", "XX", None, True, 123, ["AP"]):
        with pytest.raises(ValueError):
            export_peppol_mod.sample(bad_code)

    out_dir = tmp_path / "peppol_out"
    assert export_peppol_mod.main(["--sample", "--out", str(out_dir)]) == 0
    for code in ("AP", "CA", "RE"):
        xml_file = out_dir / f"sample-order-response-advanced-{code}.xml"
        assert xml_file.is_file() and xml_file.stat().st_size > 100

    regular_file = tmp_path / "not_a_dir.txt"
    regular_file.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        export_peppol_mod.main(["--sample", "--out", str(regular_file)])

    with pytest.raises(ValueError, match="xsd schema file not found"):
        export_peppol_mod.main(["--sample", "--out", str(out_dir), "--xsd", str(tmp_path / "missing.xsd")])

    for bad_order in ("", "   ", "../ORD-1", "ORD/1", "ORD\\1", "ORD\n1", "ORD 1", "x" * 129):
        with pytest.raises(ValueError):
            export_peppol_mod.main(["--order", bad_order, "--out", str(out_dir)])

    monkeypatch.setattr("aimo.backend.orders.get_order", lambda _oid: None)
    assert export_peppol_mod.main(["--order", "ORD-does-not-exist", "--out", str(out_dir)]) == 2

    bad_sub = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "export_peppol_order_response.py"), "--order", "../evil"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert bad_sub.returncode == 2
    assert "error:" in bad_sub.stderr
    assert "Traceback" not in bad_sub.stderr

    for bad_argv in ("--sample", True, ["--sample\x00"], [123]):
        with pytest.raises(ValueError):
            export_peppol_mod.main(bad_argv)

    for bad_xml in ("", "   ", True, None, "<OrderResponse/>\x00"):
        with pytest.raises(ValueError):
            export_peppol_mod.xsd_validate(bad_xml, str(tmp_path / "missing.xsd"))


def test_export_peppol_real_order_and_corrupt_db_order_id_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order_obj, quote_obj = export_peppol_mod.sample("AP")
    monkeypatch.setattr("aimo.backend.orders.get_order", lambda oid: order_obj if oid == order_obj.order_id else None)
    monkeypatch.setattr("aimo.backend.orders.get_quote", lambda qid: quote_obj if qid == quote_obj.quote_id else None)

    out_dir = tmp_path / "peppol_real"
    rc = export_peppol_mod.main(["--order", order_obj.order_id, "--out", str(out_dir)])
    assert rc == 0
    assert (out_dir / f"{order_obj.order_id}-order-response-advanced.xml").is_file()

    corrupt_order = order_obj.model_copy(update={"order_id": "../corrupt-order"})
    monkeypatch.setattr("aimo.backend.orders.get_order", lambda _oid: corrupt_order)
    with pytest.raises(ValueError, match="order_id"):
        export_peppol_mod.main(["--order", "ORD-safe-lookup", "--out", str(out_dir)])


def test_catalog_cli_demo_runs_end_to_end_keyless_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(tmp_path / "data"),
        "AIMO_STATE_DB": str(tmp_path / "state.sqlite"),
        "AIMO_KEY_DIR": str(tmp_path / "keys"),
        "AIMO_REPLAY": str(ROOT / "tests" / "fixtures" / "snapshot" / "replay"),
        "AIMO_API_KEY": "",
        "VALIO_API_KEY": "",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
    }
    res = subprocess.run(
        [sys.executable, "-m", "aimo.catalog", "demo"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "464306" in res.stdout


def test_mcp_check_stdio_runs_end_to_end_keyless_replay(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AIMO_DATA_DIR": str(tmp_path / "data"),
        "AIMO_STATE_DB": str(tmp_path / "state.sqlite"),
        "AIMO_KEY_DIR": str(tmp_path / "keys"),
        "AIMO_REPLAY": str(ROOT / "tests" / "fixtures" / "snapshot" / "replay"),
        "AIMO_API_KEY": "",
        "VALIO_API_KEY": "",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
    }
    res = subprocess.run(
        [sys.executable, "-m", "aimo.gateway.mcp_check", "stdio"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "13 tools:" in res.stdout
    assert "search_products ->" in res.stdout
    assert "get_offers ->" in res.stdout


