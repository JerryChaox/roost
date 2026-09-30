# Driver protocol (v1alpha1)

| | |
|---|---|
| Status | Draft; changes freely until M0 closes |
| Parent | [Contracts](contracts.md) · [RFC 0001](../rfcs/0001-durable-agent-runtime.md) |

The driver protocol is how the control plane (`roost`) talks to `roost-driver`, the process inside every sandbox. The control plane always calls the driver; the driver never calls the control plane. This is why `roost` needs no public address.

## 1. Roles

- **Control plane** owns conversations, turns, grants and bindings. It decides what runs where.
- **Driver** owns what happens inside one sandbox: it starts one agent process per turn attempt, streams its output, checkpoints each step into the shadow repository, and snapshots the workspace.
- **Attempt**: one agent process working on one turn. When a process dies and the turn resumes, that is the next attempt of the same turn.

## 2. Transport

- HTTP/1.1, JSON bodies, UTF-8.
- Reached through the backend's endpoint for the driver port (E2B: the sandbox URL for that port with its traffic token; Docker: a published port).
- Every request carries `Authorization: Bearer <driver token>` and `Roost-Protocol: 1`. Every response carries `Roost-Protocol: 1`.
- The driver token is created by the control plane when the sandbox is created and handed to the driver in its environment at start. It is never written to disk. The driver runs as a different OS user from the agent, so the agent cannot read it.
- A request with a missing or unknown `Roost-Protocol` gets `400 unsupported_protocol`. Unknown fields in bodies are ignored; missing optional fields mean "not supported by this peer", never a default.

## 3. Grants

A grant says which executor may act for a conversation.

```json
{ "generation": 7, "token": "g_2Hq...", "lease_ms": 30000 }
```

The conversation is the prefix of the turn id, so the grant does not repeat it.

- `generation` is the workspace's binding generation. It increases every time the workspace is bound to a new sandbox, so one sandbox only ever sees one generation. The driver echoes it; the control plane is the one that uses it (see below).
- `token` identifies the conversation's current grant. It changes on every takeover.
- The control plane renews the lease implicitly: every event pull carries the grant (see `GET /v1/turns/{turn}/events`). If the driver sees no valid pull for a conversation within `lease_ms`, the lease has lapsed.

Driver rules:

1. A request whose token differs from the conversation's current token is rejected with `409 superseded`, unless it is a turn submission, which replaces the grant. This stops a stale control-plane instance from interrupting or snapshotting on an old grant.
2. While a conversation's lease has lapsed, the tool-admission hook denies every tool call for that conversation. The agent can still think; it cannot act.
3. Every event carries the grant it was produced under.

Control-plane rules:

1. Events are stored only if the workspace is still bound at the event's `generation` and the conversation's token still matches; a pull that started before a rebind and returned after it is dropped whole.
2. A workspace is restored only from a snapshot id reported by `snapshot.completed` under the current generation, never from "the latest snapshot in the repository".

A replaced sandbox is stopped by three things together: nobody pulls from it, so its leases lapse and its tools are denied; its snapshot credentials are per turn and expire; and anything it still uploads is never chosen for a restore.

## 4. Endpoints

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | Readiness and identity |
| `POST /v1/turns` | Start a turn attempt (idempotent) |
| `GET /v1/turns/{turn}/events` | Pull events (long poll) |
| `POST /v1/turns/{turn}/interrupt` | Stop the running attempt |
| `GET /v1/state` | Everything the driver knows, for `inspect` |
| `POST /v1/restore` | Restore the workspace from a snapshot before the first turn |
| `POST /v1/snapshots` | Take a snapshot now |
| `POST /v1/drain` | Prepare the sandbox to be replaced |

### `GET /v1/health`

```json
{ "ready": true, "fingerprint": "sha256:9c1e..." }
```

`fingerprint` is the hash of the driver binary plus the protocol version. The control plane compares it with the fingerprint it expects to decide whether the sandbox is out of date.

