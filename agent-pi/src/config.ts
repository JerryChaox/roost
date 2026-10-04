// `initialize` parameters (driver-protocol §5) and the model access built from them.
import type { Api, Model, ModelThinkingLevel } from "@earendil-works/pi-ai";
import { createModels, type MutableModels, type Provider } from "@earendil-works/pi-ai/models";
import { builtinProviders } from "@earendil-works/pi-ai/providers/all";
import type { ModelRef } from "@earendil-works/pi-durable";

export const THINKING_LEVELS: readonly ModelThinkingLevel[] = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

export type InitializeParams = {
	protocol: 1;
	storage: string;
	socket: string;
	models: { baseUrls: Record<string, string>; credential: string };
	agent: {
		model: ModelRef;
		thinking: ModelThinkingLevel;
		modelInfo: ModelInfo | undefined;
		systemPrompt: { base: "pi" | "none"; append: string | null };
	};
};

/** Model metadata from roost: required for a model outside Pi's catalog, an override for one in it. */
export type ModelInfo = {
	contextWindow: number;
	maxOutputTokens: number;
	reasoning: boolean;
	input: ("text" | "image")[];
};

export class InvalidParams extends Error {}

const isObject = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);

function onlyKeys(value: Record<string, unknown>, allowed: readonly string[], where: string): void {
	const extra = Object.keys(value).filter((key) => !allowed.includes(key));
	if (extra.length > 0) throw new InvalidParams(`${where}: unknown field(s) ${extra.join(", ")}`);
}

function nonEmptyString(value: unknown, where: string): string {
	if (typeof value !== "string" || value.length === 0) throw new InvalidParams(`${where} must be a non-empty string`);
	return value;
}

/** `<provider>/<modelId>`, split on the first `/`. */
export function parseModel(value: unknown, where: string): ModelRef {
	const text = nonEmptyString(value, where);
	const slash = text.indexOf("/");
	if (slash <= 0 || slash === text.length - 1) {
		throw new InvalidParams(`${where} must be "<provider>/<modelId>", got ${JSON.stringify(text)}`);
	}
	return { provider: text.slice(0, slash), modelId: text.slice(slash + 1) };
}

const positiveInt = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0;

/** `{ contextWindow, maxOutputTokens, reasoning, input }`, every field required. */
export function parseModelInfo(value: unknown, where: string): ModelInfo {
	if (!isObject(value)) throw new InvalidParams(`${where} must be an object`);
	onlyKeys(value, ["contextWindow", "maxOutputTokens", "reasoning", "input"], where);
	if (!positiveInt(value.contextWindow)) throw new InvalidParams(`${where}.contextWindow must be a positive integer`);
	if (!positiveInt(value.maxOutputTokens)) throw new InvalidParams(`${where}.maxOutputTokens must be a positive integer`);
	if (typeof value.reasoning !== "boolean") throw new InvalidParams(`${where}.reasoning must be a boolean`);
	const input = value.input;
	const ok =
		Array.isArray(input) &&
		((input.length === 1 && input[0] === "text") || (input.length === 2 && input[0] === "text" && input[1] === "image"));
	if (!ok) throw new InvalidParams(`${where}.input must be ["text"] or ["text","image"]`);
	return {
		contextWindow: value.contextWindow,
		maxOutputTokens: value.maxOutputTokens,
		reasoning: value.reasoning,
		input: [...(input as ("text" | "image")[])],
	};
}

const sameInfo = (a: ModelInfo, b: ModelInfo) =>
	a.contextWindow === b.contextWindow &&
	a.maxOutputTokens === b.maxOutputTokens &&
	a.reasoning === b.reasoning &&
	a.input.join() === b.input.join();

