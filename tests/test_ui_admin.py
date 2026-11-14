import io
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import offers
from aimo.app import app

WEB = Path(__file__).resolve().parent.parent / "web"
SELF = Path(__file__).resolve()
FILES = ["index.html", "style.css", "shell.js", "admin.html", "admin.js", "admin.css"]
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27bf\u2b50\u2b55]")
ADMIN_HEADERS = {"X-Admin-Key": "demo-admin"}
AGENT_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}


def _node_bin():
    fixed = Path("/home/silas/tools/node/bin/node")
    if fixed.is_file():
        return str(fixed)
    found = shutil.which("node")
    assert found, "node binary not found"
    return found


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "admin_ui.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    offers.clear_simulation()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        offers.clear_simulation()


def test_files_exist():
    for f in FILES:
        assert (WEB / f).is_file(), f


def test_no_emoji_or_em_dash():
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert not EMOJI.search(t), f
        assert "\u2014" not in t, f


def test_no_external_scripts():
    for f in ["index.html", "admin.html"]:
        for src in re.findall(r'<script[^>]+src="([^"]+)"', (WEB / f).read_text(encoding="utf-8")):
            assert src.startswith("/"), src
    css = (WEB / "style.css").read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\"')\s]+", css)
    assert all(u.startswith("https://fonts.googleapis.com/") for u in urls)


def test_tokens_and_header():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for t in ["--paper:#F5F4F0", "--valio:#1F4E8C", "--stop:#A3342B", "prefers-reduced-motion", "table.ledger"]:
        assert t in css
    assert 'aria-current="page"' in (WEB / "admin.html").read_text(encoding="utf-8")


def test_mock_approvers_match_mandates():
    js = (WEB / "admin.js").read_text(encoding="utf-8")
    for name in ["Laura Virtanen", "Mikko Heikkinen", "Helena Korhonen", "Antti Lahtinen", "Sari Nieminen"]:
        assert name in js
    for stale in ["head.chef", "kitchen.manager", "dietitian", "school.cook"]:
        assert stale not in js
    assert "Limits must be greater than zero." in js


def test_zero_comments_and_visual_hygiene():
    grad_word = "grad" + "ient"
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert grad_word not in t.lower(), f
    html = (WEB / "admin.html").read_text(encoding="utf-8")
    for marker in ["<!--", "-->", "/*"]:
        assert marker not in html, marker
    js = (WEB / "admin.js").read_text(encoding="utf-8")
    for marker in ["//", "/*", "*/", "<!--"]:
        assert marker not in js, marker
    self_text = SELF.read_text(encoding="utf-8")
    assert not EMOJI.search(self_text)
    assert "\u2014" not in self_text
    assert grad_word not in self_text.lower()
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(self_text).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert not comments, comments


