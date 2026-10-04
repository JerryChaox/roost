// The agent host: Pi Durable over agent storage, the conversation operations behind the HTTP interface, and the
// state the control channel reports.
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import type { Context } from "@earendil-works/chord";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import {
	AgentDoc,
	type AgentChange,
	configure,
	type ConversationRecord,
	createRegistry,
	type Cursor,
	defineExtension,
	type EntryRecord,
	Harness,
	InboxDoc,
	LiveDoc,
	type PromptInput,
	section,
	type SubmissionRecord,
} from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { type InitializeParams, InvalidParams, ModelAccess, type ModelInfo, parseModel, parseModelInfo } from "./config.ts";
import { type HostLock, takeHostLock } from "./lock.ts";
import type { Logger } from "./log.ts";
import { ConvDoc, type ConvState, KeyIndex, RunsDoc, RunTracker, type Span } from "./runs.ts";
import { loadProjectContextFiles } from "./vendor/pi/context-files.ts";
import { buildSystemPromptSections, type ContextFile, SECTION_KEYS } from "./vendor/pi/system-prompt.ts";

export const ctx: Context = BACKGROUND_CONTEXT;

export const PI_DURABLE_VERSION: string = createRequire(import.meta.url)("@earendil-works/pi-durable/package.json").version;

/** A request the conversation interface answers with a JSON error. */
export class ApiError extends Error {
	constructor(
		readonly status: number,
		readonly code: string,
		detail: string,
	) {
		super(detail);
	}
}

const badRequest = (detail: string) => new ApiError(400, "invalid_request", detail);
const unknownConversation = (detail: string) => new ApiError(404, "unknown_conversation", detail);

const ENTRY_KINDS: Readonly<Record<string, string>> = {
	"pi.user": "user",
	"pi.assistant": "assistant",
	"pi.tool-result": "tool_result",
	"pi.system": "system",
	"pi.reset": "reset",
	"pi.compaction": "compaction",
	"roost.note": "note",
};

export type WireEntry = { cursor: string; kind: string; run: string | null; time?: string; raw: EntryRecord };

export type HostEvents = {
	snapshot(params: { conversation: string; run: string; position: string }): void;
};

/** Pi's system prompt (`base: "pi"`) or none, then `append`, as one extension of sections. */
function promptExtension(workdir: string, prompt: InitializeParams["agent"]["systemPrompt"], log: Logger) {
	const contextFiles = new Map<string, ContextFile[]>();
	const built = new WeakMap<PromptInput, Record<string, string>>();
	const build = (input: PromptInput): Record<string, string> => {
		let sections = built.get(input);
		if (sections === undefined) {
			const cwd = input.env?.cwd ?? input.agent.cwd ?? workdir;
			// Loaded once per directory, as Pi does at startup.
			let files = contextFiles.get(cwd);
			if (files === undefined) {
				files = loadProjectContextFiles(cwd, (message) => log.warn(message));
				contextFiles.set(cwd, files);
			}
			sections = buildSystemPromptSections({
				cwd,
				selectedTools: input.agent.tools.map((tool) => tool.name),
				contextFiles: files,
			});
			built.set(input, sections);
		}
		return sections;
	};
	const sections = [];
	if (prompt.base === "pi") {
		for (const key of SECTION_KEYS) sections.push(section(key, (input) => build(input)[key], { tag: false }));
	}
	const append = prompt.append;
	if (append !== null && append !== "") sections.push(section("roost_append", () => append, { tag: false }));
	return defineExtension({ name: "roost-prompt", sections });
}

/** Serializes async work per key. */
class KeyedMutex {
	readonly #tails = new Map<number, Promise<unknown>>();
	run<T>(key: number, work: () => Promise<T>): Promise<T> {
		const previous = this.#tails.get(key) ?? Promise.resolve();
		const result = previous.then(work, work);
		const tail = result.then(
			() => {},
			() => {},
		);
		this.#tails.set(key, tail);
		void tail.then(() => {
			if (this.#tails.get(key) === tail) this.#tails.delete(key);
		});
		return result;
	}
}

export class Host {
	readonly harness: Harness;
	readonly models: ModelAccess;
	readonly tracker: RunTracker;
	readonly startedAt = Date.now();
	draining = false;
	closing = false;
	readonly #lock: HostLock;
	readonly #log: Logger;
	readonly #events: HostEvents;
	readonly #admission = new KeyedMutex();
	readonly #persisting = new Map<number, Promise<void>>();
	readonly #skippedKinds = new Set<string>();

