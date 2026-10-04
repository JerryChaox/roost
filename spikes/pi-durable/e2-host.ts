// E2 host: `start` submits and keeps running until killed; `recover` reopens the same storage and continues.
import { LiveDoc } from "@earendil-works/pi-durable";
import { ANTHROPIC, allEntries, brief, ctx, openHost, out } from "./lib.ts";

const [mode, db, text] = process.argv.slice(2);
const { harness } = await openHost(db);

async function inspect(label: string) {
	const ins = await harness.inspect(ctx);
	out(`${label}.scheduling`, ins.scheduling);
	for (const t of ins.tasks)
		out(`${label}.task`, {
			id: t.record.id,
			kind: t.record.kind,
			conv: t.record.conversationId,
			status: t.record.state.status,
			phase: (t.record.state as any).checkpoint?.phase,
			sched: t.state.kind,
		});
	for (const s of ins.submissions) out(`${label}.submission`, s);
	out(`${label}.pi.live`, await harness.snapshot(LiveDoc, 1 as any, ctx));
}

if (mode === "start") {
	const root = await harness.root(ctx, { agent: { model: ANTHROPIC } });
	const sub = await root.submit({ type: "input", content: text, requestId: "req-kill" }, ctx);
	out("SUBMITTED", sub.id);
	await new Promise(() => {}); // until SIGKILL
} else if (mode === "peek") {
	// Open only: no resume(), no submit, no wait.
	await inspect("PEEK");
	await new Promise((r) => setTimeout(r, 2000));
	await inspect("PEEK_AFTER_2S");
	await harness.close(ctx);
} else if (mode === "recover") {
	await inspect("BEFORE_RESUME");
	const t0 = Date.now();
	harness.resume();
	await harness.waitForIdle(ctx);
	out("IDLE_AFTER_MS", Date.now() - t0);
	const root = (await harness.conversation(1 as any, ctx))!;
	for (const e of await allEntries(root)) console.log("   ", brief(e));
	const byReq = await root.commit((tx) => tx.submissionByRequest(root.id, "req-kill"), ctx);
	out("SUBMISSION", byReq);
	await inspect("AFTER");
	await harness.close(ctx);
}
