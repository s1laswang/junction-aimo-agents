"""Model fallback tests for aimo.gemini.generate across daily quota, transient 429, env overrides, and ledger rows."""
from __future__ import annotations

import json
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from aimo import gemini

DAILY_PER_DAY = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_requests_per_model_per_day, limit: 10000, model: gemini-3.8-flash"
)
DAILY_PERDAY = "429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel limit reached"
DAILY_PER_SPACE_DAY = "429 RESOURCE_EXHAUSTED: quota exceeded per day for gemini-3.8-flash"
TRANSIENT_429 = "429 RESOURCE_EXHAUSTED: rate limit per minute exceeded"


class FakeModels:
    def __init__(self, behaviour: dict[str, Any]) -> None:
        self.behaviour = behaviour
        self.calls: list[str] = []

    def generate_content(self, model: str, contents: Any, config: Any = None) -> Any:
        self.calls.append(model)
        spec = self.behaviour[model]
        if isinstance(spec, list):
            out = spec.pop(0) if len(spec) > 1 else spec[0]
        elif callable(spec):
            out = spec()
        else:
            out = spec
        if isinstance(out, Exception):
            raise out
        if hasattr(out, "usage_metadata"):
            return out
        return types.SimpleNamespace(text=out, usage_metadata=None)


def _read_ledger(ledger_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fp in sorted(ledger_dir.glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sleeps: list[float] = []
    ledger_dir = tmp_path / "ledger"

    def install(behaviour: dict[str, Any]) -> FakeModels:
        models = FakeModels(behaviour)
        monkeypatch.setattr(gemini, "client", lambda: types.SimpleNamespace(models=models))
        return models

    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("AIMO_GEMINI_TRACE", raising=False)
    gemini._exhausted.clear()
    gemini._tl.model = None
    yield install, sleeps, ledger_dir
    gemini._exhausted.clear()
    gemini._tl.model = None


def test_file_hygiene_no_comments_or_dashes() -> None:
    test_src = Path(__file__).read_text(encoding="utf-8")
    mod_src = Path(gemini.__file__).read_text(encoding="utf-8")
    for src in (test_src, mod_src):
        assert "\x23" not in src
        assert "\u2014" not in src
        assert "\u2013" not in src


def test_requested_model_first_when_healthy(fake_env) -> None:
    install, sleeps, ledger_dir = fake_env
    models = install({"gemini-3.8-flash": "ok-primary", "gemini-3.5-flash": "ok-fallback"})
    caller_meta = {"step": 0, "thinking_level": "LOW"}

    r = gemini.generate("buyer.step", "gemini-3.8-flash", "hi", run_id="run-healthy", meta=caller_meta)

    assert r.text == "ok-primary"
    assert gemini.last_model() == "gemini-3.8-flash"
    assert models.calls == ["gemini-3.8-flash"]
    assert sleeps == []

    rows = _read_ledger(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["model"] == "gemini-3.8-flash"
    assert rows[0]["run_id"] == "run-healthy"
    assert rows[0]["meta"] == {"step": 0, "thinking_level": "LOW"}
    assert "fallback_from" not in rows[0]["meta"]


@pytest.mark.parametrize("daily_msg", [DAILY_PER_DAY, DAILY_PERDAY, DAILY_PER_SPACE_DAY])
def test_daily_quota_switches_immediately_records_ledger_and_skips_for_30m(
    fake_env,
    daily_msg: str,
) -> None:
    install, sleeps, ledger_dir = fake_env
    models = install(
        {
            "gemini-3.8-flash": RuntimeError(daily_msg),
            "gemini-3.5-flash": "ok-fallback",
            "gemini-4-hydrogen": "ok-secondary",
        }
    )
    caller_meta = {"step": 2}

    r1 = gemini.generate("buyer.agent_step", "gemini-3.8-flash", "hello", run_id="run-dq", meta=caller_meta)

    assert r1.text == "ok-fallback"
    assert gemini.last_model() == "gemini-3.5-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash"]
    assert sleeps == []
    assert caller_meta == {"step": 2}
    assert "gemini-3.8-flash" in gemini._exhausted

    r2 = gemini.generate("buyer.agent_step", "gemini-3.8-flash", "hello again", run_id="run-dq", meta=caller_meta)
    assert r2.text == "ok-fallback"
    assert gemini.last_model() == "gemini-3.5-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash"]
    assert sleeps == []

    rows = _read_ledger(ledger_dir)
    assert len(rows) == 3
    assert rows[0]["ok"] is False
    assert rows[0]["model"] == "gemini-3.8-flash"
    assert rows[0]["meta"] == {"step": 2}
    assert rows[1]["ok"] is True
    assert rows[1]["model"] == "gemini-3.5-flash"
    assert rows[1]["meta"] == {"step": 2, "fallback_from": "gemini-3.8-flash"}
    assert rows[2]["ok"] is True
    assert rows[2]["model"] == "gemini-3.5-flash"
    assert rows[2]["meta"] == {"step": 2, "fallback_from": "gemini-3.8-flash"}


def test_exhausted_model_reenters_candidates_after_ttl_expires(
    fake_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, sleeps, _ = fake_env
    clock = [1000.0]
    monkeypatch.setattr(gemini.time, "time", lambda: clock[0])

    models = install(
        {
            "gemini-3.8-flash": [RuntimeError(DAILY_PER_DAY), "recovered-primary"],
            "gemini-3.5-flash": "ok-fallback",
            "gemini-4-hydrogen": "ok-secondary",
        }
    )

    r1 = gemini.generate("test", "gemini-3.8-flash", "first")
    assert r1.text == "ok-fallback"
    assert gemini._exhausted["gemini-3.8-flash"] == pytest.approx(1000.0 + gemini.EXHAUSTED_S)

    clock[0] = 1000.0 + gemini.EXHAUSTED_S - 1.0
    r2 = gemini.generate("test", "gemini-3.8-flash", "still-within-window")
    assert r2.text == "ok-fallback"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash"]

    clock[0] = 1000.0 + gemini.EXHAUSTED_S + 1.0
    r3 = gemini.generate("test", "gemini-3.8-flash", "after-window")
    assert r3.text == "recovered-primary"
    assert gemini.last_model() == "gemini-3.8-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash", "gemini-3.8-flash"]
    assert sleeps == []


def test_transient_429_retries_once_then_switches_to_fallback(fake_env) -> None:
    install, sleeps, ledger_dir = fake_env
    models = install(
        {
            "gemini-3.8-flash": [RuntimeError(TRANSIENT_429), RuntimeError(TRANSIENT_429), "recovered"],
            "gemini-3.5-flash": "ok-after-two-429s",
            "gemini-4-hydrogen": "ok-third",
        }
    )

    r = gemini.generate("buyer.step", "gemini-3.8-flash", "hi", retries=5, meta={"attempt_test": True})

    assert r.text == "ok-after-two-429s"
    assert gemini.last_model() == "gemini-3.5-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash"]
    assert len(sleeps) == 1
    assert "gemini-3.8-flash" not in gemini._exhausted

    rows = _read_ledger(ledger_dir)
    assert [(row["model"], row["ok"], row["meta"].get("fallback_from")) for row in rows] == [
        ("gemini-3.8-flash", False, None),
        ("gemini-3.8-flash", False, None),
        ("gemini-3.5-flash", True, "gemini-3.8-flash"),
    ]

    r_next = gemini.generate("buyer.step", "gemini-3.8-flash", "hi again", retries=5)
    assert r_next.text == "recovered"
    assert gemini.last_model() == "gemini-3.8-flash"
    assert models.calls[-1] == "gemini-3.8-flash"


