"""Reproduce the injection-screen false-positive number: run the regex layer (no Gemini, no network) over every
non-empty supplier marketing text in the product data (all languages) and count how many are flagged.

    uv run python docs/pitch/repro_injection_fp.py
"""
from __future__ import annotations

import json
import time

from aimo.config import PRODUCT_JSON
from aimo.trust import screen_text


def main() -> None:
    t0 = time.time()
    products = json.load(open(PRODUCT_JSON))
    texts = [t["value"] for p in products for t in ((p.get("synkkaData") or {}).get("marketingTexts") or [])
             if t.get("value")]
    flagged = [s for s in texts if screen_text(s, use_gemini=False).get("suspicious")]
    print(f"products={len(products)} marketing_texts={len(texts)} flagged={len(flagged)} "
          f"({time.time() - t0:.1f} s)")
    for s in flagged[:5]:
        print("  e.g.", s[:160].replace("\n", " "))


if __name__ == "__main__":
    main()
