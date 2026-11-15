from __future__ import annotations

import asyncio as real_asyncio
import contextlib
import io
import json
import socket
import threading
import time
import tokenize
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn

import aimo.buyer.agent as agent_mod
import aimo.buyer.fakes as fakes_mod
from aimo import cli, config, gemini
from aimo.buyer import fake_server, run_demo, runs
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, mock_wanted
from aimo.gateway import human_auth

ROOT = Path(__file__).resolve().parents[1]
ANNA = "anna@hotel-aurora.example"
LIISA = "liisa@school-tampere.example"
TEST_PIN = "test-pin-12chars-ok"


class _FastStopProxy:
    def __init__(self, ev: threading.Event) -> None:
        self._ev = ev

    def is_set(self) -> bool:
        return self._ev.is_set()

    def set(self) -> None:
        self._ev.set()

    def wait(self, timeout: float | None = None) -> bool:
        capped = min(float(timeout), 0.015) if timeout is not None else None
        return self._ev.wait(capped)


class _FastAsyncioProxy:
    def __init__(self, mod: Any) -> None:
        self._mod = mod

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mod, name)

    async def sleep(self, delay: float, *args: Any, **kwargs: Any) -> Any:
        return await self._mod.sleep(min(float(delay), 0.01), *args, **kwargs)


