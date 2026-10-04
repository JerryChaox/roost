// E3: idempotent submission (incl. across restart), create-or-get by key, busy delivery, abort, reset, notes,
// entry cursors, listing, run state. Run as: node e3-interface.ts <phase> <db>
import { defineDoc, defineDocFamily, InboxDoc, LiveDoc, watchEvents } from "@earendil-works/pi-durable";
import { ANTHROPIC, allEntries, brief, ctx, openHost, out } from "./lib.ts";

const [phase, db] = process.argv.slice(2);
const { harness } = await openHost(db);

// Q8: a caller `key` per conversation, as a Session-scoped document family keyed by the caller key.
const KeyIndex = defineDocFamily<{ conversationId: number | null }, null>({
	kind: "roost.key",
	version: 1,
	scope: "session",
	family: true,
	initial: () => ({ conversationId: null }),
});
const Meta = defineDoc<{ key: string | null }>({
	kind: "roost.meta",
	version: 1,
	scope: "conversation",
	history: "latest",
	fork: "initial",
	initial: () => ({ key: null }),
});

async function createOrGet(key: string, settings: { model: typeof ANTHROPIC; instructions?: string }) {
	const id = await harness.commit(async (tx) => {
		const slot = await tx.doc(KeyIndex, key, null);
		if (slot.conversationId !== null) return slot.conversationId;
		const created = await tx.createConversation({ ownership: { kind: "ownerless" } });
		slot.conversationId = created.id;
		(await tx.doc(Meta, created.id)).key = key;
		return created.id;
	}, ctx);
	const conversation = (await harness.conversation(id as any, ctx))!;
	// configure() in a second commit; a real host would do it in the creating commit via configure(tx, ...).
	if ((await conversation.agent(ctx)).model === undefined) await conversation.configure(settings, ctx);
	return conversation;
}

const statusOf = async (id: number) => (await harness.submission(id as any, ctx))!.status(ctx);
const fakeRequests: string[] = [];

