"""Gemini embeddings (gemini-embedding-2) over product names + short ingredients.

Build once:  uv run python -m aimo.catalog.embeddings build      (~176 calls of 100 texts, <= 8 concurrent)
Files:       data/embeddings.npy (float16, L2-normalised, N x DIM), data/embeddings_ids.json
Used for:    semantic recall in search() when FTS is weak; "similar product" recall in substitution.
Every call is logged through aimo.gemini.record (feature catalog.embed.*). Query vectors are cached
on disk (data/cache/query_embeddings.json) so the demo works even if the API is slow.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

import numpy as np

from aimo.config import DATA_DIR, GEMINI_EMBED_MODEL

DIM = 768
EMB_PATH = DATA_DIR / "embeddings.npy"
IDS_PATH = DATA_DIR / "embeddings_ids.json"
CHUNK_DIR = DATA_DIR / "cache" / "emb_chunks"
QCACHE = DATA_DIR / "cache" / "query_embeddings.json"
_qlock = threading.Lock()


def product_text(d: dict) -> str:
    parts = [d.get("name_en") or "", d.get("name_fi") or ""]
    if d.get("brand"):
        parts.append(f"brand {d['brand']}")
    ing = (d.get("ingredients_text") or "")[:200]
    if ing:
        parts.append(f"ingredients: {ing}")
    claims = [c for c in (d.get("claims") or []) if c.startswith("FREE_FROM") or c in ("VEGAN", "VEGETARIAN", "ORGANIC")]
    if claims:
        parts.append(" ".join(c.lower().replace("_", " ") for c in claims))
    return " | ".join(p for p in parts if p)


def _embed_batch(texts: list[str], feature: str) -> np.ndarray:
    from google.genai import types

    from aimo import gemini
    last: Exception | None = None
    for attempt in range(5):
        t0 = time.time()
        try:
            r = gemini.client().models.embed_content(
                model=GEMINI_EMBED_MODEL, contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
                config=types.EmbedContentConfig(output_dimensionality=DIM))
            gemini.record(feature, GEMINI_EMBED_MODEL, kind="embed", latency_s=time.time() - t0, meta={"n": len(texts)})
            v = np.array([e.values for e in r.embeddings], dtype=np.float32)
            if v.shape[0] != len(texts):
                raise RuntimeError(f"embedding count mismatch {v.shape[0]} != {len(texts)}")
            v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
            return v
        except Exception as e:  # noqa: BLE001
            last = e
            gemini.record(feature, GEMINI_EMBED_MODEL, kind="embed", latency_s=time.time() - t0, ok=False,
                          error=type(e).__name__, meta={"n": len(texts)})
            time.sleep(min(30, 2 ** attempt))
    raise last  # type: ignore[misc]


def build(concurrency: int = 8, batch: int = 100) -> None:
    from aimo.backend.catalog_store import connect, row_to_dict
    con = connect()
    rows = [row_to_dict(r) for r in con.execute("select * from products order by id")]
    ids = [r["id"] for r in rows]
    texts = [product_text(r) for r in rows]
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    chunks = [(i, texts[i:i + batch]) for i in range(0, len(texts), batch)]

    def work(item):
        i, tx = item
        p = CHUNK_DIR / f"{i:06d}-{len(tx)}.npy"
        if p.exists():
            return
        v = _embed_batch(tx, "catalog.embed.build")
        np.save(p, v.astype(np.float16))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(8, concurrency)) as ex:
        for n, _ in enumerate(ex.map(work, chunks), 1):
            if n % 20 == 0:
                print(f"  {n}/{len(chunks)} chunks, {time.time() - t0:.0f}s", flush=True)
    mats = [np.load(CHUNK_DIR / f"{i:06d}-{len(tx)}.npy") for i, tx in chunks]
    M = np.concatenate(mats).astype(np.float16)
    assert M.shape == (len(ids), DIM), M.shape
    np.save(EMB_PATH, M)
    IDS_PATH.write_text(json.dumps(ids))
    print(f"embeddings: {M.shape} in {time.time() - t0:.0f}s -> {EMB_PATH}")


@lru_cache(maxsize=1)
def _matrix() -> tuple[np.ndarray, list[str], dict[str, int]]:
    if not EMB_PATH.exists() or not IDS_PATH.exists():
        raise FileNotFoundError("embeddings not built (uv run python -m aimo.catalog.embeddings build)")
    M = np.load(EMB_PATH).astype(np.float32)
    ids = json.loads(IDS_PATH.read_text())
    return M, ids, {pid: i for i, pid in enumerate(ids)}


def available() -> bool:
    return EMB_PATH.exists() and IDS_PATH.exists()


def _qcache() -> dict:
    try:
        return json.loads(QCACHE.read_text())
    except Exception:  # noqa: BLE001
        return {}


@lru_cache(maxsize=512)
def embed_query(q: str) -> np.ndarray:
    key = q.strip().lower()
    with _qlock:
        cache = _qcache()
        if key in cache:
            return np.array(cache[key], dtype=np.float32)
    v = _embed_batch([q], "catalog.embed.query")[0]
    with _qlock:
        cache = _qcache()
        cache[key] = [round(float(x), 5) for x in v]
        QCACHE.parent.mkdir(parents=True, exist_ok=True)
        QCACHE.write_text(json.dumps(cache))
    return v


def semantic_search(query: str, k: int = 30) -> list[tuple[str, float]]:
    M, ids, _ = _matrix()
    sims = M @ embed_query(query)
    top = np.argpartition(-sims, min(k, len(ids) - 1))[:k]
    top = top[np.argsort(-sims[top])]
    return [(ids[i], float(sims[i])) for i in top]


def similar_products(product_id: str, k: int = 15) -> list[tuple[str, float]]:
    M, ids, idx = _matrix()
    i = idx.get(product_id)
    if i is None:
        return []
    sims = M @ M[i]
    top = np.argpartition(-sims, min(k + 1, len(ids) - 1))[:k + 1]
    top = top[np.argsort(-sims[top])]
    return [(ids[j], float(sims[j])) for j in top if j != i][:k]


def similarity(a: str, b: str) -> Optional[float]:
    try:
        M, _ids, idx = _matrix()
    except FileNotFoundError:
        return None
    if a not in idx or b not in idx:
        return None
    return float(M[idx[a]] @ M[idx[b]])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build()
    else:
        for pid, s in semantic_search(" ".join(sys.argv[1:]) or "lactose-free milk", k=10):
            print(pid, round(s, 3))
