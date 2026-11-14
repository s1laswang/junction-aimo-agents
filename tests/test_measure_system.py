import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import tokenize
from pathlib import Path

from pitch import measure_system
from scripts.smoke_demo import EXPECTED_TOOLS

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"

EXPECTED_MANDATES = {
    "hotel-aurora": {
        "per_order_eur": 2500.0,
        "per_week_eur": 15000.0,
        "exclude_allergens": [],
        "substitutions": "within_prefs",
        "approvers": ["Laura Virtanen", "Mikko Heikkinen"],
    },
    "hospital-tays": {
        "per_order_eur": 1500.0,
        "per_week_eur": 8000.0,
        "exclude_allergens": ["peanut", "nuts", "egg"],
        "substitutions": "ask",
        "approvers": ["Helena Korhonen", "Antti Lahtinen"],
    },
    "school-hervanta": {
        "per_order_eur": 600.0,
        "per_week_eur": 3000.0,
        "exclude_allergens": ["peanut", "nuts"],
        "substitutions": "within_prefs",
        "approvers": ["Sari Nieminen"],
    },
}

EXPECTED_SCENARIOS = [
    "s0_guardrails",
    "s1_milk",
    "s2_need_recipe_order",
    "s3_event_substitution",
    "s4_stockout",
    "s5_allergen_safe",
    "s6_proactive_seller",
    "s7_drift",
]

EXPECTED_UI_VIEWS = [
    "admin.html",
    "audit.html",
    "console.html",
    "inbox.html",
    "index.html",
]

COUNT_PATTERNS = {
    "mcp_tools": r"`(\d+)`\s+MCP tools\b",
    "openapi_paths": r"`(\d+)`\s+OpenAPI paths\b",
    "v1_paths": r"`(\d+)`\s+under\s+`/v1/\*`",
    "v1_operations": r"with\s+`(\d+)`\s+HTTP operations\b",
    "recipes": r"`(\d+)`\s+recipe dishes\b",
    "seeded_customers": r"`(\d+)`\s+seeded mandates\b",
    "scenarios": r"`(\d+)`\s+scenario scripts\b",
    "ui_views": r"`(\d+)`\s+UI views\b",
    "smoke_checks": r"`(\d+)`\s+smoke checks\b",
}


def parse_interface_counts_bullet(markdown_text):
    bullet = next(
        (
            line.strip()
            for line in markdown_text.splitlines()
            if line.strip().startswith("- Interface and planning counts")
        ),
        None,
    )
    assert bullet is not None, "Missing Interface and planning counts bullet in pitch/NUMBERS.md"
    assert "`.venv/bin/python -m pitch.measure_system`" in bullet
    parsed = {}
    for key, pattern in COUNT_PATTERNS.items():
        match = re.search(pattern, bullet)
        assert match is not None, f"Missing {key} pattern in bullet: {bullet}"
        parsed[key] = int(match.group(1))
    return bullet, parsed


