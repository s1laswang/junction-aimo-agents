import json
from pathlib import Path
import re
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PAGES = ["index.html", "console.html", "inbox.html", "audit.html", "admin.html"]
EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b50\u2b55\u2705\u274c]")
EM_DASH = "\u2014"
GRAD_WORD = "grad" + "ient"
SLASH_COMMENT = "/" + "/"
BLOCK_OPEN = "/" + "*"
BLOCK_CLOSE = "*" + "/"
HTML_OPEN = "<!" + "--"

NODE_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const shellPath = process.argv[2];
const scenario = process.argv[3];
const code = fs.readFileSync(shellPath, "utf8");

function makeClassList(initial) {
  const set = new Set((initial || []).filter(Boolean));
  return {
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    contains(c) { return set.has(c); },
    toggle(c, force) {
      const next = force === undefined ? !set.has(c) : Boolean(force);
      if (next) set.add(c); else set.delete(c);
      return next;
    },
    values() { return Array.from(set); }
  };
}

function makeEl(tag, classes) {
  const listeners = {};
  const attrs = {};
  return {
    tagName: tag.toUpperCase(),
    value: "",
    innerHTML: "",
    focusCalls: 0,
    classList: makeClassList(classes),
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type, ev) {
      const e = Object.assign({ type }, ev || {});
      (listeners[type] || []).slice().forEach((fn) => fn(e));
    },
    focus() { this.focusCalls += 1; }
  };
}

function createEnv(opts) {
  const cfg = opts || {};
  const store = Object.assign({}, cfg.storage || {});
  const localStorage = {
    getItem(k) {
      if (cfg.throwStorage) throw new Error("blocked");
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
    },
    setItem(k, v) {
      if (cfg.throwStorage) throw new Error("blocked");
      store[k] = String(v);
    },
    removeItem(k) { delete store[k]; },
    dump() { return Object.assign({}, store); }
  };

  const topEl = makeEl("header", ["top"]);
  const menuBtn = makeEl("button", ["btn", "btn-quiet", "menu-btn"]);
  menuBtn.setAttribute("aria-expanded", "false");
  const mockTag = makeEl("span", ["tag", "warn", "mocktag"]);
  const customerSel = cfg.noSelect ? null : makeEl("select", []);

  const docListeners = {};
  const winListeners = {};
  const location = { search: cfg.search || "", origin: "localhost", pathname: "/" };

  const document = {
    readyState: cfg.readyState || "complete",
    getElementById(id) {
      if (id === "customer") return customerSel;
      return null;
    },
    querySelector(sel) {
      if (sel === ".top") return topEl;
      if (sel === ".menu-btn") return menuBtn;
      if (sel === ".mocktag") return mockTag;
      if (sel === "#customer") return customerSel;
      return null;
    },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    dispatchEvent(ev) {
      (docListeners[ev.type] || []).slice().forEach((fn) => fn(ev));
      return true;
    }
  };

  const calls = [];
  const fetchImpl = cfg.fetch || (async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));

  const windowObj = {
    location,
    document,
    localStorage,
    CustomEvent,
    addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
    dispatchEvent(ev) {
      (winListeners[ev.type] || []).slice().forEach((fn) => fn(ev));
      return true;
    }
  };

  const ctx = vm.createContext({
    window: windowObj,
    document,
    location,
    localStorage,
    fetch: async (url, reqOpts) => {
      calls.push({ url, opts: reqOpts });
      return fetchImpl(url, reqOpts);
    },
    URLSearchParams,
    CustomEvent,
    Response,
    Headers,
    Object,
    Array,
    String,
    Number,
    Boolean,
    JSON,
    Promise
  });
  vm.runInContext(code, ctx, { filename: shellPath });
  return { aimo: windowObj.aimo, window: windowObj, document, location, localStorage, topEl, menuBtn, mockTag, customerSel, calls };
}

