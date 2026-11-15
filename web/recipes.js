// "Recipes" tab: dish + portions -> ingredient plan (POST /v1/recipes/plan), then quote and order it through the
// gateway as the customer's own agent, so the customer's policy decides (it may land in the approval inbox).
import { h, mount, eur, num, day, titleCase } from "./util.js";

function nextWeekday(dow) { // 1 = Monday
  const d = new Date();
  const add = ((dow - d.getDay() + 7) % 7) || 7;
  d.setDate(d.getDate() + add);
  return d.toISOString().slice(0, 10);
}

export async function loadRecipes(ctx) {
  const R = (ctx.S.recipes = ctx.S.recipes || { state: "idle", list: [], recipeId: null, portions: 120, delivery: nextWeekday(1), lactose: false, gluten: false, plan: null, planning: false, error: null, ordered: null });
  if (R.list.length || R.state === "loading") return;
  R.state = "loading";
  ctx.rerender();
  try {
    R.list = await ctx.api.recipes();
    R.recipeId = R.recipeId || R.list[0]?.id;
    R.state = "ok";
  } catch (e) {
    R.state = "error";
    R.error = e.status === 404 ? "This gateway has no recipe endpoint yet." : e.message;
  }
  ctx.rerender();
}

function lineScreenBadge(l) {
  const sc = l.injection_screen;
  const suspicious = Boolean((sc && sc.suspicious) || (l.flags || []).includes("supplier_text_injection_suspected"));
  if (suspicious) {
    const reasons = (sc?.reasons && sc.reasons.length ? sc.reasons : sc?.strong_reasons || []).join(", ");
    return h("div", { class: "row wrap", style: { marginTop: "3px", gap: "6px" } },
      h("span", { class: "flag bad" }, "Prompt injection flagged"),
      reasons ? h("span", { class: "small muted mono" }, reasons) : null);
  }
  if (sc && !sc.suspicious) {
    return h("div", { class: "row wrap", style: { marginTop: "3px" } },
      h("span", { class: "flag ok" }, "Screened: clean"));
  }
  return null;
}

async function plan(ctx) {
  const R = ctx.S.recipes;
  const cust = ctx.S.customers.find((c) => c.id === ctx.S.customerId);
  const customerId = String(cust?.id || ctx.S.customerId || ctx.S.customers?.[0]?.id || "hotel-aurora").trim();
  R.planning = true; R.error = null; R.orderError = null; R.plan = null; R.ordered = null;
  ctx.rerender();
  try {
    R.plan = await ctx.api.planRecipe({
      recipe_id: R.recipeId, portions: Number(R.portions), delivery_by: R.delivery, customer_id: customerId,
      excluded_allergens: cust?.excluded_allergens || [], lactose_free: R.lactose, gluten_free: R.gluten, include_candidates: false,
    }, customerId);
  } catch (e) {
    R.error = e.code === "portions_required" ? "Enter the number of portions." : e.message;
  }
  R.planning = false;
  ctx.rerender();
}

async function order(ctx) {
  const R = ctx.S.recipes;
  const cust = ctx.S.customers.find((c) => c.id === ctx.S.customerId);
  const customerId = String(R.plan?.customer_id || R.plan?.quote_request?.customer_id || cust?.id || ctx.S.customerId || "hotel-aurora").trim();
  const planCust = ctx.S.customers.find((c) => c.id === customerId) || cust;
  const agentId = (planCust?.agent_id || `${customerId}/buyer-1`).split("/").pop();
  R.ordering = true; R.orderError = null;
  ctx.rerender();
  try {
    R.ordered = await ctx.api.orderQuoteRequest({ ...(R.plan?.quote_request || {}), customer_id: customerId }, customerId, agentId);
    ctx.afterOrder(R.ordered.order);
  } catch (e) {
    const refused = e.body?.detail?.refused;
    R.orderError = refused ? `The gateway refused ${refused.length} line(s): ${refused.map((x) => `${x.product_id} (${x.reason})`).join(", ")}. Re-plan for a later date.` : e.message;
  }
  R.ordering = false;
  ctx.rerender();
}

