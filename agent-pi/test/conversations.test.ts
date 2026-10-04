// Idempotent admission and create-or-get across a host restart (contracts §16 scenario 1), and forward paging.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { type Fake, initParams, startFake, startHost, tempDir } from "./helpers.ts";

let fake: Fake;
before(async () => {
	fake = await startFake();
});
after(() => fake.stop());

test("same key and same requestId survive a restart: same conversation, 200 duplicate", async (t) => {
	const paths = tempDir(t);
	let { host } = await startHost(paths, initParams(fake, paths));

	const created = await host.request("POST", "/v1/conversations", { key: "slack:C1:1.0" });
	assert.equal(created.status, 200);
	const conversation = created.body.conversation;
	assert.match(conversation, /^c_[0-9]+$/);
	assert.equal((await host.request("POST", "/v1/conversations", { key: "slack:C1:1.0" })).body.conversation, conversation);
	const other = (await host.request("POST", "/v1/conversations", { key: "slack:C1:2.0" })).body.conversation;
	assert.notEqual(other, conversation);

	const path = `/v1/conversations/${conversation}/messages`;
	const first = await host.request("POST", path, { requestId: "m-1", content: "first" });
	assert.equal(first.status, 202);
	assert.equal(first.body.status, "running");
	const again = await host.request("POST", path, { requestId: "m-1", content: "different content" });
	assert.deepEqual([again.status, again.body], [200, { status: "duplicate" }]);
	await host.idle(conversation);
	await host.shutdown();

	({ host } = await startHost(paths, initParams(fake, paths)));
	t.after(() => host.shutdown());
	assert.equal((await host.request("POST", "/v1/conversations", { key: "slack:C1:1.0" })).body.conversation, conversation);
	const listed = await host.request("GET", `/v1/conversations?key=${encodeURIComponent("slack:C1:1.0")}`);
	assert.deepEqual(listed.body, { conversations: [{ conversation, key: "slack:C1:1.0", active: false }], next: null });
	const afterRestart = await host.request("POST", path, { requestId: "m-1", content: "first" });
	assert.deepEqual([afterRestart.status, afterRestart.body], [200, { status: "duplicate" }]);

	// Answered once: one user entry for m-1.
	const users = (await host.allEntries(conversation)).filter((entry) => entry.kind === "user");
	assert.equal(users.length, 1);
	assert.equal(users[0].run, "m-1");
});

test("entries page forward with stable cursors, also after a restart", async (t) => {
	const paths = tempDir(t);
	let { host } = await startHost(paths, initParams(fake, paths));
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;
	for (const [index, content] of ["one", 'two [tool bash {"command":"echo two"}]', "three"].entries()) {
		await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: `p-${index}`, content });
		await host.idle(conversation);
	}
	const all = await host.allEntries(conversation);
	assert.ok(all.length >= 8, `expected a few entries, got ${all.length}`);
	const cursors = all.map((entry) => Number(entry.cursor));
	assert.deepEqual(cursors, [...cursors].sort((a, b) => a - b));
	assert.equal(new Set(cursors).size, cursors.length);
	for (const entry of all) {
		assert.ok(["user", "assistant", "tool_result", "system"].includes(entry.kind), entry.kind);
		assert.match(entry.run, /^p-[0-2]$/);
		assert.equal(entry.raw.id, Number(entry.cursor));
	}

	const walk = async () => {
		const seen: any[] = [];
		let after: string | null = null;
		do {
			const page: { body: { entries: any[]; next: string | null } } = await host.request(
				"GET",
				`/v1/conversations/${conversation}/entries?limit=3${after === null ? "" : `&after=${after}`}`,
			);
			assert.ok(page.body.entries.length <= 3);
			if (page.body.next !== null) assert.equal(page.body.next, page.body.entries.at(-1).cursor);
			seen.push(...page.body.entries);
			after = page.body.next;
		} while (after !== null);
		return seen;
	};
	assert.deepEqual(await walk(), all);
	// Any cursor can be read again; it returns exactly what follows it.
	const middle = all[3].cursor;
	const fromMiddle = await host.request("GET", `/v1/conversations/${conversation}/entries?after=${middle}&limit=2`);
	assert.deepEqual(fromMiddle.body.entries, all.slice(4, 6));
	const last = await host.request("GET", `/v1/conversations/${conversation}/entries?after=${all.at(-1).cursor}`);
	assert.deepEqual(last.body, { entries: [], next: null });

	await host.shutdown();
	({ host } = await startHost(paths, initParams(fake, paths)));
	t.after(() => host.shutdown());
	assert.deepEqual(await walk(), all);
});
