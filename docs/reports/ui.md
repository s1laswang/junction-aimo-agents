# S5 `ui`: finished UI work + voice approval

Owner: session S5 (`team/ui`). Owns `web/`, `aimo/voice/`, `tests/test_voice*.py` (+ the 2-line voice mount in `aimo/gateway/app.py`).

## Done (on main)

### Rescued UI work, wired to the live endpoints
The stopped frontend worktree (`agent-a382899e20328ded2`, uncommitted) was 3-way applied onto main and every
"PROPOSED" field was switched to the contract that actually landed (docs/interfaces.md):

| Feature | Live endpoint / field | Where in `/ui` |
|---|---|---|
| Clarifying question card | step `needs_clarification` → `POST /v1/agent/runs/{id}/answer {option_id \| text, question_id}` | Agent activity; run status "Needs your answer" |
| Photo intake | `POST /v1/agent/runs {image_b64, image_mime}`; the `thought` step with `extracted` (vision.ImageOrder) | "Add or take photo" / drop on the request card; line table + unreadable parts |
| Always allow this swap | decision `{remember, remember_scope}`; `GET /v1/customers/{id}/learned` (`available:false` hides the panel) | Decision form (substitution orders); "Learned swaps" panel |
| Re-quote diff | 409 `requote_needed` `detail {new_quote, new_order, changes}` | Approvals: old vs new lines, one-click approve of the new quote |
| CO₂ / reliability badges | product `co2_kg_per_kg`, `delivery_reliability` | Plan lines + candidate cards; browser re-rank (cheapest/greenest/reliable) |
| Valio Aimo ops tab | `/v1/approvals?status=all`, `/v1/drift/{id}`, `/v1/proposals` | "Valio Aimo ops" tab |
| Blocked-attacks feed | audit `approval.refused`, `order.refused`, `request.refused`, `proposal.accept_refused` | Header pill "N attacks blocked" + ops feed |
| Recipes tab | `/v1/recipes`, `/v1/recipes/plan`, quote+order as the customer's agent | "Recipes" tab |

### Approver sign-in panel (no more `window.prompt`)
- Header chip "Sign in as approver" → a modal: approver (directory, grouped by customer, the order's required role
  pre-selected) + PIN → `POST /v1/auth/approver-session`. Only the 8-hour session token is kept (sessionStorage).
- The inbox never pops the panel by itself: signed out, it shows "Approvals are human-only" with a sign-in button.
- The decision form shows **Deciding as Anna Virtanen · Procurement Manager [Switch]**; the body carries no
  self-declared approver beyond the session's own id. Warns when the signed-in person is not in the order customer's
  directory or holds a different role than the policy asks for (the gateway enforces both).
- Mock mode (`?mock=1`) has the same panel (any PIN).

### Voice approval (`aimo/voice/`)
- `GET /v1/voice/status`, `POST /v1/voice/prompt {order_id}`, `POST /v1/voice/transcribe` (multipart `audio`),
  `POST /v1/voice/intent {transcript, order_id}`. All but status need `X-Approver-Session`; agent identity → 403.
- **Voice never decides.** The prompt is read out (ElevenLabs `eleven_flash_v2_5` when `ELEVENLABS_API_KEY` is set,
  else browser `speechSynthesis`); the answer is transcribed (ElevenLabs Scribe, else browser `SpeechRecognition`, else
  typed); intent comes from Gemini (`aimo.gemini`, ledger feature `voice.intent`, fast model) with a rule safety net
  (a negated/mixed answer is never "approve"). The UI shows the transcript and the intent, and **one confirm click**
  calls the normal `POST /v1/approvals/{id}/decision` with the session and the quote hash. The audit note records
  `Voice (<engine>): “<transcript>”`.
- Inbox auto-refresh pauses while a voice decision is open.

### Re-quote is reachable from the UI
The old decision form disabled Approve once the 15-minute price lock expired, so the gateway's 409 `requote_needed`
could never be reached. Now an expired order shows **"Price lock expired: re-price and review"**; clicking it gets the
new signed quote + new pending order and shows the old-vs-new diff (heading follows `detail.reason`), then
**Approve new quote** approves the new order with its own hash. Verified live on ORD-20261114-35cf6376 →
ORD-20261114-f30ca431 (old order `expired`, `provenance.requoted_as`).

### Presenter mode
Checked at 1280×800 `&present=1`: top bar no longer wraps (interface label + brand subtitle hide below 1480 px),
voice card and decision bar fit, voice card survives the inbox refresh.

### Evidence
- `uv run --extra dev pytest -q tests/test_voice.py` → 18 passed (intent table incl. negations, Gemini overruled by
  negation, routes need a session and refuse agents, voice changes nothing until the decision call, ElevenLabs TTS/STT
  path with a fake HTTP client).
- Live run on `aimo serve --port 8155` (Playwright, Chromium): sign in as Anna → hero milk run → "Decide by voice" →
  typed fallback "Yes, approve it" → Gemini intent APPROVE → Confirm → order placed, Peppol AP, audit
  `approval.verified … "Voice (typed): “Yes, approve it”"`.
- Screenshots: `docs/reports/frontend/s5-*.png`.

## Open items
- No `ELEVENLABS_API_KEY` in the team `.env`: the ElevenLabs path is unit-tested with a fake client only. Add the key
  and the same button uses ElevenLabs voice + Scribe automatically (`/v1/voice/status` shows which engine is on).
- `aimo/trust/learned.py` (S3) is not on main yet, so the learned-swaps panel stays hidden (`available:false`).

## Requests
- S4 (docs): DEMO.md beat "a human signs in, agents never can" now uses the header chip "Sign in as approver";
  voice beat: Approvals → "Decide by voice" → say "approve" → Confirm.
- Contracts: no change to `aimo/models.py`. Appended a "Voice approval" section to `docs/interfaces.md` (`/v1/voice/*`).
