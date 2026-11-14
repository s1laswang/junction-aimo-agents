import json
import os
import re

import httpx


def _config():
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    return base, key, model


def available():
    base, key, model = _config()
    return bool(base and key and model)


def chat(system, user):
    base, key, model = _config()
    if not (base and key and model):
        raise ValueError("LLM configuration missing")
    r = httpx.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid LLM response")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Empty choices in LLM response")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("Invalid choice in LLM response")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing message in LLM response")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("Missing content in LLM response")
    return content.strip()


def _single_token_match(text, ids):
    hits = [
        orig
        for cid, orig in ids.items()
        if cid and re.search(rf"(?<![A-Za-z0-9_-]){re.escape(cid)}(?![A-Za-z0-9_-])", text)
    ]
    return hits[0] if len(hits) == 1 else None


def choose(request_text, candidates):
    if not available() or not candidates:
        return None
    try:
        rows = [
            {
                "product_id": o["product_id"],
                "name": (o.get("product") or {}).get("name"),
                "total_net": o.get("total_net"),
                "reliability": o.get("reliability"),
                "available": o.get("available_amount"),
                "date": o.get("available_date"),
                "notes": o.get("notes"),
            }
            for o in candidates
        ]
        ids = {str(o["product_id"]): o["product_id"] for o in candidates}
        out = chat(
            "You are a purchasing agent. Reply with only the product_id of the best offer for the request: "
            "meets constraints, cheapest, available in time, reliable.",
            json.dumps({"request": request_text, "offers": rows}, default=str),
        )
    except Exception:
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    cleaned = out.strip().strip('"').strip("'")
    if cleaned in ids:
        return ids[cleaned]
    unfenced = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", out.strip()).strip()
    unquoted = unfenced.strip("`").strip().strip('"').strip("'")
    if unquoted in ids:
        return ids[unquoted]
    try:
        parsed = json.loads(unfenced)
    except Exception:
        parsed = None
    if isinstance(parsed, dict) and "product_id" in parsed:
        pid = parsed.get("product_id")
        if isinstance(pid, (str, int)) and not isinstance(pid, bool):
            key = str(pid).strip()
            return ids[key] if key in ids else _single_token_match(key, ids)
        if isinstance(pid, list):
            return _single_token_match(json.dumps(pid), ids)
        return None
    if isinstance(parsed, (str, int)) and not isinstance(parsed, bool):
        key = str(parsed).strip()
        if key in ids:
            return ids[key]
    return _single_token_match(unfenced, ids)


def explain(facts):
    if not available():
        return None
    try:
        out = chat(
            "Explain this purchasing outcome to a kitchen manager in 2 plain sentences. Use only the facts given.",
            json.dumps(facts, default=str),
        )
    except Exception:
        return None
    if not isinstance(out, str):
        return None
    cleaned = out.strip()
    return cleaned or None
