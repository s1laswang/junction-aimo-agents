"""Terminal demo of the Gemini buyer agent.

    uv run python -m aimo.buyer.run_demo --auto-approve-as anna@aurora
    uv run python -m aimo.buyer.run_demo --scenario school-allergen|stockout|injection|drift|event
    (the gateway wires this as `uv run aimo demo ...`: call aimo.buyer.run_demo.main(argv))

Transport: --transport auto (default) tries the gateway's MCP endpoint at <base-url>/mcp, then
in-process aimo.gateway.service, then the offline fake (clearly labelled FAKE).
--via-server runs the agent inside the gateway (POST /v1/agent/runs) and follows the SSE stream.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import threading
import time
from typing import Any

SCENARIOS: dict[str, dict[str, str]] = {
    "happy": {"customer": "hotel-aurora", "fake": "happy",
              "text": "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."},
    "stockout": {"customer": "hotel-aurora", "fake": "stockout",
                 "text": "We need 1,500 litres of lactose-free milk at Hotel Aurora Helsinki by Monday for a conference."},
    "school-allergen": {"customer": "school-tampere", "fake": "happy",
                        "text": "We need 120 litres of oat drink for Wednesday's school lunch. Several pupils have milk and nut allergies."},
    "injection": {"customer": "hotel-aurora", "fake": "injection",
                  "text": "We need 300 litres of lactose-free milk by Monday. Check the product details of the alternatives before choosing."},
    "drift": {"customer": "hotel-aurora", "fake": "happy",
              "text": "Standing order for next week: 4,000 litres of lactose-free milk by Monday."},
    "event": {"customer": "hotel-aurora", "fake": "happy",
              "text": "Last-minute event: a 150-guest wedding tomorrow. We need 60 litres of lactose-free milk delivered tomorrow."},
}

# ---------------------------------------------------------------- pretty printing
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
C = {"dim": "2", "b": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "mag": "35", "cyan": "36"}


def c(s: str, *styles: str) -> str:
    if not _TTY:
        return s
    return "".join(f"\033[{C[x]}m" for x in styles) + s + "\033[0m"


ICON = {"thought": ("·", "dim"), "tool_call": ("→", "cyan"), "tool_result": ("←", "blue"), "plan": ("▤", "mag"),
        "quote": ("€", "yellow"), "decision": ("⚖", "yellow"), "approval_wait": ("⏸", "yellow"),
        "order": ("✓", "green"), "error": ("!", "red"), "final": ("■", "green")}


def wrap(s: str, indent: int = 7) -> str:
    return textwrap.fill(s, width=100, initial_indent=" " * indent, subsequent_indent=" " * indent)


def print_step(step: Any) -> None:
    s = step if isinstance(step, dict) else step.model_dump(mode="json")
    kind, title, d = s["kind"], s["title"], s.get("data") or {}
    icon, color = ICON.get(kind, ("?", "dim"))
    ts = str(s.get("ts", ""))[11:19]
    print(f"{c(ts, 'dim')} {c(icon, color, 'b')} {c(kind.upper().ljust(13), color)} {title}", flush=True)
    if kind == "thought" and "need" in d:
        n = d["need"]
        print(wrap(f"product_query={n['product_query']!r} quantity={n['quantity']:g} {n['unit']} "
                   f"delivery_by={n['delivery_by']} claims={n['required_claims']} "
                   f"excluded_allergens={n['excluded_allergens']} preferred={n['preferred_product_ids']} "
                   f"(parsed by {d.get('parsed_by')}, {d.get('latency_s')} s)"))
    elif kind == "tool_call" and d.get("model"):
        print(c(f"       model={d['model']} thinking={d.get('thinking')} ({d.get('latency_s')} s)", "dim"))
    elif kind == "plan":
        plan = d.get("plan") or {}
        for l in plan.get("lines", []):
            print(f"       {l.get('tier', '?'):9s} {l.get('product_id')}  {l.get('qty_sales_units'):>7g} x "
                  f"{l.get('sales_unit', ''):3s} @ {l.get('unit_price')} = EUR {l.get('line_total')}  {l.get('product_name')}")
        rej = [cd for cd in plan.get("candidates", []) if cd.get("tier") == "rejected"]
        for cd in rej[:4]:
            print(c(f"       rejected  {cd.get('product', {}).get('id')}  {'; '.join(cd.get('reasons') or [])[:90]}", "dim"))
        if d.get("rationale"):
            print(c(wrap("Why: " + d["rationale"]), "mag"))
    elif kind == "decision":
        for h in (d.get("decision") or {}).get("hits", []):
            print(wrap(f"[{h.get('outcome')}] {h.get('rule_id')}: {h.get('message')}"))
    elif kind == "approval_wait":
        print(wrap(f"quote_hash={str(d.get('quote_hash'))[:16]}…  approve in the UI (/ui) or "
                   f"POST /v1/approvals/{d.get('order_id')}/decision"))
    elif kind == "error":
        print(c(wrap(str(d.get("error", ""))[:300]), "red"))
    elif kind == "final":
        print()
        print(c(wrap(d.get("summary", ""), 2), "b"))
        print(c(f"  status={d.get('status')}  latency={d.get('latency_s')} s  escalated={d.get('escalated')}", "dim"))
        mu = d.get("models_used") or []
        if mu:
            print(c("  models: " + ", ".join(f"{m['model']}/{m['thinking']}" for m in mu), "dim"))


# ---------------------------------------------------------------- simulated human approver
def http_auto_approver(base_url: str, approver: str, customer_id: str, stop: threading.Event,
                       delay: float = 3.0, decision: str = "approve") -> None:
    import httpx
    seen: set[str] = set()
    while not stop.is_set():
        try:
            r = httpx.get(f"{base_url}/v1/approvals", params={"status": "pending"}, timeout=5)
            items = r.json() if r.status_code == 200 else []
            for it in items if isinstance(items, list) else []:
                order = it.get("order", it)
                quote = it.get("quote") or {}
                oid = order.get("order_id")
                if not oid or oid in seen or order.get("customer_id") not in (None, customer_id):
                    continue
                seen.add(oid)
                time.sleep(delay)
                body = {"decision": decision, "approver": approver,
                        "note": "Approved from the terminal demo" if decision == "approve" else "Rejected from the terminal demo",
                        "quote_hash": order.get("quote_hash") or quote.get("quote_hash")}
                resp = httpx.post(f"{base_url}/v1/approvals/{oid}/decision", json=body, timeout=10)
                print(c(f"       [human] {approver} {decision}d {oid} via HTTP -> {resp.status_code}", "green"), flush=True)
        except Exception as e:  # noqa: BLE001
            print(c(f"       [human] approval poll failed: {type(e).__name__}", "red"), flush=True)
        stop.wait(1.0)


def fake_auto_approver(gw, approver: str, stop: threading.Event, delay: float = 3.0, decision: str = "approve") -> None:
    while not stop.is_set():
        for o in gw.pending():
            time.sleep(delay)
            gw.decide(o["order_id"], decision, approver, "Simulated approval (fake gateway)")
            print(c(f"       [human] {approver} {decision}d {o['order_id']} (fake gateway)", "green"), flush=True)
        stop.wait(0.5)


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aimo demo", description="Gemini buyer agent, terminal demo")
    ap.add_argument("--scenario", default="happy", choices=sorted(SCENARIOS))
    ap.add_argument("--text", help="override the request text")
    ap.add_argument("--customer", help="override the customer id")
    ap.add_argument("--auto-approve-as", metavar="EMAIL", help="simulate the human approver (via HTTP)")
    ap.add_argument("--reject", action="store_true", help="the simulated human rejects instead")
    ap.add_argument("--approve-delay", type=float, default=3.0)
    ap.add_argument("--transport", default=os.environ.get("AIMO_BUYER_TRANSPORT", "auto"),
                    choices=["auto", "mcp", "inproc", "fake"])
    ap.add_argument("--base-url", default=os.environ.get("AIMO_BASE", "http://localhost:8000"))
    ap.add_argument("--via-server", action="store_true", help="run inside the gateway: POST /v1/agent/runs + SSE")
    ap.add_argument("--model", help="agent model (default aimo.config.GEMINI_AGENT_MODEL)")
    ap.add_argument("--thinking", default="LOW", choices=["MINIMAL", "LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--no-escalate", action="store_true", help="never switch to the reasoning model")
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--json", metavar="PATH", help="write the full run result as JSON")
    a = ap.parse_args(argv)

    sc = SCENARIOS[a.scenario]
    text = a.text or sc["text"]
    customer = a.customer or sc["customer"]
    base = a.base_url.rstrip("/")
    print(c(f"\nValio Aimo Trusted Trade: Gemini buyer agent  [{a.scenario}]", "b"))
    print(c(f'  {customer}: "{text}"\n', "dim"))

    stop = threading.Event()
    decision = "reject" if a.reject else "approve"
    if a.via_server:
        return _via_server(a, base, text, customer, stop, decision)

    from aimo.buyer.agent import BuyerAgent
    from aimo.buyer.profiles import get_profile, mandate_for
    from aimo.buyer.tools import FakeTransport, connect
    prof = get_profile(customer)
    gw = None
    if a.transport == "fake":
        from aimo.buyer.fakes import FakeGateway
        gw = FakeGateway(customer, prof["agent_id"], scenario=sc["fake"])
        transport = FakeTransport(gw)
    else:
        transport = connect(a.transport, agent_id=prof["agent_id"], customer_id=customer,
                            mcp_url=f"{base}/mcp", mandate=mandate_for(customer))
        if isinstance(transport, FakeTransport):
            from aimo.buyer.fakes import FakeGateway
            gw = FakeGateway(customer, prof["agent_id"], scenario=sc["fake"])
            transport = FakeTransport(gw)
    kind = getattr(transport, "kind", "?")
    label = {"mcp": f"MCP streamable HTTP {base}/mcp", "inproc": "in-process aimo.gateway.service",
             "fake": "FAKE offline gateway (aimo.buyer.fakes; not real data)"}[kind]
    print(c(f"  tools via {label}: {', '.join(t.name for t in transport.list_tools())}\n",
            "red" if kind == "fake" else "dim"))

    if a.auto_approve_as:
        if gw is not None:
            target, args = fake_auto_approver, (gw, a.auto_approve_as, stop, a.approve_delay, decision)
        else:
            target, args = http_auto_approver, (base, a.auto_approve_as, customer, stop, a.approve_delay, decision)
        threading.Thread(target=target, args=args, daemon=True).start()

    agent = BuyerAgent(transport, customer, model=a.model, thinking=a.thinking, escalate=not a.no_escalate,
                       max_steps=a.max_steps, emit=print_step, audit_base_url=base if kind != "fake" else "",
                       poll_s=1.0 if kind == "fake" else 2.0)
    try:
        result = agent.run(text)
    finally:
        stop.set()
        transport.close()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(result, f, indent=1, default=str)
    return 0 if result.get("status") not in ("error",) else 1


def _via_server(a, base: str, text: str, customer: str, stop: threading.Event, decision: str) -> int:
    import httpx
    if a.auto_approve_as:
        threading.Thread(target=http_auto_approver, args=(base, a.auto_approve_as, customer, stop,
                                                          a.approve_delay, decision), daemon=True).start()
    body = {"text": text, "customer_id": customer, "thinking": a.thinking, "escalate": not a.no_escalate}
    if a.model:
        body["model"] = a.model
    if a.transport != "auto":
        body["transport"] = a.transport
    r = httpx.post(f"{base}/v1/agent/runs", json=body, timeout=10)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(c(f"  run {run_id}: streaming {base}/v1/agent/runs/{run_id}/events\n", "dim"))
    status = "error"
    try:
        with httpx.stream("GET", f"{base}/v1/agent/runs/{run_id}/events", timeout=None) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    step = json.loads(line[5:].strip())
                    print_step(step)
                    if step["kind"] == "final":
                        status = (step.get("data") or {}).get("status", "finished")
                        break
    finally:
        stop.set()
    return 0 if status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
