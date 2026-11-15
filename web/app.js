// Trusted Trade console. Vanilla ES modules, no build step.
// ?mock=1 plays the demo with no backend. ?api=http://host:port points at a gateway on another origin.
import {
  h, mount, eur, num, pct, time, hm, day, dayTime, full, countdown, shortHash, groupedHash, relTime,
  claimLabel, roleLabel, flagInfo, flagEl, TIER_LABEL, TIER_HINT, VERDICT, actorKind, pretty, download, verifyJws,
} from "./util.js";
import { createLiveApi } from "./api.js";
import { renderOps, loadOps, blockedEvents } from "./ops.js";
import { renderRecipes, loadRecipes } from "./recipes.js";

const params = new URLSearchParams(location.search);
const MOCK = params.get("mock") === "1";
// Presenter mode (&present=1): ~15% larger type, dev-only JSON expanders hidden.
if (params.get("present") === "1") document.documentElement.classList.add("present");
// In presenter mode, press "j" to show or hide the JSON expanders.
document.addEventListener("keydown", (e) => { if (e.key === "j" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "")) document.documentElement.classList.toggle("show-json"); });
const $ = (id) => document.getElementById(id);

const MODELS = [
  { id: "gemini-3.8-flash", label: "Gemini 3.8 Flash", hint: "agent default" },
  { id: "gemini-4-hydrogen", label: "Gemini 4 Hydrogen", hint: "reasoning" },
  { id: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash-Lite", hint: "fast" },
];
const THINKING = ["minimal", "low", "medium", "high"];
const DEFAULT_REQ = {
  "hotel-aurora": "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
  "school-tampere": "We need 120 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies.",
  "hospital-meilahti": "We need 40 kg of gluten-free bread for the Meilahti wards by Wednesday.",
};
const TIER_ORDER = ["preferred", "best", "better", "good"];

let api;
const S = {
  customers: [], customersState: "loading", customersError: null,
  customerId: "hotel-aurora", model: MODELS[0].id, thinking: "medium", text: DEFAULT_REQ["hotel-aurora"], textEdited: false,
  run: null,                   // {id, status: running|waiting|done|error, steps[], need, plan, quote, decision, order, error}
  tab: "plan",
  approvals: { state: "idle", items: [], error: null },
  decided: new Map(),          // order_id -> {order, quote, decision}
  selected: null,              // order_id
  deciding: null, decideError: null,
  approver: "", note: "",
  sigs: new Map(),             // quote_hash -> {state, detail} | "pending"
  jwks: null,
  audit: { state: "idle", events: [], error: null, scope: "flow", verify: null, verifying: false },
  image: null,                 // {mime, data_b64, name, preview} photo intake
  optimize: "agent",           // agent | cheapest | greenest | reliable
  answers: {},                 // step seq -> answer label (needs_clarification)
  learned: {},                 // customer_id -> {state, items[]}
  remember: false, rememberScope: "product",
  requote: null,               // {order_id, old, fresh, detail} after 409 requote_needed
  ledger: { events: [], at: 0 }, // whole-ledger cache for the blocked-attacks feed
  voice: null,                 // {order_id, phase, text, tts, transcript, intent, error} voice approval
  voiceStatus: null,           // GET /v1/voice/status
};
const OPTIMIZE = [
  { id: "agent", label: "Agent's ranking" },
  { id: "cheapest", label: "Cheapest" },
  { id: "greenest", label: "Greenest" },
  { id: "reliable", label: "Most reliable" },
];
let closeStream = null;

// ================================================================== boot
async function boot() {
  if (MOCK) {
    const { createMockApi } = await import("./mock.js");
    api = await createMockApi();
  } else {
    api = createLiveApi(params.get("api") || "");
  }
  const pill = $("mode-pill");
  pill.textContent = MOCK ? "MOCK DATA · no backend" : `LIVE · ${api.base.replace(/^https?:\/\//, "")}`;
  pill.className = `mode-pill ${MOCK ? "mock" : "live"}`;
  if (!MOCK) {
    fetch(`${api.base}/healthz`).then((r) => r.json()).then((h) => {
      const rp = h && h.replay;
      if (!rp || !rp.forced) return;
      pill.textContent = `REPLAY · served_from: replay · recorded ${String(rp.time_now || "").slice(0, 16).replace("T", " ")}`;
      pill.className = "mode-pill mock";
      pill.title = `No AIMO_API_KEY: price and availability come from the committed snapshot (${(rp.dates || []).join(", ")}). Simulated clock: ${h.clock && h.clock.now}`;
    }).catch(() => {});
  }
  for (const k of ["docs", "otap", "jwks", "mcp"]) $(`lnk-${k}`).href = api.links[k];
  $("btn-connect").addEventListener("click", openConnect);
  api.setSignInHandler?.(openSignIn);
  renderApproverChip();
  api.voiceStatus?.().then((v) => { S.voiceStatus = v; }).catch(() => {});
  $("blocked-pill").addEventListener("click", () => setTab("ops"));
  const rc = $("request-card");
  rc.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.items || [])].some((i) => i.kind === "file")) { e.preventDefault(); rc.classList.add("drop"); } });
  rc.addEventListener("dragleave", () => rc.classList.remove("drop"));
  rc.addEventListener("drop", (e) => { e.preventDefault(); rc.classList.remove("drop"); const f = e.dataTransfer?.files?.[0]; if (f) readPhoto(f); });
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
  setTab("plan");
  renderRequest();
  renderTimeline();
  renderPlan();
  renderApprovals();
  renderAudit();
  setInterval(tick, 1000);
  await loadCustomers();
  api.demoAvailable?.().then((ok) => { S.demoTamper = !!ok; renderAudit(); }).catch(() => {});
  if (params.get("customer") && S.customers.find((c) => c.id === params.get("customer"))) selectCustomer(params.get("customer"));
  refreshApprovals();
  loadLearned(S.customerId);
  refreshLedger();
  setInterval(() => refreshLedger(), MOCK ? 4000 : 8000);
  // Demo/recording helpers: ?autorun=1 starts the run, &autoapprove=1 approves when the agent waits, &tab=audit opens a tab.
  if (params.get("autorun") === "1") startRun();
  else if (params.get("tab")) setTab(params.get("tab"));
  if (!MOCK) setInterval(() => { if (S.tab === "approvals" && !S.deciding && !S.voice && !S.requote) refreshApprovals(true); }, 5000);
}

function banner(msg, actions = [], kind = "") {
  const el = $("global-banner");
  if (!msg) { el.hidden = true; return; }
  el.hidden = false;
  el.className = `global-banner ${kind}`;
  mount(el, h("span", { class: "grow" }, msg), actions);
}

async function loadCustomers() {
  S.customersState = "loading";
  renderRequest();
  try {
    let list = await api.customers();
    if (list && !Array.isArray(list)) list = list.customers || Object.values(list);
    S.customers = (list || []).map(normCustomer).filter((c) => c.id);
    S.customersState = "ok";
    if (!S.customers.find((c) => c.id === S.customerId) && S.customers[0]) selectCustomer(S.customers[0].id);
    banner(null);
  } catch (e) {
    S.customersState = "error";
    S.customersError = e.message;
    S.customers = Object.keys(DEFAULT_REQ).map((id) => normCustomer({ customer_id: id }));
    banner(`Gateway not reachable: ${e.message}. Start it with “uv run aimo serve”, or play the demo without a backend.`, [
      h("button", { class: "btn btn-sm", onclick: loadCustomers }, "Retry"),
      h("a", { class: "btn btn-sm btn-primary", href: "?mock=1" }, "Open mock demo"),
    ]);
  }
  renderRequest();
}

