"""Operator-only heartbeat/queue check. Prints counts, never messages or credentials."""
from datetime import datetime, timezone
from crewai_open_instinct.config import load_environment
from crewai_open_instinct.providers import Supabase
from crewai_open_instinct.schedules import timestamp

load_environment()
db = Supabase()
try:
    result = db.rpc("ci_runtime_status")
    now = timestamp(result["database_now"])
    ok = True
    for component, max_age in [("worker", 360), ("scheduler", 120)]:
        seen = result["heartbeats"].get(component)
        age = (now-timestamp(seen)).total_seconds() if seen else None
        healthy = age is not None and age <= max_age
        ok &= healthy
        print(component, "OK" if healthy else "STALE", f"age={int(age)}s" if age is not None else "no heartbeat")
    print("queued:",result["queued"],"uncertain:",result["uncertain"],"failed_24h:",result["failed_24h"])
    if not ok:
        raise SystemExit(1)
finally:
    db.close()