def test_node_syntax_check():
    node = _node_bin()
    for f in ["admin.js", "shell.js"]:
        res = subprocess.run([node, "--check", str(WEB / f)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stderr


def test_admin_html_ids_labels_and_simulation_wiring():
    html = (WEB / "admin.html").read_text(encoding="utf-8")
    js = (WEB / "admin.js").read_text(encoding="utf-8")
    expected_ids = [
        "customer",
        "agent",
        "who",
        "off",
        "f",
        "per_order_eur",
        "per_week_eur",
        "substitutions",
        "exclude_allergens-in",
        "allowed_categories-in",
        "preferred_brands-in",
        "approvers-in",
        "save",
        "reset",
        "msg",
        "last",
        "sim-sec",
        "sim-form",
        "sim-product",
        "sim-amount",
        "sim-date",
        "sim-apply",
        "sim-clear",
        "sim-msg",
    ]
    for elem_id in expected_ids:
        assert f'id="{elem_id}"' in html, elem_id
    for label_for in re.findall(r'<label[^>]+for="([^"]+)"', html):
        assert f'id="{label_for}"' in html, label_for
    assert "Stock simulation" in html
    assert "Clear simulation" in html
    for snippet in [
        "Number.isFinite(cur.per_order_eur)",
        "Number.isFinite(cur.per_week_eur)",
        "Limits must be greater than zero.",
        "Per order limit exceeds weekly limit.",
        "At least one approver is required.",
        "Saved (mock data).",
        "Reset to saved mandate.",
        "/audit.html",
        "/v1/mandate",
        "/v1/admin/simulate",
        '"POST"',
        '"DELETE"',
    ]:
        assert snippet in js, snippet


def test_admin_js_mock_and_live_flows():
    node = _node_bin()
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeHarness(search, fetchImpl) {
  const storage = new Map();
  const byId = new Map();
  const chipBoxes = new Map();
  const winListeners = new Map();

  class Elem {
    constructor(tag, id) {
      this.tagName = tag.toUpperCase();
      this.id = id || "";
      this.value = "";
      this.hidden = false;
      this.disabled = false;
      this.className = "";
      this.dataset = {};
      this.attributes = {};
      this.children = [];
      this.parentNode = null;
      this.onclick = null;
      this._listeners = {};
      this._text = "";
      this._html = "";
      const self = this;
      this.classList = {
        _set: new Set(),
        toggle(cls, force) {
          const on = force === undefined ? !this._set.has(cls) : !!force;
          if (on) this._set.add(cls); else this._set.delete(cls);
          self.className = Array.from(this._set).join(" ");
          return on;
        },
        contains(cls) { return this._set.has(cls); }
      };
      if (this.id) byId.set(this.id, this);
    }
    set textContent(v) {
      this._text = String(v == null ? "" : v);
      this.children = [];
    }
    get textContent() {
      return this._text + this.children.map(c => typeof c === "string" ? c : c.textContent).join("");
    }
    set innerHTML(v) {
      this._html = String(v == null ? "" : v);
      this._text = "";
      this.children = [];
    }
    get innerHTML() { return this._html; }
    append(...nodes) {
      for (const n of nodes) {
        if (typeof n === "object" && n !== null) n.parentNode = this;
        this.children.push(n);
      }
    }
    insertBefore(node, ref) {
      node.parentNode = this;
      const idx = this.children.indexOf(ref);
      if (idx < 0) this.children.push(node);
      else this.children.splice(idx, 0, node);
    }
    remove() {
      if (!this.parentNode) return;
      const idx = this.parentNode.children.indexOf(this);
      if (idx >= 0) this.parentNode.children.splice(idx, 1);
    }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k]; }
    focus() {}
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    }
    async trigger(ev, extra) {
      const e = Object.assign({ preventDefault() {} }, extra || {});
      for (const fn of (this._listeners[ev] || [])) await fn.call(this, e);
      if (ev === "click" && typeof this.onclick === "function") await this.onclick(e);
    }
    querySelector(sel) {
      if (sel === "input") return this.children.find(c => c && c.tagName === "INPUT") || null;
      return null;
    }
    querySelectorAll(sel) {
      if (sel === ".chip") return this.children.filter(c => c && c.className === "chip");
      return [];
    }
  }

  const mockTag = new Elem("span", "");
  mockTag.classList.toggle("mocktag", true);
  const topBar = new Elem("header", "");
  topBar.classList.toggle("top", true);
  const menuBtn = new Elem("button", "");

  for (const id of ["customer", "who", "agent", "off", "f", "per_order_eur", "per_week_eur", "substitutions", "save", "reset", "msg", "last", "sim-sec", "sim-form", "sim-product", "sim-amount", "sim-date", "sim-apply", "sim-clear", "sim-msg"]) {
    new Elem("div", id);
  }
  byId.get("sim-amount").value = "0";
  byId.get("reset").disabled = true;

  const chipInputs = [];
  for (const k of ["exclude_allergens", "allowed_categories", "preferred_brands", "approvers"]) {
    const box = new Elem("div", "");
    box.dataset.k = k;
    const inp = new Elem("input", k + "-in");
    box.append(inp);
    chipBoxes.set(k, box);
    chipInputs.push(inp);
  }

  const document = {
    readyState: "complete",
    getElementById(id) { return byId.get(id) || null; },
    createElement(tag) { return new Elem(tag, ""); },
    querySelector(sel) {
      if (sel === ".mocktag") return mockTag;
      if (sel === ".menu-btn") return menuBtn;
      if (sel === ".top") return topBar;
      const m = /^\[data-k="([^"]+)"\](\s+input)?$/.exec(sel);
      if (m) {
        const box = chipBoxes.get(m[1]);
        return m[2] ? box.querySelector("input") : box;
      }
      return null;
    },
    querySelectorAll(sel) {
      if (sel === ".chips input") return chipInputs;
      return [];
    },
    addEventListener() {}
  };

  const sandbox = {
    location: { search },
    localStorage: {
      getItem(k) { return storage.has(k) ? storage.get(k) : null; },
      setItem(k, v) { storage.set(k, String(v)); }
    },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    addEventListener(ev, fn) { (winListeners.get(ev) || winListeners.set(ev, []).get(ev)).push(fn); },
    dispatchEvent(e) { for (const fn of (winListeners.get(e.type) || [])) fn(e); },
    fetch: fetchImpl,
    document,
    Intl,
    URLSearchParams,
    JSON,
    Number,
    Object,
    Array,
    Date,
    encodeURIComponent
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext("this.window = this;", ctx);
  vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  return { byId, chipBoxes, mockTag, ctx, flush: () => new Promise(r => setImmediate(r)) };
}

