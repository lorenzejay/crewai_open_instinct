"""Conversation tools use a live user job lease, never caller-selected tenants."""
import json
import re
from datetime import datetime, timezone, timedelta

from crewai.tools import tool

from .schedules import next_run, validate_schedule, timestamp


def automation_tools(db, job_id: str, lease_token: str, *, requested_at: str = "", saved: list | None = None):
    lease = {"p_job": job_id, "p_token": lease_token}

    @tool("create_automation")
    def create(name: str, kind: str, instruction: str, timezone_name: str,
               schedule_type: str = "once", at: str = "", delay_minutes: int = 0,
               local_time: str = "", interval_minutes: int = 60,
               url: str = "", gmail_query: str = "", github_repo: str = "") -> str:
        """Create an explicitly requested reminder, briefing, page monitor or conditional followup.
        kind: reminder/briefing/monitor/followup. timezone_name: explicit IANA timezone.
        schedule_type: once/daily/weekdays/interval. For once use at (ISO with offset)
        OR delay_minutes for relative reminders like in 5 minutes (not both).
        Relative delays start when the message arrived; overdue requests run immediately.
        For daily/weekdays use local_time HH:MM; interval uses interval_minutes (15..10080).
        monitor needs url; briefing needs exactly one of url, gmail_query, github_repo.
        github_repo is owner/repo; it reads open PRs and your pending review requests via MCP.
        followup needs gmail_query.
        A followup texts its reminder only when that Gmail query returns no messages.
        Creating the same name updates that automation. No booking or external writes.
        """
        try:
            now = datetime.now(timezone.utc)
            if delay_minutes:
                if at or schedule_type != "once" or not 1 <= delay_minutes <= 525600:
                    raise ValueError("Relative reminders need a positive delay and no absolute time")
                start = timestamp(requested_at) if requested_at else now
                at = (start+timedelta(minutes=delay_minutes)).isoformat()
            spec = {"type": schedule_type}
            if schedule_type == "once":
                spec["at"] = at
            elif schedule_type == "interval":
                spec["minutes"] = interval_minutes
            else:
                spec["time"] = local_time
            source = {}
            if url:
                source["url"] = url
            if gmail_query:
                source["gmail_query"] = gmail_query
            if github_repo:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", github_repo):
                    raise ValueError("GitHub repository must be owner/repo")
                source["github_repo"] = github_repo
            validate_schedule(spec, timezone_name)
            if kind not in {"reminder", "briefing", "monitor", "followup"}:
                raise ValueError("Unsupported automation kind")
            if kind == "reminder" and source:
                raise ValueError("Reminders do not use a data source")
            if kind == "monitor" and set(source) != {"url"}:
                raise ValueError("Page monitoring needs one public URL")
            if kind == "briefing" and set(source) not in ({"url"}, {"gmail_query"}, {"github_repo"}):
                raise ValueError("Briefings need one public URL, Gmail query or GitHub owner/repo")
            if kind == "followup" and set(source) != {"gmail_query"}:
                raise ValueError("Followups need an explicit Gmail reply search query")
            if "url" in source:
                from .browser import public_url
                public_url(source["url"])
            if "gmail_query" in source and (not isinstance(source["gmail_query"], str)
                    or not 1 <= len(source["gmail_query"]) <= 1000):
                raise ValueError("Gmail query must be 1–1000 characters")
            if kind == "followup" and spec["type"] != "once":
                raise ValueError("Conditional followups are one-time checks")
            due = next_run(spec, timezone_name, now)
            if due is None and delay_minutes:
                due = now+timedelta(seconds=1)
            if due is None:
                raise ValueError("The reminder time must be in the future")
            result = db.rpc("ci_put_automation", **lease, p_name=name, p_kind=kind,
                p_instruction=instruction, p_timezone=timezone_name, p_schedule=spec,
                p_source=source, p_next=due.isoformat())
            if not isinstance(result, dict) or not result.get("name") or not result.get("next_run_at"):
                raise ValueError("Database did not confirm a saved task")
            if saved is not None:
                saved.append(result)
            return json.dumps(result)
        except (ValueError, TypeError, KeyError) as exc:
            return "Not saved: " + str(exc)
        except Exception as exc:
            return f"Not saved ({type(exc).__name__}); do not claim it was scheduled."

    @tool("manage_automations")
    def manage(action: str, name: str = "", timezone_name: str = "",
               quiet_start: int = -1, quiet_end: int = -1, daily_limit: int = 10) -> str:
        """List, pause, resume or cancel automations by exact name. action=list/pause/resume/cancel.
        action=pause_all/resume_all pauses only proactive texts; chat still works.
        action=settings sets timezone_name, daily_limit (1..50), and quiet hours
        quiet_start/quiet_end (0..23, or both -1 to disable). Only change when requested.
        Pause/cancel cannot recall an already accepted outgoing text.
        """
        try:
            if action == "settings":
                from zoneinfo import ZoneInfo
                ZoneInfo(timezone_name)
                if not 1 <= daily_limit <= 50 or not (
                    quiet_start == quiet_end == -1 or
                    0 <= quiet_start <= 23 and 0 <= quiet_end <= 23 and quiet_start != quiet_end):
                    raise ValueError("Invalid quiet hours or notification limit")
            return json.dumps(db.rpc("ci_manage_automations", **lease, p_action=action,
                p_name=name, p_timezone=timezone_name, p_quiet_start=quiet_start,
                p_quiet_end=quiet_end, p_limit=daily_limit))
        except Exception as exc:
            return f"Automation request failed ({type(exc).__name__}); do not claim a change."

    @tool("watch_account")
    def watch(name: str, toolkit: str, instruction: str, owner: str = "", repo: str = "") -> str:
        """Watch the user's connected Gmail for new messages or GitHub for repository commits.
        toolkit=gmail/github. GitHub requires owner and repo; leave both empty for Gmail.
        instruction specifies when to notify (e.g. only mail from Alex). Requires prior
        account connection plus Composio trigger permissions and webhook setup.
        This creates a read/notification subscription, never sends email or changes a repo.
        """
        from .account_events import create_watch
        try:
            return json.dumps(create_watch(db, job_id=job_id, lease_token=lease_token,
                name=name, toolkit=toolkit, instruction=instruction,
                trigger_config={"owner": owner, "repo": repo} if owner or repo else {}))
        except Exception as exc:
            return f"Watch not saved ({type(exc).__name__}). Check account connection, trigger permissions and webhook setup."

    return [create, manage, watch]
