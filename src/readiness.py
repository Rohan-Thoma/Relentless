"""
Fail-loud system readiness & configuration diagnostics for Relentless.

Never exposes secret values; reports only configuration presence, DB health,
profile completeness, and actionable remediation steps for the CLI, Web UI,
startup checks, and Telegram alerts.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config import load_config
from src.db import _dsn, get_connection
from src.settings_store import ProfileNotConfigured, load_master_profile

logger = logging.getLogger("relentless.readiness")


def _has_env(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def collect_readiness(check_db: bool = True) -> dict[str, Any]:
    """Evaluates required & optional dependencies and returns structured readiness diagnostics."""
    checks: list[dict[str, str]] = []
    missing_required: list[str] = []
    warnings: list[str] = []

    # 1. PostgreSQL Database
    dsn = _dsn()
    db_ok = False
    has_db_profile = False
    has_db_config = False
    if not dsn:
        missing_required.append("DATABASE_URL")
        checks.append(
            {
                "id": "database",
                "label": "PostgreSQL (Neon)",
                "status": "error",
                "detail": "DATABASE_URL is not set in environment.",
                "fix": "Create a free Neon Postgres project, copy the connection URI, and set DATABASE_URL in .env or host variables.",
            }
        )
    elif check_db:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                cur.fetchone()
                cur.execute(
                    "SELECT key FROM app_settings WHERE key IN ('master_profile', 'config_yaml');"
                )
                keys = {row["key"] for row in cur.fetchall()}
                has_db_profile = "master_profile" in keys
                has_db_config = "config_yaml" in keys
            db_ok = True
            checks.append(
                {
                    "id": "database",
                    "label": "PostgreSQL (Neon)",
                    "status": "ok",
                    "detail": "Connected; schema reachable.",
                    "fix": "",
                }
            )
        except Exception as e:
            missing_required.append("DATABASE_CONNECTION")
            checks.append(
                {
                    "id": "database",
                    "label": "PostgreSQL (Neon)",
                    "status": "error",
                    "detail": f"Database connection failed: {e}",
                    "fix": "Verify DATABASE_URL credentials/SSL mode and run migrations.",
                }
            )
    else:
        db_ok = True
        checks.append(
            {
                "id": "database",
                "label": "PostgreSQL (Neon)",
                "status": "ok",
                "detail": "DATABASE_URL is configured.",
                "fix": "",
            }
        )

    # 2. AppConfig (config/config.yaml + DB override)
    cfg = None
    config_configured = False
    try:
        cfg = load_config()
        config_configured = True
        checks.append(
            {
                "id": "engine_config",
                "label": "Engine Config (AppConfig)",
                "status": "ok",
                "detail": (
                    f"Apply mode: {cfg.automation.normalized_apply_mode()}; "
                    f"tailor: {cfg.models.tailor.provider}/{cfg.models.tailor.model}; "
                    f"embeddings: {cfg.models.embeddings.provider}/{cfg.models.embeddings.model}; "
                    f"location rules: {len(cfg.effective_location_policy().rules)} ({cfg.effective_location_policy().operator}); "
                    f"sources synced: {'DB' if has_db_config else 'config.yaml'}."
                ),
                "fix": "",
            }
        )
    except Exception as e:
        missing_required.append("ENGINE_CONFIG")
        checks.append(
            {
                "id": "engine_config",
                "label": "Engine Config (AppConfig)",
                "status": "error",
                "detail": f"Config load failed: {e}",
                "fix": "Fix config/config.yaml or re-save settings in 06 // CONFIG & VAULT.",
            }
        )

    # 3. Master Profile
    profile_configured = False
    profile_error: str | None = None
    missing_profile_fields: list[str] = []
    is_template_profile = False
    profile_data: dict[str, Any] | None = None
    try:
        profile_data = load_master_profile()
        if not isinstance(profile_data, dict):
            raise ProfileNotConfigured("Master profile must be a JSON object.")

        name = str(profile_data.get("name") or "").strip()
        email = str(profile_data.get("email") or "").strip()
        location = str(profile_data.get("location") or "").strip()
        skills = profile_data.get("core_skills") or []
        exp = profile_data.get("experience") or []

        if not name:
            missing_profile_fields.append("name")
        if not email:
            missing_profile_fields.append("email")
        if not location:
            missing_profile_fields.append("location")
        if not isinstance(skills, list) or len(skills) == 0:
            missing_profile_fields.append("core_skills")
        if not isinstance(exp, list) or len(exp) == 0:
            missing_profile_fields.append("experience")

        if name == "Alex Developer" or email == "alex@example.com":
            is_template_profile = True
            warnings.append(
                "Master profile still contains starter template placeholders (Alex Developer). Customize it in 06 // CONFIG & VAULT."
            )

        if missing_profile_fields:
            profile_error = (
                f"Master profile is incomplete; missing: {', '.join(missing_profile_fields)}."
            )
            missing_required.append("MASTER_PROFILE_FIELDS")
            checks.append(
                {
                    "id": "master_profile",
                    "label": "Candidate Master Profile",
                    "status": "error",
                    "detail": profile_error,
                    "fix": "Complete all required fields in 06 // CONFIG & VAULT -> Master Profile and click Save.",
                }
            )
        else:
            profile_configured = True
            src_label = "DB + local" if has_db_profile else "local / env"
            status_val = "warning" if is_template_profile else "ok"
            detail_val = (
                "Starter template active (Alex Developer) — replace with your own resume data."
                if is_template_profile
                else f"Configured for {name} ({len(exp)} role(s), {len(skills)} skills; {src_label})."
            )
            checks.append(
                {
                    "id": "master_profile",
                    "label": "Candidate Master Profile",
                    "status": status_val,
                    "detail": detail_val,
                    "fix": "Update your personal details in 06 // CONFIG & VAULT."
                    if is_template_profile
                    else "",
                }
            )
    except ProfileNotConfigured as e:
        profile_error = str(e)
        missing_required.append("MASTER_PROFILE")
        checks.append(
            {
                "id": "master_profile",
                "label": "Candidate Master Profile",
                "status": "error",
                "detail": profile_error,
                "fix": "Open 06 // CONFIG & VAULT -> Candidate Master Profile in the Web UI (or run python manage.py setup).",
            }
        )
    except Exception as e:
        profile_error = f"Failed to load master profile: {e}"
        missing_required.append("MASTER_PROFILE")
        checks.append(
            {
                "id": "master_profile",
                "label": "Candidate Master Profile",
                "status": "error",
                "detail": profile_error,
                "fix": "Inspect logs and fix master_profile JSON via 06 // CONFIG & VAULT.",
            }
        )

    # 4. AI Triage Key (OpenRouter or TypeSafe)
    has_openrouter = _has_env("OPENROUTER_API_KEY")
    has_typesafe = _has_env("TYPESAFE_API_KEY")
    if has_openrouter or has_typesafe:
        provider = "TypeSafe Direct" if has_typesafe else "OpenRouter Decisions"
        checks.append(
            {
                "id": "triage_llm",
                "label": "AI Triage Key (Jev)",
                "status": "ok",
                "detail": f"Configured ({provider}).",
                "fix": "",
            }
        )
    else:
        missing_required.append("OPENROUTER_API_KEY")
        checks.append(
            {
                "id": "triage_llm",
                "label": "AI Triage Key (Jev)",
                "status": "error",
                "detail": "Neither OPENROUTER_API_KEY nor TYPESAFE_API_KEY is set.",
                "fix": "Create an OpenRouter key (openrouter.ai) and set OPENROUTER_API_KEY in .env or Railway Variables.",
            }
        )

    # 5. OpenAI Key (Tailor PDF & LLM Form Agent fallback)
    if _has_env("OPENAI_API_KEY"):
        checks.append(
            {
                "id": "tailor_llm",
                "label": "Resume Tailor Key (OpenAI)",
                "status": "ok",
                "detail": "OPENAI_API_KEY configured.",
                "fix": "",
            }
        )
    else:
        msg = "OPENAI_API_KEY is not set; clicking Tailor Resume PDF will fail until configured."
        warnings.append(msg)
        checks.append(
            {
                "id": "tailor_llm",
                "label": "Resume Tailor Key (OpenAI)",
                "status": "warning",
                "detail": msg,
                "fix": "Set OPENAI_API_KEY from platform.openai.com for GPT-4o-mini resume synthesis.",
            }
        )

    # 6. Telegram HITL Control
    has_tg_token = _has_env("TELEGRAM_BOT_TOKEN")
    has_tg_chat = _has_env("TELEGRAM_CHAT_ID")
    if has_tg_token and has_tg_chat:
        checks.append(
            {
                "id": "telegram",
                "label": "Telegram Bot & Alerts",
                "status": "ok",
                "detail": "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID configured.",
                "fix": "",
            }
        )
    else:
        missing_tg = [
            k
            for k, present in (
                ("TELEGRAM_BOT_TOKEN", has_tg_token),
                ("TELEGRAM_CHAT_ID", has_tg_chat),
            )
            if not present
        ]
        msg = f"Missing {', '.join(missing_tg)}; Telegram alerts & /logs bot commands are disabled."
        warnings.append(msg)
        checks.append(
            {
                "id": "telegram",
                "label": "Telegram Bot & Alerts",
                "status": "warning",
                "detail": msg,
                "fix": "Create a bot via @BotFather, get your numeric chat ID via @userinfobot, and set both env vars.",
            }
        )

    # 7. Web Security Key
    if _has_env("WEB_API_KEY") or _has_env("WEB_ADMIN_PASSWORD"):
        checks.append(
            {
                "id": "web_security",
                "label": "Web Access Security",
                "status": "ok",
                "detail": "WEB_API_KEY / WEB_ADMIN_PASSWORD configured for remote login.",
                "fix": "",
            }
        )
    else:
        msg = "Neither WEB_API_KEY nor WEB_ADMIN_PASSWORD is set; remote web access will deny logins."
        warnings.append(msg)
        checks.append(
            {
                "id": "web_security",
                "label": "Web Access Security",
                "status": "warning",
                "detail": msg,
                "fix": "Generate a random secret and set WEB_API_KEY in .env or Railway Variables.",
            }
        )

    # 8. Optional Gmail Scanner
    if cfg and cfg.ingestion.enable_gmail:
        has_gm_addr = _has_env("GMAIL_ADDRESS")
        has_gm_pass = _has_env("GMAIL_APP_PASSWORD")
        if has_gm_addr and has_gm_pass:
            checks.append(
                {
                    "id": "gmail",
                    "label": "Gmail Job Alert Scanner",
                    "status": "ok",
                    "detail": f"Enabled for label '{cfg.gmail_scanner.label}'.",
                    "fix": "",
                }
            )
        else:
            missing_gm = [
                k
                for k, present in (
                    ("GMAIL_ADDRESS", has_gm_addr),
                    ("GMAIL_APP_PASSWORD", has_gm_pass),
                )
                if not present
            ]
            msg = f"Gmail scanner is enabled in config, but {', '.join(missing_gm)} is missing."
            warnings.append(msg)
            checks.append(
                {
                    "id": "gmail",
                    "label": "Gmail Job Alert Scanner",
                    "status": "warning",
                    "detail": msg,
                    "fix": "Set a Google App Password + GMAIL_ADDRESS, or disable Gmail Alerts Scanner in 06 // CONFIG & VAULT.",
                }
            )
    else:
        checks.append(
            {
                "id": "gmail",
                "label": "Gmail Job Alert Scanner",
                "status": "disabled",
                "detail": "Disabled in Engine Config.",
                "fix": "",
            }
        )

    ready = len(missing_required) == 0 and db_ok and profile_configured and config_configured
    return {
        "ready": ready,
        "db_ok": db_ok,
        "profile_configured": profile_configured,
        "profile_error": profile_error,
        "missing_profile_fields": missing_profile_fields,
        "is_template_profile": is_template_profile,
        "config_configured": config_configured,
        "missing_required": missing_required,
        "warnings": warnings,
        "checks": checks,
    }


def log_startup_readiness(report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Emits explicit ERROR/WARNING/INFO log lines for each readiness check."""
    rep = report if report is not None else collect_readiness(check_db=True)
    for chk in rep["checks"]:
        status = chk["status"]
        line = f"[{chk['label']}] {chk['detail']}"
        if chk.get("fix"):
            line += f" | Fix: {chk['fix']}"
        if status == "error":
            logger.error(line)
        elif status == "warning":
            logger.warning(line)
        elif status == "ok":
            logger.info(line)
    return rep
