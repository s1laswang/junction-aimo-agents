from datetime import date, timedelta
import io
import json
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import allocate, catalog, keys, offers, valio
from aimo.app import app
from aimo.models import Mandate, Product, UnitSize

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
        "allergen-picks",
        "allergen-note",
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
        "sim-picks",
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
    for allergen in ["lactose", "milk", "gluten", "peanut", "nuts", "egg", "soy", "fish"]:
        assert f'data-allergen="{allergen}"' in html, allergen
    for note_part in ["ML", "AM", "allowing lactose-free dairy", "blocking all dairy"]:
        assert note_part in html, note_part
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
        "/v1/orders?customer=",
        "/v1/admin/simulate",
        '"POST"',
        '"DELETE"',
    ]:
        assert snippet in js, snippet


def test_mock_constant_validates_as_pydantic_mandates():
    node = _node_bin()
    extractor = """
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8").split("\\n")[0];
const ctx = vm.createContext({});
vm.runInContext(src, ctx);
process.stdout.write(JSON.stringify(ctx.MOCK));
"""
    res = subprocess.run([node, "-e", extractor, str(WEB / "admin.js")], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert set(parsed.keys()) == {"hotel-aurora", "hospital-tays", "school-hervanta"}
    for cust, raw in parsed.items():
        m = Mandate.model_validate(raw)
        assert m.customer == cust


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

  for (const id of ["customer", "who", "agent", "off", "f", "per_order_eur", "per_week_eur", "substitutions", "allergen-picks", "allergen-note", "save", "reset", "msg", "last", "sim-sec", "sim-form", "sim-product", "sim-picks", "sim-amount", "sim-date", "sim-apply", "sim-clear", "sim-msg"]) {
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
  const hospActiveAllergens = m.byId.get("allergen-picks").children.filter(b => b.classList.contains("on")).map(b => b.dataset.allergen);
  assert.deepStrictEqual(hospActiveAllergens, ["peanut", "nuts", "egg"]);
  const hospPickBtns = m.byId.get("sim-picks").children.filter(c => c.tagName === "BUTTON");
  assert.strictEqual(hospPickBtns[0].dataset.pid, "462775");

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
    if (path.startsWith("/v1/orders")) {
      return {
        ok: true,
        status: 200,
        async json() {
          return [
            {
              order_id: "ord_blocked_99",
              status: "blocked",
              lines: [{ product_id: "999999", name: "Blocked Peanut Product", quantity: 5 }]
            },
            {
              order_id: "ord_10",
              status: "confirmed",
              lines: [
                { product_id: "463982", name: "Kieku Barn Eggs L180 12300g", quantity: 4 },
                { product_id: "460468", name: "Paulig Presidentti Tumma Paahto 18x300g", quantity: 3 }
              ]
            },
            {
              order_id: "ord_11",
              status: "pending_approval",
              lines: [
                { product_id: "410008", name: "Valio Eila laktoositon kevytmaito 1 L", quantity: 200 },
                { product_id: "463982", name: "Kieku Barn Eggs L180 12300g", quantity: 2 }
              ]
            }
          ];
        }
      };
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
  await live.flush();

  const expectedAllergens = ["lactose", "milk", "gluten", "peanut", "nuts", "egg", "soy", "fish"];
  const mAllergenBtns = m.byId.get("allergen-picks").children;
  assert.deepStrictEqual(mAllergenBtns.map(b => b.dataset.allergen), expectedAllergens);
  assert.ok(m.byId.get("allergen-note").textContent.includes("ML"));
  assert.ok(m.byId.get("allergen-note").textContent.includes("AM"));
  assert.ok(m.byId.get("allergen-note").textContent.includes("allowing lactose-free dairy"));
  assert.ok(m.byId.get("allergen-note").textContent.includes("blocking all dairy"));

  await m.byId.get("reset").trigger("click");
  const soyBtnInit = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "soy");
  assert.strictEqual(soyBtnInit.getAttribute("aria-pressed"), "true");
  assert.strictEqual(soyBtnInit.classList.contains("on"), true);

  const lactoseBtn = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "lactose");
  assert.strictEqual(lactoseBtn.getAttribute("aria-pressed"), "false");
  await lactoseBtn.trigger("click");
  assert.strictEqual(m.byId.get("reset").disabled, false);
  const lactoseBtnAfter = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "lactose");
  assert.strictEqual(lactoseBtnAfter.getAttribute("aria-pressed"), "true");
  assert.strictEqual(lactoseBtnAfter.classList.contains("on"), true);
  assert.ok(m.byId.get("allergen-note").textContent.includes("lactose-free dairy allowed"));

  const milkBtn = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "milk");
  await milkBtn.trigger("click");
  const milkBtnAfter = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "milk");
  assert.strictEqual(milkBtnAfter.getAttribute("aria-pressed"), "true");
  assert.strictEqual(milkBtnAfter.classList.contains("on"), true);
  assert.ok(m.byId.get("allergen-note").textContent.includes("all dairy blocked"));

  await milkBtnAfter.trigger("click");
  const lactoseBtnOff = m.byId.get("allergen-picks").children.find(b => b.dataset.allergen === "lactose");
  await lactoseBtnOff.trigger("click");
  assert.strictEqual(m.byId.get("reset").disabled, true);

  const mockPickBtns = m.byId.get("sim-picks").children.filter(c => c.tagName === "BUTTON");
  assert.ok(mockPickBtns.length >= 2);
  assert.strictEqual(mockPickBtns[0].dataset.pid, "463982");
  assert.ok(mockPickBtns[0].textContent.includes("463982"));
  assert.ok(mockPickBtns[0].textContent.includes("Kieku"));
  await mockPickBtns[0].trigger("click");
  assert.strictEqual(m.byId.get("sim-product").value, "463982");
  assert.strictEqual(mockPickBtns[0].getAttribute("aria-pressed"), "true");
  assert.strictEqual(mockPickBtns[0].classList.contains("on"), true);
  assert.strictEqual(mockFetchCalls, 0);

  const ordersCall = calls.find(c => c.path === "/v1/orders?customer=hotel-aurora" && c.method === "GET");
  assert.ok(ordersCall);
  assert.strictEqual(ordersCall.headers["X-Admin-Key"], "demo-admin");
  const livePickBtns = live.byId.get("sim-picks").children.filter(c => c.tagName === "BUTTON");
  assert.deepStrictEqual(livePickBtns.map(b => b.dataset.pid), ["410008", "463982", "460468"]);
  assert.ok(livePickBtns[0].textContent.includes("410008"));
  assert.ok(livePickBtns[0].textContent.includes("Valio Eila"));
  assert.ok(!livePickBtns.some(b => b.dataset.pid === "999999"));

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

  live.byId.get("sim-product").value = "";
  await livePickBtns[0].trigger("click");
  assert.strictEqual(live.byId.get("sim-product").value, "410008");
  assert.strictEqual(livePickBtns[0].getAttribute("aria-pressed"), "true");
  assert.strictEqual(livePickBtns[0].classList.contains("on"), true);
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
  assert.strictEqual(livePickBtns[0].getAttribute("aria-pressed"), "false");
  assert.strictEqual(livePickBtns[0].classList.contains("on"), false);
  const simDel = calls.find(c => c.path === "/v1/admin/simulate" && c.method === "DELETE");
  assert.ok(simDel);
  assert.strictEqual(simDel.headers["X-Admin-Key"], "demo-admin");

  const emptyOrdersLive = makeHarness("", async (path) => {
    if (path.startsWith("/v1/mandate")) {
      return { ok: true, status: 200, async json() { return { customer: "hotel-aurora", agent_id: "buyer-aurora", per_order_eur: 2500, per_week_eur: 15000, exclude_allergens: [], allowed_categories: null, substitutions: "within_prefs", preferred_brands: ["Valio"], approvers: ["Laura Virtanen"] }; } };
    }
    if (path.startsWith("/v1/orders")) {
      return { ok: true, status: 200, async json() { return [{ order_id: "ord_rej", status: "rejected", lines: [{ product_id: "777", name: "Rejected", quantity: 1 }] }]; } };
    }
    if (path.startsWith("/v1/audit")) {
      return { ok: true, status: 200, async json() { return { events: [] }; } };
    }
    throw new Error("unexpected path " + path);
  });
  await emptyOrdersLive.flush();
  await emptyOrdersLive.flush();
  await emptyOrdersLive.flush();
  assert.strictEqual(emptyOrdersLive.byId.get("sim-picks").children.filter(c => c.tagName === "BUTTON").length, 0);
  assert.strictEqual(emptyOrdersLive.byId.get("sim-picks").textContent, "No active orders yet for this customer.");
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


