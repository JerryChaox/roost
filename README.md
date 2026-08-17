# roost

Durable, exactly-once agent sessions on disposable sandboxes.

A session is a migratory bird: it can perch anywhere and stay itself. The sandbox
is just tonight's roost — destroy it, replace it, upgrade it, and the session
lives on.

## Why

E2B, Modal and friends solve "give me a sandbox". They don't solve what a
message-triggered agent actually needs from one:

- Your queue delivers at-least-once, so the same message will eventually arrive
  twice — and an agent that answers twice is broken in a way users notice.
- Sandboxes die, get paused, get garbage-collected. The conversation must not.
- You will ship a new runtime version while conversations are in flight, and
  "please start a new chat" is not an upgrade strategy.
- Sometimes the agent process inside just hangs, and something has to notice,
  kill it, and get the turn answered anyway.

roost is that layer. It sits between your delivery queue and your sandbox
provider, and holds three invariants:

| Invariant | Meaning | Proof |
|---|---|---|
| **Exactly-once** | At-least-once delivery in, exactly one execution out. Deterministic turn ids, a CAS turn ledger on the host, and an idempotent turn registry inside the sandbox — re-runs are only ever legal on a fresh sandbox after an explicit requeue. | `cli_chat.py --duplicate` |
| **Durability** | The sandbox is a cache; the workspace snapshot is the truth. Kill the container mid-conversation and the next message rebuilds it, state intact. | `cli_chat.py --counter` + `/kill` |
| **Never worse off** | A stalled sandbox is detected, killed, and the turn re-answered on a fresh one. A runtime upgrade replaces the sandbox under the conversation via live snapshot and atomic rebind — and if the upgrade fails at any step, the old sandbox keeps answering as if nothing happened. | `cli_chat.py --hang-first`; forced-update e2e tests |

Everything above runs against a real local Docker daemon in this repo's test
suite (200+ tests, CI on every push).

## Try it

Requires Python ≥ 3.11 and a running Docker daemon. The core library has zero
runtime dependencies.

```bash
git clone https://github.com/JerryChaox/roost && cd roost
python -m venv .venv && .venv/bin/pip install -e .

# Demo 1 — exactly-once: every message is delivered twice, answered once.
.venv/bin/python examples/cli_chat.py --duplicate

# Demo 2 — durability: the counter survives you destroying the sandbox.
.venv/bin/python examples/cli_chat.py --counter --snapshot-dir /tmp/roost-snap
#   you> tick            →  agent> tick counter=1
#   you> /kill           →  docker rm -f <sandbox>
#   you> tick            →  agent> tick counter=2   (fresh container, restored state)

# Demo 3 — stall recovery: first attempt hangs inside the sandbox; the
# watchdog kills it, requeues, and the answer arrives from a fresh sandbox.
.venv/bin/python examples/cli_chat.py --hang-first --stall-timeout 6 --lock-seconds 2
```

The demo agent is a deliberately boring echo harness — the point of these demos
is the runtime semantics around it, which don't care what the agent is. The
harness interface is pluggable; a Claude Agent SDK harness is on the roadmap
before 0.1.

## How it sits in your stack

```
your app            identity, routing, rendering, storage choice
──── six ports ────────────────────────────────────────────────
roost (host side)   session↔sandbox registry · turn pipeline ·
                    watchdog · event reducer · forced update
──── control protocol (loopback HTTP, PROTOCOL.md) ────────────
roost driver        turn registry (idempotency) · harness runner ·
(inside sandbox)    event log · workspace pack/restore
──── harness ──────────────────────────────────────────────────
your agent          Claude Agent SDK, or anything with a run() loop
```

You inject six small interfaces — delivery queue, state store, snapshot store,
sandbox backend, event sink, session context — and roost owns the lifecycle
between them. Defaults ship for local use (in-process queue, SQLite, filesystem
or S3-compatible snapshots, Docker sandboxes); swap any of them for your infra.
The library never sees your domain: sessions, turns and snapshot keys are opaque
strings, and host context rides through as an uninterpreted blob.

## Status

Pre-release, interfaces stabilizing. Implemented and tested today: the turn
pipeline, SQLite state store, in-process delivery, the driver and control
protocol, Docker backend, filesystem/S3 snapshot stores, watchdog stall
recovery, and fingerprint-driven zero-downtime updates. Not yet: an LLM harness
(echo only), the E2B backend, PyPI packaging. See [ROADMAP.md](ROADMAP.md).

## Read next

- [DESIGN.md](DESIGN.md) — the mental model and the three invariants.
- [PROTOCOL.md](PROTOCOL.md) — the wire contract, including the idempotency split
  between host and driver.
- [CONTRACTS.md](CONTRACTS.md) — pinned interfaces and the adjudication log of
  every design decision made while porting this from a production system.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
