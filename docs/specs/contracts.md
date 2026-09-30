# roost contracts (v1alpha1)

| | |
|---|---|
| Status | Draft; changes freely until M0 closes |
| Design | [RFC 0001](../rfcs/0001-durable-agent-runtime.md) |

This document is the normative contract between roost and the things around it: applications, channels, sandbox backends, Kits, object storage and operators. The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. Sections marked **TBD** are not specified yet; nothing may depend on them.

## 1. Identifiers

| Identifier | Form | Owner |
|---|---|---|
| Tenant | `[a-z0-9-]{1,63}` | Operator |
| Workspace | `<tenant>/<name>`; by default the conversation's `owner` | roost |
| Conversation | `<adapter>:<natural key>` for channels (e.g. `slack:C123:1712.0000`, `feishu:oc_x:om_y`, `telegram:123456:42`); `api:<ulid>` for the conversation API | roost |
| Message | Caller-supplied, unique per conversation; it is the idempotency key | Caller or adapter |
| Turn | `<conversation>/<ulid>` | roost |
| Grant | Workspace generation + conversation token; internal | roost |

A conversation's workspace and tenant are fixed when it is created and MUST NOT change.

## 2. Invariants

These hold for every backend and every configuration. None of them can be turned off.

1. A message with an already-seen id MUST NOT start a second turn or produce a second answer.
2. A conversation MUST run at most one turn at a time.
3. At most one executor MAY hold a conversation's grant. Writes to roost carrying a superseded grant MUST be rejected.
4. Nothing MUST cross a tenant: bindings, snapshots, credentials, caches and object-storage prefixes are all tenant-scoped.
5. Credentials injected by roost MUST NOT be written to the workspace, the shadow repository or any snapshot.
6. A snapshot MUST cover the whole workspace at one instant, MUST be taken only while no agent process in the workspace is running (idle, or frozen for the snapshot), and MUST be atomic: a restore sees a complete snapshot or the previous one.
7. roost MUST NOT store a copy of a sandbox's lifecycle state; it asks the backend.
8. A Kit that requires a capability the backend cannot enforce MUST be rejected before any sandbox is created.
9. A Kit update that widens permissions MUST wait for operator approval.

## 3. Conversation API

HTTP/JSON under `/v1`, authenticated with a tenant API key. Called from the application's backend, not from browsers.

### Create a conversation

`POST /v1/conversations`

```json
{ "owner": "user-42", "workspace": "optional-name" }
```

Response `201`: `{ "id": "api:01J9Z...", "workspace": "acme/user-42" }`. If `workspace` is omitted it defaults to `owner`.

### List conversations

`GET /v1/conversations?owner=<id>&cursor=<c>&limit=<n>`: newest first; `next_cursor` when more exist.

### Send a message

`POST /v1/conversations/{id}/messages`

```json
{ "id": "m-1", "text": "hi" }
```

Responses:

| Status | Body | Meaning |
|---|---|---|
| `202` | `{ "status": "queued" }` | Accepted; it will be part of the next turn |
| `200` | `{ "status": "duplicate" }` | This message id was already accepted; nothing new happens |
| `404` | | Unknown conversation |
| `409` | | Conflicting workspace or tenant |
| `429` | | Tenant quota exceeded |

### Stream events

`GET /v1/conversations/{id}/events` returns Server-Sent Events. Each event has an `id`; a client resumes with `Last-Event-ID`. Event types:

| Type | Payload |
|---|---|
| `turn.started` | `turn_id`, `message_ids` |
| `delta` | `turn_id`, `text` |
| `tool` | `turn_id`, `name`, `status` |
| `notice` | `turn_id`, `kind` (`waking`, `recovering`, `upgrading`) |
| `turn.completed` | `turn_id` |
| `turn.failed` | `turn_id`, `reason` |

Delivery is at-least-once; clients deduplicate by event `id`.

## 4. Channel adapters

- Webhook endpoint: `POST /v1/channels/{adapter}/{tenant}/webhook`. The adapter verifies the channel's signature and MUST acknowledge only after the message is durably accepted.
- The adapter derives the conversation id from the payload and records the reply route (chat, thread, anchor message) when the conversation is created.
- Replies stream as plain text by editing one message per turn. Messages the adapter posts are recorded so that a user replying to them lands in the same conversation.
- v1alpha1 adapters: Telegram, Slack, Feishu.

## 5. Busy conversations

When a message arrives while a turn is running:

| Policy | Behaviour |
|---|---|
| `queue` (default) | Held in the inbox; all held messages become the next turn after `batch_window` |
| `interrupt` | The running turn is stopped; held messages start a new turn |
| `inject` | Delivered into the running turn (only where the harness supports it, e.g. Claude Code over stdin) |

## 6. Sandbox backend interface

Every backend implements:

| Group | Operations |
|---|---|
| Lifecycle | `Create(template, labels)`, `Connect(id)` (resumes if paused), `Pause(id)`, `Kill(id)`, `SetTimeout(id, d)` |
| Execution | `Exec(id, argv, env) → stream(stdout, stderr), exit` |
| Files | `Read(id, path)`, `Write(id, path, bytes)` |
| Reachability | `Endpoint(id, port) → url, headers` |
| Templates | `BuildTemplate(image, resources, start, ready) → template id` |
| Inventory | `List(labels) → []{id, state, labels}` in one call |
| Capabilities | `Supports() → capability matrix` (section 7) |

Errors are classified as `not_found`, `conflict`, `unavailable` (retryable), `rejected` (not retryable) and `unsupported`.

Backends in v1alpha1: `docker`, `e2b` (E2B Cloud or E2B Embed, selected by API URL).

## 7. Kits