	private constructor(harness: Harness, models: ModelAccess, lock: HostLock, log: Logger, events: HostEvents) {
		this.harness = harness;
		this.models = models;
		this.#lock = lock;
		this.#log = log;
		this.#events = events;
		this.tracker = new RunTracker({
			persist: (conv) => this.#persist(conv),
			toolResult: (conv, run, seq) => {
				if (run === null) return;
				const notice = { conversation: `c_${conv.id}`, run, position: String(seq) };
				// Never block inside the commit listener.
				setImmediate(() => this.#events.snapshot(notice));
			},
		});
	}

	/**
	 * Validate the configuration, take the storage lock, open Pi storage and load the run state. Scheduling stays
	 * off until `resume()`.
	 */
	static async open(params: InitializeParams, workdir: string, log: Logger, events: HostEvents): Promise<Host> {
		const models = new ModelAccess(params.models.baseUrls, params.models.credential);
		models.register(params.agent.model, params.agent.modelInfo, "agent.model", true);
		mkdirSync(params.storage, { recursive: true });
		const lock = takeHostLock(join(params.storage, "host.lock"));
		let harness: Harness | undefined;
		try {
			const registry = createRegistry();
			registry.install(CodingTools);
			registry.install(promptExtension(workdir, params.agent.systemPrompt, log));
			const defaults: AgentChange = { model: params.agent.model, thinkingLevel: params.agent.thinking };
			harness = await Harness.open(
				await openNodeSqliteStorage(join(params.storage, "agent.sqlite")),
				{
					models: models.models,
					registry,
					env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? workdir }),
					// Pi has no host-level default model: every conversation the host creates gets the defaults stored.
					// Conversations owned by a task copy their owner's agent (Pi's createAgent) and are left alone.
					conversationCreated: (tx, record) =>
						record.owner === undefined && record.parent === undefined
							? configure(tx, record.id, defaults)
							: undefined,
					onReport: (error) => log.warn(`pi: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`),
				},
				ctx,
			);
			const host = new Host(harness, models, lock, log, events);
			await host.#load(defaults);
			harness.subscribeCommits((publication) => host.tracker.onCommit(publication));
			return host;
		} catch (error) {
			if (harness !== undefined) await harness.close(ctx).catch(() => {});
			lock.release();
			throw error;
		}
	}

