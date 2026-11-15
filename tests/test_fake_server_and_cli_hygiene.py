from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import tokenize
from typing import Any

from fastapi.testclient import TestClient
import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from aimo import cli
from aimo.buyer import fake_server, run_demo
from aimo.buyer.fake_server import build_app
from aimo.buyer.tools import ToolError
from aimo.gateway import human_auth, mcp_check, mcp_server

DELIVERY = "2026-11-16"


def _parse_mcp_result(res: Any) -> Any:
    text = "\n".join(getattr(c, "text", "") or "" for c in (getattr(res, "content", None) or []))
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        raise ToolError(text or "mcp tool error")
    sc = getattr(res, "structured_content", None)
    if sc is None:
        sc = getattr(res, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    return json.loads(text)


@asynccontextmanager
async def _mount_fake_mcp(scenario: str = "happy", customer: str = "hotel-aurora"):
    app = build_app(scenario=scenario, customer=customer)
    async with app.router.lifespan_context(app):
        asgi = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=asgi, base_url="http://127.0.0.1:8000") as http:
            async with Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)) as mcp_client:
                yield http, mcp_client


def test_owned_files_zero_comments_and_zero_em_en_dashes() -> None:
    targets = (
        Path(fake_server.__file__),
        Path(cli.__file__),
        Path(__file__),
    )
    for path in targets:
        src = path.read_text(encoding="utf-8")
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert comments == [], f"Unexpected comment tokens in {path}: {comments}"
        assert "\x23" not in src, f"Unexpected hash character in {path}"
        assert "\u2014" not in src, f"Unexpected em dash in {path}"
        assert "\u2013" not in src, f"Unexpected en dash in {path}"


