# roost contracts (v1alpha1)

| | |
|---|---|
| Status | Draft; changes freely until M0 closes |
| Design | [RFC 0001](../rfcs/0001-durable-agent-runtime.md) |

This document is the normative contract between roost and the things around it: applications, the sandbox provider, model providers, the backup repository, Kits and operators. The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Identifiers

| Identifier | Form | Owner |
|---|---|---|
| Tenant | `[a-z0-9-]{1,63}` | Operator |
| Workspace | `[a-z0-9-_.]{1,128}`, unique within a tenant; often one per end user | Application |
| Conversation | The agent's own id, unique within a workspace. A caller MAY also give a `key`, unique within the workspace | Agent |
| Message | Caller-supplied, unique per conversation; it is the agent's idempotency key | Caller |
| Run | The id of the message that started the run, unique within its conversation. Work the agent does in conversations it starts itself belongs to that run | Caller |
| Entry cursor | Opaque, ordered within a conversation | Agent |
| Snapshot | `snap_<ulid>` | roost |
| Restore | Caller-supplied, or `rs_<ulid>`; one restore, and its idempotency key | Caller or roost |
| Execution grant | `{ workspace, sandbox, start, driver token, backup token, model credentials }`, identified by its `start`; internal | roost |

A conversation is addressed by two fields, `(workspace, conversation)`. roost stores no conversations: their transcripts, inboxes and idempotency belong to the agent's storage in the workspace.

Every API is scoped to one tenant: a tenant API key implies it, and an operator token names it with the `Roost-Tenant` header. The only exception is the operator's overview behind `roost status` and `roost doctor`.

## 2. Invariants

These hold in every deployment and every configuration. None of them can be turned off.

1. A workspace MUST have at most one live execution grant, and every request for the workspace MUST be routed to the driver that holds it.
2. Only the control plane MAY start a driver. Each grant MUST carry a new driver token, a new backup token and new model credentials; requests carrying an earlier token MUST be rejected, and the model credentials of an earlier grant MUST be revoked.
3. A sandbox MUST run at most one agent host, and agent storage MUST be open in at most one agent loop.
4. At most one control-plane process MAY act at a time.
5. Workspaces, sandboxes, snapshots, credentials and caches MUST be scoped to one tenant; a workspace MUST be forked only within its tenant.
6. Backup data MUST leave the sandbox only by being pulled from the driver; no repository credential enters a sandbox.
7. A workspace MUST NOT sleep until its backup has caught up with its last change.
8. The sandbox of a workspace's live grant MUST NOT be removed.
9. A sandbox created from a snapshot MUST start from the template; nothing from the source's processes runs in it.
10. roost MUST NOT store a copy of a sandbox's lifecycle state; it asks the provider.
11. A Kit that requires a capability the provider cannot enforce MUST be rejected before any sandbox is created. A request that widens permissions, including a restore or fork to a wider runtime configuration, MUST carry an operator's approval and is refused without it; nothing waits for approval.
12. Requests for conversations MUST reach a workspace only while it is `active`.

## 3. Conversation API

HTTP/JSON under `/v1/workspaces/{workspace}`, authenticated with a tenant API key, or an operator token naming the tenant. Called from the application's backend, not from browsers. roost authenticates, finds the workspace's live grant and maps the request onto the agent's [conversation interface](driver-protocol.md#4-conversation-interface-served-by-the-agent-host); the semantics are the agent's.

