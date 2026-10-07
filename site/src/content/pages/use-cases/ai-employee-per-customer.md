---
title: How to give every customer their own AI agent | roost
description: Give each customer a dedicated AI agent with its own long-lived computer, files and transcripts, that survives crashes and never runs twice.
h1: How to give every customer their own AI agent
nav: An agent per customer
section: Use cases
order: 1
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  Give each customer a workspace: one long-lived sandbox that holds their agent's files and transcripts, with exactly one agent running in it. Your backend creates the workspace under the customer's id, opens a conversation per thread or ticket, and sends messages over HTTP. roost does this on E2B sandboxes with Pi Durable as the agent, so a crashed run continues instead of starting over.
related:
  - use-cases/slack-ai-teammate
  - use-cases/long-running-agents
  - guides/ai-agent-duplicate-replies
  - docs/quickstart
faq:
  - q: Should I create one workspace per customer or one per user?
    a: A workspace is one computer with one agent, and every conversation in it sees the same files. Use one per user when users must not share files, and one per customer account when a team should share the agent's computer.
  - q: Can one customer's agent see another customer's files?
    a: Each workspace has its own sandbox, and the sandbox is the workspace's trust boundary. Workspaces do not share a disk.
  - q: Can each customer get a different model or system prompt?
    a: Not in this version. The `agent` settings in `roost.yaml` (model, thinking level, system prompt) apply to every workspace, and every workspace uses one fixed sandbox template.
  - q: How many customer agents can I run?
    a: roost has not published scale numbers yet. In practice the limits are your E2B plan's sandbox limits and your model provider's rate limits; idle sandboxes pause, so they do not hold running capacity.
  - q: What happens to a customer's data if their sandbox is lost?
    a: In M1 the workspace is marked `failed`, and its files and transcripts are lost with the sandbox. Continuous backups and restore are on the roadmap (M2).
---

People describe this as an **AI employee** or a **dedicated agent per customer**: each customer gets an agent that knows their history, keeps their files, works on long tasks and is there tomorrow. Building it means running many agents at once, one per customer, and keeping each of them alive and single.

## What an agent per customer needs

- **Its own computer.** Files the agent wrote yesterday are still there today: a repository it cloned, notes, generated reports.
- **Its own memory.** Each thread or ticket has its own transcript, and it survives restarts.
- **Isolation.** One customer's agent cannot read another customer's files.
- **Durability.** A crash or a deploy does not throw away an hour of work.
- **One copy.** A retry or a stale process never leaves two agents working on the same files.
- **Your ids.** You address the agent by your customer id and your thread ids, not by sandbox or session ids you have to store.

## The shape: one workspace per customer

roost has three words. A **workspace** is the agent's computer: one long-lived sandbox with its files and the agent's storage. A **conversation** is a thread inside it, with its own transcript and inbox. A **run** is one piece of work, from a message to the final answer.

| In your product | In roost | How you address it |
|---|---|---|
| A customer (or a user) | Workspace | `PUT /v1/workspaces/{customer-id}` |
| A chat thread, ticket or task | Conversation | a `key` you choose, such as the thread id |
| A message from the customer | Message | an `id` you choose, such as the chat message id |
| The agent working on it | Run | the id of the message that started it |

Workspace names are yours: lowercase letters, digits, `-`, `_` and `.`, up to 128 characters, unique within a tenant.

## Step by step

Create the workspace when the customer signs up, or on their first message. `PUT` is idempotent: it creates the workspace or returns it.

```bash
curl -X PUT localhost:7070/v1/workspaces/acme-corp \
  -H "Authorization: Bearer $ROOST_KEY" -H 'Content-Type: application/json' \
  -d '{"owner": "acme-corp"}'

# the first workspace builds the sandbox template, which takes minutes
curl -s localhost:7070/v1/workspaces/acme-corp -H "Authorization: Bearer $ROOST_KEY" | jq .phase
# "active"
```

Open a conversation for each thread with a `key`. The same `key` always returns the same conversation, so you never store conversation ids:

```bash
curl -X POST localhost:7070/v1/workspaces/acme-corp/conversations \
  -H "Authorization: Bearer $ROOST_KEY" -d '{"key": "ticket-1042"}'
# {"conversation": "c_01J9Z..."}
```

Send the customer's message with an `id`, then follow the answer as it streams:

```bash
curl -X POST localhost:7070/v1/workspaces/acme-corp/conversations/c_01J9Z.../messages \
  -H "Authorization: Bearer $ROOST_KEY" \
  -d '{"id": "msg-88123", "text": "Can you reconcile last month’s invoices?"}'
curl -N -H "Authorization: Bearer $ROOST_KEY" \
  localhost:7070/v1/workspaces/acme-corp/conversations/c_01J9Z.../events
```

The full flow, including configuration, is in the [quickstart](/docs/quickstart/).

## What each customer's agent gets

- **A long-lived sandbox on E2B Cloud.** One per workspace, created once and kept. Its disk holds the agent's files and the agent's storage.
- **Pi Durable as the agent**, with Pi's system prompt, coding tools and skills, and the model you set in `roost.yaml`.
- **Transcripts that belong to the agent.** Each conversation's transcript, inbox and message ids live in Pi's own storage on the sandbox's disk. roost keeps no second copy that could drift.
- **Exactly one agent at a time.** roost issues one execution grant per workspace; requests with an older grant's token are rejected.

## Model keys stay out of the sandbox

The agent's model credentials come through a `SecretProvider`. On E2B Cloud (`models.access: e2b`), E2B's egress proxy adds your provider key to the agent's requests to the provider's host, so the key itself never enters the customer's sandbox. **On the roadmap (M3):** a short-lived key per grant from a LiteLLM gateway, revoked when the grant ends.

## Idle customers cost little

Most customers are idle most of the time. A sandbox with no run going pauses when its timeout runs out (an hour in M1), keeping its files and memory, and the next request wakes it. While a run is going, roost keeps extending the timeout so long tasks are not cut off.

## Limits in this version

roost is **v1alpha1**. Today it runs sandboxes on E2B Cloud only, stores its own state in SQLite, uses one fixed sandbox template for every workspace, and has no backups. **On the roadmap:** continuous backups, restore to any step and forking (M2); self-hosted sandboxes on your own KVM machines with E2B Embed (M3); a CLI, an operator API and Postgres.
