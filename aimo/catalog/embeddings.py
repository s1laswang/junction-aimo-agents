"""Gemini embeddings (gemini-embedding-2) over product names + short ingredients.

Build once:  uv run python -m aimo.catalog.embeddings build      (~176 calls of 100 texts, <= 8 concurrent)
Files:       data/embeddings.npy (float16, L2-normalised, N x DIM), data/embeddings_ids.json
Used for:    semantic recall in search() when FTS is weak; "similar product" recall in substitution.
Every call is logged through aimo.gemini.record (feature catalog.embed.*). Query vectors are cached
on disk (data/cache/query_embeddings.json) so the demo works even if the API is slow.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Optional

import numpy as np

from aimo.config import DATA_DIR, GEMINI_EMBED_MODEL

DIM = 768
MAX_QUERY_CHARS = 2000
EMB_PATH = DATA_DIR / "embeddings.npy"
IDS_PATH = DATA_DIR / "embeddings_ids.json"
CHUNK_DIR = DATA_DIR / "cache" / "emb_chunks"
QCACHE = DATA_DIR / "cache" / "query_embeddings.json"
_qlock = threading.Lock()


def _strip_bad_chars(s: str) -> str:
    return "".join(
        ch for ch in s
        if ch != "\x00" and not (0xD800 <= ord(ch) <= 0xDFFF)
    )


def _clean_pid_arg(val: Any) -> Optional[str]:
    if isinstance(val, bool) or not isinstance(val, str):
        return None
    if "\x00" in val:
        return None
    try:
        val.encode("utf-8")
    except UnicodeEncodeError:
        return None
    s = val.strip()
    if not s or len(s) > 128:
        return None
    return s


def _validate_k(k: Any) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    return k


def _clean_query_text(q: Any) -> str:
    if isinstance(q, bool) or not isinstance(q, str):
        raise ValueError("query must be a string")
    cleaned = _strip_bad_chars(q).strip()
    if not cleaned:
        raise ValueError("query must be a non-empty string")
    return cleaned[:MAX_QUERY_CHARS]


def _valid_cached_vec(raw: Any) -> Optional[np.ndarray]:
    if not isinstance(raw, list) or len(raw) != DIM:
        return None
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        if not math.isfinite(float(x)):
            return None
    return np.array(raw, dtype=np.float32)


def product_text(d: dict) -> str:
    if not isinstance(d, dict):
        return ""
    name_en = _strip_bad_chars(d["name_en"]) if isinstance(d.get("name_en"), str) else ""
    name_fi = _strip_bad_chars(d["name_fi"]) if isinstance(d.get("name_fi"), str) else ""
    parts = [name_en, name_fi]
    brand = _strip_bad_chars(d["brand"]) if isinstance(d.get("brand"), str) else ""
    if brand:
        parts.append(f"brand {brand}")
    raw_ing = d.get("ingredients_text")
    ing = _strip_bad_chars(raw_ing[:200]) if isinstance(raw_ing, str) else ""
    if ing:
        parts.append(f"ingredients: {ing}")
    raw_claims = d.get("claims")
    claims: list[str] = []
    if raw_claims is not None and not isinstance(raw_claims, (str, bytes, bytearray, dict)):
        try:
            it = iter(raw_claims)
        except TypeError:
            it = iter(())
        for c in it:
            if isinstance(c, str) and (c.startswith("FREE_FROM") or c in ("VEGAN", "VEGETARIAN", "ORGANIC")):
                claims.append(_strip_bad_chars(c))
    if claims:
        parts.append(" ".join(c.lower().replace("_", " ") for c in claims if c))
    return " | ".join(p for p in parts if p)


def _embed_batch(texts: list[str], feature: str, retries: int = 5) -> np.ndarray:
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    from google.genai import types

    from aimo import gemini
    tries = retries if (not isinstance(retries, bool) and isinstance(retries, int) and retries >= 1) else 1
    last: Exception | None = None
    for attempt in range(tries):
        t0 = time.time()
        try:
            r = gemini.client().models.embed_content(
                model=GEMINI_EMBED_MODEL, contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
                config=types.EmbedContentConfig(output_dimensionality=DIM))
            gemini.record(feature, GEMINI_EMBED_MODEL, kind="embed", latency_s=time.time() - t0, meta={"n": len(texts)})
            v = np.array([e.values for e in r.embeddings], dtype=np.float32)
            if v.shape != (len(texts), DIM):
                raise RuntimeError(f"embedding shape mismatch {v.shape} != {(len(texts), DIM)}")
            v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
            v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
            return v
        except Exception as e:
            last = e
            gemini.record(feature, GEMINI_EMBED_MODEL, kind="embed", latency_s=time.time() - t0, ok=False,
                          error=type(e).__name__, meta={"n": len(texts)})
            if attempt + 1 < tries:
                time.sleep(min(30, 2 ** attempt))
    if last is not None:
        raise last
    raise RuntimeError("embed_batch failed")


def build(concurrency: int = 8, batch: int = 100) -> None:
    from aimo.backend.catalog_store import connect, row_to_dict
    con = connect()
    rows = [row_to_dict(r) for r in con.execute("select * from products order by id")]
    ids = [r["id"] for r in rows]
    texts = [product_text(r) for r in rows]
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    eff_batch = max(1, int(batch)) if (not isinstance(batch, bool) and isinstance(batch, int)) else 100
    eff_workers = max(1, min(8, int(concurrency))) if (not isinstance(concurrency, bool) and isinstance(concurrency, int)) else 8
    chunks = [(i, texts[i:i + eff_batch]) for i in range(0, len(texts), eff_batch)]

    def work(item):
        i, tx = item
        p = CHUNK_DIR / f"{i:06d}-{len(tx)}.npy"
        if p.exists():
            return
        v = _embed_batch(tx, "catalog.embed.build")
        np.save(p, v.astype(np.float16))

    from aimo import gemini
    gemini.client()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=eff_workers) as ex:
        for n, _ in enumerate(ex.map(work, chunks), 1):
            if n % 20 == 0:
                print(f"  {n}/{len(chunks)} chunks, {time.time() - t0:.0f}s", flush=True)
    mats = [np.load(CHUNK_DIR / f"{i:06d}-{len(tx)}.npy", allow_pickle=False) for i, tx in chunks]
    M = np.concatenate(mats).astype(np.float16) if mats else np.zeros((0, DIM), dtype=np.float16)
    if M.shape != (len(ids), DIM):
        raise ValueError(f"built embeddings shape mismatch: {M.shape} != {(len(ids), DIM)}")
    EMB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_emb = EMB_PATH.with_name(f"{EMB_PATH.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npy")
    np.save(tmp_emb, M)
    tmp_emb.replace(EMB_PATH)
    tmp_ids = IDS_PATH.with_name(f"{IDS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_ids.write_text(json.dumps(ids), encoding="utf-8")
    tmp_ids.replace(IDS_PATH)
    _matrix.cache_clear()
    print(f"embeddings: {M.shape} in {time.time() - t0:.0f}s -> {EMB_PATH}")


@lru_cache(maxsize=1)
def _matrix() -> tuple[np.ndarray, list[str], dict[str, int]]:
    if not EMB_PATH.exists() or not IDS_PATH.exists():
        raise FileNotFoundError("embeddings not built (uv run python -m aimo.catalog.embeddings build)")
    try:
        ids = json.loads(IDS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ValueError("corrupt embeddings ids JSON") from exc
    if not isinstance(ids, list) or any(isinstance(x, bool) or not isinstance(x, str) or not x.strip() for x in ids):
        raise ValueError("embeddings ids must be a list of non-empty strings")
    try:
        raw_m = np.load(EMB_PATH, allow_pickle=False)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ValueError("corrupt embeddings matrix") from exc
    if not isinstance(raw_m, np.ndarray) or raw_m.ndim != 2 or raw_m.shape != (len(ids), DIM):
        raise ValueError(f"embeddings shape mismatch: got {getattr(raw_m, 'shape', None)}, expected {(len(ids), DIM)}")
    if not np.issubdtype(raw_m.dtype, np.number):
        raise ValueError(f"embeddings matrix must be numeric, got {raw_m.dtype}")
    M = np.nan_to_num(raw_m.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    M.flags.writeable = False
    return M, ids, {pid: i for i, pid in enumerate(ids)}


def available() -> bool:
    return EMB_PATH.exists() and IDS_PATH.exists()


def _qcache() -> dict:
    try:
        data = json.loads(QCACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=512)
def _embed_query_cached(cleaned: str) -> np.ndarray:
    key = cleaned.lower()
    with _qlock:
        cache = _qcache()
        if key in cache:
            vec = _valid_cached_vec(cache[key])
            if vec is not None:
                return vec
    from aimo.catalog.netcheck import gemini_online, mark_offline
    if not gemini_online():
        raise RuntimeError("semantic recall offline")
    try:
        v = _embed_batch([cleaned], "catalog.embed.query", retries=1)[0]
    except Exception:
        mark_offline()
        raise
    v = np.nan_to_num(np.asarray(v, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if v.shape != (DIM,):
        raise ValueError(f"query embedding shape mismatch: {v.shape} != {(DIM,)}")
    with _qlock:
        cache = _qcache()
        cache[key] = [round(float(x), 5) for x in v]
        QCACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QCACHE.with_name(f"{QCACHE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(cache, allow_nan=False), encoding="utf-8")
        tmp.replace(QCACHE)
    return v


def embed_query(q: str) -> np.ndarray:
    cleaned = _clean_query_text(q)
    return _embed_query_cached(cleaned).copy()


embed_query.cache_clear = _embed_query_cached.cache_clear
embed_query.cache_info = _embed_query_cached.cache_info


def semantic_search(query: str, k: int = 30) -> list[tuple[str, float]]:
    _validate_k(k)
    q = _clean_query_text(query)
    M, ids, _ = _matrix()
    if not ids:
        return []
    qvec = embed_query(q)
    k_eff = min(k, len(ids))
    sims = np.nan_to_num(M @ qvec, nan=0.0, posinf=0.0, neginf=0.0)
    if k_eff >= len(ids):
        top = np.argsort(-sims)
    else:
        top = np.argpartition(-sims, k_eff - 1)[:k_eff]
        top = top[np.argsort(-sims[top])]
    return [(ids[i], float(sims[i])) for i in top]


def similar_products(product_id: str, k: int = 15) -> list[tuple[str, float]]:
    _validate_k(k)
    pid = _clean_pid_arg(product_id)
    if pid is None:
        return []
    M, ids, idx = _matrix()
    if len(ids) <= 1:
        return []
    i = idx.get(product_id) if product_id in idx else idx.get(pid)
    if i is None:
        return []
    sims = np.nan_to_num(M @ M[i], nan=0.0, posinf=0.0, neginf=0.0)
    k_eff = min(k, len(ids) - 1)
    if k_eff + 1 >= len(ids):
        top = np.argsort(-sims)
    else:
        top = np.argpartition(-sims, k_eff)[:k_eff + 1]
        top = top[np.argsort(-sims[top])]
    return [(ids[j], float(sims[j])) for j in top if j != i][:k_eff]


def similarity(a: str, b: str) -> Optional[float]:
    pa = _clean_pid_arg(a)
    pb = _clean_pid_arg(b)
    if pa is None or pb is None:
        return None
    try:
        M, _ids, idx = _matrix()
    except (FileNotFoundError, ValueError):
        return None
    ia = idx.get(a) if a in idx else idx.get(pa)
    ib = idx.get(b) if b in idx else idx.get(pb)
    if ia is None or ib is None:
        return None
    val = float(M[ia] @ M[ib])
    return val if math.isfinite(val) else 0.0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build()
    else:
        for pid, s in semantic_search(" ".join(sys.argv[1:]) or "lactose-free milk", k=10):
            print(pid, round(s, 3))
