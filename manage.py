import os
import shutil
import sys
import time

import httpx
from dotenv import load_dotenv, set_key

from evals.evaluate_triage import run_evaluation
from src.db import get_connection, init_db, reset_database, get_pool, close_pool
from src.db_migrate import pending_migrations
from src.knowledge.store import hydrate, flush, get_store
from src.knowledge.export import export_bundle
from src.knowledge.distiller import run_distill
from src.settings_store import EXAMPLE_PROFILE_PATH, LOCAL_PROFILE_PATH, sync_local_to_db
from src.httpx_utils import get_with_retry

ENV_FILE = ".env"

def interactive_setup():
    print("\n🚀 Welcome to the Relentless v1.0 Plug-and-Play Setup Wizard!\n")
    if not os.path.exists(ENV_FILE):
        shutil.copyfile(".env.example", ENV_FILE)
    load_dotenv(ENV_FILE, override=True)

    def prompt_key(env_name: str, label: str, required: bool = True):
        current = os.getenv(env_name, "").strip()
        hint = f" [current: {current[:12]}...]" if current else ""
        val = input(f"👉 {label}{hint}: ").strip()
        if val:
            set_key(ENV_FILE, env_name, val)
            os.environ[env_name] = val
        elif required and not current:
            print(f"⚠️  {env_name} is required!")

    prompt_key("DATABASE_URL", "1. Neon PostgreSQL URL (postgresql://...)")
    prompt_key("OPENROUTER_API_KEY", "2. OpenRouter API Key (for Jev System-1 Triage)")
    prompt_key("OPENAI_API_KEY", "3. OpenAI API Key (optional, for $3 credit Resume Tailor)", required=False)
    prompt_key("TELEGRAM_BOT_TOKEN", "4. Telegram Bot Token (from @BotFather)")

    # Auto-detect Telegram Chat ID if missing!
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token and not chat_id:
        print("\n📲 Auto-Detecting your Telegram Chat ID...")
        print("   👉 Open Telegram right now and send 'hello' to your bot!")
        for _ in range(20):
            try:
                r = httpx.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", timeout=5.0).json()
                results = r.get("result", [])
                if results:
                    detected_id = str(results[-1]["message"]["chat"]["id"])
                    set_key(ENV_FILE, "TELEGRAM_CHAT_ID", detected_id)
                    os.environ["TELEGRAM_CHAT_ID"] = detected_id
                    print(f"   ✅ Auto-detected TELEGRAM_CHAT_ID: {detected_id}!")
                    break
            except Exception:
                pass
            time.sleep(2)

    # Create local gitignored master_profile.json from example if not present
    if not os.path.exists(LOCAL_PROFILE_PATH):
        shutil.copyfile(EXAMPLE_PROFILE_PATH, LOCAL_PROFILE_PATH)
        print(f"\n📄 Created local gitignored {LOCAL_PROFILE_PATH} from template.")

    print("\n🗄️  Initializing Neon PostgreSQL schema & syncing private profile...")
    init_db()
    sync_local_to_db()
    print("\n🎉 Setup Complete! Edit 'config/master_profile.json' anytime and run 'python manage.py sync'.")

