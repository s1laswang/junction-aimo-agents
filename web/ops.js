// "Valio Aimo ops" tab: the wholesaler's view across every customer's agents.
// Orders (GET /v1/approvals?status=all), blocked attacks and exceptions (from the signed audit ledger),
// standing-order drift (GET /v1/drift/{customer}) and seller proposals (GET/POST /v1/proposals).
import { h, mount, eur, num, time, dayTime, relTime, roleLabel, titleCase, actorKind } from "./util.js";

// Audit actions that mean "something was stopped". Matching is by action first, then by wording.
const BLOCK_RULES = [
  [/approval\.refused/, "Approval refused", "Someone tried to approve without the right identity, role or quote hash."],
  [/order\.refused/, "Order refused", "An agent tried to submit a quote it does not own, or lacks the order:submit scope."],
  [/request\.refused/, "Request refused", "An agent without a mandate tried to quote."],
  [/proposal\.accept_refused/, "Seller blocked", "The seller agent tried to accept its own proposal (sellers cannot order)."],
  [/injection/, "Prompt injection", "Supplier text tried to instruct the agent; it was flagged and ignored."],
  [/claims_corrected/, "Agent claim corrected", "The agent mislabelled a line; the gateway verified and corrected it."],
  [/tamper/, "Tampering", "An audit event was altered; the hash chain exposed it."],
];

export function blockedEvents(events) {
  const out = [];
  for (const e of events || []) {
    const a = String(e.action || "");
    let rule = BLOCK_RULES.find(([re]) => re.test(a));
    if (!rule && /prompt injection|injection/i.test(e.summary || "") && !/^(approval\.verified|order\.(approved|placed))/.test(a)) rule = BLOCK_RULES[4];
    if (rule) out.push({ e, label: rule[1], hint: rule[2] });
  }
  return out.reverse(); // newest first
}

const EXC_ACTIONS = /requote|expired|order\.rejected|lines_unavailable|quote\.refused|rate_limit|upstream|retry/;

export async function loadOps(ctx) {
  const { api, S } = ctx;
  const O = (S.ops = S.ops || { state: "idle", orders: [], drift: {}, proposals: [], proposeMsg: {} });
  O.state = O.orders.length ? "refresh" : "loading";
  ctx.rerender();
  const custs = S.customers.map((c) => c.id);
  const [orders, proposals, ...drifts] = await Promise.all([
    api.approvals("all", { ask: false }).catch((e) => ({ error: e.code === "signin_required" ? "Sign in as an approver to see every customer's orders." : e.message })),
    api.proposals ? api.proposals().catch((e) => ({ error: e.message })) : Promise.resolve([]),
    ...custs.map((c) => (api.drift ? api.drift(c).catch((e) => ({ error: e.message, status: e.status })) : Promise.resolve(null))),
  ]);
  O.orders = Array.isArray(orders) ? orders.sort((a, b) => new Date(b.order.created_at) - new Date(a.order.created_at)) : [];
  O.ordersError = orders?.error || null;
  O.proposals = Array.isArray(proposals) ? proposals : proposals?.proposals || [];
  O.proposalsError = proposals?.error || null;
  custs.forEach((c, i) => (O.drift[c] = drifts[i]));
  O.state = "ok";
  ctx.rerender();
}

const statusCls = { pending_approval: "pending", placed: "ok", approved: "ok", rejected: "bad", expired: "neutral", failed: "bad" };
const statusLabel = { pending_approval: "Pending approval", placed: "Placed", approved: "Approved", rejected: "Rejected", expired: "Expired", failed: "Failed" };

