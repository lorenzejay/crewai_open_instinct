import logging
import time
from datetime import datetime, timezone

from .config import check_configuration, service_line
from .providers import Sendblue, Supabase

log = logging.getLogger(__name__)


def signal(action, **kwargs):
    """A cosmetic provider failure must not change durable reply state."""
    try:
        action(**kwargs)
    except Exception as exc:
        log.warning("Sendblue conversation signal failed (%s)", type(exc).__name__)


def process_job(db: Supabase, delivery: Sendblue, job: dict) -> None:
    job_id, token = job["id"], job["lease_token"]
    sending = job["status"] == "sending"
    composing = None
    ctx = None
    try:
        ctx = db.rpc("ci_context", p_job=job_id, p_token=token)
        if ctx["line"] != service_line():
            raise ValueError("Persisted job belongs to another sending line")
        proactive = ctx.get("kind") == "proactive"
        if proactive and not db.rpc("ci_allow_proactive", p_job=job_id, p_token=token):
            return
        if sending:
            handle = delivery.send(line=ctx["line"], recipient=ctx["phone"], content=job["reply"])
            if not db.rpc("ci_complete", p_job=job_id, p_token=token, p_handle=handle):
                raise RuntimeError("Delivery confirmation was not recorded")
        elif proactive:
            from .proactive import generate
            generate(db, job_id=job_id, lease_token=token, context=ctx)
        else:
            from .main import AssistantFlow
            composing = {"line": ctx["line"], "recipient": ctx["phone"]}
            signal(delivery.mark_read, **composing)
            # Expire even if the worker crashes; normal completion stops early.
            signal(delivery.typing, **composing, state="start", max_duration_ms=240000)
            flow = AssistantFlow()
            flow.kickoff(inputs={"job_id": job_id, "lease_token": token,
                                 "requested_at": job.get("created_at", "")})
            if not db.rpc("ci_reply", p_job=job_id, p_token=token, p_reply=flow.state.reply):
                raise RuntimeError("Reply discarded because lease ended")
    except Exception as exc:
        code = type(exc).__name__
        log.error("job=%s phase=%s error=%s", job_id, "delivery" if sending else "generation", code)
        if not sending and ctx and ctx.get("kind", "user") == "user" and composing:
            # A tool might already have saved a schedule, so don't claim nothing happened.
            if db.rpc("ci_reply", p_job=job_id, p_token=token,
                      p_reply="I couldn't finish processing that request. Please ask me to check your scheduled tasks or try again."):
                return
        db.rpc("ci_error", p_job=job_id, p_token=token, p_sending=sending, p_code=code)
    finally:
        if composing:
            signal(delivery.typing, **composing, state="stop")


def run():
    missing = check_configuration(worker=True)
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import signal as process_signal
    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    process_signal.signal(process_signal.SIGTERM, shutdown)
    process_signal.signal(process_signal.SIGINT, shutdown)
    db, delivery = Supabase(), Sendblue()
    last_heartbeat = 0.0
    try:
        while not stop:
            try:
                if time.monotonic()-last_heartbeat >= 30:
                    db.rpc("ci_worker_heartbeat")
                    last_heartbeat = time.monotonic()
                jobs = db.rpc("ci_claim")
                if jobs:
                    job = jobs[0]
                    if job.get("kind", "user") == "user" and job["status"] == "running" and job.get("created_at"):
                        # Allow a separately sent link to join the same five-second turn.
                        created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
                        remaining = 5 - (datetime.now(timezone.utc) - created).total_seconds()
                        if remaining > 0:
                            time.sleep(min(remaining, 5))
                    process_job(db, delivery, jobs[0])
                else:
                    time.sleep(2)
            except Exception as exc:
                log.error("Worker iteration failed (%s)", type(exc).__name__)
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
        delivery.close()
