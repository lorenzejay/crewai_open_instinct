# OpenInstinct — CrewAI edition

> **Experimental — not intended for production use.** This project is under
> active development. Behavior and APIs may change.

An independent implementation inspired by [Merit Systems’ OpenInstinct](https://github.com/Merit-Systems/OpenInstinct).

A personal assistant you use by texting **+1 (786) 213-9363**. Each sender gets
a private workspace with conversation history, saved notes, and account
connections.

The backend uses **CrewAI, Supabase, Sendblue, Browserbase, and Composio MCP**.
It supports conversational help, public-page summaries, explicit memory,
read receipts, typing bubbles, reminders, scheduled briefings, page monitoring,
and Gmail/GitHub tools after authorization. Account alerts need additional
Composio webhook configuration.

## Get started

From the project root:

```sh
uv sync --locked --group dev
uv run preflight
uv run serve
```

Run `uv run worker` and `uv run scheduler` in two additional terminals inside the project root. Configure
the credentials and public Sendblue webhook using the backend guide below.

## Project guide

- [Setup, configuration, and features](../README.md)
- [Proactive features and conversation examples](PROACTIVE.md)
- [Hosted deployment plan](DEPLOYMENT.md)
- [Render service blueprint](../render.yaml)
- [Environment variable checklist](../.env.example)
- [Backend source](../src/crewai_open_instinct/)
- [Database migrations](../supabase/migrations/)
- [Tests](../tests/)

Credentials live in the project `.env`; exported process variables take
precedence. Archived environment files are preserved privately and are not loaded. The current shared Sendblue line
requires verified contacts. This is a local prototype; the API, worker, scheduler, and
public tunnel must stay running. The deployment guide explains how to move
these processes to always-running hosted services.
