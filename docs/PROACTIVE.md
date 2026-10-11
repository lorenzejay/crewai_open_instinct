# Proactive conversations

The API receives texts/events, the scheduler enqueues due rules, and the worker
generates and delivers replies through the existing durable outbox. Supabase
stores rules, due times, snapshots, events, leases, notification settings, and
conversation history. Restarting any process preserves this state.

## Try by texting the agent

- `My timezone is America/Los_Angeles. Remind me in 5 minutes to call Alex.`
- `Every weekday at 8 AM Pacific, summarize emails matching is:important newer_than:1d.`
- `My timezone is America/Los_Angeles. In 5 minutes brief me on PRs I should review in https://github.com/crewAIInc/crewAI.`
- `Check https://example.com every hour and text me if the page changes.`
- `Watch my Gmail and text me only when alex@example.com emails me.`
- `Watch commits on owner/repo and tell me about new commits.`
- `Friday at 3 PM Pacific, remind me to follow up if Gmail has no messages matching from:alex@example.com after:2026/10/07.`
- `List my automations.`
- `Pause all proactive messages.` / `Resume all proactive messages.`
- `Cancel Call Alex.` (Use the exact name from the confirmation/list.)
- `Set quiet hours from 10 PM to 8 AM Pacific and limit proactive texts to 5 per day.`

The agent asks for a timezone when none is provided/saved. It confirms the
stored time after a successful tool call. Quiet hours are off initially and
the default notification limit is 10 per workspace per local day. Regular
chat replies are unaffected. Max 50 non-cancelled rules per workspace; cancel
old completed reminders to reclaim capacity.

## Supported behavior

Reminders support one-time, daily, weekday, and 15-minute-to-7-day interval
schedules. Daily/weekday schedules preserve local wall-clock time across DST;
nonexistent times move forward through the gap, repeated times use the first
occurrence. The scheduler ticks every 30 seconds. Worker load and quiet hours
can delay actual delivery. A missed recurring schedule produces one current
run rather than a burst of all missed occurrences. Missed one-time reminders
run when service resumes.

Relative delays start when the inbound message arrives. If processing resumes
after that deadline, the task is saved for immediate dispatch. Text and link
messages arriving in the same five-second burst are combined into one request;
their original provider messages remain in the database.
Scheduling turns first extract a validated structured plan with a tool-free
parser. Application code validates and saves complete creation requests.
Management turns use a focused agent with automation tools. New-task
confirmations are formatted from the database result, so a model promise
alone cannot confirm a reminder or briefing.

Briefings read one public URL or up to 20 Gmail messages matching the stored
query. Gmail reads use the user's authenticated hosted MCP session with a
fixed `GMAIL_FETCH_EMAILS` call. They fetch metadata/preview evidence, not full
attachments or the whole inbox. Results are not guaranteed to be newest-first;
use a bounded date query. Disconnected accounts fail the run and record an
error; they never produce a fabricated empty-inbox report.

GitHub PR briefings use fixed read-only hosted MCP calls to verify the connected
GitHub username, fetch up to 30 direct pending review requests, and fetch the
30 most recently updated open PRs. The editor receives at most 15 non-draft
candidate PRs by other authors. Application code labels direct review requests
and suggested candidates from the verified query results; the model only
summarizes up to five PR descriptions. This is a bounded briefing, not an exhaustive repository
audit, diff review, or CI check. Scheduled reads need the existing Composio
session permissions and a connected GitHub account; they do not require
Composio trigger/webhook permissions or a webhook signing secret.

Page monitors compare normalized visible text from bounded Browserbase reads.
The first check establishes a silent baseline; unchanged checks stay quiet.
A change produces a summary of the current page, not a full semantic diff.
Dynamic counters/ads can count as changes. Monitoring arbitrary visual changes
or just a selected element is not implemented.

Conditional followups perform a one-time Gmail query and send the saved
reminder only when the successful query returns an empty message list. They
do not infer reply relationships or automatically send follow-up email. Ask
for an explicit sender/date/thread search scope.

Account watches currently support new Gmail messages and GitHub commits on a
named repository. The notification editor evaluates the saved condition
against the event payload and can stay quiet. It has no account tools or
external-write capabilities. Event payloads are untrusted data.

## Composio watch setup

1. Each sender must connect their own Gmail/GitHub through the conversation.
2. The project key needs existing session permissions plus **Triggers: Read
   and write**, **Connected accounts: Read only**, and **Webhooks: Read and
   write** when creating the webhook subscription. Rotate a scoped key to
   add permissions; they cannot be edited after creation.
3. Register a Composio trigger webhook subscription at
   `<APP_URL>/webhooks/composio`, with trigger-event delivery enabled and V3
   payloads. Copy the subscription's signing secret into
   `COMPOSIO_WEBHOOK_SECRET` on the API and worker. Restart both services.
4. Text the watch request. Creation validates a live inbound-job lease,
   resolves exactly one ACTIVE account belonging to that workspace, and
   binds the trigger instance to the workspace/account. Multiple active
   accounts of the same toolkit require disambiguation before using this
   version; the agent will report setup failure instead of picking one.

One local watch is supported per provider trigger instance. Gmail watches
share the default new-message trigger, so use one combined notification
condition per Gmail account. GitHub watches may target different repositories.
Changing a watch's provider target requires a new name. Pause/cancel stops
local processing and delivery; it does not delete the provider subscription.
Remove unused instances in Composio to stop upstream traffic. If a lease ends
after provider upsert but before local binding, the instance can remain
unbound; its events are ignored and the operator can remove it there.

The webhook verifies SDK signatures and their five-minute timestamp window
before accepting an event. It also matches the saved trigger, account, user,
toolkit event, and current workspace identity. Legacy payloads lacking an
owner are rejected. Retries deduplicate by webhook event ID. At most 20 pending
events per watch are queued; excess events are dropped to bound bursts.
Events received while globally paused are discarded; resume does not replay
them. Gmail delivery can lag by approximately 15 minutes on Composio-managed
authentication; it is not an instant-alert guarantee.

## Operations

Run the API, worker, and scheduler separately:

```sh
uv run serve
uv run worker
uv run scheduler
```

These are separate-terminal commands. `docker compose up --build` starts all
three. For operator checks run `uv run python scripts/check_runtime.py`; it
prints heartbeats and queue/error counts without exposing conversations.
The API `/health` is a liveness check and does not prove worker/scheduler health.

Pausing preserves queued/due reminders; resuming coalesces missed recurring
runs. Cancelling drops unsent jobs. A request already accepted by Sendblue
cannot be recalled. Daily caps include uncertain deliveries to prevent blind
resends. Quiet hours and caps defer jobs while allowing normal chat to proceed.
Read receipts and typing bubbles are used only for inbound chat turns.

Data is retained in Supabase; this version does not automatically prune rule
events, old messages, or job payloads. Add a retention policy before broad use.
Scheduled failures appear as `last_error` in the automation list and in the
operator status. There is no automatic user-facing failure alert yet.

Inbound generation errors now enqueue a plain failure reply when the user job
lease is still valid. Delivery timeouts remain uncertain and are not retried
automatically. Heartbeat RPCs return HTTP 204 with no body; this successful
response must be handled before parsing JSON so background loops keep running.

## References

- [Composio triggers](https://docs.composio.dev/docs/triggers)
- [Composio webhook verification](https://docs.composio.dev/docs/setting-up-triggers/subscribing-to-events)
- [Composio scoped-key permissions](https://docs.composio.dev/reference/authenticating-to-composio/project-api-key-permissions)
- [Hosted deployment plan](DEPLOYMENT.md)
