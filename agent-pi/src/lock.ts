// The agent host's own exclusive lock on agent storage (driver-protocol §2 step 6).
// Node has no flock, so the lock is a write transaction held on a dedicated SQLite file for the process lifetime:
// rollback journal (not WAL), `locking_mode=EXCLUSIVE`, `BEGIN EXCLUSIVE`, busy timeout 0. A second process gets
// SQLITE_BUSY at once. The OS releases the file lock when the process dies.
import { DatabaseSync } from "node:sqlite";

export class StorageLocked extends Error {}

export type HostLock = { release(): void };

export function takeHostLock(path: string): HostLock {
	const db = new DatabaseSync(path, { timeout: 0 });
	try {
		db.exec("PRAGMA busy_timeout = 0");
		db.exec("PRAGMA locking_mode = EXCLUSIVE");
		db.exec("PRAGMA journal_mode = DELETE");
		db.exec("BEGIN EXCLUSIVE");
	} catch (error) {
		db.close();
		const text = String(error);
		if (/SQLITE_BUSY|database is locked/i.test(text) || (error as { errcode?: number }).errcode === 5) {
			throw new StorageLocked(`agent storage is locked by another agent host (${path})`);
		}
		throw error;
	}
	let released = false;
	return {
		release() {
			if (released) return;
			released = true;
			try {
				db.exec("ROLLBACK");
			} catch {}
			db.close();
		},
	};
}