def test_transient_429_single_blip_stays_on_primary_model(fake_env) -> None:
    install, sleeps, _ = fake_env
    models = install(
        {
            "gemini-3.8-flash": [RuntimeError(TRANSIENT_429), "ok-on-second-try"],
            "gemini-3.5-flash": "unused-fallback",
        }
    )

    r = gemini.generate("buyer.step", "gemini-3.8-flash", "hi", retries=5)
    assert r.text == "ok-on-second-try"
    assert gemini.last_model() == "gemini-3.8-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.8-flash"]
    assert len(sleeps) == 1


def test_transient_429_on_last_candidate_uses_all_retries_before_raising(
    fake_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, sleeps, _ = fake_env
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash")
    models = install(
        {
            "gemini-3.8-flash": RuntimeError(TRANSIENT_429),
            "gemini-3.5-flash": RuntimeError(TRANSIENT_429),
        }
    )

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hi", retries=3)

    assert models.calls == [
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert len(sleeps) == 4


def test_non_quota_error_raises_immediately_without_masking(fake_env) -> None:
    install, sleeps, ledger_dir = fake_env
    gemini._tl.model = "stale-previous-model"
    models = install(
        {
            "gemini-3.8-flash": ValueError("400 INVALID_ARGUMENT bad schema"),
            "gemini-3.5-flash": "should-not-be-called",
        }
    )

    with pytest.raises(ValueError, match="400 INVALID_ARGUMENT"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hi")

    assert models.calls == ["gemini-3.8-flash"]
    assert sleeps == []
    assert gemini.last_model() is None
    assert gemini.last_model("default-model") == "default-model"
    rows = _read_ledger(ledger_dir)
    assert len(rows) == 1 and rows[0]["ok"] is False and rows[0]["error"] == "ValueError"


def test_all_models_exhausted_raises_quota_error_and_falls_back_to_primary_when_all_skipped(
    fake_env,
) -> None:
    install, sleeps, _ = fake_env
    models = install(
        {
            "gemini-3.8-flash": RuntimeError(DAILY_PER_DAY),
            "gemini-3.5-flash": RuntimeError(DAILY_PER_DAY),
            "gemini-4-hydrogen": RuntimeError(DAILY_PER_DAY),
        }
    )

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hi")

    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-4-hydrogen"]
    assert sleeps == []
    assert set(gemini._exhausted) == {"gemini-3.8-flash", "gemini-3.5-flash", "gemini-4-hydrogen"}

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hi-when-all-in-exhausted")

    assert models.calls == [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
    ]


def test_gemini_fallback_models_env_override(
    fake_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, sleeps, ledger_dir = fake_env
    monkeypatch.setenv(
        "GEMINI_FALLBACK_MODELS",
        " gemini-4-carbon , gemini-3.8-flash , gemini-4-argon , gemini-4-carbon ",
    )
    assert gemini.fallback_chain("gemini-3.8-flash") == [
        "gemini-3.8-flash",
        "gemini-4-carbon",
        "gemini-4-argon",
    ]

    models = install(
        {
            "gemini-3.8-flash": RuntimeError(DAILY_PER_DAY),
            "gemini-4-carbon": "ok-from-env-override",
            "gemini-4-argon": "ok-backup",
        }
    )
    r = gemini.generate("buyer.step", "gemini-3.8-flash", "hi")
    assert r.text == "ok-from-env-override"
    assert gemini.last_model() == "gemini-4-carbon"
    assert models.calls == ["gemini-3.8-flash", "gemini-4-carbon"]
    assert sleeps == []

    rows = _read_ledger(ledger_dir)
    assert rows[-1]["model"] == "gemini-4-carbon"
    assert rows[-1]["meta"]["fallback_from"] == "gemini-3.8-flash"

    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "   ,  ")
    assert gemini.fallback_chain("gemini-3.8-flash") == ["gemini-3.8-flash"]


def test_last_model_is_thread_local(fake_env) -> None:
    install, _, _ = fake_env
    install(
        {
            "gemini-3.8-flash": RuntimeError(DAILY_PER_DAY),
            "gemini-3.5-flash": "ok-worker",
            "gemini-4-hydrogen": "ok-main",
        }
    )

    gemini.generate("main", "gemini-4-hydrogen", "hi")
    assert gemini.last_model() == "gemini-4-hydrogen"

    worker_models: list[str | None] = []

    def worker() -> None:
        worker_models.append(gemini.last_model())
        gemini.generate("worker", "gemini-3.8-flash", "hi")
        worker_models.append(gemini.last_model())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)

    assert worker_models == [None, "gemini-3.5-flash"]
    assert gemini.last_model() == "gemini-4-hydrogen"


def test_raw_trace_records_request_response_and_fallback(
    fake_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, _, _ = fake_env
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace))
    install({"gemini-3.8-flash": Exception(DAILY_PER_DAY), "gemini-3.5-flash": "ok"})

    gemini.generate("test", "gemini-3.8-flash", "hello there", run_id="r1")

    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [(r["model"], r["ok"]) for r in rows] == [("gemini-3.8-flash", False), ("gemini-3.5-flash", True)]
    assert rows[0]["feature"] == "test" and rows[0]["run_id"] == "r1"
    assert rows[0]["requested_model"] == "gemini-3.8-flash" and rows[0]["attempt"] == 0
    assert isinstance(rows[0]["latency_s"], float) and rows[0]["ts"].endswith("Z")
    assert "RESOURCE_EXHAUSTED" in rows[0]["error"]
    assert "request" not in rows[0] and "response" not in rows[0] and "usage" not in rows[0]
    assert rows[1]["requested_model"] == "gemini-3.8-flash" and rows[1]["meta"]["fallback_from"] == "gemini-3.8-flash"
    assert rows[1]["attempt"] == 0 and isinstance(rows[1]["latency_s"], float)
    assert rows[1]["request"] == {"contents": "hello there", "config": None}
    assert "usage" in rows[1] and rows[1]["response"] and rows[1]["ts"].endswith("Z")
    assert "error" not in rows[1]


def test_raw_trace_disabled_when_unset_or_blank(
    fake_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, _, ledger_dir = fake_env
    install({"gemini-3.8-flash": "ok"})

    monkeypatch.delenv("AIMO_GEMINI_TRACE", raising=False)
    gemini.generate("test", "gemini-3.8-flash", "first")

    monkeypatch.setenv("AIMO_GEMINI_TRACE", "")
    gemini.generate("test", "gemini-3.8-flash", "second")

    monkeypatch.setenv("AIMO_GEMINI_TRACE", "   ")
    gemini.generate("test", "gemini-3.8-flash", "third")

    extra = [p for p in tmp_path.iterdir() if p != ledger_dir]
    assert extra == []
    assert len(_read_ledger(ledger_dir)) == 3


def test_raw_trace_unwritable_path_never_breaks_generate(
    fake_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, _, _ = fake_env
    dir_target = tmp_path / "trace-is-a-dir"
    dir_target.mkdir()
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(dir_target))

    install({"gemini-3.8-flash": "ok-despite-dir"})
    r = gemini.generate("test", "gemini-3.8-flash", "hi")
    assert r.text == "ok-despite-dir"

    missing_parent = tmp_path / "no-such-parent" / "nested" / "trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(missing_parent))
    r2 = gemini.generate("test", "gemini-3.8-flash", "hi-again")
    assert r2.text == "ok-despite-dir"
    assert not missing_parent.exists()

    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(dir_target))
    install({"gemini-3.8-flash": ValueError("400 bad request")})
    with pytest.raises(ValueError, match="400 bad request"):
        gemini.generate("test", "gemini-3.8-flash", "bad")


def test_raw_serializes_model_dump_objects(
    fake_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, _, _ = fake_env

    class DumpableObj:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            self.calls: list[tuple[Any, Any]] = []

        def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
            self.calls.append((mode, exclude_none))
            if exclude_none:
                return {k: v for k, v in self.payload.items() if v is not None}
            return dict(self.payload)

    class BrokenDumpObj:
        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("dump boom")

        def __str__(self) -> str:
            return "broken-dump-fallback"

    d = DumpableObj({"prompt_token_count": 42, "candidates_token_count": 7, "empty": None})
    assert gemini._raw(d) == {"prompt_token_count": 42, "candidates_token_count": 7}
    assert d.calls == [("json", True)]
    assert gemini._raw(BrokenDumpObj()) == "broken-dump-fallback"
    assert gemini._raw({1: (DumpableObj({"a": 1, "b": None}), [True, 3.5, None])}) == {
        "1": [{"a": 1}, [True, 3.5, None]]
    }

    class FakeSDKResponse:
        def __init__(self) -> None:
            self.text = "structured-ok"
            self.usage_metadata = DumpableObj({"prompt_token_count": 11, "candidates_token_count": 5, "unused": None})

        def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
            return {"candidates": [{"content": {"parts": [{"text": self.text}]}}], "mode": mode}

    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace))
    install({"gemini-3.8-flash": lambda: FakeSDKResponse()})
    cfg = DumpableObj({"max_output_tokens": 512, "system_instruction": None})
    prompt = [DumpableObj({"role": "user", "text": "hello", "extra": None})]

    res = gemini.generate("buyer.step", "gemini-3.8-flash", prompt, config=cfg, run_id="r-dump")
    assert res.text == "structured-ok"

    row = json.loads(trace.read_text(encoding="utf-8").strip())
    assert row["usage"] == {"prompt_token_count": 11, "candidates_token_count": 5}
    assert row["request"] == {
        "contents": [{"role": "user", "text": "hello"}],
        "config": {"max_output_tokens": 512},
    }
    assert row["response"] == {
        "candidates": [{"content": {"parts": [{"text": "structured-ok"}]}}],
        "mode": "json",
    }