async function main() {
  if (scenario === "api_surface") {
    const env = createEnv();
    const keys = Object.keys(env.aimo).sort();
    const fallbackEnv = createEnv({ storage: { "aimo.customer": "bogus-corp" } });
    const blockedEnv = createEnv({ throwStorage: true });
    console.log(JSON.stringify({
      keys,
      customers: env.aimo.CUSTOMERS,
      defaultCustomer: env.aimo.customer(),
      selectValue: env.customerSel.value,
      selectHtml: env.customerSel.innerHTML,
      fallbackCustomer: fallbackEnv.aimo.customer(),
      blockedCustomer: blockedEnv.aimo.customer()
    }));
    return;
  }

  if (scenario === "set_customer") {
    const env = createEnv();
    const seen = [];
    env.window.addEventListener("customer", (e) => {
      seen.push({ detail: e.detail, selValue: env.customerSel.value, current: env.aimo.customer() });
    });
    env.aimo.setCustomer("hospital-tays");
    env.customerSel.value = "school-hervanta";
    env.customerSel.dispatch("change");
    env.aimo.setCustomer("not-valid");
    env.aimo.setCustomer("");
    env.aimo.setCustomer(null);
    console.log(JSON.stringify({
      seen,
      stored: env.localStorage.getItem("aimo.customer"),
      finalCustomer: env.aimo.customer(),
      finalSelect: env.customerSel.value
    }));
    return;
  }

  if (scenario === "url_customer") {
    const loadingEnv = createEnv({ search: "?customer=school-hervanta", readyState: "loading", storage: { "aimo.customer": "hotel-aurora" } });
    const beforeReady = loadingEnv.aimo.customer();
    loadingEnv.document.dispatchEvent({ type: "DOMContentLoaded" });
    const afterReadySelect = loadingEnv.customerSel.value;
    loadingEnv.aimo.setCustomer("hospital-tays");
    const afterSwitch = loadingEnv.aimo.customer();

    const invalidUrlEnv = createEnv({ search: "?customer=evil-corp", storage: { "aimo.customer": "hospital-tays" } });
    console.log(JSON.stringify({
      beforeReady,
      afterReadySelect,
      afterSwitch,
      storedAfterSwitch: loadingEnv.localStorage.getItem("aimo.customer"),
      invalidUrlCustomer: invalidUrlEnv.aimo.customer(),
      invalidUrlSelect: invalidUrlEnv.customerSel.value
    }));
    return;
  }

  if (scenario === "mock_mode") {
    const cases = ["", "?customer=hotel-aurora", "?mock", "?mock=", "?mock=1", "?mock=true", "?mock=TRUE", "?mock=broken", "?mock=0", "?mock=false", "?mock=FALSE"];
    const env = createEnv();
    const out = {};
    for (const q of cases) {
      env.location.search = q;
      out[q] = env.aimo.mockMode();
    }
    env.aimo.mock(true);
    const onState = env.mockTag.classList.contains("on");
    env.aimo.mock(false);
    const offState = env.mockTag.classList.contains("on");
    console.log(JSON.stringify({ out, onState, offState }));
    return;
  }

  if (scenario === "headers_and_body") {
    const env = createEnv();
    await env.aimo.api("/v1/mandate");
    env.aimo.setCustomer("hospital-tays");
    await env.aimo.api("/v1/mandate");
    env.aimo.setCustomer("school-hervanta");
    await env.aimo.api("/v1/mandate");
    env.localStorage.setItem("aimo.agentKey", "custom-agent-99");
    await env.aimo.api("/v1/orders");
    const callerOpts = { admin: true, method: "PUT", body: { a: 1 } };
    await env.aimo.api("/v1/mandate", callerOpts);
    env.localStorage.setItem("aimo.adminKey", "secret-admin-77");
    await env.aimo.api("/v1/approvals", { admin: true });
    console.log(JSON.stringify({
      calls: env.calls,
      callerAdminStillPresent: callerOpts.admin === true,
      callerBodyStillObject: typeof callerOpts.body === "object"
    }));
    return;
  }

  if (scenario === "empty_and_errors") {
    let responder;
    const env = createEnv({ fetch: (url, opts) => responder(url, opts) });

    responder = async () => new Response(null, { status: 204 });
    const r204 = await env.aimo.api("/v1/admin/simulate", { method: "DELETE", admin: true });
    const e204 = env.aimo.lastError;

    responder = async () => new Response("   ", { status: 200 });
    const rEmpty = await env.aimo.api("/v1/admin/simulate", { method: "POST", admin: true });
    const eEmpty = env.aimo.lastError;

    responder = async () => new Response(JSON.stringify({ code: "NOT_APPROVER", text: "Only Laura Virtanen may approve" }), { status: 403 });
    const r403 = await env.aimo.api("/v1/approvals/ord_1", { method: "POST", admin: true, body: { approve: true } });
    const e403 = env.aimo.lastError;
    const e403Str = String(e403.detail);

    responder = async () => ({
      ok: false,
      status: 409,
      json: async () => ({ detail: { code: "QUOTE_EXPIRED", text: "Quote has expired" } })
    });
    const r409 = await env.aimo.api("/v1/orders", { method: "POST", body: { quote_id: "q1" } });
    const e409 = env.aimo.lastError;
    const e409Str = String(e409.detail);

    responder = async () => new Response(JSON.stringify({ detail: [{ loc: ["body", "per_order_eur"], msg: "Field required" }] }), { status: 422 });
    const r422 = await env.aimo.api("/v1/mandate", { method: "PUT", admin: true, body: {} });
    const e422 = env.aimo.lastError;

    const panel = makeEl("div", []);
    responder = async () => new Response(JSON.stringify({ code: "UPSTREAM_UNAVAILABLE", text: "Supplier down" }), { status: 503 });
    await env.aimo.api("/v1/offers", { method: "POST", body: {} });
    const off503 = env.aimo.offline(panel);
    const off503Html = panel.innerHTML;

    responder = async () => { throw new Error("ECONNREFUSED"); };
    const rNet = await env.aimo.api("/v1/audit");
    const eNet = env.aimo.lastError;
    const offNet = env.aimo.offline(panel);
    const offNetHtml = panel.innerHTML;

    console.log(JSON.stringify({
      r204, e204, rEmpty, eEmpty, r403, e403, e403Str, r409, e409, e409Str, r422, e422, off503, off503Html, rNet, eNet, offNet, offNetHtml
    }));
    return;
  }

  if (scenario === "mobile_menu") {
    const env = createEnv();
    env.menuBtn.dispatch("click");
    const openedClass = env.topEl.classList.contains("open");
    const openedAria = env.menuBtn.getAttribute("aria-expanded");
    env.document.dispatchEvent({ type: "keydown", key: "Escape" });
    const afterEscClass = env.topEl.classList.contains("open");
    const afterEscAria = env.menuBtn.getAttribute("aria-expanded");
    const focusAfterEsc = env.menuBtn.focusCalls;
    env.document.dispatchEvent({ type: "keydown", key: "Escape" });
    const focusWhenAlreadyClosed = env.menuBtn.focusCalls;
    console.log(JSON.stringify({
      openedClass, openedAria, afterEscClass, afterEscAria, focusAfterEsc, focusWhenAlreadyClosed
    }));
    return;
  }

  throw new Error("unknown scenario: " + scenario);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


def _node_bin():
    preferred = Path("/home/silas/tools/node/bin/node")
    if preferred.is_file():
        return str(preferred)
    found = shutil.which("node")
    if not found:
        pytest.skip("node binary unavailable")
    return found


def _run_scenario(scenario):
    proc = subprocess.run(
        [_node_bin(), "-", str(WEB / "shell.js"), scenario],
        input=NODE_HARNESS,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_shell_and_style_exist_and_syntax():
    shell_js = WEB / "shell.js"
    style_css = WEB / "style.css"
    assert shell_js.is_file() and shell_js.stat().st_size > 0
    assert style_css.is_file() and style_css.stat().st_size > 0
    proc = subprocess.run(
        [_node_bin(), "--check", str(shell_js)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_shell_and_style_hygiene():
    js = (WEB / "shell.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for name, text in [("shell.js", js), ("style.css", css)]:
        assert BLOCK_OPEN not in text and BLOCK_CLOSE not in text, name
        assert HTML_OPEN not in text, name
        assert EM_DASH not in text, name
        assert not EMOJI_RE.search(text), name
        assert GRAD_WORD not in text.lower(), name
        assert not re.search(r"(?<!:)/" + r"/", text), name
    assert SLASH_COMMENT not in js
    assert not re.findall(r"https?:" + SLASH_COMMENT, js)
    urls = re.findall(r"https?:" + SLASH_COMMENT + r"[^\"')\s]+", css)
    assert urls
    assert all(u.startswith("https://fonts.googleapis.com/") for u in urls)


def test_style_tokens_focus_and_responsive_selectors():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    required = [
        "--paper:#F5F4F0",
        "--sheet:#FFFFFF",
        "--well:#EFEDE8",
        "--ink:#17212B",
        "--valio:#1F4E8C",
        "--ok:#2F6B4F",
        "--warn:#8F6412",
        "--stop:#A3342B",
        "prefers-reduced-motion",
        "table.ledger",
        "a:focus-visible",
        "button:focus-visible",
        "summary:focus-visible",
        "[tabindex]:focus-visible",
        "input:focus-visible",
        "select:focus-visible",
        "textarea:focus-visible",
        "@media (max-width:640px)",
        ".top.open",
        ".menu-btn",
    ]
    for token in required:
        assert token in css, token


def test_header_and_nav_consistency_across_all_pages():
    nav_targets = ["/console.html", "/inbox.html", "/audit.html", "/admin.html"]
    for page in PAGES:
        html = (WEB / page).read_text(encoding="utf-8")
        assert "/style.css" in html, page
        assert "/shell.js" in html, page
        assert '<header class="top">' in html, page
        assert re.search(r'<a[^>]+class="brand"[^>]+href="/"[^>]*>Aimo Trade Gateway</a>', html), page
        assert re.search(r'<button[^>]+menu-btn[^>]+aria-expanded="false"', html), page
        for target in nav_targets:
            assert f'href="{target}"' in html, (page, target)
        assert "mocktag" in html, page
        assert 'id="customer"' in html, page
        assert not re.search(r'<script[^>]+src=["\']?(?:https?:)?' + SLASH_COMMENT, html, re.I), page
        if page != "index.html":
            assert re.search(rf'href="/{re.escape(page)}"[^>]*aria-current="page"|aria-current="page"[^>]*href="/{re.escape(page)}"', html), page


def test_node_public_api_and_default_customer():
    res = _run_scenario("api_surface")
    assert res["keys"] == ["CUSTOMERS", "api", "customer", "lastError", "mock", "mockMode", "offline", "setCustomer"]
    assert res["customers"] == ["hotel-aurora", "hospital-tays", "school-hervanta"]
    assert res["defaultCustomer"] == "hotel-aurora"
    assert res["selectValue"] == "hotel-aurora"
    for slug in ["hotel-aurora", "hospital-tays", "school-hervanta"]:
        assert f'<option value="{slug}">{slug}</option>' in res["selectHtml"]
    assert res["fallbackCustomer"] == "hotel-aurora"
    assert res["blockedCustomer"] == "hotel-aurora"


def test_node_set_customer_and_select_sync():
    res = _run_scenario("set_customer")
    assert res["seen"] == [
        {"detail": "hospital-tays", "selValue": "hospital-tays", "current": "hospital-tays"},
        {"detail": "school-hervanta", "selValue": "school-hervanta", "current": "school-hervanta"},
    ]
    assert res["stored"] == "school-hervanta"
    assert res["finalCustomer"] == "school-hervanta"
    assert res["finalSelect"] == "school-hervanta"


def test_node_url_customer_query_param():
    res = _run_scenario("url_customer")
    assert res["beforeReady"] == "school-hervanta"
    assert res["afterReadySelect"] == "school-hervanta"
    assert res["afterSwitch"] == "hospital-tays"
    assert res["storedAfterSwitch"] == "hospital-tays"
    assert res["invalidUrlCustomer"] == "hospital-tays"
    assert res["invalidUrlSelect"] == "hospital-tays"


def test_node_mock_mode_and_tag_toggle():
    res = _run_scenario("mock_mode")
    out = res["out"]
    assert out[""] is False
    assert out["?customer=hotel-aurora"] is False
    assert out["?mock"] is True
    assert out["?mock="] is True
    assert out["?mock=1"] is True
    assert out["?mock=true"] is True
    assert out["?mock=TRUE"] is True
    assert out["?mock=broken"] is True
    assert out["?mock=0"] is False
    assert out["?mock=false"] is False
    assert out["?mock=FALSE"] is False
    assert res["onState"] is True
    assert res["offState"] is False


def test_node_api_key_injection_and_body_serialization():
    res = _run_scenario("headers_and_body")
    calls = res["calls"]
    assert calls[0]["opts"]["headers"]["X-Agent-Key"] == "demo-hotel-aurora"
    assert calls[1]["opts"]["headers"]["X-Agent-Key"] == "demo-hospital-tays"
    assert calls[2]["opts"]["headers"]["X-Agent-Key"] == "demo-school-hervanta"
    assert calls[3]["opts"]["headers"]["X-Agent-Key"] == "custom-agent-99"
    assert calls[4]["opts"]["headers"]["X-Admin-Key"] == "demo-admin"
    assert "X-Agent-Key" not in calls[4]["opts"]["headers"]
    assert "admin" not in calls[4]["opts"]
    assert calls[4]["opts"]["headers"]["Content-Type"] == "application/json"
    assert calls[4]["opts"]["body"] == '{"a":1}'
    assert calls[5]["opts"]["headers"]["X-Admin-Key"] == "secret-admin-77"
    assert res["callerAdminStillPresent"] is True
    assert res["callerBodyStillObject"] is True


def test_node_api_204_structured_errors_and_offline():
    res = _run_scenario("empty_and_errors")
    assert res["r204"] == {}
    assert res["e204"] is None
    assert res["rEmpty"] == {}
    assert res["eEmpty"] is None
    assert res["r403"] is None
    assert res["e403"] == {
        "status": 403,
        "code": "NOT_APPROVER",
        "text": "Only Laura Virtanen may approve",
        "detail": {"code": "NOT_APPROVER", "text": "Only Laura Virtanen may approve"},
    }
    assert res["e403Str"] == "Only Laura Virtanen may approve"
    assert res["r409"] is None
    assert res["e409"] == {
        "status": 409,
        "code": "QUOTE_EXPIRED",
        "text": "Quote has expired",
        "detail": {"code": "QUOTE_EXPIRED", "text": "Quote has expired"},
    }
    assert res["e409Str"] == "Quote has expired"
    assert res["r422"] is None
    assert res["e422"] == {
        "status": 422,
        "text": "per_order_eur: Field required",
        "detail": "per_order_eur: Field required",
    }
    assert res["off503"] == "Unavailable (503)"
    assert res["off503Html"] == '<p class="empty muted">Unavailable (503)</p>'
    assert res["rNet"] is None
    assert res["eNet"] == {"status": 0, "detail": "gateway offline"}
    assert res["offNet"] == "Gateway offline"
    assert res["offNetHtml"] == '<p class="empty muted">Gateway offline</p>'


def test_node_mobile_menu_escape_accessibility():
    res = _run_scenario("mobile_menu")
    assert res["openedClass"] is True
    assert res["openedAria"] == "true"
    assert res["afterEscClass"] is False
    assert res["afterEscAria"] == "false"
    assert res["focusAfterEsc"] == 1
    assert res["focusWhenAlreadyClosed"] == 1