@pytest.fixture(autouse=True)
def _keyless_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[str]]:
    gemini_calls: list[str] = []
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_APPROVER_PIN", TEST_PIN)
    for var in (
        "AIMO_LLM_MODE",
        "AIMO_AUTO_APPROVE",
        "AIMO_BUYER_TRANSPORT",
        "AIMO_MCP_URL",
        "AIMO_DEMO_FAULTS",
        "AIMO_AGENT_KEY",
        "AIMO_ENV",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(gemini, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(run_demo, "_TTY", False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    human_auth.reset_for_tests()

    orig_agent_init = agent_mod.BuyerAgent.__init__

    def _fast_agent_init(self: Any, *args: Any, **kwargs: Any) -> None:
        poll_val = kwargs.get("poll_s")
        if isinstance(poll_val, (int, float)) and not isinstance(poll_val, bool) and poll_val >= 0.5:
            kwargs["poll_s"] = 0.02
        orig_agent_init(self, *args, **kwargs)

    orig_fake_approver = run_demo.fake_auto_approver

    def _fast_fake_approver(
        gw: Any,
        approver: str,
        stop: threading.Event,
        delay: float = 3.0,
        decision: str = "approve",
    ) -> None:
        orig_fake_approver(gw, approver, _FastStopProxy(stop), min(float(delay), 0.01), decision)

    orig_http_approver = run_demo.http_auto_approver

    def _fast_http_approver(
        base_url: str,
        approver: str,
        customer_id: str,
        stop: threading.Event,
        delay: float = 3.0,
        decision: str = "approve",
        client: Any = None,
        ready: threading.Event | None = None,
        poll_s: float | None = None,
    ) -> None:
        eff_poll = 0.015 if poll_s is None else poll_s
        orig_http_approver(
            base_url,
            approver,
            customer_id,
            stop,
            min(float(delay), 0.02),
            decision,
            client,
            ready,
            eff_poll,
        )

    monkeypatch.setattr(agent_mod.BuyerAgent, "__init__", _fast_agent_init)
    monkeypatch.setattr(run_demo, "fake_auto_approver", _fast_fake_approver)
    monkeypatch.setattr(run_demo, "http_auto_approver", _fast_http_approver)
    monkeypatch.setattr(runs, "asyncio", _FastAsyncioProxy(real_asyncio))

    def _forbid_generate(*args: Any, **kwargs: Any) -> Any:
        gemini_calls.append("generate")
        raise AssertionError("aimo.gemini.generate must not be called in keyless mode")

    def _forbid_embed(*args: Any, **kwargs: Any) -> Any:
        gemini_calls.append("embed")
        raise AssertionError("aimo.gemini.embed must not be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", _forbid_generate)
    monkeypatch.setattr("aimo.gemini.embed", _forbid_embed)
    try:
        yield gemini_calls
    finally:
        human_auth.reset_for_tests()


def _wrap_asgi_recorder(app: Any, log: list[dict[str, Any]]) -> Any:
    async def _asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        body_chunks: list[bytes] = []
        status_holder = [0]

        async def _recv() -> dict[str, Any]:
            msg = await receive()
            if msg.get("type") == "http.request" and method == "POST":
                chunk = msg.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
            return msg

        async def _send(msg: dict[str, Any]) -> None:
            if msg.get("type") == "http.response.start":
                status_holder[0] = int(msg.get("status", 0))
                if method == "GET" and path.endswith("/events"):
                    log.append(
                        {
                            "method": method,
                            "path": path,
                            "status": status_holder[0],
                            "json": None,
                        }
                    )
            await send(msg)

        await app(scope, _recv, _send)
        if not (method == "GET" and path.endswith("/events")):
            parsed: Any = None
            if body_chunks:
                try:
                    parsed = json.loads(b"".join(body_chunks).decode("utf-8"))
                except Exception:
                    parsed = None
            log.append(
                {
                    "method": method,
                    "path": path,
                    "status": status_holder[0],
                    "json": parsed,
                }
            )

    return _asgi


@contextlib.contextmanager
def _live_fake_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str = "happy",
    customer: str = "hotel-aurora",
    poll_s: float = 0.02,
) -> Iterator[tuple[str, Any, runs.RunRegistry, list[dict[str, Any]]]]:
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    real_gw_cls = fakes_mod.FakeGateway
    shared_gw = real_gw_cls(customer, f"{customer}/buyer-1", scenario=scenario)
    monkeypatch.setattr(fakes_mod, "FakeGateway", lambda *a, **k: shared_gw)

    fresh_reg = runs.RunRegistry()
    orig_execute = fresh_reg._execute

    def _fast_execute(run_obj: runs.Run, transport_factory: Any, agent_kwargs: dict, mcp_url: str | None = None) -> None:
        merged_kw = {"poll_s": poll_s, "approval_timeout_s": 10.0, **(agent_kwargs or {})}
        orig_execute(run_obj, transport_factory, merged_kw, mcp_url)

    monkeypatch.setattr(fresh_reg, "_execute", _fast_execute)
    monkeypatch.setattr(runs, "registry", fresh_reg)

    recorded: list[dict[str, Any]] = []
    raw_app = fake_server.build_app(scenario=scenario, customer=customer)
    wrapped_app = _wrap_asgi_recorder(raw_app, recorded)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = int(sock.getsockname()[1])
    base = f"http://127.0.0.1:{port}"

    cfg = uvicorn.Config(wrapped_app, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=1)
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None
    srv_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="uvicorn-buyer-e2e")
    srv_thread.start()

    ready = False
    for _ in range(150):
        if not srv_thread.is_alive():
            break
        if server.started:
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
        time.sleep(0.01)

    if not ready:
        server.should_exit = True
        srv_thread.join(timeout=5.0)
        sock.close()
        pytest.fail("ephemeral uvicorn server did not become ready")

    try:
        yield base, shared_gw, fresh_reg, recorded
    finally:
        for t in list(threading.enumerate()):
            if t.name.startswith("buyer-run-") and t.is_alive():
                t.join(timeout=3.0)
        server.should_exit = True
        srv_thread.join(timeout=5.0)
        sock.close()
        assert not srv_thread.is_alive()


def _is_pool_thread(name: str) -> bool:
    return name == "AnyIO worker thread" or name.startswith("aimo-guard_")


def _leaked_app_threads(before_ids: set[int | None]) -> list[str]:
    for _ in range(25):
        cur = [
            t.name
            for t in threading.enumerate()
            if t.is_alive() and t.ident not in before_ids and not _is_pool_thread(t.name)
        ]
        if not cur:
            return []
        time.sleep(0.02)
    return [
        t.name
        for t in threading.enumerate()
        if t.is_alive() and t.ident not in before_ids and not _is_pool_thread(t.name)
    ]


def test_keyless_approval_timeout_leaves_over_500_order_pending(
    _keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert mock_wanted() is True
    out_json = tmp_path / "pending_timeout.json"
    rc = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--no-auto-approve",
            "--approval-timeout",
            "0.06",
            "--json",
            str(out_json),
        ]
    )
    stdout = capsys.readouterr().out
    assert rc == 0, stdout
    res = json.loads(out_json.read_text(encoding="utf-8"))

    assert res["mock"] is True
    assert res["status"] == "pending_approval"
    assert res["quote"]["total_net"] > 500.0
    assert res["order"]["status"] == "pending_approval"
    assert res["order"]["timed_out"] is True
    assert res["order"].get("approved_by") is None
    assert any(s["kind"] == "approval_wait" for s in res["steps"])
    assert any(s["kind"] == "error" and "No human decision within" in s["title"] for s in res["steps"])
    assert "[injection_screen: clean]" in stdout

    verdict_text = run_demo.scenario_verdict("happy", res)
    assert "pending_approval" in verdict_text
    assert "waiting for a human" in verdict_text
    assert "nothing ordered" in verdict_text
    assert "model: scripted (no Gemini)" in verdict_text
    assert f"Verdict [happy]: {verdict_text}" in stdout
    assert _keyless_env == []


