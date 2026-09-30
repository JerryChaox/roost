# Driver protocol (v1alpha1)

| | |
|---|---|
| Status | Draft; changes freely until M0 closes |
| Parent | [Contracts](contracts.md) · [RFC 0001](../rfcs/0001-durable-agent-runtime.md) |

The driver protocol is how the control plane (`roost`) talks to `roost-driver`, the process inside every sandbox. The control plane always calls the driver; the driver never calls the control plane. This is why `roost` needs no public address.

## 1. Roles

- **Control plane** owns conversations, turns, grants and bindings. It decides what runs where.
- **Driver** owns what happens inside one sandbox: it hosts the agent SDKs, keeps each conversation's session warm between turns, streams output, checkpoints each step into the shadow repository, and snapshots the workspace.
- **Session**: a conversation's live agent session in the driver. One session serves many turns; it is closed when idle and resumed from its files later.
- **Attempt**: one execution of one turn. When the session dies or is interrupted mid-turn, the turn continues as the next attempt.

## 2. Transport

- HTTP/1.1, JSON bodies, UTF-8.
- Reached through the backend's endpoint for the driver port (E2B: the sandbox URL for that port with its traffic token; Docker: a published port).
- Every request carries `Authorization: Bearer <driver token>` and `Roost-Protocol: 1`. Every response carries `Roost-Protocol: 1`.
- The driver token is created by the control plane when the sandbox is created and handed to the driver in its environment at start. It is never written to disk. The driver runs as a different OS user from the agent, so the agent cannot read it.
- A request with a missing or unknown `Roost-Protocol` gets `400 unsupported_protocol`. Unknown fields in bodies are ignored; missing optional fields mean "not supported by this peer", never a default.

## 3. Grants

A grant says which executor may act for a conversation.

```json
{ "generation": 7, "epoch": 12, "lease_ms": 30000 }
```

The conversation is the prefix of the turn id, so the grant does not repeat it.

- `epoch` is the conversation's fencing token: an integer that the control plane increases by compare-and-set in its database on every takeover. The database is the authority; the driver only enforces the order.
- `generation` is the workspace's binding generation. It increases every time the workspace is bound to a new sandbox, so one sandbox only ever sees one generation. The driver echoes it; the control plane uses it (see below).
- The control plane renews the lease implicitly: every event pull carries the grant (see `GET /v1/turns/{turn}/events`). If the driver sees no valid pull for a conversation within `lease_ms`, the lease has lapsed.

Why an ordered number and not a random token: at a takeover the incoming grant always differs from the one the driver holds, and the driver must tell a newer grant (a legitimate takeover) from an older one (a request from a stale control-plane instance that arrives late). Equality cannot tell them apart; order can.

Driver rules:

1. The driver keeps, per conversation, the highest `epoch` it has accepted, in its own directory (`/var/lib/roost/grants`). That directory is outside the workspace, never checkpointed or snapshotted, and not writable by the agent's user. The driver reloads it when it starts.
2. A request whose `epoch` is lower than the recorded one is rejected with `409 superseded`, whatever the request is, including a turn submission.
3. A request whose `epoch` is higher than the recorded one records the new epoch. Any attempt still running under a lower epoch is stopped at once, and its tool calls are denied from that moment.
4. While a conversation's lease has lapsed, the tool-admission hook denies every tool call for that conversation. The agent can still think; it cannot act.
5. Every event carries the grant it was produced under.

Control-plane rules:

1. Events are stored only if the workspace is still bound at the event's `generation` and the conversation's `epoch` is still current; a pull that started before a rebind or takeover and returned after it is dropped whole.
2. A workspace is restored only from a snapshot id reported by `snapshot.completed` under the current generation, never from "the latest snapshot in the repository".

A replaced sandbox is stopped by three things together: nobody pulls from it, so its leases lapse and its tools are denied; its snapshot credentials are per turn and expire; and anything it still uploads is never chosen for a restore.

### Driver restarts

The driver generates a random `boot_id` every time it starts and keeps it only in memory. It appears in the health response and in every event-pull response. A new `boot_id` tells the control plane that everything the driver held in memory is gone: events not yet pulled, the record of which attempts were submitted, and the leases.

