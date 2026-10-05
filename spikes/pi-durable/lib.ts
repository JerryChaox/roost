// Shared host setup for the spike scripts: one Harness over one SQLite file, providers pointed at the fake server.
import { appendFileSync } from "node:fs";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { Type } from "@earendil-works/pi-ai";
import { createModels, type Provider } from "@earendil-works/pi-ai/models";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import {
	createRegistry,
	defineExtension,
	defineTool,
	Harness,
	type HarnessSettings,
	section,
} from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage, type NodeSqliteStorageOptions } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";

export const ctx = BACKGROUND_CONTEXT;
export const FAKE = process.env.FAKE_URL ?? "http://127.0.0.1:18080";
export const ANTHROPIC = { provider: "anthropic", modelId: "claude-haiku-4-5" } as const;
export const OPENAI = { provider: "openai", modelId: "gpt-4.1-mini" } as const;

export const out = (tag: string, value: unknown = ""): void => {
	console.log(`${tag} ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

/**
 * Q5: base URL and key per provider, set in code. The provider keeps its models and API implementation; only its
 * auth is replaced by one that returns the placeholder key and the base URL (pi-ai models.ts:859-863 applies
 * `auth.baseUrl` to the request model).
 */
export function pointAt<P extends Provider>(provider: P, baseUrl: string, key: string): P {
	return {
		...provider,
		auth: {
			apiKey: {
				name: "roost initialize",
				resolve: async () => ({ auth: { apiKey: key, baseUrl }, source: "roost initialize" }),
			},
		},
	};
}

export function createSpikeModels(baseUrls = { anthropic: FAKE, openai: `${FAKE}/v1` }, key = "placeholder-key") {
	const models = createModels();
	models.setProvider(pointAt(anthropicProvider(), baseUrls.anthropic, `${key}-anthropic`));
	models.setProvider(pointAt(openaiProvider(), baseUrls.openai, `${key}-openai`));
	return models;
}

const sleep = (ms: number, signal?: AbortSignal) =>
	new Promise<void>((resolve, reject) => {
		const t = setTimeout(resolve, ms);
		signal?.addEventListener("abort", () => {
			clearTimeout(t);
			reject(signal.reason ?? new Error("aborted"));
		});
	});

/** A tool that runs for `seconds`, printing progress; `replay` default (unsafe). */
export const slowTool = defineTool({
	name: "slow_tool",
	description: "Sleep for some seconds",
	parameters: Type.Object({ seconds: Type.Number() }),
	execute: async (args, api, context) => {
		out("TOOL_STARTED", { tool: "slow_tool", callId: api.callId, taskId: api.taskId, pid: process.pid });
		if (process.env.EXEC_LOG) appendFileSync(process.env.EXEC_LOG, `slow_tool ${api.callId} pid=${process.pid}\n`);
		for (let i = 0; i < args.seconds * 5; i++) {
			api.output(`tick ${i}\n`);
			await sleep(200, context.abortSignal);
		}
		return { content: [{ type: "text", text: `slept ${args.seconds}s` }] };
	},
});

/** The same tool declared replay-safe. */
export const safeTool = defineTool({
	name: "safe_tool",
	description: "Sleep for some seconds; safe to rerun",
	parameters: Type.Object({ seconds: Type.Number() }),
	replay: "safe",
	execute: async (args, api, context) => {
		out("TOOL_STARTED", { tool: "safe_tool", callId: api.callId, taskId: api.taskId, pid: process.pid });
		if (process.env.EXEC_LOG) appendFileSync(process.env.EXEC_LOG, `safe_tool ${api.callId} pid=${process.pid}\n`);
		for (let i = 0; i < args.seconds * 5; i++) {
			api.output(`tick ${i}\n`);
			await sleep(200, context.abortSignal);
		}
		return { content: [{ type: "text", text: `safely slept ${args.seconds}s` }] };
	},
});

export const SpikeTools = defineExtension({
	name: "spike-tools",
	tools: [slowTool, safeTool],
	sections: [section("preamble", () => "You are the roost spike agent.", { tag: false })],
});

export async function openHost(
	dbPath: string,
	options: { storage?: NodeSqliteStorageOptions; settings?: HarnessSettings; models?: ReturnType<typeof createModels> } = {},
) {
	const registry = createRegistry();
	registry.install(CodingTools);
	registry.install(SpikeTools);
	const storage = await openNodeSqliteStorage(dbPath, options.storage);
	const harness = await Harness.open(
		storage,
		{
			models: options.models ?? createSpikeModels(),
			registry,
			settings: { retry: { maxRetries: 0 }, ...(options.settings ?? {}) },
			env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? process.cwd() }),
			onReport: (error) => out("REPORT", String(error)),
		},
		ctx,
	);
	return { harness, registry, storage };
}

/** Oldest-first list of a conversation's entries (Conversation.entries() pages newest-first). */
export async function allEntries(conversation: { entries: Function }) {
	const items: any[] = [];
	let cursor: unknown;
	do {
		const page = await conversation.entries({}, 100, cursor, ctx);
		items.push(...page.items);
		cursor = page.next;
	} while (cursor !== undefined);
	return items.reverse();
}

export function brief(entry: any) {
	const m = entry.model?.[0];
	let text = "";
	if (m?.role === "user") text = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
	else if (m?.role === "assistant")
		text = m.content
			.map((c: any) => (c.type === "text" ? c.text : c.type === "toolCall" ? `toolCall(${c.name} ${JSON.stringify(c.arguments)})` : c.type))
			.join("|") + ` [stop=${m.stopReason}]`;
	else if (m?.role === "toolResult") text = `${m.isError ? "ERROR " : ""}${m.content.map((c: any) => c.text).join("")}`;
	else text = JSON.stringify(entry.data ?? "");
	return `#${entry.id} ${entry.kind}${entry.head !== undefined ? ` head=${entry.head}` : ""}: ${text.slice(0, 140)}`;
}
