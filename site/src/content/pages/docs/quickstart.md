---
title: roost quickstart (v1alpha1) | durable agents on E2B
description: Build roost, configure E2B and a model key, run roost serve, and talk to a durable Pi agent over HTTP with curl. v1alpha1; the API may change.
h1: 'roost quickstart: run a durable AI agent per user on E2B'
nav: Quickstart
section: Docs
order: 7
badge: v1alpha1
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  Build the `roost` binary and the sandbox directory, write a `roost.yaml` with your E2B key, a model provider key and a tenant API key, and start `roost serve`. Then create a workspace with `PUT`, wait until it is `active`, open a conversation and send messages with curl. This is v1alpha1: the API may change.
related:
  - use-cases/ai-employee-per-customer
  - use-cases/slack-ai-teammate
  - guides/ai-agent-crash-recovery
  - guides/ai-agent-duplicate-replies
faq:
  - q: Why does the first workspace take minutes to become active?
    a: The first workspace builds the E2B sandbox template from the staged driver and agent host. Later workspaces reuse the template; a changed driver or agent host is a new template.
  - q: Where are transcripts stored?
    a: In Pi Durable's own storage on the workspace's sandbox disk. roost's SQLite database holds only workspaces, execution grants and audit records.
  - q: Can I use a model through an LLM gateway?
    a: Yes. Set `base_url` on the provider to the gateway's address, and add `agent.model_info` (context window, maximum output tokens, reasoning, input types) for a model Pi's own catalog does not know.
  - q: Is there a roost CLI?
    a: Not yet. This version has one command, `roost serve`. The `roost` CLI for operators and agents is on the roadmap.
---

This guide runs roost on your machine with sandboxes on **E2B Cloud**: one `roost serve` process with SQLite, and one long-lived sandbox per workspace running Pi Durable. It follows what `scripts/e2e-m1.sh` does, step by step.

## Before you start