function toLines(v) {
  if (!v) return [];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : x.message || x.text || x.summary || JSON.stringify(x)));
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${k.replace(/_/g, " ")}: ${typeof x === "object" ? JSON.stringify(x) : x}`);
  return [String(v)];
}

function normCustomer(c) {
  const id = c.customer_id || c.id;
  const m = c.mandate || {};
  return {
    id,
    name: c.name || c.display_name || id,
    segment: c.segment ? c.segment.charAt(0).toUpperCase() + c.segment.slice(1) : c.type || "",
    agent_id: c.agent_id || m.agent_id || c.agents?.[0]?.agent_id || c.mandates?.[0]?.agent_id || `${id}/buyer-1`,
    approvers: c.approvers || [],
    excluded_allergens: c.excluded_allergens || [],
    policy_id: c.policy_id || m.policy_id || id,
    scopes: m.scopes || c.scopes || null,
    summary: toLines(c.summary || c.policy_summary || c.rules || c.policy),
    default_request: c.default_request || DEFAULT_REQ[id] || "",
  };
}

function selectCustomer(id) {
  S.customerId = id;
  const c = S.customers.find((x) => x.id === id);
  if (!S.textEdited && c) S.text = c.default_request || DEFAULT_REQ[id] || S.text;
  S.approver = "";
  renderRequest();
  loadLearned(id);
}

// Learned swaps ("always allow this swap"). PROPOSED endpoint GET /v1/customers/{id}/learned; 404 hides the panel.
async function loadLearned(id, force) {
  if (!api.learned || (!force && S.learned[id]?.state === "ok")) return;
  try {
    const res = await api.learned(id);
    if (res && !Array.isArray(res) && res.available === false) { S.learned[id] = { state: "absent", items: [] }; renderRequest(); return; }
    const items = Array.isArray(res) ? res : res?.swaps || res?.items || res?.learned || [];
    S.learned[id] = { state: "ok", items };
  } catch (e) {
    S.learned[id] = { state: e.status === 404 ? "absent" : "error", items: [] };
  }
  renderRequest();
}

function learnedPanel(id) {
  const L = S.learned[id];
  if (!L || L.state !== "ok") return null;
  if (!L.items.length && S.run && !S.editing) return null;
  return h("details", { class: "learned", open: !S.run && L.items.length <= 2 },
    h("summary", { class: "section-label" }, `Learned swaps · ${L.items.length}`, L.items.length ? h("span", { class: "more" }, "show ›") : null),
    L.items.length ? h("ul", null, L.items.map((x) => h("li", null,
      h("b", null, x.product_name || x.to_name || x.to_product_name || x.to_product_id || x.product_id || x.product_type || "swap"),
      x.replaces_name || x.from_name || x.from_product_name || x.replaces_product_id || x.from_product_id
        ? [" for ", h("span", null, x.replaces_name || x.from_name || x.from_product_name || x.replaces_product_id || x.from_product_id)] : null,
      h("span", { class: "faint" }, ` · ${x.scope === "product_type" ? "any product of this type" : "this product"}`,
        x.approved_by || x.approver ? ` · ${directoryEntry(x.approved_by || x.approver)?.name || x.approved_by || x.approver}` : "",
        x.approved_at || x.created_at || x.ts ? ` · ${dayTime(x.approved_at || x.created_at || x.ts)}` : "")))) :
      h("div", { class: "faint small" }, "None yet. Tick “Always allow this swap” when approving, and the agent stops asking for it."));
}

// Photo intake: downscale in the browser (max 1600 px JPEG) and send with the run.
function readPhoto(file) {
  if (!file || !/^image\//.test(file.type)) return;
  const fr = new FileReader();
  fr.onload = () => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      const url = c.toDataURL("image/jpeg", 0.85);
      S.image = { mime: "image/jpeg", data_b64: url.split(",")[1], name: file.name || "photo.jpg", preview: url };
      renderRequest();
    };
    img.src = fr.result;
  };
  fr.readAsDataURL(file);
}

// ================================================================== approver sign-in
// Approvals are human-only. A person signs in once with their name and the approver PIN; the gateway returns an
// 8-hour session bound to that one person (POST /v1/auth/approver-session). Agents never get the PIN, and any request
// with agent identity on the decision route is refused (403), so this panel is the only way to approve.
const svgIcon = (d) => h("span", { class: "ico", "aria-hidden": "true",
  html: `<svg viewBox="0 0 16 16" width="14" height="14"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>` });
const lockIcon = () => svgIcon("M4.5 7V5a3.5 3.5 0 0 1 7 0v2M3.5 7h9v7h-9z");
const micIcon = () => svgIcon("M8 1.5a2.5 2.5 0 0 0-2.5 2.5v4a2.5 2.5 0 0 0 5 0V4A2.5 2.5 0 0 0 8 1.5zM3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5");

/** Directory entry {id?, name, role, customer} for an approver id or name, from /v1/customers. */
function directoryEntry(who) {
  if (!who) return null;
  const w = String(who).toLowerCase();
  for (const c of S.customers) for (const a of c.approvers || []) {
    if ((a.id && a.id.toLowerCase() === w) || (a.name && a.name.toLowerCase() === w)) return { ...a, customer: c };
  }
  return null;
}

function renderApproverChip() {
  const el = $("approver-chip");
  if (!el || !api?.signedInApprover) return;
  const me = api.signedInApprover();
  const e = directoryEntry(me);
  mount(el, me
    ? h("div", { class: "approver-chip on", title: "Signed in as a human approver (8-hour session). Agents can never hold this." },
        h("span", { class: "dot ok", "aria-hidden": "true" }),
        h("span", { class: "who" }, e?.name || me, e?.role ? h("span", { class: "role" }, ` · ${roleLabel(e.role)}`) : null),
        h("button", { class: "btn btn-quiet btn-sm", onclick: () => { api.signOut(); S.voice = null; renderApproverChip(); refreshApprovals(); } }, "Sign out"))
    : h("button", { class: "btn btn-sm approver-chip off", onclick: () => openSignIn().then(() => refreshApprovals()) }, lockIcon(), "Sign in as approver"));
}

function switchApprover() {
  api.signOut();
  renderApproverChip();
  openSignIn("Sign in as the approver who should decide.").then(() => { renderApprovals(); });
}

function signInCard(customerId) {
  return h("div", { class: "card signin-card" },
    h("div", { class: "card-body" },
      h("div", { class: "row" }, h("span", { class: "signin-badge" }, lockIcon()), h("h3", { class: "grow" }, "Approvals are human-only")),
      h("p", { class: "muted sm" }, "Orders outside an agent's mandate wait here for a named person. Sign in with your name and the approver PIN; agents never receive it, and the gateway refuses any approval that carries agent identity."),
      h("button", { class: "btn btn-primary", onclick: () => openSignIn(null, customerId).then(() => refreshApprovals()) }, lockIcon(), "Sign in as approver")));
}

let signInOpen = null;
/** Opens the sign-in panel. Resolves when the panel closes (signed in or cancelled); api.js checks which. */
function openSignIn(reason, customerId) {
  if (signInOpen) return signInOpen;
  const dlg = $("signin-dialog");
  const it = selectedItem();
  const custId = customerId || it?.order?.customer_id || S.customerId;
  const needRole = it?.order?.status === "pending_approval" ? it.order.decision?.approver_role : null;
  const groups = [...S.customers].filter((c) => (c.approvers || []).length)
    .sort((a, b) => (b.id === custId) - (a.id === custId));
  const home = groups.find((c) => c.id === custId);
  const pick = (home?.approvers || []).find((a) => a.role === needRole) || home?.approvers?.[0] || groups[0]?.approvers?.[0];
  const st = { who: S.signinWho || pick?.name || pick?.id || "", other: false, busy: false, error: null };

  signInOpen = new Promise((resolve) => {
    const finish = () => { signInOpen = null; resolve(); };
    const render = () => {
      const whoSel = groups.length && !st.other
        ? h("select", { class: "input", id: "signin-who", disabled: st.busy, onchange: (e) => { if (e.target.value === "__other") { st.other = true; st.who = ""; render(); } else st.who = e.target.value; } },
            groups.map((c) => h("optgroup", { label: c.name },
              c.approvers.map((a) => { const v = a.id || a.name; return h("option", { value: v, selected: v === st.who || a.name === st.who }, `${a.name || a.id} · ${roleLabel(a.role)}`); }))),
            h("option", { value: "__other" }, "Someone else…"))
        : h("input", { class: "input", id: "signin-who", placeholder: "Name or email from the approver directory", value: st.who, disabled: st.busy, oninput: (e) => (st.who = e.target.value) });
      const pin = h("input", { class: "input mono", id: "signin-pin", type: "password", autocomplete: "current-password", placeholder: "Approver PIN", disabled: st.busy,
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } } });
      mount(dlg, h("form", { class: "signin", method: "dialog", onsubmit: (e) => { e.preventDefault(); submit(); } },
        h("div", { class: "row" }, h("span", { class: "signin-badge" }, lockIcon()), h("h2", { class: "grow" }, "Sign in as approver")),
        reason ? h("div", { class: "inline-warn small" }, reason) : null,
        h("p", { class: "muted sm" }, "Approvals are human-only. Your decisions are recorded and signed under your name. Agents never get this PIN, and any approval that carries agent identity is refused."),
        h("div", { class: "field" }, h("label", { for: "signin-who" }, "Approver"), whoSel),
        h("div", { class: "field" }, h("label", { for: "signin-pin" }, "PIN"), pin),
        st.error ? h("div", { class: "inline-error small", role: "alert" }, st.error) : null,
        h("div", { class: "faint small" }, MOCK ? "Mock mode: any PIN works." : "Demo PIN: data/keys/approver_pin, or AIMO_APPROVER_PIN. In production this is the customer's SSO."),
        h("div", { class: "row signin-actions" }, h("span", { class: "grow" }),
          h("button", { type: "button", class: "btn", disabled: st.busy, onclick: () => dlg.close() }, "Cancel"),
          h("button", { type: "submit", class: "btn btn-primary", disabled: st.busy }, st.busy ? [h("span", { class: "spinner" }), "Signing in"] : "Sign in"))));
      setTimeout(() => (st.who ? pin : $("signin-who"))?.focus(), 30);
    };
    async function submit() {
      const pin = $("signin-pin")?.value || "";
      if (!st.who.trim() || !pin) { st.error = "Enter your name and the PIN."; render(); return; }
      st.busy = true; st.error = null; render();
      try {
        await api.signIn(st.who.trim(), pin);
        S.signinWho = st.who.trim();
        dlg.close();
      } catch (e) {
        st.busy = false;
        st.error = e.code === "unknown_approver" ? "That name is not in any customer's approver directory."
          : e.status === 401 ? "Wrong PIN." : e.status === 429 ? "Too many wrong PINs from this computer. Wait a few minutes."
          : e.message;
        render();
      }
    }
    dlg.addEventListener("close", async function onClose() {
      dlg.removeEventListener("close", onClose);
      if (api.signedInApprover()) { renderApproverChip(); try { await loadCustomers(); } catch { /* keep */ } renderApproverChip(); }
      finish();
    });
    render();
    dlg.showModal();
  });
  return signInOpen;
}

// ================================================================== voice approval
// Voice suggests, the human confirms. The order is read out (ElevenLabs if ELEVENLABS_API_KEY is set on the gateway,
// else the browser's speech synthesis), the answer is transcribed (ElevenLabs Scribe, else the browser's speech
// recognition, else typed), and the gateway suggests approve/reject/unclear. Nothing is decided until the approver
// clicks Confirm, which calls the same POST /v1/approvals/{id}/decision with their session and the quote hash.
const voiceBusy = () => S.voice && ["prompt", "speaking", "listening", "thinking"].includes(S.voice.phase);
let voiceAudio = null, voiceRec = null, speakDone = null;

function stopVoiceIo() {
  try { voiceAudio?.pause(); } catch { /* ignore */ }
  try { window.speechSynthesis?.cancel(); } catch { /* ignore */ }
  try { voiceRec?.stop(); } catch { /* ignore */ }
  voiceAudio = null; voiceRec = null;
}

function setVoice(patch) {
  if (!S.voice) return;
  Object.assign(S.voice, patch);
  renderApprovals();
}

async function startVoice(o, q) {
  stopVoiceIo();
  S.decideError = null;
  S.voice = { order_id: o.order_id, quote_hash: q.quote_hash, phase: "prompt" };
  renderApprovals();
  try {
    const p = await api.voicePrompt(o.order_id);
    if (!S.voice || S.voice.order_id !== o.order_id) return;
    if (p.quote_hash && p.quote_hash !== q.quote_hash) throw new Error("The quote changed since this page loaded. Refresh the inbox.");
    setVoice({ phase: "speaking", text: p.text, tts: p.tts, ttsError: p.tts_error });
    await speak(p);
    if (S.voice?.phase !== "speaking") return;
    await listen(o);
  } catch (e) {
    if (S.voice) setVoice({ phase: "error", error: e.code === "signin_cancelled" ? "Sign in as an approver to use voice." : e.message });
  }
}

function speak(p) {
  return new Promise((resolve) => {
    const cap = setTimeout(resolve, Math.min(25000, 2500 + (p.text || "").length * 75));   // never hang on audio
    const done = () => { clearTimeout(cap); speakDone = null; resolve(); };
    speakDone = done;
    if (p.audio_b64) {
      voiceAudio = new Audio(`data:${p.mime || "audio/mpeg"};base64,${p.audio_b64}`);
      voiceAudio.onended = done; voiceAudio.onerror = done;
      voiceAudio.play().catch(done);
      return;
    }
    const synth = window.speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance === "undefined") { done(); return; }
    const u = new SpeechSynthesisUtterance(p.text);
    u.lang = "en-GB"; u.rate = 1.05;
    u.onend = done; u.onerror = done;
    synth.cancel(); synth.speak(u);
  });
}

async function listen(o) {
  const serverStt = S.voiceStatus?.stt === "elevenlabs" && window.MediaRecorder && navigator.mediaDevices?.getUserMedia;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (serverStt) {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { setVoice({ phase: "type", engine: "typed", note: "Microphone blocked: type the answer instead." }); return; }
    const chunks = [];
    const rec = new MediaRecorder(stream);
    voiceRec = rec;
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      if (S.voice?.phase !== "listening") return;
      setVoice({ phase: "thinking" });
      try {
        const r = await api.voiceTranscribe(new Blob(chunks, { type: rec.mimeType || "audio/webm" }));
        await understand(o, r.transcript, "ElevenLabs Scribe");
      } catch (e) { setVoice({ phase: "error", error: e.message }); }
    };
    rec.start();
    setVoice({ phase: "listening", engine: "ElevenLabs Scribe" });
    setTimeout(() => { if (voiceRec === rec && rec.state === "recording") rec.stop(); }, 6000);
    return;
  }
  if (SR) {
    const rec = new SR();
    voiceRec = rec;
    rec.lang = navigator.language || "en-GB"; rec.interimResults = false; rec.maxAlternatives = 1;
    let got = false;
    rec.onresult = (e) => { got = true; understand(o, e.results[0][0].transcript, "browser speech recognition"); };
    rec.onerror = (e) => { if (!got) setVoice({ phase: "type", engine: "typed", note: e.error === "not-allowed" ? "Microphone blocked: type the answer instead." : "Didn't hear anything: type the answer, or try again." }); };
    rec.onend = () => { if (!got && S.voice?.phase === "listening") setVoice({ phase: "type", engine: "typed", note: "Didn't hear anything: type the answer, or try again." }); };
    try { rec.start(); setVoice({ phase: "listening", engine: "browser speech recognition" }); } catch { setVoice({ phase: "type", engine: "typed" }); }
    return;
  }
  setVoice({ phase: "type", engine: "typed", note: "This browser has no speech recognition: type what you would say." });
}

async function understand(o, transcript, engine) {
  setVoice({ phase: "thinking", transcript, engine });
  try {
    const i = await api.voiceIntent(o.order_id, transcript);
    setVoice({ phase: "confirm", intent: i });
  } catch (e) { setVoice({ phase: "error", error: e.message }); }
}

function voiceCard(o, q) {
  const V = S.voice;
  const ttsLabel = V.tts === "elevenlabs" ? "ElevenLabs voice" : "browser voice";
  const cancel = h("button", { type: "button", class: "btn btn-quiet btn-sm", onclick: () => { stopVoiceIo(); S.voice = null; renderApprovals(); } }, "Cancel");
  const again = h("button", { type: "button", class: "btn btn-sm", onclick: () => startVoice(o, q) }, "Try again");
  const body = [];
  if (V.text) body.push(h("details", { class: "voice-prompt", open: V.phase === "speaking" },
    h("summary", null, V.phase === "speaking" ? `Reading the order aloud (${ttsLabel})…` : `Read aloud (${ttsLabel})`), h("div", { class: "small" }, V.text)));
  if (V.ttsError) body.push(h("div", { class: "faint small" }, `ElevenLabs unavailable (${V.ttsError}); used the browser voice.`));
  if (V.phase === "prompt") body.push(h("div", { class: "row" }, h("span", { class: "spinner" }), h("span", { class: "muted small" }, "Preparing the spoken summary")));
  if (V.phase === "speaking") body.push(h("div", { class: "row" }, h("button", { type: "button", class: "btn btn-sm", onclick: () => { try { voiceAudio?.pause(); window.speechSynthesis?.cancel(); } catch { /* */ } speakDone?.(); } }, "Skip to answer")));
  if (V.phase === "listening") body.push(h("div", { class: "row" }, h("span", { class: "rec-dot", "aria-hidden": "true" }), h("b", null, "Listening: say “approve” or “reject”"),
    h("span", { class: "faint small grow" }, V.engine), h("button", { type: "button", class: "btn btn-sm", onclick: () => { try { voiceRec?.stop(); } catch { /* */ } } }, "Done"),
    h("button", { type: "button", class: "btn btn-quiet btn-sm", onclick: () => { stopVoiceIo(); setVoice({ phase: "type", engine: "typed" }); } }, "Type instead")));
  if (V.phase === "type") {
    // The inbox re-renders every few seconds: keep the draft in state and only take focus once.
    const inp = h("input", { class: "input input-sm", id: "voice-typed", placeholder: "e.g. yes, approve it", value: V.typed || "",
      oninput: (e) => { V.typed = e.target.value; }, onkeydown: (e) => { if (e.key === "Enter" && inp.value.trim()) understand(o, inp.value.trim(), "typed"); } });
    body.push(V.note ? h("div", { class: "faint small" }, V.note) : null,
      h("div", { class: "row" }, inp, h("button", { type: "button", class: "btn btn-sm", onclick: () => inp.value.trim() && understand(o, inp.value.trim(), "typed") }, "Send"), again));
    if (!V.focused) { V.focused = true; setTimeout(() => inp.focus({ preventScroll: true }), 30); }
    else if (document.activeElement?.id === "voice-typed") setTimeout(() => { const n = $("voice-typed"); if (n) { n.focus({ preventScroll: true }); n.setSelectionRange(n.value.length, n.value.length); } }, 0);
  }
  if (V.phase === "thinking") body.push(V.transcript ? h("div", { class: "voice-heard" }, "Heard: ", h("q", null, V.transcript)) : null,
    h("div", { class: "row" }, h("span", { class: "spinner" }), h("span", { class: "muted small" }, "Understanding the answer")));
  if (V.phase === "confirm") {
    const i = V.intent || {};
    const intent = i.intent;
    const clear = intent === "approve" || intent === "reject";
    const me = directoryEntry(api.signedInApprover?.())?.name || api.signedInApprover?.() || "you";
    body.push(
      h("div", { class: "voice-heard" }, "Heard: ", h("q", null, i.transcript || V.transcript), h("span", { class: "faint small" }, ` · ${V.engine}`)),
      h("div", { class: "row" }, h("span", { class: `voice-intent ${intent}` }, clear ? (intent === "approve" ? "Approve" : "Reject") : "Not clear"),
        h("span", { class: "faint small grow" }, `${i.source === "gemini" ? `Gemini (${i.model || "fast"})` : "keyword rules"}${i.confidence != null ? ` · confidence ${pct(i.confidence)}` : ""}${i.note ? ` · “${i.note}”` : ""}${i.reason && !clear ? ` · ${i.reason}` : ""}`)),
      clear
        ? h("div", { class: "row voice-confirm" },
            h("span", { class: "binding-note grow" }, `One click records the decision as ${me}, bound to quote hash `, h("span", { class: "mono" }, shortHash(q.quote_hash)), ". Voice alone never orders."),
            again,
            h("button", { type: "button", class: `btn btn-lg ${intent === "approve" ? "btn-ok" : "btn-bad"}`, id: "voice-confirm", disabled: !!S.deciding,
              onclick: () => decide(o, q, intent, `Voice (${V.engine}): “${String(i.transcript || V.transcript).slice(0, 120)}”`) },
              intent === "approve" ? `Confirm approve · ${eur(q.total_net)}` : "Confirm reject"))
        : h("div", { class: "row" }, h("span", { class: "muted small grow" }, "No clear approve or reject, so nothing will happen. Say it again, or use the buttons below."), again));
  }
  if (V.phase === "error") body.push(h("div", { class: "inline-error small" }, V.error), h("div", { class: "row" }, again));
  return h("div", { class: "voice-card", style: { gridColumn: "1 / -1" }, "aria-live": "polite" },
    h("div", { class: "row" }, micIcon(), h("b", { class: "grow" }, "Decide by voice"), cancel), body);
}

// ================================================================== tabs
const VIEWS = ["plan", "approvals", "audit", "recipes", "ops"];
function setTab(tab) {
  if (!VIEWS.includes(tab)) tab = "plan";
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === tab)));
  const changed = S.tab !== tab;
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== tab;
  if (changed) document.querySelector(".tab-body").scrollTop = 0;
  S.tab = tab;
  if (tab === "audit") loadAudit();
  if (tab === "approvals") refreshApprovals(true);
  if (tab === "ops") { renderOps(opsCtx); loadOps(opsCtx); refreshLedger(); }
  if (tab === "recipes") { renderRecipes(recipesCtx); loadRecipes(recipesCtx); }
}

// Context objects for the ops and recipes modules.
function openOrder(orderId) { S.selected = orderId; S.requote = null; setTab("approvals"); }
const opsCtx = { get api() { return api; }, S, rerender: () => renderOps(opsCtx), openOrder };
const recipesCtx = {
  get api() { return api; }, S, rerender: () => renderRecipes(recipesCtx), openOrder,
  afterOrder: (o) => { if (o?.status === "pending_approval") S.selected = o.order_id; refreshApprovals(true); refreshLedger(); },
};

// Whole-ledger cache: drives the "Blocked attacks" header pill and the ops feed.
let ledgerBusy = false;
async function refreshLedger() {
  if (ledgerBusy) return;
  ledgerBusy = true;
  try {
    const evs = await api.audit();
    S.ledger = { events: Array.isArray(evs) ? evs : [], at: Date.now() };
  } catch { /* keep last */ }
  ledgerBusy = false;
  const n = blockedEvents(S.ledger.events).length;
  const pill = $("blocked-pill");
  pill.hidden = false;
  pill.className = `blocked-pill ${n ? "on" : ""}`;
  pill.textContent = `${n} attack${n === 1 ? "" : "s"} blocked`;
  $("cnt-ops").textContent = n ? String(n) : "";
  $("cnt-ops").className = `tab-count ${n ? "bad" : ""}`;
  if (S.tab === "ops") renderOps(opsCtx);
}

function updateCounts() {
  const n = S.approvals.items.length;
  const c = $("cnt-approvals");
  c.textContent = n ? String(n) : "";
  c.className = `tab-count ${n ? "attn" : ""}`;
  const p = S.run?.plan;
  $("cnt-plan").textContent = p ? String(p.candidates?.length || "") : "";
  $("cnt-audit").textContent = S.audit.events.length ? String(S.audit.events.length) : "";
}

// ================================================================== request card
function renderRequest() {
  const running = S.run && S.run.status === "running";
  const c = S.customers.find((x) => x.id === S.customerId);
  const sel = h("select", { class: "input", id: "customer", disabled: running || S.customersState === "loading", onchange: (e) => selectCustomer(e.target.value) },
    (S.customers.length ? S.customers : [{ id: S.customerId, name: "Loading customers…" }]).map((x) =>
      h("option", { value: x.id, selected: x.id === S.customerId }, x.name + (x.segment ? ` · ${x.segment}` : ""))));
  const mandate = S.customersState === "loading"
    ? h("div", { class: "mandate" }, h("div", { class: "skeleton", style: { height: "14px", width: "70%" } }), h("div", { class: "skeleton", style: { height: "14px", width: "50%" } }))
    : c && h("details", { class: "mandate mandate-d", open: !S.run && c.summary.length <= 4 },
        h("summary", null, "Mandate ", h("b", null, c.agent_id), " under policy ", h("b", null, c.policy_id), h("span", { class: "more" }, `${c.summary.length} rules ›`)),
        c.summary.length ? h("ul", null, c.summary.map((s) => h("li", null, s))) : h("div", { class: "faint" }, "Policy summary not provided by /v1/customers."));
  if (S.run && !S.editing) {
    // Compact view while a run is on screen: the timeline gets the room.
    mount($("request-card"),
      h("div", { class: "req-compact" },
        h("div", { style: { minWidth: 0, display: "flex", flexDirection: "column", gap: "4px" } },
          h("span", { class: "section-label" }, c ? `${c.name}${c.segment ? " · " + c.segment : ""}` : S.customerId),
          h("div", { class: "q" }, S.run.image ? h("img", { class: "q-thumb", src: S.run.image, alt: "Attached photo" }) : null, S.text ? ["“", S.text, "”"] : h("span", { class: "muted" }, "Order from the photo")),
          h("span", { class: "faint small mono" }, `${S.model} · thinking ${S.thinking}${S.optimize !== "agent" ? ` · optimize ${S.optimize}` : ""}`)),
        h("div", { style: { display: "flex", flexDirection: "column", gap: "6px", alignItems: "flex-end" } },
          running ? h("span", { class: "status neutral" }, h("span", { class: "spinner", style: { width: "10px", height: "10px" } }), "Running")
            : S.run.status === "waiting" ? h("span", { class: "status pending" }, "Awaiting approval")
            : S.run.status === "input" ? h("span", { class: "status pending" }, "Needs your answer")
            : S.run.status === "error" ? h("span", { class: "status bad" }, "Failed")
            : h("span", { class: "status ok" }, "Finished"),
          h("button", { class: "btn btn-sm", disabled: running, onclick: () => { S.editing = true; renderRequest(); } }, "New request"))),
      mandate, learnedPanel(S.customerId));
    return;
  }
  const ta = h("textarea", { class: "input", id: "req-text", rows: 2, disabled: running, oninput: (e) => { S.text = e.target.value; S.textEdited = true; } });
  ta.value = S.text;
  const runBtn = h("button", { class: "btn btn-primary btn-lg", id: "btn-run", disabled: running || !S.text.trim(), onclick: startRun },
    running ? [h("span", { class: "spinner" }), "Running"] : "Run agent");
  mount($("request-card"),
    h("div", { class: "field" }, h("label", { for: "customer" }, "Customer"), sel),
    mandate,
    learnedPanel(S.customerId),
    h("div", { class: "field" }, h("label", { for: "req-text" }, "Request to the buyer agent"), ta),
    h("div", { class: "req-extras" },
      S.image
        ? h("span", { class: "photo-chip" }, h("img", { src: S.image.preview, alt: "" }), h("span", { class: "ellipsis" }, S.image.name),
            h("button", { class: "btn btn-quiet btn-sm", disabled: running, onclick: () => { S.image = null; renderRequest(); } }, "Remove"))
        : h("label", { class: "btn btn-sm", for: "photo-in", title: "Photo of an order note, a delivery slip or a shelf" }, "Add or take photo"),
      h("input", { type: "file", id: "photo-in", accept: "image/*", capture: "environment", hidden: true, onchange: (e) => readPhoto(e.target.files[0]) }),
      !S.image ? h("span", { class: "faint small grow" }, "or drop it here") : h("span", { class: "grow" }),
      h("select", { class: "input input-sm", id: "optimize", "aria-label": "Optimize the plan for", title: "Optimize the plan for", disabled: running, onchange: (e) => { S.optimize = e.target.value; renderPlan(); } },
        OPTIMIZE.map((o) => h("option", { value: o.id, selected: o.id === S.optimize }, o.id === "agent" ? "Optimize: agent's ranking" : `Optimize: ${o.label.toLowerCase()}`)))),
    h("div", { class: "req-controls" },
      h("div", { class: "field" }, h("label", { for: "model" }, "Model"),
        h("select", { class: "input", id: "model", disabled: running, onchange: (e) => (S.model = e.target.value) },
          MODELS.map((m) => h("option", { value: m.id, selected: m.id === S.model, title: m.hint }, m.label)))),
      h("div", { class: "field" }, h("label", { for: "thinking" }, "Thinking"),
        h("select", { class: "input", id: "thinking", disabled: running, onchange: (e) => (S.thinking = e.target.value) },
          THINKING.map((t) => h("option", { value: t, selected: t === S.thinking }, t[0].toUpperCase() + t.slice(1))))),
      runBtn));
}

// ================================================================== run
async function startRun() {
  if (closeStream) closeStream();
  S.editing = false;
  const custId = S.customerId;
  S.run = { id: null, customer_id: custId, status: "running", steps: [], need: null, plan: null, quote: null, decision: null, order: null, error: null, image: S.image?.preview || null };
  S.requote = null;
  S.audit.verify = null;
  renderRequest(); renderTimeline(); renderPlan(); updateCounts();
  setTab("plan");
  try {
    const image = S.image ? { mime: S.image.mime, data_b64: S.image.data_b64, name: S.image.name } : null;
    const { run_id } = await api.startRun({ text: S.text, customer_id: custId, model: S.model, thinking: S.thinking, image });
    S.run.id = run_id;
    $("run-meta").textContent = run_id;
    closeStream = api.streamRun(run_id, onStep, (info) => {
      if (S.run && S.run.status === "running" && info && info.error) {
        S.run.status = "error";
        S.run.error = "The event stream closed before the run finished.";
        renderTimeline(); renderRequest();
      }
    });
  } catch (e) {
    S.run.status = "error";
    S.run.error = e.code === "signin_cancelled" ? "Sign in as an approver to instruct the buying agent." : e.message;
    renderRequest(); renderTimeline(); renderPlan();
  }
}

let auditTimer = null;
function onStep(step) {
  const r = S.run;
  if (!r || (r.id && step.run_id && step.run_id !== r.id)) return;
  const statusBefore = r.status;
  r.steps.push(step);
  const d = step.data || {};
  if (d.need) r.need = d.need;
  if (step.kind === "plan" && (d.plan || d.lines)) { r.plan = d.plan || d; r.need = r.plan.need || r.need; }
  if (d.quote) r.quote = d.quote;
  if (step.kind === "quote" && !d.quote && d.quote_id) r.quote = d;
  if (d.decision) r.decision = d.decision;
  if (d.order) r.order = d.order;
  if (step.kind === "approval_wait") r.status = "waiting";
  if (step.kind === "needs_clarification" || (d.question && Array.isArray(d.options))) { r.status = "input"; r.clarSeq = step.seq; }
  if (step.kind !== "needs_clarification" && r.status === "input" && step.seq > (r.clarSeq || 0) && ["tool_call", "thought", "plan"].includes(step.kind)) r.status = "running";
  if (step.kind === "decision" && !d.order && d.order_id) r.order = { ...(r.order || {}), order_id: d.order_id, status: d.status, quote_id: r.quote?.quote_id, decision: d.decision };
  if (step.kind === "final") {
    r.status = d.status === "error" ? "error" : "done";
    if (r.status === "error") r.error = d.summary || step.title;
  }
  // `error` steps are non-fatal (a retry, a model fallback); only `final` ends the run.

  appendStep(step);
  if (d.need || ["plan", "decision", "error", "final"].includes(step.kind)) renderPlan();
  // Follow the agent between its own tabs only; never yank the user out of Recipes or Ops.
  const agentTab = ["plan", "approvals", "audit"].includes(S.tab);
  if (step.kind === "plan" && agentTab) setTab("plan");
  if (step.kind === "decision" || step.kind === "approval_wait" || step.kind === "order") {
    const oid = d.order_id || d.order?.order_id;
    if (oid) S.selected = oid;
    if (d.order && d.order.status !== "pending_approval") S.decided.set(d.order.order_id, { order: d.order, quote: r.quote, decision: d.order.decision || r.decision });
    refreshApprovals(true);
    if (step.kind === "approval_wait" && agentTab) setTimeout(() => { if (["plan", "approvals", "audit"].includes(S.tab)) setTab("approvals"); }, 900);
    if (step.kind === "approval_wait" && params.get("autoapprove") === "1") setTimeout(() => {
      const it = selectedItem();
      if (it && it.quote) decide(it.order, it.quote, "approve").then(() => { if (params.get("tab")) setTab(params.get("tab")); });
    }, 2500);
  }
  if (r.status !== statusBefore || step.kind === "final") renderRequest();
  clearTimeout(auditTimer);
  auditTimer = setTimeout(() => loadAudit(true), 400);
  updateCounts();
}

// ================================================================== timeline
const callEls = new Map(); // call_id -> {li, call}

function renderTimeline() {
  const ol = $("timeline");
  callEls.clear();
  ol.replaceChildren();
  const r = S.run;
  $("run-meta").textContent = r?.id || "";
  if (!r) {
    ol.appendChild(h("li", { class: "empty" },
      h("strong", null, "The agent's work streams here"),
      h("span", null, "Each step appears live: the parsed need, every MCP tool call with its arguments and latency, and any exception the agent handles (cut-off passed, partial stock, unknown allergens, prompt injection)."),
      h("button", { class: "btn btn-primary btn-sm", onclick: startRun }, "Run the example request")));
    return;
  }
  if (r.error && !r.steps.length) {
    ol.appendChild(h("li", { class: "tl-item k-error" }, h("span", { class: "tl-icon" }, "!"),
      h("div", { class: "tl-main" }, h("div", { class: "tl-title" }, "Could not start the run"), h("div", { class: "tl-text" }, r.error),
        h("div", null, h("button", { class: "btn btn-sm", onclick: startRun }, "Try again")))));
    return;
  }
  if (!r.steps.length) {
    ol.appendChild(h("li", { class: "tl-item" }, h("span", { class: "tl-icon" }, h("span", { class: "spinner", style: { width: "10px", height: "10px" } })),
      h("div", { class: "tl-main" }, h("div", { class: "tl-title" }, "Starting the buyer agent…"))));
    return;
  }
  r.steps.forEach(appendStep);
}

function appendStep(step) {
  const ol = $("timeline");
  if (S.run.steps.length === 1 || ol.querySelector(".empty") || (ol.children.length === 1 && !ol.firstChild.dataset.seq)) ol.replaceChildren();
  const d = step.data || {};
  if (step.kind === "tool_result") {
    const key = d.call_id && callEls.has(d.call_id) ? d.call_id : [...callEls.keys()].reverse().find((k) => !callEls.get(k).result && callEls.get(k).call.data?.name === (d.name || step.title));
    if (key) {
      const rec = callEls.get(key);
      rec.result = step;
      const li = toolItem(rec.call, step);
      rec.li.replaceWith(li);
      rec.li = li;
      scrollTimeline();
      return;
    }
  }
  let li;
  if (step.kind === "tool_call") {
    li = toolItem(step, null);
    callEls.set(d.call_id || `seq${step.seq}`, { li, call: step, result: null });
  } else li = stepItem(step);
  li.dataset.seq = step.seq;
  ol.appendChild(li);
  scrollTimeline();
}

function scrollTimeline() {
  const pane = $("timeline-scroll");
  requestAnimationFrame(() => pane.scrollTo({ top: pane.scrollHeight, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }));
}

function argSummary(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args).map(([k, v]) => {
    let s = typeof v === "string" ? JSON.stringify(v) : Array.isArray(v) ? (v.length > 4 ? `[${v.length} items]` : JSON.stringify(v)) : typeof v === "object" && v ? "{…}" : String(v);
    if (s.length > 48) s = s.slice(0, 45) + "…";
    return `${k}=${s}`;
  }).join("  ");
}

function jsonDetails(label, obj) {
  if (obj == null) return null;
  const pre = h("pre", { class: "code" });
  const det = h("details", { class: "json" }, h("summary", null, label), pre);
  det.addEventListener("toggle", () => { if (det.open && !pre.textContent) pre.textContent = pretty(obj); });
  return det;
}

function exceptionsOf(d) {
  const out = [];
  for (const e of d.exceptions || []) out.push(typeof e === "string" ? { code: e, message: flagInfo(e).label } : e);
  if (!out.length && Array.isArray(d.result)) {
    for (const o of d.result) {
      if (!o || typeof o !== "object") continue;
      const id = o.product_id || o.id;
      if (o.cutoff_passed) out.push({ code: "cutoff_passed", message: `${id}: order cut-off passed${o.closing_time ? ` ${dayTime(o.closing_time)}` : ""}.` });
      for (const i of o.issues || []) if (i !== "cutoff_passed") out.push({ code: i, message: `${id}: ${flagInfo(i).label.toLowerCase()}${i === "partial_stock" && o.available_amount != null ? `, ${num(o.available_amount, 0)} units available` : ""}.` });
    }
  }
  for (const f of d.flags || []) if (!out.find((x) => x.code === f)) out.push({ code: f, message: flagInfo(f).label });
  if (d.suspicious || d.injection) out.push({ code: "injection_flagged", message: (d.reasons || []).join("; ") || "Supplier text flagged as prompt injection and ignored." });
  return out;
}

function excEls(d) {
  return exceptionsOf(d).map((e) => {
    const f = flagInfo(e.code);
    const sev = f.sev === "bad" ? "bad" : "warn";
    return h("div", { class: `exc ${sev}` }, h("b", null, sev === "bad" ? "✕" : "!"),
      h("div", null, h("b", null, f.label + ". "), h("span", { class: "exc-body" }, e.message && e.message !== f.label ? e.message : "")));
  });
}

function toolItem(call, result) {
  const d = call.data || {};
  const rd = result?.data || {};
  const failed = result && (rd.ok === false || rd.error || (rd.result && typeof rd.result === "object" && !Array.isArray(rd.result) && rd.result.error));
  const latMs = rd.latency_ms ?? (rd.latency_s != null ? rd.latency_s * 1000 : null);
  return h("li", { class: `tl-item k-tool ${failed ? "k-error" : ""}`, "data-seq": call.seq },
    h("span", { class: "tl-icon", title: "Tool call" }, "ƒ"),
    h("div", { class: "tl-main" },
      h("div", { class: "tl-title" },
        h("span", { class: "grow" }, h("span", { class: "tool-name" }, d.name || call.title)),
        result ? (latMs != null ? h("span", { class: "latency" }, `${num(latMs, 0)} ms`) : null) : h("span", { class: "spinner", style: { width: "11px", height: "11px", color: "var(--accent)" } }),
        h("span", { class: "tl-time" }, time(call.ts))),
      d.args ? h("div", { class: "tool-args" }, argSummary(d.args)) : null,
      result ? h("div", { class: "tl-text" }, rd.summary || rd.message || result.title || "Result received") : null,
      result ? excEls(rd) : null,
      h("div", { class: "row wrap", style: { gap: "12px" } }, jsonDetails("Arguments", d.args), result ? jsonDetails("Result", rd.result ?? rd) : null)));
}

function needChips(need) {
  if (!need) return null;
  const chips = [];
  const chip = (k, v) => h("span", { class: "chip" }, h("i", null, k), h("b", null, v));
  chips.push(chip("Item", need.product_query));
  (need.required_claims || []).forEach((c) => chips.push(chip("Claim", claimLabel(c))));
  (need.excluded_allergens || []).forEach((a) => chips.push(chip("No", a)));
  chips.push(chip("Qty", `${num(need.quantity)} ${need.unit}`));
  if (need.delivery_by) chips.push(chip("By", day(need.delivery_by)));
  (need.preferred_product_ids || []).forEach((p) => chips.push(chip("Usual", p)));
  const pr = need.preferences || {};
  for (const [k, v] of Object.entries(pr)) if (v != null && typeof v !== "object" && !/^(usual_weekly_volume_l|parsed_by|standing_order|injection)/.test(k)) chips.push(chip(k.replace(/_/g, " "), String(v)));
  return h("div", { class: "chips" }, chips);
}

function stepItem(step) {
  const d = step.data || {};
  const k = step.kind;
  const isClar = k === "needs_clarification" || (d.question && Array.isArray(d.options));
  const lines = d.extracted ? (d.lines || d.extracted.lines || []) : d.extracted_lines || d.lines_from_photo || (k === "extracted_lines" ? d.lines : null);
  const cls = isClar ? "k-clar" : lines ? "k-photo" : { thought: "k-thought", plan: "k-plan", quote: "k-quote", decision: "k-decision", approval_wait: "k-wait", order: "k-plan", error: "k-warnstep", final: d.status === "error" ? "k-error" : "k-final" }[k] || "";
  const icon = isClar ? "?" : lines ? "▣" : { thought: "·", plan: "P", quote: "Q", decision: "§", approval_wait: "…", order: "✓", error: "!", final: "■" }[k] || "·";
  const body = [];
  const ttl = String(step.title || "").replace(/…$/, "");
  if (d.text && !(d.text.startsWith(ttl) && d.text.length < ttl.length + 20)) body.push(h("div", { class: "tl-text" }, d.text));
  if (k === "final" && d.summary) body.push(h("div", { class: "tl-text", style: { color: "var(--text)" } }, d.summary));
  if (d.drift) body.push(h("div", { class: "exc warn" }, h("b", null, "!"), h("div", null, h("b", null, "Unusual volume. "), h("span", { class: "exc-body" }, step.title))));
  if (d.excerpt || /(flagged|suspected) (suspected )?prompt injection/i.test(step.title)) body.push(h("div", { class: "exc bad" }, h("b", null, "✕"), h("div", null, h("b", null, "Prompt injection flagged and redacted. "),
    h("span", { class: "exc-body" }, d.excerpt ? `“${d.excerpt}”` : "", (d.reasons || []).length ? ` (${d.reasons.join("; ")})` : ""))));
  if (d.need) body.push(needChips(d.need));
  if (lines) body.push(extractedTable(lines, d));
  if (isClar) body.push(clarCard(step));
  if (k === "plan") {
    const p = d.plan || d;
    body.push(h("div", { class: "tl-text" }, `${p.lines?.length || 0} line(s), covers ${num(p.covered_amount)} ${p.need?.unit || ""}, total ${eur(p.total_net)} net.`),
      h("div", null, h("button", { class: "btn btn-quiet btn-sm", onclick: () => setTab("plan") }, "Open plan →")));
  }
  if (k === "quote") {
    const q = d.quote || d;
    body.push(h("div", { class: "tl-text" }, `${eur(q.total_net)} net · hash `, h("span", { class: "mono" }, shortHash(q.quote_hash)), ` · valid until ${hm(q.expires_at)}`));
  }
  if (k === "decision" && d.decision) {
    const v = VERDICT[d.decision.outcome] || { title: d.decision.outcome };
    const sev = d.decision.outcome === "auto_approve" ? "ok" : d.decision.outcome === "reject" ? "bad" : "pending";
    body.push(h("div", { class: "row wrap" }, h("span", { class: `status ${sev}` }, v.title + (d.decision.approver_role ? ` · ${roleLabel(d.decision.approver_role)}` : "")),
      h("span", { class: "tl-text" }, `${(d.decision.hits || []).filter((x) => x.outcome !== "allow").length} rule(s) fired`)));
  }
  if (k === "approval_wait") {
    body.push(h("div", { class: "tl-text" }, "The agent has paused. Nothing is ordered until a human approves the exact signed quote."),
      h("div", null, h("button", { class: "btn btn-sm", onclick: () => { S.selected = d.order_id || S.selected; setTab("approvals"); } }, "Open approval inbox →")));
  }
  if (k === "order" && d.order) {
    const o = d.order;
    body.push(h("div", { class: "row wrap" }, statusPill(o.status), o.response_code ? h("span", { class: `resp-code ${o.response_code}` }, o.response_code) : null,
      h("button", { class: "btn btn-quiet btn-sm", onclick: () => setTab("audit") }, "Audit trail →")));
  }
  if (k === "error") body.push(h("div", { class: "tl-text" }, d.message || d.error || ""));
  body.push(...excEls(d));
  const extra = { ...d };
  delete extra.text; delete extra.need;
  return h("li", { class: `tl-item ${cls}` },
    h("span", { class: "tl-icon" }, icon),
    h("div", { class: "tl-main" },
      h("div", { class: "tl-title" }, h("span", { class: "grow" }, step.title), h("span", { class: "tl-time" }, time(step.ts))),
      body,
      ["thought", "plan", "quote", "decision"].includes(k) && Object.keys(extra).length ? jsonDetails("Data", d) : null));
}

// Photo intake result: what Gemini read off the photo, line by line (aimo/buyer/vision.py ImageOrder).
function extractedTable(lines, d) {
  const ex = d.extracted || {};
  const unreadable = ex.unreadable || [];
  return h("div", { class: "photo-lines" },
    S.run?.image ? h("img", { src: S.run.image, alt: "Photo sent with the request" }) : null,
    h("div", { class: "grow", style: { minWidth: 0 } },
      h("table", { class: "t" },
        h("thead", null, h("tr", null, h("th", null, "On the photo"), h("th", null, "Read as"), h("th", { class: "n" }, "Qty"))),
        h("tbody", null, lines.map((l) => {
          const unclear = l.unclear || l.quantity == null && !!(l.original_text || l.text) && ex.document_type !== "menu" || (l.confidence != null && l.confidence < 0.6);
          return h("tr", { class: unclear ? "unclear" : "" },
            h("td", { class: "mono" }, l.original_text || l.text || l.raw || "–"),
            h("td", null, l.product || l.product_query || l.item || l.name || "–", l.notes ? h("span", { class: "faint small" }, ` · ${l.notes}`) : null,
              unclear ? h("span", { class: "flag warn", style: { marginLeft: "6px" } }, "Unclear") : null),
            h("td", { class: "n" }, l.quantity != null ? `${num(l.quantity)} ${l.unit || ""}` : "–"));
        }))),
      unreadable.length ? h("div", { class: "inline-warn small" }, "Not readable, not guessed: ", unreadable.join("; ")) : null,
      h("div", { class: "faint small" }, [ex.document_type ? String(ex.document_type).replace(/_/g, " ") : null, ex.language ? `language ${ex.language}` : null,
        d.model || "read by Gemini vision", d.request_text ? `→ “${d.request_text}”` : null].filter(Boolean).join(" · "))));
}

// needs_clarification: the agent asks one question with options; the answer goes to POST /v1/agent/runs/{id}/answer.
function clarCard(step) {
  const d = step.data || {};
  const answered = S.answers[step.seq];
  const opts = (d.options || []).map((o, i) => (typeof o === "string" ? { id: String(i), label: o } : { id: o.id ?? String(i), label: o.label || o.text || String(o.id) }));
  const send = async (answer, option_id) => {
    S.answers[step.seq] = { label: answer, pending: true };
    rerenderStep(step);
    try {
      await api.answer(S.run.id, option_id != null ? { option_id, question_id: d.question_id } : { text: answer, question_id: d.question_id }, S.run.customer_id || S.customerId);
      S.answers[step.seq] = { label: answer };
      if (S.run.status === "input") S.run.status = "running";
      renderRequest();
    } catch (e) {
      S.answers[step.seq] = { label: answer, error: e.message };
    }
    rerenderStep(step);
  };
  const box = h("div", { class: "clar" },
    d.question && d.question !== step.title ? h("div", { class: "clar-q" }, d.question) : null,
    d.why ? h("div", { class: "faint small" }, d.why) : null);
  if (answered && !answered.error) {
    box.appendChild(h("div", { class: "clar-a" }, answered.pending ? h("span", { class: "spinner" }) : "✓", ` You answered: ${answered.label}`));
    return box;
  }
  box.appendChild(h("div", { class: "row wrap" }, opts.map((o, i) => h("button", { class: `btn btn-sm ${i === 0 ? "btn-primary" : ""}`, onclick: () => send(o.label, o.id) }, o.label))));
  if (d.free_text !== false && d.allow_free_text !== false) {
    const inp = h("input", { class: "input input-sm", placeholder: "Or type an answer", onkeydown: (e) => { if (e.key === "Enter" && inp.value.trim()) send(inp.value.trim(), null); } });
    box.appendChild(h("div", { class: "row" }, inp, h("button", { class: "btn btn-sm", onclick: () => inp.value.trim() && send(inp.value.trim(), null) }, "Send")));
  }
  if (answered?.error) box.appendChild(h("div", { class: "inline-error small" }, answered.error));
  return box;
}

function rerenderStep(step) {
  const li = $("timeline").querySelector(`[data-seq="${step.seq}"]`);
  if (li) { const n = stepItem(step); n.dataset.seq = step.seq; n.style.animation = "none"; li.replaceWith(n); }
}

// ================================================================== plan view
function renderPlan() {
  const v = $("view-plan");
  const r = S.run;
  if (!r) {
    mount(v, h("div", { class: "empty" },
      h("strong", null, "No plan yet"),
      h("span", null, "Run the agent on a request. Its good-better-best plan appears here: every candidate it considered, the price per litre, stock, delivery reliability and CO2, and why each one was ranked or rejected."),
      h("button", { class: "btn btn-primary btn-sm", onclick: startRun }, "Run the example request")));
    return;
  }
  const p = r.plan;
  const parts = [];
  parts.push(h("div", { class: "card summary" },
    h("div", { style: { display: "flex", flexDirection: "column", gap: "8px", minWidth: 0 } },
      h("span", { class: "section-label" }, "Structured need"),
      r.need ? needChips(r.need) : h("div", { class: "skeleton", style: { height: "22px", width: "60%" } }),
      (r.need && r.need.raw_text) || S.text ? h("div", { class: "muted sm" }, "“", (r.need && r.need.raw_text) || S.text, "”") : null),
    p ? h("div", { class: "kpis" },
      h("div", { class: "kpi" }, h("span", { class: "v" }, `${num(p.covered_amount)} / ${num(p.need?.quantity)} ${p.need?.unit || ""}`), h("span", { class: "l" }, p.shortfall ? `Shortfall ${num(p.shortfall)} ${p.need?.unit}` : "Covered")),
      planCo2(p) != null ? h("div", { class: "kpi" }, h("span", { class: "v" }, `${num(planCo2(p), 0)} kg`), h("span", { class: "l" }, "CO₂e, category average")) : null,
      h("div", { class: "kpi" }, h("span", { class: "v big" }, eur(p.total_net)), h("span", { class: "l" }, "Total, net of VAT"))) : null));

  if (r.decision) parts.push(verdictBanner(r.decision));
  if (r.error && !p) parts.push(h("div", { class: "exc bad" }, h("b", null, "!"), h("div", null, h("b", null, "The run failed before a plan was ready. "), h("span", { class: "exc-body" }, r.error))));

  if (!p) {
    if (r.status === "running") parts.push(h("div", { class: "tier-group" }, h("span", { class: "section-label" }, "Ranking candidates…"),
      [1, 2, 3].map(() => h("div", { class: "skeleton", style: { height: "128px" } }))));
    mount(v, parts);
    return;
  }

  const reqWarn = planRequestScreenWarnEl(p.explanation, p.need || r.need);
  if (reqWarn) parts.push(reqWarn);
  if (p.explanation) parts.push(h("p", { class: "sm", style: { maxWidth: "80ch" } }, p.explanation));
  parts.push(planLines(p));

  const cands = p.candidates || [];
  const inPlan = new Set((p.lines || []).map((l) => l.product_id));
  const usable = cands.filter((c) => c.tier !== "rejected");
  const wins = winners(usable);
  parts.push(h("div", { class: "rank-bar" },
    h("span", { class: "section-label" }, `Candidates · ${usable.length}`), h("span", { class: "grow" }),
    h("span", { class: "muted small" }, "Rank by"),
    h("div", { class: "seg" }, OPTIMIZE.map((o) => h("button", { class: o.id === S.optimize ? "on" : "", onclick: () => { S.optimize = o.id; renderPlan(); renderRequest(); } }, o.label)))));
  if (S.optimize === "agent") {
    for (const t of TIER_ORDER) {
      const list = cands.filter((c) => c.tier === t);
      if (!list.length) continue;
      parts.push(h("div", { class: "tier-group" },
        h("div", { class: "tier-head" }, h("span", { class: `tier-tag ${t}` }, TIER_LABEL[t]), h("span", { class: "muted sm" }, TIER_HINT[t])),
        list.map((c) => candCard(c, p, inPlan, wins))));
    }
  } else {
    const key = { cheapest: (c) => perL(c).v ?? Infinity, greenest: (c) => c.product?.co2_kg_per_kg ?? Infinity, reliable: (c) => -(c.product?.delivery_reliability ?? -1) }[S.optimize];
    const sorted = [...usable].sort((a, b) => key(a) - key(b) || (perL(a).v ?? Infinity) - (perL(b).v ?? Infinity));
    parts.push(h("div", { class: "tier-group" },
      h("div", { class: "muted small" }, `Re-ranked in the browser for “${OPTIMIZE.find((o) => o.id === S.optimize).label}”. The policy still evaluates whatever the agent quotes.`),
      sorted.map((c, i) => candCard(c, p, inPlan, wins, i + 1))));
  }
  const rej = cands.filter((c) => c.tier === "rejected");
  if (rej.length) {
    parts.push(h("details", { class: "rejected-group" },
      h("summary", null, h("span", { class: "chev" }, "›"), h("span", { class: "tier-tag rejected" }, `Rejected · ${rej.length}`), h("span", { class: "muted sm" }, "Considered and excluded, with the reason")),
      h("div", { class: "card" }, rej.map((c) => h("div", { class: "rej-row" },
        h("div", { style: { minWidth: 0 } },
          h("div", { class: "ellipsis", title: c.product.name, style: { fontWeight: 500 } }, c.product.name),
          h("div", { class: "rej-reason" }, (c.reasons || []).join(" · ") || "Rejected"),
          h("div", { class: "faint small mono" }, c.product.id, c.offer?.logistic_model ? ` · ${c.offer.logistic_model}` : "")),
        h("div", { class: "row wrap", style: { justifyContent: "flex-end", alignContent: "flex-start" } }, (c.flags || []).map(flagEl)))))));
  }
  mount(v, parts);
}

function perL(c) {
  const o = c.offer || {};
  if (o.comparison_price != null) return { v: o.comparison_price, u: o.comparison_unit || c.product.content_unit || "L" };
  if (o.unit_price != null && c.product.content_amount) return { v: o.unit_price / c.product.content_amount, u: c.product.content_unit || "L" };
  return { v: null, u: c.product.content_unit || "L" };
}

const REQ_SCREEN_WARN_PREFIX = "Warning: the request contains instructions aimed at AI agents";
const REQ_APPROVAL_RX = "(?:approv\\w*|sign[- ]?off|signed off|human review|review step|verification|human check|checks?|guardrails?)";
const REQ_SCREEN_RULES = [
  ["instruction override", /\b(?:ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your|the|these|my)\b[^.\n]{0,30}\b(?:instructions?|rules?|prompts?|limits?|guardrails?|polic(?:y|ies)|checks?|restrictions?|budget)|\bignore your\b|\bdisregard (?:the|your|all)\b|\b(?:unohda|ohita|sivuuta|älä välitä)\b[^.\n]{0,40}\b(?:ohje\w*|sään\w*|raj\w*|hyväksyn\w*|tarkist\w*|budje\w*)|\b(?:ignorera|glöm|bortse från|strunta i)\b[^.\n]{0,40}\b(?:instruktion\w*|regl\w*|gräns\w*|godkänn\w*|granskning\w*)/i],
  ["limit bypass", /\b(?:ignore|bypass|skip|disable|remove|lift)\s+(?:the\s+|your\s+|all\s+|any\s+)?(?:spending\s+|spend\s+|order\s+|approval\s+|budget\s+)?(?:limits?|caps?|thresholds?|guardrails?|restrictions?)\b/i],
  ["approval bypass", new RegExp("\\b(?:skip|bypass|avoid|waive[ds]?|disable[ds]?|remove|without)\\b[^.\\n]{0,30}\\b" + REQ_APPROVAL_RX + "|" + REQ_APPROVAL_RX + "[^.\\n]{0,40}\\b(?:has been |is |was )?(?:waived|skipped|not (?:needed|required|necessary)|disabled|unnecessary|optional)\\b|\\bno (?:need|reason) (?:for|to)\\b[^.\\n]{0,30}\\b(?:approv\\w*|human|review|ask|check)|\\b(?:nobody|no one|no human) (?:has|needs|will have) to (?:approve|review|check|see)|\\bapproval[_ ]required\\s*[=:]\\s*(?:false|0|no)|\\bpolicy\\s*[=:]\\s*(?:none|off|false)|\\brequires?_approval\\s*[=:]\\s*false|\\bauto[- ]?approv\\w*|\\bapprove (?:it|this|everything|all|the order) (?:automatically|directly|now|immediately)", "i")],
  ["fake approval", /\b(?:pre[- ]?approved|pre[- ]?authori[sz]ed|pre[- ]?cleared|already (?:been )?(?:approved|authori[sz]ed|signed off|cleared|verified))\b|\b(?:has|have) (?:already )?signed off\b|\bauthori[sz]ed by (?:the|our|your) (?:cfo|ceo|cto|coo|director|manager|board|owner|boss)|\bmark (?:it|this|the order|the quote|them) as (?:verified|approved|pre-?approved|safe|allergen[- ]?safe)|\b(?:jo|valmiiksi|etukäteen) hyväksy\w*|\bhyväksyn\w* (?:on )?(?:jo )?(?:annettu|saatu)|\bhyväksyntää ei (?:tarvita|tarvitse)|\bälä lähetä\b[^.\n]{0,30}hyväksy\w*|\bei tarvitse hyväksy\w*|\b(?:redan|förhands)\s?godkän\w*|\bhoppa över\b[^.\n]{0,30}\b(?:godkänn\w*|granskning\w*|kontroll\w*)|\bingen\b[^.\n]{0,30}\b(?:godkänn\w*|granskning)[^.\n]{0,20}\b(?:behövs|krävs)/i],
  ["allergen override", /\ballerg\w*[^.\n]{0,40}\b(?:do(?:es)?n'?t|do(?:es)? not|no longer|never) (?:apply|matter|count)|\b(?:mark|treat|label|consider|report|flag|list)\s+(?:it|this|them|the product|this product|the item|this item|these)(?:\s+as)?\s+(?:an? )?(?:allergen|gluten|nut|milk|lactose|dairy)[- ]?(?:safe|free)\b/i],
  ["role injection", /["']role["']\s*:\s*["'](?:system|developer|assistant|tool)["']|\[\s*(?:system|developer|assistant instruction|admin)\b[^\]]{0,40}\]|<\s*\/?\s*(?:system|developer|instructions?)\s*>|\b(?:system|developer|admin)\s+(?:override|notice|message|instruction|mode)\b|\b(?:developer|admin|god|debug|maintenance) mode\b|\byou are now\b|\bact as (?:an? |the )?(?:admin|administrator|developer|system|root)\b|\bnew instructions?\b|\bjailbreak\b|\bdan mode\b|(?:^|\n)\s*(?:assistant|system)\s*:|\bpolicy engine\b[^.\n]{0,30}\b(?:disabled|offline|off|down|bypassed|suspended)\b/im],
  ["prompt probing", /\b(?:system|developer|hidden)\s*(?:prompt|message|instructions?)\b/i],
  ["exfiltration", /\b(?:reveal|disclose|print|output|repeat|send|share|leak)\b[^.\n]{0,40}\b(?:mandate|approval limits?|spending limits?|system prompt|instructions|api[_ ]?keys?|passwords?|credentials|policy)\b/i],
  ["tool call injection", /\bplace_order\b|\bcreate_quote\b|\bquote_hash\s*=|"?(?:function_call|tool_call|tool_use)"?\s*:|\bcall (?:the )?(?:tool|function)\b/i],
  ["secrecy", /\b(?:do it|act|proceed) (?:silently|quietly|secretly)\b|\b(?:don'?t|do not|never) (?:tell|inform|mention|notify|show)\b[^.\n]{0,30}\b(?:human|customer|user|manager|approver|buyer|anyone)|\bwithout telling\b|\bälä kerro\b|\bsäg (?:inget|inte)\b/i],
  ["price manipulation", /\baccept any price\b|\bprice\b[^.\n]{0,20}\b(?:is a placeholder|doesn'?t matter|is not final)|\b(?:change|set|override|ignore) (?:the )?(?:unit )?(?:price|total|invoice)\b/i],
  ["quantity manipulation", /\b(?:set|change|increase|raise) (?:the )?(?:quantity|qty|amount) (?:to|by)\b|\bmultiply (?:the )?(?:quantity|order|amount)\b|\b\d+\s*(?:x|×|times) the (?:requested|ordered|usual)? ?(?:quantity|amount|order)\b|\b(?:order|buy) (?:the )?(?:entire|whole|all) (?:stock|inventory)\b|\bkoko varasto\b/i],
  ["limit evasion", /\bsplit\b[^.\n]{0,30}\borders?\b[^.\n]{0,40}\b(?:under|below|less than|so (?:that )?(?:nobody|no one|it doesn'?t))|\b(?:keep|stay)\w* (?:just )?(?:under|below) the (?:approval |auto[- ]?approve |spending )?(?:limit|threshold)|\bpretend (?:the|that)\b[^.\n]{0,30}\b(?:limit|budget|approval|allergen)/i],
];

function screenRequestNeed(need) {
  if (!need || typeof need !== "object") return [];
  const raw = [need.raw_text, need.product_query].filter((s) => typeof s === "string" && s.trim()).join("\n");
  const out = [];
  if (raw) {
    const norm = raw.normalize("NFKC").replace(/[\u200B-\u200F\u202A-\u202E\u2060-\u2064\uFEFF]/g, "").replace(/[ \t]+/g, " ");
    for (const [reason, rx] of REQ_SCREEN_RULES) {
      if (rx.test(norm) && !out.includes(reason)) out.push(reason);
    }
  }
  return out;
}

function planRequestScreenWarnEl(explanation, need) {
  const text = String(explanation || "");
  if (text.includes(REQ_SCREEN_WARN_PREFIX)) {
    const fullMatch = text.match(/Warning: the request contains instructions aimed at AI agents(?:\s*\(([^)]*)\))?\.\s*They were ignored,\s*and the customer's policy will ask a human to confirm\./);
    const reasons = fullMatch ? fullMatch[1] : (text.match(/Warning: the request contains instructions aimed at AI agents\s*\(([^)]*)\)/) || [])[1];
    const detail = fullMatch ? fullMatch[0] : text.slice(text.indexOf(REQ_SCREEN_WARN_PREFIX));
    return h("div", { class: "exc warn plan-request-screen-warn", role: "alert" },
      h("b", null, "!"),
      h("div", null,
        h("b", null, `Buyer request injection screen: FLAGGED${reasons ? ` (${reasons.trim()})` : ""}. `),
        h("span", { class: "exc-body" }, detail)));
  }
  const needReasons = screenRequestNeed(need);
  if (!needReasons.length) return null;
  const label = needReasons.join(", ");
  const detail = `${REQ_SCREEN_WARN_PREFIX} (${label}). They were ignored, and the customer's policy will ask a human to confirm.`;
  return h("div", { class: "exc warn plan-request-screen-warn", role: "alert" },
    h("b", null, "!"),
    h("div", null,
      h("b", null, `Buyer request injection screen: FLAGGED (${label}). `),
      h("span", { class: "exc-body" }, detail)));
}