export function renderRecipes(ctx) {
  const v = document.getElementById("view-recipes");
  const R = ctx.S.recipes || { state: "loading", list: [] };
  const cust = ctx.S.customers.find((c) => c.id === ctx.S.customerId);
  const parts = [];
  parts.push(h("div", null, h("h2", null, "Recipe to order"),
    h("div", { class: "muted small" }, `Pick a dish and the number of portions. The planner scales the recipe, applies diet swaps, and plans every ingredient against live offers for ${cust?.name || ctx.S.customerId}.`)));
  if (R.state === "loading") { parts.push(h("div", { class: "skeleton", style: { height: "120px" } })); mount(v, parts); return; }
  if (R.state === "error") { parts.push(h("div", { class: "exc bad" }, h("b", null, "!"), h("div", null, R.error))); mount(v, parts); return; }

  const recipe = R.list.find((r) => r.id === R.recipeId);
  parts.push(h("div", { class: "card card-body recipe-form" },
    h("div", { class: "field" }, h("label", { for: "rc-dish" }, "Dish"),
      h("select", { class: "input", id: "rc-dish", onchange: (e) => { R.recipeId = e.target.value; R.plan = null; ctx.rerender(); } },
        R.list.map((r) => h("option", { value: r.id, selected: r.id === R.recipeId }, `${titleCase(r.name)}${r.name_fi ? ` (${r.name_fi})` : ""}`)))),
    h("div", { class: "field" }, h("label", { for: "rc-portions" }, "Portions"),
      h("input", { class: "input", id: "rc-portions", type: "number", min: 1, step: 10, value: R.portions, oninput: (e) => (R.portions = e.target.value) })),
    h("div", { class: "field" }, h("label", { for: "rc-date" }, "Delivery by"),
      h("input", { class: "input", id: "rc-date", type: "date", value: R.delivery, oninput: (e) => (R.delivery = e.target.value) })),
    h("div", { class: "field" }, h("span", { class: "label" }, "Diet"),
      h("div", { class: "row" },
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: R.lactose, onchange: (e) => (R.lactose = e.target.checked) }), " Lactose-free"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: R.gluten, onchange: (e) => (R.gluten = e.target.checked) }), " Gluten-free"))),
    h("button", { class: "btn btn-primary", disabled: R.planning || !R.recipeId, onclick: () => plan(ctx) }, R.planning ? [h("span", { class: "spinner" }), "Planning"] : "Plan the dish"),
    recipe ? h("div", { class: "faint small", style: { gridColumn: "1 / -1" } },
      `Per portion: ${recipe.ingredients.map((i) => `${i.query} ${num(i.per_portion)} ${i.unit}`).join(" · ")}`, cust?.excluded_allergens?.length ? ` · customer excludes ${cust.excluded_allergens.join(", ")}` : "", recipe.note ? ` · ${recipe.note}` : "") : null));

  if (R.error) parts.push(h("div", { class: "exc bad" }, h("b", null, "!"), h("div", null, R.error)));
  if (R.planning) parts.push(h("div", { class: "skeleton", style: { height: "220px" } }));
  const P = R.plan;
  if (P) {
    const covered = P.ingredients.filter((i) => !(i.plan?.shortfall > 0) && (i.plan?.lines || []).length).length;
    parts.push(h("div", { class: "card summary" },
      h("div", { style: { minWidth: 0 } }, h("span", { class: "section-label" }, "Plan"), h("p", { class: "sm", style: { marginTop: "6px" } }, P.summary)),
      h("div", { class: "kpis" },
        h("div", { class: "kpi" }, h("span", { class: "v" }, `${covered} / ${P.ingredients.length}`), h("span", { class: "l" }, "Ingredients covered")),
        h("div", { class: "kpi" }, h("span", { class: "v big" }, eur(P.total_net_estimate)), h("span", { class: "l" }, "Estimate, net")))));
    if ((P.swaps || []).length) parts.push(h("div", { class: "row wrap" }, h("span", { class: "section-label" }, "Diet swaps"),
      P.swaps.map((s) => h("span", { class: "chip", title: s.why }, h("i", null, s.ingredient), h("b", null, `→ ${s.to}`)))));
    parts.push(h("div", { class: "card" },
      h("table", { class: "t" },
        h("thead", null, h("tr", null, h("th", null, "Ingredient"), h("th", { class: "n" }, "Need"), h("th", null, "Product"), h("th", { class: "n" }, "Qty"), h("th", { class: "n" }, "Line total"))),
        h("tbody", null, P.ingredients.map((i) => {
          const lines = i.plan?.lines || [];
          const short = i.plan?.shortfall > 0 || !lines.length;
          return h("tr", { class: short ? "short" : "" },
            h("td", null, h("div", { style: { fontWeight: 500 } }, titleCase(i.ingredient)), i.swap ? h("div", { class: "small", style: { color: "var(--ok)" } }, `swapped: ${i.swap.query}`) : null),
            h("td", { class: "n" }, `${num(i.quantity)} ${i.unit}`),
            h("td", null, lines.length ? lines.map((l) => h("div", null,
              h("div", null, l.product_name, h("span", { class: "faint small mono" }, ` ${l.product_id}`)),
              lineScreenBadge(l))) : h("span", { class: "flag bad" }, "Not covered"),
              short && lines.length ? h("span", { class: "flag warn" }, `short ${num(i.plan.shortfall)} ${i.unit}`) : null),
            h("td", { class: "n" }, lines.map((l) => h("div", null, `${num(l.qty_sales_units)} ${l.sales_unit}`))),
            h("td", { class: "n" }, lines.map((l) => h("div", null, eur(l.line_total)))));
        })),
        h("tfoot", null, h("tr", null, h("td", { colspan: 4 }, "Estimate, net"), h("td", { class: "n" }, eur(P.total_net_estimate)))))));
    for (const u of P.uncovered || []) {
      parts.push(h("div", { class: "exc warn" }, h("b", null, "!"), h("div", null, h("b", null, `${titleCase(u.ingredient)} not covered. `), h("span", { class: "exc-body" }, String(u.why || "").split("What a human can do:").pop().trim() ? `What you can do: ${String(u.why).split("What a human can do:").pop().trim()}` : u.why))));
    }
    const flaggedLines = (P.injection_flagged_lines && P.injection_flagged_lines.length)
      ? P.injection_flagged_lines
      : (P.ingredients || []).flatMap((ing) =>
          (ing.plan?.lines || [])
            .filter((ln) => (ln.injection_screen && ln.injection_screen.suspicious) || (ln.flags || []).includes("supplier_text_injection_suspected"))
            .map((ln) => ({ ingredient: ing.ingredient, product_id: ln.product_id, product_name: ln.product_name, injection_screen: ln.injection_screen, flags: ln.flags })));
    if (flaggedLines.length > 0) {
      const flaggedSummary = flaggedLines.map((f) => {
        const why = (f.injection_screen?.reasons || []).join(", ");
        return `${titleCase(f.ingredient || "ingredient")} (${f.product_id}${why ? `: ${why}` : ""})`;
      }).join("; ");
      parts.push(h("div", { class: "exc bad" },
        h("b", null, "✕"),
        h("div", null,
          h("b", null, "Prompt injection flagged on recipe plan. "),
          h("span", { class: "exc-body" }, `Flagged ingredient(s): ${flaggedSummary}. Untrusted supplier text was ignored during planning, and the order will require human approval.`))));
    }
    const planCustomerId = String(P.customer_id || P.quote_request?.customer_id || cust?.id || ctx.S.customerId || "hotel-aurora").trim();
    const planCust = ctx.S.customers.find((c) => c.id === planCustomerId) || cust;
    const n = P.quote_request?.lines?.length || 0;
    parts.push(h("div", { class: "card card-body row wrap" },
      h("span", { class: "grow muted sm" }, `Quote and order ${n} line(s) as ${planCust?.agent_id || `${planCustomerId}/buyer-1`}, delivery ${day(P.delivery_by)}. The gateway re-prices live and the customer's policy decides: it may need approval.`),
      R.ordered ? h("span", { class: `status ${R.ordered.order.status === "placed" ? "ok" : R.ordered.order.status === "pending_approval" ? "pending" : "bad"}` },
        R.ordered.order.status === "pending_approval" ? "Waiting in the approval inbox" : titleCase(R.ordered.order.status)) : null,
      R.ordered && R.ordered.order.status === "pending_approval" ? h("button", { class: "btn btn-sm", onclick: () => ctx.openOrder(R.ordered.order.order_id) }, "Open approval inbox →") : null,
      !R.ordered ? h("button", { class: "btn btn-primary", disabled: R.ordering || !n, onclick: () => order(ctx) }, R.ordering ? [h("span", { class: "spinner" }), "Quoting"] : `Quote and order ${eur(P.total_net_estimate)}`) : null),
      R.orderError ? h("div", { class: "inline-error" }, R.orderError) : null);
  } else if (!R.planning && !R.error) {
    parts.push(h("div", { class: "empty" }, h("strong", null, "Plan a dish"),
      h("span", null, "Example: salmon soup for 120 portions, lactose-free. Cream, milk and butter are swapped to lactose-free versions, every ingredient is planned against live stock, and anything that cannot be covered says why.")));
  }
  mount(v, parts);
}
