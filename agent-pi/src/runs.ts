// Run tracking for roost conversations.
//
// Pi identifies a run by `pi.live.run.inputs[0]`, the submission that started it (harness/events.ts compares exactly
// that), and keeps `pi.live` only as its latest state; it records no run per entry and no timestamps. roost's run id is
// the `requestId` of that submission. This module follows every commit publication, which carries the new `pi.live`
// value, the submissions and the entries of the commit, and derives:
//   - per conversation, run spans `{ run, from, to }` over entry ids, so every entry can name its run;
//   - the run's end status from the settlement of its inputs, committed in the same commit as the run's end;
//   - progress times (in memory only), counting work in conversations a run's tasks own toward the owner's run;
//   - `snapshot` notifications for commits that commit a tool result.
// Spans are persisted in the host document `roost.runs` by a host commit right after the Pi commit that changed them.
// A crash between the two loses at most that update; `reconcile()` repairs it from `pi.live` on the next start.
import type {
	CommitPublication,
	ConversationId,
	EntryRecord,
	InboxState,
	LiveState,
	SubmissionId,
	SubmissionRecord,
} from "@earendil-works/pi-durable";
import { defineDoc, defineDocFamily } from "@earendil-works/pi-durable";

export type RunStatus = "running" | "completed" | "failed" | "aborted";

/** Entries with `from <= id <= to` (or `to === null`: still running) belong to `run`. `sub` is its first input. */
export type Span = { run: string; sub: number; from: number; to: number | null; status: RunStatus };

/** Marks a conversation created through the conversation interface; child conversations Pi creates have none. */
export type ConvMeta = {
	roost: boolean;
	key: string | null;
	/** Whether `settings.model` was given at creation; otherwise the conversation follows the host default. */
	ownModel: boolean;
	/** `settings.model` and `settings.modelInfo` as given at creation. */
	model: string | null;
	modelInfo: { contextWindow: number; maxOutputTokens: number; reasoning: boolean; input: ("text" | "image")[] } | null;
};

export const ConvDoc = defineDoc<ConvMeta>({
	kind: "roost.conv",
	version: 1,
	scope: "conversation",
	history: "latest",
	fork: "initial",
	initial: () => ({ roost: false, key: null, ownModel: false, model: null, modelInfo: null }),
});

/** Caller key → conversation id, one family member per key (spike e3). */
export const KeyIndex = defineDocFamily<{ conversationId: number | null }, null>({
	kind: "roost.key",
	version: 1,
	scope: "session",
	family: true,
	initial: () => ({ conversationId: null }),
});

export const RunsDoc = defineDoc<{ spans: Span[] }>({
	kind: "roost.runs",
	version: 1,
	scope: "conversation",
	history: "latest",
	fork: "initial",
	initial: () => ({ spans: [] }),
});

export type ConvState = {
	readonly id: number;
	readonly key: string | null;
	spans: Span[];
	/** `pi.live.run.inputs[0]` and its run id, while a run is in progress. */
	current: { sub: number; run: string } | null;
	/** Follow-up inputs waiting in `pi.inbox`; each starts a run of its own. */
	queued: number[];
	/** Newest entry id of the conversation. */
	lastEntry: number;
	lastProgress: number | null;
};

/** The run status recorded by a run's first input once the run ended. */
export function settledStatus(record: SubmissionRecord | undefined): RunStatus {
	if (record === undefined || record.status === "queued" || record.status === "placed") return "failed";
	if (record.status === "done") return "completed";
	return record.reason === "aborted" || record.reason === "reset" ? "aborted" : "failed";
}

export type TrackerHooks = {
	/** A span changed; persist the conversation's spans. */
	persist(conv: ConvState): void;
	/** A commit committed a tool result for `conv`'s run. */
	toolResult(conv: ConvState, run: string | null, seq: number): void;
};

export class RunTracker {
	readonly convs = new Map<number, ConvState>();
	readonly keys = new Map<string, number>();
	/** Owner conversation of each task-owned conversation. */
	readonly owners = new Map<number, number | undefined>();
	readonly requestIds = new Map<number, string | undefined>();
	lastProgress: number | null = null;
	readonly #hooks: TrackerHooks;

	constructor(hooks: TrackerHooks) {
		this.#hooks = hooks;
	}

	register(id: number, key: string | null, spans: Span[] = []): ConvState {
		let conv = this.convs.get(id);
		if (conv === undefined) {
			conv = { id, key, spans, current: null, queued: [], lastEntry: 0, lastProgress: null };
			this.convs.set(id, conv);
			if (key !== null) this.keys.set(key, id);
		}
		return conv;
	}

	runIdOf(sub: number): string {
		return this.requestIds.get(sub) ?? `submission-${sub}`;
	}

	/** The roost conversation that owns `id`, through task ownership; undefined for unrelated conversations. */
	rootOf(id: number): number | undefined {
		for (let current: number | undefined = id, hops = 0; current !== undefined && hops < 1000; hops++) {
			if (this.convs.has(current)) return current;
			current = this.owners.get(current);
		}
		return undefined;
	}

	runOf(conv: ConvState, entryId: number): string | null {
		for (let index = conv.spans.length - 1; index >= 0; index--) {
			const span = conv.spans[index]!;
			if (entryId < span.from) continue;
			return span.to === null || entryId <= span.to ? span.run : null;
		}
		return null;
	}

	statusOf(conv: ConvState, run: string): RunStatus | undefined {
		for (let index = conv.spans.length - 1; index >= 0; index--) {
			if (conv.spans[index]!.run === run) return conv.spans[index]!.status;
		}
		return undefined;
	}