### `POST /v1/turns`

```json
{
  "turn": "slack:C123:1712.0000/01JA0...",
  "attempt": 1,
  "grant": { "generation": 7, "token": "g_2Hq...", "lease_ms": 30000 },
  "session": "5f0c...",
  "resume_from": null,
  "messages": [ { "id": "m-1", "text": "hi" } ],
  "credentials": { "AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "...", "AWS_SESSION_TOKEN": "..." }
}
```

- `session` is the SDK session to resume; `null` starts a new one. `resume_from` is the last completed step when resuming after a process death.
- The driver starts the agent with the `agent-sessions@1` argv templates baked into the sandbox's template (for example `claude -p --resume <session> --output-format stream-json`).
- `credentials` are environment variables for this attempt only, kept on tmpfs and removed when it ends. They never enter the workspace, the shadow repository or a snapshot.

Idempotency, keyed by `(turn, attempt)`:

| Situation | Response |
|---|---|
| New `(turn, attempt)` and no attempt is active for the conversation | `202 accepted` |
| Same `(turn, attempt)` again | `200 duplicate` with its status (`running` or `finished`); nothing starts |
| Higher `attempt` for a turn whose previous attempt is still alive | `409 attempt_running`; the control plane must interrupt first |
| Lower `attempt` than one already seen | `409 stale_attempt` |
| Another turn is running in the same conversation | `409 conversation_busy` |
| The sandbox is draining | `423 draining` |
| Too many concurrent attempts in the sandbox | `429 capacity` |

A control plane that lost a response simply submits again: the duplicate answer tells it the attempt is already running or finished.

### `GET /v1/turns/{turn}/events?after=<seq>&wait_ms=<n>`

The request carries the grant in a header: `Roost-Grant: <generation>.<token>`. That header also renews the lease.

```json
{ "events": [ { "seq": 42, "...": "..." } ], "quiet_ms": 1200 }
```

- Returns events with `seq > after`; waits up to `wait_ms` (maximum 30000) when there are none. The next cursor is the last event's `seq`.
- `quiet_ms` is how long the attempt has shown no activity at all, measured by the driver, so the two clocks never need to agree.
- Re-reading with the same `after` returns the same events. The control plane persists events to its database and advances its cursor only after they are stored.

### `POST /v1/turns/{turn}/interrupt`

`{ "attempt": 1 }` → `200` once the process group is gone, including when the attempt had already finished.

### `POST /v1/restore`

`{ "snapshot": "k7f3...", "store": { "url": "s3://bucket/tenants/acme/workspaces/alice", "credentials": [ ... ] } }` → `200` when the workspace and the shadow repository match the snapshot. Only allowed before the first turn on a fresh sandbox.

### `POST /v1/snapshots`

Takes a snapshot of the whole workspace now. If attempts are running, the driver freezes their process groups for the duration of the snapshot and resumes them afterwards. The driver also snapshots on its own when the workspace goes idle after a turn, and when it has been busy for longer than the `max_interval` it was configured with.

### `POST /v1/drain`

`{ "phase": "draining" | "fenced" | "retired" }`

| Phase | Driver behaviour |
|---|---|
| `draining` | Accepts no new turns (`423`); running turns finish; repeating it renews the drain |
| `fenced` | Rejects every write; takes a final snapshot and reports its id |
| `retired` | Stops all attempts; the sandbox can be killed |

`draining` can be reverted with `{ "phase": "open" }` only before `fenced`, so a failed replacement leaves the old sandbox serving as before.

## 5. Events

Every event has the same envelope:

```json
{
  "seq": 42,
  "turn": "slack:C123:1712.0000/01JA0...",
  "attempt": 1,
  "grant": { "generation": 7, "token": "g_2Hq..." },
  "at": "2026-09-30T12:00:03.402Z",
  "kind": "delta",
  "data": { "text": "Looking at the failing test" }
}
```

