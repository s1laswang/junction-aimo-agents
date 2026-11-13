import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("AIMO_DATA_DIR", "data")

import precision_cases as pc
from aimo import catalog, substitute
from aimo.parse import parse_need
from aimo.recipes import menu_to_need

T = date(2026, 11, 13)


def top(item):
    found = catalog.search(item.query, item.constraints, limit=50)
    if not found:
        return None, 0.0
    best = sorted(found, key=lambda p: -substitute.match_score(item, p))[0]
    return best, substitute.match_score(item, best)


def judge(item, must, mustnot):
    p, score = top(item)
    if p is None:
        return f"{item.query} -> no products"
    name = p.name.lower()
    ok = (
        re.search(must, name)
        and not re.search(mustnot, name)
        and score >= 0.5
        and substitute.allergen_safe(p, item.constraints, item.constraints.exclude_allergens)
    )
    return None if ok else f"{item.query} -> {p.product_id} {p.name}"


def run_case(case):
    text, rest = case[0], case[1:]
    if rest[0] == "menu":
        by_query = {i.query: i for i in menu_to_need(text, "hotel-aurora", today=T).items}
        out = []
        for q, (must, mustnot) in rest[1].items():
            out.append(judge(by_query[q], must, mustnot) if q in by_query else f"{q} -> not in menu need")
        return [o for o in out if o]
    items = parse_need(text, "hotel-aurora", T).items
    expect = rest[0]
    if len(items) != len(expect):
        return [f"parsed {len(items)} items, expected {len(expect)}: {[i.query for i in items]}"]
    return [m for m in (judge(i, a, b) for i, (a, b) in zip(items, expect)) if m]


def score(cases):
    misses = []
    for c in cases:
        for m in run_case(c):
            misses.append(f"  MISS {c[0]!r}: {m}")
    return len(cases) - len({m.split("': ")[0] for m in misses}), len(cases), misses


def main():
    sets = [("set1", pc.SET1), ("holdout", pc.HOLDOUT), ("holdout2", pc.HOLDOUT2)]
    results = [(n, *score(c)) for n, c in sets]
    ok, total = sum(r[1] for r in results), sum(r[2] for r in results)
    print(", ".join(f"{n} {a}/{b}" for n, a, b, _ in results) + f", total {ok}/{total}")
    for _, _, _, m in results:
        print("\n".join(m)) if m else None
    if "--holdout3" in sys.argv:
        a, b, m = score(pc.HOLDOUT3)
        print(f"holdout3 {a}/{b}")
        print("\n".join(m)) if m else None


main()
