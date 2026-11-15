// Live client for the gateway. Endpoints are from docs/interfaces.md. Fields marked PROPOSED are what the UI
// sends/reads for Swarm C features whose backend contract is not in interfaces.md yet (see docs/reports/frontend.md).
// Base URL: same origin (UI served at /ui), or ?api=http://host:port for a separate preview server.

const KINDS = ["thought", "tool_call", "tool_result", "plan", "quote", "decision", "approval_wait", "order", "error", "final",
  "needs_clarification", "extracted_lines"];

export function createLiveApi(base) {
  base = (base || "").replace(/\/$/, "");
  const url = (p) => base + p;
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  let spec = null; // cached /openapi.json, used to feature-detect optional routes

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
    customers: () => req("GET", "/v1/customers"),
    /** PROPOSED: image {mime, data_b64, name} and optimize (cheapest|greenest|reliable) ride along with the run. */
    startRun: ({ text, customer_id, model, thinking, image, optimize }) =>
      req("POST", "/v1/agent/runs", {
        text, customer_id, model, thinking: thinking ? String(thinking).toUpperCase() : undefined,
        image: image || undefined, optimize: optimize && optimize !== "agent" ? optimize : undefined,
      }, { "X-Customer-Id": customer_id }),
    /** PROPOSED: answer a needs_clarification step. */
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

    approvals: (status = "pending") => req("GET", `/v1/approvals?status=${encodeURIComponent(status)}`),
    /** body: {decision, approver, approver_role?, note?, quote_hash, remember? (PROPOSED), remember_scope? (PROPOSED)} */
    decide: (orderId, body) => req("POST", `/v1/approvals/${encodeURIComponent(orderId)}/decision`, body),
    getOrder: (id) => req("GET", `/v1/orders/${encodeURIComponent(id)}`),
    getQuote: (id) => req("GET", `/v1/quotes/${encodeURIComponent(id)}`),
    /** PROPOSED: learned swaps for a customer. 404 -> feature hidden. */
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
    tamper: (seq) => req("POST", `/v1/demo/tamper/${encodeURIComponent(seq)}`),
    untamper: (seq) => req("POST", `/v1/demo/untamper/${encodeURIComponent(seq)}`),
    otap: () => req("GET", "/.well-known/otap.json"),
  };
}
