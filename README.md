# roost

Durable, exactly-once agent sessions on disposable sandboxes.

A session is a migratory bird: it can perch anywhere and stay itself. The sandbox is
just tonight's roost — destroy it, replace it, upgrade it, and the session lives on.

roost is not another way to start a sandbox (E2B, Modal and friends already do that).
It is the session runtime semantics that sit *above* the sandbox: keeping a
message-triggered agent behaving like a long-lived process that never loses a
message, never executes one twice, and can be upgraded underneath itself.

## Status: design phase

**Not yet functional — interfaces only.** This repository currently contains the
design, the pinned contracts, and a typed skeleton of the public types and host
ports. There is no queue, no state store, no sandbox backend, no driver, and no
runtime behavior of any kind yet.

## Read next

- [DESIGN.md](DESIGN.md) — mental model, invariants, layering, control protocol.
- [CONTRACTS.md](CONTRACTS.md) — the pinned port signatures and core types; the
  single source of truth for the skeleton in `src/roost/`.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