/** Validate the shape of `initialize` params; model availability is checked against the built models. */
export function parseInitialize(params: unknown): InitializeParams {
	if (!isObject(params)) throw new InvalidParams("params must be an object");
	if ("restoredFrom" in params) throw new InvalidParams("restoredFrom is not supported by this agent host in M1");
	onlyKeys(params, ["protocol", "storage", "socket", "models", "agent"], "params");
	if (params.protocol !== 1) throw new InvalidParams(`unsupported protocol ${JSON.stringify(params.protocol)}`);
	const storage = nonEmptyString(params.storage, "storage");
	const socket = nonEmptyString(params.socket, "socket");

	const models = params.models;
	if (!isObject(models)) throw new InvalidParams("models must be an object");
	onlyKeys(models, ["baseUrls", "credential"], "models");
	if (!isObject(models.baseUrls)) throw new InvalidParams("models.baseUrls must be an object");
	const baseUrls: Record<string, string> = {};
	for (const [provider, url] of Object.entries(models.baseUrls)) {
		const text = nonEmptyString(url, `models.baseUrls.${provider}`);
		try {
			new URL(text);
		} catch {
			throw new InvalidParams(`models.baseUrls.${provider} is not a URL`);
		}
		baseUrls[provider] = text;
	}
	if (typeof models.credential !== "string") throw new InvalidParams("models.credential must be a string");

	const agent = params.agent;
	if (!isObject(agent)) throw new InvalidParams("agent must be an object");
	onlyKeys(agent, ["model", "thinking", "systemPrompt", "modelInfo"], "agent");
	const model = parseModel(agent.model, "agent.model");
	const modelInfo = agent.modelInfo === undefined ? undefined : parseModelInfo(agent.modelInfo, "agent.modelInfo");
	if (typeof agent.thinking !== "string" || !THINKING_LEVELS.includes(agent.thinking as ModelThinkingLevel)) {
		throw new InvalidParams(
			`agent.thinking must be one of ${THINKING_LEVELS.join(", ")}, got ${JSON.stringify(agent.thinking)}`,
		);
	}
	const prompt = agent.systemPrompt;
	if (!isObject(prompt)) throw new InvalidParams("agent.systemPrompt must be an object");
	onlyKeys(prompt, ["base", "append"], "agent.systemPrompt");
	if (prompt.base !== "pi" && prompt.base !== "none") {
		throw new InvalidParams(`agent.systemPrompt.base must be "pi" or "none"`);
	}
	if (prompt.append !== null && typeof prompt.append !== "string") {
		throw new InvalidParams("agent.systemPrompt.append must be a string or null");
	}
	return {
		protocol: 1,
		storage,
		socket,
		models: { baseUrls, credential: models.credential },
		agent: {
			model,
			thinking: agent.thinking as ModelThinkingLevel,
			modelInfo,
			systemPrompt: { base: prompt.base, append: prompt.append },
		},
	};
}

/**
 * Pi model access for the grant: every built-in Pi provider, with its auth replaced by the grant's credential, and
 * the base URL for providers named in `baseUrls` (pi-ai applies `auth.baseUrl` to the request model). Nothing is
 * read from the environment or disk: the credential store is pi-ai's in-memory default and the replaced auth ignores
 * ambient credentials.
 *
 * Each provider is also wrapped so its `getModels()` returns the catalog plus roost's overlay (`register()`). Pi looks
 * every model up through `Models.getModel()` → `provider.getModels()` (generation, compaction), so a registered model
 * resolves everywhere without patching Pi. Models are keyed by provider and id, as in Pi, so one id has one metadata.
 */
export class ModelAccess {
	readonly models: MutableModels;
	readonly #baseUrls: Record<string, string>;
	readonly #catalog = new Map<string, readonly Model<Api>[]>();
	readonly #overlay = new Map<string, Map<string, { model: Model<Api>; info: ModelInfo }>>();

