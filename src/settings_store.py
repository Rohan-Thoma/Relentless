from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time

from psycopg.types.json import Jsonb
import yaml

from src.db import get_connection

LOCAL_PROFILE_PATH = "config/master_profile.json"


class ProfileNotConfigured(Exception):
    """Raised when no master profile is available (neither local file nor DB)."""


EXAMPLE_PROFILE_PATH = "config/master_profile.example.json"
LOCAL_CONFIG_PATH = "config/config.yaml"

_profile_cache = {"fingerprint": None, "value": None, "ts": 0.0}
CACHE_TTL = 300


def _profile_fingerprint():
    if os.path.exists(LOCAL_PROFILE_PATH):
        return ("file", os.path.getmtime(LOCAL_PROFILE_PATH))
    if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT updated_at FROM app_settings WHERE key = 'master_profile';")
                row = cur.fetchone()
                if row and row.get("updated_at") is not None:
                    return ("db", str(row["updated_at"]))
        except Exception:
            pass
    if os.path.exists(EXAMPLE_PROFILE_PATH):
        return ("example", os.path.getmtime(EXAMPLE_PROFILE_PATH))
    return ("none", 0)


def _to_epoch(val) -> float | None:
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


def get_sync_status() -> dict:
    """Compares local config/profile timestamps against Neon DB app_settings (Git-like status)."""
    db_times: dict[str, float | None] = {"master_profile": None, "config_yaml": None}
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT key, updated_at FROM app_settings WHERE key IN ('master_profile', 'config_yaml');")
            for r in cur.fetchall() or []:
                db_times[str(r["key"])] = _to_epoch(r.get("updated_at"))
    except Exception:
        pass

    result = {}
    for key, path in (("master_profile", LOCAL_PROFILE_PATH), ("config_yaml", LOCAL_CONFIG_PATH)):
        local_ts = os.path.getmtime(path) if os.path.exists(path) else None
        db_ts = db_times.get(key)
        if local_ts is None and db_ts is None:
            state = "missing"
        elif local_ts is not None and db_ts is None:
            state = "local_only"
        elif local_ts is None and db_ts is not None:
            state = "db_only"
        elif db_ts - local_ts > 2.0:
            state = "db_newer"
        elif local_ts - db_ts > 2.0:
            state = "local_newer"
        else:
            state = "in_sync"
        result[key] = {"state": state, "local_mtime": local_ts, "db_updated_at": db_ts, "path": path}
    return result


def pull_db_to_local() -> dict:
    """Git-like pull: downloads latest master_profile and config_yaml from Neon DB to local files."""
    pulled: dict[str, bool] = {"master_profile": False, "config_yaml": False}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, value, updated_at FROM app_settings WHERE key IN ('master_profile', 'config_yaml');")
        rows = {str(r["key"]): r for r in (cur.fetchall() or [])}

    if "master_profile" in rows and isinstance(rows["master_profile"].get("value"), dict):
        Path(LOCAL_PROFILE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(LOCAL_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(rows["master_profile"]["value"], f, indent=2, ensure_ascii=False)
        pulled["master_profile"] = True
        print(f"⬇️  Pulled Neon DB -> {LOCAL_PROFILE_PATH}")

    if "config_yaml" in rows and isinstance(rows["config_yaml"].get("value"), dict):
        Path(LOCAL_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(rows["config_yaml"]["value"], f, sort_keys=False, allow_unicode=True)
        pulled["config_yaml"] = True
        print(f"⬇️  Pulled Neon DB -> {LOCAL_CONFIG_PATH}")

    _invalidate_profile_cache()
    try:
        from src.config import invalidate_config

        invalidate_config()
    except Exception:
        pass
    return pulled


def sync_local_to_db(force: bool = False):
    """Git-like safe push: refuses to overwrite newer Neon DB changes unless force=True."""
    status = get_sync_status()
    skipped: list[str] = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            if os.path.exists(LOCAL_PROFILE_PATH):
                if not force and status["master_profile"]["state"] == "db_newer":
                    skipped.append("master_profile")
                    print(
                        "⚠️  SKIPPED master_profile.json: Neon DB version is NEWER (edited in Web UI/Cloud)! "
                        "Run 'python manage.py pull' first, or 'python manage.py sync --force' to overwrite."
                    )
                else:
                    with open(LOCAL_PROFILE_PATH, "r", encoding="utf-8") as f:
                        profile_data = json.load(f)
                    cur.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES ('master_profile', %s, NOW())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
                        """,
                        (Jsonb(profile_data),),
                    )
                    print("🔒 Synced private master_profile.json -> Neon PostgreSQL (app_settings)!")

            if os.path.exists(LOCAL_CONFIG_PATH):
                if not force and status["config_yaml"]["state"] == "db_newer":
                    skipped.append("config_yaml")
                    print(
                        "⚠️  SKIPPED config.yaml: Neon DB version is NEWER (edited in Web UI/Cloud)! "
                        "Run 'python manage.py pull' first, or 'python manage.py sync --force' to overwrite."
                    )
                else:
                    with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg_data = yaml.safe_load(f)
                    cur.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES ('config_yaml', %s, NOW())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
                        """,
                        (Jsonb(cfg_data),),
                    )
                    print("🔒 Synced config.yaml -> Neon PostgreSQL (app_settings)!")
        conn.commit()
    _invalidate_profile_cache()
    try:
        from src.config import invalidate_config

        invalidate_config()
    except Exception:
        pass
    return {"skipped": skipped}


def _invalidate_profile_cache():
    _profile_cache["fingerprint"] = None
    _profile_cache["value"] = None
    _profile_cache["ts"] = 0.0


def _allow_example_profile() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return os.getenv("ALLOW_EXAMPLE_PROFILE", "").strip().lower() in ("1", "true", "yes")


def load_example_profile() -> dict:
    if os.path.exists(EXAMPLE_PROFILE_PATH):
        with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    raise ProfileNotConfigured(f"Example profile not found at {EXAMPLE_PROFILE_PATH}")


def load_master_profile() -> dict:
    if os.path.exists(LOCAL_PROFILE_PATH):
        with open(LOCAL_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = 'master_profile';")
                row = cur.fetchone()
                if row and row["value"]:
                    return row["value"]
        except Exception:
            pass

    if _allow_example_profile() and os.path.exists(EXAMPLE_PROFILE_PATH):
        with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    raise ProfileNotConfigured(
        "Master profile is not configured. Run 'python manage.py sync' after creating "
        "config/master_profile.json, or set ALLOW_EXAMPLE_PROFILE=1 for demo/test use."
    )


def get_master_profile() -> dict:
    now = time.time()
    if _profile_cache["value"] is not None and (now - _profile_cache["ts"]) < CACHE_TTL:
        if Path(LOCAL_PROFILE_PATH).exists():
            if _profile_cache["fingerprint"] == ("file", os.path.getmtime(LOCAL_PROFILE_PATH)):
                return _profile_cache["value"]
        else:
            return _profile_cache["value"]

    fp = _profile_fingerprint()
    if (
        _profile_cache["value"] is not None
        and _profile_cache["fingerprint"] == fp
        and (now - _profile_cache["ts"]) < CACHE_TTL
    ):
        return _profile_cache["value"]
    value = load_master_profile()
    _profile_cache["fingerprint"] = fp
    _profile_cache["value"] = value
    _profile_cache["ts"] = now
    return value
