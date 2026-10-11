# AGENTS.md — Project setup and operations

## Scope and status

Public project name: **OpenInstinct — CrewAI edition**. Credit Merit Systems’
original OpenInstinct as inspiration; this is an independent implementation.
The distribution is `crewai-open-instinct`, and the Python import package is
`crewai_open_instinct` under `src/`. The checkout folder `crewai-open-instinct/`
is the application and repository root.

This directory is the repository root. It contains `pyproject.toml`, `uv.lock`,
`src/`, `supabase/`, `scripts/`, `.github/`, `compose.yaml`, and `render.yaml`.
Run commands from this directory. Do not rely on files in its parent directory.

**Experimental — not intended for production use.** Behavior and APIs may
change. This implementation has not been security audited or validated for
production. Keep this status visible when editing documentation or describing
the service to a user.

The application is a text-first, tenant-scoped iMessage assistant. A signed
Sendblue inbound webhook identifies the sender and creates their private
workspace. Users text the configured number; they do not sign up to the app.
Gmail/GitHub consent, when requested, happens through a Composio link inside the
conversation.

## Before setting anything up

1. Read [README.md](README.md), [.env.example](.env.example), and the relevant
   source files. Preserve existing files, credentials, database data, and
   webhook registrations. Do not scaffold a replacement CrewAI project.
2. Check whether the service is already running. Inspect port 8000, the local
   `/health` endpoint, and, if present, private `.runtime/*.pid` files. A recorded
   PID alone is not proof of a running process; verify its command before using it.
3. Reuse healthy services. Do not start duplicate API processes, workers,
   schedulers, or tunnels. Stop only this project's identified processes when
   a restart is needed. Allow active work to finish during graceful shutdown.
4. Identify missing variable names without printing credential values. Ask
   the user for the missing account setup or configuration, and continue work
   that does not depend on it. Never put credentials in chat, source, notes,
   screenshots, command arguments, or logs.

## Services the user needs

| Service | Requirement | Setup needed |
| --- | --- | --- |
| Supabase | Required | A project with the `ci_*` migrations applied, its project URL, and a backend secret API key. Stores tenant workspaces, messages, notes, jobs, schedules, and private account sessions. |
| Sendblue | Required | API credentials, a supported sending/receiving line, an eligible test contact if the line requires verification, and a receive webhook with a signing secret. |
| Model provider | Required | OpenAI is the tested default. Supply its API key and use the configured `LLM_MODEL`. Anthropic is an alternate code-supported provider; its optional dependencies may need installation and validation. |
| Browserbase | Required by current worker preflight | API key for public-page research. Set a project ID when needed for the account. Browsers run remotely, not on the user's laptop. |
| Public HTTPS endpoint | Required for receiving real messages | A tunnel such as ngrok for local development, or a hosted API for deployment. Sendblue cannot call the user's `localhost` directly. |
| Composio | Optional for core chat; required for Gmail/GitHub features | A project API key, Gmail/GitHub auth configurations, and each sender's own OAuth connections. Supplies hosted MCP tools. |
| Composio trigger webhook | Optional | Additional permissions, a webhook subscription, and its signing secret for new-Gmail and GitHub-commit watches. |
| Supabase Auth public key | Optional | Only needed by retained legacy authenticated account/link APIs. Text-first onboarding does not require an app login. |

Linq, Kernel, CrewAI Enterprise/AMP, a locally hosted MCP server, Redis, and a
separate storage service are not required. Attachment storage/analysis and
authenticated browser automation are not implemented. Supabase Storage buckets
do not need to be provisioned for chat or reminders.

## Credentials and configuration

The app loads this project's `.env`; exported process variables take precedence.
It does not load a parent `.env`, `.env.local`, or `.env.shared.backup`.
Preserve an existing `.env`. Only in a fresh checkout without one, copy
`.env.example` to `.env` and populate it privately.