function planScreenEl(item, showClean = true) {
  const flagged = Boolean(item.injection_screen?.suspicious)
    || (item.flags || []).some((f) => f === "supplier_text_injection_suspected" || f === "injection_flagged");
  if (flagged) {
    const reasons = item.injection_screen?.reasons?.length ? ` (${item.injection_screen.reasons.join(", ")})` : "";
    return h("div", { class: "plan-screen bad" },
      h("b", null, "Prompt-injection screen: FLAGGED"),
      `${reasons} - untrusted supplier text ignored; requires human approval`);
  }
  if (showClean && item.injection_screen && !item.injection_screen.suspicious) {
    return h("div", { class: "plan-screen ok" }, "Prompt-injection screen: clean");
  }
  return null;
}

function planLines(p) {
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", null, "Order lines the agent proposes"), h("span", { class: "muted sm" }, `Delivery by ${day(p.need?.delivery_by)}`)),
    h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", null, "Product"), h("th", null, "Tier"), h("th", { class: "n" }, "Qty"), h("th", { class: "n" }, "Unit price"), h("th", { class: "n" }, "Covers"), h("th", { class: "n" }, "CO₂e"), h("th", { class: "n" }, "Line total"))),
      h("tbody", null, (p.lines || []).map((l) => h("tr", null,
        h("td", null, h("div", { style: { fontWeight: 500 } }, l.product_name), h("div", { class: "faint small mono" }, l.product_id), planScreenEl(l, true), (l.flags || []).length ? h("div", { class: "row wrap", style: { marginTop: "4px" } }, l.flags.map(flagEl)) : null),
        h("td", null, h("span", { class: `tier-tag ${l.tier}` }, TIER_LABEL[l.tier] || l.tier)),
        h("td", { class: "n" }, `${num(l.qty_sales_units)} ${l.sales_unit}`),
        h("td", { class: "n" }, eur(l.unit_price)),
        h("td", { class: "n" }, `${num(l.covered_amount)} ${p.need?.unit || ""}`),
        h("td", { class: "n" }, lineCo2(p, l) != null ? `${num(lineCo2(p, l), 0)} kg` : "–"),
        h("td", { class: "n" }, eur(l.line_total))))),
      h("tfoot", null, h("tr", null, h("td", { colspan: 5 }, "Total net"), h("td", { class: "n" }, planCo2(p) != null ? `${num(planCo2(p), 0)} kg` : "–"), h("td", { class: "n" }, eur(p.total_net))))),
    h("div", { class: "card-body faint small" }, "CO₂e uses the category-average kg CO₂e per kg from the Valio Aimo dataset (1 L counted as 1 kg); it is an estimate, not a product footprint."));
}

