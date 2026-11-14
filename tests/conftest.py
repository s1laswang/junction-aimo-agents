"""Keyless pytest: a missing challenge catalog skips, it does not fail the run.

The proprietary product file ``data/valio_aimo_junction26_product_data.json`` is not in git.
Tests that open it raise ``FileNotFoundError``. This hook turns that into a skip so
``uv run --extra dev pytest`` exits 0 with no ``AIMO_API_KEY``.
"""
from __future__ import annotations

import pytest

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
