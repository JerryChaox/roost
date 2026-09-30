<p align="center">
  <img src="assets/roost-teaser.webp" width="760" alt="Tenants are walled off and every user gets a workspace; conversations share its files while their state lives outside the sandbox, so when the sandbox dies a new one picks up where it left off.">
</p>

<h1 align="center">roost</h1>

<p align="center">
  <b>A durable runtime for any agent SDK.</b><br>
  Agent state lives in your own S3, private and out of the sandbox.<br>
  Build all your enterprise agents on one stack.
</p>

## Why

You put Claude Code or Codex behind a chat: every user gets an agent, every agent gets a sandbox. Then production happens.

- **Sandboxes die.** They time out, get paused, crash. The conversation's files and context go with them.
- **Webhooks retry.** The same message arrives twice, and the agent answers twice.
- **Agents hang.** A turn stalls halfway through a task and nobody notices.
- **You ship upgrades.** Every live conversation has to start over.
- **Users open more threads.** Either each thread gets its own sandbox and can't see the others' files, or two threads edit the same files at once.

roost takes them off your hands. You talk to an agent by its address: send messages to a conversation, read events back. roost decides which sandbox it runs on, when it sleeps, how it comes back, and makes sure only one copy of it is ever running.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" width="100%" alt="A tenant contains workspaces; a workspace holds shared files, several conversations and the sandbox it currently runs on; each conversation has an inbox, a grant and one running turn. Each level lists its guarantees and operator commands.">
</picture>

You only need three words:

- **Workspace**: the agent's computer, a sandbox plus its files. Sandboxes are disposable; the workspace is not.
- **Conversation**: a thread inside a workspace, with its own inbox and context. Several conversations can share one workspace and see the same files.
- **Turn**: one run of the agent over the messages waiting in the inbox.

Each level makes its own promise:

| Level | What roost does | What you can rely on |
|---|---|---|
| Access | Channel webhooks and the conversation API | Each message gets in once. Replies go back to the thread they came from. |
| Workspace | Checkpoints files and agent sessions after every step, and snapshots the whole workspace to object storage after turns; wakes a sandbox on demand, lets it sleep when idle, and swaps it to recover or upgrade | The workspace survives any sandbox: at worst, the work since the last snapshot is redone. Idle agents cost nothing. Upgrades don't restart conversations. |
| Conversation | Deduplicates, queues and batches messages into turns; grants the right to execute | Answered once, even when delivered twice. One turn at a time, one live executor. |
| Turn | Runs the conversation's Claude Code or Codex session through the official SDKs, keeps it warm between turns, and watches it | Streams live. A hung turn recovers on its own. If the process dies, it resumes at the step. If the sandbox comes back, it continues where it stopped; if the sandbox is gone, it resumes from the last snapshot. |
| Infrastructure | Docker, or E2B Firecracker microVMs (E2B Cloud, or E2B Embed on your own machine) for sandboxes; SQLite or Postgres for state; S3, GCS or a local directory for snapshots | Swap a provider and nothing above changes. |

Isolation is by tenant: data, credentials and snapshots never cross a tenant. Inside one workspace, conversations are separated logically but trust each other, the same way two terminal windows on one machine do.

## Getting messages in

**Chat channels.** Point a Feishu, Slack or Telegram webhook at roost. The adapter works out which conversation each message belongs to, and replies stream back into the same thread. You never handle a conversation ID.

**Everything else**, such as a website. Your backend creates conversations for its own users and talks to them over HTTP:

```bash
# start a conversation for one of your users
curl -X POST localhost:7070/v1/conversations -d '{"owner": "user-42"}'
# {"id": "api:01J9Z..."}

# list that user's conversations
curl "localhost:7070/v1/conversations?owner=user-42"

# send a message (the id makes retries safe), then stream the reply
curl -X POST localhost:7070/v1/conversations/api:01J9Z.../messages -d '{"id": "m-1", "text": "hi"}'
curl -N localhost:7070/v1/conversations/api:01J9Z.../events
```

By default each `owner` gets one workspace, so the same user's conversations share files.

## Operating it

The `roost` CLI covers every layer and is built for agents as much as for people: `--json` everywhere, stable schemas, meaningful exit codes, and a `SKILL.md` so Claude Code can run it.

| Level | Look | Act |
|---|---|---|
| Access | `roost channel status` | |
| Workspace | `roost ws inspect` | `roost ws recover` |
| Conversation | `roost conv inspect`, `roost conv timeline`, `roost conv transcript` | `roost conv reset` |
| Turn | `roost turn logs` | `roost turn retry` |
| Everything | `roost status`, `roost doctor` | |

Commands that change state need an operator token and a `--reason`, support `--dry-run`, and are audited. The CLI only talks to the control plane API, and it is not available inside sandboxes.

## Two binaries

- **`roost`** is the CLI and the control plane: access, conversations and workspaces. One process with SQLite by default, Postgres when you need it.
- **`roost-driver`** is a single executable, written in TypeScript, that `roost` copies into every sandbox. It hosts the Claude Agent SDK and the Codex SDK, keeps each conversation's agent session warm between turns, and closes it when idle.

The control plane reaches the driver through the sandbox provider, so `roost` can run on your laptop without a public address.

## What roost is not

- **Not an agent framework.** It runs Claude Code and Codex as they are; you don't rewrite your agent.
- **Not a sandbox provider.** Bring Docker, E2B Cloud, or [E2B Embed](https://github.com/e2b-dev/runtime/tree/main/embed) on your own Linux machine, which needs no E2B account.
- **Not exactly-once.** Answers are deduplicated and old executors are fenced off, but the step that was running at the moment of a crash may run again. Make external side effects idempotent.

## Design

- [RFC 0001: a durable runtime for agent SDKs](docs/rfcs/0001-durable-agent-runtime.md): the model, where state lives, and why.
- [Contracts](docs/specs/contracts.md): the API, invariants, Kit support and conformance scenarios.

## License

Apache License 2.0. See [LICENSE](LICENSE).
