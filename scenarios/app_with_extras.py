"""The gateway app with the ported extras mounted (recipes, drift, proposals; HTTP + MCP).

Used by scenarios/run_all.py until the gateway owner adds the 2-line hook from aimo/gateway/extras.py to
app.py / mcp_server.py. Idempotent: once the hook is in, this is just the normal app.
    uvicorn scenarios.app_with_extras:app --port 8810
"""
from __future__ import annotations

import aimo.gateway.mcp_server as _mcp_mod
from aimo.gateway import extras

_orig_build = _mcp_mod.build_mcp


def _build_with_extras():
    return extras.register_mcp_tools(_orig_build())


_mcp_mod.build_mcp = _build_with_extras

from aimo.gateway.app import create_app  # noqa: E402  (after the patch: create_app imports build_mcp lazily)

app = extras.install(create_app())
