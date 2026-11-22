from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
import yaml

from src.db import get_connection

logger = logging.getLogger(__name__)

# Valid Jev System-1 fit tiers (no nonexistent "good_fit"!)
VALID_FIT_TIERS = ("reject", "weak_fit", "moderate_fit", "strong_fit", "dream_role")

# Canonical apply modes (with legacy alias normalization)
APPLY_MODE_ALIASES: dict[str, str] = {
    "DRY_RUN": "PREPARE_ONLY",
    "PREPARE_ONLY": "PREPARE_ONLY",
    "MANUAL": "MANUAL_ONLY",
    "MANUAL_ONLY": "MANUAL_ONLY",
    "AUTO_CONFIRM": "CONFIRM_SUBMIT",
    "CONFIRM_SUBMIT": "CONFIRM_SUBMIT",
    "LIVE": "CONFIRM_SUBMIT",
    "AUTO_EASY": "CONFIRM_SUBMIT",
}

# Real, executable presets (actually applied when preset is selected!)
AUTOMATION_PRESETS: dict[str, dict[str, Any]] = {
    "safe": {
        "apply_mode": "PREPARE_ONLY",
        "daily_cap": 5,
        "per_company_cooldown_hours": 72,
        "min_fit_prob": 0.70,
        "max_scam_prob": 0.30,
        "allowed_tiers": ["strong_fit", "dream_role"],
        "max_free_text_questions": 1,
        "first_company_confirm": True,
    },
    "balanced": {
        "apply_mode": "CONFIRM_SUBMIT",
        "daily_cap": 5,
        "per_company_cooldown_hours": 48,
        "min_fit_prob": 0.62,
        "max_scam_prob": 0.30,
        "allowed_tiers": ["strong_fit", "dream_role", "moderate_fit"],
        "max_free_text_questions": 2,
        "first_company_confirm": True,
    },
    "fast": {
        "apply_mode": "CONFIRM_SUBMIT",
        "daily_cap": 10,
        "per_company_cooldown_hours": 24,
        "min_fit_prob": 0.55,
        "max_scam_prob": 0.35,
        "allowed_tiers": ["strong_fit", "dream_role", "moderate_fit"],
        "max_free_text_questions": 3,
        "first_company_confirm": False,
    },
}


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: str = "openai"
    model: str = "gpt-4o"


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tailor: ModelChoice = Field(
        default_factory=lambda: ModelChoice(provider="openai", model="gpt-4o")
    )
    browser_agent: ModelChoice = Field(
        default_factory=lambda: ModelChoice(provider="openai", model="gpt-4o")
    )
    embeddings: ModelChoice = Field(
        default_factory=lambda: ModelChoice(provider="openai", model="text-embedding-3-small")
    )
    triage: ModelChoice = Field(
        default_factory=lambda: ModelChoice(provider="openrouter", model="typesafe/jev-1.14")
    )


class LocationRule(BaseModel):
    """Composable location/work-mode rule supporting AND/OR/XOR and NOT exclusions."""

    model_config = ConfigDict(extra="ignore")
    name: str = ""
    work_modes: list[str] = Field(default_factory=list)  # remote | hybrid | onsite
    not_work_modes: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    not_cities: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    not_countries: list[str] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list)  # global, worldwide, anywhere, apac, india, etc.
    allow_unspecified_remote: bool = True  # e.g. bare "Remote" with no country restriction
    # Optional nested boolean groups for full AND/OR/XOR expressiveness
    all_of: list[LocationRule] = Field(default_factory=list)
    any_of: list[LocationRule] = Field(default_factory=list)
    xor_of: list[LocationRule] = Field(default_factory=list)
    not_rule: LocationRule | None = None


LocationRule.model_rebuild()


class LocationPolicy(BaseModel):
    """Strict, composable work-mode and geographic policy enforced across all ingestors."""

    model_config = ConfigDict(extra="ignore")
    operator: Literal["OR", "AND", "XOR"] = "OR"
    candidate_country: str = "india"
    reject_countries: list[str] = Field(
        default_factory=lambda: [
            "us",
            "canada",
            "uk",
            "eu",
            "emea",
            "europe",
            "australia",
            "latam",
        ]
    )
    check_jd_geo_blockers: bool = True
    rules: list[LocationRule] = Field(
        default_factory=lambda: [
            LocationRule(
                name="remote_anywhere",
                work_modes=["remote"],
                allowed_regions=["anywhere", "worldwide", "global", "india", "apac"],
                allow_unspecified_remote=True,
            )
        ]
    )

    @field_validator("operator", mode="before")
    @classmethod
    def _upper_operator(cls, v: Any) -> str:
        return str(v or "OR").strip().upper()


