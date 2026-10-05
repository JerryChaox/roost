// E4: what the host can report and control: run state, queues, progress, quiesce, close mid-tool.
import { Harness, InboxDoc, LiveDoc } from "@earendil-works/pi-durable";
import { ANTHROPIC, allEntries, brief, ctx, openHost, out } from "./lib.ts";

const [phase, db] = process.argv.slice(2);
const { harness } = await openHost(db);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Host-side progress clock: wall time of the last commit touching each conversation (in memory only).
const lastProgress = new Map<number, number>();
harness.subscribeCommits((pub) => {
	for (const c of pub.changes as any[]) {
		const conv = c.value?.conversationId ?? c.conversationId;
		if (conv !== undefined) lastProgress.set(conv, Date.now());
	}
});

async function hostState() {
	const list = await harness.commit((tx) => tx.scanConversations({}, 1000, undefined), ctx);
	const rows = [];
	for (const { id } of list.items) {
		const live = await harness.snapshot(LiveDoc, id, ctx);
		const inbox = await harness.snapshot(InboxDoc, id, ctx);
		rows.push({
			conversation: id,
			run: live?.run ?? null,
			generationAttempt: live?.generation?.attempt ?? null,
			tools: (live?.tools ?? []).map((t) => `${t.name}:${t.status}`),
			queued: (inbox?.items ?? []).map((i) => i.mode),
			lastProgressMsAgo: lastProgress.has(id) ? Date.now() - lastProgress.get(id)! : null,
		});
	}
	return rows;
}

if (phase === "state") {
	const a = await harness.createConversation({ ownership: { kind: "ownerless" }, agent: { model: ANTHROPIC } }, ctx);
	const b = await harness.createConversation({ ownership: { kind: "ownerless" }, agent: { model: ANTHROPIC } }, ctx);
	await a.submit({ type: "input", content: 'x [tool slow_tool {"seconds":2}]' }, ctx);
	await a.submit({ type: "input", content: "follow" }, ctx);
	await b.submit({ type: "input", content: "y [slow 6]" }, ctx);
	await sleep(800);
	out("HOST_STATE", await hostState());
	const ins = await harness.inspect(ctx);
	out("INSPECT.scheduling", ins.scheduling);
	for (const t of ins.tasks)
		out("INSPECT.task", { id: t.record.id, kind: t.record.kind, conv: t.record.conversationId, status: t.record.state.status, sched: t.state.kind, keys: Object.keys(t.record) });
	for (const s of ins.submissions) out("INSPECT.submission", s);
	const graph = await harness.taskGraph(ctx);
	out("TASK_GRAPH", graph.value);
	graph.dispose();
	await harness.waitForIdle(ctx);
	out("HOST_STATE idle", await hostState());
	// Harness API surface: is there anything like pause/quiesce?
	const proto = Object.getPrototypeOf(harness);
	const names = new Set<string>();
	for (let p = proto; p && p !== Object.prototype; p = Object.getPrototypeOf(p))
		for (const n of Object.getOwnPropertyNames(p)) if (n !== "constructor") names.add(n);
	out("HARNESS_METHODS", [...names].sort());
	await harness.close(ctx);
} else if (phase === "quiesce") {
	// The host "quiesces" by refusing new submissions. Already-queued follow-ups still start new runs.
	const a = await harness.createConversation({ ownership: { kind: "ownerless" }, agent: { model: ANTHROPIC } }, ctx);
	await a.submit({ type: "input", content: 'x [tool slow_tool {"seconds":1}]' }, ctx);
	await a.submit({ type: "input", content: "queued before quiesce" }, ctx);
	out("QUIESCE host stops admitting; state", await hostState());
	const runsStarted: string[] = [];
	harness.subscribeCommits((pub) => {
		for (const c of pub.changes as any[])
			if (c.type === "task" && c.value.kind === "pi.generation" && c.value.state.status === "pending" && c.value.state.checkpoint?.phase === "prepare")
				runsStarted.push(`gen#${c.value.id} input=${JSON.stringify(c.value.input)}`);
	});
	await harness.waitForIdle(ctx);
	out("QUIESCE generations created after quiesce", runsStarted);
	for (const e of await allEntries(a)) console.log("   ", brief(e));
	await harness.close(ctx);
} else if (phase === "close-mid-tool") {
	const a = await harness.root(ctx, { agent: { model: ANTHROPIC } });
	await a.submit({ type: "input", content: 'x [tool slow_tool {"seconds":5}]', requestId: "c-1" }, ctx);
	while (!((await harness.snapshot(LiveDoc, a.id, ctx))?.tools ?? []).some((s) => s.status === "running")) await sleep(50);
	await sleep(500);
	const t0 = Date.now();
	await harness.close(ctx);
	out("CLOSE took ms", Date.now() - t0);
} else if (phase === "after-close") {
	const ins = await harness.inspect(ctx);
	for (const t of ins.tasks) out("AFTER_CLOSE.task", { id: t.record.id, kind: t.record.kind, status: t.record.state.status, phase: (t.record.state as any).checkpoint?.phase });
	harness.resume();
	await harness.waitForIdle(ctx);
	for (const e of await allEntries((await harness.root(ctx))!)) console.log("   ", brief(e));
	await harness.close(ctx);
}
