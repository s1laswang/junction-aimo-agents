import argparse
import asyncio
import contextlib
import os
import json
import math
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from agents import llm

DEMO = [
    ("hotel-aurora", "I need 1000 litres of lactose-free milk by Monday"),
    ("hospital-tays", "I need 40 kg of gluten-free pasta and 200 litres of oat drink without milk allergens by Friday"),
    ("school-hervanta", "We need 300 kg of potatoes and 120 kg of minced meat for school lunches by Thursday"),
]

MCP_CONFIG = """Claude Code:
  claude mcp add --transport http aimo http://127.0.0.1:8080/mcp --header "X-Agent-Key: <KEY>"

Claude Desktop (claude_desktop_config.json, stdio):
{
  "mcpServers": {
    "aimo": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "aimo.mcp_server"],
      "cwd": "<REPO>",
      "env": {"AIMO_AGENT_KEY": "demo-hotel-aurora"}
    }
  }
}"""


def _day(s):
    return str(s)[:10] if s else None


def usable(offer, need_item, deliver_by):
    if offer.get("sales_block"):
        return False
    amt = offer.get("available_amount")
    if amt is not None and amt <= 0:
        return False
    d = _day(offer.get("available_date"))
    if d and deliver_by and d > _day(deliver_by):
        return False
    banned = {a.lower() for a in (need_item.get("constraints") or {}).get("exclude_allergens") or []}
    if banned & {a.lower() for a in offer["product"].get("allergens") or []}:
        return False
    return True


BASE = {"kg": 1, "g": 0.001, "l": 1, "dl": 0.1, "cl": 0.01, "ml": 0.001}


def units_for(offer, need_item):
    us = offer["product"].get("unit_size") or {}
    f, n = BASE.get(str(us.get("unit", "")).lower()), BASE.get(str(need_item.get("unit", "")).lower())
    if f and n and us.get("value") and need_item.get("quantity"):
        return math.ceil(need_item["quantity"] * n / (us["value"] * f) - 1e-9)
    return offer["units_needed"] or 1


def rank(offers, need_item):
    return sorted(offers, key=lambda o: (o["unit_price_net"] * units_for(o, need_item), -(o.get("reliability") or 0)))


def pick(need_item, offers, deliver_by, first=None):
    ok = rank([o for o in offers if usable(o, need_item, deliver_by)], need_item)
    if first is not None:
        ok.sort(key=lambda o: o["product_id"] != first)
    chosen, left = [], 1.0
    for o in ok:
        if left <= 1e-9:
            break
        need_units = units_for(o, need_item)
        amt = o.get("available_amount")
        cap = 1.0 if amt is None else min(1.0, amt / need_units)
        take = min(cap, left)
        qty = math.ceil(take * need_units - 1e-9)
        if amt is not None:
            qty = min(qty, math.floor(amt))
        if qty < 1:
            continue
        chosen.append({"product_id": o["product_id"], "quantity": qty, "offer": o})
        left -= min(qty / need_units, left)
    return chosen, max(left, 0.0)


def _data(res):
    if res.is_error:
        raise RuntimeError("".join(getattr(c, "text", "") for c in res.content))
    sc = res.structured_content
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    return json.loads("".join(c.text for c in res.content))


async def _call(s, name, args):
    return _data(await s.call_tool(name, args))


