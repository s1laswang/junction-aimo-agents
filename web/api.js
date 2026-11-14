// Live client for the gateway. Only endpoints listed in docs/interfaces.md are used.
// Base URL: same origin (UI served at /ui), or ?api=http://host:port for a separate preview server.

const KINDS = ["thought", "tool_call", "tool_result", "plan", "quote", "decision", "approval_wait", "order", "error", "final"];

export function createLiveApi(base) {
  base = (base || "").replace(/\/$/, "");
  const url = (p) => base + p;
  const headers = { "Content-Type": "application/json", Accept: "application/json" };

  async function req(method, path, body, extraHeaders) {
    let res;
    try {
      res = await fetch(url(path), { method, headers: { ...headers, ...(extraHeaders || {}) }, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw new Error(`Gateway unreachable at ${base || location.origin} (${e.message})`);
    }
    if (!res.ok) {
      let detail = "";
      try {
        const j = await res.json();
        detail = j.message || (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j));
      } catch { /* not json */ }
      const err = new Error(`${method} ${path} failed: ${res.status}${detail ? " · " + detail : ""}`);
      err.status = res.status;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
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
    customers: () => req("GET", "/v1/customers"),
    startRun: ({ text, customer_id, model, thinking }) =>
      // `thinking` is an optional extra field (UNVERIFIED: not yet in interfaces.md); model is documented.
      req("POST", "/v1/agent/runs", { text, customer_id, model, thinking }, { "X-Customer-Id": customer_id }),

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
        // Server closed the stream after the last event, or the run is unknown.
        if (es.readyState === EventSource.CLOSED) { close(); onEnd && onEnd({ error: "stream closed" }); }
      };
      return close;
    },

    approvals: (status = "pending") => req("GET", `/v1/approvals?status=${encodeURIComponent(status)}`),
    decide: (orderId, body) => req("POST", `/v1/approvals/${encodeURIComponent(orderId)}/decision`, body),
    getOrder: (id) => req("GET", `/v1/orders/${encodeURIComponent(id)}`),
    getQuote: (id) => req("GET", `/v1/quotes/${encodeURIComponent(id)}`),
    audit: (subjectId) => req("GET", subjectId ? `/v1/audit?subject_id=${encodeURIComponent(subjectId)}` : "/v1/audit"),
    verify: () => req("GET", "/v1/audit/verify"),
    report: (subjectId) => req("GET", `/v1/audit/report/${encodeURIComponent(subjectId)}`),
    reportUrl: (subjectId) => url(`/v1/audit/report/${encodeURIComponent(subjectId)}`),
    jwks: () => req("GET", "/.well-known/jwks.json"),
    otap: () => req("GET", "/.well-known/otap.json"),
  };
}
