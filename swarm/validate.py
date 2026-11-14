"""Validate one swarm card's outputs before pushing: `uv run python swarm/validate.py <card-id>`.

Checks: (1) the report exists and is well-formed; (2) only the card's output paths changed vs origin/main;
(3) a per-kind schema check. Exit 0 = OK to push. Owner: CEO.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_card(cid: str) -> dict:
    for x in "ABCD":
        p = ROOT / "swarm" / x / "cards.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                c = json.loads(line)
                if c["id"] == cid:
                    return c
    sys.exit(f"unknown card {cid}")


def changed_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    unstaged = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=ROOT, capture_output=True, text=True).stdout
    files = set(out.split())
    for line in unstaged.splitlines():
        files.add(line[3:].strip())
    return sorted(f for f in files if f)


def allowed(path: str, outputs: list[str]) -> bool:
    return any(path == o or (o.endswith("/") and path.startswith(o)) for o in outputs)


def jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def product_ids() -> set[str]:
    try:
        import sqlite3
        from aimo.config import CATALOG_DB
        con = sqlite3.connect(CATALOG_DB)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        t = "products" if "products" in tables else tables[0]
        return {str(r[0]) for r in con.execute(f"select id from {t}")}
    except Exception:
        return set()


def check_kind(card: dict) -> list[str]:
    errs, kind, outs = [], card["kind"], [ROOT / o for o in card["outputs"]]
    data = next((o for o in outs if not o.name.endswith(".report.json")), None)
    if data is None or not data.exists():
        return [f"missing output {data}"]
    try:
        if kind in ("gold", "taxonomy", "allergen", "phrasing", "injection", "eval"):
            rows = jsonl(data)
            need = {"gold": 25, "phrasing": 50, "injection": 100}.get(kind)
            if need and len(rows) != need:
                errs.append(f"expected {need} rows, got {len(rows)}")
            keys = {"gold": {"id", "segment", "lang", "request", "customer_id", "gold"},
                    "taxonomy": {"product_id", "product_type", "is_food", "content_amount", "content_unit"},
                    "allergen": {"product_id", "allergen", "structured", "ingredient_evidence", "severity"},
                    "phrasing": {"text", "expected_need", "parsed_need", "field_pass"},
                    "injection": {"text", "label", "technique", "lang", "detected_l1"},
                    "eval": {"id", "zero_correction", "escalation_match", "allergen_safe"}}[kind]
            for i, r in enumerate(rows):
                miss = keys - set(r)
                if miss:
                    errs.append(f"row {i}: missing {sorted(miss)}")
                    break
            if kind == "gold":
                ids = product_ids()
                for r in rows:
                    for alt in r["gold"].get("acceptable_product_ids", []):
                        for pid in alt:
                            if ids and str(pid) not in ids:
                                errs.append(f"{r['id']}: unknown product {pid}")
        elif kind == "policy":
            import yaml
            y = yaml.safe_load(data.read_text())
            for k in ("policy_id", "version", "customer", "rules"):
                if k not in y:
                    errs.append(f"policy missing {k}")
        elif kind in ("redteam", "chaos"):
            r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", str(data)], cwd=ROOT,
                               capture_output=True, text=True)
            if r.returncode not in (0, 5) or "error" in r.stdout.lower():
                errs.append("pytest collection failed:\n" + r.stdout[-800:] + r.stderr[-400:])
        elif kind == "image":
            if not data.is_dir() or not (data / "expected.json").exists():
                errs.append("need a directory with images + expected.json")
        else:  # ux, copy, claims, client, proof, pitch, ops: json or md present and non-trivial
            if data.stat().st_size < 200:
                errs.append("output too small")
            if data.suffix == ".json":
                json.loads(data.read_text())
    except Exception as e:  # noqa: BLE001
        errs.append(f"schema check crashed: {e}")
    return errs


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    card = find_card(sys.argv[1])
    errs = []
    rep = ROOT / f"swarm/{card['swarm']}/out/{card['id']}.report.json"
    if not rep.exists():
        errs.append(f"missing report {rep.relative_to(ROOT)}")
    else:
        r = json.loads(rep.read_text())
        if r.get("status") not in ("done", "blocked", "broken_found"):
            errs.append("report.status must be done|blocked|broken_found")
    stray = [f for f in changed_files() if not allowed(f, card["outputs"])]
    if stray:
        errs.append(f"files outside card outputs: {stray}")
    errs += check_kind(card)
    if errs:
        print("FAIL\n- " + "\n- ".join(errs))
        sys.exit(1)
    print(f"OK {card['id']}: push branch swarm/{card['swarm']}/{card['id']}")


if __name__ == "__main__":
    main()
