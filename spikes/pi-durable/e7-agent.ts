// E7 (Q14): where model and thinking level are set: a host default through the creation hook, per conversation
// through `agent`/`configure()`, and what happens with neither.
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { configure, createRegistry, Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { ANTHROPIC, createSpikeModels, ctx, out } from "./lib.ts";

const reasoningOpenAI = openaiProvider().getModels().find((m) => m.reasoning)!;
const OPENAI_R = { provider: "openai", modelId: reasoningOpenAI.id };
const db = process.argv[2];

async function open(withDefault: boolean) {
	const registry = createRegistry();
	registry.install(CodingTools);
	return Harness.open(
		await openNodeSqliteStorage(db),
		{
			models: createSpikeModels(),
			registry,
			settings: { retry: { maxRetries: 0 } },
			// Host default for every new conversation; `agent` given at creation applies after it.
			...(withDefault
				? { conversationCreated: (tx, record) => configure(tx, record.id, { model: ANTHROPIC, thinkingLevel: "high" }) }
				: {}),
		},
		ctx,
	);
}

let harness = await open(true);
const c1 = await harness.createConversation({ ownership: { kind: "ownerless" } }, ctx);
const c2 = await harness.createConversation(
	{ ownership: { kind: "ownerless" }, agent: { model: OPENAI_R, thinkingLevel: "low" } },
	ctx,
);
for (const c of [c1, c2]) {
	const agent = await c.agent(ctx);
	out(`AGENT c${c.id}`, { model: agent.model, thinkingLevel: agent.thinkingLevel, tools: agent.tools.map((t) => t.name), sections: agent.sections.map((s) => s.key) });
	const s = await c.submit({ type: "input", content: `hi from c${c.id}` }, ctx);
	out(`SETTLED c${c.id}`, (await s.wait(ctx)).status);
}
await c1.configure({ thinkingLevel: "minimal" }, ctx);
await (await c1.submit({ type: "input", content: "after configure" }, ctx)).wait(ctx);
await harness.close(ctx);

harness = await open(false);
const c3 = await harness.createConversation({ ownership: { kind: "ownerless" } }, ctx);
out("AGENT c3 (no default)", { model: (await c3.agent(ctx)).model ?? null });
out("SETTLED c3", await (await c3.submit({ type: "input", content: "no model" }, ctx)).wait(ctx));
await harness.close(ctx);
