"""Download the Valio Aimo challenge data files into data/ if they are missing.

    uv run python scripts/fetch_data.py          # only missing files
    uv run python scripts/fetch_data.py --force  # re-download all

Endpoints (docs/valio-mock-openapi.json, tag "File downloads"): /api/products/download,
/api/delivery-reliability/download, /api/carbon-footprint/download. Each needs X-API-Key and
answers with a redirect to the file. The API key is sent only to the Valio Aimo host, never to
the redirect target. The files "will not change during the challenge", so this runs once.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aimo import config  # noqa: E402

FILES = [
    ("/api/products/download", config.PRODUCT_JSON),
    ("/api/delivery-reliability/download", config.RELIABILITY_CSV),
    ("/api/carbon-footprint/download", config.CO2_CSV),
]


def _download(endpoint: str, dest: Path) -> None:
    base = config.AIMO_BASE_URL
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
        r = c.get(base + endpoint, headers={"X-API-Key": config.AIMO_API_KEY}, follow_redirects=False)
        hops = 0
        while r.is_redirect and hops < 5:
            loc = urljoin(str(r.url), r.headers["location"])
            same_host = urlparse(loc).netloc == urlparse(base).netloc
            r = c.get(loc, headers={"X-API-Key": config.AIMO_API_KEY} if same_host else {}, follow_redirects=False)
            hops += 1
        r.raise_for_status()
        # Some download endpoints may answer with a small JSON {"url": ...} instead of a redirect.
        if "json" in r.headers.get("content-type", "") and len(r.content) < 65536:
            try:
                data = r.json()
            except ValueError:
                data = None
            url = next((data[k] for k in ("url", "downloadUrl", "link") if k in data), None) if isinstance(data, dict) else None
            if url:
                r = c.get(url, follow_redirects=True)
                r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)


def ensure_data(force: bool = False, quiet: bool = False) -> list[Path]:
    """Download missing files. Returns the paths that were downloaded."""
    done = []
    for endpoint, dest in FILES:
        if dest.exists() and dest.stat().st_size > 0 and not force:
            if not quiet:
                print(f"ok      {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
            continue
        if not config.AIMO_API_KEY:
            raise SystemExit("AIMO_API_KEY missing in .env: cannot download " + dest.name)
        print(f"fetch   {dest.name} <- {endpoint}")
        _download(endpoint, dest)
        print(f"saved   {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        done.append(dest)
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ensure_data(force=ap.parse_args().force)
