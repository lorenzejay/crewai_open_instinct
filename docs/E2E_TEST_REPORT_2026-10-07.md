# E2E validation — October 7, 2026

**Experimental — not intended for production use.** These checks establish the
listed behavior in the local pilot; they do not certify a hosted deployment.

## Results

| Check | Result | Scope |
| --- | --- | --- |
| Python regressions | 96 passed | Existing offline test suite. |
| SQL regressions | 5 suites passed | All six migrations applied to fresh disposable PostgreSQL; account sessions, conversation bursts, database isolation, proactive rules, and text onboarding. |
| Backend E2E | 15 checks passed | Actual API handler, SQL functions, CrewAI Flow, GPT-6.1 Sol, scheduler, and outbox; synthetic tenants only in disposable PostgreSQL and captured outbound transport. |
| Browserbase | Passed | Actual conversation called `read_public_page` on example.com and returned its title; remote session was released. |
| Composio MCP | Passed | Hosted discovery returned four tools with Gmail/GitHub scope; the live briefing also executed the sender's connected GitHub reads. |
| Genuine iMessage acknowledgment | DELIVERED | Signed Sendblue webhook, live Supabase, worker, and real Sendblue transport. |
| Genuine scheduled GitHub briefing | DELIVERED | One proactive job and one outbound handle; two direct review requests with distinct PR links. |
| Runtime after test | Healthy | API and tunnel health passed; worker/scheduler heartbeats current; queued, uncertain, and failed-in-24-hours counts all zero. |

## Genuine five-minute PR briefing

The user texted a request to brief them on reviewable PRs in
`https://github.com/crewAIInc/crewAI`, specifying America/Los_Angeles and a
five-minute delay. The service committed the incoming message at **9:55:48 PM
Pacific**, saved the rule for **10:00:48 PM Pacific**, and sent a delivered
acknowledgment backed by that rule.

The worker was gracefully stopped and restarted before the deadline. The
persisted rule retained the same deadline and timezone. The scheduler dispatched
one job at **10:01:08 PM**, and Sendblue accepted its briefing at **10:01:24 PM**,
about **36 seconds after the deadline**. A subsequent status lookup confirmed
**DELIVERED**. The send timestamp records acceptance, not the precise device
arrival time. The scheduler polls every 30 seconds, and account reads/model
inference add processing time.

The briefing correctly used the direct-review-request category, contained two
GitHub PR links, and recorded no job error. No fabricated inbound message was
sent to the live service. The test did not send email or modify GitHub.

## Backend coverage

The 15 assertions verified unsigned-webhook rejection, signed first-text tenant
creation, duplicate-event handling, live-model note saving, read/typing signal
invocation, note recall, second-tenant isolation, note deletion, exact relative
deadline/timezone persistence, persistence across reopened database clients,
atomic scheduler dispatch, one persisted proactive notification, combining a
request with its separately sent repository URL, actual Browserbase research,
and private-network URL rejection.

Backend outgoing messages were captured in memory. Read/typing assertions prove
worker invocation there; they do not independently prove the visual indicators
on a device. Only the genuine user-text test used real outbound iMessage.
The backend reminder deadline was accelerated only in the disposable database;
the genuine five-minute test ran at its original saved deadline.

Gmail inbox behavior, new OAuth consent, Gmail/GitHub event subscriptions,
page-change monitoring, conditional Gmail followups, hosted deployment, and
multi-device messaging were not exercised live in this run. Existing offline
regressions cover relevant rule and transport boundaries. A FastAPI/Starlette
TestClient deprecation warning was non-blocking; dependencies were unchanged.

## Repeat the backend checks

Install the locked development dependencies and provision an **empty disposable
localhost PostgreSQL database** named, for example, `openinstinct_e2e`. The test
user must be able to create fixture roles/schema. This database must not contain
real tenant data. Use the intended model API key in the project environment;
`--live-model` explicitly opts into paid inference. `--browser` also uses
Browserbase to read example.com.

```sh
uv sync --locked --group dev
export TEST_DATABASE_URL=postgresql://127.0.0.1:5432/openinstinct_e2e
uv run python scripts/test_database.py
uv run python scripts/test_e2e.py --live-model --browser
```

The first script bootstraps Supabase-like fixtures, applies migrations, and runs
SQL regressions. The second requires that migrated database to have no tenant
data and creates synthetic test tenants. It refuses remote hosts and requires
`test` or `e2e` in the database name. Repeating it requires a fresh disposable
database; it does not delete existing tenants. It replaces messaging/account
credentials in its own process and captures outbound messages. Real provider
keys and conversation payloads are never included in this report.

To repeat the genuine transport test, keep API, worker, scheduler, and HTTPS
forwarding alive; have the user send a real text, confirm the saved deadline,
restart the idle worker before it is due, and verify a single outgoing message
with Sendblue status **DELIVERED**. Do not infer delivery from `ci_jobs.sent`.
