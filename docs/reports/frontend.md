# Frontend report (team/frontend)

Last updated: Sat 14 Nov 2026, 15:10 Helsinki. Owner: aimo-frontend. Paths: `web/` only.

## Status: round 2 done, everything verified live
I tested on a local gateway built from team/frontend HEAD with origin/main merged in (gateway round 2 and catalog round 2 included), using real Gemini and the live Valio Aimo mock API. Each flow below was clicked through the UI in headless Chrome over CDP.

| Check | Result |
|---|---|
| Hotel run → needs approval → approve → AP → Verify chain | green |
| **Live tamper:** tamper the approval event → verify | **red, broken event #4 highlighted and scrolled into view** ([shot](frontend/live-tamper-red.png)) |
| **Restore** → verify | **green** ([shot](frontend/live-restore-green.png)) |
| School run ("120 litres of oat drink…", buyer scenario text) | needs Kitchen Manager approval (substitution), allergens pass ([shot](frontend/live-school-approval.png)) |
| Hospital run ("40 kg of gluten-free bread…") | needs Dietitian approval (tier-good substitution) ([shot](frontend/live-hospital-verdict.png)) |
| **Reject path** (hospital, Dietitian, with a note) | order rejected, "Order not placed", agent final "Done: rejected", chain green ([shot](frontend/live-reject.png)) |
| Approver display | the new shape (plain `approved_by` + `Order.approver_role`) shows "Anna Virtanen (Procurement Manager, anna@…)". The old `"id (role)"` shape is still parsed. |
| 1280×720 | OK. The decision bar sticks to the bottom so Approve is always visible ([shot](frontend/720p-sticky-decision.png)) |
| 1920×1080 | OK. The layout widens at 1700 px and up ([shot](frontend/1080p-plan.png)) |
| `&present=1` | type about 15% larger, JSON expanders hidden (press **j** to toggle them), hash disclosure kept ([shot, mock hospital at 720p](frontend/mock-hospital-presenter-720p.png)) |

## Round-2 changes
- **Simulate tampering (live and mock):** the button shows only when `/v1/demo/tamper/{seq}` is listed in `/openapi.json`. A 404 hides it with a "disabled on this gateway" note. It picks the human approval event (falling back to the policy or middle event), tampers with it, reloads and verifies. "Restore event #n" untampers and verifies again. The verify bar is sticky.
- **Hospital mock story:** "40 kg gluten-free bread + 2 kg fresh dill", needs Dietitian approval. The allergen facts are real catalog data:
  - 465773 "Fazer Seed bread Gluten-Free" has structured allergen data saying CONTAINS gluten, so it's rejected.
  - 472147 Reissumies may contain gluten, so it's rejected.
  - Fresh dill (460395) has no allergen data, so it's unknown and the Dietitian must confirm.
  Prices and stock are ILLUSTRATIVE.
- **Mock school story** is aligned with the buyer's `school-allergen` scenario text (120 L, Wednesday).
- **Approver scoping fix:** the approver is reset whenever you select a different order. Before, a stale approver from another customer could carry over (see the trust issue below).

## Issue for trust/gateway (security, please fix before the demo)
**The gateway accepts any approver identity.** `POST /v1/approvals/{id}/decision` with `approver: "mallory@evil.example"` and no role **placed** a school order: the role fell back to the order's required role. Earlier, a school kitchen manager rejected a hospital order as "Dietitian" the same way. `check_approval` should require the approver to be in the order customer's directory with the required (or override) role, and should refuse otherwise (403 or 409). The UI now always sends a directory approver and role.

## Other observations (for buyer and catalog)
- School live: the plan showed €249.69 / 124.74 L, but the agent quoted 120 × Oatly Oat Drink Semi for €168.00. The quote differs from the plan's proposed lines. That's probably fine (the agent chose), but the Plan tab and Approvals then show different totals. Consider emitting a fresh `plan` step or a note when the agent deviates from the plan.
- Orders now show `agent_id: "buyer-1"` (bare) while actors show `hotel-aurora/buyer-1`. Not a UI problem.

## How to open
- Live: `uv run aimo serve`, then http://localhost:8000/ui/ (add `?present=1` for the projector).
- Mock: http://localhost:8000/ui/?mock=1, or `cd web && python -m http.server`, then `index.html?mock=1`.
- Helpers: `&autorun=1`, `&autoapprove=1`, `&tab=plan|approvals|audit`, `&customer=school-tampere|hospital-meilahti`, `&api=http://host:port`.

## Contract the UI consumes (unchanged from round 1, plus)
- `POST /v1/demo/tamper/{seq}` and `POST /v1/demo/untamper/{seq}`: any 2xx is success, 404 means disabled.
- `Order.approver_role` (optional) is used for display, with fallback to parsing `approved_by`.

## Untested
- Phone width, and the tamper route's 404 behaviour under `AIMO_ENV=production` (the code path exists; I didn't run the gateway in production mode).