def test_keyless_auto_approve_flag_env_and_default_cli_place_order(
    _keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    default_cli_json = tmp_path / "default_cli.json"
    with pytest.raises(SystemExit) as exc_default:
        cli.main(
            [
                "demo",
                "--transport",
                "fake",
                "--approve-delay",
                "0.01",
                "--approval-timeout",
                "10",
                "--json",
                str(default_cli_json),
            ]
        )
    out_default = capsys.readouterr().out
    assert exc_default.value.code == 0, out_default
    assert f"simulated human approval enabled as {ANNA}" in out_default
    assert MOCK_LABEL in out_default
    res_default = json.loads(default_cli_json.read_text(encoding="utf-8"))
    assert res_default["status"] == "placed"
    assert res_default["order"]["approved_by"] == ANNA

    flag_json = tmp_path / "approved_flag.json"
    rc_flag = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--auto-approve-as",
            ANNA,
            "--approve-delay",
            "0.01",
            "--approval-timeout",
            "10",
            "--json",
            str(flag_json),
        ]
    )
    out_flag = capsys.readouterr().out
    assert rc_flag == 0, out_flag
    res_flag = json.loads(flag_json.read_text(encoding="utf-8"))
    assert res_flag["status"] == "placed"
    assert res_flag["mock"] is True
    assert res_flag["order"]["status"] == "placed"
    assert res_flag["order"]["approved_by"] == ANNA
    assert res_flag["order"].get("timed_out") is not True
    verdict_flag = run_demo.scenario_verdict("happy", res_flag)
    assert f"approved by {ANNA}" in verdict_flag
    assert "model: scripted (no Gemini)" in verdict_flag

    monkeypatch.setenv("AIMO_AUTO_APPROVE", "1")
    env_json = tmp_path / "approved_env.json"
    rc_env = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--approve-delay",
            "0.01",
            "--approval-timeout",
            "10",
            "--json",
            str(env_json),
        ]
    )
    out_env = capsys.readouterr().out
    assert rc_env == 0, out_env
    assert "simulated human approval enabled" not in out_env
    res_env = json.loads(env_json.read_text(encoding="utf-8"))
    assert res_env["status"] == "placed"
    assert res_env["order"]["status"] == "placed"
    assert res_env["order"]["approved_by"] == ANNA

    school_env_json = tmp_path / "approved_school_env.json"
    rc_school = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "school-allergen",
            "--approve-delay",
            "0.01",
            "--approval-timeout",
            "10",
            "--json",
            str(school_env_json),
        ]
    )
    capsys.readouterr()
    assert rc_school == 0
    res_school = json.loads(school_env_json.read_text(encoding="utf-8"))
    assert res_school["status"] == "placed"
    assert res_school["order"]["approved_by"] == LIISA
    assert res_school["allergen_probe"]["ran"] is True
    assert res_school["allergen_probe"]["status"] == "rejected"
    assert all("oat" in str(ln.get("product_name", "")).lower() for ln in res_school["quote"]["lines"])
    assert _keyless_env == []


def test_keyless_reject_finishes_rejected_with_zero_exit_code(
    _keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    rej_json = tmp_path / "rejected.json"
    rc = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--auto-approve-as",
            ANNA,
            "--reject",
            "--approve-delay",
            "0.01",
            "--approval-timeout",
            "10",
            "--json",
            str(rej_json),
        ]
    )
    stdout = capsys.readouterr().out
    assert rc == 0, stdout
    res = json.loads(rej_json.read_text(encoding="utf-8"))
    assert res["status"] == "rejected"
    assert res["mock"] is True
    assert res["order"]["status"] == "rejected"
    assert res["order"]["approved_by"] is None
    assert res["order"]["response_code"] == "RE"

    quote_calls = [s for s in res["steps"] if s["kind"] == "tool_call" and s["data"].get("name") == "create_quote"]
    place_calls = [s for s in res["steps"] if s["kind"] == "tool_call" and s["data"].get("name") == "place_order"]
    assert len(quote_calls) == 1
    assert len(place_calls) == 1

    cli_rej_json = tmp_path / "rejected_cli.json"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "demo",
                "--transport",
                "fake",
                "--scenario",
                "happy",
                "--auto-approve-as",
                ANNA,
                "--reject",
                "--approve-delay",
                "0.01",
                "--approval-timeout",
                "10",
                "--json",
                str(cli_rej_json),
            ]
        )
    capsys.readouterr()
    assert exc_info.value.code == 0
    assert json.loads(cli_rej_json.read_text(encoding="utf-8"))["status"] == "rejected"
    assert _keyless_env == []


