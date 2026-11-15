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
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aimo import config

FILES = [
    ("/api/products/download", config.PRODUCT_JSON),
    ("/api/delivery-reliability/download", config.RELIABILITY_CSV),
    ("/api/carbon-footprint/download", config.CO2_CSV),
]

_MAX_URL_CHARS = 4096
_MAX_REDIRECTS = 5


def _has_control_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get"):
        direct = headers.get(name)
        if direct is not None:
            return str(direct)
    target = name.lower()
    if hasattr(headers, "items"):
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() == target and v is not None:
                return str(v)
    return None


def _validate_http_url(raw: Any, label: str = "URL") -> str:
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    if _has_control_char(raw):
        raise ValueError(f"{label} contains control characters")
    url = raw.strip()
    if not url or len(url) > _MAX_URL_CHARS or any(ch.isspace() for ch in url):
        raise ValueError(f"{label} is empty or invalid")
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not host:
        raise ValueError(f"{label} must use http or https with a valid host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    if ".." in host or host.startswith("."):
        raise ValueError(f"{label} host is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    if port is not None and (port < 1 or port > 65535):
        raise ValueError(f"{label} port out of range")
    return url


def _same_origin_for_auth(target_url: str, base_url: str) -> bool:
    t = urlparse(target_url)
    b = urlparse(base_url)
    if t.scheme != b.scheme:
        return False
    if (t.hostname or "").lower() != (b.hostname or "").lower():
        return False
    t_port = t.port or (443 if t.scheme == "https" else 80)
    b_port = b.port or (443 if b.scheme == "https" else 80)
    return t_port == b_port


def _validate_endpoint(endpoint: Any) -> str:
    if isinstance(endpoint, bool) or not isinstance(endpoint, str):
        raise ValueError("endpoint must be a string")
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise ValueError("endpoint must start with a single '/'")
    if _has_control_char(endpoint) or any(ch.isspace() for ch in endpoint):
        raise ValueError("endpoint contains whitespace or control characters")
    segments = endpoint.split("?", 1)[0].split("/")
    if ".." in segments:
        raise ValueError("endpoint must not contain '..' path segments")
    return endpoint


def _validate_dest(dest: Any) -> Path:
    if isinstance(dest, bool) or not isinstance(dest, (Path, str)):
        raise ValueError("dest must be a Path or string")
    raw = str(dest)
    if not raw.strip() or _has_control_char(raw):
        raise ValueError("dest path is empty or contains control characters")
    path = dest if isinstance(dest, Path) else Path(raw)
    if path.name in ("", ".", ".."):
        raise ValueError("dest must name a target file")
    if path.exists() and path.is_dir():
        raise ValueError("dest must be a file path, not an existing directory")
    return path


def _fetch_following_redirects(
    c: httpx.Client,
    start_url: str,
    base_url: str,
    auth_headers: dict[str, str],
) -> Any:
    cur_url = _validate_http_url(start_url, "download URL")
    hops = 0
    while True:
        hdrs = dict(auth_headers) if _same_origin_for_auth(cur_url, base_url) else {}
        r = c.get(cur_url, headers=hdrs, follow_redirects=False)
        if not getattr(r, "is_redirect", False):
            break
        if hops >= _MAX_REDIRECTS:
            raise ValueError("too many redirects while downloading data")
        raw_loc = _header(getattr(r, "headers", None), "location")
        if not isinstance(raw_loc, str) or not raw_loc.strip():
            raise ValueError("redirect response missing Location header")
        if _has_control_char(raw_loc):
            raise ValueError("redirect Location header contains control characters")
        origin_url = str(getattr(r, "url", None) or cur_url)
        resolved = urljoin(origin_url, raw_loc.strip())
        cur_url = _validate_http_url(resolved, "redirect Location")
        hops += 1
    r.raise_for_status()
    return r


def _download(endpoint: str, dest: Path) -> None:
    clean_ep = _validate_endpoint(endpoint)
    dest_path = _validate_dest(dest)
    raw_base = config.AIMO_BASE_URL
    if isinstance(raw_base, bool) or not isinstance(raw_base, str):
        raise ValueError("AIMO_BASE_URL must be a string")
    base = _validate_http_url(raw_base.rstrip("/"), "AIMO_BASE_URL")
    raw_key = config.AIMO_API_KEY
    if raw_key is None:
        api_key = ""
    elif isinstance(raw_key, bool) or not isinstance(raw_key, str):
        raise ValueError("AIMO_API_KEY must be a string")
    else:
        api_key = raw_key.strip()
        if _has_control_char(api_key):
            raise ValueError("AIMO_API_KEY contains control characters")
    auth_headers = {"X-API-Key": api_key} if api_key else {}

    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
        r = _fetch_following_redirects(c, base + clean_ep, base, auth_headers)
        ctype = (_header(getattr(r, "headers", None), "content-type") or "").lower()
        body = getattr(r, "content", b"")
        if "json" in ctype and isinstance(body, (bytes, bytearray)) and len(body) < 65536:
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                url_keys = [k for k in ("url", "downloadUrl", "link") if k in data]
                if url_keys:
                    raw_json_url = data[url_keys[0]]
                    if isinstance(raw_json_url, bool) or not isinstance(raw_json_url, str):
                        raise ValueError("JSON download URL must be a string")
                    if _has_control_char(raw_json_url) or not raw_json_url.strip():
                        raise ValueError("JSON download URL is empty or contains control characters")
                    origin_url = str(getattr(r, "url", None) or (base + clean_ep))
                    resolved_json_url = _validate_http_url(
                        urljoin(origin_url, raw_json_url.strip()),
                        "JSON download URL",
                    )
                    r = _fetch_following_redirects(c, resolved_json_url, base, auth_headers)
        content = getattr(r, "content", None)
        if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
            raise ValueError(f"downloaded empty payload for {dest_path.name}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".part")
        try:
            tmp.write_bytes(bytes(content))
            tmp.replace(dest_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def ensure_data(force: bool = False, quiet: bool = False) -> list[Path]:
    """Download missing files. Returns the paths that were downloaded."""
    if not isinstance(force, bool) or not isinstance(quiet, bool):
        raise ValueError("force and quiet must be booleans")
    done: list[Path] = []
    for endpoint, dest in FILES:
        dest_path = _validate_dest(dest)
        if dest_path.exists() and dest_path.stat().st_size > 0 and not force:
            if not quiet:
                print(f"ok      {dest_path.name} ({dest_path.stat().st_size / 1e6:.1f} MB)")
            continue
        raw_key = config.AIMO_API_KEY
        key_clean = raw_key.strip() if isinstance(raw_key, str) and not isinstance(raw_key, bool) else ""
        if not key_clean:
            raise SystemExit("AIMO_API_KEY missing in .env: cannot download " + dest_path.name)
        print(f"fetch   {dest_path.name} <- {endpoint}")
        _download(endpoint, dest_path)
        print(f"saved   {dest_path.name} ({dest_path.stat().st_size / 1e6:.1f} MB)")
        done.append(dest_path)
    return done


def main(argv: list[str] | None = None) -> list[Path]:
    if argv is not None:
        if isinstance(argv, (str, bytes, bytearray, bool)) or not isinstance(argv, (list, tuple)):
            raise ValueError("argv must be a list or tuple of strings")
        for item in argv:
            if isinstance(item, bool) or not isinstance(item, str) or "\x00" in item:
                raise ValueError("argv items must be strings without null bytes")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    args = ap.parse_args(list(argv) if argv is not None else None)
    return ensure_data(force=args.force)


if __name__ == "__main__":
    main()
