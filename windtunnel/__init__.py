"""Agent-market wind tunnel: synthetic buyer scenarios, offline replay, runner, scoring. Owner: windtunnel.

Layout
  config.py    paths (everything under data/windtunnel/, gitignored)
  products.py  independent lightweight reader of the raw product JSON (oracle + baseline; NOT the catalog SUT)
  snapshot.py  record the Valio mock API once for a product pool, replay offline, inject faults
  schema.py    pydantic models: Scenario, Expected, RunResult
  oracle.py    deterministic ground-truth labels from rules + snapshot
  generate.py  seeded scenario skeletons + Gemini surface text (structured output, Batch API for large N)
  run.py       pipeline mode (offline, thousands) and agent mode (Gemini buyer vs gateway, ~50-100)
  score.py     metrics -> docs/windtunnel-results.md + data/windtunnel/results/*.json
  judge.py     Gemini judge for agent transcripts and ambiguous labels
  thinking.py  thinking-level vs accuracy/latency comparison (need parsing, substitution decision)
"""
