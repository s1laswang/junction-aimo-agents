# A real Gemini run, raw and verifiable offline

Without a Gemini key the buyer agent runs a labelled scripted model. This folder is the proof of what the **real** agent does: one live run, recorded on Sun 15 Nov 2026 05:38 Helsinki (03:38 UTC), against the live Valio Aimo mock API, on the code of the commit that added this folder.

Request: "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."

What happened, in 24.5 s of agent time and 8 Gemini calls:
- The agent searched, fetched live offers and planned. The hotel's usual 10 L box had missed its order cut-off, so the plan uses the hotel's listed alternative (the same milk in 1 L packs).
- It created a signed quote for EUR 1,010.00 and submitted the order.
- The hotel's policy asked for a human: the total is over the EUR 500 limit, and the volume is 3.3 times the usual week.
- Anna (procurement manager) approved through the human-only endpoint, and the order was placed (supplier response AP).

Models: `gemini-3.8-flash` for seven calls and `gemini-4-hydrogen` with thinking HIGH for the quote decision. No fallback was needed in this run (the trace would show it: each row has `requested_model` and `model`).

## Files

| File | What it is |
|---|---|
| `gemini-trace.jsonl` | Raw trace: one line per Gemini API call, with the full request (system prompt, tool declarations, conversation), the full response, timestamp, latency and token usage |
| `transcript.md` | The same trace as a readable table (`python docs/evidence/live-run/summarize.py`) |
| `run.json` | The agent's own step log for run `run-b9690772fe`: parsed need, tool calls and results, plan, quote, decision, order |
| `quote.json` | The signed quote `Q-20261115-964294b6` (Ed25519 JWS over the quote hash) |
| `order.json` | The placed order `ORD-20261115-06eda13d` with the policy decision and the approver |
| `audit-events.json` | The gateway's complete audit chain for that run (6 events, each hash-chained and signed) |
| `audit-report.md` | The same chain as the readable report the gateway serves |
| `jwks.json` | The public key that signed the quote and the audit events |

The private key was generated in a scratch data folder for this run and is not in the repository.

## Verify it yourself, offline

Only `cryptography` is needed. The keys come from `jwks.json`, never from the document being checked.

```bash
uv run python scripts/verify_quote.py docs/evidence/live-run/quote.json docs/evidence/live-run/jwks.json
# quote Q-20261115-964294b6: hash OK, signature OK / VERIFIED
uv run python scripts/verify_quote.py docs/evidence/live-run/audit-events.json docs/evidence/live-run/jwks.json
# event 1 … event 6 OK / VERIFIED
```

`tests/test_live_run_evidence.py` runs both checks on every test run, and also checks that the files belong to one run: the same run id in the trace and the step log, the same quote hash on the quote, the order and the audit events, and the same total everywhere. Change one byte of the quote or of an audit event and the test fails.

## Reproduce a run like it

```bash
AIMO_GEMINI_TRACE=/tmp/gemini-trace.jsonl uv run aimo demo --auto-approve-as anna@hotel-aurora.example --json /tmp/run.json
```

This needs `AIMO_API_KEY` and `GEMINI_API_KEY` in `.env` and a running gateway (`uv run aimo serve`). `AIMO_GEMINI_TRACE` is off by default.