class CandidatePreferences(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target_role_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    allowed_locations: list[str] = Field(default_factory=list)
    max_experience_years: int | None = None


class WorkdayTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")
    company: str
    tenant: str
    wd_instance: str
    site: str


class ATSTargets(BaseModel):
    model_config = ConfigDict(extra="ignore")
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    workday_cxs: list[WorkdayTarget] = Field(default_factory=list)
    smartrecruiters: list[str] = Field(default_factory=list)
    recruitee: list[str] = Field(default_factory=list)


class AutomationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    preset: str | None = None
    apply_mode: str = "PREPARE_ONLY"
    daily_cap: int = 5
    per_company_cooldown_hours: int = 72
    min_fit_prob: float = 0.65
    max_scam_prob: float = 0.30
    allowed_tiers: list[str] = Field(default_factory=lambda: ["strong_fit", "dream_role"])
    max_free_text_questions: int = 2
    first_company_confirm: bool = True
    allowed_apply_domains: list[str] = Field(default_factory=list)
    allow_headful_handoff: bool = False

    @model_validator(mode="before")
    @classmethod
    def _apply_preset_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        preset_name = (out.get("preset") or "").strip().lower()
        if preset_name and preset_name in AUTOMATION_PRESETS:
            base = dict(AUTOMATION_PRESETS[preset_name])
            # Explicit keys in YAML override preset defaults; missing keys inherit preset
            for k, v in base.items():
                if k not in out or out[k] is None:
                    out[k] = v
        # Normalize legacy apply_mode aliases cleanly
        raw_mode = str(out.get("apply_mode") or "PREPARE_ONLY").strip().upper()
        out["apply_mode"] = APPLY_MODE_ALIASES.get(raw_mode, raw_mode)
        # Filter out invalid legacy tier names like 'good_fit'
        if "allowed_tiers" in out and isinstance(out["allowed_tiers"], list):
            cleaned_tiers = []
            for t in out["allowed_tiers"]:
                t_norm = str(t).strip().lower()
                if t_norm == "good_fit":
                    t_norm = "moderate_fit"
                if t_norm in VALID_FIT_TIERS and t_norm not in cleaned_tiers:
                    cleaned_tiers.append(t_norm)
            out["allowed_tiers"] = cleaned_tiers or ["strong_fit", "dream_role"]
        return out

    def normalized_apply_mode(self) -> str:
        raw = (self.apply_mode or "PREPARE_ONLY").strip().upper()
        return APPLY_MODE_ALIASES.get(raw, "PREPARE_ONLY")


class JobboardsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sites: list[str] = Field(default_factory=lambda: ["naukri", "linkedin", "indeed"])
    search_terms: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    results_wanted: int = 20
    hours_old: int = 72
    country_indeed: str = "india"


class GmailScannerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lookback_days: int = 3
    label: str = "professional-job_alerts"
    max_job_age_hours: int = 96
    poll_interval_minutes: int = 20
    sender_keywords: list[str] = Field(
        default_factory=lambda: [
            "linkedin",
            "naukri",
            "indeed",
            "instahyre",
            "wellfound",
            "job",
            "hiring",
            "alert",
        ]
    )


class IngestionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enable_jobboards: bool = True
    enable_gmail: bool = True
    enable_ats: bool = True
    enable_workday: bool = False
    enable_remote_feeds: bool = True
    max_job_age_days: int = 7
    triage_concurrency: int = 3
    jobboards_interval_minutes: int = 60
    gmail_interval_minutes: int = 20
    ats_interval_minutes: int = 240
    workday_interval_minutes: int = 720
    # Deprecated compatibility shims (kept so legacy callers/tests never crash)
    prioritize_scraping: bool = True
    scrape_only: bool = False

    def ats_enabled(self) -> bool:
        return bool(self.enable_ats and not self.scrape_only)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    candidate_preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)
    location_policy: LocationPolicy | None = None
    ats_targets: ATSTargets = Field(default_factory=ATSTargets)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    jobboards: JobboardsConfig = Field(default_factory=JobboardsConfig)
    gmail_scanner: GmailScannerConfig = Field(default_factory=GmailScannerConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)

    def effective_location_policy(self) -> LocationPolicy:
        """Returns explicit location_policy, or translates legacy allowed_locations cleanly."""
        if self.location_policy is not None:
            return self.location_policy

        allowed = [loc.strip().lower() for loc in (self.candidate_preferences.allowed_locations or []) if loc.strip()]
        if not allowed:
            return LocationPolicy()

        remote_tokens = {"remote", "anywhere", "worldwide", "global", "wfh", "work from home"}
        has_remote = any(tok in remote_tokens for tok in allowed)
        cities = [c for c in allowed if c not in remote_tokens and c != "india"]
        has_india = "india" in allowed

        rules: list[LocationRule] = []
        if has_remote:
            regions = ["anywhere", "worldwide", "global", "apac"]
            if has_india or cities:
                regions.append("india")
            rules.append(
                LocationRule(
                    name="legacy_remote",
                    work_modes=["remote"],
                    allowed_regions=regions,
                    allow_unspecified_remote=True,
                )
            )
        if cities or has_india:
            rules.append(
                LocationRule(
                    name="legacy_onsite_hybrid",
                    work_modes=["hybrid", "onsite"],
                    cities=cities,
                    countries=["india"] if has_india else [],
                )
            )
        return LocationPolicy(operator="OR", rules=rules or LocationPolicy().rules)


