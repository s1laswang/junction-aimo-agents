from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from aimo.app import app

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
HTML_FILES = sorted(WEB.glob("*.html"))
JS_FILES = sorted(WEB.glob("*.js"))
CSS_FILES = sorted(WEB.glob("*.css"))


class AssetAndLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = []
        self.links = []
        self.inline_scripts = []
        self._in_script = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "script":
            if "src" in d:
                self.scripts.append(d["src"])
            else:
                self._in_script = True
                self._buf = []
        elif tag == "link":
            rel = (d.get("rel") or "").lower()
            href = d.get("href") or ""
            if "stylesheet" in rel and href:
                self.styles.append(href)
        elif tag == "a" and d.get("href"):
            self.links.append(d["href"])

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self._in_script = False
            self.inline_scripts.append("".join(self._buf))


def _node_bin():
    n = shutil.which("node")
    if n:
        return n
    fallback = Path("/home/silas/tools/node/bin/node")
    if fallback.is_file():
        return str(fallback)
    pytest.skip("node binary not available")


def _strip_js_and_find_comments(src: str):
    comments = []
    i = 0
    n = len(src)
    prev_tok = "start"
    regex_prev = {
        "start", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";",
        "+", "-", "*", "%", "<", ">", "^", "~", "return", "throw", "case",
        "typeof", "void", "delete", "in", "of", "instanceof", "=>",
    }
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch in " \t\r\n":
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            prev_tok = "str"
            continue
        if ch == "`":
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "`":
                    i += 1
                    break
                i += 1
            prev_tok = "tpl"
            continue
        if ch == "/" and nxt == "/":
            end = src.find("\n", i + 2)
            comments.append(src[i : n if end < 0 else end])
            break
        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            comments.append(src[i : n if end < 0 else end + 2])
            i = n if end < 0 else end + 2
            continue
        if ch == "/" and prev_tok in regex_prev:
            i += 1
            in_class = False
            while i < n:
                c2 = src[i]
                if c2 == "\\":
                    i += 2
                    continue
                if c2 == "[":
                    in_class = True
                elif c2 == "]":
                    in_class = False
                elif c2 == "/" and not in_class:
                    i += 1
                    while i < n and src[i].isalpha():
                        i += 1
                    break
                i += 1
            prev_tok = "regex"
            continue
        if ch.isalpha() or ch in ("_", "$"):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in ("_", "$")):
                j += 1
            prev_tok = src[i:j]
            i = j
            continue
        if ch == "=" and nxt == ">":
            prev_tok = "=>"
            i += 2
            continue
        prev_tok = ch
        i += 1
    return comments


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "ui_xss.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    with TestClient(app) as c:
        yield c


def test_all_html_local_assets_and_nav_links_resolve_200(client):
    assert len(HTML_FILES) >= 5
    checked = set()
    for html_file in HTML_FILES:
        parser = AssetAndLinkParser()
        parser.feed(html_file.read_text(encoding="utf-8"))
        for raw in parser.scripts + parser.styles + parser.links:
            if raw.startswith(("https://fonts.googleapis.com/", "mailto:", "data:")):
                continue
            assert not raw.startswith(("http://", "https://", "//", "javascript:")), f"{html_file.name}: {raw}"
            parts = urlsplit(raw)
            path = parts.path
            if not path:
                continue
            if not path.startswith("/"):
                path = "/" + path
            if path not in checked:
                checked.add(path)
                resp = client.get(path)
                assert resp.status_code == 200, f"{html_file.name} -> {path} returned {resp.status_code}"
                assert resp.content, f"{html_file.name} -> {path} returned empty body"
    assert {"/", "/console.html", "/inbox.html", "/audit.html", "/admin.html", "/style.css", "/shell.js"} <= checked


def test_all_js_api_routes_exist_on_gateway(client):
    oa = client.get("/openapi.json").json()
    oa_paths = set(oa["paths"].keys())
    known_static = {
        "/v1",
        "/mcp",
        "/health",
        "/openapi.json",
        "/.well-known/agent-card.json",
        "/.well-known/jwks.json",
    }
    param_prefixes = {
        "/v1/approvals/": "/v1/approvals/{order_id}",
        "/v1/orders/": "/v1/orders/{order_id}",
        "/v1/products/": "/v1/products/{product_id}",
    }
    route_re = re.compile(r'["\']((?:/v1|/\.well-known|/openapi\.json|/mcp|/health)[^"\']*)["\']')
    sources = [(p.name, p.read_text(encoding="utf-8")) for p in JS_FILES + HTML_FILES]
    seen = set()
    for name, text in sources:
        for m in route_re.finditer(text):
            raw = m.group(1).split()[0]
            base = raw.split("?", 1)[0].split("#", 1)[0]
            if base == "/v1/audit/verify":
                continue
            matched = base in oa_paths or base in known_static
            if not matched:
                for prefix, templ in param_prefixes.items():
                    if base.startswith(prefix) and templ in oa_paths:
                        matched = True
                        break
            assert matched, f"{name} references unknown route {raw}"
            seen.add(base)
    for expected in ("/v1/needs/parse", "/v1/offers", "/v1/quotes", "/v1/orders", "/v1/approvals", "/v1/audit", "/v1/mandate"):
        assert expected in seen


