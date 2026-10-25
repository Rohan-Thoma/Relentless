import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from src.db import get_connection


class CandidatePreferences(BaseModel):
    target_role_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    allowed_locations: list[str] = Field(default_factory=list)


class WorkdayTarget(BaseModel):
    company: str
    tenant: str
    wd_instance: str
    site: str


class ATSTargets(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    workday_cxs: list[WorkdayTarget] = Field(default_factory=list)
    smartrecruiters: list[str] = Field(default_factory=list)
    recruitee: list[str] = Field(default_factory=list)


class AutomationConfig(BaseModel):
    apply_mode: str = "DRY_RUN"
    daily_cap: int = 5
    per_company_cooldown_hours: int = 72
    min_fit_prob: float = 0.70
    allowed_tiers: list[str] = Field(default_factory=lambda: ["strong_fit", "dream_role"])
    max_free_text_questions: int = 0
    first_company_confirm: bool = True
    allowed_apply_domains: list[str] = Field(default_factory=list)


class JobboardsConfig(BaseModel):
    sites: list[str] = Field(default_factory=lambda: ["naukri", "linkedin", "indeed", "google"])
    search_terms: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    results_wanted: int = 20
    hours_old: int = 72


class AppConfig(BaseModel):
    candidate_preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)
    ats_targets: ATSTargets = Field(default_factory=ATSTargets)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    jobboards: JobboardsConfig = Field(default_factory=JobboardsConfig)


_config_cache: dict[str, Any] = {"value": None, "file_mtime": None, "db_updated": None}


def _file_mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return None


def _db_updated_at() -> str | None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM app_settings WHERE key = 'config_yaml';")
            row = cur.fetchone()
            if row and row.get("updated_at") is not None:
                return str(row["updated_at"])
    except Exception:
        return None
    return None


def load_config(force: bool = False) -> AppConfig:
    fm = _file_mtime("config/config.yaml")
    du = _db_updated_at()
    if (
        not force
        and _config_cache["value"] is not None
        and _config_cache["file_mtime"] == fm
        and _config_cache["db_updated"] == du
    ):
        return _config_cache["value"]

    # file fallback
    file_cfg = {}
    try:
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        file_cfg = {}

    # db overlay
    db_cfg = None
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = 'config_yaml';")
                row = cur.fetchone()
                if row and row.get("value"):
                    db_cfg = row["value"]
        except Exception:
            db_cfg = None

    merged = {}
    # start with file
    merged.update(file_cfg or {})
    # db overrides
    if db_cfg:
        for k, v in db_cfg.items():
            merged[k] = v
    # env overrides for automation switches
    # (none for full config, but allow APPLY_MODE env)
    if os.getenv("APPLY_MODE"):
        merged.setdefault("automation", {})
        merged["automation"]["apply_mode"] = os.getenv("APPLY_MODE")

    try:
        cfg = AppConfig(**merged)
    except ValidationError as e:
        raise ValueError(f"Invalid configuration: {e}")

    _config_cache["value"] = cfg
    _config_cache["file_mtime"] = fm
    _config_cache["db_updated"] = du
    return cfg


def invalidate_config():
    global _config_cache
    _config_cache = {"value": None, "file_mtime": None, "db_updated": None}