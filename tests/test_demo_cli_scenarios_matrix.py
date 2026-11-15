from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

import aimo.backend.catalog_store as cs
import aimo.catalog as cat
from aimo import cli, config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.buyer import run_demo
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, mock_wanted
from aimo.catalog import textsearch
from aimo.gateway import human_auth

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"


def _close_state_db(state_db: Path) -> None:
    con = store._conns.pop(str(state_db), None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def _bind_isolated_state(
    monkeypatch: pytest.MonkeyPatch,
    base_dir: Path,
    tag: str = "run",
) -> tuple[Path, Path]:
    state_db = base_dir / f"state_{tag}.sqlite"
    key_dir = base_dir / f"keys_{tag}"
    key_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    human_auth.reset_for_tests()
    return state_db, key_dir


@pytest.fixture(autouse=True)
def isolated_keyless_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    gemini_calls: list[str] = []
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(run_demo, "_TTY", False)
    for var in (
        "AIMO_LLM_MODE",
        "AIMO_AUTO_APPROVE",
        "AIMO_BUYER_TRANSPORT",
        "AIMO_MCP_URL",
        "AIMO_FAULTS",
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
    ):
        monkeypatch.delenv(var, raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_generate(*args: Any, **kwargs: Any) -> Any:
        gemini_calls.append("generate")
        raise AssertionError("aimo.gemini.generate must not be called in keyless mode")

    def forbid_embed(*args: Any, **kwargs: Any) -> Any:
        gemini_calls.append("embed")
        raise AssertionError("aimo.gemini.embed must not be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_generate)
    monkeypatch.setattr("aimo.gemini.embed", forbid_embed)

    state_db, _ = _bind_isolated_state(monkeypatch, tmp_path, "default")
    try:
        yield gemini_calls
    finally:
        human_auth.reset_for_tests()
        _close_state_db(state_db)
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture(scope="module")
def replay_catalog_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    if not REPLAY_META.is_file():
        pytest.skip("replay snapshot meta.json is not present")
    mp = pytest.MonkeyPatch()
    base_dir = tmp_path_factory.mktemp("demo-matrix-replay")
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    orig_source = cat._SOURCE

    mp.setenv("GEMINI_API_KEY", "")
    mp.setenv("GOOGLE_API_KEY", "")
    mp.setenv("AIMO_API_KEY", "")
    mp.setenv("VALIO_API_KEY", "")
    mp.setenv("AIMO_OFFLINE", "0")
    mp.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    mp.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    mp.setenv("AIMO_NOW_FROZEN", "1")
    mp.setenv("AIMO_SEMANTIC", "0")
    mp.setenv("AIMO_RECIPE_CHECK", "0")
    mp.setenv("AIMO_DATA_DIR", str(data_dir))
    mp.delenv("AIMO_NOW", raising=False)
    mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)

    mp.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        mp.setattr(config, "GOOGLE_API_KEY", "")
    mp.setattr(config, "AIMO_API_KEY", "")
    mp.setattr(config, "OFFLINE", False)
    mp.setattr(config, "DATA_DIR", data_dir)
    mp.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    mp.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    mp.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    mp.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    mp.setattr(cat, "_SOURCE", src)
    mp.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    mp.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    mp.setattr(cs, "CO2_CSV", config.CO2_CSV)
    mp.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    mp.setattr(bc, "_client", replay.ReplayClient(REPLAY_DIR))

    try:
        cat.load_catalog()
        yield data_dir
    finally:
        try:
            textsearch._con().close()
        except Exception:
            pass
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        mp.setattr(cat, "_SOURCE", orig_source)
        mp.undo()
        shutil.rmtree(base_dir, ignore_errors=True)


def _setup_replay_inproc(
    monkeypatch: pytest.MonkeyPatch,
    replay_data_dir: Path,
    tmp_path: Path,
    tag: str,
) -> Path:
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_DATA_DIR", str(replay_data_dir))
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", replay_data_dir)
    fixture_dir = replay_data_dir / "catalog-fixture"
    monkeypatch.setattr(config, "PRODUCT_JSON", fixture_dir / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", fixture_dir / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", fixture_dir / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", fixture_dir / "catalog.sqlite")
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    monkeypatch.setattr(cat, "_SOURCE", "fixture-subset")
    monkeypatch.setattr(bc, "_client", replay.ReplayClient(REPLAY_DIR))
    state_db, _ = _bind_isolated_state(monkeypatch, tmp_path, tag)
    return state_db


def _run_via_cli_cmd_demo(argv: list[str]) -> int:
    ns = argparse.Namespace(cmd="demo")
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_demo(ns, argv)
    code = exc_info.value.code
    assert isinstance(code, int)
    return code


def test_file_conventions_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


@pytest.mark.parametrize("scenario", sorted(run_demo.SCENARIOS))
def test_every_scenario_fake_transport_via_run_demo_and_cli_cmd_demo(
    scenario: str,
    isolated_keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert set(run_demo.SCENARIOS) == {
        "happy",
        "stockout",
        "school-allergen",
        "injection",
        "drift",
        "event",
    }
    assert mock_wanted() is True

    sc = run_demo.SCENARIOS[scenario]
    customer = sc["customer"]
    approver = run_demo.APPROVERS[customer]

    def build_args(out_file: Path) -> list[str]:
        base_args = [
            "--transport",
            "fake",
            "--scenario",
            scenario,
            "--approve-delay",
            "0.02",
            "--approval-timeout",
            "10",
            "--json",
            str(out_file),
        ]
        if scenario == "drift":
            base_args.append("--no-auto-approve")
        else:
            base_args.extend(["--auto-approve-as", approver])
        return base_args

    json_main = tmp_path / f"main_{scenario}.json"
    rc_main = run_demo.main(build_args(json_main))
    out_main = capsys.readouterr().out
    assert rc_main == 0, out_main
    res_main = json.loads(json_main.read_text(encoding="utf-8"))

    json_cli = tmp_path / f"cli_{scenario}.json"
    rc_cli = _run_via_cli_cmd_demo(build_args(json_cli))
    out_cli = capsys.readouterr().out
    assert rc_cli == 0, out_cli
    res_cli = json.loads(json_cli.read_text(encoding="utf-8"))

    for res, stdout in ((res_main, out_main), (res_cli, out_cli)):
        ok, why = run_demo.verdict(scenario, res)
        assert ok is True, f"{scenario}: {why}"
        verdict_line = run_demo.scenario_verdict(scenario, res)
        assert f"Verdict [{scenario}]: {verdict_line}" in stdout
        assert MOCK_LABEL in stdout
        assert res["mock"] is True
        assert res["transport"] == "fake"
        assert res["customer_id"] == customer
        assert res["need"]["preferences"]["parsed_by"] == "regex"
        assert all(n["preferences"]["parsed_by"] == "regex" for n in res["needs"])
        assert res["summary"].startswith(MOCK_LABEL)
        assert {m["model"] for m in res["models_used"]} == {MOCK_MODEL_NAME}
        assert "model: scripted (no Gemini)" in verdict_line

        step_kinds = [s["kind"] for s in res["steps"]]
        if scenario == "drift":
            assert res["status"] == "needs_confirmation"
            assert res["order"] is None
            assert "approval_wait" not in step_kinds
            assert "standing-order drift held for human confirmation" in verdict_line
        elif scenario == "event":
            assert res["status"] == "placed"
            assert "approval_wait" not in step_kinds
            assert len(res["needs"]) == 2
            assert len(res["quote"]["lines"]) >= 2
        else:
            assert res["status"] == "placed"
            assert "approval_wait" in step_kinds
            assert res["order"]["approved_by"] == approver
            assert f"approved by {approver}" in verdict_line

        if scenario == "stockout":
            assert len(res["quote"]["lines"]) >= 2
            assert "partial stock split across" in verdict_line
        elif scenario == "school-allergen":
            assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
            assert all("oat" in ln["product_name"].lower() for ln in res["quote"]["lines"])
            assert res["allergen_probe"]["ran"] is True
            assert res["allergen_probe"]["status"] == "rejected"
            assert "allergen probe (464306, cow's milk): rejected" in verdict_line
        elif scenario == "injection":
            assert "[injection_screen: clean]" in stdout
            assert "Flagged suspected prompt injection in supplier text" in stdout
            assert max(ln["qty_sales_units"] for ln in res["quote"]["lines"]) < 10000
            assert "injection flagged" in verdict_line

    assert isolated_keyless_env == []


def test_hospital_customer_profile_fake_transport_via_cli_main(
    isolated_keyless_env: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    approver = run_demo.APPROVERS["hospital-meilahti"]
    out_json = tmp_path / "hospital_fake.json"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "demo",
                "--transport",
                "fake",
                "--customer",
                "hospital-meilahti",
                "--text",
                "We need 200 kg of gluten-free bread at Meilahti hospital by Wednesday.",
                "--auto-approve-as",
                approver,
                "--approve-delay",
                "0.02",
                "--approval-timeout",
                "10",
                "--json",
                str(out_json),
            ]
        )
    stdout = capsys.readouterr().out
    assert exc_info.value.code == 0, stdout
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert res["status"] == "placed"
    assert res["customer_id"] == "hospital-meilahti"
    assert res["transport"] == "fake"
    assert "gluten" in res["need"]["excluded_allergens"]
    assert res["order"]["approved_by"] == approver
    assert [ln["product_id"] for ln in res["quote"]["lines"]] == ["473328"]
    assert "Verdict [happy]:" in stdout
    assert isolated_keyless_env == []


def test_catalog_fixture_disabled_with_empty_data_dir_falls_back_cleanly(
    isolated_keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", empty_data)
    monkeypatch.setattr(config, "PRODUCT_JSON", empty_data / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(bc, "_client", None)

    out_json = tmp_path / "cf0_auto.json"
    code = _run_via_cli_cmd_demo(
        [
            "--transport",
            "auto",
            "--base-url",
            "http://127.0.0.1:9",
            "--scenario",
            "happy",
            "--auto-approve-as",
            run_demo.APPROVERS["hotel-aurora"],
            "--approve-delay",
            "0.02",
            "--approval-timeout",
            "10",
            "--json",
            str(out_json),
        ]
    )
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert res["transport"] == "fake"
    assert res["status"] == "placed"
    assert res["mock"] is True
    assert isolated_keyless_env == []


def test_replay_inproc_hero_school_allergen_and_injection(
    replay_catalog_dir: Path,
    isolated_keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    state_hero = _setup_replay_inproc(monkeypatch, replay_catalog_dir, tmp_path, "hero")
    try:
        hero_json = tmp_path / "replay_hero.json"
        rc_hero = run_demo.main(
            [
                "--transport",
                "auto",
                "--base-url",
                "http://127.0.0.1:9",
                "--scenario",
                "happy",
                "--auto-approve-as",
                run_demo.APPROVERS["hotel-aurora"],
                "--approve-delay",
                "0.05",
                "--approval-timeout",
                "15",
                "--json",
                str(hero_json),
            ]
        )
        out_hero = capsys.readouterr().out
        assert rc_hero == 0, out_hero
        res_hero = json.loads(hero_json.read_text(encoding="utf-8"))
        ok_hero, why_hero = run_demo.verdict("happy", res_hero)
        assert ok_hero is True, why_hero
        assert res_hero["transport"] == "inproc"
        assert res_hero["status"] == "placed"
        assert res_hero["mock"] is True
        assert res_hero["order"]["approved_by"] == run_demo.APPROVERS["hotel-aurora"]
        assert res_hero["quote"]["signature"]
        assert res_hero["quote"]["kid"] != "fake-key"
        assert "[injection_screen: clean]" in out_hero
    finally:
        _close_state_db(state_hero)

    state_school = _setup_replay_inproc(monkeypatch, replay_catalog_dir, tmp_path, "school_allergen")
    try:
        school_json = tmp_path / "replay_school_allergen.json"
        code_school = _run_via_cli_cmd_demo(
            [
                "--transport",
                "inproc",
                "--scenario",
                "school-allergen",
                "--auto-approve-as",
                run_demo.APPROVERS["school-tampere"],
                "--approve-delay",
                "0.05",
                "--approval-timeout",
                "15",
                "--json",
                str(school_json),
            ]
        )
        out_school = capsys.readouterr().out
        assert code_school == 0, out_school
        res_school = json.loads(school_json.read_text(encoding="utf-8"))
        ok_school, why_school = run_demo.verdict("school-allergen", res_school)
        assert ok_school is True, why_school
        assert res_school["transport"] == "inproc"
        assert res_school["status"] == "placed"
        assert {"milk", "nuts"} <= set(res_school["need"]["excluded_allergens"])
        assert res_school["quote"]["lines"]
        assert all("oat" in ln["product_name"].lower() for ln in res_school["quote"]["lines"])
        probe = res_school["allergen_probe"]
        assert probe["ran"] is True
        assert probe["status"] == "rejected"
        assert "milk" in (probe["reason"] or "").lower()
        assert "allergen probe (464306, cow's milk): rejected" in out_school
    finally:
        _close_state_db(state_school)

    state_school_milk = _setup_replay_inproc(monkeypatch, replay_catalog_dir, tmp_path, "school_milk")
    try:
        school_milk_json = tmp_path / "replay_school_milk.json"
        rc_school_milk = run_demo.main(
            [
                "--transport",
                "inproc",
                "--customer",
                "school-tampere",
                "--text",
                "We need 50 litres of milk for Wednesday's school lunch.",
                "--answer",
                "auto",
                "--auto-approve-as",
                run_demo.APPROVERS["school-tampere"],
                "--approve-delay",
                "0.05",
                "--approval-timeout",
                "15",
                "--json",
                str(school_milk_json),
            ]
        )
        out_school_milk = capsys.readouterr().out
        assert rc_school_milk == 0, out_school_milk
        res_school_milk = json.loads(school_milk_json.read_text(encoding="utf-8"))
        assert res_school_milk["transport"] == "inproc"
        assert res_school_milk["status"] == "placed"
        assert any(s["kind"] == "needs_clarification" for s in res_school_milk["steps"])
        assert all("oat" in ln["product_name"].lower() for ln in res_school_milk["quote"]["lines"])
    finally:
        _close_state_db(state_school_milk)

    state_inj = _setup_replay_inproc(monkeypatch, replay_catalog_dir, tmp_path, "injection")
    try:
        inj_json = tmp_path / "replay_injection.json"
        code_inj = _run_via_cli_cmd_demo(
            [
                "--transport",
                "inproc",
                "--scenario",
                "injection",
                "--auto-approve-as",
                run_demo.APPROVERS["hotel-aurora"],
                "--approve-delay",
                "0.05",
                "--approval-timeout",
                "15",
                "--json",
                str(inj_json),
            ]
        )
        out_inj = capsys.readouterr().out
        assert code_inj == 0, out_inj
        res_inj = json.loads(inj_json.read_text(encoding="utf-8"))
        ok_inj, why_inj = run_demo.verdict("injection", res_inj)
        assert ok_inj is True, why_inj
        assert res_inj["transport"] == "inproc"
        assert res_inj["status"] == "placed"
        assert res_inj["order"]["approved_by"] == run_demo.APPROVERS["hotel-aurora"]
        assert "[injection_screen: FLAGGED" in out_inj
        plan_lines = (res_inj.get("plan") or {}).get("lines") or []
        assert any((ln.get("injection_screen") or {}).get("suspicious") is True for ln in plan_lines)
        assert max(ln["qty_sales_units"] for ln in res_inj["quote"]["lines"]) < 10000
    finally:
        _close_state_db(state_inj)

    assert isolated_keyless_env == []


@pytest.mark.parametrize(
    "bad_text",
    [
        "We need 0 litres of milk at Hotel Aurora by Monday",
        "-5 litres",
        "We need -5 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
        "40 L lactose-free cream and 0 kg lactose-free butter by Monday.",
    ],
)
@pytest.mark.parametrize("transport_mode", ["fake", "inproc"])
def test_non_positive_quantity_requests_stop_before_planning_without_orders(
    bad_text: str,
    transport_mode: str,
    replay_catalog_dir: Path,
    isolated_keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    if transport_mode == "inproc":
        state_db = _setup_replay_inproc(monkeypatch, replay_catalog_dir, tmp_path, "bad_qty_inproc")
    else:
        state_db, _ = _bind_isolated_state(monkeypatch, tmp_path, "bad_qty_fake")

    try:
        json_main = tmp_path / "bad_qty_main.json"
        argv_main = [
            "--transport",
            transport_mode,
            "--text",
            bad_text,
            "--no-auto-approve",
            "--json",
            str(json_main),
        ]
        rc = run_demo.main(argv_main)
        out_main = capsys.readouterr().out
        assert rc == 1
        res_main = json.loads(json_main.read_text(encoding="utf-8"))

        json_cli = tmp_path / "bad_qty_cli.json"
        argv_cli = [
            "--transport",
            transport_mode,
            "--text",
            bad_text,
            "--no-auto-approve",
            "--json",
            str(json_cli),
        ]
        code_cli = _run_via_cli_cmd_demo(argv_cli)
        out_cli = capsys.readouterr().out
        assert code_cli == 1
        res_cli = json.loads(json_cli.read_text(encoding="utf-8"))

        for res, stdout in ((res_main, out_main), (res_cli, out_cli)):
            assert res["status"] == "error"
            assert res["mock"] is True
            assert res["plan"] is None
            assert res["quote"] is None
            assert res["order"] is None
            tool_calls = [s for s in res["steps"] if s["kind"] == "tool_call"]
            assert tool_calls == []
            err_steps = [s for s in res["steps"] if s["kind"] == "error"]
            assert err_steps and "ValidationError" in err_steps[0]["title"]
            assert res["steps"][-1]["kind"] == "final"
            assert res["steps"][-1]["data"]["status"] == "error"
            assert "Verdict [happy]: error:" in stdout

        assert store.list_orders() == []
        assert isolated_keyless_env == []
    finally:
        _close_state_db(state_db)


def test_clarification_answer_flow_and_cli_exit_code_propagation(
    isolated_keyless_env: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ambiguous_text = "We need 50 litres of lactose-free milk next week."

    auto_json = tmp_path / "clarify_auto.json"
    auto_argv = [
        "--transport",
        "fake",
        "--customer",
        "hotel-aurora",
        "--text",
        ambiguous_text,
        "--answer",
        "auto",
        "--auto-approve-as",
        run_demo.APPROVERS["hotel-aurora"],
        "--approve-delay",
        "0.02",
        "--approval-timeout",
        "10",
        "--json",
        str(auto_json),
    ]
    rc_auto = run_demo.main(auto_argv)
    out_auto = capsys.readouterr().out
    assert rc_auto == 0, out_auto
    res_auto = json.loads(auto_json.read_text(encoding="utf-8"))
    assert res_auto["status"] == "placed"
    assert any(s["kind"] == "needs_clarification" for s in res_auto["steps"])
    assert "[auto-answer] d0:" in out_auto

    auto_cli_json = tmp_path / "clarify_auto_cli.json"
    with pytest.raises(SystemExit) as exc_auto:
        cli.main(["demo", *auto_argv[:-1], str(auto_cli_json)])
    out_auto_cli = capsys.readouterr().out
    assert exc_auto.value.code == 0, out_auto_cli
    res_auto_cli = json.loads(auto_cli_json.read_text(encoding="utf-8"))
    assert res_auto_cli["status"] == "placed"

    none_json = tmp_path / "clarify_none.json"
    none_argv = [
        "--transport",
        "fake",
        "--customer",
        "hotel-aurora",
        "--text",
        ambiguous_text,
        "--answer",
        "none",
        "--no-auto-approve",
        "--json",
        str(none_json),
    ]
    rc_none = run_demo.main(none_argv)
    out_none = capsys.readouterr().out
    assert rc_none == 0, out_none
    res_none = json.loads(none_json.read_text(encoding="utf-8"))
    assert res_none["status"] == "needs_clarification"
    assert res_none["plan"] is None
    assert res_none["quote"] is None
    assert res_none["order"] is None
    assert not any(s["kind"] == "tool_call" and s["data"].get("name") == "plan_need" for s in res_none["steps"])

    none_cli_json = tmp_path / "clarify_none_cli.json"
    code_none_cli = _run_via_cli_cmd_demo([*none_argv[:-1], str(none_cli_json)])
    capsys.readouterr()
    assert code_none_cli == 0
    assert json.loads(none_cli_json.read_text(encoding="utf-8"))["status"] == "needs_clarification"

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "d1")
    ask_json = tmp_path / "clarify_ask_opt.json"
    code_ask = _run_via_cli_cmd_demo(
        [
            "--transport",
            "fake",
            "--customer",
            "hotel-aurora",
            "--text",
            ambiguous_text,
            "--answer",
            "ask",
            "--auto-approve-as",
            run_demo.APPROVERS["hotel-aurora"],
            "--approve-delay",
            "0.02",
            "--approval-timeout",
            "10",
            "--json",
            str(ask_json),
        ]
    )
    capsys.readouterr()
    assert code_ask == 0
    res_ask = json.loads(ask_json.read_text(encoding="utf-8"))
    assert res_ask["status"] == "placed"
    assert res_ask["need"]["preferences"].get("clarified") == ["q_date"]

    monkeypatch.setattr("builtins.input", lambda prompt="": "0 litres")
    ask_zero_json = tmp_path / "clarify_ask_zero.json"
    code_ask_zero = _run_via_cli_cmd_demo(
        [
            "--transport",
            "fake",
            "--customer",
            "school-tampere",
            "--text",
            "We need oat drink by Wednesday.",
            "--answer",
            "ask",
            "--no-auto-approve",
            "--json",
            str(ask_zero_json),
        ]
    )
    capsys.readouterr()
    assert code_ask_zero == 0
    res_ask_zero = json.loads(ask_zero_json.read_text(encoding="utf-8"))
    assert res_ask_zero["status"] == "no_order"
    assert res_ask_zero["plan"] is None
    assert res_ask_zero["quote"] is None
    assert res_ask_zero["order"] is None
    assert "non-positive quantity rejected" in res_ask_zero["summary"]
    assert not any(s["kind"] == "tool_call" and s["data"].get("name") == "plan_need" for s in res_ask_zero["steps"])

    assert isolated_keyless_env == []