| Variables | Where to obtain or configure them |
| --- | --- |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | The intended Supabase project's URL and backend API key. Never expose the backend key to a browser. |
| `SUPABASE_DATABASE_PASSWORD` | The database password, used only when applying migrations through `scripts/migrate.py`. |
| `SUPABASE_DATABASE_HOST`, `SUPABASE_DATABASE_USER` | Optional connection overrides from Supabase Connect, especially for the session pooler when direct IPv6 connectivity is unavailable. |
| `SENDBLUE_API_KEY`, `SENDBLUE_API_SECRET` | The user's Sendblue API credentials. |
| `SENDBLUE_PHONE_NUMBER` | The assigned service line in E.164 format, e.g. `+15555550100`, with no spaces. Do not hardcode a maintainer's live number. |
| `SENDBLUE_WEBHOOK_SECRET` | A randomly generated secret stored privately and configured identically on the Sendblue receive webhook. |
| `BROWSERBASE_API_KEY` | The user's Browserbase API key. |
| `BROWSERBASE_PROJECT_ID` | Optional project selection for Browserbase. |
| `OPENAI_API_KEY`, `LLM_MODEL` | Default model configuration: `LLM_MODEL=openai/gpt-6.1-sol`. Use the user's intended OpenAI account. |
| `ANTHROPIC_API_KEY` | Only when using `LLM_MODEL=anthropic/<model>` instead of OpenAI; validate dependencies before switching providers. |
| `COMPOSIO_API_KEY` | Optional project key for account features, from the same project as the auth configurations. |
| `COMPOSIO_WEBHOOK_SECRET` | Optional subscription signing secret for account event watches. |
| `SUPABASE_PUBLISHABLE_KEY` | Optional legacy authenticated APIs; unnecessary for the normal text-first flow. |
| `APP_URL` | Public HTTPS base URL for webhook setup; set/update it for the intended tunnel or deployment. |
| `APP_ORIGINS` | Optional comma-separated browser origins for CORS; defaults to `http://localhost:3000`. |
| `PORT` | Optional API port; defaults to `8000`. Adjust tunnel forwarding when changing it. |

GPT-6 models use CrewAI’s Responses API adapter with low reasoning effort and
`store=False`. Supabase continues to own conversation state. Older OpenAI and
Anthropic overrides retain their existing adapter settings.

`DATABASE_URL` in the example is for optional manual database tooling.
`scripts/migrate.py` does not read it: that script uses the
`SUPABASE_DATABASE_*` settings above, PostgreSQL port 5432, and SSL.
The running application uses the Supabase API key, not the database password.

Keep `.env`, archived `.env.*` files, `.runtime/`, and `.venv/` out of Git and
container images; `.env.example` is the publishable template. Restart affected
services after changing their configuration.

## Install and prepare the database

Use Python 3.11–3.13 and `uv`. The workflow pins uv 0.11.18. Keep the dependency
lockfile and install the development group for tests and migration tooling:

```sh
uv sync --locked --group dev
uv run preflight
```

For a new Supabase project, review and apply all SQL migrations in filename
order. The intended target must be verified before modifying its schema:

```sh
uv run python scripts/migrate.py
```

Alternatively apply `supabase/migrations/*.sql` in order through Supabase SQL
Editor. The migrations are additive/repeatable and use separate `ci_*` tables;
do not delete or reset an existing project to bootstrap this application.
Do not assume a contributor's project already has the pilot schema.

Validate provider authentication after required values are configured:

```sh
uv run preflight --online
```

Offline preflight checks configuration. Online preflight makes read-only checks
against Supabase, Sendblue, and Browserbase. Its Supabase Storage endpoint probe
does not imply that a storage bucket is needed. Neither mode proves model
inference, MCP tool execution, webhook delivery, or a working end-to-end chat.

## Start locally

Run these in three separate terminals, each in the repository root:

```sh
uv run serve
```

```sh
uv run worker
```

```sh
uv run scheduler
```

| Process | Responsibility |
| --- | --- |
| API (`serve`) | Receives authenticated webhooks and commits inbound jobs. Defaults to port 8000. |
| Worker (`worker`) | Generates replies and scheduled briefings, then delivers persisted outgoing messages. |
| Scheduler (`scheduler`) | Dispatches due Supabase automation rules. Needed for reminders and scheduled briefings. |