| Method and path | Purpose |
|---|---|
| `PUT /v1/workspaces/{workspace}` | Create the workspace, or return it: `{ "owner": "user-42" }` |
| `GET /v1/workspaces/{workspace}` | The workspace: owner, `forked_from`, `phase` and its detail |
| `GET /v1/workspaces` | List workspaces: `?owner=`, `?forked_from=`, `?phase=`, cursor |
| `POST /v1/workspaces/{w}/conversations` | Create a conversation, or return the one with the same `key` |
| `GET /v1/workspaces/{w}/conversations` | List the workspace's conversations: `?key=`, `?active=true` |
| `POST /v1/workspaces/{w}/conversations/{c}/messages` | Send a message: `{ "id": "m-1", "text": "...", "delivery": "queue" \| "steer" }` |
| `GET /v1/workspaces/{w}/conversations/{c}/entries` | Read the transcript after a cursor |
| `GET /v1/workspaces/{w}/conversations/{c}/events` | Stream entries, live output and run status (SSE) |
| `POST /v1/workspaces/{w}/conversations/{c}/interrupt` | Stop the running run and withdraw queued messages |
| `POST /v1/workspaces/{w}/conversations/{c}/reset` | Start a new model context, with an optional handoff `note` |

- A message is admitted once per conversation; sending its `id` again returns `200 duplicate`.
- `delivery` says how a message reaches the agent: `queue` (the default) starts a run if none is going and otherwise waits for the next one; `steer` joins the running run after its current tool round, or starts a run if none is going.
- Conversation requests to a workspace that is not `active` get `409 workspace_busy`; reads are still served, from the projection.
- Sending a message to a sleeping workspace wakes it. **Reading** a sleeping workspace does not: `entries` and `GET .../conversations` are served from the read-only projection that `roost backup` keeps (section 7) and carry `as_of`. Because a workspace sleeps only after its backup has caught up, that projection is complete.
- Entry kinds are `user`, `assistant`, `tool_result`, `system`, `reset`, `compaction` and `note`; each entry also carries the agent's own record in `raw`.
- Listing an owner's conversations across workspaces is `GET /v1/workspaces?owner=` followed by one call per workspace the application chooses to open; roost does not fan out on its behalf.

`roost serve` maps each request onto the conversation interface; the two are designed separately, and the driver forwards the mapped request without parsing it.

| Public API | Conversation interface |
|---|---|
| `POST .../conversations` `{ key }` | `POST /v1/conversations` `{ key }` |
| `GET .../conversations` | `GET /v1/conversations` |
| `POST .../messages` `{ id, text, delivery }` | `POST /v1/conversations/{c}/messages` `{ requestId: id, content: text, whenBusy }`, with `queue` → `follow_up` and `steer` → `steer` |
| `GET .../entries` | `GET /v1/conversations/{c}/entries` |
| `GET .../events` | `GET /v1/conversations/{c}/stream` |
| `POST .../interrupt` | `POST /v1/conversations/{c}/abort` |
| `POST .../reset` | `POST /v1/conversations/{c}/reset` |

## 4. Workspace API

Under `/v1/workspaces/{workspace}`, authenticated with a tenant API key or an operator token. Requests that change state accept a `reason`, and are audited with the caller and the reason.

| Method and path | Purpose |
|---|---|
| `GET .../snapshots` | List snapshots: `?from=`, `?to=`, `?run=`, cursor |
| `POST .../snapshots` | Take a snapshot now and label it: `{ "note": "before the migration", "quiesce": true }` → `202 { "snapshot": "snap_..." }` |
| `POST .../restore` | Restore the workspace: `{ "id": "rs-1", "snapshot": "snap_..." }` or `{ "at": "2026-10-03T10:00:00Z" }`, plus `force`, `approve` → `202` and the workspace |
| `POST .../fork` | Create workspaces from a snapshot: `{ "snapshot": "snap_...", "names": ["exp-1", "exp-2"] }`, plus `approve` → `202` and the new workspaces |

### Workspace phases

A workspace records what it is (its name, owner, runtime configuration and `forked_from`) and where roost has taken it, its **phase**. Whether its sandbox is running or paused is not a phase: E2B owns that fact (invariant 10).