def test_keyless_json_output_matches_run_scenario_result(
    _keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured_results: list[dict[str, Any]] = []
    orig_run_scenario = run_demo.run_scenario

    def _spy_run_scenario(*args: Any, **kwargs: Any) -> dict[str, Any]:
        res = orig_run_scenario(*args, **kwargs)
        captured_results.append(res)
        return res

    monkeypatch.setattr(run_demo, "run_scenario", _spy_run_scenario)
    out_path = tmp_path / "out.json"
    rc = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "happy",
            "--auto-approve-as",
            ANNA,
            "--approve-delay",
            "0.01",
            "--approval-timeout",
            "10",
            "--json",
            str(out_path),
        ]
    )
    capsys.readouterr()
    assert rc == 0
    assert len(captured_results) == 1
    assert out_path.is_file()

    disk_json = json.loads(out_path.read_text(encoding="utf-8"))
    expected_json = json.loads(json.dumps(captured_results[0], default=str))
    assert disk_json == expected_json
    for key in (
        "run_id",
        "status",
        "summary",
        "mock",
        "need",
        "needs",
        "plan",
        "quote",
        "order",
        "steps",
        "models_used",
        "transport",
        "scenario",
        "customer_id",
    ):
        assert key in disk_json
    assert disk_json["status"] == "placed"
    assert disk_json["transport"] == "fake"
    assert disk_json["scenario"] == "happy"
    assert disk_json["customer_id"] == "hotel-aurora"
    assert _keyless_env == []