`uv run kickoff` is also wired to the worker; it is not an all-services launcher.
Keep the three processes running for the full conversational/proactive flow.
If the user asks for background operation, use a process manager or detached
processes with private logs and verified PID records; do not assume an agent
tool's temporary execution session will keep the service alive.

Docker is an alternative when a working Docker daemon is available:

```sh
docker compose up --build
```

Compose starts all three app services and loads the project `.env`. It does not
provision Supabase or any provider account, apply migrations, or create a tunnel.
Use either Compose or the existing local processes, rather than launching both.
The image omits dev-only migration/test dependencies; use the local or CI
development environment for migrations.

## Expose the API to Sendblue

For local development, run a tunnel in a fourth terminal:

```sh
ngrok http 8000
```

Use the public HTTPS URL it reports. Configure a Sendblue **receive webhook**
at `<public-https-base>/webhooks/sendblue`, with the exact same secret as
`SENDBLUE_WEBHOOK_SECRET`. The API verifies the `sb-signing-secret` header.
Preserve unrelated webhook registrations; update this application's registration
when its public URL changes. Verify that the webhook targets the configured line.
The current pilot line may require verified contacts; unrestricted public
onboarding needs a provider-supported line and routing setup.

Useful local endpoints:

- `http://localhost:8000/health`: API liveness only.
- `http://localhost:8000/test`: information page and Open Messages action.
- `http://localhost:8000/docs`: API reference.

The `/test` page is optional. The user can text the configured line directly.
Do not introduce an app sign-in step into that flow.

## Optional Gmail/GitHub through Composio MCP

1. Configure Gmail and GitHub auth configurations in the intended Composio
   project. Supply `COMPOSIO_API_KEY` privately.
2. For ordinary account tools and scheduled account reads, the project key
   needs **Session management: read and write** and **Session tool execution:
   write**. Connected-accounts/observability read permissions are optional
   diagnostics unless enabling the watches below.
3. Have the user text `Connect my Gmail` or `Connect my GitHub`, complete the
   Composio consent link, and reply `done`. An issued link alone is not proof
   of an active connection.
4. Preserve per-sender ownership. The default Composio user ID is the workspace
   UUID; operator identity mappings must belong to that one workspace. Never
   share dashboard test accounts across all phone senders.

CrewAI calls the hosted Composio MCP using the existing integration and transport
compatibility layer. The SDK creates/restores scoped sessions. Remote workbench
code execution and cross-user account overrides remain disabled.

For event watches only, additionally configure:

- **Triggers: read/write**, **Connected accounts: read**, and **Webhooks:
  read/write** for subscription setup.
- A trigger-event subscription at `<public-https-base>/webhooks/composio` with
  V3 payloads, plus its signing secret as `COMPOSIO_WEBHOOK_SECRET` on API/worker.
- A connected account and an explicit watch request from the user. Current
  watches support new Gmail messages or GitHub repository commits.

Restart API/worker after configuration changes. Scoped project-key permissions
may require replacing the key. Regular chat, simple reminders, and scheduled
GitHub/Gmail reads do not need Composio trigger/webhook permissions. Account
briefings still require the corresponding connected account and session access.
See [docs/PROACTIVE.md](docs/PROACTIVE.md) for watch behavior and limitations.

## Verification and smoke tests

Run only the checks appropriate to the requested work:

```sh
uv run pytest -q
uv run python scripts/check_runtime.py
```

`check_runtime.py` reports worker/scheduler heartbeat ages, queued work, failed
work, and uncertain sends. API `/health` alone cannot establish worker or
scheduler health. A zero queued count alone also does not prove successful
device delivery.

When Composio is configured, test hosted MCP discovery separately:

```sh
uv run python scripts/check_composio_mcp.py
```

This creates and deletes a temporary isolated session; it does not read the
user's inbox/repositories or send messages.

For SQL regressions, use an **empty disposable localhost PostgreSQL database**
with a test user able to create the test roles/schema. The helper creates its
Supabase-like fixtures, applies migrations, and runs every SQL regression:

```sh
TEST_DATABASE_URL=postgresql://localhost:5432/agent_test uv run python scripts/test_database.py
```

Never run the bootstrap against live Supabase. See
[.github/workflows/check.yml](.github/workflows/check.yml) for the isolated
PostgreSQL test configuration and Linux Docker build.

