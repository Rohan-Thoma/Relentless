import asyncio
import json
import os
import secrets
import shutil
import sys
import time

from dotenv import load_dotenv, set_key
from evals.evaluate_triage import run_evaluation
import httpx

from src.db import close_pool, get_connection, get_pool, init_db, reset_database
from src.db_migrate import pending_migrations
from src.hitl.telegram_bot import listen_for_button_clicks
from src.httpx_utils import get_with_retry
from src.knowledge.distiller import run_distill
from src.knowledge.export import export_bundle
from src.knowledge.store import flush, get_store, hydrate
from src.logging_config import configure_logging
from src.main_loop import main as run_loop
from src.readiness import collect_readiness
from src.settings_store import (
    EXAMPLE_PROFILE_PATH,
    LOCAL_PROFILE_PATH,
    sync_local_to_db,
)

ENV_FILE = ".env"


def interactive_setup():
    print("\n🚀 Welcome to the Relentless Setup Wizard!\n")
    if not os.path.exists(ENV_FILE):
        shutil.copyfile(".env.example", ENV_FILE)
    load_dotenv(ENV_FILE, override=True)

    def prompt_key(env_name: str, label: str, required: bool = True, default_val: str = ""):
        current = os.getenv(env_name, "").strip()
        hint = f" [current: {current[:10]}...]" if current else (" [Enter for generated default]" if default_val else "")
        val = input(f"👉 {label}{hint}: ").strip()
        chosen = val or current or default_val
        if val or (not current and default_val):
            set_key(ENV_FILE, env_name, chosen)
            os.environ[env_name] = chosen
        elif required and not current:
            print(f"❌ {env_name} is required!")

    prompt_key("DATABASE_URL", "1. Neon PostgreSQL URL (postgresql://...)")
    prompt_key("OPENROUTER_API_KEY", "2. OpenRouter API Key (for Jev System-1 Triage)")
    prompt_key("OPENAI_API_KEY", "3. OpenAI API Key (optional, for Tailored Resume PDF)", required=False)
    generated_key = secrets.token_urlsafe(32)
    prompt_key(
        "WEB_API_KEY",
        "4. Web Command Center API Key / Login Password",
        required=False,
        default_val=generated_key,
    )
    print("   🔐 WEB_API_KEY configured (keep this secret for Web UI login!).")
    prompt_key("TELEGRAM_BOT_TOKEN", "5. Telegram Bot Token from @BotFather (optional)", required=False)

    # Auto-detect Telegram Chat ID if token provided and chat ID missing
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token and not chat_id:
        print("\n📲 Auto-Detecting your Telegram Chat ID...")
        print("   👉 Open Telegram right now and send 'hello' to your bot (or press Ctrl+C to skip)...")
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
            except KeyboardInterrupt:
                print("\n   ⏭️ Skipped Telegram Chat ID auto-detect.")
                break
            except Exception:
                pass
            time.sleep(2)

    # Personalize local gitignored master_profile.json interactively instead of copying template silently
    if not os.path.exists(LOCAL_PROFILE_PATH):
        print("\n🪪 Candidate Master Profile Setup (gitignored locally & synced to Neon DB):")
        use_prompt = input("👉 Configure your basic details now? [Y/n]: ").strip().lower()
        if use_prompt in ("", "y", "yes"):
            with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
                profile = json.load(f)
            name = input("   Full Name: ").strip()
            email = input("   Email: ").strip()
            location = input("   Location (e.g., Bengaluru, India / Austin, TX / Remote): ").strip()
            skills_raw = input("   Core Skills (comma-separated): ").strip()
            if name:
                profile["name"] = name
                parts = name.split()
                profile.setdefault("application_defaults", {})["full_name"] = name
                profile["application_defaults"]["first_name"] = parts[0]
                profile["application_defaults"]["last_name"] = parts[-1] if len(parts) > 1 else ""
            if email:
                profile["email"] = email
            if location:
                profile["location"] = location
                profile.setdefault("application_defaults", {})["current_city"] = location
            if skills_raw:
                profile["core_skills"] = [s.strip() for s in skills_raw.split(",") if s.strip()]
            with open(LOCAL_PROFILE_PATH, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
            print(f"   ✅ Wrote {LOCAL_PROFILE_PATH}. Edit experience/education there OR in Web UI -> 06 // CONFIG & VAULT.")
        else:
            print("   ⏭️ Skipped local file creation. You can configure your profile visually in Web UI -> 06 // CONFIG & VAULT.")

    print("\n🗄️  Initializing PostgreSQL schema & syncing config...")
    init_db()
    if os.path.exists(LOCAL_PROFILE_PATH):
        sync_local_to_db()
    else:
        print("ℹ️  Skipped profile DB sync (no local config/master_profile.json yet). Add one via Web UI -> 06 // CONFIG & VAULT.")
    print("\n🎉 Setup Complete! Run 'python manage.py doctor' or 'python manage.py run'.")


def run_doctor():
    load_dotenv(ENV_FILE, override=True)
    configure_logging(capture_stdio=False)
    print("\n🩺 Running Relentless System Health & Readiness Check...\n")

    report = collect_readiness(check_db=True)
    icon_map = {"ok": "✅", "warning": "⚠️", "error": "❌", "disabled": "⏭️"}
    for chk in report["checks"]:
        icon = icon_map.get(chk["status"], "•")
        print(f"  {icon} {chk['label']}: {chk['detail']}")
        if chk.get("fix"):
            print(f"     ↳ Fix: {chk['fix']}")

    try:
        pending = pending_migrations()
        print(f"  ✅ Migrations: {len(pending)} pending")
    except Exception as e:
        print(f"  ❌ Migrations: {e}")

    try:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        print("  ✅ Connection pool: OK")
    except Exception as e:
        print(f"  ❌ Connection pool: {e}")

    try:
        async def _smoke():
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await get_with_retry(client, "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true")
                return resp.status_code

        code = asyncio.run(_smoke())
        print(f"  ✅ Retry helper smoke: HTTP {code}")
    except Exception as e:
        print(f"  ⚠️ Retry helper smoke: {e}")

    print(
        f"\n  Overall Status: {'🟢 READY' if report['ready'] else '🔴 INCOMPLETE — fix ❌ items above'}\n"
    )
    try:
        close_pool()
    except Exception:
        pass


def print_help():
    print(
        """
🛠️  Relentless — One-Stop Control Center (manage.py)

Usage:
  python manage.py setup          # Interactive onboarding wizard (auto-detects Telegram Chat ID!)
  python manage.py sync           # Push local gitignored master_profile.json & config.yaml to Neon DB
  python manage.py doctor         # Verify DB, API keys, and cloud profile privacy status
  python manage.py reset labels   # Soft Reset: clear only button clicks (Eval n=0, keeps Jev scores)
  python manage.py reset triage   # Medium Reset: clear Jev scores + clicks to re-score scraped jobs
  python manage.py reset all      # Factory Reset: wipe all scraped jobs and restart IDs at #1
  python manage.py eval           # Print live Precision@K, Wilson 95% CIs, and Brier calibration table
  python manage.py run            # Unified Command: Starts Telegram bot + Web API + streaming producers concurrently
  python manage.py web            # Start standalone Web Command Center API & UI server
  python manage.py run --scrape-only # Fast Mode: Scrapes Job boards & Gmail only (skips slow ATS)
  python manage.py run --ats-only    # ATS Only: Scans only company ATS endpoints
  python manage.py run --once        # Run single pipeline iteration and exit
  python manage.py ingest         # Direct one-off ingestion run with same flags
"""
    )


def _parse_run_flags(argv: list[str]) -> dict:
    flags = {
        "scrape_only": False,
        "skip_ats": False,
        "ats_only": False,
        "sources": None,
        "once": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--scrape-only" or arg == "--fast":
            flags["scrape_only"] = True
            flags["skip_ats"] = True
        elif arg == "--skip-ats":
            flags["skip_ats"] = True
        elif arg == "--ats-only":
            flags["ats_only"] = True
        elif arg == "--jobboards-only":
            flags["sources"] = ["jobboards"]
            flags["scrape_only"] = True
            flags["skip_ats"] = True
        elif arg == "--gmail-only":
            flags["sources"] = ["gmail"]
            flags["scrape_only"] = True
            flags["skip_ats"] = True
        elif arg.startswith("--sources="):
            raw = arg.split("=", 1)[1]
            flags["sources"] = [s.strip().lower() for s in raw.split(",") if s.strip()]
        elif arg == "--once":
            flags["once"] = True
        elif arg in ("-h", "--help"):
            print_help()
            sys.exit(0)
        i += 1
    return flags


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
        configure_logging(capture_stdio=True)
        run_args = _parse_run_flags(sys.argv[2:])
        asyncio.run(run_loop(**run_args))
    elif cmd == "ingest":
        configure_logging(capture_stdio=True)
        from src.ingestors.ats_poller import run_ingestion

        run_args = _parse_run_flags(sys.argv[2:])
        run_args.pop("once", None)
        asyncio.run(run_ingestion(**run_args))
    elif cmd == "bot":
        configure_logging(capture_stdio=True)
        print("🤖 Starting Telegram bot listener ONLY (independent of ingestion pipeline)...")
        asyncio.run(listen_for_button_clicks())
    elif cmd == "web":
        configure_logging(capture_stdio=True)
        from src.web.api import run_web_server

        init_db()
        hydrate()
        print("🌐 Starting Relentless Web Command Center API...")
        asyncio.run(run_web_server())
    elif cmd == "distill":
        configure_logging(capture_stdio=True)
        init_db()
        hydrate()
        run_distill()
        flush()
        print("✅ Distill complete")
    elif cmd == "health":
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
            print("✅ DB: OK")
        except Exception as e:
            print(f"❌ DB: {e}")
        print("✅ Health check complete")
        sys.exit(0)
    elif cmd == "outcome":
        job_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        kind = sys.argv[3] if len(sys.argv) > 3 else "APPLIED"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO outcome_events (job_id, kind) VALUES (%s, %s);", (job_id, kind))
            conn.commit()
        print(f"✅ Recorded outcome {kind} for job {job_id}")
        sys.exit(0)
    elif cmd == "knowledge":
        sub = sys.argv[2].lower() if len(sys.argv) > 2 else "help"
        init_db()
        if sub == "export":
            out = sys.argv[3] if len(sys.argv) > 3 else "knowledge_export"
            hydrate()
            export_bundle(out, redact="--redact" in sys.argv, force="--force" in sys.argv)
            print(f"✅ Exported to {out}")
        elif sub == "sync":
            direction = sys.argv[3].lower() if len(sys.argv) > 3 else "--from-files"
            hydrate()
            store = get_store()
            if direction == "--from-files":
                store.sync_from_files()
                flush()
                print("✅ Synced Markdown -> DB")
            elif direction == "--to-files":
                hydrate()
                print("✅ Synced DB -> Markdown (via hydrate)")
            elif direction == "--check":
                store = get_store()
                bad = [p for p, c in store.concepts.items() if not c.concept_type]
                if bad:
                    print("❌ Missing type:", bad)
                    sys.exit(1)
                print(f"✅ Knowledge check passed ({len(store.concepts)} concepts)")
            else:
                print("Usage: manage.py knowledge sync [--from-files|--to-files|--check]")
            sys.exit(0)
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
                    cur.execute(
                        "INSERT INTO knowledge_forget (pattern) VALUES (%s) ON CONFLICT DO NOTHING;", (pattern,)
                    )
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