- On start, before serving requests, the driver kills every agent process it did not start itself, reloads the recorded epochs, and holds no leases, so every tool call is denied until the control plane submits again.
- A turn submission carries `expected_boot`. If it differs from the driver's `boot_id`, the driver answers `409 boot_changed` and starts nothing. Without this, a retry of a submission whose response was lost could start the same attempt twice after a restart.
- When the control plane sees a new `boot_id`, it discards its event cursors for that sandbox (sequence numbers restart), treats the running attempts as lost, and submits the next attempt with `resume_from` set to the last step it stored. The workspace and the sessions are still on disk, so the turn continues from that step without a snapshot restore.

## 4. Endpoints

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | Readiness and identity |
| `POST /v1/turns` | Start a turn attempt (idempotent) |
| `GET /v1/turns/{turn}/events` | Pull events (long poll) |
| `POST /v1/sessions/{conversation}/warm` | Start or resume a session before its turn |
| `POST /v1/turns/{turn}/interrupt` | Stop the running attempt |
| `GET /v1/state` | Everything the driver knows, for `inspect` |
| `POST /v1/restore` | Restore the workspace from a snapshot before the first turn |
| `POST /v1/snapshots` | Take a snapshot now |
| `POST /v1/drain` | Prepare the sandbox to be replaced |

### `GET /v1/health`

```json
{ "ready": true, "fingerprint": "sha256:9c1e...", "boot_id": "b_7Qx..." }
```

`fingerprint` is the hash of the driver binary plus the protocol version. The control plane compares it with the fingerprint it expects to decide whether the sandbox is out of date.

### `POST /v1/turns`

```json
{
  "turn": "slack:C123:1712.0000/01JA0...",
  "attempt": 1,
  "expected_boot": "b_7Qx...",
  "grant": { "generation": 7, "epoch": 12, "lease_ms": 30000 },
  "session": "5f0c...",
  "resume_from": null,
  "messages": [ { "id": "m-1", "text": "hi" } ],
  "credentials": { "AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "...", "AWS_SESSION_TOKEN": "..." }
}
```

- `session` is the SDK session to resume; `null` starts a new one. `resume_from` is the last completed step when resuming after a process death.
- The driver hands the messages to the conversation's live session. If none is alive it starts one, resuming `session` when given.
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
| `expected_boot` is not the driver's current `boot_id` | `409 boot_changed` |
| `epoch` lower than the recorded one | `409 superseded` |

A control plane that lost a response simply submits again: the duplicate answer tells it the attempt is already running or finished.

### `GET /v1/turns/{turn}/events?after=<seq>&wait_ms=<n>`

The request carries the grant in a header: `Roost-Grant: <generation>.<epoch>`. That header also renews the lease.

```json
{ "events": [ { "seq": 42, "...": "..." } ], "quiet_ms": 1200, "boot_id": "b_7Qx..." }
```

- Returns events with `seq > after`; waits up to `wait_ms` (maximum 30000) when there are none. The next cursor is the last event's `seq`.
- `quiet_ms` is how long the attempt has shown no activity at all, measured by the driver, so the two clocks never need to agree.
- Re-reading with the same `after` returns the same events. The control plane persists events to its database and advances its cursor only after they are stored.

### `POST /v1/sessions/{conversation}/warm`

Starts or resumes the conversation's session ahead of a turn, so start-up overlaps the batch window. Carries the grant; answers `200` when the session is ready. Warming never runs a turn.

### `POST /v1/turns/{turn}/interrupt`

`{ "attempt": 1 }` → `200` once the attempt has stopped, including when it had already finished. Claude sessions are interrupted through the SDK and stay alive; a Codex session is closed.

### `POST /v1/restore`

`{ "snapshot": "k7f3...", "store": { "url": "s3://bucket/tenants/acme/workspaces/alice", "credentials": [ ... ] } }` → `200` when the workspace and the shadow repository match the snapshot. Only allowed before the first turn on a fresh sandbox.

### `POST /v1/snapshots`