// ---- sustainability and reliability helpers
const LABELS = {
  GOODS_FROM_FINLAND_BLUE_SWAN: "Hyvää Suomesta", FINNISH_HEART_SYMBOL: "Heart symbol", EU_ORGANIC_FARMING: "EU organic",
  OFFICIAL_ECO_LABEL_SUN: "Luomu (sun)", RAINFOREST_ALLIANCE: "Rainforest Alliance", MADE_IN_FINLAND_FLAG_WITH_KEY: "Avainlippu",
  FAIR_TRADE_MARK: "Fairtrade", ORGANIC: "Organic",
};
const labelName = (l) => LABELS[l] || (String(l).startsWith("injected:") ? null : titleCaseLabel(l));
const titleCaseLabel = (l) => String(l).toLowerCase().replace(/_/g, " ").replace(/^./, (m) => m.toUpperCase());

function productOf(p, pid) { return (p.candidates || []).find((c) => c.product?.id === pid)?.product; }
function lineCo2(p, l) {
  const prod = productOf(p, l.product_id);
  if (!prod || prod.co2_kg_per_kg == null || !prod.content_amount || prod.content_unit === "PCS") return null;
  return l.qty_sales_units * prod.content_amount * prod.co2_kg_per_kg;
}
function planCo2(p) {
  const xs = (p.lines || []).map((l) => lineCo2(p, l));
  return xs.length && xs.every((x) => x != null) ? xs.reduce((a, b) => a + b, 0) : null;
}
function relBadge(r) {
  if (r == null) return h("span", { class: "rel none", title: "No delivery reliability data for this product" }, "Reliability: no data");
  const cls = r >= 0.98 ? "ok" : r >= 0.93 ? "warn" : "bad";
  return h("span", { class: `rel ${cls}`, title: "Share of past deliveries that arrived complete and on time (Valio Aimo dataset)" }, `Reliability ${pct(r)}`);
}
// Winners among usable candidates; a badge only shows when the metric actually separates candidates.
function winners(list) {
  const pick = (f, better) => {
    const vals = list.map(f).filter((v) => v != null && isFinite(v));
    if (new Set(vals).size < 2) return null;
    const best = vals.reduce((a, b) => (better(b, a) ? b : a));
    return list.find((c) => f(c) === best)?.product?.id;
  };
  return {
    cheapest: pick((c) => perL(c).v, (a, b) => a < b),
    greenest: pick((c) => c.product?.co2_kg_per_kg, (a, b) => a < b),
    reliable: pick((c) => c.product?.delivery_reliability, (a, b) => a > b),
  };
}

