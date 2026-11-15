"""Catalog CLI.

    uv run python -m aimo.catalog build        # SQLite + FTS (2.5 s), Gemini embeddings (~45 s), allergen hints (~50 s)
    uv run python -m aimo.catalog search lactose-free milk
    uv run python -m aimo.catalog demo         # the 1,000 L lactose-free milk plan (live mock API)

The Gemini steps are skipped if their cache files already exist (they are network-bound,
<= 8 concurrent requests, and logged to the Gemini usage ledger).
"""
from __future__ import annotations

import math
import os
import sys
from typing import Any

from aimo import config

_MAX_QUERY_CHARS = 500
_INIT_CFG_KEY = str(getattr(config, "GEMINI_API_KEY", "") or "")
_INIT_ENV_KEY = str(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "")


def _has_control_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _clean_argv(argv: list[str] | None) -> list[str]:
    raw: Any = sys.argv[1:] if argv is None else argv
    if isinstance(raw, (str, bytes, bytearray, bool)) or not isinstance(raw, (list, tuple)):
        raise ValueError("argv must be a list or tuple of strings")
    cleaned: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, str):
            raise ValueError("argv items must be strings")
        if "\x00" in item or any(0xD800 <= ord(ch) <= 0xDFFF for ch in item):
            raise ValueError("argv items must not contain null bytes or lone surrogates")
        cleaned.append(item)
    return cleaned


def _gemini_skip_reason() -> str | None:
    env_offline = os.environ.get("AIMO_OFFLINE", "").strip()
    if env_offline == "1" or (bool(getattr(config, "OFFLINE", False)) and env_offline != "0"):
        return "AIMO_OFFLINE=1"
    raw_cfg = getattr(config, "GEMINI_API_KEY", "")
    cfg_key = str(raw_cfg).strip() if isinstance(raw_cfg, str) else ""
    env_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if cfg_key != _INIT_CFG_KEY.strip():
        active_key = cfg_key
    elif env_key != _INIT_ENV_KEY.strip():
        active_key = env_key
    else:
        active_key = cfg_key or env_key
    if not active_key or _has_control_char(active_key):
        return "GEMINI_API_KEY not set"
    return None


def main(argv: list[str] | None = None) -> None:
    args = _clean_argv(argv)
    if not args:
        print(__doc__)
        return
    raw_cmd = args[0]
    if _has_control_char(raw_cmd) or not raw_cmd.strip():
        raise ValueError("command must be a non-empty string without control characters")
    cmd = raw_cmd.strip()
    if cmd == "build":
        extra = args[1:]
        for flag in extra:
            if flag not in ("--force", "--no-build"):
                raise ValueError(f"unknown build option: {flag!r}")
        from aimo.backend.catalog_store import load_catalog
        load_catalog()
        print("catalog.sqlite ready")
        skip_reason = "skipped (--no-build)" if "--no-build" in extra else _gemini_skip_reason()
        from aimo.catalog import embeddings
        if not embeddings.available():
            if skip_reason:
                print(f"embeddings skipped ({skip_reason}); search uses FTS only")
            else:
                embeddings.build()
        else:
            print("embeddings present")
        from aimo.catalog import gemini_allergens
        if not gemini_allergens.CACHE.exists() or "--force" in extra:
            if skip_reason:
                print(f"allergen hints skipped ({skip_reason})")
            else:
                gemini_allergens.precompute()
        else:
            print("allergen hints present")
    elif cmd == "search":
        if len(args) == 1:
            query = "lactose-free milk"
        else:
            raw_query = " ".join(args[1:])
            if _has_control_char(raw_query):
                raise ValueError("search query must not contain control characters")
            if len(raw_query) > _MAX_QUERY_CHARS:
                raise ValueError(f"search query exceeds {_MAX_QUERY_CHARS} characters")
            query = raw_query.strip()
            if not query:
                raise ValueError("search query must not be blank")
        from aimo.catalog import search
        for p in search(query, limit=15):
            sc = p.score
            score_txt = (
                "-"
                if sc is None or isinstance(sc, bool) or (isinstance(sc, float) and not math.isfinite(sc))
                else sc
            )
            print(f"{p.id}  {score_txt:>7}  {p.content_amount} {p.content_unit}/{p.sales_unit}  {p.name}")
    elif cmd == "demo":
        if len(args) > 1:
            raise ValueError("demo takes no extra arguments")
        from aimo.backend.clock import next_delivery_day
        from aimo.catalog import plan_need
        from aimo.models import Need
        p = plan_need(Need(customer_id="hotel-aurora", product_query="lactose-free milk",
                           required_claims=["FREE_FROM_LACTOSE"], quantity=1000, unit="L",
                           delivery_by=next_delivery_day(),
                           preferred_product_ids=["463038"]))
        print(p.explanation)
        for c in p.candidates:
            reason = str(c.reasons[0])[:90] if c.reasons else ""
            flags = ",".join(str(f) for f in (c.flags or []))
            rank = (
                c.rank_score
                if isinstance(c.rank_score, (int, float))
                and not isinstance(c.rank_score, bool)
                and math.isfinite(c.rank_score)
                else 0.0
            )
            print(f"  {c.tier:9} {c.product.id} {rank:.3f} {flags:40} {reason}")
    elif cmd in ("help", "-h", "--help"):
        print(__doc__)
    else:
        raise ValueError(f"unknown catalog command: {cmd!r}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