Takes a snapshot of the whole workspace now. If attempts are running, the driver freezes their agent CLIs for the duration of the snapshot and resumes them afterwards. The driver also snapshots on its own when the workspace goes idle after a turn, and when it has been busy for longer than the `max_interval` it was configured with.

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
  "grant": { "generation": 7, "epoch": 12 },
  "at": "2026-09-30T12:00:03.402Z",
  "kind": "delta",
  "data": { "text": "Looking at the failing test" }
}
```

| `kind` | `data` |
|---|---|
| `attempt.started` | `session` (needed to resume if the session dies) |
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
| `409` | `superseded`, `boot_changed`, `stale_attempt`, `attempt_running`, `conversation_busy` |
| `404` | `unknown_turn` (pull or interrupt for a turn the driver never saw) |
| `423` | `draining` |
| `429` | `capacity` |
| `503` | `not_ready` |

## 7. Inside the sandbox (not part of the wire protocol)

- The driver is one process, compiled from TypeScript into a single executable, running as its own user. It hosts the Claude Agent SDK and the Codex SDK in-process.
- The agent CLIs the SDKs start run as the agent's user, through `spawnClaudeCodeProcess` (Claude) and `codexPathOverride` (Codex) pointing at a small wrapper. Every tool the agent runs inherits that user, so it cannot read the driver's memory, its grants directory, the driver token or snapshot credentials.
- Tool admission: for Claude, the SDK's `canUseTool` callback checks the conversation's lease in-process, and a post-tool hook tells the driver to commit a step. The Codex SDK has no per-tool callback, so when a Codex conversation's lease lapses the driver closes its session.
- The shadow repository lives at `/var/lib/roost/shadow.git`, owned by the driver's user. The driver is its only writer and serializes commits from all conversations.
- A freeze is SIGSTOP on the process group of each running agent CLI, then SIGCONT after the snapshot. Tool calls and network requests simply resume.
- Snapshots are taken with kopia using the per-turn credentials from the submission.

## 8. What changed from the production system it comes from

The protocol generalises the one running in production behind Museon's agents. Names and mechanisms changed as follows.

| Production | roost | Why |
|---|---|---|
| Host runs `curl 127.0.0.1:8789/...` inside the sandbox through `commands.run` | The control plane calls the driver's endpoint directly | One HTTP call instead of a process spawn per request |
| The worker pushes event batches to the API | The control plane pulls with a cursor | No public address needed; re-reads are safe |
| Proxy (`driver.py`) plus one Python worker per conversation hosting `ClaudeSDKClient` | `roost-driver`: one process hosting every conversation's SDK session | One process instead of a proxy and N workers; the TypeScript SDK is the same official interface |
| Idle-session LRU cap (`MAX_IDLE_SESSIONS`) | `session.idle_timeout` and `session.max_live` | Kept |
| `worker_instance_id` (random per worker process) | `attempt` for the process, `boot_id` for the driver | The same idea one level up: a new id means the old one's memory and authority are gone |
| Random `fencing_token` and lease on the session row; a different token is accepted when the worker is idle and rejected (`token_rebind`) when it is busy | `grant` with an ordered `epoch`, `generation` and `lease_ms`; lower epochs are always rejected | A late request from a stale instance is rejected by order, idle or busy, and across driver restarts |
| `TOKEN_BINDINGS` in proxy memory | Highest epoch per conversation, persisted by the driver | Survives a driver restart |
| Grant echoed in the event-ingest response | Grant carried on every pull | Pull replaces push |
| `museon.agent_driver_command.v1` verbs | REST resources | Plain HTTP semantics |
| `receipt` verb | Dropped | Submitting again is idempotent and answers the same question |
| `runtime_replacement` phases (open, draining, fenced, retired) | `POST /v1/drain` with the same phases | Kept as is |
| `record_kind: cc_message` | `kind: sdk` | Not tied to one SDK |
| `driver_alive` per second and a 5 s process heartbeat | `quiet_ms` on every pull | One liveness signal, computed by the driver, with no extra events |
| `cc_session_id` in a sandbox file | `session` in the submission and in `turn.completed` | The control plane owns it; it survives the sandbox |
| `compact`, `context`, `query_with_state` | Dropped | SDK-specific; may return as an SDK passthrough |
| `turn_preparation` permit | `POST /v1/sessions/{conversation}/warm` | Warming is the part worth keeping; memory hydration is a product concern |
| `token_rebind`, `restart_session` | Dropped | A higher epoch stops the old attempt; restarting a session is an interrupt followed by the next attempt |
| `museoncli_config`, lark context, host MCP tools | Dropped | Product features, not runtime |