| From | To | When |
|---|---|---|
| | `provisioning` | `PUT`, or one name of a fork |
| `provisioning` | `active` | The live grant's driver is ready; for a fork, after the snapshot is restored into it |
| `provisioning` | `failed` | Building the template or creating the sandbox keeps failing |
| `active` | `restoring` | `POST .../restore`, by compare-and-set on the phase |
| `restoring` | `active` | The restore completes, or fails |
| `failed` | `provisioning` | `ws recover` |

The phase carries a detail: in `restoring`, the restore (`id`, the snapshot, `force`, the caller and reason); in `failed`, the error; nothing in the other phases. Only an `active` workspace accepts conversation requests and a restore; any other phase answers `409 workspace_busy`.

### Snapshots

A snapshot is `{ template, runtime configuration, workspace file tree, agent storage position, time }`, stored as one kopia snapshot (section 7). The root filesystem is not part of it; the template rebuilds it, so changes meant to last belong in the Kit or under a persistent path.

- **When**: after every step of a run (every completed tool call), at the end of every run, on `backup.snapshot_interval`, and on request.
- **Consistency**: a snapshot is consistent for the conversation whose step produced it. Across conversations running at the same moment, it holds what a power loss would leave. A snapshot taken with `quiesce: true` waits until no run is going (up to `workspace.quiesce_wait`) and is consistent for every conversation; it is marked `consistent: true`.
- **Runtime configuration** is recorded with every snapshot: the resolved Kit configuration (egress rules, credential injection, resources, ports, startup hooks, services), creation parameters (non-secret environment, timeout and auto-pause, labels), the driver and agent host fingerprints, and secret references. Secret values are never recorded.
- **Retention**: `backup.retention`, a kopia retention policy; labeled snapshots are kept.

### Restore

- A restore takes the whole workspace back to a snapshot: files and transcripts return together, and conversations created after the snapshot no longer exist. Side effects in the outside world are not undone.
- `at` restores the latest snapshot at or before that time.
- `id` makes the request idempotent: a request with the `id` of a restore under way or finished returns that restore.
- The workspace is `restoring` until the restore completes or fails; its outcome is in the audit entry written with the phase change.
- After a restore, the agent left in the old sandbox can no longer call a model, and every conversation carries a note entry `{ "type": "restored", "snapshot": "snap_..." }`.

### Fork

Creates one workspace per name in the same tenant, each `provisioning` from the snapshot, in parallel. Each fork gets the snapshot's files and conversations, with conversation ids unchanged and keys kept, and records `forked_from`. Forks do not depend on each other or on the source; their progress is their phases (`GET /v1/workspaces?forked_from=`).

### Errors

| Status | Codes |
|---|---|
| `403` | `needs_approval` (the request widens permissions and carries no operator approval) |
| `404` | `unknown_workspace`, `unknown_snapshot` |
| `409` | `workspace_busy` (the workspace is not `active`), `workspace_exists` |
| `422` | `config_unresolvable` (a secret reference no longer resolves), `unsupported` (the `SecretProvider` cannot revoke) |

## 5. Operator API

Authenticated with an operator token; every request carries a `reason` and is audited. An operator approves a request that widens permissions by sending it with `"approve": true`.

| Method and path | CLI | Effect |
|---|---|---|
| `POST /v1/workspaces/{w}/recover` | `ws recover` | Reboots the sandbox and starts a new driver under a new grant; moves a `failed` workspace back to `provisioning` |
| `POST /v1/workspaces/{w}/revoke` | `ws revoke` | Revokes the live grant's model credentials at once: the agent can no longer call a model until a new grant is issued |

Conversation `interrupt` and `reset` are also available to operators through the CLI (`conv interrupt`, `conv reset`).

## 6. Model credentials

roost does not proxy model traffic. Model access is a cluster service, and roost reaches it through one interface, the **`SecretProvider`**, chosen by `models.access`.

