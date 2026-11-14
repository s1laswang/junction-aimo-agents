# Swarms: four humans, about 100 agents each

Goal: 10/10 on every judging criterion. Each human runs one swarm. The plan is in docs/PLAN.md, and the gates are in each BRIEF.

| Swarm | Owner | Goal | Gate |
|---|---|---|---|
| A | Silas (s1laswang) | Solve the user need | ≥95% zero-correction on a 1,000-request gold set; escalation precision ≥90% at recall ≥99% |
| B | Momin (Momin010) | Robustness: red team + chaos | 0 violations in ≥10k attacks; injection block ≥98% with false positives ≤2%; 100% graceful under chaos |
| C | Ivar (ivarakerholm-wq) | Product and Gemini experience | order without typing, approve in one click; Finnish UI; multimodal eval |
| D | Chintufp | Openness, proof and submission | public URL, conformance passes, every number reproducible, video and deck |

## Kickoff (paste into each agent; replace X with your swarm letter)
> You are one agent in swarm X of the Valio Aimo Trusted Trade Agents team. Repo: github.com/s1laswang/junction-aimo-agents. Read `swarm/README.md` and `swarm/X/BRIEF.md`. Then claim ONE unclaimed card from `swarm/X/cards.jsonl` (see the claim rule), do exactly what it says, validate with `uv run python swarm/validate.py <card-id>`, and push branch `swarm/X/<card-id>`. Never edit files outside your card's `outputs`. Never print or commit secrets. When done, claim the next card.

## Protocol
1. **Setup, once per machine:**
   - `git clone` the repo, then `uv sync --extra dev`.
   - `.env`: ask Silas for it. Never commit it.
   - `uv run aimo serve --port 8000` builds data/ on first run. The catalog build takes about 100 s and needs GEMINI_API_KEY.
2. **Claim rule (no central server):**
   - Card i belongs to agent i: number your agents 1..N and give each the card whose `n` equals its number.
   - An agent that finishes takes the card with `n` = its number + N, and so on.
   - Before starting, check the branch `swarm/X/<card-id>` doesn't exist on origin (`git ls-remote origin swarm/X/<card-id>`). If it does, skip to the next card.
3. **Output:** only the paths in the card's `outputs` (always under `swarm/X/out/`). Every output has a sibling `<card-id>.report.json` containing `{card, status: done|blocked|broken_found, summary, findings[]}`.
4. **Validate:** `uv run python swarm/validate.py <card-id>` checks schemas and that you touched nothing else. Push only if it passes.
5. **Push:** `git checkout -b swarm/X/<card-id>`, commit with message `swarm X <card-id>: <summary>`, then `git push -u origin swarm/X/<card-id>`. Don't open a PR: the swarm lead and CEO merge in bulk.
6. **Limits:**
   - Gemini: at most 4 concurrent calls per agent; go through `aimo.gemini` (it logs usage); use the Batch API for bulk.
   - Valio mock API: never loop over the catalog. Use `AIMO_REPLAY` when the gateway supports it, or batch ≤1,000 ids, at most 1 call per minute per agent.
   - Heavy compute: FR8 freighter, `normal` partition only, `--time` always set. Never touch other users' jobs or `~/momin`.
7. **Findings that need core changes** (a broken guardrail, a wrong product, a confusing message) go in the report's `findings[]` with an exact repro. The Claude lead for that area fixes them in core code.

## Leads (Claude engineers turning findings into core fixes)
- A: catalog + buyer
- B: trust + gateway + wind tunnel
- C: frontend + buyer
- D: pitch + port (conformance)