function candCard(c, p, inPlan, wins = {}, rankNo = null) {
  const prod = c.product || {};
  const o = c.offer || {};
  const pl = perL(c);
  const line = (p.lines || []).find((l) => l.product_id === prod.id);
  const need = p.need || {};
  let qtyText, totalText, qtyLabel;
  if (line) { qtyLabel = "In plan"; qtyText = `${num(line.qty_sales_units)} ${line.sales_unit}`; totalText = eur(line.line_total); }
  else if (o.unit_price != null && prod.content_amount) {
    const full = Math.ceil(need.quantity / prod.content_amount);
    const q = o.available_amount != null && o.available_amount < 1e8 ? Math.min(full, o.available_amount) : full;
    qtyLabel = "If chosen"; qtyText = `${num(q, 0)} ${o.sales_unit || prod.sales_unit}`; totalText = eur(q * o.unit_price);
  } else { qtyLabel = "If chosen"; qtyText = "–"; totalText = "–"; }
  let coverCls = "";
  // The mock API reports unlimited stock as a huge sentinel (e.g. 9999999999).
  const unlimited = o.available_amount != null && o.available_amount >= 1e8;
  let avail = unlimited ? "in stock" : o.available_amount != null ? `${num(o.available_amount, 0)} ${o.sales_unit || prod.sales_unit}` : o.cutoff_passed ? "cut-off passed" : o.available === false ? "not available" : "unknown";
  if (o.cutoff_passed || o.available === false) coverCls = "bad";
  else if (o.available_amount != null && prod.content_amount && o.available_amount * prod.content_amount < need.quantity) coverCls = "warn";
  const fact = (l, v, cls = "") => h("div", { class: "fact" }, h("span", { class: "l" }, l), h("span", { class: `v ${cls}`, title: v }, v));
  return h("article", { class: `cand ${c.tier} ${inPlan.has(prod.id) ? "in-plan" : ""}` },
    h("div", { class: "cand-body" },
      h("div", { class: "cand-top" },
        h("div", { class: "grow" },
          h("div", { class: "row wrap" },
            rankNo ? h("span", { class: "rank-no" }, `#${rankNo}`) : null,
            rankNo ? h("span", { class: `tier-tag ${c.tier}` }, TIER_LABEL[c.tier] || c.tier) : null,
            h("span", { class: "cand-name" }, prod.name), inPlan.has(prod.id) ? h("span", { class: "plan-badge" }, "✓ In plan") : null,
            wins.cheapest === prod.id ? h("span", { class: "win" }, "Cheapest") : null,
            wins.greenest === prod.id ? h("span", { class: "win green" }, "Greenest") : null,
            wins.reliable === prod.id ? h("span", { class: "win" }, "Most reliable") : null),
          h("div", { class: "cand-sub" }, [prod.id, prod.brand, prod.vendor, o.logistic_model, o.closing_time ? `cut-off ${dayTime(o.closing_time)}` : null].filter(Boolean).join(" · "))),
        h("div", { class: "cand-price" }, h("div", { class: "v" }, pl.v != null ? eur(pl.v) : "–"), h("div", { class: "l" }, `per ${pl.u}`), relBadge(prod.delivery_reliability))),
      h("div", { class: "facts" },
        fact("Pack", prod.content_amount ? `${num(prod.content_amount)} ${prod.content_unit || ""} / ${prod.sales_unit}` : prod.sales_unit || "–"),
        fact("Unit price", eur(o.unit_price)),
        fact("Available", avail, coverCls),
        fact(qtyLabel, qtyText),
        fact(qtyLabel === "In plan" ? "Line total" : "Would cost", totalText),
        fact("Reliability", prod.delivery_reliability != null ? pct(prod.delivery_reliability) : "no data"),
        fact("CO₂e / kg", prod.co2_kg_per_kg != null ? `${num(prod.co2_kg_per_kg)} kg` : "no data")),
      (c.reasons || []).length ? h("ul", { class: "reasons" }, c.reasons.map((x) => h("li", null, x))) : null,
      planScreenEl(c, false),
      (c.flags || []).length || (prod.claims || []).length || (prod.labels || []).length ? h("div", { class: "row wrap" },
        (prod.labels || []).map(labelName).filter(Boolean).map((x) => h("span", { class: "flag label" }, x)),
        (prod.claims || []).map((x) => h("span", { class: "flag ok" }, claimLabel(x))), (c.flags || []).map(flagEl)) : null));
}

