// E8 (Q9): the commit hook, its `seq`, and what an outside reader of the SQLite file sees at that moment.
import { statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { ANTHROPIC, ctx, openHost, out } from "./lib.ts";

const db = process.argv[2];
const { harness } = await openHost(db);
const root = await harness.root(ctx, { agent: { model: ANTHROPIC } });
const probe = new DatabaseSync(db, { readOnly: true });
const meta = probe.prepare("SELECT next_seq FROM durable_metadata");
const frames = () => (statSync(`${db}-wal`).size - 32) / (4096 + 24);

harness.subscribeCommits((pub) => {
	// Spike only: a synchronous read from another connection inside the listener, to compare positions.
	const steps = (pub.changes as any[])
		.filter((c) => c.type === "entry" || (c.type === "task" && c.value.state.status === "terminal"))
		.map((c) => (c.type === "entry" ? `entry ${c.value.kind}#${c.value.id}` : `task ${c.value.kind}#${c.value.id} terminal`));
	if (steps.length === 0) return;
	out("COMMIT", { seq: pub.seq, externalNextSeq: (meta.get() as any).next_seq, walFrames: frames(), steps });
});

await (await root.submit({ type: "input", content: 'go [tool bash {"command":"echo step"}]' }, ctx)).wait(ctx);
const rows = probe.prepare("SELECT id, commit_seq, json_extract(record,'$.kind') kind FROM entries ORDER BY id").all();
out("ENTRIES with commit_seq", rows);
probe.close();
await harness.close(ctx);
