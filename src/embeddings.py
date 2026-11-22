"""Real API-backed 1536-dim embeddings (OpenAI / OpenRouter) with offline test fallback.

Powers:
1. Stage-0 semantic profile <-> JD similarity (stored in pgvector HNSW column `jobs.embedding`)
2. Tier-3 semantic duplicate/repost detection across job boards
3. Semantic RAG retrieval over OKF Knowledge Brain concepts
4. Honest semantic similarity signal inside the ATS Resume Scorecard
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import Any

import httpx

from src.config import load_config
from src.model_catalog import resolve_embedding_endpoint

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1536
_PROFILE_EMB_CACHE: dict[str, Any] = {"hash": None, "vec": None, "model": None}
_CONCEPT_EMB_CACHE: dict[str, tuple[str, list[float]]] = {}


class EmbeddingProvider:
    model_name: str = "base"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic offline provider used ONLY during unit tests or when no API key is set."""

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim
        self.model_name = "offline-deterministic-bow"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import re

        out = []
        for t in texts:
            vec = [0.0] * self.dim
            tokens = re.findall(r"[a-z0-9+#\.]{2,}", (t or "").lower())
            for tok in tokens:
                digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if (digest[4] & 1) == 0 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class APIEmbeddingProvider(EmbeddingProvider):
    """Calls OpenAI or OpenRouter Embeddings API (default: text-embedding-3-small, 1536d)."""

    def __init__(self, provider: str = "openai", model: str = "text-embedding-3-small", dim: int = EMBEDDING_DIM):
        self.provider = provider
        self.model_name = model
        self.dim = dim

    def _build_request(self, texts: list[str]) -> tuple[str, dict[str, str], dict[str, Any]] | None:
        url, api_key, model_id = resolve_embedding_endpoint(self.provider, self.model_name)
        if not url or not api_key:
            return None
        clean_texts = [(t or "").strip()[:16000] or "empty" for t in texts]
        payload: dict[str, Any] = {"model": model_id, "input": clean_texts}
        if "text-embedding-3" in model_id:
            payload["dimensions"] = self.dim
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        return url, headers, payload

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        req = self._build_request(texts)
        if req is None:
            return FakeEmbeddingProvider(self.dim).embed(texts)
        url, headers, payload = req
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                items = sorted(resp.json().get("data", []), key=lambda x: x.get("index", 0))
                vecs = [it["embedding"] for it in items if "embedding" in it]
                if len(vecs) == len(texts):
                    return vecs
            logger.warning("Embedding API returned %s; falling back to offline vector", resp.status_code)
        except Exception as e:
            logger.warning("Embedding API call failed (%s); falling back to offline vector", e)
        return FakeEmbeddingProvider(self.dim).embed(texts)

    async def aembed(self, texts: list[str], client: httpx.AsyncClient | None = None) -> list[list[float]]:
        if not texts:
            return []
        req = self._build_request(texts)
        if req is None:
            return FakeEmbeddingProvider(self.dim).embed(texts)
        url, headers, payload = req
        try:
            if client is not None:
                resp = await client.post(url, headers=headers, json=payload, timeout=20.0)
            else:
                async with httpx.AsyncClient(timeout=20.0) as c:
                    resp = await c.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                items = sorted(resp.json().get("data", []), key=lambda x: x.get("index", 0))
                vecs = [it["embedding"] for it in items if "embedding" in it]
                if len(vecs) == len(texts):
                    return vecs
            logger.warning("Async embedding API returned %s; falling back to offline vector", resp.status_code)
        except Exception as e:
            logger.warning("Async embedding API call failed (%s); falling back to offline vector", e)
        return FakeEmbeddingProvider(self.dim).embed(texts)


