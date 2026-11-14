// Live client for the gateway. Endpoints are from docs/interfaces.md.
// Base URL: same origin (UI served at /ui), or ?api=http://host:port for a separate preview server.

const KINDS = ["thought", "tool_call", "tool_result", "plan", "quote", "decision", "approval_wait", "order", "error", "final",
  "needs_clarification", "extracted_lines"];

export function createLiveApi(base) {
  base = (base || "").replace(/\/$/, "");
  const url = (p) => base + p;
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  let spec = null; // cached /openapi.json, used to feature-detect optional routes

  // Human approver sign-in. Approvals need a human session (X-Approver-Session), which no agent ever receives.
  // The app supplies a sign-in panel (setSignInHandler): name + PIN -> POST /v1/auth/approver-session -> an 8-hour
  // session token bound to that one person. Only the token is kept (sessionStorage); the PIN is never stored.
  // Production: the customer's SSO replaces the shared demo PIN.
  const SKEY = "aimoApproverSession";
  let signInHandler = null;
  const store = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
  };
  const sessionHeader = () => { const s = store.get(SKEY); return s ? { "X-Approver-Session": s } : {}; };
  function signedInApprover() { return store.get(SKEY) ? store.get(SKEY + ".who") : null; }
  async function signIn(approver, pin) {
    const r = await req("POST", "/v1/auth/approver-session", { approver }, { "X-Approver-Pin": pin });
    store.set(SKEY, r.session); store.set(SKEY + ".who", r.approver);
    store.set(SKEY + ".exp", r.expires_at || null);
    return r;
  }
  function signOut() { store.set(SKEY, null); store.set(SKEY + ".who", null); store.set(SKEY + ".exp", null); }
  async function ensureSession(ask, reason) {
    let s = store.get(SKEY);
    if (s) return s;
    if (!ask || !signInHandler) { const e = new Error("Sign in as an approver to continue."); e.status = 401; e.code = "signin_required"; throw e; }
    await signInHandler(reason);            // resolves after a successful signIn(), rejects on cancel
    s = store.get(SKEY);
    if (!s) { const e = new Error("Approver sign-in cancelled."); e.status = 401; e.code = "signin_cancelled"; throw e; }
    return s;
  }
  async function humanReq(method, path, body, opts = {}) {
    const ask = opts.ask !== false;
    try {
      return await req(method, path, body, { "X-Approver-Session": await ensureSession(ask, opts.reason) });
    } catch (e) {
      if (e.status !== 401 || e.code === "signin_required" || e.code === "signin_cancelled") throw e;
      signOut();                            // expired or revoked: sign in again once
      return req(method, path, body, { "X-Approver-Session": await ensureSession(ask, "Your approver session expired. Sign in again.") });
    }
  }

  async function req(method, path, body, extraHeaders) {
    let res;
    try {
      res = await fetch(url(path), { method, headers: { ...headers, ...(extraHeaders || {}) }, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw new Error(`Gateway unreachable at ${base || location.origin} (${e.message})`);
    }
    if (!res.ok) {
      let j = null;
      try { j = await res.json(); } catch { /* not json */ }
      const msg = j ? (j.message || (typeof j.detail === "string" ? j.detail : j.detail?.message) || JSON.stringify(j.detail || j)) : "";
      const err = new Error(`${method} ${path} failed: ${res.status}${msg ? " · " + msg : ""}`);
      err.status = res.status;
      err.code = j?.error || (typeof j?.detail === "object" ? j.detail?.error : undefined);
      err.body = j;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
  }

  async function hasRoute(prefix) {
    try {
      spec = spec || (await req("GET", "/openapi.json"));
      return Object.keys(spec.paths || {}).some((p) => p.startsWith(prefix));
    } catch { return false; }
  }

  return {
    mode: "live",
    base: base || location.origin,
    links: {
      docs: url("/docs"),
      otap: url("/.well-known/otap.json"),
      jwks: url("/.well-known/jwks.json"),
      mcp: url("/mcp"),
    },
    hasRoute,
    // approver ids are only returned to a signed-in approver: send the session if we have one (never asks)
    customers: () => req("GET", "/v1/customers", null, sessionHeader()),
    /** image {mime, data_b64, name} rides along as image_b64/image_mime (RunRequest). Optimize is a browser-side re-rank. */
    startRun: ({ text, customer_id, model, thinking, image }) =>
      req("POST", "/v1/agent/runs", {
        text: text || "", customer_id, model, thinking: thinking ? String(thinking).toUpperCase() : undefined,
        image_b64: image?.data_b64 || undefined, image_mime: image?.mime || undefined,
      }, { "X-Customer-Id": customer_id }),
    /** Answer a needs_clarification step: {option_id?, text?, question_id?}. */
    answer: (runId, body) => req("POST", `/v1/agent/runs/${encodeURIComponent(runId)}/answer`, body),

    /** Subscribe to the SSE AgentStep stream. Returns a close() function. */
    streamRun(runId, onStep, onEnd) {
      const es = new EventSource(url(`/v1/agent/runs/${encodeURIComponent(runId)}/events`));
      const seen = new Set();
      let closed = false;
      const close = () => { if (!closed) { closed = true; es.close(); } };
      const handle = (ev) => {
        if (!ev.data) return;
        let step;
        try { step = JSON.parse(ev.data); } catch { return; }
        if (!step || typeof step !== "object") return;
        const key = step.seq ?? JSON.stringify(step);
        if (seen.has(key)) return; // EventSource reconnects replay the stream
        seen.add(key);
        onStep(step);
        if (step.kind === "final") { close(); onEnd && onEnd(); }
      };
      es.onmessage = handle;
      for (const k of [...KINDS, "step", "agent_step"]) es.addEventListener(k, handle);
      for (const k of ["end", "done", "close"]) es.addEventListener(k, () => { close(); onEnd && onEnd(); });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { close(); onEnd && onEnd({ error: "stream closed" }); }
      };
      return close;
    },

    /** Inbox. Asks the human to sign in unless {ask:false}, in which case it throws 401 when signed out. */
    approvals: (status = "pending", opts = {}) => humanReq("GET", `/v1/approvals?status=${encodeURIComponent(status)}`, null, opts),
    /** body: {decision, quote_hash, note?, remember?, remember_scope?}. The session is bound to one approver, so the
     *  decision is always recorded under the signed-in person (the gateway refuses any other name with 403). */
    decide: (orderId, body) => humanReq("POST", `/v1/approvals/${encodeURIComponent(orderId)}/decision`,
      { ...body, approver: signedInApprover() || undefined, approver_role: undefined }),
    signIn, signOut, signedInApprover, setSignInHandler: (fn) => { signInHandler = fn; },
    getOrder: (id) => req("GET", `/v1/orders/${encodeURIComponent(id)}`),
    getQuote: (id) => req("GET", `/v1/quotes/${encodeURIComponent(id)}`),
    /** Learned swaps ({customer_id, available, swaps}); hidden when available is false. */
    learned: (customerId) => req("GET", `/v1/customers/${encodeURIComponent(customerId)}/learned`),
    audit: (subjectId) => req("GET", subjectId ? `/v1/audit?subject_id=${encodeURIComponent(subjectId)}` : "/v1/audit"),
    verify: () => req("GET", "/v1/audit/verify"),
    report: (subjectId) => req("GET", `/v1/audit/report/${encodeURIComponent(subjectId)}`),
    reportUrl: (subjectId) => url(`/v1/audit/report/${encodeURIComponent(subjectId)}`),
    jwks: () => req("GET", "/.well-known/jwks.json"),
    // Port additions (interfaces.md): recipes, drift, seller proposals.
    recipes: () => req("GET", "/v1/recipes"),
    planRecipe: (body) => req("POST", "/v1/recipes/plan", body),
    drift: (customerId) => req("GET", `/v1/drift/${encodeURIComponent(customerId)}`),
    propose: (customerId) => req("POST", "/v1/proposals", { customer_id: customerId, note: "Requested from the Valio Aimo ops console" }, { "X-Agent-Id": "valio-aimo/seller-1" }),
    proposals: (customerId) => req("GET", customerId ? `/v1/proposals?customer_id=${encodeURIComponent(customerId)}` : "/v1/proposals"),
    /** Quote + order a recipe plan's quote_request as the customer's own agent (only that agent may submit its quote). */
    async orderQuoteRequest(qr, customerId, agentId) {
      const hdr = { "X-Customer-Id": customerId, "X-Agent-Id": agentId };
      const quote = await req("POST", "/v1/quotes", { ...qr, customer_id: customerId }, hdr);
      const order = await req("POST", "/v1/orders", { quote_id: quote.quote_id, quote_hash: quote.quote_hash }, hdr);
      return { quote, order };
    },
    // Demo-only tamper routes (gateway returns 404 when AIMO_ENV=production). Shown only if listed in the OpenAPI.
    demoAvailable: () => hasRoute("/v1/demo/tamper"),
    tamper: (seq) => humanReq("POST", `/v1/demo/tamper/${encodeURIComponent(seq)}`),
    untamper: (seq) => humanReq("POST", `/v1/demo/untamper/${encodeURIComponent(seq)}`),
    otap: () => req("GET", "/.well-known/otap.json"),
    // Voice approval helpers (aimo/voice). They never decide: the confirm click goes through decide() above.
    voiceStatus: () => req("GET", "/v1/voice/status"),
    voicePrompt: (orderId) => humanReq("POST", "/v1/voice/prompt", { order_id: orderId }),
    voiceIntent: (orderId, transcript) => humanReq("POST", "/v1/voice/intent", { order_id: orderId, transcript }),
    async voiceTranscribe(blob) {
      const fd = new FormData();
      fd.append("audio", blob, "answer.webm");
      const res = await fetch(url("/v1/voice/transcribe"), { method: "POST", body: fd, headers: { Accept: "application/json", ...sessionHeader() } });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) { const e = new Error(j.message || `transcribe failed: ${res.status}`); e.status = res.status; e.code = j.error; throw e; }
      return j;
    },
  };
}
