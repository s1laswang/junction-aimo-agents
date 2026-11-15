from __future__ import annotations

import ast
from datetime import datetime
import importlib
import io
import json
from pathlib import Path
import threading
import tokenize
import types
from typing import Any

import dotenv
import pytest

from aimo import config, gemini
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.tools import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "aimo" / "config.py"
GEMINI_PATH = ROOT / "aimo" / "gemini.py"
THIS_PATH = Path(__file__).resolve()
OWNED_FILES = (CONFIG_PATH, GEMINI_PATH, THIS_PATH)
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)

CONFIG_ENV_KEYS = (
    "AIMO_DATA_DIR",
    "AIMO_PORTAL_URL",
    "VALIO_API_BASE",
    "AIMO_API_KEY",
    "VALIO_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AIMO_STATE_DB",
    "AIMO_KEY_DIR",
    "GEMINI_AGENT_MODEL",
    "GEMINI_REASONING_MODEL",
    "GEMINI_FAST_MODEL",
    "GEMINI_EMBED_MODEL",
    "AIMO_OFFLINE",
    "GEMINI_FALLBACK_MODELS",
    "AIMO_GEMINI_TRACE",
)


@pytest.fixture(autouse=True)
def keyless_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("AIMO_GEMINI_TRACE", raising=False)
    monkeypatch.setattr(gemini, "LEDGER_DIR", tmp_path / "ledger")
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    gemini._clear_client_cache()
    gemini._exhausted.clear()
    gemini._tl.model = None
    yield
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()
    gemini._clear_client_cache()
    gemini._exhausted.clear()
    gemini._tl.model = None


@pytest.fixture
def isolated_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with monkeypatch.context() as mp:
        mp.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
        for key in CONFIG_ENV_KEYS:
            mp.delenv(key, raising=False)
        mp.setenv("AIMO_DATA_DIR", str(tmp_path / "data"))
        yield mp
    importlib.reload(config)


def _strict_no_nan(constant: str) -> Any:
    raise ValueError(f"Non-standard JSON constant forbidden: {constant}")


def _read_ledger_rows(ledger_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ledger_dir.is_dir():
        return rows
    for fp in sorted(ledger_dir.glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line, parse_constant=_strict_no_nan))
    return rows


class _BrokenIter:
    def __iter__(self):
        raise RuntimeError("broken iterator")


class _BrokenItemsIter:
    def __iter__(self):
        yield "ok"
        raise RuntimeError("midway failure")


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


class _BrokenUsageAttr:
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise RuntimeError(f"cannot read {name}")


class ReadError(Exception):
    pass


@pytest.mark.parametrize("target", OWNED_FILES, ids=lambda p: p.name)
def test_owned_files_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    ast.parse(src, filename=str(target))
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has comment tokens: {comments[:3]}"
    assert "\x23" not in src, f"{target.name} contains hash character"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


@pytest.mark.parametrize("bad_val", [None, True, False, 0, 123, 3.14, [], {}, "   ", "\x00\t\n "])
def test_config_clean_str_and_env_guard_bad_types_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    bad_val: Any,
) -> None:
    assert config._clean_str(bad_val) == ""
    assert config._clean_str("  \x00hello-world\x00  ") == "hello-world"
    monkeypatch.setenv("AIMO_API_KEY", "   ")
    monkeypatch.setenv("VALIO_API_KEY", "  valio-clean-token  ")
    env_fn: Any = config._env
    assert env_fn(None, 123, "", "AIMO_API_KEY", "VALIO_API_KEY", default="d") == "valio-clean-token"
    monkeypatch.setenv("VALIO_API_KEY", "   ")
    assert config._env("AIMO_API_KEY", "VALIO_API_KEY", default="fallback-v") == "fallback-v"


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "///",
        "http://",
        "https://",
        "https:///",
        "ftp://bad.example.test",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ws://socket.example.test",
        "not-a-url-at-all",
        "https://evil.example.test\r\nX-Injected: 1",
        "https://bad host with spaces.test",
    ],
)
def test_config_base_url_rejects_invalid_schemes_empty_hosts_and_crlf(
    isolated_config_env,
    bad_url: str,
) -> None:
    isolated_config_env.setenv("AIMO_PORTAL_URL", bad_url)
    isolated_config_env.setenv("VALIO_API_BASE", "  http://localhost:8080/v1///  ")
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "http://localhost:8080/v1"

    isolated_config_env.setenv("VALIO_API_BASE", bad_url)
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://junction26.valioaimo.fi"


