"""Apply the additive service schema using the existing database password."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from crewai_open_instinct.config import load_environment, required


def run():
    load_environment()
    ref = urlsplit(required("SUPABASE_URL")).hostname.split(".")[0]
    # Copy the session pooler host from Supabase Connect if direct IPv6 fails.
    host = (os.getenv("SUPABASE_DATABASE_HOST") or f"db.{ref}.supabase.co")
    user = (os.getenv("SUPABASE_DATABASE_USER") or ("postgres" if host.startswith("db.") else f"postgres.{ref}"))
    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    try:
        with psycopg.connect(host=host, port=5432, dbname="postgres", user=user,
                            password=required("SUPABASE_DATABASE_PASSWORD"),
                            sslmode="require", connect_timeout=15, autocommit=True) as conn:
            for migration in sorted(migrations.glob("*.sql")):
                conn.execute(migration.read_text())
        print("Messaging migrations applied.")
    except Exception as exc:
        # Never echo a connection string or provider response containing secrets.
        raise SystemExit(f"Migration failed ({type(exc).__name__}); check Supabase Connect settings.") from None


if __name__ == "__main__":
    run()
