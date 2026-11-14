"""`aimo` command line. Owner: gateway.

    aimo serve [--port 8000] [--host 127.0.0.1] [--reload] [--no-build|--build-sync]
                                                              fetch data, build catalog (+Gemini builds if missing), run gateway
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


BUILD_CMD = "uv run python -m aimo.catalog build"


def _build_catalog(gemini_builds: str = "background") -> None:
    """catalog.sqlite (FTS, ~3 s) synchronously; then Gemini embeddings + allergen hints if missing.

    gemini_builds: "background" (default; search degrades to FTS until done), "sync" (block), "skip".
    """
    try:
        from aimo.backend.catalog_store import load_catalog  # type: ignore
    except ImportError:
        print("catalog store not available: using gateway fallback search")
        return
    try:
        load_catalog()
        print("catalog ready (data/catalog.sqlite, FTS)")
    except NotImplementedError:
        print("catalog store not implemented yet: using gateway fallback search")
        return
    except FileNotFoundError as e:
        print(
            "warning: challenge catalog data is missing "
            f"({e.filename or e}); the gateway still starts (UI, /healthz, "
            "/.well-known, OpenAPI). Set AIMO_API_KEY in .env and rerun "
            "`uv run aimo fetch-data` (or `uv run aimo serve`) to download it."
        )
        return

    missing = []
    try:
        from aimo.catalog import embeddings  # type: ignore

        if not embeddings.available():
            missing.append("embeddings")
    except Exception as e:  # noqa: BLE001
        print(f"embeddings module unavailable ({e}); semantic search off, FTS only")
    try:
        from aimo.catalog import gemini_allergens  # type: ignore

        if not gemini_allergens.CACHE.exists():
            missing.append("allergen hints")
    except Exception as e:  # noqa: BLE001
        print(f"allergen-hint module unavailable ({e})")
    if not missing:
        print("embeddings and allergen hints present")
        return

    from aimo import config

    what = " + ".join(missing)
    if gemini_builds == "skip" or not config.GEMINI_API_KEY:
        why = "skipped (--no-build)" if gemini_builds == "skip" else "GEMINI_API_KEY not set"
        print(f"{what} missing, {why}: search uses FTS only. Build them with:  {BUILD_CMD}")
        return

    def run() -> None:
        try:
            if "embeddings" in missing:
                embeddings.build()
            if "allergen hints" in missing:
                gemini_allergens.precompute()
            print(f"catalog Gemini builds finished ({what}); semantic search is on")
        except Exception as e:  # noqa: BLE001
            print(f"catalog Gemini build failed ({e}); search stays on FTS. Retry with:  {BUILD_CMD}")

    if gemini_builds == "sync":
        print(f"building {what} with Gemini (~1-2 min) ...")
        run()
    else:
        import threading

        print(f"building {what} with Gemini in the background (~1-2 min); search uses FTS until done "
              f"(or stop and run:  {BUILD_CMD})")
        threading.Thread(target=run, name="catalog-gemini-build", daemon=True).start()


def _export_serve_url(host: str, port: int) -> None:
    """The buyer agent's MCP URL follows the port actually served (aimo.buyer.runs.mcp_url_for).
    Exported before uvicorn starts so --reload workers inherit it. An explicit AIMO_MCP_URL that points at a
    different local port is almost certainly stale: say so instead of letting runs hit the wrong server."""
    import os
    local = "127.0.0.1" if host in ("0.0.0.0", "::", "", "localhost") else host
    url = f"http://{'[' + local + ']' if ':' in local else local}:{port}"
    os.environ["AIMO_SERVE_URL"] = url
    explicit = os.environ.get("AIMO_MCP_URL", "").strip()
    if explicit and any(h in explicit for h in ("localhost", "127.0.0.1")) and f":{port}/" not in explicit + "/":
        print(f"warning: AIMO_MCP_URL={explicit} does not match the served port {port}; agent runs will use "
              f"{url}/mcp instead (unset AIMO_MCP_URL to silence this)")
        os.environ["AIMO_MCP_URL"] = f"{url}/mcp"


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    try:
        sys.stdout.reconfigure(line_buffering=True)  # startup messages before uvicorn's logs
    except AttributeError:
        pass
    try:
        _fetch()
    except SystemExit as e:
        print(f"warning: {e}")
    except Exception as e:  # network trouble must not block serving cached data
        print(f"warning: data download failed: {e}")
    _build_catalog("skip" if a.no_build else "sync" if a.build_sync else "background")
    print(f"Valio Aimo Trusted Trade gateway on http://{a.host}:{a.port}  (docs /docs, MCP /mcp, UI /ui)")
    _export_serve_url(a.host, a.port)
    try:
        from aimo.gateway.human_auth import credential_warning
        w = credential_warning()
        if w:
            print(f"warning: {w}")
    except Exception:  # noqa: BLE001
        pass
    # proxy_headers=False: never trust X-Forwarded-For, so per-client approver lockouts cannot be dodged by spoofing
    uvicorn.run("aimo.gateway.app:app", host=a.host, port=a.port, reload=a.reload, log_level="info",
                proxy_headers=False)


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
    s.add_argument("--no-build", action="store_true", help="skip Gemini embedding/allergen builds (FTS search only)")
    s.add_argument("--build-sync", action="store_true", help="run missing Gemini builds before serving")
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
