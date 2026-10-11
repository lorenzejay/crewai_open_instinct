"""Bootstrap and test an EMPTY disposable local database. Never loads app secrets."""
import os
from pathlib import Path
from urllib.parse import urlsplit
import psycopg

def run():
    url = os.environ["TEST_DATABASE_URL"]
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("TEST_DATABASE_URL must point to an isolated localhost database")
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("select count(*) from information_schema.tables where table_schema in ('public','auth')").fetchone()[0]:
            raise SystemExit("Refusing to bootstrap a non-empty test database")
        conn.execute("""
            do $$ begin
              if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
              if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
              if not exists(select 1 from pg_roles where rolname='service_role') then create role service_role bypassrls; end if;
            end $$;
            create schema auth;
            create table auth.users(id uuid primary key);
            create function auth.uid() returns uuid language sql stable as $$
              select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
            $$;
            grant usage on schema auth,public to anon,authenticated,service_role;
            grant execute on function auth.uid() to anon,authenticated,service_role;
        """)
        project = Path(__file__).resolve().parents[1]
        for migration in sorted((project / "supabase/migrations").glob("*.sql")):
            conn.execute(migration.read_text())
            print("Applied", migration.name)
        for path in sorted((project / "tests").glob("*.sql")):
            conn.execute(path.read_text())
            print("Passed", path.name)


if __name__ == "__main__":
    run()
