"""Shared test setup (owner: windtunnel).

- Offline by default: the wind tunnel replay client reads tests/fixtures/snapshot (public mock API responses),
  AIMO_OFFLINE=1, and outputs go to a temp WT_DIR.
- Markers:
    windtunnel  full scenario suite (slow); runs only with WT_FULL=1 (freighter: windtunnel/slurm/tests.sbatch)
    e2e         HTTP end-to-end through the gateway TestClient; skipped until aimo.gateway exposes an app
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SNAPSHOT = ROOT / "tests" / "fixtures" / "snapshot"

# must happen before windtunnel.config is imported anywhere
os.environ.setdefault("AIMO_OFFLINE", "1")
os.environ.setdefault("AIMO_APPROVER_PIN", "test-pin")  # human approver credential for tests
os.environ.setdefault("AIMO_DEMO_FAULTS", "1")
if os.environ.get("WT_FULL") != "1":
    os.environ.setdefault("WT_SNAPSHOT_DIR", str(FIXTURE_SNAPSHOT))
os.environ.setdefault("WT_DIR", tempfile.mkdtemp(prefix="wt-test-"))

HAVE_PRODUCT_DATA = (Path(os.environ.get("AIMO_DATA_DIR", ROOT / "data")) / "valio_aimo_junction26_product_data.json").exists()


def pytest_configure(config):
    config.addinivalue_line("markers", "windtunnel: full scenario suite (WT_FULL=1, freighter)")
    config.addinivalue_line("markers", "e2e: HTTP end-to-end through the gateway")
    config.addinivalue_line("markers", "needs_data: requires data/valio_aimo_junction26_product_data.json")


def pytest_collection_modifyitems(config, items):
    full = os.environ.get("WT_FULL") == "1"
    for it in items:
        if "windtunnel" in it.keywords and not full:
            it.add_marker(pytest.mark.skip(reason="full wind tunnel suite: set WT_FULL=1 (runs on freighter)"))
        if "needs_data" in it.keywords and not HAVE_PRODUCT_DATA:
            it.add_marker(pytest.mark.skip(reason="product data not in data/ (gitignored); link it first"))


@pytest.fixture(scope="session")
def fixture_snapshot() -> Path:
    return FIXTURE_SNAPSHOT


# --- Keyless pytest (from justSilas-aimo-integration): a missing challenge catalog skips instead of failing.
PRODUCT_JSON_NAME = "valio_aimo_junction26_product_data.json"
SKIP_REASON = (
    "challenge product data is not present "
    f"(data/{PRODUCT_JSON_NAME}); set AIMO_API_KEY and run `uv run aimo fetch-data`"
)


def _is_missing_product_data(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
            continue
        if isinstance(cur, FileNotFoundError):
            name = str(getattr(cur, "filename", "") or "")
            if PRODUCT_JSON_NAME in name or PRODUCT_JSON_NAME in str(cur):
                return True
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
    return False


def _exception_from(outcome) -> BaseException | None:
    excinfo = getattr(outcome, "excinfo", None)
    if not excinfo:
        return None
    # pluggy >= 1.5 stores (type, value, traceback). Older pluggy used an ExceptionInfo.
    if isinstance(excinfo, tuple):
        return excinfo[1]
    return getattr(excinfo, "value", None)


def _skip_if_missing(outcome) -> None:
    err = _exception_from(outcome)
    if err is not None and _is_missing_product_data(err):
        outcome.force_exception(pytest.skip.Exception(SKIP_REASON))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    outcome = yield
    _skip_if_missing(outcome)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    outcome = yield
    _skip_if_missing(outcome)