function verdictBanner(dec) {
  const v = VERDICT[dec.outcome] || { title: dec.outcome, icon: "?" };
  const title = dec.outcome === "needs_approval" && dec.approver_role ? `Needs approval by ${roleLabel(dec.approver_role)}` : v.title;
  const hits = [...(dec.hits || [])].sort((a, b) => rank(b.outcome) - rank(a.outcome));
  return h("section", { class: `verdict ${dec.outcome}` },
    h("div", { class: "verdict-top" },
      h("span", { class: "verdict-icon" }, v.icon),
      h("div", { class: "grow" }, h("div", { class: "verdict-title" }, title),
        h("div", { class: "muted small" }, `Policy ${dec.policy_id} · version ${dec.policy_version} · ${hits.filter((x) => x.outcome !== "allow").length} of ${hits.length} rules need attention`))),
    hits.length ? h("ul", { class: "hits" }, hits.map((x) => {
      const learned = x.learned || /learned|remembered/i.test(x.rule_id || "") || /^Allowed: you approved/i.test(x.message || "");
      return h("li", { class: `hit ${learned ? "learned" : ""}` },
        h("span", { class: `outcome ${x.outcome}` }, learned ? "Learned" : x.outcome === "needs_approval" ? "Needs approval" : x.outcome === "allow" ? "Pass" : "Reject"),
        h("span", null, x.message),
        h("span", { class: "rid" }, x.rule_id));
    })) : null);
}
const rank = (o) => ({ reject: 2, needs_approval: 1, allow: 0 }[o] ?? 0);

// ================================================================== approvals
async function refreshApprovals(quiet) {
  if (!quiet) { S.approvals.state = "loading"; renderApprovals(); }
  try {
    let items = await api.approvals("pending", { ask: false });
    // after the approver signs in (first inbox load), reload customers so approver ids become visible
    if (S.customers.some((c) => (c.approvers || []).some((a) => !a.id))) { try { await loadCustomers(); } catch { /* keep masked */ } }
    if (items && !Array.isArray(items)) items = items.items || items.approvals || [];
    items = (items || []).map((x) => (x.order ? x : { order: x, quote: null, decision: x.decision }));
    // Newest first: what needs attention now leads.
    items.sort((a, b) => new Date(b.order.created_at) - new Date(a.order.created_at));
    for (const it of items) if (!it.quote && it.order.quote_id) { try { it.quote = await api.getQuote(it.order.quote_id); } catch { /* shown as missing */ } }
    S.approvals = { state: "ok", items, error: null };
    if (!S.selected && items[0]) S.selected = items[0].order.order_id;
  } catch (e) {
    if (e.code === "signin_required" || e.code === "signin_cancelled") S.approvals = { state: "signin", items: [], error: null };
    else S.approvals = { ...S.approvals, state: "error", error: e.message };
  }
  updateCounts();
  renderApprovals();
}

function selectedItem() {
  return S.approvals.items.find((x) => x.order.order_id === S.selected) || S.decided.get(S.selected) || null;
}

function statusPill(status) {
  const map = { pending_approval: ["pending", "Pending approval"], approved: ["ok", "Approved"], placed: ["ok", "Placed"], rejected: ["bad", "Rejected"], expired: ["neutral", "Expired"], failed: ["bad", "Failed"] };
  const [cls, label] = map[status] || ["neutral", status];
  return h("span", { class: `status ${cls}` }, label);
}

function renderApprovals() {
  const v = $("view-approvals");
  const A = S.approvals;
  const parts = [];
  const decidedList = [...S.decided.values()].filter((x) => !A.items.find((y) => y.order.order_id === x.order.order_id));
  parts.push(h("div", { class: "row" },
    h("h2", { class: "grow" }, "Approval inbox"),
    !MOCK ? h("span", { class: "faint small" }, "Refreshes every 5 s") : null,
    h("button", { class: "btn btn-sm", onclick: () => refreshApprovals() }, A.state === "loading" ? [h("span", { class: "spinner" }), "Refreshing"] : "Refresh")));

  if (A.state === "signin") {
    parts.push(signInCard(selectedItem()?.order?.customer_id));
  } else if (A.state === "loading" && !A.items.length) {
    parts.push(h("div", { class: "card" }, [1, 2].map(() => h("div", { class: "inbox-row" }, h("div", { class: "skeleton", style: { height: "32px" } })))));
  } else if (A.state === "error") {
    parts.push(h("div", { class: "exc bad" }, h("b", null, "!"), h("div", null, h("b", null, "Could not load approvals. "), h("span", { class: "exc-body" }, A.error), " ",
      h("button", { class: "btn btn-sm", onclick: () => refreshApprovals() }, "Retry"))));
  } else if (!A.items.length && !decidedList.length) {
    parts.push(h("div", { class: "empty" }, h("strong", null, "Nothing waiting for approval"),
      h("span", null, "When an agent's order goes beyond its mandate (spend limit, substitution outside preference, unusual volume), it lands here with the exact signed quote. Orders inside the mandate are placed automatically."),
      !S.run ? h("button", { class: "btn btn-primary btn-sm", onclick: startRun }, "Run the example request") : null));
  }

  const rows = [...A.items, ...decidedList];
  if (rows.length) {
    parts.push(h("div", { class: "card inbox" }, rows.map((it) => {
      const o = it.order;
      const q = it.quote;
      const dec = it.decision || o.decision;
      const cust = S.customers.find((c) => c.id === o.customer_id);
      return h("div", { class: `inbox-row ${o.order_id === S.selected ? "sel" : ""}`, tabindex: 0, role: "button",
        onclick: () => { S.selected = o.order_id; S.decideError = null; renderApprovals(); },
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); S.selected = o.order_id; renderApprovals(); } } },
        h("div", { style: { minWidth: 0 } },
          h("div", { style: { fontWeight: 600 } }, cust?.name || o.customer_id, h("span", { class: "faint mono small" }, `  ${o.order_id}`)),
          h("div", { class: "muted small ellipsis" }, q ? q.lines.map((l) => `${num(l.qty_sales_units)} × ${l.product_name}`).join("; ") : "Quote not loaded",
            dec?.approver_role ? ` · for ${roleLabel(dec.approver_role)}` : "")),
        h("div", { class: "num mono", style: { fontWeight: 600 } }, q ? eur(q.total_net) : ""),
        o.status === "pending_approval" && q ? h("span", { class: "countdown", "data-expires": q.expires_at, title: "Price lock expires" }, countdown(q.expires_at).text) : h("span", { class: "faint small" }, relTime(o.updated_at || o.created_at)),
        statusPill(o.status));
    })));
  }

  const it = selectedItem();
  if (it) parts.push(approvalDetail(it));
  // The decision bar sticks to the bottom of the pane so Approve is always reachable on a 720p projector.
  if (it && S.requote && S.requote.order_id === it.order.order_id) parts.push(requoteCard(it));
  else if (it && it.order.status === "pending_approval" && it.quote) parts.push(decisionForm(it.order, it.quote));
  mount(v, parts);
  tick();
}

function approvalDetail(it) {
  const o = it.order;
  const q = it.quote;
  const dec = it.decision || o.decision;
  const pending = o.status === "pending_approval";
  const parts = [];
  if (!pending) parts.push(orderResponse(o, q));
  if (dec) parts.push(verdictBanner(dec));
  if (q) parts.push(quoteCard(q, pending));
  else parts.push(h("div", { class: "exc warn" }, h("b", null, "!"), h("div", null, "The signed quote for this order could not be loaded. Approval is disabled until it can be checked.")));
  return h("div", { style: { display: "flex", flexDirection: "column", gap: "16px" } }, parts);
}

