---
title: Why your AI agent replies twice, and how to stop it
description: Duplicate agent replies come from webhook retries, client retries and two agent copies. Fix them with idempotent message ids and one live agent.
h1: Why your AI agent replies twice, and how to stop it
nav: Duplicate replies
section: Guides
order: 5
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  An agent replies twice when the same message reaches it twice (a webhook retry, a client retry after a timeout, two workers handling one event) or when two copies of the agent run at once. The fix is to give every message a stable id that the agent records with its transcript and admits only once, and to allow only one live agent per workspace. roost does both.
related:
  - use-cases/slack-ai-teammate
  - guides/ai-agent-crash-recovery
  - use-cases/ai-employee-per-customer
  - docs/quickstart
faq:
  - q: What should I use as the message id?
    a: The id the source system already gives the message, such as the chat platform's message id or your own database id for the request. It must be the same every time the same message is sent, and unique within the conversation.
  - q: What does roost return for a duplicate message?
    a: The first send returns `202` with the status `queued` or `running`. Any later send with the same `id` to the same conversation returns `200` with the status `duplicate` and starts nothing.
  - q: Do I still need to deduplicate on my side?
    a: Only for what you post. If your bot reconnects to the event stream, remember the cursors it already posted so it never posts the same answer twice.
  - q: Does this make tool calls exactly-once?
    a: No. It stops a message from being answered twice and two agents from running at once. A tool interrupted by a crash comes back as interrupted unless it is safe to replay, and side effects are not undone.
---

**“My agent replied twice”** is one of the most common complaints about chat agents in production. It is almost never the model. The same input reached the agent twice, or two agents were running.

## The usual causes

1. **Webhook retries.** Chat platforms resend events they do not see acknowledged in time; Slack, for example, expects a response within three seconds. A bot that calls the model before answering the webhook gets the same event again.
2. **Your own retries.** A request to the agent times out, your code sends it again, and the first one had already arrived.
3. **Two consumers.** A queue delivers at least once, or two instances of your bot both receive the event.
4. **Two copies of the agent.** A process thought dead is still running, or a failover started a second one. Both answer.
5. **Replay after a crash.** A loop that keeps its state in memory restarts and runs the last turn again, sending its reply a second time.

## Fix 1: an idempotency key for every message

Give every message an `id` that is stable across retries: the platform's message id, or your own request id. The agent records it, and a message with an id it has already seen is not admitted again.

In roost, the `id` is part of the send:

```bash
curl -X POST localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../messages \
  -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "slack:C123:1712.0042", "text": "Summarise the thread"}'
# 202 {"status": "running"}

# the webhook is retried: same id
curl -X POST localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../messages \
  -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "slack:C123:1712.0042", "text": "Summarise the thread"}'
# 200 {"status": "duplicate"}
```

The record of admitted ids lives in the agent's own storage on the sandbox's disk, next to the transcript, so it is not a separate cache that can expire or disagree with the conversation. Ids are unique per conversation.

Acknowledge the webhook first, then forward the message. Even if the platform retries anyway, the second send is a `200 duplicate`.

## Fix 2: one live agent per workspace

Deduplicating messages does not help if two agents are running. roost gives each workspace one **execution grant** at a time: issuing a new grant ends the previous one in the same database transaction, and the driver in the sandbox rejects every request carrying an older token. Inside the sandbox, the driver locks the agent's storage and supervises exactly one agent process. A stale process cannot answer, because its requests no longer get through.

## Fix 3: resume instead of replaying

An agent that crashed should continue its run, not run it again. Pi Durable commits every step before it is shown, so after a restart the run continues from its last committed step rather than repeating the turn it was in (see [what happens when an agent crashes](/guides/ai-agent-crash-recovery/)).

## Fix 4: post each answer once

The last duplicate can come from your own side. If your bot follows `GET .../events` and reconnects after a drop, reconnect with `?after=<last cursor>` and keep track of which entries you already posted. A run's id is the id of the message that started it, so each answer maps back to exactly one chat message.

## A short checklist

- Every message sent to the agent carries a stable `id`.
- The webhook is acknowledged before the agent is called.
- Only one agent per workspace can run, enforced by the runtime, not by convention.
- A crashed run resumes rather than restarts.
- The bot posts each entry once, by cursor.
