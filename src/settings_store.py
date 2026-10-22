import json
import os
import time

import yaml
from psycopg.types.json import Jsonb

from src.db import get_connection

LOCAL_PROFILE_PATH = "config/master_profile.json"
EXAMPLE_PROFILE_PATH = "config/master_profile.example.json"
LOCAL_CONFIG_PATH = "config/config.yaml"

_profile_cache = {"fingerprint": None, "value": None, "ts": 0.0}
CACHE_TTL = 300


def _profile_fingerprint():
    if os.path.exists(LOCAL_PROFILE_PATH):
        return ("file", os.path.getmtime(LOCAL_PROFILE_PATH))
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


def sync_local_to_db():
    """Pushes local gitignored master_profile.json and config.yaml into private Neon DB."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if os.path.exists(LOCAL_PROFILE_PATH):
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


def _invalidate_profile_cache():
    _profile_cache["fingerprint"] = None
    _profile_cache["value"] = None
    _profile_cache["ts"] = 0.0


def load_master_profile() -> dict:
    if os.path.exists(LOCAL_PROFILE_PATH):
        with open(LOCAL_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'master_profile';")
            row = cur.fetchone()
            if row and row["value"]:
                return row["value"]
    except Exception:
        pass

    with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_master_profile() -> dict:
    now = time.time()
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