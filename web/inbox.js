(function () {
var now = Date.now();
function iso(ms) { return new Date(now + ms).toISOString(); }
function day(n) { return new Date(now + n * 864e5).toISOString().slice(0, 10); }
var MOCK = [
  { order_id: "ord_7f3a91", quote_id: "q_20261114_0042", customer: "hotel-aurora", agent_id: "aurora-buyer-1", status: "pending_approval",
    lines: [
      { product_id: "P-100234", quantity: 1000, sales_unit: "L", unit_price_net: 1.42, line_total_net: 1420 },
      { product_id: "P-100871", quantity: 40, sales_unit: "KG", unit_price_net: 14.5, line_total_net: 580 },
      { product_id: "P-204410", quantity: 12, sales_unit: "CS", unit_price_net: 28.33, line_total_net: 339.96 } ],
    total_net: 2339.96, substitutions: [],
    decision: { outcome: "needs_approval", reasons: [{ code: "per_order_limit", text: "Over per-order limit 2,000 EUR by 339.96 EUR" }] },
    created_at: iso(-12 * 6e4), updated_at: iso(-12 * 6e4), delivery_date: day(3), valid_until: iso(3.2 * 36e5) },
  { order_id: "ord_2b8c10", quote_id: "q_20261114_0043", customer: "school-hervanta", agent_id: "hervanta-buyer-1", status: "pending_approval",
    lines: [
      { product_id: "P-300118", quantity: 60, sales_unit: "KG", unit_price_net: 6.2, line_total_net: 372 },
      { product_id: "P-300552", quantity: 25, sales_unit: "KG", unit_price_net: 3.9, line_total_net: 97.5 } ],
    total_net: 469.5,
    substitutions: [{ original_id: "P-300077", substitute_id: "P-300118", reason: "Original out of stock for the requested date; same brand family, same pack size" }],
    decision: { outcome: "needs_approval", reasons: [{ code: "substitution", text: "1 product substituted and the mandate asks to be consulted first" }] },
    created_at: iso(-47 * 6e4), updated_at: iso(-47 * 6e4), delivery_date: day(2), valid_until: iso(0.6 * 36e5) },
  { order_id: "ord_91d4e7", quote_id: "q_20261114_0044", customer: "hospital-tays", agent_id: "tays-buyer-1", status: "pending_approval",
    lines: [
      { product_id: "P-410902", quantity: 200, sales_unit: "KG", unit_price_net: 4.15, line_total_net: 830 } ],
    total_net: 830,
    substitutions: [{ original_id: "P-410455", substitute_id: "P-410902", reason: "Gluten-free requirement kept, price within 5 percent" }],
    decision: { outcome: "needs_approval", reasons: [
      { code: "allergen_flag", text: "Substitute lists traces of celery; this kitchen excludes celery" },
      { code: "substitution", text: "1 product substituted" } ] },
    created_at: iso(-3 * 6e4), updated_at: iso(-3 * 6e4), delivery_date: day(1), valid_until: iso(5.5 * 36e5) }
];

var $ = function (s) { return document.querySelector(s); };
var ICON = {
  warn: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  allergen: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  verified: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  back: '<path d="m15 18-6-6 6-6"/>'
};
function icon(n) { var s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("class", "i"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.innerHTML = ICON[n]; return s; }
function h(t, a) {
  var e = document.createElement(t); a = a || {};
  for (var k in a) { if (k === "class") e.className = a[k]; else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), a[k]); else if (a[k] !== false && a[k] != null) e.setAttribute(k, a[k] === true ? "" : a[k]); }
  for (var i = 2; i < arguments.length; i++) [].concat(arguments[i]).forEach(function (x) { if (x != null && x !== false) e.append(x.nodeType ? x : document.createTextNode(x)); });
  return e;
}
var eur = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function money(v) { return eur.format(v); }
function amt(v) { return h("div", { class: "amt" }, h("span", { class: "num" }, money(v)), h("small", null, "EUR")); }
function ago(iso) { var m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 6e4)); if (m < 1) return "just now"; if (m < 60) return m + " min ago"; var hh = Math.floor(m / 60); return hh < 24 ? hh + " h ago" : Math.floor(hh / 24) + " d ago"; }
function left(iso) { var ms = Date.parse(iso) - Date.now(); if (ms <= 0) return "expired"; var m = Math.floor(ms / 6e4), d = Math.floor(m / 1440), hh = Math.floor(m % 1440 / 60); if (d) return "expires in " + d + "d " + hh + "h"; if (hh) return "expires in " + hh + "h " + (m % 60) + "m"; return "expires in " + m + "m"; }
function dday(s) { return new Date(s + "T00:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }); }
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; }

var S = { orders: [], sel: null, mock: false, loaded: false, online: true, busy: {}, name: store("aimo.approver") || "", approvers: [], sig: "", dsig: "" };
var mockPool = MOCK.slice();
var wide = window.matchMedia("(min-width:900px)");
var ADMIN = { "X-Admin-Key": store("aimo.adminKey") || "demo-admin" };

function api(path, opts) {
  if (!window.aimo) return Promise.resolve(null);
  opts = opts || {}; opts.headers = Object.assign({}, ADMIN, opts.headers);
  return window.aimo.api(path, opts);
}
function visible() { var c = window.aimo && window.aimo.customer(); return S.orders.filter(function (o) { return !c || o.customer === c; }); }
function cur() { var v = visible(); return v.filter(function (o) { return o.order_id === S.sel; })[0] || (wide.matches ? v[0] : null) || null; }

function loadMandate() {
  if (S.mock) { S.approvers = ["Anna Virtanen", "Mikko Laine"]; return; }
  api("/v1/mandate").then(function (m) { S.approvers = (m && m.approvers) || []; });
}

function poll() {
  var p = S.mock ? Promise.resolve(mockPool.slice()) : api("/v1/approvals");
  p.then(function (d) {
    if (!S.loaded && !Array.isArray(d)) { S.mock = true; S.approvers = ["Anna Virtanen", "Mikko Laine"]; if (window.aimo) window.aimo.mock(true); d = mockPool.slice(); }
    if (Array.isArray(d)) { S.online = true; S.orders = d.filter(function (o) { return !S.busy[o.order_id]; }); }
    else S.online = false;
    S.loaded = true; render();
  });
}

function reasonIcon(r) { return icon(/allerg/i.test(r.code) ? "allergen" : "warn"); }
function reasons(o) { return h("ul", { class: "why" }, ((o.decision && o.decision.reasons) || []).map(function (r) { return h("li", null, reasonIcon(r), h("span", null, r.text)); })); }
function subs(o, full) {
  if (!o.substitutions || !o.substitutions.length) return null;
  return h("ul", { class: "subs" }, o.substitutions.map(function (s) {
    return h("li", null, h("span", { class: "mono" }, s.original_id), icon("arrow"), h("span", { class: "mono" }, s.substitute_id),
      s.reason ? h("span", { class: "sr" }, s.reason) : null);
  }));
}

function card(o, isCur) {
  var b = h("button", { class: "card", type: "button", "aria-current": isCur ? "true" : false, onclick: function () { S.sel = o.order_id; location.hash = encodeURIComponent(o.order_id); } },
    h("div", { class: "head" }, h("div", { class: "who" }, h("b", null, o.customer), h("span", null, o.agent_id)), amt(o.total_net)),
    reasons(o), subs(o),
    h("div", { class: "meta" },
      o.delivery_date ? h("span", null, "Delivery ", h("span", { class: "num" }, dday(o.delivery_date))) : null,
      h("span", null, h("span", { class: "num" }, o.lines.length), o.lines.length === 1 ? " line" : " lines"),
      h("span", null, ago(o.created_at))));
  return h("li", { class: "ci" }, b, h("a", { class: "aud", href: "/audit.html?order_id=" + encodeURIComponent(o.order_id) }, "Audit trail"));
}

function lastLine() {
  var l; try { l = JSON.parse(store("aimo.inbox.last") || "null"); } catch (e) {}
  return l ? h("div", { class: "last" }, (l.approve ? "Approved " : "Rejected ") + l.customer + " " + money(l.total) + " EUR as " + l.by + ", " + ago(l.at)) : null;
}

function renderList() {
  var v = visible(), c = cur(), ul = $("#cards");
  var sig = JSON.stringify([v, c && c.order_id, S.online, S.mock]);
  $("#count").textContent = v.length;
  $("#offTag").hidden = S.online;
  $("#who").textContent = S.name ? "Approving as " + S.name + " (change)" : "Set approver name";
  document.title = (v.length ? "(" + v.length + ") " : "") + "Approvals - Aimo Trade Gateway";
  if (sig === S.sig) return; S.sig = sig;
  ul.replaceChildren();
  if (!v.length) { ul.append(h("li", { class: "empty" }, h("h2", null, "No orders waiting for you"), h("p", null, "New orders that need sign-off appear here."), lastLine())); return; }
  v.forEach(function (o) { ul.append(card(o, wide.matches && c && c.order_id === o.order_id)); });
}

function field(label, val) { return [h("dt", null, label), h("dd", null, val)]; }

function renderDetail() {
  var o = cur(), p = $("#detailPane");
  var key = JSON.stringify([o, wide.matches, S.name]);
  document.body.dataset.view = S.sel && o ? "detail" : "list";
  if (key === S.dsig) { tick(); return; } S.dsig = key;
  p.replaceChildren();
  if (!o) { if (wide.matches) p.append(h("div", { class: "empty full" }, h("h2", null, "No orders waiting for you"), h("p", null, "Pick an order from the list when one arrives."), lastLine())); return; }
  var rejecting = false, note = h("textarea", { id: "note", rows: "2", placeholder: "Tell the buyer why" });
  var noteBox = h("div", { hidden: true }, h("label", { for: "note" }, "Reason for rejecting (required)"), note);
  var app = h("button", { class: "act primary", type: "button", onclick: function () { decide(o, true, ""); } }, "Approve ", h("span", { class: "num" }, money(o.total_net) + " EUR"));
  var rej = h("button", { class: "act quiet", type: "button" }, "Reject");
  function paint() {
    noteBox.hidden = !rejecting;
    app.hidden = rejecting;
    rej.className = rejecting ? "act danger" : "act quiet";
    rej.textContent = rejecting ? "Reject order" : "Reject";
    rej.disabled = rejecting && !note.value.trim();
    back.hidden = !rejecting;
  }
  var back = h("button", { class: "act quiet", type: "button", onclick: function () { rejecting = false; paint(); } }, "Back");
  rej.addEventListener("click", function () { if (!rejecting) { rejecting = true; paint(); note.focus(); } else decide(o, false, note.value.trim()); });
  note.addEventListener("input", paint);
  paint();
  var rows = o.lines.map(function (l) { return h("tr", null, h("td", null, l.product_id), h("td", null, l.quantity + " " + l.sales_unit), h("td", null, money(l.unit_price_net)), h("td", null, money(l.line_total_net))); });
  p.append(
    h("div", { class: "dhead" }, h("button", { class: "back", type: "button", onclick: function () { history.replaceState(null, "", location.pathname + location.search); S.sel = null; render(); window.scrollTo(0, 0); } }, icon("back"), "Approvals")),
    h("div", { class: "dbody" },
      h("div", { class: "dtitle" }, h("div", null, h("h2", null, o.customer), h("span", { class: "mono" }, o.agent_id + " / " + o.order_id)), amt(o.total_net)),
      h("section", { class: "pn reasons" }, h("h3", null, "Why this needs you"), h("div", { class: "in" }, reasons(o), o.substitutions && o.substitutions.length ? h("div", { style: "margin-top:12px" }, subs(o)) : null)),
      h("section", { class: "pn" }, h("h3", null, "Lines"),
        h("div", { class: "scroll" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Product"), h("th", null, "Qty"), h("th", null, "Unit"), h("th", null, "Total"))),
          h("tbody", null, rows),
          h("tfoot", null, h("tr", null, h("td", { colspan: "3" }, "Total net"), h("td", null, money(o.total_net))))))),
      h("section", { class: "pn" }, h("h3", null, "Quote"), h("div", { class: "in" }, h("dl", { class: "kv" },
        field("Quote", [h("span", { class: "mono" }, o.quote_id), h("span", { class: "tag ok" }, icon("verified"), "Signature verified")]),
        field("Audit", h("a", { href: "/audit.html?order_id=" + encodeURIComponent(o.order_id) }, "Audit trail")),
        field("Created", ago(o.created_at)),
        o.valid_until ? field("Valid until", h("span", { class: "tag warn", "data-until": o.valid_until }, left(o.valid_until))) : null,
        o.delivery_date ? field("Delivery", dday(o.delivery_date)) : null)))),
    h("div", { class: "actions" }, noteBox, h("div", { class: "btns" }, back, rej, app)));
  tick();
}
function tick() { document.querySelectorAll("[data-until]").forEach(function (e) { e.textContent = left(e.dataset.until); }); }

function render() { renderList(); renderDetail(); }

var toastT;
function toast(msg, err) {
  var t = $("#toast"); clearTimeout(toastT); t.className = "toast" + (err ? " err" : ""); t.replaceChildren(h("span", null, msg), h("button", { type: "button", onclick: function () { t.hidden = true; } }, "Dismiss")); t.hidden = false;
  if (!err) toastT = setTimeout(function () { t.hidden = true; }, 5000);
}

function ask() {
  return new Promise(function (res) {
    var d = $("#whoDlg"), f = $("#whoField"), inp;
    if (S.approvers.length) { inp = h("select", { "aria-label": "Approver" }, S.approvers.map(function (a) { return h("option", { value: a }, a); })); inp.value = S.approvers.indexOf(S.name) >= 0 ? S.name : S.approvers[0]; }
    else inp = h("input", { type: "text", required: true, placeholder: "Your name", "aria-label": "Approver name", autocomplete: "name", value: S.name });
    f.replaceChildren(inp);
    d.addEventListener("close", function () { if (d.returnValue === "ok" && inp.value.trim()) { S.name = inp.value.trim(); store("aimo.approver", S.name); S.dsig = ""; render(); res(S.name); } else res(""); }, { once: true });
    d.returnValue = ""; d.showModal();
  });
}

async function decide(o, approve, note) {
  var by = S.name || await ask(); if (!by) return;
  var idx = S.orders.indexOf(o);
  S.busy[o.order_id] = 1; S.orders = S.orders.filter(function (x) { return x !== o; });
  if (S.sel === o.order_id) { S.sel = null; history.replaceState(null, "", location.pathname + location.search); }
  S.sig = S.dsig = ""; render(); window.scrollTo(0, 0);
  var ok;
  if (S.mock) { await new Promise(function (r) { setTimeout(r, 250); }); mockPool = mockPool.filter(function (x) { return x.order_id !== o.order_id; }); ok = true; }
  else ok = await api("/v1/approvals/" + encodeURIComponent(o.order_id), { method: "POST", body: { approve: approve, by: by, note: note } });
  delete S.busy[o.order_id];
  if (ok) {
    store("aimo.inbox.last", JSON.stringify({ approve: approve, customer: o.customer, total: o.total_net, by: by, at: new Date().toISOString() }));
    toast((approve ? "Approved " : "Rejected ") + o.customer + " " + money(o.total_net) + " EUR as " + by); render();
  } else {
    S.orders.splice(Math.max(0, idx), 0, o); S.sel = o.order_id; location.hash = encodeURIComponent(o.order_id);
    S.sig = S.dsig = ""; render();
    var e = window.aimo && window.aimo.lastError, d = e && e.detail;
    if (d && typeof d !== "string") d = d.message || d.error || JSON.stringify(d);
    toast(!e || e.status === 0 ? "Gateway offline. The order is still waiting." : "Could not " + (approve ? "approve" : "reject") + ": " + (d || "error " + e.status) + ". The order is still waiting.", true);
  }
}

function route() { S.sel = decodeURIComponent(location.hash.slice(1)) || null; render(); }
window.addEventListener("hashchange", route);
window.addEventListener("customer", function () { S.sig = S.dsig = ""; loadMandate(); render(); });
wide.addEventListener("change", function () { S.sig = S.dsig = ""; render(); });
$("#who").addEventListener("click", ask);
if (/[?&]mock=1/.test(location.search)) { S.mock = true; S.approvers = ["Anna Virtanen", "Mikko Laine"]; if (window.aimo) window.aimo.mock(true); }
S.sel = decodeURIComponent(location.hash.slice(1)) || null;
loadMandate(); poll(); setInterval(poll, 3000); setInterval(tick, 20000);
window.__inbox = S;
})();