CONFIG_DB_CHECK_TTL = 120.0
_config_cache: dict[str, Any] = {
    "value": None,
    "file_mtime": None,
    "db_updated": None,
    "checked_at": 0.0,
}


def _file_mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return None


def _parse_iso_epoch(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=UTC)
        return dt.timestamp()
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
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


def apply_automation_preset(config_dict: dict[str, Any], preset_name: str) -> dict[str, Any]:
    """Explicitly stamps a preset's values into a config dictionary (used by Web UI & CLI)."""
    key = (preset_name or "").strip().lower()
    if key not in AUTOMATION_PRESETS:
        raise ValueError(f"Unknown preset '{preset_name}'. Valid presets: {', '.join(AUTOMATION_PRESETS)}")
    out = dict(config_dict)
    auto = dict(out.get("automation") or {})
    auto.update(AUTOMATION_PRESETS[key])
    auto["preset"] = key
    out["automation"] = auto
    return out


def load_config(force: bool = False) -> AppConfig:
    now = time.monotonic()
    fm = _file_mtime("config/config.yaml")

    if (
        not force
        and _config_cache["value"] is not None
        and _config_cache["file_mtime"] == fm
        and (now - _config_cache["checked_at"]) < CONFIG_DB_CHECK_TTL
    ):
        return _config_cache["value"]

    du = _db_updated_at()
    if (
        not force
        and _config_cache["value"] is not None
        and _config_cache["file_mtime"] == fm
        and _config_cache["db_updated"] == du
    ):
        _config_cache["checked_at"] = now
        return _config_cache["value"]

    file_cfg: dict[str, Any] = {}
    try:
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        file_cfg = {}

    # Warn if dead/unknown top-level keys exist in YAML
    known_top_keys = set(AppConfig.model_fields.keys())
    unknown_keys = [k for k in file_cfg if k not in known_top_keys]
    if unknown_keys:
        logger.warning("Ignoring unknown top-level config.yaml keys: %s", ", ".join(unknown_keys))

    db_cfg: dict[str, Any] | None = None
    db_epoch: float | None = _parse_iso_epoch(du)
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT value, updated_at FROM app_settings WHERE key = 'config_yaml';")
                row = cur.fetchone()
                if row and isinstance(row.get("value"), dict):
                    db_cfg = row["value"]
                    db_epoch = _parse_iso_epoch(row.get("updated_at")) or db_epoch
        except Exception:
            db_cfg = None

    merged: dict[str, Any] = {}
    # Timestamp-aware precedence: if local config.yaml is newer than DB copy,
    # respect local edits instead of silently overwriting with a stale DB blob!
    if db_cfg and (fm is None or (db_epoch is not None and db_epoch >= fm)):
        merged.update(file_cfg)
        for k, v in db_cfg.items():
            merged[k] = v
    else:
        if db_cfg:
            merged.update(db_cfg)
            logger.info(
                "Local config/config.yaml is newer than DB app_settings.config_yaml; using local edits (run 'python manage.py sync' to push to DB)."
            )
        for k, v in file_cfg.items():
            merged[k] = v

    # Environment variable overrides
    if os.getenv("APPLY_MODE"):
        merged.setdefault("automation", {})
        if isinstance(merged["automation"], dict):
            merged["automation"]["apply_mode"] = os.getenv("APPLY_MODE")
    if os.getenv("RELENTLESS_SCRAPE_ONLY", "").strip().lower() in ("1", "true", "yes"):
        merged.setdefault("ingestion", {})
        if isinstance(merged["ingestion"], dict):
            merged["ingestion"]["scrape_only"] = True
            merged["ingestion"]["enable_ats"] = False
    if os.getenv("RELENTLESS_SKIP_ATS", "").strip().lower() in ("1", "true", "yes"):
        merged.setdefault("ingestion", {})
        if isinstance(merged["ingestion"], dict):
            merged["ingestion"]["enable_ats"] = False

    try:
        cfg = AppConfig(**merged)
    except ValidationError as e:
        raise ValueError(f"Invalid configuration: {e}") from e

    _config_cache["value"] = cfg
    _config_cache["file_mtime"] = fm
    _config_cache["db_updated"] = du
    _config_cache["checked_at"] = now
    return cfg


def invalidate_config() -> None:
    global _config_cache
    _config_cache = {
        "value": None,
        "file_mtime": None,
        "db_updated": None,
        "checked_at": 0.0,
    }
