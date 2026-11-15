from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

import aimo.backend.catalog_store as cs
import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer import run_demo
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport, connect
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
REPLAY_REL = "tests/fixtures/snapshot/replay"
DEAD_BASE = "http://127.0.0.1:9"
DEAD_MCP = f"{DEAD_BASE}/mcp"
EXPECTED_SCENARIOS = ("happy", "stockout", "school-allergen", "injection", "drift", "event")


@pytest.fixture
def isolated_keyless_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_BUYER_TRANSPORT", raising=False)
    monkeypatch.delenv("AIMO_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(bc, "_client", None)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args, **kwargs):
        raise AssertionError("Gemini API must not be called in keyless forced-replay mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    try:
        yield tmp_path
    finally:
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            con.close()


def test_file_hygiene_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_auto_transport_selects_inproc_on_forced_replay_and_fake_otherwise(
    isolated_keyless_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not config.PRODUCT_JSON.exists()
    mandate = mandate_for("hotel-aurora")

    monkeypatch.setenv("AIMO_REPLAY", REPLAY_REL)
    t_forced = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_forced, InProcessTransport)
        assert t_forced.kind == "inproc"
    finally:
        t_forced.close()

    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    t_unset = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_unset, InProcessTransport)
        assert t_unset.kind == "inproc"
    finally:
        t_unset.close()

    monkeypatch.setenv("AIMO_REPLAY", "0")
    t_off = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_off, FakeTransport)
        assert t_off.kind == "fake"
    finally:
        t_off.close()

    monkeypatch.setenv("AIMO_REPLAY", "1")
    t_bundled = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_bundled, InProcessTransport)
        assert t_bundled.kind == "inproc"
    finally:
        t_bundled.close()

    monkeypatch.setenv("AIMO_REPLAY", "tests/fixtures/snapshot")
    t_parent_snap = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_parent_snap, InProcessTransport)
        assert t_parent_snap.kind == "inproc"
    finally:
        t_parent_snap.close()

    monkeypatch.setenv("AIMO_REPLAY", REPLAY_REL)
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    t_fixture_disabled = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_fixture_disabled, FakeTransport)
        assert t_fixture_disabled.kind == "fake"
    finally:
        t_fixture_disabled.close()
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)

    empty_snap = isolated_keyless_env / "empty_snap"
    empty_snap.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_REPLAY", str(empty_snap))
    t_no_cat = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(t_no_cat, FakeTransport)
        assert t_no_cat.kind == "fake"
    finally:
        t_no_cat.close()


def test_keyless_forced_replay_montage_end_to_end(
    isolated_keyless_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_REPLAY", REPLAY_REL)
    workspace_montage = ROOT / "docs" / "demo-montage.md"
    before_exists = workspace_montage.exists()
    before_bytes = workspace_montage.read_bytes() if before_exists else None

    montage_out = isolated_keyless_env / "montage.md"
    rc = run_demo.main(
        [
            "--montage",
            "--transport",
            "auto",
            "--base-url",
            DEAD_BASE,
            "--approve-delay",
            "0.05",
            "--approval-timeout",
            "15",
            "--montage-out",
            str(montage_out),
        ]
    )
    assert rc == 0
    assert montage_out.is_file()

    if before_exists:
        assert workspace_montage.read_bytes() == before_bytes
    else:
        assert not workspace_montage.exists()

    content = montage_out.read_text(encoding="utf-8")
    assert "(transport: inproc)" in content
    assert "transport: inproc" in content
    assert "transport: fake" not in content
    assert "mock-replay (no Gemini)" in content
    assert "escalation off." in content
    assert "$0.0000" in content
    assert "| FAIL |" not in content
    assert content.count("| PASS |") == 6

    lines = content.splitlines()
    for scenario in EXPECTED_SCENARIOS:
        matching = [ln for ln in lines if ln.startswith(f"| {scenario} |")]
        assert len(matching) == 1, (scenario, matching)
        row = matching[0]
        assert "| PASS |" in row, row
        assert "| $0.0000 |" in row, row

    cost_rows = [ln for ln in lines if ln.startswith("| This run |")]
    assert len(cost_rows) == 1
    assert "| This run | 6 | 5 | 0 |" in cost_rows[0]
    assert "| $0.0000 | $0.0000 | $0.0000 |" in cost_rows[0]

    audit_check = service.audit_verify()
    assert audit_check["ok"] is True, audit_check
    assert audit_check.get("broken_at") is None
    assert audit_check.get("checked", 0) >= 15

    placed = store.list_orders(status="placed")
    assert len(placed) == 5
    for order in placed:
        quote = store.get_quote(order.quote_id)
        assert quote is not None
        assert signing.verify_quote(quote) is True