	/** Load every roost conversation, its spans, `pi.live` and `pi.inbox`, and reconcile the spans. */
	async #load(defaults: AgentChange): Promise<void> {
		const harness = this.harness;
		let cursor: Cursor | undefined;
		const records: ConversationRecord[] = [];
		do {
			const page = await harness.commit((tx) => tx.scanConversations({}, 256, cursor), ctx);
			records.push(...page.items);
			cursor = page.next;
		} while (cursor !== undefined);
		const settlements = new Map<number, SubmissionRecord | undefined>();
		const submission = async (id: number) => {
			if (!settlements.has(id)) {
				const handle = await harness.submission(id as never, ctx);
				const record = handle === undefined ? undefined : await handle.status(ctx);
				settlements.set(id, record);
				if (record !== undefined) this.tracker.requestIds.set(id, record.requestId);
			}
			return settlements.get(id);
		};
		for (const record of records) this.tracker.owners.set(record.id, record.owner?.conversationId);
		// contracts §13: the agent settings apply from the next driver start. Every root conversation follows the current
		// default thinking level; those that did not choose their own model at creation also follow the default model.
		const reconfigure = new Map<number, AgentChange>();
		for (const record of records) {
			const meta = await harness.snapshot(ConvDoc, record.id, ctx);
			if (meta?.roost !== true) continue;
			const own = meta.ownModel === true;
			if (own && meta.model !== null && meta.modelInfo !== null) {
				try {
					this.models.register(parseModel(meta.model, "model"), meta.modelInfo as ModelInfo, `c_${record.id}`, false);
				} catch (error) {
					this.#log.warn(`c_${record.id} keeps ${meta.model} with the initialize modelInfo: ${String(error)}`);
				}
			}
			const agent = await harness.snapshot(AgentDoc, record.id, ctx);
			const change: AgentChange = own ? { thinkingLevel: defaults.thinkingLevel } : defaults;
			const stale =
				agent?.thinkingLevel !== defaults.thinkingLevel ||
				(!own &&
					(agent?.model?.provider !== defaults.model?.provider || agent?.model?.modelId !== defaults.model?.modelId));
			if (stale) reconfigure.set(record.id, change);
			const spans = ((await harness.snapshot(RunsDoc, record.id, ctx))?.spans ?? []).map((span) => ({ ...span }));
			const conv = this.tracker.register(record.id, meta.key, spans as Span[]);
			const conversation = (await harness.conversation(record.id, ctx))!;
			conv.lastEntry = (await conversation.entries({}, 1, undefined, ctx)).items[0]?.id ?? 0;
			const live = await harness.snapshot(LiveDoc, record.id, ctx);
			const inbox = await harness.snapshot(InboxDoc, record.id, ctx);
			conv.queued = (inbox?.items ?? []).filter((item) => item.mode === "followUp").map((item) => item.id);
			for (const id of conv.queued) await submission(id);
			let current: { first: number; firstEntry: number } | undefined;
			const first = live?.run?.inputs[0] as number | undefined;
			if (first !== undefined) {
				const started = await submission(first);
				current = { first, firstEntry: started?.entry ?? conv.lastEntry };
				// The watchdog counts from the restart for a run that was interrupted.
				conv.lastProgress = this.startedAt;
			}
			for (const span of conv.spans) if (span.to === null) await submission(span.sub);
			if (this.tracker.reconcile(conv, current, (id) => settlements.get(id))) this.#persist(conv);
		}
		if (reconfigure.size > 0) {
			await harness.commit(async (tx) => {
				for (const [id, change] of reconfigure) await configure(tx, id as never, change);
			}, ctx);
			this.#log.info(`applied the default agent settings to ${reconfigure.size} conversation(s)`);
		}
		await Promise.all(this.#persisting.values());
	}

	/** Write the conversation's spans in a host commit; serialized per conversation. */
	#persist(conv: ConvState): void {
		const previous = this.#persisting.get(conv.id) ?? Promise.resolve();
		const next = previous.then(async () => {
			const spans = conv.spans.map((span) => ({ ...span }));
			try {
				await this.harness.commit(async (tx) => {
					const doc = await tx.doc(RunsDoc, conv.id as never);
					for (const [index, span] of spans.entries()) {
						const stored = doc.spans[index];
						if (stored === undefined) doc.spans.push(span);
						else if (stored.to !== span.to || stored.status !== span.status || stored.from !== span.from) {
							stored.to = span.to;
							stored.status = span.status;
							stored.from = span.from;
						}
					}
				}, ctx);
			} catch (error) {
				this.#log.warn(`could not persist run spans of c_${conv.id}: ${String(error)}`);
			}
		});
		this.#persisting.set(conv.id, next);
		void next.then(() => {
			if (this.#persisting.get(conv.id) === next) this.#persisting.delete(conv.id);
		});
	}

	resume(): void {
		this.harness.resume();
	}

	/** Runs in progress: roost conversations whose `pi.live.run` is set. */
	running(): number {
		let count = 0;
		for (const conv of this.tracker.convs.values()) if (conv.current !== null) count++;
		return count;
	}

	state() {
		const iso = (time: number | null) => new Date(time ?? this.startedAt).toISOString();
		const runs = [];
		for (const conv of this.tracker.convs.values()) {
			const conversation = `c_${conv.id}`;
			if (conv.current !== null) {
				runs.push({ conversation, run: conv.current.run, status: "running", lastProgressAt: iso(conv.lastProgress) });
			}
			for (const id of conv.queued) {
				runs.push({ conversation, run: this.tracker.runIdOf(id), status: "queued", lastProgressAt: iso(conv.lastProgress) });
			}
		}
		const last = this.tracker.lastProgress;
		return { runs, lastProgressAt: last === null ? null : new Date(last).toISOString() };
	}

