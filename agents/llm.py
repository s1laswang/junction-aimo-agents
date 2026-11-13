import json
import os

import httpx


def available():
    return bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL"))


def chat(system, user):
    r = httpx.post(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
        json={"model": os.environ["LLM_MODEL"], "temperature": 0,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def choose(request_text, candidates):
    if not available() or not candidates:
        return None
    rows = [{"product_id": o["product_id"], "name": o["product"]["name"], "total_net": o["total_net"],
             "reliability": o.get("reliability"), "available": o.get("available_amount"),
             "date": o.get("available_date"), "notes": o.get("notes")} for o in candidates]
    try:
        out = chat("You are a purchasing agent. Reply with only the product_id of the best offer for the request: "
                   "meets constraints, cheapest, available in time, reliable.",
                   json.dumps({"request": request_text, "offers": rows}))
    except Exception:
        return None
    ids = {str(o["product_id"]): o["product_id"] for o in candidates}
    return ids.get(out.strip().strip('"').strip("'"))


def explain(facts):
    if not available():
        return None
    try:
        return chat("Explain this purchasing outcome to a kitchen manager in 2 plain sentences. Use only the facts given.",
                    json.dumps(facts, default=str))
    except Exception:
        return None