- Kits follow the Docker Sandbox Kit Specification v3. roost resolves and assembles the workspace's Kits, builds a template from the assembled image, and caches the template id by the digest of the resolved Kit set.
- `volume@1` paths are the persistent paths: they are what the shadow repository and snapshots cover.
- `agent-sessions@1` argv templates are how the driver starts and resumes the agent.

Capability support:

| Capability | docker | e2b Cloud | e2b Embed |
|---|---|---|---|
| Image (linux/amd64, glibc or Alpine base) | yes | yes | yes |
| `resources@1` CPU, memory | yes | yes | yes |
| `resources@1` GPU | no | no | no |
| `network-policy@1` domain allow | yes (via proxy) | yes | yes |
| Domain exceptions carved out of an allow rule | TBD | no | no |
| `network-policy@2` method or path rules | no | no | no |
| `credential@1` proxy-managed header injection | TBD | yes (≤ 10 domains) | no |
| `volume@1` | yes | yes (via snapshots) | yes (via snapshots) |
| `lifecycle@1` install, startup, files | yes | yes | yes |
| `long-running@1` | yes | yes | yes |
| `port@1` | yes | yes | yes |
| `privileged@1`, `usb-device@1` | no | no | no |

A "no" for a capability a Kit requires is a rejection (invariant 8).

## 8. State layout

### Database

Logical records (the DDL is an implementation detail): tenants, workspaces (binding: `sandbox_id`, `generation`), conversations (workspace, reply route, policy), messages (id, status), turns (status, attempts, grant), grants, events (per conversation, ordered), audit entries.

### Inside the sandbox

- Shadow repository: `/var/lib/roost/shadow.git`, work tree = the workspace's `volume@1` paths. The driver is its only writer: commits from all conversations are serialized, one per completed step, and each commit message records the conversation, the turn id and the SDK's step id (e.g. Claude's `tool_use_id`). Automatic `git gc` is disabled; roost runs maintenance between turns.
- Files larger than `snapshot.max_file_size` (default 10 MiB) are ignored by the shadow repository.
- Excluded from both layers: paths listed in `snapshot.exclude` (defaults include `node_modules`, `.cache`, build output).

### Object storage

- One kopia repository per workspace at `<bucket>/tenants/<tenant>/workspaces/<workspace>/`.
- A snapshot is taken when the workspace goes idle after a turn, and at least every `snapshot.max_interval`, freezing running agent processes if needed. It is tagged with the turns completed since the previous snapshot.
- Retention: `snapshot.keep_last` (default 50) plus `snapshot.keep_daily` (default 7). Maintenance is scheduled by the control plane, never from inside a sandbox.
- Upload credentials are short-lived, issued per turn and restricted to the workspace prefix (STS session policy on S3, downscoped tokens on GCS). Local development uses a filesystem repository; tests use RustFS.

## 9. `roost.yaml`

```yaml
workspace:
  sandbox:
    backend: e2b                 # docker | e2b
    kits: [docker.io/sbx/claude-kit:2]
  assign: per-owner              # per-owner | per-conversation | per-channel-chat
  idle: { sleep_after: 15m }
  upgrade: next-turn             # next-turn | now
snapshot:
  store: s3://bucket             # s3:// | gs:// | file://
  max_interval: 10m
  max_file_size: 10MiB
  exclude: [node_modules, .cache]
  keep_last: 50
  keep_daily: 7
conversation:
  when_busy: queue               # queue | interrupt | inject
  batch_window: 2s
turn:
  watchdog: { first_output: 90s, stall: 180s, ceiling: 3h }
  retries: { restart_process: 1, replace_sandbox: 4 }
tenants:
  acme: { max_workspaces: 500, max_running_sandboxes: 20 }
```

Unknown keys are errors. Secrets appear only as references, never as values.

## 10. CLI

- Shape: `roost <object> <verb> [args]`; objects are `channel`, `ws`, `conv`, `turn`; `roost status` and `roost doctor` are global.
- Every command accepts `--json`; JSON output is a stable, versioned schema.
- Exit codes: `0` success, `1` failure, `2` usage error, `3` not found, `4` conflict, `5` needs approval.
- Commands that change state (`ws recover`, `conv reset`, `turn retry`) require an operator token and `--reason`, accept `--dry-run`, and write an audit entry.
- The CLI talks only to the control-plane API and refuses to run inside a sandbox.

## 11. Driver protocol

Specified in [driver-protocol.md](driver-protocol.md): how the control plane starts turns in a sandbox, pulls their events, carries grants, restores and snapshots workspaces, and drains a sandbox before it is replaced.

## 12. Conformance scenarios

Every backend and every change to the core must pass these, against a local stack (Docker and RustFS) without cloud credentials:

1. The same message delivered twice produces one turn and one answer.
2. Two messages sent while a turn runs become one batched turn (`queue` policy).
3. After an executor is replaced, events and writes from the old grant are rejected.
4. Killing the agent process mid-turn resumes the turn at the last completed step.
5. Killing the sandbox mid-turn restores the last snapshot on a new sandbox, and every conversation resumes from its session in that snapshot.
6. An upgrade applied at a turn boundary keeps the workspace's files and every conversation's session.
7. With two conversations busy without a break, a snapshot is still taken within `snapshot.max_interval`, and restoring it leaves each conversation's session consistent with the files.
8. Two conversations in one workspace share files but not sessions or reply routes.
9. Two tenants never share a binding, snapshot prefix, credential or cache entry.
10. A Kit requiring an unsupported capability is rejected before any sandbox exists.
11. A failed channel delivery does not mark a turn as answered, and a retry never replies into another conversation.
12. Credentials injected by roost are absent from every snapshot.
