# RFC 0001: roost, a durable runtime for agent SDKs

| | |
|---|---|
| Status | Draft |
| Created | 2026-09-30 |
| Contracts | [specs/contracts.md](../specs/contracts.md) |

## Summary

roost runs agent SDKs such as Claude Code and Codex for many users at once, on sandboxes that can be killed, paused, replaced or upgraded at any time. It keeps everything that matters about an agent outside the sandbox: conversations and turns in roost's own database, and the agent's files and sessions in object storage the operator owns. A sandbox is only ever a place to run the next step.

This RFC describes the model, the guarantees each layer makes, where state lives, and the decisions behind them. The normative interfaces are in the contracts spec.

## Motivation

Teams that put agent SDKs behind a chat or a product hit the same failures:

- **Sandboxes die.** They time out, get paused or crash, and the conversation's files and context go with them.
- **Deliveries repeat.** Webhooks retry, and the agent answers the same message twice.
- **Agents hang.** A turn stalls halfway through a task and nobody notices.
- **Runtimes change.** Shipping a new image forces every live conversation to start over.
- **Users open more threads.** Either each thread gets its own sandbox and can't see the others' files, or two threads edit the same files at once.

Enterprises add three requirements on top: tenants must be isolated, the data must stay in storage they control, and the whole thing must be operable, by people and by agents.

Sandbox providers solve "give me a machine". Agent frameworks solve "write an agent". Nothing in between owns the agent's life across machines. roost is that layer.

## Goals

1. Address an agent, not a sandbox: send messages to a conversation, read events back.
2. Survive sandbox loss, redoing at most the work since the last snapshot.
3. Answer every message once, even when it is delivered more than once.
4. Isolate tenants completely; keep agent data in the operator's own storage.
5. Run any agent SDK that can be driven from a command line, starting with Claude Code and Codex.
6. Be operable end to end from a CLI that an agent can use.

## Non-goals

- **Writing agents.** roost runs agent SDKs as they are; it is not an agent framework.
- **Providing sandboxes.** roost drives Docker and E2B; it does not implement a hypervisor.
- **Exactly-once side effects.** Answers are deduplicated and stale executors are fenced off, but a step that was running when a machine died may run again.
- **Workflow orchestration.** roost has conversations and turns, not DAGs.
- **Rich rendering.** Channel adapters stream plain text; product-specific cards stay in the product.

## Model

```text
tenant                      isolation boundary: nothing crosses it
└─ Workspace                the agent's computer: files, sessions, one sandbox at a time
   ├─ Conversation          a thread with its own inbox, session and reply route
   │  └─ Turn               one run of the agent over the messages waiting in the inbox
   └─ Conversation
      └─ Turn
```

- A **tenant** is the isolation boundary. Data, credentials and snapshots never cross it.
- A **Workspace** is the trust boundary. Conversations in one workspace share its files and trust each other, like two terminal windows on one machine. A workspace is bound to at most one sandbox at a time, and that sandbox is replaceable.
- A **Conversation** has an inbox, its own agent session and a reply route. It runs one turn at a time.
- A **Turn** is one run of the agent. It is identified by a stable id and may be retried.
- A **grant** decides which executor may act for a conversation. It is internal: users never see it, but every write back to roost carries it.

Only Workspace, Conversation and Turn appear in the public vocabulary.

## Layers and their guarantees

| Layer | What roost does | What it guarantees |
|---|---|---|
| Access | Channel adapters (Feishu, Slack, Telegram) and the conversation API | Each message gets in once. Replies return to the thread they came from. |
| Workspace | Binds a workspace to a sandbox; wakes it on demand, lets it sleep, replaces it to recover or upgrade | The workspace survives any sandbox; at worst the work since the last snapshot is redone. Idle agents cost nothing. Upgrades don't restart conversations. |
| Conversation | Deduplicates, queues and batches messages into turns; issues and revokes grants | Answered once, even when delivered twice. One turn at a time. One live executor. |
| Turn | Runs one agent process per turn and watches it | Streams live. A hung turn recovers on its own. Process dies: resume at the step. Machine dies: resume from the last snapshot. |
| Infrastructure | Sandboxes (Docker, E2B Cloud, E2B Embed), state (SQLite, Postgres), snapshots (S3, GCS, local directory) | Swap a provider and nothing above changes. |