export function renderOps(ctx) {
  const { S } = ctx;
  const v = document.getElementById("view-ops");
  const O = S.ops || { state: "loading", orders: [], drift: {}, proposals: [] };
  const blocked = blockedEvents(S.ledger.events);
  const custName = (id) => S.customers.find((c) => c.id === id)?.name || id;
  const driftFlags = Object.entries(O.drift || {}).flatMap(([c, d]) => (d && d.flags ? d.flags.map((f) => ({ ...f, customer_id: c, delivery_date: d.delivery_date })) : []));
  const pending = O.orders.filter((x) => x.order.status === "pending_approval").length;
  const openProps = O.proposals.filter((p) => p.status === "proposed" || p.status === "open").length;
  const exceptions = [
    ...O.orders.filter((x) => ["rejected", "expired", "failed"].includes(x.order.status)).map((x) => ({
      ts: x.order.updated_at || x.order.created_at, kind: statusLabel[x.order.status], customer_id: x.order.customer_id,
      text: x.order.response_code === "RE" ? `Supplier rejected every line of ${x.order.order_id}` : `${x.order.order_id} ${statusLabel[x.order.status].toLowerCase()}${x.order.approved_by ? ` by ${x.order.approved_by}` : ""}${x.order.approval_note ? `: “${x.order.approval_note}”` : ""}`,
    })),
    ...S.ledger.events.filter((e) => EXC_ACTIONS.test(e.action || "")).map((e) => ({ ts: e.ts, kind: titleCase(String(e.action).split(".").pop()), customer_id: (e.actor || "").split(":")[1]?.split("/")[0], text: e.summary })),
  ].sort((a, b) => new Date(b.ts) - new Date(a.ts));

  const kpi = (n, l, cls = "") => h("div", { class: `ops-kpi ${cls}` }, h("span", { class: "v" }, String(n)), h("span", { class: "l" }, l));
  const parts = [];
  parts.push(h("div", { class: "row wrap" },
    h("div", { class: "grow" }, h("h2", null, "Valio Aimo operations"),
      h("div", { class: "muted small" }, "The wholesaler's view: every customer's agent, what was stopped, what is drifting, and what the seller agent proposes.")),
    h("button", { class: "btn btn-sm", onclick: () => loadOps(ctx) }, O.state === "loading" || O.state === "refresh" ? [h("span", { class: "spinner" }), "Refreshing"] : "Refresh")));
  parts.push(h("div", { class: "ops-kpis" },
    kpi(blocked.length, "Blocked attacks", blocked.length ? "bad" : ""),
    kpi(pending, "Awaiting human approval", pending ? "warn" : ""),
    kpi(O.orders.length, "Agent orders"),
    kpi(driftFlags.length, "Drift alerts", driftFlags.length ? "warn" : ""),
    kpi(openProps, "Open seller proposals")));

  // ---- blocked attacks (the judge moment)
  parts.push(h("section", { class: "card ops-blocked" },
    h("div", { class: "card-head" }, h("span", { class: "shield" }, "⛨"), h("h3", { class: "grow" }, "Blocked attacks"),
      h("span", { class: "muted small" }, "Live from the signed audit ledger")),
    blocked.length ? h("ol", { class: "feed" }, blocked.slice(0, 12).map(({ e, label, hint }) => h("li", { class: "feed-row" },
      h("span", { class: "feed-time mono", title: dayTime(e.ts) }, time(e.ts)),
      h("span", { class: "feed-tag" }, label),
      h("div", { class: "feed-body" },
        h("div", null, e.summary),
        h("div", { class: "faint small" }, h("span", { class: "mono" }, e.actor), ` · ${e.action} · `, hint))))) :
      h("div", { class: "card-body muted sm" }, "Nothing blocked yet. Run the injection scenario, or have a second agent try to submit someone else's quote: every refusal lands here, signed.")));

  // ---- orders across customers
  parts.push(h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", { class: "grow" }, "All agent orders"), h("span", { class: "muted small" }, "Newest first")),
    O.state === "loading" && !O.orders.length ? h("div", { class: "card-body" }, h("div", { class: "skeleton", style: { height: "80px" } })) :
    O.ordersError ? h("div", { class: "card-body inline-error" }, O.ordersError) :
    !O.orders.length ? h("div", { class: "card-body muted sm" }, "No agent orders yet.") :
    h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", null, "Customer"), h("th", null, "Order"), h("th", null, "Agent"), h("th", null, "Decided by"), h("th", { class: "n" }, "Total"), h("th", null, "Status"), h("th", { class: "n" }, "When"))),
      h("tbody", null, O.orders.slice(0, 20).map(({ order: o, quote: q }) => h("tr", { class: "clickable", onclick: () => ctx.openOrder(o.order_id) },
        h("td", null, custName(o.customer_id)),
        h("td", { class: "mono small" }, o.order_id),
        h("td", { class: "mono small" }, o.agent_id),
        h("td", { class: "small" }, o.approved_by ? `${o.approved_by}${o.approver_role ? ` (${roleLabel(o.approver_role)})` : ""}` : "–"),
        h("td", { class: "n" }, q ? eur(q.total_net) : "–"),
        h("td", null, h("span", { class: `status ${statusCls[o.status] || "neutral"}` }, statusLabel[o.status] || o.status), o.response_code ? h("span", { class: `resp-code ${o.response_code}`, style: { marginLeft: "6px" } }, o.response_code) : null),
        h("td", { class: "n small", title: dayTime(o.created_at) }, relTime(o.created_at))))))));

  // ---- exceptions
  parts.push(h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Exceptions"), h("span", { class: "muted small" }, "Rejected, expired, re-quoted, upstream failures")),
    exceptions.length ? h("ol", { class: "feed" }, exceptions.slice(0, 10).map((x) => h("li", { class: "feed-row" },
      h("span", { class: "feed-time mono" }, time(x.ts)), h("span", { class: "feed-tag warn" }, x.kind),
      h("div", { class: "feed-body" }, h("div", null, x.text), x.customer_id ? h("div", { class: "faint small" }, custName(x.customer_id)) : null)))) :
      h("div", { class: "card-body muted sm" }, "No exceptions. Rejections, expired price locks and re-quotes show up here.")));

  // ---- drift
  parts.push(h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Standing-order drift"), h("span", { class: "muted small" }, "Seeded 8-week demo history, checked against live offers")),
    driftFlags.length ? h("ol", { class: "feed" }, driftFlags.map((f) => h("li", { class: "feed-row" },
      h("span", { class: `feed-tag ${f.route === "approval" ? "warn" : ""}` }, titleCase(f.kind)),
      h("div", { class: "feed-body" },
        h("div", null, h("b", null, custName(f.customer_id)), ` · ${f.product_name || f.product_id}`, f.change_pct != null ? h("span", { class: "mono" }, ` ${f.change_pct > 0 ? "+" : ""}${num(f.change_pct, 1)}%`) : null),
        h("div", { class: "sm" }, f.message),
        h("div", { class: "faint small" }, `Route: ${f.route === "approval" ? "needs a human" : "automatic"}`, f.fix?.action ? ` · fix: ${titleCase(f.fix.action)}${f.fix.quantity != null ? ` ${f.fix.quantity}` : ""}` : "")),
      h("span", { class: "feed-time faint small" }, f.delivery_date ? `for ${f.delivery_date}` : "")))) :
      h("div", { class: "card-body muted sm" }, O.state === "ok" ? "No drift on any standing order." : "Checking standing orders…")));

  // ---- proposals
  parts.push(h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Seller agent proposals"),
      h("span", { class: "muted small" }, "Advisory only: the seller cannot place orders")),
    h("div", { class: "card-body row wrap" },
      h("span", { class: "muted sm grow" }, "Ask the Valio Aimo seller agent for a cheaper equivalent of a customer's usual items:"),
      S.customers.map((c) => h("button", { class: "btn btn-sm", disabled: O.proposing === c.id, onclick: () => propose(ctx, c.id) }, O.proposing === c.id ? h("span", { class: "spinner" }) : null, c.name))),
    Object.entries(O.proposeMsg || {}).map(([c, m]) => h("div", { class: "card-body small muted" }, h("b", null, custName(c)), `: ${m}`)),
    O.proposals.length ? h("ol", { class: "feed" }, O.proposals.slice(0, 8).map((p) => h("li", { class: "feed-row" },
      h("span", { class: "feed-tag" }, p.kind === "cheaper_equivalent" ? "Cheaper" : "Seller pick"),
      h("div", { class: "feed-body" },
        h("div", null, h("b", null, custName(p.customer_id)), ` · ${num(p.qty_sales_units)} × ${p.product?.name || p.product?.product_id}`, p.replaces ? ` replacing ${p.replaces.name || p.replaces.product_id}` : ""),
        (p.why || []).length ? h("div", { class: "sm" }, p.why[0]) : null,
        h("div", { class: "faint small" }, p.estimated_saving_eur != null ? `Saves about ${eur(p.estimated_saving_eur)} · ` : "",
          p.policy_preview ? `Policy preview (not binding): ${p.policy_preview.outcome === "needs_approval" ? `needs ${roleLabel(p.policy_preview.approver_role)}` : p.policy_preview.outcome}` : "")),
      h("span", { class: `status ${p.status === "accepted" ? "ok" : p.status === "declined" ? "neutral" : "pending"}` }, titleCase(p.status))))) :
      h("div", { class: "card-body muted sm" }, O.proposalsError ? O.proposalsError : "No proposals yet.")));
  mount(v, parts);
}

async function propose(ctx, customerId) {
  const O = ctx.S.ops;
  O.proposing = customerId;
  ctx.rerender();
  try {
    const p = await ctx.api.propose(customerId);
    O.proposeMsg[customerId] = `proposal ${p.proposal_id} created (${p.kind === "cheaper_equivalent" ? "cheaper equivalent" : "seller pick"}).`;
  } catch (e) {
    const notes = e.body?.detail?.notes;
    O.proposeMsg[customerId] = e.code === "no_better_offer" ? `no better offer${notes?.length ? `: ${notes[0]}` : "."}` : e.message;
  }
  O.proposing = null;
  await loadOps(ctx);
}
