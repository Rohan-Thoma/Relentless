import glob
import os

from src.db import get_connection

MIGRATIONS_DIR = os.path.join("db", "migrations")


def _ensure_migrations_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
    conn.commit()


def pending_migrations() -> list[str]:
    with get_connection() as conn:
        _ensure_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations;")
            applied = {r["version"] for r in cur.fetchall()}
    files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
    return [os.path.basename(f) for f in files if os.path.basename(f) not in applied]


def run_migrations():
    files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
    with get_connection() as conn:
        _ensure_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations;")
            applied = {r["version"] for r in cur.fetchall()}
        for path in files:
            version = os.path.basename(path)
            if version in applied:
                continue
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, NOW());",
                    (version,),
                )
            conn.commit()
            print(f"✅ Applied migration {version}")
    return True


if __name__ == "__main__":
    run_migrations()