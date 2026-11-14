(function () {
var now = Date.now();
function iso(ms) { return new Date(now + ms).toISOString(); }
function day(n) { return new Date(now + n * 864e5).toISOString().slice(0, 10); }
var MOCK = [
  { order_id: "ord_7f3a91", quote_id: "q_20261114_0042", customer: "hotel-aurora", agent_id: "buyer-aurora", status: "pending_approval",
    lines: [
      { product_id: "P-100234", name: "Valio Eila laktoositon kevytmaitojuoma 1 l", quantity: 1000, sales_unit: "L", unit_price_net: 1.42, line_total_net: 1420 },
      { product_id: "P-100871", name: "Valio Oltermanni kermajuusto", quantity: 40, sales_unit: "KG", unit_price_net: 14.5, line_total_net: 580 },
      { product_id: "P-204410", name: "Valio voi suolaamaton", quantity: 12, sales_unit: "CS", unit_price_net: 54.17, line_total_net: 650.04 } ],
    total_net: 2650.04, substitutions: [],
    decision: { outcome: "needs_approval", reasons: [{ code: "OVER_ORDER_LIMIT", text: "Order total 2,650.04 EUR is over the 2,500.00 EUR per-order limit" }] },
    created_at: iso(-12 * 6e4), updated_at: iso(-12 * 6e4), delivery_date: day(3), valid_until: iso(3.2 * 36e5) },
  { order_id: "ord_2b8c10", quote_id: "q_20261114_0043", customer: "school-hervanta", agent_id: "buyer-hervanta", status: "pending_approval",
    lines: [
      { product_id: "P-300118", name: "Voipavut 500g", quantity: 60, sales_unit: "KG", unit_price_net: 6.2, line_total_net: 372, substitute_for: "P-300077" },
      { product_id: "P-300552", name: "Valio ruokakerma 15%", quantity: 25, sales_unit: "KG", unit_price_net: 3.9, line_total_net: 97.5 } ],
    total_net: 469.5,
    substitutions: [{ original_id: "P-300077", original_name: "Valio Voipavut 500g", substitute_id: "P-300118", reason: "Original out of stock for the requested date; same brand family, same pack size" }],
    decision: { outcome: "needs_approval", reasons: [{ code: "SUBSTITUTION_NEEDS_APPROVAL", text: "Substituting P-300077 with P-300118 needs human approval (Original out of stock for the requested date; same brand family, same pack size)" }, { code: "NON_PREFERRED_BRAND", text: "Voipavut 500g (P-300118) is brand Pride, preferred: Valio" }] },
    created_at: iso(-47 * 6e4), updated_at: iso(-47 * 6e4), delivery_date: day(2), valid_until: iso(0.6 * 36e5) },
  { order_id: "ord_91d4e7", quote_id: "q_20261114_0044", customer: "hospital-tays", agent_id: "buyer-tays", status: "pending_approval",
    lines: [
      { product_id: "P-410902", name: "Kaurahiutale gluteeniton", quantity: 200, sales_unit: "KG", unit_price_net: 4.15, line_total_net: 830, substitute_for: "P-410455" } ],
    total_net: 830,
    substitutions: [{ original_id: "P-410455", original_name: "Valio kaurahiutale", substitute_id: "P-410902", reason: "Gluten-free requirement kept, price within 5 percent" }],
    decision: { outcome: "needs_approval", reasons: [
      { code: "UNKNOWN_PRODUCT", text: "Could not check allergens for P-410902: product data missing" },
      { code: "SUBSTITUTION_NEEDS_APPROVAL", text: "Substituting P-410455 with P-410902 needs human approval (Gluten-free requirement kept, price within 5 percent)" } ] },
    created_at: iso(-3 * 6e4), updated_at: iso(-3 * 6e4), delivery_date: day(1), valid_until: iso(5.5 * 36e5) }
];

var APPR = { "hotel-aurora": ["Laura Virtanen", "Mikko Heikkinen"], "hospital-tays": ["Helena Korhonen", "Antti Lahtinen"], "school-hervanta": ["Sari Nieminen"] };
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
function ago(ts) { var m = Math.max(0, Math.round((Date.now() - Date.parse(ts)) / 6e4)); if (m < 1) return "just now"; if (m < 60) return m + " min ago"; var hh = Math.floor(m / 60); return hh < 24 ? hh + " h ago" : Math.floor(hh / 24) + " d ago"; }
function left(ts) { var ms = Date.parse(ts) - Date.now(); if (ms <= 0) return "expired"; var m = Math.floor(ms / 6e4), d = Math.floor(m / 1440), hh = Math.floor(m % 1440 / 60); if (d) return "expires in " + d + "d " + hh + "h"; if (hh) return "expires in " + hh + "h " + (m % 60) + "m"; return "expires in " + m + "m"; }
function dday(s) { return new Date(s + "T00:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }); }
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; }

var S = { orders: [], sel: null, mock: false, loaded: false, online: true, busy: {}, name: store("aimo.approver") || "", sig: "", dsig: "" };
var mockPool = MOCK.slice();
var wide = window.matchMedia("(min-width:900px)");
var ADMIN = { "X-Admin-Key": store("aimo.adminKey") || "demo-admin" };

function targetId() {
  var hash = decodeURIComponent((location.hash || "").slice(1)).trim();
  if (hash) return hash;
  var q = new URLSearchParams(location.search || "").get("order_id");
  return q ? q.trim() : null;
}
function cleanSearch() {
  var s = location.search || "";
  if (!/[?&]order_id=/.test(s)) return s;
  var q = s.slice(1).split("&").filter(function (p) { return p && p.split("=")[0] !== "order_id"; });
  return q.length ? "?" + q.join("&") : "";
}
function setUrlOrder(id) {
  var base = location.pathname + cleanSearch();
  history.replaceState(null, "", id ? base + "#" + encodeURIComponent(id) : base);
}

var syncingCustomer = false;
function syncSelCustomer() {
  if (!S.sel || !window.aimo) return false;
  var hit = S.orders.filter(function (o) { return o.order_id === S.sel; })[0];
  if (!hit || !hit.customer) return false;
  if (window.aimo.CUSTOMERS && window.aimo.CUSTOMERS.indexOf(hit.customer) < 0) return false;
  var sel = $("#customer");
  if (window.aimo.customer() === hit.customer) {
    if (sel && sel.value !== hit.customer) sel.value = hit.customer;
    return false;
  }
  syncingCustomer = true;
  window.aimo.setCustomer(hit.customer);
  syncingCustomer = false;
  if (sel) sel.value = hit.customer;
  return true;
}

function api(path, opts) {
  if (!window.aimo) return Promise.resolve(null);
  opts = opts || {}; opts.headers = Object.assign({}, ADMIN, opts.headers);
  return window.aimo.api(path, opts);
}
function visible() {
  var c = window.aimo && window.aimo.customer();
  return S.orders.filter(function (o) { return !c || o.customer === c || o.order_id === S.sel; });
}
function cur() {
  var v = visible();
  return v.filter(function (o) { return o.order_id === S.sel; })[0] || (wide.matches ? v[0] : null) || null;
}

var apprCache = {};
function approversFor(c) {
  if (!c) return Promise.resolve([]);
  if (S.mock) return Promise.resolve(APPR[c] || []);
  if (apprCache[c]) return Promise.resolve(apprCache[c]);
  return api("/v1/mandate?customer=" + encodeURIComponent(c)).then(function (m) {
    var a = (m && m.approvers) || [];
    if (a.length) apprCache[c] = a;
    return a;
  });
}

var pollSeq = 0;
function poll() {
  var seq = ++pollSeq;
  var p = S.mock ? Promise.resolve(mockPool.slice()) : api("/v1/approvals");
  return p.then(function (d) {
    if (seq !== pollSeq) return;
    if (!S.loaded && !Array.isArray(d)) { S.mock = true; if (window.aimo) window.aimo.mock(true); d = mockPool.slice(); }
    if (Array.isArray(d)) { S.online = true; S.orders = d.filter(function (o) { return !S.busy[o.order_id]; }); }
    else S.online = false;
    var switched = syncSelCustomer();
    S.loaded = true;
    render();
    if (switched || !(dTick++ % 4)) loadDecided();
  });
}

function level(r) { return r.level || (r.code === "NON_PREFERRED_BRAND" ? "info" : "approval"); }
function reasonIcon(r) { return icon(/allerg|unknown/i.test(r.code) ? "allergen" : "warn"); }
function reasons(o) {
  var all = (o.decision && o.decision.reasons) || [], hot = all.filter(function (r) { return level(r) !== "info"; }), cool = all.filter(function (r) { return level(r) === "info"; });
  return [hot.length ? h("ul", { class: "why" }, hot.map(function (r) { return h("li", null, reasonIcon(r), h("span", null, r.text)); })) : null,
    cool.length ? h("ul", { class: "note" }, cool.map(function (r) { return h("li", null, r.text); })) : null];
}

function findLineName(o, id) {
  var l = (o.lines || []).filter(function (x) { return x.product_id === id && x.name; })[0];
  return l ? l.name : "";
}
function origNameOf(o, s) {
  var n = (s && s.original_name) || findLineName(o, s && s.original_id);
  if (!n && s && s.reason) {
    var m = /^Substitute for (.+?): /.exec(s.reason);
    if (m && m[1] !== s.original_id) n = m[1];
  }
  return n || "";
}
function subNameOf(o, s) {
  var n = (s && s.substitute_name) || findLineName(o, s && s.substitute_id);
  if (!n && s && s.reason) {
    var m = /^Substitute for .+?: (.+?)\.?$/.exec(s.reason);
    if (m && m[1] !== s.substitute_id) n = m[1];
  }
  return n || "";
}
function subs(o) {
  if (!o.substitutions || !o.substitutions.length) return null;
  return h("ul", { class: "subs" }, o.substitutions.map(function (s) {
    var on = origNameOf(o, s), sn = subNameOf(o, s);
    return h("li", null,
      h("span", { class: "mono" }, s.original_id),
      on ? h("span", null, " (" + on + ")") : null,
      " ", icon("arrow"), " ",
      h("span", { class: "mono" }, s.substitute_id),
      sn ? h("span", null, " " + sn) : null,
      s.reason ? h("span", { class: "sr" }, s.reason) : null);
  }));
}

function card(o, isCur) {
  var b = h("button", { class: "card", type: "button", "aria-current": isCur ? "true" : false, onclick: function () { S.sel = o.order_id; setUrlOrder(o.order_id); render(); } },
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

var decided = [], mockDecided = [], dTick = 0, decSeq = 0;
function loadDecided() {
  var c = window.aimo && window.aimo.customer();
  if (S.mock) { decided = mockDecided.filter(function (e) { return !c || e.customer === c; }); renderDecided(); return Promise.resolve(decided); }
  var seq = ++decSeq;
  return api("/v1/audit?customer=" + encodeURIComponent(c || "")).then(function (d) {
    if (seq !== decSeq) return decided;
    var ev = d && (d.events || d);
    if (!Array.isArray(ev)) return decided;
    decided = ev.filter(function (e) {
      var age = Date.now() - Date.parse(e.ts);
      return /^approval\.(approved|rejected)$/.test(e.action) && /^human:/.test(e.actor) && isFinite(age) && age < 864e5;
    }).reverse().slice(0, 8).map(function (e) {
      return { customer: e.customer, order_id: e.subject, text: (e.why || "").split(" Held for approval")[0], by: e.actor.slice(6), at: e.ts, approve: e.action === "approval.approved" };
    });
    renderDecided();
    return decided;
  });
}
function renderDecided() {
  var box = $("#decided"), ul = $("#dlist");
  box.hidden = !decided.length;
  if (S.sel && decided.some(function (e) { return e.order_id === S.sel; })) box.open = true;
  $("#dcount").textContent = decided.length;
  ul.replaceChildren.apply(ul, decided.map(function (e) {
    return h("li", null, h("span", { class: "tag " + (e.approve ? "ok" : "stop") }, e.approve ? "Approved" : "Rejected"),
      h("span", { class: "dt" }, e.text), h("span", { class: "da" }, ago(e.at) + " / ", h("a", { href: "/audit.html?order_id=" + encodeURIComponent(e.order_id) }, "Audit trail")));
  }));
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

function lineCell(o, l) {
  var sub = l.substitute_for ? ((o.substitutions || []).filter(function (s) { return s.original_id === l.substitute_for && s.substitute_id === l.product_id; })[0] || { original_id: l.substitute_for }) : null;
  var origN = sub ? origNameOf(o, sub) : "";
  var parts = l.name ? [h("span", { class: "pname" }, l.name), h("span", { class: "pid" }, l.product_id)] : [h("span", { class: l.substitute_for ? "pid" : "mono" }, l.product_id)];
  if (l.substitute_for) {
    parts.push(h("span", { class: "tag warn" }, "Substitute for " + l.substitute_for));
    if (origN) parts.push(h("span", { class: "pid" }, "Replaces " + origN));
  }
  return parts;
}

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
  var rows = o.lines.map(function (l) {
    var qty = l.sales_unit ? l.quantity + " " + l.sales_unit : String(l.quantity);
    return h("tr", null, h("td", null, lineCell(o, l)), h("td", null, qty), h("td", null, money(l.unit_price_net)), h("td", null, money(l.line_total_net)));
  });
  var rem = o.valid_until ? left(o.valid_until) : "";
  p.append(
    h("div", { class: "dhead" }, h("button", { class: "back", type: "button", onclick: function () { S.sel = null; setUrlOrder(null); render(); window.scrollTo(0, 0); } }, icon("back"), "Approvals")),
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
        o.valid_until ? field("Valid until", h("span", { class: "tag " + (rem === "expired" ? "stop" : "warn"), "data-until": o.valid_until }, rem)) : null,
        o.delivery_date ? field("Delivery", dday(o.delivery_date)) : null)))),
    h("div", { class: "actions" }, noteBox, h("div", { class: "btns" }, back, rej, app)));
  tick();
}
function tick() {
  document.querySelectorAll("[data-until]").forEach(function (e) {
    var rem = left(e.dataset.until);
    e.textContent = rem;
    e.className = "tag " + (rem === "expired" ? "stop" : "warn");
  });
}

function render() { renderList(); renderDetail(); }

var toastT;
function toast(msg, err) {
  var t = $("#toast"); clearTimeout(toastT); t.className = "toast" + (err ? " err" : ""); t.replaceChildren(h("span", null, msg), h("button", { type: "button", onclick: function () { t.hidden = true; } }, "Dismiss")); t.hidden = false;
  if (!err) toastT = setTimeout(function () { t.hidden = true; }, 5000);
}

function ask(list) {
  return new Promise(function (res) {
    var d = $("#whoDlg"), f = $("#whoField"), inp;
    if (list.length) { inp = h("select", { "aria-label": "Approver" }, list.map(function (a) { return h("option", { value: a }, a); })); inp.value = list.indexOf(S.name) >= 0 ? S.name : list[0]; }
    else inp = h("input", { type: "text", required: true, placeholder: "Your name", "aria-label": "Approver name", autocomplete: "name", value: S.name });
    f.replaceChildren(inp);
    d.addEventListener("close", function () {
      if (d.returnValue === "ok" && inp.value.trim()) {
        S.name = inp.value.trim();
        store("aimo.approver", S.name);
        S.dsig = "";
        render();
        res(S.name);
      } else res("");
    }, { once: true });
    d.returnValue = ""; d.showModal();
  });
}

async function decide(o, approve, note) {
  var list = await approversFor(o.customer);
  var by = S.name && (!list.length || list.indexOf(S.name) >= 0) ? S.name : await ask(list); if (!by) return;
  var idx = S.orders.findIndex(function (x) { return x.order_id === o.order_id; });
  S.busy[o.order_id] = 1; S.orders = S.orders.filter(function (x) { return x.order_id !== o.order_id; });
  if (S.sel === o.order_id) { S.sel = null; setUrlOrder(null); }
  S.sig = S.dsig = ""; render(); window.scrollTo(0, 0);
  var ok;
  if (S.mock) { await new Promise(function (r) { setTimeout(r, 250); }); mockPool = mockPool.filter(function (x) { return x.order_id !== o.order_id; }); ok = true; }
  else ok = await api("/v1/approvals/" + encodeURIComponent(o.order_id), { method: "POST", body: { approve: approve, by: by, note: note } });
  delete S.busy[o.order_id];
  if (ok) {
    var nowIso = new Date().toISOString();
    var entry = { customer: o.customer, order_id: o.order_id, by: by, at: nowIso, approve: approve, text: by + (approve ? " approved the order of " : " rejected the order of ") + money(o.total_net) + " EUR." + (note ? " Note: " + note + "." : "") };
    store("aimo.inbox.last", JSON.stringify({ approve: approve, customer: o.customer, total: o.total_net, by: by, at: nowIso }));
    if (S.mock) {
      mockDecided.unshift(entry);
      loadDecided();
      poll();
    } else {
      var curCust = window.aimo && window.aimo.customer();
      if (!curCust || curCust === o.customer) {
        decided = [entry].concat(decided.filter(function (x) { return x.order_id !== o.order_id; })).slice(0, 8);
        renderDecided();
      }
      poll();
      loadDecided();
    }
    toast((approve ? "Approved " : "Rejected ") + o.customer + " " + money(o.total_net) + " EUR as " + by);
    render();
  } else {
    var e = window.aimo && window.aimo.lastError, d = e && e.detail;
    var code = (d && typeof d === "object" && !Array.isArray(d) && d.code) || "";
    var raw = typeof d === "string" ? d : d && (Array.isArray(d) ? d[0] && d[0].msg : d.text || d.message || d.error || JSON.stringify(d));
    var msg = (raw || "").replace(/\.+$/, "");
    var gone = code === "QUOTE_EXPIRED" || code === "NOT_PENDING" || code === "NOT_FOUND" || (e && (e.status === 404 || e.status === 409));
    if (code === "NOT_APPROVER" || (e && e.status === 403)) {
      delete apprCache[o.customer];
      if (code === "NOT_APPROVER") { S.name = ""; store("aimo.approver", ""); }
    }
    if (gone) {
      S.sig = S.dsig = "";
      render();
      poll();
      loadDecided();
      toast("Could not " + (approve ? "approve" : "reject") + ": " + (msg || "the order is no longer waiting") + ".", true);
    } else {
      if (!S.orders.some(function (x) { return x.order_id === o.order_id; })) S.orders.splice(Math.max(0, idx), 0, o);
      S.sel = o.order_id;
      setUrlOrder(o.order_id);
      S.sig = S.dsig = "";
      render();
      if (e && e.status) poll();
      toast(!e || e.status === 0 ? "Gateway offline. The order is still waiting." : "Could not " + (approve ? "approve" : "reject") + ": " + (msg || "error " + e.status) + ". The order is still waiting.", true);
    }
  }
}

function route() {
  S.sel = targetId();
  var switched = syncSelCustomer();
  S.sig = S.dsig = "";
  render();
  if (switched) loadDecided();
}
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
window.addEventListener("customer", function () {
  if (syncingCustomer) return;
  var c = window.aimo && window.aimo.customer();
  var hit = S.sel && S.orders.filter(function (o) { return o.order_id === S.sel; })[0];
  if (hit && c && hit.customer !== c) {
    S.sel = null;
    setUrlOrder(null);
  }
  S.sig = S.dsig = "";
  render();
  loadDecided();
});
wide.addEventListener("change", function () { S.sig = S.dsig = ""; render(); });
$("#who").addEventListener("click", function () {
  var o = cur();
  approversFor((o && o.customer) || (window.aimo && window.aimo.customer()) || "").then(ask);
});
if ((window.aimo && window.aimo.mockMode()) || /[?&]mock(=1|&|$)/.test(location.search) || location.search.indexOf("mock=1") >= 0) {
  S.mock = true;
  if (window.aimo) window.aimo.mock(true);
}
S.sel = targetId();
poll(); setInterval(poll, 3000); setInterval(tick, 20000);
S.poll = poll; S.decide = decide; S.loadDecided = loadDecided;
window.__inbox = S;
})();
