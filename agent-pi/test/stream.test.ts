// SSE: replay after a cursor, then live; no duplicates or gaps; `live` events carry no id.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { type Fake, initParams, SseClient, startFake, startHost, tempDir, until } from "./helpers.ts";

let fake: Fake;
before(async () => {
	fake = await startFake();
});
after(() => fake.stop());

test("stream replays entries after the cursor, then live output and run status without gaps", async (t) => {
	const paths = tempDir(t);
	const { host } = await startHost(paths, initParams(fake, paths));
	t.after(() => host.shutdown());
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;
	await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "s-1", content: "earlier" });
	await host.idle(conversation);
	const before = await host.allEntries(conversation);
	const cursor = before[1].cursor;

	const stream = new SseClient(host.socket, `/v1/conversations/${conversation}/stream?after=${cursor}`);
	t.after(() => stream.close());
	await until("replay", async () => stream.events.filter((event) => event.event === "entry").length >= before.length - 2);

	await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "s-2", content: "talk slowly [slow 4]" });
	await until("run end", async () =>
		stream.events.some((event) => event.event === "run" && event.data.run === "s-2" && event.data.status === "completed"),
	);
	const all = await host.allEntries(conversation);
	await until("every entry", async () => stream.events.filter((event) => event.event === "entry").length >= all.length - 2);

	const entries = stream.events.filter((event) => event.event === "entry");
	// Exactly the entries after the cursor, in order, each once, with the SSE id equal to the cursor.
	assert.deepEqual(
		entries.map((event) => event.id),
		all.slice(2).map((entry) => entry.cursor),
	);
	assert.deepEqual(
		entries.map((event) => event.data),
		all.slice(2),
	);

	const live = stream.events.filter((event) => event.event === "live");
	assert.ok(live.length > 0, "expected live output");
	for (const event of live) {
		assert.equal(event.id, undefined);
		assert.doesNotMatch(event.raw, /^id:/m);
		assert.equal(event.data.run, "s-2");
		assert.equal(event.data.type, "text");
	}
	assert.match(live.map((event) => event.data.delta).join(""), /chunk0 chunk1/);

	const runs = stream.events.filter((event) => event.event === "run").map((event) => event.data);
	assert.deepEqual(runs, [
		{ run: "s-2", status: "running" },
		{ run: "s-2", status: "completed" },
	]);
	for (const event of stream.events.filter((event) => event.event !== "entry")) assert.equal(event.id, undefined);
});

test("Last-Event-ID wins over after", async (t) => {
	const paths = tempDir(t);
	const { host } = await startHost(paths, initParams(fake, paths));
	t.after(() => host.shutdown());
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;
	await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "l-1", content: "hello" });
	await host.idle(conversation);
	const all = await host.allEntries(conversation);
	const stream = new SseClient(host.socket, `/v1/conversations/${conversation}/stream?after=0`, {
		"last-event-id": all[1].cursor,
	});
	t.after(() => stream.close());
	await until("replay", async () => stream.events.length >= all.length - 2);
	assert.deepEqual(
		stream.events.map((event) => event.id),
		all.slice(2).map((entry) => entry.cursor),
	);
});
