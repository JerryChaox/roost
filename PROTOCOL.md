# roost control protocol v1

Status: pinned at M2 (2026-08-17). This document is the normative description of the
wire contract between a **host** (the runtime library, running inside the host
application process) and a **driver** (the roost process running inside a disposable
sandbox). It is written against `CONTRACTS.md` Appendix B; where the two disagree,
`CONTRACTS.md` wins and this document is a bug.

Terminology follows `DESIGN.md`: *session*, *sandbox*, *turn*, *driver*, *harness*.
All identifiers (`session_id`, `turn_id`, `sandbox_id`) are opaque strings. The
protocol carries **no host domain vocabulary** — anything host-specific travels
inside the opaque `context` blob.

## 1. Transport

The driver serves a loopback HTTP/1.1 control server inside the sandbox, bound to
`127.0.0.1` on the port given by `ROOST_DRIVER_PORT` (default `8787`). It is never
exposed outside the sandbox; the host reaches it through the
`SandboxBackend.request` channel it already needs for everything else (E2B tunnel,
`docker exec`, …). The control plane is **file-free**: no state is exchanged through
the sandbox filesystem, so the protocol does not depend on any filesystem permission
model.

Encoding is JSON over UTF-8. Request bodies must carry `Content-Length`; chunked
request bodies are rejected with `411`. Response bodies are always a JSON object.

## 2. Versioning

Two version axes evolve **independently**, and conflating them is the mistake this
section exists to prevent:

| Axis | Carrier | Bumped when | Consequence |
|---|---|---|---|
| Protocol version | `X-Roost-Protocol-Version` header | a breaking change to endpoints, wire shapes, or semantics | host and driver must agree; mismatch is a hard error |
| Runtime fingerprint | sandbox runtime files hash (`RuntimeStamp.runtime_files_hash`) | any change to the runtime files shipped into the sandbox | stale fingerprint triggers a forced update (M6); the protocol is untouched |

Rules:

- Every request **and** every response carries `X-Roost-Protocol-Version`. The
  current value is `1`.
- A request whose header is missing or carries an unrecognised value is answered
  `400 {"error": "unsupported_protocol_version"}`. Missing is treated exactly like
  unrecognised: a peer that does not state its version is not a peer this protocol
  can serve safely.
- Additive, backwards-compatible fields do **not** bump the version. Decoders ignore
  unknown object members and must not fail on them. Symmetrically, an *absent*
  additive field means "this peer does not implement it" — never a default value.
  Substituting a default silently turns "I cannot tell you" into a confident answer,
  and the reader has no way to notice.
- A protocol bump is a host/driver co-deployment event. A fingerprint bump is not.

## 3. Endpoints

### `POST /v1/turn` — submit a turn

Request body is a turn envelope:

```json
{
  "turn_id": "deterministic-id",
  "session_id": "opaque",
  "payload": {"...": "prompt or message batch"},
  "context": {"...": "opaque host blob"},
  "attempt": 1
}
```

`turn_id` and `session_id` are required non-empty strings; `payload` is a required
object; `context` defaults to `{}` and `attempt` to `1`. Neither the driver nor the
library interprets `payload` or `context`. `attempt` is observational only — it never
participates in idempotency.

Response `200`:

```json
{"turn_id": "deterministic-id", "state": "accepted", "turn_state": "queued"}
```

- `state` is `"accepted"` for the first submission of a `turn_id` and `"duplicate"`
  for every subsequent one.
- `turn_state` is the registry entry's current lifecycle state: `"queued"`,
  `"running"` or `"done"`.
- A duplicate submission is answered from the existing entry and **never re-queues
  anything**, in any state, including after the turn has finished. The stored
  envelope is never overwritten: the first envelope seen for a `turn_id` is
  authoritative.

A malformed body is answered `400 {"error": "invalid_body"}`. A non-`POST` method is
answered `405 {"error": "method_not_allowed"}`.

