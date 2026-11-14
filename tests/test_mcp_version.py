"""The pinned MCP spec revision must match the SDK in uv.lock, and the manifest must state it."""
from __future__ import annotations

from importlib.metadata import version

from aimo import config
from aimo.gateway import service


def test_pinned_mcp_spec_matches_installed_sdk():
    from mcp_types import version as v

    assert version("mcp").startswith("2.3.")
    assert v.LATEST_PROTOCOL_VERSION == config.MCP_SPEC_VERSION == "2026-07-28"
    assert v.LATEST_HANDSHAKE_VERSION == config.MCP_HANDSHAKE_VERSION == "2025-11-25"


def test_manifest_states_mcp_protocol_version():
    m = service.manifest("http://test")
    assert m["mcp"]["protocol_version"] == "2026-07-28"
    assert "2026-07-28" in m["mcp"]["sdk_supported_versions"]