def test_config_base_url_handles_nul_bytes_and_uppercase_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config.os,
        "environ",
        {"AIMO_PORTAL_URL": "\x00  HTTPS://Portal.Example.Test/base///  \x00"},
    )
    assert config._base_url() == "HTTPS://Portal.Example.Test/base"
    monkeypatch.setattr(config.os, "environ", {"AIMO_PORTAL_URL": "\x00///\x00", "VALIO_API_BASE": 999})
    assert config._base_url() == "https://junction26.valioaimo.fi"


@pytest.mark.parametrize(
    ("raw_model", "expected"),
    [
        ("  gemini-4-carbon  ", "gemini-4-carbon"),
        ("", "gemini-3.8-flash"),
        ("   ", "gemini-3.8-flash"),
        ("gemini 3.8 flash", "gemini-3.8-flash"),
        ("gemini\nflash", "gemini-3.8-flash"),
        ("gemini\tflash", "gemini-3.8-flash"),
    ],
)
def test_config_model_env_strips_valid_and_rejects_whitespace_or_control(
    isolated_config_env,
    raw_model: str,
    expected: str,
) -> None:
    isolated_config_env.setenv("GEMINI_AGENT_MODEL", raw_model)
    isolated_config_env.setenv("GEMINI_REASONING_MODEL", "   ")
    isolated_config_env.setenv("GEMINI_FAST_MODEL", "bad model")
    isolated_config_env.setenv("GEMINI_EMBED_MODEL", "  gemini-embedding-custom  ")
    importlib.reload(config)
    assert config.GEMINI_AGENT_MODEL == expected
    assert config.GEMINI_REASONING_MODEL == "gemini-4-hydrogen"
    assert config.GEMINI_FAST_MODEL == "gemini-3.1-flash-lite"
    assert config.GEMINI_EMBED_MODEL == "gemini-embedding-custom"