### `GET /v1/turn/{turn_id}/events?after=<seq>&wait_ms=<n>` — long-poll events

The event stream is carried by **host-driven long-poll pull**. This is a deliberate
choice over server push: it needs no infrastructure beyond the `SandboxBackend.request`
channel that already exists, and the cursor lives on the host, which makes re-reads
naturally idempotent.

- `after` (default `0`): return only events with `seq > after`.
- `wait_ms` (default `10000`, capped at `30000`): if no such event exists yet, wait
  up to this long before returning an empty list.

Response `200`:

```json
{"events": [{"type": "delta", "turn_id": "t-1", "seq": 1, "text": "hi"}],
 "next_after": 1, "liveness_quiet_ms": 12}
```

`next_after` is the cursor for the following request: the highest `seq` returned, or
the unchanged `after` when the page is empty. Re-issuing a request with the same
`after` returns the same page — repeated reads are safe, and a host that crashes
mid-stream resumes by replaying its last cursor.

`liveness_quiet_ms` (added after v1's first release; see §2) is the number of
milliseconds since the driver last did **anything internally** — emitted an event,
accepted a turn, started or finished executing one. It is present on every page,
including empty ones, and it is computed **inside the driver**, so the host never has
to reconcile two machines' clocks: an elapsed duration travels safely across a
boundary that an absolute timestamp does not.

Two things it deliberately does *not* count: the host's own polling (`/v1/health`,
this endpoint), and any self-touching timer inside the driver. A process that looks
alive because someone is watching it reports nothing at all — the field would never
grow, and a host that keys a liveness decision off it would have built a clock that
can never expire.

A host reads it as one of two clocks: the driver being quiet is *not* by itself a
reason to kill a sandbox (a slow API call is quiet and healthy), and the driver
being busy is *not* by itself a reason to keep one (a driver that heartbeats while
producing no renderable output is precisely the hang worth killing).

**Field absent** means the driver predates the field — that is a valid v1 driver, and
a host must treat it as "this runtime has no liveness clock" and fall back to its
own progress tracking. Treating a missing field as `0` makes every such driver look
permanently alive; treating it as infinite kills every one of them on sight.

An unknown `turn_id` is answered `404 {"error": "unknown_turn"}`. Note that "unknown"
is the expected answer after a driver restart (see §5) — it means *this process never
saw the turn*, not *the turn never existed*.

Invalid query values are answered `400 {"error": "invalid_query"}`.

#### Event shapes

Every event is a JSON object with the discriminator `"type"`, the owning `turn_id`,
and a `seq`:

| `type` | Additional fields | Meaning |
|---|---|---|
| `delta` | `text` | streamed assistant text |
| `tool_event` | `name`, `phase` (`"start"` \| `"result"`), `detail` | tool call lifecycle |
| `lifecycle_notice` | `kind` (`"boot_started"` \| `"boot_finished"` \| `"update_started"` \| `"update_finished"`), `elapsed_ms` | boot/update progress, so the host can render "starting · Ns" |
| `terminal` | `status` (`"ok"` \| `"error"`), `error` (string or `null`), `usage` | the turn's final outcome |

Ordering guarantees, per turn:

- `seq` starts at `1` and increases by one with no gaps;
- `terminal` is always the last event — the driver backfills a
  `terminal` with `status: "error"` if the harness raises, or returns without
  emitting one. **A turn always ends.** A missing terminal would turn an explainable
  failure into a hang that only the host watchdog could eventually clean up.

Event buffers are per-turn, in memory, and **unbounded in v1**. Bounding them is
scheduled with the M6 productionisation work; until then a host must not rely on a
sandbox surviving an unbounded event volume.

### `GET /v1/health` — readiness

Response `200`:

```json
{"ok": true, "protocol_version": "1", "uptime_ms": 1460, "harness_ready": true}
```