- **Issue**: when a grant is issued, the `SecretProvider` returns the model endpoints and a credential, and a reference that roost stores on the grant (`model_key_ref`). The agent host receives the endpoints and the credential over the control channel and calls models directly.
- **Revoke**: when the grant ends (a new grant or a restore) or on `ws revoke`, roost asks the `SecretProvider` to revoke the grant's credentials, retries until it confirms, and records the confirmation on the grant (`key_revoked_at`). A `SecretProvider` that cannot revoke says so; `ws revoke` then answers `422 unsupported`.
- **Implementations**:
  - `litellm`: a virtual key per grant at an LLM gateway, scoped to the workspace and the tenant, revoked at the gateway. Provider keys, usage and budgets live in the gateway.
  - `e2b`: a placeholder; E2B Cloud's egress proxy replaces it with the provider key (`models.providers`) on requests to the providers' hosts. It cannot revoke.
- **Reaching the LLM gateway on E2B Embed**: E2B denies sandbox egress to private ranges unless the orchestrator node exempts them. The gateway has a fixed address (`models.gateway.url`); every sandbox's egress rules allow it; E2B Embed sets `ALLOW_SANDBOX_INTERNAL_CIDRS` to it as a `/32`. roost's manifests deploy LiteLLM this way, and `roost doctor` checks it from a test sandbox.

## 7. Backup