@contextlib.asynccontextmanager
async def _session(url, key, stdio):
    if stdio:
        params = StdioServerParameters(command=sys.executable, args=["-m", "aimo.mcp_server"],
                                       env={**os.environ, "AIMO_AGENT_KEY": key})
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                yield s
        return
    async with create_mcp_http_client(headers={"X-Agent-Key": key}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                yield s


async def _run(text, customer, key, url, log, poll, interval, stdio=False):
    out = {"need": None, "chosen": [], "quote": None, "order": None, "log": log}
    async with _session(url, key, stdio) as s:
        need = await _call(s, "parse_request", {"text": text, "customer": customer})
        out["need"] = need
        log.append(f"Understood the request for {customer}: " + "; ".join(
            f"{i['quantity']:g} {i['unit']} {i['query']}" for i in need["items"]) + f", delivery by {_day(need['deliver_by'])}.")
        try:
            res = await _call(s, "get_offers", need)
        except RuntimeError:
            res = await _call(s, "get_offers", {"need": need})
        chosen, lines = [], []
        for entry in res["items"]:
            ni, offers = entry["need_item"], entry["offers"]
            log.append(f"Found {len(offers)} offers for {ni['query']}.")
            first = llm.choose(text, [o for o in offers if usable(o, ni, need["deliver_by"])])
            got, left = pick(ni, offers, need["deliver_by"], first)
            if not got:
                log.append(f"No offer for {ni['query']} is available in time and within constraints.")
            for c in got:
                o = c["offer"]
                rel = o.get("reliability")
                log.append(f"Chose {c['quantity']} x {o['product']['name']} at {o['unit_price_net']:.2f} {o['currency']} net"
                           + (f", delivery reliability {rel:.0%}" if rel is not None else "")
                           + f", available {_day(o.get('available_date'))}.")
            if left > 1e-6:
                log.append(f"Shortfall: {left:.0%} of {ni['query']} cannot be covered.")
            chosen += got
        out["chosen"] = chosen
        if not chosen:
            log.append("Nothing to order.")
            return out
        lines = [{"product_id": c["product_id"], "quantity": c["quantity"]} for c in chosen]
        quote = await _call(s, "request_quote", {"offers": lines, "delivery_date": _day(need["deliver_by"])})
        out["quote"] = quote
        log.append(f"Signed quote {quote['quote_id']}: {quote['total_net']:.2f} {quote['currency']} net, valid until {quote['valid_until']}.")
        order = await _call(s, "place_order", {"quote_id": quote["quote_id"], "jws": quote["jws"]})
        out["order"] = order
        d = order["decision"]
        why = " ".join(r["text"] for r in d["reasons"])
        if d["outcome"] == "auto":
            log.append(f"Order {order['order_id']} placed automatically within mandate. {why}".strip())
        elif d["outcome"] == "blocked":
            log.append(f"Order {order['order_id']} blocked. {why}".strip())
        else:
            log.append(f"Order {order['order_id']} needs human approval. {why}".strip())
            for _ in range(poll):
                if order["status"] != "pending_approval":
                    break
                await asyncio.sleep(interval)
                order = await _call(s, "get_order", {"order_id": order["order_id"]})
            out["order"] = order
            log.append(f"Order {order['order_id']} is now {order['status']}.")
    return out


def run(text, customer, key=None, url="http://127.0.0.1:8080/mcp", poll=0, interval=2.0, stdio=False):
    log = []
    out = asyncio.run(_run(text, customer, key or "demo-" + customer, url, log, poll, interval, stdio))
    summary = llm.explain({"request": text, "log": log})
    if summary:
        log.append(summary)
    return out


def main():
    p = argparse.ArgumentParser(prog="agents.buyer")
    p.add_argument("text", nargs="?")
    p.add_argument("--customer", default="hotel-aurora")
    p.add_argument("--key")
    p.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    p.add_argument("--poll", type=int, default=30)
    p.add_argument("--demo", action="store_true")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--mcp-config", action="store_true")
    a = p.parse_args()
    if a.mcp_config:
        print(MCP_CONFIG)
        return
    if a.demo:
        for c, t in DEMO:
            print(f"== {c}: {t}")
            try:
                print("\n".join(run(t, c, a.key, a.url, a.poll, stdio=a.stdio)["log"]))
            except Exception as e:
                print(f"failed: {e}")
        return
    if not a.text:
        p.error("text is required")
    out = run(a.text, a.customer, a.key, a.url, a.poll, stdio=a.stdio)
    print("\n".join(out["log"]))
    o = out["order"]
    sys.exit(0 if o and o["status"] not in ("blocked", "rejected") else 1)


if __name__ == "__main__":
    main()