def run_doctor():
    load_dotenv(ENV_FILE, override=True)
    print("\n🩺 Running Relentless System Health Check...\n")
    try:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        print("  ✅ Connection pool: OK")
    except Exception as e:
        print(f"  ❌ Connection pool: {e}")

    total = 0
    has_cloud_profile = False
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM jobs;")
            total = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS s FROM app_settings WHERE key = 'master_profile';")
            has_cloud_profile = cur.fetchone()["s"] > 0
        print(f"  ✅ Neon PostgreSQL: Connected ({total} jobs in DB)")
    except Exception as e:
        print(f"  ❌ Neon PostgreSQL: {e}")

    try:
        pending = pending_migrations()
        print(f"  ✅ Migrations: {len(pending)} pending")
    except Exception as e:
        print(f"  ❌ Migrations: {e}")

    try:
        import asyncio

        async def _smoke():
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await get_with_retry(client, "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true")
                return resp.status_code

        code = asyncio.run(_smoke())
        print(f"  ✅ Retry helper smoke: HTTP {code}")
    except Exception as e:
        print(f"  ⚠️ Retry helper smoke: {e}")

    print(f"  ✅ Private Cloud Profile Synced: {'Yes (Railway-ready!)' if has_cloud_profile else 'No (Run: python manage.py sync)'}")
    print(f"  ✅ Local master_profile.json gitignored: {os.path.exists(LOCAL_PROFILE_PATH)}")
    print(f"  ✅ OpenRouter Key Set: {bool(os.getenv('OPENROUTER_API_KEY'))}")
    print(f"  ✅ OpenAI Key Set: {bool(os.getenv('OPENAI_API_KEY'))}")
    print(f"  ✅ Telegram Bot Configured: {bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'))}\n")
    try:
        close_pool()
    except Exception:
        pass

def print_help():
    print(
        """
🛠️  Relentless v1.0 — One-Stop Control Center (manage.py)

Usage:
  python manage.py setup          # Interactive 60-sec onboarding wizard (auto-detects Telegram Chat ID!)
  python manage.py sync           # Push local gitignored master_profile.json & config.yaml to Neon DB
  python manage.py doctor         # Verify DB, API keys, and cloud profile privacy status
  python manage.py reset labels   # Soft Reset: clear only button clicks (Eval n=0, keeps Jev scores)
  python manage.py reset triage   # Medium Reset: clear Jev scores + clicks to re-score scraped jobs
  python manage.py reset all      # Factory Reset: wipe all scraped jobs and restart IDs at #1
  python manage.py eval           # Print live Precision@K, Wilson 95% CIs, and Brier calibration table
  python manage.py run            # Start the 24/7 autonomous worker loop
"""
    )

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "help"
    if cmd == "setup":
        interactive_setup()
    elif cmd == "sync":
        init_db()
        sync_local_to_db()
    elif cmd == "doctor":
        run_doctor()
    elif cmd == "reset":
        mode = sys.argv[2].lower() if len(sys.argv) > 2 else "labels"
        reset_database(mode)
    elif cmd == "eval":
        run_evaluation()
    elif cmd == "run":
        import asyncio

        from src.main_loop import main as run_loop
        asyncio.run(run_loop())
    elif cmd == "distill":
        init_db()
        hydrate()
        run_distill()
        flush()
        print("✅ Distill complete")
    elif cmd == "knowledge":
        sub = sys.argv[2].lower() if len(sys.argv) > 2 else "help"
        init_db()
        if sub == "export":
            out = sys.argv[3] if len(sys.argv) > 3 else "knowledge_export"
            hydrate()
            export_bundle(out, redact="--redact" in sys.argv)
            print(f"✅ Exported to {out}")
        elif sub == "check":
            hydrate()
            store = get_store()
            bad = [p for p, c in store.concepts.items() if not c.concept_type]
            if bad:
                print("❌ Missing type:", bad)
                sys.exit(1)
            print(f"✅ Knowledge check passed ({len(store.concepts)} concepts)")
        elif sub == "flush":
            hydrate()
            flush()
            print("✅ Flushed")
        elif sub == "forget":
            pattern = sys.argv[3] if len(sys.argv) > 3 else ""
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO knowledge_forget (pattern) VALUES (%s) ON CONFLICT DO NOTHING;", (pattern,))
                conn.commit()
            hydrate()
            store = get_store()
            for path in list(store.concepts):
                if pattern.lower() in path.lower():
                    store.delete(path)
            flush()
            print(f"✅ Forgot {pattern}")
        else:
            print("Usage: manage.py knowledge [export|check|flush|forget]")
    else:
        print_help()