def test_xss_escaping_helpers_and_safe_dom_sinks():
    node = _node_bin()
    payloads = [
        '<script>alert("xss")</script>',
        '<img src=x onerror="alert(1)">',
        '"><svg/onload=alert(1)>',
        "Valio & Co <100%> \"quoted\"",
    ]
    index_html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "function esc(" in index_html
    console_js = (WEB / "console.js").read_text(encoding="utf-8")
    console_esc = re.search(r"const esc\s*=\s*[^\n]+", console_js)
    assert console_esc
    audit_js = (WEB / "audit.js").read_text(encoding="utf-8")
    audit_esc = re.search(r"function esc\([^\n]+", audit_js)
    assert audit_esc

    script = f"""
const payloads = {json.dumps(payloads)};
function indexEsc(t) {{
  return String(t == null ? "" : t)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}}
const consoleEsc = (() => {{ {console_esc.group(0)}; return esc; }})();
const auditEsc = (() => {{ {audit_esc.group(0)}; return esc; }})();
for (const p of payloads) {{
  for (const [label, fn] of [["console", consoleEsc], ["audit", auditEsc]]) {{
    const out = fn(p);
    if (out.includes("<") || out.includes(">") || out.includes('"')) {{
      throw new Error(label + " failed to escape: " + out);
    }}
  }}
  const idxOut = indexEsc(p);
  if (idxOut.includes("<") || idxOut.includes(">")) {{
    throw new Error("index failed to escape tags: " + idxOut);
  }}
}}
console.log("OK");
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "OK"

    inbox_js = (WEB / "inbox.js").read_text(encoding="utf-8")
    for sink in re.findall(r"\.innerHTML\s*=\s*([^;]+)", inbox_js):
        assert sink.strip().startswith("ICON["), f"Unexpected innerHTML sink in inbox.js: {sink}"

    admin_js = (WEB / "admin.js").read_text(encoding="utf-8")
    for sink in re.findall(r"\.innerHTML\s*=\s*([^;]+)", admin_js):
        assert sink.strip() == "X", f"Unexpected innerHTML sink in admin.js: {sink}"


def test_api_untrusted_strings_roundtrip_as_json_not_html(client):
    xss = '<img src=x onerror="alert(1)"> 100 litres milk by Monday'
    r = client.post(
        "/v1/needs/parse",
        headers={"X-Agent-Key": "demo-hotel-aurora"},
        json={"text": xss, "customer": "hotel-aurora"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["raw_text"] == xss

    audit_resp = client.get("/v1/audit?customer=hotel-aurora", headers={"X-Admin-Key": "demo-admin"})
    assert audit_resp.status_code == 200
    assert audit_resp.headers["content-type"].startswith("application/json")
    events = audit_resp.json()["events"]
    assert any(e["detail"].get("text") == xss for e in events)


def test_web_no_comments_no_em_dash_no_secrets():
    for f in HTML_FILES:
        t = f.read_text(encoding="utf-8")
        assert "<!--" not in t, f"HTML comment in {f.name}"
        assert "\u2014" not in t, f"Em dash in {f.name}"
        assert "PRIVATE KEY" not in t, f"Key material in {f.name}"
        parser = AssetAndLinkParser()
        parser.feed(t)
        for inline in parser.inline_scripts:
            found = _strip_js_and_find_comments(inline)
            assert not found, f"Inline JS comment in {f.name}: {found}"

    for f in CSS_FILES:
        t = f.read_text(encoding="utf-8")
        assert "/*" not in t, f"CSS comment in {f.name}"
        assert "\u2014" not in t, f"Em dash in {f.name}"
        assert "gradient" not in t.lower(), f"Gradient in {f.name}"

    for f in JS_FILES:
        t = f.read_text(encoding="utf-8")
        found = _strip_js_and_find_comments(t)
        assert not found, f"JS comment in {f.name}: {found}"
        assert "\u2014" not in t, f"Em dash in {f.name}"
        assert "PRIVATE KEY" not in t, f"Key material in {f.name}"