Operations run across every layer through the `roost` CLI.

## State

Every fact has one owner and one place.

| State | Where | Granularity |
|---|---|---|
| Conversations, inboxes, turns, grants, workspace bindings, events, audit | roost database (SQLite or Postgres) | Transactional |
| Workspace files and agent sessions | A shadow git repository inside the sandbox | Every step |
| The whole workspace, including the shadow repository | kopia snapshot to object storage, one repository per workspace | When the workspace goes idle after a turn, and at least every `max_interval` |
| Files larger than a threshold (default 10 MiB) | Excluded from the shadow repository; kopia only | With every snapshot |
| Processes and memory | The sandbox; pause and resume only make it faster | Disposable |

Rules:

- **Sandbox state is not copied.** Whether a sandbox is running, paused or gone is the provider's fact; roost asks the provider, listing by labels in one call when it needs many.
- **Provider volumes are not used.** See [Alternatives](#alternatives-considered).
- **A snapshot covers the whole workspace at one instant.** Conversations share the workspace's files, so their sessions and those files must come from the same moment; snapshotting one conversation's part would restore sessions and files that disagree.
- **Snapshots are taken when nothing is running.** The driver takes one as soon as the workspace goes idle after a turn. If the workspace has been busy for longer than `snapshot.max_interval` (default 10 minutes), the driver freezes every running agent process (SIGSTOP on its process group), takes the snapshot, and resumes them (SIGCONT). Snapshots are incremental, so the freeze usually lasts seconds.
- **The driver is the only writer of the shadow repository.** Step commits from all conversations are serialized; each is a checkpoint of the whole workspace, labelled with the conversation and step that caused it.
- **Rebuildable directories are excluded** from both layers (for example `node_modules`, build caches).
- **Credentials that roost injects are never persisted.** They live on tmpfs or are injected by an egress proxy.
- **One kopia repository per workspace.** Its key is present inside the sandbox; sharing a repository across workspaces would break the workspace trust boundary.

Recovery:

| Failure | Recovery |
|---|---|
| Agent process dies or hangs | Kill it; start a new process that resumes the SDK session at the last completed step |
| Sandbox or machine is lost | Start a new sandbox and restore the last snapshot; every conversation resumes from its session in that snapshot, and a step that was running at the time may repeat |
| Upgrade | At a turn boundary, start a sandbox from the new template and restore the latest snapshot |

## Sandboxes

roost defines a sandbox interface modelled on the E2B API: lifecycle (create, connect with implicit resume, pause, kill, timeout), command execution with streaming output, file transfer, an endpoint for a port inside the sandbox, templates, and listing by labels.

Backends:

- **Docker** for local development.
- **E2B Cloud** for hosted Firecracker microVMs.
- **E2B Embed** for self-hosting the same API on one Linux machine with no E2B account. Embed is new and single-node; roost treats it as a preview backend.

roost does not drive Firecracker directly. E2B publishes no Go SDK, so roost generates its clients from the OpenAPI spec and envd protobufs in `e2b-dev/runtime`.

### Kits

What a sandbox contains and what it is allowed to do is declared with the [Docker Sandbox Kit Specification](https://github.com/docker/sandbox-kit-spec) (v3): an image plus typed capabilities for network egress, credentials, persistent paths, lifecycle hooks, ports, resources and agent sessions. roost resolves and assembles a workspace's Kits, builds a provider template from the result, and caches it by the digest of the resolved set.

Each backend publishes a capability support matrix. A Kit that needs a capability the backend cannot enforce is rejected before any sandbox is created. A Kit update that widens permissions waits for operator approval.

roost's own configuration (`roost.yaml`) covers only what Kits cannot: how workspaces are assigned, when sandboxes sleep, how busy conversations treat new messages, watchdog thresholds, retries and tenant quotas.

## Access

- **Channel adapters** receive webhooks directly. The adapter derives the conversation id from the channel's natural keys, records the reply route when the conversation is created, and streams replies back as plain text by editing one message.
- **The conversation API** serves channel-less applications such as websites. The application creates and lists conversations by its own user id (`owner`), sends messages with an idempotency key, and reads events over SSE.

## Turns and harnesses

- Each turn runs one agent process: `claude -p --resume <session> --output-format stream-json` or `codex exec resume <session> --json`. The process exits when the turn ends, so idle conversations hold no memory.
- Output is streamed line by line from the process and forwarded as events.
- A conversation that receives messages while busy queues and batches them by default; it can be configured to interrupt the running turn, or (Claude Code) to inject into it over stdin.
- The watchdog uses two clocks: liveness (any activity) and progress (any renderable output). Escalation is persisted: restart the process, then replace the sandbox, then give up and mark the turn for attention. A wall-clock ceiling marks the turn for attention without killing anything.

## Processes

- **`roost`** is the CLI and the control plane. One process, SQLite by default, Postgres for more.
- **`roost-driver`** is a small static binary embedded in `roost` and copied into every sandbox. It supervises agent processes, keeps the shadow repository and takes snapshots. Its hash and the protocol version form the runtime fingerprint; control-plane releases do not force sandbox replacement.
- The control plane reaches the driver through the provider's endpoint, so `roost` needs no public address.
- The driver runs as a different user from the agent, and its control interface is not reachable by the agent.

## Operations

The `roost` CLI uses `roost <object> <verb>` and covers every layer: `status`, `doctor`, `channel status`, `ws inspect|recover`, `conv inspect|timeline|transcript|reset`, `turn logs|retry`. It is a thin client of the control-plane API, prints `--json`, ships a `SKILL.md` for agents, requires an operator token and `--reason` for anything that changes state, supports `--dry-run`, audits every change, and is not available inside sandboxes.

## Security

- **Isolation:** tenant is the hard boundary; workspace is the trust boundary.
- **Fencing:** every write back to roost carries the conversation's grant; writes from a replaced executor are rejected. Side effects an agent causes directly in the outside world are not fenced; the documented guarantee is that the running step may repeat.
- **Credentials:** on E2B Cloud, Kit credentials are injected by the provider's egress proxy and never enter the sandbox. E2B Embed has no TLS interception, so proxy-managed credentials are rejected there until roost provides its own egress proxy.

## Alternatives considered

| Alternative | Decision | Why |
|---|---|---|
| Tar the whole workspace to object storage | Rejected | Grows without bound, captures rebuildable junk, is not consistent mid-turn, and captured credentials |
| Provider volumes | Rejected | E2B volumes are a private beta on NFS; E2B advises against git, package installs and SQLite on them; they are disabled in Embed |
| rclone for snapshots | Superseded | Mirrors only the latest state and is not atomic; kopia snapshots are atomic and versioned |
| restic for snapshots | Not chosen | Comparable model, but no public Go packages and heavier repository maintenance |
| Drive Firecracker directly | Deferred | E2B Embed already provides self-hosted Firecracker with the same API |
| Keep a copy of sandbox state | Rejected | It is the provider's fact; a copy drifts |
| Python control plane and driver | Replaced | A static Go driver needs nothing installed in the sandbox image, and one Go install covers the CLI and control plane |

## Roadmap

| Milestone | Scope |
|---|---|
| M0 · Contracts | Layer interfaces, the driver protocol, the Kit mapping, conformance scenarios |
| M1 · Local | `roost up` on Docker with Claude Code, the Telegram adapter, the CLI |
| M2 · Resilience | Watchdog, step checkpoints and turn snapshots, upgrades without restarts |
| M3 · Launch | E2B Cloud and E2B Embed, Codex, Slack and Feishu adapters |

Later: rewinding a workspace to an earlier step or turn, roost's own egress proxy, more agent SDKs, multi-node control planes.

## Open questions

1. Is the public vocabulary limited to Workspace, Conversation and Turn?
2. Is the workspace the trust boundary, or does each conversation need its own OS user?
3. How is recovery stated publicly: "resume at the step, or from the last snapshot"?
4. When is the public announcement: after M3, or earlier?

To verify before M0 is closed:

- E2B template builds from Kit images: the `agent` user (uid 1000) against E2B's default `user`, and private GHCR images.
- E2B Embed end to end on a KVM host.
- STS credentials scoped to a workspace prefix on RustFS, for local tests.
- `claude --resume` after the process is killed mid-tool; `codex exec resume` behaviour and stream granularity.
- kopia per-turn cost on a realistic workspace over 50 turns.
