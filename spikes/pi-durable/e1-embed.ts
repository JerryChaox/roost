// E1: one process hosting several conversations at once, on SQLite in an arbitrary directory, models via the fake
// server. Also prints the files created, journal mode, and every commit publication (seq + change types).
import { readdirSync, statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { ANTHROPIC, allEntries, brief, ctx, OPENAI, openHost, out } from "./lib.ts";

const dir = process.argv[2] ?? "work/e1/var/lib/roost/agent";
const db = `${dir}/agent.sqlite`;
const { harness } = await openHost(db);

const ls = (label: string) =>
	out(label, Object.fromEntries(readdirSync(dir).map((f) => [f, statSync(`${dir}/${f}`).size])));
ls("FILES_AFTER_OPEN");

// Q9: every commit, with its storage seq and what changed.
const commits: string[] = [];
harness.subscribeCommits((pub) => {
	const kinds = pub.changes.map((c: any) =>
		c.type === "entry"
			? `entry:${c.value.kind}#${c.value.id}@c${c.value.conversationId}`
			: c.type === "task"
				? `task:${c.value.kind}#${c.value.id}=${c.value.state.status}`
				: c.type === "submission"
					? `sub#${c.value.id}=${c.value.status}`
					: c.type === "document"
						? `doc:${c.kind ?? c.record?.kind ?? "?"}`
						: c.type,
	);
	commits.push(`seq=${pub.seq} ${kinds.join(" ")}`);
});

// Three conversations, two providers, submitted concurrently.
const root = await harness.root(ctx, { agent: { model: ANTHROPIC, thinkingLevel: "high" } });
const c2 = await harness.createConversation({ ownership: { kind: "ownerless" }, agent: { model: OPENAI } }, ctx);
const c3 = await harness.createConversation({ ownership: { kind: "ownerless" }, agent: { model: ANTHROPIC } }, ctx);
out("CONVERSATIONS", [root.id, c2.id, c3.id]);

const t0 = Date.now();
const subs = await Promise.all([
	root.submit({ type: "input", content: "hello from root [slow 4]", requestId: "r-1" }, ctx),
	c2.submit({ type: "input", content: 'openai please [tool slow_tool {"seconds":1}]', requestId: "r-1" }, ctx),
	c3.submit({ type: "input", content: 'anthropic tool [tool bash {"command":"echo hi-from-bash"}]' }, ctx),
]);
out("SUBMITTED", subs.map((s) => s.id));
const settled = await Promise.all(subs.map((s) => s.wait(ctx)));
out("SETTLED_MS", Date.now() - t0);
for (const s of settled) out("SETTLED", s);
for (const c of [root, c2, c3]) {
	out(`ENTRIES c${c.id}`, "");
	for (const e of await allEntries(c)) console.log("   ", brief(e));
}

// Journal mode as seen by a second, independent connection.
const probe = new DatabaseSync(db, { readOnly: true });
out("PRAGMA journal_mode", probe.prepare("PRAGMA journal_mode").get());
out("PRAGMA wal_autocheckpoint (probe conn default)", probe.prepare("PRAGMA wal_autocheckpoint").get());
out("PRAGMA page_size", probe.prepare("PRAGMA page_size").get());
out("TABLES", probe.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map((r: any) => r.name));
out("ROWS", probe.prepare(
	"SELECT (SELECT count(*) FROM conversations) conversations, (SELECT count(*) FROM entries) entries, (SELECT count(*) FROM tasks) tasks, (SELECT count(*) FROM submissions) submissions, (SELECT count(*) FROM documents) documents, (SELECT count(*) FROM document_revisions) revisions, (SELECT next_seq FROM durable_metadata) next_seq",
).get());
probe.close();
ls("FILES_BEFORE_CLOSE");
out("COMMITS", commits.length);
for (const c of commits) console.log("   ", c);
await harness.close(ctx);
ls("FILES_AFTER_CLOSE");