def test_keyless_policy_bounds_and_non_positive_qty_rejected_before_planning(
    _keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    zero_json = tmp_path / "zero_qty.json"
    rc_zero = run_demo.main(
        [
            "--transport",
            "fake",
            "--customer",
            "hotel-aurora",
            "--text",
            "We need 0 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "--no-auto-approve",
            "--json",
            str(zero_json),
        ]
    )
    zero_out = capsys.readouterr().out
    assert rc_zero == 1
    assert "Input should be greater than 0" in zero_out
    res_zero = json.loads(zero_json.read_text(encoding="utf-8"))
    assert res_zero["status"] == "error"
    assert "Nothing further was ordered" in res_zero["summary"]
    assert res_zero["plan"] is None
    assert res_zero["quote"] is None
    assert res_zero["order"] is None
    assert not any(s["kind"] == "tool_call" for s in res_zero["steps"])

    inj_split_json = tmp_path / "inj_split.json"
    rc_inj = run_demo.main(
        [
            "--transport",
            "fake",
            "--scenario",
            "injection",
            "--text",
            "We need 5,200 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "--no-auto-approve",
            "--json",
            str(inj_split_json),
        ]
    )
    inj_out = capsys.readouterr().out
    assert rc_inj == 0, inj_out
    assert "[injection_screen: clean]" in inj_out
    assert "[injection_screen: FLAGGED (" in inj_out
    res_inj = json.loads(inj_split_json.read_text(encoding="utf-8"))
    assert res_inj["status"] == "needs_confirmation"
    assert any((ln.get("injection_screen") or {}).get("suspicious") is True for ln in res_inj["plan"]["lines"])
    assert any((ln.get("injection_screen") or {}).get("suspicious") is False for ln in res_inj["plan"]["lines"])

    sub_json = tmp_path / "sub_500.json"
    rc_sub = run_demo.main(
        [
            "--transport",
            "fake",
            "--customer",
            "hotel-aurora",
            "--text",
            "We need 20 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "--no-auto-approve",
            "--json",
            str(sub_json),
        ]
    )
    capsys.readouterr()
    assert rc_sub == 0
    res_sub = json.loads(sub_json.read_text(encoding="utf-8"))
    assert res_sub["status"] == "placed"
    assert res_sub["quote"]["total_net"] <= 500.0
    assert not any(s["kind"] == "approval_wait" for s in res_sub["steps"])
    assert res_sub["order"].get("timed_out") is not True

    hard_json = tmp_path / "over_5000.json"
    rc_hard = run_demo.main(
        [
            "--transport",
            "fake",
            "--customer",
            "hotel-aurora",
            "--text",
            "We need 1,500 litres of lactose-free whipping cream at Hotel Aurora Helsinki by Monday.",
            "--no-auto-approve",
            "--json",
            str(hard_json),
        ]
    )
    capsys.readouterr()
    assert rc_hard == 0
    res_hard = json.loads(hard_json.read_text(encoding="utf-8"))
    assert res_hard["status"] == "rejected"
    assert res_hard["quote"]["total_net"] > 5000.0
    assert not any(s["kind"] == "approval_wait" for s in res_hard["steps"])
    assert _keyless_env == []


def test_live_http_via_server_fake_transport_e2e_clarification_and_guards(
    _keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}

    with _live_fake_uvicorn(monkeypatch) as (base, shared_gw, fresh_reg, recorded):
        unk_resp = httpx.post(
            f"{base}/v1/agent/runs",
            json={"text": "20 L milk by Monday", "customer_id": "rogue-corp", "transport": "fake"},
            timeout=5.0,
        )
        assert unk_resp.status_code == 403
        assert unk_resp.json()["error"] == "unknown_customer"

        no_id_resp = httpx.post(
            f"{base}/v1/agent/runs",
            json={"text": "20 L milk by Monday"},
            timeout=5.0,
        )
        assert no_id_resp.status_code == 401
        assert no_id_resp.json()["error"] == "identity_required"

        recorded.clear()
        rc_hero = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "fake",
                "--auto-approve-as",
                ANNA,
                "--approve-delay",
                "0.01",
            ]
        )
        out_hero = capsys.readouterr().out
        assert rc_hero == 0, out_hero
        assert f"streaming {base}/v1/agent/runs/" in out_hero
        assert MOCK_LABEL in out_hero
        assert "[injection_screen: clean]" in out_hero
        assert "APPROVAL_WAIT" in out_hero
        assert f"{ANNA} approved" in out_hero
        assert "Done: placed" in out_hero
        assert "status=placed" in out_hero
        assert MOCK_MODEL_NAME in out_hero

        runs_list = httpx.get(f"{base}/v1/agent/runs", timeout=5.0).json()
        assert len(runs_list) == 1
        hero_run_id = runs_list[0]["run_id"]
        hero_view = httpx.get(f"{base}/v1/agent/runs/{hero_run_id}", timeout=5.0).json()
        assert hero_view["status"] == "finished"
        assert hero_view["order_status"] == "placed"
        assert hero_view["mock"] is True
        hero_kinds = [s["kind"] for s in hero_view["steps"]]
        for expected_kind in (
            "thought",
            "tool_call",
            "tool_result",
            "plan",
            "quote",
            "decision",
            "approval_wait",
            "order",
            "final",
        ):
            assert expected_kind in hero_kinds, (expected_kind, hero_kinds)

        sse_resume = httpx.get(
            f"{base}/v1/agent/runs/{hero_run_id}/events",
            headers={"Last-Event-ID": "3"},
            timeout=5.0,
        )
        assert sse_resume.status_code == 200
        resumed_ids = [
            int(ln.split(":", 1)[1].strip())
            for ln in sse_resume.text.splitlines()
            if ln.startswith("id:")
        ]
        assert resumed_ids and resumed_ids[0] == 4
        assert resumed_ids[-1] == len(hero_view["steps"])

        ans_no_id = httpx.post(
            f"{base}/v1/agent/runs/{hero_run_id}/answer",
            json={"option_id": "d0"},
            timeout=5.0,
        )
        assert ans_no_id.status_code == 401
        assert ans_no_id.json()["error"] == "identity_required"

        ans_unk = httpx.post(
            f"{base}/v1/agent/runs/{hero_run_id}/answer",
            json={"option_id": "d0", "customer_id": "rogue-corp"},
            timeout=5.0,
        )
        assert ans_unk.status_code == 403
        assert ans_unk.json()["error"] == "unknown_customer"

        assert any(
            r["method"] == "POST"
            and r["path"] == "/v1/agent/runs"
            and r["status"] == 200
            and isinstance(r["json"], dict)
            and r["json"].get("transport") == "fake"
            for r in recorded
        )
        assert any(
            r["method"] == "GET" and r["path"] == f"/v1/agent/runs/{hero_run_id}/events" and r["status"] == 200
            for r in recorded
        )
        assert any(
            r["method"] == "POST"
            and r["path"].startswith("/v1/approvals/")
            and r["path"].endswith("/decision")
            and r["status"] == 200
            and isinstance(r["json"], dict)
            and r["json"].get("approver") == ANNA
            for r in recorded
        )

        recorded.clear()
        rc_clar = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "fake",
                "--text",
                "We need lactose-free milk next week",
                "--answer",
                "auto",
                "--auto-approve-as",
                ANNA,
                "--approve-delay",
                "0.01",
            ]
        )
        out_clar = capsys.readouterr().out
        assert rc_clar == 0, out_clar
        assert "NEEDS_CLARIFICATION" in out_clar
        assert "[auto-answer] d0:" in out_clar
        assert "[auto-answer] q0:" in out_clar
        assert "Done: placed" in out_clar
        assert "status=placed" in out_clar

        runs_after = httpx.get(f"{base}/v1/agent/runs", timeout=5.0).json()
        clar_ids = [r["run_id"] for r in runs_after if r["run_id"] != hero_run_id]
        assert len(clar_ids) == 1
        clar_run_id = clar_ids[0]

        clar_view = httpx.get(f"{base}/v1/agent/runs/{clar_run_id}", timeout=5.0).json()
        assert clar_view["status"] == "finished"
        assert clar_view["order_status"] == "placed"
        assert clar_view["mock"] is True
        clar_steps = clar_view["steps"]
        clar_questions = [s for s in clar_steps if s["kind"] == "needs_clarification"]
        assert len(clar_questions) == 2

        sse_reqs = [
            r for r in recorded if r["method"] == "GET" and r["path"] == f"/v1/agent/runs/{clar_run_id}/events"
        ]
        assert len(sse_reqs) == 1 and sse_reqs[0]["status"] == 200

        ans_reqs = [
            r for r in recorded if r["method"] == "POST" and r["path"] == f"/v1/agent/runs/{clar_run_id}/answer"
        ]
        assert len(ans_reqs) == 2
        assert [r["status"] for r in ans_reqs] == [200, 200]
        assert [r["json"]["option_id"] for r in ans_reqs] == ["d0", "q0"]
        assert [r["json"]["question_id"] for r in ans_reqs] == ["q_date", "q_unit_0"]
        assert all(r["json"]["customer_id"] == "hotel-aurora" for r in ans_reqs)

        placed_orders = [o for o in shared_gw.orders.values() if o["status"] == "placed"]
        assert len(placed_orders) == 2
        assert len(fresh_reg.runs) == 2

        monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
        guard_resp = httpx.post(
            f"{base}/v1/agent/runs",
            json={
                "text": "We need 0 litres of milk by Monday.",
                "customer_id": "hotel-aurora",
                "transport": "fake",
                "faults": "partial:*:400",
            },
            timeout=5.0,
        )
        assert guard_resp.status_code == 200
        guard_body = guard_resp.json()
        assert sorted(guard_body["ignored"]) == ["faults", "transport"]
        guard_run_id = guard_body["run_id"]
        with httpx.stream("GET", f"{base}/v1/agent/runs/{guard_run_id}/events", timeout=5.0) as g_stream:
            assert g_stream.status_code == 200
            list(g_stream.iter_lines())

    assert _leaked_app_threads(threads_before) == []
    assert _keyless_env == []