def test_fake_server_decision_bad_inputs_never_500() -> None:
    app = build_app(scenario="happy", customer="hotel-aurora")
    with TestClient(app, raise_server_exceptions=False) as sync_client:
        for raw_body in (
            b"",
            b"   \n\t ",
            b"{invalid-json",
            b'{"decision": NaN}',
            b'{"decision": Infinity}',
            b"\xff\xfe\xfd",
        ):
            resp = sync_client.post(
                "/v1/approvals/O-unknown/decision",
                content=raw_body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 422, (raw_body, resp.status_code, resp.text)

    async def _exercise() -> None:
        async with _mount_fake_mcp(scenario="happy", customer="hotel-aurora") as (http, mcp_client):
            av0 = (await http.get("/v1/audit/verify")).json()
            assert av0 == {"ok": True, "events": 0, "_fake": True}

            quote = _parse_mcp_result(
                await mcp_client.call_tool(
                    "create_quote",
                    {
                        "delivery_date": DELIVERY,
                        "lines": [{"product_id": "464306", "qty_sales_units": 800, "tier": "best"}],
                    },
                )
            )
            assert (await http.get("/v1/audit/verify")).json()["events"] == 1

            order = _parse_mcp_result(
                await mcp_client.call_tool(
                    "place_order",
                    {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
                )
            )
            oid = order["order_id"]
            assert order["status"] == "pending_approval"
            assert (await http.get("/v1/audit/verify")).json()["events"] == 2

            for raw_bytes in (
                b"",
                b"   ",
                b"{broken",
                b"[1, 2, 3",
                b'{"decision": "approve", "note": NaN}',
                b"\x80\x81",
            ):
                r_raw = await http.post(
                    f"/v1/approvals/{oid}/decision",
                    content=raw_bytes,
                    headers={"Content-Type": "application/json"},
                )
                assert r_raw.status_code == 422, (raw_bytes, r_raw.status_code, r_raw.text)

            for non_dict_json in (
                [],
                ["approve"],
                None,
                0,
                42,
                -7,
                True,
                False,
                "approve",
            ):
                r_type = await http.post(f"/v1/approvals/{oid}/decision", json=non_dict_json)
                assert r_type.status_code == 422, (non_dict_json, r_type.status_code, r_type.text)

            for bad_payload in (
                {"decision": None},
                {"decision": 123},
                {"decision": True},
                {"decision": False},
                {"decision": ["approve"]},
                {"decision": {"action": "approve"}},
                {"decision": ""},
                {"decision": "hold"},
                {"decision": "APPROVE"},
                {"decision": " approve "},
                {"decision": "approve", "approver": 123},
                {"decision": "approve", "approver": True},
                {"decision": "approve", "approver": ["anna"]},
                {"decision": "approve", "approver": {"id": "anna"}},
                {"decision": "approve", "note": 99},
                {"decision": "approve", "note": False},
                {"decision": "approve", "note": ["ok"]},
                {"decision": "approve", "note": {"text": "ok"}},
                {"decision": "approve", "quote_hash": 12345},
                {"decision": "approve", "quote_hash": True},
                {"decision": "approve", "quote_hash": ["qh"]},
            ):
                r_bad = await http.post(f"/v1/approvals/{oid}/decision", json=bad_payload)
                assert r_bad.status_code == 422, (bad_payload, r_bad.status_code, r_bad.text)

            pending_now = (await http.get("/v1/approvals?status=pending")).json()
            assert [row["order"]["order_id"] for row in pending_now] == [oid]
            assert pending_now[0]["order"]["status"] == "pending_approval"
            assert (await http.get("/v1/audit/verify")).json()["events"] == 2

    asyncio.run(_exercise())


def test_fake_server_quote_hash_mismatch_double_decision_and_status_normalization() -> None:
    async def _exercise() -> None:
        async with _mount_fake_mcp(scenario="happy", customer="hotel-aurora") as (http, mcp_client):
            q1 = _parse_mcp_result(
                await mcp_client.call_tool(
                    "create_quote",
                    {
                        "delivery_date": DELIVERY,
                        "lines": [{"product_id": "464306", "qty_sales_units": 800, "tier": "best"}],
                    },
                )
            )
            o1 = _parse_mcp_result(
                await mcp_client.call_tool(
                    "place_order",
                    {"quote_id": q1["quote_id"], "quote_hash": q1["quote_hash"]},
                )
            )
            oid1 = o1["order_id"]
            assert o1["status"] == "pending_approval"

            for pending_query in (
                "/v1/approvals",
                "/v1/approvals?status=pending",
                "/v1/approvals?status=%20PENDING%20",
                "/v1/approvals?status=Pending",
                "/v1/approvals?status=pending_approval",
                "/v1/approvals?status=%20PENDING_APPROVAL%20",
                "/v1/approvals?status=",
                "/v1/approvals?status=%20%20",
            ):
                rows = (await http.get(pending_query)).json()
                assert [r["order"]["order_id"] for r in rows] == [oid1], pending_query

            r_mismatch = await http.post(
                f"/v1/approvals/{oid1}/decision",
                json={"decision": "approve", "quote_hash": "wrong-hash", "approver": "anna@hotel-aurora.example"},
            )
            assert r_mismatch.status_code == 409
            assert "different quote hash" in r_mismatch.json()["detail"]

            r_approve = await http.post(
                f"/v1/approvals/{oid1}/decision",
                json={
                    "decision": "approve",
                    "quote_hash": o1["quote_hash"],
                    "approver": "  anna@hotel-aurora.example  ",
                    "note": None,
                },
            )
            assert r_approve.status_code == 200
            decided_approve = r_approve.json()
            assert decided_approve["status"] == "placed"
            assert decided_approve["approved_by"] == "anna@hotel-aurora.example"
            assert decided_approve["approval_note"] == ""

            for repeat_dec in ("approve", "reject"):
                r_dup1 = await http.post(
                    f"/v1/approvals/{oid1}/decision",
                    json={"decision": repeat_dec, "quote_hash": o1["quote_hash"], "approver": "anna@hotel-aurora.example"},
                )
                assert r_dup1.status_code == 409
                assert "not pending approval" in r_dup1.json()["detail"]

            q2 = _parse_mcp_result(
                await mcp_client.call_tool(
                    "create_quote",
                    {
                        "delivery_date": DELIVERY,
                        "lines": [{"product_id": "464306", "qty_sales_units": 700, "tier": "best"}],
                    },
                )
            )
            o2 = _parse_mcp_result(
                await mcp_client.call_tool(
                    "place_order",
                    {"quote_id": q2["quote_id"], "quote_hash": q2["quote_hash"]},
                )
            )
            oid2 = o2["order_id"]
            assert o2["status"] == "pending_approval"

            r_reject = await http.post(
                f"/v1/approvals/{oid2}/decision",
                json={"decision": "reject", "quote_hash": o2["quote_hash"], "approver": None, "note": "budget hold"},
            )
            assert r_reject.status_code == 200
            decided_reject = r_reject.json()
            assert decided_reject["status"] == "rejected"
            assert decided_reject["approved_by"] is None
            assert decided_reject["approval_note"] == "budget hold"

            for repeat_dec in ("reject", "approve"):
                r_dup2 = await http.post(
                    f"/v1/approvals/{oid2}/decision",
                    json={"decision": repeat_dec, "quote_hash": o2["quote_hash"]},
                )
                assert r_dup2.status_code == 409
                assert "not pending approval" in r_dup2.json()["detail"]

            q_auto_placed = _parse_mcp_result(
                await mcp_client.call_tool(
                    "create_quote",
                    {
                        "delivery_date": DELIVERY,
                        "lines": [{"product_id": "464306", "qty_sales_units": 10, "tier": "best"}],
                    },
                )
            )
            o_auto_placed = _parse_mcp_result(
                await mcp_client.call_tool(
                    "place_order",
                    {"quote_id": q_auto_placed["quote_id"], "quote_hash": q_auto_placed["quote_hash"]},
                )
            )
            assert o_auto_placed["status"] == "placed"
            r_auto_placed_dec = await http.post(
                f"/v1/approvals/{o_auto_placed['order_id']}/decision",
                json={"decision": "reject"},
            )
            assert r_auto_placed_dec.status_code == 409

            q_auto_rejected = _parse_mcp_result(
                await mcp_client.call_tool(
                    "create_quote",
                    {
                        "delivery_date": DELIVERY,
                        "lines": [{"product_id": "464306", "qty_sales_units": 6000, "tier": "best"}],
                    },
                )
            )
            o_auto_rejected = _parse_mcp_result(
                await mcp_client.call_tool(
                    "place_order",
                    {"quote_id": q_auto_rejected["quote_id"], "quote_hash": q_auto_rejected["quote_hash"]},
                )
            )
            assert o_auto_rejected["status"] == "rejected"
            r_auto_rejected_dec = await http.post(
                f"/v1/approvals/{o_auto_rejected['order_id']}/decision",
                json={"decision": "approve"},
            )
            assert r_auto_rejected_dec.status_code == 409

            r_missing = await http.post("/v1/approvals/O-nonexistent/decision", json={"decision": "approve"})
            assert r_missing.status_code == 404

            placed_rows = (await http.get("/v1/approvals?status=%20PLACED%20")).json()
            rejected_rows = (await http.get("/v1/approvals?status=%20ReJeCtEd%20")).json()
            all_rows = (await http.get("/v1/approvals?status=%20ALL%20")).json()
            pending_rows = (await http.get("/v1/approvals?status=%20Pending%20")).json()
            assert {r["order"]["order_id"] for r in placed_rows} == {oid1, o_auto_placed["order_id"]}
            assert {r["order"]["order_id"] for r in rejected_rows} == {oid2, o_auto_rejected["order_id"]}
            assert len(all_rows) == 4
            assert pending_rows == []

            verify = (await http.get("/v1/audit/verify")).json()
            assert verify == {"ok": True, "events": 10, "_fake": True}

    asyncio.run(_exercise())


@pytest.mark.parametrize(
    ("host", "port", "mcp_in", "expected_serve", "expected_mcp"),
    [
        ("127.0.0.1", 8000, "http://127.0.0.1:8000/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("127.0.0.1", 8000, "http://127.0.0.1:8000/mcp/", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp/"),
        ("127.0.0.1", 8000, "  http://127.0.0.1:8000/mcp/  ", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp/"),
        ("0.0.0.0", 8000, "http://localhost:8000/mcp/", "http://127.0.0.1:8000", "http://localhost:8000/mcp/"),
        ("localhost", 8000, "  http://LOCALHOST:8000/mcp///  ", "http://127.0.0.1:8000", "http://LOCALHOST:8000/mcp///"),
        ("::1", 8000, "http://[::1]:8000/mcp/", "http://[::1]:8000", "http://[::1]:8000/mcp/"),
        ("[::1]", 8000, "  http://[::1]:8000/mcp  ", "http://[::1]:8000", "http://[::1]:8000/mcp"),
        (" [::1] ", 8000, "http://::1:8000/mcp/", "http://[::1]:8000", "http://::1:8000/mcp/"),
        (" ::1 ", 8443, "http://127.0.0.1:8443", "http://[::1]:8443", "http://127.0.0.1:8443"),
        ("127.0.0.1", 8000, "http://127.0.0.1:8000/mcp?probe=1", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp?probe=1"),
        ("0.0.0.0", 8000, "  https://gateway.example.org/mcp/  ", "http://127.0.0.1:8000", "https://gateway.example.org/mcp/"),
        ("0.0.0.0", 8000, "http://localhost.example.org:9999/mcp", "http://127.0.0.1:8000", "http://localhost.example.org:9999/mcp"),
    ],
)
def test_cli_export_serve_url_matching_port_and_external_urls_never_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    port: int,
    mcp_in: str,
    expected_serve: str,
    expected_mcp: str,
) -> None:
    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    monkeypatch.setenv("AIMO_MCP_URL", mcp_in)
    cli._export_serve_url(host, port)
    out = capsys.readouterr().out
    assert out == ""
    assert os.environ["AIMO_SERVE_URL"] == expected_serve
    assert os.environ["AIMO_MCP_URL"] == expected_mcp


def test_cli_export_serve_url_blank_whitespace_mcp_url_cleared_without_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AIMO_MCP_URL", "   \t  ")
    cli._export_serve_url("127.0.0.1", 8000)
    assert capsys.readouterr().out == ""
    assert os.environ["AIMO_SERVE_URL"] == "http://127.0.0.1:8000"
    assert "AIMO_MCP_URL" not in os.environ


@pytest.mark.parametrize(
    ("host", "port", "stale_mcp", "expected_serve", "expected_mcp"),
    [
        ("127.0.0.1", 8000, "http://127.0.0.1:9999/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("127.0.0.1", 8000, "http://[::1]:9999/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("0.0.0.0", 8000, "http://localhost:9999/mcp/", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("::1", 8000, "http://[::1]:9999/mcp/", "http://[::1]:8000", "http://[::1]:8000/mcp"),
        ("[::1]", 8000, "  http://127.0.0.1:9999/mcp/  ", "http://[::1]:8000", "http://[::1]:8000/mcp"),
        ("::1", 8088, "http://::1:9999/mcp", "http://[::1]:8088", "http://[::1]:8088/mcp"),
        ("localhost", 8000, "http://LOCALHOST:9999/mcp/", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("127.0.0.1", 8000, "http://localhost/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("127.0.0.1", 8000, "http://0.0.0.0:9999/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
        ("[::]", 8000, "http://[::]:9999/mcp", "http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
    ],
)
def test_cli_export_serve_url_stale_local_port_rewrites_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    port: int,
    stale_mcp: str,
    expected_serve: str,
    expected_mcp: str,
) -> None:
    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    monkeypatch.setenv("AIMO_MCP_URL", stale_mcp)
    cli._export_serve_url(host, port)
    out = capsys.readouterr().out
    assert "warning:" in out
    assert f"does not match the served port {port}" in out
    assert os.environ["AIMO_SERVE_URL"] == expected_serve
    assert os.environ["AIMO_MCP_URL"] == expected_mcp


def test_cli_main_subcommand_dispatch_matrix(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []

    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    monkeypatch.setattr(cli, "_fetch", lambda force=False: calls.append(("fetch", force)))
    monkeypatch.setattr(cli, "_build_catalog", lambda mode="background": calls.append(("build", mode)))
    monkeypatch.setattr(human_auth, "credential_warning", lambda: None)
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app_ref, **kwargs: calls.append(("uvicorn", (app_ref, kwargs))),
    )

    cli.main(["serve", "--host", "::1", "--port", "8123", "--no-build", "--reload"])
    capsys.readouterr()
    assert os.environ["AIMO_SERVE_URL"] == "http://[::1]:8123"
    assert calls == [
        ("fetch", False),
        ("build", "skip"),
        (
            "uvicorn",
            (
                "aimo.gateway.app:app",
                {"host": "::1", "port": 8123, "reload": True, "log_level": "info", "proxy_headers": False},
            ),
        ),
    ]

    calls.clear()
    cli.main(["serve", "--build-sync"])
    capsys.readouterr()
    assert ("build", "sync") in calls

    calls.clear()
    cli.main(["fetch-data"])
    cli.main(["fetch-data", "--force"])
    assert calls == [("fetch", False), ("fetch", True)]

    calls.clear()
    monkeypatch.setattr(mcp_server, "main", lambda: calls.append(("mcp_stdio", True)))
    cli.main(["mcp"])
    assert calls == [("mcp_stdio", True)]

    calls.clear()

    async def _fake_mcp_check(target: str) -> None:
        calls.append(("mcp_check", target))

    monkeypatch.setattr(mcp_check, "main", _fake_mcp_check)
    cli.main(["mcp-check"])
    cli.main(["mcp-check", "stdio"])
    assert calls == [("mcp_check", "http://localhost:8000/mcp"), ("mcp_check", "stdio")]

    demo_args: list[list[str]] = []

    def _fake_demo_main(argv: list[str] | None = None) -> int:
        demo_args.append(list(argv or []))
        return 3

    monkeypatch.setattr(run_demo, "main", _fake_demo_main)
    with pytest.raises(SystemExit) as exc_demo:
        cli.main(["demo", "--transport", "fake", "--scenario", "happy"])
    assert exc_demo.value.code == 3
    assert demo_args == [["--transport", "fake", "--scenario", "happy"]]

    with pytest.raises(SystemExit) as exc_missing:
        cli.main([])
    capsys.readouterr()
    assert exc_missing.value.code == 2
