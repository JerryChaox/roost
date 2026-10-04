// Models outside Pi's catalog (`modelInfo`), and the host default re-applied on every initialize (contracts §13).
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { type Fake, initParams, startFake, startHost, tempDir } from "./helpers.ts";

let fake: Fake;
before(async () => {
	fake = await startFake();
});
after(() => fake.stop());

const GATEWAY_MODEL = "anthropic/deepseek/deepseek-v4-flash";
const GATEWAY_INFO = { contextWindow: 1_000_000, maxOutputTokens: 32_768, reasoning: true, input: ["text"] };

/** The thinking setting of the fake server's requests whose newest user text is `text`. */
function thinkingFor(text: string): unknown[] {
	const requests = fake.requests.filter((request) => request.last === "user" && request.text === text);
	assert.ok(requests.length > 0, `no request for ${text}`);
	return requests.map((request) => request.thinking);
}

/** The model the fake server was asked for by the run that answered `requestId`. */
async function answeredBy(host: any, conversation: string, requestId: string): Promise<string> {
	const entries = await host.allEntries(conversation);
	const answers = entries.filter((entry: any) => entry.kind === "assistant" && entry.run === requestId);
	assert.ok(answers.length > 0, `no answer for ${requestId}`);
	const models = new Set(answers.map((entry: any) => `${entry.raw.model[0].provider}/${entry.raw.model[0].model}`));
	assert.equal(models.size, 1);
	return [...models][0] as string;
}

test("a model outside Pi's catalog needs modelInfo, then runs with tool calls", async (t) => {
	const without = tempDir(t);
	const refused = await startHost(without, initParams(fake, without, { model: GATEWAY_MODEL }));
	assert.equal(refused.reply.error?.code, -32602);
	assert.match(refused.reply.error!.message, /modelInfo/);
	assert.notEqual(await refused.host.exited, 0);

	const paths = tempDir(t);
	const { host, reply } = await startHost(
		paths,
		initParams(fake, paths, { model: GATEWAY_MODEL, thinking: "high", modelInfo: GATEWAY_INFO }),
	);
	t.after(() => host.shutdown());
	assert.ok(reply.result, JSON.stringify(reply));
	const { conversation } = (await host.request("POST", "/v1/conversations", {})).body;
	const seen = fake.requests.length;
	await host.request("POST", `/v1/conversations/${conversation}/messages`, {
		requestId: "g-1",
		content: 'go [tool bash {"command":"echo via-gateway"}]',
	});
	await host.idle(conversation);
	const entries = await host.allEntries(conversation);
	const result = entries.find((entry: any) => entry.kind === "tool_result");
	assert.match(JSON.stringify(result.raw.model[0].content), /via-gateway/);
	const last = entries.at(-1);
	assert.deepEqual([last.kind, last.run, last.raw.model[0].stopReason], ["assistant", "g-1", "stop"]);
	assert.equal(await answeredBy(host, conversation, "g-1"), GATEWAY_MODEL);
	const requests = fake.requests.slice(seen);
	assert.equal(requests.length, 2);
	for (const request of requests) {
		assert.equal(request.model, "deepseek/deepseek-v4-flash");
		assert.match(request.url, /^\/v1\/messages/);
		assert.equal(request.auth, "placeholder-key");
		assert.ok(request.thinking, "reasoning: true sends thinking");
	}

	// The same through settings: a conversation's own non-catalog model needs modelInfo too.
	const bad = await host.request("POST", "/v1/conversations", { settings: { model: "anthropic/other-gateway-model" } });
	assert.deepEqual([bad.status, bad.body.error], [400, "invalid_request"]);
	const own = await host.request("POST", "/v1/conversations", {
		settings: { model: "anthropic/other-gateway-model", modelInfo: { ...GATEWAY_INFO, reasoning: false } },
	});
	assert.equal(own.status, 200);
	await host.request("POST", `/v1/conversations/${own.body.conversation}/messages`, { requestId: "o-1", content: "hi" });
	await host.idle(own.body.conversation);
	assert.equal(await answeredBy(host, own.body.conversation, "o-1"), "anthropic/other-gateway-model");
});

test("a changed default reaches existing conversations after a restart; an own model is kept", async (t) => {
	const paths = tempDir(t);
	let { host } = await startHost(paths, initParams(fake, paths, { model: "anthropic/claude-haiku-4-5" }));
	const first = host;
	t.after(() => first.kill());
	const follows = (await host.request("POST", "/v1/conversations", { key: "follows" })).body.conversation;
	const owns = (
		await host.request("POST", "/v1/conversations", { key: "owns", settings: { model: "anthropic/claude-sonnet-4-5" } })
	).body.conversation;
	for (const [conversation, content] of [
		[follows, "follows before"],
		[owns, "owns before"],
	]) {
		await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "before", content });
		await host.idle(conversation);
	}
	// thinking "off" (the first default) disables thinking.
	for (const text of ["follows before", "owns before"]) assert.deepEqual(thinkingFor(text), [{ type: "disabled" }]);
	assert.equal(await answeredBy(host, follows, "before"), "anthropic/claude-haiku-4-5");
	assert.equal(await answeredBy(host, owns, "before"), "anthropic/claude-sonnet-4-5");
	await host.shutdown();

	({ host } = await startHost(
		paths,
		initParams(fake, paths, { model: GATEWAY_MODEL, thinking: "high", modelInfo: GATEWAY_INFO }),
	));
	t.after(() => host.shutdown());
	for (const [conversation, content] of [
		[follows, "follows after"],
		[owns, "owns after"],
	]) {
		await host.request("POST", `/v1/conversations/${conversation}/messages`, { requestId: "after", content });
		await host.idle(conversation);
	}
	// The new default thinking level ("high") reaches both, the one with its own model included.
	for (const text of ["follows after", "owns after"]) {
		for (const thinking of thinkingFor(text)) {
			assert.ok(thinking !== null && (thinking as { type?: string }).type !== "disabled", `${text}: ${JSON.stringify(thinking)}`);
		}
	}
	assert.equal(await answeredBy(host, follows, "after"), GATEWAY_MODEL);
	assert.equal(await answeredBy(host, owns, "after"), "anthropic/claude-sonnet-4-5");
});