	async close(): Promise<void> {
		this.closing = true;
		this.draining = true;
		await Promise.all(this.#persisting.values());
		try {
			await this.harness.close(ctx);
		} finally {
			this.#lock.release();
		}
	}

	// --- conversation interface -------------------------------------------------------------------------------

	conversationId(wire: string): ConvState {
		const match = /^c_([1-9][0-9]*)$/.exec(wire);
		const conv = match === null ? undefined : this.tracker.convs.get(Number(match[1]));
		if (conv === undefined) throw unknownConversation(`no conversation ${JSON.stringify(wire)}`);
		return conv;
	}

	#assertOpen(): void {
		if (this.closing) throw new ApiError(503, "not_ready", "shutting down");
	}

	async createConversation(
		key: string | undefined,
		settings: { model?: string; modelInfo?: unknown; instructions?: string },
	): Promise<string> {
		this.#assertOpen();
		const change: { model?: { provider: string; modelId: string }; instructions?: string } = {};
		let modelInfo: ModelInfo | undefined;
		if (settings.modelInfo !== undefined && settings.model === undefined) {
			throw badRequest("settings.modelInfo requires settings.model");
		}
		if (settings.model !== undefined) {
			try {
				change.model = parseModel(settings.model, "settings.model");
				modelInfo = settings.modelInfo === undefined ? undefined : parseModelInfo(settings.modelInfo, "settings.modelInfo");
				this.models.register(change.model, modelInfo, "settings.model", false);
			} catch (error) {
				if (error instanceof InvalidParams) throw badRequest(error.message);
				throw error;
			}
		}
		if (settings.instructions !== undefined) change.instructions = settings.instructions;
		const id = await this.harness.commit(async (tx) => {
			const slot = key === undefined ? undefined : await tx.doc(KeyIndex, key, null);
			if (slot !== undefined && slot.conversationId !== null) return slot.conversationId;
			const record = await tx.createConversation({ ownership: { kind: "ownerless" } });
			if (slot !== undefined) slot.conversationId = record.id;
			const meta = await tx.doc(ConvDoc, record.id);
			meta.roost = true;
			meta.key = key ?? null;
			meta.ownModel = settings.model !== undefined;
			meta.model = settings.model ?? null;
			meta.modelInfo = modelInfo === undefined ? null : { ...modelInfo, input: [...modelInfo.input] };
			if (Object.keys(change).length > 0) await configure(tx, record.id, change);
			return record.id as number;
		}, ctx);
		return `c_${id}`;
	}

	listConversations(query: { key?: string; active: boolean; cursor?: string; limit: number }) {
		let after = 0;
		if (query.cursor !== undefined) {
			if (!/^[0-9]+$/.test(query.cursor)) throw badRequest("cursor must be a cursor from an earlier page");
			after = Number(query.cursor);
		}
		let candidates: ConvState[];
		if (query.key !== undefined) {
			const id = this.tracker.keys.get(query.key);
			candidates = id === undefined ? [] : [this.tracker.convs.get(id)!];
		} else {
			candidates = [...this.tracker.convs.values()].sort((a, b) => a.id - b.id);
		}
		const matching = candidates.filter((conv) => conv.id > after && (!query.active || conv.current !== null));
		const page = matching.slice(0, query.limit);
		return {
			conversations: page.map((conv) => ({ conversation: `c_${conv.id}`, key: conv.key, active: conv.current !== null })),
			next: matching.length > query.limit ? String(page.at(-1)!.id) : null,
		};
	}

