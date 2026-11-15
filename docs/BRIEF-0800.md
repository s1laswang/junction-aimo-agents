# Aimo: morning brief (Sun 15 Nov, 09:00)

**Submission:** https://github.com/s1laswang/junction-aimo-agents, tag `aimo-v1`.

## What changed overnight
| Fix | Panel finding |
|---|---|
| An agent can no longer backdate the clock or fake stock (REST, MCP and the agent-run API); demo faults are operator-only (`AIMO_DEMO_FAULTS=1`) | A, plus the run-API bypass from the re-score |
| Agent runs need a signed-in human; the UI signs in on "Run agent" | re-score critical |
| Stock below the quoted quantity at approval → fresh quote, never silently less | B |
| Wind tunnel re-run on the final policy with committed inputs: 0 policy / 0 allergen violations, recall 99.7%, precision 69.5% | C |
| Injection text in the product field is flagged | D |
| A pack swap the customer listed passes under the spend limit; the hero verdict is now two reasons (spend, volume) | User need |
| Gemini model fallback when a model's daily quota runs out (it did, at 05:00) | robustness |
| `docs/evidence/live-run/`: a real Gemini run, raw trace + signed quote + audit chain, verifiable offline | Demo (Q2) |
| `docs/pitch/demo-walkthrough.mp4`: narrated walkthrough video (110 s) | Demo |

## Verified at the tagged commit
- pytest keyed: 375 passed, 0 failed. Keyless (clean export, no keys): 274 passed, 0 failed.
- Scenarios 8/8 live and in replay. `make accept` keyed 19/0/1; keyless 17/0/3 with an order placed.
- Panel: about 82 after the run-API fix; final re-score requested at 02575d8.

## Human-only, before 10:00
1. Submit the Junction form: the repo link at `aimo-v1`, the video link `https://github.com/s1laswang/junction-aimo-agents/raw/aimo-v1/docs/pitch/demo-walkthrough.mp4`.
2. Optional: if the form wants YouTube, upload the mp4 unlisted.
3. For a live pitch: sign in as the approver before going on stage (`data/keys/approver_pin`). If presenting after 16:30, start the gateway with `AIMO_NOW=2026-11-15T10:00:00+02:00`. The Gemini key's quota for gemini-3.8-flash resets at 10:00; until then the fallback model answers.

## Not done
- A price-sanity guard against the catalog list price (panel fix 2).
- A Gemini second opinion on the injection screen; the docs state the regex recall (44%) honestly.
- Three verified free-text keyless requests beyond the hero.