@pytest.mark.parametrize(
    ("raw_flag", "expected"),
    [
        ("1", True),
        ("  1  ", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        ("on", True),
        ("ON", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("   ", False),
        ("unexpected", False),
    ],
)
def test_config_offline_bool_parsing_variants(
    isolated_config_env,
    raw_flag: str,
    expected: bool,
) -> None:
    isolated_config_env.setenv("AIMO_OFFLINE", raw_flag)
    importlib.reload(config)
    assert config.OFFLINE is expected


def test_config_bool_env_handles_non_string_environ_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config.os,
        "environ",
        {"B_TRUE": True, "B_FALSE": False, "I_ONE": 1, "I_TWO": 2, "OBJ": object()},
    )
    assert config._bool_env("B_TRUE", default=False) is True
    assert config._bool_env("B_FALSE", default=True) is False
    assert config._bool_env("I_ONE", default=False) is True
    assert config._bool_env("I_TWO", default=True) is False
    assert config._bool_env("OBJ", default=True) is True
    assert config._bool_env("MISSING", default=True) is True


def test_config_reload_survives_file_blocker_data_dir_and_dotenv_errors(
    isolated_config_env,
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "regular_file_not_dir"
    blocker.write_text("occupied", encoding="utf-8")
    isolated_config_env.setenv("AIMO_DATA_DIR", str(blocker))
    isolated_config_env.setenv("AIMO_API_KEY", "   ")
    isolated_config_env.setenv("VALIO_API_KEY", "  v-key-trimmed  ")
    isolated_config_env.setenv("GEMINI_API_KEY", "   ")
    isolated_config_env.setenv("GOOGLE_API_KEY", "  g-key-trimmed  ")

    def boom_dotenv(*args: Any, **kwargs: Any) -> bool:
        raise PermissionError("dotenv unreadable")

    isolated_config_env.setattr(dotenv, "load_dotenv", boom_dotenv)
    importlib.reload(config)

    assert config.DATA_DIR == blocker
    assert config.AIMO_API_KEY == "v-key-trimmed"
    assert config.GEMINI_API_KEY == "g-key-trimmed"
    assert config.PRODUCT_JSON == blocker / "valio_aimo_junction26_product_data.json"
    assert config.CATALOG_DB == blocker / "catalog.sqlite"
    assert config.MCP_SPEC_VERSION == "2026-07-28"
    assert config.MCP_HANDSHAKE_VERSION == "2025-11-25"


def test_gemini_client_strips_env_key_and_is_thread_safe_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[dict[str, Any]] = []
    barrier = threading.Barrier(12)

    class StubClient:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    fake_genai = types.SimpleNamespace(Client=StubClient)
    monkeypatch.setitem(__import__("sys").modules, "google.genai", fake_genai)
    import google

    monkeypatch.setattr(google, "genai", fake_genai, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("GOOGLE_API_KEY", "  google-trimmed-key-12345  ")
    gemini.client.cache_clear()

    results: list[Any] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=5.0)
        c = gemini.client()
        with lock:
            results.append(c)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert len(results) == 12
    assert len({id(r) for r in results}) == 1
    assert constructed == [{"api_key": "google-trimmed-key-12345"}]
    assert gemini.client.cache_info().currsize == 1

    gemini.client.cache_clear()
    assert gemini.client.cache_info().currsize == 0
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    c2 = gemini.client()
    assert isinstance(c2, StubClient)
    assert constructed[-1] == {}


@pytest.mark.parametrize(
    ("usage_obj", "attr", "expected"),
    [
        (None, "prompt_token_count", 0),
        ({"prompt_token_count": 42}, "prompt_token_count", 42),
        ({"prompt_token_count": 15.0}, "prompt_token_count", 15),
        ({"prompt_token_count": 15.5}, "prompt_token_count", 0),
        ({"prompt_token_count": True}, "prompt_token_count", 0),
        ({"prompt_token_count": False}, "prompt_token_count", 0),
        ({"prompt_token_count": -7}, "prompt_token_count", 0),
        ({"prompt_token_count": 10**25}, "prompt_token_count", 0),
        ({"prompt_token_count": float("nan")}, "prompt_token_count", 0),
        ({"prompt_token_count": float("inf")}, "prompt_token_count", 0),
        ({"prompt_token_count": "42"}, "prompt_token_count", 0),
        (types.SimpleNamespace(prompt_token_count=99), "prompt_token_count", 99),
        (types.SimpleNamespace(prompt_token_count=True), "prompt_token_count", 0),
        (_BrokenUsageAttr(), "prompt_token_count", 0),
        ({"prompt_token_count": 10}, "", 0),
    ],
)
def test_gemini_token_extractor_guards_all_malformed_inputs(
    usage_obj: Any,
    attr: str,
    expected: int,
) -> None:
    out = gemini._n(usage_obj, attr)
    assert type(out) is int
    assert out == expected


def test_gemini_raw_handles_cycles_max_depth_nan_bytes_sets_and_unprintable() -> None:
    cyclic_dict: dict[str, Any] = {"ok": 1}
    cyclic_dict["self"] = cyclic_dict
    out_dict = gemini._raw(cyclic_dict)
    assert out_dict == {"ok": 1, "self": "<cycle>"}

    cyclic_list: list[Any] = ["a"]
    cyclic_list.append(cyclic_list)
    assert gemini._raw(cyclic_list) == ["a", "<cycle>"]

    deep: Any = "leaf"
    for _ in range(40):
        deep = [deep]
    serialized_deep = json.dumps(gemini._raw(deep), allow_nan=False)
    assert "<max_depth>" in serialized_deep

    nan_payload = {
        "nan": float("nan"),
        "inf": float("inf"),
        "ninf": float("-inf"),
        "finite": 2.5,
        "raw_bytes": b"maito",
        "tags": {"beta", "alpha"},
        "broken": _Unprintable(),
    }
    cleaned = gemini._raw(nan_payload)
    assert cleaned == {
        "nan": None,
        "inf": None,
        "ninf": None,
        "finite": 2.5,
        "raw_bytes": "maito",
        "tags": ["alpha", "beta"],
        "broken": "<unprintable>",
    }
    json.dumps(cleaned, allow_nan=False)


def test_gemini_secrets_and_scrub_protect_all_keys_url_encoded_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret+key/val=99")
    monkeypatch.setenv("AIMO_API_KEY", "aimo-portal-secret-112233")
    monkeypatch.setenv("VALIO_API_KEY", "valio-portal-secret-445566")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "cfg-gemini-secret-longer-prefix-999")

    secrets = gemini._secrets()
    assert "test" not in secrets
    assert "google-secret+key/val=99" in secrets
    assert "google-secret%2Bkey%2Fval%3D99" in secrets
    assert "aimo-portal-secret-112233" in secrets
    assert "valio-portal-secret-445566" in secrets
    assert "cfg-gemini-secret-longer-prefix-999" in secrets

    payload = {
        "feature": "test",
        "url": "https://api.example.test?key=google-secret%2Bkey%2Fval%3D99",
        "cfg": "token=cfg-gemini-secret-longer-prefix-999",
        "headers": {"X-Goog-Api-Key": "unlisted-ephemeral-header-key", "X-Api-Key": "second-key"},
    }
    scrubbed = gemini._scrub(payload, secrets)
    assert scrubbed["feature"] == "test"
    assert scrubbed["url"] == "https://api.example.test?key=[REDACTED]"
    assert scrubbed["cfg"] == "token=[REDACTED]"
    assert scrubbed["headers"]["X-Goog-Api-Key"] == "[REDACTED]"
    assert scrubbed["headers"]["X-Api-Key"] == "[REDACTED]"

    header_only = gemini._scrub({"headers": {"x-goog-api-key": "raw-header-secret"}}, ())
    assert header_only["headers"]["x-goog-api-key"] == "[REDACTED]"


def test_gemini_record_never_raises_on_unwritable_ledger_and_scrubs_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record_fn: Any = gemini.record

    blocker_file = tmp_path / "ledger_is_file"
    blocker_file.write_text("not-a-dir", encoding="utf-8")
    monkeypatch.setattr(gemini, "LEDGER_DIR", blocker_file)
    record_fn("buyer.step", "gemini-3.8-flash", latency_s=1.2)

    monkeypatch.setattr(gemini, "LEDGER_DIR", None)
    record_fn("buyer.step", "gemini-3.8-flash")

    valid_ledger = tmp_path / "valid_ledger"
    monkeypatch.setattr(gemini, "LEDGER_DIR", valid_ledger)
    secret = "super-secret-gemini-token-887766"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setattr(gemini.socket, "gethostname", lambda: "weird/host@name.localdomain")

    cyclic_meta: dict[str, Any] = {"api_key": secret, "nan_val": float("nan")}
    cyclic_meta["loop"] = cyclic_meta

    record_fn(
        feature=f"buyer.{secret}",
        model="gemini-3.8-flash",
        kind="generate",
        usage={"prompt_token_count": 20, "candidates_token_count": True},
        latency_s=float("nan"),
        ok=False,
        error=f"AuthError with {secret}",
        run_id=f"run-{secret}",
        meta=cyclic_meta,
    )
    record_fn(
        feature="buyer.bad_types",
        model="gemini-3.8-flash",
        latency_s=-9.5,
        ok=True,
        error=True,
        meta="not-a-dict",
    )

    def boom_hostname() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(gemini.socket, "gethostname", boom_hostname)
    record_fn(feature="buyer.no_host", model="gemini-3.8-flash", latency_s=10**400)

    raw_files = sorted(valid_ledger.glob("*.jsonl"))
    assert len(raw_files) == 2
    for fp in raw_files:
        assert ".." not in fp.name
        assert secret not in fp.read_text(encoding="utf-8")

    rows = _read_ledger_rows(valid_ledger)
    assert len(rows) == 3
    by_feat = {r["feature"]: r for r in rows}
    redacted_row = by_feat["buyer.[REDACTED]"]
    assert redacted_row["in"] == 20 and redacted_row["out"] == 0
    assert redacted_row["latency_s"] == 0.0
    assert "[REDACTED]" in redacted_row["error"]
    assert redacted_row["meta"]["api_key"] == "[REDACTED]"
    assert redacted_row["meta"]["nan_val"] is None
    assert redacted_row["meta"]["loop"] == "<cycle>"
    assert by_feat["buyer.bad_types"]["latency_s"] == 0.0
    assert by_feat["buyer.bad_types"]["error"] == ""
    assert by_feat["buyer.bad_types"]["meta"] == {}
    assert by_feat["buyer.no_host"]["host"] == "host"
    assert by_feat["buyer.no_host"]["latency_s"] == 0.0


def test_gemini_record_concurrent_writes_produce_valid_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_dir = tmp_path / "concurrent_ledger"
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)
    barrier = threading.Barrier(10)

    def writer(tid: int) -> None:
        barrier.wait(timeout=5.0)
        for step in range(25):
            gemini.record(
                "buyer.agent_step",
                "gemini-3.8-flash",
                usage={"prompt_token_count": 100 + step, "candidates_token_count": 10},
                latency_s=0.125,
                run_id=f"run-{tid}",
                meta={"step": step},
            )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    rows = _read_ledger_rows(ledger_dir)
    assert len(rows) == 250
    assert {r["run_id"] for r in rows} == {f"run-{i}" for i in range(10)}


