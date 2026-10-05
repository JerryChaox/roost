// E6: read-only open (Q13) and WAL / checkpoint behaviour with and without an external reader (Q3).
import { chmodSync, copyFileSync, existsSync, mkdirSync, openSync, readSync, closeSync, rmSync, statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { ROOT_CONVERSATION_ID } from "@earendil-works/pi-durable";
import { SqliteStorage } from "@earendil-works/pi-durable/storage/sqlite";
import { NodeSqliteDatabase, openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { ANTHROPIC, ctx, openHost, out } from "./lib.ts";

const [phase, src] = process.argv.slice(2);

/** WAL header: checkpoint sequence and salts change whenever SQLite restarts the WAL from the beginning. */
function walHeader(path: string) {
	if (!existsSync(path)) return { exists: false };
	const fd = openSync(path, "r");
	const b = Buffer.alloc(32);
	const n = readSync(fd, b, 0, 32, 0);
	closeSync(fd);
	if (n < 32) return { exists: true, bytes: statSync(path).size };
	return { bytes: statSync(path).size, ckptSeq: b.readUInt32BE(12), salt1: b.readUInt32BE(16) };
}

if (phase === "readonly") {
	const dir = "work/e6/ro";
	rmSync(dir, { recursive: true, force: true });
	mkdirSync(dir, { recursive: true });
	copyFileSync(src, `${dir}/agent.sqlite`);
	chmodSync(`${dir}/agent.sqlite`, 0o444);
	// (a) Pi's storage over a read-only connection.
	try {
		const ro = new DatabaseSync(`${dir}/agent.sqlite`, { readOnly: true });
		const storage = await SqliteStorage.open(new NodeSqliteDatabase(ro));
		out("RO (a) SqliteStorage.open over readOnly connection", "opened");
		await storage.close(ctx);
	} catch (error) {
		out("RO (a) SqliteStorage.open over readOnly connection FAILED", String(error));
	}
	// (b) the normal opener on a read-only file.
	try {
		const storage = await openNodeSqliteStorage(`${dir}/agent.sqlite`);
		out("RO (b) openNodeSqliteStorage on a 0444 file", "opened");
		await storage.close(ctx);
	} catch (error) {
		out("RO (b) openNodeSqliteStorage on a 0444 file FAILED", String(error));
	}
	// (c) plain SQL on the same read-only connection: the read model a host could serve list/entries from.
	const ro = new DatabaseSync(`${dir}/agent.sqlite`, { readOnly: true });
	out("RO (c) raw SQL conversations", ro.prepare("SELECT id, record FROM conversations").all());
	out(
		"RO (c) raw SQL entries after #20, ascending",
		ro.prepare("SELECT id, commit_seq, json_extract(record,'$.kind') kind FROM entries WHERE conversation_id = 3 AND id > 20 ORDER BY id LIMIT 4").all(),
	);
	ro.close();
	// (d) a writable copy: open, then everything already pending would be flipped/resumed. Opening writes.
	copyFileSync(src, `${dir}/copy.sqlite`);
	const before = statSync(`${dir}/copy.sqlite`).mtimeMs;
	const s = await openNodeSqliteStorage(`${dir}/copy.sqlite`);
	await s.close(ctx);
	out("RO (d) opening a closed copy with Pi rewrites it", { mtimeChanged: statSync(`${dir}/copy.sqlite`).mtimeMs !== before });
} else if (phase === "wal") {
	const autockpt = Number(process.argv[4] ?? 1000);
	const dir = `work/e6/wal-${autockpt}`;
	rmSync(dir, { recursive: true, force: true });
	const db = `${dir}/agent.sqlite`;
	const { harness } = await openHost(db, { storage: { walAutoCheckpointPages: autockpt } });
	const root = await harness.root(ctx, { agent: { model: ANTHROPIC } });
	const probe = new DatabaseSync(db, { readOnly: true });
	out("sqlite_version", probe.prepare("SELECT sqlite_version() v").get());
	const blob = "x".repeat(3000);
	const writes = async (n: number) => {
		for (let i = 0; i < n; i++)
			await harness.commit((tx) => tx.appendEntry(ROOT_CONVERSATION_ID, { kind: "roost.note", data: { i, blob } }), ctx);
	};
	out("WAL start", walHeader(`${db}-wal`));
	for (let round = 1; round <= 4; round++) {
		await writes(400);
		out(`WAL after ${round * 400} commits`, walHeader(`${db}-wal`));
	}
	// External reader holds a read transaction open while the host keeps committing.
	probe.exec("BEGIN");
	const seen = probe.prepare("SELECT count(*) n FROM entries").get();
	out("READER holds a read txn; entries it sees", seen);
	for (let round = 1; round <= 3; round++) {
		await writes(400);
		out(`WAL with reader open, +${round * 400} commits`, walHeader(`${db}-wal`));
	}
	out("READER still sees (snapshot isolation)", probe.prepare("SELECT count(*) n FROM entries").get());
	probe.exec("COMMIT");
	await writes(400);
	out("WAL after reader released, +400 commits", walHeader(`${db}-wal`));
	// Close with the reader holding a read transaction: close runs wal_checkpoint(TRUNCATE).
	probe.exec("BEGIN");
	probe.prepare("SELECT count(*) n FROM entries").get();
	const t0 = Date.now();
	try {
		await harness.close(ctx);
		out("CLOSE with reader open: ms", Date.now() - t0);
	} catch (error) {
		out("CLOSE with reader open FAILED after ms", { ms: Date.now() - t0, error: String(error) });
	}
	out("WAL after close (reader still open)", walHeader(`${db}-wal`));
	probe.exec("COMMIT");
	probe.close();
	out("WAL after reader closed", walHeader(`${db}-wal`));
}
