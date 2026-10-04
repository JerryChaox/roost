// Control channel: initialize validation, quiesce/resume admission, and the storage lock (contracts §16 scenario 6).
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { existsSync, type Fake, initParams, join, startFake, startHost, tempDir, until } from "./helpers.ts";

let fake: Fake;
before(async () => {
	fake = await startFake();
});
after(() => fake.stop());

const badAgents: [string, Record<string, unknown>][] = [
	["model without a provider", { model: "claude-haiku-4-5" }],
	["unknown provider", { model: "nosuchprovider/some-model" }],
	["model not in Pi's catalog", { model: "anthropic/no-such-model" }],
	["unknown thinking level", { thinking: "extreme" }],
];

for (const [name, agent] of badAgents) {
	test(`initialize rejects ${name}, exits non-zero, and opens no storage`, async (t) => {
		const paths = tempDir(t);
		const { host, reply } = await startHost(paths, initParams(fake, paths, agent));
		assert.equal(reply.error?.code, -32602, JSON.stringify(reply));
		assert.notEqual(await host.exited, 0);
		assert.equal(existsSync(join(paths.storage, "agent.sqlite")), false);
	});
}

test("initialize rejects restoredFrom", async (t) => {
	const paths = tempDir(t);
	const params = { ...initParams(fake, paths), restoredFrom: { snapshot: "snap_1" } };
	const { host, reply } = await startHost(paths, params);
	assert.equal(reply.error?.code, -32602);
	assert.notEqual(await host.exited, 0);
});

test("quiesce makes new messages 503 not_ready; resume admits them again", async (t) => {
	const paths = tempDir(t);
	const { host, reply } = await startHost(paths, initParams(fake, paths));
	t.after(() => host.shutdown());
	assert.deepEqual(reply.result, {
		agent: "pi-durable",
		version: "1.0.2",
		durability: "step",
		capabilities: ["steer", "reset_note"],
	});
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;

	assert.deepEqual((await host.rpc("quiesce")).result, { running: 0 });
	const refused = await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "q-1", content: "hi" });
	assert.equal(refused.status, 503);
	assert.deepEqual(refused.body, { error: "not_ready", detail: "draining" });

	assert.deepEqual((await host.rpc("resume")).result, { running: 0 });
	const admitted = await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "q-1", content: "hi" });
	assert.equal(admitted.status, 202);
	await host.idle(conversation);
	const entries = await host.allEntries(conversation);
	assert.ok(entries.some((entry) => entry.kind === "assistant" && entry.run === "q-1"));
});

test("a second host on the same storage cannot take the lock and exits; the first keeps working", async (t) => {
	const paths = tempDir(t);
	const first = await startHost(paths, initParams(fake, paths));
	t.after(() => first.host.shutdown());
	assert.ok(first.reply.result);

	const otherSocket = join(paths.dir, "b.sock");
	const second = await startHost({ ...paths, socket: otherSocket }, initParams(fake, { ...paths, socket: otherSocket }));
	assert.equal(second.reply.error?.code, -32000, JSON.stringify(second.reply));
	assert.match(second.reply.error!.message, /locked/);
	assert.notEqual(await second.host.exited, 0);
	// It stopped at the lock, before Pi storage: it never listened.
	assert.equal(existsSync(otherSocket), false);

	const { conversation } = (await first.host.request("POST", "/v1/conversations", {})).body;
	const sent = await first.host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "x-1", content: "still here" });
	assert.equal(sent.status, 202);
	await until("answer", async () =>
		(await first.host.allEntries(conversation)).some((entry) => entry.kind === "assistant" && entry.run === "x-1"),
	);
});
