"""Central config. Secrets come only from .env / environment. Never log them. Owner: CEO."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("AIMO_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

AIMO_BASE_URL = os.environ.get("AIMO_PORTAL_URL", "https://junction26.valioaimo.fi/").rstrip("/")
AIMO_API_KEY = os.environ.get("AIMO_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

PRODUCT_JSON = DATA_DIR / "valio_aimo_junction26_product_data.json"
RELIABILITY_CSV = DATA_DIR / "valio_aimo_junction26_delivery_reliability.csv"
CO2_CSV = DATA_DIR / "valio_aimo_junction26_carbon_footprint.csv"
CATALOG_DB = DATA_DIR / "catalog.sqlite"
STATE_DB = DATA_DIR / "state.sqlite"        # quotes, orders, approvals, audit
KEY_DIR = DATA_DIR / "keys"                 # Ed25519 signing key (demo; production: KMS)

# Gemini models (see docs/GEMINI.md). Override via env.
GEMINI_AGENT_MODEL = os.environ.get("GEMINI_AGENT_MODEL", "gemini-3.8-flash")
GEMINI_REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-4-hydrogen")
GEMINI_FAST_MODEL = os.environ.get("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-2")

# Offline mode: no live mock API calls (uses cached responses in data/cache). For tests only.
OFFLINE = os.environ.get("AIMO_OFFLINE", "0") == "1"
