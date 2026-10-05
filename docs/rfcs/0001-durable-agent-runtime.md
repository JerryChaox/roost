# RFC 0001: roost, a durable agent runtime built on Pi Durable

| | |
|---|---|
| Status | Draft |
| Created | 2026-09-30 |
| Contracts | [specs/contracts.md](../specs/contracts.md) · [specs/driver-protocol.md](../specs/driver-protocol.md) |

## Summary

roost runs durable agents for many users at once. The agent loop is [Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable): it calls the model, runs tools, and commits every step before showing it. roost is the agent layer on top of a sandbox platform: it gives each workspace one long-lived sandbox on E2B and runs Pi inside it. Around it, roost guarantees that exactly one agent loop owns a workspace at any moment, backs the workspace up continuously so it can be restored or branched from any step, and makes the whole system operable.

This RFC explains the principles, the model, where state lives, and why. The normative interfaces are in the contracts and the sandbox protocols.

## Motivation

Teams that run agents for their users hit the same failures:

- **Sandboxes die.** They time out, get paused or crash, and the agent's files and context go with them.
- **Processes die mid-task.** The work is lost, or redone from the start.
- **Two copies run at once.** A retried request, a stale instance or a restored sandbox ends up with two agents writing the same state.
- **Environments can't be reproduced.** Debugging a run, or branching it many times for evaluation or reinforcement learning, needs the exact workspace at an exact step.

Enterprises add three requirements: tenants must be isolated, the data must stay in storage they control, and the whole system must be operable, by people and by agents.

## Principles