	async postMessage(
		conv: ConvState,
		requestId: string,
		content: string,
		whenBusy: "follow_up" | "steer",
	): Promise<{ code: 200 | 202; status: "queued" | "running" | "duplicate" }> {
		this.#assertOpen();
		if (this.draining) throw new ApiError(503, "not_ready", "draining");
		// Pi admits in its own commit and offers no hook into it, so the duplicate check is a read of
		// `submissionByRequest` in a commit just before admission. Admissions are serialized per conversation and this
		// process is the only one with the storage open (host lock), so nothing can admit the same id in between.
		return this.#admission.run(conv.id, async () => {
			const existing = await this.harness.commit(
				(tx) => tx.submissionByRequest(conv.id as never, requestId),
				ctx,
			);
			if (existing !== undefined) {
				if (existing.type !== "input") throw badRequest(`requestId ${JSON.stringify(requestId)} already names a note`);
				return { code: 200, status: "duplicate" };
			}
			const conversation = (await this.harness.conversation(conv.id as never, ctx))!;
			const submission = await conversation.submit(
				{ type: "input", content, requestId, whenBusy: whenBusy === "steer" ? "steer" : "followUp" },
				ctx,
			);
			const record = await submission.status(ctx);
			return { code: 202, status: record.status === "queued" ? "queued" : "running" };
		});
	}

	async abort(conv: ConvState): Promise<void> {
		this.#assertOpen();
		const conversation = (await this.harness.conversation(conv.id as never, ctx))!;
		await conversation.abort(ctx);
	}

	async reset(conv: ConvState, note: string | undefined): Promise<void> {
		this.#assertOpen();
		const conversation = (await this.harness.conversation(conv.id as never, ctx))!;
		await conversation.reset(note, ctx);
	}

	async note(conv: ConvState, id: string, data: Record<string, unknown>): Promise<void> {
		this.#assertOpen();
		const conversation = (await this.harness.conversation(conv.id as never, ctx))!;
		try {
			await conversation.submit(
				{ type: "write", entry: { kind: "roost.note", data: data as never }, requestId: id },
				ctx,
			);
		} catch (error) {
			if (error instanceof Error && /already identifies a submission/.test(error.message)) {
				throw badRequest(`note id ${JSON.stringify(id)} already names a message`);
			}
			throw error;
		}
	}

	/** Entries strictly after `after`, oldest first; `scanned` is the newest entry examined (skipped kinds too). */
	async entriesAfter(
		conv: ConvState,
		after: number,
		limit: number,
	): Promise<{ entries: WireEntry[]; scanned: number | undefined; more: boolean }> {
		const conversation = (await this.harness.conversation(conv.id as never, ctx))!;
		const newest = (await conversation.entries({}, 1, undefined, ctx)).items[0]?.id ?? 0;
		const found: EntryRecord[] = [];
		// Pi pages newest-first; entry ids are global, so read forward in growing id windows.
		let low = after + 1;
		let width = Math.max(limit * 4, 64);
		while (low <= newest && found.length <= limit) {
			const high = Math.min(newest, low + width - 1);
			const window: EntryRecord[] = [];
			let cursor: Parameters<typeof conversation.entries>[2];
			do {
				const page = await conversation.entries(
					{ minEntryId: low as never, maxEntryId: high as never },
					256,
					cursor,
					ctx,
				);
				window.push(...page.items);
				cursor = page.next;
			} while (cursor !== undefined);
			found.push(...window.reverse());
			low = high + 1;
			width *= 2;
		}
		const page = found.slice(0, limit);
		return {
			entries: page.flatMap((entry) => this.toWire(conv, entry) ?? []),
			scanned: page.at(-1)?.id,
			more: found.length > limit,
		};
	}

	/** The entry as the conversation interface shows it, or undefined for a kind it does not show. */
	toWire(conv: ConvState, entry: EntryRecord): WireEntry | undefined {
		const kind = ENTRY_KINDS[entry.kind];
		if (kind === undefined) {
			if (!this.#skippedKinds.has(entry.kind)) {
				this.#skippedKinds.add(entry.kind);
				this.#log.warn(`skipping entries of unknown kind ${JSON.stringify(entry.kind)}`);
			}
			return undefined;
		}
		const timestamp = (entry.model?.[0] as { timestamp?: unknown } | undefined)?.timestamp;
		return {
			cursor: String(entry.id),
			kind,
			run: this.tracker.runOf(conv, entry.id),
			...(typeof timestamp === "number" && Number.isFinite(timestamp) ? { time: new Date(timestamp).toISOString() } : {}),
			raw: entry,
		};
	}
}