function quoteCard(q, pending) {
  const sig = sigState(q);
  const sigCls = sig === "pending" ? "" : sig.state === "verified" ? "ok" : sig.state === "invalid" ? "bad" : "";
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("h3", { class: "grow" }, "Signed quote ", h("span", { class: "mono muted", style: { fontWeight: 500 } }, q.quote_id)),
      h("span", { class: "muted sm" }, `Delivery ${day(q.delivery_date)} · agent `, h("span", { class: "mono" }, q.agent_id)),
      pending ? h("span", { class: "sm" }, "Price lock ", h("span", { class: "countdown", "data-expires": q.expires_at }, countdown(q.expires_at).text)) : null),
    h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", null, "Product"), h("th", null, "Tier"), h("th", { class: "n" }, "Qty"), h("th", { class: "n" }, "Unit price"), h("th", { class: "n" }, "Line total"))),
      h("tbody", null, q.lines.map((l) => h("tr", null,
        h("td", null, h("div", { style: { fontWeight: 500 } }, l.product_name), h("div", { class: "faint small mono" }, l.product_id), (l.flags || []).length ? h("div", { class: "row wrap", style: { marginTop: "4px" } }, l.flags.map(flagEl)) : null),
        h("td", null, h("span", { class: `tier-tag ${l.tier}` }, TIER_LABEL[l.tier] || l.tier)),
        h("td", { class: "n" }, `${num(l.qty_sales_units)} ${l.sales_unit}`),
        h("td", { class: "n" }, eur(l.unit_price)),
        h("td", { class: "n" }, eur(l.line_total))))),
      h("tfoot", null,
        h("tr", null, h("td", { colspan: 4 }, "Total net"), h("td", { class: "n" }, eur(q.total_net))),
        q.total_gross != null ? h("tr", null, h("td", { colspan: 4, class: "muted" }, "Total incl. VAT"), h("td", { class: "n muted" }, eur(q.total_gross))) : null)),
    h("div", { class: "seal" },
      h("span", { class: `seal-mark ${sigCls}`, title: "Ed25519 signature" }, sig === "pending" ? "…" : sig.state === "verified" ? "✓" : sig.state === "invalid" ? "✕" : "?"),
      h("div", { style: { minWidth: 0 } },
        h("div", { class: "hash", title: q.quote_hash }, h("span", { class: "faint small", style: { marginRight: "8px" } }, "SHA-256"), groupedHash(q.quote_hash).map((g) => h("span", { class: "g" }, g)), h("span", { class: "faint" }, "…")),
        h("div", { class: "small muted" }, `Created ${full(q.created_at)} · expires ${hm(q.expires_at)}`)),
      sig === "pending" ? h("span", { class: "status neutral" }, "Verifying signature…")
        : sig.state === "verified" ? h("span", { class: "status ok", title: sig.detail }, "Signature verified")
        : sig.state === "invalid" ? h("span", { class: "status bad", title: sig.detail }, "Signature invalid")
        : h("span", { class: "status neutral", title: sig.detail }, "Signature not checked")),
    sig !== "pending" && sig.state !== "verified" ? h("div", { class: "card-body small muted" }, sig.detail) : null);
}

function sigState(q) {
  if (!q.quote_hash) return { state: "unverifiable", detail: "Quote has no hash" };
  const cur = S.sigs.get(q.quote_hash);
  if (cur) return cur;
  S.sigs.set(q.quote_hash, "pending");
  (async () => {
    try { if (!S.jwks) S.jwks = await api.jwks(); } catch (e) { S.sigs.set(q.quote_hash, { state: "unverifiable", detail: `JWKS unavailable: ${e.message}` }); renderApprovals(); return; }
    S.sigs.set(q.quote_hash, await verifyJws(q.signature, S.jwks, q.quote_hash));
    renderApprovals();
  })();
  return "pending";
}

function decisionForm(o, q) {
  const busy = S.deciding === o.order_id;
  const role = roleLabel((o.decision || {}).approver_role || "approver");
  // Approvals are bound to the signed-in human (POST /v1/auth/approver-session). The gateway records the decision
  // under that person only and refuses a different name (403 approver_mismatch), so there is no approver picker.
  const cust = S.customers.find((c) => c.id === o.customer_id);
  const dir = cust?.approvers || [];
  const needRole = (o.decision || {}).approver_role;
  const me = api.signedInApprover?.();
  const meEntry = me ? dir.find((a) => a.id === me || a.name === me) : null;
  const meAny = me ? directoryEntry(me) : null;
  const approver = me
    ? h("div", { class: "signed-as", id: "approver" },
        h("span", { class: "dot ok", "aria-hidden": "true" }), h("b", null, meAny?.name || me),
        meAny?.role ? h("span", { class: "muted small" }, roleLabel(meAny.role)) : null,
        h("button", { class: "btn btn-quiet btn-sm", disabled: busy, onclick: () => switchApprover() }, "Switch"))
    : h("button", { class: "btn btn-sm", id: "approver", onclick: () => openSignIn().then(() => renderApprovals()) }, lockIcon(), "Sign in as approver");
  const roleWarn = !me ? null
    : !meEntry && dir.length ? `You are not in ${cust?.name || o.customer_id}'s approver directory; the gateway will refuse this decision.`
    : meEntry && needRole && meEntry.role !== needRole ? `The policy asks for a ${roleLabel(needRole)}; you are signed in as ${roleLabel(meEntry.role)}. The gateway checks the role.` : null;
  const note = h("input", { class: "input", id: "note", placeholder: "Optional, stored in the audit trail", oninput: (e) => (S.note = e.target.value), disabled: busy });
  note.value = S.note;
  const expired = new Date(q.expires_at) < new Date();
  const isSwap = (q.lines || []).some((l) => (l.tier && l.tier !== "preferred") || (l.flags || []).some((f) => /preference|substitution/.test(f)))
    || (o.decision?.hits || []).some((x) => /substitution/.test(x.rule_id || "") && x.outcome !== "allow");
  const remember = isSwap ? h("div", { class: "remember" },
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: S.remember, disabled: busy, onchange: (e) => { S.remember = e.target.checked; renderApprovals(); } }),
      " Always allow this swap"),
    h("select", { class: "input input-sm", disabled: busy || !S.remember, onchange: (e) => (S.rememberScope = e.target.value) },
      h("option", { value: "product", selected: S.rememberScope === "product" }, "for this product"),
      h("option", { value: "product_type", selected: S.rememberScope === "product_type" }, "for any product of this type")),
    h("span", { class: "faint small" }, "Next time the policy shows “Allowed: you approved this swap” and the agent does not ask.")) : null;
  return h("div", { class: "card decide-sticky" },
    h("div", { class: "card-body decide" },
      h("h3", { style: { gridColumn: "1 / -1" } }, `Decision as ${role}`),
      h("div", { class: "field" }, h("label", { for: "approver" }, "Deciding as"), approver),
      h("div", { class: "field" }, h("label", { for: "note" }, "Note"), note),
      remember,
      roleWarn ? h("div", { class: "inline-warn small", style: { gridColumn: "1 / -1" } }, roleWarn) : null,
      S.voice && S.voice.order_id === o.order_id ? voiceCard(o, q) : null,
      S.decideError ? h("div", { class: "inline-error", style: { gridColumn: "1 / -1" } }, S.decideError) : null,
      h("div", { class: "decide-actions" },
        h("span", { class: "binding-note" }, "Your decision is bound to quote hash ", h("span", { class: "mono" }, shortHash(q.quote_hash)), ". If the price or lines change, it no longer applies."),
        h("button", { class: "btn btn-voice", disabled: busy || expired || voiceBusy(), title: "Hear the order read out, answer approve or reject, then confirm on screen", onclick: () => startVoice(o, q) }, micIcon(), "Decide by voice"),
        h("button", { class: "btn btn-bad", disabled: busy, onclick: () => decide(o, q, "reject") }, "Reject"),
        h("button", { class: `btn btn-lg ${expired ? "btn-primary" : "btn-ok"}`, disabled: busy,
          title: expired ? "The price lock has expired. The gateway re-prices the same lines; you review the difference before ordering." : "",
          onclick: () => decide(o, q, "approve") }, busy ? [h("span", { class: "spinner" }), expired ? "Re-pricing" : "Placing order"] : expired ? "Price lock expired: re-price and review" : `Approve ${eur(q.total_net)}`))));
}

async function decide(o, q, decision, voiceNote) {
  S.deciding = o.order_id;
  S.decideError = null;
  renderApprovals();
  try {
    const remember = decision === "approve" && S.remember ? { remember: true, remember_scope: S.rememberScope } : {};
    const note = [voiceNote, S.note.trim()].filter(Boolean).join(" · ");
    const order = await api.decide(o.order_id, { decision, note: note || undefined, quote_hash: q.quote_hash, ...remember });
    S.voice = null;
    renderApproverChip();
    if (remember.remember) { S.remember = false; loadLearned(o.customer_id, true); }
    if (S.requote && (S.requote.order_id === o.order_id || S.requote.new_order_id === o.order_id)) S.requote = null;
    const dec = order.decision || o.decision;
    S.decided.set(order.order_id, { order, quote: q, decision: dec });
    if (S.run && S.run.order && S.run.order.order_id === order.order_id) { S.run.order = order; }
    S.note = "";
    S.selected = order.order_id;
    S.deciding = null;
    await refreshApprovals(true);
    document.getElementById("order-response")?.scrollIntoView({ behavior: "smooth", block: "start" });
    loadAudit(true);
  } catch (e) {
    S.deciding = null;
    const det = (e.body && typeof e.body.detail === "object" ? e.body.detail : null) || e.body || {};
    const fresh = det.new_quote || det.fresh_quote || det.quote;
    if (e.code === "requote_needed" || fresh) {
      // Prices moved since the quote was signed: show old vs new and let the approver approve the fresh quote in one click.
      S.requote = { order_id: o.order_id, old: q, fresh: fresh || null, new_order_id: det.new_order?.order_id || det.new_order_id || det.order_id || o.order_id,
        changes: det.changes || null, why: det.reason || null, reason: e.body?.message || e.message };
      S.voice = null;
      if (!fresh && (det.new_quote_id || det.quote_id)) { try { S.requote.fresh = await api.getQuote(det.new_quote_id || det.quote_id); } catch { /* shown as missing */ } }
      renderApprovals();
      return;
    }
    if (e.code === "signin_cancelled") { S.decideError = "Sign in as an approver to decide. Agents can never approve."; renderApprovals(); return; }
    S.decideError = e.status === 409 ? `The gateway refused: ${e.message}. The quote may have changed; ask the agent for a new quote.` : e.message;
    renderApprovals();
  }
}

function requoteCard(it) {
  const R = S.requote;
  const old = R.old || it.quote;
  const fresh = R.fresh;
  const busy = !!S.deciding;
  const byId = new Map((fresh?.lines || []).map((l) => [l.product_id, l]));
  const ids = [...new Set([...(old.lines || []).map((l) => l.product_id), ...byId.keys()])];
  const oldBy = new Map((old.lines || []).map((l) => [l.product_id, l]));
  const delta = fresh ? fresh.total_net - old.total_net : null;
  const diffCell = (a, b, fmt) => a === b || b == null ? h("span", null, a == null ? "–" : fmt(a)) : h("span", null, h("s", { class: "faint" }, a == null ? "–" : fmt(a)), " ", h("b", { class: b > a ? "up" : "down" }, fmt(b)));
  return h("div", { class: "card requote decide-sticky" },
    h("div", { class: "card-head" }, h("h3", { class: "grow" },
      R.why === "cutoff_passed" ? "Cut-off passed while waiting: re-planned"
        : R.why === "line_unavailable" ? "A line became unavailable: re-planned"
        : R.why === "partial_stock" ? "Stock fell below quoted quantity: re-planned"
        : delta ? "Prices changed: re-quote needed" : R.why === "quote_expired" ? "Price lock expired: fresh quote, same price" : "Re-quote needed"),
      delta != null ? h("span", { class: `delta ${delta > 0 ? "up" : "down"}` }, `${delta > 0 ? "+" : ""}${eur(delta)} (${num((delta / old.total_net) * 100, 1)}%)`) : null),
    h("div", { class: "card-body sm muted" }, R.reason, " Your approval was bound to the old quote hash, so it did not apply. Nothing was ordered."),
    h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", null, "Product"), h("th", { class: "n" }, "Qty"), h("th", { class: "n" }, "Unit price"), h("th", { class: "n" }, "Line total"))),
      h("tbody", null, ids.map((id) => {
        const a = oldBy.get(id), b = byId.get(id);
        return h("tr", null, h("td", null, (a || b).product_name, h("div", { class: "faint small mono" }, id, !b ? " · removed" : !a ? " · new" : "")),
          h("td", { class: "n" }, diffCell(a?.qty_sales_units, b?.qty_sales_units, (x) => num(x))),
          h("td", { class: "n" }, diffCell(a?.unit_price, b?.unit_price, eur)),
          h("td", { class: "n" }, diffCell(a?.line_total, b?.line_total, eur)));
      })),
      h("tfoot", null, h("tr", null, h("td", { colspan: 3 }, "Total net"), h("td", { class: "n" }, diffCell(old.total_net, fresh?.total_net, eur))))),
    h("div", { class: "card-body row" },
      h("span", { class: "binding-note grow" }, fresh ? ["New signed quote ", h("span", { class: "mono" }, fresh.quote_id), " · hash ", h("span", { class: "mono" }, shortHash(fresh.quote_hash)), ` · locked until ${hm(fresh.expires_at)}`] : "The gateway did not return the fresh quote; ask the agent to re-quote."),
      h("button", { class: "btn btn-sm", disabled: busy, onclick: () => { S.requote = null; renderApprovals(); } }, "Back"),
      fresh ? h("button", { class: "btn btn-ok btn-lg", disabled: busy, onclick: () => decide({ ...it.order, order_id: R.new_order_id, quote_id: fresh.quote_id, quote_hash: fresh.quote_hash }, fresh, "approve") },
        busy ? [h("span", { class: "spinner" }), "Placing order"] : `Approve new quote ${eur(fresh.total_net)}`) : null));
}

function approverName(customerId, raw, roleField) {
  // Old shape: "anna@x.example (procurement_manager)"; new shape: plain id + Order.approver_role.
  const m = String(raw || "").match(/^(.*?)\s*\(([^()]+)\)\s*$/);
  const id = m ? m[1] : raw;
  const role = roleField || (m ? m[2] : null);
  const a = (S.customers.find((c) => c.id === customerId)?.approvers || []).find((x) => x.id === id || x.name === id);
  const r = role || a?.role;
  if (a) return `${a.name} (${roleLabel(r)}, ${a.id})`;
  return r ? `${id} (${roleLabel(r)})` : id;
}

