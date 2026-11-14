import base64
from datetime import date
import json
import os
import subprocess
import sys
import pytest

sys.path.insert(0, "scenarios")
import run_all
from _common import (
    allergens_of,
    best_picks_covering,
    decode_jws,
    next_delivery_day,
    next_monday,
    picks_from,
    product_db,
    table,
    violates,
)


def test_table():
    out, fails = table([(True, "a", ""), (False, "b", "boom")])
    assert fails == 1 and "FAIL  b" in out and "1/2 passed" in out
    assert callable(run_all.run_all)


def test_picks_from():
    single = {"items": [{"offers": [{"product_id": "p1", "units_needed": 4}]}]}
    assert picks_from(single) == [{"product_id": "p1", "quantity": 4, "substitute_for": None}]

    split = {
        "items": [
            {
                "offers": [
                    {"product_id": "p1", "units_needed": 6},
                    {"product_id": "p2", "units_needed": 4},
                    {"product_id": "p3", "units_needed": 2, "substitute_for": "p1"},
                    {"product_id": "p4", "units_needed": 0},
                ]
            }
        ]
    }
    assert picks_from(split) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 4, "substitute_for": None},
        {"product_id": "p3", "quantity": 2, "substitute_for": "p1"},
    ]
    assert picks_from(split, per_item=1) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
    ]


def test_best_picks_covering():
    single = {
        "need_item": {"quantity": 50},
        "offers": [{"product_id": "p1", "units_needed": 5, "available_amount": 20}],
    }
    assert best_picks_covering(single) == [{"product_id": "p1", "quantity": 5, "substitute_for": None}]

    split = {
        "need_item": {"quantity": 100},
        "offers": [
            {"product_id": "p1", "units_needed": 6, "available_amount": 6},
            {"product_id": "p2", "units_needed": 4, "available_amount": 10, "substitute_for": "p1"},
            {"product_id": "p3", "units_needed": 3, "available_amount": 0},
        ],
    }
    assert best_picks_covering(split) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 4, "substitute_for": "p1"},
    ]
    assert best_picks_covering(split, need_units=8) == [
        {"product_id": "p1", "quantity": 6, "substitute_for": None},
        {"product_id": "p2", "quantity": 2, "substitute_for": "p1"},
    ]


def _write_products(folder, items):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "valio_aimo_junction26_product_data.json").write_text(json.dumps(items), encoding="utf-8")


def test_allergens_and_violates(tmp_path, monkeypatch):
    d1 = tmp_path / "d1"
    _write_products(
        d1,
        [
            {
                "id": "101",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [
                                {"id": "AP", "unit": "CONTAINS"},
                                {"id": "ML", "unit": "FREE_FROM"},
                            ],
                        }
                    ]
                },
            },
            {
                "id": "102",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [{"id": "AN", "unit": "MAY_CONTAIN"}],
                        }
                    ]
                },
            },
            {"id": "103", "deleted": False, "synkkaData": {"classifications": []}},
        ],
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(d1))
    assert allergens_of("101") == {"AP"}
    assert allergens_of("102") == {"AN"}
    assert allergens_of("103") == set()
    assert allergens_of("999") is None
    assert violates("101", {"peanuts"})
    assert violates("101", {"nuts"})
    assert not violates("101", {"lactose"})
    assert violates("102", {"nuts"})
    assert violates("102", {"tree nuts"})
    assert not violates("102", {"egg"})
    assert not violates("103", {"nuts", "peanuts", "egg"})
    assert not violates("999", {"nuts"})

    d2 = tmp_path / "d2"
    _write_products(
        d2,
        [
            {
                "id": "201",
                "deleted": False,
                "synkkaData": {
                    "classifications": [
                        {
                            "name": "allergen",
                            "values": [{"id": "AE", "unit": "MAY_CONTAIN"}],
                        }
                    ]
                },
            }
        ],
    )
    monkeypatch.setenv("AIMO_DATA_DIR", str(d2))
    assert set(product_db()) == {"201"}
    assert allergens_of("101") is None
    assert allergens_of("201") == {"AE"}
    assert violates("201", {"egg"})


def test_allergens_of_missing_data_dir(tmp_path, monkeypatch):
    missing = tmp_path / "empty_dir"
    missing.mkdir()
    monkeypatch.setenv("AIMO_DATA_DIR", str(missing))
    assert allergens_of("101") is None
    assert not violates("101", {"nuts", "milk"})
    with pytest.raises(FileNotFoundError):
        product_db()