`harness_ready` reports whether the driver's execution loop is running and able to
accept work. `uptime_ms` is measured from the moment the control server bound its
port, and is a useful signal for "did this sandbox restart under me".

### `POST /v1/update` — reserved

Answered `501 {"error": "reserved_until_m6"}`, and it stays that way: the zero-downtime
forced update shipped in M6 is a *replacement* flow, not an in-place one. When the host
finds a sandbox whose runtime fingerprint is stale it pulls the live workspace off the
old sandbox, boots a new one from the current runtime files, swaps the binding with a
CAS, and only then kills the old sandbox — none of which touches this endpoint. It
therefore remains reserved (the error code keeps its original spelling so that already
deployed drivers stay wire-compatible), available for a future in-place update that
genuinely needs a protocol verb.

### `GET /v1/workspace` — download the workspace

Returns the workspace directory as a gzipped tar archive.

```
200 OK
Content-Type: application/gzip
<tar.gz bytes>
```

Member names are paths relative to the workspace root, with no leading `./`. An empty
or absent workspace answers a valid empty archive — "there is no workspace" and "the
workspace is empty" are the same answer to whoever restores it. Packing failures
answer `500 {"error": "workspace_pack_failed"}`.

### `PUT /v1/workspace` — restore the workspace

Body is a gzipped tar archive, unpacked **over** the workspace directory: members
overwrite same-named files, and existing files not present in the archive are left
alone. Answers `200 {"ok": true}`.

Every member is validated before anything is written, so a rejected archive leaves
the workspace untouched:

- member names must be relative, must not be absolute, and must not escape the
  workspace via `..`;
- only directories and regular files are accepted. Symlinks, devices and FIFOs are
  refused, because their escape hatch is the link target rather than the member name,
  and `GET` does not emit them either — the two directions carry the same member set.

A violation answers `400 {"error": "unsafe_archive"}`; a corrupt or unreadable archive
answers `500 {"error": "workspace_unpack_failed"}`.

Persistence deliberately rides the control plane rather than a `download` method on
the host's sandbox backend port: every backend already has to provide an HTTP channel
to the driver, and nothing about backup needs a second transport.

## 4. The idempotency contract

This section is the reason the project exists; it is normative.

- **Delivery is assumed at-least-once.** Any transport a host plugs into
  `TurnDelivery` (Cloud Tasks, a message queue, an in-process retry loop) may deliver
  the same turn any number of times. The protocol does not ask it to deduplicate,
  and a host must not build one that promises to.
- **`turn_id` is deterministically derived from the triggering message** and is the
  idempotency key. Random ids are a protocol violation: they make duplicate delivery
  indistinguishable from a genuine second request.
- **The driver deduplicates by `turn_id` and thereby provides exactly-once
  execution.** A repeated `POST /v1/turn` returns the existing registry entry with
  `state: "duplicate"` and executes nothing. This holds in every state, including
  after completion.
- **Re-execution happens only on a new sandbox, after an explicit host requeue.**
  The host watchdog decides that a turn is wedged and requeues it; a fresh sandbox
  means a fresh process, which means an empty registry, which is the only legitimate
  way the same `turn_id` runs twice.
