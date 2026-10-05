// Delivery while a run is going (contracts §16 scenario 2) and a host killed mid-tool-call (scenario 3).
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { existsSync, type Fake, initParams, join, startFake, startHost, tempDir, until } from "./helpers.ts";

let fake: Fake;
before(async () => {
	fake = await startFake();
});
after(() => fake.stop());

const text = (entry: any): string => {
	const message = entry.raw.model?.[0];
	if (message === undefined) return "";
	if (typeof message.content === "string") return message.content;
	return message.content.map((block: any) => block.text ?? "").join("");
};

test("steer joins the running run; follow_up is answered in the next run", async (t) => {
	const paths = tempDir(t);
	const { host } = await startHost(paths, initParams(fake, paths));
	t.after(() => host.shutdown());
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;
	const path = `/v1/conversations/${conversation}/messages`;

	const started = join(paths.workdir, "started");
	await host.request("POST", path, { requestId: "b-1", content: 'go [tool bash {"command":"touch started; sleep 2"}]' });
	await until("tool running", async () => existsSync(started));

	const steer = await host.request("POST", path, { requestId: "b-2", content: "STEER-1", whenBusy: "steer" });
	const follow = await host.request("POST", path, { requestId: "b-3", content: "FOLLOW-1" });
	assert.deepEqual([steer.status, steer.body], [202, { status: "queued" }]);
	assert.deepEqual([follow.status, follow.body], [202, { status: "queued" }]);

	const state = (await host.rpc("state")).result;
	const rows = state.runs.filter((row: any) => row.conversation === conversation);
	assert.deepEqual(
		rows.map((row: any) => [row.run, row.status]),
		[
			["b-1", "running"],
			["b-3", "queued"],
		],
	);
	for (const row of rows) assert.ok(!Number.isNaN(Date.parse(row.lastProgressAt)));
	assert.ok(state.lastProgressAt !== null);

	await host.idle(conversation);
	const entries = await host.allEntries(conversation);
	const steered = entries.find((entry) => entry.kind === "user" && text(entry) === "STEER-1");
	const followed = entries.find((entry) => entry.kind === "user" && text(entry) === "FOLLOW-1");
	assert.equal(steered.run, "b-1", "the steer joins the running run");
	assert.equal(followed.run, "b-3", "the follow-up starts its own run");
	// b-1 kept going after the steer: it answered once more before the follow-up's run began.
	const afterSteer = entries.filter(
		(entry) => Number(entry.cursor) > Number(steered.cursor) && Number(entry.cursor) < Number(followed.cursor),
	);
	assert.ok(afterSteer.some((entry) => entry.kind === "assistant" && entry.run === "b-1"));
	const last = entries.at(-1);
	assert.deepEqual([last.kind, last.run, text(last)], ["assistant", "b-3", "echo: FOLLOW-1"]);
	assert.ok(entries.every((entry) => entry.run === "b-1" || entry.run === "b-3"));
});

test("a host killed mid-tool-call is replaced on the same storage and the run continues", async (t) => {
	const paths = tempDir(t);
	let { host } = await startHost(paths, initParams(fake, paths));
	const { conversation } = (await host.request("POST", "/v1/conversations", { key: "crash" })).body;
	const started = join(paths.workdir, "started");
	await host.request("POST", `/v1/conversations/${conversation}/messages`, {
		requestId: "k-1",
		content: 'run it [tool bash {"command":"touch started; sleep 3; echo finished"}]',
	});
	await until("tool running", async () => existsSync(started));
	await host.kill();

	({ host } = await startHost(paths, initParams(fake, paths)));
	t.after(() => host.shutdown());
	await host.idle(conversation);
	const entries = await host.allEntries(conversation);
	assert.ok(entries.every((entry) => entry.run === "k-1"), JSON.stringify(entries.map((entry) => [entry.kind, entry.run])));
	const result = entries.find((entry) => entry.kind === "tool_result");
	assert.ok(result, "the interrupted call has a result");
	// Pi does not rerun an unsafe tool (bash) after a crash; it reports the call as interrupted and the run goes on.
	assert.ok(result.raw.data.diagnostics.some((diagnostic: any) => diagnostic.code === "interrupted"), JSON.stringify(result.raw));
	assert.equal(result.raw.model[0].isError, true);
	const last = entries.at(-1);
	assert.equal(last.kind, "assistant");
	assert.equal(last.raw.model[0].stopReason, "stop");
	assert.match(text(last), /^tool said: /);
	// The same message after the restart is still a duplicate.
	const again = await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "k-1", content: "x" });
	assert.deepEqual([again.status, again.body], [200, { status: "duplicate" }]);
});