def test_next_monday_and_next_delivery_day():
    fri = date(2026, 11, 13)
    sat = date(2026, 11, 14)
    sun = date(2026, 11, 15)
    mon = date(2026, 11, 16)
    thu = date(2026, 11, 12)

    assert next_monday(fri) == date(2026, 11, 16)
    assert next_monday(sat) == date(2026, 11, 16)
    assert next_monday(sun) == date(2026, 11, 16)
    assert next_monday(mon) == date(2026, 11, 23)

    assert next_delivery_day(today=fri) == date(2026, 11, 16)
    assert next_delivery_day(today=sat) == date(2026, 11, 16)
    assert next_delivery_day(today=sun) == date(2026, 11, 17)
    assert next_delivery_day(today=mon) == date(2026, 11, 18)
    assert next_delivery_day(today=thu) == date(2026, 11, 16)
    assert next_delivery_day(min_days=1, today=fri) == date(2026, 11, 16)


def test_decode_jws():
    def _b64u(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    for payload in (
        {"quote_id": "q_1", "total": 12.5},
        {"quote_id": "q_long_padding_2", "note": "laktoositon maito"},
        {"quote_id": "q_333", "items": [1, 2, 3]},
    ):
        hdr = {"alg": "EdDSA", "kid": "k1"}
        token = f"{_b64u(hdr)}.{_b64u(payload)}.sig_part"
        got_h, got_p = decode_jws(token)
        assert got_h == hdr
        assert got_p == payload


def _proc(stdout="", stderr="", returncode=1):
    return subprocess.CompletedProcess(args=["scen"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_reason_no_false_positive_on_port_or_ids():
    p = _proc(
        stdout='  [PASS] quote ok\n  [FAIL] tamper rejected (order_id="ord_4290")\n',
        stderr="",
    )
    server_log = (
        "INFO: Uvicorn running on http://127.0.0.1:54291\n"
        'INFO: 127.0.0.1:54291 - "POST /v1/orders HTTP/1.1" 200 OK\n'
    )
    assert run_all.reason(p, server_log) == 'tamper rejected (order_id="ord_4290")'

    p_valio = _proc(
        stdout='  [FAIL] Valio item 465039 mismatch (got 0)\n',
        stderr="url=http://127.0.0.1:50312/v1/offers\n",
    )
    assert run_all.reason(p_valio, "") == "Valio item 465039 mismatch (got 0)"


def test_reason_detects_real_429_and_valio_503():
    expected = "VALIO 429 (mock rate limit), not a scenario bug"
    assert run_all.reason(_proc(stderr="HTTP/1.1 429 Too Many Requests")) == expected
    assert run_all.reason(_proc(stdout=""), log="Client error '429 Too Many Requests'") == expected
    assert run_all.reason(_proc(stderr="upstream status=429")) == expected
    assert run_all.reason(_proc(stderr='{"code":"UPSTREAM_UNAVAILABLE"}')) == expected
    assert (
        run_all.reason(
            _proc(stderr="HTTPStatusError: 503 Service Unavailable"),
            log="Valio upstream HTTP/1.1 503 Service Unavailable",
        )
        == expected
    )


def test_reason_extracts_fail_stderr_and_no_output():
    long_detail = "x" * 200
    p_fail = _proc(stdout=f"  [PASS] step1\n  [FAIL] check_label ({long_detail})\n  [FAIL] second\n")
    got = run_all.reason(p_fail)
    assert got.startswith("check_label (")
    assert len(got) == 140

    p_err = _proc(stdout="  [PASS] step1\n", stderr="Traceback (most recent call last):\n  File 'x.py'\nValueError: bad json\n")
    assert run_all.reason(p_err) == "ValueError: bad json"

    p_empty = _proc(stdout="", stderr="   \n")
    assert run_all.reason(p_empty) == "no output"


def test_run_all_only_filtering_and_llm_env_strip(monkeypatch):
    calls = []
    monkeypatch.setenv("LLM_API_KEY", "secret-token")
    monkeypatch.setenv("LLM_MODEL", "demo-model")

    def fake_run(cmd, capture_output, text, env, cwd):
        calls.append((cmd, env, cwd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(run_all.subprocess, "run", fake_run)
    rows = run_all.run_all("http://127.0.0.1:54291", only="s1", fresh=False)
    assert rows == [(True, "s1_milk", "")]
    assert len(calls) == 1
    cmd, env, cwd = calls[0]
    assert cmd[1].endswith("s1_milk.py")
    assert cmd[2:] == ["--base", "http://127.0.0.1:54291"]
    assert env["AIMO_URL"] == "http://127.0.0.1:54291"
    assert "LLM_API_KEY" not in env and "LLM_MODEL" not in env
    assert cwd == run_all.ROOT


def test_run_all_fresh_boot_isolation_and_cleanup(monkeypatch):
    monkeypatch.setenv("AIMO_DB", "/tmp/outer-should-not-win.db")
    monkeypatch.delenv("AIMO_KEY_DIR", raising=False)
    monkeypatch.delenv("AIMO_DRIFT_STATE", raising=False)
    captured = {}

    class FakeProc:
        def __init__(self):
            self.terminated = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, env, stdout, stderr, cwd):
        d = os.path.dirname(stdout.name)
        stdout.write("INFO: Uvicorn running on http://127.0.0.1:54291\n")
        stdout.flush()
        captured["cmd"] = cmd
        captured["env"] = dict(env)
        captured["logf"] = stdout
        captured["dir"] = d
        captured["dir_existed_at_boot"] = os.path.isdir(d)
        proc = FakeProc()
        captured["proc"] = proc
        return proc

    class FakeResp:
        status_code = 200

    def fake_run(cmd, capture_output, text, env, cwd):
        captured["dir_existed_at_run"] = os.path.isdir(captured["dir"])
        captured["run_env"] = dict(env)
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="  [FAIL] expected 200 (got 409)\n",
            stderr="",
        )

    monkeypatch.setattr(run_all.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_all.httpx, "get", lambda url: FakeResp())
    monkeypatch.setattr(run_all.subprocess, "run", fake_run)
    monkeypatch.setattr(run_all.time, "sleep", lambda s: None)

    rows = run_all.run_all("http://127.0.0.1:54291", only="s0", fresh=True)
    assert rows == [(False, "s0_guardrails", "expected 200 (got 409)")]

    d = captured["dir"]
    assert captured["dir_existed_at_boot"]
    assert captured["dir_existed_at_run"]
    assert captured["env"]["AIMO_DB"] == os.path.join(d, "aimo.db")
    assert captured["env"]["AIMO_KEY_DIR"] == os.path.join(d, "keys")
    assert captured["env"]["AIMO_DRIFT_STATE"] == os.path.join(d, "drift.json")
    assert captured["proc"].terminated and captured["proc"].waited
    assert captured["logf"].closed
    assert not os.path.exists(d)


def test_run_all_cleans_up_on_runner_exception(monkeypatch):
    captured = {}

    class FakeProc:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, env, stdout, stderr, cwd):
        captured["logf"] = stdout
        captured["dir"] = os.path.dirname(stdout.name)
        captured["proc"] = FakeProc()
        return captured["proc"]

    class FakeResp:
        status_code = 200

    def boom_run(*a, **kw):
        raise RuntimeError("runner crashed")

    monkeypatch.setattr(run_all.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_all.httpx, "get", lambda url: FakeResp())
    monkeypatch.setattr(run_all.subprocess, "run", boom_run)

    with pytest.raises(RuntimeError, match="runner crashed"):
        run_all.run_all("http://127.0.0.1:18080", only="s1", fresh=True)

    assert captured["proc"].terminated
    assert captured["logf"].closed
    assert not os.path.exists(captured["dir"])


def test_boot_dead_process_exits_immediately(monkeypatch):
    sleeps = []
    health_checks = []

    class DeadProc:
        def __init__(self, polls):
            self._polls = list(polls)

        def poll(self):
            return self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(run_all.time, "sleep", lambda s: sleeps.append(s))

    def fail_get(url):
        health_checks.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(run_all.httpx, "get", fail_get)

    monkeypatch.setattr(run_all.subprocess, "Popen", lambda *a, **kw: DeadProc([1]))
    srv, logf = run_all.boot("http://127.0.0.1:18081")
    d1 = os.path.dirname(logf.name)
    assert sleeps == []
    assert health_checks == []
    run_all.cleanup(srv, logf)
    assert logf.closed and not os.path.exists(d1)

    monkeypatch.setattr(run_all.subprocess, "Popen", lambda *a, **kw: DeadProc([None, 1]))
    srv2, logf2 = run_all.boot("http://127.0.0.1:18082")
    d2 = os.path.dirname(logf2.name)
    assert sleeps == []
    assert len(health_checks) == 1
    run_all.cleanup(srv2, logf2)
    assert logf2.closed and not os.path.exists(d2)
