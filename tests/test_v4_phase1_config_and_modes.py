"""Phase 1 tests: Config presets, apply_mode normalization, models catalog, and daemon/CLI producer resolution."""

import pytest

from src.config import (
    AUTOMATION_PRESETS,
    AppConfig,
    AutomationConfig,
    IngestionConfig,
    apply_automation_preset,
    load_config,
)
from src.main_loop import _resolve_active_producers
from src.model_catalog import (
    fetch_live_model_catalog,
    resolve_chat_endpoint,
    resolve_embedding_endpoint,
    resolve_triage_endpoint,
)


def test_real_automation_presets_and_tier_sanitization():
    # Selecting preset without explicit overrides populates real preset values
    auto_bal = AutomationConfig(preset="balanced")
    assert auto_bal.normalized_apply_mode() == "CONFIRM_SUBMIT"
    assert auto_bal.per_company_cooldown_hours == 48
    assert auto_bal.min_fit_prob == 0.62
    assert "good_fit" not in auto_bal.allowed_tiers
    assert "moderate_fit" in auto_bal.allowed_tiers

    # Explicit overrides still take precedence over preset
    auto_override = AutomationConfig(preset="fast", apply_mode="DRY_RUN", daily_cap=2)
    assert auto_override.normalized_apply_mode() == "PREPARE_ONLY"
    assert auto_override.daily_cap == 2
    assert auto_override.per_company_cooldown_hours == 24

    # Legacy nonexistent 'good_fit' tier gets sanitized to 'moderate_fit'
    auto_legacy = AutomationConfig(allowed_tiers=["strong_fit", "good_fit", "bogus_tier"])
    assert auto_legacy.allowed_tiers == ["strong_fit", "moderate_fit"]

    # Helper apply_automation_preset stamps values explicitly
    stamped = apply_automation_preset({"automation": {"daily_cap": 99}}, "safe")
    assert stamped["automation"]["daily_cap"] == AUTOMATION_PRESETS["safe"]["daily_cap"]
    assert stamped["automation"]["apply_mode"] == "PREPARE_ONLY"


def test_apply_mode_legacy_aliases():
    assert AutomationConfig(apply_mode="DRY_RUN").normalized_apply_mode() == "PREPARE_ONLY"
    assert AutomationConfig(apply_mode="PREPARE_ONLY").normalized_apply_mode() == "PREPARE_ONLY"
    assert AutomationConfig(apply_mode="MANUAL").normalized_apply_mode() == "MANUAL_ONLY"
    assert AutomationConfig(apply_mode="MANUAL_ONLY").normalized_apply_mode() == "MANUAL_ONLY"
    assert AutomationConfig(apply_mode="AUTO_CONFIRM").normalized_apply_mode() == "CONFIRM_SUBMIT"
    assert AutomationConfig(apply_mode="LIVE").normalized_apply_mode() == "CONFIRM_SUBMIT"
    assert AutomationConfig(apply_mode="CONFIRM_SUBMIT").normalized_apply_mode() == "CONFIRM_SUBMIT"


def test_config_yaml_loads_cleanly_with_v4_defaults():
    cfg = load_config(force=True)
    assert cfg.models.tailor.provider == "openai"
    assert cfg.models.tailor.model == "gpt-4o"
    assert cfg.models.browser_agent.model == "gpt-4o"
    assert cfg.models.embeddings.provider == "openai"
    assert cfg.models.embeddings.model == "text-embedding-3-small"
    assert cfg.models.triage.model == "typesafe/jev-1.14"

    # Default location policy is strict remote-only
    policy = cfg.effective_location_policy()
    assert len(policy.rules) == 1
    assert policy.rules[0].work_modes == ["remote"]

    # Intern roles excluded by default
    assert "intern" in cfg.candidate_preferences.exclude_title_keywords

    # Curated ATS targets have zero cross-platform duplicates across Greenhouse/Lever/Ashby
    gh = set(cfg.ats_targets.greenhouse)
    lv = set(cfg.ats_targets.lever)
    ab = set(cfg.ats_targets.ashby)
    assert not (gh & lv)
    assert not (gh & ab)
    assert not (lv & ab)
    assert "StarbucksAU" not in cfg.ats_targets.smartrecruiters
    assert cfg.ats_targets.recruitee == []


def test_resolve_active_producers_fixes_inverted_ats_only_and_honors_flags():
    cfg_all = AppConfig(
        ingestion=IngestionConfig(
            enable_jobboards=True,
            enable_gmail=True,
            enable_ats=True,
            enable_workday=False,
            enable_remote_feeds=True,
        )
    )

    # Normal mode honors config
    act = _resolve_active_producers(cfg_all)
    assert act["jobboards"] is True
    assert act["gmail"] is True
    assert act["ats"] is True
    assert act["workday"] is False
    assert act["remote_feeds"] is True

    # --ats-only MUST enable ATS and disable JobSpy/Gmail (previously inverted!)
    act_ats = _resolve_active_producers(cfg_all, ats_only=True)
    assert act_ats["ats"] is True
    assert act_ats["jobboards"] is False
    assert act_ats["gmail"] is False

    # Disabling jobboards/gmail in config MUST disable them in daemon mode too
    cfg_none = AppConfig(
        ingestion=IngestionConfig(
            enable_jobboards=False,
            enable_gmail=False,
            enable_ats=False,
            enable_remote_feeds=False,
        )
    )
    act_none = _resolve_active_producers(cfg_none)
    assert act_none == {
        "jobboards": False,
        "gmail": False,
        "ats": False,
        "workday": False,
        "remote_feeds": False,
    }


@pytest.mark.asyncio
async def test_model_catalog_and_endpoint_routing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    url, key, model = resolve_chat_endpoint("openai", "gpt-4o")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert key == "sk-test-openai"
    assert model == "gpt-4o"

    url_or, key_or, model_or = resolve_chat_endpoint("openrouter", "anthropic/claude-sonnet-4")
    assert url_or == "https://openrouter.ai/api/v1/chat/completions"
    assert key_or == "sk-or-test"
    assert model_or == "anthropic/claude-sonnet-4"

    e_url, e_key, e_model = resolve_embedding_endpoint("openai", "text-embedding-3-small")
    assert e_url == "https://api.openai.com/v1/embeddings"
    assert e_key == "sk-test-openai"
    assert e_model == "text-embedding-3-small"

    t_url, t_key, t_model = resolve_triage_endpoint("openrouter", "typesafe/jev-1.14")
    assert t_url == "https://openrouter.ai/api/alpha/decisions"
    assert t_key == "sk-or-test"
    assert t_model == "typesafe/jev-1.14"

    catalog = await fetch_live_model_catalog(force=True)
    assert catalog["providers"]["openai"] is True
    assert catalog["providers"]["openrouter"] is True
    assert any(m["id"] == "gpt-4o" for m in catalog["chat_models"]["openai"])
    assert any(m["id"] == "text-embedding-3-small" for m in catalog["embedding_models"]["openai"])
