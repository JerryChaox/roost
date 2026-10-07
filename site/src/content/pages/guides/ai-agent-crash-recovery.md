---
title: What happens when an AI agent crashes mid-task | roost
description: When an agent process dies mid-task, what survives, what is redone and what is not undone. Step-level commits, interrupted tools and side effects.
h1: What happens when an AI agent crashes mid-task
nav: Agent crash recovery
section: Guides
order: 4
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  It depends on where the agent keeps its progress. If the loop's state lives only in memory, a crash loses the run and it starts over. With step-level commits, as in Pi Durable under roost, every finished model call and tool call is already stored, so the run resumes from its last step; only the step in flight is redone, and side effects it already caused are not undone.
related:
  - use-cases/long-running-agents
  - guides/ai-agent-duplicate-replies
  - use-cases/ai-employee-per-customer
  - docs/quickstart
faq:
  - q: Does the agent start the task over after a crash?
    a: Not with step-level commits. The run continues from its last committed step. Only the model request in flight is sent again, and a tool that was running comes back as interrupted unless it is safe to replay.
  - q: Is roost exactly-once?
    a: No. Each message is admitted once and one agent runs per workspace, but a tool interrupted by a crash may or may not have finished its work in the outside world, and roost does not undo side effects.
  - q: What if the whole sandbox is lost, not just a process?
    a: In M1 the workspace is marked `failed`, and its files and transcripts go with the sandbox. Continuous backups and restoring a workspace to any step are on the roadmap (M2).
  - q: Does my client need to do anything after a crash?
    a: The event stream breaks when the agent process restarts. Reconnect to `GET .../events?after=<last cursor>` and you receive every entry committed since, including the end of the run.
---

An agent crash is rarely dramatic. A process is killed for using too much memory, a container is replaced by a deploy, a dependency segfaults in the middle of a tool call. What matters is what the agent finds when it starts again.

## Three ways an agent can hold its progress

1. **In memory.** The loop holds the conversation and its position in variables. A crash loses the run; at best the user's message is still in a queue and the work **starts over** from the beginning.
2. **In a saved session.** The transcript is written to a file or database after each turn. The conversation survives, but the execution does not: a run interrupted in the middle of a step has no record of what it was doing, and someone has to prompt it again.
3. **Step-level commits.** Every model response and every tool result is committed before it is shown or acted on, together with the run's position. A restarted process reads the run's last committed step and continues. This is durable execution, and it is what [Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable) does.

roost runs Pi Durable in each workspace's sandbox, so its agents are in the third group.

## What roost does when a process dies

| Failure | What happens | Work redone |
|---|---|---|
| The agent host process dies, for example mid tool call | The driver in the sandbox restarts it under the same execution grant; Pi resumes the interrupted run | The model request in flight; a running tool returns `interrupted` unless it is safe to replay |
| The driver is lost | roost issues a new grant on the same sandbox and starts a new driver; the old driver's token is rejected; conversations and transcripts are kept | The same |
| The sandbox is lost | M1 marks the workspace `failed` | Everything in it. **On the roadmap (M2):** restore from continuous backups |

The first two rows are tested on E2B Cloud by `scripts/e2e-m1.sh --failures`, which kills the driver and then kills the agent host while a slow `bash` tool is running; the run still completes.

## Interrupted tools

When a process dies while a tool is running, nobody knows whether the tool finished. A shell command may have written half its output; an HTTP call may have reached the server. Pi Durable does not guess. The tool call comes back to the model as **interrupted**, unless the tool is declared safe to replay, in which case it runs again. The model then decides what to do: check the state and run the command again, or tell the user.

A simplified transcript of such a run, where the model chose to run the command again:

```text
assistant     tool call: bash "sleep 20 && echo done"
              (agent host killed; the driver restarts it)
tool_result   interrupted
assistant     tool call: bash "sleep 20 && echo done"
tool_result   done
assistant     "The command printed: done"
run m-3       completed
```

## Side effects are not undone

Durable execution protects the agent's own progress, not the outside world. If a tool sent an email, charged a card, pushed a commit or ran half a migration before the crash, that happened, and nothing rolls it back. roost states this plainly: it is **not exactly-once**.

Design tools for it:

- Pass an idempotency key to external APIs that accept one, derived from the run and the step, so a repeated call is recognised.
- Check before acting: look for the commit, the sent message or the created record before creating it again.
- Keep tools that cannot be repeated out of the replay-safe set, so the model sees `interrupted` and decides.

## What your client sees

The event stream (`GET .../events`, server-sent events) breaks when the agent host restarts. Reconnect with `?after=<last cursor>`: entries committed since that cursor are sent first, then the stream continues live, including the run's final status. A message you resend with the same `id` is not admitted twice (see [why agents reply twice](/guides/ai-agent-duplicate-replies/)).