(async () => {
  let mockFetchCalls = 0;
  const m = makeHarness("?mock=1", async () => { mockFetchCalls++; throw new Error("unexpected network call"); });
  await m.flush();

  assert.strictEqual(m.mockTag.classList.contains("on"), true);
  assert.strictEqual(m.byId.get("who").textContent, "hotel-aurora");
  assert.strictEqual(m.byId.get("per_order_eur").value, "2500");
  assert.strictEqual(m.byId.get("reset").disabled, true);

  m.byId.get("per_order_eur").value = "3100";
  await m.byId.get("per_order_eur").trigger("input");
  assert.strictEqual(m.byId.get("reset").disabled, false);

  await m.byId.get("reset").trigger("click");
  assert.strictEqual(m.byId.get("per_order_eur").value, "2500");
  assert.strictEqual(m.byId.get("reset").disabled, true);
  assert.strictEqual(m.byId.get("msg").textContent, "Reset to saved mandate.");

  m.byId.get("per_order_eur").value = "0";
  await m.byId.get("f").trigger("submit");
  assert.strictEqual(m.byId.get("msg").textContent, "Limits must be greater than zero.");

  m.byId.get("per_order_eur").value = "";
  await m.byId.get("f").trigger("submit");
  assert.strictEqual(m.byId.get("msg").textContent, "Limits must be greater than zero.");

  m.byId.get("per_order_eur").value = "20000";
  await m.byId.get("f").trigger("submit");
  assert.strictEqual(m.byId.get("msg").textContent, "Per order limit exceeds weekly limit.");

  m.byId.get("per_order_eur").value = "2500";
  while (m.chipBoxes.get("approvers").querySelectorAll(".chip").length) {
    const chip = m.chipBoxes.get("approvers").querySelectorAll(".chip")[0];
    await chip.children[1].trigger("click");
  }
  await m.byId.get("f").trigger("submit");
  assert.strictEqual(m.byId.get("msg").textContent, "At least one approver is required.");

  await m.byId.get("reset").trigger("click");
  m.byId.get("per_order_eur").value = "3200";
  m.byId.get("exclude_allergens-in").value = "soy";
  await m.byId.get("f").trigger("submit");
  assert.strictEqual(m.byId.get("msg").textContent, "Saved (mock data).");
  assert.strictEqual(m.byId.get("reset").disabled, true);
  assert.strictEqual(mockFetchCalls, 0);

  m.byId.get("customer").value = "hospital-tays";
  await m.byId.get("customer").trigger("change");
  await m.flush();
  assert.strictEqual(m.byId.get("per_order_eur").value, "1500");

  m.byId.get("customer").value = "hotel-aurora";
  await m.byId.get("customer").trigger("change");
  await m.flush();
  assert.strictEqual(m.byId.get("per_order_eur").value, "3200");

  m.byId.get("sim-product").value = "";
  await m.byId.get("sim-form").trigger("submit");
  assert.strictEqual(m.byId.get("sim-msg").textContent, "Product ID is required.");

  m.byId.get("sim-product").value = "410008";
  m.byId.get("sim-amount").value = "-1";
  await m.byId.get("sim-form").trigger("submit");
  assert.strictEqual(m.byId.get("sim-msg").textContent, "Available amount must be zero or greater.");

  m.byId.get("sim-amount").value = "0";
  m.byId.get("sim-date").value = "2026-11-18";
  await m.byId.get("sim-form").trigger("submit");
  assert.strictEqual(m.byId.get("sim-msg").textContent, "Simulated 410008 at 0 on 2026-11-18 (mock data).");

  await m.byId.get("sim-clear").trigger("click");
  assert.strictEqual(m.byId.get("sim-msg").textContent, "Simulation cleared (mock data).");
  assert.strictEqual(m.byId.get("sim-product").value, "");
  assert.strictEqual(mockFetchCalls, 0);

  const calls = [];
  let seq = 7;
  let rejectNextPut = false;
  const live = makeHarness("", async (path, opts) => {
    calls.push({ path, method: (opts && opts.method) || "GET", headers: opts && opts.headers, body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (path.startsWith("/v1/mandate") && (!opts || !opts.method)) {
      return { ok: true, status: 200, async json() { return { customer: "hotel-aurora", agent_id: "buyer-aurora", per_order_eur: 2500, per_week_eur: 15000, exclude_allergens: [], allowed_categories: null, substitutions: "within_prefs", preferred_brands: ["Valio"], approvers: ["Laura Virtanen"] }; } };
    }
    if (path.startsWith("/v1/audit")) {
      return { ok: true, status: 200, async json() { return { events: [{ seq, ts: "2026-11-14T15:00:00Z", actor: "human:admin", action: "mandate_updated" }] }; } };
    }
    if (path === "/v1/mandate" && opts.method === "PUT") {
      if (rejectNextPut) {
        rejectNextPut = false;
        return { ok: false, status: 409, async json() { return { code: "INVALID_MANDATE", text: "Server rejected mandate." }; } };
      }
      seq = 8;
      return { ok: true, status: 200, async json() { return JSON.parse(opts.body); } };
    }
    if (path === "/v1/admin/simulate") {
      return { ok: true, status: 200, async json() { return { ok: true }; } };
    }
    throw new Error("unexpected path " + path);
  });
  await live.flush();
  await live.flush();

  assert.ok(live.byId.get("last").textContent.includes("#7"));
  const lastLink = live.byId.get("last").children.find(c => c && c.tagName === "A");
  assert.strictEqual(lastLink.href, "/audit.html#e7");

  live.byId.get("per_order_eur").value = "2800";
  await live.byId.get("f").trigger("submit");
  assert.strictEqual(live.byId.get("msg").textContent, "Saved, recorded in audit #8.");
  const msgLink = live.byId.get("msg").children.find(c => c && c.tagName === "A");
  assert.strictEqual(msgLink.href, "/audit.html#e8");
  assert.strictEqual(live.byId.get("reset").disabled, true);
  const putCall = calls.find(c => c.method === "PUT" && c.path === "/v1/mandate");
  assert.strictEqual(putCall.headers["X-Admin-Key"], "demo-admin");
  assert.strictEqual(putCall.body.allowed_categories, null);

  rejectNextPut = true;
  live.byId.get("per_order_eur").value = "2900";
  await live.byId.get("f").trigger("submit");
  assert.strictEqual(live.byId.get("msg").textContent, "Blocked: Server rejected mandate.");

  live.byId.get("sim-product").value = "410008";
  live.byId.get("sim-amount").value = "0";
  live.byId.get("sim-date").value = "";
  await live.byId.get("sim-form").trigger("submit");
  assert.strictEqual(live.byId.get("sim-msg").textContent, "Simulated 410008 at 0.");
  const simPost = calls.filter(c => c.path === "/v1/admin/simulate" && c.method === "POST")[0];
  assert.deepStrictEqual(simPost.body, { product_id: "410008", available_amount: 0 });
  assert.strictEqual(simPost.headers["X-Admin-Key"], "demo-admin");

  live.byId.get("sim-date").value = "2026-11-19";
  live.byId.get("sim-amount").value = "15";
  await live.byId.get("sim-form").trigger("submit");
  assert.strictEqual(live.byId.get("sim-msg").textContent, "Simulated 410008 at 15 on 2026-11-19.");

  await live.byId.get("sim-clear").trigger("click");
  assert.strictEqual(live.byId.get("sim-msg").textContent, "Simulation cleared.");
  const simDel = calls.find(c => c.path === "/v1/admin/simulate" && c.method === "DELETE");
  assert.ok(simDel);
  assert.strictEqual(simDel.headers["X-Admin-Key"], "demo-admin");
})();
"""
    res = subprocess.run(
        [node, "-e", harness, str(WEB / "shell.js"), str(WEB / "admin.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr or res.stdout


def test_gateway_serves_admin_assets(api_client):
    for route, token in [
        ("/admin.html", 'id="sim-form"'),
        ("/admin.js", "/v1/admin/simulate"),
        ("/admin.css", ".chips"),
    ]:
        r = api_client.get(route)
        assert r.status_code == 200, route
        assert token in r.text


def test_api_mandate_get_put_and_validation(api_client):
    r_get = api_client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN_HEADERS)
    assert r_get.status_code == 200
    base = r_get.json()
    assert base["customer"] == "hotel-aurora"
    assert base["agent_id"] == "buyer-aurora"
    assert base["per_order_eur"] == 2500.0
    assert base["per_week_eur"] == 15000.0
    assert base["approvers"] == ["Laura Virtanen", "Mikko Heikkinen"]

    assert api_client.put("/v1/mandate", json=base).status_code == 401
    assert api_client.put("/v1/mandate", headers=AGENT_HEADERS, json=base).status_code == 403

    zero_order = {**base, "per_order_eur": 0.0}
    r_zero = api_client.put("/v1/mandate", headers=ADMIN_HEADERS, json=zero_order)
    assert r_zero.status_code == 409
    assert r_zero.json()["code"] == "INVALID_MANDATE"

    zero_week = {**base, "per_week_eur": 0.0}
    r_zero_w = api_client.put("/v1/mandate", headers=ADMIN_HEADERS, json=zero_week)
    assert r_zero_w.status_code == 409
    assert r_zero_w.json()["code"] == "INVALID_MANDATE"

    over_week = {**base, "per_order_eur": 16000.0, "per_week_eur": 15000.0}
    r_over = api_client.put("/v1/mandate", headers=ADMIN_HEADERS, json=over_week)
    assert r_over.status_code == 409
    assert r_over.json()["code"] == "INVALID_MANDATE"

    updated = {
        **base,
        "per_order_eur": 3100.0,
        "per_week_eur": 16500.0,
        "exclude_allergens": ["soy"],
        "substitutions": "ask",
    }
    r_put = api_client.put("/v1/mandate", headers=ADMIN_HEADERS, json=updated)
    assert r_put.status_code == 200
    assert r_put.json()["per_order_eur"] == 3100.0
    assert r_put.json()["exclude_allergens"] == ["soy"]

    r_after = api_client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN_HEADERS)
    assert r_after.status_code == 200
    assert r_after.json()["per_order_eur"] == 3100.0
    assert r_after.json()["substitutions"] == "ask"

    r_audit = api_client.get("/v1/audit?customer=hotel-aurora", headers=ADMIN_HEADERS)
    assert r_audit.status_code == 200
    audit_body = r_audit.json()
    assert audit_body["verified"]["ok"] is True
    mandate_events = [e for e in audit_body["events"] if e["action"] == "mandate_updated"]
    assert len(mandate_events) == 1
    assert "3100.0" in mandate_events[0]["why"]


def test_api_admin_simulate_post_and_delete(api_client):
    payload = {"product_id": "410008", "available_amount": 0}
    assert api_client.post("/v1/admin/simulate", json=payload).status_code == 401
    assert api_client.post("/v1/admin/simulate", headers=AGENT_HEADERS, json=payload).status_code == 403
    assert api_client.delete("/v1/admin/simulate").status_code == 401
    assert api_client.delete("/v1/admin/simulate", headers=AGENT_HEADERS).status_code == 403

    r_sim = api_client.post("/v1/admin/simulate", headers=ADMIN_HEADERS, json=payload)
    assert r_sim.status_code == 200
    assert r_sim.json() == {"ok": True}
    assert "410008" in offers._overrides
    assert offers._overrides["410008"].available_amount == 0.0
    assert offers._overrides["410008"].available_date is None

    dated_payload = {"product_id": "410008", "available_amount": 24.0, "available_date": "2026-11-18"}
    r_dated = api_client.post("/v1/admin/simulate", headers=ADMIN_HEADERS, json=dated_payload)
    assert r_dated.status_code == 200
    assert r_dated.json() == {"ok": True}
    assert offers._overrides["410008"].available_amount == 24.0
    assert str(offers._overrides["410008"].available_date) == "2026-11-18"

    r_del = api_client.delete("/v1/admin/simulate", headers=ADMIN_HEADERS)
    assert r_del.status_code == 200
    assert r_del.json() == {"ok": True}
    assert "410008" not in offers._overrides