def test_raw_trace_never_logs_gemini_or_google_api_keys(
    fake_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install, _, _ = fake_env
    gemini_secret = "gemini-dummy-secret-998877"
    google_secret = "google-dummy-secret-665544"
    monkeypatch.setenv("GEMINI_API_KEY", gemini_secret)
    monkeypatch.setenv("GOOGLE_API_KEY", google_secret)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace))

    class EchoResponse:
        text = "done"
        usage_metadata = None

        def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
            return {
                "sdk_http_response": {"headers": {"x-goog-api-key": gemini_secret}},
                "candidates": [{"content": {"parts": [{"text": "done"}]}}],
            }

    install(
        {
            "gemini-3.8-flash": RuntimeError(f"{DAILY_PER_DAY} key={google_secret}"),
            "gemini-3.5-flash": lambda: EchoResponse(),
        }
    )

    r = gemini.generate(
        "buyer.step",
        "gemini-3.8-flash",
        "order milk",
        config={"api_key_debug": gemini_secret},
        run_id="r-sec",
    )
    assert r.text == "done"

    raw_text = trace.read_text(encoding="utf-8")
    assert gemini_secret not in raw_text
    assert google_secret not in raw_text
    rows = [json.loads(line) for line in raw_text.splitlines()]
    assert len(rows) == 2
    assert "[REDACTED]" in rows[0]["error"]
    assert rows[1]["request"]["config"]["api_key_debug"] == "[REDACTED]"
    assert rows[1]["response"]["sdk_http_response"]["headers"]["x-goog-api-key"] == "[REDACTED]"