	/** Commit listener: synchronous, never throws, calls no Session API. */
	onCommit(publication: CommitPublication): void {
		const now = Date.now();
		type Changes = { entries: EntryRecord[]; subs: SubmissionRecord[]; live?: LiveState | null; inbox?: InboxState | null };
		const byConv = new Map<number, Changes>();
		const changesOf = (id: number): Changes => {
			let found = byConv.get(id);
			if (found === undefined) {
				found = { entries: [], subs: [] };
				byConv.set(id, found);
			}
			return found;
		};
		const touched = new Set<number>();
		for (const change of publication.changes) {
			switch (change.type) {
				case "conversation":
					this.owners.set(change.value.id, change.value.owner?.conversationId);
					touched.add(change.value.id);
					break;
				case "entry":
					changesOf(change.value.conversationId).entries.push(change.value);
					touched.add(change.value.conversationId);
					break;
				case "task":
					touched.add(change.value.conversationId);
					break;
				case "submission":
					this.requestIds.set(change.value.id, change.value.requestId);
					changesOf(change.value.conversationId).subs.push(change.value);
					touched.add(change.value.conversationId);
					break;
				case "document": {
					const kind = change.record.kind;
					const id = change.conversationId;
					if (kind.startsWith("roost.")) {
						const value = change.value as { roost?: boolean; key?: string | null } | null;
						if (kind === ConvDoc.definition.kind && id !== undefined && value?.roost === true) {
							this.register(id, value.key ?? null);
						}
						break;
					}
					if (id === undefined) break;
					touched.add(id);
					if (change.record.key !== undefined) break;
					if (kind === "pi.live") changesOf(id).live = change.value as LiveState | null;
					if (kind === "pi.inbox") changesOf(id).inbox = change.value as InboxState | null;
					break;
				}
				case "document.copy":
					touched.add(change.conversationId);
					break;
			}
		}
		for (const id of touched) {
			const root = this.rootOf(id);
			if (root === undefined) continue;
			this.convs.get(root)!.lastProgress = now;
			this.lastProgress = now;
		}
		// Per roost conversation, the run of the tool results this commit committed: a result in the conversation itself
		// names its own span (also when the same commit ended the run); one in a child conversation, the current run.
		const toolResults = new Map<number, string | null>();
		for (const [id, changes] of byConv) {
			const conv = this.convs.get(id);
			if (conv !== undefined) this.#apply(conv, changes);
			const result = changes.entries.findLast((entry) => entry.kind === "pi.tool-result");
			if (result === undefined) continue;
			const root = this.rootOf(id);
			if (root === undefined) continue;
			const rootConv = this.convs.get(root)!;
			const run = root === id ? this.runOf(rootConv, result.id) : (rootConv.current?.run ?? null);
			if (run !== null || !toolResults.has(root)) toolResults.set(root, run);
		}
		for (const [root, run] of toolResults) this.#hooks.toolResult(this.convs.get(root)!, run, publication.seq);
	}

	#apply(
		conv: ConvState,
		changes: { entries: EntryRecord[]; subs: SubmissionRecord[]; live?: LiveState | null; inbox?: InboxState | null },
	): void {
		const entries = [...changes.entries].sort((a, b) => a.id - b.id);
		if (changes.live !== undefined) {
			const first = changes.live?.run?.inputs[0] as number | undefined;
			if (first !== conv.current?.sub) {
				const started = first === undefined ? undefined : changes.subs.find((sub) => sub.id === first);
				const newStart =
					first === undefined ? undefined : (started?.entry ?? entries[0]?.id ?? conv.lastEntry + 1);
				if (conv.current !== null) {
					const old = conv.spans.findLast((span) => span.sub === conv.current!.sub && span.to === null);
					if (old !== undefined) {
						const before = entries.filter((entry) => newStart === undefined || entry.id < newStart);
						old.to = Math.max(old.from, before.at(-1)?.id ?? conv.lastEntry);
						old.status = settledStatus(changes.subs.find((sub) => sub.id === conv.current!.sub));
					}
				}
				if (first !== undefined && newStart !== undefined) {
					const run = this.runIdOf(first);
					conv.spans.push({ run, sub: first, from: newStart, to: null, status: "running" });
					conv.current = { sub: first, run };
				} else {
					conv.current = null;
				}
				this.#hooks.persist(conv);
			}
		}
		if (changes.inbox !== undefined) {
			conv.queued = (changes.inbox?.items ?? []).filter((item) => item.mode === "followUp").map((item) => item.id);
		}
		const last = entries.at(-1);
		if (last !== undefined && last.id > conv.lastEntry) conv.lastEntry = last.id;
	}

	/**
	 * At startup, after loading `roost.runs`: align the spans with `pi.live`. A run in progress whose span is missing
	 * gets one from its first input's entry; an open span whose run is over is closed at the newest entry with the
	 * settlement of its first input. Returns whether anything changed.
	 */
	reconcile(
		conv: ConvState,
		live: { first: number; firstEntry: number } | undefined,
		settlementOf: (sub: number) => SubmissionRecord | undefined,
	): boolean {
		let changed = false;
		const open = conv.spans.at(-1)?.to === null ? conv.spans.at(-1) : undefined;
		if (open !== undefined && open.sub !== live?.first) {
			open.to = Math.max(open.from, live === undefined ? conv.lastEntry : live.firstEntry - 1);
			open.status = settledStatus(settlementOf(open.sub));
			changed = true;
		}
		if (live !== undefined && open?.sub !== live.first) {
			conv.spans.push({ run: this.runIdOf(live.first), sub: live.first, from: live.firstEntry, to: null, status: "running" });
			changed = true;
		}
		conv.current = live === undefined ? null : { sub: live.first, run: this.runIdOf(live.first) };
		return changed;
	}
}

export type { ConversationId, SubmissionId };