def test_live_http_via_server_env_auto_approve_and_reject(
    _keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}

    with _live_fake_uvicorn(monkeypatch) as (base, shared_gw, fresh_reg, _recorded):
        monkeypatch.setenv("AIMO_AUTO_APPROVE", "1")
        rc_env = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "fake",
                "--approve-delay",
                "0.01",
            ]
        )
        out_env = capsys.readouterr().out
        assert rc_env == 0, out_env
        assert f"{ANNA} approved" in out_env
        assert "status=placed" in out_env

        rc_rej = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "fake",
                "--auto-approve-as",
                ANNA,
                "--reject",
                "--approve-delay",
                "0.01",
            ]
        )
        out_rej = capsys.readouterr().out
        assert rc_rej == 0, out_rej
        assert "Done: rejected" in out_rej
        assert "status=rejected" in out_rej

        statuses = {r.view(with_steps=False)["order_status"] for r in fresh_reg.runs.values()}
        assert statuses == {"placed", "rejected"}
        assert {o["status"] for o in shared_gw.orders.values()} == {"placed", "rejected"}

    assert _leaked_app_threads(threads_before) == []
    assert _keyless_env == []


def test_live_http_via_server_real_gateway_replay_and_clarification_e2e(
    _keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    replay_dir = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
    if not (replay_dir / "meta.json").is_file():
        pytest.skip("bundled replay snapshot not present")

    import aimo.backend.catalog_store as cs
    import aimo.catalog as cat
    from aimo.backend import client as bc
    from aimo.backend import orders as store
    from aimo.backend import replay
    from aimo.catalog import textsearch
    from aimo.gateway.app import create_app
    from aimo.models import Quote
    from aimo.trust import signing

    data_dir = tmp_path / "gw_data"
    state_db = tmp_path / "gw_state.sqlite"
    key_dir = tmp_path / "gw_keys"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    orig_source = cat._SOURCE
    monkeypatch.setenv("AIMO_REPLAY", str(replay_dir))
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)

    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(store, "DB_PATH", state_db)

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    monkeypatch.setattr(cat, "_SOURCE", src)
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    monkeypatch.setattr(bc, "_client", replay.ReplayClient(replay_dir))
    human_auth.reset_for_tests()

    fresh_reg = runs.RunRegistry()
    orig_execute = fresh_reg._execute

    def _fast_execute(run_obj: runs.Run, transport_factory: Any, agent_kwargs: dict, mcp_url: str | None = None) -> None:
        merged_kw = {"poll_s": 0.02, "approval_timeout_s": 10.0, **(agent_kwargs or {})}
        orig_execute(run_obj, transport_factory, merged_kw, mcp_url)

    monkeypatch.setattr(fresh_reg, "_execute", _fast_execute)
    monkeypatch.setattr(runs, "registry", fresh_reg)

    recorded: list[dict[str, Any]] = []
    gw_app = _wrap_asgi_recorder(create_app(with_mcp=False), recorded)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = int(sock.getsockname()[1])
    base = f"http://127.0.0.1:{port}"

    cfg = uvicorn.Config(gw_app, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=1)
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None
    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}
    srv_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="uvicorn-real-gw")
    srv_thread.start()

    for _ in range(150):
        if server.started:
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except Exception:
                pass
        time.sleep(0.01)

    try:
        cat.load_catalog()
        rc_hero = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "inproc",
                "--auto-approve-as",
                ANNA,
                "--approve-delay",
                "0.01",
            ]
        )
        out_hero = capsys.readouterr().out
        assert rc_hero == 0, out_hero
        assert "[injection_screen: clean]" in out_hero
        assert "status=placed" in out_hero

        recorded.clear()
        rc_clar = run_demo.main(
            [
                "--via-server",
                "--base-url",
                base,
                "--transport",
                "inproc",
                "--text",
                "We need lactose-free milk next week",
                "--answer",
                "auto",
                "--auto-approve-as",
                ANNA,
                "--approve-delay",
                "0.01",
            ]
        )
        out_clar = capsys.readouterr().out
        assert rc_clar == 0, out_clar
        assert "NEEDS_CLARIFICATION" in out_clar
        assert "[auto-answer] d0:" in out_clar
        assert "[auto-answer] q0:" in out_clar
        assert "status=placed" in out_clar

        ans_posts = [r for r in recorded if r["method"] == "POST" and r["path"].endswith("/answer")]
        assert len(ans_posts) == 2
        assert [r["status"] for r in ans_posts] == [200, 200]

        jwks = httpx.get(f"{base}/.well-known/jwks.json", timeout=5.0).json()
        placed = store.list_orders(status="placed")
        assert len(placed) == 2
        for order_obj in placed:
            quote_obj = store.get_quote(order_obj.quote_id)
            assert quote_obj is not None
            assert isinstance(quote_obj, Quote)
            assert signing.verify_quote(quote_obj, jwks_=jwks) is True

        audit_ver = httpx.get(f"{base}/v1/audit/verify", timeout=5.0).json()
        assert audit_ver["ok"] is True
        assert audit_ver.get("broken_at") is None
    finally:
        for t in list(threading.enumerate()):
            if t.name.startswith("buyer-run-") and t.is_alive():
                t.join(timeout=3.0)
        server.should_exit = True
        srv_thread.join(timeout=5.0)
        sock.close()
        try:
            textsearch._con().close()
        except Exception:
            pass
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            con.close()
        monkeypatch.setattr(cat, "_SOURCE", orig_source)

    assert not srv_thread.is_alive()
    assert _leaked_app_threads(threads_before) == []
    assert _keyless_env == []


