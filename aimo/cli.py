"""`aimo` command line. Owner: gateway.

    aimo serve [--port 8000] [--host 127.0.0.1] [--reload]   fetch data if missing, build catalog, run gateway
    aimo demo [...]                                           terminal demo (aimo.buyer.run_demo:main)
    aimo mcp                                                  MCP server over stdio
    aimo fetch-data [--force]                                 download challenge data files
    aimo mcp-check [URL|stdio]                                list MCP tools and call search_products
"""
from __future__ import annotations

import argparse
import logging
import sys


def _fetch(force: bool = False) -> None:
    from aimo import config

    sys.path.insert(0, str(config.ROOT / "scripts"))
    try:
        from fetch_data import ensure_data  # type: ignore
    finally:
        sys.path.pop(0)
    ensure_data(force=force, quiet=not force)


def _build_catalog() -> None:
    try:
        from aimo.backend.catalog_store import load_catalog  # type: ignore
    except ImportError:
        print("catalog store not available yet: using gateway fallback search")
        return
    try:
        load_catalog()
    except NotImplementedError:
        print("catalog store not implemented yet: using gateway fallback search")


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    try:
        _fetch()
    except SystemExit as e:
        print(f"warning: {e}")
    except Exception as e:  # network trouble must not block serving cached data
        print(f"warning: data download failed: {e}")
    _build_catalog()
    print(f"Valio Aimo Trusted Trade gateway on http://{a.host}:{a.port}  (docs /docs, MCP /mcp, UI /ui)")
    uvicorn.run("aimo.gateway.app:app", host=a.host, port=a.port, reload=a.reload, log_level="info")


def cmd_demo(a: argparse.Namespace, rest: list[str]) -> None:
    try:
        from aimo.buyer.run_demo import main as demo_main  # type: ignore
    except ImportError:
        raise SystemExit("aimo.buyer.run_demo is not available yet (buyer engineer). "
                         "Meanwhile: `aimo serve` and follow docs/reports/gateway.md curl walkthrough.")
    sys.argv = ["aimo demo", *rest]
    demo_main()


def cmd_mcp(_: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel
    from aimo.gateway.mcp_server import main

    main()


def cmd_mcp_check(a: argparse.Namespace) -> None:
    import asyncio

    from aimo.gateway.mcp_check import main

    asyncio.run(main(a.target))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aimo", description="Valio Aimo Trusted Trade Agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the gateway (HTTP + MCP + UI)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--reload", action="store_true")
    sub.add_parser("demo", help="terminal end-to-end demo (buyer agent)")
    sub.add_parser("mcp", help="MCP server over stdio")
    f = sub.add_parser("fetch-data", help="download data files into data/")
    f.add_argument("--force", action="store_true")
    c = sub.add_parser("mcp-check", help="list MCP tools and call search_products")
    c.add_argument("target", nargs="?", default="http://localhost:8000/mcp")

    a, rest = ap.parse_known_args(argv)
    if a.cmd == "serve":
        cmd_serve(a)
    elif a.cmd == "demo":
        cmd_demo(a, rest)
    elif a.cmd == "mcp":
        cmd_mcp(a)
    elif a.cmd == "fetch-data":
        _fetch(force=a.force)
    elif a.cmd == "mcp-check":
        cmd_mcp_check(a)


if __name__ == "__main__":
    main()