	constructor(baseUrls: Record<string, string>, credential: string) {
		this.#baseUrls = baseUrls;
		this.models = createModels();
		const providers = builtinProviders();
		const known = new Set(providers.map((provider) => provider.id));
		const unknown = Object.keys(baseUrls).filter((id) => !known.has(id));
		if (unknown.length > 0) throw new InvalidParams(`models.baseUrls names unknown Pi provider(s) ${unknown.join(", ")}`);
		for (const provider of providers) {
			this.#catalog.set(provider.id, provider.getModels());
			this.models.setProvider(this.#wrap(provider, credential, baseUrls[provider.id]));
		}
	}

	#wrap(provider: Provider, credential: string, baseUrl: string | undefined): Provider {
		return {
			...provider,
			auth: {
				apiKey: {
					name: "roost initialize",
					resolve: async () => ({
						auth: { apiKey: credential, ...(baseUrl === undefined ? {} : { baseUrl }) },
						source: "roost initialize",
					}),
				},
			},
			getModels: () => {
				const overlay = this.#overlay.get(provider.id);
				const catalog = provider.getModels();
				if (overlay === undefined || overlay.size === 0) return catalog;
				const merged = catalog.map((model) => overlay.get(model.id)?.model ?? model);
				for (const [id, entry] of overlay) if (!catalog.some((model) => model.id === id)) merged.push(entry.model);
				return merged;
			},
		};
	}

	/** The metadata registered for `ref`, if any. */
	registered(ref: ModelRef): ModelInfo | undefined {
		return this.#overlay.get(ref.provider)?.get(ref.modelId)?.info;
	}

	/**
	 * Make `ref` resolvable: a catalog model as is, or with `info` overriding its metadata; a model outside the catalog
	 * only with `info`, built in the provider's API shape. `replace: false` refuses metadata that differs from what is
	 * already registered for the same id.
	 */
	register(ref: ModelRef, info: ModelInfo | undefined, where: string, replace: boolean): void {
		const catalog = this.#catalog.get(ref.provider);
		if (catalog === undefined) throw new InvalidParams(`${where}: unknown Pi provider ${JSON.stringify(ref.provider)}`);
		const base = catalog.find((model) => model.id === ref.modelId);
		const existing = this.#overlay.get(ref.provider)?.get(ref.modelId);
		if (info === undefined) {
			if (base === undefined && existing === undefined) {
				throw new InvalidParams(
					`${where}: Pi's catalog has no model ${JSON.stringify(ref.modelId)} for provider ${JSON.stringify(ref.provider)}; give modelInfo`,
				);
			}
			return;
		}
		if (existing !== undefined && !replace && !sameInfo(existing.info, info)) {
			throw new InvalidParams(
				`${where}: ${ref.provider}/${ref.modelId} is already in use with different modelInfo; one model id has one metadata`,
			);
		}
		const metadata = {
			contextWindow: info.contextWindow,
			maxTokens: info.maxOutputTokens,
			reasoning: info.reasoning,
			input: [...info.input],
		};
		let model: Model<Api>;
		if (base !== undefined) model = { ...base, ...metadata };
		else {
			// The provider's API shape: the one API its catalog models use (Anthropic Messages for `anthropic`).
			const apis = [...new Set(catalog.map((entry) => entry.api))];
			if (apis.length !== 1) {
				throw new InvalidParams(
					`${where}: provider ${ref.provider} serves several APIs (${apis.join(", ") || "none"}); a model outside its catalog is not supported`,
				);
			}
			const provider = this.models.getProvider(ref.provider)!;
			model = {
				id: ref.modelId,
				name: ref.modelId,
				api: apis[0]!,
				provider: ref.provider,
				baseUrl: this.#baseUrls[ref.provider] ?? provider.baseUrl ?? catalog[0]!.baseUrl,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				...metadata,
			};
		}
		let overlay = this.#overlay.get(ref.provider);
		if (overlay === undefined) {
			overlay = new Map();
			this.#overlay.set(ref.provider, overlay);
		}
		overlay.set(ref.modelId, { model, info: { ...info, input: [...info.input] } });
	}
}