def test_measure_system_exact_contract_and_speed(monkeypatch, tmp_path):
    no_data = tmp_path / "no_data"
    outer_db = tmp_path / "outer.db"
    outer_keys = tmp_path / "outer_keys"
    monkeypatch.setenv("AIMO_DATA_DIR", str(no_data))
    monkeypatch.setenv("AIMO_DB", str(outer_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(outer_keys))
    monkeypatch.setenv("AIMO_AGENT_KEYS", '{"bad": ["x"]}')

    measure_system.measure()
    t0 = time.perf_counter()
    res = measure_system.measure()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.5, f"measure() took {elapsed:.3f}s"
    assert not no_data.exists()
    assert not outer_db.exists()
    assert not outer_keys.exists()
    assert os.environ.get("AIMO_DB") == str(outer_db)
    assert os.environ.get("AIMO_KEY_DIR") == str(outer_keys)
    assert os.environ.get("AIMO_AGENT_KEYS") == '{"bad": ["x"]}'

    assert res["mcp_tools"] == 12
    assert res["mcp_tool_names"] == sorted(EXPECTED_TOOLS)
    assert res["openapi_paths"] == 15
    assert res["v1_paths"] == 12
    assert res["v1_operations"] == 15
    assert res["non_v1_paths"] == ["/.well-known/agent-card.json", "/.well-known/jwks.json", "/health"]
    assert res["recipes"] == 11
    assert res["seeded_customers"] == 3
    assert res["mandates"] == EXPECTED_MANDATES
    assert res["scenarios"] == 8
    assert res["scenario_names"] == EXPECTED_SCENARIOS
    assert res["ui_views"] == 5
    assert res["ui_view_names"] == EXPECTED_UI_VIEWS
    assert res["smoke_checks"] == 6
    assert len(res["smoke_check_labels"]) == 6


def test_measure_system_inside_running_event_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))

    async def call_in_loop():
        return measure_system.measure()

    res = asyncio.run(call_in_loop())
    assert res["mcp_tools"] == 12
    assert res["mcp_tool_names"] == sorted(EXPECTED_TOOLS)


def test_measure_system_dynamic_root_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    (tmp_path / "data_extra").mkdir()
    (tmp_path / "data_extra" / "recipes.json").write_text(
        json.dumps({"dishes": [{"id": "d1"}, {"id": "d2"}]}),
        encoding="utf-8",
    )
    (tmp_path / "scenarios").mkdir()
    for name in ("s0_alpha.py", "s1_beta.py", "_common.py", "run_all.py"):
        (tmp_path / "scenarios" / name).write_text("", encoding="utf-8")
    (tmp_path / "web").mkdir()
    for name in ("index.html", "console.html", "shell.js", "style.css"):
        (tmp_path / "web" / name).write_text("", encoding="utf-8")

    res = measure_system.measure(root=tmp_path)
    assert res["recipes"] == 2
    assert res["scenarios"] == 2
    assert res["scenario_names"] == ["s0_alpha", "s1_beta"]
    assert res["ui_views"] == 2
    assert res["ui_view_names"] == ["console.html", "index.html"]


def test_measure_system_cli_and_main_json(capsys, monkeypatch, tmp_path):
    no_data = str(tmp_path / "no_data")
    monkeypatch.setenv("AIMO_DATA_DIR", no_data)
    returned = measure_system.main()
    stdout_json = json.loads(capsys.readouterr().out)
    assert stdout_json == returned

    env = {**os.environ, "AIMO_DATA_DIR": no_data}
    proc = subprocess.run(
        [sys.executable, "-m", "pitch.measure_system"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    cli_json = json.loads(proc.stdout)
    assert cli_json == returned


def test_numbers_md_matches_measure_system(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    measured = measure_system.measure()
    text = (ROOT / "pitch" / "NUMBERS.md").read_text(encoding="utf-8")
    bullet, parsed_counts = parse_interface_counts_bullet(text)

    for key, doc_val in parsed_counts.items():
        assert doc_val == measured[key], f"{key}: NUMBERS.md has {doc_val}, measure() has {measured[key]}"

    for route in measured["non_v1_paths"]:
        assert f"`{route}`" in bullet

    for customer, mandate in measured["mandates"].items():
        assert f"`{customer}`" in bullet
        formatted_limit = f"{mandate['per_order_eur']:,.2f} EUR"
        assert re.search(
            rf"`{re.escape(customer)}`\s*\(`{re.escape(formatted_limit)}`\s+per-order limit\)",
            text,
        ), f"Missing per-order limit {formatted_limit} for {customer} in pitch/NUMBERS.md"


def test_measure_system_hygiene():
    for rel in ("pitch/measure_system.py", "tests/test_measure_system.py", "pitch/NUMBERS.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert EM_DASH not in text, rel
    for rel in ("pitch/measure_system.py", "tests/test_measure_system.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert HASH_CHAR not in source, rel
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        assert not [tok for tok in tokens if tok.type == tokenize.COMMENT], rel
