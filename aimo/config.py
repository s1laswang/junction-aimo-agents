"""Central config. Secrets come only from .env or environment. Never log them. Owner: CEO."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
try:
    load_dotenv(ROOT / ".env")
except (OSError, ValueError):
    pass


def _clean_str(val: Any) -> str:
    if not isinstance(val, str):
        return ""
    return val.replace("\x00", "").strip()


def _env(*names: str, default: str = "") -> str:
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        cleaned = _clean_str(os.environ.get(name, ""))
        if cleaned:
            return cleaned
    return default


def _model_env(name: str, default: str) -> str:
    cleaned = _env(name)
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        return default
    return cleaned


def _base_url() -> str:
    for name in ("AIMO_PORTAL_URL", "VALIO_API_BASE"):
        cleaned = _clean_str(os.environ.get(name, "")).rstrip("/").strip()
        if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
            continue
        parts = urlsplit(cleaned)
        if parts.scheme.lower() in ("http", "https") and parts.netloc.strip():
            return cleaned
    return "https://junction26.valioaimo.fi"


def _path_env(name: str, default: Path) -> Path:
    raw = _env(name)
    if not raw:
        return default
    try:
        return Path(raw)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw == 1
    if isinstance(raw, str):
        cleaned = raw.replace("\x00", "").strip().lower()
        if cleaned in ("1", "true", "yes", "on"):
            return True
        if cleaned in ("0", "false", "no", "off", ""):
            return False
    return default


DATA_DIR = _path_env("AIMO_DATA_DIR", ROOT / "data")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (OSError, ValueError):
    pass

AIMO_BASE_URL = _base_url()
AIMO_API_KEY = _env("AIMO_API_KEY", "VALIO_API_KEY")
GEMINI_API_KEY = _env("GEMINI_API_KEY", "GOOGLE_API_KEY")

PRODUCT_JSON = DATA_DIR / "valio_aimo_junction26_product_data.json"
RELIABILITY_CSV = DATA_DIR / "valio_aimo_junction26_delivery_reliability.csv"
CO2_CSV = DATA_DIR / "valio_aimo_junction26_carbon_footprint.csv"
CATALOG_DB = DATA_DIR / "catalog.sqlite"

STATE_DB = _path_env("AIMO_STATE_DB", DATA_DIR / "state.sqlite")
KEY_DIR = _path_env("AIMO_KEY_DIR", DATA_DIR / "keys")

GEMINI_AGENT_MODEL = _model_env("GEMINI_AGENT_MODEL", "gemini-3.8-flash")
GEMINI_REASONING_MODEL = _model_env("GEMINI_REASONING_MODEL", "gemini-4-hydrogen")
GEMINI_FAST_MODEL = _model_env("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
GEMINI_EMBED_MODEL = _model_env("GEMINI_EMBED_MODEL", "gemini-embedding-2")

MCP_SPEC_VERSION = "2026-07-28"
MCP_HANDSHAKE_VERSION = "2025-11-25"
MCP_SDK = "mcp 2.3.0 (python-sdk)"

OFFLINE = _bool_env("AIMO_OFFLINE", default=False)