def test_keyless_montage_six_scenarios_pass_with_mock_header_and_no_thread_leaks(
    _keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    workspace_montage = ROOT / "docs" / "demo-montage.md"
    before_exists = workspace_montage.exists()
    before_bytes = workspace_montage.read_bytes() if before_exists else None

    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}
    montage_out = tmp_path / "montage.md"

    rc = run_demo.main(
        [
            "--montage",
            "--transport",
            "fake",
            "--approve-delay",
            "0.01",
            "--montage-out",
            str(montage_out),
        ]
    )
    stdout = capsys.readouterr().out
    assert rc == 0, stdout
    assert montage_out.is_file()
    assert "[injection_screen: clean]" in stdout
    assert "supplier text(s) flagged+redacted" in stdout

    leaked = [t.name for t in threading.enumerate() if t.is_alive() and t.ident not in threads_before]
    assert leaked == []

    if before_exists:
        assert workspace_montage.read_bytes() == before_bytes
    else:
        assert not workspace_montage.exists()

    md = montage_out.read_text(encoding="utf-8")
    assert md.startswith("\x23 Demo montage results\n")
    header_section = md.split("| Scenario |", 1)[0]
    assert MOCK_MODEL_NAME in header_section
    assert f"Agent model `{MOCK_MODEL_NAME}`" in header_section
    assert "(transport: fake)" in header_section
    assert "escalation off." in header_section

    assert md.count("| PASS |") == 6
    assert "| FAIL |" not in md

    lines = md.splitlines()
    for name in run_demo.MONTAGE:
        matching_rows = [ln for ln in lines if ln.startswith(f"| {name} |")]
        assert len(matching_rows) == 1, (name, matching_rows)
        assert "| PASS |" in matching_rows[0]
        assert "| $0.0000 |" in matching_rows[0]

    this_run_rows = [ln for ln in lines if ln.startswith("| This run |")]
    assert len(this_run_rows) == 1
    assert "| This run | 6 | 5 | 0 |" in this_run_rows[0]
    assert "| $0.0000 | $0.0000 | $0.0000 |" in this_run_rows[0]
    assert "**Approvals:**" in md
    assert "1 not placed (drift: needs_confirmation)" in md
    assert _keyless_env == []