@pytest.mark.parametrize("bad_model", [None, "", "   ", True, False, 123, "gemini 3.8", "gemini\nflash"])
def test_fallback_chain_and_select_candidates_guard_bad_models_and_zero_clock(
    monkeypatch: pytest.MonkeyPatch,
    bad_model: Any,
) -> None:
    chain_fn: Any = gemini.fallback_chain
    monkeypatch.setenv(
        "GEMINI_FALLBACK_MODELS",
        "  gemini-3.5-flash , bad model , gemini\n2 , gemini-4-hydrogen , gemini-3.5-flash ",
    )
    chain = chain_fn(bad_model)
    assert chain == [config.GEMINI_AGENT_MODEL, "gemini-3.5-flash", "gemini-4-hydrogen"]

    gemini._exhausted.clear()
    gemini._exhausted["stale-nan"] = float("nan")
    gemini._exhausted["expired"] = 0.0
    selected = gemini._select_candidates(chain, 0.0)
    assert selected == chain
    assert "stale-nan" not in gemini._exhausted
    assert "expired" not in gemini._exhausted


@pytest.mark.parametrize("zero_or_neg", [0, -1, -50])
def test_generate_zero_or_negative_retries_raises_runtime_error(zero_or_neg: int) -> None:
    with pytest.raises(RuntimeError, match="zero retries"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hi", retries=zero_or_neg)


@pytest.mark.parametrize("bad_retries", [True, False, 1.5, "3", [3], {"r": 3}])
def test_generate_non_int_retries_raises_value_error(bad_retries: Any) -> None:
    gen_fn: Any = gemini.generate
    with pytest.raises(ValueError, match="retries must be an integer"):
        gen_fn("buyer.step", "gemini-3.8-flash", "hi", retries=bad_retries)


def test_generate_handles_bare_response_non_dict_meta_retries_cap_and_4xx_vs_5xx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gemini.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash")
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_file))

    class BareResp:
        text = "bare-ok"

    calls: list[str] = []

    def stub_generate_content(model: str, contents: Any, config: Any = None) -> Any:
        calls.append(model)
        if model == "gemini-3.8-flash":
            raise RuntimeError("429 RESOURCE_EXHAUSTED: daily quota reached")
        return BareResp()

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(generate_content=stub_generate_content)),
    )
    gen_fn: Any = gemini.generate
    res = gen_fn("buyer.step", "  gemini-3.8-flash  ", "hello", retries=None, meta="non-dict-meta")
    assert res.text == "bare-ok"
    assert calls == ["gemini-3.8-flash", "gemini-3.5-flash"]
    assert gemini.last_model() == "gemini-3.5-flash"

    gemini._exhausted.clear()
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    err_calls = 0

    def invalid_arg_with_500(model: str, contents: Any, config: Any = None) -> Any:
        nonlocal err_calls
        err_calls += 1
        raise ValueError("400 INVALID_ARGUMENT: max_output_tokens 5000 exceeds limit 500")

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(generate_content=invalid_arg_with_500)),
    )
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hello", retries=5)
    assert err_calls == 1

    cap_calls = 0

    def always_read_error(model: str, contents: Any, config: Any = None) -> Any:
        nonlocal cap_calls
        cap_calls += 1
        raise ReadError("socket closed mid-stream")

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(generate_content=always_read_error)),
    )
    with pytest.raises(ReadError):
        gemini.generate("buyer.step", "gemini-3.8-flash", "hello", retries=500)
    assert cap_calls == 20