if (phase === "idem1") {
	const c = await createOrGet("slack:C123:1712.0000", { model: ANTHROPIC });
	const again = await createOrGet("slack:C123:1712.0000", { model: ANTHROPIC });
	out("KEY create-or-get", { first: c.id, second: again.id });
	out("KEY snapshot (non-creating read)", await harness.snapshot(KeyIndex, "slack:C123:1712.0000", ctx));
	out("KEY unknown", await harness.snapshot(KeyIndex, "nope", ctx));
	const a = await c.submit({ type: "input", content: "first", requestId: "m-1" }, ctx);
	const b = await c.submit({ type: "input", content: "first", requestId: "m-1" }, ctx);
	const d = await c.submit({ type: "input", content: "DIFFERENT content", requestId: "m-1" }, ctx);
	out("IDEM same process", { a: a.id, b: b.id, differentContent: d.id });
	out("IDEM status", await statusOf(a.id));
	try {
		await c.submit({ type: "write", entry: { kind: "roost.note", data: {} }, requestId: "m-1" }, ctx);
	} catch (error) {
		out("IDEM same requestId, other type", String(error));
	}
	await a.wait(ctx);
	out("IDEM settled", await statusOf(a.id));
	await harness.close(ctx);
} else if (phase === "idem2") {
	const c = await createOrGet("slack:C123:1712.0000", { model: ANTHROPIC });
	out("KEY after restart", c.id);
	const r = await c.submit({ type: "input", content: "first", requestId: "m-1" }, ctx);
	out("IDEM after restart", { id: r.id, status: await statusOf(r.id) });
	const n = await c.submit({ type: "input", content: "new", requestId: "m-2" }, ctx);
	await n.wait(ctx);
	for (const e of await allEntries(c)) console.log("   ", brief(e));
	await harness.close(ctx);
} else if (phase === "busy") {
	const c = await createOrGet("busy", { model: ANTHROPIC });
	const stream = await watchEvents(harness, c.id, ctx);
	const seen: string[] = [];
	const chatty: Record<string, number> = {};
	const samples: Record<string, string> = {};
	stream.start(async (events) => {
		for (const e of events) {
			if (e.type === "message_update" || e.type === "tool_execution_update") {
				// too chatty to print; count them and keep one sample of each
				chatty[e.type] = (chatty[e.type] ?? 0) + 1;
				samples[e.type] ??= JSON.stringify(e).slice(0, 220);
				continue;
			}
			seen.push(
				e.type === "submission"
					? `submission#${e.record.id}=${e.record.status}${(e.record as any).reason ? `(${(e.record as any).reason})` : ""}`
					: e.type === "inbox_update"
						? `inbox[${e.items.map((i) => `${i.id}:${i.mode}`).join(",")}]`
						: e.type === "entry_appended" || e.type === "message_end"
							? `${e.type}#${e.entry.id}:${e.entry.kind}`
							: e.type === "run_start" || e.type === "run_end"
								? `${e.type}(${e.inputs.join(",")})`
								: e.type,
			);
		}
	});
	const run = await c.submit({ type: "input", content: 'go [tool slow_tool {"seconds":2}]', requestId: "b-1" }, ctx);
	// wait until the tool runs
	while (!((await harness.snapshot(LiveDoc, c.id, ctx))?.tools ?? []).some((s) => s.status === "running"))
		await new Promise((r) => setTimeout(r, 50));
	const steer = await c.submit({ type: "input", content: "STEER-1", whenBusy: "steer", requestId: "b-2" }, ctx);
	const f1 = await c.submit({ type: "input", content: "FOLLOW-1", whenBusy: "followUp", requestId: "b-3" }, ctx);
	const f2 = await c.submit({ type: "input", content: "FOLLOW-2", requestId: "b-4" }, ctx);
	const note = await c.submit(
		{ type: "write", entry: { kind: "roost.note", data: { text: "note while busy" } }, requestId: "note-1" },
		ctx,
	);
	const noteAgain = await c.submit(
		{ type: "write", entry: { kind: "roost.note", data: { text: "note while busy" } }, requestId: "note-1" },
		ctx,
	);
	try {
		await c.submit({ type: "input", content: "REJECT", whenBusy: "reject" }, ctx);
	} catch (error) {
		out("BUSY reject", String(error));
	}
	out("BUSY statuses while running", {
		run: (await statusOf(run.id)).status,
		steer: (await statusOf(steer.id)).status,
		f1: (await statusOf(f1.id)).status,
		f2: (await statusOf(f2.id)).status,
		note: (await statusOf(note.id)).status,
		noteAgainSameId: noteAgain.id === note.id,
	});
	out("BUSY inbox", await harness.snapshot(InboxDoc, c.id, ctx));
	await harness.waitForIdle(ctx);
	for (const id of [run.id, steer.id, f1.id, f2.id, note.id]) out("BUSY settled", await statusOf(id));
	for (const e of await allEntries(c)) console.log("   ", brief(e));
	out("EVENTS", seen.join(" "));
	out("EVENTS live (not printed above)", chatty);
	for (const [k, v] of Object.entries(samples)) out(`EVENT sample ${k}`, v);
	await stream.stop();

	// Abort with queued items.
	const long = await c.submit({ type: "input", content: "long one [slow 40]", requestId: "a-1" }, ctx);
	while ((await harness.snapshot(LiveDoc, c.id, ctx))?.generation?.message === undefined)
		await new Promise((r) => setTimeout(r, 50));
	const qf = await c.submit({ type: "input", content: "QUEUED-FOLLOW", requestId: "a-2" }, ctx);
	const qs = await c.submit({ type: "input", content: "QUEUED-STEER", whenBusy: "steer", requestId: "a-3" }, ctx);
	const qn = await c.submit({ type: "write", entry: { kind: "roost.note", data: { text: "queued note" } }, requestId: "a-4" }, ctx);
	const t0 = Date.now();
	await c.abort(ctx);
	out("ABORT resolved after ms", Date.now() - t0);
	for (const id of [long.id, qf.id, qs.id, qn.id]) out("ABORT status", await statusOf(id));
	out("ABORT inbox", await harness.snapshot(InboxDoc, c.id, ctx));
	out("ABORT live", await harness.snapshot(LiveDoc, c.id, ctx));
	// The queued note is placed at the next boundary: the next submission.
	const after = await c.submit({ type: "input", content: "after abort", requestId: "a-5" }, ctx);
	await after.wait(ctx);
	out("ABORT note after next submit", await statusOf(qn.id));

	// Reset with handoff.
	await c.reset("We were fixing the flaky login test. Continue.", ctx);
	const post = await c.submit({ type: "input", content: "after reset", requestId: "r-1" }, ctx);
	await post.wait(ctx);
	const view = await c.context(ctx);
	out("RESET context head", view.head?.id);
	out("RESET model context", view.messages.map((m: any) => `${m.role}:${typeof m.content === "string" ? m.content : m.content.map((x: any) => x.text ?? x.type).join("")}`.slice(0, 60)));
	for (const e of await allEntries(c)) console.log("   ", brief(e));
	await harness.close(ctx);
} else if (phase === "cursor") {
	// Entry cursor semantics on a conversation that has entries (run after `busy` on the same db).
	const c = (await harness.conversation((await harness.snapshot(KeyIndex, "busy", ctx))!.conversationId as any, ctx))!;
	const all = await allEntries(c);
	out("CURSOR ids oldest-first", all.map((e) => e.id));
	const p1 = await c.entries({}, 3, undefined, ctx);
	out("CURSOR entries({},3) page1 (newest-first)", { ids: p1.items.map((e) => e.id), next: p1.next });
	const p2 = await c.entries({}, 3, p1.next, ctx);
	out("CURSOR entries({},3,next) page2", { ids: p2.items.map((e) => e.id), next: p2.next });
	const after = all[2].id;
	const fwd = await c.entries({ minEntryId: after + 1 }, 3, undefined, ctx);
	out(`CURSOR entries({minEntryId:${after + 1}},3) - wanted the 3 after #${after}`, { ids: fwd.items.map((e) => e.id), next: fwd.next });
	// Listing: no Harness list API; scan in a commit (as the experimental coding agent does).
	const list = await harness.commit((tx) => tx.scanConversations({}, 100, undefined), ctx);
	out("LIST scanConversations", list.items);
	for (const conv of list.items)
		out(`LIST c${conv.id}`, {
			key: (await harness.snapshot(Meta, conv.id, ctx))?.key ?? null,
			busy: (await harness.snapshot(LiveDoc, conv.id, ctx))?.run !== undefined,
		});
	await harness.close(ctx);
}
