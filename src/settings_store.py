import os
import json
import yaml
from psycopg.types.json import Jsonb
from src.db import get_connection

LOCAL_PROFILE_PATH = "config/master_profile.json"
EXAMPLE_PROFILE_PATH = "config/master_profile.example.json"
LOCAL_CONFIG_PATH = "config/config.yaml"

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

def load_master_profile() -> dict:
    """
    Loads master_profile in priority order:
    1. Local gitignored config/master_profile.json (if present on laptop)
    2. Private Neon PostgreSQL app_settings table (when running on Railway cloud!)
    3. Fallback config/master_profile.example.json
    """
    if os.path.exists(LOCAL_PROFILE_PATH):
        with open(LOCAL_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = 'master_profile';")
                row = cur.fetchone()
                if row and row["value"]:
                    return row["value"]
    except Exception:
        pass

    with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)