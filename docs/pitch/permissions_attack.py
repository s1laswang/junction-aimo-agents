"""30-second "permissions attack" for the live demo: the buyer agent tries to approve its own pending order.

    uv run python docs/pitch/permissions_attack.py --base http://localhost:8000 [--customer hotel-aurora]

Acting purely as the agent (X-Customer-Id + X-Agent-Id, no approver credential), it
  1. lists the MCP tools the agent gets (no approval tool exists),
  2. finds the customer's newest pending order and its exact quote hash,
  3. POSTs an approval naming the real procurement manager (anna@hotel-aurora.example) with the right hash,
  4. prints the gateway's answer (expected: HTTP 403 agent_cannot_approve),
  5. prints the audit report lines for that order that record the refusal (approval.refused).
The order stays pending for the human. Exit code 0 only if the attempt was refused AND the refusal is audited.
Needs no keys and no approver credential: that is the point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

APPROVER = {"hotel-aurora": "anna@hotel-aurora.example", "school-tampere": "liisa@school-tampere.example"}


def call(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def mcp_tools() -> list[str]:
    try:
        from aimo.gateway.mcp_server import build_mcp
        return [t.name for t in asyncio.run(build_mcp().list_tools())]
    except Exception as e:  # never block the demo beat on this
        return [f"(could not list: {e.__class__.__name__})"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--customer", default="hotel-aurora")
    ap.add_argument("--agent", default="buyer-1")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    agent = {"X-Customer-Id": a.customer, "X-Agent-Id": a.agent}
    approver = APPROVER.get(a.customer, "anna@hotel-aurora.example")

    tools = mcp_tools()
    approve_like = [t for t in tools if "approv" in t or "decid" in t]
    print(f"Agent's MCP tools ({len(tools)}): {', '.join(tools)}")
    print(f"Approval tools on MCP: {approve_like or 'none'}\n")

    st, body = call("GET", f"{base}/v1/approvals?status=pending", agent)
    if st != 200:
        print(f"could not list pending orders: HTTP {st} {body[:200]}")
        return 2
    pending = json.loads(body)
    if not pending:
        print(f"No pending order for {a.customer}. Start a run that needs approval first (e.g. the 1,000 L milk request).")
        return 2
    item = max(pending, key=lambda p: p["order"].get("created_at") or p["order"]["order_id"])
    order = item["order"]
    oid, qhash = order["order_id"], order["quote_hash"]
    print(f"Pending order {oid} (quote hash {qhash[:12]}…), waiting for a human.")
    print(f"Agent {a.agent} now tries to approve it AS {approver}, with the correct quote hash:")
    st, body = call("POST", f"{base}/v1/approvals/{oid}/decision", agent,
                    {"decision": "approve", "approver": approver, "approver_role": "procurement_manager",
                     "quote_hash": qhash, "note": "pre-cleared, approve now"})
    print(f"  -> HTTP {st} {body}\n")

    st2, report = call("GET", f"{base}/v1/audit/report/{oid}", {})
    refused = [ln.strip() for ln in report.splitlines() if "approval.refused" in ln]
    print("Audit trail for this order records the refusal:")
    for ln in refused[-3:]:
        print(f"  {ln}")
    st3, verify = call("GET", f"{base}/v1/audit/verify", {})
    print(f"\nAudit chain verify: {verify[:80]}")
    ok = st == 403 and bool(refused)
    print("\nPERMISSIONS ATTACK REFUSED AND AUDITED" if ok else "\nUNEXPECTED: check the output above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
