"""Provider-agnostic LLM & Embedding model catalog and endpoint resolution.

Supports OpenAI (OPENAI_API_KEY), OpenRouter (OPENROUTER_API_KEY), and TypeSafe
Direct (TYPESAFE_API_KEY) with live model listing for the Web UI model picker.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Curated recommended models shown in Web UI and referenced in config.yaml comments
CURATED_CHAT_MODELS: dict[str, list[dict[str, Any]]] = {
    "openai": [
        {"id": "gpt-4o", "label": "GPT-4o (Recommended — Vision + Strong Reasoning)", "vision": True, "tier": "balanced"},
        {"id": "gpt-4.1", "label": "GPT-4.1 (Top Precision Tailoring & Agents)", "vision": True, "tier": "frontier"},
        {"id": "gpt-4.1-mini", "label": "GPT-4.1 Mini (Fast & Economical)", "vision": True, "tier": "economy"},
        {"id": "gpt-4o-mini", "label": "GPT-4o Mini (Ultra-Low Cost)", "vision": True, "tier": "economy"},
        {"id": "o4-mini", "label": "o4-mini (Reasoning)", "vision": True, "tier": "reasoning"},
    ],
    "openrouter": [
        {"id": "openai/gpt-4o", "label": "OpenAI GPT-4o (via OpenRouter)", "vision": True, "tier": "balanced"},
        {"id": "openai/gpt-4.1", "label": "OpenAI GPT-4.1 (via OpenRouter)", "vision": True, "tier": "frontier"},
        {"id": "anthropic/claude-sonnet-4", "label": "Claude Sonnet 4 (via OpenRouter)", "vision": True, "tier": "frontier"},
        {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro (via OpenRouter)", "vision": True, "tier": "frontier"},
        {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash (via OpenRouter)", "vision": True, "tier": "economy"},
        {"id": "deepseek/deepseek-chat-v3-0324", "label": "DeepSeek V3 (via OpenRouter)", "vision": False, "tier": "economy"},
    ],
}

CURATED_EMBEDDING_MODELS: dict[str, list[dict[str, Any]]] = {
    "openai": [
        {"id": "text-embedding-3-small", "label": "OpenAI text-embedding-3-small (1536d — Default, ~$0.02/1M tokens)", "dim": 1536},
        {"id": "text-embedding-3-large", "label": "OpenAI text-embedding-3-large (1536d truncated — High Precision)", "dim": 1536},
    ],
    "openrouter": [
        {"id": "openai/text-embedding-3-small", "label": "OpenAI text-embedding-3-small via OpenRouter (1536d)", "dim": 1536},
        {"id": "openai/text-embedding-3-large", "label": "OpenAI text-embedding-3-large via OpenRouter (1536d)", "dim": 1536},
    ],
}

CURATED_TRIAGE_MODELS: dict[str, list[dict[str, Any]]] = {
    "openrouter": [
        {"id": "typesafe/jev-1.14", "label": "TypeSafe Jev 1.14 via OpenRouter Decisions (Default)"},
    ],
    "typesafe": [
        {"id": "jev-1.13.0", "label": "TypeSafe Jev 1.13.0 Direct (/v1/systemone)"},
    ],
}

_LIVE_CATALOG_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_LIVE_CACHE_TTL = 900.0  # 15 minutes


def provider_key_status() -> dict[str, bool]:
    return {
        "openai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "typesafe": bool(os.getenv("TYPESAFE_API_KEY", "").strip()),
    }


def resolve_chat_endpoint(provider: str, model: str) -> tuple[str, str, str]:
    """Resolves (url, api_key, model_id) for chat completions with safe provider fallback."""
    prov = (provider or "openai").strip().lower()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if prov == "openai" and openai_key:
        clean_model = model.removeprefix("openai/")
        return "https://api.openai.com/v1/chat/completions", openai_key, clean_model
    if prov == "openrouter" and openrouter_key:
        return "https://openrouter.ai/api/v1/chat/completions", openrouter_key, model

    # Fallback to whichever key is configured
    if openai_key:
        clean_model = model.removeprefix("openai/")
        return "https://api.openai.com/v1/chat/completions", openai_key, clean_model
    if openrouter_key:
        or_model = model if "/" in model else f"openai/{model}"
        return "https://openrouter.ai/api/v1/chat/completions", openrouter_key, or_model
    return "", "", model


def resolve_embedding_endpoint(provider: str, model: str) -> tuple[str, str, str]:
    """Resolves (url, api_key, model_id) for embeddings API."""
    prov = (provider or "openai").strip().lower()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if prov == "openai" and openai_key:
        clean_model = model.removeprefix("openai/")
        return "https://api.openai.com/v1/embeddings", openai_key, clean_model
    if prov == "openrouter" and openrouter_key:
        or_model = model if "/" in model else f"openai/{model}"
        return "https://openrouter.ai/api/v1/embeddings", openrouter_key, or_model

    if openai_key:
        return "https://api.openai.com/v1/embeddings", openai_key, model.removeprefix("openai/")
    if openrouter_key:
        or_model = model if "/" in model else f"openai/{model}"
        return "https://openrouter.ai/api/v1/embeddings", openrouter_key, or_model
    return "", "", model


def resolve_triage_endpoint(provider: str, model: str) -> tuple[str, str, str]:
    """Resolves (url, api_key, model_id) for Jev System-1 Decisions API."""
    prov = (provider or "openrouter").strip().lower()
    typesafe_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if prov == "typesafe" and typesafe_key:
        return "https://api.typesafe.ai/v1/systemone", typesafe_key, model or "jev-1.13.0"
    if prov == "openrouter" and openrouter_key:
        return "https://openrouter.ai/api/alpha/decisions", openrouter_key, model or "typesafe/jev-1.14"

    if typesafe_key:
        return "https://api.typesafe.ai/v1/systemone", typesafe_key, "jev-1.13.0"
    if openrouter_key:
        return "https://openrouter.ai/api/alpha/decisions", openrouter_key, "typesafe/jev-1.14"
    return "", "", model


async def fetch_live_model_catalog(force: bool = False) -> dict[str, Any]:
    """Returns curated + live-discovered models for configured providers (cached 15 min)."""
    now = time.monotonic()
    if (
        not force
        and _LIVE_CATALOG_CACHE["data"] is not None
        and (now - _LIVE_CATALOG_CACHE["ts"]) < _LIVE_CACHE_TTL
    ):
        return _LIVE_CATALOG_CACHE["data"]

    keys = provider_key_status()
    chat_models = {k: list(v) for k, v in CURATED_CHAT_MODELS.items()}
    embedding_models = {k: list(v) for k, v in CURATED_EMBEDDING_MODELS.items()}
    triage_models = {k: list(v) for k, v in CURATED_TRIAGE_MODELS.items()}

    # Avoid network calls during unit tests unless explicitly allowed
    if not os.getenv("PYTEST_CURRENT_TEST"):
        async with httpx.AsyncClient(timeout=8.0) as client:
            if keys["openai"]:
                try:
                    resp = await client.get(
                        "https://api.openai.com/v1/models",
                        headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '').strip()}"},
                    )
                    if resp.status_code == 200:
                        existing_chat = {m["id"] for m in chat_models["openai"]}
                        existing_emb = {m["id"] for m in embedding_models["openai"]}
                        for row in sorted(resp.json().get("data", []), key=lambda x: x.get("id", "")):
                            mid = str(row.get("id") or "")
                            if not mid:
                                continue
                            if "embedding" in mid and mid not in existing_emb:
                                embedding_models["openai"].append({"id": mid, "label": mid, "dim": 1536})
                                existing_emb.add(mid)
                            elif (
                                mid.startswith(("gpt-4", "gpt-5", "o1", "o3", "o4"))
                                and "realtime" not in mid
                                and "audio" not in mid
                                and "transcribe" not in mid
                                and "tts" not in mid
                                and mid not in existing_chat
                            ):
                                chat_models["openai"].append({"id": mid, "label": mid, "vision": True, "tier": "live"})
                                existing_chat.add(mid)
                except Exception as e:
                    logger.debug("Live OpenAI model discovery skipped: %s", e)

            if keys["openrouter"]:
                try:
                    resp = await client.get(
                        "https://openrouter.ai/api/v1/models",
                        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '').strip()}"},
                    )
                    if resp.status_code == 200:
                        existing_or = {m["id"] for m in chat_models["openrouter"]}
                        popular_prefixes = ("openai/gpt-", "anthropic/claude-", "google/gemini-", "deepseek/", "meta-llama/")
                        for row in resp.json().get("data", []):
                            mid = str(row.get("id") or "")
                            if mid and mid.startswith(popular_prefixes) and mid not in existing_or:
                                name = row.get("name") or mid
                                chat_models["openrouter"].append({"id": mid, "label": f"{name} ({mid})", "vision": True, "tier": "live"})
                                existing_or.add(mid)
                            if len(chat_models["openrouter"]) >= 45:
                                break
                except Exception as e:
                    logger.debug("Live OpenRouter model discovery skipped: %s", e)

    result = {
        "providers": keys,
        "chat_models": chat_models,
        "embedding_models": embedding_models,
        "triage_models": triage_models,
    }
    _LIVE_CATALOG_CACHE["data"] = result
    _LIVE_CATALOG_CACHE["ts"] = now
    return result
