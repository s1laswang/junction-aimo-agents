"""Run the real gateway (aimo.gateway.app) with the mock API replaced by the recorded snapshot, for agent mode.
The gateway's own fault layer (X-Aimo-Faults) still applies on top. The Gemini buyer agent needs network.

  uv run python -m windtunnel.serve_replay --port 8765
"""
from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    os.environ.setdefault("AIMO_RECIPE_CHECK", "0")
    import uvicorn
    import aimo.backend as B
    from windtunnel.snapshot import ReplayClient
    B.set_client(ReplayClient())
    from aimo.gateway.app import app
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