@pytest.mark.parametrize(
    "empty_texts",
    [
        None,
        [],
        (),
        "",
        "   ",
        b"",
        b"   ",
        True,
        False,
        0,
        42,
        3.14,
        {"a": "b"},
        [None, True, False],
        _BrokenIter(),
        _BrokenItemsIter(),
    ],
)
def test_embed_empty_or_invalid_texts_returns_empty_without_network_or_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_texts: Any,
) -> None:
    def forbid_client() -> Any:
        raise AssertionError("gemini.client() must not be called on empty/invalid embed inputs")

    monkeypatch.setattr(gemini, "client", forbid_client)
    embed_fn: Any = gemini.embed
    assert embed_fn("catalog.embed", empty_texts) == []
    assert _read_ledger_rows(gemini.LEDGER_DIR) == []


def test_embed_happy_coercion_model_fallback_ledger_trace_and_error_handling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "embed_trace.jsonl"
    monkeypatch.setenv("AIMO_GEMINI_TRACE", str(trace_path))
    secret = "embed-secret-api-key-998877"
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    seen_calls: list[tuple[str, list[str]]] = []

    def stub_embed(model: str, contents: list[str]) -> Any:
        seen_calls.append((model, list(contents)))
        return types.SimpleNamespace(
            embeddings=[
                types.SimpleNamespace(values=[0.25, float("nan"), float("inf"), True, "bad", -0.5]),
                types.SimpleNamespace(values=None),
            ]
        )

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(embed_content=stub_embed)),
    )
    embed_fn: Any = gemini.embed
    vecs = embed_fn("catalog.embed.query", [b"kauramaito", None, True, "lactose free milk", 100], model="   ")
    assert seen_calls == [(config.GEMINI_EMBED_MODEL, ["kauramaito", "lactose free milk", "100"])]
    assert vecs == [[0.25, 0.0, 0.0, 0.0, 0.0, -0.5], []]

    single_vecs = embed_fn("catalog.embed.single", "single string query", model="  gemini-embedding-custom  ")
    assert seen_calls[-1] == ("gemini-embedding-custom", ["single string query"])
    assert len(single_vecs) == 2

    def stub_embed_none_embeddings(model: str, contents: list[str]) -> Any:
        return types.SimpleNamespace(embeddings=None)

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(embed_content=stub_embed_none_embeddings)),
    )
    assert gemini.embed("catalog.embed.none", ["milk"]) == []

    def stub_embed_error(model: str, contents: list[str]) -> Any:
        raise RuntimeError(f"503 embed failed key={secret}")

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: types.SimpleNamespace(models=types.SimpleNamespace(embed_content=stub_embed_error)),
    )
    with pytest.raises(RuntimeError, match="503 embed failed"):
        gemini.embed("catalog.embed.fail", ["milk"])

    rows = _read_ledger_rows(gemini.LEDGER_DIR)
    assert len(rows) == 4
    assert [r["kind"] for r in rows] == ["embed", "embed", "embed", "embed"]
    assert [r["ok"] for r in rows] == [True, True, True, False]
    assert rows[0]["meta"] == {"n": 3}
    assert rows[3]["error"] == "RuntimeError"

    trace_raw = trace_path.read_text(encoding="utf-8")
    assert secret not in trace_raw
    trace_rows = [json.loads(line, parse_constant=_strict_no_nan) for line in trace_raw.splitlines()]
    assert len(trace_rows) == 4
    assert "[REDACTED]" in trace_rows[-1]["error"]


def test_keyless_scripted_buyer_e2e_unaffected_by_config_and_gemini_hardening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_live(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live Gemini must never be called in keyless buyer run")

    monkeypatch.setattr("aimo.gemini.generate", forbid_live)
    monkeypatch.setattr("aimo.gemini.embed", forbid_live)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", scenario="happy", now=SAT)
    agent = BuyerAgent(FakeTransport(gw), "hotel-aurora", poll_s=0.01, approval_timeout_s=2.0, now=SAT)
    res = agent.run("We need 100 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
    assert res["mock"] is True
    assert agent.model == MOCK_MODEL_NAME
    assert res["status"] == "placed"
    assert res["summary"].startswith(MOCK_LABEL)
