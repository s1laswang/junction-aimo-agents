"""Wind tunnel paths. All outputs live under data/windtunnel/ (gitignored) unless WT_DIR is set
(on freighter: /data/silas/junction-aimo-agents/windtunnel)."""
from __future__ import annotations

import os
from pathlib import Path

from aimo.config import DATA_DIR, ROOT

WT_DIR = Path(os.environ.get("WT_DIR", DATA_DIR / "windtunnel"))
SNAPSHOT_DIR = Path(os.environ.get("WT_SNAPSHOT_DIR", WT_DIR / "snapshot"))
SCENARIO_DIR = WT_DIR / "scenarios"
RUNS_DIR = WT_DIR / "runs"
RESULTS_DIR = WT_DIR / "results"
FIXTURES_DIR = ROOT / "tests" / "fixtures"          # small, committed: no secrets, only public mock API responses
RESULTS_MD = ROOT / "docs" / "windtunnel-results.md"

# Gemini models used by the wind tunnel (feature names windtunnel.*)
GEN_MODEL = os.environ.get("WT_GEN_MODEL", "gemini-3.8-flash")
JUDGE_MODEL = os.environ.get("WT_JUDGE_MODEL", "gemini-3.8-flash")
MAX_CONCURRENCY = min(16, int(os.environ.get("WT_CONCURRENCY", "8")))  # shared key: never above 16

for d in (WT_DIR, SNAPSHOT_DIR, SCENARIO_DIR, RUNS_DIR, RESULTS_DIR):
    d.mkdir(parents=True, exist_ok=True)