- **The turn registry is in-process state whose lifetime is bound to the driver
  process.** This is the boundary line of the two-sided division of labour behind
  invariant I1: the driver guarantees "never twice within this process", and the host
  guarantees "never dispatch a second process for a live turn" (session serialisation
  gate plus turn locks in the host's `StateStore`). Neither side alone is sufficient,
  and neither side may assume the other's half.

Two corollaries that follow directly, and that implementations get wrong if they are
left implicit:

- A driver restart **loses** the registry. Turns submitted to the previous process
  answer `404` on their event endpoint. That is not an error condition to be repaired
  by resubmitting to the same sandbox — it is the host's cue to make a requeue
  decision under the rule above.
- The protocol client performs **no business retries**. Timeouts and 5xx are raised
  to the host untouched. A client that silently retried a submission would be making
  a requeue decision it has no information to make, and would be pushing the
  consequences onto the driver's registry.

## 5. Driver lifecycle

- The driver is started as `python -m roost.driver`. It uses only the Python standard
  library, so the sandbox needs nothing installed.
- `ROOST_DRIVER_PORT` selects the port (default `8787`); `0` asks the kernel for a
  free one. Once bound, the driver prints one readiness line to stdout and flushes:

  ```
  roost-driver listening on 127.0.0.1:<port>
  ```

  This line is the only race-free way to learn the port when it was assigned
  dynamically. Callers that pinned the port may instead poll `/v1/health`.
- `ROOST_WORKSPACE_DIR` selects the workspace directory (default `~/workspace`,
  with `~` expanded at driver startup — `/root/workspace` when the sandbox runs as
  root, `/home/user/workspace` on a non-root sandbox such as E2B), and
  the driver makes sure it exists at startup. Failing to create it is a warning on
  stderr, not a startup failure: the driver's job is to accept turns, and a host that
  will not hand out that directory should still get a working control plane (the
  workspace endpoints then answer `500`).
- Turns execute on a **single FIFO worker**. There is no concurrency inside a driver:
  a sandbox hosts one session, and a session runs one turn at a time. The host has
  its own serialisation gate — the redundancy is intentional, since the driver does
  not trust upstream ordering.
- `SIGINT` / `SIGTERM` stop the server and the worker. A turn interrupted by shutdown
  keeps its `running` entry and receives no terminal event: the driver does not
  pretend it can resume across processes, and recovery is the host's requeue
  decision.
- The driver is **restartable in place**. A host may stop it
  (`python -m roost.driver.stop`, which signals every process whose command line
  contains the argument `roost.driver`) and start it again inside a live sandbox,
  keeping the workspace and everything else on disk. This is much cheaper than a cold
  boot, and it is the first rung of a host's escalation ladder.

  Two consequences are expected rather than tolerated. The port rebinds, because the
  old process closed its listener and cancelled its in-flight long polls on `SIGTERM`.
  And the turn registry and event buffers start **empty**: the new process answers
  `404 unknown_turn` for turns the old one ran. That emptiness is exactly what makes
  resubmitting the same turn legitimate — the "never run a turn twice" invariant is a
  property of a single driver process, and a process that has no record of a turn has
  not run it.
- `python -m roost.driver.probe` reports, as one line of JSON, whether the driver
  process tree is doing anything (`{"active": bool, "reason": str, "driver_pid": int
  | null, "processes": [...]}`, read from `/proc`). It exists because both clocks
  above can only see what the driver *reports*; the kernel can see what it is
  actually doing. Exit status is always `0` — "the probe found nothing" is a result,
  not a failure.

## 6. Error responses

| Status | Body | Cause |
|---|---|---|
| `400` | `{"error": "unsupported_protocol_version"}` | version header missing or unrecognised |
| `400` | `{"error": "invalid_body"}` | body is not a valid turn envelope |
| `400` | `{"error": "invalid_query"}` | `after` / `wait_ms` not parseable |
| `400` | `{"error": "unsafe_archive"}` | workspace archive member escapes the directory or is not a file/directory |
| `404` | `{"error": "unknown_turn"}` | this process has no entry for that `turn_id` |
| `404` | `{"error": "not_found"}` | no such endpoint |
| `405` | `{"error": "method_not_allowed"}` | endpoint exists, method does not |
| `500` | `{"error": "workspace_pack_failed"}` | workspace could not be archived |
| `500` | `{"error": "workspace_unpack_failed"}` | workspace archive could not be unpacked |
| `501` | `{"error": "reserved_until_m6"}` | `/v1/update`, reserved |

Clients must treat any other non-`200` status as a transport-level failure and
surface it to the host rather than interpreting it.