1. **Conversation facts belong to the agent.** Transcripts, inboxes, message idempotency and run state are written by the agent loop and live in its storage. roost keeps no copy and adds no conversation layer; it routes requests to the agent and maps them onto the agent's conversation interface.
2. **roost owns execution.** The one fact roost decides is which process may run a workspace. Every workspace has at most one live execution grant, and every request goes to the grant's holder.
3. **The sandbox is the trust boundary.** The agent loop and the code it runs share a user, as in any coding agent. What must stay out of that code's reach, roost keeps outside the sandbox or in the root-owned driver. Confining the agent's tools inside the sandbox is planned ([last section](#later-confinement-and-environment-capture)).
4. **Capture is cheap inside, heavy work is outside.** The sandbox belongs to the agent; roost's own work in it is limited to what only the sandbox can do.

## Where roost sits

roost is one layer, not a sandbox platform. The split follows DeepSeek's DSec, whose cluster services, node runtime and sandbox backends correspond to E2B, and whose per-rollout worker and agent harness correspond to roost.

| Layer | Owns | Here |
|---|---|---|
| Cluster services | Model access with virtual keys, object storage, databases | An LLM gateway such as LiteLLM; S3 or GCS; SQLite or Postgres |
| **roost**, the agent layer | Tenants and routing, execution grants, the agent in each sandbox, continuous backups, operator tooling | `roost-core` and the `roost serve` and `roost backup` built on it; `roost-driver` and `roost-agent-pi` in each sandbox |
| Sandbox platform | Placement, nodes, images, microVMs, egress allow lists | E2B Cloud or E2B Embed |

roost never schedules sandboxes, builds a hypervisor or proxies model traffic. It uses the layer below through its API and the services beside it through theirs.

## Goals

1. Address an agent, not a sandbox: send messages to a conversation, read its transcript and events back.
2. Keep exactly one agent loop per workspace, across restarts, reboots, restores and control-plane failover.
3. Survive process crashes, stuck sandboxes and host restarts without losing finished steps.
4. Back every workspace up as it runs, and restore or branch it from any step, on any deployment.
5. Isolate tenants completely.
6. Be operable end to end from a CLI that an agent can use.

## Non-goals

- **Writing an agent loop.** Pi Durable is the agent loop.
- **Running other agent CLIs** such as Claude Code or Codex.
- **Chat integrations.** Slack, Feishu or Telegram bots are built on the conversation API.
- **Providing sandboxes.** roost drives E2B.
- **Exactly-once side effects.** A tool that was running when a process died is not run again unless it is declared safe to replay; side effects it caused are not undone.

## Model

```text
tenant                      isolation boundary: nothing crosses it
└─ Workspace                the agent's computer: one long-lived sandbox, its files and the agent's storage
   ├─ Conversation          a Pi conversation: transcript, inbox, agent settings
   │  └─ Run                one run of the agent, from an input to its final answer
   └─ Conversation
```

- A **tenant** is the isolation boundary.
- A **Workspace** is the unit of execution and of backup. It has one sandbox at a time, one agent loop and one execution grant. Its name is chosen by the application, often one per end user.
- A **Conversation** is the agent's own object. It is addressed as `(workspace, conversation)`; a caller may give it a `key`, such as a chat thread id, unique within the workspace.
- A **Run** answers an input. It is made of turns, each one model response and its tool calls.

## Layers and their guarantees

| Layer | What roost does | What it guarantees |
|---|---|---|
| API | Authenticates and routes conversation, workspace and operator requests | Every request reaches the workspace's one agent loop. Sleeping workspaces stay readable. |
| Execution | Issues execution grants, starts and supervises drivers, rotates and revokes tokens | One agent loop per workspace, whatever fails. |
| Agent | Runs Pi Durable in the sandbox | Every step is committed before it is shown; a crashed run continues from its last step; a message is admitted once. |
| Backup | Captures changes in the sandbox, stores them outside as content-addressed snapshots | Restore or branch any workspace from any step, on any deployment. |
| Infrastructure | E2B Cloud or E2B Embed; SQLite or Postgres; a persistent disk or object storage for the repository | The same API on both. |

## State

| State | Owner and place |
|---|---|
| Transcripts, inboxes, message idempotency, run checkpoints | The agent: Pi's SQLite storage in `/var/lib/roost/agent` on the sandbox's disk |
| Workspace files | The sandbox's disk, under the persistent paths the Kit declares |
| Workspaces and their phases, execution grants, audit | roost's database |
| Snapshots | The tenant's kopia repository |
| Sandboxes, running or paused; templates | E2B |
| Tenants, `SecretProvider` settings and repository passwords (secret references), Kits, policies | `roost.yaml` |

Snapshots and the read-only projection are copies of past states. A restore starts a new sandbox from one; the live state always has one owner.

roost's database holds only what roost decides, in three tables:

- **`workspaces`**: what each workspace is (name, owner, runtime configuration, `forked_from`) and where roost has taken it, its phase: `provisioning`, `active`, `restoring` or `failed`. A restore is a phase of the workspace, not a separate record; a fork is new workspaces `provisioning` from a snapshot.
- **`grants`**: one row per execution grant, live or ended, with the reason it ended and whether its model credentials are revoked.
- **`audit`**: who asked for what, and how each restore ended.

Two things share a table only if they are one-to-one at every moment, change in the same transitions and have the same writer. No table records work to be done: roost reconciles these rows with what E2B and the drivers report, and repeats an idempotent step until they agree. A crash only means the step runs again.

## Execution grant

A workspace has at most one live grant: `{ workspace, sandbox, start, driver token, backup token, model credentials }`.

- **Issued** as a row of the `grants` table whenever the control plane starts a driver: on creation, reboot, restore, fork, upgrade, or when the driver stops answering. Issuing ends the previous grant in the same transaction, and a partial unique index allows one live grant per workspace.
- **Checked** by the driver for every request (the driver token from the control plane, the backup token from `roost backup`, which reaches only backup, restore and state) Older tokens are rejected, and the model credentials of every ended grant are revoked; a revocation is retried until it is confirmed.
- **Rotated** in place when a new control-plane process takes over: the driver token changes, so requests from the earlier process lose effect without a restart.
- **Ended** before a restore replaces the sandbox: its model credentials are revoked, so an agent loop left in the old sandbox cannot call a model.

Inside the sandbox, the driver locks the agent's storage and supervises exactly one agent host, so the grant maps to exactly one process. The path of every request is tenant → workspace → grant → driver → agent host → conversation. Requests reach a workspace only while it is `active`.

## Inside the sandbox

Two processes:

- **`roost-driver`** (Go, root) is roost's part. It gates requests with the grant's tokens and forwards conversation requests to the agent host without parsing them. It also supervises the agent host, locks agent storage, carries the grant and captures backup data. It does not understand conversations.
- **The agent host** (`roost-agent-pi`, TypeScript, the agent's user) is Pi Durable with Pi's system prompt, coding tools, skills and `AGENTS.md`, modelled on Pi's own experimental durable coding agent. It serves the conversation interface over a Unix socket and tells the driver when a step finishes.

The driver and the host speak JSON-RPC over stdio for control (`initialize`, `quiesce`, `resume`, `state`, `shutdown`, `snapshot`). Model credentials reach the host there, never through files or environments. Any agent loop that implements the conversation interface and the control channel, and keeps its storage in SQLite in WAL mode, can replace Pi; the driver does not change. Pi Durable extensions extend the agent; extensions written for the `pi` CLI must be ported.

## Recovery

| Failure | Recovery | Work redone |
|---|---|---|
| Agent host crashes | The driver restarts it; Pi resumes interrupted runs | The model request in flight; a running tool returns `interrupted` unless it is safe to replay |
| Driver crashes or hangs | A new driver under a new grant | The same |
| Sandbox stuck or unreachable | Reboot it from its own disk, then a new driver under a new grant | The same, plus anything held only in memory |
| Host or disk lost | Restore the workspace from its latest snapshot, on any machine | Changes not yet pulled by `roost backup`, at most a few seconds |
| A change must be undone | `ws restore` to an earlier snapshot | Everything since that snapshot |
| Control plane fails over | The new process takes the lock and rotates every grant | Nothing |

## Backup and snapshots

A snapshot is the template, the runtime configuration, the workspace's file tree and the agent storage's position. The root filesystem is not backed up; the template rebuilds it, so changes meant to last belong in the Kit or under a persistent path.

- **Capture in the sandbox**, at low priority: the driver follows the agent storage's WAL continuously, and tracks changed files in the persistent paths with one fanotify mark on the filesystem. Events only say where to look; a metadata scan, after any overflow and on a timer, decides what changed.
- **Heavy work outside, in kopia**: `roost backup` pulls the change stream from the driver and hands each workspace to [kopia](https://kopia.io), used as a library, as a filesystem built from that stream. kopia skips what did not change, reads changed files through the driver, splits them into content-defined chunks, compresses and encrypts them, and stores only new chunks. Each tenant has its own kopia repository and password, on the persistent disk, S3 or GCS, and kopia alone can restore its files.
- **Snapshots** are taken after every step of a run, at the end of every run, on a timer and on request; each is one kopia snapshot. A snapshot is consistent for the conversation whose step produced it; across conversations running at the same moment it holds what a power loss would leave; a quiesced snapshot is consistent for all.
- **Restoring into a sandbox**: a new sandbox's driver waits for the snapshot, and `roost backup` pushes the files and the agent storage into it. Nothing in a sandbox reads the repository; its password never enters one.
- **Sleep only when backed up.** A workspace sleeps only after its backup has caught up, so its latest snapshot is complete and its transcripts can be read from the projection without waking it.
- **Uses**: point-in-time restore; forking a snapshot into many independent workspaces, for experiments or reinforcement-learning rollouts, with chunks shared rather than copied; the read-only projection; disaster recovery.

## Model credentials

roost does not proxy model traffic; model access is a cluster service. roost reaches it through one interface, the **`SecretProvider`**:

- **Issue**: when a grant is issued, roost asks the `SecretProvider` for the agent's model endpoints and credential, and hands them to the agent host over the control channel.
- **Revoke**: when the grant ends (a new grant, a restore, `ws revoke`), roost asks the `SecretProvider` to revoke them, and retries until it confirms. This is fencing: an agent left in an old sandbox cannot call a model.
- **What stands behind it** belongs to the deployment: an LLM gateway that issues a short-lived key per grant, such as LiteLLM, or E2B Cloud's egress proxy, which injects the key into the agent's requests.
- **Reaching the gateway on Embed**: E2B blocks private addresses from sandboxes unless the orchestrator node exempts them. The gateway therefore has a fixed address that every sandbox may reach and that Embed exempts as a `/32`; roost's manifests deploy LiteLLM this way and `roost doctor` checks it.

## Sandboxes

roost drives E2B through an interface modelled on its API: create from a template with a runtime configuration and labels, connect with implicit resume, pause, reboot from disk, kill, execute as a user, reach a port, update egress rules, build and find templates by alias, list by labels. **E2B Cloud** hosts Firecracker microVMs; **E2B Embed** self-hosts the same API on Linux machines with KVM and no E2B account, as a preview backend. E2B publishes no Go SDK, so roost generates its clients from the OpenAPI spec and envd protobufs in `e2b-dev/runtime`.

What a sandbox contains and may do is declared with the [Docker Sandbox Kit Specification](https://github.com/docker/sandbox-kit-spec) (v3). roost assembles a workspace's Kits, adds the driver and the agent host, and builds an E2B template. A Kit's `volume@1` paths are the persistent, backed-up paths. A workspace keeps the configuration it was created with; a changed Kit set applies to new workspaces.

## Code and processes

Everything roost does outside the sandbox is one Go library, **`roost-core`**: execution grants, the workspace lifecycle, routing to the current grant, snapshots, restore and fork, model credentials through the `SecretProvider`, the sandbox provider, the backup pipeline, and the reconcilers. It speaks no network protocol of its own. Two processes run it:

- **`roost serve`** is the control plane: `roost-core` behind one HTTP API, for applications and operators alike. It authenticates, calls `roost-core` and runs the reconcilers that create sandboxes, issue grants, watch runs, revoke model credentials and put workspaces to sleep. One process acts at a time, holding a lock in the database (SQLite by default, Postgres for more).
- **`roost backup`** runs kopia: it pulls change streams, writes the repositories, pushes snapshots into restored and forked sandboxes, and answers `roost serve`'s reads of snapshot listings and the read-only projection, which it serves by running the agent host read-only on a snapshot's agent storage. The repositories have one holder.

Both, and the CLI, are one `roost` binary. In every sandbox run **`roost-driver`** and **`roost-agent-pi`**; when a release changes them, sandboxes get them in place at their next quiet moment.

The HTTP API is the public contract; the Go API of `roost-core` is not yet stable. `roost serve` and `roost backup` reach the driver through E2B's endpoint, so roost needs no public address.

## Operations

The `roost` CLI uses `roost <object> <verb>`: `status`, `doctor`, `ws inspect|recover|revoke|snapshots|snapshot|restore|fork`, `conv list|transcript|interrupt|reset`. It is a thin client of the HTTP API of `roost serve`, prints `--json`, ships a `SKILL.md` for agents, requires an operator token and `--reason` for anything that changes state, supports `--dry-run`, audits every change, and refuses to run inside a sandbox.

## Deployment

| Where | You need | Command | What runs |
|---|---|---|---|
| Any machine, E2B Cloud | An E2B account | `roost up` | `roost serve` and `roost backup`; sandboxes and key injection on E2B Cloud |
| One Linux machine | KVM and Docker | `docker compose up -d --wait` | `roost serve`, `roost backup`, LiteLLM and a pinned E2B Embed, all state on the data disk |
| GCP | A project | `terraform apply` | A VM with nested virtualization and a persistent data disk, running the same compose stack |
| Kubernetes | Nodes with KVM | `kubectl apply -k` | E2B Embed, roost's processes, and LiteLLM behind a Service with a fixed cluster IP |

In single-machine setups roost uses SQLite on the data disk and adds no database beyond Embed's; Embed is pinned at a fixed version; the backup repository defaults to the data disk and can live in S3 or GCS instead.

## Security

- **Tenants** are the hard boundary: workspaces, snapshots, chunks and credentials never cross them.
- **The sandbox** is the trust boundary of a workspace. Inside it, the agent's code can read the model credential and change the agent's storage; both stay within the workspace.
- **Secrets** stay out of the agent's reach: repository credentials in `roost backup`, the driver token in the root-owned driver.
- **Egress** is limited to the Kit's destinations, and to the LLM gateway among private addresses.
- **Fencing**: one grant per workspace, new tokens and new model credentials per grant, rotation on failover and revocation on restore.

## Alternatives considered

| Alternative | Decision | Why |
|---|---|---|
| A conversation layer in roost (conversation tables, message dedupe, inboxes) | Rejected | It duplicates facts the agent owns, and every duplicate needs reconciliation on restore and fork |
| Host Claude Code and Codex through their SDKs | Rejected | Their loops are opaque; durability would stop at the session file |
| The `pi` CLI in RPC mode | Rejected | It persists the conversation but not the execution; a run interrupted mid-step needs a person to continue it |
| A durable agent loop of roost's own | Rejected | Pi Durable already commits every step and makes submissions idempotent |
| Separate OS users for the agent loop and its tools | Rejected | It costs a privileged helper and an RPC for every tool call; confinement, when it comes, uses Landlock instead |
| Run backup work inside the sandbox | Rejected | It would compete with the agent; only capture stays inside, at low priority |
| Read sandbox disks from the E2B Embed host | Deferred | Needs E2B's internal storage format or a change to its orchestrator, and works on Embed only |
| inotify for change tracking | Rejected | One watch per directory, races on new directories, limits on large trees; fanotify covers a filesystem with one mark |
| btrfs for atomic snapshots | Deferred | E2B's guest kernel is built without btrfs |
| Tar the workspace on every snapshot | Rejected | Re-uploads everything each time; content-defined chunks upload only what changed and share data between branches |
| A backup engine of roost's own | Rejected | kopia already does content-defined chunking, deduplication, encryption, retention and maintenance, and restores files without roost |
| kopia running inside the sandbox | Rejected | It needs the repository's password, which would let the agent's code read or delete every workspace of the tenant |
| A table of side effects to perform (an outbox) | Rejected | It repeats intent the workspace and grant rows already state; reconciling those rows against E2B and the drivers needs no second record |
| A table of operations for restores and forks | Rejected | A restore is a phase of its workspace and a fork is new workspaces; a separate record would hold the same fact twice |
| Back up the root filesystem | Rejected | The template rebuilds it; restoring system directories into a running machine is fragile |
| Provider volumes or a network file system for workspaces | Rejected | E2B volumes are an NFS beta disabled in Embed; JuiceFS measured 6 to 11 s for `git status` and 64 to 141 s for `npm ci` |
| roost as a library only, embedded in the application's backend | Rejected | Only Go backends could use it; the loops that keep one live agent per workspace need a long-running process, which a serverless or scaled-to-zero backend is not; E2B and gateway credentials would spread into every caller |
| A Docker backend | Rejected | It has no microVM isolation |
| Channel adapters | Out of scope | Built on the conversation API by the integrator |

## Roadmap

| Milestone | Scope |
|---|---|
| M0 · Contracts | The contracts, the sandbox protocols, the Kit mapping, conformance scenarios |
| M1 · Agent | `roost-core` and `roost serve`, the driver, the Pi host, the conversation API, execution grants, E2B Cloud, the CLI |
| M2 · Backup | Change capture, `roost backup` on kopia, snapshots, restore, fork, the read-only projection |
| M3 · Self-hosted | E2B Embed with LiteLLM, one-command deployments |

After M3: confining the agent's tools and capturing environment directories ([last section](#later-confinement-and-environment-capture)).

## Later: confinement and environment capture

Not part of v1alpha1. Two additions, both drawn from DeepSeek's DSec, which runs agent reinforcement learning at scale and reports models searching platform files for answers, calling internal sockets and forging requests.

**Strict mode: confining the agent's tools.** It would become the default, with a relaxed mode for trusted interactive use.

- Tool processes start through `roost-driver confine`, which applies a Landlock ruleset before executing them: the persistent paths and `/tmp` read-write, system directories read-only, and no access to `/var/lib/roost`, `/run/roost` or the workspace's hidden paths (graders, reference answers). Landlock needs no privilege and its rules pass to child processes, so no second user is involved.
- File tools that run inside the agent host resolve every path beneath a persistent path (`openat2` with `RESOLVE_BENEATH`) and refuse roost's directories and hidden paths.
- `kernel.yama.ptrace_scope=1` keeps tools from reading the agent host's memory, and the host's socket accepts only the driver (`SO_PEERCRED`).
- Hidden paths and the isolation mode become part of the runtime configuration; moving from strict to relaxed counts as widening permissions.
- E2B's guest kernel has Landlock, Yama and SELinux, but not AppArmor, which DSec uses.

**Environment capture: overlays over system directories.**

- Before the agent starts, the driver mounts overlayfs over `/usr/local`, `/opt`, `/home` and `/root`, and any directory the Kit adds, with their writable layers on the sandbox's disk.
- What the agent installs there is captured with the workspace and comes back with a snapshot, in the way DSec captures a sandbox's writable layer. The rest of the root filesystem still comes from the template.
- Overlaying `/usr` or `/etc` after boot is fragile and left to Kits that need it.
