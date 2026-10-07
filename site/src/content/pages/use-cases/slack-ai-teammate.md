---
title: Build an AI teammate for Slack that remembers threads
description: Map each Slack, Feishu or Teams thread to a durable agent conversation. Thread id as the key, message id against duplicates, queue or steer for follow-ups.
h1: Build an AI teammate for Slack (or Feishu, Teams) that remembers every thread
nav: AI teammate for Slack
section: Use cases
order: 2
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  Give your bot one roost workspace per user or team and one conversation per chat thread, using the thread id as the conversation `key`. Send each chat message with the platform's message id as its `id`, so a retried webhook is answered once, and choose `queue` or `steer` for messages that arrive while the agent is still working.
related:
  - guides/ai-agent-duplicate-replies
  - use-cases/ai-employee-per-customer
  - use-cases/long-running-agents
  - docs/quickstart
faq:
  - q: Does roost include a Slack, Feishu or Teams bot?
    a: No. roost is not a chat integration. You write the bot with the platform's SDK, and the bot talks to agents through roost's conversation API.
  - q: What happens when Slack retries an event?
    a: If the bot sends the retried message with the same `id`, roost answers `200 duplicate` and starts nothing. The agent replies once.
  - q: Can different threads share files?
    a: Yes, when they are conversations in the same workspace. Each thread keeps its own transcript, and all conversations in a workspace see the same files.
  - q: How does the agent remember a thread after a restart?
    a: The transcript lives in Pi Durable's storage on the workspace's sandbox disk, and the thread id maps to the same conversation every time. A restarted agent process reads the same storage.
  - q: Can a user stop the agent or start over?
    a: Map a stop command to `POST .../interrupt`, which stops the running run and withdraws queued messages, and a start-over command to `POST .../reset`, which starts a new model context with an optional handoff note.
---

An **AI teammate** in Slack, Feishu or Teams is a bot that people mention in threads and that picks up where it left off: it remembers what was said in the thread, keeps the files it worked on, and does not answer the same message twice. The chat side is your bot. The agent side, one durable agent with its own computer, is what roost runs.

## How chat maps onto roost

| Chat platform | roost | Example |
|---|---|---|
| A user, team or channel you give an agent to | Workspace | `team-t024be7lr` |
| A thread | Conversation, with the thread id as its `key` | `slack:C123:1712.0000` |
| A message in the thread | Message, with the platform's message id as its `id` | `slack:C123:1712.0042` |
| A message that arrives while the agent works | `delivery`: `queue` or `steer` | |

For the thread key, use whatever identifies the thread on the platform: in Slack, the channel and the thread's `ts`; in Feishu, the chat and the thread's root message; in Teams, the conversation id. For the message id, use the platform's own id for that message, so that a retried event carries the same one.

Workspace names must be lowercase letters, digits, `-`, `_` or `.`, so lowercase platform ids before using them in one.

## The bot loop

The pattern is the same on every platform: acknowledge the event quickly, then hand the message to the agent.

```js
// On every chat message event (Slack Events API, Feishu event, Teams activity)
async function onMessage(event) {
  const ws = `team-${event.team.toLowerCase()}`;
  const api = `${ROOST_URL}/v1/workspaces/${ws}`;
  const headers = { Authorization: `Bearer ${ROOST_KEY}`, 'Content-Type': 'application/json' };

  // once per workspace; PUT returns the existing workspace if it is there
  await fetch(api, { method: 'PUT', headers, body: JSON.stringify({ owner: ws }) });

  // the thread's conversation: the same key always returns the same one
  const thread = event.thread_ts ?? event.ts;
  const res = await fetch(`${api}/conversations`, {
    method: 'POST', headers,
    body: JSON.stringify({ key: `slack:${event.channel}:${thread}` }),
  });
  const { conversation } = await res.json();

  // the message: its id makes a retried event harmless (200 duplicate)
  await fetch(`${api}/conversations/${conversation}/messages`, {
    method: 'POST', headers,
    body: JSON.stringify({ id: `slack:${event.channel}:${event.ts}`, text: event.text, delivery: 'queue' }),
  });
}
```

A new workspace is `provisioning` until its sandbox is ready; conversation requests to it get `409 workspace_busy` until it is `active`, so create workspaces ahead of time or retry with the same ids.

## Posting the agent's replies

Follow the conversation's events (`GET .../events`, server-sent events). `entry` events carry new transcript entries with a `cursor`, a `kind` (such as `assistant` or `tool_result`) and the `run` they belong to; `run` events carry a run's status. A run's id is the id of the message that started it, so you know which chat message an answer belongs to. Post the assistant's final text to the thread when the run is `completed`.

If the stream drops, reconnect with `?after=<last cursor>` and nothing committed in between is missed. Remember which cursors you already posted, so a reconnect never posts a reply twice.

## Queue or steer: messages that arrive mid-run

People keep typing while the agent works. Each message says how it should reach the agent:

- **`queue`** (the default) waits and is answered in the next run. Use it for a new, separate request.
- **`steer`** joins the running run after its current tool round. Use it for corrections such as “use the staging database instead”.

Two more endpoints map well to chat commands: **interrupt** stops the running run and withdraws queued messages, and **reset** starts a new model context in the same conversation, with an optional handoff note.

## Why it remembers every thread

Each conversation's transcript lives in Pi Durable's storage on the workspace's sandbox disk, and the sandbox is long-lived. The thread key always leads back to the same conversation, and conversations in one workspace share the same files. If the agent process crashes mid-answer, the run continues from its last step (see [what happens when an agent crashes](/guides/ai-agent-crash-recovery/)).

## What you still build

roost does not talk to Slack, Feishu or Teams. You build the app: OAuth and installation, event handling, choosing which workspace a message belongs to, formatting replies and posting them. roost gives the bot an agent per workspace and the conversation API above.
