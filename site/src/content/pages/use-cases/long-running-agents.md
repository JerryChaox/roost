---
title: How to run AI agents for hours without losing progress
description: Long agent runs die to crashes, timeouts and restarts. Commit every step, give each agent a long-lived sandbox, and resume where it stopped.
h1: How to run AI agents for hours without losing progress
nav: Long-running agents
section: Use cases
order: 3
published: '2026-10-07'
updated: '2026-10-07'
answer: >-
  Run the agent somewhere that outlives your web process, commit every step it takes, and make sure only one copy of it runs. In roost each agent runs Pi Durable inside its own long-lived E2B sandbox: every model call and tool call is committed before it is shown, so when the agent process dies the run continues from its last step instead of starting over.
related:
  - guides/ai-agent-crash-recovery
  - use-cases/ai-employee-per-customer
  - guides/ai-agent-duplicate-replies
  - compare/running-agents-on-a-mac-mini
faq:
  - q: How long can a run last in roost?
    a: roost M1 sets no limit on a run. While a run is going, roost keeps extending its sandbox's timeout; your E2B plan's sandbox limits and your model budget still apply.
  - q: What happens if my backend restarts during a run?
    a: Nothing happens to the run. It executes inside the workspace's sandbox, not in your backend. Reconnect to the event stream with `?after=<last cursor>` and you receive everything committed in the meantime.
  - q: Is work redone after a crash?
    a: Only the step in flight. The model request that was running is sent again, and a tool that was running comes back as `interrupted` unless it is safe to replay. Finished steps are not redone.
  - q: Can I change the agent's direction without restarting it?
    a: >-
      Yes. Send a message with `"delivery": "steer"` and it joins the running run after the current tool round. `interrupt` stops the run if you need to.
---

Agents that work for hours (migrating a codebase, researching a market, reconciling a month of records) fail in ways a chat reply never does. The longer the run, the more likely something underneath it restarts. **Losing progress** means one of those restarts sent the agent back to the beginning.

## What kills a long agent run

- **The agent process dies**: an out-of-memory kill, a bug, a crashed dependency.
- **The machine or container restarts**: a deploy, an autoscaler, a host failure.
- **The sandbox times out** while the agent is in the middle of a task.
- **The connection drops** between your backend and the agent, and your backend thinks the run failed.
- **A retry starts a second copy**, and two agents now work on the same files.

If the loop keeps its progress in memory, every one of these starts the work over.

## Commit every step

[Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable), the agent loop roost runs, commits every model response and every tool result to its storage before it shows them. A run is a sequence of committed steps, so a restarted process reads where it was and continues.

Only the step in flight is redone: the model request that was running is sent again, and a tool that was running comes back as `interrupted` unless it is safe to replay. [What happens when an AI agent crashes mid-task](/guides/ai-agent-crash-recovery/) covers this in detail.

## Keep the run out of your web process

In roost the run lives in the workspace's sandbox, next to the agent's files. Your backend only sends messages and listens. Deploying or restarting your backend does not touch a running agent: reconnect to `GET .../events?after=<last cursor>`, or read `GET .../entries` after a cursor, and you get everything committed while you were away.

## Keep the sandbox up while it works

Each workspace has one long-lived sandbox. While a run is going, roost keeps extending the sandbox's timeout, so a long task is not cut off. With no run going, the sandbox pauses when its timeout runs out (an hour in M1), keeping its files and memory, and the next request wakes it.

## Restart the process, not the run

When the agent host process dies in the middle of a tool call, roost's driver in the sandbox restarts it under the same execution grant, and Pi resumes the run. When the driver itself is lost, roost issues a new grant on the same sandbox and starts a new driver; conversations and transcripts are kept. Both cases are exercised by `scripts/e2e-m1.sh --failures` on E2B Cloud.

## Make sure only one copy runs

A long run is exactly when a retry or a stale process is most likely to start a second agent. roost gives each workspace one execution grant at a time. Issuing a new grant ends the old one in the same transaction, and the sandbox's driver rejects requests carrying an older token. Inside the sandbox, the driver locks the agent's storage and supervises exactly one agent process.

## Redirect instead of restarting

You rarely need to kill a long run to change it. A message sent with `"delivery": "steer"` joins the running run after its current tool round; a `queue` message waits for the next run. `interrupt` stops the run and withdraws queued messages.

## What M1 does not cover yet

- **If the sandbox itself is lost**, the workspace is marked `failed` and there is no backup to restore from. **On the roadmap (M2):** continuous backups with kopia and restore to any step.
- Detecting a stalled run and flagging runs that go on too long are specified in the contracts but not built in M1.
