"""Deterministic, timezone-aware schedules; no in-memory timers own state."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Use an ISO timestamp with an explicit timezone offset")
    return result.astimezone(UTC)


def validate_schedule(spec: dict, timezone_name: str) -> dict:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use an IANA timezone, for example America/Los_Angeles") from None
    kind = spec.get("type")
    allowed = {"once": {"type", "at"}, "interval": {"type", "minutes"},
               "daily": {"type", "time"}, "weekdays": {"type", "time"}}
    if kind not in allowed or set(spec) != allowed[kind]:
        raise ValueError("Schedule must be once/at, interval/minutes, daily/time or weekdays/time")
    if kind == "once":
        timestamp(spec["at"])
    elif kind == "interval":
        value = spec["minutes"]
        if isinstance(value, bool) or not isinstance(value, int) or not 15 <= value <= 10080:
            raise ValueError("Repeat intervals must be 15 minutes to 7 days")
    else:
        try:
            hour, minute = map(int, spec["time"].split(":"))
            if spec["time"] != f"{hour:02d}:{minute:02d}" or not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("Use a 24-hour HH:MM time") from None
    return spec


def next_run(spec: dict, timezone_name: str, after: datetime) -> datetime | None:
    validate_schedule(spec, timezone_name)
    after = after.astimezone(UTC)
    if spec["type"] == "once":
        at = timestamp(spec["at"])
        return at if at > after else None
    if spec["type"] == "interval":
        return after + timedelta(minutes=spec["minutes"])
    zone = ZoneInfo(timezone_name)
    hour, minute = map(int, spec["time"].split(":"))
    today = after.astimezone(zone).date()
    for offset in range(9):
        day = today + timedelta(days=offset)
        if spec["type"] == "weekdays" and day.weekday() >= 5:
            continue
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        candidate = local.astimezone(UTC)
        # DST gaps move forward through the gap; overlaps use the first occurrence.
        if candidate > after:
            return candidate
    raise ValueError("Could not calculate the next occurrence")


def scheduler_tick(db) -> int:
    """Coalesce missed recurring runs into one; dispatch is atomic in Postgres."""
    due = db.rpc("ci_due_automations")
    dispatched = 0
    for rule in due:
        now = timestamp(rule["database_now"])
        following = next_run(rule["schedule"], rule["timezone"], now)
        dispatched += bool(db.rpc("ci_dispatch_automation", p_rule=rule["id"],
            p_expected=rule["next_run_at"], p_next=following.isoformat() if following else None))
    db.rpc("ci_scheduler_heartbeat")
    return dispatched


def run():
    import logging
    import signal
    import time
    from .config import load_environment, required
    from .providers import Supabase
    load_environment()
    required("SUPABASE_URL")
    required("SUPABASE_SECRET_KEY")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    db = Supabase()
    try:
        while not stop:
            try:
                count = scheduler_tick(db)
                if count:
                    logging.info("Dispatched %s scheduled jobs", count)
            except Exception as exc:
                logging.error("Scheduler tick failed (%s)", type(exc).__name__)
            for _ in range(30):
                if stop:
                    break
                time.sleep(1)
    finally:
        db.close()
