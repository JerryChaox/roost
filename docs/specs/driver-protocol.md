# Sandbox protocols (v1alpha1)

| | |
|---|---|
| Status | Draft; changes freely until M0 closes |
| Parent | [Contracts](contracts.md) · [RFC 0001](../rfcs/0001-durable-agent-runtime.md) |

Every sandbox runs two roost processes:

- **`roost-driver`**, the core, written in Go. It gates every request from outside, supervises the agent host, holds the lock on agent storage, carries the workspace's execution grant, and captures backup data. It does not understand conversations.
- **The agent host**, which runs the agent loop and serves the conversation interface. In v1alpha1 it is `roost-agent-pi`, a TypeScript program that embeds [Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable).

```text
control plane ──HTTP/SSE, driver token──▶ roost-driver ──proxy over a Unix socket──▶ agent host (conversation interface)
backup service ──HTTP, backup token────▶ roost-driver ◀──JSON-RPC over stdio──────▶ agent host (control)
```

The control plane and the backup service always call the driver; nothing in the sandbox calls roost, so roost needs no public address.

## 1. Execution grant

A workspace has at most one live **execution grant**: `{ workspace, sandbox, start, driver token, backup token, model credentials }`, identified by its `start`. The control plane issues it as a row of its database, ending the previous grant in the same transaction, and routes every request for the workspace to the driver that holds it.

- The control plane issues a new grant whenever it starts a driver: when the sandbox is created, rebooted, restored or forked, when an upgrade replaces the driver, and when the driver stops answering while the sandbox is reachable.
- A new control-plane process rotates the driver token of each grant it takes over (section 3, `rotate`), so requests from an earlier process lose effect without restarting anything.
- The driver accepts only the current grant's tokens: the driver token, used by the control plane, on every route; the backup token, used by the backup service, only on `/v1/backup/*`, `/v1/restore` and `/v1/state`. Everything else gets `401 unauthorized`. The model credentials are issued for the grant by the `SecretProvider` and revoked when the grant ends, so an older grant's credentials reach no model.
- The tokens exist only in roost's database, in the memory of the process that uses them and in the driver's memory; the model credential only with its `SecretProvider` and in the agent host's memory. None of them is written to the sandbox's disk.

## 2. Starting the driver

1. The control plane stops every running driver and agent host in the sandbox through the provider's `Exec`, and confirms they are gone.
2. It starts `roost-driver` with the grant's `start` and tokens, the model endpoints, the backup position and, in a sandbox created for a restore or a fork, the snapshot to restore (with the restore's id for a restore), as one JSON object on the driver's standard input. The driver reads it and closes its input; none of it is passed in arguments or the environment.
3. The driver takes an exclusive lock on `/var/lib/roost/agent/lock`; without it, it exits.
4. With a snapshot to restore, the driver reports `awaiting_restore` and waits until `roost backup` has pushed it (section 6).
5. The driver starts the agent host as a child process, which dies with it, and sends `initialize` over stdio (section 5).
6. The agent host takes its own exclusive lock on agent storage, opens it and resumes interrupted runs; the driver reports ready. A second agent host cannot take the lock and exits without opening storage.

A driver without a grant answers `503 not_ready`. Nothing in the sandbox starts a driver on its own.

## 3. Driver protocol (control plane and backup service → driver)

HTTP/1.1 through the provider's endpoint for the driver port. Every request carries `Authorization: Bearer <driver token or backup token>` and `Roost-Protocol: 1`. Path parameters are URL-encoded.

| Method and path | Handled by | Purpose |
|---|---|---|
| `GET /v1/health` | Driver | Readiness, fingerprints of driver and agent host, the agent's name, durability and capabilities |
| `GET /v1/state` | Driver, with the host's `state` | The grant's `start`, `awaiting_restore` or `ready`, `restoredFrom`, the backup position the backup service has confirmed, runs and queues |
| `POST /v1/drain` | Driver → host `quiesce` | `{ "phase": "draining" \| "open" }` → `{ "running": n }` |
| `POST /v1/grant/rotate` | Driver | `{ "driverToken": "..." }`, authorized by the current token; the old token stops working at once |
| `GET /v1/backup/stream` | Driver | Change records for the backup service (section 6) |
| `GET /v1/backup/content` | Driver | Bytes of a changed file or WAL segment |
| `POST /v1/snapshot` | Driver | `{ "note": "...", "quiesce": true }` → `{ "snapshot": "snap_..." }`: record a snapshot now |
| `PUT /v1/restore` | Driver | The snapshot to restore, from the backup service; accepted only while `awaiting_restore` (section 6) |
| `/v1/conversations/...` | Agent host | The conversation interface (section 4), forwarded untouched, streams included |

The driver checks the token and the protocol header, then forwards conversation requests to the agent host over `/run/roost/agent.sock`. It parses none of them.

## 4. Conversation interface (served by the agent host)