def get_provider() -> EmbeddingProvider:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_EMBEDDINGS"):
        return FakeEmbeddingProvider()
    try:
        emb_cfg = load_config().models.embeddings
        url, key, _ = resolve_embedding_endpoint(emb_cfg.provider, emb_cfg.model)
        if url and key:
            return APIEmbeddingProvider(provider=emb_cfg.provider, model=emb_cfg.model)
    except Exception:
        pass
    return FakeEmbeddingProvider()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


def embed_texts(texts: list[str]) -> list[list[float]]:
    return get_provider().embed(texts)


async def aembed_texts(texts: list[str], client: httpx.AsyncClient | None = None) -> list[list[float]]:
    prov = get_provider()
    if isinstance(prov, APIEmbeddingProvider):
        return await prov.aembed(texts, client=client)
    return prov.embed(texts)


def profile_to_embedding_text(profile: dict[str, Any]) -> str:
    parts = [
        str(profile.get("experience_summary") or ""),
        "Core Skills: " + ", ".join(str(s) for s in (profile.get("core_skills") or [])),
    ]
    for exp in profile.get("experience") or []:
        parts.append(f"{exp.get('role', '')} at {exp.get('company', '')}: " + " ".join(exp.get("bullets") or []))
    return "\n".join(p for p in parts if p).strip()


async def aembed_profile(profile: dict[str, Any], client: httpx.AsyncClient | None = None) -> tuple[list[float], str]:
    text = profile_to_embedding_text(profile)
    prov = get_provider()
    h = hashlib.sha256(f"{prov.model_name}|{text}".encode()).hexdigest()
    if _PROFILE_EMB_CACHE["hash"] == h and _PROFILE_EMB_CACHE["vec"] is not None:
        return _PROFILE_EMB_CACHE["vec"], prov.model_name
    vecs = await aembed_texts([text], client=client)
    vec = vecs[0] if vecs else []
    _PROFILE_EMB_CACHE["hash"] = h
    _PROFILE_EMB_CACHE["vec"] = vec
    _PROFILE_EMB_CACHE["model"] = prov.model_name
    return vec, prov.model_name


async def semantic_search_concepts(
    query_text: str,
    concepts: list[Any],
    top_k: int = 5,
    min_sim: float = 0.15,
    client: httpx.AsyncClient | None = None,
) -> list[Any]:
    """Semantically retrieves the top-K most relevant OKF concepts for a job or prompt."""
    if not concepts or not query_text:
        return []
    prov = get_provider()
    q_vecs = await aembed_texts([query_text[:6000]], client=client)
    if not q_vecs:
        return concepts[:top_k]
    q_vec = q_vecs[0]

    to_embed_indices: list[int] = []
    to_embed_texts: list[str] = []
    concept_vecs: list[list[float] | None] = [None] * len(concepts)

    for idx, c in enumerate(concepts):
        c_text = f"{c.concept_type}: {c.name} {' '.join(c.tags or [])}\n{c.body[:2000]}"
        ch = hashlib.sha256(f"{prov.model_name}|{c_text}".encode()).hexdigest()
        cached = _CONCEPT_EMB_CACHE.get(c.path)
        if cached and cached[0] == ch:
            concept_vecs[idx] = cached[1]
        else:
            to_embed_indices.append(idx)
            to_embed_texts.append(c_text)

    if to_embed_texts:
        new_vecs = await aembed_texts(to_embed_texts, client=client)
        for idx, vec in zip(to_embed_indices, new_vecs):
            c = concepts[idx]
            c_text = f"{c.concept_type}: {c.name} {' '.join(c.tags or [])}\n{c.body[:2000]}"
            ch = hashlib.sha256(f"{prov.model_name}|{c_text}".encode()).hexdigest()
            _CONCEPT_EMB_CACHE[c.path] = (ch, vec)
            concept_vecs[idx] = vec

    scored = []
    for c, vec in zip(concepts, concept_vecs):
        if vec is None:
            continue
        sim = cosine(q_vec, vec)
        if sim >= min_sim:
            scored.append((sim, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]