The engine is [kopia](https://kopia.io), used as a library by `roost backup`.

- **Capture**: the driver captures changes in the sandbox at low priority ([sandbox protocols §6](driver-protocol.md#6-backup-capture-and-restore)): the agent storage's WAL continuously, and workspace files through fanotify plus a metadata scan.
- **Upload**: `roost backup` pulls the change stream from every running driver and presents each workspace to kopia as a filesystem built from that stream. kopia reuses everything whose metadata did not change and reads only changed files through the driver; it splits them into content-defined chunks, compresses and encrypts them, and stores only chunks the repository lacks.
- **Repository**: one kopia repository per tenant, under `backup.repository` (a directory on the persistent disk by default, or S3 or GCS) unless the tenant names its own, encrypted with the tenant's password. The repository and its password stay with `roost backup`. kopia alone can restore a snapshot's files.
- **Snapshot layout**: one kopia snapshot per roost snapshot, tagged with the snapshot id, workspace, run and conversation. It holds the persistent paths, `.roost/runtime.json` (the runtime configuration) and `.roost/agent/` (the agent storage: a base database and the WAL segments after it, up to the snapshot's position).
- **Maintenance**: `roost backup` runs kopia's maintenance and retention as the repository's only owner.
- **Filling a sandbox**: for a restore or a fork, `roost backup` assembles the snapshot, with the agent storage at the snapshot's position, and pushes it to the new sandbox's driver.
- **Reads for `roost serve`**: snapshot listings and the read-only projection are served by `roost backup` on its internal API (`backup.api`), so the repository has one holder. For the projection, `roost backup` runs the agent host read-only on the agent storage of the latest snapshot, outside any sandbox ([sandbox protocols §4](driver-protocol.md#4-conversation-interface-served-by-the-agent-host)), so roost never reads the agent's storage format itself.
- A workspace sleeps only after `roost backup` has stored everything up to its last change (invariant 7).
- Backed-up paths are the Kit's `volume@1` paths, `/workspace` at least; paths under them can be excluded (for example `node_modules`).

## 8. Runs and the watchdog

The agent runs the loop. A run is `queued`, `running`, `completed`, `failed` or `aborted`.

| Setting | Trigger | Action |
|---|---|---|
| `run.watchdog.stall` | The agent host reports no progress on a run for this long | Restart the driver under a new grant; the agent resumes the run |
| `run.watchdog.ceiling` | A run has gone on this long | Flag it `needs_attention`; nothing is stopped |

A driver that stops answering is started again under a new grant; a sandbox unreachable for `workspace.recover_wait` is rebooted. Every restart and reboot ends a grant with its reason and the run it was for, so the count per run is read from the grants. After `run.retries.restart_driver` restarts and `run.retries.reboot_sandbox` reboots for the same run, the run is aborted and flagged. `needs_attention` is derived from these counts and from the ceiling, not stored.

## 9. Sandbox provider

The provider in v1alpha1 is E2B: E2B Cloud or E2B Embed, selected by API URL.

| Group | Operations |
|---|---|
| Lifecycle | `Create(template, config, labels)`, `Connect(id)` (resumes if paused), `Pause(id)`, `Reboot(id)` (pause without keeping memory, then resume: a cold boot from the sandbox's disk), `Kill(id)` (invariant 8), timeouts that pause and never kill; a request to a paused sandbox's endpoint resumes it |
| Execution | `Exec(id, argv, env, user)` with streaming output |
| Reachability | `Endpoint(id, port) → url, headers` |
| Network | Update a running sandbox's egress rules |
| Templates | `BuildTemplate(image, resources, start, ready, alias) → template id`, `FindTemplate(alias)` |
| Inventory | `List(labels)` in one call. Every sandbox carries `roost.tenant` and `roost.workspace`, and a restore's sandbox `roost.restore` |

E2B's own snapshots are not used; roost's snapshots come from the backup (section 7).

## 10. Kits

- Kits follow the Docker Sandbox Kit Specification v3. roost resolves and assembles a workspace's Kits, adds `roost-driver` and the agent host, and builds a template whose alias is the digest of the resolved set, so finding a built template asks the provider.
- A workspace keeps the runtime configuration it was created with; a changed Kit set applies to new workspaces.

| Capability | E2B Cloud | E2B Embed |
|---|---|---|
| Image (linux/amd64, glibc or Alpine base) | yes | yes |
| `resources@1` CPU, memory | yes | yes |
| `resources@1` GPU | no | no |
| `network-policy@1` domain allow | yes | yes |
| Domain exceptions carved out of an allow rule | no | no |
| `network-policy@2` method or path rules | no | no |
| `credential@1` proxy-managed header injection | yes (≤ 10 domains) | no |
| `volume@1` (persistent, backed-up paths) | yes | yes |
| `lifecycle@1` install, startup, files | yes | yes |
| `long-running@1`, `port@1` | yes | yes |
| `privileged@1`, `usb-device@1` | no | no |

## 11. State layout

| State | Where |
|---|---|
| Tenants, `SecretProvider` settings and repository passwords (secret references), Kits, policies | `roost.yaml` |
| Workspaces, execution grants, audit | roost's database: the three tables below |
| Transcripts, inboxes, message idempotency, run checkpoints | Agent storage: `/var/lib/roost/agent` on the sandbox's disk |
| Workspace files | Persistent paths on the sandbox's disk |
| Snapshots | The tenant's kopia repository |
| Sandboxes, running or paused; templates | E2B |

The database holds only what roost decides: what each workspace is and where roost has taken it, who may execute it, and who asked for what. Two things share a table only if they are one-to-one at every moment, change in the same transitions and have the same writer.

**`workspaces`**: one row per workspace.

| Column | |
|---|---|
| `tenant`, `name` | Primary key |
| `owner`, `forked_from`, `created_at` | Set at creation |
| `runtime_config` | The resolved configuration the workspace runs with; replaced only when a restore completes |
| `phase`, `phase_since`, `phase_detail` | Section 4. A `CHECK` ties the detail to the phase: none in `provisioning` and `active`, the restore in `restoring`, the error in `failed` |

**`grants`**: one row per execution grant.

| Column | |
|---|---|
| `id` | The grant's `start` |
| `tenant`, `workspace`, `sandbox_id` | |
| `driver_token`, `backup_token`, `model_key_ref` | The two tokens, and the `SecretProvider`'s reference to the grant's model credentials (none when it needs none) |
| `issued_at`, `ended_at`, `end_reason` | `end_reason`: `driver_lost`, `stalled`, `rebooted`, `recover`, `upgrade` or `restore` |
| `run` | The conversation and run a restart or reboot was for (section 8) |
| `key_revoked_at` | When the `SecretProvider` confirmed the revocation of the grant's model credentials |

A partial unique index on `(tenant, workspace) WHERE ended_at IS NULL` allows one live grant per workspace. Issuing a grant ends the previous one in the same transaction.

**`audit`**: append-only; the caller, tenant, action, target, reason and request (never a secret) of every request that changes state, including `--dry-run`, and the outcome of every restore.

No table records work to be done. roost reconciles: it compares these rows with what E2B and the drivers report, and repeats an idempotent step until they agree (section 12).

## 12. Control plane

- `roost-core` is the Go library that holds all of roost's logic outside the sandbox. It speaks no network protocol. Its Go API is not part of this contract in v1alpha1.
- `roost serve` is the control plane: `roost-core` behind the HTTP API of sections 3 to 5, its only protocol. It authenticates requests, calls `roost-core` and runs its reconcilers.
- One `roost serve` process acts at a time. It holds an exclusive lock in the database (a lock row with a lease on SQLite, an advisory lock on Postgres) and exits when it loses it.
- A process that takes over rotates the tokens of every grant before sending other requests.
- `roost backup` runs the backup pipeline of `roost-core` and holds its own lock.

Reconcilers compare the database with what E2B and the drivers report, and act until the two agree. Every step is idempotent, so a crash only means it runs again:

| Owner | Condition | Step |
|---|---|---|
| `roost serve` | `provisioning`, and no sandbox with the workspace's labels | Create it (finding the template by alias, building it if missing) |
| `roost serve` | `provisioning`, and its sandbox holds no live grant | Issue a grant and start its driver; for a fork, with the snapshot to restore |
| `roost serve` | `provisioning`, and the live grant's driver is ready | Phase to `active` |
| `roost serve` | `active`, and a sandbox with the workspace's labels that is not the live grant's | Remove it |
| `roost serve` | `active`, the live grant's driver stops answering or a run stalls | A new grant on the same sandbox (section 8) |
| `roost serve` | An ended grant whose model credentials are not yet revoked | Revoke them through the `SecretProvider` |
| `roost serve` | `active`, idle for `workspace.sleep_after`, and the driver reports the backup caught up | Pause the sandbox |
| `roost backup` | A live grant's driver reports changes | Pull them and store them |
| `roost backup` | A live grant's driver waits for a snapshot | Push the snapshot to it |

## 13. `roost.yaml`

```yaml
workspace:
  provider: { api_url: https://api.e2b.app, api_key: secret://e2b }
  kits: [docker.io/sbx/base-kit:3]
  sleep_after: 15m               # pause an idle sandbox after this long
  recover_wait: 2m               # reboot a sandbox that stays unreachable this long
  quiesce_wait: 2m               # how long a consistent snapshot or a restore waits for running runs
agent:
  model: anthropic/claude-opus-5-5
  thinking: high
  system_prompt:
    base: pi                     # pi: Pi's own system prompt; none: start empty
    append: file://./prompts/company.md   # optional, added after the base
run:
  watchdog: { stall: 5m, ceiling: 3h }
  retries: { restart_driver: 2, reboot_sandbox: 1 }
backup:
  repository: file:///var/lib/roost/backup   # file:// | s3:// | gs://; each tenant gets <repository>/<tenant>
  snapshot_interval: 60s
  retention: { keep_within: 30d }            # a kopia retention policy: keep_within, keep_latest, keep_daily, ...
  api: { url: http://127.0.0.1:7071, token: secret://roost-backup-api }   # roost backup's internal API, for roost serve
models:
  access: litellm                # litellm (E2B Embed) | e2b (E2B Cloud)
  gateway:
    url: http://10.96.0.50:4000  # the LLM gateway's fixed address; exempted as a /32 on E2B Embed
    admin_key: secret://litellm-admin
  providers:                     # for E2B Cloud's injection; on E2B Embed they live in the gateway
    anthropic: { key: secret://anthropic }
    openai: { key: secret://openai }
tenants:
  acme:
    quotas: { max_workspaces: 500, max_running_sandboxes: 20 }
    backup: { password: secret://acme-backup }   # optional: repository: s3://acme-roost/backup
```

Unknown keys are errors. Secrets appear only as references, never as values. The `agent` settings reach the agent host in `initialize` ([sandbox protocols §5](driver-protocol.md#5-host-control-channel-driver--agent-host)) and apply from the next driver start.

## 14. CLI and processes

- One binary, built on `roost-core`: `roost serve` (control plane), `roost backup`, and the CLI. Model credentials come from a `SecretProvider` (section 6).
- CLI shape: `roost <object> <verb> [args]`. Objects: `ws`, `conv`. `roost status` and `roost doctor` are global.
- Verbs: `ws inspect`, `ws recover`, `ws revoke`, `ws snapshots`, `ws snapshot`, `ws restore`, `ws fork`; `conv list`, `conv transcript`, `conv interrupt`, `conv reset`.
- Every command accepts `--json` with a stable, versioned schema. Exit codes: `0` success, `1` failure, `2` usage error, `3` not found, `4` conflict, `5` needs approval.
- Commands that change state require an operator token and `--reason`, accept `--dry-run`, and write an audit entry. The CLI talks only to the HTTP API of `roost serve` and refuses to run inside a sandbox.

## 15. Sandbox protocols

Specified in [driver-protocol.md](driver-protocol.md): the execution grant, how a driver starts, the driver protocol, the conversation interface, the host control channel and backup capture.

## 16. Conformance scenarios

A release passes every scenario on E2B Cloud and on E2B Embed.

1. The same message sent twice is admitted once and answered once, including across a driver crash between the two sends.
2. A message sent while a run is going is answered in the next run (`queue`) or joins the running run (`steer`).
3. Killing the agent host mid-run: it is restarted and the run continues from its last step.
4. Rebooting a stuck sandbox keeps files and transcripts, and interrupted runs continue.
5. After a new grant, or after `rotate`, every request with an older driver token is rejected; after a new grant, the older model credentials reach no model.
6. A second agent host in the same sandbox cannot open agent storage.
7. A second control-plane process cannot act while the first holds the lock.
8. Repository credentials never appear in a sandbox, and revoking a grant's model credentials stops its agent from calling a model at once.
9. On E2B Embed, a sandbox cannot reach any private address other than the LLM gateway.
10. Every completed tool call produces a snapshot within `backup.snapshot_interval`.
11. A workspace does not sleep while its backup is behind; a sleeping workspace's transcript is readable without waking it.
12. Restoring to a snapshot brings back files and transcripts together, appends a `restored` note, and the old sandbox's agent can no longer call a model; sending the same restore `id` again starts nothing.
13. Forking one snapshot into N workspaces gives N independent workspaces with the snapshot's files and conversations; storing them adds no duplicate chunks.
14. A restore or fork to a wider runtime configuration is refused with `needs_approval`, and runs when an operator sends it with `approve`.
15. A file changed while fanotify's queue overflowed is still captured by the next metadata scan.
16. Two tenants never share a workspace, sandbox, snapshot, chunk, credential or cache entry.
17. A Kit requiring an unsupported capability is rejected before any sandbox exists.
18. An in-place upgrade lets running runs finish and redoes nothing.
19. A control plane killed after creating a sandbox and before issuing its grant leaves no extra sandbox once the workspace is `active`.
20. Model credentials whose grant ended while the `SecretProvider` was unavailable are revoked once it is back.
21. A tenant's repository restored with the kopia CLI alone yields the workspace's files at a snapshot.