function orderResponse(o, q) {
  const placed = o.status === "placed" || o.status === "approved";
  const names = new Map((q?.lines || []).map((l) => [l.product_id, l.product_name]));
  return h("div", { class: "card", id: "order-response" },
    h("div", { class: "card-head" },
      h("h3", { class: "grow" }, placed ? "Order response" : "Order not placed"),
      statusPill(o.status),
      o.response_code ? h("span", { class: `resp-code ${o.response_code}`, title: "Peppol BIS order response code" }, o.response_code) : null),
    h("div", { class: "card-body sm" },
      o.approved_by ? h("div", null, `${o.status === "rejected" ? "Rejected" : "Approved"} by `, h("strong", null, approverName(o.customer_id, o.approved_by, o.approver_role)), ` · ${dayTime(o.updated_at)}`, o.approval_note ? ` · “${o.approval_note}”` : "") : h("div", { class: "muted" }, o.status === "placed" ? "Placed automatically within the mandate." : ""),
      !placed && o.status === "rejected" ? h("div", { class: "muted" }, "Nothing was sent to the warehouse. The agent was told the order was rejected.") : null),
    (o.response_lines || []).length ? h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", null, "Code"), h("th", null, "Product"), h("th", { class: "n" }, "Confirmed"), h("th", null, "Note"))),
      h("tbody", null, o.response_lines.map((l) => h("tr", null,
        h("td", null, h("span", { class: `resp-code ${l.code}` }, l.code), h("span", { class: "muted small" }, { AP: " Accepted", CA: " Accepted with changes", RE: " Rejected" }[l.code] || "")),
        h("td", null, names.get(l.product_id) || l.product_id, h("div", { class: "faint small mono" }, l.product_id)),
        h("td", { class: "n" }, num(l.qty_confirmed)),
        h("td", { class: "muted" }, l.note || ""))))) : null,
    h("div", { class: "card-body row" }, h("span", { class: "grow muted small" }, "Peppol BIS Order Response semantics: AP accepted, CA accepted with changes, RE rejected."),
      h("button", { class: "btn btn-sm btn-primary", onclick: () => { S.audit.scope = "flow"; setTab("audit"); } }, "View audit trail →")));
}

// ================================================================== audit
function flowSubjects() {
  const r = S.run;
  const ids = new Set();
  if (r?.id) ids.add(r.id);
  if (r?.quote?.quote_id) ids.add(r.quote.quote_id);
  if (r?.order?.order_id) ids.add(r.order.order_id);
  const it = selectedItem();
  if (it && (!r || it.order.order_id === r?.order?.order_id || !r.order)) { ids.add(it.order.order_id); ids.add(it.order.quote_id); }
  return [...ids].filter(Boolean);
}

let auditSeq = 0;
async function loadAudit(quiet) {
  const my = ++auditSeq;
  const subjects = flowSubjects();
  const scope = S.audit.scope === "flow" && subjects.length ? "flow" : "all";
  if (!quiet || !S.audit.events.length) { S.audit.state = "loading"; renderAudit(); }
  try {
    let evs;
    if (scope === "flow") {
      const lists = await Promise.all(subjects.map((s) => api.audit(s).catch(() => [])));
      const m = new Map();
      lists.flat().forEach((e) => m.set(e.seq, e));
      evs = [...m.values()];
    } else evs = await api.audit();
    if (my !== auditSeq) return;
    evs.sort((a, b) => a.seq - b.seq);
    S.audit = { ...S.audit, state: "ok", events: evs, error: null, effectiveScope: scope };
  } catch (e) {
    if (my !== auditSeq) return;
    S.audit = { ...S.audit, state: "error", error: e.message };
  }
  updateCounts();
  renderAudit();
}

async function verifyChain() {
  S.audit.verifying = true;
  renderAudit();
  try { S.audit.verify = await api.verify(); }
  catch (e) { S.audit.verify = { ok: false, error: e.message }; }
  S.audit.verifying = false;
  renderAudit();
  const b = S.audit.verify && !S.audit.verify.ok && S.audit.verify.broken_at;
  if (b) requestAnimationFrame(() => document.getElementById(`ev-${b}`)?.scrollIntoView({ behavior: "smooth", block: "center" }));
}

// Demo tamper: edit the human approval event in the stored ledger, verify (red), restore, verify (green).
// Live uses POST /v1/demo/tamper/{seq} and /v1/demo/untamper/{seq} (404 when AIMO_ENV=production).
async function tamperToggle() {
  const A = S.audit;
  A.tamperBusy = true; A.tamperError = null;
  renderAudit();
  try {
    if (A.tampered == null) {
      const evs = A.events;
      const target = evs.find((e) => actorKind(e.actor) === "human" && /approv/.test(e.action)) || evs.find((e) => /approv|policy/.test(e.action)) || evs[Math.floor(evs.length / 2)];
      if (!target) throw new Error("no audit events to tamper with yet");
      await api.tamper(target.seq);
      A.tampered = target.seq;
    } else {
      await api.untamper(A.tampered);
      A.tampered = null;
    }
    A.tamperBusy = false;
    await loadAudit(true);
    await verifyChain();
  } catch (e) {
    A.tamperBusy = false;
    if (e.status === 404 && A.tampered == null) { S.demoTamper = false; A.tamperError = "Tamper demo is disabled on this gateway (production mode)."; }
    else A.tamperError = e.message;
    renderAudit();
  }
}

async function evidencePack() {
  const r = S.run || {};
  let verify = S.audit.verify;
  try { verify = await api.verify(); } catch { /* keep last */ }
  let jwks = S.jwks;
  try { jwks = jwks || (await api.jwks()); } catch { /* optional */ }
  const it = selectedItem();
  const pack = {
    kind: "valio-aimo-trusted-trade/evidence-pack", version: 1, generated_at: new Date().toISOString(),
    gateway: api.base, mode: api.mode, subject_ids: flowSubjects(),
    request: { text: r.need?.raw_text || S.text, customer_id: S.customerId, model: S.model, thinking: S.thinking },
    need: r.need || null, plan: r.plan || null, quote: r.quote || it?.quote || null,
    policy_decision: r.decision || it?.decision || null, order: (it && it.order) || r.order || null,
    agent_steps: r.steps || [], audit_events: S.audit.events, chain_verification: verify, jwks,
    how_to_verify: "Each audit event's hash is SHA-256 over its canonical JSON with prev_hash; signature is a compact JWS (EdDSA) over hash, verifiable with the JWKS above.",
  };
  const id = pack.order?.order_id || r.id || "ledger";
  download(`evidence-${id}.json`, JSON.stringify(pack, null, 2));
}

async function openReport() {
  const id = S.run?.order?.order_id || selectedItem()?.order.order_id || S.run?.id;
  if (!id) return;
  if (api.reportUrl) { window.open(api.reportUrl(id), "_blank", "noopener"); return; }
  const md = await api.report(id);
  const url = URL.createObjectURL(new Blob([md], { type: "text/plain;charset=utf-8" }));
  window.open(url, "_blank", "noopener");
}

function renderAudit() {
  const v = $("view-audit");
  const A = S.audit;
  const subjects = flowSubjects();
  const scope = A.scope === "flow" && subjects.length ? "flow" : "all";
  const vr = A.verify;
  const parts = [];
  parts.push(h("div", { class: "row wrap" },
    h("div", { class: "grow" }, h("h2", null, "Signed audit trail"),
      h("div", { class: "muted small" }, scope === "flow" ? ["This flow: ", subjects.map((s, i) => [i ? ", " : "", h("span", { class: "mono" }, s)])] : "Whole ledger, every agent and every customer")),
    h("div", { class: "row" },
      h("button", { class: `btn btn-sm ${scope === "flow" ? "btn-primary" : ""}`, disabled: !subjects.length, onclick: () => { S.audit.scope = "flow"; loadAudit(); } }, "This flow"),
      h("button", { class: `btn btn-sm ${scope === "all" ? "btn-primary" : ""}`, onclick: () => { S.audit.scope = "all"; loadAudit(); } }, "Whole ledger"))));
  parts.push(h("div", { class: "card card-body audit-tools" },
    h("button", { class: "btn", onclick: verifyChain, disabled: A.verifying }, A.verifying ? [h("span", { class: "spinner" }), "Verifying"] : "Verify chain"),
    vr ? (vr.ok
      ? h("span", { class: "verify-result ok" }, `✓ Chain intact · ${num(vr.checked, 0)} events, hashes and signatures checked`)
      : h("span", { class: "verify-result bad" }, vr.error ? `✕ Verification failed: ${vr.error}` : `✕ Chain broken at event #${vr.broken_at} (${num(vr.checked, 0)} checked)`)) : h("span", { class: "muted sm" }, "Recomputes every hash and checks every link and signature on the server."),
    h("span", { class: "grow" }),
    S.demoTamper && A.events.length ? h("button", { class: `btn btn-sm ${A.tampered != null ? "btn-ok" : "btn-bad"}`, disabled: A.tamperBusy,
      title: "Demo only: alter one stored audit event, then verify the chain", onclick: tamperToggle },
      A.tamperBusy ? h("span", { class: "spinner" }) : null, A.tampered != null ? `Restore event #${A.tampered}` : "Simulate tampering") : null,
    A.tamperError ? h("span", { class: "inline-error small" }, A.tamperError) : null,
    h("button", { class: "btn btn-sm", onclick: openReport, disabled: !subjects.length }, "Readable report"),
    h("button", { class: "btn btn-sm btn-primary", onclick: evidencePack, disabled: !A.events.length }, "Download evidence pack")));

  if (A.state === "loading" && !A.events.length) {
    parts.push(h("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } }, [1, 2, 3, 4].map(() => h("div", { class: "skeleton", style: { height: "64px" } }))));
  } else if (A.state === "error") {
    parts.push(h("div", { class: "exc bad" }, h("b", null, "!"), h("div", null, h("b", null, "Could not load the audit trail. "), h("span", { class: "exc-body" }, A.error), " ", h("button", { class: "btn btn-sm", onclick: () => loadAudit() }, "Retry"))));
  } else if (!A.events.length) {
    parts.push(h("div", { class: "empty" }, h("strong", null, "No audit events yet"),
      h("span", null, "Every step that matters (request, live price lookup, plan, signed quote, policy verdict, human decision, order response) is appended here as a hash-chained, signed event."),
      !S.run ? h("button", { class: "btn btn-primary btn-sm", onclick: startRun }, "Run the example request") : null));
  } else {
    const evs = A.events;
    parts.push(h("ol", { class: "chain" }, evs.map((e, i) => {
      const prev = evs[i - 1];
      const linked = prev && e.prev_hash === prev.hash;
      const kind = actorKind(e.actor);
      const glyph = { agent: "A", human: "H", system: "S" }[kind];
      const hd = h("details", { class: "json keep" }, h("summary", null, "Hash, link and signature"),
        h("div", { class: "chain-hashes" },
          h("b", null, "hash"), h("span", null, e.hash),
          h("b", null, "prev_hash"), h("span", null, e.prev_hash, linked ? h("span", { style: { color: "var(--ok)" } }, "  ✓ matches #" + prev.seq) : ""),
          h("b", null, "signature"), h("span", null, String(e.signature || "").slice(0, 96) + (String(e.signature || "").length > 96 ? "…" : "")),
          h("b", null, "kid"), h("span", null, e.kid)),
        Object.keys(e.data || {}).length ? jsonDetails("Event data", e.data) : null);
      return h("li", { class: `chain-ev ${linked ? "linked" : ""}`, id: `ev-${e.seq}` },
        h("div", { class: "chain-time", title: full(e.ts) }, time(e.ts), h("div", { class: "faint" }, `#${e.seq}`)),
        h("div", { class: "chain-rail" }, h("span", { class: `chain-node ${kind}`, title: kind }, glyph)),
        h("div", { class: `chain-card ${vr && !vr.ok && vr.broken_at === e.seq ? "broken" : ""}` },
          h("div", { class: "who" }, h("span", { class: "mono" }, e.actor), h("span", { class: "act" }, e.action), h("span", { class: "faint mono" }, shortHash(e.hash, 10))),
          h("div", { class: "sum" }, e.summary),
          hd));
    })));
  }
  mount(v, parts);
}

// ================================================================== connect dialog
function openConnect() {
  const dlg = $("connect-dialog");
  const origin = api.mode === "live" ? api.base : location.origin;
  const c = S.customers.find((x) => x.id === S.customerId) || { id: S.customerId, agent_id: `${S.customerId}/buyer-1` };
  const remote = { mcpServers: { "valio-aimo-trusted-trade": { type: "http", url: `${origin}/mcp`, headers: { "X-Agent-Id": c.agent_id, "X-Customer-Id": c.id } } } };
  const stdio = { mcpServers: { "valio-aimo-trusted-trade": { command: "uv", args: ["run", "python", "-m", "aimo.gateway.mcp_server"], env: { AIMO_AGENT_ID: c.agent_id, AIMO_CUSTOMER_ID: c.id } } } };
  const curl = `curl -s ${origin}/v1/products/search?q=lactose-free%20milk \\\n  -H "X-Agent-Id: ${c.agent_id}" -H "X-Customer-Id: ${c.id}"`;
  const block = (title, text) => {
    const pre = h("pre", { class: "code", style: { maxHeight: "200px" } }, text);
    const btn = h("button", { class: "btn btn-sm", onclick: async () => { try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; } catch { btn.textContent = "Select and copy"; } setTimeout(() => (btn.textContent = "Copy"), 1500); } }, "Copy");
    return h("div", null, h("div", { class: "row" }, h("span", { class: "section-label grow" }, title), btn), pre);
  };
  mount(dlg, h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Connect your own agent"), h("button", { class: "btn btn-quiet btn-sm", onclick: () => dlg.close() }, "Close")),
    h("div", { class: "card-body" },
      h("p", { class: "sm muted" }, "Any MCP-capable agent can use the same tools as the reference Gemini buyer agent: search_products, get_offers, plan_need, create_quote, place_order, get_order_status, get_audit_trail, list_policies. Every call runs under the customer's mandate and lands in this audit trail."),
      block("MCP, streamable HTTP", JSON.stringify(remote, null, 2)),
      block("MCP, stdio (local checkout)", JSON.stringify(stdio, null, 2)),
      block("REST (OpenAPI 3.1)", curl),
      h("div", { class: "row wrap sm" }, h("a", { href: api.links.docs, target: "_blank" }, "OpenAPI docs"), "·", h("a", { href: api.links.otap, target: "_blank" }, "Discovery manifest /.well-known/otap.json"), "·", h("a", { href: api.links.jwks, target: "_blank" }, "JWKS (verify signatures)"))));
  dlg.showModal();
}

// ================================================================== ticker
function tick() {
  document.querySelectorAll("[data-expires]").forEach((el) => {
    const c = countdown(el.dataset.expires);
    el.textContent = c.text;
    el.className = `countdown ${c.cls}`;
  });
}

boot().catch((e) => banner(`The console failed to start: ${e.message}`));
