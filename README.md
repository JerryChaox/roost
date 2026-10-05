<h1 align="center">roost</h1>

<p align="center">
  <b>Durable agents for every user, built on Pi Durable.</b><br>
  Every workspace is backed up as it runs: restore or branch it from any step.<br>
  Build all your enterprise agents on one stack.
</p>

## Why

You give every user an agent, and every agent a sandbox. Then production happens.

- **Sandboxes die.** They time out, get paused, crash. The agent's files and context go with them.
- **Processes die mid-task.** The work is lost, or redone from the start.
- **Two copies run at once.** A retry, a stale instance or a restored sandbox, and two agents write the same state.
- **Keys leak.** An agent that runs generated code next to a model key can be talked into sending it out.
- **Runs can't be reproduced.** Debugging a run, or branching it a hundred times for evaluation, needs the exact workspace at the exact step.

[Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable) makes one agent durable: every model call and tool call is committed before it is shown, and a crashed run continues from its last step. roost is the agent layer that runs it for all your users, on top of a sandbox platform: one sandbox per workspace on E2B, exactly one agent per workspace whatever fails, continuous backups you can restore or branch from any step, and provider keys that never enter a sandbox.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" width="100%" alt="A tenant contains workspaces; a workspace holds several conversations and one long-lived sandbox whose disk keeps the shared files and Pi's storage; each conversation has an inbox and runs one run at a time. Each level lists its guarantees and operator commands.">
</picture>

You only need three words:

- **Workspace**: the agent's computer, one long-lived sandbox with its files and Pi's storage, backed up as it runs. Its name is yours to choose, often one per user.
- **Conversation**: a Pi conversation inside a workspace, with its own transcript and inbox. Conversations in one workspace see the same files.
- **Run**: one run of the agent, from a message to its final answer.

Each level makes its own promise:

| Level | What roost does | What you can rely on |
|---|---|---|
| API | Authenticates and routes every request to the workspace's agent | A message is admitted once. Sleeping workspaces stay readable. |
| Execution | Gives each workspace one execution grant and one agent process | Exactly one agent per workspace, through crashes, reboots, restores and failover. |
| Agent | Runs Pi Durable in the sandbox | Every step is committed first. A crashed run continues from its last step. |
| Backup | Captures every change and stores it outside the sandbox, deduplicated | Restore or branch a workspace from any step, on any machine. |
| Credentials | Mints a model key per grant at your LLM gateway, or uses E2B's egress proxy | Provider keys never enter a sandbox. One command cuts an agent off. |

Isolation is by tenant: data, backups and credentials never cross a tenant. A workspace's sandbox is its trust boundary.

## Talking to agents

Your backend talks to conversations over HTTP. A chat bot can use its thread id as the `key`, so it never stores a conversation id:

```bash
# a workspace per user, a conversation per thread
curl -X PUT localhost:7070/v1/workspaces/user-42 -H "Authorization: Bearer $ROOST_KEY"
# ready once GET /v1/workspaces/user-42 shows "phase": "active"
curl -X POST localhost:7070/v1/workspaces/user-42/conversations -H "Authorization: Bearer $ROOST_KEY" \
  -d '{"key": "slack:C123:1712.0000"}'
# {"conversation": "c_01J9Z..."}

# send a message (its id makes retries safe), then stream the answer
curl -X POST localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../messages \
  -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "m-1", "text": "Fix the failing test"}'
curl -N -H "Authorization: Bearer $ROOST_KEY" localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../events
```

A message sent while the agent is working waits for the next run, or joins the running one with `"delivery": "steer"`.

## Restore and branch

Every step leaves a snapshot: the workspace's files and Pi's storage at that moment, plus how the sandbox was configured.

```bash
roost ws snapshots user-42 --run run_01J...                         # the snapshots of one run, step by step
roost ws restore user-42 --at 2026-10-03T10:00Z                     # files and transcripts go back together
roost ws fork user-42 --snapshot snap_01J... --names rl-1,rl-2,rl-3   # independent workspaces, chunks shared
```

Backups are [kopia](https://kopia.io) repositories, one per tenant, on your disk, S3 or GCS: incremental, deduplicated and encrypted; kopia alone can restore the files. Their passwords never enter a sandbox.

## Operating it

The `roost` CLI covers every layer and is built for agents as much as for people: `--json` everywhere, stable schemas, meaningful exit codes, and a `SKILL.md` so an agent can run it.

| Level | Look | Act |
|---|---|---|
| Workspace | `roost ws inspect`, `roost ws snapshots` | `roost ws recover`, `roost ws revoke`, `roost ws snapshot`, `roost ws restore`, `roost ws fork` |
| Conversation | `roost conv list`, `roost conv transcript` | `roost conv interrupt`, `roost conv reset` |
| Everything | `roost status`, `roost doctor` | |

Commands that change state need an operator token and a `--reason`, support `--dry-run`, and are audited.

## Processes

All of roost's logic outside the sandbox is one Go library, `roost-core`. One `roost` binary runs it as two services, plus the CLI:

- **`roost serve`**: the control plane, `roost-core` behind one HTTP API. One process with SQLite by default, Postgres when you need it.
- **`roost backup`**: pulls changes from every running sandbox into kopia, and pushes snapshots into restored and forked ones.

Inside every sandbox:

- **`roost-driver`** gates every request, keeps exactly one agent running and captures changes at low priority.
- **`roost-agent-pi`** runs Pi Durable with Pi's prompts, tools and skills.

Nothing in a sandbox calls roost, so roost needs no public address. Agents reach models through an LLM gateway such as LiteLLM, with a key roost mints per grant and revokes at will, or through E2B Cloud's egress proxy.

## What roost is not

- **Not an agent loop.** The agent is Pi Durable, with any model Pi supports.
- **Not a chat integration.** Slack, Feishu or Telegram bots are built on the conversation API.
- **Not a sandbox provider.** Sandboxes run on E2B Cloud, or on [E2B Embed](https://github.com/e2b-dev/runtime/tree/main/embed) (preview) on your own Linux machines with no E2B account.
- **Not exactly-once.** A tool that was running when a process died is not run again unless it is safe to replay, and side effects it already caused are not undone.

## Run it

```bash
roost up                      # any machine: sandboxes on E2B Cloud
docker compose up -d --wait   # one Linux machine with KVM: roost, LiteLLM and E2B Embed
terraform apply               # a GCP VM with a persistent data disk
kubectl apply -k              # KVM nodes in your Kubernetes cluster
```

## Design

- [RFC 0001: a durable agent runtime built on Pi Durable](docs/rfcs/0001-durable-agent-runtime.md): the principles, the model, where state lives, and why.
- [Contracts](docs/specs/contracts.md): the APIs, invariants, backups, credentials, Kit support and conformance scenarios.
- [Sandbox protocols](docs/specs/driver-protocol.md): the execution grant, the driver, the conversation interface and backup capture.

## License

Apache License 2.0. See [LICENSE](LICENSE).
