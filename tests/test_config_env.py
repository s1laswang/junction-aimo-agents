from __future__ import annotations

import importlib
from pathlib import Path

import dotenv
import pytest

from aimo import config

ENV_KEYS = (
    "AIMO_DATA_DIR",
    "AIMO_PORTAL_URL",
    "VALIO_API_BASE",
    "AIMO_API_KEY",
    "VALIO_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AIMO_STATE_DB",
    "AIMO_KEY_DIR",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    with monkeypatch.context() as m:
        m.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        for key in ENV_KEYS:
            m.delenv(key, raising=False)
        m.setenv("AIMO_DATA_DIR", str(tmp_path / "data"))
        yield m
    importlib.reload(config)


def test_defaults_when_env_unset(clean_env, tmp_path):
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://junction26.valioaimo.fi"
    assert config.AIMO_API_KEY == ""
    assert config.GEMINI_API_KEY == ""
    assert config.DATA_DIR == tmp_path / "data"
    assert config.STATE_DB == tmp_path / "data" / "state.sqlite"
    assert config.KEY_DIR == tmp_path / "data" / "keys"


def test_aimo_api_key_precedence_and_valio_fallback(clean_env):
    clean_env.setenv("VALIO_API_KEY", "test-valio-fallback")
    importlib.reload(config)
    assert config.AIMO_API_KEY == "test-valio-fallback"

    clean_env.setenv("AIMO_API_KEY", "")
    importlib.reload(config)
    assert config.AIMO_API_KEY == "test-valio-fallback"

    clean_env.setenv("AIMO_API_KEY", "test-aimo-primary")
    importlib.reload(config)
    assert config.AIMO_API_KEY == "test-aimo-primary"


def test_base_url_precedence_and_valio_api_base_fallback(clean_env):
    clean_env.setenv("VALIO_API_BASE", "https://valio-alt.example.test///")
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://valio-alt.example.test"

    clean_env.setenv("AIMO_PORTAL_URL", "")
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://valio-alt.example.test"

    clean_env.setenv("AIMO_PORTAL_URL", "https://portal-primary.example.test/")
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://portal-primary.example.test"

    clean_env.setenv("AIMO_PORTAL_URL", "")
    clean_env.setenv("VALIO_API_BASE", "")
    importlib.reload(config)
    assert config.AIMO_BASE_URL == "https://junction26.valioaimo.fi"


def test_gemini_api_key_precedence_and_google_fallback(clean_env):
    clean_env.setenv("GOOGLE_API_KEY", "test-google-fallback")
    importlib.reload(config)
    assert config.GEMINI_API_KEY == "test-google-fallback"

    clean_env.setenv("GEMINI_API_KEY", "")
    importlib.reload(config)
    assert config.GEMINI_API_KEY == "test-google-fallback"

    clean_env.setenv("GEMINI_API_KEY", "test-gemini-primary")
    importlib.reload(config)
    assert config.GEMINI_API_KEY == "test-gemini-primary"


def test_state_db_and_key_dir_env_overrides(clean_env, tmp_path):
    custom_db = tmp_path / "isolated" / "custom_state.sqlite"
    custom_keys = tmp_path / "isolated" / "custom_keys"
    clean_env.setenv("AIMO_STATE_DB", str(custom_db))
    clean_env.setenv("AIMO_KEY_DIR", str(custom_keys))
    importlib.reload(config)
    assert isinstance(config.STATE_DB, Path)
    assert isinstance(config.KEY_DIR, Path)
    assert config.STATE_DB == custom_db
    assert config.KEY_DIR == custom_keys

    clean_env.setenv("AIMO_STATE_DB", "")
    clean_env.setenv("AIMO_KEY_DIR", "")
    importlib.reload(config)
    assert config.STATE_DB == tmp_path / "data" / "state.sqlite"
    assert config.KEY_DIR == tmp_path / "data" / "keys"