| `kind` | `data` |
|---|---|
| `attempt.started` | `session` (needed to resume if the process dies) |
| `delta` | `text` |
| `tool` | `step`, `name`, `status` (`started`, `completed`, `failed`) |
| `step` | `step`, `commit`: the shadow-repository commit for a completed step |
| `sdk` | The SDK's own message, passed through unchanged, for transcripts and debugging |
| `notice` | `kind` (`resuming`, `denied_tool`), human-readable `text` |
| `turn.completed` | `session` |
| `turn.failed` | `reason` |
| `snapshot.completed` | `snapshot` |

Ordering: `seq` increases by one per event within a turn, across attempts. `turn.completed` or `turn.failed` is the last event of an attempt. `snapshot.completed` is reported on the stream of every turn it covers.

## 6. Errors

Errors are JSON: `{ "error": "<code>", "detail": "..." }`.

| Status | Codes |
|---|---|
| `400` | `unsupported_protocol`, `invalid_request` |
| `401` | `unauthorized` |
| `409` | `superseded`, `stale_attempt`, `attempt_running`, `conversation_busy` |
| `404` | `unknown_turn` (pull or interrupt for a turn the driver never saw) |
| `423` | `draining` |
| `429` | `capacity` |
| `503` | `not_ready` |

## 7. Inside the sandbox (not part of the wire protocol)

- The driver installs the agent's tool hooks. The pre-tool hook asks the driver, over a Unix socket only the driver can serve, whether the conversation's lease is valid; the post-tool hook tells the driver to commit a step.
- The shadow repository lives at `/var/lib/roost/shadow.git`, owned by the driver's user. The driver is its only writer and serializes commits from all conversations.
- A freeze is SIGSTOP on each running attempt's process group, then SIGCONT after the snapshot. Hooks and network calls simply resume.
- Snapshots are taken with kopia using the per-turn credentials from the submission.

## 8. What changed from the production system it comes from

The protocol generalises the one running in production behind Museon's agents. Names and mechanisms changed as follows.

| Production | roost | Why |
|---|---|---|
| Host runs `curl 127.0.0.1:8789/...` inside the sandbox through `commands.run` | The control plane calls the driver's endpoint directly | One HTTP call instead of a process spawn per request |
| The worker pushes event batches to the API | The control plane pulls with a cursor | No public address needed; re-reads are safe |
| Proxy (`driver.py`) | `roost-driver` | One name for the in-sandbox process |
| Long-lived session worker per conversation | One agent process per turn attempt | Idle conversations hold no memory; a restart is just the next attempt |
| `worker_instance_id` | `attempt` | Follows from the line above |
| `fencing_token` and lease on the session row | `grant` with `generation`, `token` and `lease_ms` | Adds the workspace generation, so replacing a sandbox fences every conversation at once |
| Grant echoed in the event-ingest response | Grant carried on every pull | Pull replaces push |
| `museon.agent_driver_command.v1` verbs | REST resources | Plain HTTP semantics |
| `receipt` verb | Dropped | Submitting again is idempotent and answers the same question |
| `runtime_replacement` phases (open, draining, fenced, retired) | `POST /v1/drain` with the same phases | Kept as is |
| `record_kind: cc_message` | `kind: sdk` | Not tied to one SDK |
| `driver_alive` per second and a 5 s process heartbeat | `quiet_ms` on every pull | One liveness signal, computed by the driver, with no extra events |
| `cc_session_id` in a sandbox file | `session` in the submission and in `turn.completed` | The control plane owns it; it survives the sandbox |
| `compact`, `context`, `query_with_state` | Dropped | SDK-specific; may return as an SDK passthrough |
| `turn_preparation` permit | Dropped | Existed to hydrate long-lived workers |
| `token_rebind`, `restart_session` | Dropped | No long-lived workers to rebind |
| `museoncli_config`, lark context, host MCP tools | Dropped | Product features, not runtime |