The agent host implements these over HTTP on the Unix socket. Their meaning belongs to the agent. `roost serve` maps its public API onto them ([contracts §3](contracts.md#3-conversation-api)); the driver forwards them without parsing.

| Method and path | Purpose |
|---|---|
| `POST /v1/conversations` | Create a conversation, or return the one with the same `key`: `{ "key": "slack:C123:1712.0000", "settings": { "model": "...", "instructions": "..." } }` → `{ "conversation": "c_..." }` |
| `GET /v1/conversations` | List conversations: `?key=`, `?active=true`, cursor |
| `POST /v1/conversations/{c}/messages` | `{ "requestId": "m-1", "content": "...", "whenBusy": "follow_up" \| "steer" }` → `202 { "status": "queued" \| "running" }`, or `200 duplicate` |
| `GET /v1/conversations/{c}/entries` | Transcript entries after a cursor, in order; any cursor can be read again |
| `GET /v1/conversations/{c}/stream` | SSE: entries after `after`, then live output and run status |
| `POST /v1/conversations/{c}/abort` | Stop the running run and withdraw queued messages |
| `POST /v1/conversations/{c}/reset` | Start a new model context, optionally from a handoff `note` |
| `POST /v1/conversations/{c}/notes` | `{ "id": "...", "data": { ... } }`: append a note entry without asking the model; idempotent by `id` |

- Messages are admitted durably before `202`; the same `requestId` returns the original submission (Pi's `requestId`).
- Entries have roost kinds, `user`, `assistant`, `tool_result`, `system`, `reset`, `compaction` and `note`, and carry the agent's own record unchanged in `raw`.
- `live` events (partial answer, running tool output) carry no SSE `id` and are not replayed after a reconnect.
- An agent that lacks an optional capability (`steer`, `reset_note`) answers `422 unsupported`.
- **Read-only**: the agent host also runs outside any sandbox on a copy of agent storage, opened read-only, serving `GET /v1/conversations` and `GET /v1/conversations/{c}/entries` and refusing everything else. The backup service runs it this way for the read-only projection.

## 5. Host control channel (driver ↔ agent host)

JSON-RPC 2.0 over the agent host's stdin and stdout, one message per line. Only the driver holds this channel.

| Direction | Method | Purpose |
|---|---|---|
| driver → host | `initialize` | `{ "protocol": 1, "storage": "/var/lib/roost/agent", "socket": "/run/roost/agent.sock", "models": { "baseUrls": {...}, "credential": "..." }, "agent": { "model": "...", "thinking": "high" }, "restoredFrom": { "snapshot": "snap_...", "restore": "rs-1" } }` → `{ "agent": "pi-durable", "version": "...", "durability": "step", "capabilities": ["steer", "reset_note"] }`. `restoredFrom` is set in a sandbox filled from a snapshot; `restore` only for a restore, and then the host appends a `restored` note to every conversation, idempotent by the restore's id |
| driver → host | `quiesce` / `resume` | Stop starting runs and report how many still run / start them again |
| driver → host | `state` / `shutdown` | Runs, queues and last progress / stop after flushing |
| host → driver | `snapshot` (notification) | A step finished: `{ "conversation": "c_...", "run": "run_...", "position": "<agent storage position>" }`; the driver takes a snapshot |

Model credentials reach the host here, not through its environment. The host calls the model endpoints it is given directly, with the grant's credential.

## 6. Backup capture and restore

The driver captures, the backup service does the heavy work in kopia ([contracts §7](contracts.md#7-backup)).

- **Agent storage**: Pi's SQLite database runs in WAL mode. The driver follows the WAL continuously, in the manner of Litestream, and cuts it into ordered segments. Every commit is captured within seconds. From time to time it also offers a new base copy of the database, so the segments after a base stay few.
- **Workspace files**: the driver places one fanotify mark on the filesystem (`FAN_MARK_FILESYSTEM` with `FAN_REPORT_DFID_NAME`, including `FAN_CLOSE_WRITE`) and records which paths under the persistent paths changed. Events only say where to look: a scan of file metadata (size, modification time, inode), after every queue overflow and on a timer, decides what actually changed.
- **Snapshots**: on every `snapshot` from the host, at the end of every run and on a timer, the driver records `{ file tree changes since the last snapshot, agent storage position, time }`.
- **Cost**: capture runs in a low-priority cgroup (`nice`, `ionice`, a small CPU weight). Chunking, hashing, compression, encryption and upload happen outside the sandbox.

`GET /v1/backup/stream?after=<position>` returns, in order, `base` and `wal` records (agent storage), `files` records (changed, created and deleted paths with metadata) and `snapshot` records. `GET /v1/backup/content` returns the bytes the backup service asks for. The backup service advances its position only after it has stored what it read, and the driver reports the position it last confirmed.

**Restore.** A driver started with a snapshot to restore waits in `awaiting_restore`. The backup service sends `PUT /v1/restore` with a tar stream: the persistent paths with their owners and modes, then the agent storage assembled at the snapshot's position. The driver writes it, syncs it to disk, writes `/var/lib/roost/restored-from` last, and only then starts the agent host. A restore is accepted once; a second `PUT` gets `409`, and a driver that restarts finds the marker and does not wait again. Nothing in the sandbox reads the repository.

## 7. Errors

Errors are JSON: `{ "error": "<code>", "detail": "..." }`.

| Status | Codes |
|---|---|
| `400` | `unsupported_protocol`, `invalid_request` |
| `401` | `unauthorized`, including a token of an earlier grant |
| `404` | `unknown_conversation` |
| `409` | `not_awaiting_restore` |
| `422` | `unsupported` |
| `503` | `not_ready` |

## 8. Inside the sandbox

- The sandbox is the workspace's trust boundary. The driver runs as root; the agent host and the tools it runs share the agent's user, as in any coding agent.
- The driver token, the lock and the backup capture belong to the root-owned driver and are out of the agent's reach.
- Code the agent runs can read the model credential and change the agent's own storage. Neither reaches beyond the workspace, and the credential can be revoked when its `SecretProvider` supports it. Confining the agent's tools is planned work ([RFC 0001, last section](../rfcs/0001-durable-agent-runtime.md#later-confinement-and-environment-capture)).
- A sandbox created from a snapshot starts from the template; nothing from the source's processes runs in it.