def test_e2e_mandate_lactose_vs_milk_policy_outcome_and_orders_list(tmp_path, monkeypatch):
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "allergen_e2e.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(keys, "_cache", {})
    monkeypatch.setattr(offers, "_overrides", {})
    monkeypatch.setattr(allocate, "_data", lambda: {})

    items = {
        "1001": {
            "product": Product(
                product_id="1001",
                name="Valio Eila laktoositon kevytmaito 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk"],
                labels=["free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.25,
            "gross": 1.43,
            "stock": 500.0,
        },
        "1004": {
            "product": Product(
                product_id="1004",
                name="Valio Kuohukerma 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk", "lactose"],
                labels=[],
                origin="FI",
                temperature=4,
            ),
            "net": 3.40,
            "gross": 3.88,
            "stock": 500.0,
        },
        "2002": {
            "product": Product(
                product_id="2002",
                name="Valio Oddlygood Kaurajuoma gluteeniton 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["oats"],
                labels=["free_from_gluten", "free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.55,
            "gross": 1.77,
            "stock": 500.0,
        },
    }

    monkeypatch.setattr(catalog, "get", lambda pid: items[str(pid)]["product"] if str(pid) in items else None)
    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [v["product"] for v in items.values()])
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.98 if str(pid) in items else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.1 if str(pid) in items else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, day, ids: {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": items[str(pid)]["net"]},
                "priceWithTax": {"value": items[str(pid)]["gross"]},
            }
            for pid in ids
            if str(pid) in items
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, day, ids: {
            str(pid): {
                "productId": str(pid),
                "closingTime": "2026-11-12T23:59:59+02:00",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": items[str(pid)]["stock"],
                        "availableDate": day.isoformat(),
                        "salesUnit": items[str(pid)]["product"].sales_unit,
                    }
                ],
            }
            for pid in ids
            if str(pid) in items
        },
    )

    delivery = (date.today() + timedelta(days=3)).isoformat()
    with TestClient(app) as client:
        base_mandate = client.get("/v1/mandate?customer=hotel-aurora", headers=ADMIN_HEADERS).json()

        lactose_mandate = {**base_mandate, "exclude_allergens": ["lactose"]}
        r_lactose_put = client.put("/v1/mandate", headers=ADMIN_HEADERS, json=lactose_mandate)
        assert r_lactose_put.status_code == 200
        assert r_lactose_put.json()["exclude_allergens"] == ["lactose"]

        q_lf_under_lactose = client.post(
            "/v1/quotes",
            headers=AGENT_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 20.0}], "delivery_date": delivery},
        ).json()
        ord_lf_under_lactose = client.post(
            "/v1/orders",
            headers=AGENT_HEADERS,
            json={"quote_id": q_lf_under_lactose["quote_id"], "jws": q_lf_under_lactose["jws"]},
        ).json()
        assert ord_lf_under_lactose["decision"]["outcome"] == "auto"
        assert ord_lf_under_lactose["status"] == "confirmed"
        assert any(r["code"] == "WITHIN_MANDATE" for r in ord_lf_under_lactose["decision"]["reasons"])

        q_cream_under_lactose = client.post(
            "/v1/quotes",
            headers=AGENT_HEADERS,
            json={"offers": [{"product_id": "1004", "quantity": 10.0}], "delivery_date": delivery},
        ).json()
        ord_cream_under_lactose = client.post(
            "/v1/orders",
            headers=AGENT_HEADERS,
            json={"quote_id": q_cream_under_lactose["quote_id"], "jws": q_cream_under_lactose["jws"]},
        ).json()
        assert ord_cream_under_lactose["decision"]["outcome"] == "blocked"
        assert ord_cream_under_lactose["status"] == "blocked"
        assert any(
            r["code"] == "ALLERGEN_CONFLICT" and "lactose" in r["text"]
            for r in ord_cream_under_lactose["decision"]["reasons"]
        )

        milk_mandate = {**base_mandate, "exclude_allergens": ["milk"]}
        r_milk_put = client.put("/v1/mandate", headers=ADMIN_HEADERS, json=milk_mandate)
        assert r_milk_put.status_code == 200
        assert r_milk_put.json()["exclude_allergens"] == ["milk"]

        q_lf_under_milk = client.post(
            "/v1/quotes",
            headers=AGENT_HEADERS,
            json={"offers": [{"product_id": "1001", "quantity": 20.0}], "delivery_date": delivery},
        ).json()
        ord_lf_under_milk = client.post(
            "/v1/orders",
            headers=AGENT_HEADERS,
            json={"quote_id": q_lf_under_milk["quote_id"], "jws": q_lf_under_milk["jws"]},
        ).json()
        assert ord_lf_under_milk["decision"]["outcome"] == "blocked"
        assert ord_lf_under_milk["status"] == "blocked"
        assert any(
            r["code"] == "ALLERGEN_CONFLICT" and "milk" in r["text"]
            for r in ord_lf_under_milk["decision"]["reasons"]
        )

        q_oat_under_milk = client.post(
            "/v1/quotes",
            headers=AGENT_HEADERS,
            json={"offers": [{"product_id": "2002", "quantity": 20.0}], "delivery_date": delivery},
        ).json()
        ord_oat_under_milk = client.post(
            "/v1/orders",
            headers=AGENT_HEADERS,
            json={"quote_id": q_oat_under_milk["quote_id"], "jws": q_oat_under_milk["jws"]},
        ).json()
        assert ord_oat_under_milk["decision"]["outcome"] == "auto"
        assert ord_oat_under_milk["status"] == "confirmed"

        listed_orders = client.get("/v1/orders?customer=hotel-aurora", headers=ADMIN_HEADERS)
        assert listed_orders.status_code == 200
        active_pids = [
            line["product_id"]
            for o in reversed(listed_orders.json())
            if o["status"] in ("placed", "approved", "confirmed", "pending_approval")
            for line in o["lines"]
        ]
        assert active_pids == ["2002", "1001"]