- Go 1.27 or later, and Node.js 22.19 or later with npm (to build the agent host)
- An [E2B](https://e2b.dev) account and API key
- An API key for a model provider; the examples use Anthropic
- `curl` and `jq`; the end-to-end test also uses `openssl` and `sqlite3`

## 1. Build

```bash
git clone https://github.com/JerryChaox/roost
cd roost

# stage the sandbox directory: roost-driver (linux/amd64) and the Pi agent host
scripts/build-sandbox.sh          # writes dist/sandbox

# the roost binary
go build -o roost ./cmd/roost
```

## 2. Configure

Start from `roost.example.yaml` in the repository. A minimal configuration for E2B Cloud:

```yaml
server:
  listen: 127.0.0.1:7070
  database: sqlite://./roost.db

workspace:
  provider: { api_url: https://api.e2b.app, api_key: secret://e2b }

agent:
  model: anthropic/claude-sonnet-4-5   # <provider>/<model id>, from Pi's catalog
  thinking: high
  system_prompt:
    base: pi                           # Pi's own system prompt; none: start empty

models:
  access: e2b                          # E2B's egress proxy injects the provider key
  providers:
    anthropic: { key: secret://anthropic }

tenants:
  acme:
    api_keys: [secret://acme-api-key]
```

Secrets are never written in the file. Each `secret://<name>` reference is read from the environment variable `ROOST_SECRET_<NAME>`, upper case with `-` turned into `_`: `secret://e2b` is `ROOST_SECRET_E2B` and `secret://acme-api-key` is `ROOST_SECRET_ACME_API_KEY`.

With `models.access: e2b`, the provider key is added by E2B's egress proxy to requests for the provider's host, and never enters the sandbox.

Notes for this version:

- Unknown keys are errors.
- `workspace.kits` must stay empty: every workspace uses one fixed sandbox template.
- `sleep_after`, `recover_wait` and `quiesce_wait` are accepted but not acted on yet.
- For a model behind an LLM gateway, set `base_url` on the provider and add `agent.model_info`.

## 3. Run roost serve

Set the secrets in your shell without echoing them, then start the control plane:

```bash
read -rs ROOST_SECRET_E2B && export ROOST_SECRET_E2B
read -rs ROOST_SECRET_ANTHROPIC && export ROOST_SECRET_ANTHROPIC
export ROOST_SECRET_ACME_API_KEY="$(openssl rand -hex 24)"

./roost serve --config roost.yaml --sandbox-dir dist/sandbox
```

`roost serve` is the HTTP API and the reconcilers that create sandboxes, issue execution grants and watch drivers. Nothing in a sandbox calls it, so it needs no public address.

## 4. Create a workspace

In another shell, with the tenant key from step 3:

```bash
export ROOST_KEY="$ROOST_SECRET_ACME_API_KEY"

curl -X PUT localhost:7070/v1/workspaces/user-42 \
  -H "Authorization: Bearer $ROOST_KEY" -H 'Content-Type: application/json' \
  -d '{"owner": "user-42"}'

# poll until "active"; the first workspace builds the template, which takes minutes
curl -s localhost:7070/v1/workspaces/user-42 -H "Authorization: Bearer $ROOST_KEY" | jq .phase
```

A workspace moves from `provisioning` to `active` once its sandbox's driver is ready, or to `failed` if building the template or creating the sandbox keeps failing. Conversation requests to a workspace that is not `active` get `409 workspace_busy`.

## 5. Open a conversation

```bash
curl -X POST localhost:7070/v1/workspaces/user-42/conversations \
  -H "Authorization: Bearer $ROOST_KEY" -H 'Content-Type: application/json' \
  -d '{"key": "thread-1"}'
# {"conversation": "c_01J9Z..."}

# the same key returns the same conversation; you can also look it up
curl -s "localhost:7070/v1/workspaces/user-42/conversations?key=thread-1" -H "Authorization: Bearer $ROOST_KEY"
```

## 6. Send a message and stream the answer

```bash
C=c_01J9Z...   # the conversation id from step 5
curl -X POST localhost:7070/v1/workspaces/user-42/conversations/$C/messages \
  -H "Authorization: Bearer $ROOST_KEY" -H 'Content-Type: application/json' \
  -d '{"id": "m-1", "text": "Reply with exactly the word: pong", "delivery": "queue"}'
# 202 {"status": "running"}

curl -N -H "Authorization: Bearer $ROOST_KEY" \
  localhost:7070/v1/workspaces/user-42/conversations/$C/events
```

The events stream is server-sent events:

- `entry`: a committed transcript entry, with its `cursor`, `kind` (`user`, `assistant`, `tool_result`, `system`, `reset`, `compaction` or `note`), the `run` it belongs to and the agent's own record in `raw`
- `live`: output as it is generated
- `run`: a run's status, such as `completed`, `failed` or `aborted`

A run's id is the id of the message that started it, here `m-1`. If the stream drops, reconnect with `?after=<last cursor>`. To read the transcript instead of streaming it:

```bash
curl -s localhost:7070/v1/workspaces/user-42/conversations/$C/entries -H "Authorization: Bearer $ROOST_KEY" | jq '[.entries[].kind]'
```

## 7. Retries, follow-ups and stopping

```bash
M=localhost:7070/v1/workspaces/user-42/conversations/$C

# the same id again: admitted once
curl -X POST $M/messages -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "m-1", "text": "Reply with exactly the word: pong"}'
# 200 {"status": "duplicate"}

# join the running run after its current tool round
curl -X POST $M/messages -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "m-2", "text": "Also list the files", "delivery": "steer"}'

# stop the running run and withdraw queued messages
curl -X POST $M/interrupt -H "Authorization: Bearer $ROOST_KEY"

# start a new model context, with an optional handoff note
curl -X POST $M/reset -H "Authorization: Bearer $ROOST_KEY" -d '{"note": "Continue from the summary above."}'
```

## 8. Run the end-to-end test

The repository's end-to-end test does all of the above against E2B Cloud on a temporary SQLite database and a random tenant, then removes every sandbox it created:

```bash
# E2B_API_KEY and ANTHROPIC_API_KEY come from the environment or the repository's .env
scripts/e2e-m1.sh

# also kill the driver (a new grant must take over on the same sandbox, and the
# old driver token must get 401) and kill the agent host mid-run (the run must complete)
scripts/e2e-m1.sh --failures
```

## Not in this version

roost is **v1alpha1**. This version has no CLI beyond `roost serve`, runs sandboxes on E2B Cloud only, stores its state in SQLite only, and uses one fixed sandbox template. **On the roadmap:** continuous backups with kopia, restore to any step and forking a snapshot into many workspaces (M2); self-hosted sandboxes with E2B Embed, per-grant keys from a LiteLLM gateway and one-command deployments (M3); the `roost` CLI, the operator API and Postgres.