def test_keyless_montage_with_baseline_pid_comparison_row(
    _keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    baseline_row = {
        "pid": 424242,
        "run_id": "run-base-1",
        "feature": "buyer.agent_step",
        "model": "gemini-3.8-flash",
        "in": 2000,
        "cached": 500,
        "out": 200,
        "thinking": 100,
        "latency_s": 0.5,
        "ok": True,
    }
    (ledger_dir / "baseline.jsonl").write_text(json.dumps(baseline_row) + "\n", encoding="utf-8")

    montage_out = tmp_path / "montage_baseline.md"
    rc = run_demo.main(
        [
            "--montage",
            "--transport",
            "fake",
            "--approve-delay",
            "0.01",
            "--baseline-pid",
            "424242",
            "--baseline-orders",
            "5",
            "--baseline-label",
            "Round 2 baseline",
            "--montage-out",
            str(montage_out),
        ]
    )
    capsys.readouterr()
    assert rc == 0
    md = montage_out.read_text(encoding="utf-8")
    assert "| Round 2 baseline | 1 | 5 | 1 |" in md
    assert md.count("| PASS |") == 6
    assert MOCK_MODEL_NAME in md
    assert _keyless_env == []


def test_buyer_code_hygiene_zero_dashes_and_zero_comments() -> None:
    buyer_dir = ROOT / "aimo" / "buyer"
    buyer_modules = sorted(buyer_dir.glob("*.py"))
    assert len(buyer_modules) >= 16

    self_path = Path(__file__).resolve()
    targets = [*buyer_modules, self_path]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        assert "\u2014" not in src, f"{path.name} contains an em dash"
        assert "\u2013" not in src, f"{path.name} contains an en dash"
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
        assert comments == [], f"{path.name} contains comment tokens: {comments}"

    self_src = self_path.read_text(encoding="utf-8")
    assert "\x23" not in self_src
    assert all(ord(ch) < 128 for ch in self_src)
