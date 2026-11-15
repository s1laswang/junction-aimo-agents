"""Print a readable transcript of the raw Gemini trace in this folder (gemini-trace.jsonl).
Usage: python docs/evidence/live-run/summarize.py > docs/evidence/live-run/transcript.md"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [json.loads(line) for line in (HERE / "gemini-trace.jsonl").read_text().splitlines() if line.strip()]
run = json.loads((HERE / "run.json").read_text())
order = json.loads((HERE / "order.json").read_text())
quote = json.loads((HERE / "quote.json").read_text())


def short(x, n=260):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + " …"


print(f"# Live Gemini run `{run['run_id']}`: transcript\n")
print(f"Request: \"{(run.get('need') or {}).get('raw_text', '')}\"\n")
print(f"Result: order `{order['order_id']}` **{order['status']}** (supplier response {order.get('response_code')}), "
      f"quote `{quote['quote_id']}` EUR {quote['total_net']:.2f} net, approved by {order.get('approved_by')}. "
      f"Agent wall time {run.get('latency_s')} s.\n")
print("Every row below is one real Gemini API call from `gemini-trace.jsonl` (full request and response are in "
      "that file). `requested` differs from `answered` only when the model fallback stepped in.\n")
print("| # | Time (UTC) | Feature | Requested | Answered | OK | Latency s | Tokens in / out / thinking | What the model did |")
print("|---|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rows, 1):
    u = r.get("usage") or {}
    did = []
    for c in (r.get("response") or {}).get("candidates", []):
        for p in (c.get("content") or {}).get("parts", []):
            if "function_call" in p:
                fc = p["function_call"]
                did.append(f"calls `{fc['name']}`({short(fc.get('args', {}), 150)})")
            elif p.get("text") and not p.get("thought"):
                did.append("says: " + short(p["text"].replace("\n", " "), 150))
    if not r["ok"]:
        did.append("error: " + short(r.get("error", ""), 150))
    cell = "; ".join(did).replace("|", "\\|") or "-"
    print(f"| {i} | {r['ts'][11:19]} | {r['feature']} | {r['requested_model']} | {r['model']} | "
          f"{'yes' if r['ok'] else 'no'} | {r['latency_s']} | {u.get('prompt_token_count', '-')} / "
          f"{u.get('candidates_token_count', '-')} / {u.get('thoughts_token_count') or 0} | {cell} |")
fallbacks = [r for r in rows if r["ok"] and r["model"] != r["requested_model"]]
failed = [r for r in rows if not r["ok"]]
print(f"\nCalls: {len(rows)} ({len(failed)} failed attempts, {len(fallbacks)} answered by a fallback model). "
      f"Models that answered: {', '.join(sorted({r['model'] for r in rows if r['ok']}))}.")