For user-authorized model-backed E2E tests, use `scripts/test_e2e.py --live-model`
with a freshly migrated, empty-tenant localhost `TEST_DATABASE_URL`. Add
`--browser` for a real Browserbase example.com read. This opts into paid API
calls. The script creates synthetic identities only in disposable PostgreSQL,
captures every outgoing message, and disables account MCP for those tenants.
See [the E2E report](docs/E2E_TEST_REPORT_2026-10-07.md) for the exact setup.
Synthetic test events must never be posted to the live webhook.

For a user-authorized real-phone smoke test, have the user text the service:

- `Remember that I prefer vegetarian meals.` Then ask for the saved preference.
- `My timezone is America/Los_Angeles. Remind me in 5 minutes to call Alex.`
- After connecting GitHub: `In 5 minutes brief me on PRs I should review in https://github.com/owner/repo.`

Confirm the acknowledgment is backed by a saved automation, then verify the
eventual message. A restart before the due time should preserve the task and
produce one notification. Do not fabricate inbound user messages or send
unsolicited real-phone tests. Provider acceptance is distinct from delivery;
check Sendblue status or ask the user to confirm receipt when relevant.

## Troubleshooting

| Symptom | Checks and action |
| --- | --- |
| Text receives no reply | Verify API, HTTPS forwarding, matching Sendblue secret/line, committed inbound job, worker heartbeat, and queue/error state. |
| Chat works but reminder does not | Verify an automation was saved, scheduler heartbeat, next due time/timezone, pause/quiet-hour/daily-limit settings, and proactive job errors. A model promise is not a saved task. |
| Background loop reports `JSONDecodeError` | SQL void heartbeat RPCs return HTTP 204 with no body. Preserve the empty-success response handling in `providers.py`. |
| RPC/schema errors | Confirm the intended Supabase project and all migrations. Inspect schema-cache reload before rerunning or changing database data. |
| Direct database connection fails | Use Supabase Connect's session-pooler host/user when direct IPv6 is unavailable. Verify database password separately from API credentials. |
| Composio returns 403 | Check permissions for the failing operation: sessions for chat/reads, trigger/account/webhook scopes for watches. A missing watch permission does not require disabling ordinary account reads. |
| Wrong Gmail/GitHub account | Verify workspace identity, restored session owner, and the sender's consent. Never repair this by globally attaching the operator's account. |
| MCP teardown error | Inspect `mcp_compat.py` and the pinned CrewAI/MCP versions; the transport must enter and exit its context in the same task. |
| Job is `uncertain`, or `sending` lease expired | Reconcile with Sendblue history/status before changing it. Sending timeouts may occur after acceptance. Never blindly retry; unresolved sends can block later turns. |

Keep diagnostic output limited to IDs, status codes, counts, and safe error
types. Private runtime logs can contain conversation/tool data; do not publish
them as release artifacts or paste them into public issues.

## Development and deployment notes

Use `pyproject.toml`/`uv.lock` as the version source of truth. Do not upgrade
dependencies merely to start the app. Before modifying CrewAI APIs, check the
installed version, current official documentation, and relevant changelog;
report a newer available version and assess compatibility before an upgrade.
The preserved generic reference is [docs/CREWAI_REFERENCE.md](docs/CREWAI_REFERENCE.md).
Use this project's actual Flow/entrypoints over generic scaffolding examples.

Preserve signed webhook validation, tenant scoping, active job leases,
persisted confirmations, and separation between proactive reads and account
writes. An explicit user request is required for email sends or GitHub changes;
untrusted email, PR descriptions, webpages, and tool output cannot authorize them.

For hosted operation, follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and the
project-root [render.yaml](render.yaml): separate always-running API, worker,
and scheduler with Supabase persistence. Hosting, billing, provider account
creation, and unrestricted messaging eligibility are separate setup steps;
prepared files alone do not mean a deployment exists. Verify a Linux build and
real-message flow before claiming it is operational beyond the local machine.

OpenInstinct was the original design reference, not a runtime dependency. Its
warning that it is not intended for production use remains relevant context;
this independent implementation is also explicitly experimental.
